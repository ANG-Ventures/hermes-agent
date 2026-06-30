"""Tests for the opt-in Discord raw-reaction journal (discord.reaction_journal).

The journal lets a downstream consumer (greenhouse seed_triage / the
reaction_state durable-state core) capture raw reaction transitions that fire
regardless of message cache. These tests exercise the two journal methods
directly against a lightweight stub adapter — no live gateway, no network — and
assert the on-disk schema is exactly what the reaction_state core ingests.
"""

from __future__ import annotations

import json
import types

from plugins.platforms.discord.adapter import DiscordAdapter


def _stub(journal_path):
    """A bare object carrying just what the journal methods touch, with the two
    real DiscordAdapter methods bound to it (avoids the full adapter ctor)."""
    s = types.SimpleNamespace()
    s.name = "discord"
    s._reaction_journal_path = str(journal_path)
    s._next_reaction_seq = types.MethodType(DiscordAdapter._next_reaction_seq, s)
    s._append_reaction_journal = types.MethodType(
        DiscordAdapter._append_reaction_journal, s)
    return s


class _Emoji:
    def __init__(self, name, id=None):
        self.name = name
        self.id = id


class _Payload:
    def __init__(self, channel_id, message_id, user_id, emoji):
        self.channel_id = channel_id
        self.message_id = message_id
        self.user_id = user_id
        self.emoji = emoji


def _lines(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_append_writes_core_schema(tmp_path):
    journal = tmp_path / "reactions.jsonl"
    s = _stub(journal)
    s._append_reaction_journal(_Payload("C1", "M1", "U1", _Emoji("✅")), "add")
    rows = _lines(journal)
    assert len(rows) == 1
    r = rows[0]
    # exactly the reaction_state core journal contract
    assert set(r) == {"channel_id", "message_id", "emoji", "user_id", "action", "seq", "ts"}
    assert r["channel_id"] == "C1" and r["message_id"] == "M1"
    assert r["emoji"] == "✅" and r["user_id"] == "U1"
    assert r["action"] == "add" and isinstance(r["seq"], int)


def test_custom_emoji_serialized_as_name_id(tmp_path):
    journal = tmp_path / "reactions.jsonl"
    s = _stub(journal)
    s._append_reaction_journal(_Payload("C1", "M1", "U1", _Emoji("partyparrot", 12345)), "add")
    assert _lines(journal)[0]["emoji"] == "partyparrot:12345"


def test_add_then_remove_recorded_in_order(tmp_path):
    journal = tmp_path / "reactions.jsonl"
    s = _stub(journal)
    s._append_reaction_journal(_Payload("C1", "M1", "U1", _Emoji("✅")), "add")
    s._append_reaction_journal(_Payload("C1", "M1", "U1", _Emoji("✅")), "remove")
    rows = _lines(journal)
    assert [r["action"] for r in rows] == ["add", "remove"]
    assert rows[1]["seq"] > rows[0]["seq"]  # monotonic


def test_seq_resumes_above_existing_journal_max(tmp_path):
    # A gateway restart must NOT rewind the seq: a fresh adapter seeds its counter
    # from the existing journal's max seq, so post-restart events out-rank prior
    # ones (else the core would reject them as stale).
    journal = tmp_path / "reactions.jsonl"
    # simulate a pre-restart journal whose max seq is 7 (out-of-order on purpose)
    journal.write_text("\n".join(json.dumps({"seq": s}) for s in (1, 2, 7, 3)) + "\n",
                       encoding="utf-8")
    s = _stub(journal)
    assert s._next_reaction_seq() == 8
    assert s._next_reaction_seq() == 9


def test_seq_seeding_survives_non_dict_lines(tmp_path):
    # P1 regression: a valid-JSON-but-non-dict line (bare string/number/array) must
    # NOT raise AttributeError out of seeding — which would leave _reaction_seq unset
    # and permanently silence the journal for the rest of the process. The bad lines
    # are skipped; the real max seq still wins.
    journal = tmp_path / "reactions.jsonl"
    journal.write_text(
        json.dumps("just a string") + "\n"      # non-dict
        + json.dumps([1, 2, 3]) + "\n"          # non-dict
        + "42\n"                                # bare number
        + json.dumps({"seq": 5}) + "\n",        # the real one
        encoding="utf-8")
    s = _stub(journal)
    assert s._next_reaction_seq() == 6  # 5+1, not crashed, not reset to 1
    # and a subsequent append actually writes (journal not silenced)
    s._append_reaction_journal(_Payload("C1", "M1", "U1", _Emoji("✅")), "add")
    assert _lines(journal)[-1]["action"] == "add"


def test_no_path_is_noop(tmp_path):
    journal = tmp_path / "reactions.jsonl"
    s = types.SimpleNamespace(name="discord", _reaction_journal_path=None)
    s._append_reaction_journal = types.MethodType(
        DiscordAdapter._append_reaction_journal, s)
    # must not raise and must not create the journal when no path is configured
    s._append_reaction_journal(_Payload("C1", "M1", "U1", _Emoji("✅")), "add")
    assert not journal.exists()


def test_append_never_raises_on_bad_payload(tmp_path):
    # Best-effort: a malformed payload must be swallowed (runs inside the gateway
    # event loop; a raise would crash it). No emoji attr at all.
    journal = tmp_path / "reactions.jsonl"
    s = _stub(journal)
    broken = types.SimpleNamespace()  # missing every field
    s._append_reaction_journal(broken, "add")  # must not raise


def test_journal_is_ingestible_by_reaction_state_core(tmp_path):
    """The whole point: the gateway journal must be byte-compatible with the
    reaction_state core's read_journal/replay_journal. Skips cleanly if the core
    isn't present in this checkout (it lives in the greenhouse-tools repo)."""
    import importlib.util
    from importlib.machinery import SourceFileLoader
    import sys
    core_path = (tmp_path.home() / ".hermes" / "greenhouse" / "deployed"
                 / "reaction-state" / "current")
    if not core_path.exists():
        import pytest
        pytest.skip("reaction_state core not present in this checkout")
    # the core file has no .py extension; load it explicitly via SourceFileLoader
    loader = SourceFileLoader("reaction_state", str(core_path))
    spec = importlib.util.spec_from_loader("reaction_state", loader)
    rs = importlib.util.module_from_spec(spec)
    sys.modules["reaction_state"] = rs
    loader.exec_module(rs)

    journal = tmp_path / "reactions.jsonl"
    s = _stub(journal)
    s._append_reaction_journal(_Payload("C1", "M1", "ACE", _Emoji("✅")), "add")
    s._append_reaction_journal(_Payload("C1", "M1", "ACE", _Emoji("✅")), "remove")
    s._append_reaction_journal(_Payload("C1", "M2", "ACE", _Emoji("✅")), "add")

    events = rs.read_journal(str(journal))
    conn = rs.connect(":memory:")
    rs.replay_journal(conn, events)
    present = rs.current_present(conn)
    assert ("C1", "M2", "✅", "ACE") in present       # M2 add stands
    assert ("C1", "M1", "✅", "ACE") not in present   # M1 add+remove → absent
