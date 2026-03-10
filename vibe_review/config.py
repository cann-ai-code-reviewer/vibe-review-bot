"""全局常量配置。"""
from pathlib import Path

GITCODE_API_BASE = "https://api.gitcode.com/api/v5"
OWNER = "cann"
# REPO 和 REPO_URL 改为运行时从 --repo 参数确定，见 RepoConfig
SCRIPT_DIR = Path(__file__).resolve().parent.parent
REPOS_ROOT = SCRIPT_DIR.parent.parent.parent  # ~/repo/
# 单个 PR diff 最大字符数（防止超出 Claude 上下文窗口）
MAX_DIFF_CHARS = 80000
# vibe-review skill 路径
SKILL_MD_PATH = Path.home() / ".claude" / "skills" / "vibe-review" / "SKILL.md"
# 单条 PR 评论最大字符数（GitCode 限制）
MAX_COMMENT_CHARS = 60000
# claude -p 最大 agentic 回合数（工具调用 + 文本输出）
# 质量优先：充足的回合数确保 Claude 有空间进行深度分析和工具验证
MAX_CLAUDE_TURNS = 40
# 美元兑人民币汇率（用于费用显示，近似值）
USD_TO_CNY = 7.25
# 模型价格表（$/MTok，来源：platform.claude.com/docs/en/about-claude/pricing）
# cache_write 为 5 分钟缓存写入价格（1.25× 输入价）
# cache_read 为缓存命中价格（0.1× 输入价）
MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-6":   {"input": 5,  "output": 25, "cache_write": 6.25, "cache_read": 0.50},
    "claude-opus-4-5":   {"input": 5,  "output": 25, "cache_write": 6.25, "cache_read": 0.50},
    "claude-opus-4-1":   {"input": 15, "output": 75, "cache_write": 18.75, "cache_read": 1.50},
    "claude-sonnet-4-6": {"input": 3,  "output": 15, "cache_write": 3.75, "cache_read": 0.30},
    "claude-sonnet-4-5": {"input": 3,  "output": 15, "cache_write": 3.75, "cache_read": 0.30},
    "claude-haiku-4-5":  {"input": 1,  "output": 5,  "cache_write": 1.25, "cache_read": 0.10},
}
# 支持的模型：claude-opus-4-6, claude-sonnet-4-6, claude-sonnet-4-5, claude-haiku-4-5
DEFAULT_MODEL = "claude-opus-4-6"
_review_model = DEFAULT_MODEL
# AI 评论标识（用于识别和清理旧评论）
AI_REVIEW_MARKER = "## AI Code Review"
# 行内评论标识（附加在每条行内评论 body 末尾，用于识别和清理）
AI_INLINE_MARKER = "<!-- AI_CODE_REVIEW -->"
# 并发审查上限，避免 API 限流
MAX_PARALLEL_REVIEWS = 4
# 目录审查文件数上限
MAX_DIR_FILES = 20
# 审查结果最短有效长度（低于此值视为无效输出，触发重试）
MIN_REVIEW_CHARS = 500
# 小组人员名单（姓名 gitcode 账号，每行一人，首行为标题）
TEAM_FILE = SCRIPT_DIR / "teams" / "hccl.txt"
# 审查结果日志目录
LOG_DIR = SCRIPT_DIR / "log"
# 审查追踪数据库（存活性检测 + 采纳率统计）
TRACKING_DB = LOG_DIR / "review_tracking.db"

# 文件审查工具：允许 Claude 读取本地文件和搜索代码
FILE_REVIEW_TOOLS = ["Read", "Grep", "Glob", "Skill"]
# PR 审查工具：在文件审查工具基础上允许只读 git 命令
# Claude 可能用 git -C <path> 在非 cwd 仓库执行，需同时覆盖直接和 -C 两种形式
PR_REVIEW_TOOLS = [
    "Read", "Grep", "Glob", "Skill",
    "Bash(git show *)", "Bash(git log *)", "Bash(git diff *)", "Bash(git blame *)", "Bash(git fetch *)",
    "Bash(git -C * show *)", "Bash(git -C * log *)", "Bash(git -C * diff *)", "Bash(git -C * blame *)", "Bash(git -C * fetch *)",
]
