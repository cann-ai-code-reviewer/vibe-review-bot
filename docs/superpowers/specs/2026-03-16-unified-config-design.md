# Unified Config Design

**Date:** 2026-03-16
**Branch:** `configurable`
**Related issues:** #6 #7 #8 #9 #10 #11

---

## Background

Multiple values are hardcoded across `ai_reviewer.py` and `review_loop.sh`:

| Issue | Hardcoded value |
|-------|----------------|
| #7 | `OWNER = "cann"`, default repo `"hcomm"` |
| #8 | `GITCODE_API_BASE = "https://api.gitcode.com/api/v5"` |
| #6 | `REPOS_ROOT = SCRIPT_DIR.parent.parent.parent` |
| #9 | `MAX_DIFF_CHARS`, `MAX_CLAUDE_TURNS`, `MIN_REVIEW_CHARS`, `MAX_PARALLEL_REVIEWS`, `MAX_DIR_FILES` |
| #10 | `LOG_DIR`, `TRACKING_DB`, `TEAM_FILE` |

This makes the tool hard to use in different environments without modifying source code.

---

## Goals

- All user-tunable values configurable via `config.yaml` in the project root
- Config file ships with the repo so users get sensible defaults out of the box
- Command-line arguments continue to override config values (highest priority)
- No new heavy dependencies

---

## Non-Goals

- User-level config (`~/.config/vibe-review/`)
- Environment variable overrides
- `.reviewrc` or other formats

---

## Configuration Priority (low → high)

```
Code defaults → config.yaml → command-line arguments
```

---

## New Files

### `config.yaml` (project root)

```yaml
# vibe-review-bot 配置文件
# 所有项均有默认值，只需修改你想覆盖的部分

# ── 仓库配置 ──────────────────────────────────────────
owner: cann                                      # 默认 owner（--repo 未含 / 时使用）
default_repo: hcomm                              # --repo 的默认值
repos_root: ""                                   # 本地仓库根目录，空表示脚本目录往上三级

# ── API 配置 ──────────────────────────────────────────
api_base: "https://api.gitcode.com/api/v5"       # GitCode API base URL

# ── 模型配置 ──────────────────────────────────────────
default_model: "claude-opus-4-6"                 # 默认审查模型

# ── 审查限制参数 ───────────────────────────────────────
max_diff_chars: 80000       # 单 PR diff 最大字符数
max_claude_turns: 40        # Claude 最大交互轮数
min_review_chars: 500       # 审查结果最短有效长度
max_parallel_reviews: 4     # 最大并行审查数
max_dir_files: 20           # 目录模式最大文件数

# ── 路径配置 ──────────────────────────────────────────
log_dir: ""                  # 日志目录，空表示脚本目录下的 log/
team_file: ""                # 默认团队文件，空表示 teams/hccl.txt
```

### `config.py` (project root)

Contains `AppConfig` dataclass and `load_config()`. Exposes a module-level `cfg` instance.

```python
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
    cfg = AppConfig()
    config_path = script_dir / "config.yaml"
    if config_path.exists():
        import yaml
        data = yaml.safe_load(config_path.read_text()) or {}
        for f in fields(cfg):
            if f.name in data and data[f.name] not in (None, ""):
                setattr(cfg, f.name, data[f.name])
    return cfg


SCRIPT_DIR = Path(__file__).resolve().parent
cfg: AppConfig = load_config(SCRIPT_DIR)
```

---

## Changes to `ai_reviewer.py`

1. Add at top: `from config import cfg`
2. Replace module-level constants with reads from `cfg`:

| Old constant | New source |
|---|---|
| `GITCODE_API_BASE` | `cfg.api_base` |
| `OWNER` | `cfg.owner` |
| `REPOS_ROOT` | `Path(cfg.repos_root) if cfg.repos_root else SCRIPT_DIR.parent.parent.parent` |
| `MAX_DIFF_CHARS` | `cfg.max_diff_chars` |
| `MAX_CLAUDE_TURNS` | `cfg.max_claude_turns` |
| `MIN_REVIEW_CHARS` | `cfg.min_review_chars` |
| `MAX_PARALLEL_REVIEWS` | `cfg.max_parallel_reviews` |
| `MAX_DIR_FILES` | `cfg.max_dir_files` |
| `LOG_DIR` | `Path(cfg.log_dir) if cfg.log_dir else SCRIPT_DIR / "log"` |
| `TEAM_FILE` | `Path(cfg.team_file) if cfg.team_file else SCRIPT_DIR / "teams" / "hccl.txt"` |
| `DEFAULT_MODEL` | `cfg.default_model` |

3. `--repo` argument default value: `cfg.default_repo`
4. Owner parsing fallback: `cfg.owner`

---

## Changes to `review_loop.sh`

Add a block after `SCRIPT_DIR` is set that reads `config.yaml` via Python and exports shell variables:

```bash
eval "$(python3 -c "
import yaml, pathlib, sys
p = pathlib.Path('$SCRIPT_DIR/config.yaml')
cfg = yaml.safe_load(p.read_text()) if p.exists() else {}
print('CFG_OWNER=' + str(cfg.get('owner', 'cann')))
print('CFG_DEFAULT_REPO=' + str(cfg.get('default_repo', 'hcomm')))
print('CFG_API_BASE=' + str(cfg.get('api_base', 'https://api.gitcode.com/api/v5')))
log = cfg.get('log_dir', '') or '$SCRIPT_DIR/log'
print('CFG_LOG_DIR=' + log)
")"
```

Then replace hardcoded values:

| Old | New |
|---|---|
| `OWNER="cann"` | `OWNER="$CFG_OWNER"` |
| `REPO="${1:-hcomm}"` | `REPO="${1:-$CFG_DEFAULT_REPO}"` |
| `LOG_DIR="$SCRIPT_DIR/log/run"` | `LOG_DIR="$CFG_LOG_DIR/run"` |
| `https://gitcode.com/api/v5/...` | Use `$CFG_API_BASE` (strip trailing `/api/v5`, use base domain) |
| `CACHE_FILE="/tmp/..."` | `CACHE_FILE="$CFG_LOG_DIR/.review_loop_${OWNER}_${REPO}_shas"` |

---

## Testing

- Existing tests in `tests/test_match_filter.py` should continue to pass unchanged
- Manual smoke test: run with default `config.yaml`, verify behavior identical to current
- Manual override test: change `owner`/`default_repo` in `config.yaml`, verify tool uses new values
