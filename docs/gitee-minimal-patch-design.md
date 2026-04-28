# Gitee 平台支持 — 最小化补丁设计方案

> 在不引入平台抽象层的前提下，通过 `if/else` 分支让现有代码同时支持 GitCode 和 Gitee。
> 目标：快、稳、可验证。抽象层后续再搞。

---

## 1. 设计原则

1. **改动最小化**：只在必要处加 `if repo.platform == "gitee"`，不改无关代码
2. **向后兼容**：不配置 platform 时默认走 GitCode 老逻辑，不影响现有用户
3. **一处一判断**：同一平台差异的 if/else 尽量集中在同一函数内，不分散到多个地方
4. **可测试**：每处改动都配有白盒/黑盒测试用例

---

## 2. 改动清单与实现方案

### 2.1 配置层：`config.py` + `config.yaml`

**改动**：`AppConfig` 新增 `platform` 字段

```python
# config.py
@dataclass
class AppConfig:
    # ... 原有字段 ...
    platform: str = "gitcode"   # 新增
    api_base: str = "https://api.gitcode.com/api/v5"
```

```yaml
# config.yaml
platform: "gitcode"   # 可选，默认 gitcode；改为 "gitee" 即切换
api_base: "https://api.gitcode.com/api/v5"
```

**向后兼容**：不传 `platform` 时默认 `"gitcode"`

---

### 2.2 仓库配置：`RepoConfig`

**改动**：`RepoConfig` 新增 `platform` 字段，`url` 属性根据平台切换

```python
@dataclass
class RepoConfig:
    name: str
    owner: str
    path: Path
    platform: str = "gitcode"   # 新增

    @property
    def url(self) -> str:
        if self.platform == "gitee":
            return f"https://gitee.com/{self.owner}/{self.name}"
        return f"https://gitcode.com/{self.owner}/{self.name}"

    @property
    def api_prefix(self) -> str:
        return f"/repos/{self.owner}/{self.name}"
```

**构造处改动**：`main()` 中初始化 `RepoConfig` 时传入 `platform`

```python
repo = RepoConfig(
    name=repo_name, owner=repo_owner, path=repo_path,
    platform=cfg.platform,   # 新增
)
```

---

### 2.3 行内评论：`_build_diff_position_map` + `_post_inline_comments`

这是**唯一真正的核心难点**。

#### 背景

| 平台 | `position` 参数含义 |
|------|--------------------|
| GitCode | **源码行号**（文件中的绝对行号） |
| Gitee | **diff 块内相对行号**（从 `@@` 后第一行开始计数 1，每个 diff 块独立） |

当前代码中 `_build_diff_position_map` 返回的 `position`（diff 相对行号）**在 GitCode 场景下根本没被用到**——`_post_inline_comments` 直接传了 `finding.line`（源码行号）。但 Gitee 需要传 diff 相对行号。

#### 方案

**`_build_diff_position_map`**：按平台返回不同的 position

```python
def _build_diff_position_map(raw_diff: str, platform: str = "gitcode") -> dict[int, tuple[int, bool]]:
    """解析单个文件的 raw diff，建立行号→position 映射。

    返回：{new_line_number: (position, is_added)}
    - position: 平台相关的定位值
      - GitCode: 源码行号（为了兼容现有调用习惯，position = new_line）
      - Gitee: diff 块内相对行号（从 1 开始，每个块独立）
    - is_added: True 表示 '+' 行
    """
    mapping: dict[int, tuple[int, bool]] = {}
    new_line = 0
    in_hunk = False

    # Gitee 专用计数器
    gitee_position = 0

    for line in raw_diff.split("\n"):
        if not line and in_hunk:
            continue  # 跳过尾部空行

        if line.startswith("@@"):
            match = re.search(r"\+(\d+)", line)
            if match:
                new_line = int(match.group(1)) - 1
            in_hunk = True
            if platform == "gitee":
                gitee_position = 0  # 每个 diff 块重新从 0 开始，下面先+1
            continue

        if not in_hunk:
            continue

        if platform == "gitee":
            gitee_position += 1
            position = gitee_position
        else:
            position = new_line + 1  # GitCode 用源码行号

        if line.startswith("+"):
            new_line += 1
            mapping[new_line] = (position, True)
        elif line.startswith("-"):
            pass  # 删除行不增加 new_line，也不进 mapping
        elif line.startswith("\\"):
            pass
        else:
            new_line += 1
            mapping[new_line] = (position, False)

    return mapping
```

**`_post_inline_comments`**：调用时传入 `repo.platform`，发布时传 `position` 而非 `finding.line`

```python
# 当前代码（GitCode）
resp = api_post_form(
    f"{repo.api_prefix}/pulls/{pr_number}/comments",
    token,
    {"body": comment_body, "commit_id": commit_id,
     "path": finding.file, "position": finding.line},
)

# 改为：
if repo.platform == "gitee":
    resp = api_post(
        f"{repo.api_prefix}/pulls/{pr_number}/comments",
        token,
        {"body": comment_body, "commit_id": commit_id,
         "path": finding.file, "position": _position},   # ← diff 相对行号
    )
else:
    resp = api_post_form(
        f"{repo.api_prefix}/pulls/{pr_number}/comments",
        token,
        {"body": comment_body, "commit_id": commit_id,
         "path": finding.file, "position": finding.line},  # ← 源码行号
    )
```

---

### 2.4 评论链接：`_resolve_comment_url`

```python
def _resolve_comment_url(resp: dict, repo: RepoConfig, token: str, pr_number: int) -> str | None:
    did = str(resp["id"])

    if repo.platform == "gitee":
        # Gitee: https://gitee.com/{owner}/{repo}/pulls/{n}#note_{id}
        return f"{repo.url}/pulls/{pr_number}#note_{did}"

    # GitCode 原有逻辑
    base = f"{repo.url}/merge_requests/{pr_number}?ref=&did={did}"
    notes = resp.get("notes")
    if isinstance(notes, list) and notes:
        nid = notes[0].get("id")
        if isinstance(nid, int):
            return f"{base}#tid-{nid}"
    # 回退 GET...
    return base
```

---

### 2.5 PR 总结评论：`post_review_comment`

```python
# markdown 链接中的路径切换
pr_path = "pulls" if repo.platform == "gitee" else "merge_requests"
f"| 链接 | [{repo.url}/{pr_path}/{pr_number}]({repo.url}/{pr_path}/{pr_number}) |\n"
```

---

### 2.6 入口与认证

**环境变量**：兼容处理，优先读平台特定的，再读通用的

```python
# main() 中
env_var = "GITEE_TOKEN" if cfg.platform == "gitee" else "GITCODE_TOKEN"
token = args.token or os.environ.get(env_var) or os.environ.get("VIBE_TOKEN")

if not token:
    platform_name = "Gitee" if cfg.platform == "gitee" else "GitCode"
    print(f"  {_fail(f'未提供 {platform_name} 访问令牌')}")
    # ...
```

---

### 2.7 `review_loop.sh`

```bash
# 从 config.yaml 读取 platform
print('CFG_PLATFORM=' + _sh(cfg.get('platform', 'gitcode')))

# 认证环境变量兼容
if cfg.get('platform') == 'gitee':
    TOKEN="${GITEE_TOKEN:?请设置环境变量 GITEE_TOKEN}"
else:
    TOKEN="${GITCODE_TOKEN:?请设置环境变量 GITCODE_TOKEN}"
fi

# api_base 也支持平台化默认值
if cfg.get('platform') == 'gitee':
    default_api='https://gitee.com/api/v5'
else:
    default_api='https://api.gitcode.com/api/v5'
fi
```

---

## 3. 测试用例

### 3.1 白盒测试（单元测试）

#### TC-W01: `_build_diff_position_map` — 单 diff 块 + Gitee

```python
def test_position_map_gitee_single_hunk():
    raw = "@@ -69,3 +69,4 @@ git apply\n" \
          " git apply --whitespace=nowarn a\n" \
          " git apply --whitespace=nowarn b\n" \
          " git apply --whitespace=nowarn c\n" \
          "+git apply --whitespace=nowarn d\n"

    mapping = _build_diff_position_map(raw, platform="gitee")

    # 源码行号 → (position, is_added)
    assert mapping[69] == (1, False)   # 上下文行
    assert mapping[70] == (2, False)   # 上下文行
    assert mapping[71] == (3, False)   # 上下文行
    assert mapping[72] == (4, True)    # 新增行
```

#### TC-W02: `_build_diff_position_map` — 多 diff 块 + Gitee

```python
def test_position_map_gitee_multi_hunk():
    raw = "@@ -10,2 +10,3 @@\n" \
          " context A\n" \
          " context B\n" \
          "+added C\n" \
          "@@ -20,2 +21,3 @@\n" \
          " context D\n" \
          " context E\n" \
          "+added F\n"

    mapping = _build_diff_position_map(raw, platform="gitee")

    # 第一个块
    assert mapping[10] == (1, False)
    assert mapping[11] == (2, False)
    assert mapping[12] == (3, True)   # position=3（块内相对）

    # 第二个块：position 从 1 重新开始
    assert mapping[21] == (1, False)
    assert mapping[22] == (2, False)
    assert mapping[23] == (3, True)   # position=3（块内相对，非跨块连续）
```

#### TC-W03: `_build_diff_position_map` — 向后兼容（GitCode）

```python
def test_position_map_gitcode_backward_compat():
    raw = "@@ -10,2 +10,3 @@\n" \
          " context A\n" \
          " context B\n" \
          "+added C\n"

    mapping = _build_diff_position_map(raw, platform="gitcode")

    # GitCode: position = 源码行号
    assert mapping[10] == (10, False)
    assert mapping[11] == (11, False)
    assert mapping[12] == (12, True)
```

#### TC-W04: `RepoConfig.url` — 平台切换

```python
def test_repo_config_url():
    gc = RepoConfig(name="repo", owner="org", path=Path("/tmp"), platform="gitcode")
    assert gc.url == "https://gitcode.com/org/repo"

    gt = RepoConfig(name="repo", owner="org", path=Path("/tmp"), platform="gitee")
    assert gt.url == "https://gitee.com/org/repo"
```

---

### 3.2 黑盒测试（集成/端到端）

#### TC-B01: 完整 PR 审查流程 — Gitee

**前置条件**：
- config.yaml 中 `platform: gitee`、`api_base: https://gitee.com/api/v5`
- 环境变量 `GITEE_TOKEN` 已设置
- 目标仓库有一个 open PR 且有文件变更

**执行步骤**：
```bash
python3 ai_reviewer.py \
  --repo omniai/omniinfer \
  --pr 2227 \
  --comment \
  --inline
```

**预期结果**：
1. 脚本成功拉取 PR diff
2. 行内评论成功发布到具体代码行（position 正确）
3. 评论链接可点击跳转到正确代码位置
4. 再次运行同一 PR 时，旧的 AI 行内评论被正确删除

#### TC-B02: 完整 PR 审查流程 — GitCode（回归测试）

**前置条件**：
- config.yaml 中 `platform: gitcode`（或省略，默认）
- 环境变量 `GITCODE_TOKEN` 已设置

**执行步骤**：同 TC-B01

**预期结果**：与改造前行为完全一致，无任何回归

#### TC-B03: 行内评论定位准确性 — Gitee

**测试方法**：针对一个多 diff 块的 PR（每个文件有多个 `@@` 块），手动检查每条行内评论是否定位到了正确的代码行。

**通过标准**：
- 新增行的评论准确落在新增代码上
- 上下文行的评论（如 "建议" 级别）准确落在对应上下文行
- 无评论飘移到其他 diff 块的情况

#### TC-B04: 评论删除 — Gitee

**测试方法**：
1. 先运行一次 `--comment --inline` 产生 AI 评论
2. 再次运行同一 PR
3. 验证旧的 AI 总结评论和行内评论均被删除

**通过标准**：第二次运行后，PR 页面无历史 AI 评论残留

---

### 3.3 边界场景

| 场景 | 预期行为 |
|------|---------|
| `status` 字段为 `null`（Gitee） | `get_file_status()` fallback 到 `patch.new_file`/`deleted_file`/`renamed_file`，返回正确状态 |
| PR 文件数量超过 300（Gitee 限制） | API 返回前 300 个文件，脚本正常处理 |
| 行内评论 position 越界 | Gitee API 返回 422 错误，脚本捕获并跳过该条 finding |
| `platform` 字段拼写错误（如 `"gitee"` 写成 `"giteee"`） | 默认走 GitCode 逻辑（向后兼容），但日志提示 "未知平台，默认使用 gitcode" |
| `config.yaml` 中 platform 和 api_base 不匹配（如 platform=gitee, api_base=gitcode.com） | 以 `api_base` 为准，但日志 warning 提示配置不一致 |

---

## 4. 实施顺序建议

```
Step 1: 配置层改造（config.py + config.yaml + RepoConfig）
        ↓ 验证：TC-W04
Step 2: _build_diff_position_map 平台分支
        ↓ 验证：TC-W01 / TC-W02 / TC-W03
Step 3: _post_inline_comments + _resolve_comment_url 平台分支
        ↓ 验证：TC-B01（Gitee 行内评论发布）
Step 4: post_review_comment 链接路径 + main() 认证
        ↓ 验证：TC-B01（总结评论）+ TC-B04（删除）
Step 5: review_loop.sh 平台化
        ↓ 验证：TC-B01（轮询场景）
Step 6: TC-B02（GitCode 回归测试）
```

---

## 5. 风险与回滚

| 风险 | 缓解措施 |
|------|---------|
| `_build_diff_position_map` 改动影响 GitCode 现有逻辑 | 保留 `platform="gitcode"` 分支，确保行为 100% 不变 |
| Gitee 行内评论 position 计算仍有偏差 | 先在小 PR 上测试，验证定位准确后再上生产 |
| `patch` 字段在特定场景下格式不同 | `get_file_diff()` 已兼容 dict/string，兜底可用 |
| 并发删除评论时 Gitee API 限流 | 现有 ThreadPoolExecutor(max_workers=5) 已控制并发，无需调整 |

**回滚方案**：
- 若改造后 GitCode 出现问题，将 `platform` 恢复为 `"gitcode"`（默认值）即可
- 若 Gitee 有问题，将 `platform` 切回 `"gitcode"`，无需改代码

---

## 6. 代码风格约定

1. 所有平台判断统一用 `repo.platform == "gitee"`，不用 `repo.platform != "gitcode"`
2. 新增平台判断时，在注释中标注 `platform: gitee` 和 `platform: gitcode` 的行为差异
3. 不引入新的全局变量，平台配置通过 `RepoConfig` 传递
4. `_build_diff_position_map` 的 `platform` 参数默认 `"gitcode"`，确保所有现有调用点无需修改
