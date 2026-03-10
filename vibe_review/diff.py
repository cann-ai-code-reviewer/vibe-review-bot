"""Diff 格式化和 position 计算工具。"""
import re
from pathlib import Path

from .config import MAX_DIFF_CHARS
from .gitcode import get_file_diff, get_file_status, get_filename
from .models import RepoConfig, InlineFinding


def is_cpp_file(filename: str) -> bool:
    """判断是否为 C/C++ 源文件或头文件。"""
    exts = {".h", ".hpp", ".hxx", ".c", ".cc", ".cpp", ".cxx"}
    return Path(filename).suffix.lower() in exts


def format_diff_for_review(repo: RepoConfig, pr: dict, files: list) -> str:
    """将 PR 元信息和文件 diff 格式化为审查用文本。"""
    pr_number = pr.get("number", "?")
    pr_title = pr.get("title", "无标题")
    author = pr.get("user", {}).get("login", "unknown")
    head_ref = pr.get("head", {}).get("ref", "?")
    base_ref = pr.get("base", {}).get("ref", "?")
    body = (pr.get("body") or "").strip()

    lines = [
        f"# PR #{pr_number}: {pr_title}",
        f"",
        f"- 作者：{author}",
        f"- 分支：{head_ref} -> {base_ref}",
        f"- 链接：{repo.url}/merge_requests/{pr_number}",
    ]
    if body:
        lines.append(f"- 描述：{body[:500]}")
    lines.append("")

    # 文件列表概览
    cpp_files = [f for f in files if is_cpp_file(get_filename(f))]
    non_cpp_files = [f for f in files if not is_cpp_file(get_filename(f))]

    lines.append(f"## 变更文件 ({len(files)} 个, 其中 C/C++ 文件 {len(cpp_files)} 个)")
    lines.append("")
    for f in files:
        fname = get_filename(f)
        status = get_file_status(f)
        adds = f.get("additions", 0)
        dels = f.get("deletions", 0)
        marker = " *" if is_cpp_file(fname) else ""
        lines.append(f"- [{status}] {fname} (+{adds}, -{dels}){marker}")
    lines.append("")

    # 只输出 C/C++ 文件的 diff（代码审查重点）
    review_files = cpp_files if cpp_files else files
    lines.append("## Diff 内容")
    lines.append("")

    total_chars = 0
    for f in review_files:
        fname = get_filename(f)
        diff_text = get_file_diff(f)
        if not diff_text:
            continue

        # 防止超长 diff
        if total_chars + len(diff_text) > MAX_DIFF_CHARS:
            lines.append(f"### {fname}")
            lines.append("(diff 过长，已截断)")
            lines.append("")
            break

        lines.append(f"### {fname}")
        lines.append("```diff")
        lines.append(diff_text)
        lines.append("```")
        lines.append("")
        total_chars += len(diff_text)

    # 如果有非 C++ 文件被跳过，注明
    if cpp_files and non_cpp_files:
        skipped = ", ".join(get_filename(f) for f in non_cpp_files[:10])
        lines.append(f"> 注：以下非 C/C++ 文件未纳入审查：{skipped}")
        lines.append("")

    return "\n".join(lines)


def _build_diff_position_map(raw_diff: str) -> dict[int, tuple[int, bool]]:
    """解析单个文件的 raw diff，建立行号→position 映射。

    返回：{new_line_number: (position, is_added)}
    - position: GitCode API 的 diff 相对行号（从 1 开始）
    - is_added: True 表示 '+' 行（新增），False 表示上下文行

    算法说明：
    - 首个 @@ 行不计入 position（position 从其后第一行开始为 1）
    - 后续 @@ 行本身计入 position（占 1 个 position）
    - '-' 行（删除行）计入 position 但不增加 new_line
    - '+' 行（新增行）计入 position 且增加 new_line
    - 上下文行（无 +/-）计入 position 且增加 new_line
    """
    mapping: dict[int, tuple[int, bool]] = {}
    position = 0
    new_line = 0
    first_hunk = True

    for line in raw_diff.split("\n"):
        if not line and not first_hunk:
            continue  # 跳过尾部空行（split 产物）
        if line.startswith("@@"):
            # 解析 @@ -old_start,old_count +new_start,new_count @@
            match = re.search(r"\+(\d+)", line)
            if match:
                new_line = int(match.group(1)) - 1
            if first_hunk:
                first_hunk = False
            else:
                position += 1
            continue

        if first_hunk:
            # 跳过 diff header（--- a/... / +++ b/... 等）
            continue

        position += 1
        if line.startswith("+"):
            new_line += 1
            mapping[new_line] = (position, True)
        elif line.startswith("-"):
            pass  # 删除行不增加 new_line
        elif line.startswith("\\"):
            pass  # "\ No newline at end of file" 等元数据行，计 position 但不增加 new_line
        else:
            new_line += 1
            mapping[new_line] = (position, False)

    return mapping


def _build_diff_line_content(raw_diff: str) -> dict[int, str]:
    """解析 diff，构建 {新文件行号：行内容} 映射（用于行号校验）。"""
    content_map: dict[int, str] = {}
    new_line = 0
    in_hunk = False

    for line in raw_diff.split("\n"):
        if line.startswith("@@"):
            match = re.search(r"\+(\d+)", line)
            if match:
                new_line = int(match.group(1)) - 1
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith("+"):
            new_line += 1
            content_map[new_line] = line[1:]  # 去掉 '+' 前缀
        elif line.startswith("-") or line.startswith("\\"):
            pass
        else:
            new_line += 1
            content_map[new_line] = line[1:] if line.startswith(" ") else line

    return content_map


def _find_nearest_diff_line(
    target: int, pos_map: dict[int, tuple[int, bool]], max_offset: int = 5,
) -> int | None:
    """验证行号是否在 diff 中，优先匹配 '+' 行，其次上下文行。"""
    # 精确匹配 — 无论 '+' 行还是上下文行都接受
    if target in pos_map:
        return target

    # 附近搜索第一轮：优先 '+' 行
    for offset in range(1, max_offset + 1):
        for candidate in [target + offset, target - offset]:
            if candidate in pos_map:
                _, is_added = pos_map[candidate]
                if is_added:
                    return candidate

    # 附近搜索第二轮：接受上下文行
    for offset in range(1, max_offset + 1):
        for candidate in [target + offset, target - offset]:
            if candidate in pos_map:
                return candidate

    return None


def _verify_and_correct_line(
    finding: InlineFinding, content_map: dict[int, str], max_offset: int = 5,
) -> int:
    """校验 finding 的行号是否精确指向问题代码，偏差时自动修正。

    优先用 title 中的标识符定位最相关的行（title 描述了具体问题，比 body 中的
    多行代码块更精确），然后用 body 代码行作为后备。
    """
    # ---- 第 1 层：从 title 提取标识符（最能指向问题行的关键词）----
    title_ids: list[str] = []
    # 反引号包裹的标识符
    for m in re.finditer(r"`([^`]{3,})`", finding.title):
        title_ids.append(m.group(1))
    # ALL_CAPS 标识符（HCCL_ERROR, HCCL_INFO, SPRINTF 等）
    for m in re.finditer(r"\b([A-Z][A-Z0-9_]{4,})\b", finding.title):
        ident = m.group(1)
        if ident not in title_ids:
            title_ids.append(ident)
    # PascalCase 函数名（GetEndpointNum, AddrPositionToEndpointLoc 等）
    for m in re.finditer(r"\b([A-Z][a-z]+(?:[A-Z][a-z0-9]*)+)\b", finding.title):
        ident = m.group(1)
        if ident not in title_ids:
            title_ids.append(ident)

    # ---- 第 2 层：从 body 提取代码行关键词 ----
    body_kws: list[str] = []
    # 围栏代码块内的代码行
    for fence_m in re.finditer(r"```\w*\n(.*?)```", finding.body, re.DOTALL):
        for line in fence_m.group(1).split("\n"):
            code = line.strip()
            if len(code) >= 10 and not code.startswith("//"):
                body_kws.append(code)
    # 4 空格缩进代码行（兼容旧格式）
    if not body_kws:
        for m in re.finditer(r"^    (.+)$", finding.body, re.MULTILINE):
            code = m.group(1).strip()
            if len(code) >= 10 and not code.startswith("//"):
                body_kws.append(code)

    if not title_ids and not body_kws:
        return finding.line

    def _search(keywords: list[str]) -> int | None:
        """在 finding.line ±max_offset 内搜索匹配 keywords 的行。"""
        cur = content_map.get(finding.line, "")
        if any(kw in cur for kw in keywords):
            return finding.line
        for off in range(1, max_offset + 1):
            # 优先向后搜索（问题代码常在代码块的后几行）
            for cand in [finding.line + off, finding.line - off]:
                c = content_map.get(cand, "")
                if any(kw in c for kw in keywords):
                    return cand
        return None

    # 优先用 title 标识符定位（更精确地指向问题行本身）
    if title_ids:
        result = _search(title_ids)
        if result is not None:
            return result

    # 后备：用 body 代码行关键词
    if body_kws:
        result = _search(body_kws)
        if result is not None:
            return result

    return finding.line
