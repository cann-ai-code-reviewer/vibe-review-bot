"""GitCode API 客户端和 PR 拉取工具。"""
import argparse
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import GITCODE_API_BASE, TEAM_FILE
from .models import RepoConfig
from .terminal import _fail, _warn, _dim


def _api_request(
    method: str, path: str, token: str,
    params: dict | None = None, body: dict | None = None,
) -> dict | list | None:
    """GitCode REST API 统一请求封装。

    返回 JSON 响应或 None（出错时）。DELETE 方法成功时返回空 dict。
    """
    if params is None:
        params = {}
    params["access_token"] = token
    url = f"{GITCODE_API_BASE}{path}?{urlencode(params)}"

    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")

    req = Request(url, data=data, method=method, headers=headers)
    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except HTTPError as e:
        resp_body = e.read().decode("utf-8", errors="replace")
        print(f"  {_fail(f'API {method} 失败：{path}')}")
        print(f"  {_dim(f'HTTP {e.code}: {resp_body[:300]}')}")
        return None
    except URLError as e:
        print(f"  {_fail(f'网络错误：{e.reason}')}")
        return None
    except (TimeoutError, OSError) as e:
        print(f"  {_fail(f'网络超时：{e}')}")
        return None


def api_get(path: str, token: str, params: dict = None) -> dict | list | None:
    """调用 GitCode REST API（GET），返回 JSON 响应。"""
    return _api_request("GET", path, token, params=params)


def api_post(path: str, token: str, body: dict) -> dict | list | None:
    """调用 GitCode REST API（POST JSON），返回 JSON 响应。"""
    return _api_request("POST", path, token, body=body)


def api_post_form(path: str, token: str, fields: dict) -> dict | list | None:
    """调用 GitCode REST API（POST form-encoded），返回 JSON 响应。

    GitCode 部分 API（如行内评论）仅在 form-encoded 格式下正确处理
    path/position/commit_id 等字段，JSON 格式会被静默忽略。
    """
    fields["access_token"] = token
    url = f"{GITCODE_API_BASE}{path}"
    data = urlencode(fields).encode("utf-8")
    headers = {"Content-Type": "application/x-www-form-urlencoded",
               "Accept": "application/json"}
    req = Request(url, data=data, method="POST", headers=headers)
    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except HTTPError as e:
        resp_body = e.read().decode("utf-8", errors="replace")
        print(f"  {_fail(f'API POST 失败：{path}')}")
        print(f"  {_dim(f'HTTP {e.code}: {resp_body[:300]}')}")
        return None
    except URLError as e:
        print(f"  {_fail(f'网络错误：{e.reason}')}")
        return None
    except (TimeoutError, OSError) as e:
        print(f"  {_fail(f'网络超时：{e}')}")
        return None


def api_delete(path: str, token: str) -> bool:
    """调用 GitCode REST API（DELETE），返回是否成功。"""
    return _api_request("DELETE", path, token) is not None


def fetch_open_prs(repo: RepoConfig, token: str, count: int = 3, state: str = "open") -> list:
    """获取指定数量的 PR 列表。count=0 表示获取全部。"""
    if count == 0:
        # 获取全部：翻页遍历
        all_prs = []
        page = 1
        per_page = 50
        max_pages = 50
        while page <= max_pages:
            data = api_get(
                f"{repo.api_prefix}/pulls",
                token,
                {"state": state, "per_page": per_page, "page": page, "sort": "created", "direction": "desc"},
            )
            if not data:
                break
            all_prs.extend(data)
            if len(data) < per_page:
                break
            page += 1
        return all_prs

    data = api_get(
        f"{repo.api_prefix}/pulls",
        token,
        {"state": state, "per_page": count, "page": 1, "sort": "created", "direction": "desc"},
    )
    if data is None:
        return []
    return data[:count]


def fetch_pr_by_number(repo: RepoConfig, token: str, pr_number: int) -> dict | None:
    """根据 PR 编号获取单个 PR 详情。"""
    return api_get(f"{repo.api_prefix}/pulls/{pr_number}", token)


def fetch_prs_by_authors(repo: RepoConfig, token: str, authors: list, count: int = 3, state: str = "open") -> list:
    """获取指定用户的 PR，翻页遍历直到满足数量要求。count=0 表示获取全部。

    GitCode API 不支持按 author 过滤，因此客户端侧翻页 + 过滤。
    """
    authors_lower = {a.lower() for a in authors}
    matched = []
    page = 1
    per_page = 20  # 每页拉取量（平衡请求次数和过滤效率）
    max_pages = 50  # 安全上限，防止无限翻页

    while (count == 0 or len(matched) < count) and page <= max_pages:
        data = api_get(
            f"{repo.api_prefix}/pulls",
            token,
            {"state": state, "per_page": per_page, "page": page, "sort": "created", "direction": "desc"},
        )
        if not data:
            break

        for pr in data:
            login = pr.get("user", {}).get("login", "")
            if login.lower() in authors_lower:
                matched.append(pr)
                if count > 0 and len(matched) >= count:
                    break

        # 最后一页不足 per_page 条，说明已经没有更多数据
        if len(data) < per_page:
            break
        page += 1

    return matched


def load_team_members(filepath: Path = TEAM_FILE) -> tuple[list[str], dict[str, str]]:
    """从 team 文件读取小组成员的 gitcode 账号列表。

    文件格式：每行 '姓名 gitcode 账号'，首行为标题行。
    返回 (账号列表, {账号：姓名} 映射)。账号去重保序。
    """
    if not filepath.exists():
        print(f"  {_fail(f'人员名单不存在：{filepath}')}")
        sys.exit(1)

    accounts = []
    info_map: dict[str, str] = {}
    for i, line in enumerate(filepath.read_text(encoding="utf-8").splitlines()):
        if i == 0:
            continue  # 跳过标题行
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            account = parts[-1]  # 最后一列是 gitcode 账号
            info_map[account] = parts[0]
            accounts.append(account)
        elif len(parts) == 1:
            accounts.append(parts[0])   # 只有账号的简略格式
    if not accounts:
        print(f"  {_fail(f'人员名单为空：{filepath}')}")
        sys.exit(1)
    unique = list(dict.fromkeys(accounts))  # 去重保序
    return unique, info_map


def collect_prs(repo: RepoConfig, token: str, args: argparse.Namespace) -> list:
    """根据命令行参数收集待审查的 PR 列表。

    优先级：--pr > --team > --author > 默认(最近 open)
    """
    if args.pr:
        # 精确模式：逐个获取指定 PR
        prs = []
        for num in args.pr:
            print(f"  获取 PR #{num}")
            pr = fetch_pr_by_number(repo, token, num)
            if pr:
                prs.append(pr)
            else:
                print(f"  {_warn(f'PR #{num} 获取失败，跳过。')}")
        return prs

    if args.team:
        # 小组模式：从 team file 读取全部成员，获取每人的 PR
        members, info_map = load_team_members(args.team)
        display = [f"{m}({info_map[m]})" if m in info_map else m for m in members]
        print(f"  小组成员 ({len(members)} 人): {', '.join(display)}")
        return fetch_prs_by_authors(repo, token, members, args.count, args.state)

    if args.author:
        # 用户过滤模式
        print(f"  筛选用户：{', '.join(args.author)}")
        return fetch_prs_by_authors(repo, token, args.author, args.count, args.state)

    # 默认模式：最近 N 个 PR
    return fetch_open_prs(repo, token, args.count, args.state)


def fetch_pr_files(repo: RepoConfig, token: str, pr_number: int) -> list:
    """获取 PR 的变更文件列表（含 patch diff）。

    GitCode API 返回格式:
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
    data = api_get(f"{repo.api_prefix}/pulls/{pr_number}/files", token)
    if data is None:
        return []
    if isinstance(data, list):
        return data
    # 兼容可能的嵌套格式
    return data.get("files", data.get("data", []))


def get_file_diff(file_entry: dict) -> str:
    """从文件条目中提取 diff 文本，兼容不同 API 返回格式。"""
    patch = file_entry.get("patch", "")
    if isinstance(patch, dict):
        # GitCode 格式：patch 是嵌套对象，diff 在 patch.diff 字段
        return patch.get("diff", "")
    # 兼容 GitHub 格式：patch 直接是字符串
    return patch


def get_file_status(file_entry: dict) -> str:
    """从文件条目中推导文件变更状态。"""
    # 优先使用顶层 status 字段（GitHub 兼容）
    status = file_entry.get("status", "")
    if status:
        return status
    # GitCode 格式：从 patch 对象的布尔标志推导
    patch = file_entry.get("patch", {})
    if isinstance(patch, dict):
        if patch.get("new_file"):
            return "added"
        if patch.get("deleted_file"):
            return "removed"
        if patch.get("renamed_file"):
            return "renamed"
    return "modified"


def get_filename(file_entry: dict) -> str:
    """获取文件名，优先使用 patch.new_path。"""
    patch = file_entry.get("patch", {})
    if isinstance(patch, dict):
        return patch.get("new_path", "") or file_entry.get("filename", "unknown")
    return file_entry.get("filename", "unknown")
