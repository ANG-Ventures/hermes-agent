"""Per-turn wall budget across the fallback chain while no byte has arrived (t_452c6ead).

The chain is walked lane by lane, and each lane keeps its own clocks (relay
``per_attempt_timeout_s`` 420 s, a reasoning-model stale floor of 600-840 s). On
2026-10-11 01:59-02:34 a turn walked alr -> dtlr -> btpr -> bpr on the SAME model
and the user got zero bytes for 34 minutes: two dtlr seats stalled 420 s each,
then btpr/bpr ate 429s and a 460 s dead seat (t_f2ddf6d1, defect 3).

This module adds one clock that spans the lanes. It is armed when an API call
starts waiting and disarmed by the first real stream event (a keepalive ping does
not count) or a completed response. While armed:

* every ``fallback.still_waiting_notice_seconds`` (120) the chat gets a
  "still waiting" line naming how many lanes were tried;
* once ``fallback.no_first_byte_budget_seconds`` (180) is spent, the current
  attempt is cut (only when a lane on a DIFFERENT model is still ahead in the
  chain), and the chain walker skips the remaining lanes that serve the same
  model as the stalled one: they are other faces of the same pool, which is
  what just failed to answer.

``0`` disables either knob. State lives on the agent as ``_wall_budget``.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_BUDGET_S = 180.0
DEFAULT_NOTICE_S = 120.0


class FallbackWallBudgetExceeded(RuntimeError):
    """The attempt was cut because the turn's no-first-byte budget was spent.

    Classified ``pool_stalled`` (non-retryable, fall back): re-sending to the same
    lane re-enters the stall the budget just measured."""


def _knobs() -> tuple[float, float]:
    try:
        from hermes_cli.config import load_config_readonly
        cfg = (load_config_readonly() or {}).get("fallback") or {}
    except Exception:
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}

    def _num(key: str, default: float) -> float:
        value = cfg.get(key, default)
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return default

    return (_num("no_first_byte_budget_seconds", DEFAULT_BUDGET_S),
            _num("still_waiting_notice_seconds", DEFAULT_NOTICE_S))


def _state(agent: Any) -> Optional[dict]:
    st = getattr(agent, "_wall_budget", None)
    return st if isinstance(st, dict) else None


def reset(agent: Any) -> None:
    """Turn start: nothing is armed."""
    agent._wall_budget = None


def arm(agent: Any, now: Optional[float] = None) -> None:
    """An API call is about to wait. Keeps an already-armed clock: a fallback
    restart is the same wait continuing on another lane."""
    if _state(agent) is not None:
        return
    budget_s, notice_s = _knobs()
    agent._wall_budget = {
        "started": time.monotonic() if now is None else now,
        "budget_s": budget_s, "notice_s": notice_s,
        "lanes": 1, "notices": 0, "spent_logged": False,
    }


def disarm(agent: Any) -> None:
    """A real byte (or a full response) arrived: the wait is over."""
    if getattr(agent, "_wall_budget", None) is not None:
        agent._wall_budget = None


def note_lane_switch(agent: Any) -> None:
    st = _state(agent)
    if st is not None:
        st["lanes"] += 1


def elapsed(agent: Any, now: Optional[float] = None) -> Optional[float]:
    st = _state(agent)
    if st is None:
        return None
    return (time.monotonic() if now is None else now) - st["started"]


def spent(agent: Any, now: Optional[float] = None) -> bool:
    st = _state(agent)
    if st is None or st["budget_s"] <= 0:
        return False
    return elapsed(agent, now) >= st["budget_s"]


def _norm(model: Any) -> str:
    return str(model or "").strip().lower()


def _remaining_entries(agent: Any) -> list:
    chain = getattr(agent, "_fallback_chain", None) or []
    idx = int(getattr(agent, "_fallback_index", 0) or 0)
    return [fb for fb in chain[idx:] if isinstance(fb, dict)]


def has_other_model_ahead(agent: Any, model: Any = None) -> bool:
    """A lane on a model other than ``model`` (default: the current one) is still ahead."""
    current = _norm(getattr(agent, "model", "") if model is None else model)
    return any(_norm(fb.get("model")) not in ("", current) for fb in _remaining_entries(agent))


def should_cut_attempt(agent: Any, now: Optional[float] = None) -> bool:
    """Cut the in-flight attempt: budget spent and somewhere better to go. Fires once per
    wait, so the lane the cut moves to gets its own clocks instead of an instant cut."""
    st = _state(agent)
    if st is None or st.get("fired") or not spent(agent, now) or not has_other_model_ahead(agent):
        return False
    st["fired"] = True
    return True


def skip_lane(agent: Any, fb_model: Any, now: Optional[float] = None) -> bool:
    """Chain walker: skip ``fb_model`` when the budget is spent, it is the model that
    just stalled, and a lane on another model is still ahead (the walk reaches it)."""
    if not spent(agent, now):
        return False
    stalled = _norm(getattr(agent, "model", ""))
    if _norm(fb_model) != stalled or not has_other_model_ahead(agent, stalled):
        return False
    st = _state(agent) or {}
    logger.info("Fallback skip: %s lane is the model that stalled; no-first-byte budget spent "
                "(%.0fs >= %.0fs, %d lanes tried)", fb_model, elapsed(agent, now) or 0.0,
                st.get("budget_s", 0.0), st.get("lanes", 0))
    return True


def notice_due(agent: Any, now: Optional[float] = None) -> Optional[str]:
    """The next "still waiting" line when its interval has passed, else None (one per interval)."""
    st = _state(agent)
    if st is None or st["notice_s"] <= 0:
        return None
    waited = elapsed(agent, now) or 0.0
    due = int(waited // st["notice_s"])
    if due <= st["notices"]:
        return None
    st["notices"] = due
    lanes = st["lanes"]
    route = f"{getattr(agent, 'provider', '') or '?'}/{getattr(agent, 'model', '') or '?'}"
    return (f"⏳ still waiting for a first reply: {int(waited)}s, "
            f"{lanes} lane{'s' if lanes != 1 else ''} tried (now on {route})")
