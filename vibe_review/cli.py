"""命令行入口：PR 审查、本地文件审查、统计等主流程。"""
import argparse
import concurrent.futures
import io
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

from . import config as _config
from .config import (
    DEFAULT_MODEL, LOG_DIR, OWNER, REPOS_ROOT, SCRIPT_DIR,
    SKILL_MD_PATH, MAX_DIR_FILES, MAX_PARALLEL_REVIEWS, MIN_REVIEW_CHARS,
    USD_TO_CNY, TEAM_FILE,
)
from .models import RepoConfig, PRResult, ReviewStats, _DirectOutput
from .terminal import (
    _bold, _dim, _ok, _fail, _warn, _skip,
    _red, _green, _yellow, _cyan, _fmt_secs, _file_link, _now,
)
from .gitcode import fetch_pr_files, collect_prs, load_team_members, get_filename, get_file_diff
from .diff import is_cpp_file, format_diff_for_review, _build_diff_position_map
from .inline import _extract_findings_for_inline
from .claude import (
    run_claude_review, run_claude_file_review, run_claude_dir_review,
    write_review_md, write_file_review_md, write_dir_review_md,
    _extract_issue_summary,
)
from .comments import (
    delete_old_review_comments, post_review_comment, _post_review_comment_quiet,
    _post_inline_comments, _is_already_reviewed, _get_last_review_info,
    _get_head_commit_info,
)
from .tracking import (
    _init_tracking_db, _track_outcomes, _save_review, _save_findings,
    _harvest_replies, _extract_all_findings, _main_stats, _main_track,
    _main_import_logs,
)


def _migrate_legacy_logs(repo: RepoConfig) -> None:
    """一次性迁移旧的扁平 log 目录到按仓库分层的结构。"""
    for subdir in ("by_file", "by_dir"):
        old = LOG_DIR / subdir
        new = SCRIPT_DIR / "log" / repo.owner / repo.name / subdir
        if old.exists() and not new.exists():
            new.parent.mkdir(parents=True, exist_ok=True)
            old.rename(new)


def _review_single_pr(
    repo: RepoConfig, pr: dict, index: int, total: int, args, token: str,
    save_local: bool, output_dir: Path, show_progress: bool = False,
    direct_output: bool = False,
) -> PRResult | None:
    """审查单个 PR，返回结果。

    direct_output=True 时直接输出到 stdout（顺序模式），
    否则缓冲到 StringIO（并行模式，防止输出交叉）。
    """
    buf = _DirectOutput() if direct_output else io.StringIO()
    log = print if direct_output else (lambda s: buf.write(s + "\n"))
    pr_number = pr["number"]
    pr_title = pr["title"]
    pr_start = time.monotonic()

    # 提前获取 head_sha，用于跳过判断和后续传递
    head_sha = pr.get("head", {}).get("sha", "")

    # 检查是否已基于最新提交审查过（--force 跳过此检查）
    if not getattr(args, "force", False) and head_sha and token:
        if _is_already_reviewed(repo, token, pr_number, head_sha):
            buf.write(f"  PR #{pr_number}: 跳过 (已审查 {head_sha[:12]})\n")
            return PRResult(pr_number, pr_title, None, False, ReviewStats(), buf.getvalue(), skipped=True)

    buf.write(f"{_bold(f'[Step 2.{index + 1}]')} {_dim(_now())} PR #{pr_number}: {pr_title}\n")

    # 检测是否为新提交触发的重新审查
    if head_sha and token:
        last = _get_last_review_info(repo, token, pr_number)
        if last:
            last_sha, last_time = last
            commit_info = _get_head_commit_info(repo, token, pr_number)
            author = commit_info[0] if commit_info else "?"
            commit_date = commit_info[1] if commit_info else "?"
            buf.write(f"  {_dim(f'上次检视：{last_sha[:12]} @ {last_time}')}\n")
            buf.write(f"  {_dim(f'新提交：{head_sha[:12]} by {author} @ {commit_date}，需要重新检视')}\n")

    # 获取变更文件
    buf.write(f"  {_dim(_now())} 获取变更文件\n")
    t0 = time.monotonic()
    files = fetch_pr_files(repo, token, pr_number)
    buf.write(f"  {_dim(f'耗时：{_fmt_secs(time.monotonic() - t0)}')}\n")

    if not files:
        buf.write(f"  {_warn('无变更文件或获取失败，跳过。')}\n")
        return PRResult(pr_number, pr_title, None, False, ReviewStats(), buf.getvalue(), success=False)

    cpp_count = sum(1 for f in files if is_cpp_file(get_filename(f)))
    total_adds = sum(f.get("additions", 0) for f in files)
    total_dels = sum(f.get("deletions", 0) for f in files)
    buf.write(f"  共 {len(files)} 个变更文件 (C/C++: {cpp_count}, {_green(f'+{total_adds}')}, {_red(f'-{total_dels}')})\n")

    # 格式化 diff
    diff_text = format_diff_for_review(repo, pr, files)

    if args.dry_run:
        # dry-run 模式：仅保存 diff 不审查
        pr_diff_dir = output_dir / f"pr_{pr_number}"
        pr_diff_dir.mkdir(parents=True, exist_ok=True)
        diff_file = pr_diff_dir / f"{head_sha[:12]}_diff.md" if head_sha else pr_diff_dir / "diff.md"
        diff_file.write_text(diff_text, encoding="utf-8")
        buf.write(f"  {_dim(f'[dry-run] Diff 已保存：{_file_link(diff_file)}')}\n")
        return PRResult(pr_number, pr_title, None, False, ReviewStats(), buf.getvalue())

    # 拉取 PR 分支 commit（供 Claude 用 git show 读取文件，也供存活性检测使用）
    if head_sha:
        fetch_result = subprocess.run(
            ["git", "fetch", "origin", head_sha],
            capture_output=True, text=True, timeout=60,
            cwd=str(repo.path),
        )
        if fetch_result.returncode == 0:
            buf.write(f"  {_ok(f'已拉取 PR commit: {head_sha[:12]}')}\n")
        else:
            buf.write(f"  {_warn('拉取 PR commit 失败，Claude 将使用本地文件作为上下文')}\n")

    # [追踪] 对旧发现做存活性检测（必须在 git fetch 之后，确保 new_sha 已在本地）
    if head_sha:
        try:
            _tracking_conn = _init_tracking_db()
            _track_outcomes(_tracking_conn, repo.path, repo.full_name, pr_number, head_sha, log=log)
            _tracking_conn.close()
        except Exception:
            pass  # 追踪失败不影响主流程

    # 调用 Claude Code 审查（第一步：完整审查，prompt 不变，保证质量）
    use_inline = getattr(args, "inline", False)
    buf.write(f"  {_dim(_now())} 调用 Claude Code (vibe-review skill) 进行代码审查\n")
    if use_inline:
        buf.write(f"  模式：行内评论 (--inline, 两步法)\n")
    t0 = time.monotonic()
    review_text, stats = run_claude_review(diff_text, pr, repo.path, show_progress=show_progress, log=log)
    review_secs = time.monotonic() - t0
    buf.write(f"  {_dim(f'审查耗时：{_fmt_secs(review_secs)}')}\n")
    buf.write(f"  {_dim(f'Token 消耗：{stats.fmt()}')}\n")

    if review_text is None:
        buf.write(f"  {_warn(f'跳过 PR #{pr_number}（审查无结果）')}\n")
        return PRResult(pr_number, pr_title, None, False, stats, buf.getvalue(), success=False)

    # [追踪] 保存审查结果和 findings 到数据库
    if len(review_text) >= MIN_REVIEW_CHARS:
        try:
            _tracking_conn = _init_tracking_db()
            pr_author = pr.get("user", {}).get("login", "unknown")
            severity_summary = _extract_issue_summary(review_text)
            all_findings = _extract_all_findings(review_text)
            duration_ms = int(review_secs * 1000)
            review_id = _save_review(
                _tracking_conn, repo.full_name, pr_number, pr_title, pr_author,
                head_sha, stats, duration_ms, severity_summary, len(all_findings),
            )
            if review_id and all_findings:
                n_saved = _save_findings(_tracking_conn, review_id, all_findings)
                snippet_count = sum(1 for f in all_findings if f.get("code_snippet"))
                log(f"  [追踪] 已记录 {n_saved} 个 findings (snippet: {snippet_count}/{n_saved})")
            _tracking_conn.close()
        except Exception:
            pass  # 追踪失败不影响主流程

    # 发布到 PR 评论
    posted = False
    if args.comment:
        # [追踪] 删除旧评论前，先采集开发者回复
        try:
            _tracking_conn = _init_tracking_db()
            _harvest_replies(_tracking_conn, repo, token, pr_number, repo.full_name, log=log)
            _tracking_conn.close()
        except Exception:
            pass

        buf.write(f"  {_dim(_now())} 发布审查结果到 PR #{pr_number} 评论区\n")
        t0 = time.monotonic()
        author = pr.get("user", {}).get("login", "unknown")

        if use_inline:
            # 行内评论模式：删除旧评论 → 发完整总结 → 发 inline
            # 顺序很重要：先总结再 inline，避免 inline 被误删
            deleted = delete_old_review_comments(repo, token, pr_number)
            if deleted > 0:
                buf.write(f"  {_dim(f'已删除 {deleted} 条旧的 AI 审查评论')}\n")

            # 发布完整审查结果作为总结评论（已删除旧评论，跳过重复删除）
            posted = _post_review_comment_quiet(
                repo, token, pr_number, pr_title, author, review_text, buf,
                head_sha=head_sha)
            if posted:
                buf.write(f"  {_ok('总结评论发布成功')}\n")
            else:
                buf.write(f"  {_fail('总结评论发布失败')}\n")

            # 预构建 position maps，供提取和发布共用
            fp_maps: dict[str, dict[int, tuple[int, bool]]] = {}
            for f in files:
                fname = get_filename(f)
                raw_diff = get_file_diff(f)
                if raw_diff:
                    fp_maps[fname] = _build_diff_position_map(raw_diff)
            findings = _extract_findings_for_inline(
                review_text, files, buf, file_position_maps=fp_maps)
            if findings:
                posted_count, unmapped = _post_inline_comments(
                    repo, token, pr_number, head_sha, findings, files, buf,
                    file_position_maps=fp_maps)
                inline_msg = f"行内评论：{_green(str(posted_count))} 条已发布"
                if unmapped:
                    inline_msg += f", {_yellow(str(len(unmapped)))} 条未能定位"
                buf.write(f"  {inline_msg}\n")
            else:
                if not posted:
                    buf.write(f"  {_warn('未能提取行内评论数据，回退到常规评论')}\n")
                    posted = post_review_comment(repo, token, pr_number, pr_title, author, review_text,
                                                head_sha=head_sha)
        else:
            # 常规评论（与 inline 路径一致：先删旧评论写入 buf，再用 quiet 版本发布）
            deleted = delete_old_review_comments(repo, token, pr_number)
            if deleted > 0:
                buf.write(f"  {_dim(f'已删除 {deleted} 条旧的 AI 审查评论')}\n")
            posted = _post_review_comment_quiet(
                repo, token, pr_number, pr_title, author, review_text, buf,
                head_sha=head_sha)

        buf.write(f"  {_dim(f'发布耗时：{_fmt_secs(time.monotonic() - t0)}')}\n")

    # 保存本地文件
    output_file = None
    if save_local:
        output_file = write_review_md(repo, pr, review_text, output_dir, head_sha=head_sha)
        buf.write(f"  {_ok(f'审查结果已保存：{_file_link(output_file)}')}\n")
    elif not posted:
        # 不保存且未发布，输出到终端防止结果丢失
        buf.write(f"\n{review_text}\n\n")

    pr_secs = time.monotonic() - pr_start
    buf.write(f"  {_dim(f'PR #{pr_number} 总耗时：{_fmt_secs(pr_secs)}')}\n")
    return PRResult(pr_number, pr_title, output_file, posted, stats, buf.getvalue())


# ======================== 主流程 ========================
def main() -> None:
    parser = argparse.ArgumentParser(
        description="代码审查工具：支持 GitCode PR 审查和本地文件审查（Claude Code）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            示例：
              %(prog)s                                    # 审查最近 3 个 open PR
              %(prog)s --count 5                          # 审查最近 5 个 open PR
              %(prog)s --pr 1150                          # 审查指定 PR
              %(prog)s --pr 1150 1144 1143                # 审查多个指定 PR
              %(prog)s --pr 1150 1144 1143 -j1            # 强制顺序审查
              %(prog)s --author lilin_137                 # 审查某用户的 open PR（默认最近 3 个）
              %(prog)s --author lilin_137 -n 0            # 审查某用户的全部 open PR
              %(prog)s --author user1 user2 -n 5          # 审查多个用户的 open PR（最多 5 个）
              %(prog)s --team teams/hccl.txt              # 审查小组全员的 open PR
              %(prog)s --team teams/hccl.txt --count 0    # 审查小组全员的所有 open PR
              %(prog)s --team teams/hccl.txt --state merged -n 5  # 审查小组最近 5 个已合并 PR
              %(prog)s --state merged --count 3           # 审查最近 3 个已合并 PR
              %(prog)s --pr 1150 --save                   # 审查并保存本地文件
              %(prog)s --pr 1150 --comment                # 审查并发布评论到 PR
              %(prog)s --pr 1150 --comment --inline       # 审查并逐行评论到代码
              %(prog)s --pr 1150 --comment --save         # 发布评论 + 保存本地
              %(prog)s --pr 1150 --comment --force        # 强制重新审查（忽略跳过逻辑）
              %(prog)s --pr 1150 --dry-run                # 只拉取 diff 不审查
              %(prog)s --file src/xxx.cpp                 # 审查本地文件
              %(prog)s --file src/a.cpp src/b.h --save    # 审查多个本地文件并保存
              %(prog)s --file src/platform/resource/      # 审查目录下所有 C/C++ 文件
              %(prog)s --repo hcomm --pr 100              # 审查 hcomm 仓库的 PR
              %(prog)s --repo hcomm --file src/x.cpp      # 审查 hcomm 仓库的本地文件
              %(prog)s --clean 1150                       # 清除指定 PR 的 AI 审查评论
              %(prog)s --clean 1150 1144                  # 清除多个 PR 的 AI 审查评论
              %(prog)s --stats                            # 查看审查采纳率统计（默认 30 天）
              %(prog)s --stats --days 90                  # 查看 90 天统计
              %(prog)s --track                            # 追踪所有 pending PR 的结果
              %(prog)s --track --pr 1150                  # 追踪指定 PR 的结果
              %(prog)s --import-logs                      # 导入历史审查日志
        """),
    )
    parser.add_argument("--pr", type=int, nargs="+", metavar="NUM",
                        help="指定 PR 编号（可多个，如 --pr 1150 1144）")
    parser.add_argument("--file", type=str, nargs="+", metavar="PATH",
                        help="审查本地文件或目录（可多个，目录递归扫描 C/C++ 文件，无需 GitCode 令牌）")
    parser.add_argument("--dir", type=str, nargs="+", metavar="DIR",
                        help="审查整个目录（递归扫描 C/C++ 文件，生成合并报告，支持跨文件分析，无需 GitCode 令牌）")
    parser.add_argument("--team", type=Path, metavar="FILE",
                        help="审查小组全员的 PR，需指定人员名单文件路径（如 --team teams/hccl.txt）")
    parser.add_argument("--author", type=str, nargs="+", metavar="USER",
                        help="按用户名筛选 open PR（可多个，如 --author user1 user2）")
    parser.add_argument("-n", "--count", type=int, default=2,
                        help="审查的 PR 数量上限（默认 2，0 表示全部，--pr 模式下忽略）")
    parser.add_argument("--state", type=str, default="open",
                        choices=["open", "merged", "closed", "all"],
                        help="PR 状态筛选（默认 open，--pr 模式下忽略）")
    parser.add_argument("--token", type=str, default=None,
                        help="GitCode 访问令牌（也可用 GITCODE_TOKEN 环境变量）")
    parser.add_argument("--dry-run", action="store_true",
                        help="仅获取 PR 信息和 diff，不调用 Claude 审查")
    parser.add_argument("--comment", action="store_true",
                        help="将审查结果发布为 PR 评论")
    parser.add_argument("--inline", action="store_true",
                        help="将审查发现评论到代码具体行（需配合 --comment）")
    parser.add_argument("--save", action="store_true",
                        help="保存审查结果到本地文件（默认仅输出到终端）")
    parser.add_argument("-j", "--jobs", type=int, default=0, metavar="N",
                        help="并行审查的最大 PR 数（默认 0 即自动：1 个 PR 顺序，多个 PR 并行，上限 3）")
    parser.add_argument("--force", action="store_true",
                        help="强制审查，忽略已审查过最新提交的判断")
    parser.add_argument("--model", type=str, default=DEFAULT_MODEL,
                        help=f"审查使用的模型（默认 {DEFAULT_MODEL}）")
    parser.add_argument("--repo", type=str, default="hcomm", dest="target_repo",
                        help="目标仓库，支持 owner/name（如 myorg/myrepo）或仅 name（默认 owner=cann）")
    parser.add_argument("--clean", type=int, nargs="+", metavar="NUM",
                        help="清除指定 PR 的所有 AI 审查评论（可多个，如 --clean 1150 1144）")
    parser.add_argument("--stats", action="store_true",
                        help="输出 AI 审查采纳率统计报告")
    parser.add_argument("--detail", action="store_true",
                        help="配合 --stats 列出每条检视意见明细")
    parser.add_argument("--track", action="store_true",
                        help="手动触发结果追踪（对已合并 PR 做最终分类）")
    parser.add_argument("--import-logs", action="store_true", dest="import_logs",
                        help="从日志目录导入历史审查数据到追踪数据库")
    parser.add_argument("--days", type=int, default=30,
                        help="--stats 的统计天数范围（默认 30）")
    parser.add_argument("--highlight", type=str, default="",
                        help="高亮显示的 PR 编号（逗号分隔，如 --highlight 385,538），用于标记触发审查的变更 PR")
    args = parser.parse_args()

    _config._review_model = args.model

    # 解析 owner/name，支持 --repo cann/hcomm 或 --repo hcomm（默认 owner=cann）
    if "/" in args.target_repo:
        repo_owner, repo_name = args.target_repo.split("/", 1)
    else:
        repo_owner, repo_name = OWNER, args.target_repo

    # --stats 不依赖本地仓库目录，提前处理
    if args.stats:
        # 用户显式传了 --repo 则只看该仓库，否则看全部
        if any(a in sys.argv for a in ("--repo",)):
            explicit_repo = f"{repo_owner}/{repo_name}"
        else:
            explicit_repo = None
        _main_stats(args, explicit_repo)
        return

    # 初始化仓库配置
    repo_path = REPOS_ROOT / repo_owner / repo_name
    if not repo_path.is_dir():
        print(f"  {_fail(f'本地仓库目录不存在：{repo_path}')}")
        sys.exit(1)
    repo = RepoConfig(name=repo_name, owner=repo_owner, path=repo_path)
    _migrate_legacy_logs(repo)

    if args.clean:
        # --clean 模式：只需要 token，不需要其他参数校验
        token = args.token or os.environ.get("GITCODE_TOKEN")
        if not token:
            print(f"  {_fail('未提供 GitCode 访问令牌')}")
            print("  请通过以下任一方式提供:")
            print("    1. 环境变量：export GITCODE_TOKEN=your_token")
            print("    2. 命令行：  python3 ai_reviewer.py --token your_token")
            sys.exit(1)
        _main_clean(repo, args.clean, token)
        return

    if args.import_logs:
        _main_import_logs(repo, args)
        return

    if args.track:
        token = args.token or os.environ.get("GITCODE_TOKEN")
        if not token:
            print(f"  {_fail('--track 需要 GitCode 访问令牌')}")
            sys.exit(1)
        _main_track(repo, args, token)
        return

    # 互斥校验：--file、--dir、--pr 三者互斥
    mode_flags = sum(bool(x) for x in [args.file, args.dir, args.pr])
    if mode_flags > 1:
        print(f"  {_fail('--file、--dir 和 --pr 不能同时使用')}")
        sys.exit(1)

    if args.inline and not args.comment:
        print(f"  {_fail('--inline 需要配合 --comment 使用')}")
        print("  用法：python3 ai_reviewer.py --pr <number> --comment --inline")
        sys.exit(1)

    # --file / --dir 模式不需要 GitCode 令牌
    token = None
    if not args.file and not args.dir:
        token = args.token or os.environ.get("GITCODE_TOKEN")
        if not token:
            print(f"  {_fail('未提供 GitCode 访问令牌')}")
            print("  请通过以下任一方式提供:")
            print("    1. 环境变量：export GITCODE_TOKEN=your_token")
            print("    2. 命令行：  python3 ai_reviewer.py --token your_token")
            print(f"  获取令牌：{_dim('https://gitcode.com -> 设置 -> 安全设置 -> 私人令牌')}")
            sys.exit(1)

    # 校验 vibe-review skill
    if not args.dry_run and not SKILL_MD_PATH.exists():
        print(f"  {_fail('vibe-review skill 未安装')}")
        print(f"  缺失文件：{_dim(str(SKILL_MD_PATH))}")
        print("  请先安装 vibe-review skill 到 ~/.claude/skills/vibe-review/SKILL.md")
        sys.exit(1)

    save_local = args.save

    if args.dir:
        _main_dir_review(repo, args, save_local)
    elif args.file:
        _main_file_review(repo, args, save_local)
    else:
        _main_pr_review(repo, args, token, save_local)


def _print_results_summary(
    total_secs: float, stats_list: list[ReviewStats],
    item_lines: list[str], parallel_workers: int = 0,
    succeeded: int = 0, failed: int = 0, skipped: int = 0,
) -> None:
    """打印审查结果汇总统计（PR 审查和文件审查共用）。"""
    total_cost = sum(s.best_cost for s in stats_list)

    print(_dim("─" * 60))
    # 构建一行紧凑摘要
    status_parts = []
    if succeeded > 0:
        status_parts.append(_green(f'审查 {succeeded}'))
    if skipped > 0:
        status_parts.append(_dim(f'跳过 {skipped}'))
    if failed > 0:
        status_parts.append(_red(f'失败 {failed}'))
    if status_parts:
        status = " / ".join(status_parts)
    else:
        status = _bold(_green("审查完成!"))
    summary_parts = [f"总耗时：{_bold(_fmt_secs(total_secs))}"]
    if parallel_workers > 1:
        summary_parts.append(f"并行：{parallel_workers}")
    # 多项时展示费用合计和 token 合计（单项已在 Step 中展示过）
    if len(stats_list) > 1 and total_cost > 0:
        summary_parts.append(f"费用合计：{_cyan(f'${total_cost:.4f}')} / {_cyan(f'¥{total_cost * USD_TO_CNY:.4f}')}")
    print(f"  {status} {' | '.join(summary_parts)}")
    if len(stats_list) > 1:
        total_output = sum(s.output_tokens for s in stats_list)
        total_cache_write = sum(s.cache_creation_tokens for s in stats_list)
        total_cache_read = sum(s.cache_read_tokens for s in stats_list)
        total_input = sum(s.input_tokens for s in stats_list) + total_cache_read + total_cache_write
        if total_input or total_output:
            tok_parts = [f"输入 {total_input:,}", f"输出 {total_output:,}"]
            if total_cache_read or total_cache_write:
                tok_parts.append(f"缓存 {total_cache_read:,}读 + {total_cache_write:,}写")
            sep = " / "
            print(f"  {_dim(f'Token 合计：{sep.join(tok_parts)}')}")
    for line in item_lines:
        print(f"  {line}")
    print(_dim("─" * 60))


def _main_clean(repo: RepoConfig, pr_numbers: list[int], token: str):
    """清除指定 PR 的所有 AI 审查评论。"""
    total_deleted = 0
    for pr_number in pr_numbers:
        print(f"PR #{pr_number}: 清除 AI 审查评论")
        deleted = delete_old_review_comments(repo, token, pr_number)
        total_deleted += deleted
        if deleted > 0:
            print(f"  {_ok(f'已删除 {deleted} 条评论')}")
        else:
            print(f"  {_dim('无 AI 审查评论')}")
    if len(pr_numbers) > 1:
        print(f"\n{_ok(f'共删除 {total_deleted} 条 AI 审查评论')}")


def _main_dir_review(repo: RepoConfig, args: argparse.Namespace, save_local: bool) -> None:
    """目录审查主流程：递归扫描 C/C++ 文件，生成一份合并审查报告。"""
    output_dir = repo.dir_log_dir
    if save_local:
        output_dir.mkdir(parents=True, exist_ok=True)

    repo_root = repo.path
    CPP_EXTS = {".h", ".hpp", ".hxx", ".c", ".cc", ".cpp", ".cxx"}

    all_file_paths = []
    dir_labels = []  # 用于显示的目录路径

    for d in args.dir:
        p = Path(d)
        if not p.is_absolute():
            p = repo_root / p
        if not p.exists():
            print(f"  {_warn(f'目录不存在：{d}，跳过')}")
            continue
        if not p.is_dir():
            print(f"  {_warn(f'不是目录：{d}，请使用 --file 审查单个文件')}")
            continue

        found = sorted(
            fp for fp in p.rglob("*") if fp.is_file() and fp.suffix.lower() in CPP_EXTS
        )
        if not found:
            print(f"  {_warn(f'目录中无 C/C++ 文件：{d}，跳过')}")
            continue

        print(f"  扫描目录 {d}: 发现 {len(found)} 个 C/C++ 文件")
        for fp in found:
            try:
                rel = fp.resolve().relative_to(repo_root.resolve())
            except ValueError:
                rel = fp
            all_file_paths.append(str(rel))
        dir_labels.append(d)

    if not all_file_paths:
        print("  无有效文件，退出。")
        sys.exit(0)

    if len(all_file_paths) > MAX_DIR_FILES:
        print(f"  {_fail(f'文件数 ({len(all_file_paths)}) 超过上限 ({MAX_DIR_FILES})')}")
        print("  请缩小目录范围，或使用 --file 逐文件审查。")
        sys.exit(1)

    dir_display = ", ".join(dir_labels)

    output_modes = []
    if save_local:
        output_modes.append(f"本地文件 ({output_dir})")
    if not output_modes:
        output_modes.append("终端")

    print(_dim("─" * 60))
    print(f"  {_bold('目录代码审查工具')} (跨文件合并审查)")
    print(f"  目录：{dir_display}")
    print(f"  文件数：{len(all_file_paths)}")
    print(f"  输出：{' + '.join(output_modes)}")
    print(_dim("─" * 60))
    print()

    total_start = time.monotonic()

    print(f"  {_dim(_now())} 调用 Claude Code (vibe-review skill) 进行跨文件代码审查 ...")
    t0 = time.monotonic()
    review_text, stats = run_claude_dir_review(all_file_paths, repo.path, show_progress=True)
    wall_secs = time.monotonic() - t0
    print(f"  {_dim(f'审查耗时：{_fmt_secs(wall_secs)}')}")
    print(f"  {_dim(f'Token 消耗：{stats.fmt()}')}")

    if review_text is None:
        print(f"  {_warn('审查无结果')}")
        sys.exit(1)

    output_file = None
    if save_local:
        output_file = write_dir_review_md(dir_display, all_file_paths, review_text, output_dir)
        print(f"  {_ok(f'审查结果已保存：{_file_link(output_file)}')}")
    else:
        print(f"\n{review_text}\n")

    total_secs = time.monotonic() - total_start
    item_lines = [f"目录：{dir_display} ({len(all_file_paths)} 个文件)"]
    if output_file:
        item_lines.append(f"报告：{_file_link(output_file)}")
    _print_results_summary(total_secs, [stats], item_lines)


def _main_file_review(repo: RepoConfig, args: argparse.Namespace, save_local: bool) -> None:
    """本地文件审查主流程。"""
    output_dir = repo.file_log_dir
    if save_local:
        output_dir.mkdir(parents=True, exist_ok=True)

    # 收集待审查文件（支持文件和目录）
    repo_root = repo.path
    CPP_EXTS = {".h", ".hpp", ".hxx", ".c", ".cc", ".cpp", ".cxx"}
    file_paths = []
    for f in args.file:
        p = Path(f)
        if not p.is_absolute():
            p = repo_root / p
        if not p.exists():
            print(f"  {_warn(f'路径不存在：{f}，跳过')}")
            continue
        if p.is_dir():
            # 递归扫描目录下所有 C/C++ 文件
            found = sorted(
                fp for fp in p.rglob("*") if fp.is_file() and fp.suffix.lower() in CPP_EXTS
            )
            if not found:
                print(f"  {_warn(f'目录中无 C/C++ 文件：{f}，跳过')}")
                continue
            print(f"  扫描目录 {f}: 发现 {len(found)} 个 C/C++ 文件")
            for fp in found:
                try:
                    rel = fp.resolve().relative_to(repo_root.resolve())
                except ValueError:
                    rel = fp
                file_paths.append(str(rel))
        else:
            if not is_cpp_file(str(p)):
                print(f"  {_warn(f'非 C/C++ 文件：{f}，跳过')}")
                continue
            try:
                rel = p.resolve().relative_to(repo_root.resolve())
            except ValueError:
                rel = p
            file_paths.append(str(rel))

    if not file_paths:
        print("  无有效文件，退出。")
        sys.exit(0)

    output_modes = []
    if save_local:
        output_modes.append(f"本地文件 ({output_dir})")
    if not output_modes:
        output_modes.append("终端")

    print(_dim("─" * 60))
    print(f"  {_bold('本地文件代码审查工具')}")
    print(f"  文件：{', '.join(file_paths)}")
    print(f"  输出：{' + '.join(output_modes)}")
    print(_dim("─" * 60))
    print()

    total_start = time.monotonic()
    results = []

    for i, file_path in enumerate(file_paths):
        file_start = time.monotonic()
        print(f"{_bold(f'[{i + 1}/{len(file_paths)}]')} {_dim(_now())} {file_path}")

        print(f"  {_dim(_now())} 调用 Claude Code (vibe-review skill) 进行代码审查")
        t0 = time.monotonic()
        review_text, stats = run_claude_file_review(file_path, repo.path, show_progress=True)
        wall_secs = time.monotonic() - t0
        print(f"  {_dim(f'审查耗时：{_fmt_secs(wall_secs)}')}")
        print(f"  {_dim(f'Token 消耗：{stats.fmt()}')}")

        if review_text is None:
            print(f"  {_warn(f'跳过 {file_path}（审查无结果）')}")
            print()
            continue

        output_file = None
        if save_local:
            output_file = write_file_review_md(file_path, review_text, output_dir)
            print(f"  {_ok(f'审查结果已保存：{_file_link(output_file)}')}")
        else:
            print(f"\n{review_text}\n")

        file_secs = time.monotonic() - file_start
        print(f"  {_dim(f'{file_path} 总耗时：{_fmt_secs(file_secs)}')}")
        print()
        results.append((file_path, output_file, stats))

    total_secs = time.monotonic() - total_start
    if results:
        item_lines = []
        for fp, path, st in results:
            detail = f"文件：{_file_link(path)}" if path else _green("完成")
            item_lines.append(f"{fp}: {detail}")
        _print_results_summary(
            total_secs, [s for *_, s in results], item_lines)


def _main_pr_review(repo: RepoConfig, args: argparse.Namespace, token: str, save_local: bool) -> None:
    """PR 审查主流程。"""
    output_dir = repo.pr_log_dir
    if save_local or args.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    # 描述当前模式
    if args.pr:
        mode_desc = f"指定 PR: {', '.join(f'#{n}' for n in args.pr)}"
    elif args.team:
        count_desc = "全部" if args.count == 0 else f"最多 {args.count} 个"
        mode_desc = f"小组全员 ({count_desc}, {args.state})"
    elif args.author:
        count_desc = "全部" if args.count == 0 else f"最多 {args.count} 个"
        mode_desc = f"用户：{', '.join(args.author)} ({count_desc}, {args.state})"
    else:
        mode_desc = f"全部 {args.state} PR" if args.count == 0 else f"最近 {args.count} 个 {args.state} PR"

    output_modes = []
    if save_local:
        output_modes.append(f"本地文件 ({output_dir})")
    if args.comment:
        output_modes.append("PR 评论")
    if not output_modes:
        output_modes.append("终端")

    print(_dim("─" * 60))
    print(f"  {_bold('GitCode PR 代码审查工具')}")
    print(f"  仓库：{_cyan(repo.full_name)}")
    print(f"  模式：{mode_desc}")
    print(f"  输出：{' + '.join(output_modes)}")
    print(_dim("─" * 60))
    print()

    total_start = time.monotonic()

    # Step 1: 收集 PR 列表
    print(f"{_bold('[Step 1]')} {_dim(_now())} 获取 PR")
    t0 = time.monotonic()
    prs = collect_prs(repo, token, args)
    print(f"  {_dim(f'耗时：{_fmt_secs(time.monotonic() - t0)}')}")

    # 批量模式下跳过标题含 [WIP] 的 PR（--pr 精确模式不过滤）
    if not args.pr:
        filtered = []
        for pr in prs:
            if "wip" in pr.get("title", "").lower():
                pr_num = pr["number"]
                print(f"  {_skip(f'PR #{pr_num} 标题含 WIP，跳过')}")
            else:
                filtered.append(pr)
        prs = filtered

    if not prs:
        print(f"  {_warn('未找到匹配的 PR，退出。')}")
        sys.exit(0)

    # 按变更规模升序排列（短任务优先，减少总等待时间）
    # GitCode PR 详情接口不含 additions/deletions，需从文件列表接口获取
    def _pr_size(pr: dict) -> int:
        return pr.get("_additions", 0) + pr.get("_deletions", 0)

    if len(prs) > 1:
        print(f"  获取变更统计（用于排序）...")
        for pr in prs:
            files = fetch_pr_files(repo, token, pr["number"])
            pr["_additions"] = sum(f.get("additions", 0) for f in files)
            pr["_deletions"] = sum(f.get("deletions", 0) for f in files)
            pr["_changed_files"] = len(files)
        prs.sort(key=_pr_size)

    # 加载姓名映射（team file 优先，回退到 GitCode API 的 name 字段）
    _team_name: dict[str, str] = {}
    team_file = args.team if args.team else TEAM_FILE
    if team_file.exists():
        _, _team_info = load_team_members(team_file)
        for acct, info in _team_info.items():
            _team_name[acct] = info.split()[0]  # 只取姓名，不要工号

    highlight_prs = set()
    if getattr(args, "highlight", ""):
        highlight_prs = {int(x) for x in args.highlight.split(",") if x.strip().isdigit()}

    print(f"  共 {_bold(str(len(prs)))} 个 PR (按变更规模升序):")
    for pr in prs:
        user = pr.get("user", {})
        login = user.get("login", "?")
        api_name = user.get("name", "")
        team_name = _team_name.get(login, "")
        name_parts = [login]
        if api_name and api_name != login:
            name_parts.append(api_name)
        if team_name and team_name not in name_parts:
            name_parts.append(team_name)
        author = "/".join(name_parts)
        state = pr.get("state", "?")
        adds = pr.get("_additions", "?")
        dels = pr.get("_deletions", "?")
        n_files = pr.get("_changed_files", "")
        size_parts = []
        if n_files:
            size_parts.append(f"{n_files} 文件")
        size_parts.append(f"{_green(f'+{adds}')}/{_red(f'-{dels}')}")
        marker = _yellow("▶ ") if pr["number"] in highlight_prs else "  "
        print(f"  {marker}#{pr['number']} {_dim(f'[{state}]')} ({_cyan(author)}) {pr['title']} {_dim('(' + ', '.join(size_parts) + ')')}")
    print()

    # Step 2: 审查 PR（顺序或并行）
    # 解析并行度
    if args.jobs == 0:
        # 自动：1 个 PR 顺序，多个 PR 自动并行
        max_workers = 1 if len(prs) == 1 else min(len(prs), MAX_PARALLEL_REVIEWS)
    else:
        max_workers = min(args.jobs, len(prs))

    results = []
    if max_workers <= 1:
        # 顺序模式：直接输出到 stdout，每步实时显示
        for i, pr in enumerate(prs):
            result = _review_single_pr(repo, pr, i, len(prs), args, token, save_local, output_dir,
                                       show_progress=True, direct_output=True)
            if result is not None:
                results.append(result)
    else:
        # 并行模式
        print(f"{_bold('[Step 2]')} {_dim(_now())} 并行审查 ({max_workers} 个同时)\n")
        print_lock = threading.Lock()
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(_review_single_pr, repo, pr, i, len(prs), args, token, save_local, output_dir): pr
                for i, pr in enumerate(prs)
            }
            try:
                completed_count = 0
                total_count = len(futures)
                for future in concurrent.futures.as_completed(futures):
                    try:
                        result = future.result()
                    except Exception as exc:
                        pr = futures[future]
                        pr_num = pr.get("number", "?")
                        with print_lock:
                            print(f"  {_fail(f'PR #{pr_num} 审查异常：{exc}')}")
                        completed_count += 1
                        continue
                    completed_count += 1
                    if result is not None:
                        with print_lock:
                            print(result.log, end="")
                        results.append(result)
                    remaining = total_count - completed_count
                    if remaining > 0 and result is not None and not result.skipped:
                        print(f"  {_dim(f'[{completed_count}/{total_count}] 剩余 {remaining} 个 PR 审查中...')}", flush=True)
            except KeyboardInterrupt:
                print(f"\n{_yellow('中断：正在取消剩余任务...')}")
                for f in futures:
                    f.cancel()
                pool.shutdown(wait=False, cancel_futures=True)
                sys.exit(130)

    # Step 3: 汇总
    total_secs = time.monotonic() - total_start
    if results:
        item_lines = []
        for r in results:
            if r.skipped:
                pass  # 跳过的 PR 不在最终汇总中显示
            elif r.success:
                parts: list[str] = []
                if r.output_file:
                    parts.append(f"文件：{_file_link(r.output_file)}")
                if r.posted:
                    parts.append(_green("已发布到 PR"))
                detail = " | ".join(parts) if parts else _green("完成")
                item_lines.append(_ok(f"PR #{r.pr_number} ({r.pr_title}): {detail}"))
            else:
                item_lines.append(_fail(f"PR #{r.pr_number} ({r.pr_title}): {_red('审查失败')}"))

        succeeded = sum(1 for r in results if r.success and not r.skipped)
        skipped = sum(1 for r in results if r.skipped)
        failed = sum(1 for r in results if not r.success)
        _print_results_summary(
            total_secs, [r.stats for r in results], item_lines,
            parallel_workers=max_workers, succeeded=succeeded, failed=failed, skipped=skipped)
        if failed > 0:
            sys.exit(1)
