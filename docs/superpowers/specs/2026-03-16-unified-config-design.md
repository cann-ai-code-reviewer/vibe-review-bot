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

## Dependencies

PyYAML is required. Add `pyyaml>=6.0` to a new `requirements.txt` at the project root.

```
pyyaml>=6.0
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
api_base: "https://api.gitcode.com/api/v5"       # GitCode API base URL（末尾不含 /）

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
```

Key notes:
- `val is None or val == ""` guards string "empty = use default" without accidentally dropping integer `0`.
- `ImportError` with a clear message if PyYAML is missing.

---

## Changes to `ai_reviewer.py`

1. Add at top: `from config import cfg, SCRIPT_DIR as CONFIG_SCRIPT_DIR`
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
| `TRACKING_DB` | re-derived as `LOG_DIR / "review_tracking.db"` after `LOG_DIR` is updated |
| `TEAM_FILE` | `Path(cfg.team_file) if cfg.team_file else SCRIPT_DIR / "teams" / "hccl.txt"` |
| `DEFAULT_MODEL` | `cfg.default_model` |

3. `--repo` argument default value: `cfg.default_repo`
4. Owner parsing fallback: `cfg.owner`

### RepoConfig log path properties

`RepoConfig.pr_log_dir`, `file_log_dir`, `dir_log_dir` and `_migrate_legacy_logs` currently
hardcode `SCRIPT_DIR / "log"` directly. They must be updated to use the config-driven `LOG_DIR`:

```python
@property
def pr_log_dir(self) -> Path:
    return LOG_DIR / self.owner / self.name

@property
def file_log_dir(self) -> Path:
    return LOG_DIR / self.owner / self.name / "by_file"

@property
def dir_log_dir(self) -> Path:
    return LOG_DIR / self.owner / self.name / "by_dir"
```

`_migrate_legacy_logs` has two paths to fix. Currently:

```python
old = LOG_DIR / subdir                                          # already uses LOG_DIR
new = SCRIPT_DIR / "log" / repo.owner / repo.name / subdir    # hardcoded — must change
```

After the change, `new` must become `LOG_DIR / repo.owner / repo.name / subdir`.

---

## Changes to `review_loop.sh`

Add a block after `SCRIPT_DIR` is set that reads `config.yaml` via Python and exports shell
variables. `SCRIPT_DIR` is passed via environment variable (not string interpolation) to avoid
issues with paths containing shell metacharacters:

```bash
eval "$(VIBE_SCRIPT_DIR="$SCRIPT_DIR" python3 -c "
import yaml, pathlib, os
p = pathlib.Path(os.environ['VIBE_SCRIPT_DIR']) / 'config.yaml'
cfg = yaml.safe_load(p.read_text()) if p.exists() else {}
print('CFG_OWNER=' + str(cfg.get('owner', 'cann')))
print('CFG_DEFAULT_REPO=' + str(cfg.get('default_repo', 'hcomm')))
print('CFG_API_BASE=' + str(cfg.get('api_base', 'https://api.gitcode.com/api/v5')))
log = cfg.get('log_dir') or os.environ['VIBE_SCRIPT_DIR'] + '/log'
print('CFG_LOG_DIR=' + log)
" 2>&1)" || { echo "ERROR: failed to read config.yaml (is pyyaml installed?)"; exit 1; }
```

Then replace hardcoded values:

| Old | New |
|---|---|
| `OWNER="cann"` | `OWNER="$CFG_OWNER"` |
| `REPO="${1:-hcomm}"` | `REPO="${1:-$CFG_DEFAULT_REPO}"` |
| `LOG_DIR="$SCRIPT_DIR/log/run"` | `LOG_DIR="$CFG_LOG_DIR/run"` |
| `CACHE_FILE="/tmp/..."` | `CACHE_FILE="$CFG_LOG_DIR/.review_loop_${OWNER}_${REPO}_shas"` |
| `https://gitcode.com/api/v5/repos/...` | `${CFG_API_BASE}/repos/...` |

Note: `CFG_API_BASE` is the full API base including `/api/v5` (e.g.
`https://api.gitcode.com/api/v5`). The shell script builds the full URL as
`${CFG_API_BASE}/repos/${OWNER}/${REPO}/pulls?...` — consistent with how
`ai_reviewer.py` uses `GITCODE_API_BASE`.

Note on hostname: `review_loop.sh` currently uses `https://gitcode.com/api/v5`
while `ai_reviewer.py` uses `https://api.gitcode.com/api/v5`. This substitution
intentionally unifies both to `api.gitcode.com` via `CFG_API_BASE`.

---

## Token Unification: REVIEW_TOKEN → GITCODE_TOKEN

`review_loop.sh` currently uses `REVIEW_TOKEN` while `ai_reviewer.py` uses `GITCODE_TOKEN`.
They represent the same credential. Unify to `GITCODE_TOKEN` — do **not** add token to
`config.yaml` (credentials must not be committed to version control).

Changes to `review_loop.sh`:

```bash
# Before
TOKEN="${REVIEW_TOKEN:?请设置环境变量 REVIEW_TOKEN}"

# After
TOKEN="${GITCODE_TOKEN:?请设置环境变量 GITCODE_TOKEN}"
```

Also update the comment on line 4:

```bash
# Before: # 环境变量：REVIEW_TOKEN(必需) REVIEW_INTERVAL(默认120)
# After:  # 环境变量：GITCODE_TOKEN(必需) REVIEW_INTERVAL(默认120)
```

---

## Changes to `README.md`

1. **持续轮询** usage example: add token export before the command.

```bash
export GITCODE_TOKEN=your_token
bash review_loop.sh hcomm teams/hccl.txt          # 轮询审查全部PR
bash review_loop.sh hcomm teams/hccl.txt PLZ      # 只审查标题含PLZ的PR
```

2. **配置项** table: replace the two hardcoded-constant rows with a `config.yaml` row,
   and remove any mention of `REVIEW_TOKEN`.

| 配置 | 说明 |
| --- | --- |
| `GITCODE_TOKEN` | GitCode 个人访问令牌（环境变量或 `--token` 参数，不放入 config.yaml） |
| `config.yaml` | 所有可调参数（owner、repos_root、api_base、max_diff_chars 等），见文件注释 |
| `--repo` | 目标仓库名，同时决定本地路径和 GitCode API 目标 |
| `--match` | 只审查标题包含该关键字的 PR |
| `teams/*.txt` | 团队成员名单，不纳入 git 托管，需自行创建 |

---

## Testing

- Existing tests in `tests/test_match_filter.py` should continue to pass unchanged
- Manual smoke test: run with default `config.yaml`, verify behavior identical to current
- Manual override test: change `owner`/`default_repo` in `config.yaml`, verify tool uses new values
- Edge case: set `max_parallel_reviews: 0` in `config.yaml`, verify it is respected (not silently dropped)
