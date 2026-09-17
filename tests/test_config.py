import pytest

from cclens import config


def test_roots_dedupe_by_real_path(tmp_path):
    """A .claude reached through a symlink is the same corpus, not a second one."""
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real)

    cfg = config.from_env({
        "HOME": str(tmp_path),
        "CCLENS_CLAUDE_HOME": f"mac={real}:vm={linked}",
    })
    assert [r.name for r in cfg.roots] == ["mac"]


def test_two_roots_keep_their_labels(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    cfg = config.from_env({
        "HOME": str(tmp_path),
        "CCLENS_CLAUDE_HOME": f"mac={one}:vm={two}",
    })
    assert [(r.name, r.path) for r in cfg.roots] == [("mac", one), ("vm", two)]


def test_a_root_that_is_not_a_directory_fails_at_boot(tmp_path):
    with pytest.raises(config.ConfigError, match="not a directory"):
        config.from_env({"HOME": str(tmp_path),
                         "CCLENS_CLAUDE_HOME": str(tmp_path / "absent")})


def test_duplicate_root_names_fail_at_boot(tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    with pytest.raises(config.ConfigError, match="duplicate root names"):
        config.from_env({"HOME": str(tmp_path),
                         "CCLENS_CLAUDE_HOME": f"same={one}:same={two}"})


def test_an_unknown_excluded_kind_fails_at_boot(tmp_path):
    (tmp_path / ".claude").mkdir()
    with pytest.raises(config.ConfigError, match="unknown kinds"):
        config.from_env({"HOME": str(tmp_path), "CCLENS_EXCLUDE": "transcript,badger"})


def test_the_index_defaults_under_the_cache_directory(tmp_path):
    (tmp_path / ".claude").mkdir()
    cfg = config.from_env({"HOME": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "c")})
    assert cfg.db_path == tmp_path / "c" / "cclens" / "index.db"


def test_a_non_loopback_bind_is_reported_as_such(tmp_path):
    (tmp_path / ".claude").mkdir()
    cfg = config.from_env({"HOME": str(tmp_path), "CCLENS_HOST": "0.0.0.0"})
    assert cfg.loopback_only is False
