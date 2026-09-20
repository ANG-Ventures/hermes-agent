"""The restart-loop guard's boot-level verdict must be HONEST and REACHABLE.

Incident (2026-09-20, 02:01:58). The gateway logged, 64ms apart in one boot:

    02:01:58,227 WARNING Restart-loop breaker TRIPPED: 3 chained
                         restart-interrupted gateway boots ... Skipping
                         auto-resume to break a suspected SIGTERM-respawn loop
    02:01:58,288 WARNING PHASE=boot_resume_scheduled key=...  mode=auto
    02:01:58,291 INFO    Scheduled auto-resume for 1 restart-interrupted session(s)

It announced a skip and then resumed anyway, because the short-circuit was
spelled ``if _tripped and _restart_loop_threshold() <= 0: return 0`` while
``_restart_loop_threshold`` clamps to ``max(1, min(value, 100))`` — never
``<= 0`` for ANY config or env value. The branch was unreachable dead code, and
the log line asserted an action nothing took.

Two properties are pinned here:

1. The clamp/guard contradiction cannot come back. The predicate that decides
   the short-circuit must be satisfiable by some real config value, and the
   deferral must name which mechanism actually owns the break.
2. The chain-gap accounting reaches the restart cadence that was actually
   observed. On 2026-09-20 the boots that scheduled a resume were 796s / 3106s /
   2779s / 12994s / 1379s apart, and the rig's 05:30:36 -> 05:37:12 SIGTERM pair
   was 396s — every one of them wider than the 300s default ``max_gap_seconds``,
   so the chain reset to 1 on each boot and the counter could never reach the
   threshold. The gap must be operator-configurable up to that range and must
   chain correctly when it is.
"""

from __future__ import annotations

import logging

import pytest

from gateway import restart_loop_guard as rlg
from gateway.fork_ext.restart_policy import (
    _AGENT_CONFIG_ENV_BRIDGE,
    _auto_resume_max_attempts,
    _restart_loop_threshold,
)


# --------------------------------------------------------------------------
# 1. The dead-guard contradiction
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["0", "-1", "-999", "", "abc", None],
    ids=["zero", "neg1", "neg999", "empty", "garbage", "unset"],
)
def test_restart_loop_threshold_is_never_non_positive(monkeypatch, raw):
    """The clamp that made the old ``<= 0`` short-circuit unreachable.

    Pinned as an explicit invariant so any future guard written against this
    helper is checked against what it can actually return. This is deliberately
    NOT a snapshot of the default value — it asserts the floor contract.
    """
    if raw is None:
        monkeypatch.delenv("HERMES_RESTART_LOOP_THRESHOLD", raising=False)
    else:
        monkeypatch.setenv("HERMES_RESTART_LOOP_THRESHOLD", raw)
    assert _restart_loop_threshold() >= 1


def test_auto_resume_cap_disable_value_is_reachable(monkeypatch):
    """The cap's escape hatch must not repeat the threshold's off-by-one.

    ``_restart_loop_threshold`` floors at 1, which is why its "disabled"
    sentinel was unreachable. ``_auto_resume_max_attempts`` floors at 0 so the
    documented "0 disables the cap" value survives the clamp.
    """
    monkeypatch.setenv("HERMES_AUTO_RESUME_MAX_ATTEMPTS", "0")
    assert _auto_resume_max_attempts() == 0

    monkeypatch.setenv("HERMES_AUTO_RESUME_MAX_ATTEMPTS", "-4")
    assert _auto_resume_max_attempts() == 0

    monkeypatch.setenv("HERMES_AUTO_RESUME_MAX_ATTEMPTS", "9999")
    assert _auto_resume_max_attempts() == 100

    monkeypatch.setenv("HERMES_AUTO_RESUME_MAX_ATTEMPTS", "garbage")
    assert _auto_resume_max_attempts() >= 1


def test_auto_resume_cap_is_bridged_from_config(monkeypatch):
    """config.yaml is the documented surface; the env var is only the bridge."""
    assert (
        _AGENT_CONFIG_ENV_BRIDGE["auto_resume_max_attempts"]
        == "HERMES_AUTO_RESUME_MAX_ATTEMPTS"
    )

    from gateway.fork_ext.restart_policy import _bridge_agent_config_to_env

    monkeypatch.setenv("HERMES_AUTO_RESUME_MAX_ATTEMPTS", "7")
    _bridge_agent_config_to_env({"auto_resume_max_attempts": 2})
    # config UNCONDITIONALLY wins over a pre-set env var (PR #18413).
    assert _auto_resume_max_attempts() == 2


def test_cap_default_matches_the_documented_config_default():
    """Invariant, not a snapshot: the shipped default IS the helper's fallback.

    Comparing two hardcoded literals would make the test the drift it is meant
    to catch, so read both from their real sources.
    """
    from gateway.auto_resume import DEFAULT_AUTO_RESUME_MAX_ATTEMPTS
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert (
        DEFAULT_CONFIG["agent"]["auto_resume_max_attempts"]
        == DEFAULT_AUTO_RESUME_MAX_ATTEMPTS
    )


def test_cap_helper_default_is_the_module_default(monkeypatch):
    """With no env override the helper returns the module's documented default."""
    from gateway.auto_resume import DEFAULT_AUTO_RESUME_MAX_ATTEMPTS

    monkeypatch.delenv("HERMES_AUTO_RESUME_MAX_ATTEMPTS", raising=False)
    assert _auto_resume_max_attempts() == DEFAULT_AUTO_RESUME_MAX_ATTEMPTS


def test_tripped_breaker_log_does_not_claim_an_action_it_does_not_take(caplog, tmp_path, monkeypatch):
    """The 02:01:58 defect: "Skipping auto-resume" while auto-resume proceeded.

    The module-level breaker does not know what its caller will do, so its log
    line must not assert a specific outcome. The caller emits the authoritative
    ``deferred_to=`` line (asserted through the real scheduler in
    ``test_boot_resume_attempt_cap.py``).
    """
    monkeypatch.setattr(rlg, "_state_path", lambda: tmp_path / "restart_loop.json")

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        tripped = False
        for i in range(3):
            tripped = rlg.check_and_record(3, 60, now=1000.0 + i, max_gap_seconds=300)
    assert tripped is True

    message = "\n".join(r.getMessage() for r in caplog.records)
    assert "TRIPPED" in message
    # The specific false claim that shipped.
    assert "Skipping auto-resume" not in message
    # It must instead point at whoever decides.
    assert "deferred_to=" in message


# --------------------------------------------------------------------------
# 2. Chain-gap coverage for the observed restart cadence
# --------------------------------------------------------------------------


# Inter-boot gaps (seconds) between the 2026-09-20 boots that scheduled an
# auto-resume, read from gateway.log. Every one exceeds the 300s default.
_OBSERVED_OVERNIGHT_GAPS = [796, 3106, 2779, 1379]
# The rig's paired SIGTERMs at 05:30:36 and 05:37:12.
_OBSERVED_RIG_PAIR_GAP = 396


@pytest.mark.parametrize("gap", _OBSERVED_OVERNIGHT_GAPS + [_OBSERVED_RIG_PAIR_GAP])
def test_default_gap_cannot_chain_the_observed_restart_cadence(gap, tmp_path, monkeypatch):
    """Why the breaker never fired on 04:28/05:02/05:36 — documented, not guessed.

    This is the diagnosis pinned as a test: at the shipped default the chain
    breaks on every one of the spacings actually observed, so the counter can
    never leave 1 no matter how long the loop runs.
    """
    monkeypatch.setattr(rlg, "_state_path", lambda: tmp_path / f"rl_{gap}.json")
    assert gap > rlg.DEFAULT_MAX_GAP_SECONDS

    now = 1000.0
    tripped = None
    for _ in range(5):
        tripped = rlg.check_and_record(
            3, 60, now=now, max_gap_seconds=rlg.DEFAULT_MAX_GAP_SECONDS
        )
        now += gap
    assert tripped is False


@pytest.mark.parametrize("gap", _OBSERVED_OVERNIGHT_GAPS + [_OBSERVED_RIG_PAIR_GAP])
def test_widening_max_gap_chains_the_observed_cadence(gap, tmp_path, monkeypatch):
    """...and the knob that fixes it actually reaches that far.

    ``gateway.restart_loop_guard.max_gap_seconds`` is the operator's lever for a
    slow restart cycle; it must chain boots spaced like the real incident.
    """
    monkeypatch.setattr(rlg, "_state_path", lambda: tmp_path / f"rlw_{gap}.json")

    now = 1000.0
    verdicts = []
    for _ in range(3):
        verdicts.append(rlg.check_and_record(3, 60, now=now, max_gap_seconds=14400))
        now += gap
    assert verdicts == [False, False, True]


def test_a_quiet_period_still_breaks_the_chain(tmp_path, monkeypatch):
    """Widening the gap must not make the breaker permanently sticky."""
    monkeypatch.setattr(rlg, "_state_path", lambda: tmp_path / "rl.json")

    assert rlg.check_and_record(3, 60, now=1000.0, max_gap_seconds=14400) is False
    assert rlg.check_and_record(3, 60, now=2000.0, max_gap_seconds=14400) is False
    # A gap wider than max_gap_seconds ends the episode.
    assert rlg.check_and_record(3, 60, now=2000.0 + 14401, max_gap_seconds=14400) is False
