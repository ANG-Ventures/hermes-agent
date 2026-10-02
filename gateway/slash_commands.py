"""Gateway slash-command handlers for GatewayRunner: lifted out of ``gateway/run.py`` into a mixin
so ``self._handle_*_command`` keeps resolving via the MRO.  Cohesive clusters live in the sibling
mixins (``slash_commands_model/_session/_status/_goals``); this module keeps the shared helpers plus
the one-off commands.  run.py helpers are imported lazily."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import inspect
import logging
import os
import re
import shlex
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Union

from agent.account_usage import fetch_account_usage, render_account_usage_lines
from agent.i18n import t
from agent.turn_context import extract_api_content_sidecar
from gateway.config import HomeChannel, Platform, PlatformConfig, persist_home_channel
from gateway.platforms.base import EphemeralReply
from gateway.platforms.event import MessageEvent
from gateway.session import AsyncSessionStore, SessionSource, build_session_key
from gateway.session_transcript import TranscriptReadError
from gateway.slash_commands_goals import GatewayGoalCommandsMixin
from gateway.slash_commands_model import GatewayModelCommandsMixin, _model_switch_skew_guard
from gateway.slash_commands_session import (
    _RESET_CLEANUP_TIMEOUT_S,
    _reset_process_scoped_tool_state,
    GatewaySessionCommandsMixin,
)
from gateway.slash_commands_login import GatewayLoginCommandsMixin
from gateway.slash_commands_status import (
    GatewayStatusCommandsMixin,
    _clean_str,
    _configured_provider,
    _fmt,
    _int_value,
    _n,
    _quiet,
    _status_model_route,
    history_unreadable,
)
from hermes_cli.config import atomic_config_write, cfg_get, clear_model_endpoint_credentials
from hermes_cli.status_report import build_status_fields
from utils import atomic_json_write, base_url_host_matches, is_truthy_value

logger = logging.getLogger("gateway.run")


# /rollback result keys -> i18n line for files the safe restore left alone.
_ROLLBACK_SKIP_LINES = (("skipped_user_edits", "gateway.rollback.kept_user_edits"),
                        ("skipped_oversize", "gateway.rollback.kept_oversize"),
                        ("failed_deletes", "gateway.rollback.failed_deletes"))

# /busy input modes -> (status-card behavior key, set-confirmation behavior key); text via t().
_BUSY_MODE_BEHAVIOR = {
    "queue": ("gateway.busy.behavior_queue_short", "gateway.busy.behavior_queue_long"),
    "steer": ("gateway.busy.behavior_steer_short", "gateway.busy.behavior_steer_long"),
    "interrupt": ("gateway.busy.behavior_interrupt_short", "gateway.busy.behavior_interrupt_long"),
}

# /diff argument -> diff mode (unknown args leave the mode unchanged).
_DIFF_MODE_BY_ARG = {**dict.fromkeys(("staged", "--staged", "cached", "--cached"), "staged"),
                     **dict.fromkeys(("all", "--all", "head"), "all"), "session": "session"}

# /voice subcommand -> stored mode (None = auto-TTS disabled), confirmation i18n key.
_VOICE_MODE_BY_ARG = {
    **dict.fromkeys(("on", "enable"), ("voice_only", "gateway.voice.enabled_voice_only")),
    **dict.fromkeys(("off", "disable"), ("off", "gateway.voice.disabled_text")),
    "tts": ("all", "gateway.voice.tts_enabled")}

# /footer argument -> new enabled state ("" toggles; anything else is a usage error).
_FOOTER_STATE_BY_ARG = {**dict.fromkeys(("on", "enable", "true", "1"), True),
                        **dict.fromkeys(("off", "disable", "false", "0"), False)}

# /approve modifier tokens -> approval choice (default "once").
_APPROVE_CHOICE_BY_ARG = {**dict.fromkeys(("always", "permanent", "permanently"), "always"),
                          **dict.fromkeys(("session", "ses"), "session")}


_WINDOWS_UPDATE_HELPER = """
import os, subprocess, sys
output_path, exit_code_path, cmd = sys.argv[1], sys.argv[2], sys.argv[3:]
env = dict(os.environ, PYTHONUNBUFFERED="1")
with open(output_path, "wb") as f:
    rc = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, env=env).wait(timeout=3600)
with open(exit_code_path, "w", encoding="utf-8") as f:
    f.write(str(rc))
""".strip()


def _nested_dict(root: dict, *keys: str) -> dict:
    """Walk/create ``root[k1][k2]...`` as dicts, replacing any non-dict value on the path."""
    for k in keys:
        if not isinstance(root.get(k), dict):
            root[k] = {}
        root = root[k]
    return root


def _write_raw_config_leaf(config_path: Path, keys: tuple, value) -> None:
    """Set one leaf through a strict raw round-trip. The behavioral read is fail-open (``{}``) and
    expanded, so writing it back wipes the file after a read error and persists ``${VAR}`` values."""
    from hermes_cli.config import read_user_config_raw
    raw = read_user_config_raw(config_path)
    *parents, leaf = keys
    _nested_dict(raw, *parents)[leaf] = value
    atomic_config_write(config_path, raw)


def _preview(text: str, limit: int = 60) -> str:
    return text[:limit] + ("..." if len(text) > limit else "")


def _execute(command: str, **ctx_kwargs):
    """Run *command* through the shared slash executor on the gateway surface."""
    from hermes_cli.slash_exec import CommandContext, execute_command
    return execute_command(command, CommandContext(surface="gateway", **ctx_kwargs))


def _restart_notify_payload(event: MessageEvent) -> dict:
    """Requester routing info so the new gateway process can notify them once back online.
    ``profile`` is persisted so the notice leaves through the requester's own profile bot after the
    restart (a bare platform lookup would resolve the default profile's adapter)."""
    source = event.source
    data = {"platform": source.platform.value if source.platform else None,
            "chat_id": source.chat_id, "chat_type": source.chat_type}
    if source.delivered_via_upstream_relay is True:
        data["delivered_via_upstream_relay"] = True
        data.update({k: getattr(source, k) for k in ("user_id", "scope_id") if getattr(source, k)})
    optional = (("thread_id", source.thread_id), ("message_id", event.message_id),
                ("profile", getattr(source, "profile", None)))
    data.update({k: v for k, v in optional if v})
    return data


def _spawn_detached_update(hermes_cmd, output_path, exit_code_path) -> None:
    """Spawn ``hermes update --gateway`` detached so it survives the gateway restart it may trigger.
    setsid is portable (works where ``systemd-run --user`` lacks a D-Bus session); ``--gateway``
    enables file-based IPC so interactive prompts are forwarded; PYTHONUNBUFFERED lets the gateway
    stream output live.  Windows has no setsid: an inline helper runs the updater as a module under
    this interpreter (not venv\\Scripts\\hermes.exe — that shim holds its own file open, and the
    update must replace it), redirects both outputs to one file and writes the exit code."""
    import shutil
    import subprocess
    if sys.platform == "win32":
        from hermes_cli._subprocess_compat import windows_detach_popen_kwargs
        subprocess.Popen(
            [sys.executable, "-c", _WINDOWS_UPDATE_HELPER, str(output_path), str(exit_code_path),
             sys.executable, "-m", "hermes_cli.main", "update", "--gateway"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **windows_detach_popen_kwargs())
        return
    hermes_cmd_str = " ".join(shlex.quote(part) for part in hermes_cmd)
    update_cmd = (
        f"PYTHONUNBUFFERED=1 {hermes_cmd_str} update --gateway"
        f" > {shlex.quote(str(output_path))} 2>&1; "
        # Avoid `status=$?`: `status` is read-only in zsh and this template is reused in
        # macOS/zsh operator wrappers, so keep it zsh-safe even though bash runs it here.
        f"rc=$?; printf '%s' \"$rc\" > {shlex.quote(str(exit_code_path))}")
    # Preferred: setsid creates a new session, fully detached; fallback start_new_session=True
    # calls os.setsid() in the child.
    setsid_bin = shutil.which("setsid")
    argv = [setsid_bin, "bash", "-c", update_cmd] if setsid_bin else ["bash", "-c", update_cmd]
    subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def _home_thread_from_source(source) -> Optional[str]:
    """The thread id /sethome should persist on the home target, or None.  Slack thread-per-message
    keying stamps a top-level message's own id as ``source.thread_id`` (a session key, not a
    location); persisting it would pin HOME to that ephemeral thread.  A thread id equal to the
    message's own id is synthetic and dropped; a real thread (id = parent's) is kept."""
    thread_id = getattr(source, "thread_id", None)
    if not thread_id:
        return None
    synthetic = (getattr(source, "platform", None) == Platform.SLACK and getattr(source, "message_id", None)
                 and str(thread_id) == str(source.message_id))
    return None if synthetic else str(thread_id)


# /compress transcript-row count above which an interim "⏳ compressing…" ack is
# sent before the blocking compression. Below this a compress finishes fast
# enough that an ack is just noise. A large tool-heavy session (hundreds of
# rows) can take minutes of chunked auxiliary summarization, during which the
# only signal to the user is the final result — so the ack disambiguates
# "working" from "hung". Chosen conservatively: well above a trivial chat but
# below the sessions that actually go slow.
_COMPRESS_PROGRESS_ACK_MIN_MESSAGES = 200

# Default for ``model.stale_code_switch_guard``. The guard trades a rare
# annoyance (a refused /model in the window between a deploy and a restart) for
# a mid-conversation ImportError crash, so it ships ON; operators who deploy
# often and accept the crash risk can turn it off in config.yaml.
_STALE_CODE_SWITCH_GUARD_DEFAULT = True


def _stale_code_switch_guard_enabled() -> bool:
    """Runtime-read toggle for the model-switch stale-code guard.

    Reads ``model.stale_code_switch_guard`` from ``config.yaml`` on every call
    (no restart needed to flip it). A missing key, a malformed section, or any
    read failure uses the safe default (guard ON) — a config problem must never
    silently disarm a crash guard.
    """
    try:
        from hermes_cli.config import read_raw_config

        raw = read_raw_config() or {}
        model_cfg = raw.get("model", {}) if isinstance(raw, dict) else {}
        if not isinstance(model_cfg, dict):
            logger.warning(
                "Malformed model config section while reading "
                "stale_code_switch_guard; defaulting to enabled"
            )
            return _STALE_CODE_SWITCH_GUARD_DEFAULT
        value = model_cfg.get(
            "stale_code_switch_guard", _STALE_CODE_SWITCH_GUARD_DEFAULT
        )
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "on"}:
                return True
            if normalized in {"false", "0", "no", "off"}:
                return False
        logger.warning(
            "Malformed model.stale_code_switch_guard (%r); defaulting to enabled",
            value,
        )
        return _STALE_CODE_SWITCH_GUARD_DEFAULT
    except Exception as exc:
        logger.warning(
            "Could not read model.stale_code_switch_guard; defaulting to "
            "enabled (error=%s)",
            type(exc).__name__,
        )
        return _STALE_CODE_SWITCH_GUARD_DEFAULT


def _with_resolved_route(label: str, result: Any) -> str:
    """Append the resolved ``provider/model`` pair to a /model confirmation.

    The display label alone ("Claude BPX-5") hides a mis-split such as
    ``claude-apr`` + ``name:claude-bpx-5/x``; the literal pair makes it
    visible in the reply on every platform.
    """
    provider = str(getattr(result, "target_provider", "") or "").strip()
    model = str(getattr(result, "new_model", "") or "").strip()
    if not provider or not model:
        return label
    return f"{label} · `{provider}/{model}`"


_RESIDENT_UNKNOWN_FLAGS = (
    "input_tokens_unknown",
    "output_tokens_unknown",
    "cache_read_tokens_unknown",
    "cache_write_tokens_unknown",
    "usage_unknown",
)


def _resident_thin_snapshot(agent, as_int=None) -> dict:
    """The resident ``/usage`` lane's thin snapshot, WITH its UNKNOWN flags.

    The five ``session_*`` counters are plain ints: an unmeasured call adds the
    canonical ``0`` and leaves no trace. Handing the renderer those five keys
    alone made every UNKNOWN branch dead code on this lane, so a session whose
    provider returned no usage payload rendered ``Total (billed in+out): 0`` —
    an unmeasured value presented as a measurement, the exact defect class this
    work removes (r6 finding 8).

    The discriminators come from the ABSORBING SESSION-LEVEL latch
    (``agent.session_*_unknown``, set beside the counter increments in
    ``agent/conversation_loop.py``), NOT from ``agent.last_turn_usage``.
    ``last_turn_usage`` is rewritten on every provider call, so it carries the
    last CALL's provenance; stamping it onto a cumulative total was wrong in
    both directions (r6 round-4 finding 7):

    1. earlier call unmeasured, final call measured → no flag → an exact-looking
       ``Total (billed in+out): 412,338`` that silently omits real spend, which
       is finding 8 displaced by one call; and
    2. final call unmeasured, 99 earlier calls measured → every row collapses to
       ``unknown``, discarding hundreds of thousands of measured tokens.

    A flag describing an aggregate has to be latched over that same aggregate.
    """
    if as_int is None:
        def _coerce(v):
            try:
                return int(v or 0)
            except (TypeError, ValueError):
                return 0
        as_int = _coerce

    snap = {
        "input_tokens": as_int(getattr(agent, "session_input_tokens", 0)),
        "output_tokens": as_int(getattr(agent, "session_output_tokens", 0)),
        "cache_read_tokens": as_int(getattr(agent, "session_cache_read_tokens", 0)),
        "cache_write_tokens": as_int(getattr(agent, "session_cache_write_tokens", 0)),
        "reasoning_tokens": as_int(getattr(agent, "session_reasoning_tokens", 0)),
    }
    for flag in _RESIDENT_UNKNOWN_FLAGS:
        # `is True`, not truthiness. The latch is written as a real bool by
        # `agent/agent_init.py` (False) and `agent/conversation_loop.py` (True),
        # so any OTHER value means the attribute was never initialised on this
        # object — and the dominant such object is a test double. A bare
        # `getattr(..., False)` reads a `MagicMock`'s auto-created child
        # attribute as TRUTHY, which collapsed every measured session counter to
        # `unknown` on the resident lane (tests/gateway/test_usage_command.py).
        # The five counters above are coerced through `as_int` for the same
        # reason; this is the flags' half of that contract.
        if getattr(agent, f"session_{flag}", False) is True:
            snap[flag] = True
    return snap


def render_thin_last_turn_lines(thin_snap, fallback_label=None) -> list:
    """Render the degraded /usage last-turn card from a thin usage snapshot.

    Module-level and callable so tests drive the SHIPPED renderer directly
    instead of AST-lifting it out of the mixin method below (a source-text
    anchor that both breaks on benign refactors and can go green against a
    fixture no producer emits).

    ``thin_snap`` is whatever the two real producers hand over:
    ``HermesState.get_last_turn_usage`` (persisted, agent evicted) or the
    resident agent's session counters.

    The RESIDENT producer carries the UNKNOWN discriminators, so an unmeasured
    bucket renders ``unknown`` here rather than presenting the stored 0 as a
    measurement. The PERSISTED producer does NOT: the sessions schema has no
    ``last_turn_*_unknown`` columns (``hermes_state_common.py``), and
    ``get_last_turn_usage`` returns exactly five integer keys. Adding them is a
    schema change owned by PR #797. What this lane guarantees instead is that
    the persisted snapshot is never an unmeasured zero in the first place:
    ``conversation_loop._last_turn_snapshot_kwargs`` writes ``None`` for an
    unknown call, so ``COALESCE`` retains the last REAL split rather than
    stamping a measured-looking 0 over it. This renderer therefore only ever
    sees measured values on the persisted lane, and the flag lookups below are
    a no-op there by construction, not by guarantee.
    """
    from agent.usage_pricing import format_token_count, prompt_tokens_unknown

    def _as_int(v):
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0
    lt_in = _as_int(thin_snap.get("input_tokens"))
    lt_out = _as_int(thin_snap.get("output_tokens"))
    lt_cr = _as_int(thin_snap.get("cache_read_tokens"))
    lt_cw = _as_int(thin_snap.get("cache_write_tokens"))
    lt_rsn = _as_int(thin_snap.get("reasoning_tokens"))
    in_billed = lt_in + lt_cr + lt_cw
    out_billed = lt_out + lt_rsn  # fold reasoning into the output total
    out_label = fallback_label or "persisted; agent not resident"
    out_lines = [f"📊 **Last turn** ({out_label})"]

    # Route only the UNKNOWN case through the shared rule; keep this card's
    # own comma formatting for measured values via ``formatter``.
    def _tok(value: int, *, unknown: bool) -> str:
        return format_token_count(value, unknown=unknown, formatter=lambda v: f"{v:,}")

    input_unknown = prompt_tokens_unknown(thin_snap)
    output_unknown = bool(thin_snap.get("output_tokens_unknown") or thin_snap.get("usage_unknown"))
    total_unknown = bool(
        input_unknown or output_unknown or thin_snap.get("total_tokens_unknown")
    )
    if input_unknown:
        out_lines.append(f"• Tokens in: {_tok(in_billed, unknown=True)}")
    elif in_billed:
        out_lines.append(
            f"• Tokens in: {in_billed:,} billed "
            f"({lt_cr:,} cache-read + {lt_cw:,} cache-write + {lt_in:,} uncached)"
        )
    if out_billed or output_unknown:
        out_lines.append(f"• Tokens out: {_tok(out_billed, unknown=output_unknown)} billed")
    out_lines.append(
        f"• Total (billed in+out): "
        f"{_tok(in_billed + out_billed, unknown=total_unknown)}"
    )
    return out_lines


class GatewaySlashCommandsMixin(
    GatewayLoginCommandsMixin,
    GatewayModelCommandsMixin,
    GatewaySessionCommandsMixin,
    GatewayStatusCommandsMixin,
    GatewayGoalCommandsMixin):
    """In-session slash-command handlers for GatewayRunner (plus the helpers the sibling mixins share)."""

    async_session_store: AsyncSessionStore

    # ------------------------------------------------------------------ shared helpers
    def _cached_agent_for(self, session_key: str, *, lockless_fallback: bool = False):
        """Peek the cached AIAgent for *session_key* without evicting it, or None. Entries are
        ``(agent, signature, ...)`` tuples (bare agents from test doubles accepted). Historical callers
        read the cache ONLY under ``_agent_cache_lock`` and got None when a fixture that skipped
        ``__init__`` had no lock; the manual codex ``/compress`` path was the one exception that read
        lock-free (``lockless_fallback=True``)."""
        cache = getattr(self, "_agent_cache", None)
        lock = getattr(self, "_agent_cache_lock", None)
        if cache is None or (lock is None and not lockless_fallback):
            return None
        try:
            if lock:
                with lock:
                    entry = cache.get(session_key)
            else:
                entry = cache.get(session_key)
        except Exception:
            return None
        return (entry[0] if entry else None) if isinstance(entry, (tuple, list)) else entry or None

    def _resident_agent_for(self, session_key: str):
        """The live running agent for *session_key*, else the cached one, else None. The pending
        sentinel (a run that is starting) never counts as a usable agent."""
        from gateway.run import _AGENT_PENDING_SENTINEL
        agent = self._running_agents.get(session_key)
        if agent is not None and agent is not _AGENT_PENDING_SENTINEL:
            return agent
        return self._cached_agent_for(session_key)

    @staticmethod
    def _session_db_unavailable_reply() -> str:
        from hermes_state import format_session_db_unavailable
        return format_session_db_unavailable(prefix=t("gateway.shared.session_db_unavailable_prefix"))

    def _reply_metadata(self, event: MessageEvent):
        """Thread/reply metadata for an outbound send anchored on *event*."""
        return self._thread_metadata_for_source(event.source, self._reply_anchor_for_event(event))

    def _adapter_and_key_for(self, event: MessageEvent):
        """``(adapter, session_key)`` for the event's source, either None when no source. The source's
        OWN transport (profile-aware, fail-closed) — ``self.adapters`` is the default profile's map."""
        if not event.source:
            return None, None
        return self._delivery_adapter_for(event.source), self._session_key_for_source(event.source)

    def _telegramized_command_reply(self, event: MessageEvent, text: str) -> str:
        from gateway.run import _telegramize_command_mentions
        return _telegramize_command_mentions(text, getattr(getattr(event, "source", None), "platform", None))

    def _checkpoint_manager(self):
        """A CheckpointManager from gateway config, or None when checkpoints are disabled."""
        from gateway.run import _checkpoint_agent_kwargs, _load_gateway_config
        from tools.checkpoint_manager import CheckpointManager
        cp = _checkpoint_agent_kwargs(_load_gateway_config())
        if not cp["checkpoints_enabled"]:
            return None
        # AIAgent kwargs are ``checkpoint_<field>``; CheckpointManager takes the bare field names.
        fields = {k[len("checkpoint_"):]: v for k, v in cp.items() if k.startswith("checkpoint_")}
        return CheckpointManager(enabled=True, **fields)

    def _write_approval_setter(self, section: str, event: MessageEvent):
        """``set_mode_fn`` for /memory and /skills: persist ``<section>.write_approval``. Raw read is
        correct for the write-back round-trip (merged defaults must not be persisted back to the
        user's file); the cached agent is dropped so the setting takes effect next message."""
        from gateway.run import _gateway_config_home
        # Persist to config (default) unless --session opted out, mirroring the text /model command path
        # above so a picked model survives across sessions like a typed one (#49066).
        from hermes_cli.config import read_user_config_raw
        config_path = _gateway_config_home() / "config.yaml"
        session_key = self._session_key_for_source(event.source)

        def _set_approval(enabled: bool):
            user_config = read_user_config_raw(config_path)
            user_config.setdefault(section, {})["write_approval"] = bool(enabled)
            atomic_config_write(config_path, user_config)
            # Evict any cached agent for this session so the next message rebuilds with the correct
            # session_id end-to-end — mirrors /branch and /reset. Without this, the cached AIAgent (and its
            # memory provider, which cached `_session_id` during initialize()) keeps writing into the wrong
            # session's record. See #6672.
            self._evict_cached_agent(session_key)
        return _set_approval

    async def _deliver_approval_confirmation(self, event: MessageEvent, confirmation_text: str, verb: str):
        """Return *confirmation_text* for normal delivery, or push it on native-streaming adapters
        (WeCom msgtype:"stream"), which need it sent directly with control-lane metadata (reliable
        proactive send, not the finalized reply stream). ``is not True``: mocks auto-create attrs."""
        source = event.source
        adapter = self._delivery_adapter_for(source)  # the receiving bot, not the default profile's
        if adapter:
            adapter.resume_typing_for_chat(source.chat_id)  # agent is about to continue
        if getattr(adapter, "SUPPORTS_NATIVE_STREAMING", False) is not True:
            return confirmation_text
        if adapter:
            try:
                await adapter.send(
                    source.chat_id, confirmation_text, reply_to=event.message_id,
                    metadata={"is_approval_prompt": True, "force_proactive_send": True})
            except Exception as exc:
                logger.warning("Failed to send /%s confirmation to %s: %s", verb, source.chat_id,
                               exc, exc_info=True)
        return None

    def _typed_command_prefix_for(self, platform) -> str:
        """The prefix users can always type to reach Hermes commands (adapter ``typed_command_prefix``,
        default "/"). Slack and Matrix use "!" because typed "/" is blocked/reserved there; their
        adapters rewrite "!command" to "/command"."""
        adapter = self.adapters.get(platform) if getattr(self, "adapters", None) else None
        return getattr(adapter, "typed_command_prefix", "/") if adapter is not None else "/"

    def _terminal_cwd(self) -> str:
        from tools.terminal_scope import terminal_env
        return terminal_env("TERMINAL_CWD", str(Path.home()))

    @staticmethod
    def _display_config_target(event: MessageEvent):
        """``(config.yaml path, platform config key)`` for the per-platform display settings."""
        from gateway.run import _gateway_config_home, _platform_config_key
        return _gateway_config_home() / "config.yaml", _platform_config_key(event.source.platform)

    async def _handle_profile_command(self, event: MessageEvent) -> str:
        """Handle /profile — show the profile serving this source and its home.  On a multiplexed
        gateway the process-level profile is the multiplexer's own ("default" in every chat), so
        with ``multiplex_profiles`` on report ``source.profile`` and resolve home under that
        profile's runtime scope; when off the stamp is ignored, mirroring ``_run_agent``."""
        from hermes_constants import display_hermes_home
        source = getattr(event, "source", None)
        profile_name = display = ""
        if getattr(getattr(self, "config", None), "multiplex_profiles", False):
            profile_name = (getattr(source, "profile", "") or "").strip()
            try:
                from gateway.run import _profile_runtime_scope
                with _profile_runtime_scope(self._resolve_profile_home_for_source(source)):
                    display = display_hermes_home()
            except Exception:
                display = display_hermes_home()

        # Shared executor resolves process-level fallbacks; the multiplexed per-source overrides
        # (when any) ride in via options.
        reply = _execute("profile", options={"profile_name": profile_name, "home_display": display})
        return "\n".join([t("gateway.profile.header", profile=reply.data["profile"]),
                          t("gateway.profile.home", home=reply.data["home"])])

    async def _handle_whoami_command(self, event: MessageEvent) -> str:
        """Handle /whoami — platform, DM-vs-group scope, tier and runnable commands (always allowed)."""
        from gateway.slash_access import policy_for_runner_source
        source = event.source
        policy = policy_for_runner_source(self, source)
        platform = source.platform.value if source and source.platform else "?"
        chat_type = ((source.chat_type if source else "") or "dm").lower()
        scope = t("gateway.whoami.scope_dm" if chat_type in {"dm", "direct", "private", ""} else "gateway.whoami.scope_group")
        user_id = (source.user_id if source else None) or "?"
        head = t("gateway.whoami.header", platform=platform, scope=scope, user_id=user_id)
        if not policy.enabled:
            return head + t("gateway.whoami.tier_unrestricted")
        if policy.is_admin(user_id):
            return head + t("gateway.whoami.tier_admin")
        # Non-admin: floor first (mirrors slash_access._ALWAYS_ALLOWED_FOR_USERS), then operator
        # additions, deduped in order.
        runnable = list(dict.fromkeys(["help", "whoami"] + sorted(policy.user_allowed_commands)))
        runnable_str = ", ".join(f"/{c}" for c in runnable) if runnable else t("gateway.shared.none_marker")
        return head + t("gateway.whoami.tier_user", commands=runnable_str)

    async def _handle_kanban_command(self, event: MessageEvent) -> str:
        """Handle /kanban — delegate to the shared kanban CLI (DB work in a thread pool). Allowed
        while an agent runs: the board is profile-agnostic and never touches agent state."""
        from hermes_cli.kanban import _HOME_GUARDED_ACTIONS, run_slash

        # Strip the leading "/kanban" (with or without slash), leaving args.
        text = (event.text or "").strip().lstrip("/")
        if text.startswith("kanban"):
            text = text[len("kanban"):].lstrip()
        requested_board = action = None
        tokens = iter(shlex.split(text) if text else [])
        for tok in tokens:  # leading --board/--board=<b> options, then the action verb
            if tok == "--board":
                requested_board = next(tokens, requested_board)
            elif tok.startswith("--board="):
                requested_board = tok.split("=", 1)[1]
            else:
                action = tok
                break
        is_create = action == "create"

        # The invoking chat session, passed EXPLICITLY: create stamps it as the
        # card's home and the home-session guard compares against it. Never
        # read from env here -- os.environ is shared by every session.
        # Read-only lookup through the ONE awaited store boundary (never a
        # sync store call on the event loop, never get_or_create).
        invoking_session_id = None
        try:
            _entry = await self.async_session_store.entry_for(
                self._session_key_for_source(event.source)
            )
            invoking_session_id = getattr(_entry, "session_id", None) or None
        except Exception as exc:
            logger.warning(
                "/kanban: could not resolve invoking session (%s); "
                "running sessionless -- home-session guard skipped, create "
                "leaves the card unstamped",
                exc,
            )
            invoking_session_id = None

        # Fail CLOSED: without the invoking session a mutation would skip the
        # home-session guard and ``create`` would leave its card unstamped
        # (first-contact chat, store not loaded, or a lookup error).
        if invoking_session_id is None and (
            is_create or action in _HOME_GUARDED_ACTIONS
        ):
            logger.warning(
                "/kanban %s refused: session-unresolved (no invoking session)", action
            )
            return (
                f"/kanban {action} refused (session-unresolved): this chat's "
                "session could not be resolved, so the card's home-session "
                "guard cannot run. Send any message to open the session, then retry."
            )

        if action == "dashboard":
            from gateway.kanban_dashboard_link import dashboard_link

            # dashboard_link reads config.yaml: sync file I/O, keep it off the loop.
            link = await asyncio.to_thread(dashboard_link, invoking_session_id)
            return link or "Dashboard link unavailable: configure dashboard.public_url."

        try:
            output = await asyncio.to_thread(
                run_slash, text, session_id=invoking_session_id
            )
        except Exception as exc:  # pragma: no cover - defensive
            return t("gateway.kanban.error_prefix", error=exc)

        # Auto-subscribe on create, parsing the task id from the CLI's standard success line
        # ("Created t_abcd  (ready, ...)"). With --json there is no such line, so a scripting user
        # gets no subscription and can call /kanban notify-subscribe explicitly.
        m = re.search(r"Created\s+(t_[0-9a-f]+)\b", output) if is_create and output else None
        if m:
            task_id = m.group(1)
            try:
                if await self._kanban_auto_subscribe(event, task_id, requested_board):
                    output = output.rstrip() + "\n" + t("gateway.kanban.subscribed_suffix", task_id=task_id)
            except Exception as exc:
                logger.warning("kanban create auto-subscribe failed: %s", exc)

        # Gateway messages have practical length caps; truncate long listings.
        if len(output) > 3800:
            output = output[:3800] + "\n" + t("gateway.kanban.truncated_suffix")
        return output or t("gateway.kanban.no_output")

    async def _kanban_auto_subscribe(self, event: MessageEvent, task_id: str, requested_board) -> bool:
        """Subscribe the event's chat to *task_id* notifications (notify+wake). False when the
        source has no platform/chat to route back to."""
        source = event.source

        def _field(name: str) -> Optional[str]:
            return str(getattr(source, name, "") or "") or None
        platform = getattr(source, "platform", None)
        platform_str = (platform.value if hasattr(platform, "value") else str(platform or "")).lower()
        chat_id, chat_type = _field("chat_id"), _field("chat_type")
        # Canonicalize on WRITE (see tools/kanban_tools.py): a Discord guild chat is keyed
        # 'group', so persisting the raw 'channel' envelope spelling writes a row that cannot
        # match its own chat's routing entry -> identity-less wake -> phantom session
        # (2026-09-12; readers fixed in #682).
        if chat_type:
            try:
                from gateway.routing_identity import canonical_chat_type
                chat_type = canonical_chat_type(platform_str, chat_type)
            except Exception:  # pragma: no cover - never block a sub
                pass
        delivery_metadata = self._thread_metadata_for_source(
            source, self._reply_anchor_for_event(event)
        ) or None
        if isinstance(delivery_metadata, dict) and chat_type:
            delivery_metadata.setdefault("chat_type", chat_type)
        if not (platform_str and chat_id):
            return False

        def _sub():
            from hermes_cli import kanban_db_connect as _kbc
            from hermes_cli import kanban_db_notify as _kbn
            conn = _kbc.connect(board=requested_board)
            try:
                _kbn.add_notify_sub(
                    conn, task_id=task_id, platform=platform_str, chat_id=chat_id, chat_type=chat_type,
                    thread_id=_field("thread_id"), user_id=_field("user_id"),
                    # The two session-key inputs user_id cannot express: build_session_key keys
                    # the participant on ``user_id_alt or user_id`` (feishu/signal/dingtalk carry
                    # the canonical id in the alt slot) and prefixes the Slack workspace scope
                    # before chat_id. Persisting them is what lets the wake rebuild the creator's
                    # OWN key instead of a second, chat-unreachable one.
                    user_id_alt=_field("user_id_alt"),
                    scope_id=_field("scope_id"),
                    notifier_profile=_field("profile") or getattr(self, "_kanban_notifier_profile", None) or self._active_profile_name(),
                    # Subscribing from chat: deliver the passive message and wake the destination agent.
                    delivery_mode="notify+wake", delivery_metadata=delivery_metadata)
            finally:
                conn.close()
        await asyncio.to_thread(_sub)
        return True

    async def _handle_stop_command(self, event: MessageEvent) -> Union[str, EphemeralReply]:
        """Handle /stop command - interrupt a running agent.  A truly hung agent (blocked thread
        never checking _interrupt_requested) is caught by the early intercept in _handle_message();
        this handler runs via normal dispatch or as a fallback, and force-cleans the session lock in
        all cases.  The session is preserved so the user can continue."""
        from gateway.run import _AGENT_PENDING_SENTINEL, _INTERRUPT_REASON_STOP
        source = event.source
        session_entry = await self.async_session_store.get_or_create_session(source)
        session_key = session_entry.session_key

        async def _stop(key: str, invalidation_reason: str) -> None:
            await self._interrupt_and_clear_session(
                key, source, interrupt_reason=_INTERRUPT_REASON_STOP,
                invalidation_reason=invalidation_reason)
        agent = self._running_agents.get(session_key)
        if agent is _AGENT_PENDING_SENTINEL:  # force-clean the sentinel so the session is unlocked
            await _stop(session_key, "stop_command_pending")
            logger.info("STOP (pending) for session %s — sentinel cleared", session_key)
            return EphemeralReply(t("gateway.stop.stopped_pending"))
        if agent:  # force-clean the session lock so a truly hung agent doesn't keep it forever
            await _stop(session_key, "stop_command_handler")
            return EphemeralReply(t("gateway.stop.stopped"))

        # No run under the caller's own key: a live turn in THIS chat may still carry a differently
        # shaped key. One scan feeds both tiers; the chat tier is a superset of the thread-sibling
        # tier (a sibling needs the caller's own thread slot, which satisfies the chat predicate), so
        # it is the set to act on — acting on the sibling subset alone would reply "Stopped" while a
        # same-thread run under a differently shaped key kept going. See `_chat_scoped_run_keys` for
        # the shapes and isolation bounds; both tiers are authorization-gated.
        runs = self._same_chat_runs(source, session_key)
        sibling_keys = self._sibling_thread_run_keys(source, runs)
        fallback_keys = self._chat_scoped_run_keys(source, runs)
        # Reason is per-stop, not per-key: a stop that only ever had thread siblings keeps its own
        # label for hook consumers, anything wider is a chat-scope stop.
        reason = (
            "stop_command_thread_sibling"
            if fallback_keys == sibling_keys
            else "stop_command_chat_scope"
        )
        if fallback_keys and self._is_user_authorized_for_source(source):
            for fallback_key in fallback_keys:
                await _stop(fallback_key, reason)
            logger.info("STOP (%s) by %s — interrupted %d run(s): %s",
                        reason, session_key, len(fallback_keys), ", ".join(fallback_keys))
            return EphemeralReply(t("gateway.stop.stopped"))

        # Detached delegations can outlive the parent turn, leaving no resident
        # agent even though this session still owns cancellable work.
        try:
            from tools.async_delegation import interrupt_for_session

            async_stopped = interrupt_for_session(
                session_key=session_key,
                parent_session_id=str(getattr(session_entry, "session_id", "") or ""),
                reason="stop_command",
            )
        except Exception:
            logger.debug(
                "Failed to cancel background delegations for session %s",
                session_key,
                exc_info=True,
            )
            async_stopped = 0
        if async_stopped:
            return EphemeralReply(t("gateway.stop.stopped"))

        # No running agent anywhere for this scope. A platform status
        # indicator can still be stuck — e.g. Slack's persistent
        # assistant.threads.setStatus survives a gateway restart or a turn
        # that died without a final send (#32295). Best-effort clear so
        # /stop always dismisses a phantom "is thinking...".
        adapter = getattr(self, "adapters", {}).get(source.platform)
        try:
            if adapter and hasattr(adapter, "_stop_typing_with_metadata"):
                await adapter._stop_typing_with_metadata(source.chat_id, self._reply_metadata(event))
        except Exception:
            logger.debug("Failed to clear typing on /stop with no active agent", exc_info=True)
        return t("gateway.stop.no_active")

    async def _handle_platform_command(self, event: MessageEvent) -> str:
        """Handle ``/platform list|pause|resume [name]`` — inspect and manually control failed/paused
        adapters (pause stops the reconnect watcher; resume re-queues for retry)."""
        # Strip the leading "/platform" (or "/PLATFORM") token if present
        parts = (getattr(event, "content", "") or "").strip().split(maxsplit=2)
        if parts and parts[0].lower().lstrip("/").startswith("platform"):
            parts = parts[1:]
        action = (parts[0] if parts else "list").lower()
        target = parts[1].lower() if len(parts) > 1 else ""
        failed = getattr(self, "_failed_platforms", {}) or {}
        if action == "list":
            connected = ", ".join(sorted(p.value for p in self.adapters)) or t("gateway.shared.none_marker")
            lines = [t("gateway.platform.header"), t("gateway.platform.connected", platforms=connected)]
            for p, info in failed.items():
                if info.get("paused"):
                    reason = info.get("pause_reason") or t("gateway.platform.reason_default")
                    lines.append(t("gateway.platform.item_paused", name=p.value, reason=reason))
                else:
                    lines.append(t("gateway.platform.item_retrying", name=p.value, attempts=info.get("attempts", 0)))
            return "\n".join(lines + ([] if failed else [t("gateway.platform.none_failed")]))
        if action not in {"pause", "resume"}:
            return t("gateway.platform.usage")
        if not target:
            return t("gateway.platform.usage_action", action=action)
        # Resolve platform name (case-insensitive, value match)
        platform = next((p for p in Platform.__members__.values() if p.value.lower() == target), None)
        if platform is None:
            return t("gateway.platform.unknown", name=target)
        name = platform.value
        queued = platform in failed
        paused = queued and bool(failed[platform].get("paused"))
        if action == "pause":
            if not queued:
                return t("gateway.platform.not_queued_pause", name=name)
            if paused:
                return t("gateway.platform.already_paused", name=name)
            self._pause_failed_platform(platform, reason=t("gateway.platform.reason_manual"))
            return t("gateway.platform.paused", name=name)
        if not queued:
            return t("gateway.platform.not_queued_resume", name=name)
        if not paused:
            return t("gateway.platform.already_retrying", name=name)
        self._resume_paused_platform(platform)
        return t("gateway.platform.resumed", name=name)

    async def _handle_restart_command(self, event: MessageEvent) -> Union[str, EphemeralReply]:
        """Handle /restart command - drain active work, then restart the gateway."""
        from gateway.run import _hermes_home
        # Idempotency check: if the previous gateway process recorded this same /restart (platform +
        # update_id) and we see it *again*, it's a redelivery from PTB's graceful-shutdown get_updates
        # ACK failing on the way out. Ignoring it prevents a loop where every fresh gateway re-restarts.
        if self._is_stale_restart_redelivery(event):
            src = event.source
            logger.info("Ignoring redelivered /restart (platform=%s, update_id=%s) — "
                        "already processed by a previous gateway instance.",
                        src.platform.value if src and src.platform else "?",
                        event.platform_update_id)
            return ""
        if self._restart_requested or self._draining:
            count = self._running_agent_count()
            return t("gateway.draining", count=count) if count else EphemeralReply(t("gateway.restart.in_progress"))

        async def _write_marker(name: str, build, label: str) -> None:
            try:
                await asyncio.to_thread(atomic_json_write, _hermes_home / name, build(), indent=None)
            except Exception as e:
                logger.debug("Failed to write restart %s: %s", label, e)

        def _notify_payload() -> dict:
            data = _restart_notify_payload(event)
            mid = str(event.message_id) if event.message_id is not None else event.source.message_id
            try:
                self._restart_command_source = dataclasses.replace(event.source, message_id=mid)
            except Exception:
                self._restart_command_source = event.source
            return data

        def _dedup_payload() -> dict:
            # Platform + update_id of the triggering /restart, for redelivery detection.
            data = {"platform": event.source.platform.value if event.source.platform else None,
                    "requested_at": time.time()}
            if event.platform_update_id is not None:
                data["update_id"] = event.platform_update_id
            return data

        # Save the requester's routing info so the new gateway process can notify them once back.
        await _write_marker(".restart_notify.json", _notify_payload, "notify file")
        # Record the triggering platform + update_id in a dedicated dedup marker. Unlike
        # .restart_notify.json (unlinked once the new gateway sends its notification) this persists
        # so a delayed Telegram redelivery is still detectable. Overwritten on every /restart.
        await _write_marker(".restart_last_processed.json", _dedup_payload, "dedup marker")
        active_agents = self._running_agent_count()
        # Under a service manager (systemd/launchd) or Docker/Podman, exit 75 so the supervisor /
        # restart policy restarts us — detached setsid+bash fails there (systemd KillMode=mixed kills
        # the cgroup; tini exits with the gateway). The explicit marker covers ``sudo env -i`` wrappers.
        from gateway.restart import is_container_restart_context, is_gateway_supervisor_process
        via_service = is_gateway_supervisor_process() or is_container_restart_context()
        self.request_restart(detached=not via_service, via_service=via_service)
        # Track sessions that were active at shutdown for stuck-loop detection (#7536). On each restart, the
        # counter increments for sessions that were running. If a session hits the threshold (3 consecutive
        # restarts while active), the next startup auto-suspends it — breaking the loop.
        if active_agents:
            return t("gateway.draining", count=active_agents)
        return EphemeralReply(t("gateway.restart.restarting"))

    async def _handle_version_command(self, event: MessageEvent) -> str:
        """Handle /version — show the running Hermes Agent version."""
        return _execute("version").text

    def _catalog_options(self, event: MessageEvent) -> dict:
        """``allowed_commands`` for /help and /commands when the caller is a gated non-admin:
        the slash-access floor + ``user_allowed_commands`` (mirrors /whoami), so the catalog
        never advertises commands ``_check_slash_access`` would refuse. Admins / ungated -> {}."""
        from gateway.slash_access import policy_for_runner_source
        source = event.source
        # Partially-constructed runners (``GatewayRunner.__new__`` in tests) have no ``config``;
        # policy_for_source treats None as ungated.
        policy = policy_for_runner_source(self, source)
        if policy.enabled and not policy.is_admin(source.user_id if source else None):
            return {"allowed_commands": {"help", "whoami", *policy.user_allowed_commands}}
        return {}

    async def _handle_help_command(self, event: MessageEvent) -> str:
        """Handle /help command - list available commands."""
        return self._telegramized_command_reply(
            event, _execute("help", options=self._catalog_options(event)).text)

    async def _handle_commands_command(self, event: MessageEvent) -> str:
        # Page size is a surface parameter (Telegram messages are shorter).
        page_size = 15 if event.source.platform == Platform.TELEGRAM else 20
        options = {"page_size": page_size, **self._catalog_options(event)}
        reply = _execute("commands", args=event.get_command_args(), options=options)
        return self._telegramized_command_reply(event, reply.text)

    async def _handle_set_home_command(self, event: MessageEvent) -> str:
        """Handle /sethome command -- set the current chat as the platform's home channel."""
        from gateway.run import _home_target_env_var, _home_thread_env_var
        source = event.source
        platform_name = source.platform.value if source.platform else "unknown"
        chat_id = source.chat_id
        chat_name = source.chat_name or chat_id
        if source.platform is None:
            return t("gateway.set_home.save_failed", error=t("gateway.set_home.err_missing_platform"))
        via_relay = getattr(source, "delivered_via_upstream_relay", False) is True
        if via_relay:
            adapter_for_source = getattr(self, "_intake_adapter_for", None)
            relay_adapter = adapter_for_source(source) if callable(adapter_for_source) else None
            fronts_platform = getattr(relay_adapter, "fronts_platform", None)
            if (source.platform in {None, Platform.LOCAL, Platform.RELAY}
                    or not getattr(source, "user_id", None)
                    or not callable(fronts_platform) or not fronts_platform(source.platform)):
                return t("gateway.set_home.save_failed", error=t("gateway.set_home.err_relay_unauthenticated"))
        thread_id = _home_thread_from_source(source)
        home = HomeChannel(
            platform=source.platform, chat_id=str(chat_id), name=chat_name, thread_id=thread_id,
            user_id=str(source.user_id) if getattr(source, "user_id", None) else None,
            scope_id=str(source.scope_id) if getattr(source, "scope_id", None) else None)
        # config.yaml is canonical because it can persist the authenticated logical-target
        # provenance required by Relay after a restart.
        try:
            persist_home_channel(home, enabled_if_new=not via_relay)
        except Exception as e:
            return t("gateway.set_home.save_failed", error=e)
        # Preserve legacy home env vars for existing cron/setup consumers.
        try:
            from hermes_cli.config import save_env_value
            save_env_value(_home_target_env_var(platform_name), str(chat_id))
            save_env_value(_home_thread_env_var(platform_name), str(thread_id or ""))
        except Exception as e:
            logger.warning("Home config saved but legacy env persistence failed: %s", e)
        # Keep the running gateway config in sync too. The pre-restart notification path reads
        # self.config before the process reloads config.
        platform_config = self.config.platforms.setdefault(source.platform, PlatformConfig(enabled=not via_relay))
        platform_config.home_channel = home
        return t("gateway.set_home.success", name=chat_name, chat_id=chat_id)

    async def _handle_voice_command(self, event: MessageEvent) -> str:
        """Handle /voice [on|off|tts|channel|leave|status] command."""
        args = event.get_command_args().strip().lower()
        chat_id = event.source.chat_id
        # Voice state belongs to the (bot, chat) pair: resolve the adapter that received the
        # command and key the mode by its owning profile so two multiplexed bots in one chat keep
        # independent /voice state.
        # See #75198.
        voice_key = self._voice_key_for_source(event.source)
        adapter = self._delivery_adapter_for(event.source)

        def _set_mode(mode: str) -> None:
            self._voice_mode[voice_key] = mode
            self._save_voice_modes()
            if not adapter:
                return
            if mode == "off":
                self._set_adapter_auto_tts_disabled(adapter, chat_id, disabled=True)
            else:
                self._set_adapter_auto_tts_enabled(adapter, chat_id, enabled=True)

        if args in _VOICE_MODE_BY_ARG:
            mode, reply_key = _VOICE_MODE_BY_ARG[args]
            _set_mode(mode)
            return t(reply_key)
        if args in {"channel", "join"}:
            return await self._handle_voice_channel_join(event)
        if args == "leave":
            return await self._handle_voice_channel_leave(event)
        if args == "status":
            mode = self._voice_mode.get(voice_key, "off")
            label = t(f"gateway.voice.label_{mode}") if mode in ("off", "voice_only", "all") else mode
            lines = [t("gateway.voice.status_mode", label=label)]
            guild_id = self._get_guild_id(event)  # append voice channel info if connected
            info = adapter.get_voice_channel_info(guild_id) if guild_id and hasattr(adapter, "get_voice_channel_info") else None
            if info:
                lines += [t("gateway.voice.status_channel", channel=info['channel_name']),
                          t("gateway.voice.status_participants", count=info['member_count'])]
                for m in info["members"]:
                    status = t("gateway.voice.speaking") if m.get("is_speaking") else ""
                    lines.append(t("gateway.voice.status_member", name=m['display_name'], status=status))
            return "\n".join(lines)

        # Toggle: off → on, on/all → off
        turning_on = self._voice_mode.get(voice_key, "off") == "off"
        _set_mode("voice_only" if turning_on else "off")
        toggle_line = t("gateway.voice.enabled_short" if turning_on else "gateway.voice.disabled_short")
        # Bare /voice still toggles, but append an explainer so users discover the on/off/tts/status
        # subcommands (and, on Discord, live voice-channel join/leave). Toggle result shows first.
        supports_voice_channels = adapter is not None and hasattr(adapter, "join_voice_channel")
        channels = t("gateway.voice.help_channels") if supports_voice_channels else ""
        return t("gateway.voice.help", toggle=toggle_line, channels=channels)

    async def _handle_rollback_command(self, event: MessageEvent) -> str:
        """Handle /rollback command — list or restore filesystem checkpoints."""
        from tools.checkpoint_manager import format_checkpoint_list
        mgr = self._checkpoint_manager()
        if mgr is None:
            return t("gateway.rollback.not_enabled")
        cwd = self._terminal_cwd()
        # --all / --force: classic full restore, overwriting user edits too.
        tokens = event.get_command_args().strip().split()
        restore_all = any(tok.lower() in ("--all", "--force") for tok in tokens)
        arg = " ".join(tok for tok in tokens if tok.lower() not in ("--all", "--force"))
        # Container-backed session: host checkpoints belong to another tree, so a restore is
        # refused; the bare listing stays visible, prefixed with the reason (same as the CLI).
        reason = mgr.unsupported_backend_reason()
        if reason and arg:
            return reason
        checkpoints = mgr.list_checkpoints(cwd)
        if not arg:
            listing = format_checkpoint_list(checkpoints, cwd)
            return f"{reason}\n{listing}" if reason else listing
        if not checkpoints:
            return t("gateway.rollback.none_found", cwd=cwd)

        # Restore by number or hash
        try:
            idx = int(arg) - 1
        except ValueError:
            target_hash = arg
        else:
            if not 0 <= idx < len(checkpoints):
                return t("gateway.rollback.invalid_number", max=len(checkpoints))
            target_hash = checkpoints[idx]["hash"]
        result = mgr.restore(cwd, target_hash, safe=not restore_all)
        if not result["success"]:
            return t("gateway.rollback.restore_failed", error=result["error"])
        msg = t("gateway.rollback.restored", hash=result["restored_to"], reason=result["reason"])
        for result_key, i18n_key in _ROLLBACK_SKIP_LINES:
            files = result.get(result_key) or []
            if files:
                more = f" (+{len(files) - 5})" if len(files) > 5 else ""
                msg += "\n" + t(i18n_key, files=", ".join(files[:5]) + more)
        return msg

    async def _handle_diff_command(self, event: MessageEvent) -> str:
        """Handle /diff — show git changes in the working directory.  Diff body is truncated hard
        here (chat is not a pager); platform senders clamp further."""
        args = [a.lower() for a in event.get_command_args().strip().split()]
        stat_only = bool({"--stat", "stat"} & set(args))
        mode = "working"
        for low in args:
            mode = _DIFF_MODE_BY_ARG.get(low, mode)
        cwd = self._terminal_cwd()
        if mode == "session":
            # Cumulative checkpoint-baseline diff.
            mgr = self._checkpoint_manager()
            if mgr is None:
                return t("gateway.diff.not_enabled")
            if reason := mgr.unsupported_backend_reason():  # host baseline is not this session's tree
                return reason
            result = await asyncio.to_thread(mgr.session_diff, cwd)
        else:
            from tools.working_diff import collect_working_diff
            result = await asyncio.to_thread(collect_working_diff, cwd, mode)
        if not result.get("success"):
            return t("gateway.diff.failed", error=result.get("error") or t("gateway.diff.err_default"))
        return self._render_diff_result(result, stat_only)

    def _render_diff_result(self, result: dict, stat_only: bool) -> str:
        """Render a working/session diff result: stat block, untracked list, fenced (truncated) diff."""
        stat = result.get("stat", "")
        diff = result.get("diff", "")
        untracked = result.get("untracked", [])
        if result.get("empty") or (not stat and not diff and not untracked):
            return t("gateway.diff.no_changes")
        out: list[str] = []
        if stat:
            out.append(f"```\n{stat}\n```")
        if untracked:
            shown = "\n".join(f"+ {rel}" for rel in untracked[:15])
            more = t("gateway.diff.more_untracked", count=len(untracked) - 15) if len(untracked) > 15 else ""
            out.append(f"{t('gateway.diff.untracked_label')}\n```\n{shown}{more}\n```")
        if not stat_only and diff:
            out.append(self._fenced_truncated_diff(diff))
        return "\n\n".join(out)

    @staticmethod
    def _fenced_truncated_diff(diff: str, max_lines: int = 60, max_chars: int = 3000) -> str:
        """Fence a diff body, truncating to messaging-friendly size."""
        diff_lines = diff.splitlines()
        truncated = len(diff_lines) > max_lines
        if truncated:
            diff = "\n".join(diff_lines[:max_lines])
        if len(diff) > max_chars:
            diff = diff[:max_chars]
            truncated = True
        note = ""
        if truncated:
            note = t("gateway.diff.truncated_note", lines=len(diff_lines))
        return f"```diff\n{diff}{note}\n```"

    def _track_background_task(self, coro) -> None:
        """Fire-and-forget *coro*, keeping a strong ref in ``_background_tasks`` until it finishes."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _handle_background_command(self, event: MessageEvent) -> str:
        """Handle /bg <prompt> — run a prompt in a background thread with its own session; the
        result is sent to the same chat without touching the active session's history."""
        prompt = event.get_command_args().strip()
        if not prompt:
            return t("gateway.background.usage")
        task_id = f"bg_{datetime.now().strftime('%H%M%S')}_{os.urandom(3).hex()}"
        self._track_background_task(self._run_background_task(
            prompt, event.source, task_id, event_message_id=self._reply_anchor_for_event(event),
            # Forward image/audio attachments so the background agent can see them.
            media_urls=list(event.media_urls or []), media_types=list(event.media_types or [])))
        return t("gateway.background.started", preview=_preview(prompt), task_id=task_id)

    async def _handle_btw_command(self, event: MessageEvent) -> str:
        """Handle /btw <question> — one-shot auxiliary LLM call on a transcript snapshot; live history
        is never touched (alternation + prompt cache intact, current turn keeps running). Unlike /bg,
        which spawns a fresh contextless session."""
        question = event.get_command_args().strip()
        if not question:
            return t("gateway.btw.usage")
        source = event.source
        session_entry = await self.async_session_store.get_or_create_session(source)
        try:
            history = await self.async_session_store.load_transcript(session_entry.session_id)
        except TranscriptReadError:
            return history_unreadable()
        if not history:
            return t("gateway.btw.no_history")
        try:
            # Off-loop: override rehydrate can re-resolve credentials and GET the
            # provider's /v1/models synchronously (t_515b7fce).
            model, rt = await asyncio.to_thread(self._resolve_session_agent_runtime, source=source)
        except Exception:
            model, rt = None, {}
        if not rt.get("api_key"):
            return t("gateway.btw.no_provider")
        main_runtime = {
            "model": model,
            **{k: rt.get(k) for k in ("provider", "base_url", "api_key", "api_mode")},
            "session_id": session_entry.session_id,
        }
        history_snapshot = list(history)
        # Prefer the cache-parity fork when a live cached AIAgent exists: it replays the snapshot
        # against the warm provider prefix cache, giving FULL context at cache-read prices. With no
        # cached agent the cache is cold anyway — answer_side_question's digest fallback handles it.
        try:
            parent_agent = self._cached_agent_for(self._session_key_for_source(source))
        except Exception:
            parent_agent = None
        _thread_metadata = self._reply_metadata(event)
        adapter = self._delivery_adapter_for(source)
        preview = _preview(question)

        async def _run_side_question() -> None:
            from agent.side_question import answer_side_question
            try:
                answer = await asyncio.to_thread(
                    answer_side_question, question, history_snapshot,
                    parent_agent=parent_agent, main_runtime=main_runtime)
                reply = t("gateway.btw.answer", preview=preview, answer=answer or "")
            except Exception as e:
                logger.warning("/btw side question failed: %s", e)
                reply = t("gateway.btw.failed", preview=preview, error=str(e))
            if adapter is not None:
                await adapter.send(source.chat_id, reply, metadata=_thread_metadata)

        self._track_background_task(_run_side_question())
        return t("gateway.btw.started", preview=preview)

    async def _handle_memory_command(self, event: MessageEvent) -> str:
        """Handle /memory — review pending memory writes + toggle the approval gate. Entries are small
        enough to review inline, so the full flow works on every platform."""
        from hermes_cli.write_approval_commands import handle_pending_subcommand
        from tools import write_approval as wa
        from tools.memory_tool import load_on_disk_store
        # Apply approved writes against a fresh on-disk store (the gateway has no long-lived agent;
        # the store persists to the same MEMORY/USER.md and honors the configured char limits).
        out = handle_pending_subcommand(
            wa.MEMORY, event.get_command_args().strip().split(), memory_store=load_on_disk_store(),
            set_mode_fn=self._write_approval_setter("memory", event))
        return out if out is not None else t("gateway.memory.unknown_subcommand")

    async def _handle_skills_command(self, event: MessageEvent) -> str:
        """Handle /skills on the gateway — pending skill-write review only (hub stays CLI-only). Gated
        by ``skills.write_approval`` but still answers when staged writes exist after the gate is off
        (never stranded). ``diff`` is truncated for chat."""
        from hermes_cli.write_approval_commands import handle_pending_subcommand
        from tools import write_approval as wa
        args = event.get_command_args().strip().split()
        sub = args[0].lower() if args else ""
        gate_off = not wa.write_approval_enabled(wa.SKILLS) and sub not in {"approval", "mode"}
        if gate_off and wa.pending_count(wa.SKILLS) == 0:
            return t("gateway.skills.gate_off")
        out = handle_pending_subcommand(
            wa.SKILLS, args, set_mode_fn=self._write_approval_setter("skills", event))
        if out is None:
            return t("gateway.skills.unknown_subcommand")

        # Chat bubbles can't hold a full skill diff — truncate and point at the pending JSON file
        # (NOT `hermes skills diff <name>`, which diffs a bundled skill against its stock version).
        if sub == "diff" and len(out) > 3000:
            pending_id = args[1] if len(args) > 1 else "<id>"
            out = out[:3000] + t("gateway.skills.diff_truncated", pending_id=pending_id)
        return out

    async def _handle_approvals_command(self, event: MessageEvent) -> str:
        """Show or persist the profile-wide dangerous-command approval mode."""
        from gateway.slash_access import policy_for_runner_source
        from hermes_cli.approval_mode import run_approval_mode_command
        requested = event.get_command_args().strip() or None
        # This mutates profile-wide security policy. The central slash gate can allow selected
        # commands to non-admin users, so enforce admin again at this side-effect boundary.
        # Unconfigured policies remain unrestricted.
        policy = policy_for_runner_source(self, event.source)
        if requested and not policy.is_admin(event.source.user_id):
            return t("gateway.approvals.admin_only")
        # Approval checks load config dynamically; do not evict the cached agent or alter its
        # system prompt/tool schema (prompt-cache prefix is sacred).
        return run_approval_mode_command(requested).message

    async def _handle_yolo_command(self, event: MessageEvent) -> Union[str, EphemeralReply]:
        """Handle /yolo — toggle dangerous command approval bypass for this session only."""
        from tools.approval import disable_session_yolo, enable_session_yolo, is_session_yolo_enabled
        session_key = self._session_key_for_source(event.source)
        if is_session_yolo_enabled(session_key):
            disable_session_yolo(session_key)
            return EphemeralReply(t("gateway.yolo.disabled"))
        enable_session_yolo(session_key)
        return EphemeralReply(t("gateway.yolo.enabled"))

    async def _handle_verbose_command(self, event: MessageEvent) -> str:
        """Handle /verbose — cycle tool progress display mode (off → new → all → verbose → log) per
        *current platform*, saved to ``display.platforms.<platform>.tool_progress``. Gated by
        ``display.tool_progress_command`` (default off)."""
        from gateway.run import _load_gateway_config
        config_path, platform_key = self._display_config_target(event)
        try:
            user_config = _load_gateway_config()
            gate_enabled = is_truthy_value(cfg_get(user_config, "display", "tool_progress_command"),
                                           default=False)
        except Exception:
            gate_enabled = False
        if not gate_enabled:
            return t("gateway.verbose.not_enabled")
        # Cycle mode (per-platform), reading the current effective mode via the resolver.
        from gateway.display_config import resolve_display_setting
        cycle = ["off", "new", "all", "verbose", "log"]
        current = resolve_display_setting(user_config, platform_key, "tool_progress", "all")
        new_mode = cycle[(cycle.index(current if current in cycle else "all") + 1) % len(cycle)]
        description = t(f"gateway.verbose.mode_{new_mode}")
        try:
            _write_raw_config_leaf(config_path, ("display", "platforms", platform_key, "tool_progress"), new_mode)
            return f"{description}\n" + t("gateway.verbose.saved_suffix", platform=platform_key)
        except Exception as e:
            logger.warning("Failed to save tool_progress mode: %s", e)
            return f"{description}\n" + t("gateway.verbose.save_failed", error=e)

    async def _handle_busy_command(self, event: MessageEvent) -> Union[str, EphemeralReply]:
        """Handle /busy — control what happens when messaging while Hermes is working."""
        arg = event.get_command_args().strip().lower()
        if not arg or arg == "status":
            mode = self._effective_busy_input_mode(event.source)
            behavior = t(_BUSY_MODE_BEHAVIOR.get(mode, _BUSY_MODE_BEHAVIOR["interrupt"])[0])
            return EphemeralReply(t("gateway.busy.status", mode=mode, behavior=behavior))
        if arg not in _BUSY_MODE_BEHAVIOR:
            return EphemeralReply(t("gateway.busy.unknown_mode", arg=arg))

        # Persist before mutate
        from cli import save_config_value
        if not save_config_value("display.busy_input_mode", arg):
            return EphemeralReply(t("gateway.busy.save_failed"))
        profile_name = self._busy_profile_name_for_source(event.source)
        if profile_name:
            from gateway.run import _load_gateway_config
            self._snapshot_profile_busy_modes(profile_name, _load_gateway_config())
        else:
            self._busy_input_mode = arg
            # busy_input_mode is also the source of truth for the text mode — re-derive it so the
            # adapter refresh below doesn't keep a stale value and keep interrupting.
            self._busy_text_mode = self._load_busy_text_mode()

        adapter = self._delivery_adapter_for(event.source)
        if adapter is not None:
            adapter._busy_text_mode = self._effective_busy_text_mode(event.source)
        return EphemeralReply(t("gateway.busy.set", mode=arg, behavior=t(_BUSY_MODE_BEHAVIOR[arg][1])))

    async def _handle_footer_command(self, event: MessageEvent) -> str:
        """Handle /footer command — toggle the runtime-metadata footer."""
        from gateway.run import _load_gateway_config, _resolve_gateway_model
        from gateway.runtime_footer import format_runtime_footer, resolve_footer_config
        config_path, platform_key = self._display_config_target(event)
        arg = ""
        try:
            text = (getattr(event, "message", None) or "").strip()
            if text.startswith("/"):
                parts = text.split(None, 1)
                arg = parts[1].strip().lower() if len(parts) > 1 else ""
        except Exception:
            arg = ""
        try:
            user_config: dict = _load_gateway_config()
        except Exception as e:
            return t("gateway.config_read_failed", error=e)
        effective = resolve_footer_config(user_config, platform_key)

        def _state(enabled: bool) -> str:
            return t("gateway.footer.state_on") if enabled else t("gateway.footer.state_off")
        if arg in {"status", "?"}:
            return t("gateway.footer.status", state=_state(effective["enabled"]),
                     fields=", ".join(effective.get("fields") or []), platform=platform_key)
        if arg and arg not in _FOOTER_STATE_BY_ARG:
            return t("gateway.footer.usage")
        new_state = _FOOTER_STATE_BY_ARG[arg] if arg else not effective["enabled"]
        try:
            _write_raw_config_leaf(config_path, ("display", "runtime_footer", "enabled"), new_state)
        except Exception as e:
            logger.warning("Failed to save runtime_footer.enabled: %s", e)
            return t("gateway.config_save_failed", error=e)
        example = ""
        if new_state:
            # Show a preview using current agent state if available.
            preview = format_runtime_footer(
                model=_resolve_gateway_model(user_config) or None, context_tokens=0, context_length=None,
                fields=effective.get("fields") or ["provider_model", "context_full", "cwd"])
            if preview:
                example = t("gateway.footer.example_line", preview=preview)
        return t("gateway.footer.saved", state=_state(new_state), example=example)

    async def _handle_reload_mcp_command(self, event: MessageEvent) -> Optional[str]:
        """Handle /reload-mcp — reconnect MCP servers and rebuild the cached agent. Reloading
        invalidates the provider prompt cache (tool schemas live in the system prompt), so it routes
        through slash-confirm; "Always Approve" persists ``approvals.mcp_reload_confirm: false``."""
        session_key = self._session_key_for_source(event.source)
        # Read the gate fresh from disk so a prior "always" click takes effect on the next
        # invocation without restarting the gateway.
        user_config = self._read_user_config()
        approvals = user_config.get("approvals") if isinstance(user_config, dict) else None
        if isinstance(approvals, dict) and not approvals.get("mcp_reload_confirm", True):
            return await self._execute_mcp_reload(event)
        # Route through slash-confirm. The primitive sends the prompt and stores the resume handler;
        # the button/text response triggers ``_resolve_slash_confirm`` which invokes the handler
        # with the chosen outcome.
        async def _on_confirm(choice: str) -> Optional[str]:
            if choice == "cancel":
                return t("gateway.reload_mcp.cancelled")
            if choice == "always":
                # Persist the opt-out and run the reload.
                try:
                    from cli import save_config_value
                    save_config_value("approvals.mcp_reload_confirm", False)
                    logger.info("User opted out of /reload-mcp confirmation (session=%s)", session_key)
                except Exception as exc:
                    logger.warning("Failed to persist mcp_reload_confirm=false: %s", exc)
            # once / always → run the reload
            result = await self._execute_mcp_reload(event)
            if choice == "always":
                return f"{result}\n\n" + t("gateway.reload_mcp.always_followup")
            return result
        return await self._request_slash_confirm(
            event=event, command="reload-mcp", title="/reload-mcp",
            message=t("gateway.reload_mcp.confirm_prompt"), handler=_on_confirm)

    async def _handle_reload_skills_command(self, event: MessageEvent) -> str:
        """Handle /reload-skills — rescan skills dir, queue a note for next turn. Skills are invoked at
        runtime, not baked into the system prompt, so this does NOT clear the prompt cache. The diff
        goes into ``_pending_skills_reload_notes[session_key]``, prepended to the NEXT user message —
        nothing out-of-band, so alternation is preserved."""
        try:
            from agent.skill_commands import reload_skills

            # _run_in_executor_with_context, not a bare hop: the rescan walks
            # get_hermes_home()/skills, a contextvar override under multiplex.
            result = await self._run_in_executor_with_context(reload_skills)
            try:
                from gateway.run import _invalidate_skill_slug_index

                # Off-loop: the invalidation takes _skill_slug_index_lock, which
                # a cold index build holds for its whole rglob walk (C5 #33,
                # PR #969). Blocking on it here would stall the event loop.
                await self._run_in_executor_with_context(_invalidate_skill_slug_index)
            except Exception:
                logger.debug("skill slug index invalidation failed", exc_info=True)
            added, removed = result.get("added", []), result.get("removed", [])  # [{"name", "description"}]
            total = result.get("total", 0)
            # Let adapters refresh platform-side state that cached the skill list at startup (today:
            # Discord /skill autocomplete — otherwise new skills stay invisible and deleted ones
            # error). Adapters without refresh_skill_group are skipped; the in-process reload suffices.
            for adapter in list(self.adapters.values()):
                refresh = getattr(adapter, "refresh_skill_group", None)
                if not callable(refresh):
                    continue
                try:
                    if inspect.iscoroutinefunction(refresh):
                        maybe = refresh()
                    else:
                        # Sync refreshes rescan the skill catalog on disk;
                        # keep that filesystem work off the event loop
                        # (t_620ba53d).
                        maybe = await asyncio.to_thread(refresh)
                    if inspect.isawaitable(maybe):
                        await maybe
                except Exception as exc:
                    logger.warning("Adapter %s refresh_skill_group raised: %s",
                                   getattr(adapter, "name", adapter), exc)

            lines = [t("gateway.reload_skills.header")]
            if not added and not removed:
                lines += [t("gateway.reload_skills.no_new"), t("gateway.reload_skills.total", count=total)]
                return "\n".join(lines)

            def _fmt_line(item: dict) -> str:
                nm, desc = item.get("name", ""), item.get("description", "")
                return (t("gateway.reload_skills.item_with_desc", name=nm, desc=desc) if desc
                        else t("gateway.reload_skills.item_no_desc", name=nm))

            # Queue a one-shot note for the next user turn in this session too. Format matches how
            # the system prompt renders pre-existing skills (``    - name: description``) so the
            # model reads the diff in the same shape as its original skill catalog.
            sections = ["[USER INITIATED SKILLS RELOAD:"]
            for i18n_key, note_header, items in (
                ("gateway.reload_skills.added_header", "Added Skills:", added),
                ("gateway.reload_skills.removed_header", "Removed Skills:", removed)):
                if items:
                    formatted = [_fmt_line(item) for item in items]
                    lines += [t(i18n_key)] + formatted
                    sections += ["", note_header] + formatted
            lines.append(t("gateway.reload_skills.total", count=total))
            sections += ["", "Use skills_list to see the updated catalog.]"]
            session_key = self._session_key_for_source(event.source)
            if not hasattr(self, "_pending_skills_reload_notes"):
                self._pending_skills_reload_notes = {}
            if session_key:
                self._pending_skills_reload_notes[session_key] = "\n".join(sections)
            return "\n".join(lines)
        except Exception as e:
            logger.warning("Skills reload failed: %s", e)
            return t("gateway.reload_skills.failed", error=e)

    async def _handle_bundles_command(self, event: MessageEvent) -> str:
        """Handle /bundles — list installed skill bundles (mirrors the CLI handler). Bundles are
        loaded by invoking their own ``/<slug>`` command, not by this one."""
        reply = _execute("bundles")
        if "error" in reply.data:
            logger.warning("Bundles command unavailable: %s", reply.data["error"])
            return reply.text
        bundles = reply.data["bundles"]
        if not bundles:
            return t("gateway.bundles.none", dir=reply.data["dir"])
        lines = [t("gateway.bundles.header", count=len(bundles)), ""]
        for info in bundles:
            skills = info.get("skills", [])
            desc = info.get("description") or t("gateway.bundles.default_desc", count=len(skills))
            lines += [t("gateway.bundles.item", slug=info["slug"], desc=desc, count=len(skills))] + [f"    · {s}" for s in skills]
        return "\n".join(lines + ["", t("gateway.bundles.invoke_hint")])

    def _blocking_approval_or_stale(self, event: MessageEvent, stale_key: str, none_key: str):
        """``(session_key, None)`` when an agent thread is blocked on approval, else the reply to send.
        A pending-approvals entry with no blocked thread is a stale prompt: drop it and say so."""
        from tools.approval import has_blocking_approval
        session_key = self._session_key_for_source(event.source)
        if has_blocking_approval(session_key):
            return session_key, None
        if session_key in self._pending_approvals:
            self._pending_approvals.pop(session_key)
            return session_key, t(stale_key)
        return session_key, t(none_key)

    async def _handle_approve_command(self, event: MessageEvent) -> Optional[str]:
        """Handle /approve — unblock waiting agent thread(s). They block inside tools/approval.py;
        signalling the event resumes them so the command executes inline (same flow as the CLI)."""
        from tools.approval import resolve_gateway_approval
        session_key, stale = self._blocking_approval_or_stale(event, "gateway.approval_expired",
                                                              "gateway.approve.no_pending")
        if stale:
            return stale
        # Args: "all", "all session", "all always", "session", "always" ("always" beats "session").
        args = event.get_command_args().strip().lower().split()
        choices = {_APPROVE_CHOICE_BY_ARG[a] for a in args if a in _APPROVE_CHOICE_BY_ARG}
        choice = "always" if "always" in choices else "session" if "session" in choices else "once"
        count = resolve_gateway_approval(session_key, choice, resolve_all="all" in args)
        if not count:
            return t("gateway.approve.no_pending")
        confirmation_text = t(f"gateway.approve.{choice}_{'plural' if count > 1 else 'singular'}", count=count)
        logger.info("User approved %d dangerous command(s) via /approve (%s)", count, choice)
        return await self._deliver_approval_confirmation(event, confirmation_text, "approve")

    async def _handle_deny_command(self, event: MessageEvent) -> str:
        """Handle /deny — reject pending dangerous command(s) with a definitive BLOCKED result, as in
        the CLI. ``/deny`` denies the oldest; ``/deny all`` denies everything.

        ``/deny <reason>`` (or ``/deny all <reason>``) attaches a one-line reason that is relayed back to
        the agent so it can adapt instead of only hearing "denied". Ported from qwibitai/nanoclaw#2832.
        """
        from tools.approval import resolve_gateway_approval
        session_key, stale = self._blocking_approval_or_stale(event, "gateway.deny.stale",
                                                              "gateway.deny.no_pending")
        if stale:
            return stale
        # A leading "all" denies every pending command; the rest (or the whole arg string without
        # "all") is the optional deny reason relayed to the agent, capped to a sane one-liner.
        raw_args = event.get_command_args().strip()
        tokens = raw_args.split()
        resolve_all = bool(tokens) and tokens[0].lower() == "all"
        reason = (raw_args[len(tokens[0]):].strip() if resolve_all else raw_args)[:280].strip()
        count = resolve_gateway_approval(session_key, "deny", resolve_all=resolve_all, reason=reason or None)
        if not count:
            return t("gateway.deny.no_pending")
        logger.info("User denied %d dangerous command(s) via /deny%s", count,
                    " (with reason)" if reason else "")
        key = "gateway.deny.denied" + ("_reason" if reason else "") + ("_plural" if count > 1 else "_singular")
        confirmation_text = t(key, count=count, reason=reason)
        return await self._deliver_approval_confirmation(event, confirmation_text, "deny")

    async def _handle_debug_command(self, event: MessageEvent) -> str:
        """Handle /debug — upload ONLY the summary (system info + log tails), never full logs, to
        protect privacy; ``hermes debug share`` from the CLI does full uploads."""
        from hermes_cli.debug import (_GATEWAY_PRIVACY_NOTICE, _best_effort_sweep_expired_pastes,
                                      _capture_dump, _is_dpaste_url, _schedule_auto_delete,
                                      collect_debug_report, upload_to_pastebin)

        def _collect_and_upload():  # blocking I/O (dump capture, log reads, uploads) -> thread
            _best_effort_sweep_expired_pastes()
            report = collect_debug_report(log_lines=200, dump_text=_capture_dump())
            try:
                urls = {t("gateway.debug.report_label"): upload_to_pastebin(report)}
            except Exception as exc:
                return t("gateway.debug.upload_failed", error=exc)
            _schedule_auto_delete(list(urls.values()))  # paste.rs only; dpaste.com has no delete
            label_width = max(len(k) for k in urls)
            # The 6-hour line is only true for paste.rs; the privacy notice above already states
            # the dpaste.com fallback retention, so drop the line rather than contradict it.
            auto_delete = [] if any(map(_is_dpaste_url, urls.values())) else [
                t("gateway.debug.auto_delete")]
            return "\n".join([_GATEWAY_PRIVACY_NOTICE, "", t("gateway.debug.header"), "",
                              *(f"`{label:<{label_width}}`  {url}" for label, url in urls.items()),
                              "", *auto_delete, t("gateway.debug.full_logs_hint"),
                              t("gateway.debug.share_hint")])

        # _run_in_executor_with_context, not a bare hop: this collects the profile's logs/config off
        # ``get_hermes_home()`` and uploads them to a public paste. Losing the contextvar override
        # would publish the DEFAULT profile's diagnostics from another profile's chat.
        return await self._run_in_executor_with_context(_collect_and_upload)

    async def _handle_update_command(self, event: MessageEvent) -> str:
        """Handle /update — spawn ``hermes update`` detached (``setsid``) so it survives the gateway
        restart it may trigger; marker files let this or the next gateway process notify the user."""
        import json
        from gateway.run import _hermes_home, _resolve_hermes_bin
        from hermes_cli.config import is_managed, format_managed_message
        # Block non-messaging platforms (API server, webhooks, ACP); plugin platforms with
        # allow_update_command=True are also allowed.
        src = event.source
        if src.platform not in self._UPDATE_ALLOWED_PLATFORMS:
            try:
                from gateway.platform_registry import platform_registry
                entry = platform_registry.get(src.platform.value)
                if not entry or not entry.allow_update_command:
                    return t("gateway.update.platform_not_messaging")
            except Exception:
                return t("gateway.update.platform_not_messaging")
        if is_managed():
            return f"✗ {format_managed_message(t('gateway.update.managed_action'))}"

        project_root = Path(__file__).parent.parent.resolve()

        # Not a git-managed install (docker/nix/desktop-app/source): refuse
        # with the steward's own update mechanism instead of git-pulling a
        # tree `hermes update` does not own.
        try:
            from hermes_cli.config import (
                detect_install_method,
                recommended_update_command_for_method,
            )

            method = detect_install_method(project_root)
            if method not in {"git", "unknown"}:
                return t("gateway.update.not_applicable", method=method,
                         command=recommended_update_command_for_method(method))
        except Exception:
            pass  # config unreadable — fall through to the .git check below

        git_dir = project_root / '.git'

        if not git_dir.exists():
            return t("gateway.update.not_git_repo")
        hermes_cmd = _resolve_hermes_bin()
        if not hermes_cmd:
            return t("gateway.update.hermes_cmd_not_found")
        pending_path = _hermes_home / ".update_pending.json"
        output_path = _hermes_home / ".update_output.txt"
        exit_code_path = _hermes_home / ".update_exit_code"
        pending = {
            "platform": src.platform.value, "chat_id": src.chat_id, "chat_type": src.chat_type,
            "user_id": src.user_id, "session_key": self._session_key_for_source(src),
            "timestamp": datetime.now().isoformat()}
        # ``profile``: the update watcher (possibly the NEXT gateway process) must answer through the
        # requester's own profile bot, not the default profile's adapter for the same platform.
        pending.update({k: v for k, v in (("thread_id", src.thread_id), ("message_id", event.message_id),
                                          ("profile", getattr(src, "profile", None))) if v})
        _tmp_pending = pending_path.with_suffix(".tmp")
        _tmp_pending.write_text(json.dumps(pending), encoding="utf-8")
        _tmp_pending.replace(pending_path)
        exit_code_path.unlink(missing_ok=True)
        try:
            _spawn_detached_update(hermes_cmd, output_path, exit_code_path)
        except Exception as e:
            pending_path.unlink(missing_ok=True)
            exit_code_path.unlink(missing_ok=True)
            return t("gateway.update.start_failed", error=e)
        self._schedule_update_notification_watch()
        return t("gateway.update.starting")

    # ------------------------------------------------------------------ fork overrides
    # Fleet behavior re-threaded onto the upstream mixin split (parity sync 2026-10-01, lane
    # R09-tui-gw). This class is first in the MRO, so a method here supersedes the sibling
    # mixin's copy: /undo (half-turn core + draining guard), /new sticky route preferences,
    # /branch Discord thread spawn + branch-point stamp (/merge), /compress progress ack +
    # manual trigger attribution, post-compaction context readings, /usage last-turn card +
    # compact account limits, /fast full-route validation + service-tier persistence,
    # /reasoning + /model switch announcements, /merge, /redo, /resume-handoff.

    async def _handle_undo_command(self, event: MessageEvent) -> str:
        """Handle /undo [N] by delegating to the shared half-turn undo core; evicts the cached agent
        so the next turn rebuilds from the active-only transcript."""
        source = event.source

        # Refuse while a /stop'd turn is still draining (still writing rows) —
        # a rewind here would race it and land somewhere the drain clobbers.
        if self._session_turn_draining(source):
            return t("gateway.undo.draining")

        # Parse optional half-turn count: "/undo" → 1, "/undo 3" → 3.
        n = 1
        raw_args = event.get_command_args().strip()
        if raw_args:
            try:
                n = max(1, int(raw_args.split()[0]))
            except ValueError:
                return t("gateway.undo.invalid_count", arg=raw_args.split()[0])
        session_entry = await self.async_session_store.get_or_create_session(source)
        result = await self.async_session_store.rewind_session(session_entry.session_id, n)
        if result is None:
            return t("gateway.undo.nothing")
        # Honest reporting of a non-empty failure (2026-07-15 fix): distinguish a
        # transient DB-busy from a real internal error — never render either as
        # "Nothing to undo." Both mean the rewind did NOT happen (validation +
        # orphan-guard run before the write).
        status = result.get("status") if isinstance(result, dict) else None
        if status == "busy":
            return t("gateway.undo.busy")
        if status == "error":
            return t("gateway.undo.error")

        session_entry.last_prompt_tokens = 0  # transcript was truncated
        self._record_model_friction(
            "undo", source, session_entry.session_id,
            result.get("turns_undone") or result.get("half_turns") or n,
        )
        # Evict the cached agent so the next turn rebuilds from the active-only
        # transcript and memory providers refresh their per-session caches.
        try:
            # The cache is keyed by the profile-namespaced key; a bare build_session_key(source)
            # yields ``agent:main:…`` and misses for every secondary profile.
            self._evict_cached_agent(self._session_key_for_source(source))
        except Exception as e:
            logger.debug("undo: cached-agent eviction skipped: %s", e)

        return t(
            "gateway.undo.removed",
            # What the core rewound (clamped to the history), not what was asked.
            turns=result.get("half_turns", n),
            count=len(result.get("rewound_ids") or []),
        ) + self._undo_tail_suffix(session_entry.session_id)

    async def _cleanup_old_agent_for_reset(self, session_key: str) -> None:
        """Close the old agent's tool resources (sandboxes, browsers, subprocesses) before eviction.
        Blocking work on the event loop (confirm-button click) → offloaded with a bounded timeout.
        wait_for cancels the await, not the worker thread: a wedged teardown keeps running (or
        leaks); the reset proceeds either way."""
        _old_agent = self._cached_agent_for(session_key)
        if _old_agent is None:
            return
        try:
            await asyncio.wait_for(
                self._run_housekeeping_in_executor(self._cleanup_agent_resources, _old_agent),
                timeout=_RESET_CLEANUP_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.warning(
                "Agent resource cleanup for session %s exceeded %ss during /new reset; proceeding with "
                "reset (the worker thread is left to finish on its own). (#35994)",
                session_key, _RESET_CLEANUP_TIMEOUT_S)
        except Exception as cleanup_exc:
            logger.warning(
                "Agent resource cleanup for session %s failed during /new reset: %s (#35994)",
                session_key, cleanup_exc)

    async def _handle_reset_command(self, event: MessageEvent) -> Union[str, EphemeralReply]:
        """Handle /new or /reset command."""
        source = event.source
        session_key = self._session_key_for_source(source)
        self._invalidate_session_run_generation(session_key, reason="session_reset")
        # Evict the running-agent slot now that the generation is bumped: the in-flight run's own
        # guarded release (old generation) returns False and would leave a zombie slot that silently
        # drops all later messages (#28686). Idempotent, so the run's finally calling it again is harmless.
        self._release_running_agent_state(session_key)
        # Snapshot the old entry so on_session_finalize can report the expiring session id.
        old_entry = self.session_store._entries.get(session_key)
        await self._cleanup_old_agent_for_reset(session_key)
        self._evict_cached_agent(session_key)
        # Conversation boundary: ALL conversation-scoped per-session state + security state in one
        # funnel call (see _CONVERSATION_SCOPED_STATE in gateway/run.py).
        self._clear_conversation_scope(session_key, reason="session_reset")
        # In-flight async delegations end WITH the conversation: once the id rotates their
        # completions have no live owner. Expire by durable id, routing key as legacy fallback.
        with contextlib.suppress(Exception):
            from tools.async_delegation import interrupt_for_session
            interrupt_for_session(session_key=session_key, reason="session_reset",
                                  parent_session_id=str(getattr(old_entry, "session_id", "") or ""))
        _reset_process_scoped_tool_state()

        # Manual /new keeps the chat's model/reasoning preferences (sticky route) unless
        # configured otherwise; the conversation-scope funnel above already cleared the
        # in-memory overrides, so a preserved route is rehydrated from the new row.
        preserve_route_preferences = (
            self._preserve_route_preferences_on_manual_reset()
        )
        new_entry = await self.async_session_store.reset_session(
            session_key,
            preserve_route_preferences=preserve_route_preferences,
        )
        if preserve_route_preferences:
            await asyncio.to_thread(
                self._rehydrate_manual_reset_route_preferences, session_key, new_entry
            )
        _old_sid = old_entry.session_id if old_entry else None
        await self._fire_session_reset_hooks(source, session_key, _old_sid,
                                             new_entry.session_id if new_entry else None)
        # Scoped to the profile serving this source so a multiplexed /new banner reports the
        # profile's model, not the base config's.
        try:
            session_info = await asyncio.to_thread(
                self._reset_notice_session_info, source,
                session_key=session_key, session_entry=new_entry,
            )
        except Exception:
            session_info = ""
        if new_entry:
            default_header = t("gateway.reset.header_default")
        else:  # no existing session: create one
            new_entry = await self.async_session_store.get_or_create_session(source, force_new=True)
            default_header = t("gateway.reset.header_new")
        header = await asyncio.to_thread(self._telegram_topic_new_header, source) or default_header
        _title_arg = event.get_command_args().strip()
        if _title_arg and self._session_db and new_entry:
            header = await self._reset_titled_header(header, new_entry.session_id, _title_arg)

        preserved_preferences = []
        unavailable_model_preference = False
        if preserve_route_preferences and new_entry:
            route_lookup = await asyncio.to_thread(
                self._persisted_session_route_identity, session_key
            )
            invalid_model_preference = route_lookup.state == "unavailable"
            if (
                route_lookup.identity
                or self._session_model_overrides.get(session_key)
                or session_key in getattr(self, "_session_model_override_unavailable", set())
                or invalid_model_preference
            ):
                unavailable_model_preference = invalid_model_preference or session_key in getattr(
                    self, "_session_model_override_unavailable", set()
                )
                if not unavailable_model_preference:
                    preserved_preferences.append("model")
            if new_entry.reasoning_override is not None:
                preserved_preferences.append("reasoning")
        if preserved_preferences:
            header += t(
                "gateway.reset.preferences_preserved",
                preferences=" and ".join(preserved_preferences),
            )
        if unavailable_model_preference:
            header += t("gateway.reset.model_preference_unavailable")

        # Telegram DM topic lane: rebind (chat_id, thread_id) → session_id so the next message uses
        # the fresh session instead of switching back to the old one.
        if await asyncio.to_thread(self._is_telegram_topic_lane, source) and new_entry is not None:
            try:
                await asyncio.to_thread(self._record_telegram_topic_binding, source, new_entry)
            except Exception:
                logger.debug("Failed to rebind Telegram topic after /new", exc_info=True)
        _new_sid = new_entry.session_id if new_entry else None
        # Plugin on_session_reset hook (new session guaranteed to exist); best-effort.
        try:
            from hermes_cli.lifecycle import invoke_hook as _invoke_hook
            _invoke_hook("on_session_reset", session_id=_new_sid, reason="new_session",
                         platform=source.platform.value if source.platform else "",
                         old_session_id=_old_sid, new_session_id=_new_sid)
        except Exception:
            pass
        try:
            from hermes_cli.tips import get_random_tip
            _tip_line = t("gateway.reset.tip", tip=get_random_tip())
        except Exception:
            _tip_line = ""
        body = f"{header}\n\n{session_info}" if session_info else header
        return EphemeralReply(f"{body}{_tip_line}")

    async def _handle_compress_command_inner(self, event: MessageEvent) -> str:
        """Handle /compress command -- manually compress conversation context.

        Accepts an optional focus topic: ``/compress <focus>`` guides the
        summariser to preserve information related to *focus* while being
        more aggressive about discarding everything else.

        Also accepts the boundary-aware form ``/compress here [N]``:
        summarize everything except the most recent ``N`` exchanges
        (default 2), kept verbatim. Inspired by Claude Code's Rewind
        "Summarize up to here" action (v2.1.139, May 2026,
        https://code.claude.com/docs/en/whats-new/2026-w20).
        """
        source = event.source
        session_entry = await self.async_session_store.get_or_create_session(source)
        history = await self.async_session_store.load_transcript(session_entry.session_id)

        if not history or len(history) < 4:
            return t("gateway.compress.not_enough")

        # Real, provider-measured context size from the last live API call
        # (the same number /usage reports). Captured BEFORE the post-compress
        # reset further down zeroes session_entry.last_prompt_tokens. This is
        # the only tokenizer-truth figure available here; the estimates below
        # are char/4 heuristics. Showing both side-by-side stops the
        # apples-to-oranges confusion where a tool-heavy real context (e.g.
        # 290K) gets compared against a transcript-only estimate.
        real_before_tokens = int(getattr(session_entry, "last_prompt_tokens", 0) or 0)

        # Parse args: either a focus topic (full compress) or the
        # boundary-aware "here [N]" form (partial compress).
        from hermes_cli.partial_compress import (
            extract_compress_flags,
            parse_partial_compress_args,
            rejoin_compressed_head_and_tail,
            split_history_for_partial_compress,
            summarize_compress_preview,
        )
        from agent.conversation_compression import (
            finalize_context_engine_compression_notification,
        )
        _raw_args = (event.get_command_args() or "").strip()
        # Strip --preview/--dry-run/--aggressive before positional parsing
        # so the flags coexist with 'here [N]' / focus-topic forms.
        _raw_args, _preview, _aggressive = extract_compress_flags(_raw_args)
        partial, keep_last, focus_topic = parse_partial_compress_args(_raw_args)

        _agg_note = ""
        if _aggressive:
            # LLM-free hard truncation is not supported on this surface —
            # it would need its own transcript-persistence branch outside
            # the guarded _compress_context rotation machinery (#44794).
            _agg_note = t("gateway.compress.aggressive_unsupported")
            if not _preview:
                return _agg_note

        if _preview:
            # Report what WOULD be compressed — no agent, no writes.
            from agent.model_metadata import estimate_request_tokens_rough
            _pv_msgs = [
                {"role": m.get("role"), "content": m.get("content")}
                for m in history
                if m.get("role") in {"user", "assistant"} and m.get("content")
            ]
            approx_tokens = estimate_request_tokens_rough(_pv_msgs)
            report = summarize_compress_preview(
                _pv_msgs, partial, keep_last, focus_topic, approx_tokens
            )
            lines = [f"🗜️ {line}" for line in report["lines"]]
            if _aggressive:
                lines.append(_agg_note)
            return "\n".join(lines)

        try:
            from run_agent import AIAgent
            from agent.manual_compression_feedback import summarize_manual_compression
            from agent.model_metadata import (
                estimate_messages_tokens_rough,
                estimate_request_tokens_rough,
            )

            session_key = self._session_key_for_source(source)
            # Preserve the same platform + stable gateway session identity that a
            # normal gateway turn passes (gateway/run.py main turn), so external
            # context engines bind this temporary compression agent to the
            # original platform conversation instead of falling back to an
            # unbound/default "cli" host source — see #50422. _platform_config_key
            # maps LOCAL->"cli" exactly like the live turn, avoiding a new
            # "local" vs "cli" mismatch.
            from gateway.run import (
                _GATEWAY_HYGIENE_PLATFORM,
                _platform_config_key,
                _seed_hygiene_system_prompt,
            )
            platform_key = (
                _platform_config_key(source.platform) if source.platform else None
            )
            # Off-loop: override rehydrate can re-resolve credentials and GET the
            # provider's /v1/models synchronously (t_515b7fce).
            model, runtime_kwargs = await asyncio.to_thread(
                self._resolve_session_agent_runtime,
                source=source,
                session_key=session_key,
            )

            # A manual /compress runs between turns and builds a temporary
            # AIAgent. The normal resolver above returns the configured primary
            # route, but the resident session agent may currently be serving on
            # a healthy fallback. Inherit that LIVE route as one atomic unit so
            # the summarizer does not jump back onto the congested primary.
            resident_agent = getattr(self, "_running_agents", {}).get(session_key)
            if not callable(getattr(resident_agent, "_current_main_runtime", None)):
                cache_lock = getattr(self, "_agent_cache_lock", None)
                cache = getattr(self, "_agent_cache", None)
                if cache_lock is not None and cache is not None:
                    try:
                        with cache_lock:
                            cached = cache.get(session_key)
                        if cached:
                            resident_agent = cached[0]
                    except Exception:
                        resident_agent = None
            current_runtime = getattr(resident_agent, "_current_main_runtime", None)
            if callable(current_runtime):
                try:
                    runtime_candidate = current_runtime()
                except Exception:
                    runtime_candidate = None
                live_runtime = (
                    runtime_candidate if isinstance(runtime_candidate, dict) else {}
                )
                live_provider = str(live_runtime.get("provider") or "").strip()
                live_model = str(live_runtime.get("model") or "").strip()
                live_api_key = live_runtime.get("api_key")
                if isinstance(live_api_key, str):
                    live_api_key = live_api_key.strip()
                if live_provider and live_model and live_api_key:
                    model = live_model
                    # Empty base_url/api_mode values are valid for built-in and
                    # provider-catalog routes; the live provider id re-resolves
                    # those defaults. Never retain either value from the stale
                    # configured route just to make this snapshot look complete.
                    # Rebuild rather than merge: requested_provider, ACP command
                    # args, and credential pools may also belong to that stale
                    # route. The temporary agent derives omitted defaults from
                    # the live provider/model instead.
                    runtime_kwargs = {"provider": live_provider, "api_key": live_api_key}
                    for field in ("base_url", "api_mode"):
                        value = live_runtime.get(field)
                        if isinstance(value, str):
                            value = value.strip()
                        if value not in (None, ""):
                            runtime_kwargs[field] = value
                    live_max_tokens = getattr(resident_agent, "max_tokens", None)
                    if live_max_tokens is not None:
                        runtime_kwargs["max_tokens"] = live_max_tokens
                    logger.info(
                        "Manual /compress inheriting resident runtime: "
                        "session=%s provider=%s model=%s",
                        session_key,
                        live_provider,
                        live_model,
                    )
            if not runtime_kwargs.get("api_key"):
                return t("gateway.compress.no_provider")

            # Chat-only projection: user/assistant turns with real content,
            # PROJECTED to {role, content}. This drives the DISPLAY/ESTIMATE
            # axes only (chat_before_tokens, the "N chat" message count, the
            # both-axes feedback reconciliation) — NOT the compressor input.
            # Tool results, system rows, and contentless (tool-call-only) turns
            # are measured separately as ``non_chat_rows`` (#F1, 2026-07-02
            # compress-feedback-honesty).
            msgs = [
                {"role": m.get("role"), "content": m.get("content")}
                for m in history
                if m.get("role") in {"user", "assistant"} and m.get("content")
            ]

            # Compressor input: the FULL transcript (tool results included) —
            # same rationale as the session-hygiene auto-compress in
            # gateway/run.py (#3854 / #58551). Filtering to user/assistant-only
            # starves the compressor's tool-result pruning (tool results are
            # usually the bulk of the context) and trips the protect-first/last
            # early-return on short filtered histories, making /compress a no-op
            # on huge tool-heavy sessions. Keep the full message dicts (tool_calls
            # stubs, tool results) intact, matching what the agent loop feeds
            # _compress_context.
            compress_input = [
                m
                for m in history
                if m.get("role") in {"user", "assistant", "tool"}
            ]

            # The rows EXCLUDED from the chat-only compression input: tool
            # results, system rows, contentless (tool-call-only) turns. When
            # the transcript rewrite below happens, these stored rows are
            # dropped — usually the bulk of a tool-heavy session. Measure
            # them so the feedback can reconcile both axes instead of
            # claiming "No changes" over a six-figure token cut (#F1,
            # 2026-07-02 spec: compress-feedback-honesty).
            non_chat_rows = [
                m
                for m in history
                if not (m.get("role") in {"user", "assistant"} and m.get("content"))
            ]
            non_chat_count = len(non_chat_rows)

            # Boundary-aware split: only the head is summarized; the most
            # recent `keep_last` exchanges are preserved verbatim. The
            # split snaps the tail to a user-turn start so the rejoined
            # transcript keeps role alternation valid. Operates on the FULL
            # compressor input (tool rows included), not the chat-only `msgs`.
            tail: list = []
            head = compress_input
            if partial:
                head, tail = split_history_for_partial_compress(compress_input, keep_last)
                if not tail:
                    # Degenerate split — fall back to full compression.
                    partial = False
                    head = compress_input

            # Bind the temporary compression agent to the originating source's
            # platform + stable gateway session key. These are *authoritative*
            # identity invariants (derived from `source`), so assign them into
            # runtime_kwargs directly rather than via setdefault: a value already
            # present there from the resolver would be a placeholder/stale
            # identity and must not win. Assigning (vs passing a second explicit
            # kwarg) also keeps each key single-valued, avoiding a "got multiple
            # values for keyword argument" TypeError. platform is only set when
            # known: for a source without platform metadata we leave it unset so
            # AIAgent's default (platform=None -> source "cli") applies, exactly
            # the prior behavior. _resolve_session_agent_runtime does not set
            # either key today, so in practice this just adds them.
            if platform_key is not None:
                runtime_kwargs["platform"] = platform_key
            runtime_kwargs["gateway_session_key"] = session_key

            # Same reasoning setting as a live turn (session /reasoning > per-model > global):
            # without it the transport applies its default effort — a 400 on non-reasoning
            # models (#85153 class).
            runtime_kwargs["reasoning_config"] = self._resolve_session_reasoning_config(
                source=source, model=model,
            )
            # Build through the shared helper (upstream seam, restores the live prompt and
            # honours compression.checkpoint_required); it constructs the AIAgent OFF the loop.
            _compress_sid = session_entry.session_id
            tmp_agent = await self._build_manual_compression_agent(
                session_entry.session_id, model, runtime_kwargs,
            )
            # Keep the real source platform during construction so external
            # context engines bind correctly. If compression has to rebuild the
            # prompt, stamp that provider-less fallback as stale for the next
            # real gateway turn.
            tmp_agent.platform = _GATEWAY_HYGIENE_PLATFORM
            try:
                tmp_agent._print_fn = lambda *a, **kw: None
                # C5 #40 (PR #976): a /new or /reset while the agent was being
                # built (off-loop) rebinds the key to a new session. Do not
                # compress the stale one and rotate the key back onto it.
                try:
                    _bound_sid = await self.async_session_store.peek_session_id(session_key)
                except Exception:
                    _bound_sid = None
                if session_entry.session_id != _compress_sid or (
                    isinstance(_bound_sid, str) and _bound_sid != _compress_sid
                ):
                    return (
                        "Compression cancelled: the session was reset while "
                        "/compress was starting. Nothing was compressed."
                    )
                # Prevent close() from ending the newly rotated session —
                # the gateway session entry now points at the new id and
                # must remain open for the next user turn.
                tmp_agent._end_session_on_close = False

                # Two independent measurements, surfaced as two lines so the
                # user can see both "how big is the conversation" and "how big
                # is the actual request":
                #
                #  • Chat size  — estimate_messages_tokens_rough over the
                #    user/hermes turns only (`msgs`). Excludes system prompt,
                #    tool schemas, and tool results. This is the original
                #    "Approx request size" figure Ace wanted to retain.
                #  • Full request size — estimate_request_tokens_rough over the
                #    FULL transcript (`history`) plus system prompt + tool
                #    schemas. This is what the model is actually sent and lines
                #    up with the real provider count from /usage.
                #
                # The compressor itself is fed the full-request estimate so its
                # pressure logic reflects reality (#6217).
                #
                # F3 (2026-07-02 honesty spec): resolve the REAL fixed overhead
                # (resident agent's system prompt + full tool schemas) ONCE,
                # up front, and use it for BOTH the before and after estimates.
                # The temp agent here is memory-only (empty system prompt,
                # tools=[memory]), so measuring "before" with its overhead
                # under-reports by the entire fixed cost while the line claims
                # "includes chat, system, tools".
                _tmp_sys = getattr(tmp_agent, "_cached_system_prompt", "") or ""
                _tmp_tools = getattr(tmp_agent, "tools", None) or None
                _real_sys, _real_tools = self._resolve_fixed_overhead(session_key)
                _full_after_has_fixed = bool(_real_sys or _real_tools)
                _sys_prompt = _real_sys if _full_after_has_fixed else _tmp_sys
                _tools = _real_tools if _full_after_has_fixed else _tmp_tools
                approx_tokens = estimate_request_tokens_rough(
                    history, system_prompt=_sys_prompt, tools=_tools
                )
                chat_before_tokens = estimate_messages_tokens_rough(msgs)

                compressor = tmp_agent.context_compressor
                # Route the SUMMARISER at the session's LIVE provider, not the
                # config default. The compressor passes its own
                # provider/model/base_url as the auxiliary ``main_runtime``
                # (see ContextCompressor._generate_summary), and this throwaway
                # agent was built from config — so a session that moved onto a
                # different provider at runtime (a mid-session fallback) would
                # have its summariser cross onto the config route instead.
                # Observed 2026-08-08: a session serving every call on
                # claude-apx-7 had its summariser sent to a rate-limited
                # claude-apr, which stalled and killed the compaction.
                # No-op when no agent is resident (post-restart / evicted).
                _live_route = self._resolve_live_session_route(session_key)
                if _live_route:
                    _live_model = str(_live_route.get("model") or "")
                    _live_provider = str(_live_route.get("provider") or "")
                    if (
                        _live_provider
                        and _live_model
                        and (
                            _live_provider != getattr(compressor, "provider", "")
                            or _live_model != getattr(compressor, "model", "")
                        )
                    ):
                        logger.info(
                            "Manual /compress: routing the summarizer at the "
                            "live session route %s (%s) instead of the "
                            "config-resolved %s (%s).",
                            _live_model,
                            _live_provider,
                            getattr(compressor, "model", ""),
                            getattr(compressor, "provider", ""),
                        )
                        compressor.model = _live_model
                        compressor.provider = _live_provider
                        compressor.base_url = str(_live_route.get("base_url") or "")
                        compressor.api_key = _live_route.get("api_key") or ""
                        compressor.api_mode = str(_live_route.get("api_mode") or "")
                if not compressor.has_content_to_compress(head):
                    return t("gateway.compress.nothing_to_do")

                # Interim progress ack for a large session. /compress blocks on the
                # executor hop below (a big session can take many minutes of
                # chunked auxiliary summarization); the handler's return value is the
                # ONLY signal the user gets, so without this a long compress is
                # indistinguishable from a hang. Best-effort and fire-and-forget:
                # mirror _send_startup_restore_ack — a failed ack must never break
                # the compress. Gated on transcript size so trivial (fast) compresses
                # stay quiet instead of emitting noise.
                if len(history) >= _COMPRESS_PROGRESS_ACK_MIN_MESSAGES:
                    await self._send_compress_progress_ack(source, len(history))

                # _run_in_executor_with_context (not a bare run_in_executor):
                # the profile secret scope installed by the wrapper is a
                # contextvar, and the default-executor hop would drop it —
                # the compressor's aux-client provider resolution would then
                # read credentials unscoped and fail closed under
                # multiplexing.
                compressed, _ = await self._run_in_executor_with_context(
                    lambda: tmp_agent._compress_context(
                        head,
                        "",
                        approx_tokens=approx_tokens,
                        focus_topic=focus_topic,
                        force=True,
                        trigger_reason="manual_compress_command",
                        defer_context_engine_notification=True,
                    )
                )

                # If _compress_context returned unchanged because a
                # concurrent compression lock is held, tell the user
                # clearly instead of showing the misleading
                # "No changes from compression" no-op text. The wording
                # distinguishes a confirmed holder from an unconfirmed
                # acquisition failure (describe_compression_lock_skip).
                # The deferred context-engine notification is discarded by
                # the finally block below (finalize committed=False).
                _lock_skipped = getattr(tmp_agent, "_compression_skipped_due_to_lock", None)
                if _lock_skipped is True or isinstance(_lock_skipped, str):
                    from agent.manual_compression_feedback import (
                        describe_compression_lock_skip,
                    )
                    return describe_compression_lock_skip(_lock_skipped)

                if partial and tail:
                    compressed = rejoin_compressed_head_and_tail(compressed, tail)

                # _compress_context either rotated (legacy: ended the old
                # session, created a continuation id — write compressed messages
                # into the NEW session so the original stays searchable) or
                # compacted in place (compression.in_place / #38763: same id,
                # transcript replaced with the compacted set).
                new_session_id = tmp_agent.session_id
                rotated = new_session_id != session_entry.session_id
                _old_session_id = session_entry.session_id
                _in_place = bool(getattr(tmp_agent, "_last_compaction_in_place", False))
                # Did the compressor produce a compacted list whose DB persist
                # was rolled back (locked/contended state.db, FK error, ENOSPC)?
                # This is a TRANSIENT, retryable failure that leaves session_id
                # unchanged — the same surface signature as a genuine no-op — so
                # without this flag the reply reports the bland "No changes:
                # transcript preserved" for a save that actually failed. Read it
                # to render the honest retry message instead (#44794).
                _persist_failed = bool(
                    getattr(tmp_agent, "_last_compaction_persist_failed", False)
                )
                # Did the compaction ABORT before producing a compacted list at
                # all (stalled summariser → no-progress watchdog)? Distinct from
                # ``_persist_failed``: there, a list existed and the DB write was
                # rolled back; here nothing was ever produced, so BOTH that flag
                # and the session_id look exactly like a genuine no-op.
                # ``is True`` (not truthiness): this reads a possibly-absent
                # attribute off an agent object, and a permissive bool() would
                # misclassify a persist-failure or a test double as an abort.
                _aborted = (
                    getattr(tmp_agent, "_last_compaction_aborted", False) is True
                )

                # Persist the compressed transcript BEFORE repointing the live
                # session onto the new session_id. Order matters: if we
                # repointed first and the canonical DB write then failed (lock
                # contention under concurrent writes, ENOSPC, a disk/IO error),
                # the session entry would already reference a brand-new, empty
                # session_id while the handler still reported success — the
                # user's active conversation would silently vanish from view.
                # Writing first, and treating a write failure as fatal, keeps
                # the old history reachable (on rotation the entry still points
                # at it; in place the original transcript is untouched) and lets
                # the outer handler surface a "compress failed" banner instead.
                #
                # Only rewrite the transcript when rotation produced a NEW
                # session id.  In-place compaction does NOT need a rewrite:
                # archive_and_compact() has already soft-archived the previous
                # active rows and inserted the compacted messages as the new
                # active set inside _compress_context().  Calling
                # rewrite_transcript() after in-place compaction would invoke
                # replace_messages(active_only=False) which DELETEs ALL rows —
                # including the archived turns that archive_and_compact()
                # deliberately preserved (silent data loss, #61145).
                #
                # The third case: _compress_context could NOT rotate AND was
                # not in-place (e.g. legacy mode but _session_db unavailable /
                # the DB split raised) — there session_id is unchanged for a
                # FAILURE reason, and rewrite_transcript() would DELETE the
                # original messages and replace them with only the compressed
                # summary (permanent data loss #44794, #39704).
                if rotated:
                    if not await self.async_session_store.rewrite_transcript(
                        new_session_id, compressed
                    ):
                        raise RuntimeError(
                            f"failed to persist compressed transcript for "
                            f"session {new_session_id}"
                        )
                    session_entry.session_id = new_session_id
                    await self.async_session_store._save()
                    await asyncio.to_thread(
                        self._sync_telegram_topic_binding,
                        source, session_entry, reason="compress-command",
                    )
                elif _in_place:
                    # archive_and_compact() already persisted the compacted
                    # transcript inside _compress_context — nothing to do.
                    pass
                else:
                    # Distinguish the two very different reasons this branch is
                    # reached, because they send a triager to opposite places:
                    #
                    #  • the in-place CONFIG knob is genuinely off (legacy
                    #    rotation mode) and the rotation could not complete;
                    #  • in-place is ON but the compaction never COMPLETED
                    #    (stalled summariser / no-progress abort), so the
                    #    run-level ``_last_compaction_in_place`` outcome flag is
                    #    False for a failure reason, not a config reason.
                    #
                    # Reading the outcome flag alone and reporting "in-place
                    # mode is off" blames a config setting that is very often
                    # correct — a real misdiagnosis trap observed 2026-08-08,
                    # where compression.in_place was `true` the whole time.
                    _in_place_configured = bool(
                        getattr(tmp_agent, "compression_in_place", True)
                    )
                    if _in_place_configured:
                        logger.warning(
                            "Manual /compress: the compaction did not complete "
                            "(session_id unchanged, no in-place commit) — "
                            "preserving original transcript instead of "
                            "overwriting it (#44794). in-place mode is ON; "
                            "this is a failed/aborted compaction, NOT a "
                            "config issue. Reason: %s.",
                            (
                                getattr(
                                    tmp_agent,
                                    "_last_compaction_abort_reason",
                                    "",
                                )
                                or "unknown"
                            ),
                        )
                    else:
                        logger.warning(
                            "Manual /compress: session rotation did not occur "
                            "(session_id unchanged) and in-place mode is off "
                            "(compression.in_place=false) — preserving original "
                            "transcript instead of overwriting it (#44794)."
                        )
                # Whether the STORED transcript actually changed. Downstream
                # feedback must be computed on this basis — when no rewrite
                # happened, the next request resends the ORIGINAL transcript
                # (tool rows included), so an "after" measured over the
                # chat-only `compressed` list would claim a shrink that never
                # occurred (#F2).
                _rewritten = bool(rotated or _in_place)
                if _rewritten:
                    # Reset stored token count — transcript changed, old value
                    # is stale. On a preserved transcript the real provider-
                    # measured count is still valid; zeroing it would destroy
                    # the only tokenizer-truth figure for the next /compress
                    # or /usage (#F4).
                    await self.async_session_store.update_session(
                        session_entry.session_key, last_prompt_tokens=0
                    )
                # After-compression estimates.
                #  • chat_after  — user/hermes turns only (headline + Chat size).
                #  • full_after  — PRE-FLIGHT estimate of the NEXT request: the
                #    REAL fixed overhead (resident agent's system prompt + full
                #    tool schemas, resolved below) + the post-compress transcript.
                #    The old code measured this off the memory-only temp agent,
                #    whose _cached_system_prompt is empty and tools = [memory] —
                #    so it under-reported by the entire ~30k fixed overhead
                #    (e.g. "56,965 -> 3,436"). Now it reflects what's actually
                #    sent next turn.
                #    Basis: the rows the next request will actually carry —
                #    `compressed` after a real rewrite, the ORIGINAL `history`
                #    when the store was preserved (#F2).
                # Chat-side view of the compressor output. The compressor now
                # receives the FULL transcript (tool rows included, #58551), so
                # `compressed` carries tool/system rows too. The chat-axis
                # feedback (#172: CASE A/B/C headline, chat_after token delta)
                # must compare the chat-only `msgs` against the chat-only slice
                # of the result — otherwise a chat no-op (compressor returned the
                # rows unchanged) looks like a change purely because tool /
                # contentless-tool-call rows are present on both sides. Apply the
                # SAME eligibility filter as `msgs` (content-bearing user /
                # assistant), but preserve object identity when nothing is
                # stripped so pre-existing chat-only outputs are measured exactly
                # as before (baseline fed the compressor a chat-only list).
                _chat_slice = [
                    m
                    for m in compressed
                    if m.get("role") != "tool" and m.get("content")
                ]
                compressed_chat = (
                    compressed if len(_chat_slice) == len(compressed) else _chat_slice
                )
                _after_basis = compressed_chat if _rewritten else history
                chat_after_tokens = (
                    estimate_messages_tokens_rough(compressed_chat)
                    if _rewritten
                    else chat_before_tokens
                )
                finalize_context_engine_compression_notification(
                    tmp_agent,
                    committed=True,
                )
                non_chat_tokens = (
                    estimate_messages_tokens_rough(non_chat_rows)
                    if non_chat_rows
                    else 0
                )
                # Fixed overhead already resolved up front (F3): _sys_prompt /
                # _tools hold the resident agent's real system prompt + tool
                # schemas when available (_full_after_has_fixed=True), else the
                # temp agent's memory-only fallback — the same basis used for
                # `approx_tokens`, so before/after are finally comparable.
                # No-rewrite path skips the estimate entirely: the reply says
                # "unchanged" and never reads an after figure, and estimating
                # over the full tool-heavy history is not free.
                full_after_tokens = (
                    estimate_request_tokens_rough(
                        _after_basis, system_prompt=_sys_prompt, tools=_tools
                    )
                    if _rewritten
                    else approx_tokens
                )
                # Both-axes feedback: chat delta AND the stored tool/system
                # rows the rewrite dropped, so the headline reconciles with
                # the token math (#F1/#F5).
                summary = summarize_manual_compression(
                    msgs,
                    compressed_chat,
                    chat_before_tokens,
                    chat_after_tokens,
                    non_chat_count=non_chat_count,
                    non_chat_tokens=non_chat_tokens,
                    transcript_rewritten=_rewritten,
                    full_before_count=len(history),
                    compression_state=compressor,
                )
                # Granular reconciling breakdown (the same CompactionStats +
                # renderer the auto-compaction announce uses): Messages /
                # Context / per-bucket "Removed from live context" lines with
                # the tool-vs-other sub-split. Built ONLY when the rewrite
                # actually happened; degrades to the enhanced two-line form on
                # ANY failure — a reconcile bug must never break /compress or
                # ship wrong math (same contract as the hygiene announce).
                _granular = None
                _granular_has_wire = False
                if _rewritten and len(compressed_chat) != len(msgs):
                    try:
                        from agent.compaction_stats import build_hygiene_stats
                        from agent.conversation_compression import (
                            _format_granular_announce,
                        )
                        from agent.provider_model_util import format_provider_model

                        _engine_name = getattr(compressor, "name", None)
                        _stats = build_hygiene_stats(
                            raw_history=history,
                            eligible_msgs=msgs,
                            compressed=compressed_chat,
                            estimator=estimate_messages_tokens_rough,
                            engine_is_lcm=(_engine_name == "lcm"),
                        )
                        _s_ok, _s_why = _stats.validate()
                        if _s_ok:
                            _prov = (
                                runtime_kwargs.get("provider")
                                if isinstance(runtime_kwargs, dict)
                                else None
                            )
                            _model_part = (
                                format_provider_model(_prov, model) if model else ""
                            )
                            # Reasoning level — session-truthful (same
                            # override-aware resolver as the footer/auto-
                            # announce; the manual path never showed r: at
                            # all, an inconsistency vs the auto-compaction
                            # banner). No live agent config here (the temp
                            # compression agent isn't the resident agent), so
                            # resolve session override > per-model > global.
                            # Label mapping via the shared chokepoint.
                            try:
                                from hermes_constants import (
                                    reasoning_label as _rlabel,
                                )
                                _r = _rlabel(
                                    self._resolve_session_reasoning_config(
                                        source=source, model=model or "",
                                    )
                                )
                                if _r and _r not in {"default", "none"}:
                                    _model_part = (
                                        f"{_model_part} · r:{_r}"
                                        if _model_part
                                        else f"r:{_r}"
                                    )
                            except Exception:
                                pass
                            if _engine_name == "lcm":
                                _model_part = (
                                    f"{_model_part} · engine: lcm"
                                    if _model_part
                                    else "engine: lcm"
                                )
                            _granular = _format_granular_announce(
                                f"🗜️ {summary['headline']}",
                                _stats,
                                _model_part,
                                False,
                                None,
                                None,
                                basis="stored",
                                # Wire-first (Ace 2026-07-02): when the REAL
                                # provider-measured before-count exists, the
                                # block's Context line carries the wire story
                                # (measured before → next-request estimate)
                                # and the trailing Full-request line below is
                                # skipped as a duplicate.
                                wire_before=real_before_tokens,
                                wire_after=full_after_tokens,
                            )
                            _granular_has_wire = (
                                real_before_tokens > 0 and full_after_tokens > 0
                            )
                            # Honest, store-correct recovery pointer.
                            if _engine_name == "lcm":
                                _granular += (
                                    "\n↩ Nothing lost — original messages preserved "
                                    "in lcm.db (recover with lcm_grep / lcm_expand)"
                                )
                            elif rotated:
                                _granular += (
                                    f"\n↩ previous transcript preserved: "
                                    f"{_old_session_id} (searchable via session_search)"
                                )
                        else:
                            logger.warning(
                                "Manual /compress granular stats reconcile "
                                "failed (%s) — using two-line form",
                                _s_why,
                            )
                    except Exception:
                        logger.warning(
                            "Manual /compress granular stats build failed "
                            "(non-fatal, using two-line form)",
                            exc_info=True,
                        )
                        _granular = None
                        _granular_has_wire = False
                # Detect summary-generation failure so we can surface a
                # visible warning to the user even on the manual /compress
                # path (otherwise the failure is silently logged).
                # _last_compress_aborted means the aux LLM returned no
                # usable summary and the compressor preserved messages
                # unchanged (no drop, no placeholder).  force=True was
                # passed above so any active cooldown is bypassed.
                _summary_aborted = bool(getattr(compressor, "_last_compress_aborted", False))
                _summary_err = getattr(compressor, "_last_summary_error", None)
                # Force-redact provider exception text at this UI boundary
                # even when global redaction is disabled.
                if _summary_err:
                    from agent.redact import redact_sensitive_text
                    _summary_err = redact_sensitive_text(_summary_err, force=True)
                # Separately: did the user's CONFIGURED aux model fail
                # and we recovered via main?  Surface that as an info
                # note so they can fix their config.
                _aux_fail_model = getattr(compressor, "_last_aux_model_failure_model", None)
                _aux_fail_err = getattr(compressor, "_last_aux_model_failure_error", None)
            finally:
                finalize_context_engine_compression_notification(
                    tmp_agent,
                    committed=False,
                )
                # Evict cached agent so next turn rebuilds system prompt
                # from current files (SOUL.md, memory, etc.).
                self._evict_cached_agent(session_key)
                # Off-loop + bounded: temporary-agent teardown can block on
                # subprocess/network/SQLite work. Running it inline freezes the
                # gateway loop and stalls platform polling / heartbeat, the same
                # wedge class fixed for /new (#35994) and hygiene/shutdown
                # (#53175).
                await self._cleanup_agent_resources_off_loop(
                    tmp_agent, context="manual compression"
                )
            # CASE D (persist failure): the compressor produced a compacted
            # transcript but the DB write to persist it was rolled back (a
            # locked/contended state.db, an FK error, ENOSPC). session_id is
            # unchanged, so this looks identical on the surface to a genuine
            # no-op — but it is a TRANSIENT, retryable FAILURE, not "no changes."
            # Tell the user plainly, distinctly from both the honest no-op and a
            # real compaction, and stop here: there is nothing persisted to
            # report a before/after over, and the next request resends the same
            # context. Nothing was lost (the original transcript is untouched).
            # See #44794.
            # CASE E: the compaction ABORTED before producing anything — the
            # summariser stalled and the no-progress watchdog gave up. Unlike
            # CASE D there is no compacted list at all, so
            # ``_last_compaction_persist_failed`` is False and session_id is
            # unchanged: on the surface this is indistinguishable from a
            # genuine "nothing to compress" no-op, and pre-fix it rendered the
            # bland "No changes: transcript preserved" for a run that actually
            # died on a stalled provider (observed 2026-08-08). Report the real
            # cause and tell the user it is retryable. Nothing was lost.
            if _aborted and not _persist_failed and not _rewritten:
                _fr_before = (
                    f"{real_before_tokens:,}"
                    if real_before_tokens > 0
                    else f"~{approx_tokens:,}"
                )
                _ab_lines = [t("gateway.compress.timed_out")]
                if focus_topic:
                    _ab_lines.append(
                        t("gateway.compress.focus_line", topic=focus_topic)
                    )
                _ab_lines.append(
                    t("gateway.compress.full_request_unchanged", before=_fr_before)
                )
                return "\n".join(_ab_lines)
            if _persist_failed and not _rewritten:
                _fr_before = (
                    f"{real_before_tokens:,}"
                    if real_before_tokens > 0
                    else f"~{approx_tokens:,}"
                )
                _pf_lines = [t("gateway.compress.persist_failed")]
                if focus_topic:
                    _pf_lines.append(
                        t("gateway.compress.focus_line", topic=focus_topic)
                    )
                _pf_lines.append(
                    t("gateway.compress.full_request_unchanged", before=_fr_before)
                )
                return "\n".join(_pf_lines)
            # Headline + per-axis lines. When the granular reconciling
            # breakdown built successfully (rewrite happened, stats validated),
            # it REPLACES the headline/chat/dropped lines with the full
            # Messages/Context/Removed-buckets block — the same renderer the
            # auto-compaction announce uses. The Full-request line below still
            # appends (its scope is chat+system+tools, complementary to the
            # granular block's transcript-only Context line).
            if _granular:
                lines = [_granular]
                if focus_topic:
                    lines.append(t("gateway.compress.focus_line", topic=focus_topic))
            else:
                lines = [f"🗜️ {summary['headline']}"]
                if focus_topic:
                    lines.append(t("gateway.compress.focus_line", topic=focus_topic))

                # Line 1 — Chat size. Enhanced mode (tool-heavy stored transcript)
                # renders the reconciling per-axis lines from the summary helper:
                # the chat line + the dropped tool/system rows line. Classic mode
                # keeps the original chat_size locale lines.
                if summary.get("enhanced"):
                    if summary.get("chat_line"):
                        lines.append(summary["chat_line"])
                    if summary.get("dropped_line"):
                        lines.append(summary["dropped_line"])
                elif summary["noop"] and chat_after_tokens == chat_before_tokens:
                    lines.append(
                        t("gateway.compress.chat_size_unchanged", before=chat_before_tokens)
                    )
                else:
                    lines.append(
                        t(
                            "gateway.compress.chat_size",
                            before=chat_before_tokens,
                            after=chat_after_tokens,
                        )
                    )

            # Line 2 — Full request size: what the model is actually sent
            # (chat + system + tools + tool results). The "before" is the REAL
            # provider-measured count from the last live request when we have
            # one (no ~ prefix); otherwise it falls back to the char-based
            # full-request estimate (~ prefix). The "after" is always an
            # estimate — the real post-compression count doesn't exist until
            # the next live API call.
            #
            # Wire-first: when the granular block already rendered the wire
            # story on its Context line (measured before → next-request
            # estimate), this line would be a duplicate — skip it.
            #
            # When the stored transcript was NOT rewritten, the next request
            # resends the same context — say "unchanged" instead of printing a
            # before → after pair whose delta is pure estimator noise (#F2).
            if not _rewritten:
                _fr_before = (
                    f"{real_before_tokens:,}"
                    if real_before_tokens > 0
                    else f"~{approx_tokens:,}"
                )
                lines.append(
                    t("gateway.compress.full_request_unchanged", before=_fr_before)
                )
            elif _granular_has_wire:
                pass  # wire story already on the granular Context line
            elif real_before_tokens > 0:
                lines.append(
                    t(
                        "gateway.compress.full_request_real",
                        before=real_before_tokens,
                        after=full_after_tokens,
                    )
                )
            else:
                lines.append(
                    t(
                        "gateway.compress.full_request_est",
                        before=approx_tokens,
                        after=full_after_tokens,
                    )
                )
            # Honesty note: when no resident agent was available to supply the
            # real fixed overhead, the "after" omits system prompt + tool
            # schemas (~tens of k tokens). Say so rather than under-report.
            # Only relevant when an "after" estimate was actually shown.
            if _rewritten and not _full_after_has_fixed:
                lines.append(t("gateway.compress.full_request_no_fixed"))

            if summary["note"]:
                lines.append(summary["note"])
            if _summary_aborted:
                lines.append(
                    t(
                        "gateway.compress.aborted",
                        error=(_summary_err or "unknown error"),
                    )
                )
            elif _aux_fail_model:
                lines.append(
                    t(
                        "gateway.compress.aux_failed",
                        model=_aux_fail_model,
                        error=(_aux_fail_err or "unknown error"),
                    )
                )
            return "\n".join(lines)
        except Exception as e:
            logger.warning("Manual compress failed: %s", e)
            return t("gateway.compress.failed", error=e)

    async def _resolve_context_figures(self, agent, ctx, session_entry, source):
        """``(used, context_length, model_name)`` for /context: used = compressor -> SessionStore;
        model = agent -> SessionDB row; window = compressor -> gateway model route -> model metadata.

        The compressor reading goes through ``live_context_tokens``: ``-1`` after a compaction means
        the post-compaction estimate, never the stored pre-compaction figure (t_64728f32), so the
        SessionStore fallback is skipped in that case."""
        from gateway.runtime_footer import live_context_tokens

        used = 0
        used_post_compaction = False
        if ctx is not None:
            _reading = live_context_tokens(ctx)
            used = max(0, _reading.tokens or 0)
            used_post_compaction = _reading.post_compaction
        if not used and not used_post_compaction:
            used = max(0, _int_value(getattr(session_entry, "last_prompt_tokens", 0)))
        context_length = _n(ctx, "context_length")
        model_name = _clean_str(getattr(agent, "model", "")) if agent is not None else ""
        # getattr guard: reachable on a GatewayRunner built via object.__new__ (plugin-command
        # tests, partially-initialised runners) where __init__ never ran and _session_db is absent.
        _sdb = getattr(self, "_session_db", None)
        if not model_name and _sdb:
            row = await _quiet(lambda: _sdb.get_session(session_entry.session_id))
            model_name = _clean_str(row.get("model", "")) if isinstance(row, dict) else ""
        if not context_length:
            resolved = await self._resolve_route_context(source, model_name)
            if resolved is not None:
                model_name = model_name or resolved.model
                context_length = _int_value(resolved.context_length)
        if not context_length and model_name:
            from agent.model_metadata import get_model_context_length
            context_length = _int_value(
                await _quiet(lambda: asyncio.to_thread(get_model_context_length, model_name))
            )
        return used, context_length, model_name

    async def _handle_status_command(self, event: MessageEvent) -> str:
        """Handle /status command."""
        from gateway.run import _AGENT_PENDING_SENTINEL
        source = event.source
        session_entry = await self.async_session_store.get_or_create_session(source)
        session_key = session_entry.session_key
        # Keep the sentinel distinct: a starting/pending run is not a usable agent for
        # model/context display, but it still occupies the session slot.
        agent = self._running_agents.get(session_key)
        is_running = agent is not None and agent is not _AGENT_PENDING_SENTINEL
        # Pending /queue follow-ups (slot + overflow).
        adapter = self.adapters.get(source.platform) if source else None
        queue_depth = self._queue_depth(session_key, adapter=adapter)
        title, session_row, db_total_tokens, persisted_route = await self._status_session_db_facts(
            session_entry.session_id
        )
        # Prefer the live or cached agent (actual runtime route + context compressor); fall back
        # to an active /model override, then SessionDB metadata + last_prompt_tokens so /status
        # stays useful between turns. Rehydrate first so this precedence survives gateway restarts.
        status_agent = agent if is_running else self._cached_agent_for(session_key)
        self._rehydrate_session_model_override(session_key)
        active_override = self._session_model_override(session_key) or {}
        if not active_override:
            # Fork rehydrate fails closed on a credential-unresolvable persisted identity (the turn
            # resolver must not run it); /status is display-only and still shows the user's
            # committed /model pin rather than the pre-switch DB row (upstream contract).
            try:  # pins sqlite read: off-loop, as /fast does
                _lookup = await asyncio.to_thread(self._persisted_session_route_identity, session_key)
            except Exception:
                _lookup = None
            if _lookup is not None and _lookup.state == "valid" and _lookup.identity:
                active_override = {k: v for k, v in dict(_lookup.identity).items() if k in ("model", "provider")}
        model_name, provider_name, context_used, context_total, route = _status_model_route(
            status_agent, active_override, persisted_route, session_row, session_entry
        )
        # -1 on the compressor after a compaction means "post-compaction estimate": prefer that
        # reading over the stored pre-compaction figure _status_model_route fell back to (t_64728f32).
        _ctx = getattr(status_agent, "context_compressor", None) if status_agent is not None else None
        context_estimated = False
        if _ctx is not None:
            from gateway.runtime_footer import live_context_tokens

            _reading = live_context_tokens(_ctx)
            context_estimated = _reading.estimated
            if _reading.post_compaction:
                context_used = max(0, _reading.tokens or 0)
        if not context_total and model_name:
            # Same resolver /context uses (off-loop: it can probe /models). A window the resolver only
            # invented (unknown model → DEFAULT_FALLBACK_CONTEXT) stays hidden rather than being shown
            # as a real limit; the occupancy-only line below is honest for that case.
            resolved = await self._resolve_route_context(source, model_name, route)
            if resolved is not None and resolved.context_source != "default":
                context_total = _int_value(resolved.context_length)

        fields = build_status_fields(
            session_entry.session_id, None, session_row, title=title, model=model_name, provider=provider_name,
            created=session_entry.created_at, last_activity=session_entry.updated_at,
            tokens=db_total_tokens, agent_running=is_running,
        )
        lines = [t("gateway.status.header"), "",
                 t("gateway.status.session_id", session_id=fields["session_id"])]
        if fields["title"]:
            lines.append(t("gateway.status.title", title=fields["title"]))
        lines += [t("gateway.status.created", timestamp=fields["created"]),
                  t("gateway.status.last_activity", timestamp=fields["last_activity"])]
        if fields["model"] and fields["provider"]:
            lines.append(t("gateway.status.model_provider", model=fields["model"], provider=fields["provider"]))
        elif fields["model"]:
            lines.append(t("gateway.status.model", model=fields["model"]))
        try:
            from hermes_cli.anon_auth import free_tier_route

            free_tier_active = await self._run_in_executor_with_context(free_tier_route)
            if free_tier_active:
                lines.append(t("gateway.status.free_tier"))
        except Exception:
            pass
        from agent.context_breakdown import context_display_source
        # "~": upstream's preflight-seed provenance OR the fork's post-compaction estimate.
        mark = "~" if (context_estimated or context_display_source(_ctx) != "provider_usage") else ""
        if context_total:
            pct = min(100, round((context_used / context_total) * 100))
            lines.append(t("gateway.status.context", used=mark + _fmt(context_used), total=_fmt(context_total),
                           pct=f"{mark}{pct}"))
        elif context_used:
            # Template already carries "~" (a stored figure is an estimate by construction).
            lines.append(t("gateway.status.context_used", used=_fmt(context_used)))
        state = t("gateway.status.state_yes") if fields["agent_running"] else t("gateway.status.state_no")
        lines += [t("gateway.status.tokens", tokens=fields["tokens"]),
                  t("gateway.status.agent_running", state=state)]
        if queue_depth:
            lines.append(t("gateway.status.queued", count=queue_depth))
        if source.platform == Platform.MATRIX:
            scope = getattr(self.adapters.get(Platform.MATRIX), "_matrix_session_scope",
                            os.getenv("MATRIX_SESSION_SCOPE", "auto"))
            lines += [
                "",
                t("gateway.status.matrix_scope_header"),
                t("gateway.status.matrix_scope_room", room=source.chat_name or source.chat_id),
                t("gateway.status.matrix_scope_room_id", room_id=source.chat_id),
                t("gateway.status.matrix_scope_thread", thread_id=source.thread_id or t("gateway.shared.none_value")),
                t("gateway.status.matrix_scope_mode", scope=scope),
                t("gateway.status.matrix_scope_key",
                  session_key=self._redact_matrix_session_key(session_key)),
            ]
        lines += ["", t("gateway.status.platforms", platforms=', '.join(p.value for p in self.adapters))]
        return "\n".join(lines)

    async def _handle_usage_command(self, event: MessageEvent) -> str:
        """Handle /usage command -- show token usage for the current session.

        Checks both _running_agents (mid-turn) and _agent_cache (between turns)
        so that rate limits, cost estimates, and detailed token breakdowns are
        available whenever the user asks, not only while the agent is running.
        """
        from gateway.run import _AGENT_PENDING_SENTINEL
        source = event.source
        session_key = self._session_key_for_source(source)

        # `/usage reset [--force]` — redeem one banked Codex rate-limit reset
        # credit. Parsed before the display path so it never mixes with the
        # stats rendering below.
        raw_args = event.get_command_args().strip()
        args = [a.lower() for a in raw_args.split()] if raw_args else []
        wants_reset = bool(args) and args[0] == "reset"
        if args and not wants_reset:
            return t("gateway.usage.unknown_subcommand", args=raw_args)

        # Try running agent first (mid-turn), then cached agent (between turns)
        agent = self._running_agents.get(session_key)
        if not agent or agent is _AGENT_PENDING_SENTINEL:
            _cache_lock = getattr(self, "_agent_cache_lock", None)
            _cache = getattr(self, "_agent_cache", None)
            if _cache_lock and _cache is not None:
                with _cache_lock:
                    cached = _cache.get(session_key)
                    if cached:
                        agent = cached[0]

        # Resolve provider/base_url/api_key for the account-usage fetch.
        # Prefer the live agent; fall back to persisted billing data on the
        # SessionDB row so `/usage` still returns account info between turns
        # when no agent is resident.
        provider = getattr(agent, "provider", None) if agent and agent is not _AGENT_PENDING_SENTINEL else None
        base_url = getattr(agent, "base_url", None) if agent and agent is not _AGENT_PENDING_SENTINEL else None
        api_key = getattr(agent, "api_key", None) if agent and agent is not _AGENT_PENDING_SENTINEL else None
        if not provider and getattr(self, "_session_db", None) is not None:
            provider, base_url = await self._persisted_billing_route(source)
        if not provider:
            # Fresh or evicted session with no persisted route (e.g. /usage right after login):
            # fall back to the configured provider, as /status does, so account limits such as
            # Codex subscription windows still render from on-disk credentials (#15167).
            provider = await _quiet(lambda: asyncio.to_thread(_configured_provider)) or None

        if wants_reset:
            normalized_provider = str(provider or "").strip().lower()
            if normalized_provider != "openai-codex":
                return t("gateway.usage.reset_wrong_provider")
            force = "--force" in args[1:]
            from agent.account_usage import redeem_codex_reset_credit

            result = await asyncio.to_thread(
                redeem_codex_reset_credit,
                base_url=base_url,
                api_key=api_key,
                force=force,
            )
            return result.message

        # Account limits — the DRY compact multi-subscription block (one line per
        # sub: all Claude subs + Codex), sourced from the SAME claude_usage_lib
        # render the full /claude-usage uses, so add/remove a subscription and
        # both surfaces track automatically. Falls back to the single-provider
        # snapshot when the shared lib isn't available. Off the event loop;
        # fail-open (account_lines stays []).
        account_lines: list[str] = []
        credits_lines: list[str] = []
        try:
            account_lines = await asyncio.to_thread(self._compact_account_limit_lines)
        except Exception:
            account_lines = []
        if not account_lines and provider:
            # Fallback: the resident provider's own snapshot (legacy single-sub).
            try:
                account_snapshot = await asyncio.to_thread(
                    fetch_account_usage,
                    provider,
                    base_url=base_url,
                    api_key=api_key,
                )
            except Exception:
                account_snapshot = None
            if account_snapshot:
                account_lines = render_account_usage_lines(account_snapshot, markdown=True)

        # ── Nous credits magnitudes + monthly-grant % gauge ─────────────
        # Shared with the CLI / TUI /usage block via nous_credits_lines(): a single
        # auth-gate + portal-fetch + render path (which also honors the dev fixture).
        # Run off the event loop. The helper gates on "a Nous account is logged in"
        # — NOT the inference provider and NOT nested under `if provider:` — so a
        # Nous-credentialled user running inference elsewhere (or with none resident)
        # still sees their balance. NO recovery trigger: messaging binds no notice
        # consumer, so /usage only displays. Fail-open: never break /usage.
        try:
            from agent.account_usage import nous_credits_lines

            credits_lines = await asyncio.to_thread(nous_credits_lines, markdown=True)
        except Exception:
            credits_lines = []  # fail-open: never break /usage

        if agent and hasattr(agent, "session_total_tokens") and agent.session_api_calls > 0:
            lines = []

            # Rate limits (provider throttling headroom) are surfaced DOWN next to
            # the Account-limits section (both are "how close am I to a ceiling"),
            # not at the top — see below. Render them as a small block rather than
            # a cramped pipe-separated row so the ceiling sections stay readable.
            rate_limit_lines: list[str] = []
            rl_state = agent.get_rate_limit_state()
            if rl_state and rl_state.has_data:
                from agent.rate_limit_tracker import format_rate_limit_compact
                _compact_rl = format_rate_limit_compact(rl_state)
                _rl_parts = [p.strip() for p in str(_compact_rl).split("|") if p.strip()]
                if _rl_parts:
                    rate_limit_lines.append(t("gateway.usage.rate_limits", state="").rstrip())
                    rate_limit_lines.extend(f"• {p}" for p in _rl_parts)

            # The full last-turn card (PRD usage-format-codex Part A) is the SAME
            # renderer /context uses, and already carries Model / Agent / Session /
            # API Calls / tokens / Compressions — so the old session header +
            # Model/Total/API-calls lines were redundant with it and were removed
            # (Ace, 2026-06-30). The card (cost, tokens in/out with finished/
            # unfinished + uncached, context window, cached, compressions, session)
            # follows directly.

            # The rich /context last-turn card for THIS channel (replaces the old
            # hand-built input/output + char/4 composition block). When the
            # blackbox store has no turn recorded for this channel yet (e.g. the
            # agent is resident but its first turn hasn't landed in the store, or
            # tests), fall back to the resident agent's OWN session counters so the
            # card is never emptier than the pre-card display. The live compressor's
            # compression_count is threaded into the card (• Compressions: N row,
            # right after Cached) instead of being appended orphaned below.
            def _as_int(v):
                try:
                    return int(v or 0)
                except (TypeError, ValueError):
                    return 0
            agent_thin = _resident_thin_snapshot(agent, _as_int)
            ctx = agent.context_compressor
            _comp_count = _as_int(getattr(ctx, "compression_count", 0))

            # Per-category context breakdown (estimated — chars/4 heuristic) goes
            # FIRST (Ace 2026-06-30): "where is my CURRENT context budget going"
            # (system prompt / tools / rules / skills / MCP / subagents / memory /
            # conversation) frames the rest. Same engine the desktop popover uses
            # (upstream PR #54907/#55204). Fail-open: error → no breakdown.
            breakdown_lines = await asyncio.to_thread(
                self._context_breakdown_lines, agent, source
            )
            if breakdown_lines:
                lines.extend(breakdown_lines)

            # The full last-turn card (PRD usage-format-codex Part A) — the SAME
            # renderer /context uses — answers "what the last turn cost". When the
            # blackbox store has no turn for this channel yet, fall back to the
            # resident agent's OWN session counters so the card is never emptier
            # than the pre-card display. compression_count is threaded in (•
            # Compressions: N row, after Cached).
            try:
                card = self._render_last_turn_card(
                    source, agent_thin,
                    fallback_label="session totals; first turn not yet recorded",
                    compressions=_comp_count,
                )
            except Exception:
                card = []
            if card:
                # The card carries a leading blank line — keep it as a separator
                # from the breakdown above, but strip it when the card is the very
                # first block (no breakdown rendered) so /usage doesn't open blank.
                if not breakdown_lines and card and card[0] == "":
                    card = card[1:]
                lines.extend(card)

            # Rate limits + Account limits together — both answer "how close am I
            # to a ceiling": rate limits = provider throttling headroom (this
            # minute/hour), account limits = subscription quota (5h/7d windows).
            if rate_limit_lines or account_lines:
                lines.append("")
            if rate_limit_lines:
                lines.extend(rate_limit_lines)
            if rate_limit_lines and account_lines:
                lines.append("")
            if account_lines:
                lines.extend(account_lines)
            if credits_lines:
                lines.append("")
                lines.extend(credits_lines)

            return "\n".join(lines)

        # No agent at all -- check session history for a rough count
        session_entry = await self.async_session_store.get_or_create_session(source)

        # Eviction-safe last-turn card: even with no resident agent, render the
        # full /context last-turn card for THIS channel from the blackbox store
        # (PRD usage-format-codex Part A). Falls back to the thin persisted
        # get_last_turn_usage snapshot (reworded) when blackbox has no matching
        # turn — the helper handles both + the channel-match guard.
        last_turn_lines: list[str] = []
        thin_snap = None
        if getattr(self, "_session_db", None) is not None:
            try:
                # _session_db is an AsyncSessionDB facade (upstream d153918f1):
                # every method is offloaded via asyncio.to_thread and returns a
                # coroutine, so this MUST be awaited or `thin_snap` is a coroutine
                # (truthy) and downstream .get(...) blows up.
                thin_snap = await self._session_db.get_last_turn_usage(session_entry.session_id)
            except Exception:
                thin_snap = None
        try:
            last_turn_lines = self._render_last_turn_card(source, thin_snap)
        except Exception:
            last_turn_lines = []

        history = await self.async_session_store.load_transcript(session_entry.session_id)
        if history:
            from agent.model_metadata import estimate_messages_tokens_rough
            msgs = [m for m in history if m.get("role") in {"user", "assistant"} and m.get("content")]
            approx = estimate_messages_tokens_rough(msgs)
            lines = [
                t("gateway.usage.header_session_info"),
                t("gateway.usage.label_messages", count=len(msgs)),
                t("gateway.usage.label_estimated_context", count=f"{approx:,}"),
                t("gateway.usage.detailed_after_first"),
            ]
            if last_turn_lines:
                lines.append("")
                lines.extend(last_turn_lines)
            if account_lines:
                lines.append("")
                lines.extend(account_lines)
            if credits_lines:
                lines.append("")
                lines.extend(credits_lines)
            return "\n".join(lines)
        if last_turn_lines:
            if account_lines:
                last_turn_lines.append("")
                last_turn_lines.extend(account_lines)
            if credits_lines:
                last_turn_lines.append("")
                last_turn_lines.extend(credits_lines)
            return "\n".join(last_turn_lines)
        if account_lines or credits_lines:
            # account-only, credits-only, or both — joined with a blank divider.
            parts = list(account_lines)
            if credits_lines:
                if parts:
                    parts.append("")
                parts.extend(credits_lines)
            return "\n".join(parts)
        return t("gateway.usage.no_data")

    async def _handle_fast_command(self, event: MessageEvent) -> Optional[str]:
        """Handle /fast — mirror the CLI Priority Processing toggle in gateway chats."""
        from gateway.run import _load_gateway_config
        from hermes_cli.models import resolve_fast_mode_capability

        raw_args = event.get_command_args().strip().lower()
        # Reuse the /reasoning arg parser: strips --global (any position),
        # normalizes unicode dashes.
        args, persist_global = self._parse_reasoning_command_args(raw_args)
        session_key = self._session_key_for_source(event.source)
        self._service_tier = self._resolve_session_service_tier(
            session_key=session_key
        )

        user_config = _load_gateway_config()
        session_key = self._session_key_for_source(event.source)
        persisted_lookup = await asyncio.to_thread(
            self._persisted_session_route_identity, session_key
        )
        if persisted_lookup.state == "unavailable":
            return t("gateway.fast.preference_unavailable", route="<unreadable>")
        model, provider, api_mode = self._resolve_configured_session_route_identity(
            source=event.source,
            session_key=session_key,
            user_config=user_config,
            persisted_route_lookup=persisted_lookup,
        )
        capability = resolve_fast_mode_capability(
            model=model,
            provider=provider,
            api_mode=api_mode,
        )
        ultrafast_capability = resolve_fast_mode_capability(
            model=model,
            provider=provider,
            api_mode=api_mode,
            tier="ultrafast",
        )
        route = f"{provider or '<unknown>'}/{model or '<unset>'}"
        persisted_preference = persisted_lookup.identity
        preference_unavailable = session_key in getattr(
            self, "_session_model_override_unavailable", set()
        )
        logger.debug(
            "Fast capability route=%s api_mode=%s family=%s supported=%s",
            route,
            api_mode or "",
            capability.family,
            capability.supported,
        )

        def _apply_fast_selection(value: str, persist: bool = False) -> Optional[str]:
            """Apply a /fast argument (typed or picked) and return the reply.

            Session-scoped by default; ``persist`` (``--global``) writes
            agent.service_tier to config.yaml. Route-capability aware: an
            unsupported route refuses to enable Fast, and the reply names the
            resolved family/route (fork route-preference response layer).
            """
            if value in {"fast", "on"}:
                if persist:
                    # ``--global`` lane (upstream scoping): the persisted
                    # agent.service_tier default applies to the configured
                    # gateway route, so gate on that route rather than the
                    # current session route.
                    from gateway.run import _resolve_gateway_model
                    from hermes_cli.models import (
                        resolve_fast_mode_capability_for_configured_route,
                    )
                    _, global_provider, global_api_mode = (
                        self._configured_route_identity(user_config)
                    )
                    if not resolve_fast_mode_capability_for_configured_route(
                        model=_resolve_gateway_model(user_config),
                        provider=global_provider,
                        api_mode=global_api_mode,
                    ).supported:
                        return t("gateway.fast.not_supported")
                else:
                    if persisted_preference and preference_unavailable:
                        return t("gateway.fast.preference_unavailable", route=route)
                    if not capability.supported:
                        return t(
                            "gateway.fast.route_unavailable", reason=capability.reason
                        )
                tier = "priority"
                saved_value = "fast"
                label = t("gateway.fast.label_fast")
            elif value == "ultrafast":
                if persist:
                    from gateway.run import _resolve_gateway_model
                    from hermes_cli.models import (
                        resolve_fast_mode_capability_for_configured_route,
                    )
                    _, global_provider, global_api_mode = (
                        self._configured_route_identity(user_config)
                    )
                    global_capability = resolve_fast_mode_capability_for_configured_route(
                        model=_resolve_gateway_model(user_config),
                        provider=global_provider,
                        api_mode=global_api_mode,
                        tier="ultrafast",
                    )
                    if not global_capability.supported:
                        return t(
                            "gateway.fast.route_unavailable",
                            reason=global_capability.reason,
                        )
                else:
                    if persisted_preference and preference_unavailable:
                        return t("gateway.fast.preference_unavailable", route=route)
                    if not ultrafast_capability.supported:
                        return t(
                            "gateway.fast.route_unavailable",
                            reason=ultrafast_capability.reason,
                        )
                tier = "ultrafast"
                saved_value = "ultrafast"
                label = t("gateway.fast.label_ultrafast")
            elif value in {"normal", "off"}:
                tier = None
                saved_value = "normal"
                label = t("gateway.fast.label_normal")
            else:
                return t("gateway.fast.unknown_arg", arg=value)
            self._service_tier = tier
            family = (
                ultrafast_capability.family if tier == "ultrafast" else capability.family
            )
            family_label = t(f"gateway.fast.family_{family}")
            if persist:
                if self._save_gateway_config_key("agent.service_tier", saved_value):
                    # Global write supersedes any session override.
                    self._set_session_service_tier_override(
                        session_key, None, clear=True
                    )
                    self._persist_session_service_tier(
                        session_key, None, clear=True
                    )
                    self._evict_cached_agent(session_key)
                    return t("gateway.fast.route_saved", family=family_label, label=label)
                # Config write failed. ``self._service_tier`` is process-wide
                # runner state, so the applied change is honestly gateway-
                # process-wide until restart — NOT this-session-only.
                self._evict_cached_agent(session_key)
                return t(
                    "gateway.fast.route_session_only", family=family_label, label=label
                )
            # Session-scoped (default): record the override in memory and
            # durably persist it so it survives a restart. A persistence
            # failure keeps the in-memory override live but the change is then
            # honestly gateway-process-wide until restart.
            self._set_session_service_tier_override(session_key, tier)
            self._evict_cached_agent(session_key)
            if self._persist_session_service_tier(session_key, tier):
                return t("gateway.fast.route_saved", family=family_label, label=label)
            return t(
                "gateway.fast.route_session_only", family=family_label, label=label
            )

        if not args or args == "status":
            # Interactive picker on platforms that support it (parity with the
            # /model + /reasoning pickers). Gated on the configured gateway
            # route's fast support; falls through to the text status card when
            # the platform has no picker or the send fails. Session-scoped by
            # default; `/fast --global` persists agent.service_tier.
            from gateway.run import _resolve_gateway_model
            from hermes_cli.models import (
                resolve_fast_mode_capability_for_configured_route,
            )

            _fast_supported = False
            try:
                _, _cfg_provider, _cfg_api_mode = self._configured_route_identity(
                    user_config
                )
                _fast_supported = resolve_fast_mode_capability_for_configured_route(
                    model=_resolve_gateway_model(user_config),
                    provider=_cfg_provider,
                    api_mode=_cfg_api_mode,
                ).supported
            except Exception:
                _fast_supported = False

            if _fast_supported:
                _is_fast = self._service_tier in {"priority", "ultrafast"}

                def _apply_fast_choice(value: str) -> str:
                    """Apply a picker tap. The picker was already gated on fast
                    support at display time, so it trusts that gate rather than
                    re-checking the session route capability."""
                    if value in {"fast", "on"}:
                        tier = "priority"
                        saved_value = "fast"
                        label = t("gateway.fast.label_fast")
                    elif value in {"normal", "off"}:
                        tier = None
                        saved_value = "normal"
                        label = t("gateway.fast.label_normal")
                    else:
                        return t("gateway.fast.unknown_arg", arg=value)
                    self._service_tier = tier
                    family_label = t(f"gateway.fast.family_{capability.family}")
                    if persist_global:
                        if self._save_gateway_config_key(
                            "agent.service_tier", saved_value
                        ):
                            self._set_session_service_tier_override(
                                session_key, None, clear=True
                            )
                            self._persist_session_service_tier(
                                session_key, None, clear=True
                            )
                            self._evict_cached_agent(session_key)
                            return t(
                                "gateway.fast.route_saved",
                                family=family_label,
                                label=label,
                            )
                        self._evict_cached_agent(session_key)
                        return t(
                            "gateway.fast.route_session_only",
                            family=family_label,
                            label=label,
                        )
                    self._set_session_service_tier_override(session_key, tier)
                    self._evict_cached_agent(session_key)
                    if self._persist_session_service_tier(session_key, tier):
                        return t(
                            "gateway.fast.route_saved",
                            family=family_label,
                            label=label,
                        )
                    return t(
                        "gateway.fast.route_session_only",
                        family=family_label,
                        label=label,
                    )

                async def _on_fast_choice(_chat_id: str, value: str) -> str:
                    return _apply_fast_choice(value)

                _fast_status = t(
                    "gateway.fast.state_on" if _is_fast else "gateway.fast.state_off"
                )
                picker_sent = await self._try_send_choice_picker(
                    event,
                    session_key,
                    title=t("gateway.fast.picker_title", mode=_fast_status),
                    choices=[
                        {
                            "value": "fast",
                            "label": t("gateway.fast.choice_fast"),
                            "is_current": _is_fast,
                        },
                        {
                            "value": "normal",
                            "label": t("gateway.fast.choice_normal"),
                            "is_current": not _is_fast,
                        },
                    ],
                    on_choice_selected=_on_fast_choice,
                )
                if picker_sent:
                    return None  # Picker sent — adapter handles the response

            if persisted_preference and preference_unavailable:
                return t(
                    "gateway.fast.preference_unavailable",
                    route=route,
                )
            status_capability = (
                ultrafast_capability
                if self._service_tier == "ultrafast"
                or (
                    self._service_tier is None
                    and not capability.supported
                    and ultrafast_capability.supported
                )
                else capability
            )
            if not status_capability.supported:
                key = (
                    "gateway.fast.preference_off"
                    if persisted_preference
                    else "gateway.fast.route_off"
                )
                return t(key, reason=status_capability.reason)
            state = t(
                "gateway.fast.state_on"
                if self._service_tier in {"priority", "ultrafast"}
                else "gateway.fast.state_off"
            )
            family_label = t(f"gateway.fast.family_{status_capability.family}")
            return t(
                "gateway.fast.preference_status"
                if persisted_preference
                else "gateway.fast.route_status",
                state=state,
                family=family_label,
                route=route,
            )

        return _apply_fast_selection(args, persist=persist_global)

    async def _handle_reasoning_command(self, event: MessageEvent) -> Optional[str]:
        """Handle /reasoning command — manage reasoning effort and display toggle."""
        from gateway.run import _platform_config_key
        from hermes_constants import VALID_REASONING_EFFORTS

        raw_args = event.get_command_args().strip()
        args, persist_global = self._parse_reasoning_command_args(raw_args)
        # Normalize (Telegram DM topic recovery) so the override key matches the next turn's.
        # See #30479.
        _reasoning_source = await asyncio.to_thread(self._normalize_source_for_session_key, event.source)
        session_key = self._session_key_for_source(_reasoning_source)
        self._show_reasoning = self._load_show_reasoning()
        # Effective model (session /model override wins) so per-model reasoning_overrides display.
        _session_model = str(
            ((getattr(self, "_session_model_overrides", {}) or {}).get(session_key) or {}).get("model") or ""
        )
        self._reasoning_config = self._resolve_session_reasoning_config(
            source=event.source, session_key=session_key, model=_session_model,
        )
        platform_key = _platform_config_key(event.source.platform)
        # Deliberate /reasoning switches post ONE channel-visible announce line (P2 reasoning-only
        # change announce, gateway/run.py::_announce_switch): capture the resolved label before and
        # after the applier so a no-op (same label) stays silent.
        async def _apply_and_announce(value: str, *, persist: bool = False) -> str:
            _old_effort = self._resolved_effort_label(source=event.source, session_key=session_key)
            reply = self._apply_reasoning_selection(session_key, platform_key, value, persist_global=persist)
            _new_effort = self._resolved_effort_label(source=event.source, session_key=session_key)
            await self._announce_switch(event.source, "Reasoning", _old_effort, _new_effort)
            return reply

        if raw_args:  # typed path — same applier the picker uses
            return await _apply_and_announce(args, persist=persist_global)
        rc = self._reasoning_config
        # Labels tell the truth about the route: a Hermes-internal step (``ultra``) that the wire
        # clamps is shown as "ultra (sends max on this route)" instead of a distinct level (#61634).
        from agent.reasoning_effort import effort_display_label
        from gateway.run import _load_gateway_config
        _session_route = ((getattr(self, "_session_model_overrides", {}) or {}).get(session_key) or {})
        _model_cfg = {}
        with contextlib.suppress(Exception):  # fail-open on config read errors, like /model does
            _model_cfg = _load_gateway_config(config_path=self.config_path).get("model", {}) or {}
        _route = (
            _session_route.get("provider") or _model_cfg.get("provider"),
            _session_model or _model_cfg.get("default") or _model_cfg.get("model"),
        )
        if rc is None:
            level, current_effort = t("gateway.reasoning.level_default"), "medium"
        elif rc.get("enabled") is False:
            level, current_effort = t("gateway.reasoning.level_disabled"), "none"
        else:
            current_effort = rc.get("effort", "medium")
            level = effort_display_label(current_effort, *_route)
        display_state = t("gateway.reasoning.display_on") if self._show_reasoning else t("gateway.reasoning.display_off")
        has_session_override = session_key in (getattr(self, "_session_reasoning_overrides", {}) or {})
        scope = t("gateway.reasoning.scope_session") if has_session_override else t("gateway.reasoning.scope_global")

        async def _on_reasoning_choice(_chat_id: str, value: str) -> str:
            return await _apply_and_announce(value)

        picker_sent = await self._try_send_choice_picker(
            event,
            session_key,
            title=t("gateway.reasoning.picker_title", level=level, scope=scope, display=display_state),
            choices=[
                {"value": "none", "label": t("gateway.reasoning.choice_none"), "is_current": current_effort == "none"},
                *({"value": lv, "label": effort_display_label(lv, *_route), "is_current": lv == current_effort}
                  for lv in VALID_REASONING_EFFORTS),
                *({"value": v, "label": t(f"gateway.reasoning.choice_{v}"), "is_current": False}
                  for v in ("reset", "show", "hide")),
            ],
            on_choice_selected=_on_reasoning_choice,
        )
        if picker_sent:
            return None  # Picker sent — adapter handles the response
        return t("gateway.reasoning.status", level=level, scope=scope, display=display_state)

    async def _handle_model_command_locked(self, event: MessageEvent) -> Optional[str]:
        """Handle /model command — switch model.

        Supports:
          /model                              — interactive picker (Telegram/Discord) or text list
          /model <name>                       — switch model (this session only)
          /model <name> --once                — switch for the next turn only
          /model <name> --session             — switch for this session only (explicit)
          /model <name> --global              — switch and persist to config.yaml
          /model <name> --provider <provider> — switch provider + model
          /model --provider <provider>        — switch to provider, auto-detect model
        """
        from gateway.run import _hermes_home, _load_gateway_config
        from hermes_cli.model_switch import (
            switch_model as _switch_model, parse_model_switch_args,
            resolve_persist_behavior,
            list_authenticated_providers,
        )
        # From the source module (not model_switch's re-export) so tests patching
        # ``hermes_cli.model_switch_providers.list_picker_providers`` intercept, as for upstream's sibling.
        from hermes_cli.model_switch_providers import list_picker_providers
        from hermes_cli.providers import get_label

        raw_args = event.get_command_args().strip()
        source = event.source
        _command_profile_home = None
        if getattr(getattr(self, "config", None), "multiplex_profiles", False):
            _command_profile_home = getattr(
                self, "_resolve_profile_home_for_source"
            )(source)

        # Parse --provider, --global, --session, --once, and --refresh flags
        # via the shared single-owner parser (hermes_cli.model_switch).
        request = parse_model_switch_args(raw_args)
        model_input = request.target
        explicit_provider = request.explicit_provider
        is_global_flag = request.is_global
        force_refresh = request.force_refresh
        is_session = request.is_session
        one_turn = request.is_once
        if request.errors:
            # Gateway decoration: "❌ " prefix over the canonical error copy.
            return f"❌ {request.error_messages()[0]}"
        persist_global = resolve_persist_behavior(
            is_global_flag,
            is_session,
            is_once=one_turn,
            explicit_provider=explicit_provider,
        )

        # --refresh: bust the disk cache so the picker shows live data.
        if force_refresh:
            try:
                from hermes_cli.models import clear_provider_models_cache
                clear_provider_models_cache()
            except Exception:
                pass

        # Read current model/provider from config
        current_model = ""
        current_provider = "openrouter"
        current_base_url = ""
        current_api_key = ""
        user_provs = None
        custom_provs = None
        excluded_provs = []
        config_path = (_command_profile_home or _hermes_home) / "config.yaml"
        try:
            cfg = _load_gateway_config(config_path=config_path)
            if cfg:
                model_cfg = cfg.get("model", {})
                if isinstance(model_cfg, dict):
                    current_model = model_cfg.get("default", "")
                    current_provider = model_cfg.get("provider", current_provider)
                    current_base_url = model_cfg.get("base_url", "")
                user_provs = cfg.get("providers")
                try:
                    from hermes_cli.config import get_compatible_custom_providers
                    custom_provs = get_compatible_custom_providers(cfg)
                except Exception:
                    custom_provs = cfg.get("custom_providers")
                _excl = cfg.get("model_catalog", {}).get("excluded_providers")
                if isinstance(_excl, list):
                    excluded_provs = _excl
        except Exception:
            pass

        # Check for session override. Normalize the source the same way a normal
        # message turn does
        # (Telegram DM topic recovery) before deriving the override key, so
        # the override is stored under the key the next message turn reads
        # (#30479).
        source = await asyncio.to_thread(self._normalize_source_for_session_key, source)
        session_key = self._session_key_for_source(source)
        configured_model = current_model
        configured_provider = current_provider
        override = self._session_model_overrides.get(session_key, {})
        restore_snapshot = (
            self._snapshot_session_model_override(session_key) if one_turn else None
        )
        if override:
            current_model = override.get("model", current_model)
            current_provider = override.get("provider", current_provider)
            current_base_url = override.get("base_url", current_base_url)
            current_api_key = override.get("api_key", current_api_key)

        # The LIVE agent is the ground truth for what we are switching FROM.
        # config.yaml + the session override describe the route that was
        # *requested*; a failover / model fallback can move the cached agent off
        # both without touching either. Measured 2026-09-19: a Telegram session
        # running claude-opus-5 on claude-apx-1 (visible in every API-call log
        # line) got the note "switched from claude-fable-5-1 to claude-fable-5-1
        # via Claude BPX-8" because the stale override still said fable-5-1 —
        # while the in-place switch log, which reads the agent, correctly said
        # "claude-opus-5 (claude-apx-1) -> claude-fable-5-1 (claude-bpx-8)".
        # Reading the agent here fixes the note, the picker's captured
        # "current", the announce fallback and the bare-model resolution
        # provider in one place. Best-effort: a cache hiccup must not break
        # the switch.
        _live_agent = None
        try:
            _live_lock = getattr(self, "_agent_cache_lock", None)
            _live_cache = getattr(self, "_agent_cache", None)
            _live_entry = None
            if _live_lock and _live_cache is not None:
                with _live_lock:
                    _live_entry = _live_cache.get(session_key)
            _live_agent = _live_entry[0] if _live_entry else None
            if _live_agent is not None:
                current_model = getattr(_live_agent, "model", None) or current_model
                current_provider = getattr(_live_agent, "provider", None) or current_provider
        except Exception:
            logger.debug("live-agent route read skipped (non-fatal)", exc_info=True)

        # Explicit preference clear is authoritative. It clears both the
        # identity field and the legacy sanitized mirror so a later /new or
        # restart cannot resurrect the prior pin.
        if model_input.strip().lower() == "reset" and not explicit_provider:
            try:
                await self._persist_session_model_override(
                    session_key,
                    None,
                    require_persistence=True,
                )
            except Exception:
                logger.warning("Model route reset persistence failed")
                return (
                    "Model session override was not cleared because the session "
                    "store could not persist the reset. No route change was made."
                )
            getattr(self, "_pending_model_notes", {}).pop(session_key, None)
            getattr(self, "_last_resolved_model", {}).pop(session_key, None)
            _fb_cleared = await asyncio.to_thread(
                self._clear_fallback_for_user_route,
                session_key,
                _live_agent,
            )
            try:
                self._evict_cached_agent(session_key)
            except Exception:
                pass
            await self._announce_switch(
                event.source,
                "Model",
                f"{current_provider}/{current_model}",
                f"{configured_provider}/{configured_model}",
            )
            _reset_reply = (
                "Model session override cleared; using configured route "
                f"`{configured_provider}/{configured_model}`."
            )
            if _fb_cleared:
                _reset_reply += "\n" + self._fallback_cleared_line(
                    configured_provider, configured_model)
            return _reset_reply

        # No args: show interactive picker (Telegram/Discord) or text list
        if not model_input and not explicit_provider:
            # Try interactive picker if the platform supports it
            adapter = getattr(self, "_adapter_for_source")(source)
            has_picker = (
                adapter is not None
                and getattr(type(adapter), "send_model_picker", None) is not None
            )

            if has_picker:
                try:
                    # Offload blocking provider-listing (can fall through to a
                    # synchronous urllib HTTP fetch on a stale cache) off the
                    # event loop so the gateway doesn't freeze. See #41289.
                    providers = await asyncio.to_thread(
                        list_picker_providers,
                        current_provider=current_provider,
                        current_base_url=current_base_url,
                        current_model=current_model,
                        user_providers=user_provs,
                        custom_providers=custom_provs,
                        max_models=50,
                        include_moa=True,
                        excluded_providers=excluded_provs,
                        # #74003 (upstream): the chat /model reply is a READ path — cache-only
                        # catalogs, and live-probe only the selected custom endpoint, like the GUI.
                        non_blocking_catalogs=True,
                        probe_custom_providers=False,
                        probe_current_custom_provider=True,
                    )
                except Exception:
                    providers = []

                if providers:
                    # Build a callback closure for when the user picks a model.
                    # Captures self + locals needed for the switch logic.
                    _self = self
                    _session_key = session_key
                    _cur_model = current_model
                    _cur_provider = current_provider
                    _cur_base_url = current_base_url
                    _cur_api_key = current_api_key
                    _picker_profile_home = _command_profile_home

                    async def _on_model_selected(
                        _chat_id: str, model_id: str, provider_slug: str
                    ) -> str:
                        """Perform the model switch and return confirmation text."""
                        skew_error = _model_switch_skew_guard()
                        if skew_error:
                            return skew_error
                        # Offload the switch off the event loop — switch_model()
                        # can fall through to a synchronous models.dev HTTP fetch
                        # (requests.get, 15s timeout) on a cold/expired cache,
                        # which freezes the gateway otherwise. See #20525, #41289.
                        result = await asyncio.to_thread(
                            _switch_model,
                            raw_input=model_id,
                            current_provider=_cur_provider,
                            current_model=_cur_model,
                            current_base_url=_cur_base_url,
                            current_api_key=_cur_api_key,
                            is_global=persist_global,
                            explicit_provider=provider_slug,
                            user_providers=user_provs,
                            custom_providers=custom_provs,
                        )
                        if not result.success:
                            return t("gateway.model.error_prefix", error=result.error_message)

                        try:
                            from hermes_cli.context_switch_guard import (
                                enrich_model_switch_warnings_for_gateway,
                            )

                            # Offload: merge_preflight_compression_warning()
                            # calls the sync resolve_display_context_length()
                            # provider probe ladder — must not run on the loop.
                            await asyncio.to_thread(
                                enrich_model_switch_warnings_for_gateway,
                                result,
                                _self,
                                session_key=_session_key,
                                source=event.source,
                                custom_providers=custom_provs,
                                load_gateway_config=_load_gateway_config,
                            )
                        except Exception as exc:
                            logger.debug("preflight-compression switch warning failed: %s", exc)

                        # Update cached agent in-place
                        _switch_announce_kwargs = {}
                        cached_entry = None
                        _cache_lock = getattr(_self, "_agent_cache_lock", None)
                        _cache = getattr(_self, "_agent_cache", None)
                        if _cache_lock and _cache is not None:
                            with _cache_lock:
                                cached_entry = _cache.get(_session_key)
                        # Explicit route beats an automatic one (t_b2e9bb23).
                        _fb_cleared = await asyncio.to_thread(
                            _self._clear_fallback_for_user_route,
                            _session_key,
                            cached_entry[0] if cached_entry else None,
                        )
                        if cached_entry and cached_entry[0] is not None:
                            _sw_old_model = getattr(cached_entry[0], "model", _cur_model)
                            _sw_old_provider = getattr(cached_entry[0], "provider", _cur_provider)
                            _sw_effort = getattr(cached_entry[0], "reasoning_config", None)
                            _sw_old_window = None
                            try:
                                _sw_old_window = getattr(
                                    getattr(cached_entry[0], "context_compressor", None),
                                    "context_length",
                                    None,
                                )
                            except Exception:
                                _sw_old_window = None
                            try:
                                # Off-loop: the in-place swap rebuilds clients and
                                # can probe the provider (t_515b7fce).
                                await asyncio.to_thread(
                                    cached_entry[0].switch_model,
                                    new_model=result.new_model,
                                    new_provider=result.target_provider,
                                    api_key=result.api_key,
                                    base_url=result.base_url,
                                    api_mode=result.api_mode,
                                    # Keep the session's /reasoning override
                                    # across the route change (#467 contract);
                                    # without this switch_model would fall back
                                    # to its own keep-current rule, which is
                                    # blind to a session override the gateway
                                    # holds but config.yaml does not.
                                    **_self._switch_reasoning_kwargs(
                                        source=event.source,
                                        session_key=_session_key,
                                        model=result.new_model,
                                    ),
                                )
                            except Exception as exc:
                                # The in-place swap rolled the agent back to the
                                # OLD working model/client and re-raised.  Abort
                                # the rest of the commit: do NOT persist the
                                # failed model to the DB, do NOT set a session
                                # override pointing at the broken model, and do
                                # NOT evict the working cached agent.  Otherwise
                                # the next message rebuilds a dead agent from the
                                # broken override and the conversation is lost
                                # (#50163).  A failed switch must be a no-op.
                                logger.warning(
                                    "Picker model switch failed for cached agent: %s", exc
                                )
                                return t(
                                    "gateway.model.error_prefix",
                                    error=(
                                        f"Model switch to {result.new_model} failed ({exc}); "
                                        f"staying on {_cur_model}."
                                    ),
                                )

                            # Announce the deliberate switch in-chat (like
                            # failover does) so spectators who didn't run
                            # /model see the change, not just the ephemeral
                            # reply. Delivery belongs to this command's source.
                            _sw_new_window = None
                            try:
                                _sw_new_window = getattr(
                                    getattr(cached_entry[0], "context_compressor", None),
                                    "context_length",
                                    None,
                                )
                            except Exception:
                                _sw_new_window = None

                            # #467 rule 3: the NEW side reads the agent's ACTUAL
                            # post-switch reasoning_config, never the value that
                            # requested the change — a per-model override for the
                            # new model must show up here.
                            _sw_new_effort = _sw_effort
                            try:
                                _sw_new_effort = _self._reasoning_effort_label(
                                    _self._post_switch_reasoning_config(
                                        cached_entry[0],
                                        source=event.source,
                                        session_key=_session_key,
                                        model=result.new_model,
                                    )
                                )
                            except Exception:
                                _sw_new_effort = _sw_effort
                            _switch_announce_kwargs = {
                                "source": event.source,
                                "old_model": _sw_old_model,
                                "new_model": result.new_model,
                                "old_provider": _sw_old_provider,
                                "new_provider": result.target_provider,
                                "old_effort": _sw_effort,
                                "new_effort": _sw_new_effort,
                                "old_window": _sw_old_window,
                                "new_window": _sw_new_window,
                            }

                        # Persist the new model to the session DB so the
                        # dashboard shows the updated model (#34850).
                        _sess_db = getattr(_self, "_session_db", None)
                        if _sess_db is not None:
                            try:
                                _sess_entry = await _self.async_session_store.get_or_create_session(
                                    event.source
                                )
                                await _sess_db.update_session_model(
                                    _sess_entry.session_id, result.new_model,
                                    provider=result.target_provider,
                                )
                            except Exception as exc:
                                logger.debug(
                                    "Failed to persist model switch to DB: %s", exc
                                )

                        # Store model note + session override.  Use display
                        # form (strips opaque Palantir prefix) for the user-
                        # visible note; session-override map still gets the
                        # full opaque ID, which is what the wire needs.
                        from hermes_cli.model_switch import format_model_for_display
                        _display_cur = format_model_for_display(_cur_model)
                        _display_new = format_model_for_display(result.new_model)
                        if not hasattr(_self, "_pending_model_notes"):
                            _self._pending_model_notes = {}
                        # Route (provider) is named explicitly: a sub-to-sub move
                        # keeps the model slug, so "fable-5-1 to fable-5-1" alone
                        # reads as a no-op when the provider actually changed.
                        # `_cur_provider` is the LIVE agent's provider (read at the
                        # top of the handler), not the config/override value.
                        _self._pending_model_notes[_session_key] = (
                            f"[Note: model was just switched from {_display_cur} to {_display_new} "
                            f"via {result.provider_label or result.target_provider} "
                            f"(route {_cur_provider} -> {result.target_provider}). "
                            f"Adjust your self-identification accordingly.]"
                        )
                        # Off the loop: the persistability check re-resolves
                        # credentials (config load + provider resolution, which
                        # can refresh an OAuth token over the network).
                        await _self._persist_session_model_override(_session_key, {
                            "model": result.new_model,
                            "provider": result.target_provider,
                            "api_key": result.api_key,
                            "base_url": result.base_url,
                            "api_mode": result.api_mode,
                            "request_overrides": dict(result.request_overrides or {}),
                            "capabilities": dict(result.runtime_capabilities or {}),
                        })

                        # Announce the deliberate switch to the conversation (P2).
                        if not cached_entry or cached_entry[0] is None:
                            await _self._announce_switch(
                                event.source,
                                "Model",
                                f"{_cur_provider}/{_cur_model}",
                                f"{result.target_provider}/{result.new_model}",
                            )
                        else:
                            await _self._announce_model_switch(
                                cached_entry[0],
                                **_switch_announce_kwargs,
                            )

                        # Config write-through (--global, #49066), session-override
                        # write-through / #100314 redundant-override drop, and the
                        # cache eviction — one durable commit shared with the typed path.
                        _global_error = await _self._commit_model_switch_durable(
                            result, session_key=_session_key, source=event.source,
                            config_path=config_path, persist_global=persist_global,
                            one_turn=False,
                        )

                        # Build confirmation text.  Use display form so opaque
                        # Palantir IDs (ri.language-model-service..*) get
                        # shortened to their trailing slug for the UI.
                        plabel = result.provider_label or result.target_provider
                        lines = [t("gateway.model.switched", model=format_model_for_display(result.new_model))]
                        _fast_row = self._fast_unavailable_model_switch_row(result)
                        if _fast_row:
                            lines.append(_fast_row)
                        lines.append(t("gateway.model.provider_label", provider=_with_resolved_route(plabel, result)))
                        try:
                            # Read the ACTUAL post-switch effort off the agent
                            # (#467 rule 3) rather than re-resolving for the OLD
                            # model, so the row can't advertise an effort the
                            # session is not running at.
                            _reasoning_label = _self._reasoning_effort_label(
                                _self._post_switch_reasoning_config(
                                    cached_entry[0] if cached_entry else None,
                                    source=event.source,
                                    session_key=_session_key,
                                    model=result.new_model,
                                )
                            )
                            if _reasoning_label:
                                lines.append(t("gateway.model.reasoning_label", effort=_reasoning_label))
                        except Exception:
                            pass
                        mi = result.model_info
                        from hermes_cli.model_switch import resolve_display_context_length_async
                        _sw_config_ctx = None
                        _sw_model_cfg = {}
                        try:
                            _sw_cfg = _load_gateway_config()
                            _sw_model_cfg = _sw_cfg.get("model", {})
                            if isinstance(_sw_model_cfg, dict):
                                _sw_raw = _sw_model_cfg.get("context_length")
                                if _sw_raw is not None:
                                    _sw_config_ctx = int(_sw_raw)
                        except Exception:
                            pass
                        if not isinstance(_sw_model_cfg, dict):
                            _sw_model_cfg = {}
                        ctx = await resolve_display_context_length_async(
                            result.new_model,
                            result.target_provider,
                            base_url=result.base_url or current_base_url or "",
                            api_key=result.api_key or current_api_key or "",
                            model_info=mi,
                            custom_providers=custom_provs,
                            config_context_length=_sw_config_ctx,
                            configured_model=(
                                _sw_model_cfg.get("default")
                                or _sw_model_cfg.get("model")
                            ),
                            configured_provider=_sw_model_cfg.get("provider"),
                            configured_base_url=_sw_model_cfg.get("base_url"),
                        )
                        if ctx:
                            lines.append(t("gateway.model.context_label", tokens=f"{ctx:,}"))
                        if mi:
                            if mi.max_output:
                                lines.append(t("gateway.model.max_output_label", tokens=f"{mi.max_output:,}"))
                            lines.append(t("gateway.model.capabilities_label", capabilities=mi.format_capabilities()))
                        if result.warning_message:
                            lines.append(t("gateway.model.warning_prefix", warning=result.warning_message))
                        if persist_global and _global_error is not None:
                            # Never claim a clean global commit the disk did not take (#100314).
                            lines.append(t("gateway.model.warning_prefix", warning=_global_error))
                            lines.append(t("gateway.model.session_only_hint"))
                        elif persist_global:
                            lines.append(t("gateway.model.saved_global"))
                        else:
                            lines.append(t("gateway.model.session_only_hint"))
                        if _fb_cleared:
                            lines.append(_self._fallback_cleared_line(
                                result.target_provider, result.new_model))
                        return "\n".join(lines)

                    async def _on_model_selected_dispatch(
                        _chat_id: str, model_id: str, provider_slug: str
                    ) -> str:
                        if _picker_profile_home is None:
                            return await _on_model_selected(
                                _chat_id, model_id, provider_slug
                            )
                        from gateway.run import _profile_runtime_scope

                        with _profile_runtime_scope(_picker_profile_home):
                            return await _on_model_selected(
                                _chat_id, model_id, provider_slug
                            )

                    metadata = self._thread_metadata_for_source(source, self._reply_anchor_for_event(event))
                    result = await adapter.send_model_picker(
                        chat_id=source.chat_id,
                        providers=providers,
                        current_model=current_model,
                        current_provider=current_provider,
                        session_key=session_key,
                        on_model_selected=_on_model_selected_dispatch,
                        metadata=metadata,
                    )
                    if result.success:
                        return None  # Picker sent — adapter handles the response

            # Fallback: text list (for platforms without picker or if picker failed)
            # get_label can fall through to a synchronous models.dev fetch
            # (requests.get) on a cold cache -- keep it off the loop.
            provider_label = await asyncio.to_thread(get_label, current_provider)
            lines = [t("gateway.model.current_label", model=current_model or "unknown", provider=provider_label), ""]

            try:
                # Offload blocking provider-listing off the event loop so the
                # gateway doesn't freeze on a stale-cache HTTP fetch. See #41289.
                providers = await asyncio.to_thread(
                    list_authenticated_providers,
                    current_provider=current_provider,
                    current_base_url=current_base_url,
                    current_model=current_model,
                    user_providers=user_provs,
                    custom_providers=custom_provs,
                    max_models=5,
                    excluded_providers=excluded_provs,
                )
                for p in providers:
                    tag = t("gateway.model.current_tag") if p["is_current"] else ""
                    lines.append(f"**{p['name']}** `--provider {p['slug']}`{tag}:")
                    if p["models"]:
                        model_strs = ", ".join(f"`{m}`" for m in p["models"])
                        extra = t("gateway.model.more_models_suffix", count=p["total_models"] - len(p["models"])) if p["total_models"] > len(p["models"]) else ""
                        lines.append(f"  {model_strs}{extra}")
                    elif p.get("api_url"):
                        lines.append(f"  `{p['api_url']}`")
                    lines.append("")
            except Exception:
                pass

            lines.append(t("gateway.model.usage_switch_model"))
            lines.append(t("gateway.model.usage_switch_provider"))
            lines.append(t("gateway.model.usage_persist"))
            return "\n".join(lines)

        # Perform the switch
        skew_error = _model_switch_skew_guard()
        if skew_error:
            return skew_error
        # Offload the switch off the event loop — switch_model() can fall
        # through to a synchronous models.dev HTTP fetch (requests.get, 15s
        # timeout) on a cold/expired cache, which freezes the gateway
        # otherwise. See #20525, #41289.
        result = await asyncio.to_thread(
            _switch_model,
            raw_input=model_input,
            current_provider=current_provider,
            current_model=current_model,
            current_base_url=current_base_url,
            current_api_key=current_api_key,
            is_global=persist_global,
            explicit_provider=explicit_provider,
            user_providers=user_provs,
            custom_providers=custom_provs,
        )

        if not result.success:
            return t("gateway.model.error_prefix", error=result.error_message)

        try:
            from hermes_cli.context_switch_guard import (
                enrich_model_switch_warnings_for_gateway,
            )

            # Offload: merge_preflight_compression_warning() calls the sync
            # resolve_display_context_length() provider probe ladder — must
            # not run on the loop.
            await asyncio.to_thread(
                enrich_model_switch_warnings_for_gateway,
                result,
                self,
                session_key=session_key,
                source=source,
                custom_providers=custom_provs,
                load_gateway_config=_load_gateway_config,
            )
        except Exception as exc:
            logger.debug("preflight-compression switch warning failed: %s", exc)

        async def _finish_switch() -> str:
            """Apply the resolved switch (agent, session, config) and build the reply."""
            # If there's a cached agent, update it in-place
            _switch_announce_kwargs = {}
            cached_entry = None
            _cache_lock = getattr(self, "_agent_cache_lock", None)
            _cache = getattr(self, "_agent_cache", None)
            if _cache_lock and _cache is not None:
                with _cache_lock:
                    cached_entry = _cache.get(session_key)
            # Explicit route beats an automatic one: read + close the fallback
            # episode BEFORE the in-place swap resets the live flags.
            _fb_cleared = await asyncio.to_thread(
                self._clear_fallback_for_user_route,
                session_key,
                cached_entry[0] if cached_entry else None,
            )

            if cached_entry and cached_entry[0] is not None:
                _sw_old_model = getattr(cached_entry[0], "model", current_model)
                _sw_old_provider = getattr(cached_entry[0], "provider", current_provider)
                _sw_effort = getattr(cached_entry[0], "reasoning_config", None)
                _sw_old_window = None
                try:
                    _sw_old_window = getattr(
                        getattr(cached_entry[0], "context_compressor", None),
                        "context_length",
                        None,
                    )
                except Exception:
                    _sw_old_window = None
                try:
                    # Off-loop: the in-place swap rebuilds clients and can
                    # probe the provider (t_515b7fce).
                    await asyncio.to_thread(
                        cached_entry[0].switch_model,
                        new_model=result.new_model,
                        new_provider=result.target_provider,
                        api_key=result.api_key,
                        base_url=result.base_url,
                        api_mode=result.api_mode,
                        # Keep the session's /reasoning override across the
                        # route change (#467 contract) — see the picker path.
                        **self._switch_reasoning_kwargs(
                            source=source,
                            session_key=session_key,
                            model=result.new_model,
                        ),
                    )
                except Exception as exc:
                    # In-place swap rolled the agent back to the OLD working
                    # model/client and re-raised.  Abort the commit: skip DB
                    # persist, session override, cache eviction, and config
                    # write so a failed switch is a no-op rather than a dead
                    # conversation (#50163).  Without this early return the
                    # next message rebuilds a broken agent from the override.
                    logger.warning("In-place model switch failed for cached agent: %s", exc)
                    return t(
                        "gateway.model.error_prefix",
                        error=(
                            f"Model switch to {result.new_model} failed ({exc}); "
                            f"staying on {current_model}."
                        ),
                    )

                # Announce the deliberate switch in-chat (like failover) so
                # spectators who didn't run /model see it, not just the
                # ephemeral reply. Symmetric to/from with effort + window.
                _sw_new_window = None
                try:
                    _sw_new_window = getattr(
                        getattr(cached_entry[0], "context_compressor", None),
                        "context_length",
                        None,
                    )
                except Exception:
                    _sw_new_window = None

                # #467 rule 3: the NEW side reads the agent's ACTUAL post-switch
                # reasoning_config, not the value that requested the change.
                _sw_new_effort = _sw_effort
                try:
                    _sw_new_effort = self._reasoning_effort_label(
                        self._post_switch_reasoning_config(
                            cached_entry[0],
                            source=source,
                            session_key=session_key,
                            model=result.new_model,
                        )
                    )
                except Exception:
                    _sw_new_effort = _sw_effort
                _switch_announce_kwargs = {
                    "source": event.source,
                    "old_model": _sw_old_model,
                    "new_model": result.new_model,
                    "old_provider": _sw_old_provider,
                    "new_provider": result.target_provider,
                    "old_effort": _sw_effort,
                    "new_effort": _sw_new_effort,
                    "old_window": _sw_old_window,
                    "new_window": _sw_new_window,
                }

            # Persist the new model to the session DB so the dashboard
            # shows the updated model (#34850).
            _sess_db = getattr(self, "_session_db", None)
            if _sess_db is not None:
                try:
                    _sess_entry = await self.async_session_store.get_or_create_session(source)
                    # If this session was auto-reset, consume the flag so the
                    # next regular message's cleanup does not wipe the model
                    # override just stored below (Closes #48031).
                    if getattr(_sess_entry, "was_auto_reset", False):
                        _sess_entry.was_auto_reset = False
                    await _sess_db.update_session_model(
                        _sess_entry.session_id, result.new_model,
                        provider=result.target_provider,
                    )
                except Exception as exc:
                    logger.debug(
                        "Failed to persist model switch to DB: %s", exc
                    )

            # Store a note to prepend to the next user message so the model
            # knows about the switch (avoids system messages mid-history).
            # Display form strips opaque Palantir RID prefixes; the override
            # map below keeps the full ID for the wire.
            from hermes_cli.model_switch import format_model_for_display
            if not hasattr(self, "_pending_model_notes"):
                self._pending_model_notes = {}
            # `current_model`/`current_provider` are the LIVE agent's route (read
            # near the top of the handler), so the note names what the session
            # was actually running, and the provider is spelled out because a
            # sub-to-sub move keeps the model slug.
            self._pending_model_notes[session_key] = (
                f"[Note: model was just switched from {format_model_for_display(current_model)} to {format_model_for_display(result.new_model)} "
                f"via {result.provider_label or result.target_provider} "
                f"(route {current_provider} -> {result.target_provider}). "
                f"{'This override applies to the next turn only. ' if one_turn else ''}"
                f"Adjust your self-identification accordingly.]"
            )

            # Store session override so next agent creation uses the new model
            # (single door — also persists the config-backed identity, RC-2/P3b).
            # Off the loop: the persistability check re-resolves credentials
            # (config load + provider resolution, which can refresh an OAuth
            # token over the network). On-loop, this chain held Discord for
            # 10 s on 2026-09-24 (PHASE=event_loop_blocked at
            # open_credentialed_url) and expired /model interactions.
            await self._persist_session_model_override(session_key, {
                "model": result.new_model,
                "provider": result.target_provider,
                "api_key": result.api_key,
                "base_url": result.base_url,
                "api_mode": result.api_mode,
                "request_overrides": dict(result.request_overrides or {}),
                # Upstream: the override carries the route's resolved runtime capabilities
                # (native compaction etc.) so the next agent build does not re-probe them.
                "capabilities": dict(result.runtime_capabilities or {}),
            })
            if one_turn:
                # A repeated --once before the turn runs must keep the EARLIEST snapshot: the
                # later command's snapshot is the first temporary model, not the user's
                # standing override (upstream _claim_one_turn_restore).
                self._claim_one_turn_restore(
                    session_key, restore_snapshot or {"had_override": False, "override": None},
                )
            elif hasattr(self, "_pending_one_turn_model_restores"):
                self._pending_one_turn_model_restores.pop(session_key, None)

            # Announce the deliberate switch to the conversation (P2). Compares the
            # (provider, model, api_mode) route so a same-slug/different-endpoint
            # switch still announces; silent on a true no-op. Best-effort.
            if not cached_entry or cached_entry[0] is None:
                await self._announce_switch(
                    event.source,
                    "Model",
                    f"{current_provider}/{current_model}",
                    f"{result.target_provider}/{result.new_model}",
                )
            else:
                await self._announce_model_switch(
                    cached_entry[0],
                    **_switch_announce_kwargs,
                )

            # Session-override write-through (never for --once, #29923), config
            # write-through (--global), the #100314 redundant-override drop and the
            # cache eviction — one durable commit shared with the picker path.
            _global_error = await self._commit_model_switch_durable(
                result, session_key=session_key, source=event.source,
                config_path=config_path, persist_global=persist_global, one_turn=one_turn,
            )

            # Build confirmation message with full metadata
            provider_label = result.provider_label or result.target_provider
            lines = [t("gateway.model.switched", model=format_model_for_display(result.new_model))]
            lines.append(t("gateway.model.provider_label", provider=_with_resolved_route(provider_label, result)))
            _fast_row = self._fast_unavailable_model_switch_row(result)
            if _fast_row:
                lines.append(_fast_row)

            # Reasoning effort in effect after the switch. /model does NOT clear
            # a /reasoning session override, so read the agent's ACTUAL
            # post-switch config (#467 rule 3), falling back to the
            # session-aware resolver for the NEW model when no cached agent was
            # swapped — never the global default.
            try:
                _reasoning_label = self._reasoning_effort_label(
                    self._post_switch_reasoning_config(
                        cached_entry[0] if cached_entry else None,
                        source=source,
                        session_key=session_key,
                        model=result.new_model,
                    )
                )
                if _reasoning_label:
                    lines.append(t("gateway.model.reasoning_label", effort=_reasoning_label))
            except Exception:
                pass

            # Context: always resolve via the provider-aware chain so Codex OAuth,
            # Copilot, and Nous-enforced caps win over the raw models.dev entry.
            mi = result.model_info
            from hermes_cli.model_switch import resolve_display_context_length_async
            _sw2_config_ctx = None
            _sw2_model_cfg = {}
            try:
                _sw2_cfg = _load_gateway_config()
                _sw2_model_cfg = _sw2_cfg.get("model", {})
                if isinstance(_sw2_model_cfg, dict):
                    _sw2_raw = _sw2_model_cfg.get("context_length")
                    if _sw2_raw is not None:
                        _sw2_config_ctx = int(_sw2_raw)
            except Exception:
                pass
            if not isinstance(_sw2_model_cfg, dict):
                _sw2_model_cfg = {}
            ctx = await resolve_display_context_length_async(
                result.new_model,
                result.target_provider,
                base_url=result.base_url or current_base_url or "",
                api_key=result.api_key or current_api_key or "",
                model_info=mi,
                custom_providers=custom_provs,
                config_context_length=_sw2_config_ctx,
                configured_model=(
                    _sw2_model_cfg.get("default")
                    or _sw2_model_cfg.get("model")
                ),
                configured_provider=_sw2_model_cfg.get("provider"),
                configured_base_url=_sw2_model_cfg.get("base_url"),
            )
            if ctx:
                lines.append(t("gateway.model.context_label", tokens=f"{ctx:,}"))
            if mi:
                if mi.max_output:
                    lines.append(t("gateway.model.max_output_label", tokens=f"{mi.max_output:,}"))
                lines.append(t("gateway.model.capabilities_label", capabilities=mi.format_capabilities()))

            # Cache notice
            cache_enabled = (
                (base_url_host_matches(result.base_url or "", "openrouter.ai") and "claude" in result.new_model.lower())
                or result.api_mode == "anthropic_messages"
            )
            if cache_enabled:
                lines.append(t("gateway.model.prompt_caching_enabled"))

            if result.warning_message:
                lines.append(t("gateway.model.warning_prefix", warning=result.warning_message))

            if persist_global and _global_error is not None:
                # Never claim a clean global commit the disk did not take (#100314).
                lines.append(t("gateway.model.warning_prefix", warning=_global_error))
                lines.append(t("gateway.model.session_only_hint"))
            elif persist_global:
                lines.append(t("gateway.model.saved_global"))
            elif one_turn:
                lines.append("    (next turn only — restores after one response)")
            else:
                lines.append(t("gateway.model.session_only_hint"))
            if _fb_cleared:
                lines.append(self._fallback_cleared_line(
                    result.target_provider, result.new_model))

            return "\n".join(lines)

        # Selection-guard confirmation gate (typed /model <name> path).
        # The pickers (Telegram/Discord inline keyboards, TUI, dashboard)
        # already confirm via their own UI affordances; this covers the
        # direct text command, which previously bypassed the guard.
        # Runs the unified registry (cost + data-policy + future guards).
        # Pricing lookups may hit models.dev or a /models endpoint on a
        # cache miss, so run it off the event loop.
        _cost_warning = None
        try:
            from hermes_cli.model_selection_guards import combined_selection_warning

            _cost_warning = await asyncio.to_thread(
                combined_selection_warning,
                result.new_model,
                provider=result.target_provider,
                base_url=result.base_url or current_base_url or "",
                api_key=result.api_key or current_api_key or "",
                model_info=result.model_info,
            )
        except Exception:
            _cost_warning = None
        if _cost_warning is not None:
            async def _on_cost_confirm(choice: str) -> str:
                if choice == "cancel":
                    return (
                        f"🟡 Model switch cancelled. Current model unchanged "
                        f"({current_model or 'unknown'})."
                    )
                # "once" and "always" both proceed — there is no persistent
                # opt-out for selection guards (each guarded switch should be
                # an explicit decision).
                return await _finish_switch()

            _p = self._typed_command_prefix_for(event.source.platform)
            return await self._request_slash_confirm(
                event=event,
                command="model",
                title=_cost_warning.title,
                message=(
                    f"⚠️ **{_cost_warning.title}**\n\n{_cost_warning.message}\n\n"
                    f"_Text fallback: reply `{_p}approve` to switch or `{_p}cancel` to keep "
                    "the current model._"
                ),
                handler=_on_cost_confirm,
            )

        return await _finish_switch()

    async def _commit_model_switch_durable(
        self, result, *, session_key: str, source, config_path, persist_global: bool, one_turn: bool,
    ) -> Optional[str]:
        """Durable half of a committed /model switch (fork handler; mirrors upstream's
        ``GatewayModelCommandsMixin._record_model_switch``): config write-through via the
        ONE persist writer (#11576390), the #100314 redundant-override rule, the session-store
        write-through (never for ``--once``, #29923) and the cache eviction.

        Returns the warning for a ``--global`` switch whose config write or stale-override
        cleanup failed (the switch then truthfully stays a session override), else ``None``.
        """
        from gateway import slash_commands_model as _model_mixin

        global_error: Optional[str] = None
        if persist_global:
            try:
                # Resolved through the module so tests/operators can patch the writer seam.
                await _model_mixin._persist_model_switch_to_config(result, config_path)
            except Exception as e:
                logger.warning("Failed to persist model switch: %s", e)
                global_error = t("gateway.model.err_config_not_updated", error=str(e) or type(e).__name__)
        # A --global switch has ONE durable authority: config.yaml. On success drop the session
        # override (memory + store) — a redundant copy would shadow every later global change
        # after a restart (#100314). Precedence is session > channel_overrides > config.yaml, so
        # under a channel_overrides model the session override must stay.
        if persist_global and global_error is None and self._channel_override_for(source) is None:
            try:
                await self.async_session_store.set_model_override(session_key, None)
            except Exception as e:
                logger.warning("Failed to clear persisted session model override: %s", e)
                global_error = t("gateway.model.err_stale_override", error=e)
            else:
                self._session_model_overrides.pop(session_key, None)
        elif not one_turn:
            # Non-secret write-through so the override survives a restart (api_key/api_mode are
            # re-resolved on rehydration); a --once override must NOT outlive a restart (#29923).
            try:
                await self.async_session_store.set_model_override(
                    session_key, self._session_model_overrides[session_key],
                )
            except Exception:
                logger.debug("Failed to persist session model override", exc_info=True)
        # Evict cached agent so the next turn builds fresh from the override.
        self._evict_cached_agent(session_key)
        return global_error

    async def _announce_model_switch(
        self,
        agent,
        *,
        source: SessionSource,
        old_model: str,
        new_model: str,
        old_provider: str,
        new_provider: str,
        old_effort: Any = None,
        new_effort: Any = None,
        old_window: "int | None" = None,
        new_window: "int | None" = None,
    ) -> None:
        """Announce a deliberate ``/model`` switch in-chat, like failover does.

        Config-gated (``model.announce_switch``, default ON). Delivers directly
        to the command's source, never through an old cached turn's callback.
        Symmetric to/from with model, effort and context-window deltas.

        A bug here must NEVER break the switch: everything is wrapped so the
        already-applied ``switch_model`` result stands regardless. This is a
        status EMISSION, never a context mutation — it cannot break prompt
        caching (see ``chat_completion_helpers._emit_switch_announce``).
        """
        try:
            if agent is None:
                return
            # Config gate: model.announce_switch (default True). Read per-call so
            # a value flip after deploy takes effect without a code redeploy.
            announce = True
            try:
                from gateway.run import _load_gateway_config

                _cfg = _load_gateway_config() or {}
                _model_cfg = _cfg.get("model", {})
                if isinstance(_model_cfg, dict) and "announce_switch" in _model_cfg:
                    announce = is_truthy_value(_model_cfg.get("announce_switch"))
            except Exception:
                announce = True
            if not announce:
                return
            from agent.chat_completion_helpers import _format_switch_announce

            message = _format_switch_announce(
                old_model=old_model or "",
                new_model=new_model or "",
                new_provider=new_provider or "",
                old_provider=old_provider or None,
                old_window=old_window,
                new_window=new_window,
                old_effort=old_effort,
                new_effort=new_effort,
            )
            if message is None:
                return
            # A cached agent's status callback belongs to a PREVIOUS turn.
            # Commands own their source, so deliver directly to that source.
            adapter = self._adapter_for_source(source)
            if adapter is not None:
                await adapter.send(
                    source.chat_id, message,
                    metadata=self._thread_metadata_for_source(source, None),
                )
        except Exception as exc:  # noqa: BLE001 — announce must never break the switch
            logger.debug("model-switch announce failed: %s", exc)

    def _build_merge_summarizer(self):
        """Construct a minimal ContextCompressor for /merge's summary call.

        Reuses the auxiliary ``task="compression"`` path (cheap model resolved
        from config) — ``main_runtime`` is only a fallback. Returns None if the
        runtime provider can't be resolved (surfaced as a no-summary merge).
        """
        try:
            from agent.context_compressor import ContextCompressor
            from hermes_cli.runtime_provider import (
                resolve_runtime_provider,
                _get_model_config,
            )
            runtime = resolve_runtime_provider()
            model_cfg = _get_model_config() or {}
            model = (
                (model_cfg.get("default") if isinstance(model_cfg, dict) else None)
                or runtime.get("model")
                or "gpt-4o-mini"
            )
            return ContextCompressor(
                model=model,
                quiet_mode=True,
                base_url=runtime.get("base_url", "") or "",
                api_key=runtime.get("api_key", "") or "",
                provider=runtime.get("provider", "") or "",
                api_mode=runtime.get("api_mode", "") or "",
            )
        except Exception as exc:
            logger.warning("merge: could not build summarizer: %s", exc)
            return None

    def _clear_fallback_for_user_route(self, session_key: str, agent=None) -> bool:
        """``/model`` is an explicit route and beats an automatic fallback
        (t_b2e9bb23): close every active sticky episode on the session's
        lineage so the rebuilt agent cannot resume it. Sync (sqlite); call via
        ``asyncio.to_thread``. Returns whether a fallback was in effect, so the
        reply can say so. The pre-run rebuild site closes again from the
        ``_override_target_just_changed`` stamp (covers a non-resident agent
        whose lineage root differs from its session id)."""
        try:
            from agent import fallback_sticky_store as _fss
            from agent import fallback_wiring as _fw

            was_live = _fw.live_fallback_active(agent)
            root = _fss.lineage_root_for_agent(agent) if agent is not None else ""
            if not root:
                store = getattr(self, "session_store", None)
                entry = store.entry_for(session_key) if store is not None else None
                root = str(getattr(entry, "session_id", "") or "")
            closed = _fw.close_episodes_for_user_route(root)
            return bool(was_live or closed)
        except Exception:
            logger.debug("user-route fallback clear failed (non-fatal)", exc_info=True)
            return False

    def _compact_account_limit_lines(self) -> list:
        """Build the compact '📈 Account limits' block (one line per subscription).

        DRY: loads the SAME ~/.hermes/scripts/claude_usage_lib.py that powers the
        full /claude-usage report and calls its render_compact_lines() — so the
        set of subscriptions (all Claude subs + Codex) tracks automatically with
        the registry. Returns [] (header omitted) when nothing is available so
        the caller can fall back to the single-provider snapshot. Never raises.
        """
        try:
            import importlib.util
            import os
            import sys

            lib_path = os.path.expanduser("~/.hermes/scripts/claude_usage_lib.py")
            if not os.path.isfile(lib_path):
                return []
            # Cache the loaded module under a stable sys.modules key so the
            # library's top-level imports (subprocess, sqlite3, etc.) are only
            # executed once instead of re-run on every /usage call (Greptile P2).
            _MOD_KEY = "_hermes_claude_usage_lib"
            mod = sys.modules.get(_MOD_KEY)
            if mod is None:
                spec = importlib.util.spec_from_file_location(_MOD_KEY, lib_path)
                if spec is None or spec.loader is None:
                    return []
                mod = importlib.util.module_from_spec(spec)
                sys.modules[_MOD_KEY] = mod
                try:
                    spec.loader.exec_module(mod)
                except Exception:
                    sys.modules.pop(_MOD_KEY, None)  # don't cache a half-loaded module
                    raise
            render = getattr(mod, "render_compact_lines", None)
            if render is None:
                return []
            sub_lines = render() or []
            if not sub_lines:
                return []
            return ["📈 **Account limits**", *sub_lines]
        except Exception:
            return []

    @staticmethod
    def _fallback_cleared_line(provider, model) -> str:
        return f"Fallback cleared; next turn runs on `{provider}/{model}`."

    def _fast_unavailable_model_switch_row(self, result: Any) -> Optional[str]:
        """Explain when an enabled Fast toggle cannot follow a new route."""
        tier = getattr(self, "_service_tier", None)
        if tier not in {"priority", "ultrafast"}:
            return None
        try:
            from hermes_cli.models import resolve_fast_mode_capability

            provider = getattr(result, "target_provider", None)
            api_mode = getattr(result, "api_mode", None)
            if not api_mode:
                _, _, api_mode = self._configured_route_identity(
                    {"model": {"provider": provider}}
                )
            capability = resolve_fast_mode_capability(
                model=getattr(result, "new_model", None),
                provider=provider,
                api_mode=api_mode,
                tier=tier,
            )
            if capability.supported:
                return None
            route = (
                f"{getattr(result, 'target_provider', None) or '<unknown>'}/"
                f"{getattr(result, 'new_model', None) or '<unset>'}"
            )
            return t(
                "gateway.fast.model_switch_unavailable",
                route=route,
            )
        except Exception:
            return None

    async def _handle_merge_command(self, event: MessageEvent) -> str:
        """Handle /merge [name] — fold a summary of THIS session into a target.

        - ``/merge <name>`` (any platform): summarize the current session and fold
          the summary into the session titled/ided ``<name>``. Owner-guarded
          exactly like /resume — you can only merge into sessions you own.
        - ``/merge`` (no arg) inside a branched Discord thread: target = the
          thread's parent session, then archive+lock the thread.
        - ``/merge`` (no arg) elsewhere: list the caller's recent titled sessions
          (mergeable targets), do nothing.

        The current session is READ-ONLY here — it is summarized, never ended or
        switched; the user keeps talking in it.
        """
        if not self._session_db:
            from hermes_state import format_session_db_unavailable
            return format_session_db_unavailable(prefix=t("gateway.shared.session_db_unavailable_prefix"))

        source = event.source
        name = event.get_command_args().strip()
        # Strip surrounding quotes/brackets like /resume does.
        if len(name) >= 2 and (
            (name[0] == "[" and name[-1] == "]")
            or (name[0] == '"' and name[-1] == '"')
            or (name[0] == "'" and name[-1] == "'")
        ):
            name = name[1:-1].strip()

        current_entry = await self.async_session_store.get_or_create_session(source)
        source_session_id = current_entry.session_id

        # --- Resolve the TARGET session and whether this is the thread form. ---
        is_thread_form = False
        thread_adapter = None
        target_id = None
        target_title = None
        branch_point_len = None  # thread form: index into source_history where new exploration begins

        if not name:
            # No arg. In a branched Discord thread → target = parent, archive after.
            branch_parent = None
            if source.platform == Platform.DISCORD and source.chat_type == "thread" and source.thread_id:
                try:
                    row = await self._session_db.get_session(source_session_id)
                except Exception:
                    row = None
                if row:
                    branch_parent = row.get("parent_session_id")
                    mc = row.get("model_config")
                    if isinstance(mc, str) and mc:
                        try:
                            import json as _json
                            mc = _json.loads(mc)
                        except Exception:
                            mc = {}
                    if isinstance(mc, dict):
                        if not branch_parent:
                            branch_parent = mc.get("_branched_from")
                        # Delta boundary: only summarize turns AFTER the branch
                        # point so we don't re-fold history the parent already has.
                        _bpl = mc.get("_branch_point_len")
                        if isinstance(_bpl, int) and _bpl >= 0:
                            branch_point_len = _bpl
            if branch_parent:
                is_thread_form = True
                thread_adapter = self.adapters.get(Platform.DISCORD) if getattr(self, "adapters", None) else None
                target_id = branch_parent
                target_title = await self._session_db.get_session_title(target_id) or "parent"
            else:
                # Not a branched thread → list mergeable sessions.
                return await self._list_mergeable_sessions(source)
        else:
            target_id, resolved = await self._resolve_merge_target(source, name)
            if not target_id:
                return resolved  # error/not-found string
            target_title = resolved

        # Don't merge a session into itself.
        if target_id == source_session_id:
            return t("gateway.merge.same_session")

        # --- Owner guard: a merge WRITES into the target (bigger than /resume's
        # read), so refuse cross-owner targets fail-closed, same as /resume. ---
        if not is_thread_form:
            try:
                allowed = await self._resume_target_allowed(source, target_id)
            except Exception:
                allowed = False
            if not allowed:
                return t("gateway.merge.blocked_not_owner", name=target_title)

        # --- Idempotency guard (Greptile P1): refuse a duplicate fold of the
        # SAME source into the SAME target. A retry, double-submit, or a
        # reopened/re-invoked thread would otherwise append the branch summary
        # to the parent twice and skew later agent behavior. We record each
        # completed (source → target) merge in the source's model_config
        # `_merged_into` list; a repeat (source, target) is a no-op. Merging the
        # same source into a DIFFERENT target later is still allowed. ---
        already = await self._merge_already_done(source_session_id, target_id)
        if already:
            return t("gateway.merge.already_merged", title=target_title)

        # --- Summarize the CURRENT session (read-only). ---
        source_history = await self.async_session_store.load_transcript(source_session_id)
        if not source_history:
            return t("gateway.merge.no_conversation")
        source_title = await self._session_db.get_session_title(source_session_id) or "session"

        # Thread form: only summarize the DELTA — the turns that happened in the
        # branch AFTER the branch point. A branched thread is a full copy of the
        # parent up to the branch, plus the new exploration; folding the whole
        # copy back re-summarizes context the parent already has. The named form
        # has no shared prefix, so it summarizes the whole source session.
        summarize_history = source_history
        if is_thread_form and isinstance(branch_point_len, int) and branch_point_len >= 0:
            # The delta is authoritative once we have a valid branch point: even
            # branch_point_len == len(source_history) (zero new turns) must yield
            # an EMPTY delta → no_new_turns, NOT a re-summary of the whole copy.
            summarize_history = source_history[branch_point_len:]
            if not summarize_history:
                return t("gateway.merge.no_new_turns")
        if not summarize_history:
            return t("gateway.merge.no_new_turns")

        summarizer = self._build_merge_summarizer()
        summary = None
        if summarizer is not None:
            try:
                summary = await asyncio.to_thread(
                    summarizer._generate_summary,
                    summarize_history,
                    f"merging session '{source_title}' into '{target_title}'",
                )
            except Exception as exc:
                logger.warning("merge: summary generation failed: %s", exc)
                summary = None
        if summary:
            summary = summary.strip()
        if not summary:
            return t("gateway.merge.no_summary")

        # --- Layer 1: fold ONE labeled user-role message into the TARGET. ---
        # Make the claim ATOMIC (Greptile P1): hold a per-(source→target)
        # asyncio lock across re-check → record → append so two overlapping
        # /merge calls can't both pass the duplicate check and both append. The
        # gateway is single-event-loop, so this lock fully serializes the claim.
        lock = self._merge_claim_lock(source_session_id, target_id)
        async with lock:
            # Authoritative re-check INSIDE the lock — the earlier pre-summary
            # check is only a cheap fast-path; this is the one that gates the write.
            if await self._merge_already_done(source_session_id, target_id):
                return t("gateway.merge.already_merged", title=target_title)
            recorded = await self._record_merge_done(source_session_id, target_id)
            if not recorded:
                return t("gateway.merge.ledger_failed")
            fold_key = "gateway.merge.fold_body_thread" if is_thread_form else "gateway.merge.fold_body"
            fold_text = t(fold_key, title=source_title, summary=summary)
            try:
                await self._session_db.append_message(
                    session_id=target_id,
                    role="user",
                    content=fold_text,
                )
            except Exception as exc:
                logger.error("merge: failed to append fold to target %s: %s", target_id, exc)
                # Roll back the ledger entry — the fold never landed, so a retry
                # must be allowed to try again rather than being wrongly refused.
                await self._unrecord_merge_done(source_session_id, target_id)
                return t("gateway.merge.fold_failed", error=exc)

        # --- Layer 2: durable .md record (self-purging). ---
        record_path = await asyncio.to_thread(
            self._write_merge_record,
            source_title, source_session_id, target_title, target_id,
            summary, source.platform.value if source.platform else "gateway",
        )

        # --- Layer 1b: mark the SOURCE session as merged too. ---
        # /merge records the merge on the TARGET (the _merged_into ledger) but
        # historically wrote nothing back to the SOURCE — so if you kept talking
        # in the branch (or resumed it later), the agent had no idea a merge ever
        # happened. Append a small labeled marker into the source transcript so a
        # resumed branch knows its exploration was already folded into the parent.
        # Best-effort; never blocks the merge.
        try:
            from hermes_time import now as _merge_now
            _ts = _merge_now().strftime("%Y-%m-%d %H:%M")
        except Exception:
            from datetime import datetime as _dt
            _ts = _dt.now().strftime("%Y-%m-%d %H:%M")
        try:
            await self._session_db.append_message(
                session_id=source_session_id,
                role="user",
                content=t(
                    "gateway.merge.source_marker" if is_thread_form else "gateway.merge.source_marker_named",
                    target=target_title, when=_ts,
                ),
            )
        except Exception as exc:
            logger.debug("merge: source-marker append skipped: %s", exc)
        # Evict the source's cached agent so the marker is live if it keeps going.
        try:
            src_key = self._session_key_for_source(source)
            self._evict_cached_agent(src_key)
        except Exception as exc:
            logger.debug("merge: source agent eviction skipped: %s", exc)

        # Evict the target's cached agent so the fold is live on its next turn.
        target_entry = await self.async_session_store.lookup_by_session_id(target_id)
        if target_entry is not None:
            try:
                self._evict_cached_agent(target_entry.session_key)
            except Exception as exc:
                logger.debug("merge: target agent eviction skipped: %s", exc)

        # --- Layer 3: visible note posted where the fold LANDED — the TARGET
        # session's own origin channel/thread — for BOTH forms. (Previously the
        # thread form posted to source.parent_chat_id, the Discord *hosting
        # channel*, which for a thread-branched-from-a-thread is NOT where the
        # parent SESSION lives — so the note showed up in the wrong place.) We
        # resolve the target's real origin and post there so the "merged in" note
        # always lands in the conversation that just received the summary.
        #
        # The note is INTENTIONALLY terse: it says WHAT happened (context added),
        # HOW MUCH (N user turns folded), and WHERE the detail is (the .md path).
        # The full summary is already in the agent's context — dumping it into the
        # channel as a wall of text is noise, not signal (Ace, 2026-07-08). ---
        # Count = raw rows folded (matches the /footer "N/Nmsgs" semantics =
        # sessions.message_count = len(history)), NOT a user-only subset.
        turns_folded = len(summarize_history)
        path_str = str(record_path) if record_path else ""
        md_note = t("gateway.merge.note_record_suffix", path=path_str) if path_str else ""
        try:
            target_origin = self._gateway_session_origin_for_id(target_id)
        except Exception:
            target_origin = None
        # Reciprocal link: the parent-side note links back to the CHILD thread
        # the summary came from (thread form only — named merges have no thread),
        # mirroring how /branch links the parent INTO the child.
        child_link = ""
        if is_thread_form and source.thread_id:
            child_link = t("gateway.merge.source_thread_link", thread=f"<#{source.thread_id}>")
        posted_note = False
        if isinstance(target_origin, SessionSource) and target_origin.platform:
            dest_adapter = self.adapters.get(target_origin.platform) if getattr(self, "adapters", None) else None
            dest_chat = target_origin.thread_id or target_origin.chat_id
            if dest_adapter is not None and dest_chat:
                try:
                    note = t(
                        "gateway.merge.target_note" if is_thread_form else "gateway.merge.target_note_named",
                        source=source_title, turns=turns_folded, record=md_note, child=child_link,
                    )
                    await dest_adapter.send(str(dest_chat), note)
                    posted_note = True
                except Exception as exc:
                    logger.debug("merge: target-origin note send failed: %s", exc)
        # Fallback (thread form only): if the target's origin couldn't be resolved
        # (e.g. the parent session isn't live in the store), post to the thread's
        # hosting channel so the merge is still visible somewhere sensible.
        if not posted_note and is_thread_form and thread_adapter is not None and source.parent_chat_id:
            try:
                note = t("gateway.merge.target_note", source=source_title,
                         turns=turns_folded, record=md_note, child=child_link)
                await thread_adapter.send(str(source.parent_chat_id), note)
            except Exception as exc:
                logger.debug("merge: parent-channel fallback note send failed: %s", exc)

        # --- Archive the thread (thread form only). ---
        archived = False
        if is_thread_form and thread_adapter is not None:
            try:
                archived = await thread_adapter.archive_thread(str(source.thread_id), lock=True)
            except Exception as exc:
                logger.debug("merge: archive_thread failed: %s", exc)

        # --- Confirm to the caller. ---
        md_part = t("gateway.merge.md_suffix", path=path_str) if path_str else ""
        if is_thread_form:
            # Reciprocal link: name+link the PARENT conversation this thread
            # folded back into (mirrors the child link in the parent's note).
            parent_link = ""
            if isinstance(target_origin, SessionSource):
                parent_ref = target_origin.thread_id or target_origin.chat_id
                if parent_ref:
                    parent_link = t("gateway.merge.target_parent_link", parent=f"<#{parent_ref}>")
            confirm_key = "gateway.merge.merged_thread" if archived else "gateway.merge.merged_thread_no_archive"
            return t(confirm_key, title=target_title, md=md_part, parent=parent_link)
        return t("gateway.merge.merged_named", title=target_title, md=md_part)

    async def _handle_redo_command(self, event: MessageEvent) -> str:
        """Handle /redo [N] by delegating to the shared redo core."""
        source = event.source
        # Same drain guard as /undo: a restore during a /stop'd turn's drain
        # window races the still-writing turn.
        if self._session_turn_draining(source):
            return t("gateway.redo.draining")
        n = 1
        raw_args = event.get_command_args().strip()
        if raw_args:
            try:
                n = int(raw_args.split()[0])
            except (ValueError, IndexError):
                return t("gateway.redo.invalid_count", arg=raw_args.split()[0])

        session_entry = await self.async_session_store.get_or_create_session(source)
        result = await self.async_session_store.restore_session(session_entry.session_id, n)
        if result is None:
            return t("gateway.redo.nothing")
        # Honest reporting of a non-empty failure (2026-07-15 fix): a transient
        # DB-busy or a real internal error must not read as "nothing to redo".
        status = result.get("status") if isinstance(result, dict) else None
        if status == "busy":
            return t("gateway.redo.busy")
        if status == "error":
            return t("gateway.redo.error")

        reactivated = int(result.get("reactivated_count") or 0)
        if reactivated <= 0:
            message = str(result.get("message") or "")
            if "restart" in message:
                return t("gateway.redo.restart_lost")
            return t("gateway.redo.nothing")

        session_entry.last_prompt_tokens = 0
        try:
            session_key = build_session_key(source)
            self._evict_cached_agent(session_key)
        except Exception as e:
            logger.debug("redo: cached-agent eviction skipped: %s", e)

        base = t(
            "gateway.redo.restored",
            # What the core redid (clamped to the undo-stack depth), not what
            # was asked; fall back to n only for a core without the key.
            ops=result.get("ops_redone", n),
            count=reactivated,
        )
        # If only SOME ops were redone (a transcript rewrite or a mid-loop
        # transient/hard error), surface the honest partial note the undo core
        # produced instead of implying a full redo. Detect via the language-
        # neutral ``partial`` flag, not the (English) message text.
        partial_note = str(result.get("message") or "")
        if result.get("partial") and partial_note:
            base = f"{base}\n⚠️ {partial_note}"
        return base + self._undo_tail_suffix(session_entry.session_id)

    async def _handle_resume_handoff_command(self, event: MessageEvent) -> str:
        """Handle /resume-handoff — replay a turn cut by a provider failure.

        The handoff was written by ``agent.turn_handoff.capture_turn_handoff``
        at the cut and is keyed by the same session key the gateway resolves
        for every other session command. The command previews what was captured
        without consuming it; the next user turn injects the handoff into the
        model context exactly once.
        """
        from gateway.turn_handoff_command import (
            NO_HANDOFF_REPLY,
            render_resume_handoff_reply,
        )
        from types import SimpleNamespace

        source = await asyncio.to_thread(
            self._normalize_source_for_session_key, event.source
        )
        session_key = self._session_key_for_source(source)
        if not session_key:
            return NO_HANDOFF_REPLY
        return await asyncio.to_thread(
            render_resume_handoff_reply,
            SimpleNamespace(_gateway_session_key=session_key),
        )

    async def _list_mergeable_sessions(self, source) -> str:
        """Return the numbered list of the caller's titled sessions (merge targets).

        Mirrors /resume's no-arg listing so /merge with no arg (and not in a
        branched thread) surfaces what names are mergeable instead of a dead no-op.
        """
        try:
            user_source = source.platform.value if source.platform else None
            sessions = await self._session_db.list_sessions_rich(source=user_source, limit=10)
            titled = [s for s in sessions if s.get("title")][:10]
            titled = [
                s for s in titled
                if await self._resume_row_visible(source, s, False)
            ]
            # Don't offer the caller's OWN current session as a merge target.
            try:
                cur = await self.async_session_store.get_or_create_session(source)
                titled = [s for s in titled if s.get("id") != cur.session_id]
            except Exception:
                pass
            if not titled:
                return t("gateway.merge.no_named_sessions")
            lines = [t("gateway.merge.list_header")]
            for idx, s in enumerate(titled[:10], start=1):
                title = s["title"]
                preview = s.get("preview", "")[:40]
                preview_part = t("gateway.resume.list_preview_suffix", preview=preview) if preview else ""
                lines.append(t("gateway.resume.list_item_numbered", index=idx, title=title, preview_part=preview_part))
            lines.append(t("gateway.merge.list_footer"))
            return "\n".join(lines)
        except Exception as e:
            logger.debug("merge: failed to list titled sessions: %s", e)
            return t("gateway.resume.list_failed", error=e)

    async def _merge_already_done(self, source_session_id, target_id) -> bool:
        """Whether this (source → target) merge has already been recorded."""
        return str(target_id) in await self._merged_targets_for(source_session_id)

    def _merge_claim_lock(self, source_session_id, target_id):
        """Return a process-local asyncio.Lock for this (source → target) claim.

        Serializes the re-check → record → append critical section so two
        overlapping /merge calls for the same pair can't both append. Lazily
        created and cached; the gateway runs one event loop so a plain dict of
        locks is sufficient (no cross-process concurrency on a single session).
        """
        import asyncio as _asyncio
        locks = getattr(self, "_merge_claim_locks", None)
        if locks is None:
            locks = {}
            self._merge_claim_locks = locks
        key = f"{source_session_id}->{target_id}"
        lock = locks.get(key)
        if lock is None:
            lock = _asyncio.Lock()
            locks[key] = lock
        return lock

    async def _merged_targets_for(self, source_session_id):
        """Return the set of target session ids this source has already merged into.

        Recorded in the source session's ``model_config._merged_into`` list.
        Best-effort — returns an empty set on any read error.
        """
        try:
            row = await self._session_db.get_session(source_session_id)
        except Exception:
            return set()
        if not row:
            return set()
        mc = row.get("model_config")
        if isinstance(mc, str) and mc:
            try:
                import json as _json
                mc = _json.loads(mc)
            except Exception:
                return set()
        if not isinstance(mc, dict):
            return set()
        merged = mc.get("_merged_into")
        if isinstance(merged, list):
            return {str(x) for x in merged}
        return set()

    def _persist_session_service_tier(
        self, session_key: str, service_tier, clear: bool = False
    ) -> bool:
        """Durably persist a session-scoped /fast override.

        Written to a dedicated ``gateway_session_tiers.yaml`` (NOT
        ``config.yaml`` — the ``--global`` lane owns ``agent.service_tier``) so
        a session /fast toggle survives a gateway restart. Routes through the
        same fail-closed ``atomic_config_write`` chokepoint config edits use.
        Returns ``True`` on a durable write; ``False`` when persistence failed —
        the caller keeps the in-memory override live and reports the change as
        honestly gateway-process-wide until restart rather than claiming a
        durable per-session save.
        """
        if not session_key:
            return False
        import hermes_yaml as yaml
        path = self._session_service_tiers_path()
        try:
            existing = {}
            if path.exists():
                with open(path, encoding="utf-8-sig") as f:
                    loaded = yaml.safe_load(f) or {}
                if isinstance(loaded, dict):
                    existing = loaded
            tiers = existing.get("session_service_tiers")
            if not isinstance(tiers, dict):
                tiers = {}
            if clear:
                tiers.pop(session_key, None)
            else:
                tiers[session_key] = service_tier
            existing["session_service_tiers"] = tiers
            atomic_config_write(path, existing)
            return True
        except Exception as exc:
            logger.error(
                "Failed to persist session service tier (session=%s error=%s)",
                session_key,
                type(exc).__name__,
            )
            return False

    def _post_switch_reasoning_config(
        self,
        agent,
        *,
        source: Any = None,
        session_key: "str | None" = None,
        model: str = "",
    ) -> "dict | None":
        """The reasoning config the session ACTUALLY runs at after the switch.

        #467 rule 3 — never compute the displayed effort from the field that
        REQUESTED the change; read the post-swap state. Prefers the live
        agent's ``reasoning_config`` (the value ``switch_model`` just settled),
        falling back to the session resolver for the new model when there is no
        cached agent to swap.
        """
        cfg = getattr(agent, "reasoning_config", None) if agent is not None else None
        if isinstance(cfg, dict):
            return cfg
        try:
            return self._resolve_session_reasoning_config(
                source=source, session_key=session_key, model=model,
            )
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _preserve_route_preferences_on_manual_reset() -> bool:
        """Runtime-read kill switch for manual reset carry-over.

        A missing section uses the new default. Read or shape failures use the
        legacy false behavior so an operator's rollback remains dependable.
        """
        try:
            from gateway.run import _gateway_config_home

            config_path = _gateway_config_home() / "config.yaml"
            if not config_path.exists():
                return True
            import hermes_yaml as yaml

            # One immutable byte snapshot, parsed once. Do not validate one
            # read and obtain policy from a second loader: that creates a
            # rollback-policy TOCTOU when the second read fails or changes.
            snapshot = config_path.read_text(encoding="utf-8-sig")
            cfg = yaml.safe_load(snapshot)
            if cfg is None:
                cfg = {}
        except Exception as exc:
            logger.warning(
                "Could not read config session_reset manual preference policy; "
                "using legacy preserve_route_preferences_on_manual_reset=false "
                "(error=%s)",
                type(exc).__name__,
            )
            return False
        if not isinstance(cfg, dict):
            logger.warning(
                "Malformed gateway config while reading manual reset policy; "
                "using legacy preserve_route_preferences_on_manual_reset=false"
            )
            return False
        section = cfg.get("session_reset")
        if section is None:
            return True
        if not isinstance(section, dict):
            logger.warning(
                "Malformed session_reset config (expected mapping); defaulting "
                "preserve_route_preferences_on_manual_reset=false"
            )
            return False
        value = section.get("preserve_route_preferences_on_manual_reset", True)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "on"}:
                return True
            if normalized in {"false", "0", "no", "off"}:
                return False
        logger.warning(
            "Malformed session_reset.preserve_route_preferences_on_manual_reset; "
            "defaulting to false"
        )
        return False

    def _purge_old_merge_records(self, merges_dir, max_age_days: int = 30) -> None:
        """Delete merge-record .md files older than ``max_age_days``.

        Merge records are ephemeral operational artifacts (the summary's content
        also lives permanently in the target session's transcript). Self-purge on
        every write so the dir never accumulates — no separate cron needed.
        """
        import time
        try:
            cutoff = time.time() - max_age_days * 86400
            for p in merges_dir.glob("*.md"):
                try:
                    if p.stat().st_mtime < cutoff:
                        p.unlink()
                except Exception:
                    pass
        except Exception as exc:
            logger.debug("merge: purge sweep skipped: %s", exc)

    async def _record_merge_done(self, source_session_id, target_id) -> bool:
        """Append ``target_id`` to the source's ``model_config._merged_into`` list.

        Idempotency ledger for /merge — read-modify-write of the source session's
        model_config. Returns ``True`` only when the marker is CONFIRMED durably
        recorded (re-read after write); ``False`` on any failure. The caller must
        NOT append the fold when this returns False, otherwise a marker-less fold
        could be duplicated by a later retry (Greptile P1).
        """
        try:
            row = await self._session_db.get_session(source_session_id)
            mc = (row or {}).get("model_config")
            if isinstance(mc, str) and mc:
                import json as _json
                try:
                    mc = _json.loads(mc)
                except Exception:
                    mc = {}
            if not isinstance(mc, dict):
                mc = {}
            merged = mc.get("_merged_into")
            if not isinstance(merged, list):
                merged = []
            if str(target_id) not in {str(x) for x in merged}:
                merged.append(str(target_id))
            mc["_merged_into"] = merged
            import json as _json
            await self._session_db.update_session_meta(source_session_id, _json.dumps(mc))
        except Exception as exc:
            logger.warning("merge: could not record merge ledger: %s", exc)
            return False
        # Confirm the marker actually persisted before we trust it — a silently
        # failed/locked write must NOT let the fold proceed marker-less.
        try:
            return str(target_id) in await self._merged_targets_for(source_session_id)
        except Exception:
            return False

    def _rehydrate_manual_reset_route_preferences(
        self, session_key: str, entry: Any
    ) -> None:
        """Rebuild runtime caches from the newly rotated persisted entry."""
        if not hasattr(self, "_session_model_overrides"):
            self._session_model_overrides = {}
        if not hasattr(self, "_session_reasoning_overrides"):
            self._session_reasoning_overrides = {}
        if not hasattr(self, "_session_model_override_unavailable"):
            self._session_model_override_unavailable = set()
        # NOTE(P3b/RC-2): cache reconciliation, not a preference clear. The
        # newly rotated persisted entry is authoritative and is applied below.
        self._session_model_overrides.pop(session_key, None)
        self._session_reasoning_overrides.pop(session_key, None)
        self._session_model_override_unavailable.discard(session_key)

        reasoning = getattr(entry, "reasoning_override", None) if entry else None
        if isinstance(reasoning, dict):
            self._session_reasoning_overrides[session_key] = dict(reasoning)

        # Prefer the same chat-pin-aware authority as the next turn. Only
        # lightweight stores without that capability use the returned entry.
        # No user-action setter is called, so this cannot announce a switch.
        identity = getattr(entry, "model_override_identity", None) if entry else None
        store = getattr(self, "session_store", None)
        if callable(getattr(type(store), "lookup_persisted_route_identity", None)):
            route_lookup = self._persisted_session_route_identity(session_key)
            identity = route_lookup.identity
            if route_lookup.state == "unavailable":
                self._session_model_override_unavailable.add(session_key)
        if (
            isinstance(identity, dict)
            and identity.get("model")
            and identity.get("provider")
        ):
            try:
                resolved = self._reresolve_model_override_credentials(identity)
            except Exception:
                resolved = None
            if resolved is not None:
                self._session_model_overrides[session_key] = resolved
                self._session_model_override_unavailable.discard(session_key)
            else:
                self._session_model_override_unavailable.add(session_key)
        getattr(self, "_override_target_just_changed", {}).pop(session_key, None)

    def _render_last_turn_card(self, source, thin_snap, fallback_label=None, compressions=None) -> list:
        """Render the rich /context last-turn card for the invoking channel, or a
        reworded thin fallback. Returns a list of lines (may be empty).

        PRD usage-format-codex Part A: /usage's last-turn section reuses the SAME
        renderer /context uses (plugins.blackbox.last_turn) so the numbers are
        byte-identical. Channel is passed EXPLICITLY (event.source) and the
        returned record's channel is verified to match — a found-but-wrong-channel
        record falls back + WARNs, never renders confidently-wrong cross-channel
        numbers (D-7). `thin_snap` is the eviction-safe get_last_turn_usage dict
        (or the resident agent's session counters, or None) used for the fallback
        when blackbox has no matching turn. `fallback_label` overrides the thin
        fallback's parenthetical so the header is honest at each call site (the
        persisted branch says "agent not resident"; the resident branch says
        "session totals").
        """
        platform = source.platform.value if source and source.platform else ""
        chat_id = str(source.chat_id) if source and source.chat_id is not None else ""
        rec = None
        render_last_turn_record = None
        try:
            from plugins.blackbox.last_turn import (
                compute_last_turn_record,
                render_last_turn_record,
            )
            rec = compute_last_turn_record(platform, chat_id)
        except Exception as e:
            logger.warning("usage last-turn card: blackbox compute failed (%s); using fallback", e)
            rec = None

        if rec and rec.get("found") and render_last_turn_record is not None:
            # Channel-match guard (D-7): a non-empty channel request must return
            # THIS channel's row. If the store handed back another channel's row,
            # do NOT render it — fall back loudly.
            if platform and chat_id:
                if str(rec.get("platform", "")) == platform and str(rec.get("chat_id", "")) == chat_id:
                    try:
                        return render_last_turn_record(rec, compressions=compressions)
                    except Exception as e:
                        logger.warning("usage last-turn card: render failed (%s); using fallback", e)
                else:
                    logger.warning(
                        "usage last-turn card: blackbox returned channel %s/%s != requested %s/%s; "
                        "falling back to thin snapshot",
                        rec.get("platform"), rec.get("chat_id"), platform, chat_id,
                    )
            else:
                # No channel bound (shouldn't happen for a messaging /usage) — the
                # global-newest rec is acceptable; render it.
                try:
                    return render_last_turn_record(rec, compressions=compressions)
                except Exception as e:
                    logger.warning("usage last-turn card: render failed (%s); using fallback", e)

        # Fallback: blackbox unavailable / no turn for this channel / mismatch.
        # Reworded thin snapshot in the same vocabulary (uncached + cache + a
        # labelled total). Reasoning is folded into the output total so "Total"
        # means the same magnitude as the rich card's billed-out (INV-4).
        if not thin_snap:
            return []
        logger.warning("usage last-turn card: degraded to thin get_last_turn_usage snapshot")
        return render_thin_last_turn_lines(thin_snap, fallback_label)

    async def _resolve_merge_target(self, source, name):
        """Resolve a /merge <name> arg to a target session id + title.

        Mirrors /resume: numeric → index into the caller's titled list; else a
        direct session-id lookup, then a title lookup. Returns (target_id, title)
        or (None, error_string).
        """
        if name.isdigit():
            try:
                user_source = source.platform.value if source.platform else None
                sessions = await self._session_db.list_sessions_rich(source=user_source, limit=10)
                titled = [s for s in sessions if s.get("title")][:10]
                titled = [
                    s for s in titled
                    if await self._resume_row_visible(source, s, False)
                ]
                try:
                    cur = await self.async_session_store.get_or_create_session(source)
                    titled = [s for s in titled if s.get("id") != cur.session_id]
                except Exception:
                    pass
            except Exception as e:
                return None, t("gateway.resume.list_failed", error=e)
            index = int(name)
            if index < 1 or index > len(titled):
                return None, t("gateway.resume.out_of_range", index=index)
            target = titled[index - 1]
            return target.get("id"), (target.get("title") or name)

        # Non-numeric: direct id lookup first, then title.
        session = await self._session_db.get_session(name)
        if session:
            return session["id"], (session.get("title") or name)
        target_id = await self._session_db.resolve_session_by_title(name)
        if not target_id:
            return None, t("gateway.merge.not_found", name=name)
        title = await self._session_db.get_session_title(target_id) or name
        return target_id, title

    async def _send_compress_progress_ack(self, source, message_count: int) -> None:
        """Best-effort interim "compressing…" ack for a large /compress.

        Fire-and-forget: a failed ack must never break the compress, so every
        failure is swallowed (mirrors _send_startup_restore_ack). Resolves the
        live adapter and thread metadata exactly like _send_goal_status_notice.
        """
        try:
            adapter = self._adapter_for_source(source)
            if adapter is None:
                return
            try:
                metadata = self._thread_metadata_for_source(source)
            except Exception:
                metadata = None
            await adapter.send(
                source.chat_id,
                t("gateway.compress.in_progress", count=message_count),
                metadata=metadata,
            )
        except Exception:
            logger.debug("compress progress ack send failed", exc_info=True)

    def _session_turn_draining(self, source: "SessionSource") -> bool:
        """True if this source's session has a /stop'd-but-still-draining turn.

        After /stop, the running-agent slot is released immediately but the turn
        coroutine keeps appending transcript rows until it reaches its next
        cooperative interrupt checkpoint. A rewind (/undo) or restore (/redo)
        during that window races the still-writing turn and produces a landing
        the drain immediately clobbers (the 2026-07-14 undo-clobber incident).
        Reads `_draining_turns`, pruning the entry on access once its task is
        done. FAIL-OPEN: any lookup/introspection error returns False so a guard
        bug can never wedge /undo — the worst case reverts to prior behavior.
        """
        try:
            session_key = self._session_key_for_source(source)
            draining = getattr(self, "_draining_turns", None)
            if not draining:
                return False
            task = draining.get(session_key)
            if task is None:
                return False
            if task.done():
                draining.pop(session_key, None)  # prune-on-access
                return False
            return True
        except Exception as e:  # pragma: no cover - defensive fail-open
            logger.debug("undo/redo drain-guard lookup skipped: %s", e)
            return False

    def _switch_reasoning_kwargs(
        self,
        *,
        source: Any = None,
        session_key: "str | None" = None,
        model: str = "",
    ) -> dict:
        """Kwargs threading the session's resolved effort into ``switch_model``.

        ``agent.agent_runtime_helpers.switch_model`` lives in ``agent/`` and has
        no access to gateway session state, so it cannot see a ``/reasoning``
        session override (which lives on ``SessionState.conversation``, not in
        config.yaml). Left to itself it can only apply the #467 fallback rule
        (per-model override, else keep the agent's current effort); handing it
        the session-resolved value for the NEW model closes the last gap so the
        full ratified ladder applies:

            session ``/reasoning`` override > per-model ``reasoning_overrides``
            for the new model > the effort already in effect.

        Returns ``{}`` when resolution fails, which degrades to switch_model's
        own keep-current behaviour — never to the global config default.
        """
        try:
            return {
                "session_reasoning_config": self._resolve_session_reasoning_config(
                    source=source, session_key=session_key, model=model,
                )
            }
        except Exception:  # noqa: BLE001
            logger.debug("session reasoning resolution for /model failed", exc_info=True)
            return {}

    def _undo_tail_suffix(self, session_id: str) -> str:
        """Render a one-line '↦ now at …' confirmation of the active tail.

        Lets a user confirm at a glance WHERE the thread landed after /undo or
        /redo — which message is now last — without scrolling. Best-effort:
        never let a preview failure break the command's primary reply.
        """
        try:
            import hermes_undo

            hermes_undo._session_db = self.session_store._db
            info = hermes_undo.tail_preview(session_id)
        except Exception as e:
            logger.debug("undo/redo tail preview skipped: %s", e)
            return ""
        # The read itself failed (transient DB error) — the primary undo/redo
        # already succeeded, so omit the suffix rather than misreport "empty".
        if info.get("error"):
            return ""
        if info.get("empty"):
            return "\n" + t("gateway.undo.now_empty")
        # Bound the role to the set that has a translated party label; an
        # unexpected role (system/developer/legacy function) would otherwise
        # ask t() for a missing key and render the raw key path to the user.
        role = info.get("role") or "message"
        if role not in {"user", "assistant", "tool", "message"}:
            role = "message"
        who = t(f"gateway.undo.party.{role}")
        preview = info.get("preview")
        if preview:
            return "\n" + t("gateway.undo.now_at", who=who, preview=preview)
        return "\n" + t("gateway.undo.now_at_notext", who=who)

    async def _unrecord_merge_done(self, source_session_id, target_id) -> None:
        """Remove ``target_id`` from the source's ledger (rollback on failed fold)."""
        try:
            row = await self._session_db.get_session(source_session_id)
            mc = (row or {}).get("model_config")
            if isinstance(mc, str) and mc:
                import json as _json
                try:
                    mc = _json.loads(mc)
                except Exception:
                    return
            if not isinstance(mc, dict):
                return
            merged = mc.get("_merged_into")
            if not isinstance(merged, list):
                return
            new = [x for x in merged if str(x) != str(target_id)]
            if new != merged:
                mc["_merged_into"] = new
                import json as _json
                await self._session_db.update_session_meta(source_session_id, _json.dumps(mc))
        except Exception as exc:
            logger.debug("merge: could not roll back merge ledger: %s", exc)

    def _write_merge_record(self, source_title, source_id, target_title,
                            target_id, summary, platform):
        """Write a durable merge-record .md and return its path (or None).

        Location: ``<HERMES_HOME>/merges/<ts>-<source-slug>.md``. Self-purges
        records older than 30 days first. Best-effort — never raises.
        """
        import re as _re
        from datetime import datetime as _dt
        try:
            from hermes_constants import get_hermes_home
            base = get_hermes_home()
        except Exception:
            base = os.path.expanduser("~/.hermes")
        try:
            merges_dir = Path(base) / "merges"
            merges_dir.mkdir(parents=True, exist_ok=True)
            self._purge_old_merge_records(merges_dir)
            ts = _dt.now().strftime("%Y%m%d-%H%M%S")
            slug = _re.sub(r"[^a-z0-9]+", "-", (source_title or "session").lower()).strip("-")[:40] or "session"
            path = merges_dir / f"{ts}-{slug}.md"
            body = (
                f"# Merge record — {source_title}\n\n"
                f"- When: {_dt.now().isoformat(timespec='seconds')}\n"
                f"- Platform: {platform}\n"
                f"- Source session: {source_title} (`{source_id}`)\n"
                f"- Target session: {target_title} (`{target_id}`)\n\n"
                f"## Summary folded into the target\n\n{summary}\n"
            )
            path.write_text(body, encoding="utf-8")
            return path
        except Exception as exc:
            logger.debug("merge: could not write merge record: %s", exc)
            return None
