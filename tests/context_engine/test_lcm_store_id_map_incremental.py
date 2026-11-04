"""Repeat store-id mapping must not recompute every stored row's replay identity.

``_get_store_id_map_for_messages`` runs several times per turn over the whole
uncompacted backlog (compaction preflight, ignored-backlog scan, ledger passes).
Recomputing each stored row's identity (tool-call JSON canonicalization,
placeholder restore) on every call made it 28% of gateway GIL samples
(t_676a681c). The second call must do work proportional to what changed.
"""
from __future__ import annotations

import pytest

from plugins.context_engine.lcm.config import LCMConfig
from plugins.context_engine.lcm.engine import LCMEngine


def _msgs(n: int) -> list[dict]:
    out: list[dict] = []
    for i in range(n):
        if i % 3 == 1:
            out.append({
                "role": "assistant",
                "content": f"calling {i}",
                "tool_calls": [{"id": f"c{i}", "type": "function",
                                "function": {"name": "terminal", "arguments": f'{{"command": "echo {i}"}}'}}],
            })
        elif i % 3 == 2:
            out.append({"role": "tool", "content": f"out {i}", "tool_call_id": f"c{i - 1}"})
        else:
            out.append({"role": "user", "content": f"question {i}"})
    return out


def _engine(tmp_path, n: int) -> tuple[LCMEngine, list[dict], list[int]]:
    eng = LCMEngine(config=LCMConfig())
    eng._bind_storage(str(tmp_path / f"lcm-{n}.db"))
    eng._session_id = "S"
    store_ids = [eng._store.append("S", m) for m in _msgs(n)]
    return eng, [dict(m) for m in _msgs(n)], store_ids


def _count_stored_identity_computes(eng: LCMEngine, monkeypatch) -> dict[str, int]:
    counter = {"n": 0}
    original = LCMEngine._compute_message_replay_identity

    def counting(self, msg, *, stored_row=False):
        if stored_row:
            counter["n"] += 1
        return original(self, msg, stored_row=stored_row)

    monkeypatch.setattr(LCMEngine, "_compute_message_replay_identity", counting)
    return counter


@pytest.mark.parametrize("n", [30, 300])
def test_second_call_recomputes_only_new_rows(tmp_path, monkeypatch, n):
    eng, active, store_ids = _engine(tmp_path, n)
    counter = _count_stored_identity_computes(eng, monkeypatch)

    first = eng._get_store_id_map_for_messages(active)
    assert counter["n"] == n
    assert [first[id(m)] for m in active] == store_ids

    counter["n"] = 0
    second = eng._get_store_id_map_for_messages(active)
    assert counter["n"] == 0, "second call re-derived identities of unchanged rows"
    assert [second[id(m)] for m in active] == store_ids

    new_msg = {"role": "user", "content": "brand new turn"}
    new_id = eng._store.append("S", new_msg)
    counter["n"] = 0
    third = eng._get_store_id_map_for_messages(active + [dict(new_msg)])
    assert counter["n"] == 1
    assert list(third.values()) == store_ids + [new_id]


def test_rewritten_row_is_recomputed_and_map_matches_a_cold_engine(tmp_path, monkeypatch):
    eng, active, store_ids = _engine(tmp_path, 30)
    eng._get_store_id_map_for_messages(active)

    # A GC rewrite changes a tool row's content under the same store_id; the
    # cached identity must not survive it.
    gc_target = store_ids[2]
    placeholder = "[GC'd externalized tool output: chars=5; ref=missing.json]"
    assert eng._store.gc_externalized_tool_result(gc_target, placeholder)
    active[2] = {"role": "tool", "content": placeholder, "tool_call_id": "c1"}

    counter = _count_stored_identity_computes(eng, monkeypatch)
    warm = eng._get_store_id_map_for_messages(active)
    assert counter["n"] == 1

    cold = LCMEngine(config=LCMConfig())
    cold._bind_storage(str(tmp_path / "lcm-30.db"))
    cold._session_id = "S"
    cold_map = cold._get_store_id_map_for_messages(active)
    assert [warm.get(id(m)) for m in active] == [cold_map.get(id(m)) for m in active]
    assert warm[id(active[2])] == gc_target
