"""Enforcement: no gateway-reachable module writes per-session state to the
process-global os.environ (the v3-latch bug class).

The gateway runs concurrent sessions in ONE process. A per-session
``os.environ["HERMES_SESSION_ID"|"HERMES_SESSION_KEY"|"HERMES_CRON_SESSION"] =``
write in a gateway-reachable module clobbers other concurrent sessions. v3 fixed
HERMES_CRON_SESSION; the gateway-session-env-leak PRD fixed SESSION_ID/KEY. This
test fails if a regression reintroduces such a write outside the sanctioned
single-process entrypoints (CLI / TUI worker / ACP server / oneshot), which are
single-session-per-process and where os.environ is the correct mechanism.
"""

import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]

# Per-session vars that must never be written to process-global os.environ from
# a gateway-reachable module.
_VARS = ("HERMES_SESSION_ID", "HERMES_SESSION_KEY", "HERMES_CRON_SESSION")

# Single-session-per-process entrypoints where os.environ writes are correct.
_ALLOWED_PREFIXES = (
    "cli.py",
    "hermes_cli/",
    "tui_gateway/",
    "acp_adapter/",
    "scripts/",
    "tests/",
)

# Specific allowlisted lines: the except-fallback in agent init/compression
# (only fires if the import of set_current_session_id fails; set_current_session_id
# is itself gateway-aware) — documented in the PRD. Plus session_context.py
# itself, which owns the ONE sanctioned gateway-AWARE setter (its os.environ
# write is guarded by `if _HERMES_GATEWAY != "1"` — the CLI/cron/worker fallback).
_ALLOWED_FILES_FOR_FALLBACK = (
    "agent/agent_init.py",
    "agent/conversation_compression.py",
    "gateway/session_context.py",
)

_WRITE_RE = re.compile(
    r"""os\.environ\[\s*['"](HERMES_SESSION_ID|HERMES_SESSION_KEY|HERMES_CRON_SESSION)['"]\s*\]\s*="""
)


def _is_allowed(rel: str) -> bool:
    return any(rel == p or rel.startswith(p) for p in _ALLOWED_PREFIXES)


def test_no_gateway_session_env_writes():
    violations = []
    for path in REPO.rglob("*.py"):
        rel = str(path.relative_to(REPO))
        if _is_allowed(rel):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in _WRITE_RE.finditer(text):
            line_no = text[: m.start()].count("\n") + 1
            # Allow the documented except-fallback lines in agent init/compression.
            if rel in _ALLOWED_FILES_FOR_FALLBACK:
                continue
            violations.append(f"{rel}:{line_no}: {m.group(0)}")
    assert not violations, (
        "Gateway-reachable per-session os.environ write(s) found (v3-latch bug "
        "class). Use the per-turn contextvar (set_session_vars/set_cron_session), "
        "not process-global os.environ:\n  " + "\n  ".join(violations)
    )
