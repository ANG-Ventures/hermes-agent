"""tests/flake_quarantine.py: entry validation, matching, and the real pytest hook path."""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from tests import flake_quarantine as fq

NOW = dt.datetime(2026, 9, 26, tzinfo=dt.timezone.utc)
FUTURE = "2026-10-03T00:00:00Z"
REPO = Path(__file__).resolve().parents[2]


def _entry(**kw):
    base = {"id": "tests/x.py::test_y", "card": "t_0123abcd", "owner": "daedalus", "expires": FUTURE}
    base.update(kw)
    return base


def test_unset_or_empty_is_a_no_op():
    assert fq.load_entries(None, NOW) == ([], [])
    assert fq.load_entries("  ", NOW) == ([], [])


def test_valid_entry_is_active():
    active, ignored = fq.load_entries(json.dumps([_entry()]), NOW)
    assert [e["id"] for e in active] == ["tests/x.py::test_y"] and ignored == []


def test_entry_without_card_or_owner_is_ignored_loudly():
    raw = json.dumps([_entry(card=""), _entry(owner=""), _entry(card="1234")])
    active, ignored = fq.load_entries(raw, NOW)
    assert active == []
    assert len(ignored) == 3 and all("card or owner" in why for why in ignored)


def test_expired_entry_is_ignored_and_says_so():
    active, ignored = fq.load_entries(json.dumps([_entry(expires="2026-09-25T00:00:00Z")]), NOW)
    assert active == [] and "expired" in ignored[0]


def test_missing_or_naive_expiry_is_ignored():
    for exp in ("", "2026-10-03T00:00:00", "soon"):
        active, ignored = fq.load_entries(json.dumps([_entry(expires=exp)]), NOW)
        assert active == [] and "expires" in ignored[0], exp


def test_bad_json_quarantines_nothing():
    active, ignored = fq.load_entries("{not json", NOW)
    assert active == [] and "not valid JSON" in ignored[0]


def test_match_rules():
    assert fq.match("tests/x.py::test_y", "tests/x.py::test_y")
    assert fq.match("tests/x.py::test_y[a-b]", "tests/x.py::test_y")
    assert not fq.match("tests/x.py::test_y2", "tests/x.py::test_y")
    assert not fq.match("tests/x.py::test_y[b]", "tests/x.py::test_y[a]")
    assert fq.match("tests/x.py::C::test_z", "tests/x.py")
    assert not fq.match("tests/x2.py::test_y", "tests/x.py")


def _run(tmp_path: Path, entries) -> subprocess.CompletedProcess:
    """Run pytest in a scratch rootdir that loads this plugin, like tests/conftest.py does."""
    tdir = tmp_path / "tests"
    tdir.mkdir()
    (tdir / "__init__.py").write_text("")
    (tmp_path / "conftest.py").write_text('pytest_plugins = ["tests.flake_quarantine"]\n')
    (tdir / "flake_quarantine.py").write_text(Path(fq.__file__).read_text())
    (tdir / "test_sample.py").write_text(textwrap.dedent(
        """
        import pytest

        def test_flaky():
            assert False

        @pytest.mark.parametrize("n", [1, 2])
        def test_param(n):
            assert False

        def test_real():
            assert False

        def test_flaky_passes_this_time():
            assert True
        """
    ))
    (tdir / "test_slow.py").write_text("def test_hang():\n    assert False\n")
    env = {**os.environ, "CI_TEST_QUARANTINE": json.dumps(entries)}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-rfxs", "-q", "tests"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120,
    )


def test_hook_xfails_quarantined_and_leaves_the_rest_red(tmp_path):
    entries = [
        _entry(id="tests/test_sample.py::test_flaky", expires="2999-01-01T00:00:00Z"),
        _entry(id="tests/test_sample.py::test_param", expires="2999-01-01T00:00:00Z"),
        _entry(id="tests/test_slow.py", expires="2999-01-01T00:00:00Z"),
        _entry(id="tests/test_sample.py::test_real", expires="2000-01-01T00:00:00Z"),
    ]
    r = _run(tmp_path, entries)
    out = r.stdout
    assert "1 failed, 1 passed, 1 skipped, 3 xfailed" in out, out
    assert "FAILED tests/test_sample.py::test_real" in out
    assert "quarantined flake: card t_0123abcd, owner daedalus" in out
    assert "applied: tests/test_sample.py::test_flaky (1 test(s))" in out
    assert "ignored: tests/test_sample.py::test_real: quarantine expired" in out
    assert r.returncode == 1


def test_hook_all_quarantined_is_green(tmp_path):
    entries = [_entry(id=i, expires="2999-01-01T00:00:00Z") for i in (
        "tests/test_sample.py", "tests/test_slow.py")]
    r = _run(tmp_path, entries)
    assert r.returncode == 0, r.stdout


def test_quarantined_flake_that_passes_stays_green(tmp_path):
    # A flake passes most of the time; xfail must be non-strict or every pass goes red.
    entries = [
        _entry(id="tests/test_sample.py::test_flaky_passes_this_time", expires="2999-01-01T00:00:00Z"),
        _entry(id="tests/test_sample.py::test_flaky", expires="2999-01-01T00:00:00Z"),
        _entry(id="tests/test_sample.py::test_param", expires="2999-01-01T00:00:00Z"),
        _entry(id="tests/test_sample.py::test_real", expires="2999-01-01T00:00:00Z"),
        _entry(id="tests/test_slow.py", expires="2999-01-01T00:00:00Z"),
    ]
    r = _run(tmp_path, entries)
    assert r.returncode == 0, r.stdout
    assert "1 xpassed" in r.stdout, r.stdout


def test_workflow_exports_the_variable_to_test_slices():
    wf = (REPO / ".github/workflows/tests.yml").read_text()
    assert "CI_TEST_QUARANTINE: ${{ vars.CI_TEST_QUARANTINE }}" in wf


def test_root_conftest_loads_the_plugin():
    conftest = (REPO / "tests/conftest.py").read_text()
    assert '"tests.flake_quarantine"' in conftest
