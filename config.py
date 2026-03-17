import sys
import warnings
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
    if config_path.exists():
        try:
            import yaml
        except ImportError:
            raise ImportError("PyYAML is required: pip install pyyaml") from None
        data = {}
        try:
            data = yaml.safe_load(config_path.read_text()) or {}
        except yaml.YAMLError as e:
            sys.exit(f"ERROR: config.yaml is malformed: {e}")
        for f in fields(cfg):
            val = data.get(f.name)
            if val is None or val == "":
                continue
            field_type = type(getattr(AppConfig(), f.name))
            try:
                setattr(cfg, f.name, field_type(val))
            except (ValueError, TypeError):
                warnings.warn(
                    f"config.yaml: invalid value for '{f.name}': {val!r} "
                    f"(expected {field_type.__name__}), using default {getattr(AppConfig(), f.name)!r}",
                    stacklevel=2,
                )
    # Fill in path defaults that depend on script_dir
    if not cfg.repos_root:
        cfg.repos_root = str(script_dir.parent.parent.parent)
    if not cfg.log_dir:
        cfg.log_dir = str(script_dir / "log")
    if not cfg.team_file:
        cfg.team_file = str(script_dir / "teams" / "hccl.txt")
    return cfg


SCRIPT_DIR = Path(__file__).resolve().parent
cfg: AppConfig = load_config(SCRIPT_DIR)
