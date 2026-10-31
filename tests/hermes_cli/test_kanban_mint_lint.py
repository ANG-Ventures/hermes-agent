"""Mint-time lint on the kanban create surfaces (CLI + kanban_create tool), t_6d1df155.

TIME GATE: t_e1319480 read "BLOCKED UNTIL ... 24 h of shadow have elapsed" but was
minted with a ``blocks`` edge, which released it one minute into the window. A
blocks-edge card that reads time-gated is refused unless it is minted ``--until``
(parked ``scheduled``) or says ``--i-mean-blocks``.

OPEN DUPLICATE: t_22499911 / t_07450218 re-filed Momus passes whose cards
(t_d9f9b6de / t_aed3fd25) were still open. A title whose first 80 chars score
>= 0.6 against an OPEN card, or the same "Momus pass N" + spec, is refused unless
``--not-a-duplicate "<why>"``.
"""

from __future__ import annotations

import datetime as _dt
import json
import shlex
import time
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb

# t_e1319480 verbatim (origin line dropped: create stamps its own).
GATED_TITLE = 'KWS VAD gate live E2E (after pcv#899 merges + clanker-deploy): 24 h shadow numbers per room + NEAR_MISS miss count with n; enforcing flip + kws-load-sample before/after; bench 3 dist x 2 vol x 20 trials (VAD-off baseline first); barge-during-TTS unchanged; overnight asleep_frac > 0 in >= 2 rooms + wake-from-asleep latency hop 1000 vs 250; week of felt-deaf'
GATED_BODY = (
    'host: studio (ssh ace-ai-lan for the hub). Parent t_5feabff8 landed the gate: ANG-Ventures/pipecat-house-voice#899 (SHADOW by default: WEBRTC_KWS_VAD=1, WEBRTC_KWS_VAD_SHADOW=1, THRESHOLD 0.5, HANGOVER_S 1.0, FEEDS_SLEEP 1). Design + offline numbers + the pending list: vault Engineering/HA Voice/"Clanker Wake Gating — how a mic frame becomes a GPU inference (2026-10-12)" step 8; repo docs/CONFIG-KNOB-MAP.md row "KWS speech-presence gate".\n'
    '\n'
    "BLOCKED UNTIL: #899 merged AND scripts/clanker-deploy.sh has restarted the clanker-webrtc@<room> units on ACE-AI (every room's journal shows KWS_TICK_STATS lines carrying vad_shadow=1), AND 24 h of shadow have elapsed. Operator (Apollo) owns the merge, the deploy, and the shadow->enforcing flip (WEBRTC_KWS_VAD_SHADOW=0 in ~/.config/clanker-satellites/_fleet/hub.env via the manifest/render path, then clanker-deploy.sh); this card measures.\n"
    '\n'
    'Stages (card body of t_5feabff8 E2E (1)-(6)):\n'
    '(1) Shadow 24 h: per room, from `journalctl --user -u clanker-webrtc@<room> -o cat | grep KWS_TICK_STATS`: median vad_skip_frac, vad_would_skip sum, vad_p50_ms/p95_ms; miss count = `grep -c "KWS_VAD_NEAR_MISS reason=shadow_scored"` over the 24 h vs accepted wakes n (TURN_RECORD accepted / WAKE_LATENCY lines). State n and the 95% bound (0 in n ~ 3/n). Any NEAR_MISS line: read its vad_prob and the wake\'s audio (~/clanker-utterances/<date>/<session>/detector_input.wav.enc) before deciding threshold vs hangover.\n'
    '(2) Enforcing: run skills-shared/smart-home/clanker-satellite-registry/scripts/kws-load-sample.sh 60 on ACE-AI BEFORE the flip (baseline; reference 21.5 inferences/s, ~45% GPU wall, 38% core) and AFTER at the same hour; report inferences/s, batches/s, GPU-time estimate, sidecar CPU.\n'
    "(3) Bench acoustic matrix, D-54 bench only (.97, Ace's bedroom, quiet_until_moved: needs an Ace-cleared window or the shed-rig move S2 t_950259cc; `satellitectl quiet bench off` for the session, back on after). Skills clanker-wake + live-acoustic-test-integrity. Same rig, same session: VAD OFF first (WEBRTC_KWS_VAD=0 drop-in on the bench unit only) for the baseline pass rate, then VAD enforcing. 3 distances x 2 volumes x 20 trials each arm; pass = DETECT + arbitration WINNER. Enforcing pass rate >= baseline per cell; state n per cell.\n"
    '(4) clanker-barge: barge-during-TTS score distribution unchanged (gate is out of scope while the bot speaks by construction; measure anyway).\n'
    '(5) Overnight after enforcing: asleep_frac > 0 in >= 2 rooms (KWS_TICK_STATS); wake-from-asleep latency on the bench: WAKE_LATENCY lines with sleep_wakeback=1 vs without, 20 trials each at sleep hop 1000 ms, p50/p95 total_ms; say whether 1000 is acceptable or WEBRTC_KWS_SLEEP_HOP_MS needs 500.\n'
    '(6) Felt-deaf counts (clanker-felt-deaf-monitor journal / FeltDeafTracker) for the enforcing week vs the prior week.\n'
    '\n'
    'Hand back: the six numbers blocks appended to the vault note step 8 (replace the "Still pending" list), and a one-line verdict on whether the sidecar consolidation (appliance spec D1) may proceed on the measured rate. Never lower a wake threshold to make a cell pass (clanker-wake scar #10d).\n'
)

# t_d9f9b6de / t_aed3fd25 (the open passes) and the re-files t_22499911 / t_07450218.
MOMUS2_OPEN = ("Momus pass 2: Fleet Work Allocation \u2014 Traceability-vs-Placement Separation "
               "spec v1.1 (Cadmus fold t_f6f8377f; --prior = pass 1 t_13c0a00f comment 42512)")
MOMUS2_REFILE = ("Momus pass 2: Fleet Work Allocation \u2014 Traceability-vs-Placement Separation "
                 "spec v1.1 (2026-10-11)")
MOMUS1_OPEN = ("Momus pass 1: Clanker Voice Appliance \u2014 GPU in ci-box (spec, 2026-10-12); "
               "read-only review, mirror verdict into the vault")
MOMUS1_REFILE = ("Momus pass 1: Clanker Voice Appliance spec v1.0 (GPU in ci-box, whole voice path "
                 "off ACE-AI): adversarial review, scope incl. M1 (C5 acceptance bar as a number of "
                 "quiet days + voice must not depend on /srv/ci) and M2 (user-systemd vs system "
                 "units on a headless appliance)")


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    kb.init_db()
    return home


def _events(conn, tid, kind):
    return [
        json.loads(r["payload"]) if r["payload"] else None
        for r in conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ?", (tid, kind))
    ]


def _parent(conn):
    return kb.create_task(conn, title="pcv#899 KWS VAD gate PR", body="parent", assignee="daedalus")


# --- time gate: library ---------------------------------------------------------------

def test_time_gated_blocks_child_refused_without_until(kanban_home):
    with kb.connect_closing() as conn:
        parent = _parent(conn)
        with pytest.raises(kb_lint().TimeGateError) as exc:
            kb.create_task(conn, title=GATED_TITLE, body=GATED_BODY, assignee="argus",
                           parents=[parent], time_gate_guard=True)
        assert "reads time-gated" in str(exc.value) and "--until" in str(exc.value)
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1


def test_until_mints_scheduled_with_timed_wake(kanban_home):
    until = int(time.time()) + 24 * 3600
    with kb.connect_closing() as conn:
        parent = _parent(conn)
        tid = kb.create_task(conn, title=GATED_TITLE, body=GATED_BODY, assignee="argus",
                             parents=[parent], time_gate_guard=True, until=until)
        task = kb.get_task(conn, tid)
        assert task.status == "scheduled"
        assert task.next_eligible_at == until
        assert _events(conn, tid, "scheduled")[0]["kind"] == "deferred"
        assert _events(conn, tid, "scheduled")[0]["until"] == until
        # The parent closing does not release it; the wake time does.
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))
        conn.commit()
        kb.recompute_ready(conn)
        assert kb.get_task(conn, tid).status == "scheduled"
        assert kb.wake_due_scheduled(conn, now=until - 1) == []
        assert kb.wake_due_scheduled(conn, now=until) == [tid]
        assert kb.get_task(conn, tid).status == "ready"


def test_i_mean_blocks_mints_and_records_event(kanban_home):
    with kb.connect_closing() as conn:
        parent = _parent(conn)
        tid = kb.create_task(conn, title=GATED_TITLE, body=GATED_BODY, assignee="argus",
                             parents=[parent], time_gate_guard=True, i_mean_blocks=True)
        assert kb.get_task(conn, tid).status == "todo"
        forced = _events(conn, tid, "time_gate_forced")
    assert len(forced) == 1 and forced[0]["parents"] == [parent]
    assert "24 h" in forced[0]["match"] or "shadow" in forced[0]["match"]


@pytest.mark.parametrize("text", [
    "Read the VAD shadow numbers tomorrow 01:30",       # bare marker, no lead word
    "run this on 2026-10-13 after the window",          # future date before 'after'
    "Wait until\n24 hours of shadow have elapsed",      # wrapped across a line break
    "check the load tonight at 23:00",
    "next Monday re-run the bench",
])
def test_time_gate_shapes_without_lead_word_or_on_one_line(text):
    assert kb_lint().time_gate_match(text, None, today=_dt.date(2026, 10, 12))


@pytest.mark.parametrize("text", [
    "Momus pass 2: spec v1.1 (2026-10-11)",              # provenance stamp: past date
    "filed 2026-10-12 from the alerts sweep",            # same-day stamp
    "3 false positives tonight incl. the card itself",   # past-tense tonight
    "fix the bug after the parent PR lands",
    "when the test fails, read the log",
])
def test_non_gate_text_passes(text):
    assert kb_lint().time_gate_match(text, None, today=_dt.date(2026, 10, 12)) is None


def test_wrapped_body_blocks_child_refused(kanban_home):
    with kb.connect_closing() as conn:
        parent = _parent(conn)
        with pytest.raises(kb_lint().TimeGateError):
            kb.create_task(conn, title="Read the shadow numbers", assignee="argus",
                           body="Wait until\n24 hours of shadow have elapsed.",
                           parents=[parent], time_gate_guard=True)


def test_derived_from_and_parentless_time_text_are_not_gated(kanban_home):
    with kb.connect_closing() as conn:
        parent = _parent(conn)
        kb.create_task(conn, title=GATED_TITLE, body=GATED_BODY, assignee="argus",
                       parents=[parent], parents_kind="derived-from", time_gate_guard=True)
        kb.create_task(conn, title=GATED_TITLE + " (standalone)", body=GATED_BODY,
                       assignee="argus", time_gate_guard=True, duplicate_guard=False)


def test_plain_blocks_child_passes(kanban_home):
    with kb.connect_closing() as conn:
        parent = _parent(conn)
        tid = kb.create_task(conn, title="Deploy the KWS VAD gate to every room",
                             body="Run clanker-deploy.sh once the PR merges.",
                             assignee="daedalus", parents=[parent], time_gate_guard=True)
        assert kb.get_task(conn, tid).status == "todo"
        assert _events(conn, tid, "time_gate_forced") == []


# --- open duplicate: library ----------------------------------------------------------

@pytest.mark.parametrize("open_title,refile", [
    (MOMUS2_OPEN, MOMUS2_REFILE), (MOMUS1_OPEN, MOMUS1_REFILE)])
def test_momus_refile_of_open_pass_refused_naming_it(kanban_home, open_title, refile):
    with kb.connect_closing() as conn:
        first = kb.create_task(conn, title=open_title, body="pass", assignee="momus")
        with pytest.raises(kb.NearDuplicateError) as exc:
            kb.create_task(conn, title=refile, body="re-file", assignee="momus",
                           duplicate_guard=True)
    msg = str(exc.value)
    assert first in msg and "status ready" in msg and "assignee momus" in msg
    assert exc.value.duplicates[0]["id"] == first


def test_not_a_duplicate_files_and_records_event(kanban_home):
    with kb.connect_closing() as conn:
        first = kb.create_task(conn, title=MOMUS2_OPEN, body="pass", assignee="momus")
        second = kb.create_task(conn, title=MOMUS2_REFILE, body="re-file", assignee="momus",
                                duplicate_guard=True, force_reason="pass 2 on the v1.2 fold")
        forced = _events(conn, second, "near_duplicate_forced")
    assert forced[0]["reason"] == "pass 2 on the v1.2 fold"
    assert forced[0]["open_duplicates"][0]["id"] == first
    assert forced[0]["open_duplicates"][0]["rule"] == "momus_pass"


def test_done_card_is_not_an_open_duplicate(kanban_home):
    with kb.connect_closing() as conn:
        first = kb.create_task(conn, title=MOMUS1_OPEN, body="pass", assignee="momus")
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (first,))
        conn.commit()
        kb.create_task(conn, title=MOMUS1_REFILE, body="re-file", assignee="momus",
                       duplicate_guard=True)


def test_series_siblings_keyed_automation_and_parent_pass(kanban_home):
    p2 = "Fleet Work Placement Phase 2 BUILD (PENDING browser-sandbox research t_11433423): derivation"
    p3 = "Fleet Work Placement Phase 3 BUILD (PENDING browser-sandbox research t_11433423): continuation"
    page = "P80 #alerts page with no owner: deploy-parity@studio `studio:prism-router` (1 episode open)"
    with kb.connect_closing() as conn:
        a = kb.create_task(conn, title=p2, body="x", assignee="daedalus")
        kb.create_task(conn, title=p3, body="y", assignee="daedalus", duplicate_guard=True)
        kb.create_task(conn, title=page, body="z", assignee="daedalus")
        kb.create_task(conn, title=page.replace("prism-router", "hermes"), body="z",
                       assignee="daedalus", idempotency_key="pager:studio:x:1",
                       duplicate_guard=True)
        # A child that resembles the card it hangs off is linked, not a duplicate.
        kb.create_task(conn, title=p2 + " follow-up", body="w", assignee="daedalus",
                       parents=[a], parents_kind="derived-from", duplicate_guard=True)


def test_normal_card_passes_both_guards(kanban_home):
    with kb.connect_closing() as conn:
        kb.create_task(conn, title=MOMUS2_OPEN, body="pass", assignee="momus")
        parent = _parent(conn)
        tid = kb.create_task(conn, title="Rotate the Decodo proxy credentials on ACE-AI",
                             body="unrelated", assignee="daedalus", parents=[parent],
                             duplicate_guard=True, time_gate_guard=True)
        assert kb.near_duplicate_warning(conn, tid) is None


# --- both surfaces ----------------------------------------------------------------------

def test_cli_time_gate_refuses_then_until_and_override(kanban_home, tmp_path):
    with kb.connect_closing() as conn:
        parent = _parent(conn)
    body_file = tmp_path / "body.md"
    body_file.write_text(GATED_BODY)
    base = f"create {shlex.quote(GATED_TITLE)} --body-file {body_file} --assignee argus --parent {parent}"
    refused = kc.run_slash(base + " --json")
    assert "reads time-gated" in refused
    scheduled = json.loads(kc.run_slash(base + " --until +24h --json"))
    assert scheduled["status"] == "scheduled"
    forced = json.loads(kc.run_slash(base + " --i-mean-blocks --not-a-duplicate 'second arm' --json"))
    assert forced["status"] == "todo"
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, scheduled["id"]).next_eligible_at > time.time() + 23 * 3600
        assert len(_events(conn, forced["id"], "time_gate_forced")) == 1


def test_cli_open_duplicate_refuses_then_not_a_duplicate(kanban_home):
    first = json.loads(kc.run_slash(f"--board default create {shlex.quote(MOMUS2_OPEN)} --assignee momus --json"))
    refused = kc.run_slash(f"--board default create {shlex.quote(MOMUS2_REFILE)} --assignee momus --json")
    assert first["id"] in refused and "assignee momus" in refused
    forced = json.loads(kc.run_slash(
        f"--board default create {shlex.quote(MOMUS2_REFILE)} --assignee momus "
        "--not-a-duplicate 'pass 2 of the next fold' --json"))
    with kb.connect_closing() as conn:
        assert _events(conn, forced["id"], "near_duplicate_forced")[0]["reason"] == (
            "pass 2 of the next fold")


def test_tool_time_gate_and_open_duplicate(kanban_home):
    from tools import kanban_tools as kt

    with kb.connect_closing() as conn:
        parent = _parent(conn)
    args = {"title": GATED_TITLE, "body": GATED_BODY, "assignee": "argus", "parents": [parent]}
    refused = json.loads(kt._handle_create(dict(args)))
    assert "reads time-gated" in refused["error"]
    parked = json.loads(kt._handle_create({**args, "until": "+24h"}))
    assert parked["ok"] is True and parked["status"] == "scheduled"
    forced = json.loads(kt._handle_create(
        {**args, "i_mean_blocks": True, "not_a_duplicate": "second arm"}))
    assert forced["ok"] is True

    first = json.loads(kt._handle_create({"title": MOMUS1_OPEN, "assignee": "momus"}))
    dup = json.loads(kt._handle_create({"title": MOMUS1_REFILE, "assignee": "momus"}))
    assert first["task_id"] in dup["error"]
    ok = json.loads(kt._handle_create(
        {"title": MOMUS1_REFILE, "assignee": "momus", "not_a_duplicate": "new spec version"}))
    assert ok["ok"] is True
    with kb.connect_closing() as conn:
        assert len(_events(conn, forced["task_id"], "time_gate_forced")) == 1
        assert _events(conn, ok["task_id"], "near_duplicate_forced")[0]["reason"] == (
            "new spec version")


def kb_lint():
    from hermes_cli import kanban_mint_lint
    return kanban_mint_lint
