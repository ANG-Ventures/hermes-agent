"""The runtime must never chmod or write inside a sealed release used as HERMES_HOME.

A sealed release (``<home>/releases/<name>/`` holding ``RELEASE.json``) is r-x. A job re-fired
with ``HERMES_HOME=<home>/releases/<sha>`` made ``initialize_home`` chmod the release root,
``hooks`` and ``cron`` to 0700, and the cron fire fence then wrote ``cron/.fire-<hex>.lock`` there,
so the release failed verify (t_da2a67e9, t_a3367113). Startup now refuses such a home and names
the stable home; the cron store refuses it too, so no path reaches a chmod or a write.
"""

import os
import stat

import pytest

from hermes_cli.config_home import HomeInitializationError, initialize_home


def _release(tmp_path):
    home = tmp_path / ".hermes"
    release = home / "releases" / "0206139d795e"
    for sub in ("hooks", "cron"):
        (release / sub).mkdir(parents=True)
    (release / "RELEASE.json").write_text("{}\n", encoding="utf-8")
    sealed = (release, release / "hooks", release / "cron")
    for path in reversed(sealed):
        path.chmod(0o555)
    return home, release, sealed


def _state(paths):
    return [(stat.S_IMODE(p.stat().st_mode), sorted(os.listdir(p))) for p in paths]


@pytest.fixture
def sealed_release(tmp_path):
    home, release, sealed = _release(tmp_path)
    yield home, release, sealed
    for path in sealed:
        path.chmod(0o755)


def test_startup_on_a_release_root_leaves_the_seal(sealed_release, monkeypatch):
    home, release, sealed = sealed_release
    before = _state(sealed)
    monkeypatch.setenv("HERMES_HOME", str(release))

    from hermes_cli import config

    with pytest.raises(HomeInitializationError, match=f"sealed release.*{home}"):
        config.ensure_hermes_home()
    with pytest.raises(HomeInitializationError):
        initialize_home(release, config._HERMES_HOME_SUBDIRS, set())

    assert _state(sealed) == before == [(0o555, s) for _, s in before]


def test_cron_store_on_a_release_root_neither_chmods_nor_writes_a_fire_lock(sealed_release):
    home, release, sealed = sealed_release
    before = _state(sealed)

    from cron import jobs

    with jobs.use_cron_store(release):
        with pytest.raises(PermissionError, match=f"sealed release.*{home}"):
            jobs.ensure_dirs()
        with pytest.raises(PermissionError):
            with jobs._fire_job_lock("abc123"):
                pass

    assert _state(sealed) == before
    assert not list((release / "cron").glob(".fire-*.lock"))


def test_ordinary_home_still_initializes(tmp_path):
    """Control: a home that is not a release root keeps the 0700 policy."""
    home = tmp_path / ".hermes"
    initialize_home(home, ("cron", "hooks"), set())
    assert (home / "cron").is_dir()
