"""Fan-out brake: a per-board 24h USD ceiling on dispatcher spawning.

Incident 2026-09-22: ~730 worked cards / ~$13K in two days, with nothing in the
dispatcher that could see the dollar cost it was creating. Concurrency caps
(``max_spawn``, ``max_in_progress``) bound how many workers run AT ONCE; they
say nothing about the total spend of a long fan-out.

This module measures what a board's workers have actually cost in a rolling
window, from the per-profile blackbox turn ledgers, and lets the dispatcher stop
spawning for that board when the ceiling is crossed.

Design constraints this file honours:

* **Read-only.** Every ledger is opened ``mode=ro`` through a URI. A cost
  accounting bug must never be able to write to a turn ledger.
* **Tolerant.** A missing, locked, or corrupt ledger contributes 0 and logs
  ONCE (per process) — a diagnostic must not brick dispatch.
* **Cheap.** One query per ledger per tick, and a caller-supplied per-tick cache
  so N boards do not re-read the same ledgers N times.

Attribution: a worker turn's ``user_text`` is literally
``work kanban task t_XXXXXXXX``. A card id belongs to a board iff that board's
``tasks`` table has the row — so the measurement is "sum the cost of turns whose
card lives on THIS board", which is correct across boards without needing a
board column in the ledger.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)

# Card ids are ``t_`` + 8 lowercase hex. Anchored to a word boundary so a longer
# token can't be truncated into a false id.
_TASK_ID_RE = re.compile(r"\bt_[0-9a-f]{8}\b")

DEFAULT_WINDOW_HOURS = 24
PAUSE_MARKER_NAME = ".budget_paused.json"

# #logs — the recovery line goes to the quiet channel, not #alerts. A pause is
# an alert; a resume is a log.
RECOVERY_TARGET = "1480525090331561984"

# One-shot log guards, keyed by ledger path, so a permanently broken ledger
# does not emit a warning on every tick of every board.
_warned_ledgers: set[str] = set()


def _load_config() -> dict:
    from hermes_cli.config import load_config

    return load_config() or {}


def _budget_cfg() -> dict:
    try:
        cfg = _load_config()
    except Exception:
        return {}
    kcfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    budget = kcfg.get("budget") if isinstance(kcfg, dict) else None
    return budget if isinstance(budget, dict) else {}


def configured_ceiling_usd() -> Optional[float]:
    """``kanban.budget.usd_per_24h``, or None when the brake is off.

    Fails toward OFF: an unreadable or non-numeric value leaves the ceiling
    disabled rather than pausing a board on a config typo. (Unlike the park
    policy, whose safe direction is ON — pausing a whole board on bad input
    would be a self-inflicted outage.)
    """
    raw = _budget_cfg().get("usd_per_24h")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        _log.warning(
            "kanban budget: kanban.budget.usd_per_24h=%r is not a number; "
            "ceiling disabled", raw,
        )
        return None
    return value if value > 0 else None


def configured_window_hours() -> int:
    try:
        value = int(_budget_cfg().get("window_hours", DEFAULT_WINDOW_HOURS))
    except (TypeError, ValueError):
        return DEFAULT_WINDOW_HOURS
    return value if value > 0 else DEFAULT_WINDOW_HOURS


def configured_page_channel() -> str:
    value = _budget_cfg().get("page_channel")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "#alerts"


def _hermes_root(home: Optional[Path] = None) -> Path:
    if home is not None:
        return Path(home)
    from hermes_constants import get_default_hermes_root

    return Path(get_default_hermes_root())


def _board_task_ids(board_db_path: Path) -> set[str]:
    """Every non-archived-or-otherwise card id on this board. Read-only."""
    path = Path(board_db_path)
    if not path.exists():
        return set()
    uri = f"file:{path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    except sqlite3.Error as exc:
        _warn_once(str(path), "board db", exc)
        return set()
    try:
        rows = conn.execute("SELECT id FROM tasks").fetchall()
    except sqlite3.Error as exc:
        _warn_once(str(path), "board db", exc)
        return set()
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return {r[0] for r in rows if r and r[0]}


def _warn_once(path: str, what: str, exc: Exception) -> None:
    if path in _warned_ledgers:
        return
    _warned_ledgers.add(path)
    _log.warning(
        "kanban budget: %s %s unreadable (%s: %s); counted as $0 this window",
        what, path, type(exc).__name__, exc,
    )


def _ledger_paths(root: Path) -> list[Path]:
    profiles = root / "profiles"
    try:
        entries = sorted(profiles.iterdir())
    except OSError:
        return []
    out = []
    for entry in entries:
        ledger = entry / "blackbox" / "turns.db"
        if ledger.exists():
            out.append(ledger)
    return out


def _ledger_card_costs(ledger: Path, since_ts: int) -> list[tuple[str, float]]:
    """``(card_id, cost_usd)`` for every in-window worker turn in this ledger.

    ONE query per ledger: the window filter is pushed into SQL. The result is
    deliberately BOARD-INDEPENDENT — the ledgers are the same files for every
    board on the host, so this is the part worth caching once per tick. The
    board membership test is the cheap set lookup left to ``_sum_ledgers``
    (a parameterised ``IN`` over thousands of ids would be worse than this).

    Rows are returned in ledger order so a per-board sum accumulates in exactly
    the order the old per-ledger loop did.
    """
    uri = f"file:{ledger}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    except sqlite3.Error as exc:
        _warn_once(str(ledger), "turn ledger", exc)
        return []
    try:
        rows = conn.execute(
            "SELECT cost_usd, user_text FROM turns "
            "WHERE ts_start >= ? AND cost_usd IS NOT NULL AND user_text IS NOT NULL",
            (since_ts,),
        ).fetchall()
    except sqlite3.Error as exc:
        _warn_once(str(ledger), "turn ledger", exc)
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    out: list[tuple[str, float]] = []
    for cost, text in rows:
        if not text or "work kanban task" not in text:
            continue
        match = _TASK_ID_RE.search(text)
        if match is None:
            continue
        try:
            value = float(cost)
        except (TypeError, ValueError):
            continue
        out.append((match.group(0), value))
    return out


def _window_start_ts(
    window_hours: int, *, now: Optional[int] = None, cache: Optional[dict] = None,
) -> int:
    """Start of the rolling window, PINNED for the life of ``cache``.

    Without the pin, each board calls ``time.time()`` a few ms apart, every
    board gets a different ``since_ts``, and the board-independent ledger cache
    below misses on every board — the exact failure this change exists to fix.
    An explicit ``now`` (tests, dispatcher) bypasses the pin entirely.
    """
    if now is not None:
        return int(now) - window_hours * 3600
    if cache is None:
        return int(time.time()) - window_hours * 3600
    key = ("_tick_now", int(window_hours))
    pinned = cache.get(key)
    if pinned is None:
        pinned = int(time.time())
        cache[key] = pinned
    return int(pinned) - window_hours * 3600


def _all_ledger_card_costs(
    root: Path, since_ts: int,
) -> list[list[tuple[str, float]]]:
    """One list of ``(card_id, cost)`` per ledger, outer list in ledger order."""
    return [
        _ledger_card_costs(ledger, since_ts) for ledger in _ledger_paths(root)
    ]


def _sum_ledgers(
    root: Path,
    since_ts: int,
    board_ids: set[str],
    *,
    cache: Optional[dict] = None,
) -> float:
    """Total in-window cost of turns whose card is on this board.

    The expensive half (opening every ledger) does not depend on the board, so
    it is cached on ``(root, since_ts)`` — NOT on the board db path, which is
    distinct for every board and therefore misses on every board.
    """
    key = ("_ledger_card_costs", str(root), int(since_ts))
    if cache is not None and key in cache:
        per_ledger = cache[key]
    else:
        per_ledger = _all_ledger_card_costs(root, since_ts)
        if cache is not None:
            cache[key] = per_ledger
    total = 0.0
    for ledger_rows in per_ledger:
        subtotal = 0.0
        for card_id, cost in ledger_rows:
            if card_id in board_ids:
                subtotal += cost
        total += subtotal
    return total


def board_spend_usd(
    board_db_path,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    home=None,
    *,
    cache: Optional[dict] = None,
    now: Optional[int] = None,
) -> float:
    """Total USD spent by workers on THIS board's cards in the window.

    ``cache`` is a caller-owned dict scoped to one dispatcher tick — pass the
    same dict for every board so the ledgers are read once, not once per board.
    The per-board entry only memoises the cheap board-membership filter; the
    ledger reads are cached separately (and board-independently) inside
    ``_sum_ledgers``, which is what makes a tick 10 ledger opens instead of
    10 x N_boards.
    """
    path = Path(board_db_path)
    key = (str(path), int(window_hours))
    if cache is not None and key in cache:
        return cache[key]
    root = _hermes_root(home)
    since_ts = _window_start_ts(window_hours, now=now, cache=cache)
    board_ids = _board_task_ids(path)
    spend = (
        0.0
        if not board_ids
        else _sum_ledgers(root, since_ts, board_ids, cache=cache)
    )
    if cache is not None:
        cache[key] = spend
    return spend


# ---------------------------------------------------------------------------
# Pause marker + paging
# ---------------------------------------------------------------------------


def _marker_path(board_dir: Path) -> Path:
    return Path(board_dir) / PAUSE_MARKER_NAME


def read_pause_marker(board_dir) -> Optional[dict]:
    path = _marker_path(Path(board_dir))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _run_notify(argv: list[str]) -> None:
    """Fire notify.py out-of-agent. Best-effort by contract."""
    try:
        subprocess.run(
            argv,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except Exception:
        pass


def _notify_script_path(home=None) -> Optional[str]:
    """Locate notify.py under the RUNNING root, never a hardcoded ~/.hermes.

    A hermetic test home has no notify.py, so this returns None there and the
    page is a no-op — which is exactly right: a sandboxed board must never fire
    a real alert about cards that do not exist on the real board.
    """
    root = _hermes_root(home)
    for candidate in (
        root / "scripts" / "notify.py",
        root / "skills-shared/general/scheduler/scripts/notify.py",
        root / "skills/devops/scheduler/scripts/notify.py",
    ):
        try:
            if candidate.exists():
                return str(candidate)
        except Exception:
            continue
    return None


def evaluate_board_budget(
    board: Optional[str],
    board_db_path,
    board_dir,
    *,
    cache: Optional[dict] = None,
    home=None,
    now: Optional[int] = None,
) -> bool:
    """Return True when this board must NOT spawn this tick.

    Side effects, once per state TRANSITION (never once per tick):

    * crossing the ceiling → write ``.budget_paused.json`` and page ``--sev warn``
    * dropping back under  → delete the marker and post one ✅ line to #logs

    The marker file is the episode latch. Paging off "is the marker absent"
    rather than off an in-memory flag is deliberate: the dispatcher lives in a
    gateway that restarts, and an in-memory latch would re-page on every
    restart for the duration of a long pause.
    """
    ceiling = configured_ceiling_usd()
    board_dir = Path(board_dir)
    if ceiling is None:
        # Brake off. Do NOT clear a marker here: a board paused under a ceiling
        # that was then unset should keep its evidence for the operator.
        return False
    window = configured_window_hours()
    try:
        spend = board_spend_usd(
            board_db_path, window_hours=window, home=home, cache=cache, now=now,
        )
    except Exception as exc:
        # Fail OPEN on the measurement: a cost-accounting bug must not be able
        # to halt every board on the host.
        _log.warning(
            "kanban budget: spend measurement failed for board %s (%s: %s); "
            "not pausing", board, type(exc).__name__, exc,
        )
        return False
    existing = read_pause_marker(board_dir)
    label = board or "default"
    if spend >= ceiling:
        try:
            board_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "since": (existing or {}).get("since") or int(
                    now if now is not None else time.time()
                ),
                "spend": round(spend, 4),
                "ceiling": ceiling,
                "window_hours": window,
            }
            _marker_path(board_dir).write_text(
                json.dumps(payload, indent=2) + "\n", encoding="utf-8",
            )
        except Exception as exc:
            _log.warning(
                "kanban budget: could not write pause marker for board %s (%s)",
                label, exc,
            )
        if existing is None:
            _page_paused(label, spend, ceiling, window, home=home)
        return True
    if existing is not None:
        try:
            _marker_path(board_dir).unlink()
        except Exception:
            pass
        _page_recovered(label, spend, ceiling, window, home=home)
    return False


def _page_paused(
    board: str, spend: float, ceiling: float, window: int, *, home=None,
) -> None:
    script = _notify_script_path(home)
    if script is None:
        return
    body = (
        f"💸 Kanban board '{board}' PAUSED on budget — "
        f"${spend:.2f} spent in the last {window}h vs a "
        f"${ceiling:.2f} ceiling.\n"
        "No new workers will spawn on this board until spend falls back under "
        "the ceiling (the window rolls forward) or the ceiling is raised.\n"
        "Lift it: set kanban.budget.usd_per_24h higher (or null to disable) in "
        "config.yaml. Inspect: hermes kanban budget"
    )
    _run_notify([sys.executable, script, "--sev", "warn", "--send", body])


def _page_recovered(
    board: str, spend: float, ceiling: float, window: int, *, home=None,
) -> None:
    script = _notify_script_path(home)
    if script is None:
        return
    body = (
        f"✅ Kanban board '{board}' budget recovered — ${spend:.2f} in the last "
        f"{window}h is back under the ${ceiling:.2f} ceiling; spawning resumed."
    )
    _run_notify(
        [
            sys.executable, script,
            "--channel", "discord",
            "--target", RECOVERY_TARGET,
            "--send", body,
        ]
    )


def board_budget_report(board: Optional[str] = None, *, home=None) -> list[dict]:
    """Read-only per-board spend/ceiling/paused rows for ``hermes kanban budget``."""
    from hermes_cli import kanban_db as kb

    ceiling = configured_ceiling_usd()
    window = configured_window_hours()
    if board:
        slugs = [kb._normalize_board_slug(board) or kb.DEFAULT_BOARD]
    else:
        try:
            slugs = [
                b.get("slug") or kb.DEFAULT_BOARD
                for b in kb.list_boards(include_archived=False)
            ]
        except Exception:
            slugs = [kb.DEFAULT_BOARD]
    cache: dict = {}
    out = []
    for slug in slugs:
        with kb.enumerating_boards():
            db_path = kb.kanban_db_path(slug)
            bdir = kb.board_dir(slug)
        try:
            spend = board_spend_usd(
                db_path, window_hours=window, home=home, cache=cache,
            )
            error = None
        except Exception as exc:
            # A report must never crash, but it must not print a confident
            # "$0.00" for a measurement it could not make — "cannot measure"
            # and "spent nothing" are not the same answer.
            spend = 0.0
            error = f"{type(exc).__name__}: {exc}"
        out.append(
            {
                "board": slug,
                "spend_usd": spend,
                "ceiling_usd": ceiling,
                "window_hours": window,
                "paused": read_pause_marker(bdir) is not None,
                "error": error,
            }
        )
    return out


def format_budget_report(rows) -> str:
    lines = []
    for row in rows:
        ceiling = row.get("ceiling_usd")
        ceiling_str = f"${ceiling:.2f}" if ceiling is not None else "(no ceiling)"
        if row.get("error"):
            state = f"UNMEASURABLE ({row['error']})"
            spend_str = "        ?"
        else:
            state = "PAUSED" if row.get("paused") else "ok"
            spend_str = f"{row['spend_usd']:>9.2f}"
        lines.append(
            f"{row['board']:24s}  ${spend_str} / {ceiling_str:>14s}"
            f"  {row['window_hours']}h  {state}"
        )
    if not lines:
        return "(no boards)"
    return "\n".join(lines)
