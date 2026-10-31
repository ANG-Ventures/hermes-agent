#!/usr/bin/env python3
"""Route-change notes E2E probe (RN-3, t_87e0d430; spec 2026-10-29 model-change-notes v3 §10).

The drain-probe rig (scripts/relay_drain_e2e_probe.py) pointed at the route-note path: a SCRATCH
claude-pool bpr relay (deployed relay code, its own port / cache-dir / log, ONE worker seat, the
test-only cap action on), a scratch HERMES_HOME, and four real turns through the agent. The live
relays, the live runtime tree and every gateway are never touched.

Each turn is its own process: a fresh AIAgent bound to a gateway ``SessionStore`` entry in the
scratch home through the real ``GatewayRunner._bind_route_known`` sink, so ``Known`` crosses turns
only through ``SessionEntry.route_known`` on disk (spec §3, E4), and the history replays from the
scratch state.db.

  step 0  baseline   default opus                       -> 0 notes (Known := {opus, opus})
  step 1  /model fable, fable capped on the seat        -> 1 combined note, served opus  (R2)
  step 2  next turn, fable still capped                 -> 0 notes, served opus          (R3)
  step 3  unblock (relay restarted, cap cleared)        -> 1 "restored" note, served fable (R4)

"/model fable" is the session override write the gateway's /model handler persists
(``SessionStore.set_model_override``); the turn builds its agent on it like a fresh gateway agent.
Fable is made unavailable with the relay's own test-only model cap (``/admin/pool/cap/<seat>``), the
exact mark a seat-level model limit takes, so the agent sees the real 503 body. Unblock = SIGTERM the
scratch relay, drop its scratch capmarks, relaunch. Scratch config: ``agent.capacity_retry_attempts:
0`` (no 90 s same-provider wait on the cap 503) and ``fallback.sticky_policy: false`` so step 2 really
re-tries fable (R3's "retries, fails, lands on the same fallback") and step 3 can restore.

A note counts when it rides the TAIL of a SUCCESSFUL outbound request (the payload the model saw).
Notes on failed rungs are dumped too: they are ephemeral by design (§5) and never committed.

Verdict (exit 0 = PASS, 1 = FAIL, 2 = setup error): note counts 0/1/0/1 on steps 0..3, the served
model per step as above, the expected note texts, transcript and audit log agree.

  python3 scripts/route_notes_e2e_probe.py --scratch [--out DIR] [--seat sub-vps-N] [--port P]
      [--pinned-model claude-sonnet-5]   # pin/cap another model while fable is capped fleet-wide

"fable" above stands for --pinned-model (default claude-fable-5-1); the expected note texts follow it.
The scratch relay counts as ready on /health status ok OR degraded with eligible_count >= 1.

Dumps under --out: report.json, step<N>.json (every outbound call: model, ok, tail notes, messages),
transcript.json (scratch state.db rows), model-route-changes.log, relay.log.
Registers nothing on a schedule: an operator instrument for route-note changes.
"""

from __future__ import annotations

import argparse
import atexit
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve()
REPO = HERE.parent.parent
HOME_ENV = "HERMES" + "_HOME"
FABLE = "claude-fable-5-1"
OPUS = "claude-opus-5-5"
DEFAULT_PORT = 18898
PROMPT = "Reply with exactly the text ROUTE-PROBE-OK and nothing else. Do not use tools."
NOTE_OPENER = "[Route note:"
SESSION_CHAT = "route-notes-e2e"


def model_label(model: str) -> str:
    """The short name the route note uses (agent.route_identity.model_label for claude-* ids)."""
    model = model.rsplit("/", 1)[-1]
    return model[len("claude-"):] if model.startswith("claude-") and len(model) > 7 else model


def steps_for(pinned: str) -> tuple:
    """step -> (session /model override, expected note count, expected served model, note must contain)."""
    return (
        (0, None, 0, OPUS, None),
        (1, pinned, 1, OPUS, f"the user selected {model_label(pinned)}"),
        (2, None, 0, OPUS, None),
        (3, None, 1, pinned, f"{model_label(pinned)} restored"),
    )


def _load_drain_rig():
    spec = importlib.util.spec_from_file_location("relay_drain_e2e_probe", HERE.parent / "relay_drain_e2e_probe.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def log(msg: str) -> None:
    print(f"[route-probe {time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ------------------------------------------------------------------------------------------------
# Orchestrator side (stdlib only)
# ------------------------------------------------------------------------------------------------
class CapRelay:
    """The drain rig's scratch relay, launched with ``--admin-test-actions`` (cap action on)."""

    def __init__(self, rig, port: int, workdir: Path, registry: Path):
        self.rig = rig
        self.inner = rig.ScratchRelay(port, workdir, registry)

    def start(self, wait_s: float = 60.0) -> None:
        r = self.inner
        if r.port in self.rig.REFUSED_PORTS:
            raise SystemExit(f"refusing production relay port {r.port}")
        if r.port_in_use():
            raise SystemExit(f"port {r.port} already has a listener; pick a free --port")
        r.launches += 1
        out = open(r.workdir / f"relay.stdout.{r.launches}.log", "ab")
        r.proc = subprocess.Popen(
            [str(self.rig.DEPLOY_RELAY_PY), str(self.rig.DEPLOY_RELAY), "--pool", "bpr", "--host", "127.0.0.1",
             "--port", str(r.port), "--registry", str(r.registry), "--cache-dir", str(r.cache),
             "--log", str(r.log_path), "--admin-test-actions"],
            cwd=str(self.rig.DEPLOY_RELAY.parent), env=r.env, stdout=out, stderr=subprocess.STDOUT,
        )
        r.wait_ready(out.name, wait_s)

    def eligible(self, model: str) -> list:
        return list(self.rig.http("GET", self.inner.base + f"/eligibility?model={model}").get("eligible") or [])

    def cap(self, seat: str, model: str, ttl_s: int) -> dict:
        r = self.inner
        if r.proc is None or r.proc.poll() is not None:
            raise SystemExit("cap: the scratch relay is not running")
        res = self.rig.http("POST", r.base + f"/admin/pool/cap/{seat}?model={model}&ttl_s={ttl_s}",
                            {"reason": "route-notes e2e (t_87e0d430)"})
        log(f"cap {seat} {model} -> {res}")
        if not res.get("ok"):
            raise SystemExit(f"cap refused: {res}")
        return res

    def uncap(self) -> None:
        """Unblock: stop, drop the scratch capmarks, relaunch (the relay has no uncap action)."""
        self.inner.stop()
        for name in ("capmarks.json",):
            p = self.inner.cache / name
            if p.exists():
                p.unlink()
                log(f"removed scratch {p.name}")
        self.start()

    def stop(self) -> None:
        self.inner.stop()


def pick_seat(rig, port: int, work: Path, pinned: str) -> str:
    """The first worker seat the relay calls eligible for BOTH models (never sub 0)."""
    d = json.loads(rig.PROD_REGISTRY.read_text(encoding="utf-8-sig"))
    d["subs"] = [s for s in d["subs"] if str(s.get("label", "")).split(" ")[0] not in rig.SUB0_KEYS
                 and "Mac Studio" not in str(s.get("label"))]
    reg = work / "registry-all.json"
    rig.write_private(reg, json.dumps(d))
    sel = work / "seat-select"
    sel.mkdir(mode=0o700)
    relay = CapRelay(rig, port, sel, reg)
    try:
        relay.start()
        elig = {m: relay.eligible(m) for m in (pinned, OPUS)}
    finally:
        relay.stop()
        reg.unlink()
    return choose_seat(elig, pinned)


def choose_seat(elig: dict, pinned: str) -> str:
    """First seat in ``elig[pinned]`` also in ``elig[OPUS]``; the refusal names the model(s) at fault."""
    both = [k for k in elig[pinned] if k in set(elig[OPUS])]
    if not both:
        empty = [m for m, keys in elig.items() if not keys]
        why = (f"no eligible seat for {', '.join(empty)}" if empty else
               f"{pinned} eligible on {elig[pinned]}, {OPUS} on {elig[OPUS]}, no seat in both")
        hint = " (try --pinned-model with an uncapped model)" if pinned in empty else ""
        raise SystemExit(f"no worker seat is eligible for both {pinned} and {OPUS}: {why}{hint}")
    log(f"seat eligible for {pinned}+{OPUS}: {both[0]} (of {both})")
    return both[0]


def state_db_rows(db: Path) -> list:
    import sqlite3
    if not db.exists():
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [{"id": i, "role": r, "content": c, "api_content": ac, "display_metadata": dm}
                for i, r, c, ac, dm in con.execute(
                    "select id, role, content, api_content, display_metadata from messages order by id")]
    finally:
        con.close()


def relay_turn_picks(events: list) -> list:
    """Models the relay routed for the turns' main calls, in order (aux calls carry no lane_inputs or
    a non-"-" aux_task). Must equal the payload models of the successful calls, so the PASS rests on
    what went on the wire and what the relay served, not on agent.model."""
    return [e.get("model") for e in events if e.get("event") == "pick"
            and (e.get("lane_inputs") or {}).get("aux_task") == "-"]


def turn_python(tree: Path) -> Path:
    for cand in (tree / ".venv/bin/python", tree / "venv/bin/python",
                 Path.home() / ".hermes/hermes-agent/venv/bin/python"):
        if cand.exists():
            return cand
    raise SystemExit("no interpreter for the tree (.venv, venv, ~/.hermes/hermes-agent/venv)")


def run_turn(py: Path, tree: Path, env: dict, work: Path, home: Path, step: int, override) -> dict:
    out = work / f"step{step}.json"
    cmd = [str(py), str(HERE), "--_turn", str(step), "--home", str(home), "--dump", str(out),
           "--runtime-tree", str(tree)]
    if override:
        cmd += ["--model-override", override]
    log(f"step {step}: turn start (override={override})")
    with open(work / f"step{step}.stdout.log", "wb") as so, open(work / f"step{step}.stderr.log", "wb") as se:
        rc = subprocess.run(cmd, cwd=str(work), env=env, stdin=subprocess.DEVNULL, stdout=so, stderr=se,
                            timeout=900).returncode
    if rc or not out.exists():
        tail = (work / f"step{step}.stderr.log").read_text(errors="replace")[-1500:]
        raise SystemExit(f"step {step}: turn process rc={rc}\n{tail}")
    return json.loads(out.read_text(encoding="utf-8"))


OUT_MARKER = ".route-notes-probe-out"
OUT_LOCK = ".route-notes-probe.lock"


def claim_out(out: Path | None, scratch_root: Path) -> Path:
    """Own the output dir for this run. Default: a fresh mkdtemp (atomically unique). An explicit
    --out is locked with an O_EXCL lock file holding our pid BEFORE anything in it is replaced, so a
    second probe on the same dir is refused (never wipes a running probe's state). Released at exit."""
    if out is None:
        work = Path(tempfile.mkdtemp(prefix="route-notes-e2e-", dir=scratch_root)).resolve()
    else:
        work = out.resolve()
        work.mkdir(parents=True, mode=0o700, exist_ok=True)
    lock = work / OUT_LOCK
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        owner = lock.read_text(encoding="utf-8", errors="replace").strip() or "?"
        raise SystemExit(f"--out {work} is locked by probe pid {owner} ({lock}); refusing. "
                         f"If that pid is dead, remove the lock file.")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(f"{os.getpid()}\n")
    atexit.register(lambda: lock.unlink(missing_ok=True))
    entries = [p for p in work.iterdir() if p.name != OUT_LOCK]
    if entries and not (work / OUT_MARKER).is_file():
        raise SystemExit(f"--out {work} is non-empty and not a previous probe output; refusing")
    for p in entries:
        shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()
    (work / OUT_MARKER).write_text("route_notes_e2e_probe output dir\n", encoding="utf-8")
    return work


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scratch", action="store_true", help="scratch relay + scratch home (the only mode)")
    ap.add_argument("--runtime-tree", type=Path, default=REPO, help="tree the turns import (default: this checkout)")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--seat", default=None, help="worker seat (default: first eligible for the pinned model AND opus)")
    ap.add_argument("--pinned-model", default=FABLE,
                    help=f"model /model pins in step 1 and the relay caps (default {FABLE}; any model but {OPUS})")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--_turn", type=int, default=None, help=argparse.SUPPRESS)
    ap.add_argument("--home", type=Path, default=None, help=argparse.SUPPRESS)
    ap.add_argument("--dump", type=Path, default=None, help=argparse.SUPPRESS)
    ap.add_argument("--model-override", default=None, help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a._turn is not None:
        return turn_main(a)
    if not a.scratch:
        ap.error("--scratch is required (the probe never runs against live relays or homes)")
    pinned = a.pinned_model
    if pinned == OPUS:
        ap.error(f"--pinned-model must differ from the fallback {OPUS}")
    steps_spec = steps_for(pinned)

    rig = _load_drain_rig()
    tree = a.runtime_tree.resolve()
    live = (Path.home() / ".hermes/runtime/hermes-agent").resolve()
    scratch_root = Path(os.environ.get("TMPDIR") or "/tmp")
    work = claim_out(a.out, scratch_root)
    log(f"tree={tree} (live runtime tree: {'YES, read-only' if tree == live else 'no'}) out={work} pinned={pinned}")

    seat = a.seat or pick_seat(rig, a.port, work, pinned)
    registry = work / "registry.json"
    rig.one_seat_registry(seat, registry)
    home = rig.scratch_home(work, a.port, OPUS, OPUS, None)
    # Fallback ladder: the pinned model -> opus. capacity wait off; sticky off so R3 re-tries.
    cfg = (home / "config.yaml").read_text(encoding="utf-8")
    cfg += "agent:\n  capacity_retry_attempts: 0\n  api_max_retries: 1\nfallback:\n  sticky_policy: false\n"
    (home / "config.yaml").write_text(cfg, encoding="utf-8")
    env = rig.agent_env(home, tree, a.port)
    py = turn_python(tree)

    relay = CapRelay(rig, a.port, work, registry)
    steps = []
    try:
        relay.start()
        for step, override, *_ in steps_spec:
            if step == 1:
                relay.cap(seat, pinned, 900)
            if step == 3:
                relay.uncap()
            steps.append(run_turn(py, tree, env, work, home, step, override))
    finally:
        relay.stop()

    events = rig.relay_events(relay.inner.log_path)
    audit = home / "state" / "model-route-changes.log"
    audit_all = audit.read_text(encoding="utf-8").splitlines() if audit.exists() else []
    if audit.exists():
        shutil.copy2(audit, work / "model-route-changes.log")
    # The log also carries the existing failover/recovery rows; the note's own rows are route_note.
    audit_rows = [r for r in audit_all if " route_note " in r]
    transcript = state_db_rows(home / "state.db")
    (work / "transcript.json").write_text(json.dumps(transcript, indent=2), encoding="utf-8")

    results, ok = [], True
    for (step, override, want_n, want_model, want_text), st in zip(steps_spec, steps):
        served = [c for c in st["calls"] if c["ok"]]
        notes = [n for c in served for n in c["tail_notes"]]
        failed_notes = [n for c in st["calls"] if not c["ok"] for n in c["tail_notes"]]
        last_model = served[-1]["model"] if served else None
        good = (len(notes) == want_n and last_model == want_model and st["reply_ok"]
                and (want_text is None or bool(notes and want_text in notes[0])))
        ok &= good
        results.append({"step": step, "override": override, "notes": notes, "note_count": len(notes),
                        "served_model": last_model, "calls": [(c["model"], c["ok"]) for c in st["calls"]],
                        "failed_rung_notes": failed_notes, "known_after": st["known_after"],
                        "reply": st["reply"][:120], "pass": good})
    # A note prepended to a user row lives in its api_content sidecar + route_note_prefix provenance
    # (what replays to the model); a standalone note row has it as content.
    note_rows = [m for m in transcript if m["role"] == "user"
                 and (NOTE_OPENER in str(m["api_content"] or "") or NOTE_OPENER in str(m["content"] or ""))]
    counts = "/".join(str(r["note_count"]) for r in results[1:])
    picked, served_seq = relay_turn_picks(events), [c["model"] for st in steps for c in st["calls"] if c["ok"]]
    ok &= picked == served_seq
    report = {
        "tree": str(tree), "pinned_model": pinned, "seat": seat, "port": a.port, "relay_launches": relay.inner.launches,
        "relay_models_picked": [e.get("model") for e in events if e.get("event") == "pick"],
        "relay_turn_picks": picked, "payload_served_sequence": served_seq, "relay_matches_payload": picked == served_seq,
        "relay_admin_caps": [e for e in events if e.get("event") == "admin_test_cap"],
        "steps": results, "note_counts_steps_1_2_3": counts, "audit_route_note_rows": audit_rows,
        "audit_all_rows": audit_all,
        "transcript_rows_with_notes": len(note_rows),
    }
    total_notes = sum(r["note_count"] for r in results)
    ok &= len(audit_rows) == total_notes and len(note_rows) == total_notes
    (work / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, default=str))
    log(f"{'PASS' if ok else 'FAIL'} note counts {counts} (step 0 baseline: {results[0]['note_count']}) "
        f"served {'/'.join(str(r['served_model']) for r in results)} dumps={work}")
    return 0 if ok else 1


# ------------------------------------------------------------------------------------------------
# Turn side (runs under the tree's interpreter, scratch HERMES_HOME)
# ------------------------------------------------------------------------------------------------
def _texts(payload: dict) -> list:
    out = []
    for m in payload.get("messages") or payload.get("input") or []:
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, list):
            c = "".join(p.get("text", "") for p in c if isinstance(p, dict))
        out.append(str(c or ""))
    return out


def _tail_notes(payload: dict) -> list:
    msgs = payload.get("messages") or payload.get("input") or []
    tail = _texts({"messages": msgs[-1:]})
    return [ln for t in tail for ln in t.split("\n\n") if ln.startswith(NOTE_OPENER)]


def turn_main(a) -> int:
    tree = a.runtime_tree.resolve()  # the tree under test, not the checkout holding this script
    sys.path.insert(0, str(tree))
    from gateway.config import GatewayConfig, Platform
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource, SessionStore
    from run_agent import AIAgent
    from hermes_cli.config import load_config
    from hermes_cli.fallback_config import get_fallback_chain
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from hermes_constants import resolve_reasoning_config
    from hermes_state import SessionDB

    import agent.route_identity as ri
    for mod in (ri, sys.modules["gateway.run"]):
        f = os.path.realpath(mod.__file__)
        if not f.startswith(os.path.realpath(str(tree)) + os.sep):
            raise SystemExit(f"module resolved OUTSIDE the tree: {f}")

    home = a.home
    store = SessionStore(sessions_dir=home / "sessions", config=GatewayConfig())
    source = SessionSource(platform=Platform.LOCAL, chat_id=SESSION_CHAT, user_id="probe", user_name="probe",
                           chat_type="dm")
    entry = store.get_or_create_session(source)
    key = entry.session_key
    if a.model_override:  # what the gateway /model handler persists for the session
        store.set_model_override(key, {"model": a.model_override, "provider": "claude-bpr"})
        entry = store._entries[key]
    cfg = load_config()
    model = (entry.model_override or {}).get("model") or cfg["model"]["default"]
    runtime = resolve_runtime_provider(requested="claude-bpr", target_model=model)
    db = SessionDB(db_path=home / "state.db")
    history = db.get_messages_as_conversation(entry.session_id) if a._turn else []
    # The pinned route falls back to the configured opus rung; a turn already on opus has none.
    chain = [fb for fb in (get_fallback_chain(cfg) or []) if fb.get("model") != model] or None
    agent = AIAgent(
        api_key=runtime.get("api_key"), base_url=runtime.get("base_url"), provider=runtime.get("provider"),
        api_mode=runtime.get("api_mode"), model=model, enabled_toolsets=[], quiet_mode=True, platform="cli",
        session_db=db, session_id=entry.session_id, fallback_model=chain,
        reasoning_config=resolve_reasoning_config(cfg, model), skip_context_files=True, skip_memory=True,
    )
    if agent.tools:
        raise SystemExit(f"probe turn would expose {len(agent.tools)} tools; refusing")
    runner = object.__new__(GatewayRunner)
    runner.session_store = store
    known_before = entry.route_known
    runner._bind_route_known(agent, key)

    calls = []

    def _recording(orig):
        def call(api_kwargs, *args, **kw):
            # The served model is what goes on the wire, not what the agent believes it is on.
            rec = {"model": api_kwargs.get("model"), "agent_model": agent.model, "provider": agent.provider,
                   "ok": False,
                   "tail_notes": _tail_notes(api_kwargs),
                   "messages": api_kwargs.get("messages") or api_kwargs.get("input"),
                   "system_chars": len(str(api_kwargs.get("system") or ""))}
            calls.append(rec)
            try:
                res = orig(api_kwargs, *args, **kw)
            except BaseException as e:
                rec["error"] = f"{type(e).__name__}: {str(e)[:300]}"
                raise
            rec["ok"] = True
            return res
        return call

    agent._interruptible_api_call = _recording(agent._interruptible_api_call)
    agent._interruptible_streaming_api_call = _recording(agent._interruptible_streaming_api_call)
    result = agent.run_conversation(PROMPT, conversation_history=history)
    reply = str(result.get("final_response") or "")
    reread = SessionStore(sessions_dir=home / "sessions", config=GatewayConfig())  # what a restart sees
    reread.get_or_create_session(source)
    dump = {
        "step": a._turn, "model_requested": model, "known_before": known_before,
        "known_after": reread._entries[key].route_known, "reply": reply, "reply_ok": "ROUTE-PROBE-OK" in reply,
        "calls": calls,
    }
    a.dump.write_text(json.dumps(dump, indent=2, default=str), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
