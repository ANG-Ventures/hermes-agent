"""Prism 0466e3e9cee8 (#1768 @b8c47611): a legacy unit whose `systemctl stop` fails or times out is still
running; remove_legacy_hermes_units must report it in `remaining` (never unlink it, never raise), so the
LegacyUnitsRemain guard stops a new gateway from starting next to it on the same bot token."""
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import gateway as gw


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    unit = tmp_path / "hermes.service"
    unit.write_text("[Unit]\nDescription=Hermes Gateway\n", encoding="utf-8")
    monkeypatch.setattr(gw, "_find_legacy_hermes_units", lambda: [("hermes.service", unit, False)])
    from hermes_cli import gateway_service_owner as owner
    monkeypatch.setattr(owner, "assert_may_mutate", lambda *a, **k: None)
    monkeypatch.setattr(gw, "_service_home_for_unit", lambda path, system: None)
    calls: list[list[str]] = []
    state = SimpleNamespace(unit=unit, calls=calls, stop=0, active="active")

    def fake_systemctl(args, *, system=False, **kwargs):
        calls.append(list(args))
        verb = args[0]
        if verb == "stop":
            if state.stop == "timeout":
                raise subprocess.TimeoutExpired(["systemctl", *args], kwargs.get("timeout"))
            return SimpleNamespace(returncode=state.stop, stdout="", stderr="")
        if verb == "is-active":
            return SimpleNamespace(returncode=0 if state.active == "active" else 3, stdout=state.active + "\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(gw, "_run_systemctl", fake_systemctl)
    return state


@pytest.mark.parametrize("stop", [1, "timeout"])
def test_unit_whose_stop_fails_is_reported_not_removed(legacy, stop):
    legacy.stop = stop
    removed, remaining = gw.remove_legacy_hermes_units(interactive=False)
    assert (removed, remaining) == (0, [legacy.unit])
    assert legacy.unit.exists()
    assert ["disable", "hermes.service"] not in legacy.calls


@pytest.mark.parametrize("stop", [1, "timeout"])
def test_failed_stop_reaches_the_leftover_guard(legacy, stop):
    legacy.stop = stop
    with pytest.raises(gw.LegacyUnitsRemain) as exc:
        gw.remove_legacy_units_or_raise()
    assert exc.value.remaining == [legacy.unit]


def test_stop_error_on_an_inactive_unit_still_removes_it(legacy):
    """`stop` on a unit the manager never loaded exits non-zero; nothing runs, so it is removed."""
    legacy.stop, legacy.active = 5, "inactive"
    removed, remaining = gw.remove_legacy_hermes_units(interactive=False)
    assert (removed, remaining) == (1, [])
    assert not legacy.unit.exists()


def test_timeouts_after_a_good_stop_are_reported_not_raised(legacy, monkeypatch):
    """disable / daemon-reload timing out must not escape as TimeoutExpired past the caller's guard."""
    real = gw._run_systemctl

    def slow(args, **kwargs):
        if args[0] in ("disable", "daemon-reload"):
            raise subprocess.TimeoutExpired(["systemctl", *args], kwargs.get("timeout"))
        return real(args, **kwargs)
    monkeypatch.setattr(gw, "_run_systemctl", slow)
    removed, remaining = gw.remove_legacy_hermes_units(interactive=False)
    assert (removed, remaining) == (0, [legacy.unit])
