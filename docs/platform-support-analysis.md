# GitCode 平台支持现状分析

> 本文档系统梳理当前代码库中 GitCode 平台支持的实现方式，为后续接入 Gitee（或其他平台）提供改造依据。

---

## 1. 概述

当前代码库**没有平台抽象层**。GitCode 不是作为一个可插拔的"平台适配器"存在，而是作为**唯一被假设的平台**，其 URL 格式、API 行为、认证方式、响应结构被硬编码在脚本的每一处关键路径中。

核心影响文件：

| 文件 | 说明 |
|------|------|
| `ai_reviewer.py` | 主脚本（~3800 行），GitCode 耦合最集中 |
| `config.py` | 配置模型，默认值写死 GitCode API base |
| `config.yaml` | 用户配置，默认 `api_base` 指向 GitCode |
| `review_loop.sh` | 轮询脚本，curl 直接调 GitCode API |

---

## 2. 逐层详解

### 2.1 配置层

`config.py` 的 `AppConfig` 将 GitCode API base 作为默认值：

```python
# config.py:14
api_base: str = "https://api.gitcode.com/api/v5"
```

运行时直接导出为模块级全局变量：

```python
# ai_reviewer.py:89
GITCODE_API_BASE = cfg.api_base
```

同时，`MAX_COMMENT_CHARS = 60000` 也是按 GitCode 的单条评论长度限制硬编码的。

**Gitee 改造要点**：需要支持按平台切换 `api_base`，且不同平台的评论长度限制可能不同。

---

### 2.2 仓库配置：`RepoConfig`

`RepoConfig` 直接拼死 GitCode 的域名和 API 路径格式：

```python
# ai_reviewer.py:157-163
@property
def url(self) -> str:
    return f"https://gitcode.com/{self.owner}/{self.name}"

@property
def api_prefix(self) -> str:
    return f"/repos/{self.owner}/{self.name}"
```

- `url` 直接输出 `https://gitcode.com/...`，用于生成 PR 评论中的 markdown 链接。
- `api_prefix` 生成 `/repos/{owner}/{repo}`，被所有 API 调用拼接路径。

**Gitee 改造要点**：`url` 需要变成 `https://gitee.com/...`；`api_prefix` 虽然格式相同，但评论/讨论相关的 Web 链接格式完全不同（见 2.5 节）。

---

### 2.3 API 客户端层

#### 2.3.1 认证方式：token 放在 query param

`_api_request` 将 `access_token` 塞进 URL query string：

```python
# ai_reviewer.py:733
params["access_token"] = token
url = f"{GITCODE_API_BASE}{path}?{urlencode(params)}"
```

GitCode 支持这种传法，但 Gitee V5 更规范的认证方式是放在 `Authorization` header 中（`Authorization: token xxx`）。如果仅换 `api_base`，Gitee 可能不会认 query param 的 token。

#### 2.3.2 `api_post_form()`：GitCode 行内评论的 workaround

```python
# ai_reviewer.py:770-797
def api_post_form(path: str, token: str, fields: dict) -> dict | list | None:
    """GitCode 部分 API（如行内评论）仅在 form-encoded 格式下正确处理
    path/position/commit_id 等字段，JSON 格式会被静默忽略。"""
    fields["access_token"] = token
    data = urlencode(fields).encode("utf-8")
    headers = {"Content-Type": "application/x-www-form-urlencoded",
               "Accept": "application/json"}
```

这是专门为 GitCode 行内评论接口写的 workaround：GitCode 接收 JSON 时会**静默忽略** `path`/`position`/`commit_id`，必须用 `x-www-form-urlencoded` 才能生效。

**Gitee 改造要点**：
- 认证方式（query param `access_token`）已验证兼容 Gitee，无需修改。
- 行内评论接口已验证**仅接受 JSON**，`api_post_form` 在 Gitee 下不需要，行内评论可直接用 `api_post()`。

---

### 2.4 PR diff 获取与解析

#### 2.4.1 `fetch_pr_files()`：GitCode 特有的嵌套 patch

```python
# ai_reviewer.py:944-954
"""GitCode API 返回格式:
  [{
    "sha": "...", "filename": "path/to/file.cc",
    "additions": 5, "deletions": 3,
    "patch": {
      "diff": "--- a/...\n+++ b/...\n@@...",
      "old_path": "...", "new_path": "...",
      "new_file": false, "renamed_file": false, "deleted_file": false,
      "added_lines": 5, "removed_lines": 3
    }
  }, ...]
"""
```

GitCode 的 `patch` 是一个**嵌套对象**，diff 文本在 `patch.diff` 里，文件状态在 `patch.new_file` / `patch.deleted_file` / `patch.renamed_file` 布尔标志里。

#### 2.4.2 解析函数 trio

```python
# ai_reviewer.py:966-999
def get_file_diff(file_entry: dict) -> str:
    patch = file_entry.get("patch", "")
    if isinstance(patch, dict):
        # GitCode 格式：patch 是嵌套对象，diff 在 patch.diff 字段
        return patch.get("diff", "")
    # 兼容 GitHub 格式：patch 直接是字符串
    return patch

def get_file_status(file_entry: dict) -> str:
    status = file_entry.get("status", "")
    if status:
        return status
    patch = file_entry.get("patch", {})
    if isinstance(patch, dict):
        if patch.get("new_file"): return "added"
        if patch.get("deleted_file"): return "removed"
        if patch.get("renamed_file"): return "renamed"
    return "modified"

def get_filename(file_entry: dict) -> str:
    patch = file_entry.get("patch", {})
    if isinstance(patch, dict):
        return patch.get("new_path", "") or file_entry.get("filename", "unknown")
    return file_entry.get("filename", "unknown")
```

这三个函数构成了 diff 解析的核心路径。`get_file_diff()` 留了一个 GitHub 兼容的 fallback（`patch` 直接是字符串），但 GitCode 的嵌套对象是当前的主要路径。

**Gitee 改造要点**：已验证 — Gitee 的 `patch` 也是**嵌套对象**（和 GitCode 完全一致），内部字段（`diff`、`new_path`、`old_path`、`new_file`、`deleted_file`、`renamed_file`）完全相同。现有的 `get_file_diff()`、`get_file_status()`、`get_filename()` 三个解析函数**无需任何修改**，直接兼容 Gitee。

唯一的小差异：Gitee 的 `status` 字段可能为 `null`（而非字符串），但现有 fallback 逻辑（从 `patch` 对象的布尔标志推导）已能正确处理。

---

### 2.5 评论生命周期

#### 2.5.1 评论链接构建：GitCode discussion 格式

```python
# ai_reviewer.py:2821-2845
def _resolve_comment_url(resp: dict, repo: RepoConfig, token: str, pr_number: int) -> str | None:
    did = str(resp["id"])
    base = f"{repo.url}/merge_requests/{pr_number}?ref=&did={did}"

    notes = resp.get("notes")
    if isinstance(notes, list) and notes:
        nid = notes[0].get("id")
        if isinstance(nid, int):
            return f"{base}#tid-{nid}"

    # 回退：GET 最近的评论，按 discussion_id 匹配
    comments = api_get(
        f"{repo.api_prefix}/pulls/{pr_number}/comments", token,
        {"page": 1, "per_page": 5, "sort": "created", "direction": "desc"},
    )
    ...
    return f"{base}#tid-{c['id']}"
```

链接格式是 GitCode MR 讨论区特有的：`https://gitcode.com/{owner}/{repo}/merge_requests/{n}?ref=&did={did}#tid-{nid}`。

#### 2.5.2 PR 总结评论发布

```python
# ai_reviewer.py:2867-2868
f"| 链接 | [{repo.url}/merge_requests/{pr_number}]({repo.url}/merge_requests/{pr_number}) |\n"
```

markdown 链接直接拼的是 `/merge_requests/` 路径。

#### 2.5.3 删除旧评论

```python
# ai_reviewer.py:1771
pool.submit(api_delete, f"{repo.api_prefix}/pulls/comments/{cid}", token)
```

假设删除接口为 `/repos/{owner}/{repo}/pulls/comments/{id}`。

#### 2.5.4 行内评论发布

```python
# ai_reviewer.py:3029-3034
resp = api_post_form(
    f"{repo.api_prefix}/pulls/{pr_number}/comments",
    token,
    {"body": comment_body, "commit_id": commit_id,
     "path": finding.file, "position": finding.line},
)
```

- 使用 `api_post_form`（form-encoded workaround）。
- 参数名是 `position`，值为**源码行号**（不是 diff 中的相对位置）。

**Gitee 改造要点**：
- 评论链接格式完全不同，需要重新实现 `_resolve_comment_url()`（Gitee 使用 `#note_{id}` 而非 GitCode 的 `?did={did}#tid-{nid}`）。
- 总结评论中的 markdown 链接需要改为 Gitee 的 `/pulls/` 路径。
- 删除接口路径已验证一致（`DELETE /pulls/comments/{id}`）。
- 行内评论参数名相同（`position`），但**含义完全不同**：GitCode 是源码行号，Gitee 是 diff 块内相对行号。

---

### 2.6 入口与认证：`GITCODE_TOKEN`

```python
# ai_reviewer.py:3411-3417
token = args.token or os.environ.get("GITCODE_TOKEN")
if not token:
    print(f"  {_fail('未提供 GitCode 访问令牌')}")
    print("  请通过以下任一方式提供:")
    print("    1. 环境变量：export GITCODE_TOKEN=your_token")
    print("    2. 命令行：  python3 ai_reviewer.py --token your_token")
```

`main()` 里有多处这种判断，提示文案直接写的是 "GitCode"。

**Gitee 改造要点**：环境变量名和提示文案需要平台化，或者统一为中性名称如 `PLATFORM_TOKEN` / `VIBE_TOKEN`。

---

### 2.7 轮询脚本：`review_loop.sh`

```bash
# review_loop.sh:19
print('CFG_API_BASE=' + _sh(cfg.get('api_base') or 'https://api.gitcode.com/api/v5'))

# review_loop.sh:25
TOKEN="${GITCODE_TOKEN:?请设置环境变量 GITCODE_TOKEN}"

# review_loop.sh:45-46
curl -s -H "PRIVATE-TOKEN: $TOKEN" \
  "${CFG_API_BASE}/repos/${OWNER}/${REPO}/pulls?state=open&per_page=100"
```

注意：这里用的是 `PRIVATE-TOKEN` header 认证，而 Python 代码用的是 query param `access_token`。GitCode 两种都支持，但 Gitee 通常只认 `Authorization: token xxx` 或 query param。

**Gitee 改造要点**：
- 环境变量名需要统一。
- curl 的认证方式已验证兼容（query param `access_token` 可直接复用，或改用 `Authorization: token $TOKEN`）。
- 响应字段（`user.login`、`head.sha`、`base.sha`、`number`）已验证与 GitCode 完全一致。

---

## 3. 改造方案对比

| 方案 | 思路 | 优点 | 缺点 |
|------|------|------|------|
| **A. 最小化补丁** | 在现有代码中增加 `if platform == 'gitcode'` / `'gitee'` 分支 | 改动量小，快速可用 | 继续积累技术债务，后续再加平台会更痛苦 |
| **B. 平台抽象重构** | 提取 `PlatformClient` 协议/接口，实现 `GitCodeClient` / `GiteeClient` | 架构清晰，后续加平台只需新增实现类 | 初期改动量大，需要重构 `ai_reviewer.py` 的核心调用路径 |

---

## 4. Gitee V5 API 已确认信息

基于官方文档和实测资料，以下 Gitee API 行为已确认：

### 4.1 认证方式

- **Query param**：`?access_token=TOKEN` ✅ 支持
- **Header**：`Authorization: token TOKEN` ✅ 支持

与 GitCode 完全一致，现有 `_api_request()` 的 query param 传法可直接兼容。

### 4.2 PR 基础操作

| 操作 | 方法 | 路径 | 与 GitCode 对比 |
|------|------|------|----------------|
| 列表查询 | GET | `/pulls?state=open` | ✅ 相同 |
| 详情查询 | GET | `/pulls/{number}` | ✅ 相同 |
| 获取变更文件 | GET | `/pulls/{number}/files` | ✅ 路径相同，`patch` 为嵌套对象（与 GitCode 完全一致） |
| 普通评论 | POST | `/pulls/{number}/comments` | ✅ 相同，JSON body `{body}` |
| 行内评论 | POST | `/pulls/{number}/comments` | ⚠️ **相同路径，但参数语义完全不同（见 4.3）** |
| 删除评论 | DELETE | `/pulls/comments/{id}` | ✅ 路径相同 |

### 4.3 行内评论：核心差异 ⚠️

**这是 GitCode 与 Gitee 之间最大的平台差异。**

| 维度 | GitCode | Gitee |
|------|---------|-------|
| **请求格式** | **必须 form-encoded**，JSON 会被静默忽略 | **仅支持 JSON**，不支持 form-encoded |
| **`position` 含义** | **源码行号**（文件中的绝对行号） | **diff 块内相对行号**（从 `@@` 头之后第一行开始计数 1、2、3...） |
| **必需参数** | `body`, `path`, `position`, `commit_id` | `body`, `path`, `position`, `commit_id` |

#### position 差异的代码影响

当前代码中，`_build_diff_position_map()` 建立的是 `源码行号 -> (源码行号, is_added)` 映射（因为 GitCode 的 position 就是源码行号本身）：

```python
# ai_reviewer.py 当前逻辑（GitCode）
file_position_maps[fname] = _build_diff_position_map(raw_diff)
# 返回: {源码行号: (源码行号, is_added)}
# 行内评论调用: api_post_form(..., {"position": finding.line, ...})
```

对于 Gitee，需要改为建立 `源码行号 -> (diff块内相对行号, is_added)` 映射：

```python
# Gitee 需要的逻辑
# 返回: {源码行号: (diff_relative_position, is_added)}
# 行内评论调用: api_post(..., {"position": diff_relative_position, ...})
```

**position 计数规则（已实测验证）**：
- 从 `@@` 头后的**第一行**开始计数为 1
- 每行（上下文行/新增行/删除行）都 +1
- `@@` 头本身**不计入**
- 每个 diff 块内独立计数（多 `@@` 场景下，每个块从 1 重新开始）

**实测示例**（PR #2227，`infer_engines/bash_install_code.sh`）：
```
@@ -69,3 +69,4 @@ ...     ← 块头（不计入 position）
 git apply ...                  ← position=1（上下文行）
 git apply ...                  ← position=2（上下文行）
 git apply ...                  ← position=3（上下文行）
+git apply ...                  ← position=4（新增行） ← 测试成功定位到此行
```

**关键难点**：`_build_diff_position_map()` 需要重写，按 diff 块分别计算相对行号。如果 PR 涉及多个 diff 块，源码行号可能跨块，需要找到对应块内的相对位置。

> 注：GitHub API 的 `position` 参数定义与 Gitee 相同（也是 diff 块内相对行号），这说明 GitCode 的"源码行号"传法是它自己的非标准行为。

### 4.4 评论 permalink 格式

| 平台 | 格式 |
|------|------|
| GitCode | `https://gitcode.com/{owner}/{repo}/merge_requests/{n}?ref=&did={did}#tid-{nid}` |
| Gitee | `https://gitee.com/{owner}/{repo}/pulls/{n}#note_{comment_id}` |

> ⚠️ **注意**：用户文档写的是 `#comment-{id}`，但**实测 API 返回的 permalink 是 `#note_{id}`**（如 `https://gitee.com/omniai/omniinfer/pulls/2227#note_49821749`）。应以实测结果为准。

Gitee 的格式更简单，只需评论 ID 即可拼接。

### 4.5 PR 列表响应字段

Gitee 的响应字段与 GitCode 完全兼容：

```json
{
  "number": 1,
  "state": "open",
  "title": "...",
  "user": {"login": "username", "name": "..."},
  "head": {"ref": "feature/xxx", "sha": "abc123..."},
  "base": {"ref": "main", "sha": "def456..."},
  "html_url": "..."
}
```

`user.login`、`head.sha`、`base.sha`、`number` 等核心字段名一致，现有解析逻辑可直接复用。

---

## 5. 改造优先级评估

根据 API 实测结果，各改造点的优先级如下：

| 优先级 | 改造项 | 工作量 | 说明 |
|--------|--------|--------|------|
| **P0** | `position` 映射逻辑重写 | 大 | 核心难点，影响 inline 评论准确性。GitCode 用源码行号，Gitee 用 diff 块内相对行号 |
| **P0** | 行内评论请求格式切换（form → JSON） | 小 | Gitee 仅支持 JSON，可去掉 `api_post_form` workaround |
| **P1** | `RepoConfig` 平台化（url、api_prefix） | 小 | url 从 `gitcode.com` 改 `gitee.com`，Web 链接改为 `/pulls/` |
| **P1** | 评论链接构建 | 小 | `_resolve_comment_url()` 需支持 Gitee 的 `#note_{id}` 格式 |
| **P1** | 环境变量/提示文案平台化 | 小 | `GITCODE_TOKEN` → 中性名称 |
| **P1** | `review_loop.sh` 平台化 | 小 | 环境变量名和认证方式统一 |
| **P2** | `patch` 响应格式适配 | **零** | 已验证 Gitee `patch` 与 GitCode 完全一致（嵌套对象），现有解析函数无需修改 |

---

## 6. API 验证记录

以下三项已通过实测验证（仓库：`omniai/omniinfer`，PR #2227）：

| 验证项 | 结论 | 备注 |
|--------|------|------|
| `pulls/{n}/files` 响应格式 | ✅ `patch` 为嵌套对象 | 与 GitCode 完全一致，`get_file_diff()` 等函数无需修改 |
| `position` 计数规则 | ✅ diff 块内相对行号 | 从 `@@` 后第一行开始计数，上下文行/新增行/删除行都计入，`@@` 头不计入 |
| 评论创建响应 | ✅ 返回 `id` 字段 | 同时返回 `new_line`（源码行号）等辅助字段；permalink 格式为 `#note_{id}` |
| 评论删除 | ✅ 接口路径一致 | `DELETE /pulls/comments/{id}` 验证成功 |
| PR 列表字段 | ✅ 字段名一致 | `user.login`、`head.sha`、`base.sha`、`number` 与 GitCode 完全兼容 |

**已无待验证项，可以进入改造实施阶段。**
