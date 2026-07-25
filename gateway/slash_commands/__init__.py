"""Gateway slash-command handlers for GatewayRunner.

Extracted from ``gateway/run.py`` (god-file decomposition Phase 3b,
``619bd7827``) and then split from a single 5,060-line module into this package
(Phase N+1). These are the in-session slash commands (/model, /reset, /usage,
/compress, ...) the gateway dispatches from ``_handle_message``.

``GatewaySlashCommandsMixin`` is now a composition of 16 per-family leaf mixins.
``GatewayRunner`` still inherits the one class, ``self`` is still the
``GatewayRunner`` inside every handler, and every ``self._handle_*_command``
dispatch and test reference resolves through the MRO exactly as before —
no handler body was edited.

Module-level run.py helpers a handler needs (``_hermes_home``,
``_load_gateway_config``, ``_resolve_gateway_model``, etc.) are still imported
lazily inside the handler body — a deferred ``from gateway.run import ...``
resolves at call time (run.py fully loaded by then), avoiding an import cycle.
No module in this package imports ``gateway.run`` at module scope.
"""

from __future__ import annotations

from gateway.session import AsyncSessionStore
from gateway.slash_commands._shared import (
    _RESET_CLEANUP_TIMEOUT_S,
    _model_switch_skew_guard,
    logger,
)
from gateway.slash_commands.registry import GATEWAY_SLASH_HANDLERS
from gateway.slash_commands.session_lifecycle import SessionLifecycleCommandsMixin
from gateway.slash_commands.model import ModelCommandsMixin
from gateway.slash_commands.agents_ops import AgentsOpsCommandsMixin
from gateway.slash_commands.compress import CompressCommandsMixin
from gateway.slash_commands.runtime_flags import RuntimeFlagsCommandsMixin
from gateway.slash_commands.info import InfoCommandsMixin
from gateway.slash_commands.skills import SkillsCommandsMixin
from gateway.slash_commands.usage import UsageCommandsMixin
from gateway.slash_commands.goals import GoalsCommandsMixin
from gateway.slash_commands.reasoning import ReasoningCommandsMixin
from gateway.slash_commands.update import UpdateCommandsMixin
from gateway.slash_commands.approvals import ApprovalsCommandsMixin
from gateway.slash_commands.kanban import KanbanCommandsMixin
from gateway.slash_commands.voice import VoiceCommandsMixin
from gateway.slash_commands.home import HomeCommandsMixin
from gateway.slash_commands.memory import MemoryCommandsMixin


class GatewaySlashCommandsMixin(
    SessionLifecycleCommandsMixin,
    ModelCommandsMixin,
    AgentsOpsCommandsMixin,
    CompressCommandsMixin,
    RuntimeFlagsCommandsMixin,
    InfoCommandsMixin,
    SkillsCommandsMixin,
    UsageCommandsMixin,
    GoalsCommandsMixin,
    ReasoningCommandsMixin,
    UpdateCommandsMixin,
    ApprovalsCommandsMixin,
    KanbanCommandsMixin,
    VoiceCommandsMixin,
    HomeCommandsMixin,
    MemoryCommandsMixin,
):
    """In-session slash-command handlers for GatewayRunner."""

    async_session_store: AsyncSessionStore


__all__ = [
    "GatewaySlashCommandsMixin",
    "GATEWAY_SLASH_HANDLERS",
    "logger",
    "_RESET_CLEANUP_TIMEOUT_S",
    "_model_switch_skew_guard",
    "SessionLifecycleCommandsMixin",
    "ModelCommandsMixin",
    "AgentsOpsCommandsMixin",
    "CompressCommandsMixin",
    "RuntimeFlagsCommandsMixin",
    "InfoCommandsMixin",
    "SkillsCommandsMixin",
    "UsageCommandsMixin",
    "GoalsCommandsMixin",
    "ReasoningCommandsMixin",
    "UpdateCommandsMixin",
    "ApprovalsCommandsMixin",
    "KanbanCommandsMixin",
    "VoiceCommandsMixin",
    "HomeCommandsMixin",
    "MemoryCommandsMixin",
]
