import ast
import inspect

import pytest

import hermes_undo
from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path, monkeypatch):
    session_db = SessionDB(db_path=tmp_path / "state.db")
    monkeypatch.setattr(hermes_undo, "_session_db", session_db)
    hermes_undo.clear_state()
    yield session_db
    session_db.close()
    hermes_undo.clear_state()


def _make_session(db, sid="s1"):
    db.create_session(sid, source="cli")
    return sid


def _active_ids(db, sid):
    return [m["id"] for m in db.get_messages(sid)]


def _seed_three_half_turns(db, sid):
    u1 = db.append_message(sid, "user", "u1")
    a1 = db.append_message(sid, "assistant", "a1")
    u2 = db.append_message(sid, "user", "u2")
    a2 = db.append_message(sid, "assistant", "a2")
    return u1, a1, u2, a2


def test_undo_redo_transition_table_identity_and_redo_count(db):
    sid = _make_session(db)
    ids = _seed_three_half_turns(db, sid)
    before = _active_ids(db, sid)

    undone = hermes_undo.undo(sid, 1)
    assert undone["rewound_ids"] == [ids[-1]]
    assert undone["prefill_text"] == "u2"
    state = hermes_undo.get_state(sid)
    assert [op.rewound_ids for op in state.undo_stack] == [[ids[-1]]]
    assert state.redo_stack == []
    assert _active_ids(db, sid) == before[:-1]

    redone = hermes_undo.redo(sid, 1)
    assert redone == {"reactivated_count": 1, "new_tail_id": ids[-1], "prefill_text": None}
    assert _active_ids(db, sid) == before
    assert state.undo_stack == []
    assert [op.rewound_ids for op in state.redo_stack] == [[ids[-1]]]
    assert db.get_session(sid)["redo_count"] == 1


def test_stacked_ops_have_disjoint_ids_and_redo_lifo_pop_order(db):
    sid = _make_session(db)
    ids = _seed_three_half_turns(db, sid)

    op1 = hermes_undo.undo(sid, 1)
    op2 = hermes_undo.undo(sid, 1)
    op3 = hermes_undo.undo(sid, 1)
    rewound_sets = [set(op["rewound_ids"]) for op in (op1, op2, op3)]
    assert rewound_sets == [{ids[3]}, {ids[2]}, {ids[1]}]
    assert rewound_sets[0].isdisjoint(rewound_sets[1])
    assert rewound_sets[0].isdisjoint(rewound_sets[2])
    assert rewound_sets[1].isdisjoint(rewound_sets[2])

    redone = hermes_undo.redo(sid, 2)
    assert redone["reactivated_count"] == 2
    assert _active_ids(db, sid) == [ids[0], ids[1], ids[2]]
    state = hermes_undo.get_state(sid)
    assert [op.rewound_ids for op in state.redo_stack] == [[ids[1]], [ids[2]]]
    assert [op.rewound_ids for op in state.undo_stack] == [[ids[3]]]


def test_redo_m_non_positive_and_empty_stack_do_not_bump(db):
    sid = _make_session(db)
    db.append_message(sid, "user", "u")

    assert hermes_undo.redo(sid, 0)["message"] == "nothing to redo"
    assert hermes_undo.redo(sid, -1)["message"] == "nothing to redo"
    assert db.get_session(sid)["redo_count"] is None

    assert hermes_undo.redo(sid, 10)["message"] == "nothing to redo"
    assert db.get_session(sid)["redo_count"] is None

    hermes_undo.undo(sid, 1)
    hermes_undo.clear_state(sid)
    cold = hermes_undo.redo(sid, 1)
    assert "doesn't survive a restart" in cold["message"]
    assert db.get_session(sid)["redo_count"] is None


def test_redo_raises_if_restore_reactivates_fewer_than_popped_op(monkeypatch, db):
    sid = _make_session(db)
    _seed_three_half_turns(db, sid)
    hermes_undo.undo(sid, 1)
    monkeypatch.setattr(db, "restore_ids", lambda _sid, _ids: 0)

    with pytest.raises(RuntimeError, match="invariant"):
        hermes_undo.redo(sid, 1)


def test_user_message_append_clears_redo_only(db):
    sid = _make_session(db)
    _seed_three_half_turns(db, sid)
    hermes_undo.undo(sid, 1)
    hermes_undo.redo(sid, 1)
    state = hermes_undo.get_state(sid)
    assert len(state.redo_stack) == 1

    db.append_message(sid, "user", "new branch")
    hermes_undo.on_user_message_appended(sid)

    assert state.redo_stack == []
    assert state.undo_stack == []


def test_new_undo_clears_redo_and_discarded_ops_are_unreachable(db):
    sid = _make_session(db)
    _seed_three_half_turns(db, sid)
    hermes_undo.undo(sid, 1)
    hermes_undo.redo(sid, 1)
    state = hermes_undo.get_state(sid)
    assert state.redo_stack

    hermes_undo.undo(sid, 1)
    assert state.redo_stack == []
    source = inspect.getsource(hermes_undo)
    tree = ast.parse(source)
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "UndoOp"
    ]
    assert len(calls) == 1


def test_d13_fires_on_lone_multimodal_user_tail_and_redo_round_trips(db):
    sid = _make_session(db)
    u_prev = db.append_message(sid, "user", "plain")
    a1 = db.append_message(sid, "assistant", "answer one")
    mm = db.append_message(
        sid,
        "user",
        [{"type": "text", "text": "see this"}, {"type": "image_url", "image_url": "x"}],
    )
    a2 = db.append_message(sid, "assistant", "answer two")
    before = _active_ids(db, sid)
    assert hermes_undo.compute_half_turn_target(db.get_messages(sid), 1) == a2

    result = hermes_undo.undo(sid, 1)

    assert set(result["rewound_ids"]) == {mm, a2}
    assert result["prefill_text"] is None
    assert _active_ids(db, sid) == [u_prev, a1]
    assert db.get_messages(sid)[-1]["id"] == a1

    redo = hermes_undo.redo(sid, 1)
    assert redo["new_tail_id"] == a2
    assert _active_ids(db, sid) == before


def test_d13_firing_fixture_is_reachable_by_two_turn_operation_sequence(db):
    sid = _make_session(db)
    db.append_message(sid, "user", "first text")
    a1 = db.append_message(sid, "assistant", "first answer")
    mm = db.append_message(
        sid,
        "user",
        [{"type": "text", "text": "image prompt"}, {"type": "image_url", "image_url": "x"}],
    )
    a2 = db.append_message(sid, "assistant", "image answer")

    result = hermes_undo.undo(sid, 1)

    assert set(result["rewound_ids"]) == {mm, a2}
    assert _active_ids(db, sid)[-1] == a1
    assert result["prefill_text"] is None


def test_d13_plain_string_control_does_not_lower_target(db):
    sid = _make_session(db)
    db.append_message(sid, "user", "plain")
    db.append_message(sid, "assistant", "answer one")
    user = db.append_message(sid, "user", "editable")
    a2 = db.append_message(sid, "assistant", "answer two")

    result = hermes_undo.undo(sid, 1)

    assert result["rewound_ids"] == [a2]
    assert result["prefill_text"] == "editable"
    assert _active_ids(db, sid)[-1] == user
