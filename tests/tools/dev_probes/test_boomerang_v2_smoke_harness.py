"""Boomerang v2 smoke harness (tools/dev_probes/boomerang_v2_smoke/run.py) on fakes.

Every step runs in --dry-run against the shipped fixtures; each assertion family is also shown
to FAIL on a fixture that breaks the contract (so a green run is not a matcher that always
matches). Step 1 additionally runs --live end to end against a fake kanban CLI that plays the
gateway (appends the notifier log line and the origin-chat line), so the live poll path and the
board guard are exercised without any real board, gateway or relay.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
HARNESS_DIR = REPO / "tools" / "dev_probes" / "boomerang_v2_smoke"
RUN_PY = HARNESS_DIR / "run.py"
FIXTURES = HARNESS_DIR / "fixtures"


def _load():
    spec = importlib.util.spec_from_file_location("bv2_smoke_run", RUN_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bv2_smoke_run"] = mod
    spec.loader.exec_module(mod)
    return mod


run = _load()

_BV2_VARS = ("BV2_KANBAN_CMD", "BV2_SCRATCH_BOARD", "BV2_HOME_SESSION", "BV2_ORIGIN_CHAT",
             "BV2_GATEWAY_LOG", "BV2_GATEWAY_ENV", "BV2_CONFIG", "BV2_WIRELOG_DIR", "BV2_ACK_FILE",
             "BV2_EVIDENCE", "BV2_PLAN", "BV2_GATEWAY_PID", "BV2_STOP_FILE")


@pytest.fixture
def env(tmp_path, monkeypatch):
    for var in _BV2_VARS:
        monkeypatch.delenv(var, raising=False)
    evidence = tmp_path / "smoke.md"
    monkeypatch.setenv("BV2_EVIDENCE", str(evidence))
    # A kanban CLI that must never run in dry-run: it leaves a marker if it does.
    marker = tmp_path / "kanban-was-called"
    tripwire = tmp_path / "tripwire.sh"
    tripwire.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n")
    tripwire.chmod(tripwire.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("BV2_KANBAN_CMD", str(tripwire))
    return {"tmp": tmp_path, "evidence": evidence, "marker": marker}


def test_help_exits_zero():
    proc = subprocess.run([sys.executable, str(RUN_PY), "--help"], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "--step" in proc.stdout and "--dry-run" in proc.stdout and "--live" in proc.stdout


@pytest.mark.parametrize("step", ["1", "2", "3", "4"])
def test_each_step_dry_run_passes_on_fixtures(env, step):
    assert run.main(["--step", step, "--dry-run"]) == 0
    text = env["evidence"].read_text()
    assert f"<!-- bv2-smoke:step{step}:begin -->" in text
    assert "- verdict: **PASS**" in text
    assert "### Commands" in text and "### Raw output" in text
    assert not env["marker"].exists(), "dry-run executed a kanban command"


def test_all_dry_run_writes_four_sections_and_rerun_replaces_them(env):
    assert run.main(["--step", "all", "--dry-run"]) == 0
    first = env["evidence"].read_text()
    assert run.main(["--step", "all", "--dry-run"]) == 0
    second = env["evidence"].read_text()
    for n in (1, 2, 3, 4):
        assert second.count(f"<!-- bv2-smoke:step{n}:begin -->") == 1
    assert second.count("- verdict: **PASS**") == 4
    assert len(second.splitlines()) == len(first.splitlines())
    # the printed commands target only the scratch board
    assert "--board bv2-smoke " in second and "dry-run: not executed" in second


def _copy_fixtures(tmp_path: Path) -> Path:
    dst = tmp_path / "fx"
    shutil.copytree(FIXTURES, dst)
    return dst


def test_step1_fails_when_needs_input_is_boomerang_woken(env, monkeypatch):
    fx = _copy_fixtures(env["tmp"])
    with open(fx / "fake_origin_chat.jsonl", "a") as fh:
        fh.write(json.dumps({"ts": 1792378999.0, "content": "[boomerang-wake] t_5m0ce002 - closed it"}) + "\n")
    monkeypatch.setenv("BV2_ORIGIN_CHAT", str(fx / "fake_origin_chat.jsonl"))
    assert run.main(["--step", "1", "--dry-run"]) == 1
    assert "needs_input card t_5m0ce002: no `[boomerang-wake]` line | FAIL" in env["evidence"].read_text()


def test_step1_fails_without_the_boomerang_wake_line(env, monkeypatch):
    fx = _copy_fixtures(env["tmp"])
    chat = fx / "fake_origin_chat.jsonl"
    lines = chat.read_text().splitlines()
    kept = [ln for ln in lines if not json.loads(ln)["content"].startswith("[boomerang-wake] t_5m0ce001")]
    assert len(kept) == len(lines) - 1
    chat.write_text("".join(ln + "\n" for ln in kept))
    monkeypatch.setenv("BV2_ORIGIN_CHAT", str(chat))
    assert run.main(["--step", "1", "--dry-run"]) == 1


def test_step2_fails_when_switch_flip_is_not_visible(env, monkeypatch):
    fx = _copy_fixtures(env["tmp"])
    # stage1 still says fork_enabled: true -> the flip Apollo acked never reached config
    (fx / "fake_config" / "stage1.yaml").write_text((fx / "fake_config" / "stage0.yaml").read_text())
    monkeypatch.setenv("BV2_CONFIG", str(fx / "fake_config"))
    assert run.main(["--step", "2", "--dry-run"]) == 1


def test_step2_fails_when_the_stop_file_is_not_visible(env, monkeypatch):
    fx = _copy_fixtures(env["tmp"])
    # Apollo acked `boomerang_wake=disabled` but the stop file never appeared
    (fx / "fake_stop" / "stage2.disabled").unlink()
    monkeypatch.setenv("BV2_STOP_FILE", str(fx / "fake_stop"))
    assert run.main(["--step", "2", "--dry-run"]) == 1
    assert "stop file boomerang-wake.disabled present after ack | FAIL" in env["evidence"].read_text()


def test_step2_fails_without_ack(env, monkeypatch):
    ack = env["tmp"] / "empty.ack"
    ack.write_text("")
    monkeypatch.setenv("BV2_ACK_FILE", str(ack))
    assert run.main(["--step", "2", "--dry-run", "--ack-timeout", "0"]) == 1


def test_step3_fails_on_x_hermes_header_upstream(env, monkeypatch):
    fx = _copy_fixtures(env["tmp"])
    path = fx / "fake_wirelog" / "apx" / "2026-10-18.jsonl"
    rec = json.loads(path.read_text().splitlines()[0])
    rec["id"] = "leaky"
    rec["req"]["headers"].append(["x-hermes-pin-sub", "local"])
    with open(path, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    monkeypatch.setenv("BV2_WIRELOG_DIR", str(fx / "fake_wirelog"))
    assert run.main(["--step", "3", "--dry-run"]) == 1
    assert "leaky: x-hermes-pin-sub" in env["evidence"].read_text()


def test_step3_no_capture_is_incomplete_not_pass(env, monkeypatch):
    empty = env["tmp"] / "nowire"
    empty.mkdir()
    monkeypatch.setenv("BV2_WIRELOG_DIR", str(empty))
    assert run.main(["--step", "3", "--dry-run"]) == 3


def test_step4_fails_without_resurface(env, monkeypatch):
    fx = _copy_fixtures(env["tmp"])
    log = fx / "fake_gateway.log"
    log.write_text("".join(ln + "\n" for ln in log.read_text().splitlines() if "re-surfaced" not in ln))
    monkeypatch.setenv("BV2_GATEWAY_LOG", str(log))
    assert run.main(["--step", "4", "--dry-run"]) == 1


def test_step4_ignores_a_boot_line_from_before_the_dispatch(env, monkeypatch):
    fx = _copy_fixtures(env["tmp"])
    log = fx / "fake_gateway.log"
    keep = [ln for ln in log.read_text().splitlines()
            if not (ln.startswith("2026-10-18 20:16:00") and "Gateway running" in ln)]
    log.write_text("\n".join(keep) + "\n")  # only the 20:00 boot line, which predates the dispatch
    monkeypatch.setenv("BV2_GATEWAY_LOG", str(log))
    assert run.main(["--step", "4", "--dry-run"]) == 1
    assert "gateway restart observed in the log | FAIL" in env["evidence"].read_text()


def test_refuses_non_scratch_board(env, monkeypatch, capsys):
    monkeypatch.setenv("BV2_SCRATCH_BOARD", "default")
    assert run.main(["--step", "1", "--dry-run"]) == 2
    assert "refusing board" in capsys.readouterr().err


def test_live_requires_home_session_and_origin_chat(env, monkeypatch):
    assert run.main(["--step", "1", "--live"]) == 2
    monkeypatch.setenv("BV2_HOME_SESSION", "s")
    assert run.main(["--step", "1", "--live"]) == 2


def test_contract_line_matchers_accept_both_spellings():
    card = "t_abc12345"
    assert run.re_boomerang_wake_line(card).search(f"[boomerang-wake] {card} \u2014 Landed #12")
    assert run.re_boomerang_wake_line(card).search(f"[boomerang-wake] {card} - Landed #12")
    assert not run.re_boomerang_wake_line(card).search(f"[boomerang-wake] {card}9 - x")
    assert run.re_failed_line(card).search(f"\u26a0\ufe0f boomerang-wake failed for {card}: boom")
    assert run.re_failed_line(card).search(f"warning boomerang-wake failed {card}: boom")
    m = run.re_notifier_line(card).search(f"x kanban notifier: boomerang-wake {card} disposition=surface")
    assert m and m.group("disp") == "surface"


FAKE_GATEWAY_KANBAN = r"""#!/usr/bin/env python3
# Fake kanban CLI that also plays the gateway: `complete` emits the contract log + chat lines.
import json, os, sys, time
argv = sys.argv[1:]
state = os.environ["FAKE_STATE"]
with open(os.path.join(state, "calls.jsonl"), "a") as fh:
    fh.write(json.dumps(argv) + "\n")
board = argv[argv.index("--board") + 1] if "--board" in argv else None
if board is not None and not board.startswith("bv2-smoke"):
    sys.exit(9)
verb = next((a for a in argv if a in {"boards", "create", "notify-subscribe", "edit", "complete", "block"}), None)
stamp = time.strftime("%Y-%m-%d %H:%M:%S") + ",000"
if verb == "create":
    n = len(os.listdir(os.path.join(state, "cards")))
    card = "t_fa4e%04d" % n
    open(os.path.join(state, "cards", card), "w").close()
    print(json.dumps({"id": card, "status": "todo"}))
elif verb == "complete":
    card = argv[argv.index("complete") + 1]
    with open(os.environ["BV2_GATEWAY_LOG"], "a") as fh:
        fh.write(f"{stamp} INFO gateway.kanban_watchers: kanban notifier: boomerang-wake {card} disposition=auto\n")
    with open(os.environ["BV2_ORIGIN_CHAT"], "a") as fh:
        fh.write(json.dumps({"ts": time.time(), "content": f"[boomerang-wake] {card} \u2014 verified"}) + "\n")
elif verb == "block":
    card = argv[argv.index("block") + 1]
    with open(os.environ["BV2_GATEWAY_LOG"], "a") as fh:
        fh.write(f"{stamp} INFO gateway.kanban_watchers: kanban notifier: delivered blocked for {card}\n")
"""


def test_step1_live_against_fake_gateway_kanban(env, monkeypatch):
    tmp = env["tmp"]
    state = tmp / "state"
    (state / "cards").mkdir(parents=True)
    fake = tmp / "fake_kanban.py"
    fake.write_text(FAKE_GATEWAY_KANBAN)
    log = tmp / "gateway.log"
    log.write_text("2026-10-18 19:00:00,000 INFO kanban notifier: boomerang-wake t_fa4e0001 disposition=auto\n")
    chat = tmp / "chat.jsonl"
    chat.write_text("")
    plan = json.loads((FIXTURES / "plan.json").read_text())
    plan["commands"]["inject_failure"] = None
    plan_path = tmp / "plan.json"
    plan_path.write_text(json.dumps(plan))
    for k, v in {"BV2_KANBAN_CMD": f"{sys.executable} {fake}", "FAKE_STATE": str(state),
                 "BV2_GATEWAY_LOG": str(log), "BV2_ORIGIN_CHAT": str(chat), "BV2_PLAN": str(plan_path),
                 "BV2_HOME_SESSION": "sess-fake", "BV2_SCRATCH_BOARD": "bv2-smoke-test"}.items():
        monkeypatch.setenv(k, v)

    rc = run.main(["--step", "1", "--live", "--tick-seconds", "0.05", "--slack-seconds", "0.1",
                   "--timeout", "5"])
    text = env["evidence"].read_text()
    # no fault hook configured -> the failure-line check is SKIP, so the step is INCOMPLETE (3), not PASS
    assert rc == 3, text
    assert "| SKIP |" in text and "| FAIL |" not in text
    calls = [json.loads(ln) for ln in (state / "calls.jsonl").read_text().splitlines()]
    verbs = [c[2] if c[0] == "--board" else c[0] for c in calls]
    assert verbs.count("create") == 2 and "complete" in verbs and "block" in verbs
    assert all("bv2-smoke-test" in c for c in calls)
    block = next(c for c in calls if "block" in c)
    assert block[block.index("--kind") + 1] == "needs_input"
    # the stale pre-run log line for t_fa4e0001 was not counted: live reads only new log bytes
    assert "kanban notifier: boomerang-wake t_fa4e0000 disposition=auto" in text


# --- Prism round 1 regressions (#2102 @7e312ccc) -------------------------------------------------

def test_step3_unrelated_seat_traffic_is_not_coverage(env, monkeypatch):
    """Prism 7555e6381e68: a clean record from ANOTHER seat must not stand in for the fork request."""
    fx = _copy_fixtures(env["tmp"])
    log = fx / "fake_gateway.log"
    log.write_text(log.read_text().replace("served_by=local", "served_by=sub-vps-7"))
    monkeypatch.setenv("BV2_GATEWAY_LOG", str(log))
    monkeypatch.setenv("BV2_WIRELOG_DIR", str(fx / "fake_wirelog"))   # only a local (sub 0) record
    assert run.main(["--step", "3", "--dry-run"]) == 3                 # INCOMPLETE, never PASS
    assert "attributable to seat `sub-vps-7`" in env["evidence"].read_text()


def test_seat_aliases_normalize():
    assert run._norm_seat(0) == run._norm_seat("local") == run._norm_seat("sub-0") == "local"
    assert run._norm_seat("sub-vps-7") == run._norm_seat("sub-7") == run._norm_seat(7) == "sub-7"
    assert run._norm_seat(5) == "sub-5" and run._norm_seat("sub-vps-5") != run._norm_seat("sub-vps-7")
    assert run._rec_seat({"sub": 0}) == "local" and run._rec_seat({}) is None
    # live shape: relay record `sub: "16"` belongs to the seat the log names `sub-vps-16`
    assert run._rec_seat({"sub": "16", "host": "sub-vps-16"}) == run._norm_seat("sub-vps-16")


def test_failed_forcing_transition_aborts_not_passes(env, monkeypatch):
    """Prism 7d1bf9b6fa33: a `complete` that exits 1 generates no wake; the step must abort."""
    ctx = run.Ctx.__new__(run.Ctx)
    ctx.live = True
    ctx.kanban_cmd = lambda rep, key, **v: (1, "")
    with pytest.raises(run.StepAbort, match="forcing transition `complete` failed"):
        run.Ctx.force(ctx, None, "complete", card="t_x")


def test_step4_late_completion_is_watched_for_a_full_window(env, monkeypatch):
    """Prism b2a4a7e824f7: the negative check polls for the tick window, it does not read once."""
    seen = []
    class _C:
        live = True
        tick_window = 7.0
        def poll(self, fn, window):
            seen.append(window)
            return fn()
    src = (HARNESS_DIR / "run.py").read_text()
    i = src.index("no late `[boomerang-wake]")
    assert "ctx.poll(lambda: find_chat(chat, re_boomerang_wake_line(card)), ctx.tick_window)" in src[i - 400:i]


# --- t_480d08ce: AC-9 positive control (canary) + AC-7 live re-surface fault -----------------------

def _wire(fx: Path) -> Path:
    return fx / "fake_wirelog" / "apx" / "2026-10-18.jsonl"


def _drop_canary(path: Path) -> None:
    kept = [ln for ln in path.read_text().splitlines() if "lane_src" not in json.loads(ln)]
    path.write_text("".join(ln + "\n" for ln in kept))


def test_step3_canary_seen_and_no_leak_is_pass(env):
    assert run.main(["--step", "3", "--dry-run"]) == 0
    text = env["evidence"].read_text()
    assert "positive control: canary `bv2canary0123456789ab` recorded" in text
    assert "fake-canary-1" in text and "| PASS |" in text


def test_step3_canary_absent_is_incomplete_not_pass(env, monkeypatch):
    """No canary = the capture's view of fleet headers is unproven: a clean scan is blind."""
    fx = _copy_fixtures(env["tmp"])
    _drop_canary(_wire(fx))
    monkeypatch.setenv("BV2_WIRELOG_DIR", str(fx / "fake_wirelog"))
    assert run.main(["--step", "3", "--dry-run"]) == 3
    text = env["evidence"].read_text()
    assert "- verdict: **INCOMPLETE**" in text
    assert "recorded (fleet header reached the capture) | INCOMPLETE |" in text
    # the fork record itself is still clean: the INCOMPLETE comes from the missing control alone
    assert "no `x-hermes-*` header in any upstream request | PASS |" in text


def test_step3_canary_present_with_leak_is_fail(env, monkeypatch):
    fx = _copy_fixtures(env["tmp"])
    path = _wire(fx)
    rec = json.loads(path.read_text().splitlines()[0])
    rec["id"] = "leaky"
    rec["req"]["headers"].append(["x-hermes-session", "s"])
    with open(path, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    monkeypatch.setenv("BV2_WIRELOG_DIR", str(fx / "fake_wirelog"))
    assert run.main(["--step", "3", "--dry-run"]) == 1
    text = env["evidence"].read_text()
    assert "recorded (fleet header reached the capture) | PASS |" in text
    assert "leaky: x-hermes-session" in text


def test_step3_canary_record_carrying_a_fleet_header_is_fail(env, monkeypatch):
    """The canary's own upstream header set is scanned: a lane that let it through is a leak."""
    fx = _copy_fixtures(env["tmp"])
    path = _wire(fx)
    lines = path.read_text().splitlines()
    out = []
    for ln in lines:
        rec = json.loads(ln)
        if "lane_src" in rec:
            rec["req"]["headers"].append(["x-hermes-qa-canary", "bv2canary0123456789ab"])
        out.append(json.dumps(rec))
    path.write_text("\n".join(out) + "\n")
    monkeypatch.setenv("BV2_WIRELOG_DIR", str(fx / "fake_wirelog"))
    assert run.main(["--step", "3", "--dry-run"]) == 1
    assert "fake-canary-1: x-hermes-qa-canary" in env["evidence"].read_text()


def test_step1_fault_asserts_warning_and_resurfaced_wake(env):
    assert run.main(["--step", "1", "--dry-run"]) == 0
    text = env["evidence"].read_text()
    assert "warning reason is the injected fault (/state moved/) | PASS |" in text
    assert "home session: re-delivered wake turn for t_5m0ce003 | PASS |" in text
    assert "msg='[kanban] Task t_5m0ce003 completed." in text
    assert "no `no live home for t_5m0ce003` line | PASS |" in text


def test_step1_fault_without_resurfaced_wake_fails(env, monkeypatch):
    """The old live result: warning posted, home not live, wake not re-surfaced -> FAIL now."""
    fx = _copy_fixtures(env["tmp"])
    log = fx / "fake_gateway.log"
    keep = [ln for ln in log.read_text().splitlines() if "[kanban] Task t_5m0ce003" not in ln]
    keep.append("2026-10-18 20:01:31,000 WARNING gateway.kanban_boomerang_wake: kanban boomerang-wake: "
                "no live home for t_5m0ce003; wake not re-surfaced")
    log.write_text("\n".join(keep) + "\n")
    monkeypatch.setenv("BV2_GATEWAY_LOG", str(log))
    assert run.main(["--step", "1", "--dry-run"]) == 1
    text = env["evidence"].read_text()
    assert "home session: re-delivered wake turn for t_5m0ce003 | FAIL |" in text
    assert "no `no live home for t_5m0ce003` line | FAIL |" in text


def test_step1_fault_with_a_different_reason_fails(env, monkeypatch):
    """A warning from some other failure (e.g. the old rehome fault) is not the injected one."""
    fx = _copy_fixtures(env["tmp"])
    chat = fx / "fake_origin_chat.jsonl"
    chat.write_text(chat.read_text().replace("write refused: state moved",
                                             "child failed: dispatch failed: home session not live here"))
    monkeypatch.setenv("BV2_ORIGIN_CHAT", str(chat))
    assert run.main(["--step", "1", "--dry-run"]) == 1
    assert "warning reason is the injected fault (/state moved/) | FAIL |" in env["evidence"].read_text()


FAKE_FAULT_KANBAN = r"""#!/usr/bin/env python3
# Fake kanban CLI + gateway for the AC-7 fault: the failure card's `complete` dispatches only;
# the `comment` (inject_failure) refuses its verdict: warning line + re-delivered home wake.
import json, os, sys, time
argv = sys.argv[1:]
state = os.environ["FAKE_STATE"]
with open(os.path.join(state, "calls.jsonl"), "a") as fh:
    fh.write(json.dumps(argv) + "\n")
verbs = {"boards", "create", "notify-subscribe", "edit", "complete", "block", "comment"}
verb = next((a for a in argv if a in verbs), None)
stamp = time.strftime("%Y-%m-%d %H:%M:%S") + ",000"
log, chat = os.environ["BV2_GATEWAY_LOG"], os.environ["BV2_ORIGIN_CHAT"]
def logline(s):
    with open(log, "a") as fh:
        fh.write(f"{stamp} INFO {s}\n")
def say(s):
    with open(chat, "a") as fh:
        fh.write(json.dumps({"ts": time.time(), "content": s}) + "\n")
if verb == "create":
    title = argv[argv.index("create") + 1]
    n = len(os.listdir(os.path.join(state, "cards")))
    card = "t_fa4e%04d" % n
    with open(os.path.join(state, "cards", card), "w") as fh:
        fh.write(title)
    print(json.dumps({"id": card}))
elif verb == "complete":
    card = argv[argv.index("complete") + 1]
    title = open(os.path.join(state, "cards", card)).read()
    logline(f"gateway.kanban_boomerang_wake: kanban notifier: boomerang-wake {card} disposition=auto")
    if "forced child failure" not in title:
        say(f"[boomerang-wake] {card} \u2014 verified")
elif verb == "comment":
    card = argv[argv.index("comment") + 1]
    say(f"\u26a0\ufe0f boomerang-wake failed {card}: write refused: state moved")
    logline(f"gateway.run: inbound message: platform=discord user=u chat=c msg='[kanban] Task {card} completed. "
            f"Title: x' reply_to_id=None reply_to_text=''")
"""


def test_step1_live_fault_against_fake_gateway(env, monkeypatch):
    """Live path: the fault runs only after the dispatch line, and both INV-6 halves are read."""
    tmp = env["tmp"]
    state = tmp / "state"
    (state / "cards").mkdir(parents=True)
    fake = tmp / "fake_kanban.py"
    fake.write_text(FAKE_FAULT_KANBAN)
    log, chat = tmp / "gateway.log", tmp / "chat.jsonl"
    log.write_text("")
    chat.write_text("")
    for k, v in {"BV2_KANBAN_CMD": f"{sys.executable} {fake}", "FAKE_STATE": str(state),
                 "BV2_GATEWAY_LOG": str(log), "BV2_ORIGIN_CHAT": str(chat),
                 "BV2_HOME_SESSION": "sess-fake", "BV2_SCRATCH_BOARD": "bv2-smoke-test"}.items():
        monkeypatch.setenv(k, v)
    rc = run.main(["--step", "1", "--live", "--tick-seconds", "0.05", "--slack-seconds", "0.1",
                   "--timeout", "5"])
    text = env["evidence"].read_text()
    assert rc == 0, text
    calls = [json.loads(ln) for ln in (state / "calls.jsonl").read_text().splitlines()]
    verbs = [next(a for a in c if a in {"boards", "create", "notify-subscribe", "edit", "complete",
                                        "block", "comment"}) for c in calls]
    fail_card = "t_fa4e0002"
    i_complete = next(i for i, c in enumerate(calls) if "complete" in c and fail_card in c)
    i_comment = verbs.index("comment")
    assert calls[i_comment][calls[i_comment].index("comment") + 1] == fail_card
    assert i_complete < i_comment  # the fault lands after the dispatch, never before
    assert f"home session: re-delivered wake turn for {fail_card} | PASS |" in text




# --- BV2_ACK_CMD: scripted operator for an isolated scratch gateway (t_b901c171) -----------------

def _ack_ctx(tmp, ack_timeout=5.0):
    ctx = run.Ctx.__new__(run.Ctx)
    ctx.live = True
    ctx.ack = tmp / "scripted.ack"
    ctx.tick = 0.05
    ctx.args = type("A", (), {"ack_timeout": ack_timeout})()
    return ctx


def test_ack_cmd_runs_the_action_and_appends_the_token(env, monkeypatch):
    tmp = env["tmp"]
    acker = tmp / "acker.sh"
    acker.write_text(f"#!/bin/sh\necho \"$1\" >> {tmp / 'acted'}\nexit 0\n")
    acker.chmod(acker.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("BV2_ACK_CMD", str(acker))
    ctx = _ack_ctx(tmp)
    rep = run.StepReport(2, "t", "live")
    assert run.Ctx.wait_ack(ctx, rep, "fork_enabled=false", "flip it")
    assert (tmp / "acted").read_text().strip() == "fork_enabled=false"      # the action ran
    assert ctx.ack.read_text().splitlines() == ["fork_enabled=false"]       # the token was appended
    assert [c.result for c in rep.checks] == [run.PASS]
    assert any("scripted ack, exit 0" in c for c in rep.commands)


def test_ack_cmd_nonzero_exit_is_a_fail_not_a_pass(env, monkeypatch):
    tmp = env["tmp"]
    acker = tmp / "acker.sh"
    acker.write_text("#!/bin/sh\necho boom >&2\nexit 3\n")
    acker.chmod(acker.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("BV2_ACK_CMD", str(acker))
    ctx = _ack_ctx(tmp)
    rep = run.StepReport(4, "t", "live")
    assert run.Ctx.wait_ack(ctx, rep, "restart", "restart it") is None
    assert not ctx.ack.exists()                                             # no token on failure
    assert [c.result for c in rep.checks] == [run.FAIL]
    assert "exit 3" in rep.checks[0].detail and "boom" in rep.checks[0].detail


# --- Prism ae1cccf47944 (#2168 @1b5ae360): a failed fork-seat sync must not certify step 3 ---------

def _step3_live_remote_seat(env, monkeypatch, sync_body):
    """Live step 3 on remote seat sub-vps-9 (the finding's repro): the canary and one clean post-start
    `sub: 9` record are already in the capture root; only `wirelog_sync` varies. Nothing real is
    contacted: send_canary is faked and the sync command is a local script."""
    tmp = env["tmp"]
    wire = tmp / "wire" / "apx" / "today.jsonl"
    wire.parent.mkdir(parents=True)
    log = tmp / "gateway.log"
    log.write_text("")
    calls = tmp / "sync-calls"
    plan = json.loads((FIXTURES / "plan.json").read_text())
    if sync_body is None:
        plan["commands"].pop("wirelog_sync", None)
    else:
        sync = tmp / "sync.sh"
        # 1st call = the canary's seat (succeeds); 2nd call = the fork's seat (the variant under test)
        sync.write_text(f"#!/bin/sh\necho x >> {calls}\n[ \"$(wc -l < {calls})\" -le 1 ] && exit 0\n{sync_body}\n")
        sync.chmod(sync.stat().st_mode | stat.S_IEXEC)
        plan["commands"]["wirelog_sync"] = [str(sync), "{sub}"]
    plan_path = tmp / "plan.json"
    plan_path.write_text(json.dumps(plan))
    token = "bv2canaryfeedfacecafe"
    hdrs = [["content-type", "application/json"], ["anthropic-version", "2023-06-01"]]

    def fake_send(url, tok, model, timeout=120.0):
        ts = run._dt.datetime.now(run._dt.timezone.utc).isoformat()
        with open(wire, "a") as fh:
            fh.write(json.dumps({"id": "canary", "sub": "9", "ts": ts, "req": {"headers": hdrs},
                                 "lane_src": {"platform": tok}}) + "\n")
            fh.write(json.dumps({"id": "unrelated-clean", "sub": "9", "ts": ts, "req": {"headers": hdrs}}) + "\n")
        with open(log, "a") as fh:   # the fork turn is logged after the harness started reading
            fh.write(run.time.strftime("%Y-%m-%d %H:%M:%S") + ",000 INFO tools.delegate_tool: "
                     "boomerang.fork served_by=sub-vps-9 read_share=1\n")
        return 200, "sub-vps-9", "ok"

    monkeypatch.setattr(run, "send_canary", fake_send)
    monkeypatch.setattr(run, "canary_token", lambda live, plan: token)
    for k, v in {"BV2_PLAN": str(plan_path), "BV2_GATEWAY_LOG": str(log),
                 "BV2_WIRELOG_DIR": str(tmp / "wire"), "BV2_HOME_SESSION": "sess-fake",
                 "BV2_ORIGIN_CHAT": str(tmp / "chat.jsonl"), "BV2_SYNC_TIMEOUT": "1"}.items():
        monkeypatch.setenv(k, v)
    rc = run.main(["--step", "3", "--live", "--no-trigger", "--tick-seconds", "0.05",
                   "--slack-seconds", "0", "--timeout", "3"])
    return rc, env["evidence"].read_text()


def test_step3_fork_seat_sync_ok_still_passes(env, monkeypatch):
    rc, text = _step3_live_remote_seat(env, monkeypatch, "exit 0")
    assert rc == 0, text
    assert "fork seat `sub-vps-9` capture pulled after the fork turn | PASS |" in text


@pytest.mark.parametrize("body,why", [("sleep 5", "exit 124"), ("exit 2", "exit 2")])
def test_step3_failed_fork_seat_sync_is_not_pass(env, monkeypatch, body, why):
    """A timed-out or failing pull may have dropped the fork request; unrelated clean `sub: 9`
    traffic plus the canary must not add up to a PASS."""
    rc, text = _step3_live_remote_seat(env, monkeypatch, body)
    assert rc == 3, text                                   # INCOMPLETE, never PASS
    assert "- verdict: **INCOMPLETE**" in text
    assert "fork seat `sub-vps-9` capture pulled after the fork turn | INCOMPLETE |" in text
    assert why in text


def test_failed_notify_subscribe_aborts_not_passes():
    """Same class, make_card: a subscription that exits non-zero means no wake is ever delivered,
    so step 1's negative checks would pass vacuously; abort like set_boomerang_wake does."""
    ctx = run.Ctx.__new__(run.Ctx)
    ctx.live = True
    ctx.board_ready = True
    ctx.kanban_cmd = lambda rep, key, **v: (0, '{"id": "t_abc12345"}') if key == "create" else (
        (1, "") if key == "notify_subscribe" else (0, ""))
    rep = run.StepReport(1, "t", "live")
    with pytest.raises(run.StepAbort, match="notify_subscribe failed for t_abc12345"):
        run.Ctx.make_card(ctx, rep, "s1", "title")


def test_step3_remote_fork_seat_without_sync_command_is_not_pass(env, monkeypatch):
    rc, text = _step3_live_remote_seat(env, monkeypatch, None)
    assert rc == 3, text
    assert "no plan.commands.wirelog_sync for remote seat `sub-9`" in text
