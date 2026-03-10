"""数据类定义。"""
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .config import SCRIPT_DIR, USD_TO_CNY
from .terminal import _fmt_secs


@dataclass
class RepoConfig:
    """目标仓库配置（从 --repo 参数派生）。"""
    name: str       # "hcomm-dev"
    owner: str      # "cann"
    path: Path      # ~/repo/cann/hcomm-dev

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def url(self) -> str:
        return f"https://gitcode.com/{self.owner}/{self.name}"

    @property
    def api_prefix(self) -> str:
        return f"/repos/{self.owner}/{self.name}"

    @property
    def pr_log_dir(self) -> Path:
        return SCRIPT_DIR / "log" / self.owner / self.name

    @property
    def file_log_dir(self) -> Path:
        return SCRIPT_DIR / "log" / self.owner / self.name / "by_file"

    @property
    def dir_log_dir(self) -> Path:
        return SCRIPT_DIR / "log" / self.owner / self.name / "by_dir"


@dataclass
class ReviewStats:
    """单次审查的 token 消耗和耗时统计。"""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0       # Claude Code 报告的费用
    calc_cost_usd: float = 0.0  # 基于官方价格表独立计算的费用
    model_names: list[str] = field(default_factory=list)  # 使用的模型名
    permission_denials: list[str] = field(default_factory=list)  # 被拒绝的工具调用描述
    duration_ms: int = 0
    num_turns: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def best_cost(self) -> float:
        """优先使用 Claude Code 报告的费用，兜底使用独立计算值。"""
        return self.cost_usd if self.cost_usd > 0 else self.calc_cost_usd

    def fmt(self) -> str:
        """格式化为一行摘要。"""
        parts = []
        if self.model_names:
            parts.append("+".join(self.model_names))
        total_input = self.input_tokens + self.cache_read_tokens + self.cache_creation_tokens
        if total_input or self.output_tokens:
            parts.append(f"输入 {total_input:,} / 输出 {self.output_tokens:,} tokens")
        if self.cache_read_tokens or self.cache_creation_tokens:
            parts.append(f"缓存 {self.cache_read_tokens:,}读 + {self.cache_creation_tokens:,}写")
        cost = self.best_cost
        if cost > 0:
            parts.append(f"${cost:.4f} / ¥{cost * USD_TO_CNY:.4f}")
        if self.num_turns > 0:
            parts.append(f"{self.num_turns} 回合")
        if self.duration_ms > 0:
            parts.append(f"{_fmt_secs(self.duration_ms / 1000)} (API)")
        return " | ".join(parts) if parts else "无统计数据"


class _DirectOutput:
    """直接输出到 stdout 的流适配器（顺序模式使用），接口兼容 StringIO。"""

    def write(self, s: str) -> int:
        sys.stdout.write(s)
        sys.stdout.flush()
        return len(s)

    def getvalue(self) -> str:
        return ""


@dataclass
class InlineFinding:
    """单个行内审查发现。"""
    id: int
    severity: str       # "严重" / "一般" / "建议"
    title: str
    file: str
    line: int
    body: str


@dataclass
class PRResult:
    """单个 PR 审查结果。"""
    pr_number: int
    pr_title: str
    output_file: Path | None
    posted: bool
    stats: ReviewStats
    log: str  # 该 PR 处理过程的日志文本
    success: bool = True  # 审查是否成功产出结果
    skipped: bool = False  # 已审查过，本次跳过
