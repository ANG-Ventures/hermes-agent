"""The fork's voice warm client (pipecat-house-voice ``server/tier3_warm_client.py``) sends params the
upstream contracts do not know. With ``extra="forbid"`` an unknown key answers 4000 and every tier-2
voice turn fails (ACE-AI 2026-10-03 19:30 PT, t_f146c725). These pin the exact frames the client sends.
"""

from __future__ import annotations

from tui_gateway import contracts
from tui_gateway.contracts.registry import validate_params


def _validate(method: str, params: dict) -> str | None:
    _, problem = validate_params(contracts.METHODS[method], params)
    return problem


def test_session_create_accepts_cache_scope():
    # tier3_warm_client._create_session: source/title/profile + cache_scope (L3, t_c16a5ab2)
    assert _validate("session.create", {
        "source": "clanker", "title": "warm pool", "profile": "clanker",
        "cache_scope": "clanker:clanker",
    }) is None


def test_prompt_submit_accepts_room_with_system_context():
    # tier3_warm_client.ask on a gateway advertising turn_system_context
    assert _validate("prompt.submit", {
        "session_id": "s1", "text": "turn the lights on",
        "system_context": "this turn came from the kitchen satellite", "room": "kitchen",
    }) is None


def test_prompt_submit_accepts_room_without_system_context():
    # tier3_warm_client.ask on a gateway without the capability: room rides in the text AND as ``room``
    assert _validate("prompt.submit", {
        "session_id": "s1", "text": "[origin_room=kitchen]\nturn the lights on", "room": "kitchen",
    }) is None


def test_unknown_key_still_rejected():
    assert _validate("prompt.submit", {"session_id": "s1", "text": "x", "rooom": "kitchen"})

