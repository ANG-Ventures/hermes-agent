"""The global LogRecord factory must not call Path.resolve() on every record.

It runs for every log record on every thread, the asyncio loop included. A
per-record realpath stalled the Apollo gateway loop for 10 s under disk
pressure (PHASE=event_loop_blocked, 2026-10-26). The resolved home is now
computed once per unresolved home string.
"""

import logging
import pathlib

import pytest

import hermes_logging
from hermes_constants import reset_hermes_home_override, set_hermes_home_override

hermes_logging._install_session_record_factory()


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear = getattr(getattr(hermes_logging, "_resolved_home_str", None), "cache_clear", None)
    if clear:
        clear()
    yield
    if clear:
        clear()


@pytest.fixture
def resolve_calls(monkeypatch):
    calls = []
    real = pathlib.Path.resolve

    def counting(self, *args, **kwargs):
        calls.append(str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "resolve", counting)
    return calls


def _record():
    return logging.getLogger("test.resolve_cache").makeRecord(
        "test.resolve_cache", logging.INFO, __file__, 1, "msg", (), None
    )


def _with_home(home):
    token = set_hermes_home_override(home)
    try:
        return _record()
    finally:
        reset_hermes_home_override(token)


def test_same_home_is_resolved_once_across_records(tmp_path, resolve_calls):
    home = tmp_path / "home"
    home.mkdir()
    want = str(home.resolve())
    resolve_calls.clear()
    records = [_with_home(home) for _ in range(20)]
    hits = [c for c in resolve_calls if c == str(home)]
    assert len(hits) == 1, f"Path.resolve ran {len(hits)}x for 20 records"
    assert {r.hermes_home for r in records} == {want}


def test_distinct_homes_resolve_separately(tmp_path, resolve_calls):
    real_a = tmp_path / "real_a"
    real_a.mkdir()
    link_a = tmp_path / "link_a"
    link_a.symlink_to(real_a)
    home_b = tmp_path / "b"
    home_b.mkdir()
    want_a, want_b = str(real_a.resolve()), str(home_b.resolve())
    resolve_calls.clear()

    rec_a = _with_home(link_a)
    rec_b = _with_home(home_b)
    rec_a2 = _with_home(link_a)

    assert rec_a.hermes_home == want_a  # symlink still resolved
    assert rec_b.hermes_home == want_b
    assert rec_a2.hermes_home == rec_a.hermes_home
    assert [c for c in resolve_calls if c in (str(link_a), str(home_b))] == [str(link_a), str(home_b)]


def test_resolve_failure_is_not_cached(tmp_path, monkeypatch):
    home = tmp_path / "flaky"
    home.mkdir()
    real = pathlib.Path.resolve
    state = {"fail": True}

    def flaky(self, *args, **kwargs):
        if state["fail"] and str(self) == str(home):
            raise OSError("disk hiccup")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "resolve", flaky)
    assert _with_home(home).hermes_home == ""
    state["fail"] = False
    assert _with_home(home).hermes_home == str(home.resolve())
