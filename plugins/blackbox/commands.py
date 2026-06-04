"""Slash commands for the blackbox telemetry plugin."""

from __future__ import annotations

from typing import Any, Optional, Tuple

try:
    from plugins.blackbox import card, store
except Exception:  # pragma: no cover - other blackbox modules may load later.
    card = None  # type: ignore[assignment]
    store = None  # type: ignore[assignment]


_NO_TURNS = "No turns recorded yet for this channel."
_NOT_FOUND = "Turn not found in this channel."


def _session_value(name: str) -> str:
    """Best-effort gateway channel lookup for slash-command dispatch.

    Plugin slash commands are dispatched before the gateway binds the full
    agent session environment, so the no-arg form depends on
    gateway.session_context having current event values available.
    """
    try:
        from gateway.session_context import get_session_env

        return (get_session_env(name, "") or "").strip()
    except Exception:
        return ""


def _current_channel() -> Tuple[str, str]:
    return (
        _session_value("HERMES_SESSION_PLATFORM"),
        _session_value("HERMES_SESSION_CHAT_ID"),
    )


def _fmt_usd(value: Any) -> str:
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0.0
    return f"${amount:.4f}".rstrip("0").rstrip(".")


def _field(record: dict, *names: str, default: Any = "") -> Any:
    for name in names:
        value = record.get(name)
        if value not in (None, ""):
            return value
    return default


def _render_card(record: dict) -> str:
    if card is None:
        return str(record)
    return card.render(record)


def _handle_latest() -> str:
    platform, chat_id = _current_channel()
    if not platform or not chat_id:
        return _NO_TURNS

    record = store.get_last_turn(platform, chat_id)
    if not record:
        return _NO_TURNS
    return _render_card(record)


def _same_channel(record: dict, platform: str, chat_id: str) -> bool:
    return (
        bool(platform)
        and bool(chat_id)
        and str(record.get("platform") or "") == platform
        and str(record.get("chat_id") or "") == chat_id
    )


def _handle_turn(turn_id: str) -> str:
    platform, chat_id = _current_channel()
    record = store.get_turn(turn_id)
    if not record or not _same_channel(record, platform, chat_id):
        return _NOT_FOUND

    lines = [_render_card(record)]
    user_text = record.get("user_text") or ""
    final_text = record.get("final_text") or ""
    if user_text:
        lines.extend(["", "User:", str(user_text)])
    if final_text:
        lines.extend(["", "Final:", str(final_text)])

    tool_calls = store.get_tool_calls(turn_id) or []
    if tool_calls:
        lines.extend(["", "Tools:"])
        for call in tool_calls:
            name = call.get("name") or "(unknown)"
            args_preview = call.get("args_preview") or ""
            result_preview = call.get("result_preview") or ""
            lines.append(f"- {name}: args={args_preview} result={result_preview}")

    get_children = getattr(store, "get_children", None)
    if callable(get_children):
        children = get_children(turn_id) or []
        if children:
            lines.extend(["", "Child turns:"])
            for child in children:
                child_id = child.get("turn_id") or "(unknown)"
                model = child.get("model") or ""
                cost = _fmt_usd(child.get("cost_usd"))
                suffix = f" — {model}" if model else ""
                lines.append(f"- {child_id} — {cost}{suffix}")

    return "\n".join(lines)


def _handle_session() -> str:
    platform, chat_id = _current_channel()
    if not platform or not chat_id:
        return _NO_TURNS

    rollup = store.session_rollup(platform, chat_id, limit=50) or {}
    count = int(rollup.get("count") or 0)
    total = _fmt_usd(rollup.get("total_usd"))
    avg = _fmt_usd(rollup.get("avg_usd"))
    max_turn = rollup.get("max_turn") or {}
    if max_turn:
        turn_id = max_turn.get("turn_id") or "(unknown)"
        max_cost = _fmt_usd(max_turn.get("cost_usd"))
    else:
        turn_id = "none"
        max_cost = "$0"
    return f"Session spend: {total} over {count} turns (avg {avg}). Priciest: {turn_id} {max_cost}"


def _parse_top_n(parts: list[str]) -> int:
    if len(parts) < 2:
        return 5
    try:
        return max(1, int(parts[1]))
    except ValueError:
        return 5


def _handle_top(parts: list[str]) -> str:
    turns = store.top_turns(_parse_top_n(parts), since_days=30) or []
    if not turns:
        return "No turns recorded yet."

    lines = []
    for idx, record in enumerate(turns, 1):
        turn_id = record.get("turn_id") or "(unknown)"
        cost = _fmt_usd(record.get("cost_usd"))
        model = record.get("model") or "(unknown model)"
        when = _field(record, "datetime", "ts", "ts_start", "created_at", default="")
        lines.append(f"{idx}. {turn_id} — {cost} — {model} — {when}")
    return "\n".join(lines)


def handle_cost(raw_args: str) -> str:
    """Handle the /cost gateway slash command without raising."""
    try:
        if store is None:
            raise RuntimeError("blackbox store is unavailable")

        args = (raw_args or "").strip()
        if not args:
            return _handle_latest()

        parts = args.split()
        subcommand = parts[0].lower()
        if subcommand == "session":
            return _handle_session()
        if subcommand == "top":
            return _handle_top(parts)
        return _handle_turn(parts[0])
    except Exception as exc:
        return f"⚠️ /cost error: {exc}"


def register(ctx) -> None:
    ctx.register_command(
        "cost",
        handler=handle_cost,
        description="Show blackbox telemetry costs for this channel.",
    )
