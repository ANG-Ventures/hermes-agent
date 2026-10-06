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

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_STATE_NAME = "fallback_notice_day.json"


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


def mark_noticed(home: Path, job_id: str, pair: str, day: Optional[str] = None) -> None:
    """Record a DELIVERED notice. Called only after delivery succeeded."""
    try:
        path = Path(home) / "cron" / _STATE_NAME
        data = _load(path)
        data[str(job_id)] = {"day": day or _today(), "pair": pair}
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:
        logger.debug("fallback notice gate write failed: %r", e)
