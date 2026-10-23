#!/usr/bin/env python3
"""Payload for scripts/e2e-internal-silence.sh: three arms through a live scratch gateway.

Runs INSIDE puppet-lane.sh (env: PUPPET_PY, PUPPET_CH, PUPPET_REASON, PUPPET_LANE_HOME,
PUPPET_HEAD_SHA, PUPPET_THREADS). Each arm gets its own thread (= its own session). The puppet
asks the agent to start a background process with notify_on_complete and to answer the
COMPLETION turn (MessageEvent internal=True, the path that swallowed replies, t_bb4059d2) in a
given shape. The verdict reads the scratch state.db for the moment the internal turn landed and
Discord for what the bot posted after it.

    arm=long  head=<sha> posted=<msg id|none> marker_leaked=<y|n> verdict=PASS|FAIL
"""
from __future__ import annotations

import datetime as _dt
import importlib.util
import os
import secrets
import sqlite3
import sys
import time
from dataclasses import dataclass

SILENCE_TOKEN = "NO_REPLY"
LONG_WAIT_S = 120.0
QUIET_WINDOW_S = 60.0
# The long reply is judged once its Discord content has not changed for this long.
EDIT_SETTLE_S = 20.0
COMPLETION_WAIT_S = 240.0
# Long enough that the launching turn has finished before the completion lands, so the completion
# is its own internal turn (not a queued follow-up of a still-running human turn).
SLEEP_S = 20


@dataclass(frozen=True)
class Posted:
    id: str
    ts: float
    content: str


def arm_verdict(arm: str, nonce: str, after_completion: list[Posted]) -> tuple[str, bool, bool]:
    """``(posted_id|"none", marker_leaked, passed)`` from the bot's messages after the completion."""
    if arm == "long":
        hit = next((m for m in after_completion if f"E2E-LONG-{nonce}" in m.content), None)
        if hit is None:
            return "none", False, False
        leaked = SILENCE_TOKEN in hit.content
        return hit.id, leaked, not leaked
    first = after_completion[0] if after_completion else None
    if first is None:
        return "none", False, True
    return first.id, SILENCE_TOKEN in first.content, False


def prompt_for(arm: str, nonce: str) -> str:
    start = (f"Start this as a background process with notify_on_complete=true: "
             f"`sleep {SLEEP_S}; echo e2e-done`. Right now reply with only the word: started. ")
    if arm == "long":
        return start + (
            "When the completion notification arrives, reply with a three-sentence summary of the "
            f"process result that contains the literal token E2E-LONG-{nonce}, and end that reply "
            f"with {SILENCE_TOKEN} on its own line.")
    if arm == "short":
        return start + ("When the completion notification arrives, reply with exactly two lines: "
                        f"'Already handled.' then {SILENCE_TOKEN}.")
    return start + f"When the completion notification arrives, reply with only {SILENCE_TOKEN}."


def _iso_ts(s: str) -> float:
    return _dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _load_puppet(path: str):
    spec = importlib.util.spec_from_file_location("puppet", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _db_rows(home: str, sql: str, args: tuple) -> list[tuple]:
    path = os.path.join(home, "state.db")
    if not os.path.exists(path):
        return []
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        return con.execute(sql, args).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()


def _wait_completion(home: str, since: float) -> tuple[float | None, str]:
    """``(ts, "")`` of the internal (completion) user row persisted after ``since``, or
    ``(None, reason)``: a failed launching turn (provider down, model capped) ends the wait at once."""
    deadline = time.time() + COMPLETION_WAIT_S
    while time.time() < deadline:
        rows = _db_rows(home, "select min(timestamp) from messages where role='user' "
                              "and display_kind='internal_notification' and timestamp > ?", (since,))
        if rows and rows[0][0]:
            return float(rows[0][0]), ""
        failed = _db_rows(home, "select count(*) from messages where role='assistant' "
                                "and display_kind='failed_turn' and timestamp > ?", (since,))
        if failed and failed[0][0]:
            return None, "launch_turn_failed(see scratch errors.log: provider/model)"
        time.sleep(3)
    return None, f"no_completion_turn_in_{COMPLETION_WAIT_S:.0f}s"


def _wait_turn_end(home: str, since: float) -> bool:
    """True once the completion turn finished (a ``stop`` assistant row after ``since``); False on
    timeout, so a stalled or crashed turn is never read as successful silence (Prism 7f089cac187d)."""
    deadline = time.time() + COMPLETION_WAIT_S
    while time.time() < deadline:
        rows = _db_rows(home, "select count(*) from messages where role='assistant' "
                              "and finish_reason='stop' and timestamp > ?", (since,))
        if rows and rows[0][0]:
            return True
        time.sleep(3)
    return False


def settled_messages(fetch, settle_s: float = EDIT_SETTLE_S, limit_s: float = LONG_WAIT_S) -> list[Posted]:
    """The bot's messages once no post or edit has changed them for ``settle_s``.

    Called after the completion turn finished: a streamed reply can still gain its trailing
    NO_REPLY in a later edit, so the verdict reads the FINAL content, never the first frame that
    carries the nonce (Prism 7f089cac187d on #2060).  ``limit_s`` bounds a reply that keeps
    changing; the last fetch is judged then."""
    deadline = time.time() + limit_s
    last = fetch()
    stable_since = time.time()
    while time.time() < deadline:
        time.sleep(min(4.0, settle_s))
        cur = fetch()
        if cur != last:
            last, stable_since = cur, time.time()
        elif time.time() - stable_since >= settle_s:
            break
    return last


def _bot_messages(dc, channel: str, target_id: str, after_id: str, since_ts: float) -> list[Posted]:
    msgs = dc.req("GET", f"/channels/{channel}/messages?after={after_id}&limit=50") or []
    out = [Posted(m["id"], _iso_ts(m["timestamp"]), m.get("content", ""))
           for m in msgs if m["author"]["id"] == target_id]
    return sorted((m for m in out if m.ts > since_ts), key=lambda m: int(m.id))


def _log_has(home: str, needle: str) -> bool:
    try:
        with open(os.path.join(home, "logs", "gateway.log"), encoding="utf-8-sig", errors="replace") as fh:
            return any(needle in line for line in fh)
    except FileNotFoundError:
        return False


def run_arm(arm: str, env: dict, puppet, dc, target_id: str) -> bool:
    nonce = secrets.token_hex(3)
    home, head = env["PUPPET_LANE_HOME"], env.get("PUPPET_HEAD_SHA", "?")[:12]
    thread = dc.req("POST", f"/channels/{env['PUPPET_CH']}/threads",
                    {"name": f"t_bb4059d2 e2e {arm} {nonce}", "auto_archive_duration": 10080, "type": 11})
    tid = thread["id"]
    with open(env["PUPPET_THREADS"], "a", encoding="utf-8") as fh:
        fh.write(f"{tid}\n")
    t_send = time.time()
    sent = dc.req("POST", f"/channels/{tid}/messages", {"content": f"<@{target_id}> {prompt_for(arm, nonce)}"})
    done_ts, why = _wait_completion(home, t_send)
    if done_ts is None:
        print(f"arm={arm} head={head} posted=none marker_leaked=n verdict=FAIL "
              f"thread={tid} sent={sent['id']} reason={why}", flush=True)
        return False
    if not _wait_turn_end(home, done_ts):
        print(f"arm={arm} head={head} posted=none marker_leaked=n verdict=FAIL thread={tid} "
              f"sent={sent['id']} nonce={nonce} reason=completion_turn_not_finished_in_{COMPLETION_WAIT_S:.0f}s",
              flush=True)
        return False
    fetch = lambda: _bot_messages(dc, tid, target_id, sent["id"], done_ts)  # noqa: E731
    if arm == "long":
        after = settled_messages(fetch)
    else:
        time.sleep(QUIET_WINDOW_S)
        after = fetch()
    posted, leaked, ok = arm_verdict(arm, nonce, after)
    extra = ""
    if arm == "short":
        extra = f" log_suppressed_line={'y' if _log_has(home, 'silence marker suppressed an') else 'n'}"
    elif arm == "long":
        extra = f" log_delivering_line={'y' if _log_has(home, 'silence marker on a long internal reply') else 'n'}"
    print(f"arm={arm} head={head} posted={posted} marker_leaked={'y' if leaked else 'n'} "
          f"verdict={'PASS' if ok else 'FAIL'} thread={tid} sent={sent['id']} nonce={nonce}{extra}", flush=True)
    return ok


def enable_agent_notify(home: str) -> None:
    """Scratch-only: production keeps it off; the E2E needs bg completions to wake the agent.
    Read at spawn time (tools/process_registry.py::resolve_agent_notify_mode), so no restart."""
    with open(os.path.join(home, "config.yaml"), "a", encoding="utf-8") as fh:  # windows-footgun: ok — append-mode write, not a read
        fh.write("display:\n  background_process_agent_notify: 'on'\n")


def main() -> int:
    env = dict(os.environ)
    puppet = _load_puppet(env["PUPPET_PY"])
    tok = puppet._env_value("~/.hermes/.env", "DISCORD_BOT_TOKEN")
    ttok = puppet._env_value("~/.hermes/profiles/aegis/.env", "DISCORD_BOT_TOKEN")
    if not tok or not ttok:
        print("error: driver or target token missing")
        return 1
    dc = puppet.Discord(tok)
    target_id = puppet.Discord(ttok).me()["id"]
    enable_agent_notify(env["PUPPET_LANE_HOME"])
    results = [run_arm(arm, env, puppet, dc, target_id) for arm in ("long", "short", "bare")]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
