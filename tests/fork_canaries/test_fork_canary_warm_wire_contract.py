"""Fork canary: the gateway contracts accept every frame the pipecat warm client sends.

The fork's voice hub (pipecat-house-voice ``server/tier3_warm_client.py``) drives this gateway over
``/api/ws``. The 2026-10-01 upstream parity merge (2d966948b0, #1624) replaced ``tui_gateway/contracts``
with ``extra="forbid"`` models that did not know two fork params: ``session.create`` ``cache_scope``
and ``prompt.submit`` ``room``. Nothing in this repo failed; on ACE-AI every tier-2 voice turn
answered 4000 for ~30 min (t_f146c725). This file is the test that goes red on the NEXT such merge
before it ships.

The frames are not written by hand: ``tests/fixtures/warm_wire_contract.json`` is a byte copy of the
file ``pipecat-house-voice/scripts/export_warm_wire_fixture.py`` generates by driving the real client
through a recording transport. pipecat CI fails when its client changes without a re-export; this
side pins the fixture's ``payload_sha256`` so a copy that drifts from the exporter fails here. The
refresh is a two-repo PR: re-export there, copy the file here, teach the contract model the new field.

Registered in ``docs/sync/fork-features.json`` ("gateway contracts accept the fork warm-client
wire") so the parity-merge checklist runs it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tui_gateway import contracts
from tui_gateway.contracts.registry import validate_params

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "warm_wire_contract.json"


def _payload_sha(frames: list) -> str:
    canon = json.dumps(frames, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canon).hexdigest()


@pytest.fixture(scope="module")
def doc() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_fixture_is_the_exporters_output(doc):
    # Same canonicalisation as export_warm_wire_fixture.payload_sha: a hand edit here, or a copy
    # that lags the pipecat export, is a different sha.
    assert doc["source"] == "server/tier3_warm_client.py"
    assert doc["payload_sha256"] == _payload_sha(doc["frames"])
    methods = {f["method"] for f in doc["frames"]}
    assert methods == {"session.create", "prompt.submit", "session.interrupt", "session.close"}


def test_every_recorded_frame_validates_against_its_contract(doc):
    # RED on 2d966948b0: "session.create: cache_scope: Extra inputs are not permitted" (and
    # prompt.submit: room). Drop ``cache_scope`` from tui_gateway/contracts/sessions.py
    # SessionCreateParams or ``room`` from prompt_voice.py PromptSubmitParams to see it fail.
    problems = []
    for frame in doc["frames"]:
        contract = contracts.METHODS.get(frame["method"])
        if contract is None:
            problems.append(f"{frame['method']}: no contract registered")
            continue
        _, problem = validate_params(contract, frame["params"])
        if problem:
            problems.append(f"{frame['method']} ({frame['case']}): {problem}")
    assert not problems, "\n".join(problems)


def test_the_incident_params_are_in_the_fixture(doc):
    # The fixture must still EXERCISE the two params the merge dropped; a re-export that lost them
    # (the client stopped sending them) is a product change to review, not a silent pass.
    creates = [f["params"] for f in doc["frames"] if f["method"] == "session.create"]
    submits = [f["params"] for f in doc["frames"] if f["method"] == "prompt.submit"]
    assert creates and all("cache_scope" in p for p in creates)
    assert submits and all("room" in p for p in submits)
    assert {"system_context" in p for p in submits} == {True, False}


def test_unknown_keys_still_answer_4000(doc):
    # The forbid policy itself is the other half of the contract: accepting the fork's keys must
    # not have been "fixed" by widening the models to extra=allow.
    submit = next(f["params"] for f in doc["frames"] if f["method"] == "prompt.submit")
    _, problem = validate_params(contracts.METHODS["prompt.submit"], {**submit, "rooom": "kitchen"})
    assert problem and "rooom" in problem
    create = next(f["params"] for f in doc["frames"] if f["method"] == "session.create")
    _, problem = validate_params(contracts.METHODS["session.create"], {**create, "cache_scopee": "x"})
    assert problem and "cache_scopee" in problem
