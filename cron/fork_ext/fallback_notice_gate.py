"""One DECLARED-fallback notice per cron job per local day (t_2efae1cd).

A declared fallback firing is the safety net working, and the notice says "no action needed".
While a primary is walled for days (xai-oauth 403 spending-limit, 2026-10-06), every run and every
retry of the same job walked the same chain, and #logs got the same line once per run: three
`Cron fallback used — morning-digest` lines on 10-06 for one known condition. The first notice of
the day carries all the information. Later ones in the same day for the same job and the same
primary -> fallback pair are dropped. A different pair is new information and still posts.

UNDECLARED fallbacks never come here: they stay loud on every run.
State: ``<home>/cron/fallback_notice_day.json`` = {job_id: {"day": "YYYY-MM-DD", "pair": "a -> b"}}.
Fail-open: any error reading the state means "post it".
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - non-Unix: in-process lock only
    fcntl = None

logger = logging.getLogger(__name__)

_STATE_NAME = "fallback_notice_day.json"
_LOCK_NAME = ".fallback_notice_day.lock"
_thread_lock = threading.Lock()


def _today() -> str:
    return datetime.now().astimezone().date().isoformat()


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def already_noticed(home: Path, job_id: str, pair: str, day: Optional[str] = None) -> bool:
    """True when this job already posted a notice for this pair today."""
    try:
        rec = _load(Path(home) / "cron" / _STATE_NAME).get(str(job_id))
        return isinstance(rec, dict) and rec.get("day") == (day or _today()) and rec.get("pair") == pair
    except Exception as e:  # never cost a notice
        logger.debug("fallback notice gate read failed (posting): %r", e)
        return False


@contextlib.contextmanager
def _state_lock(cron_dir: Path):
    """Serialize the read-modify-replace: parallel tick threads share one process
    (``cron.max_parallel_jobs``) and a CLI ``cron run`` is another process. Each writer
    otherwise loads the same snapshot and the last replace drops the others' records."""
    with _thread_lock:
        fd = None
        try:
            if fcntl is not None:
                try:
                    fd = open(cron_dir / _LOCK_NAME, "a+", encoding="utf-8")
                    fcntl.flock(fd, fcntl.LOCK_EX)
                except OSError as e:  # in-process lock still held
                    logger.debug("fallback notice gate flock unavailable: %r", e)
            yield
        finally:
            if fd is not None:
                fd.close()  # releases the flock


def mark_noticed(home: Path, job_id: str, pair: str, day: Optional[str] = None) -> None:
    """Record a DELIVERED notice. Called only after delivery succeeded."""
    try:
        path = Path(home) / "cron" / _STATE_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with _state_lock(path.parent):
            data = _load(path)
            data[str(job_id)] = {"day": day or _today(), "pair": pair}
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(json.dumps(data, sort_keys=True))
                os.replace(tmp, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise
    except Exception as e:
        logger.debug("fallback notice gate write failed: %r", e)
