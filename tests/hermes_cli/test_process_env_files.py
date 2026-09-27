"""agent.process_env_files: sourced once at agent-process start, inherited by
every child (t_45c11886 -- process-spawned gh must ride the profile's lane)."""

import os
import stat

import pytest

from hermes_cli import process_env_files as pef


@pytest.fixture(autouse=True)
def _clean_overlay():
    saved = dict(pef._OVERLAY)
    pef._OVERLAY.clear()
    yield
    pef._OVERLAY.clear()
    pef._OVERLAY.update(saved)


def _write(path, text):
    path.write_text(text)
    return str(path)


def _cfg(*files):
    return {"agent": {"process_env_files": list(files)}}


def _lane_env_fixture(tmp_path):
    """Same shape as fleet/gh-lane-env.sh, with a fake shim."""
    shim_dir = tmp_path / "gh-shim"
    shim_dir.mkdir()
    shim = shim_dir / "gh"
    shim.write_text('#!/bin/sh\necho \'{"lane": "workers"}\'\n')
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    return _write(tmp_path / "lane-env.sh", f"""
_s="{shim}"
if [ -x "$_s" ]; then
    export PATH="{shim_dir}:$PATH"
    export GIT_CONFIG_COUNT=2
    export GIT_CONFIG_KEY_0=credential.https://github.com.helper
    export GIT_CONFIG_VALUE_0=
    export GIT_CONFIG_KEY_1=credential.https://github.com.helper
    export GIT_CONFIG_VALUE_1="!$_s auth git-credential"
    _l=`"$_s" | sed -n 's/.*"lane": "\\([a-z-]*\\)".*/\\1/p'`
    if [ "$_l" = "workers" ]; then
        export GIT_AUTHOR_NAME='bot[bot]'
    fi
    unset _l
fi
unset _s
"""), str(shim_dir), str(shim)


def test_configured_files_expands_and_skips_missing(tmp_path, monkeypatch):
    f = _write(tmp_path / "a.sh", "export A=1\n")
    monkeypatch.setenv("PEF_TEST_DIR", str(tmp_path))
    assert pef.configured_files(_cfg("$PEF_TEST_DIR/a.sh", str(tmp_path / "missing.sh"), "")) == [f]
    assert pef.configured_files({"agent": {"process_env_files": "x"}}) == []
    assert pef.configured_files({}) == []
    assert pef.configured_files(None) == []


def test_apply_merges_diff_into_env(tmp_path):
    lane, shim_dir, shim = _lane_env_fixture(tmp_path)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "DROP_ME": "x"}
    unset = _write(tmp_path / "unset.sh", "unset DROP_ME\n")
    diff = pef.apply_process_env_files(_cfg(lane, unset), env)
    assert env["PATH"] == f"{shim_dir}:/usr/bin:/bin"
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert env["GIT_CONFIG_VALUE_1"] == f"!{shim} auth git-credential"
    assert env["GIT_AUTHOR_NAME"] == "bot[bot]"
    assert "DROP_ME" not in env
    # sh bookkeeping and the files' private temporaries never leak
    assert not {"PWD", "SHLVL", "_", "_s", "_l"} & set(diff)
    assert "PEF_PY" not in env and "PEF_DUMP" not in env


def test_fail_open_leaves_env_untouched(tmp_path):
    bad = _write(tmp_path / "bad.sh", "export LEAK=1\nexit 3\n")
    env = {"PATH": "/usr/bin:/bin"}
    assert pef.apply_process_env_files(_cfg(bad), env) == {}
    assert env == {"PATH": "/usr/bin:/bin"}


def test_nothing_configured_spawns_nothing(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no subprocess when unconfigured")

    monkeypatch.setattr(pef.subprocess, "run", boom)
    env = {"PATH": "/bin"}
    assert pef.apply_process_env_files({}, env) == {}


def test_process_apply_then_strip_restores_plain_script_env(tmp_path, monkeypatch):
    lane, shim_dir, _ = _lane_env_fixture(tmp_path)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    for k in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_1", "GIT_CONFIG_VALUE_0",
              "GIT_CONFIG_VALUE_1", "GIT_AUTHOR_NAME"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "pre-existing")  # restored, not dropped
    pef.apply_process_env_files(_cfg(lane))
    try:
        assert os.environ["PATH"].split(":")[0] == shim_dir
        child = dict(os.environ)
        child["PATH"] = "/opt/venv/bin:" + child["PATH"]  # re-edited after start
        child["GIT_AUTHOR_NAME"] = "someone-else"          # deliberate later change
        pef.strip_overlay(child)
        assert child["PATH"] == "/opt/venv/bin:/usr/bin:/bin"
        assert "GIT_CONFIG_COUNT" not in child and "GIT_CONFIG_VALUE_1" not in child
        assert child["GIT_CONFIG_KEY_0"] == "pre-existing"
        assert child["GIT_AUTHOR_NAME"] == "someone-else"
    finally:
        for k in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_1", "GIT_CONFIG_VALUE_0",
                  "GIT_CONFIG_VALUE_1", "GIT_AUTHOR_NAME"):
            os.environ.pop(k, None)


def test_main_entry_applies_profile_config(tmp_path, monkeypatch):
    """Real loader path: config.yaml in HERMES_HOME -> main's hook -> os.environ."""
    marker = _write(tmp_path / "m.sh", "export PEF_MAIN_PROBE=from-file\n")
    (tmp_path / "config.yaml").write_text(f"agent:\n  process_env_files:\n    - {marker}\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("PEF_MAIN_PROBE", raising=False)
    from hermes_cli.main import _apply_process_env_files

    _apply_process_env_files()
    try:
        assert os.environ.get("PEF_MAIN_PROBE") == "from-file"
    finally:
        os.environ.pop("PEF_MAIN_PROBE", None)
