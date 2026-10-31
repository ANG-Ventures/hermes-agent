"""Readiness + seat-choice contracts of the route-notes E2E probe instrument (t_cb2583dd).

The probe refused to start whenever the scratch relay said "degraded" (any model capped on every
seat, so most of the week) even with seats eligible, and could only pin fable.
"""

import importlib.util
from pathlib import Path

import pytest

from agent.route_identity import model_label as runtime_label

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rig = _load("relay_drain_e2e_probe")
probe = _load("route_notes_e2e_probe")


@pytest.mark.parametrize("status", ["ok", "degraded"])
def test_serving_status_with_an_eligible_seat_is_ready(status):
    assert rig.health_refusal({"status": status, "eligible_count": 10}) is None


@pytest.mark.parametrize("h", [{"status": "degraded", "eligible_count": 0},
                               {"status": "all_capped", "eligible_count": 0},
                               {"status": "unreachable", "eligible_count": 3}])
def test_refusal_names_the_status_and_count_it_saw(h):
    why = rig.health_refusal(h)
    assert why and repr(h["status"]) in why and f"eligible_count={h['eligible_count']}" in why


def test_seat_refusal_names_the_model_with_no_eligible_seat():
    with pytest.raises(SystemExit) as e:
        probe.choose_seat({probe.FABLE: [], probe.OPUS: ["sub-vps-3"]}, probe.FABLE)
    msg = str(e.value)
    assert f"no eligible seat for {probe.FABLE}" in msg and "--pinned-model" in msg


def test_seat_is_eligible_for_both_models():
    elig = {"claude-sonnet-5": ["sub-vps-2", "sub-vps-11"], probe.OPUS: ["sub-vps-11"]}
    assert probe.choose_seat(elig, "claude-sonnet-5") == "sub-vps-11"


@pytest.mark.parametrize("pinned", [probe.FABLE, "claude-sonnet-5", "claude-haiku-5"])
def test_expected_note_text_follows_the_pinned_model_label(pinned):
    steps = {s[0]: s for s in probe.steps_for(pinned)}
    assert steps[1][1] == pinned and steps[3][3] == pinned
    # the probe's stdlib label must equal the label the runtime renders into the note
    assert steps[1][4] == f"the user selected {runtime_label(pinned)}"
    assert steps[3][4] == f"{runtime_label(pinned)} restored"
