"""Pure cron scheduler helpers owned by the fork."""

from __future__ import annotations

import os

from hermes_constants import parse_reasoning_effort, resolve_reasoning_config


def get_script_timeout(script_timeout, default_script_timeout: int, *, load_config, logger) -> int:
    """Resolve cron pre-run script timeout from module/env/config with a safe default."""
    if script_timeout != default_script_timeout:
        try:
            timeout = int(float(script_timeout))
            if timeout > 0:
                return timeout
        except Exception:
            logger.warning("Invalid patched _SCRIPT_TIMEOUT=%r; using env/config/default", script_timeout)

    env_value = os.getenv("HERMES_CRON_SCRIPT_TIMEOUT", "").strip()
    if env_value:
        try:
            timeout = int(float(env_value))
            if timeout > 0:
                return timeout
        except Exception:
            logger.warning("Invalid HERMES_CRON_SCRIPT_TIMEOUT=%r; using config/default", env_value)

    try:
        cfg = load_config() or {}
        cron_cfg = cfg.get("cron", {}) if isinstance(cfg, dict) else {}
        configured = cron_cfg.get("script_timeout_seconds")
        if configured is not None:
            timeout = int(float(configured))
            if timeout > 0:
                return timeout
    except Exception as exc:
        logger.debug("Failed to load cron script timeout from config: %s", exc)

    return default_script_timeout


# Floor for a schedule-derived script ceiling: a */1 job still gets a full
# minute, and a sub-minute ceiling would make the kill race the spawn.
MIN_DERIVED_SCRIPT_TIMEOUT = 60

# jobs.json key for an explicit per-job script ceiling (seconds).
JOB_SCRIPT_TIMEOUT_KEY = "timeout_s"


def schedule_interval_seconds(schedule, *, now=None):
    """Seconds between consecutive fires of *schedule*, or None.

    ``interval`` schedules use ``minutes``; ``cron`` schedules use the gap
    between the fire slot containing *now* and the next one (so a job
    firing late still gets its slot's full width). ``once`` and anything
    unparseable return None.
    """
    if not isinstance(schedule, dict):
        return None
    kind = schedule.get("kind")
    try:
        if kind == "interval":
            minutes = float(schedule.get("minutes"))
            return minutes * 60 if minutes > 0 else None
        if kind == "cron":
            from datetime import datetime

            from cron import jobs as _jobs

            expr = schedule.get("expr")
            if not expr or not _jobs._ensure_croniter():
                return None
            base = now or datetime.now().astimezone()
            prev_fire = _jobs.croniter(expr, base).get_prev(float)
            next_fire = _jobs.croniter(expr, base).get_next(float)
            gap = next_fire - prev_fire
            return gap if gap > 0 else None
    except Exception:
        return None
    return None


def resolve_job_script_timeout(job, global_timeout: int, *, now=None) -> tuple[int, str]:
    """Per-job script ceiling: ``(seconds, source)``.

    Precedence: explicit ``timeout_s`` on the job, else the schedule
    interval (a job must not outlive its own next fire), else the global
    ``cron.script_timeout_seconds``. The result is NEVER above the global,
    which stays the operator's hard cap.
    """
    global_timeout = int(global_timeout)
    if not isinstance(job, dict):
        return global_timeout, "global"
    raw = job.get(JOB_SCRIPT_TIMEOUT_KEY)
    if raw is not None and not isinstance(raw, bool):
        try:
            explicit = int(float(raw))
        except (TypeError, ValueError):
            explicit = 0
        if explicit > 0:
            return min(explicit, global_timeout), "job"
    interval = schedule_interval_seconds(job.get("schedule"), now=now)
    if interval:
        derived = max(int(interval), MIN_DERIVED_SCRIPT_TIMEOUT)
        return min(derived, global_timeout), "interval"
    return global_timeout, "global"


def resolve_cron_reasoning_config(job: dict, cfg, model: str) -> dict | None:
    """Resolve a cron job's per-job reasoning override, falling back to config."""
    reasoning_config = None
    job_effort = str(job.get("reasoning_effort") or "").strip()
    if job_effort:
        reasoning_config = parse_reasoning_effort(job_effort)
    if reasoning_config is None:
        reasoning_config = resolve_reasoning_config(cfg if isinstance(cfg, dict) else {}, str(model))
    return reasoning_config
