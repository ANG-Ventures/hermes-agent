"""Verdict logic of scripts/e2e_internal_silence.py (the live E2E for t_bb4059d2)."""

import importlib.util
import sys
from pathlib import Path

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "e2e_internal_silence.py"
_spec = importlib.util.spec_from_file_location("e2e_internal_silence", _PATH)
e2e = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = e2e  # dataclasses resolve their module through sys.modules
_spec.loader.exec_module(e2e)

P = e2e.Posted


def test_long_arm_passes_only_on_the_tokened_message_without_marker():
    assert e2e.arm_verdict("long", "ab12", []) == ("none", False, False)
    other = P("1", 1.0, "started")
    assert e2e.arm_verdict("long", "ab12", [other]) == ("none", False, False)
    assert e2e.arm_verdict("long", "ab12", [other, P("2", 2.0, "Done. E2E-LONG-ab12 ok.")]) == ("2", False, True)
    assert e2e.arm_verdict("long", "ab12", [P("3", 2.0, "E2E-LONG-ab12\n\nNO_REPLY")]) == ("3", True, False)


def test_quiet_arms_fail_on_any_post_after_the_completion():
    for arm in ("short", "bare"):
        assert e2e.arm_verdict(arm, "x", []) == ("none", False, True)
        assert e2e.arm_verdict(arm, "x", [P("9", 1.0, "Already handled.\nNO_REPLY")]) == ("9", True, False)


def test_turn_end_wait_reports_timeout_instead_of_passing(monkeypatch):
    """Prism 7f089cac187d: a stalled completion turn must not read as successful silence."""
    clock = iter(range(0, 10_000, 100))
    monkeypatch.setattr(e2e.time, "time", lambda: next(clock))
    monkeypatch.setattr(e2e.time, "sleep", lambda _s: None)
    monkeypatch.setattr(e2e, "_db_rows", lambda *_a: [(0,)])
    assert e2e._wait_turn_end("home", 0.0) is False
    monkeypatch.setattr(e2e, "_db_rows", lambda *_a: [(1,)])
    assert e2e._wait_turn_end("home", 0.0) is True


def test_prompts_name_the_arm_shape():
    assert "E2E-LONG-ab12" in e2e.prompt_for("long", "ab12")
    assert "Already handled." in e2e.prompt_for("short", "ab12")
    assert "notify_on_complete=true" in e2e.prompt_for("bare", "ab12")


class _FakeDiscord:
    """A thread where the bot's reply first lands carrying the nonce, then a later edit
    appends the marker (what a streaming transport does)."""

    def __init__(self, target_id: str, frames: list[str]):
        self.target_id, self.frames, self.gets = target_id, frames, 0

    def req(self, method, path, body=None):
        if method == "POST":
            return {"id": "th1" if path.endswith("/threads") else "100"}
        content = self.frames[min(self.gets, len(self.frames) - 1)]
        self.gets += 1
        return [{"id": "200", "timestamp": "2026-10-18T00:00:10+00:00",
                 "author": {"id": self.target_id}, "content": content}]


def test_long_arm_judges_the_final_edit_not_the_first_frame(monkeypatch, tmp_path, capsys):
    """Prism 7f089cac187d on #2060: a later edit that adds NO_REPLY must fail the long arm."""
    clock = [e2e._iso_ts("2026-10-18T00:00:05+00:00")]
    monkeypatch.setattr(e2e.time, "time", lambda: clock[0])
    monkeypatch.setattr(e2e.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(e2e.secrets, "token_hex", lambda _n: "ab12")
    monkeypatch.setattr(e2e, "_wait_completion", lambda _h, _s: (clock[0], ""))
    monkeypatch.setattr(e2e, "_wait_turn_end", lambda _h, _s: True)
    monkeypatch.setattr(e2e, "_log_has", lambda *_a: False)
    body = "Done. E2E-LONG-ab12 ok. Exit 0."
    dc = _FakeDiscord("bot", [body, body + "\n\nNO_REPLY"])
    env = {"PUPPET_LANE_HOME": str(tmp_path), "PUPPET_HEAD_SHA": "f" * 40, "PUPPET_CH": "c1",
           "PUPPET_THREADS": str(tmp_path / "threads")}

    assert e2e.run_arm("long", env, None, dc, "bot") is False
    out = capsys.readouterr().out
    assert "verdict=FAIL" in out and "marker_leaked=y" in out


def test_long_arm_passes_when_the_final_content_is_clean(monkeypatch, tmp_path):
    clock = [e2e._iso_ts("2026-10-18T00:00:05+00:00")]
    monkeypatch.setattr(e2e.time, "time", lambda: clock[0])
    monkeypatch.setattr(e2e.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(e2e.secrets, "token_hex", lambda _n: "ab12")
    monkeypatch.setattr(e2e, "_wait_completion", lambda _h, _s: (clock[0], ""))
    monkeypatch.setattr(e2e, "_wait_turn_end", lambda _h, _s: True)
    monkeypatch.setattr(e2e, "_log_has", lambda *_a: False)
    dc = _FakeDiscord("bot", ["Done. E2E-LONG-ab12", "Done. E2E-LONG-ab12 ok. Exit 0."])
    env = {"PUPPET_LANE_HOME": str(tmp_path), "PUPPET_HEAD_SHA": "f" * 40, "PUPPET_CH": "c1",
           "PUPPET_THREADS": str(tmp_path / "threads")}

    assert e2e.run_arm("long", env, None, dc, "bot") is True
