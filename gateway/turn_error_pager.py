"""Operator page when the gateway tells a messaging user their turn failed.

A friend's room can die on a deterministic error (2026-10-28 00:39: a Matrix room hit
SessionKeyConflict on every turn) and the only witness is the user, who sees
"Sorry, I encountered an error". ``_notify_turn_error`` is the one place every platform
sends that notice, so it calls :func:`maybe_page` right after the notice to page #alerts.

Transport: the fleet's one #alerts pager, ``scripts/lib/episode_pager.py`` under the
running Hermes root, run as a bounded fire-and-forget subprocess. Episodes: key
``<platform>:<chat_id>``; the first error in a room pages, further errors inside the
episode are silent, and the episode clears after ``QUIET_S`` with no new error (the timer
re-arms on every error). A missing pager script (hermetic home, non-fleet install) is a
logged no-op. Nothing here ever raises into the error path or makes a network call.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

PRODUCER = "gateway-turn-error"
QUIET_S = 30 * 60
SUBPROCESS_TIMEOUT_S = 10
REASON_CHARS = 200
# Surfaces where a human reads the reply; api_server/webhook/cron/local are not paged.
USER_FACING_PLATFORMS = frozenset({
    "matrix", "discord", "telegram", "whatsapp", "whatsapp_cloud", "signal",
    "bluebubbles", "imessage",
})

_open_episodes: Dict[str, asyncio.TimerHandle] = {}
_inflight: Set[asyncio.Task] = set()


def _platform_name(platform: Any) -> str:
    return str(getattr(platform, "value", platform) or "").strip().lower()


def episode_key(event: Any) -> Optional[str]:
    """``<platform>:<chat_id>`` for a user-facing surface, else None (not paged)."""
    src = getattr(event, "source", None)
    platform = _platform_name(getattr(src, "platform", None))
    chat_id = str(getattr(src, "chat_id", "") or "")
    if platform not in USER_FACING_PLATFORMS or not chat_id:
        return None
    return f"{platform}:{chat_id}"


def page_text(event: Any, error: BaseException, session_key: Optional[str]) -> str:
    src = event.source
    platform = _platform_name(src.platform)
    room = f"`{src.chat_id}`" + (f" ({src.chat_name})" if getattr(src, "chat_name", None) else "")
    user_id = getattr(src, "user_id", None) or "?"
    user_name = getattr(src, "user_name", None)
    user = f"{user_name} ({user_id})" if user_name and user_name != user_id else str(user_id)
    reason = " ".join(str(error).split())[:REASON_CHARS] or "(no detail)"
    return (
        f"🔴 **Gateway turn error** · {platform} room {room}\n"
        f"user: {user} · error: `{type(error).__name__}`\n"
        f"reason: {reason}\n"
        f"session: `{session_key or '?'}`\n"
        f"-# The user got the error notice. One page per room per episode; "
        f"clears after {QUIET_S // 60} min with no new error."
    )


def maybe_page(event: Any, error: BaseException, session_key: Optional[str] = None) -> bool:
    """Page once per room episode; True when a page was dispatched. Never raises."""
    key = None
    try:
        key = episode_key(event)
        if key is None:
            return False
        loop = asyncio.get_running_loop()
        handle = _open_episodes.get(key)
        _open_episodes[key] = loop.call_later(QUIET_S, _clear_episode, key)
        if handle is not None:
            handle.cancel()
            return False
        _dispatch(["page", "--producer", PRODUCER, "--key", key,
                   "--text", page_text(event, error, session_key),
                   "--resolve-text", f"✅ **Gateway turn error** cleared · `{key}`: "
                                     f"no new error for {QUIET_S // 60} min"], key)
        return True
    except Exception:
        logger.error("turn-error page for %s not dispatched", key, exc_info=True)
        if key is not None:
            _forget(key)  # the next error in this room retries the page
        return False


def _forget(key: str) -> None:
    handle = _open_episodes.pop(key, None)
    if handle is not None:
        handle.cancel()


def _clear_episode(key: str) -> None:
    _open_episodes.pop(key, None)
    try:
        _dispatch(["clear", "--producer", PRODUCER, "--key", key], None)
    except Exception:
        logger.error("turn-error episode clear for %s not dispatched", key, exc_info=True)


def pager_script() -> Optional[Path]:
    """episode_pager.py under the RUNNING root (never a hardcoded home)."""
    from hermes_constants import get_default_hermes_root
    path = get_default_hermes_root() / "scripts" / "lib" / "episode_pager.py"
    return path if path.is_file() else None


def _dispatch(args: List[str], page_key: Optional[str]) -> None:
    task = asyncio.get_running_loop().create_task(_run_pager(args, page_key))
    _inflight.add(task)
    task.add_done_callback(_inflight.discard)


async def _run_pager(args: List[str], page_key: Optional[str]) -> None:
    try:
        script = pager_script()
        if script is None:
            logger.warning("turn-error page skipped: episode_pager.py not found (%s)", args[:5])
            return
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(script), *args, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), SUBPROCESS_TIMEOUT_S)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"episode_pager timed out after {SUBPROCESS_TIMEOUT_S}s")
        outcome = out.decode(errors="replace").strip()
        if proc.returncode != 0:
            raise RuntimeError(f"episode_pager rc={proc.returncode}: "
                               f"{err.decode(errors='replace').strip()[:300]}")
        logger.info("turn-error pager %s %s: %s", args[0], args[4], outcome)
    except Exception:
        logger.error("turn-error pager %s failed", args[:5], exc_info=True)
        if page_key is not None:
            _forget(page_key)
