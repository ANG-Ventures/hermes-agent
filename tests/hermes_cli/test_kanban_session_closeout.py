"""``hermes kanban session-closeout`` (t_2d022d30): census across boards, every card listed
once, header accounting, FALSE-DONE detection, rulings / incidents / flips, cost, CLI wiring."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_session_closeout as sc

SID = "20260924_121319_aaaaaaaa"
OTHER = "20260924_121319_ffffffff"


@pytest.fixture
def root(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_SANDBOX", "1")
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_DB", "HERMES_KANBAN_HOME",
                "HERMES_KANBAN_BOARD", "HERMES_SESSION_ID", "HERMES_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    return home


def _conn(home: Path, slug: str = "default"):
    path = home / "kanban.db" if slug == "default" else home / "kanban" / "boards" / slug / "kanban.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return kb.connect(path)


def _card(conn, title, *, body="", status=None, result=None, block=None, run_md=None, sid=SID):
    tid = kb.create_task(conn, title=title, body=body, session_id=sid)
    sets, args = [], []
    if status:
        sets.append("status=?"); args.append(status)
    if result is not None:
        sets.append("result=?"); args.append(result)
    if sets:
        conn.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", (*args, tid))
    if block is not None:
        reason, kind = block
        conn.execute("UPDATE tasks SET status='blocked', block_kind=? WHERE id=?", (kind, tid))
        conn.execute("INSERT INTO task_events(task_id, kind, payload, created_at) VALUES (?,?,?,?)",
                     (tid, "blocked", json.dumps({"reason": reason}), 1))
    if run_md is not None:
        conn.execute("INSERT INTO task_runs(task_id, profile, status, started_at, summary, metadata) "
                     "VALUES (?,?,?,?,?,?)", (tid, "w", "done", 1, "s", json.dumps(run_md)))
    conn.commit()
    return tid


def _fixture(home):
    d = _conn(home)
    ids = {}
    ids["w"] = _card(d, "W8-4: header accounting", status="done",
                     run_md={"pr_url": "https://github.com/ANG-Ventures/hermes-home/pull/10",
                             "live_evidence": "yes: ran it"})
    ids["r"] = _card(d, "r12 D: backup lane", status="done", result="merged ANG-Ventures/hermes-agent#20")
    ids["body"] = _card(d, "fix the pager", body="origin: discord · Ace 'boil the ocean' wave 7. Do X.",
                        status="done", result="shipped")
    ids["auto"] = _card(d, "Prism P1 wake: ANG-Ventures/hermes-home#30 @abc", status="done", result="ok")
    ids["plain"] = _card(d, "something else", status="done")  # bare done: no comment, no result
    ids["ext"] = _card(d, "W9-1: vendor", block=("EXTERNAL: vendor must ship", "dependency"))
    ids["ace"] = _card(d, "W9-2: question", block=("NEEDS RULING: which?", "needs_input"))
    ids["gated"] = _card(d, "W9-3: gated", block=(f"waits on {ids['ace']}", "dependency"))
    ids["noflip"] = _card(d, "W9-4: stuck", block=("hmm", "dependency"))
    kb.add_comment(d, ids["w"], "apollo", "Ace ruled 10-03 14:42: option A, refuse at birth.")
    kb.add_comment(d, ids["w"], "w", "live proof: ran the verb on the live board, 969/969")
    kb.add_comment(d, ids["r"], "apollo", "The root cause is a TZ-dependent ps lstart parse.")
    kb.add_comment(d, ids["body"], "w", "CORRECTION: the earlier count was wrong. close-on: never")
    kb.add_comment(d, ids["noflip"], "w", "Ace 2026-10-03 23:54 'boil the ocean'")
    _card(d, "not mine", sid=OTHER, status="done")
    sub = _conn(home, "subs-ace")
    ids["sub"] = _card(sub, "r5 B: subs board card", status="done",
                       run_md={"pr_urls": ["https://github.com/ANG-Ventures/subs-ace/pull/5"]})
    d.close(); sub.close()
    return ids


STATES = {("ANG-Ventures/hermes-home", 10): "MERGED", ("ANG-Ventures/hermes-agent", 20): "OPEN",
          ("ANG-Ventures/subs-ace", 5): "MERGED"}


def _build(home, **kw):
    return sc.build(home, [SID], pr_query=lambda repo, n: STATES.get((repo, n)), **kw)


def test_every_card_on_every_board_is_listed_exactly_once(root):
    ids = _fixture(root)
    r = _build(root)
    listed = sc.to_json(r)["cards_listed"]
    assert sorted(listed) == sorted(ids.values())          # other session's card excluded
    assert len(listed) == len(set(listed)) == r["census"] == r["total"]
    assert r["per_board"] == {"default": len(ids) - 1, "subs-ace": 1}


def test_header_accounting_splits_external_and_sums(root):
    _fixture(root)
    r = _build(root)
    assert r["by_status"]["external"] == 1
    assert r["by_status"]["blocked"] == 3
    assert sum(r["by_status"].values()) == r["total"]
    assert next(g for g in r["gates"] if g["row"] == "acct")["status"] == "PASS"
    assert "ACCOUNTING MISMATCH" not in sc.render(r)


def test_false_done_open_pr_fails_the_elicitation_gate(root):
    ids = _fixture(root)
    r = _build(root)
    gate = next(g for g in r["gates"] if g["row"] == "-1")
    assert gate["status"] == "FAIL" and ids["r"] in gate["evidence"]
    assert next(g for g in r["gates"] if g["row"] == "6b")["status"] == "FAIL"


def test_recorded_close_and_upstream_prs_do_not_fail_the_pr_gates(root):
    d = _conn(root)
    sup = _card(d, "r1 A: superseded", status="done",
                result="ANG-Ventures/hermes-agent#40 closed SUPERSEDED-BY #41; #41 merged")
    up = _card(d, "r1 B: upstream", status="done", result="opened NousResearch/hermes-agent#90000")
    d.close()
    states = {("ANG-Ventures/hermes-agent", 40): "CLOSED", ("ANG-Ventures/hermes-agent", 41): "MERGED",
              ("NousResearch/hermes-agent", 90000): "OPEN"}
    r = sc.build(root, [SID], pr_query=lambda repo, n: states.get((repo, n)))
    gates = {g["row"]: g for g in r["gates"]}
    assert gates["-1"]["status"] == gates["6b"]["status"] == gates["6b'"]["status"] == "PASS"
    assert "upstream still-open, mention only: 1" in gates["6b"]["evidence"]
    assert sup and up


def test_unread_pr_state_is_never_a_pass(root):
    _fixture(root)
    r = sc.build(root, [SID], network=False)
    assert all(s is None for c in r["cards"] for _, s in c["prs"])
    gates = {g["row"]: g["status"] for g in r["gates"]}
    assert gates["-1"] == gates["6b"] == gates["6b'"] == "FAIL"
    assert "UNREAD" in sc.render(r)


def test_waves_rounds_body_tags_and_automation_groups(root):
    ids = _fixture(root)
    r = _build(root)
    where = {c["id"]: key[2] for key, cs in r["groups"].items() for c in cs}
    assert where[ids["w"]] == "Wave 8"
    assert where[ids["r"]] == "Round 12"
    assert where[ids["body"]] == "Wave 7"
    assert where[ids["auto"]].startswith("Automation-minted: Prism P1 wake")
    assert where[ids["plain"]].startswith("Untagged")
    assert where[ids["sub"]] == "Round 5"


def test_flips_rulings_incidents_and_outcome_marks(root):
    ids = _fixture(root)
    r = _build(root)
    flips = {c["id"]: g for g, rows in r["flips"].items() for c, _ in rows}
    assert flips[ids["ext"]] == "external"
    assert flips[ids["ace"]] == "needs Ace"
    assert flips[ids["gated"]] == "gated on in-session card"
    assert flips[ids["noflip"]] == "no flip named"
    stamps = {x["stamp"] for x in r["rulings"]}
    assert {"10-03 14:42", "2026-10-03 23:54"} <= stamps
    assert [i["card"] for i in r["incidents"]] == [ids["r"]]
    by = {c["id"]: c for c in r["cards"]}
    assert by[ids["w"]]["proof"] and by[ids["body"]]["retracted"]
    bare = next(g for g in r["gates"] if g["row"] == "11'")
    assert ids["plain"] in bare["evidence"] and ids["w"] not in bare["evidence"]


def test_worker_cost_sums_turns_keyed_on_card_ids_across_profiles(root):
    ids = _fixture(root)
    for name in ("blackbox", "profiles/daedalus/blackbox"):
        path = root / name / "turns.db"
        path.parent.mkdir(parents=True)
        with sqlite3.connect(path) as b:
            b.execute("CREATE TABLE turns (turn_id TEXT PRIMARY KEY, chat_id TEXT, ts_end REAL, cost_usd REAL, "
                      "cost_status TEXT, input_tokens INT, output_tokens INT, cache_read INT, cache_write INT)")
            b.execute("INSERT INTO turns VALUES ('a', ?, 5, 1.5, 'estimated', 10, 20, 30, 40)", (ids["w"],))
            b.execute("INSERT INTO turns VALUES ('u', ?, 5, NULL, 'unknown', 1, 1, 1, 1)", (ids["r"],))
            b.execute("INSERT INTO turns VALUES ('b', 't_00000000', 5, 99, 'estimated', 1, 1, 1, 1)")
    cost = _build(root)["cost"]["workers"]
    assert cost["total"] == [4, 3.0, 22, 42, 62, 82, 2, 0]   # unpriced turns counted, not summed as $0
    assert set(cost["per_profile"]) == {"default", "daedalus"}
    assert "known-only (2 turns unpriced" in sc._tok(cost["total"])


def test_session_turns_come_from_the_owning_profile_by_turn_id_prefix(root):
    _fixture(root)
    prof = root / "profiles" / "athena"
    (prof / "blackbox").mkdir(parents=True)
    with sqlite3.connect(prof / "state.db") as st:
        st.execute("CREATE TABLE sessions (id TEXT, started_at REAL, ended_at REAL, chat_id TEXT, "
                   "estimated_cost_usd REAL)")
        st.execute("INSERT INTO sessions VALUES (?, 1, 9, 'chan', 7.5)", (SID,))
    for path, rows in ((prof / "blackbox" / "turns.db", [(f"{SID}:{SID}:a", 2.0), ("other:other:x", 50.0)]),
                       (root / "blackbox" / "turns.db", [(f"{SID}:{SID}:z", 1000.0)])):  # wrong profile: ignored
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as b:
            b.execute("CREATE TABLE turns (turn_id TEXT PRIMARY KEY, chat_id TEXT, cost_usd REAL, cost_status TEXT, "
                      "input_tokens INT, output_tokens INT, cache_read INT, cache_write INT)")
            for tid, usd in rows:
                b.execute("INSERT INTO turns VALUES (?, 'chan', ?, 'estimated', 1, 1, 1, 1)", (tid, usd))
    (sess,) = _build(root)["cost"]["sessions"]
    assert sess["profile"] == "athena" and sess["state_db_usd"] == 7.5
    assert sess["session_turns"]["row"][:2] == [1, 2.0] and sess["session_turns"]["unreadable"] is None


def test_cli_verb_writes_both_outputs_and_check_exit(root, tmp_path, capsys):
    _fixture(root)
    from hermes_cli import kanban as kc
    from hermes_cli import kanban_parser

    top = argparse.ArgumentParser()
    kanban_parser.build_parser(top.add_subparsers(dest="cmd"))
    out, vault = tmp_path / "plans" / "c.md", tmp_path / "vault" / "AI" / "Closeouts" / "c.md"
    args = top.parse_args(["kanban", "session-closeout", SID, "--no-network", "--root", str(root),
                           "--out", str(out), "--vault-out", str(vault), "--check"])
    assert kc.kanban_command(args) == 1                        # --check: some gate FAILs
    assert out.read_text(encoding="utf-8-sig") == vault.read_text(encoding="utf-8-sig")
    head = out.read_text(encoding="utf-8-sig").split("---\n")[1]
    fm = dict(line.split(": ", 1) for line in head.splitlines())
    # obsidian-vault vault_pr_lint vocabulary: VALID_T / VALID_S / last_verified YYYY-MM-DD
    assert fm["type"] == "handoff" and fm["status"] == "active"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", fm["last_verified"])
    printed = capsys.readouterr().out
    assert "PASS  5" in printed and "FAIL" in printed


def test_unknown_session_exits_2(root, capsys):
    _fixture(root)
    ns = argparse.Namespace(session_ids=["nope"], root=str(root), vault=None, no_network=True,
                            out=None, vault_out=None, json=False, check=False)
    assert sc.run(ns) == 2


def test_prism_r1_proof_close_record_own_prs_and_parent_gated_todo(root):
    d = _conn(root)
    compact = _card(d, "r2 A: compact md", status="done",
                    run_md={"live_evidence": "yes: saw it", "pr_url": "https://github.com/ANG-Ventures/a/pull/1"})
    negated = _card(d, "r2 B: negated", status="done", result="ANG-Ventures/a#2 merged")
    kb.add_comment(d, negated, "w", "No live proof available yet; native proof failed.")
    url_closed = _card(d, "r2 C: url close", status="done", result="see below")
    kb.add_comment(d, url_closed, "w", "CLOSED: https://github.com/ANG-Ventures/a/pull/3 — superseded")
    own = _card(d, "r2 D: own_prs only", status="review", result="awaiting merge",
                run_md={"own_prs": ["ANG-Ventures/a#4"], "survivor": {"refs": [{"pr": "ANG-Ventures/a#5"}]}})
    parent = _card(d, "r2 E: parent", block=("NEEDS RULING", "needs_input"))
    child = kb.create_task(d, title="r2 F: child", session_id=SID, parents=[parent])
    d.commit(); d.close()
    # url_closed's PR is only named in a comment: give it a metadata pointer so it is the card's own PR
    with sqlite3.connect(root / "kanban.db") as c:
        c.execute("INSERT INTO task_runs(task_id, profile, status, started_at, metadata) VALUES (?,?,?,?,?)",
                  (url_closed, "w", "done", 1, json.dumps({"pr": "https://github.com/ANG-Ventures/a/pull/3"})))
    states = {("ANG-Ventures/a", n): s for n, s in ((1, "MERGED"), (2, "MERGED"), (3, "CLOSED"),
                                                    (4, "OPEN"), (5, "MERGED"))}
    r = sc.build(root, [SID], pr_query=lambda repo, n: states.get((repo, n)))
    by = {c["id"]: c for c in r["cards"]}
    assert by[compact]["proof"] and not by[negated]["proof"]
    assert {ref for ref, _ in by[own]["prs"]} == {("ANG-Ventures/a", 4), ("ANG-Ventures/a", 5)}
    gates = {g["row"]: g for g in r["gates"]}
    assert "ANG-Ventures/a#3" not in gates["6b'"]["evidence"]
    assert "ANG-Ventures/a#4" in gates["6b"]["evidence"]
    flips = {c["id"]: g for g, rows in r["flips"].items() for c, _ in rows}
    assert by[child]["status"] == "todo" and flips[child] == "gated on in-session card"


def test_card_queries_chunk_past_the_sqlite_variable_limit(root, monkeypatch):
    d = _conn(root)
    ids = [_card(d, f"r3 A: bulk {i}", status="done") for i in range(12)]
    for i in ids:
        kb.add_comment(d, i, "w", "ok")
    d.close()
    monkeypatch.setattr(sc, "_CHUNK", 5)
    cards, bad = sc.load_cards(root, [SID])
    assert not bad and len(cards) == 12 and all(len(c["comments"]) == 1 for c in cards)


def test_prism_r2_requests_are_not_proof_close_records_bind_exact_repo_summary_prs(root):
    d = _conn(root)
    req = _card(d, "r4 A: request", status="done", result="ANG-Ventures/a#1 merged")
    for body in ("Please attach live proof before closing", "Where is the native proof?",
                 "TODO: collect live proof before closing"):
        kb.add_comment(d, req, "w", body)
    other = _card(d, "r4 B: other repo close", status="done", result="ANG-Ventures/a#42 see notes")
    kb.add_comment(d, other, "w", "ANG-Ventures/b#42 closed")
    summ = _card(d, "r4 C: summary only", status="review")
    d.execute("INSERT INTO task_runs(task_id, profile, status, started_at, summary) VALUES (?,?,?,?,?)",
              (summ, "w", "done", 1, "handed off ANG-Ventures/a#7 for review"))
    d.commit(); d.close()
    states = {("ANG-Ventures/a", 1): "MERGED", ("ANG-Ventures/a", 42): "CLOSED", ("ANG-Ventures/a", 7): "OPEN"}
    r = sc.build(root, [SID], pr_query=lambda repo, n: states.get((repo, n)))
    by = {c["id"]: c for c in r["cards"]}
    assert not by[req]["proof"]
    gates = {g["row"]: g for g in r["gates"]}
    assert "ANG-Ventures/a#42" in gates["6b'"]["evidence"]       # b#42's close does not explain a#42
    assert "ANG-Ventures/a#7" in gates["6b"]["evidence"]         # summary-only handoff PR is found
    assert gates["1"]["status"] == "FAIL"


def test_prism_r2_no_eligible_card_and_unchecked_docs_never_pass(root):
    d = _conn(root)
    c = _card(d, "r5 A: open pr", status="review", result="ANG-Ventures/a#9",
              body="see skills/x/SKILL.md and AI/Fleet/Thing.md")
    d.close()
    (root / "skills-shared" / "x").mkdir(parents=True)
    (root / "skills-shared" / "x" / "SKILL.md").write_text("x", encoding="utf-8")
    r = sc.build(root, [SID], pr_query=lambda repo, n: "OPEN")
    gates = {g["row"]: g for g in r["gates"]}
    assert gates["1"]["status"] == "FAIL"                         # no done PR card: no evidence
    assert gates["4"]["status"] == "FAIL" and "1 unchecked" in gates["4"]["evidence"]
    assert {d["path"] for d in r["docs"]} == {"skills/x/SKILL.md", "AI/Fleet/Thing.md"}
    assert c


def test_prism_r2_unreadable_ledgers_are_reported_not_zero(root):
    _fixture(root)
    bad = root / "profiles" / "daedalus" / "blackbox" / "turns.db"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"not a sqlite database at all, just bytes" * 10)
    r = _build(root)
    assert r["cost"]["workers"]["unreadable"] and r["cost"]["workers"]["unreadable"][0].startswith("daedalus:")
    assert "INCOMPLETE, unreadable ledgers" in sc.render(r)
