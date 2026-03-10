"""终端颜色工具和文本格式化工具。"""
import os
import re
import sys
import unicodedata
from datetime import datetime


def _supports_color() -> bool:
    """检测终端是否支持 ANSI 颜色（遵循 no-color.org 标准）。"""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


_USE_COLOR = _supports_color()


def _c(code: str, text: str) -> str:
    """应用 ANSI 颜色代码。"""
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else str(text)


_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _vw(s: str) -> int:
    """计算字符串在终端中的视觉宽度（去除ANSI码，CJK字符算2列）。"""
    s = _ANSI_RE.sub("", s)
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def _pad(s: str, width: int) -> str:
    """按视觉宽度右填充空格，使其对齐到指定列。"""
    return s + " " * max(0, width - _vw(s))


def _dim(t: str) -> str:    return _c("2", t)
def _bold(t: str) -> str:   return _c("1", t)
def _red(t: str) -> str:    return _c("31", t)
def _green(t: str) -> str:  return _c("32", t)
def _yellow(t: str) -> str: return _c("33", t)
def _blue(t: str) -> str:   return _c("34", t)
def _cyan(t: str) -> str:   return _c("36", t)


def _sev(severity: str) -> str:
    """为严重级别添加颜色。"""
    if "严重" in severity:
        return _red(severity)
    if "一般" in severity:
        return _yellow(severity)
    if "建议" in severity:
        return _blue(severity)
    return severity


def _file_link(path) -> str:
    """用 OSC 8 生成终端可点击的文件超链接（WezTerm/iTerm2/等支持）。"""
    p = str(path)
    if _USE_COLOR:
        return f"\033]8;;file://{p}\033\\{p}\033]8;;\033\\"
    return p

def _ok(msg: str) -> str:   return f"{_green('✓')} {msg}"
def _fail(msg: str) -> str:  return f"{_red('✗')} {msg}"
def _warn(msg: str) -> str:  return f"{_yellow('⚠')} {msg}"
def _skip(msg: str) -> str:  return f"{_dim('○')} {msg}"

def _now() -> str:           return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
def _fmt_secs(s: float) -> str: return f"{s:.1f}s" if s < 60 else f"{int(s)//60}m {int(s)%60}s"


def _compact_line_numbers(raw: str) -> str:
    """'119, 124' → '119,124'；'119, 120, 121' → '119-121'；已是范围格式则原样返回。"""
    if "-" in raw and "," not in raw:
        return raw.strip()
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    nums = []
    for p in parts:
        if "-" in p:
            return raw.replace(" ", "")
        try:
            nums.append(int(p))
        except ValueError:
            return raw.replace(" ", "")
    if not nums:
        return raw.strip()
    nums.sort()
    # 检查是否连续
    if len(nums) > 1 and nums[-1] - nums[0] == len(nums) - 1:
        return f"{nums[0]}-{nums[-1]}"
    return ",".join(str(n) for n in nums)


def _normalize_location_lines(text: str) -> str:
    """统一审查文本中 '位置：`file:lines`' 的行号格式。"""
    def _repl(m):
        path, nums = m.group(1), m.group(2)
        return f"位置：`{path}:{_compact_line_numbers(nums)}`"
    return re.sub(r"位置[：:]\s*`([^:``]+):([^`]+)`", _repl, text)
