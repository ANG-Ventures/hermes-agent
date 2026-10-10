"""mem0 maintenance window: conclude freeze + refused-fact journal + drain pause + replay CLI.

PRD-studio-cutover v4, I2 (b)(c), RC1-3. Two host-wide flag files, same JSON shape
``{"started_at", "expires_at", "reason"}``:

  <root>/state/mem0-window.flag             mem0_conclude refuses AND journals the fact
  <root>/state/mem0-capture-drain.pause     the capture drain makes no attempts (rows stay pending)

``<root>`` is the Hermes ROOT (``~/.hermes``), not the profile home: the window is per HOST, so
every profile on the box (Apollo, Aegis, ...) must see the same flag. An expired flag is ignored
(logged once) so a crashed window script cannot freeze concludes; an unreadable flag counts as
ACTIVE (fail closed: the fact is journaled, never lost) and ``status --check`` pages on it.

CLI (run from the hermes-agent checkout):
  python3 -m plugins.memory.mem0.window open --minutes N [--reason R]
  python3 -m plugins.memory.mem0.window status [--check]
  python3 -m plugins.memory.mem0.window replay
  python3 -m plugins.memory.mem0.window close
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

WINDOW_FLAG_NAME = "mem0-window.flag"
PAUSE_FLAG_NAME = "mem0-capture-drain.pause"
JOURNAL_NAME = "mem0-window-journal.jsonl"
REPLAYING_SUFFIX = ".replaying"
LOCK_NAME = "mem0-window-journal.lock"
DEFAULT_QUEUE_PATH = "~/.hermes/state/mem0-capture/capture_queue.db"  # = capture_pipeline default
PENDING_MAX_AGE_S = 30 * 60

_expired_logged: set = set()


# ---- paths ----------------------------------------------------------------------------------
def state_dir() -> Path:
    from hermes_constants import get_default_hermes_root
    return get_default_hermes_root() / "state"


def window_flag_path() -> Path:
    return state_dir() / WINDOW_FLAG_NAME


def pause_flag_path() -> Path:
    return state_dir() / PAUSE_FLAG_NAME


def journal_path() -> Path:
    return state_dir() / JOURNAL_NAME


@contextlib.contextmanager
def _journal_lock(path: Optional[Path] = None) -> Iterator[None]:
    """Host-wide exclusive lock serializing (flag check + journal append), flag removal and
    journal rotation, so no conclude can write into a journal being drained or land after
    ``close()`` finished. A separate file: the journal itself is renamed away."""
    path = path or state_dir() / LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as f:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


# ---- flags ----------------------------------------------------------------------------------
def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def flag_state(path: Path, *, now: Optional[float] = None) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Return (state, data): state in {"absent", "active", "expired", "invalid"}."""
    now = time.time() if now is None else now
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "absent", None
    except OSError:
        return "invalid", None
    try:
        data = json.loads(raw)
        expires = _parse_ts(data["expires_at"])
    except Exception:
        return "invalid", None
    if expires <= now:
        key = (str(path), data.get("expires_at"))
        if key not in _expired_logged:
            _expired_logged.add(key)
            logger.warning("mem0 window: ignoring EXPIRED flag %s (expires_at=%s)",
                           path, data.get("expires_at"))
        return "expired", data
    return "active", data


def flag_active(path: Path, *, now: Optional[float] = None) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Active or unreadable (fail closed) -> True; absent or expired -> False."""
    state, data = flag_state(path, now=now)
    return state in ("active", "invalid"), data


def drain_paused() -> bool:
    """Per-tick drain-pause read (capture_drain). Never raises."""
    try:
        return flag_active(pause_flag_path())[0]
    except Exception as e:
        logger.debug("mem0 drain-pause check failed (not paused): %s", e)
        return False


def write_flag(path: Path, *, minutes: float, reason: str, now: Optional[float] = None) -> Dict[str, Any]:
    now = time.time() if now is None else now
    data = {"started_at": _iso(now), "expires_at": _iso(now + minutes * 60), "reason": reason}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return data


# ---- conclude gate (called by the provider) -------------------------------------------------
def refuse_conclude(user_id: str, agent_id: str, text: str) -> Optional[str]:
    """If the window flag is active: journal the fact and return the tool JSON (result when journaled, error when not); else None.

    The flag read and the append happen under the journal lock, so ``close()`` (which removes
    the flags under the same lock) cannot finish while a refused fact is still unwritten."""
    try:
        with _journal_lock():
            return _refuse_locked(user_id, agent_id, text)
    except OSError as e:  # lock unusable (state dir broken): keep the pre-lock behaviour
        logger.error("mem0 window: journal lock unavailable, proceeding unlocked: %s", e)
        return _refuse_locked(user_id, agent_id, text)


def _refuse_locked(user_id: str, agent_id: str, text: str) -> Optional[str]:
    active, data = flag_active(window_flag_path())
    if not active:
        return None
    until = (data or {}).get("expires_at", "the window closes")
    try:
        _append_journal(journal_path(), {"user_id": user_id, "agent_id": agent_id,
                                         "text": text, "ts": _iso(time.time())})
    except Exception as e:
        logger.error("mem0 window: journal append FAILED, fact not kept: %s", e)
        return json.dumps({"error": f"mem0 maintenance window, re-issue after {until} "
                                    f"(journal write failed: {e}; the fact was NOT kept)"})
    # Journaled: replay at close is the sole writeback, so the agent must NOT re-issue it
    # (a re-issue after the window would store every maintenance-time fact twice).
    return json.dumps({"result": f"mem0 maintenance window until {until}: fact journaled, it is "
                                 f"stored automatically when the window closes. Do not re-issue it."})


def _append_journal(path: Path, entry: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


# ---- replay ---------------------------------------------------------------------------------
AddFn = Callable[[Dict[str, Any]], Any]


def default_add_fn() -> AddFn:
    """Direct ``POST /memories {infer:false}``: never the plugin tool path, so the flag is bypassed."""
    from plugins.memory.mem0 import _DirectRestMem0Client, _load_config
    cfg = _load_config()
    if not cfg.get("host"):
        raise RuntimeError("mem0 window replay: no self-hosted host configured (MEM0_HOST / mem0.json)")
    client = _DirectRestMem0Client(host=cfg["host"], admin_api_key=cfg.get("admin_api_key", ""),
                                   agent_id=cfg.get("agent_id", ""), ca_bundle=cfg.get("ca_bundle", ""))

    def _add(entry: Dict[str, Any]) -> Any:
        return client.add([{"role": "user", "content": entry["text"]}],
                          user_id=entry.get("user_id"), agent_id=entry.get("agent_id"),
                          infer=False, metadata={"write_kind": "deliberate"})
    return _add


def replay(add_fn: AddFn, *, path: Optional[Path] = None) -> Tuple[int, int]:
    """Atomically drain the journal: mv journal -> journal.replaying, POST each row.

    Returns (journaled, replayed). The .replaying file is deleted when every row landed; on a
    partial failure it is rewritten with ONLY the failed rows, so a re-run does not duplicate the
    rows that already landed. A .replaying left by a crashed run is replayed first.
    """
    path = path or journal_path()
    replaying = path.with_name(path.name + REPLAYING_SUFFIX)
    leftover = replaying.exists()
    if not leftover:
        # Under the lock no conclude holds an fd on the live journal (appends open+write+close
        # inside it), so nothing can be written into the renamed file after we read it.
        with _journal_lock(path.with_name(LOCK_NAME)):
            try:
                os.rename(path, replaying)
            except FileNotFoundError:
                return 0, 0
    lines = [ln for ln in replaying.read_text(encoding="utf-8").splitlines() if ln.strip()]
    failed: List[str] = []
    replayed = 0
    for ln in lines:
        try:
            entry = json.loads(ln)
            if not entry.get("text"):
                raise ValueError("journal row has no text")
            add_fn(entry)
            replayed += 1
        except Exception as e:
            logger.error("mem0 window replay: row failed: %s", e)
            failed.append(ln)
    if failed:
        tmp = replaying.with_name(replaying.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("".join(ln + "\n" for ln in failed))
        os.replace(tmp, replaying)
    else:
        replaying.unlink()
        if leftover and path.exists():  # crash leftover done; now the journal written since
            j, r = replay(add_fn, path=path)
            return len(lines) + j, replayed + r
    return len(lines), replayed


def close(add_fn: AddFn) -> Tuple[int, int]:
    """Remove both flags, replay the journal, then one post-removal sweep.

    The flags are removed under the journal lock: every conclude that read the flag as active
    has already appended, and every later one sees it absent and stores directly."""
    with _journal_lock():
        for p in (window_flag_path(), pause_flag_path()):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
    j1, r1 = replay(add_fn)
    if r1 != j1:
        return j1, r1  # leftover stays in .replaying; a sweep now would recount it
    j2, r2 = replay(add_fn)
    return j1 + j2, r1 + r2


# ---- status / healthcheck -------------------------------------------------------------------
def _pending_stats(queue_path: str, now: float) -> Tuple[int, Optional[float]]:
    qp = os.path.expanduser(queue_path)
    if not os.path.exists(qp):
        return 0, None
    conn = sqlite3.connect(f"file:{qp}?mode=ro", uri=True, timeout=10)
    try:
        n, oldest = conn.execute(
            "SELECT COUNT(*), MIN(created_at) FROM capture_queue WHERE status='pending'").fetchone()
    finally:
        conn.close()
    return int(n or 0), (now - oldest) if oldest is not None else None


def status(*, queue_path: str = DEFAULT_QUEUE_PATH, now: Optional[float] = None) -> Dict[str, Any]:
    now = time.time() if now is None else now
    out: Dict[str, Any] = {}
    for name, p in (("window", window_flag_path()), ("pause", pause_flag_path())):
        st, data = flag_state(p, now=now)
        out[name] = st
        out[f"{name}_expires_at"] = (data or {}).get("expires_at")
    jp = journal_path()
    out["journal"] = sum(
        sum(1 for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip())
        for f in (jp, jp.with_name(jp.name + REPLAYING_SUFFIX)) if f.exists())
    try:
        out["pending"], age = _pending_stats(queue_path, now)
        out["oldest_pending_age_s"] = None if age is None else int(age)
    except Exception as e:
        out["pending"], out["oldest_pending_age_s"] = None, None
        out["queue_error"] = str(e)
    problems = []
    for name in ("window", "pause"):
        if out[name] in ("expired", "invalid"):
            problems.append(f"{name}_flag_{out[name]}")
    if (out["pause"] in ("active", "invalid") and out["oldest_pending_age_s"] is not None
            and out["oldest_pending_age_s"] > PENDING_MAX_AGE_S):
        problems.append("pending_older_than_30m_while_paused")
    if out["window"] != "active" and out["journal"]:
        problems.append("journal_unreplayed")  # facts refused but never replayed
    out["problems"] = problems
    return out


# ---- CLI ------------------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None, *, add_fn: Optional[AddFn] = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m plugins.memory.mem0.window")
    sub = ap.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("open", help="create/refresh both flags")
    o.add_argument("--minutes", type=float, required=True)
    o.add_argument("--reason", default="mem0 maintenance window")
    s = sub.add_parser("status")
    s.add_argument("--check", action="store_true", help="exit 2 on a stale flag or old pending rows")
    s.add_argument("--queue", default=DEFAULT_QUEUE_PATH)
    sub.add_parser("replay")
    sub.add_parser("close", help="remove both flags, replay the journal, sweep once more")
    args = ap.parse_args(argv)

    if args.cmd == "open":
        if args.minutes <= 0:
            ap.error("--minutes must be > 0")
        data = write_flag(window_flag_path(), minutes=args.minutes, reason=args.reason)
        write_flag(pause_flag_path(), minutes=args.minutes, reason=args.reason)
        print(f"window=open expires_at={data['expires_at']}")
        return 0
    if args.cmd == "status":
        st = status(queue_path=args.queue)
        print(" ".join(f"{k}={v}" for k, v in st.items() if k != "problems")
              + f" problems={','.join(st['problems']) or 'none'}")
        return 2 if args.check and st["problems"] else 0
    fn = add_fn or default_add_fn()
    journaled, replayed = replay(fn) if args.cmd == "replay" else close(fn)
    print(f"journaled={journaled} replayed={replayed}")
    return 0 if journaled == replayed else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(main())
