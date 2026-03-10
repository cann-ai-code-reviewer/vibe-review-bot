"""发布和管理 GitCode PR 评论。"""
import concurrent.futures
import io
import re
from datetime import datetime

from .config import AI_REVIEW_MARKER, AI_INLINE_MARKER, MAX_COMMENT_CHARS
from .models import RepoConfig, InlineFinding
from .terminal import _ok, _fail, _warn, _dim, _skip
from .gitcode import api_get, api_post, api_post_form, api_delete, get_filename, get_file_diff
from .terminal import _normalize_location_lines
from .claude import _extract_issue_summary
from .diff import _build_diff_position_map, _build_diff_line_content, _verify_and_correct_line


def _split_comment(text: str, max_chars: int = MAX_COMMENT_CHARS) -> list:
    """将过长的评论拆分为多条，优先在 '---' 分隔线处拆分。"""
    if len(text) <= max_chars:
        return [text]

    parts = []
    remaining = text

    while remaining:
        if len(remaining) <= max_chars:
            parts.append(remaining)
            break

        # 在 max_chars 范围内寻找最后一个 '---' 分隔线
        search_range = remaining[:max_chars]
        split_pos = search_range.rfind("\n---\n")

        if split_pos == -1 or split_pos < max_chars // 4:
            # 找不到合适的分隔线，在最后一个换行处拆分
            split_pos = search_range.rfind("\n")
            if split_pos == -1 or split_pos < max_chars // 4:
                split_pos = max_chars

        chunk = remaining[:split_pos].rstrip()
        remaining = remaining[split_pos:].lstrip("\n-").lstrip()
        parts.append(chunk)

    # 为多条评论添加序号
    if len(parts) > 1:
        parts = [f"**[{i + 1}/{len(parts)}]**\n\n{p}" for i, p in enumerate(parts)]

    return parts


def _fetch_all_pr_comments(repo: RepoConfig, token: str, pr_number: int) -> list:
    """翻页获取 PR 的所有评论（总结 + 行内）。"""
    all_comments = []
    page = 1
    per_page = 100
    while True:
        data = api_get(
            f"{repo.api_prefix}/pulls/{pr_number}/comments",
            token,
            {"per_page": per_page, "page": page},
        )
        if not data or not isinstance(data, list):
            break
        all_comments.extend(data)
        if len(data) < per_page:
            break
        page += 1
    return all_comments


def _is_already_reviewed(repo: RepoConfig, token: str, pr_number: int, head_sha: str) -> bool:
    """检查 PR 是否已基于最新提交审查过。

    扫描评论区的 AI 审查评论，查找隐藏标记 <!-- REVIEWED_SHA:xxx -->，
    若其中的 SHA 与当前 head_sha 一致，说明已审查过，返回 True。
    """
    comments = _fetch_all_pr_comments(repo, token, pr_number)
    for comment in comments:
        body = comment.get("body", "")
        is_ai_comment = (
            body.startswith(AI_REVIEW_MARKER)
            or (body.startswith("**[") and AI_REVIEW_MARKER in body[:200])
        )
        if not is_ai_comment:
            continue
        match = re.search(r"<!-- REVIEWED_SHA:(\w+) -->", body)
        if match and match.group(1) == head_sha:
            return True
    return False


def _get_last_review_info(repo: RepoConfig, token: str, pr_number: int
                          ) -> tuple[str, str] | None:
    """获取上次 AI 审查的 SHA 和时间。返回 (reviewed_sha, review_time) 或 None。"""
    comments = _fetch_all_pr_comments(repo, token, pr_number)
    for comment in reversed(comments):
        body = comment.get("body", "")
        is_ai_comment = (
            body.startswith(AI_REVIEW_MARKER)
            or (body.startswith("**[") and AI_REVIEW_MARKER in body[:200])
        )
        if not is_ai_comment:
            continue
        match = re.search(r"<!-- REVIEWED_SHA:(\w+) -->", body)
        if match:
            reviewed_sha = match.group(1)
            review_time = comment.get("updated_at") or comment.get("created_at", "")
            return reviewed_sha, review_time
    return None


def _get_head_commit_info(repo: RepoConfig, token: str, pr_number: int
                          ) -> tuple[str, str, str] | None:
    """获取 PR 最新 commit 的作者、时间、消息。返回 (author, date, message) 或 None。"""
    from .gitcode import _api_request
    data = _api_request("GET", f"/repos/{repo.full_name}/pulls/{pr_number}/commits",
                        token, params={"per_page": 1, "page": 1, "sort": "created", "direction": "desc"})
    if not data or not isinstance(data, list) or len(data) == 0:
        return None
    commit = data[-1]  # API 可能不支持 direction，取最后一个
    c = commit.get("commit", {})
    author_info = c.get("author", {})
    return (author_info.get("name", "?"),
            author_info.get("date", "?"),
            c.get("message", "").split("\n")[0][:80])


def delete_old_review_comments(repo: RepoConfig, token: str, pr_number: int) -> int:
    """删除 PR 中已有的 AI 评论（含总结评论和行内评论，并行删除）。返回删除数量。"""
    comments = _fetch_all_pr_comments(repo, token, pr_number)

    to_delete = []
    for comment in comments:
        body = comment.get("body", "")
        is_ai_comment = (
            body.startswith(AI_REVIEW_MARKER)
            or (body.startswith("**[") and AI_REVIEW_MARKER in body[:200])
            or AI_INLINE_MARKER in body
        )
        if is_ai_comment:
            comment_id = comment.get("id")
            if comment_id:
                to_delete.append(comment_id)

    if not to_delete:
        return 0

    deleted = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = {
            pool.submit(api_delete, f"{repo.api_prefix}/pulls/comments/{cid}", token): cid
            for cid in to_delete
        }
        for future in concurrent.futures.as_completed(futures):
            if future.result():
                deleted += 1

    return deleted


def _resolve_comment_url(resp: dict, repo: RepoConfig, token: str, pr_number: int) -> str | None:
    """从 POST 响应中构建评论永久链接。

    POST 返回的 id 是 discussion_id（hex 字符串），numeric note id
    需要从嵌套 notes 或回查 GET 接口获取。
    """
    did = str(resp["id"])
    base = f"{repo.url}/merge_requests/{pr_number}?ref=&did={did}"

    # 尝试从 POST 响应的嵌套 notes 中获取 numeric id
    notes = resp.get("notes")
    if isinstance(notes, list) and notes:
        nid = notes[0].get("id")
        if isinstance(nid, int):
            return f"{base}#tid-{nid}"

    # 回退：GET 最近的评论，按 discussion_id 匹配
    try:
        comments = api_get(
            f"{repo.api_prefix}/pulls/{pr_number}/comments",
            token,
            {"page": 1, "per_page": 5, "sort": "created", "direction": "desc"},
        )
        if isinstance(comments, list):
            for c in comments:
                if str(c.get("discussion_id", "")) == did:
                    return f"{base}#tid-{c['id']}"
    except Exception:
        pass

    return base


def post_review_comment(repo: RepoConfig, token: str, pr_number: int, pr_title: str, author: str,
                        review_text: str, skip_delete: bool = False,
                        head_sha: str = "") -> bool:
    """将审查结果发布为 PR 评论。先删除旧的 AI 评论，再发布新评论。返回是否成功。"""
    # 删除旧评论（inline 模式已提前删除，skip_delete=True 避免重复）
    if not skip_delete:
        deleted = delete_old_review_comments(repo, token, pr_number)
        if deleted > 0:
            print(f"  {_dim(f'已删除 {deleted} 条旧的 AI 审查评论')}")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    summary = _extract_issue_summary(review_text)
    summary_row = f"\n| 发现 | {summary} |" if summary else ""

    header = (
        f"{AI_REVIEW_MARKER}\n\n"
        f"| 属性 | 值 |\n"
        f"|------|------|\n"
        f"| 标题 | {pr_title} |\n"
        f"| 作者 | {author} |\n"
        f"| 链接 | [{repo.url}/merge_requests/{pr_number}]({repo.url}/merge_requests/{pr_number}) |\n"
        f"| 审查时间 | {now} |\n"
        f"| 审查工具 | Claude Code (`vibe-review` skill) |\n"
        f"| 基线提交 | {head_sha[:12]} |{summary_row}\n\n"
        f"---\n\n"
    )
    sha_tag = f"\n<!-- REVIEWED_SHA:{head_sha} -->" if head_sha else ""
    footer = (
        "\n\n---\n\n"
        "<sub>此评论由 AI 自动生成，仅供参考，请结合实际情况判断。</sub>"
        f"{sha_tag}"
    )

    review_text = _normalize_location_lines(review_text)
    full_text = header + review_text + footer
    parts = _split_comment(full_text)

    success = True
    for i, part in enumerate(parts):
        resp = api_post(
            f"{repo.api_prefix}/pulls/{pr_number}/comments",
            token,
            {"body": part},
        )
        if resp is None:
            print(f"  {_fail(f'第 {i + 1}/{len(parts)} 条评论发布失败')}")
            success = False
            break
        else:
            print(f"  {_ok(f'评论 {i + 1}/{len(parts)} 发布成功')}")
            if isinstance(resp, dict) and resp.get("id"):
                url = _resolve_comment_url(resp, repo, token, pr_number)
                print(f"  {_dim(url)}")

    return success


def _post_review_comment_quiet(
    repo: RepoConfig, token: str, pr_number: int, pr_title: str, author: str,
    review_text: str, buf: io.StringIO, head_sha: str = "",
) -> bool:
    """发布完整审查评论（不删除旧评论，日志输出到 buf）。供 inline 模式使用。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    summary = _extract_issue_summary(review_text)
    summary_row = f"\n| 发现 | {summary} |" if summary else ""

    header = (
        f"{AI_REVIEW_MARKER}\n\n"
        f"| 属性 | 值 |\n"
        f"|------|------|\n"
        f"| 标题 | {pr_title} |\n"
        f"| 作者 | {author} |\n"
        f"| 链接 | [{repo.url}/merge_requests/{pr_number}]({repo.url}/merge_requests/{pr_number}) |\n"
        f"| 审查时间 | {now} |\n"
        f"| 审查工具 | Claude Code (`vibe-review` skill) |\n"
        f"| 基线提交 | {head_sha[:12]} |{summary_row}\n\n"
        f"---\n\n"
    )
    sha_tag = f"\n<!-- REVIEWED_SHA:{head_sha} -->" if head_sha else ""
    footer = (
        "\n\n---\n\n"
        "<sub>此评论由 AI 自动生成，仅供参考，请结合实际情况判断。</sub>"
        f"{sha_tag}"
    )

    review_text = _normalize_location_lines(review_text)
    full_text = header + review_text + footer
    parts = _split_comment(full_text)

    success = True
    for i, part in enumerate(parts):
        resp = api_post(
            f"{repo.api_prefix}/pulls/{pr_number}/comments",
            token,
            {"body": part},
        )
        if resp is None:
            buf.write(f"  {_fail(f'第 {i + 1}/{len(parts)} 条评论发布失败')}\n")
            success = False
            break
        else:
            buf.write(f"  {_ok(f'评论 {i + 1}/{len(parts)} 发布成功')}\n")
            if isinstance(resp, dict) and resp.get("id"):
                url = _resolve_comment_url(resp, repo, token, pr_number)
                buf.write(f"  {_dim(url)}\n")

    return success


def _post_inline_comments(
    repo: RepoConfig, token: str, pr_number: int, commit_id: str,
    findings: list[InlineFinding],
    files: list[dict], buf,
    file_position_maps: dict[str, dict[int, tuple[int, bool]]] | None = None,
) -> tuple[int, list[InlineFinding]]:
    """发布行内评论（并行），返回 (成功数, 未能发布的 findings 列表)。

    GitCode API 要求使用 form-encoded 格式（非 JSON）来设置行内评论的
    path/position/commit_id 字段。position 参数为源码行号（非 diff 相对位置）。
    """
    # 构建 diff 中存在的文件名集合和行号映射（用于校验 finding 是否在 diff 内）
    if file_position_maps is None:
        file_position_maps = {}
        for f in files:
            fname = get_filename(f)
            raw_diff = get_file_diff(f)
            if raw_diff:
                file_position_maps[fname] = _build_diff_position_map(raw_diff)

    # 第一步：筛选可发布的 findings
    to_post: list[InlineFinding] = []
    unmapped: list[InlineFinding] = []

    for finding in findings:
        pos_map = file_position_maps.get(finding.file)
        if pos_map is None:
            buf.write(f"  {_skip(f'文件不在 diff 中：{finding.file}:{finding.line}')}\n")
            unmapped.append(finding)
            continue

        pos_info = pos_map.get(finding.line)
        if pos_info is None:
            buf.write(f"  {_skip(f'行号不在 diff 中：{finding.file}:{finding.line}')}\n")
            unmapped.append(finding)
            continue

        _position, is_added = pos_info
        if not is_added and finding.severity == "建议":
            buf.write(f"  {_skip(f'#{finding.id} [{finding.severity}] {finding.file}:{finding.line} (非新增行，跳过)')}\n")
            unmapped.append(finding)
            continue

        to_post.append(finding)

    if not to_post:
        return 0, unmapped

    # 第 1.5 步：校验并修正行号（用代码片段在 diff 中验证）
    file_content_maps: dict[str, dict[int, str]] = {}
    for f in files:
        fname = get_filename(f)
        raw_diff = get_file_diff(f)
        if raw_diff:
            file_content_maps[fname] = _build_diff_line_content(raw_diff)

    for finding in to_post:
        cm = file_content_maps.get(finding.file)
        if cm is None:
            continue
        corrected = _verify_and_correct_line(finding, cm)
        if corrected != finding.line:
            buf.write(f"  {_dim(f'#{finding.id} 行号修正：{finding.line}→{corrected}')}\n")
            finding.line = corrected

    # 第二步：并行发布行内评论（form-encoded，position=源码行号）
    def _post_one(finding: InlineFinding) -> tuple[InlineFinding, bool]:
        comment_body = (
            f"**[{finding.severity}]** {finding.title}\n\n"
            f"{finding.body}\n\n"
            f"{AI_INLINE_MARKER}"
        )
        resp = api_post_form(
            f"{repo.api_prefix}/pulls/{pr_number}/comments",
            token,
            {"body": comment_body, "commit_id": commit_id,
             "path": finding.file, "position": finding.line},
        )
        return finding, resp is not None

    posted_count = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(_post_one, f) for f in to_post]
        for future in concurrent.futures.as_completed(futures):
            finding, ok = future.result()
            if ok:
                posted_count += 1
            else:
                buf.write(f"  {_fail(f'API 发布失败：{finding.file}:{finding.line}')}\n")
                unmapped.append(finding)

    return posted_count, unmapped
