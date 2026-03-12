"""TDD 测试：--match 关键字过滤功能 (Issue #2)

测试目标函数：filter_prs_by_title(prs, match, is_exact_mode) -> list
  - prs: PR 字典列表，每个 PR 至少包含 {"number": int, "title": str}
  - match: 关键字字符串或 None
  - is_exact_mode: 是否为 --pr 精确指定模式（True 时跳过过滤）
  - 返回：过滤后的 PR 列表

匹配规则：
  - 全字匹配（word boundary），大小写不敏感
  - match=None 时不做关键字过滤（但 WIP 过滤仍生效）
  - is_exact_mode=True 时不做任何过滤
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_reviewer import filter_prs_by_title


# ── helpers ──────────────────────────────────────────────────────────────

def _pr(number: int, title: str) -> dict:
    """构造最小 PR 字典。"""
    return {"number": number, "title": title}


def _titles(prs: list) -> list[str]:
    """提取 PR 标题列表，方便断言比较。"""
    return [p["title"] for p in prs]


# ═══════════════════════════════════════════════════════════════════════
# 一、参数解析（argparse）
# ═══════════════════════════════════════════════════════════════════════

class TestArgparse:
    """测试 --match 参数的解析行为。"""

    def test_match_with_value(self):
        """#1: --match PLZ → args.match == 'PLZ'"""
        from ai_reviewer import _build_parser
        parser = _build_parser()
        args = parser.parse_args(["--match", "PLZ"])
        assert args.match == "PLZ"

    def test_match_default_none(self):
        """#2: 不传 --match → args.match is None"""
        from ai_reviewer import _build_parser
        parser = _build_parser()
        args = parser.parse_args([])
        assert args.match is None

    def test_match_missing_value_exits(self):
        """#3: --match 后面没有值 → argparse 报错退出"""
        import pytest
        from ai_reviewer import _build_parser
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--match"])


# ═══════════════════════════════════════════════════════════════════════
# 二、核心过滤逻辑
# ═══════════════════════════════════════════════════════════════════════

class TestMatchFilter:
    """测试 filter_prs_by_title 的关键字匹配行为。"""

    def test_title_contains_keyword_kept(self):
        """#4: 标题包含关键字 → 保留"""
        prs = [_pr(1, "fix: PLZ review")]
        result = filter_prs_by_title(prs, "PLZ", is_exact_mode=False)
        assert len(result) == 1
        assert result[0]["number"] == 1

    def test_title_without_keyword_filtered(self):
        """#5: 标题不包含关键字 → 过滤掉"""
        prs = [_pr(1, "fix: update config")]
        result = filter_prs_by_title(prs, "PLZ", is_exact_mode=False)
        assert len(result) == 0

    def test_case_insensitive(self):
        """#6: 大小写不敏感匹配"""
        prs = [
            _pr(1, "plz check this"),
            _pr(2, "Plz fix the bug"),
            _pr(3, "PLZ review code"),
        ]
        result = filter_prs_by_title(prs, "PLZ", is_exact_mode=False)
        assert len(result) == 3

    def test_mixed_list(self):
        """#7: 混合列表 — 2 个含关键字，1 个不含"""
        prs = [
            _pr(1, "PLZ review this"),
            _pr(2, "normal update"),
            _pr(3, "fix: PLZ check"),
        ]
        result = filter_prs_by_title(prs, "PLZ", is_exact_mode=False)
        assert len(result) == 2
        assert {p["number"] for p in result} == {1, 3}

    def test_none_match(self):
        """#8: 全部不匹配 → 返回空列表"""
        prs = [
            _pr(1, "update readme"),
            _pr(2, "fix bug"),
            _pr(3, "refactor module"),
        ]
        result = filter_prs_by_title(prs, "PLZ", is_exact_mode=False)
        assert result == []

    def test_empty_pr_list(self):
        """#9: 空 PR 列表 → 返回空列表"""
        result = filter_prs_by_title([], "PLZ", is_exact_mode=False)
        assert result == []

    def test_empty_title_filtered(self):
        """#10: 空标题 → 过滤掉"""
        prs = [_pr(1, "")]
        result = filter_prs_by_title(prs, "PLZ", is_exact_mode=False)
        assert result == []

    def test_keyword_as_substring_not_matched(self):
        """#11: 关键字是其他单词的子串 → 不匹配（全字匹配）"""
        prs = [_pr(1, "PLAZA project")]
        result = filter_prs_by_title(prs, "PLZ", is_exact_mode=False)
        assert result == []

    def test_keyword_as_standalone_word(self):
        """#18: 关键字作为独立单词出现 → 匹配"""
        prs = [_pr(1, "fix: PLZ review this")]
        result = filter_prs_by_title(prs, "PLZ", is_exact_mode=False)
        assert len(result) == 1


# ═══════════════════════════════════════════════════════════════════════
# 三、--pr 模式绕过
# ═══════════════════════════════════════════════════════════════════════

class TestExactModeBypass:
    """测试 --pr 精确指定模式下 --match 不生效。"""

    def test_exact_mode_skips_filter(self):
        """#12: --pr 模式 + --match → 不过滤，即使标题不含关键字"""
        prs = [_pr(1, "normal update without keyword")]
        result = filter_prs_by_title(prs, "PLZ", is_exact_mode=True)
        assert len(result) == 1

    def test_batch_mode_applies_filter(self):
        """#13: 批量模式 + --match → 正常过滤"""
        prs = [_pr(1, "normal update without keyword")]
        result = filter_prs_by_title(prs, "PLZ", is_exact_mode=False)
        assert len(result) == 0


# ═══════════════════════════════════════════════════════════════════════
# 四、未指定 --match 时的行为
# ═══════════════════════════════════════════════════════════════════════

class TestNoMatch:
    """测试 match=None 时不做关键字过滤。"""

    def test_no_match_keeps_all(self):
        """#14: match=None → 全部保留"""
        prs = [
            _pr(1, "update readme"),
            _pr(2, "fix bug"),
            _pr(3, "add feature"),
        ]
        result = filter_prs_by_title(prs, None, is_exact_mode=False)
        assert len(result) == 3

    def test_no_match_still_filters_wip(self):
        """#15: match=None 时 WIP 过滤仍生效"""
        prs = [
            _pr(1, "[WIP] draft feature"),
            _pr(2, "PLZ review this"),
        ]
        result = filter_prs_by_title(prs, None, is_exact_mode=False)
        assert len(result) == 1
        assert result[0]["number"] == 2


# ═══════════════════════════════════════════════════════════════════════
# 五、与 WIP 过滤的交互
# ═══════════════════════════════════════════════════════════════════════

class TestWipAndMatchInteraction:
    """测试 WIP 过滤与 --match 过滤的共同作用。"""

    def test_wip_and_match_combined(self):
        """#16: WIP + match 同时作用 — WIP 先过滤，match 再过滤"""
        prs = [
            _pr(1, "[WIP] PLZ fix this"),   # WIP → 过滤
            _pr(2, "PLZ review code"),       # 保留
            _pr(3, "normal update"),         # 无 PLZ → 过滤
        ]
        result = filter_prs_by_title(prs, "PLZ", is_exact_mode=False)
        assert len(result) == 1
        assert result[0]["number"] == 2

    def test_wip_with_keyword_still_filtered(self):
        """#17: 标题含 WIP 且含关键字 → 仍被 WIP 过滤掉"""
        prs = [_pr(1, "WIP: PLZ check this")]
        result = filter_prs_by_title(prs, "PLZ", is_exact_mode=False)
        assert result == []
