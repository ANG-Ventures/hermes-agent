"""A cron job's ``env`` field: the names a job adds to its script child's environment.

The runner (``cron.scheduler_script._run_job_script``) overlays it after the sanitizer;
``cron.jobs.update_job`` refuses a bad one at edit time. Only script children receive it (the
job's ``script`` / ``monitor_script``); an agent turn runs in-process and never sees it.
"""

from __future__ import annotations

from typing import Any, Optional

# Names the runner sets or strips on purpose: a job must not override them.
# _HERMES_CRON_SCRIPT_RUN == cron.scheduler_script.SCRIPT_RUN_TOKEN_ENV (pinned by a test);
# _HERMES_GATEWAY == hermes_cli.process_bootstrap.GATEWAY_MARKER_ENV.
RESERVED_JOB_ENV_KEYS = frozenset({
    "HERMES_CRON_JOB_ID", "_HERMES_CRON_SCRIPT_RUN", "_HERMES_GATEWAY",
    "AI_AGENT", "HERMES_AGENT",
})


def job_env_error(env: Any) -> Optional[str]:
    """The reason ``env`` is not a valid job env, or None. None / {} are valid (no overlay)."""
    if env is None:
        return None
    if not isinstance(env, dict):
        return f"cron job env must be a mapping of str to str, got {type(env).__name__}"
    for key, value in env.items():
        if not isinstance(key, str) or not key or "=" in key or "\0" in key:
            return f"cron job env has an invalid name {key!r}"
        if key in RESERVED_JOB_ENV_KEYS:
            return f"cron job env may not set {key}: the script runner owns it"
        if not isinstance(value, str) or "\0" in value:
            return f"cron job env {key} must be a str, got {type(value).__name__}"
    return None
