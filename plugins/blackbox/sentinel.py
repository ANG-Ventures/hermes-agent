"""New-model pricing sentinel — event-driven unpriced-model detection.

Card t_2e382a4b. The trigger for this is the claude-opus-5 launch (2026-07-24):
the model went live on the fleet and every turn it recorded landed with
``cost_usd`` NULL / ``cost_status`` "unknown" because no rate existed yet, and
NOBODY NOTICED until someone happened to read a spend rollup. The gap between
"a new model starts running" and "we know we can't price it" was open-ended.

Design (Ace-ratified): there is no detector cron, because the event already
exists in-process. ``plugins.blackbox`` prices every turn at record time via
``compute_turn_cost``; the moment that comes back unpriced for a model absent
from the pricing snapshot, we know. So the detector hangs off the WRITE:

  1. ``observe_turn`` is called right after ``store.insert_turn`` with the
     already-computed cost status. It is a pure predicate + one INSERT OR
     IGNORE into ``seen_unpriced_models``; the (model, provider) PRIMARY KEY
     is the dedup, so a repeat sighting is a no-op.
  2. On the unseen → seen transition ONLY, it fires a Discord #alerts
     notification fire-and-forget on a daemon thread and stamps ``alerted_at``.

THE HOT-PATH CONTRACT (the load-bearing property, test (b)):

    Nothing in this module may raise into, block, or slow the turn-record
    path. The turn is already durably stored before ``observe_turn`` runs; an
    alert failure must lose an alert, never telemetry.

That is enforced structurally, not by hope:
  * ``observe_turn`` wraps its ENTIRE body in ``try/except Exception`` and
    returns a bool — it has no failure mode that escapes.
  * The detection predicate (``is_known_model``) is network-free by contract.
  * Delivery happens on a separate daemon thread, so a hung/slow Discord API
    call cannot add latency to the user's turn. The caller does not join it.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# #alerts. The sentinel is a fleet-operations signal (a rate ingestion is
# owed), not a per-conversation card, so it always goes to the ops channel
# rather than back to the session that happened to trip it.
ALERTS_CHANNEL_ID = "1480528231286181948"

CARD_REF = "t_2e382a4b"

# Cost statuses that mean "we could not price this turn". "partial" is
# deliberately EXCLUDED: a partial turn priced at least one call, so its model
# is known — the missing piece is a sibling call (e.g. a failed MoA advisor),
# which is a different problem with a different owner.
_UNPRICED_STATUSES = frozenset({"unknown"})

# Retain references to in-flight alert threads so they are observable in tests
# and can't be garbage-collected mid-send.
_PENDING: set = set()
_PENDING_LOCK = threading.Lock()


def _notify_script() -> Optional[Path]:
    """Locate ``notify.py`` (the out-of-agent Discord/Telegram alert helper).

    Checked in order and returned on first hit; ``None`` when none exists, in
    which case delivery is skipped (and the ledger row stays unstamped, so a
    later run can still alert).
    """
    candidates = [
        Path(os.path.expanduser("~/.hermes/scripts/notify.py")),
        Path(os.path.expanduser(
            "~/.hermes/skills-shared/general/scheduler/scripts/notify.py"
        )),
        Path(os.path.expanduser(
            "~/.hermes/skills/devops/scheduler/scripts/notify.py"
        )),
    ]
    for path in candidates:
        try:
            if path.exists():
                return path
        except Exception:
            continue
    return None


def render_alert(model: str, provider: str) -> str:
    """The #alerts message body. Pure, so it is directly assertable."""
    return (
        "🆕💵 Unpriced model detected\n"
        f"• Model: {model or '(empty)'}\n"
        f"• Provider: {provider or '(empty)'}\n"
        "• First unpriced turn recorded — rate ingestion needed.\n"
        f"• See card {CARD_REF}"
    )


def send_alert(model: str, provider: str) -> bool:
    """Deliver the sentinel alert to Discord #alerts. Never raises.

    Runs on the alert thread, not the record path. Returns True only on a
    confirmed successful send, which is what gates the ``alerted_at`` stamp.
    """
    try:
        script = _notify_script()
        if script is None:
            logger.warning("blackbox sentinel: no notify.py found; alert skipped")
            return False
        proc = subprocess.run(
            [
                sys.executable,
                str(script),
                "--send",
                render_alert(model, provider),
                "--channel",
                "discord",
                "--target",
                ALERTS_CHANNEL_ID,
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        return proc.returncode == 0
    except Exception:
        logger.warning("blackbox sentinel: alert delivery failed", exc_info=True)
        return False


def _alert_and_stamp(model: str, provider: str, alert_fn: Callable[[str, str], bool]) -> None:
    """Alert body run on the background thread. Swallows everything."""
    try:
        if not alert_fn(model, provider):
            return
        from plugins.blackbox import store

        store.mark_unpriced_alerted(model, provider)
    except Exception:
        logger.warning("blackbox sentinel: alert thread failed", exc_info=True)


def _dispatch_alert(model: str, provider: str, alert_fn: Callable[[str, str], bool]) -> None:
    """Fire the alert on a daemon thread. Fire-and-forget; never joined."""
    thread = threading.Thread(
        target=_alert_and_stamp,
        args=(model, provider, alert_fn),
        name="blackbox-pricing-sentinel",
        daemon=True,
    )
    with _PENDING_LOCK:
        _PENDING.add(thread)
    try:
        thread.start()
    except Exception:
        # Thread creation itself can fail under resource pressure. Losing the
        # alert here is acceptable; losing the turn is not.
        with _PENDING_LOCK:
            _PENDING.discard(thread)
        logger.warning("blackbox sentinel: could not start alert thread", exc_info=True)
        return

    def _reap() -> None:
        try:
            thread.join()
        finally:
            with _PENDING_LOCK:
                _PENDING.discard(thread)

    threading.Thread(target=_reap, name="blackbox-pricing-sentinel-reap", daemon=True).start()


def observe_turn(
    model: str,
    provider: str,
    cost_status: str,
    cost_usd: Any = None,
    *,
    base_url: str | None = None,
    alert_fn: Callable[[str, str], bool] | None = None,
    dispatch: Callable[[str, str, Callable[[str, str], bool]], None] | None = None,
) -> bool:
    """Detector entrypoint, called once per recorded turn. NEVER raises.

    Returns True iff this call recorded a NEW (model, provider) ledger row and
    therefore dispatched an alert. The return value is diagnostic only — the
    caller on the record path ignores it.

    ``alert_fn`` / ``dispatch`` are injection seams for tests (including the
    alert-failure injection that proves the hot path survives a broken
    alerter); production leaves both at their defaults.
    """
    try:
        status = str(cost_status or "").strip().lower()
        if status not in _UNPRICED_STATUSES:
            return False
        # An unpriced turn must genuinely have no cost. Belt-and-braces against
        # a caller that passes a stale status alongside a real number.
        if cost_usd is not None:
            return False
        model_name = str(model or "").strip()
        if not model_name:
            return False
        provider_name = str(provider or "").strip()

        from agent.usage_pricing import is_known_model

        if is_known_model(model_name, provider_name, base_url):
            return False

        from plugins.blackbox import store

        if not store.note_unpriced_model(model_name, provider_name):
            # Already in the ledger: the alert for this model already fired.
            return False

        logger.warning(
            "blackbox sentinel: first unpriced turn for model=%s provider=%s (card %s)",
            model_name,
            provider_name,
            CARD_REF,
        )
        (dispatch or _dispatch_alert)(model_name, provider_name, alert_fn or send_alert)
        return True
    except Exception:
        # The turn is ALREADY stored by the time we run. Telemetry is never at
        # risk here; the only thing this except can cost is an alert.
        logger.warning("blackbox pricing sentinel failed", exc_info=True)
        return False
