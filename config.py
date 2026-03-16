from dataclasses import dataclass, fields
from pathlib import Path


@dataclass
class AppConfig:
    # 仓库
    owner: str = "cann"
    default_repo: str = "hcomm"
    repos_root: str = ""
    # API
    api_base: str = "https://api.gitcode.com/api/v5"
    # 模型
    default_model: str = "claude-opus-4-6"
    # 审查参数
    max_diff_chars: int = 80000
    max_claude_turns: int = 40
    min_review_chars: int = 500
    max_parallel_reviews: int = 4
    max_dir_files: int = 20
    # 路径
    log_dir: str = ""
    team_file: str = ""


def load_config(script_dir: Path) -> AppConfig:
    """Load config.yaml from script_dir, falling back to dataclass defaults."""
    cfg = AppConfig()
    config_path = script_dir / "config.yaml"
    if not config_path.exists():
        return cfg
    try:
        import yaml
    except ImportError:
        raise ImportError("PyYAML is required: pip install pyyaml") from None
    data = yaml.safe_load(config_path.read_text()) or {}
    for f in fields(cfg):
        val = data.get(f.name)
        if val is None or val == "":
            continue
        setattr(cfg, f.name, val)
    return cfg


SCRIPT_DIR = Path(__file__).resolve().parent
cfg: AppConfig = load_config(SCRIPT_DIR)
