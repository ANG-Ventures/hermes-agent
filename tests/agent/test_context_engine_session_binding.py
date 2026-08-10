"""The engine must actually RECEIVE the session store, or persistence is inert.

Measured on the live tree 2026-08-09, and the reason this file exists:

    COMPACTION_SKEW ... ratio=1.405 ... class=tool     <- loop running
    COMPACTION_SKEW ... ratio=1.353 ... class=tool
    sqlite> SELECT COUNT(*) FROM compression_skew_calibration;
    0                                                  <- nothing persisted

Three PRs shipped the calibration stack (#529 persist-across-restart, #539
model-keyed, #541 per-class). All three were correct. All three were INERT for
the LCM engine, because ``agent_init`` binds the session store through::

    getattr(agent.context_compressor, "bind_session_state", None)

``ContextCompressor`` defines it; ``ContextEngine`` did not. Every plugin
engine therefore skipped the bind, ``_session_db`` stayed unset, and
``_persist_skew_history()`` returned at its first guard forever.

These tests assert the WIRING, not just the logic — a persist method that works
in isolation while nothing hands it a DB is precisely the failure that shipped
three times in this subsystem.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from agent.context_engine import ContextEngine  # noqa: E402


class _RecordingDB:
    """Minimal session-store double that records what it was asked to persist."""

    def __init__(self):
        self.model_writes: list[tuple[str, str, list]] = []
        self.session_writes: list[tuple[str, list]] = []

    def record_model_skew_history(self, provider, model, history):
        self.model_writes.append((provider, model, list(history)))

    def record_compression_skew_history(self, session_id, history):
        self.session_writes.append((session_id, list(history)))


class _MinimalEngine(ContextEngine):
    """A plugin-style engine: subclasses the ABC, defines no bind of its own.

    This is the shape of every plugin engine (LCM included) -- the whole point
    is that it must inherit a WORKING bind without knowing it needs one.
    """

    @property
    def name(self) -> str:
        return "minimal"

    def __init__(self):
        self.model = "claude-opus-5"
        self.provider = "claude-apr"
        self._recent_skews = [1.38, 1.22]

    # --- ABC surface, unused by these tests ---
    def update_from_response(self, *a, **k):
        return None

    def should_compress(self, *a, **k):
        return False

    def compress(self, *a, **k):
        return []


def test_abc_defines_bind_session_state() -> None:
    """The bind must exist on the ABC itself, not only on ContextCompressor.

    agent_init resolves it with getattr(); an engine that lacks it skips the
    bind SILENTLY -- no exception, no log, just permanently unset state.
    """
    assert hasattr(ContextEngine, "bind_session_state"), (
        "ContextEngine must define bind_session_state; agent_init binds via "
        "getattr() so a missing method makes persistence inert with no error"
    )
    assert callable(ContextEngine.bind_session_state)


def test_a_plugin_engine_inherits_a_working_bind() -> None:
    """The regression: a subclass defining no bind still receives the store."""
    eng = _MinimalEngine()
    assert getattr(eng, "_session_db", None) is None

    db = _RecordingDB()
    eng.bind_session_state(session_db=db, session_id="sess-1")

    assert eng._session_db is db
    assert eng._session_id == "sess-1"


def test_persist_actually_writes_after_binding() -> None:
    """End of the chain: bind -> persist -> the DB really got the rows.

    Asserting bind alone would repeat the original mistake at one level up.
    """
    eng = _MinimalEngine()
    db = _RecordingDB()
    eng.bind_session_state(session_db=db, session_id="sess-1")

    eng._persist_skew_history()

    assert db.model_writes == [("claude-apr", "claude-opus-5", [1.38, 1.22])], (
        "model-keyed persistence did not reach the store after a bind"
    )
    assert db.session_writes == [("sess-1", [1.38, 1.22])]


def test_persist_is_a_noop_without_a_bind() -> None:
    """The measured production state -- documented so it cannot regress quietly."""
    eng = _MinimalEngine()
    db = _RecordingDB()

    eng._persist_skew_history()  # never bound

    assert db.model_writes == []
    assert db.session_writes == []


def test_bind_tolerates_none_and_empty_session_id() -> None:
    """Binding is best-effort by contract; it must never raise into a turn."""
    eng = _MinimalEngine()
    eng.bind_session_state()
    assert eng._session_db is None
    assert eng._session_id == ""

    eng.bind_session_state(session_db=None, session_id=None)  # type: ignore[arg-type]
    assert eng._session_id == ""


def test_model_keyed_write_is_skipped_when_the_model_is_unknown() -> None:
    """A ratio with no model attached is unattributable -- must not be filed.

    The session-keyed write still happens: it is keyed on the session, not the
    model, so it stays valid.
    """
    eng = _MinimalEngine()
    eng.model = ""
    db = _RecordingDB()
    eng.bind_session_state(session_db=db, session_id="sess-1")

    eng._persist_skew_history()

    assert db.model_writes == []
    assert db.session_writes == [("sess-1", [1.38, 1.22])]


def test_context_compressor_override_still_wins() -> None:
    """The ABC default must not shadow ContextCompressor's richer version.

    ContextCompressor.bind_session_state also rehydrates cooldowns and failure
    streaks; if the ABC default were picked up instead, those would silently
    stop being restored.
    """
    from agent.context_compressor import ContextCompressor

    assert (
        ContextCompressor.bind_session_state is not ContextEngine.bind_session_state
    ), "ContextCompressor must keep its own bind_session_state override"


def test_every_concrete_engine_can_be_bound() -> None:
    """Class-level guard: catch the NEXT engine that would fail this way.

    Enumerates ContextEngine subclasses and asserts each resolves a callable
    bind, which is exactly what agent_init's getattr() will look for.
    """
    subclasses = ContextEngine.__subclasses__()
    assert subclasses, "no ContextEngine subclasses found -- guard would be vacuous"

    for cls in subclasses:
        bind = getattr(cls, "bind_session_state", None)
        assert callable(bind), (
            f"{cls.__name__} has no callable bind_session_state; agent_init "
            f"would skip its bind and its skew persistence would be inert"
        )


def test_agent_init_binds_through_getattr() -> None:
    """Pin the call shape this fix depends on.

    If agent_init ever stops binding here, the ABC default becomes decorative
    and this test should be the thing that notices.
    """
    import re

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    src = open(os.path.join(root, "agent", "agent_init.py"), encoding="utf-8").read()

    assert re.search(
        r"getattr\(\s*agent\.context_compressor\s*,\s*[\"']bind_session_state[\"']",
        src,
    ), (
        "agent_init no longer resolves bind_session_state via getattr on the "
        "engine -- re-check that plugin engines still get their session store"
    )
    assert "_bind_session_state(session_db=session_db" in src, (
        "agent_init no longer passes session_db into the bind"
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
