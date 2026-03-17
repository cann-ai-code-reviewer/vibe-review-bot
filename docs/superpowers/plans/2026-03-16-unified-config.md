# Unified Config Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace all hardcoded values in `ai_reviewer.py` and `review_loop.sh` with a project-root `config.yaml` backed by a new `config.py` module.

**Architecture:** New `config.py` defines `AppConfig` dataclass with code-level defaults, reads and merges `config.yaml` at import time, and exposes a module-level `cfg` instance. `ai_reviewer.py` imports `cfg` and replaces all module-level constants. `review_loop.sh` reads `config.yaml` via a Python one-liner eval block. Token variable `REVIEW_TOKEN` is unified to `GITCODE_TOKEN`.

**Tech Stack:** Python 3, PyYAML ≥ 6.0, bash

**Spec:** `docs/superpowers/specs/2026-03-16-unified-config-design.md`

---

## Chunk 1: Foundation — requirements.txt, config.py, config.yaml

### Task 1: Add PyYAML dependency

**Files:**
- Create: `requirements.txt`

- [ ] **Step 1: Create requirements.txt**

```
pyyaml>=6.0
```

File path: `requirements.txt` (project root)

- [ ] **Step 2: Install the dependency**

```bash
pip install pyyaml
```

- [ ] **Step 3: Commit**

```bash
git add requirements.txt
git commit -m "chore: add pyyaml dependency"
```

---

### Task 2: Create config.py with AppConfig and load_config()

**Files:**
- Create: `config.py`
- Create: `tests/test_config.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_config.py`:

```python
import pytest
from pathlib import Path
from unittest.mock import patch
import tempfile, os, textwrap


def _write_yaml(tmp_path, content):
    p = tmp_path / "config.yaml"
    p.write_text(textwrap.dedent(content))
    return tmp_path


def test_defaults_when_no_config_file(tmp_path):
    """AppConfig defaults are used when config.yaml is absent."""
    from config import load_config, AppConfig
    cfg = load_config(tmp_path)
    assert cfg == AppConfig()


def test_yaml_overrides_defaults(tmp_path):
    """Values in config.yaml override AppConfig defaults."""
    _write_yaml(tmp_path, """
        owner: myorg
        default_repo: myrepo
        api_base: https://example.com/api/v5
        max_diff_chars: 40000
    """)
    from config import load_config
    cfg = load_config(tmp_path)
    assert cfg.owner == "myorg"
    assert cfg.default_repo == "myrepo"
    assert cfg.api_base == "https://example.com/api/v5"
    assert cfg.max_diff_chars == 40000
    # Unset fields keep defaults
    assert cfg.max_claude_turns == 40


def test_empty_string_keeps_default(tmp_path):
    """Empty string in yaml is treated as 'not set', keeping the code default.

    log_dir and team_file both default to "" in AppConfig, so the empty-string
    guard cannot be proven in isolation. We co-locate a non-empty override (owner)
    to prove YAML is being read at all, confirming the guard is exercised.
    """
    _write_yaml(tmp_path, """
        owner: myorg
        log_dir: ""
        team_file: ""
    """)
    from config import load_config, AppConfig
    cfg = load_config(tmp_path)
    assert cfg.owner == "myorg"    # proves YAML was read
    assert cfg.log_dir == ""       # empty string → guard skipped → default "" kept
    assert cfg.team_file == ""     # empty string → guard skipped → default "" kept


def test_unknown_keys_are_ignored(tmp_path):
    """Keys in config.yaml that don't match AppConfig fields are silently ignored."""
    _write_yaml(tmp_path, """
        owner: myorg
        unknown_future_option: some_value
    """)
    from config import load_config
    cfg = load_config(tmp_path)
    assert cfg.owner == "myorg"
    assert not hasattr(cfg, "unknown_future_option")


def test_integer_zero_is_respected(tmp_path):
    """Integer 0 in yaml is a valid value, not treated as 'not set'."""
    _write_yaml(tmp_path, """
        max_parallel_reviews: 0
    """)
    from config import load_config
    cfg = load_config(tmp_path)
    assert cfg.max_parallel_reviews == 0


def test_null_value_keeps_default(tmp_path):
    """Null (None) in yaml keeps the dataclass default."""
    _write_yaml(tmp_path, """
        owner: ~
        max_diff_chars: ~
    """)
    from config import load_config
    cfg = load_config(tmp_path)
    assert cfg.owner == "cann"
    assert cfg.max_diff_chars == 80000
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /home/guosj/Documents/github_repos/vibe-review-bot
pytest tests/test_config.py -v
```

Expected: `ModuleNotFoundError: No module named 'config'`

- [ ] **Step 3: Create config.py**

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

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_config.py -v
```

Expected: 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add config.py tests/test_config.py
git commit -m "feat: add AppConfig dataclass and load_config() in config.py"
```

---

### Task 3: Create config.yaml

**Files:**
- Create: `config.yaml`

- [ ] **Step 1: Create config.yaml with all defaults**

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

- [ ] **Step 2: Verify existing tests still pass**

```bash
pytest tests/ -v
```

Expected: all tests PASS

- [ ] **Step 3: Commit**

```bash
git add config.yaml
git commit -m "feat: add config.yaml with all configurable defaults"
```

---

## Chunk 2: Wire config.py into ai_reviewer.py

### Task 4: Replace module-level constants in ai_reviewer.py

**Files:**
- Modify: `ai_reviewer.py:86-132` (constants block)

- [ ] **Step 1: Add import and replace constants block**

At the top of the file, add the import alongside the other stdlib imports (before line 86):

```python
from config import cfg, SCRIPT_DIR
```

Then remove the now-redundant local definition at line 90:
```python
# DELETE this line:
SCRIPT_DIR = Path(__file__).resolve().parent
```

Both `config.py` and `ai_reviewer.py` live in the project root, so `SCRIPT_DIR` resolves
identically from either file. Importing it from `config` and removing the local definition
eliminates the duplicate and the potential shadowing risk.

Replace lines 87–132 of the constants block with the following (keep unchanged lines noted
in Step 1 notes below):

```python
from config import cfg, SCRIPT_DIR  # noqa: E402 — add with stdlib imports at top of file
```

Then replace lines 87–132 (the constants block) with:

```python
# ======================== 配置 ========================
GITCODE_API_BASE = cfg.api_base
OWNER = cfg.owner
REPOS_ROOT = Path(cfg.repos_root) if cfg.repos_root else SCRIPT_DIR.parent.parent.parent
MAX_DIFF_CHARS = cfg.max_diff_chars
MAX_CLAUDE_TURNS = cfg.max_claude_turns
MIN_REVIEW_CHARS = cfg.min_review_chars
MAX_PARALLEL_REVIEWS = cfg.max_parallel_reviews
MAX_DIR_FILES = cfg.max_dir_files
DEFAULT_MODEL = cfg.default_model
LOG_DIR = Path(cfg.log_dir) if cfg.log_dir else SCRIPT_DIR / "log"
TRACKING_DB = LOG_DIR / "review_tracking.db"
TEAM_FILE = Path(cfg.team_file) if cfg.team_file else SCRIPT_DIR / "teams" / "hccl.txt"
```

Keep the following lines unchanged (they are not configurable):
- `SKILL_MD_PATH` (line 95)
- `MAX_COMMENT_CHARS` (line 97)
- `USD_TO_CNY` (line 102)
- `MODEL_PRICING` (lines 106–113)
- `AI_REVIEW_MARKER` (line 118)
- `AI_INLINE_MARKER` (line 120)
- `FILE_REVIEW_TOOLS` / `PR_REVIEW_TOOLS` (lines 135–142)

Note: move `from config import cfg, SCRIPT_DIR` to sit with the other imports at the
top of the file (after the stdlib imports, before the `# ======================== 配置` comment).
Delete line 90 (`SCRIPT_DIR = Path(__file__).resolve().parent`) — it is replaced by the import.
Keep `_review_model = DEFAULT_MODEL` (line 116) unchanged — it is the module-level initializer
for the global and must not be removed. After `DEFAULT_MODEL` is replaced with `cfg.default_model`,
this line automatically reflects the configured default.

- [ ] **Step 2: Run existing tests**

```bash
pytest tests/ -v
```

Expected: all tests PASS

- [ ] **Step 3: Quick smoke check — import succeeds**

```bash
python3 -c "import ai_reviewer; print('OK')"
```

Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add ai_reviewer.py
git commit -m "feat: wire cfg into ai_reviewer.py constants block"
```

---

### Task 5: Fix RepoConfig log path properties and _migrate_legacy_logs

**Files:**
- Modify: `ai_reviewer.py:164-183` (RepoConfig properties + _migrate_legacy_logs)

- [ ] **Step 1: Update pr_log_dir, file_log_dir, dir_log_dir**

Replace lines 164–174:

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

- [ ] **Step 2: Fix _migrate_legacy_logs (line 181)**

Replace the `new = ...` line:

```python
# Before
new = SCRIPT_DIR / "log" / repo.owner / repo.name / subdir

# After
new = LOG_DIR / repo.owner / repo.name / subdir
```

- [ ] **Step 3: Run tests and smoke check**

```bash
pytest tests/ -v
python3 -c "import ai_reviewer; print('OK')"
```

Expected: all PASS

- [ ] **Step 4: Commit**

```bash
git add ai_reviewer.py
git commit -m "fix: use config-driven LOG_DIR in RepoConfig paths and _migrate_legacy_logs"
```

---

### Task 6: Update argparse defaults in ai_reviewer.py

**Files:**
- Modify: `ai_reviewer.py:3354-3388` (argument parser)

- [ ] **Step 1: Update --model and --repo defaults**

Line 3354 — change `default=DEFAULT_MODEL` (this is already `cfg.default_model` via the
constant, no code change needed here — verify it reads the right value).

Line 3356 — change `default="hcomm"` to `default=cfg.default_repo`:

```python
# Before
parser.add_argument("--repo", type=str, default="hcomm", dest="target_repo",

# After
parser.add_argument("--repo", type=str, default=cfg.default_repo, dest="target_repo",
```

Line 3388 — change `OWNER` fallback to `cfg.owner` (already satisfied by the constant
replacement in Task 4 — verify no further change needed).

- [ ] **Step 2: Run tests and smoke check**

```bash
pytest tests/ -v
python3 -c "import ai_reviewer; print('OK')"
```

Expected: all PASS

- [ ] **Step 3: Commit**

```bash
git add ai_reviewer.py
git commit -m "feat: use cfg.default_repo as --repo argparse default"
```

---

## Chunk 3: review_loop.sh — config reading and token unification

### Task 7: Update review_loop.sh

**Files:**
- Modify: `review_loop.sh`

- [ ] **Step 1: Replace REVIEW_TOKEN with GITCODE_TOKEN (lines 4 and 11)**

Line 4 comment:
```bash
# Before: # 环境变量：REVIEW_TOKEN(必需) REVIEW_INTERVAL(默认120)
# After:  # 环境变量：GITCODE_TOKEN(必需) REVIEW_INTERVAL(默认120)
```

Line 11:
```bash
# Before
TOKEN="${REVIEW_TOKEN:?请设置环境变量 REVIEW_TOKEN}"

# After
TOKEN="${GITCODE_TOKEN:?请设置环境变量 GITCODE_TOKEN}"
```

- [ ] **Step 2: Replace hardcoded values throughout the script (line numbers for original file)**

Apply these text replacements before inserting the eval block, so line numbers stay stable:

| Line | Before | After |
|------|--------|-------|
| 10 | `OWNER="cann"` | `OWNER="$CFG_OWNER"` |
| 13 | `REPO="${1:-hcomm}"` | `REPO="${1:-$CFG_DEFAULT_REPO}"` |
| 18 | `LOG_DIR="$SCRIPT_DIR/log/run"` | `LOG_DIR="$CFG_LOG_DIR/run"` |
| 23 | `CACHE_FILE="/tmp/.review_loop_${OWNER}_${REPO}_shas"` | `CACHE_FILE="$CFG_LOG_DIR/.review_loop_${OWNER}_${REPO}_shas"` |
| 32 | `"https://gitcode.com/api/v5/repos/${OWNER}/${REPO}/pulls?state=open&per_page=100"` | `"${CFG_API_BASE}/repos/${OWNER}/${REPO}/pulls?state=open&per_page=100"` |

- [ ] **Step 3: Insert config.yaml reading block after SCRIPT_DIR is set (after line 9)**

After the text replacements, insert the following block immediately after line 9
(`SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"`):

```bash
# 从 config.yaml 读取配置（SCRIPT_DIR 通过环境变量传递，避免路径中特殊字符问题）
eval "$(VIBE_SCRIPT_DIR="$SCRIPT_DIR" python3 -c "
import yaml, pathlib, os
p = pathlib.Path(os.environ['VIBE_SCRIPT_DIR']) / 'config.yaml'
cfg = yaml.safe_load(p.read_text()) if p.exists() else {}
print('CFG_OWNER=' + str(cfg.get('owner', 'cann')))
print('CFG_DEFAULT_REPO=' + str(cfg.get('default_repo', 'hcomm')))
print('CFG_API_BASE=' + str(cfg.get('api_base', 'https://api.gitcode.com/api/v5')))
log = cfg.get('log_dir') or os.environ['VIBE_SCRIPT_DIR'] + '/log'
print('CFG_LOG_DIR=' + log)
" 2>/dev/stderr)" || { echo "ERROR: failed to read config.yaml (is pyyaml installed?)"; exit 1; }
```

Note: `2>/dev/stderr` keeps Python error output visible in the terminal/log without
mixing it into the `eval` input (which would cause confusing shell syntax errors on
Python tracebacks). `config.yaml` is developer-controlled trusted input; values are
not shell-escaped before `eval`.

- [ ] **Step 4: Smoke test the shell script syntax**

```bash
bash -n review_loop.sh
```

Expected: no output (syntax OK)

- [ ] **Step 5: Verify config block works standalone**

```bash
cd /home/guosj/Documents/github_repos/vibe-review-bot
VIBE_SCRIPT_DIR="$(pwd)" python3 -c "
import yaml, pathlib, os
p = pathlib.Path(os.environ['VIBE_SCRIPT_DIR']) / 'config.yaml'
cfg = yaml.safe_load(p.read_text()) if p.exists() else {}
print('CFG_OWNER=' + str(cfg.get('owner', 'cann')))
print('CFG_DEFAULT_REPO=' + str(cfg.get('default_repo', 'hcomm')))
print('CFG_API_BASE=' + str(cfg.get('api_base', 'https://api.gitcode.com/api/v5')))
log = cfg.get('log_dir') or os.environ['VIBE_SCRIPT_DIR'] + '/log'
print('CFG_LOG_DIR=' + log)
"
```

Expected output (matching config.yaml defaults):
```
CFG_OWNER=cann
CFG_DEFAULT_REPO=hcomm
CFG_API_BASE=https://api.gitcode.com/api/v5
CFG_LOG_DIR=<path>/log
```

- [ ] **Step 6: Run all tests**

```bash
pytest tests/ -v
```

Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add review_loop.sh
git commit -m "feat: read config.yaml in review_loop.sh, unify REVIEW_TOKEN→GITCODE_TOKEN"
```

---

## Chunk 4: README and final cleanup

### Task 8: Update README.md

**Files:**
- Modify: `README.md:117-122` (持续轮询 section)
- Modify: `README.md:151-161` (配置项 table)

- [ ] **Step 1: Update 持续轮询 code block (around line 119)**

```bash
# Before
bash review_loop.sh hcomm teams/hccl.txt          # 轮询审查全部PR
bash review_loop.sh hcomm teams/hccl.txt PLZ      # 只审查标题含PLZ的PR

# After
export GITCODE_TOKEN=your_token
bash review_loop.sh hcomm teams/hccl.txt          # 轮询审查全部PR
bash review_loop.sh hcomm teams/hccl.txt PLZ      # 只审查标题含PLZ的PR
```

- [ ] **Step 2: Update 配置项 table (around line 153)**

Replace the existing table with:

```markdown
| 配置               | 说明                                                                                 |
| ------------------ | ------------------------------------------------------------------------------------ |
| `GITCODE_TOKEN`    | GitCode 个人访问令牌（环境变量或 `--token` 参数，不写入 config.yaml）                |
| `config.yaml`      | 所有可调参数（owner、repos_root、api_base、max_diff_chars 等），见文件注释            |
| `--repo`           | 目标仓库名，同时决定本地路径`~/repo/cann/<repo>/`和GitCode API目标`cann/<repo>`      |
| `--match`          | 只审查标题包含该关键字的PR（全字匹配，大小写不敏感，`--pr`模式下忽略）               |
| `teams/*.txt`      | 团队成员名单，按仓库命名（如`hcomm.txt`），不纳入git托管，需自行创建。格式见下方说明 |
```

- [ ] **Step 3: Run all tests**

```bash
pytest tests/ -v
```

Expected: all PASS

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: update README for config.yaml and GITCODE_TOKEN unification"
```

---

### Task 9: Final verification

- [ ] **Step 1: Run full test suite**

```bash
pytest tests/ -v
```

Expected: all PASS

- [ ] **Step 2: Verify no remaining REVIEW_TOKEN references**

```bash
grep -rn "REVIEW_TOKEN" . --include="*.py" --include="*.sh" --include="*.md"
```

Expected: no output

- [ ] **Step 3: Verify no remaining hardcoded owner/repo/api in source**

```bash
grep -n 'OWNER = "cann"\|default="hcomm"\|GITCODE_API_BASE = "https' ai_reviewer.py
```

Expected: no output

```bash
grep -n "gitcode.com/api/v5" review_loop.sh
```

Expected: no output (old hostname replaced by `${CFG_API_BASE}`)

- [ ] **Step 4: Verify config.yaml is present and parseable**

```bash
python3 -c "import yaml; cfg = yaml.safe_load(open('config.yaml')); print(cfg)"
```

Expected: dict with all config keys printed

- [ ] **Step 5: Final commit if any loose changes**

```bash
git status
# if clean, nothing to do
```
