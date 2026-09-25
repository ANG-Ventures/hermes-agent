"""Fork-owned pure helpers for hermes_state.py.

This module intentionally imports nothing from hermes_state. Keep SQL execution,
connection handling, and SessionDB methods in hermes_state.py.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Tuple

# Keep the ORIGINAL logger name: these records logged as "hermes_state" for
# their whole life pre-extraction; renaming the logger would silently move them
# out of any log-filter/handler configured on "hermes_state" (Greptile P2 —
# logger name is an observable output the golden did not capture).
logger = logging.getLogger("hermes_state")

def _sql_placeholders(values) -> str:
    return ",".join("?" for _ in values)


def _session_list_denorm_enabled() -> bool:
    """Lazy config.yaml-only gate for the dormant session.list denorm path."""
    try:
        from hermes_cli.config import cfg_get, read_raw_config

        value = cfg_get(
            read_raw_config(),
            "dashboard",
            "session_list_denorm",
            default=False,
        )
    except Exception as exc:
        logger.debug("dashboard.session_list_denorm read failed: %s", exc)
        return False
    return value is True
