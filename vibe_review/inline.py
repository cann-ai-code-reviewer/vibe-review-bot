"""行内评论提取工具。"""
import io
import re
from pathlib import Path

from .models import InlineFinding
from .terminal import _warn, _bold, _skip, _green, _sev, _dim
from .gitcode import get_filename, get_file_diff
from .diff import _build_diff_position_map, _find_nearest_diff_line


def _extract_findings_for_inline(
    review_text: str, files: list[dict], buf: io.StringIO,
    file_position_maps: dict[str, dict[int, tuple[int, bool]]] | None = None,
) -> list[InlineFinding]:
    """从审查报告中提取发现并定位到 diff 中的精确位置。

    纯文本解析 + diff 搜索，无需额外 API 调用（零成本、无超时风险）。
    定位策略：
    1. 从 '位置:' 行提取明确的行号（如 file.cc:395），在 diff 中验证
    2. 从代码片段在 diff 的所有可见行中搜索匹配
    3. 从函数名/标识符在 diff 的所有可见行中搜索
    3.5. 从位置描述中提取标识符搜索
    """
    # 构建文件名 → raw diff 映射
    file_diffs: dict[str, str] = {}
    for f in files:
        fname = get_filename(f)
        raw_diff = get_file_diff(f)
        if raw_diff:
            file_diffs[fname] = raw_diff

    # 按 ### #N [...] 分割审查发现
    finding_pattern = r'### #(\d+)\s+\[([^\]]+)\]\s+(.*?)(?=\n---\s*$|\n### #\d|\Z)'
    matches = list(re.finditer(finding_pattern, review_text, re.DOTALL | re.MULTILINE))

    if not matches:
        buf.write(f"  {_warn('未能从审查报告中解析到发现')}\n")
        return []

    buf.write(f"  解析审查报告：发现 {_bold(str(len(matches)))} 个问题\n")

    findings: list[InlineFinding] = []
    for m in matches:
        fid = int(m.group(1))
        severity = m.group(2).strip()
        content = m.group(3)

        # 提取 title（第一行，去掉尾部 "— 描述"）
        title = content.split("\n")[0].strip()
        title = re.sub(r"\s*—\s*.*$", "", title)
        if len(title) > 80:
            title = title[:77] + "..."

        # 提取文件路径（优先 backtick 格式，兼容无 backtick 格式）
        # 同时匹配中文全角冒号 `：` 和 ASCII 半角冒号 `:`（SKILL.md 模板用全角，prompt 示例用半角）
        loc_match = re.search(r"位置[：:]\s*`([^`]+)`", content)
        if not loc_match:
            # 兼容无 backtick 格式："- 位置：file.cc:123"
            loc_match = re.search(r"位置[：:]\s*(\S+)", content)
        if not loc_match:
            buf.write(f"  {_skip(f'#{fid}: 未找到位置信息')}\n")
            continue
        location = loc_match.group(1)

        # 解析 file:line 格式（如 "file.cc:395, 427, 457" 或 "file.cc:31-33"）
        file_path = location
        explicit_lines: list[int] = []
        line_match = re.match(r"^(.+?):(\d[\d,\s\-]*)$", location)
        if line_match:
            file_path = line_match.group(1)
            # 解析逗号分隔的行号/范围，取每组的起始行号
            # "66-70, 94-98" → [66, 94]; "395, 427" → [395, 427]
            for part in re.split(r',\s*', line_match.group(2).strip()):
                nums = re.findall(r'\d+', part)
                if nums:
                    explicit_lines.append(int(nums[0]))

        # 匹配到 diff 中的实际文件名
        matched_file = _match_diff_filename(file_path, file_diffs)
        if not matched_file:
            buf.write(f"  {_skip(f'#{fid}: 文件不在 diff 中：{file_path}')}\n")
            continue

        raw_diff = file_diffs[matched_file]

        # 构建行内评论 body
        body = _build_inline_body(content)

        # 定位策略
        target_lines: list[int] = []

        strategy = ""
        if explicit_lines:
            # 策略 1：使用「位置」: 中的明确行号，在 diff 中验证
            if file_position_maps and matched_file in file_position_maps:
                pos_map = file_position_maps[matched_file]
            else:
                pos_map = _build_diff_position_map(raw_diff)
            # 第一轮：精确匹配（行号必须在 diff 中）
            for ln in explicit_lines:
                if ln in pos_map:
                    target_lines.append(ln)
                    strategy = f"策略 1-精确（行 {ln}）"
                    break
            # 第二轮：少量行号时允许偏移匹配，多位置发现不偏移（避免匹配无关行）
            if not target_lines and len(explicit_lines) <= 3:
                for ln in explicit_lines:
                    adjusted = _find_nearest_diff_line(ln, pos_map)
                    if adjusted is not None:
                        target_lines.append(adjusted)
                        strategy = f"策略 1-偏移（行{ln}→{adjusted})"
                        break

        if not target_lines:
            # 策略 2：从代码片段搜索（兼容多种格式，搜索所有可见行）
            code_lines = _extract_code_snippet(content)
            for code_line in code_lines:
                if len(code_line) < 15:
                    continue
                # 处理 ... 截断：取 ... 之前的部分
                search_str = re.split(r"\.\.\.", code_line)[0].rstrip('" ;,')
                if len(search_str) < 15:
                    search_str = code_line  # ... 在开头或太短，用全行
                found = _search_in_diff_all_lines(search_str, raw_diff)
                if found is not None:
                    target_lines.append(found)
                    strategy = f"策略 2-代码片段（行 {found}）"
                    break
                # 回退：用前 40 字符搜索
                if len(search_str) > 40:
                    found = _search_in_diff_all_lines(search_str[:40], raw_diff)
                    if found is not None:
                        target_lines.append(found)
                        strategy = f"策略 2-代码片段前 40（行 {found}）"
                        break

        if not target_lines:
            # 策略 3：从函数名搜索（多种格式）
            func_match = re.search(r"`(\w+(?:::\w+)*)\s*\(\)`", content)
            if not func_match:
                # 不带反引号但带 () 的函数名
                func_match = re.search(r"(?<!\w)(\w+(?:::\w+)*)\s*\(\)(?!\w)", content)
            if not func_match:
                # 位置行 "— `FuncName` 函数" 格式
                func_match = re.search(r"—\s*`(\w+(?:::\w+)*)`", content)
            if func_match:
                func_name = func_match.group(1)
                found = _search_in_diff_all_lines(func_name, raw_diff)
                if found is not None:
                    target_lines.append(found)
                    strategy = f"策略 3-函数名 '{func_name}'（行 {found}）"
                elif "::" in func_name:
                    # 回退：仅搜索方法名部分
                    method = func_name.split("::")[-1]
                    found = _search_in_diff_all_lines(method, raw_diff)
                    if found is not None:
                        target_lines.append(found)
                        strategy = f"策略 3-方法名 '{method}'（行 {found}）"

        if not target_lines:
            # 策略 3.5：从位置行描述中提取标识符搜索
            loc_desc = re.search(r"位置[：:].*?—\s*(.*?)$", content, re.MULTILINE)
            if loc_desc:
                identifiers = re.findall(r"`(\w+(?:::\w+)*)`", loc_desc.group(1))
                for ident in identifiers:
                    found = _search_in_diff_all_lines(ident, raw_diff)
                    if found is not None:
                        target_lines.append(found)
                        strategy = f"策略 3.5-标识符 '{ident}'（行 {found}）"
                        break
                    if "::" in ident:
                        method = ident.split("::")[-1]
                        found = _search_in_diff_all_lines(method, raw_diff)
                        if found is not None:
                            target_lines.append(found)
                            strategy = f"策略 3.5-方法名 '{method}'（行 {found}）"
                            break

        if not target_lines:
            buf.write(f"  {_skip(f'#{fid}: 无法在 diff 中定位（位置：{location}）')}\n")
            continue

        buf.write(f"  {_green('→')} #{fid} [{_sev(severity)}] {matched_file}:{target_lines[0]} {_dim(f'({strategy})')}\n")
        for ln in target_lines:
            findings.append(InlineFinding(
                id=fid, severity=severity, title=title,
                file=matched_file, line=ln, body=body,
            ))

    buf.write(f"  定位完成：{_green(str(len(findings)))} 条发现已定位\n")
    return findings


def _match_diff_filename(file_path: str, file_diffs: dict[str, str]) -> str | None:
    """将审查报告中的文件路径匹配到 diff 中的实际文件名。"""
    # 精确匹配
    if file_path in file_diffs:
        return file_path
    # 后缀匹配（审查报告可能使用简短路径）
    for fname in file_diffs:
        if fname.endswith(file_path) or file_path.endswith(fname):
            return fname
    # 文件名匹配
    basename = Path(file_path).name
    for fname in file_diffs:
        if Path(fname).name == basename:
            return fname
    return None


def _search_in_diff_all_lines(
    search_str: str, raw_diff: str, prefer_added: bool = True,
) -> int | None:
    """在 diff 的所有可见行（'+' 行和上下文行）中搜索字符串。

    优先返回 '+' 行的匹配，其次返回上下文行的匹配。
    """
    new_line = 0
    in_hunk = False
    first_added_match = None
    first_context_match = None

    for line in raw_diff.split("\n"):
        if not line and in_hunk:
            continue  # 跳过尾部空行（split 产物）
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
            if search_str in line and first_added_match is None:
                first_added_match = new_line
        elif line.startswith("-"):
            pass
        elif line.startswith("\\"):
            pass  # "\ No newline at end of file" 等元数据行
        else:
            new_line += 1
            if search_str in line and first_context_match is None:
                first_context_match = new_line

    if prefer_added and first_added_match is not None:
        return first_added_match
    if first_context_match is not None:
        return first_context_match
    if first_added_match is not None:
        return first_added_match
    return None


def _extract_code_snippet(content: str) -> list[str]:
    """从审查发现内容中提取代码片段行，兼容多种格式。"""
    # 模式 0: "问题代码(...):" 后接围栏代码块 ```...```
    m = re.search(r"问题代码[^:：\n]*[：:]\s*\n```\w*\n(.*?)```", content, re.DOTALL)
    if m:
        return [l for l in m.group(1).split("\n") if l.strip()]

    # 模式 1: "问题代码(...):"\n\n 后接 4+ 空格缩进代码块
    m = re.search(r"问题代码[^:：\n]*[：:]\s*\n\n?((?:    .+\n?)+)", content)
    if m:
        return [l.strip() for l in m.group(1).split("\n") if l.strip()]

    # 模式 2: "问题描述(...):"\n 后续段落中的缩进代码块
    m = re.search(r"问题描述[^:：\n]*[：:]\s*\n(.*?)((?:\n    .+)+)", content, re.DOTALL)
    if m:
        return [l.strip() for l in m.group(2).split("\n") if l.strip()]

    # 模式 3: "以下代码..." 后的缩进代码块
    m = re.search(r"以下代码[^：:\n]*[：:]?\s*\n\n?((?:    .+\n?)+)", content)
    if m:
        return [l.strip() for l in m.group(1).split("\n") if l.strip()]

    # 模式 3.5: 通用围栏代码块回退 — 第一个 ```...``` 块
    # 安全检查：如果代码块前面出现了修复建议关键词，说明是修复代码，跳过
    m = re.search(r"```\w*\n(.*?)```", content, re.DOTALL)
    if m:
        before = content[:m.start()]
        if not re.search(r"修复建议|建议修改|建议改为|建议修复|Suggested\s+fix", before, re.IGNORECASE):
            return [l for l in m.group(1).split("\n") if l.strip()]

    # 模式 4: 通用回退 — 第一个连续 4 空格缩进块（至少 1 行）
    blocks = re.findall(r"(?:^    .+$\n?)+", content, re.MULTILINE)
    if blocks:
        return [l.strip() for l in blocks[0].split("\n") if l.strip()]

    return []


def _build_inline_body(section_text: str) -> str:
    """从发现内容构建精简的行内评论 body（适合行内评论，≤500 字）。"""
    lines = section_text.split("\n")
    body_parts: list[str] = []
    in_code_block = False

    for line in lines:
        stripped = line.strip()
        # 围栏代码块内不做元数据过滤（避免代码中恰好含 "- 位置:" 等模式被误删）
        if stripped.startswith("```"):
            in_code_block = not in_code_block
        # 跳过元数据行（同时匹配中文全角冒号 `：` 和 ASCII 半角冒号 `:`）
        if not in_code_block and (
                re.match(r"- 位置[：:]", stripped) or re.match(r"- 规则[：:]", stripped) or
                re.match(r"- 置信度[：:]", stripped)):
            continue
        # 跳过标题行（第一行）
        if not body_parts and not stripped:
            continue
        if stripped:
            body_parts.append(line)
        elif body_parts:
            body_parts.append("")  # 保留段落间空行

    body = "\n".join(body_parts).strip()
    # 截断（行内评论保留足够空间展示代码片段和修复建议）
    if len(body) > 2000:
        body = body[:1997] + "..."
    return body
