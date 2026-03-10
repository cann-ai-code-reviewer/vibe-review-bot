"""调用 Claude Code 执行审查，以及将审查结果写入 .md 文件。"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from .config import (
    MIN_REVIEW_CHARS, MAX_CLAUDE_TURNS, MODEL_PRICING,
    PR_REVIEW_TOOLS, FILE_REVIEW_TOOLS,
)
from .models import RepoConfig, ReviewStats
from .terminal import _warn, _fail, _dim, _yellow, _fmt_secs

_SPINNER_CHARS = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _spinner_thread(stop_event: threading.Event):
    """后台线程：显示旋转动画和已耗时。"""
    start = time.monotonic()
    idx = 0
    while not stop_event.is_set():
        elapsed = time.monotonic() - start
        ch = _SPINNER_CHARS[idx % len(_SPINNER_CHARS)]
        sys.stderr.write(f"\r  审查中 {ch} {_fmt_secs(elapsed)} ")
        sys.stderr.flush()
        idx += 1
        stop_event.wait(0.5)


def _extract_issue_summary(review_text: str) -> str:
    """从审查正文中提取问题计数摘要（如 '严重 3 / 一般 7 / 建议 4'）。"""
    match = re.search(r"(严重\s*\d+\s*/\s*一般\s*\d+\s*/\s*建议\s*\d+)", review_text)
    return match.group(1) if match else ""


def _parse_json_output(raw: str) -> tuple[str, ReviewStats]:
    """解析 claude -p --output-format json 的输出，提取文本和统计信息。"""
    stats = ReviewStats()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw, stats  # JSON 解析失败，当作纯文本返回

    # 提取文本结果
    text = data.get("result", "")
    if "result" not in data and "type" not in data:
        # 兜底：JSON 中无 result 字段且非 Claude Code 结构化输出，尝试整体当文本
        text = raw

    # 提取权限拒绝信息（由调用者负责输出）
    for d in data.get("permission_denials", []):
        tool = d.get("tool_name", "?")
        cmd = d.get("tool_input", {}).get("command", "")
        desc = f"权限拒绝 {tool}: {cmd[:120]}" if cmd else f"权限拒绝 {tool}"
        stats.permission_denials.append(desc)

    # 提取费用和耗时
    stats.cost_usd = data.get("cost_usd", 0) or data.get("total_cost_usd", 0)
    stats.duration_ms = data.get("duration_api_ms", 0) or data.get("duration_ms", 0)
    stats.num_turns = data.get("num_turns", 0)

    # 提取 token 用量 —— 优先使用 modelUsage（会话级汇总，比 usage 更完整）
    # usage 只是最后一轮的快照，modelUsage 是跨所有轮次、所有模型的累计
    model_usage = data.get("modelUsage", {})
    if isinstance(model_usage, dict) and model_usage:
        for model_name, mu in model_usage.items():
            stats.model_names.append(model_name)
            stats.input_tokens += mu.get("inputTokens", 0)
            stats.output_tokens += mu.get("outputTokens", 0)
            stats.cache_read_tokens += mu.get("cacheReadInputTokens", 0)
            stats.cache_creation_tokens += mu.get("cacheCreationInputTokens", 0)
            # 基于官方价格表独立计算费用
            prices = MODEL_PRICING.get(model_name)
            if prices:
                stats.calc_cost_usd += (
                    mu.get("inputTokens", 0) * prices["input"]
                    + mu.get("outputTokens", 0) * prices["output"]
                    + mu.get("cacheCreationInputTokens", 0) * prices["cache_write"]
                    + mu.get("cacheReadInputTokens", 0) * prices["cache_read"]
                ) / 1_000_000
    else:
        # 兜底：使用 usage（可能只是单轮数据）
        usage = data.get("usage", {})
        if isinstance(usage, dict):
            stats.input_tokens = usage.get("input_tokens", 0)
            stats.output_tokens = usage.get("output_tokens", 0)
            stats.cache_read_tokens = usage.get("cache_read_input_tokens", 0)
            stats.cache_creation_tokens = usage.get("cache_creation_input_tokens", 0)

    return text, stats


def _clean_review_output(text: str) -> str | None:
    """清理审查输出中的非审查内容（如 Claude 的中间推理文字、Explanatory 风格 Insight 块）。

    1. 剥离 Explanatory 输出风格产生的 ★ Insight 块（settings.local.json outputStyle 污染）
    2. 审查正文以 '## ' 开头的 markdown 标题起始，之前的内容视为内部推理，予以删除。
    清理后不足 MIN_REVIEW_CHARS 字符则视为无效，返回 None。
    """
    # 剥离 ★ Insight 块（Explanatory 输出风格产生的教育性内容）
    # 格式：`★ Insight ───...───`\n内容\n`───...───`
    text = re.sub(
        r"`★ Insight[^`]*`\s*\n.*?\n`─+`\s*\n?",
        "", text, flags=re.DOTALL,
    )
    # 兜底：剥离残余的 Insight 标记行
    text = re.sub(r"^`[★─][^`]*`\s*$", "", text, flags=re.MULTILINE)

    match = re.search(r"^## ", text, re.MULTILINE)
    if match and match.start() > 0:
        text = text[match.start():]
    text = text.strip()
    if len(text) < MIN_REVIEW_CHARS:
        return None
    return text


def _run_claude(prompt: str, cwd: Path, max_retries: int = 2, allowed_tools: list = None,
                show_progress: bool = False, timeout: int = 900,
                max_turns: int = MAX_CLAUDE_TURNS,
                log=print) -> tuple[str | None, ReviewStats]:
    """调用 claude -p 执行审查。空结果时自动重试。

    返回 (清理后的审查文本或 None, 统计信息)。
    使用 --output-format json 获取 token 用量和费用信息。

    重试策略：空结果或结果过短时重试（保持相同工具配置）。
    回合耗尽或权限拒绝属于确定性失败，直接放弃，不降级为无工具模式。

    cwd: 子进程工作目录（被审查仓库的根目录）。
    allowed_tools: 授权 Claude 自主使用的工具列表（如 ["Read", "Grep", "Glob"]）。
    show_progress: 是否显示实时进度 spinner（仅顺序模式下启用，并行模式下关闭）。
    timeout: 子进程超时秒数（默认 900s，大 PR 应按 diff 大小动态调整）。
    log: 日志输出函数。顺序模式用 print，并行模式传入写 buffer 的函数。
    """
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    env["CLAUDE_CODE_OUTPUT_STYLE"] = ""

    from .config import _review_model  # 避免循环导入（_review_model 运行时可变）
    stats = ReviewStats()
    for attempt in range(1, max_retries + 1):
        cmd = ["claude", "-p", "--output-format", "json", "--model", _review_model]
        if allowed_tools:
            for tool in allowed_tools:
                cmd.extend(["--allowedTools", tool])
            cmd.extend(["--max-turns", str(max_turns)])

        actual_prompt = prompt

        try:
            if show_progress:
                proc = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True,
                    cwd=str(cwd), env=env,
                )
                proc.stdin.write(actual_prompt)
                proc.stdin.close()

                stop_event = threading.Event()
                spinner = threading.Thread(
                    target=_spinner_thread, args=(stop_event,), daemon=True)
                spinner.start()

                try:
                    stdout, stderr_out = proc.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                    raise
                finally:
                    stop_event.set()
                    spinner.join(timeout=2)
                    sys.stderr.write("\r" + " " * 40 + "\r")
                    sys.stderr.flush()

                class _Result:
                    pass
                result = _Result()
                result.stdout = stdout
                result.stderr = stderr_out
                result.returncode = proc.returncode
            else:
                result = subprocess.run(
                    cmd,
                    input=actual_prompt,
                    capture_output=True,
                    text=True,
                    cwd=str(cwd),
                    timeout=timeout,
                    env=env,
                )

            stderr = result.stderr.strip()
            if result.returncode != 0:
                log(f"  {_warn(f'claude 返回码 {result.returncode}')}")
                if stderr:
                    log(f"  {_dim(f'stderr: {stderr[:500]}')}")

            output = result.stdout.strip()
            if not output:
                log(f"  {_warn(f'第 {attempt} 次审查未返回结果 (returncode={result.returncode})')}")
                if stderr:
                    log(f"  {_dim(f'stderr: {stderr[:500]}')}")
                if attempt == max_retries:
                    _diagnose_empty_output(prompt, cwd, allowed_tools, env, log)
            else:
                text, stats = _parse_json_output(output)
                for denial in stats.permission_denials:
                    log(f"  {_warn(denial)}")
                cleaned = _clean_review_output(text)
                if cleaned is not None:
                    return cleaned, stats
                log(f"  {_warn(f'第 {attempt} 次审查结果过短 ({len(text)} 字符)，视为无效')}")
                log(f"  {_dim(f'前 200 字符: {text[:200]}')}")

                # 回合耗尽或权限拒绝是确定性失败，重试不会改善，直接放弃
                turns_exhausted = stats.num_turns >= max_turns - 2
                has_denials = len(stats.permission_denials) > 0
                if turns_exhausted:
                    log(f"  {_fail(f'回合耗尽 ({stats.num_turns}/{max_turns})，放弃本次审查')}")
                    break
                elif has_denials:
                    log(f"  {_fail(f'工具权限拒绝 ({len(stats.permission_denials)} 次)，放弃本次审查')}")
                    break

            if attempt < max_retries:
                log(f"  {_yellow(f'重试中 ({attempt + 1}/{max_retries})')}")

        except FileNotFoundError:
            log(f"  {_fail('未找到 claude 命令，请确认 Claude Code CLI 已安装并在 PATH 中')}")
            sys.exit(1)
        except subprocess.TimeoutExpired:
            log(f"  {_warn(f'第 {attempt} 次审查超时（超过 {_fmt_secs(timeout)}）')}")
            if attempt < max_retries:
                log(f"  {_yellow(f'重试中 ({attempt + 1}/{max_retries})')}")

    log(f"  {_fail(f'{max_retries} 次尝试均未获得审查结果')}")
    return None, stats


def _diagnose_empty_output(prompt: str, cwd: Path, allowed_tools: list, env: dict, log=print):
    """当审查返回空结果时，用 JSON 格式做一次诊断性调用，分析失败原因。"""
    _diag = lambda s: log(f"  {_dim(f'[诊断] {s}')}")
    _diag("尝试用 JSON 格式获取诊断信息")
    diag_cmd = ["claude", "-p", "--output-format", "json", "--max-turns", "3"]
    diag_prompt = "请回复'连通性测试成功'。"
    try:
        diag_result = subprocess.run(
            diag_cmd,
            input=diag_prompt,
            capture_output=True,
            text=True,
            cwd=str(cwd),
            timeout=60,
            env=env,
        )
        if diag_result.stdout.strip():
            _diag(f"基本连通性正常，问题可能是工具调用耗尽了 {MAX_CLAUDE_TURNS} 个 turns")
            _diag(f"建议：增大 MAX_CLAUDE_TURNS（当前={MAX_CLAUDE_TURNS}）或减少工具数量")
            if allowed_tools:
                _diag(f"当前启用的工具：{', '.join(allowed_tools)}")
        else:
            _diag(f"基本连通性也失败 (returncode={diag_result.returncode})")
            if diag_result.stderr.strip():
                _diag(f"stderr: {diag_result.stderr.strip()[:500]}")
    except Exception as e:
        _diag(f"诊断调用异常：{e}")


def run_claude_review(diff_text: str, pr: dict, cwd: Path, max_retries: int = 2,
                      show_progress: bool = False,
                      log=print) -> tuple[str | None, ReviewStats]:
    """调用 claude -p 并使用 vibe-review skill 执行 PR 代码审查。

    启用工具能力，Claude 可主动读取文件和搜索代码以获取上下文。
    对于 PR 审查，指引 Claude 用 git show 读取 PR 分支的文件内容。
    """
    head_sha = pr.get("head", {}).get("sha", "")
    head_ref = pr.get("head", {}).get("ref", "")
    base_ref = pr.get("base", {}).get("ref", "")

    # 根据 diff 大小动态调整超时和 turns（需在 prompt 构造前计算，以便写入工具预算）
    # 超时：基准 600s + 每 50 字符 diff 加 1s，上限 1800s（30 分钟）
    review_timeout = min(600 + len(diff_text) // 50, 1800)
    # turns：基准 35 + 每 200 行 diff 加 5 turns，上限 80
    # 基准 35 = 约 20 次工具调用 + 15 次文本输出余量，防止小 PR 回合耗尽
    diff_lines = diff_text.count("\n")
    review_turns = min(35 + (diff_lines // 200) * 5, 80)
    # 工具调用预算 = 总回合的 60%，剩余留给文本输出
    max_tool_calls = review_turns * 3 // 5

    # 构建工具使用和输出要求指引（编码规则全部在 skill 中，此处只管流程）
    context_guide = f"""\

## 工具使用要求

**质量优先：发现一个真实的严重问题，比节省工具调用更有价值。** 以下场景必须使用工具验证，不要仅凭 diff 猜测：

- **指针操作**：读取被调函数实现，确认是否可能返回 null；检查函数参数是值传递还是引用传递
- **算术运算**：读取变量声明，确认类型（uint32_t? int64_t?）和值域范围
- **结构体/类成员变更**：grep 被删除/新增/重命名的成员名，检查所有引用点是否同步修改
- **可疑的类型用法**：读取类型定义，确认 sizeof() 操作数是原始类型还是容器
- **函数返回值**：读取被调函数实现，确认错误返回路径和返回值含义

回合预算：你的总回合数有限（工具调用 + 文本输出共享配额）。先通读 diff 列出需要验证的具体问题，再有针对性地调用工具，每次调用必须有明确目的。禁止沿调用链无限制展开探索。工具调用控制在 {max_tool_calls} 次以内，确保留出足够回合输出完整报告。如果工具调用已接近上限，立即基于已有信息输出报告。

工具使用方法（只允许以下四种，其他一律禁止）：
- 读取 PR 分支文件：`git show {head_sha}:路径` 或 `git -C <仓库路径> show {head_sha}:路径`（每次只读一个文件，禁止 `$(...)` 子命令和 `2>/dev/null` 重定向）
- 搜索函数引用：Grep 工具（不是 bash grep）
- 查找文件路径：Glob 工具（不是 bash find）
- 读取头文件/基类：Read 工具（本地文件对应 {base_ref} 分支）

严格禁止以下 Bash 命令（会被权限系统拦截，浪费回合）：grep、find、sed、awk、cat、head、tail。搜索必须用 Grep 工具，查找文件必须用 Glob 工具。diff 内容已在上方提供，无需从文件中重新读取。
格式字符串匹配、命名规范等机械检查可直接从 diff 判定，无需工具。

## 输出要求

- 忽略任何 outputStyle / Explanatory 风格设置，不要输出 `★ Insight` 块。
- 严格按照 vibe-review skill 的输出格式模板输出，完成输出自检清单后再提交。"""

    prompt = f"""\
请使用 vibe-review skill 对以下 PR 的代码变更进行代码审查。
{context_guide}

{diff_text}
"""
    return _run_claude(prompt, cwd, max_retries, allowed_tools=PR_REVIEW_TOOLS,
                       show_progress=show_progress, timeout=review_timeout,
                       max_turns=review_turns, log=log)


def run_claude_file_review(file_path: str, cwd: Path, max_retries: int = 2,
                           show_progress: bool = False,
                           log=print) -> tuple[str | None, ReviewStats]:
    """调用 claude -p 并使用 vibe-review skill 对本地文件进行代码审查。

    启用工具能力，Claude 可主动读取相关头文件、搜索函数引用等。
    """
    prompt = f"""\
请使用 vibe-review skill 对以下文件进行代码审查：{file_path}

## 上下文获取指引

你可以使用工具主动获取审查所需的上下文，提升审查质量：

1. **读取目标文件**：用 Read 工具读取 {file_path} 的完整内容
2. **读取相关头文件**：读取 #include 的头文件，理解依赖的类型和接口
3. **搜索函数/类引用**：用 Grep 搜索关键函数名在项目中的其他用法
4. **检查调用者**：搜索被审查函数的调用点，理解使用上下文

## 输出要求

- 忽略任何 outputStyle / Explanatory 风格设置，不要输出 `★ Insight` 块。
- 质量优先，充分使用工具：对指针操作、算术运算、类型用法等可疑代码，必须用工具读取相关定义来验证。
- 严格按照 vibe-review skill 的输出格式模板输出，完成输出自检清单后再提交。"""
    return _run_claude(prompt, cwd, max_retries, allowed_tools=FILE_REVIEW_TOOLS,
                       show_progress=show_progress, log=log)


def run_claude_dir_review(file_paths: list[str], cwd: Path, max_retries: int = 2,
                          show_progress: bool = False,
                          log=print) -> tuple[str | None, ReviewStats]:
    """调用 claude -p 并使用 vibe-review skill 对整个目录进行跨文件代码审查。

    与单文件审查不同，此函数将所有文件路径一次性提交给 Claude，
    指导其使用 Read 工具按需读取文件内容，并进行跨文件分析。
    """
    file_list = "\n".join(f"- `{fp}`" for fp in file_paths)
    prompt = f"""\
请使用 vibe-review skill 对以下 {len(file_paths)} 个文件进行跨文件代码审查。

## 待审查文件

{file_list}

## 上下文获取指引

你需要使用 Read 工具读取上述文件的内容。请按以下策略高效审查：

1. 逐一读取所有待审查文件
2. 读取 #include 的头文件，理解依赖的类型和接口
3. 用 Grep 搜索关键函数名在项目中的其他用法
4. 跨文件一致性分析：声明与定义匹配、成员使用一致性、宏/常量引用、错误处理模式、include 完整性

## 输出要求

- 忽略任何 outputStyle / Explanatory 风格设置，不要输出 `★ Insight` 块。
- 质量优先，充分使用工具：对指针操作、算术运算、类型用法等可疑代码，必须用工具读取相关定义来验证。
- 严格按照 vibe-review skill 的输出格式模板输出，完成输出自检清单后再提交。"""
    return _run_claude(prompt, cwd, max_retries, allowed_tools=FILE_REVIEW_TOOLS,
                       show_progress=show_progress, log=log)


def write_review_md(repo: RepoConfig, pr: dict, review_text: str, output_dir: Path, head_sha: str = "") -> Path:
    """将审查结果写入 markdown 文件。

    目录结构：log/<owner>/<repo>/pr_<number>/<sha>.md
    """
    pr_number = pr.get("number", 0)
    pr_title = pr.get("title", "无标题")
    author = pr.get("user", {}).get("login", "unknown")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    pr_dir = output_dir / f"pr_{pr_number}"
    pr_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{head_sha[:12]}.md" if head_sha else "review.md"
    output_file = pr_dir / filename

    summary = _extract_issue_summary(review_text)
    summary_row = f"\n| 发现 | {summary} |" if summary else ""

    content = (
        f"# Code Review: PR #{pr_number}\n\n"
        f"| 属性 | 值 |\n"
        f"|------|------|\n"
        f"| 标题 | {pr_title} |\n"
        f"| 作者 | {author} |\n"
        f"| 链接 | [{repo.url}/merge_requests/{pr_number}]({repo.url}/merge_requests/{pr_number}) |\n"
        f"| 审查时间 | {now} |\n"
        f"| 审查工具 | Claude Code (`vibe-review` skill) |\n"
        f"| 基线提交 | {head_sha[:12]} |{summary_row}\n\n"
        f"---\n\n"
        f"{review_text}\n"
    )

    output_file.write_text(content, encoding="utf-8")
    return output_file


def write_file_review_md(file_path: str, review_text: str, output_dir: Path) -> Path:
    """将本地文件审查结果写入 markdown 文件。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    safe_name = Path(file_path).name.replace(".", "_")
    output_file = output_dir / f"{safe_name}_review.md"

    summary = _extract_issue_summary(review_text)
    summary_row = f"\n| 发现 | {summary} |" if summary else ""

    content = (
        f"# Code Review: {file_path}\n\n"
        f"| 属性 | 值 |\n"
        f"|------|------|\n"
        f"| 文件 | `{file_path}` |\n"
        f"| 审查时间 | {now} |\n"
        f"| 审查工具 | Claude Code (`vibe-review` skill) |{summary_row}\n\n"
        f"---\n\n"
        f"{review_text}\n"
    )

    output_file.write_text(content, encoding="utf-8")
    return output_file


def write_dir_review_md(dir_path: str, file_paths: list[str],
                        review_text: str, output_dir: Path) -> Path:
    """将目录审查结果写入 markdown 文件。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    safe_name = Path(dir_path).name or "root"
    output_file = output_dir / f"{safe_name}_review.md"

    summary = _extract_issue_summary(review_text)
    summary_row = f"\n| 发现 | {summary} |" if summary else ""

    file_list = "\n".join(f"  - `{fp}`" for fp in file_paths)
    content = (
        f"# Code Review: {dir_path}/\n\n"
        f"| 属性 | 值 |\n"
        f"|------|------|\n"
        f"| 目录 | `{dir_path}` |\n"
        f"| 文件数 | {len(file_paths)} |\n"
        f"| 审查时间 | {now} |\n"
        f"| 审查工具 | Claude Code (`vibe-review` skill) |{summary_row}\n\n"
        f"<details>\n<summary>审查文件列表</summary>\n\n{file_list}\n</details>\n\n"
        f"---\n\n"
        f"{review_text}\n"
    )

    output_file.write_text(content, encoding="utf-8")
    return output_file
