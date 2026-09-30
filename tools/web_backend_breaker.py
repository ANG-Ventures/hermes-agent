"""Circuit breaker for web backends that are dead for billing/auth reasons.

A keyed web backend answering HTTP 402 (out of credits) or 401 (bad key) will
answer the same way on the next call, and the one after that. Without a
breaker every ``web_search`` pays a doomed round-trip plus a WARNING line
before the fallback chain serves the call.

Behaviour:

* A 402/401-class failure opens the breaker for ``web.dead_backend_cooldown_seconds``
  (default 3600). While open, the backend is skipped (no network call) and the
  normal fallback chain / keyless rescue serves the call.
* When the cooldown lapses, the next call probes the backend once. Another
  402/401 re-opens it quietly; a success closes the episode.
* An EPISODE runs from the first dead response to the next success. It logs one
  WARNING at the start and one INFO at recovery, and runs
  ``web.dead_backend_alert_command`` at most once (it retries on a later trip
  only if the command exited nonzero).

State is a small JSON file under ``$HERMES_HOME/state`` so every process of a
profile (gateway, warm clients, subagents) shares one episode and one page.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
import secrets
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_COOLDOWN_SECONDS = 3600
_ALERT_TIMEOUT_SECONDS = 30
_STATE_FILENAME = "web_backend_breaker.json"

# Billing (402) and auth (401) failures. Rate limits (429) and 5xx are
# transient and deliberately NOT matched.
_DEAD_PATTERNS = (
    (re.compile(r"\b402\b|payment required|insufficient credits|out of credits"
                r"|credits? (?:exhausted|depleted)|quota exceeded for (?:this|your) (?:plan|account)",
                re.IGNORECASE), 402),
    (re.compile(r"\b401\b|unauthori[sz]ed|invalid api key|invalid_api_key"
                r"|api key (?:is )?invalid", re.IGNORECASE), 401),
)


def classify_dead(error: Any) -> Optional[int]:
    """Return 402 or 401 when *error* reads as a billing/auth death, else None."""
    text = str(error or "")
    if not text:
        return None
    for pattern, status in _DEAD_PATTERNS:
        if pattern.search(text):
            return status
    return None


def _web_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config().get("web", {})
        return cfg if isinstance(cfg, dict) else {}
    except Exception:  # noqa: BLE001 — config optional here
        return {}


def cooldown_seconds(cfg: Optional[Dict[str, Any]] = None) -> int:
    cfg = _web_config() if cfg is None else cfg
    try:
        return max(0, int(cfg.get("dead_backend_cooldown_seconds", DEFAULT_COOLDOWN_SECONDS)))
    except (TypeError, ValueError):
        return DEFAULT_COOLDOWN_SECONDS


def _state_path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "state" / _STATE_FILENAME


_thread_lock = threading.Lock()


@contextmanager
def _locked_state():
    """Yield the mutable state dict under a thread + file lock; persist on exit."""
    path = _state_path()
    with _thread_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_suffix(".lock")
        with open(lock_path, "a+", encoding="utf-8") as lock_fh:
            try:
                import fcntl

                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
            except (ImportError, OSError):  # Windows / odd FS: thread lock only
                pass
            try:
                state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
                if not isinstance(state, dict):
                    state = {}
            except (OSError, ValueError):
                state = {}
            before = json.dumps(state, sort_keys=True)
            yield state
            if json.dumps(state, sort_keys=True) != before:
                tmp = path.with_suffix(f".tmp.{os.getpid()}")
                tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
                os.replace(tmp, path)


def open_until(backend: str, now: Optional[float] = None) -> Optional[float]:
    """Return the epoch the breaker for *backend* reopens, or None when closed."""
    if not backend:
        return None
    now = time.time() if now is None else now
    try:
        with _locked_state() as state:
            entry = state.get(backend)
            until = float(entry.get("open_until", 0)) if isinstance(entry, dict) else 0.0
    except Exception as exc:  # noqa: BLE001 — breaker must never break search
        logger.debug("web backend breaker read failed: %s", exc)
        return None
    return until if until > now else None


def skip_error(backend: str, until: float) -> str:
    """Error text for a call skipped because the breaker is open."""
    entry = _entry(backend) or {}
    reopen = time.strftime("%H:%M", time.localtime(until))
    return (f"backend '{backend}' skipped: circuit open until {reopen} after HTTP "
            f"{entry.get('status', '?')} ({str(entry.get('error', ''))[:160]})")


def _entry(backend: str) -> Optional[Dict[str, Any]]:
    try:
        with _locked_state() as state:
            entry = state.get(backend)
            return dict(entry) if isinstance(entry, dict) else None
    except Exception:  # noqa: BLE001
        return None


def record_failure(backend: str, error: Any, now: Optional[float] = None,
                   cfg: Optional[Dict[str, Any]] = None) -> bool:
    """Trip the breaker when *error* is a 402/401 death.

    Returns True when this call started a NEW episode.
    """
    status = classify_dead(error)
    if status is None or not backend:
        return False
    cfg = _web_config() if cfg is None else cfg
    cooldown = cooldown_seconds(cfg)
    if cooldown <= 0:
        return False
    now = time.time() if now is None else now
    new_episode = False
    need_page = False
    try:
        with _locked_state() as state:
            entry = state.get(backend)
            if not isinstance(entry, dict):
                entry = {"episode_start": now, "paged": False}
                new_episode = True
            entry.update(status=status, error=str(error)[:300], open_until=now + cooldown,
                         last_trip=now, trips=int(entry.get("trips", 0)) + 1)
            state[backend] = entry
            # "paging" marks an alert in flight; a stale mark (the process
            # died mid-send) must not suppress the page forever.
            in_flight = now - float(entry.get("paging") or 0) < 4 * _ALERT_TIMEOUT_SECONDS
            need_page = not entry.get("paged") and not in_flight
            if need_page:
                entry["paging"] = now
                entry["alert_id"] = secrets.token_hex(8)
            episode_start = entry["episode_start"]
            alert_id = entry.get("alert_id", "")
    except Exception as exc:  # noqa: BLE001
        logger.debug("web backend breaker write failed: %s", exc)
        return False
    if new_episode:
        logger.warning(
            "web backend '%s' is dead (HTTP %s: %s); skipping it for %ds. "
            "Further failures this episode are not logged per call.",
            backend, status, str(error)[:200], cooldown,
        )
    else:
        logger.debug("web backend '%s' still dead (HTTP %s); breaker re-opened", backend, status)
    if need_page:
        _fire_alert(backend, status, str(error), cfg, episode_start, alert_id)
    return new_episode


def record_success(backend: str) -> None:
    """Close an open episode for *backend* (no-op when none is open)."""
    if not backend:
        return
    try:
        with _locked_state() as state:
            entry = state.pop(backend, None)
    except Exception as exc:  # noqa: BLE001
        logger.debug("web backend breaker reset failed: %s", exc)
        return
    if isinstance(entry, dict):
        logger.info("web backend '%s' recovered; dead-backend episode closed", backend)


def _fire_alert(backend: str, status: int, error: str, cfg: Dict[str, Any],
                episode_start: float, alert_id: str) -> None:
    command = str(cfg.get("dead_backend_alert_command") or "").strip()
    if not command:
        _mark_paged(backend, episode_start, alert_id, ok=True,
                    note="no alert command configured")
        return

    from hermes_constants import get_hermes_home

    # The gateway can multiplex profiles with ContextVar-scoped homes. Capture
    # the initiating context BEFORE spawning the alert thread and give the
    # child only its profile home + path (never inherited default-profile
    # credentials). A host-local pager can load its own credentials.
    context = contextvars.copy_context()
    profile_home = str(get_hermes_home())

    def _run() -> None:
        env = {"HOME": os.path.expanduser("~"),
               "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
               "HERMES_HOME": profile_home,
               "WEB_BACKEND": backend, "WEB_BACKEND_STATUS": str(status),
               "WEB_BACKEND_ERROR": error[:300]}
        try:
            proc = subprocess.run(command, shell=True, env=env, stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=_ALERT_TIMEOUT_SECONDS)
            ok = proc.returncode == 0
            note = f"rc={proc.returncode} {(proc.stderr or '').strip()[:200]}"
        except Exception as exc:  # noqa: BLE001
            ok, note = False, f"{type(exc).__name__}: {exc}"
        if ok:
            logger.info("web backend '%s' dead-backend alert sent", backend)
        else:
            logger.error("web backend '%s' dead-backend alert FAILED (%s); "
                         "will retry on the next trip", backend, note)
        _mark_paged(backend, episode_start, alert_id, ok=ok, note=note)

    threading.Thread(target=lambda: context.run(_run), name="web-backend-alert",
                     daemon=True).start()


def _mark_paged(backend: str, episode_start: float, alert_id: str,
                *, ok: bool, note: str) -> None:
    try:
        with _locked_state() as state:
            entry = state.get(backend)
            if (not isinstance(entry, dict)
                    or entry.get("episode_start") != episode_start
                    or entry.get("alert_id") != alert_id):
                return  # stale alert from an earlier episode/attempt
            entry.pop("paging", None)
            if ok:
                entry["paged"] = True
                entry["paged_note"] = note[:200]
    except Exception as exc:  # noqa: BLE001
        logger.debug("web backend breaker page mark failed: %s", exc)
