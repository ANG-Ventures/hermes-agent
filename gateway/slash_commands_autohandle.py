"""``/autohandle on|off|status`` — session-level auto-handle default (Boomerang v2 D-15).
Bound onto ``GatewayRunner`` through ``GatewaySlashCommandsMixin``.

The value lives in the session entry's metadata (``kanban_auto_handle.SESSION_METADATA_KEY``)
so the kanban wake paths read it per home session at wake time. A card's own ``auto_handle``
flag beats it; with neither set the board default applies, then off.
"""

from __future__ import annotations

from agent.i18n import t
from gateway.kanban_auto_handle import SESSION_METADATA_KEY, board_default
from gateway.kanban_wake_disposition import resolve_auto_handle
from gateway.platforms.base import EphemeralReply
from gateway.platforms.event import MessageEvent

_ARG_VALUES = {"on": True, "off": False}


class GatewayAutoHandleCommandsMixin:
    """/autohandle."""

    async def _handle_autohandle_command(self, event: MessageEvent) -> EphemeralReply:
        arg = event.get_command_args().strip().lower()
        session_key = self._session_key_for_source(event.source)
        if arg in _ARG_VALUES:
            if not self.session_store.set_session_metadata(session_key, SESSION_METADATA_KEY,
                                                           _ARG_VALUES[arg]):
                return EphemeralReply(t("gateway.autohandle.unavailable"))
            return EphemeralReply(t("gateway.autohandle.set", value=arg))
        if arg not in ("", "status"):
            return EphemeralReply(t("gateway.autohandle.usage"))
        session_value = self.session_store.get_session_metadata(session_key, SESSION_METADATA_KEY)
        session_value = session_value if isinstance(session_value, bool) else None
        board_value = board_default()
        resolved = resolve_auto_handle(None, session_value, board_value)
        source = ("session" if session_value is not None
                  else "board default" if board_value is not None else "default")
        return EphemeralReply(t("gateway.autohandle.status", value="on" if resolved else "off",
                                source=source))
