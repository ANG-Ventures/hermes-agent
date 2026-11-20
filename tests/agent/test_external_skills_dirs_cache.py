"""Guards for ``get_external_skills_dirs`` mtime-based memo.

``get_external_skills_dirs()`` is called once per skill during banner
construction and tool registration — on a typical install that's 120+
calls.  Without caching, each call re-reads + YAML-parses the full
config.yaml (~85ms each, 10+ seconds total).  This test pins the
behavior: first call parses, subsequent calls return cached result,
cache invalidates when config.yaml's mtime changes.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from agent import skill_utils
from agent.skill_utils import (
    _external_dirs_cache_clear,
    get_external_skills_dirs,
)


@pytest.fixture
def hermes_home_with_config(tmp_path, monkeypatch):
    """Isolated ``~/.hermes/`` with a config.yaml referencing one external dir."""
    home = tmp_path / ".hermes"
    home.mkdir()
    external = tmp_path / "external_skills"
    external.mkdir()

    config = home / "config.yaml"
    config.write_text(
        "skills:\n"
        f"  external_dirs:\n"
        f"    - {external}\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _external_dirs_cache_clear()
    yield home, external, config
    _external_dirs_cache_clear()






def test_cache_invalidates_on_mtime_change(hermes_home_with_config):
    """A config.yaml edit invalidates the cache on the next call."""
    _home, external, config = hermes_home_with_config
    other = external.parent / "other_skills"
    other.mkdir()

    # Prime cache with original contents.
    first = get_external_skills_dirs()
    assert first == [external.resolve()]

    # Rewrite config; bump mtime forward explicitly so filesystems with
    # coarse mtime granularity still register the change on fast test
    # systems.
    config.write_text(
        "skills:\n"
        f"  external_dirs:\n"
        f"    - {other}\n",
        encoding="utf-8",
    )
    stat = config.stat()
    future = stat.st_atime + 10
    os.utime(config, (future, future))

    second = get_external_skills_dirs()
    assert second == [other.resolve()]






def test_cache_key_is_per_config_path(tmp_path, monkeypatch):
    """Two different HERMES_HOMEs keep separate cache entries."""
    home_a = tmp_path / "home_a" / ".hermes"
    home_a.mkdir(parents=True)
    ext_a = tmp_path / "ext_a"
    ext_a.mkdir()
    (home_a / "config.yaml").write_text(
        f"skills:\n  external_dirs:\n    - {ext_a}\n", encoding="utf-8"
    )

    home_b = tmp_path / "home_b" / ".hermes"
    home_b.mkdir(parents=True)
    ext_b = tmp_path / "ext_b"
    ext_b.mkdir()
    (home_b / "config.yaml").write_text(
        f"skills:\n  external_dirs:\n    - {ext_b}\n", encoding="utf-8"
    )

    _external_dirs_cache_clear()

    monkeypatch.setenv("HERMES_HOME", str(home_a))
    assert get_external_skills_dirs() == [ext_a.resolve()]

    monkeypatch.setenv("HERMES_HOME", str(home_b))
    assert get_external_skills_dirs() == [ext_b.resolve()]

    # And switching back still works — both entries coexist in the cache.
    monkeypatch.setenv("HERMES_HOME", str(home_a))
    assert get_external_skills_dirs() == [ext_a.resolve()]


def _write_dirs(config: Path, *dirs: Path) -> None:
    config.write_text(
        "skills:\n  external_dirs:\n" + "".join(f"    - {d}\n" for d in dirs),
        encoding="utf-8",
    )


def test_listed_dir_that_appears_later_is_returned(hermes_home_with_config):
    """A listed folder created after the first resolve shows up without a config edit."""
    _home, external, config = hermes_home_with_config
    late = external.parent / "late_skills"
    _write_dirs(config, external, late)
    _external_dirs_cache_clear()

    assert get_external_skills_dirs() == [external.resolve()]
    config_sig = config.stat()

    late.mkdir()
    assert config.stat().st_mtime_ns == config_sig.st_mtime_ns  # config untouched
    assert get_external_skills_dirs() == [external.resolve(), late.resolve()]


def test_listed_dir_that_is_removed_is_dropped(hermes_home_with_config):
    """A listed folder removed after the first resolve drops out without a config edit."""
    _home, external, config = hermes_home_with_config
    second = external.parent / "second_skills"
    second.mkdir()
    _write_dirs(config, external, second)
    _external_dirs_cache_clear()

    assert get_external_skills_dirs() == [external.resolve(), second.resolve()]

    second.rmdir()
    assert get_external_skills_dirs() == [external.resolve()]


def test_hot_path_does_not_reread_or_reparse_config(hermes_home_with_config):
    """N calls with nothing changed read and parse config.yaml once, including when a
    listed entry is missing (the case that must still notice the folder appearing)."""
    _home, external, config = hermes_home_with_config
    _write_dirs(config, external, external.parent / "missing_skills")
    _external_dirs_cache_clear()

    parses = []
    reads = []
    real_yaml_load = skill_utils.yaml_load
    real_read_text = Path.read_text

    def counting_yaml_load(content):
        parses.append(1)
        return real_yaml_load(content)

    def counting_read_text(self, *a, **kw):
        if self == config:
            reads.append(1)
        return real_read_text(self, *a, **kw)

    with patch.object(skill_utils, "yaml_load", counting_yaml_load), \
            patch.object(Path, "read_text", counting_read_text):
        results = [get_external_skills_dirs() for _ in range(200)]

    assert all(r == [external.resolve()] for r in results)
    assert len(parses) == 1
    assert len(reads) == 1
