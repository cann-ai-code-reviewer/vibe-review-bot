"""审查结果追踪：SQLite 数据库、存活性检测、统计报告、导入历史日志。"""
import argparse
import re
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from .config import TRACKING_DB, LOG_DIR, AI_REVIEW_MARKER, AI_INLINE_MARKER
from .models import RepoConfig, ReviewStats
from .terminal import (
    _warn, _dim, _bold, _red, _yellow, _green, _blue, _cyan, _pad, _vw,
    _compact_line_numbers,
)
from .inline import _extract_code_snippet
from .comments import _fetch_all_pr_comments
from .gitcode import api_get


def _init_tracking_db() -> sqlite3.Connection:
    """初始化追踪数据库，返回连接。表不存在时自动创建。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(TRACKING_DB), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pr_number INTEGER NOT NULL,
            repo TEXT NOT NULL,
            pr_title TEXT,
            pr_author TEXT,
            head_sha TEXT NOT NULL,
            review_timestamp TEXT NOT NULL,
            review_round INTEGER DEFAULT 1,
            finding_count INTEGER,
            severity_summary TEXT,
            cost_usd REAL,
            duration_ms INTEGER,
            UNIQUE(pr_number, repo, head_sha)
        );
        CREATE TABLE IF NOT EXISTS findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            review_id INTEGER NOT NULL REFERENCES reviews(id),
            finding_index INTEGER NOT NULL,
            severity TEXT NOT NULL,
            title TEXT NOT NULL,
            file_path TEXT,
            line_numbers TEXT,
            rule TEXT,
            confidence TEXT,
            code_snippet TEXT,
            body TEXT,
            outcome TEXT,
            outcome_method TEXT,
            outcome_detail TEXT,
            outcome_sha TEXT,
            outcome_timestamp TEXT,
            UNIQUE(review_id, finding_index)
        );
    """)
    # 兼容旧数据库：添加 fix_snippet 列（如果不存在）
    try:
        conn.execute("ALTER TABLE findings ADD COLUMN fix_snippet TEXT")
    except sqlite3.OperationalError:
        pass  # 列已存在
    conn.commit()
    return conn


def _normalize_whitespace(s: str) -> str:
    """空白归一化：去除首尾空白、连续空白压缩为单空格。"""
    return re.sub(r"\s+", " ", s.strip())


def _extract_fix_snippet(finding_text: str) -> str | None:
    """从 finding 文本中提取"修复建议"部分的代码片段。

    匹配常见的修复建议格式：围栏代码块或缩进代码块，
    跟在"修复建议"/"建议修改"/"建议改为"等标题之后。
    """
    # 模式 1: "修复建议(...):" 后接围栏代码块
    m = re.search(
        r"(?:修复建议|建议修改|建议改为|建议修复|修改建议|Suggested fix)[^:：\n]*[：:]\s*\n```\w*\n(.*?)```",
        finding_text, re.DOTALL | re.IGNORECASE,
    )
    if m:
        lines = [l.strip() for l in m.group(1).split("\n") if l.strip()]
        if lines:
            return "\n".join(lines)

    # 模式 2: "修复建议(...):" 后接 4 空格缩进代码块
    m = re.search(
        r"(?:修复建议|建议修改|建议改为|建议修复|修改建议|Suggested fix)[^:：\n]*[：:]\s*\n\n?((?:    .+\n?)+)",
        finding_text, re.IGNORECASE,
    )
    if m:
        lines = [l.strip() for l in m.group(1).split("\n") if l.strip()]
        if lines:
            return "\n".join(lines)

    return None


def _extract_snippet_for_tracking(finding_text: str) -> str | None:
    """从 finding 文本提取问题代码核心行（用于存活性检测）。

    取问题代码块中所有有辨识度的行（用 \\n 连接），
    排除注释行、空行、省略号。跨多行 finding 保留多行以提高判定准确性。
    """
    lines = _extract_code_snippet(finding_text)
    if not lines:
        return None
    # 过滤掉省略号行、纯注释行
    filtered = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped in ("...", "…", "// ..."):
            continue
        if stripped.startswith("//") and len(stripped) < 10:
            continue
        # 排除冲突标记
        if stripped.startswith(("<<<<<<", "======", ">>>>>>")):
            continue
        # 排除 git diff 注解行（如 "\ No newline at end of file"）
        if stripped.startswith("\\") and "newline" in stripped.lower():
            continue
        # 排除低辨识度的 include guard / 通用单行关键字
        if stripped in ("#endif", "#else", "return;", "break;", "default:",
                        "continue;", "} else {", "};", "});"):
            continue
        if re.match(r"^#(?:ifndef|define)\s+\w+_H_?$", stripped):
            continue
        if len(stripped) >= 8:
            filtered.append(stripped)
    if not filtered:
        return None
    # 保留所有有效行，用换行连接
    return "\n".join(filtered)


def _extract_all_findings(review_text: str) -> list[dict]:
    """从审查报告中提取所有结构化 findings。

    返回 list[dict]，每个 dict 含：
    index, severity, title, file_path, line_numbers, rule, confidence,
    code_snippet, body
    """
    finding_pattern = r"### #(\d+)\s+\[([^\]]+)\]\s+(.*?)(?=\n---\s*$|\n### #\d|\Z)"
    matches = list(re.finditer(finding_pattern, review_text, re.DOTALL | re.MULTILINE))
    results = []
    for m in matches:
        idx = int(m.group(1))
        severity = m.group(2).strip()
        content = m.group(3).strip()
        title_line = content.split("\n")[0].strip()
        # 提取位置
        file_path = None
        line_numbers = None
        loc_m = re.search(r"位置[：:]\s*`([^`]+)`", content)
        if loc_m:
            loc_str = loc_m.group(1)
            # file.cc:123, 456
            fp_m = re.match(r"([^:]+):(.+)", loc_str)
            if fp_m:
                file_path = fp_m.group(1).strip()
                line_numbers = fp_m.group(2).strip()
            else:
                file_path = loc_str.strip()
        # 提取规则
        rule = None
        rule_m = re.search(r"规则[：:]\s*(.+?)(?:\n|$)", content)
        if rule_m:
            rule = rule_m.group(1).strip()
        # 提取置信度：只取核心标签（确定/较确定/待确认）
        confidence = None
        conf_m = re.search(r"置信度[：:]\s*\*{0,2}(确定|较确定|待确认)\*{0,2}", content)
        if conf_m:
            confidence = conf_m.group(1)
        # 提取代码片段
        snippet = _extract_snippet_for_tracking(content)
        results.append({
            "index": idx,
            "severity": severity,
            "title": title_line,
            "file_path": file_path,
            "line_numbers": line_numbers,
            "rule": rule,
            "confidence": confidence,
            "code_snippet": snippet,
            "body": content,
        })
    return results


def _save_review(
    conn: sqlite3.Connection, repo_name: str, pr_number: int, pr_title: str,
    pr_author: str, head_sha: str, stats: "ReviewStats", duration_ms: int,
    severity_summary: str, finding_count: int,
) -> int | None:
    """写入一条 review 记录，返回 review_id。若已存在返回 None。"""
    # 计算 review_round
    row = conn.execute(
        "SELECT COUNT(*) FROM reviews WHERE pr_number=? AND repo=?",
        (pr_number, repo_name),
    ).fetchone()
    review_round = (row[0] if row else 0) + 1
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        cur = conn.execute(
            """INSERT INTO reviews
               (pr_number, repo, pr_title, pr_author, head_sha, review_timestamp,
                review_round, finding_count, severity_summary, cost_usd, duration_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pr_number, repo_name, pr_title, pr_author, head_sha, now,
             review_round, finding_count, severity_summary,
             stats.best_cost if stats else 0, duration_ms),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        # UNIQUE(pr_number, repo, head_sha) 冲突 — 同一 SHA 已审查过
        return None


def _save_findings(conn: sqlite3.Connection, review_id: int, findings: list[dict]) -> int:
    """批量写入 findings，返回写入数量。"""
    saved = 0
    for f in findings:
        body = f.get("body", "")[:5000]
        fix_snippet = _extract_fix_snippet(body) if body else None
        try:
            conn.execute(
                """INSERT INTO findings
                   (review_id, finding_index, severity, title, file_path, line_numbers,
                    rule, confidence, code_snippet, body, fix_snippet)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (review_id, f["index"], f["severity"], f["title"],
                 f.get("file_path"), f.get("line_numbers"), f.get("rule"),
                 f.get("confidence"), f.get("code_snippet"),
                 body, fix_snippet),
            )
            saved += 1
        except sqlite3.IntegrityError:
            pass
    conn.commit()
    return saved


def _check_snippet_alive(repo_path: Path, sha: str, file_path: str, code_snippet: str) -> bool | None:
    """检查代码片段在指定 SHA 的文件中是否仍存在。

    支持多行 snippet（用 \\n 分隔）。逐行检测：
    - 全部消失 → False（已修复）
    - 全部存在 → True（未修复）
    - 部分消失 → False（视为已处理，至少动了）

    Returns:
        True  — 片段仍存在
        False — 片段消失（文件不存在或片段未找到）
        None  — 无法判断（snippet 无效或 git 命令失败）
    """
    if not code_snippet or not file_path or not sha:
        return None
    # 拆分多行 snippet
    snippet_lines = [s for s in code_snippet.split("\n") if s.strip()]
    if not snippet_lines:
        return None
    # 过滤太短的行（缺乏辨识度）
    valid_lines = [s for s in snippet_lines if len(_normalize_whitespace(s)) >= 8]
    if not valid_lines:
        return None
    try:
        result = subprocess.run(
            ["git", "show", f"{sha}:{file_path}"],
            capture_output=True, text=True, timeout=10,
            cwd=str(repo_path),
        )
        if result.returncode != 0:
            if "does not exist" in result.stderr or "not exist" in result.stderr:
                return False
            return None
        file_lines = {_normalize_whitespace(line) for line in result.stdout.splitlines() if line.strip()}
        alive_count = sum(1 for s in valid_lines if _normalize_whitespace(s) in file_lines)
        survival_rate = alive_count / len(valid_lines)
        if survival_rate > 0.5:
            return True   # 多数行仍存在
        return False      # 多数行已消失 → 已处理
    except (subprocess.TimeoutExpired, OSError):
        return None


def _check_fix_snippet_present(repo_path: Path, sha: str, file_path: str, fix_snippet: str) -> bool | None:
    """检查修复代码片段是否出现在指定 SHA 的文件中。

    Returns:
        True  — 修复代码存在
        False — 修复代码未出现
        None  — 无法判断
    """
    if not fix_snippet or not file_path or not sha:
        return None
    snippet_lines = [s for s in fix_snippet.split("\n") if s.strip()]
    if not snippet_lines:
        return None
    valid_lines = [s for s in snippet_lines if len(_normalize_whitespace(s)) >= 8]
    if not valid_lines:
        return None
    try:
        result = subprocess.run(
            ["git", "show", f"{sha}:{file_path}"],
            capture_output=True, text=True, timeout=10,
            cwd=str(repo_path),
        )
        if result.returncode != 0:
            return None
        file_lines = {_normalize_whitespace(line) for line in result.stdout.splitlines() if line.strip()}
        # 至少半数修复行出现即视为修复代码存在
        match_count = sum(1 for s in valid_lines if _normalize_whitespace(s) in file_lines)
        return match_count >= max(1, len(valid_lines) // 2)
    except (subprocess.TimeoutExpired, OSError):
        return None


def _check_finding_status(
    repo_path: Path, new_sha: str, file_path: str,
    code_snippet: str, fix_snippet: str | None = None,
) -> str | None:
    """判定 finding 状态（三级判定）。

    Returns:
        'addressed'    — 问题代码消失 且 修复代码出现（或无 fix_snippet 可比较）
        'coincidental' — 问题代码消失 但 修复代码未出现（巧合变更）
        'alive'        — 问题代码仍在
        None           — 无法判断
    """
    alive = _check_snippet_alive(repo_path, new_sha, file_path, code_snippet)
    if alive is None:
        return None
    if alive is True:
        return "alive"
    # 问题代码已消失，进一步判断是否真正采纳
    if fix_snippet:
        fix_present = _check_fix_snippet_present(repo_path, new_sha, file_path, fix_snippet)
        if fix_present is True:
            return "addressed"
        if fix_present is False:
            return "coincidental"
        # fix_present is None → 无法判断修复代码，保守标为 addressed
    return "addressed"


def _track_outcomes(
    conn: sqlite3.Connection, repo_path: Path, repo_name: str,
    pr_number: int, new_sha: str, log=print,
) -> int:
    """对该 PR 的 pending findings 做存活性检测，返回更新数。"""
    rows = conn.execute(
        """SELECT f.id, f.code_snippet, f.file_path, f.fix_snippet
           FROM findings f
           JOIN reviews r ON f.review_id = r.id
           WHERE r.pr_number = ? AND r.repo = ? AND f.outcome IS NULL
                 AND f.code_snippet IS NOT NULL""",
        (pr_number, repo_name),
    ).fetchall()
    if not rows:
        return 0

    updated = 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for fid, snippet, fpath, fix_snip in rows:
        status = _check_finding_status(repo_path, new_sha, fpath, snippet, fix_snip)
        if status == "addressed":
            conn.execute(
                """UPDATE findings SET outcome='addressed', outcome_method='snippet_search',
                   outcome_sha=?, outcome_timestamp=? WHERE id=?""",
                (new_sha, now, fid),
            )
            updated += 1
        elif status == "coincidental":
            conn.execute(
                """UPDATE findings SET outcome='indeterminate', outcome_method='snippet_search',
                   outcome_detail='coincidental_change',
                   outcome_sha=?, outcome_timestamp=? WHERE id=?""",
                (new_sha, now, fid),
            )
            updated += 1
        # status is None → 保持 NULL，留给下一次追踪或最终判定
        # status == 'alive' → 保持 pending，等最终判定
    conn.commit()
    if updated:
        log(f"  追踪结果：{updated}/{len(rows)} 个旧发现已更新状态")
    return updated


# 开发者回复关键词分类
_REPLY_POSITIVE = re.compile(
    r"已修复|已改|已修改|fixed|done|好的|改了|修改了|已处理|已解决|感谢指出",
    re.IGNORECASE,
)
_REPLY_NEGATIVE = re.compile(
    r"不是问题|误报|false.?positive|无需修改|by.?design|设计如此|不需要改|不用改",
    re.IGNORECASE,
)
_REPLY_DEFERRED = re.compile(
    r"下个版本|后续处理|TODO|单独提.?PR|后续.?PR|后面再|稍后|postpone|later",
    re.IGNORECASE,
)


def _classify_reply(text: str) -> str | None:
    """对开发者回复做关键词分类。返回 positive/negative/deferred/None。"""
    if _REPLY_POSITIVE.search(text):
        return "positive"
    if _REPLY_NEGATIVE.search(text):
        return "negative"
    if _REPLY_DEFERRED.search(text):
        return "deferred"
    return None


def _harvest_replies(
    conn: sqlite3.Connection, repo: "RepoConfig", token: str,
    pr_number: int, repo_name: str, log=print,
) -> int:
    """扫描 PR 评论中的开发者回复，关联到 AI findings。返回采集数。"""
    if not token:
        return 0
    # 查看是否有该 PR 的 findings
    review_rows = conn.execute(
        "SELECT id FROM reviews WHERE pr_number=? AND repo=?",
        (pr_number, repo_name),
    ).fetchall()
    if not review_rows:
        return 0

    comments = _fetch_all_pr_comments(repo, token, pr_number)
    if not comments:
        return 0

    # 分离 AI 评论和非 AI 评论
    ai_comments = []
    human_comments = []
    for c in comments:
        body = c.get("body", "")
        is_ai = (
            body.startswith(AI_REVIEW_MARKER)
            or (body.startswith("**[") and AI_REVIEW_MARKER in body[:200])
            or AI_INLINE_MARKER in body
        )
        if is_ai:
            ai_comments.append(c)
        else:
            human_comments.append(c)

    if not human_comments:
        return 0

    # 收集 AI 评论所在的 discussion_id，只采集同线程的回复
    ai_discussion_ids = {c.get("discussion_id") for c in ai_comments if c.get("discussion_id")}
    # AI 行内评论可能嵌入了 finding 编号: <!-- AI_FINDING:3 -->
    ai_finding_map: dict[str, int] = {}  # discussion_id → finding_index
    for ac in ai_comments:
        did = ac.get("discussion_id", "")
        body = ac.get("body", "")
        m = re.search(r"<!-- AI_FINDING:(\d+) -->", body)
        if m and did:
            ai_finding_map[did] = int(m.group(1))

    harvested = 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for hc in human_comments:
        # 只处理和 AI 评论在同一 discussion 线程里的回复
        hc_did = hc.get("discussion_id", "")
        if hc_did not in ai_discussion_ids:
            continue

        reply_body = hc.get("body", "")
        classification = _classify_reply(reply_body)
        if not classification:
            continue

        latest_review = conn.execute(
            """SELECT id FROM reviews WHERE pr_number=? AND repo=?
               ORDER BY review_timestamp DESC LIMIT 1""",
            (pr_number, repo_name),
        ).fetchone()
        if not latest_review:
            continue

        review_id = latest_review[0]
        detail = f"[{classification}] {reply_body[:200]}"

        # 优先通过 AI_FINDING 标记精确关联到具体 finding
        target_idx = ai_finding_map.get(hc_did)
        # 根据分类决定 outcome 更新（仅在 outcome IS NULL 时设置，不覆盖 snippet_search 结果）
        outcome_val = None
        if classification == "positive":
            outcome_val = "addressed"
        elif classification == "negative":
            outcome_val = "persisted"

        if target_idx is not None:
            row = conn.execute(
                """SELECT id, outcome FROM findings
                   WHERE review_id=? AND finding_index=? AND outcome_detail IS NULL""",
                (review_id, target_idx),
            ).fetchone()
            if row:
                fid, existing_outcome = row
                if outcome_val and existing_outcome is None:
                    conn.execute(
                        """UPDATE findings SET outcome=?, outcome_method='developer_reply',
                           outcome_detail=?, outcome_timestamp=? WHERE id=?""",
                        (outcome_val, detail, now, fid),
                    )
                else:
                    conn.execute(
                        "UPDATE findings SET outcome_detail=?, outcome_timestamp=? WHERE id=?",
                        (detail, now, fid),
                    )
                harvested += 1
                continue

        # 回退：总结评论下的回复，标记为 PR 级别反馈
        row = conn.execute(
            """SELECT id, outcome FROM findings
               WHERE review_id=? AND outcome_detail IS NULL
               ORDER BY finding_index LIMIT 1""",
            (review_id,),
        ).fetchone()
        if row:
            fid, existing_outcome = row
            detail = f"[{classification}:summary] {reply_body[:200]}"
            if outcome_val and existing_outcome is None:
                conn.execute(
                    """UPDATE findings SET outcome=?, outcome_method='developer_reply',
                       outcome_detail=?, outcome_timestamp=? WHERE id=?""",
                    (outcome_val, detail, now, fid),
                )
            else:
                conn.execute(
                    "UPDATE findings SET outcome_detail=?, outcome_timestamp=? WHERE id=?",
                    (detail, now, fid),
                )
            harvested += 1

    conn.commit()
    if harvested:
        log(f"  采集到 {harvested} 条开发者回复")
    return harvested


def _finalize_outcomes(
    conn: sqlite3.Connection, repo_path: Path, repo_name: str,
    pr_number: int, final_sha: str, log=print,
) -> int:
    """对已合并 PR 做最终结果判定：仍 pending 的 findings → persisted。"""
    rows = conn.execute(
        """SELECT f.id, f.code_snippet, f.file_path, f.fix_snippet
           FROM findings f
           JOIN reviews r ON f.review_id = r.id
           WHERE r.pr_number = ? AND r.repo = ? AND f.outcome IS NULL""",
        (pr_number, repo_name),
    ).fetchall()
    if not rows:
        return 0

    updated = 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for fid, snippet, fpath, fix_snip in rows:
        if snippet and fpath:
            status = _check_finding_status(repo_path, final_sha, fpath, snippet, fix_snip)
            if status == "addressed":
                conn.execute(
                    """UPDATE findings SET outcome='addressed', outcome_method='snippet_search',
                       outcome_sha=?, outcome_timestamp=? WHERE id=?""",
                    (final_sha, now, fid),
                )
                updated += 1
                continue
            if status == "coincidental":
                conn.execute(
                    """UPDATE findings SET outcome='indeterminate', outcome_method='snippet_search',
                       outcome_detail='coincidental_change',
                       outcome_sha=?, outcome_timestamp=? WHERE id=?""",
                    (final_sha, now, fid),
                )
                updated += 1
                continue
        # snippet 仍存在 或 无法判断 → persisted（最终判定）
        if snippet:
            conn.execute(
                """UPDATE findings SET outcome='persisted', outcome_method='snippet_search',
                   outcome_sha=?, outcome_timestamp=? WHERE id=?""",
                (final_sha, now, fid),
            )
        else:
            conn.execute(
                """UPDATE findings SET outcome='indeterminate', outcome_method='snippet_search',
                   outcome_sha=?, outcome_timestamp=? WHERE id=?""",
                (final_sha, now, fid),
            )
        updated += 1
    conn.commit()
    if updated:
        log(f"  最终判定：{updated} 个 findings 已分类")
    return updated


_SEV_ICON = {"严重": _red("●"), "一般": _yellow("●"), "建议": _cyan("○")}
_OUTCOME_LABEL = {
    "addressed": _green("已采纳"),
    "persisted": _red("未采纳"),
    "indeterminate": _dim("不确定"),
}


def _print_stats_for_repo(
    conn: "sqlite3.Connection", repo_name: str | None,
    start_str: str, end_str: str, title: str | None = None,
) -> None:
    """输出单个仓库（或全部仓库汇总）的统计。repo_name=None 表示不做 repo 过滤。"""
    where = "WHERE r.review_timestamp >= ?"
    params: list = [start_str]
    if repo_name:
        where += " AND r.repo = ?"
        params.append(repo_name)

    row = conn.execute(
        f"SELECT COUNT(DISTINCT r.id), COUNT(DISTINCT r.pr_number), SUM(r.review_round) "
        f"FROM reviews r {where}", params,
    ).fetchone()
    total_prs, total_rounds = row[1] or 0, row[2] or 0

    row = conn.execute(
        f"SELECT COUNT(*) FROM findings f JOIN reviews r ON f.review_id=r.id {where}",
        params,
    ).fetchone()
    total_findings = row[0] or 0

    if total_findings == 0:
        label = title or repo_name or "全部"
        print(f"  {_warn(f'{label}: 在 {start_str} ~ {end_str} 期间没有找到审查数据')}")
        return

    dist = conn.execute(
        f"""SELECT
            SUM(CASE WHEN f.outcome='addressed' THEN 1 ELSE 0 END),
            SUM(CASE WHEN f.outcome='persisted' THEN 1 ELSE 0 END),
            SUM(CASE WHEN f.outcome='indeterminate' THEN 1 ELSE 0 END),
            SUM(CASE WHEN f.outcome IS NULL THEN 1 ELSE 0 END)
           FROM findings f JOIN reviews r ON f.review_id=r.id {where}""",
        params,
    ).fetchone()
    addressed, persisted = dist[0] or 0, dist[1] or 0
    indeterminate, untracked = dist[2] or 0, dist[3] or 0

    _pct = lambda n: f"{n/total_findings*100:.1f}%" if total_findings else "0%"
    heading = title or repo_name or "全部"

    print()
    print(f"  {_bold(heading)} {_dim(f'({start_str} ~ {end_str})')}")
    print(f"  {'─' * 50}")
    print(f"  审查 PR: {_bold(str(total_prs))}  |  发现: {_bold(str(total_findings))}  |  轮次: {_bold(str(total_rounds))}")
    print()

    # 结果分布
    print(f"  结果分布")
    print(f"    {_green('已采纳')}    {addressed:>4}  {_dim(_pct(addressed)):>6}")
    print(f"    {_red('未采纳')}    {persisted:>4}  {_dim(_pct(persisted)):>6}")
    print(f"    {_yellow('不确定')}    {indeterminate:>4}  {_dim(_pct(indeterminate)):>6}")
    print(f"    {_dim('未追踪')}    {untracked:>4}  {_dim(_pct(untracked)):>6}")

    # 按严重级别 / 置信度（只显示有已结案数据的行）
    _sev_color = {"严重": _red, "一般": _yellow, "建议": _cyan}
    _conf_color = {"确定": _green, "较确定": _blue, "待确认": _dim}
    for group_label, col_name, items, color_map in [
        ("按严重级别", "severity", [("严重", 2), ("一般", 2), ("建议", 2)], _sev_color),
        ("按置信度",   "confidence", [("确定", 4), ("较确定", 2), ("待确认", 2)], _conf_color),
    ]:
        group_rows = []
        for val, pad_width in items:
            row = conn.execute(
                f"""SELECT
                    SUM(CASE WHEN f.outcome='addressed' THEN 1 ELSE 0 END),
                    COUNT(*)
                   FROM findings f JOIN reviews r ON f.review_id=r.id
                   {where} AND f.{col_name}=?""",
                params + [val],
            ).fetchone()
            addr, total = row[0] or 0, row[1] or 0
            if total > 0:
                rate = f"{addr/total*100:.0f}%"
                padding = " " * pad_width
                color_fn = color_map.get(val, lambda x: x)
                group_rows.append(f"    {color_fn(val)}{padding}{rate:>5}  {_dim(f'({addr}/{total})')}")
        if group_rows:
            print()
            print(f"  {group_label}")
            for line in group_rows:
                print(line)

    # 按规则 — 采纳率最低/最高 Top 5
    for label, order, show_suggestion in [
        ("采纳率最低 Top 5 (候选降权)", "ASC", True),
        ("采纳率最高 Top 5 (高价值)", "DESC", False),
    ]:
        rows = conn.execute(
            f"""SELECT f.rule,
                COUNT(*) as total,
                SUM(CASE WHEN f.outcome='addressed' THEN 1 ELSE 0 END) as addr
               FROM findings f JOIN reviews r ON f.review_id=r.id
               {where} AND f.rule IS NOT NULL
               GROUP BY f.rule HAVING total >= 3
               ORDER BY CAST(addr AS REAL)/total {order} LIMIT 5""",
            params,
        ).fetchall()
        if not rows:
            continue
        col_w = max(_vw(rule or "?") for rule, _, _ in rows) + 2
        print()
        print(f"  {label}")
        for rule, total, addr in rows:
            rate = f"{addr/total*100:.0f}%" if total > 0 else "  -"
            tag = _red("  ← 降权") if show_suggestion and total > 0 and addr / total < 0.3 else ""
            print(f"    {_pad(_dim(rule or '?'), col_w)} {total:>3}  {rate:>5}{tag}")

    print()


def _print_findings_detail(
    conn: "sqlite3.Connection", repo_name: str, start_str: str,
) -> None:
    """按 PR 分组列出每条 finding 的明细。"""
    rows = conn.execute(
        """SELECT r.pr_number, r.pr_title, f.finding_index, f.severity,
                  f.title, f.file_path, f.line_numbers, f.outcome, f.outcome_detail,
                  f.outcome_sha
           FROM findings f JOIN reviews r ON f.review_id=r.id
           WHERE r.review_timestamp >= ? AND r.repo = ?
           ORDER BY r.pr_number, f.finding_index""",
        (start_str, repo_name),
    ).fetchall()
    if not rows:
        return

    print(f"  检视意见明细")
    print(f"  {'─' * 50}")

    cur_pr = None
    for pr_num, pr_title, idx, sev, title, fpath, lines, outcome, detail, outcome_sha in rows:
        if pr_num != cur_pr:
            cur_pr = pr_num
            print(f"  PR #{pr_num} {_dim(pr_title or '')}")

        icon = _SEV_ICON.get(sev, "·")
        outcome_str = _OUTCOME_LABEL.get(outcome, _dim("待定")) if outcome else _dim("待定")

        # 位置信息
        loc = ""
        if fpath:
            short = fpath.rsplit("/", 1)[-1] if "/" in fpath else fpath
            loc = f"{short}"
            if lines:
                loc += f":{_compact_line_numbers(lines)}"

        # 位置 + 状态放一行紧凑显示
        status_parts = [outcome_str]
        if outcome_sha:
            status_parts.append(_dim(outcome_sha[:8]))
        if loc:
            status_parts.append(_dim(loc))
        print(f"    {icon} #{idx} [{sev}] {title}")
        print(f"      {' · '.join(status_parts)}")
        if detail:
            # 提取分类标签和回复摘要分开显示
            m = re.match(r'\[(\w+)\]\s*(.*)', detail, re.DOTALL)
            if m:
                reply_tag = m.group(1)
                reply_text = m.group(2).split("\n")[0][:60].strip()
                tag_label = {"positive": _green("采纳"), "negative": _red("拒绝"),
                             "deferred": _yellow("延后")}.get(reply_tag, reply_tag)
                print(f"      开发者: {tag_label} {_dim(reply_text) if reply_text else ''}")

    print()


def _main_stats(args: argparse.Namespace, repo_name: str | None) -> None:
    """输出采纳率统计报告。repo_name=None 时显示各仓库分别统计 + 汇总。"""
    if not TRACKING_DB.exists():
        print(f"  {_warn('追踪数据库不存在，请先运行审查或 --import-logs')}")
        return

    conn = sqlite3.connect(str(TRACKING_DB))
    days = getattr(args, "days", 30)

    end_date = datetime.now()
    start_date = end_date - timedelta(days=days)
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")

    detail = getattr(args, "detail", False)

    if repo_name:
        _print_stats_for_repo(conn, repo_name, start_str, end_str)
        if detail:
            _print_findings_detail(conn, repo_name, start_str)
    else:
        repos = [r[0] for r in conn.execute(
            "SELECT DISTINCT repo FROM reviews WHERE review_timestamp >= ? ORDER BY repo",
            (start_str,),
        ).fetchall()]
        if not repos:
            print(f"  {_warn(f'在 {start_str} ~ {end_str} 期间没有找到审查数据')}")
            conn.close()
            return
        for rn in repos:
            _print_stats_for_repo(conn, rn, start_str, end_str)
            if detail:
                _print_findings_detail(conn, rn, start_str)
        if len(repos) > 1:
            print(f"  {'━' * 50}")
            _print_stats_for_repo(conn, None, start_str, end_str, title="汇总")

    conn.close()


def _main_track(repo: "RepoConfig", args: argparse.Namespace, token: str) -> None:
    """手动触发结果追踪：对已合并 PR 做最终分类。"""
    conn = _init_tracking_db()
    repo_name = repo.full_name

    # 获取需要追踪的 PR（有 pending findings 的）
    if getattr(args, "pr", None):
        pr_numbers = args.pr
    else:
        rows = conn.execute(
            """SELECT DISTINCT r.pr_number FROM findings f
               JOIN reviews r ON f.review_id=r.id
               WHERE r.repo=? AND f.outcome IS NULL""",
            (repo_name,),
        ).fetchall()
        pr_numbers = [r[0] for r in rows]

    if not pr_numbers:
        print(f"  {_dim('没有需要追踪的 pending findings')}")
        conn.close()
        return

    print(f"追踪 {len(pr_numbers)} 个 PR 的审查结果")
    total_updated = 0
    for pr_num in pr_numbers:
        # 获取 PR 状态
        try:
            pr_data = api_get(f"{repo.api_prefix}/pulls/{pr_num}", token)
            state = pr_data.get("state", "unknown") if pr_data else "unknown"
            head_sha = pr_data.get("head", {}).get("sha", "") if pr_data else ""
        except Exception:
            print(f"  PR #{pr_num}: 获取状态失败，跳过")
            continue

        print(f"  PR #{pr_num}: 状态={state}")
        if state == "merged":
            # 先采集开发者回复，让 positive/negative 参与判定
            _harvest_replies(conn, repo, token, pr_num, repo_name)
            # 用 head_sha 做最终判定
            if head_sha:
                # 尝试 fetch
                subprocess.run(
                    ["git", "fetch", "origin", head_sha],
                    capture_output=True, timeout=30, cwd=str(repo.path),
                )
            n = _finalize_outcomes(conn, repo.path, repo_name, pr_num, head_sha)
            total_updated += n
        elif state == "closed":
            # closed 未合并 → indeterminate
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            n = conn.execute(
                """UPDATE findings SET outcome='indeterminate',
                   outcome_method='pr_closed', outcome_timestamp=?
                   WHERE outcome IS NULL AND review_id IN
                   (SELECT id FROM reviews WHERE pr_number=? AND repo=?)""",
                (now, pr_num, repo_name),
            ).rowcount
            conn.commit()
            total_updated += n
            if n:
                print(f"    {n} 个 findings 标记为 indeterminate (PR closed)")
        elif state == "open" and head_sha:
            # 先采集开发者回复
            _harvest_replies(conn, repo, token, pr_num, repo_name)
            # open PR：做中间追踪
            subprocess.run(
                ["git", "fetch", "origin", head_sha],
                capture_output=True, timeout=30, cwd=str(repo.path),
            )
            n = _track_outcomes(conn, repo.path, repo_name, pr_num, head_sha)
            total_updated += n

    print(f"\n追踪完成：共更新 {total_updated} 个 findings")
    conn.close()


def _main_import_logs(repo: "RepoConfig", args: argparse.Namespace) -> None:
    """从日志目录导入历史审查数据。

    兼容两种目录结构：
    - 旧格式：by_pr/pr_<number>_review.md
    - 新格式：pr_<number>/<sha>.md
    """
    conn = _init_tracking_db()
    repo_name = repo.full_name
    log_dir = repo.pr_log_dir

    if not log_dir.exists():
        print(f"  {_warn(f'日志目录不存在：{log_dir}')}")
        conn.close()
        return

    # 兼容旧格式（by_pr/pr_*_review.md）和新格式（pr_*//*.md）
    review_files = sorted(log_dir.glob("pr_*_review.md"))
    old_by_pr = log_dir / "by_pr"
    if old_by_pr.exists():
        review_files.extend(sorted(old_by_pr.glob("pr_*_review.md")))
    review_files.extend(sorted(log_dir.glob("pr_*/*.md")))
    if not review_files:
        print(f"  {_warn('未找到审查日志文件')}")
        conn.close()
        return

    print(f"导入 {len(review_files)} 个历史审查报告")
    imported_reviews = 0
    imported_findings = 0
    skipped = 0

    for fpath in review_files:
        # 从文件路径提取 PR 编号（兼容旧格式 pr_696_review.md 和新格式 pr_696/sha.md）
        m = re.search(r"pr_(\d+)", str(fpath))
        if not m:
            continue
        pr_number = int(m.group(1))

        content = fpath.read_text(encoding="utf-8")

        # 解析元数据表
        pr_title = ""
        pr_author = ""
        head_sha = ""
        review_timestamp = ""
        severity_summary = ""

        title_m = re.search(r"\|\s*标题\s*\|\s*(.+?)\s*\|", content)
        if title_m:
            pr_title = title_m.group(1).strip()
        author_m = re.search(r"\|\s*作者\s*\|\s*(.+?)\s*\|", content)
        if author_m:
            pr_author = author_m.group(1).strip()
        sha_m = re.search(r"\|\s*基线提交\s*\|\s*(\w+)\s*\|", content)
        if sha_m:
            head_sha = sha_m.group(1).strip()
        time_m = re.search(r"\|\s*审查时间\s*\|\s*(.+?)\s*\|", content)
        if time_m:
            review_timestamp = time_m.group(1).strip()
        summary_m = re.search(r"(严重\s*\d+\s*/\s*一般\s*\d+(?:\s*/\s*建议\s*\d+)?)", content)
        if summary_m:
            severity_summary = summary_m.group(1).strip()

        if not head_sha:
            head_sha = f"imported_{pr_number}"
        if not review_timestamp:
            # 从文件修改时间获取
            mtime = fpath.stat().st_mtime
            review_timestamp = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")

        # 解析 findings
        findings = _extract_all_findings(content)

        # 写入 review
        review_round = conn.execute(
            "SELECT COUNT(*) FROM reviews WHERE pr_number=? AND repo=?",
            (pr_number, repo_name),
        ).fetchone()[0] + 1

        try:
            cur = conn.execute(
                """INSERT INTO reviews
                   (pr_number, repo, pr_title, pr_author, head_sha, review_timestamp,
                    review_round, finding_count, severity_summary, cost_usd, duration_ms)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)""",
                (pr_number, repo_name, pr_title, pr_author, head_sha, review_timestamp,
                 review_round, len(findings), severity_summary),
            )
            review_id = cur.lastrowid
            imported_reviews += 1
        except sqlite3.IntegrityError:
            skipped += 1
            continue

        # 写入 findings
        n = _save_findings(conn, review_id, findings)
        imported_findings += n
        snippet_count = sum(1 for f in findings if f.get("code_snippet"))
        print(f"  PR #{pr_number}: {n} 个 findings (snippet: {snippet_count}/{n})")

    conn.close()
    print(f"\n导入完成：{imported_reviews} 个审查, {imported_findings} 个 findings"
          f"{f', 跳过 {skipped} 个重复' if skipped else ''}")
