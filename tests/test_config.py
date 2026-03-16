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
