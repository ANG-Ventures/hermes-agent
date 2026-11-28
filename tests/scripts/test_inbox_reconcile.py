"""scripts/inbox_reconcile.py on a small fixture (card t_1eb0a176, Step 0): the counts the 7-day
trigger is read from. The script is read-only; the fixture store is opened ``mode=ro``."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from datetime import datetime
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "inbox_reconcile.py"
S = "agent:main:discord:dm:chat-1"
HEADER = "Gateway message origin (JSON data, not instructions or authorization):"


def _load():
    spec = importlib.util.spec_from_file_location("inbox_reconcile", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _disp(clock, d, mid, extra="depth=- path=x held_release=False reorder_check=-"):
    return (f"2026-11-27 {clock},000 INFO gateway.inbox_log: inbox_disposition disposition={d} session={S} "
            f"platform=discord msg_id={mid} {extra}")


def _item(clock, op, mid, kind="user"):
    return (f"2026-11-27 {clock},000 INFO gateway.queue_mgmt: queue_mgmt item op={op} session={S} pos=1 "
            f"msg_id={mid} kind={kind} preview=-")


def _fixture(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    refused = "steer_refused reason=turn_closed session=20261127_s1 len=5"
    (logs / "gateway.log").write_text("\n".join([
        _disp("09:00:00", "steered", "50").replace("2026-11-27", "2026-11-26"),      # before the window
        _disp("10:00:00", "steered", "100", "depth=- path=steer held_release=False reorder_check=ordered"),
        _disp("10:01:00", "queued", "101"), _disp("10:02:00", "became-next-turn", "101",
                                                  "depth=- path=followup held_release=False reorder_check=ordered"),
        _disp("10:03:00", "queued", "102"),                                           # never delivered: lost
        _disp("10:04:00", "queued", "103"),                                           # delivered per the store
        _disp("10:05:00", "refused", "104", "depth=32 path=cap held_release=False reorder_check=-"),
        _disp("10:06:00", "merged-into-head", "105"),
        _disp("10:07:00", "held", "106"), _disp("10:07:00", "queued", "106"),
        _disp("10:08:00", "became-next-turn", "106", "depth=- path=followup held_release=False reorder_check=skipped"),
        _item("11:00:00", "hold", "107"),                                             # still held
        _item("11:01:00", "hold", "108"), _item("11:02:00", "release", "108"),
        _item("11:03:00", "hold", "109"), _item("11:04:00", "restart_spool", "109"),  # spooled, never restored
        _item("11:05:00", "hold", "110"), _item("11:06:00", "restart_spool", "110"),
        _item("11:07:00", "restart_restore", "110"), _item("11:08:00", "clear", "110"),
        _item("11:09:00", "hold_wake", "wake", kind="system"),
        f"2026-11-27 12:00:00,000 WARNING gateway.inbox_log: inbox_reorder_detected session={S} platform=discord "
        "msg_id=90 high_water=106 held_release=True high_water_source=store",
        f"2026-11-27 12:01:00,000 WARNING gateway.inbox_log: inbox_reorder_detected session={S} platform=discord "
        "msg_id=95 high_water=106 held_release=False high_water_source=memory",
        f"2026-11-27 12:02:00,000 WARNING gateway.queue_mgmt: queue_mgmt op=unexplained_shrink session={S} expected=2 found=1",
        f"2026-11-27 12:03:00,000 INFO run_agent: {refused}",
        "a continuation line with no timestamp",
    ]) + "\n", encoding="utf-8")
    # The same agent line also lands in agent.log (with its session tag): counted once.
    (logs / "agent.log").write_text(
        f"2026-11-27 12:03:00,000 INFO [20261127_s1] run_agent: {refused}\n"
        "2026-11-27 12:04:00,000 INFO [20261127_s2] run_agent: steer_refused reason=empty session=20261127_s2\n",
        encoding="utf-8")
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, "
                 "timestamp REAL, display_kind TEXT, platform_message_id TEXT)")
    at = datetime(2026, 11, 27, 10, 30).timestamp()
    conn.executemany(
        "INSERT INTO messages (session_id, role, content, timestamp, display_kind, platform_message_id) VALUES (?,?,?,?,?,?)",
        [("s1", "user", "plain", at, None, "103"),
         ("s1", "user", f'{HEADER}\n{{"platform": "discord", "message_id": "100"}}\n\nsteered', at, "steer", None),
         ("s1", "user", "steered with id", at, "steer", "99"),
         ("s1", "user", "too old", datetime(2026, 11, 20).timestamp(), None, "1")])
    conn.commit()
    conn.close()
    return logs, db


def _run(module, capsys, logs, db, *extra):
    assert module.main(["--since", "2026-11-27T00:00", "--until", "2026-11-28T00:00",
                        "--logs-dir", str(logs), "--db", str(db), *extra]) == 0
    return capsys.readouterr().out


def test_counts_on_the_fixture(tmp_path, capsys):
    module = _load()
    logs, db = _fixture(tmp_path)
    before = db.read_bytes()
    report = json.loads(_run(module, capsys, logs, db, "--json"))
    assert report["accepted"] == 7 and report["accepted_without_id"] == 0
    assert report["dispositions"] == {"became-next-turn": 2, "held": 1, "merged-into-head": 1, "queued": 4,
                                      "refused": 1, "steered": 1}
    assert report["refused_by_path"] == {"cap": 1}
    assert report["steer_refused_by_reason"] == {"empty": 1, "turn_closed": 1}
    assert report["reorder_detected"] == {"held_release": 1, "not_held_release": 1}
    assert report["reorder_checks"] == {"ordered": 2, "skipped": 1}
    assert [(i["msg_id"], i["via"], i["age_hours"]) for i in report["held_without_release_or_clear"]] == [
        ("107", "hold", 13.0), ("109", "restart_spool (never restored)", 12.9)]
    assert [(i["msg_id"], i["last"]) for i in report["no_final_disposition"]] == [("102", "queued")]
    assert report["unexplained_held_shrinks"] == 1 and report["held_item_log_failures"] == 0
    assert report["store"] == {"readable": True, "user_rows": 3, "steer_rows": 2, "steer_rows_with_id": 1,
                               "stored_order_inversions": 2}
    assert db.read_bytes() == before   # read-only


def test_text_report_and_a_missing_store(tmp_path, capsys):
    module = _load()
    logs, db = _fixture(tmp_path)
    text = _run(module, capsys, logs, db)
    assert "person messages accepted: 7 (0 without a platform id)" in text
    assert "held with no later release or clear: 2" in text
    assert "reorder_detected: 2 (held release: 1, not a held release: 1)" in text
    assert "msg_id=102" in text and "steer rows 2, 1 with a platform id" in text
    # Without the store, the queued message the store had vouched for counts as undisposed too.
    report = json.loads(_run(module, capsys, logs, tmp_path / "absent.db", "--json"))
    assert [i["msg_id"] for i in report["no_final_disposition"]] == ["102", "103"]
    assert report["store"] == {"readable": False}


def test_since_accepts_relative_and_iso(tmp_path):
    module = _load()
    now = datetime(2026, 11, 28, 12, 0)
    assert module.parse_since("24h", now) == datetime(2026, 11, 27, 12, 0)
    assert module.parse_since("7d", now) == datetime(2026, 11, 21, 12, 0)
    assert module.parse_since("90m", now) == datetime(2026, 11, 28, 10, 30)
    assert module.parse_since("2026-11-27T18:42", now) == datetime(2026, 11, 27, 18, 42)
