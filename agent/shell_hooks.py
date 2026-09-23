"""
Shell-script hooks bridge.

Reads the ``hooks:`` block from ``cli-config.yaml``, prompts the user for
consent on first use of each ``(event, command)`` pair, and registers
callbacks on the existing plugin hook manager so every existing
``invoke_hook()`` site dispatches to the configured shell scripts — with
zero changes to call sites.

Design notes
------------
* Python plugins and shell hooks compose naturally: both flow through
  :func:`hermes_cli.plugins.invoke_hook` and its aggregators.  Python
  plugins are registered first (via ``discover_and_load()``) so their
  block decisions win ties over shell-hook blocks.
* Subprocess execution uses ``shlex.split(os.path.expanduser(command))``
  with ``shell=False`` — no shell injection footguns.  Users that need
  pipes/redirection wrap their logic in a script.
* First-use consent is gated by the allowlist under
  ``~/.hermes/shell-hooks-allowlist.json``.  Non-TTY callers must pass
  ``accept_hooks=True`` (resolved from ``--accept-hooks``,
  ``HERMES_ACCEPT_HOOKS``, or ``hooks_auto_accept: true`` in config)
  for registration to succeed without a prompt.
* Registration is idempotent — safe to invoke from both the CLI entry
  point (``hermes_cli/main.py``) and the gateway entry point
  (``gateway/run.py``).

Wire protocol
-------------
**stdin** (JSON, piped to the script)::

    {
        "hook_event_name": "pre_tool_call",
        "tool_name":       "terminal",
        "tool_input":      {"command": "rm -rf /"},
        "session_id":      "sess_abc123",
        "cwd":             "/home/user/project",
        "extra":           {...}   # event-specific kwargs
    }

**stdout** (JSON, optional — anything else is ignored)::

    # Block a pre_tool_call (either shape accepted; normalised internally):
    {"decision": "block", "reason":  "Forbidden command"}   # Claude-Code-style
    {"action":   "block", "message": "Forbidden command"}   # Hermes-canonical

    # Inject context for pre_llm_call:
    {"context": "Today is Friday"}

    # Modify tool input for pre_tool_call (Hermes-canonical):
    {"action": "modify", "args": {"new_string": "fixed content"}}

    # Modify tool input for pre_tool_call (Claude-Code-style):
    {"decision": "modify", "tool_input": {"new_string": "fixed content"}}

    # Allow (exit 0; {} is also accepted as a legacy explicit no-op):
    {"action": "allow"}

    # Silent no-op for default fail-open hooks only:
    <empty or any non-matching JSON object>

**exit codes**

Exit code 2 from a ``pre_tool_call`` hook blocks the tool call even when
stdout carries no block JSON (Claude-Code / Cursor compatible).  The block
message is taken from the stdout block JSON when present, then the first
400 characters of stderr, then a generic default.  For events whose block
directive is not honored, exit 2 is logged at warning like any other
non-zero exit.  All other non-zero exits log a warning. Fail-closed hooks
block regardless of stdout; default fail-open hooks still parse it normally.

**failure semantics**

Hooks fail *open* by default: a spawn error, timeout, or unparseable
stdout logs a warning and contributes nothing.  A ``pre_tool_call`` entry
can opt into fail-*closed* semantics with ``fail_closed: true``
(``failClosed`` also accepted for Cursor/Claude-Code config compat) —
spawn errors, timeouts, abnormal exits, empty/malformed stdout, and callback
errors then BLOCK the tool call
with ``hook <name>#<digest> failed closed: <reason>``, where the name is a
secret-free identifier derived from the command (see ``hook_display_name``) —
the raw command goes to the log only, never to the model.  Use this for
security-gating hooks (secret scanners, policy checks) where a crashed
hook must not silently allow the action.  On non-blocking events
``fail_closed`` is ignored with a warning. A successful fail-closed hook must
emit a valid block, allow, or modify directive (or the legacy no-op ``{}``).

A fail-closed hook that exits non-zero WITHOUT the exit-2 deny convention has
MALFUNCTIONED rather than denied, and blocks with a distinct
``hook <name>#<digest> CRASHED`` message carrying ``error_class:
hook_internal_error`` — naming the exception class and, for an import failure,
the missing module.  Both outcomes still fail closed; only the classification
and the operator-facing text differ, so a broken hook is not mistaken for the
policy it would have enforced.

Per-event ``extra`` keys
~~~~~~~~~~~~~~~~~~~~~~~~

The ``extra`` object contains every kwarg that is **not** one of the
top-level payload keys (``tool_name``, ``args``, ``session_id``,
``parent_session_id``).  The tables below list the ``extra`` keys
emitted by each built-in hook site.

``post_tool_call`` (emitted from ``model_tools.py``)::

    result          – tool return value (serialised string)
    status          – "ok" | "error" | "blocked"
    error_type      – error category (e.g. "ValueError"), or None
    error_message   – human-readable error text, or None
    duration_ms     – wall-clock time in milliseconds
    task_id         – current task id (empty string if none)
    tool_call_id    – provider tool-call id
    turn_id         – current turn id
    api_request_id  – current API request id
    middleware_trace – list of dicts from tool middleware chain

``pre_tool_call`` (emitted from ``model_tools.py``)::

    task_id         – current task id (empty string if none)
    tool_call_id    – provider tool-call id
    turn_id         – current turn id
    api_request_id  – current API request id
    middleware_trace – list of dicts from tool middleware chain

``on_session_start`` (emitted from ``agent/conversation_loop.py``)::

    model           – model name (e.g. "claude-sonnet-4-20250514")
    platform        – platform identifier (e.g. "cli", "whatsapp")

``on_session_end`` (emitted from ``agent/turn_finalizer.py``)::

    task_id         – current task id
    turn_id         – current turn id
    completed       – bool, True when the turn produced a final response
    interrupted     – bool, True when the user interrupted
    model           – model name
    platform        – platform identifier

``subagent_stop`` (emitted from ``tools/delegate_tool.py``)::

    parent_turn_id  – parent agent's current turn id
    child_session_id – child (subagent) session id
    child_role      – role string of the child agent
    child_summary   – summary of the child's work
    child_status    – exit status string (e.g. "success", "error")
    tool_call_history – redacted tool name/input summary/byte counts/status list
    duration_ms     – wall-clock time of the child run in milliseconds
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple

from hermes_cli._subprocess_compat import IS_WINDOWS, kill_process_tree, windows_hide_flags

try:
    import fcntl  # POSIX only; Windows falls back to best-effort without flock.
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from hermes_constants import get_hermes_home
from utils import atomic_replace

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 300
ALLOWLIST_FILENAME = "shell-hooks-allowlist.json"
_DEFAULT_BLOCK_MESSAGE = "Blocked by shell hook."

# Exit code that signals "block this action" from a hook script, independent
# of stdout content.  Claude Code / Cursor compatible.
BLOCK_EXIT_CODE = 2

# Events whose block directive is actually honored downstream (see
# hermes_cli.plugins.get_pre_tool_call_block_message / _get_pre_tool_call_
# directive_details).  Exit-code-2 blocking and ``fail_closed`` only make
# sense for these.
_BLOCKING_EVENTS = frozenset({"pre_tool_call"})

# Cap on stderr excerpt reused as a block message.
_STDERR_MESSAGE_LIMIT = 400


# (event, matcher, command) triples that have been wired to the plugin
# manager in the current process.  Matcher is part of the key because
# the same script can legitimately register for different matchers under
# the same event (e.g. one entry per tool the user wants to gate).
# Second registration attempts for the exact same triple become no-ops
# so the CLI and gateway can both call register_from_config() safely.
_registered: Set[Tuple[str, Optional[str], str]] = set()
_registered_lock = threading.Lock()

# Intra-process lock for allowlist read-modify-write on platforms that
# lack ``fcntl`` (non-POSIX).  Kept separate from ``_registered_lock``
# because ``register_from_config`` already holds ``_registered_lock`` when
# it triggers ``_record_approval`` — reusing it here would self-deadlock
# (``threading.Lock`` is non-reentrant).  POSIX callers use the sibling
# ``.lock`` file via ``fcntl.flock`` and bypass this.
_allowlist_write_lock = threading.Lock()


@dataclass
class ShellHookSpec:
    """Parsed and validated representation of a single ``hooks:`` entry."""

    event: str
    command: str
    matcher: Optional[str] = None
    timeout: int = DEFAULT_TIMEOUT_SECONDS
    fail_closed: bool = False
    compiled_matcher: Optional[re.Pattern] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        # Strip whitespace introduced by YAML quirks (e.g. multi-line string
        # folding) — a matcher of " terminal" would otherwise silently fail
        # to match "terminal" without any diagnostic.
        if isinstance(self.matcher, str):
            stripped = self.matcher.strip()
            self.matcher = stripped if stripped else None
        if self.matcher:
            try:
                self.compiled_matcher = re.compile(self.matcher)
            except re.error as exc:
                logger.warning(
                    "shell hook matcher %r is invalid (%s) — treating as "
                    "literal equality", self.matcher, exc,
                )
                self.compiled_matcher = None

    def matches_tool(self, tool_name: Optional[str]) -> bool:
        if not self.matcher:
            return True
        if tool_name is None:
            return False
        if self.compiled_matcher is not None:
            return self.compiled_matcher.fullmatch(tool_name) is not None
        # compiled_matcher is None only when the regex failed to compile,
        # in which case we already warned and fall back to literal equality.
        return tool_name == self.matcher


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def register_from_config(
    cfg: Optional[Dict[str, Any]],
    *,
    accept_hooks: bool = False,
) -> List[ShellHookSpec]:
    """Register every configured shell hook on the plugin manager.

    ``cfg`` is the full parsed config dict (``hermes_cli.config.load_config``
    output).  The ``hooks:`` key is read out of it.  Missing, empty, or
    non-dict ``hooks`` is treated as zero configured hooks.

    ``accept_hooks=True`` skips the TTY consent prompt — the caller is
    promising that the user has opted in via a flag, env var, or config
    setting.  ``HERMES_ACCEPT_HOOKS=1`` and ``hooks_auto_accept: true`` are
    also honored inside this function so either CLI or gateway call sites
    pick them up.

    Returns the list of :class:`ShellHookSpec` entries that ended up wired
    up on the plugin manager.  Skipped entries (unknown events, malformed,
    not allowlisted, already registered) are logged but not returned.
    """
    if not isinstance(cfg, dict):
        return []

    # Safe mode (--safe-mode / HERMES_SAFE_MODE=1): shell hooks are user
    # customizations too — skip registration entirely so a troubleshooting
    # run fires zero user-configured code (plugins, MCP, AND hooks).
    from utils import env_var_enabled

    if env_var_enabled("HERMES_SAFE_MODE"):
        logger.info("HERMES_SAFE_MODE=1 — shell-hook registration skipped")
        return []

    effective_accept = _resolve_effective_accept(cfg, accept_hooks)

    specs = _parse_hooks_block(cfg.get("hooks"))
    if not specs:
        return []

    registered: List[ShellHookSpec] = []

    # Import lazily — avoids circular imports at module-load time.
    from hermes_cli.plugins import get_plugin_manager

    manager = get_plugin_manager()

    # Idempotence + allowlist read happen under the lock; the TTY
    # prompt runs outside so other threads aren't parked on a blocking
    # input().  Mutation re-takes the lock with a defensive idempotence
    # re-check in case two callers ever race through the prompt.
    for spec in specs:
        key = (spec.event, spec.matcher, spec.command)
        with _registered_lock:
            if key in _registered:
                continue
            already_allowlisted = _is_allowlisted(spec.event, spec.command)

        if not already_allowlisted:
            if not _prompt_and_record(
                spec.event, spec.command, accept_hooks=effective_accept,
            ):
                logger.warning(
                    "shell hook for %s (%s) not allowlisted — skipped. "
                    "Use --accept-hooks / HERMES_ACCEPT_HOOKS=1 / "
                    "hooks_auto_accept: true, or approve at the TTY "
                    "prompt next run.",
                    spec.event, spec.command,
                )
                continue

        with _registered_lock:
            if key in _registered:
                continue
            manager._hooks.setdefault(spec.event, []).append(_make_callback(spec))
            _registered.add(key)
            registered.append(spec)
            logger.info(
                "shell hook registered: %s -> %s (matcher=%s, timeout=%ds, "
                "fail_closed=%s)",
                spec.event, spec.command, spec.matcher, spec.timeout,
                spec.fail_closed,
            )

    return registered


def iter_configured_hooks(cfg: Optional[Dict[str, Any]]) -> List[ShellHookSpec]:
    """Return the parsed ``ShellHookSpec`` entries from config without
    registering anything.  Used by ``hermes hooks list`` and ``doctor``."""
    if not isinstance(cfg, dict):
        return []
    return _parse_hooks_block(cfg.get("hooks"))


def re_register_config_hooks() -> None:
    """Re-register shell hooks from config after a plugin force-reload.

    ``PluginManager.discover_and_load(force=True)`` unloads via the ownership
    ledger and clears the manager's ``_hooks`` dict, which silently drops
    shell hooks that were registered from ``config.yaml`` at startup (they
    are config-owned, not plugin-owned, so the ledger cannot restore them).
    Clear the idempotence set and re-run ``register_from_config()`` so hooks
    are wired again (#60036 / PR #60267; tracking #64178 — salvaged from
    PR #64188).

    Commands already allowlisted stay allowlisted, so this never re-prompts
    at a TTY for hooks the user previously approved.
    """
    with _registered_lock:
        _registered.clear()
    from hermes_cli.config import load_config

    register_from_config(load_config())


def reset_for_tests() -> None:
    """Clear the idempotence set.  Test-only helper."""
    with _registered_lock:
        _registered.clear()


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------

def _parse_hooks_block(hooks_cfg: Any) -> List[ShellHookSpec]:
    """Normalise the ``hooks:`` dict into a flat list of ``ShellHookSpec``.

    Malformed entries warn-and-skip — we never raise from config parsing
    because a broken hook must not crash the agent.
    """
    from hermes_cli.plugins import SHELL_UNSUPPORTED_HOOKS, VALID_HOOKS

    if not isinstance(hooks_cfg, dict):
        return []

    specs: List[ShellHookSpec] = []

    for event_name, entries in hooks_cfg.items():
        # Reserved sub-keys that aren't event names — skip silently. These
        # are config sub-sections nested under `hooks:` for related
        # functionality (e.g. output-spill budgets, outbound webhooks —
        # the latter parsed by agent/outbound_webhooks.py).
        if event_name in ("output_spill", "outbound"):
            continue
        if event_name in SHELL_UNSUPPORTED_HOOKS:
            # Registering would "succeed" while the hook's return value is
            # silently dropped (_parse_response has no channel for these
            # events' directives) — refuse loudly instead.
            logger.warning(
                "hook event %r is Python-plugin-only: shell hooks cannot "
                "return its directive, so this registration is refused "
                "rather than silently ignored",
                event_name,
            )
            continue
        if event_name not in VALID_HOOKS:
            suggestion = difflib.get_close_matches(
                str(event_name), VALID_HOOKS, n=1, cutoff=0.6,
            )
            if suggestion:
                logger.warning(
                    "unknown hook event %r in hooks: config — did you mean %r?",
                    event_name, suggestion[0],
                )
            else:
                logger.warning(
                    "unknown hook event %r in hooks: config (valid: %s)",
                    event_name, ", ".join(sorted(VALID_HOOKS)),
                )
            continue

        if entries is None:
            continue

        if not isinstance(entries, list):
            logger.warning(
                "hooks.%s must be a list of hook definitions; got %s",
                event_name, type(entries).__name__,
            )
            continue

        for i, raw in enumerate(entries):
            spec = _parse_single_entry(event_name, i, raw)
            if spec is not None:
                specs.append(spec)

    return specs


def _parse_single_entry(
    event: str, index: int, raw: Any,
) -> Optional[ShellHookSpec]:
    if not isinstance(raw, dict):
        logger.warning(
            "hooks.%s[%d] must be a mapping with a 'command' key; got %s",
            event, index, type(raw).__name__,
        )
        return None

    command = raw.get("command")
    if not isinstance(command, str) or not command.strip():
        logger.warning(
            "hooks.%s[%d] is missing a non-empty 'command' field",
            event, index,
        )
        return None

    matcher = raw.get("matcher")
    if matcher is not None and not isinstance(matcher, str):
        logger.warning(
            "hooks.%s[%d].matcher must be a string regex; ignoring",
            event, index,
        )
        matcher = None

    if matcher is not None and event not in {"pre_tool_call", "post_tool_call"}:
        logger.warning(
            "hooks.%s[%d].matcher=%r will be ignored at runtime — the "
            "matcher field is only honored for pre_tool_call / "
            "post_tool_call.  The hook will fire on every %s event.",
            event, index, matcher, event,
        )
        matcher = None

    timeout_raw = raw.get("timeout", DEFAULT_TIMEOUT_SECONDS)
    try:
        timeout = int(timeout_raw)
    except (TypeError, ValueError):
        logger.warning(
            "hooks.%s[%d].timeout must be an int (got %r); using default %ds",
            event, index, timeout_raw, DEFAULT_TIMEOUT_SECONDS,
        )
        timeout = DEFAULT_TIMEOUT_SECONDS

    if timeout < 1:
        logger.warning(
            "hooks.%s[%d].timeout must be >=1; using default %ds",
            event, index, DEFAULT_TIMEOUT_SECONDS,
        )
        timeout = DEFAULT_TIMEOUT_SECONDS

    if timeout > MAX_TIMEOUT_SECONDS:
        logger.warning(
            "hooks.%s[%d].timeout=%ds exceeds max %ds; clamping",
            event, index, timeout, MAX_TIMEOUT_SECONDS,
        )
        timeout = MAX_TIMEOUT_SECONDS

    # ``fail_closed`` (canonical) / ``failClosed`` (Cursor/Claude-Code
    # config compat).  Canonical spelling wins when both are present.
    fail_closed_raw = raw.get("fail_closed", raw.get("failClosed", False))
    if not isinstance(fail_closed_raw, bool):
        logger.warning(
            "hooks.%s[%d].fail_closed must be a boolean (got %r); "
            "using default false (fail open)",
            event, index, fail_closed_raw,
        )
        fail_closed_raw = False
    fail_closed = fail_closed_raw

    if fail_closed and event not in _BLOCKING_EVENTS:
        logger.warning(
            "hooks.%s[%d].fail_closed=true will be ignored at runtime — "
            "fail_closed only applies to blocking-capable events (%s).  "
            "The hook will fail open on %s like any other hook.",
            event, index, ", ".join(sorted(_BLOCKING_EVENTS)), event,
        )
        fail_closed = False

    return ShellHookSpec(
        event=event,
        command=command.strip(),
        matcher=matcher,
        timeout=timeout,
        fail_closed=fail_closed,
    )


# ---------------------------------------------------------------------------
# Subprocess callback
# ---------------------------------------------------------------------------

_TOP_LEVEL_PAYLOAD_KEYS = {"tool_name", "args", "session_id", "parent_session_id"}


def _spawn(spec: ShellHookSpec, stdin_json: str) -> Dict[str, Any]:
    """Run ``spec.command`` as a subprocess with ``stdin_json`` on stdin.

    Returns a diagnostic dict with the same keys for every outcome
    (``returncode``, ``stdout``, ``stderr``, ``timed_out``,
    ``elapsed_seconds``, ``error``, ``error_detail``).  This is the single
    place the subprocess is actually invoked — both the live callback path
    (:func:`_make_callback`) and the CLI test helper (:func:`run_once`)
    go through it.

    TWO error channels, because they have opposite requirements:

    * ``error`` is REDACTED and model-facing. It reaches the model through
      :func:`_evaluate_result` -> :func:`_fail_closed_block`, so it carries
      the failure's exception CLASS only — never the raw command, argv, or
      ``str(exc)``, any of which can hold an inline credential.
    * ``error_detail`` is the OPERATOR channel: the full diagnostic text, for
      the log and for ``hermes hooks test`` / ``hermes doctor``. Without it a
      shlex ``No closing quotation`` and a spawn ``EACCES``/``ENOEXEC`` are
      indistinguishable to whoever has to fix the hook. It is ``None`` when
      there is nothing to add beyond ``error``.

    ``error_detail`` must never be routed to a model-facing string.
    """
    result: Dict[str, Any] = {
        "returncode": None,
        "stdout": "",
        "stderr": "",
        "timed_out": False,
        "elapsed_seconds": 0.0,
        "error": None,
        "error_detail": None,
    }
    try:
        # Windows-safe: plain shlex.split eats backslashes in paths (#78293).
        from hermes_cli._subprocess_compat import split_command_line

        argv = split_command_line(os.path.expanduser(spec.command))
    except ValueError as exc:
        # `error` reaches the MODEL via _evaluate_result -> _fail_closed_block,
        # so it must never carry the raw command or exception text derived from
        # it: an unparseable command is exactly the shape that still holds an
        # inline credential (`sh -c 'export TOK=…` with the quote left open).
        # Name the hook by its digest and the failure by its exception CLASS.
        # The raw command stays on the log channel only (_evaluate_result).
        result["error"] = (
            f"command cannot be parsed ({type(exc).__name__})"
        )
        # Operator channel only — `exc` stringifies the offending command text.
        result["error_detail"] = f"command cannot be parsed: {exc}"
        return result
    if not argv:
        result["error"] = "empty command"
        return result

    t0 = time.monotonic()
    # Spawn the hook in its own process group on POSIX (``process_group=0``,
    # Python ≥3.11) so a timed-out hook's descendants can be reaped with the
    # hook itself. Windows keeps the hidden-window flags; tree cleanup there
    # goes through ``taskkill /T`` in ``kill_process_tree``. Hooks that
    # complete in time keep their descendants — an intentionally detached
    # helper (``some-daemon &``) survives a successful run. Ported from
    # openai/codex#37527 ("Terminate timed-out hook process trees").
    _popen_kwargs: Dict[str, Any] = (
        {"creationflags": windows_hide_flags()} if IS_WINDOWS else {"process_group": 0}
    )
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True, encoding='utf-8', errors='replace',
            shell=False,
            **_popen_kwargs,
        )
    except FileNotFoundError:
        result["error"] = "command not found"
        return result
    except PermissionError:
        result["error"] = "command not executable"
        return result
    except Exception as exc:  # pragma: no cover — defensive
        # Same channel as the parse failure above: spawn exceptions routinely
        # embed argv (OSError stringifies the program path), and argv[0] can be
        # the credential itself. Exception CLASS only.
        result["error"] = f"spawn failed ({type(exc).__name__})"
        # Operator channel only — OSError stringifies argv/the program path,
        # and this is what distinguishes EACCES from ENOEXEC from EMFILE.
        result["error_detail"] = f"spawn failed: {exc}"
        return result

    try:
        stdout, stderr = proc.communicate(input=stdin_json, timeout=spec.timeout)
    except subprocess.TimeoutExpired:
        # Take down the whole process tree, not just the direct child —
        # otherwise a hook that forked helpers leaves them running (and,
        # holding the pipe write ends, they'd stall the drain below).
        kill_process_tree(proc)
        try:
            proc.communicate(timeout=1)
        except Exception:
            pass
        result["timed_out"] = True
        result["elapsed_seconds"] = round(time.monotonic() - t0, 3)
        return result
    except Exception as exc:  # pragma: no cover — defensive
        kill_process_tree(proc)
        try:
            proc.communicate(timeout=1)
        except Exception:
            pass
        # Model-facing via _fail_closed_block — exception CLASS only, never
        # text that may carry argv or payload content.
        result["error"] = f"communication failed ({type(exc).__name__})"
        # Operator channel only — may carry argv or payload content.
        result["error_detail"] = f"communication failed: {exc}"
        return result

    result["returncode"] = proc.returncode
    result["stdout"] = stdout or ""
    result["stderr"] = stderr or ""
    result["elapsed_seconds"] = round(time.monotonic() - t0, 3)
    return result


def _make_callback(spec: ShellHookSpec) -> Callable[..., Optional[Dict[str, Any]]]:
    """Build the closure that ``invoke_hook()`` will call per firing."""

    def _callback(**kwargs: Any) -> Optional[Dict[str, Any]]:
        try:
            # Matcher gate — only meaningful for tool-scoped events.
            if spec.event in {"pre_tool_call", "post_tool_call"}:
                if not spec.matches_tool(kwargs.get("tool_name")):
                    return None

            r = _spawn(spec, _serialize_payload(spec.event, kwargs))
            return _evaluate_result(spec, r)
        except Exception as exc:
            # Generic plugin/dispatcher catches fail open. Keep the policy
            # decision here, where the individual hook's opt-in is known.
            logger.warning(
                "shell hook callback failed (event=%s command=%s): %s",
                spec.event, spec.command, exc,
            )
            if spec.fail_closed and spec.event in _BLOCKING_EVENTS:
                return _fail_closed_block(spec, f"callback error ({type(exc).__name__})")
            return None

    # The dispatcher reads __name__ into user-facing refusal messages returned
    # to the MODEL (hermes_cli/plugins.py), so it must never carry the raw
    # command — hook commands routinely embed credentials inline. The raw
    # command stays on the log channel only, via `spec.command` above.
    _callback.__name__ = f"shell_hook[{spec.event}:{hook_display_name(spec.command)}]"
    _callback.__qualname__ = _callback.__name__
    # The outer plugin callback budget can expire before the shell hook's own
    # subprocess timeout under severe scheduler contention. Preserve this
    # individual hook's configured failure policy at that outer boundary.
    setattr(_callback, "_hermes_timeout_fail_closed", spec.fail_closed)
    return _callback


# A display label may ONLY be a plain basename-shaped token. This is an
# ALLOWLIST on purpose: blocklisting "unsafe" command shapes means every shape
# nobody anticipated (``env TOK=secret prog``, ``https://user:secret@host``,
# an unbalanced quote) silently becomes a label and smuggles its content out.
# Anything that does not match is dropped and identity falls to the digest.
_HOOK_LABEL_ALLOWED = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._+-]*\Z")

# Leading tokens that are launchers rather than the hook's own identity: keep
# looking past them for the script that actually names the hook, since real
# configs read ``/usr/bin/python3 /path/to/my-guard.py``.
_HOOK_LAUNCHER_STEMS = frozenset({
    "sh", "bash", "zsh", "dash", "ksh", "fish", "env",
    "python", "python2", "python3", "perl", "ruby", "node", "deno", "bun",
    "uv", "uvx", "npx", "pwsh", "powershell",
})


def hook_display_name(command: str) -> str:
    """Return a stable, secret-free identifier for a hook *command*.

    A hook command is operator-supplied and routinely carries credentials
    inline (``sh -c 'export TOK=…; …'``). The raw command must therefore never
    reach a model-visible channel; only the log gets it. This yields
    ``<label>#<digest>``, where *label* is a plain basename-shaped token taken
    from the front of the command and *digest* is a short stable hash of the
    whole command so two hooks are always distinguishable even when their
    labels collide (or when no token qualifies as a label at all).
    """
    digest = hashlib.sha256(command.encode("utf-8", "replace")).hexdigest()[:8]
    try:
        tokens = shlex.split(command)
    except ValueError:
        # Unbalanced quotes — fall back to whitespace splitting. The allowlist
        # below is what keeps this safe, not the quality of the parse.
        tokens = command.split()
    label = ""
    for token in tokens:
        if token.startswith("-"):
            # A flag; anything after it may be an inline program body
            # (``-c 'export TOK=…'``). Never walk into that.
            break
        base = os.path.basename(token)
        if not _HOOK_LABEL_ALLOWED.match(base):
            break
        if base.split(".")[0].lower() in _HOOK_LAUNCHER_STEMS:
            label = base  # remember the launcher, prefer a script after it
            continue
        label = base
        break
    return f"{label or 'hook'}#{digest}"


def _fail_closed_block(spec: ShellHookSpec, reason: str) -> Dict[str, Any]:
    """Canonical block shape for a ``fail_closed`` hook that failed.

    Names the hook by its :func:`hook_display_name`, never by ``spec.command``:
    this message is returned to the model, and hook commands carry secrets.
    """
    return {
        "action": "block",
        "message": (
            f"hook {hook_display_name(spec.command)} failed closed: {reason}"
        ),
    }


# A crashed hook's stderr is NOT a model-facing channel — it is whatever the
# process happened to print, and a hook command routinely holds credentials
# that a traceback can echo back (an `os.environ` repr, an argv dump). So the
# diagnosis below extracts only two ALLOWLISTED shapes: the exception CLASS
# name and, for an import failure, the missing MODULE name. Both are bare
# dotted identifiers by construction of these patterns; no free text from the
# child crosses into the block message.
_TRACEBACK_HEADER = "Traceback (most recent call last):"
_MISSING_MODULE = re.compile(r"No module named ['\"]([A-Za-z0-9_.]+)['\"]")
_EXCEPTION_LINE = re.compile(
    r"\A(?P<cls>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"
    r"(?:Error|Exception|Interrupt|Exit))\s*(?::|\Z)"
)


def _crash_diagnosis(stderr: str) -> Optional[str]:
    """Name a hook's CRASH from its stderr, or ``None`` if it is not one.

    A hook that dies is indistinguishable at the transport from a hook that
    denies: both arrive as a block. Measured 2026-09-22 — a hook whose sibling
    module was absent from the deployed tree surfaced only as ``hook exited
    1``, naming the POLICY it would have enforced, so operators went looking
    for the policy violation instead of the missing file, and the pressure was
    to switch a working gate off for an unrelated reason.
    """
    if _TRACEBACK_HEADER not in stderr:
        return None
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    for line in reversed(lines):
        match = _EXCEPTION_LINE.match(line)
        if not match:
            continue
        exc_class = match.group("cls")
        module = _MISSING_MODULE.search(line)
        if module:
            return (
                f"{exc_class}: the hook could not import `{module.group(1)}`, "
                "so it never evaluated this call"
            )
        return f"{exc_class} raised before the hook could evaluate this call"
    return None


def _malfunction_block(
    spec: ShellHookSpec, returncode: int, stderr: str,
) -> Dict[str, Any]:
    """Block for a fail-closed hook that MALFUNCTIONED rather than denied.

    In this protocol a denial is exit 2 or a ``block`` directive on stdout. Any
    other non-zero exit from a ``fail_closed`` hook is therefore a broken hook,
    not a policy decision — and the operator-facing text has to say so, because
    the two used to be worded identically. It still FAILS CLOSED; only the
    classification and the message change.
    """
    diagnosis = _crash_diagnosis(stderr)
    detail = diagnosis or (
        f"exited {returncode} without emitting a policy directive"
    )
    return {
        "action": "block",
        "message": (
            f"hook {hook_display_name(spec.command)} CRASHED — this is a BROKEN "
            f"ENFORCEMENT HOOK, not a policy denial. {detail}. Failing closed, "
            "so nothing was permitted that the hook would have refused. Fix the "
            "hook itself (a missing module named above is usually a partial "
            "deploy: restore it on the path the hook runs from, then retry). Do "
            "NOT disable the guard to clear this — the policy did not object to "
            "this call, the hook never ran."
        ),
        "error_class": "hook_internal_error",
    }


def _evaluate_result(
    spec: ShellHookSpec, r: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Turn a :func:`_spawn` diagnostic dict into the hook's contribution.

    Single place that encodes the failure semantics:

    * spawn error / timeout — fail open (log + ``None``) unless the spec
      is ``fail_closed`` on a blocking-capable event, in which case a
      canonical block shape is returned;
    * exit code 2 on a blocking-capable event — block, with the message
      taken from stdout block JSON, then stderr, then a default
      (Claude-Code / Cursor compatible);
    * other non-zero exits — block if ``fail_closed``, otherwise warn and
      parse stdout normally;
    * empty / invalid stdout on a ``fail_closed`` blocking hook — block
      instead of silently contributing nothing. Successful allow/modify
      directives and the legacy explicit no-op ``{}`` remain accepted.

    Shared by the live callback path (:func:`_make_callback`) and the CLI
    test helper (:func:`run_once`) so ``hermes hooks test`` reflects
    production behaviour exactly.
    """
    blocking_event = spec.event in _BLOCKING_EVENTS
    fail_closed = spec.fail_closed and blocking_event

    if r["error"]:
        # Log/operator channel gets the DETAILED reason when there is one; the
        # model-facing block below keeps the redacted `error`. This log line
        # already carries `spec.command`, so it is not a new disclosure.
        logger.warning(
            "shell hook failed (event=%s command=%s): %s",
            spec.event, spec.command, r.get("error_detail") or r["error"],
        )
        if fail_closed:
            return _fail_closed_block(spec, r["error"])
        return None
    if r["timed_out"]:
        logger.warning(
            "shell hook timed out after %.2fs (event=%s command=%s)",
            r["elapsed_seconds"], spec.event, spec.command,
        )
        if fail_closed:
            return _fail_closed_block(
                spec, f"timed out after {spec.timeout}s",
            )
        return None

    stderr = r["stderr"].strip()
    if stderr:
        logger.debug(
            "shell hook stderr (event=%s command=%s): %s",
            spec.event, spec.command, stderr[:_STDERR_MESSAGE_LIMIT],
        )

    # Exit code 2 = block (Claude-Code / Cursor compatible), for events
    # whose block directive is honored downstream.  stdout block JSON
    # still wins for the message; otherwise stderr, then a default.
    if r["returncode"] == BLOCK_EXIT_CODE and blocking_event:
        parsed = _parse_response(spec.event, r["stdout"])
        if isinstance(parsed, dict) and parsed.get("action") == "block":
            return parsed
        message = stderr[:_STDERR_MESSAGE_LIMIT] or _DEFAULT_BLOCK_MESSAGE
        logger.info(
            "shell hook exited %d — blocking (event=%s command=%s): %s",
            BLOCK_EXIT_CODE, spec.event, spec.command, message,
        )
        return {"action": "block", "message": message}

    # Never trust an allow/modify payload from an unsuccessful policy check.
    # Default fail-open hooks retain their legacy stdout handling.
    if r["returncode"] != 0:
        logger.warning(
            "shell hook exited %d (event=%s command=%s); stderr=%s",
            r["returncode"], spec.event, spec.command,
            stderr[:_STDERR_MESSAGE_LIMIT],
        )

        if fail_closed:
            # A DENIAL is exit 2 (handled above) or a block directive on
            # stdout. Any other non-zero exit is a MALFUNCTION, and saying
            # "failed closed" for both is what sent an operator hunting a
            # merge they never attempted while the real cause was an absent
            # module. Same fail-closed outcome, self-describing message.
            return _malfunction_block(spec, r["returncode"], stderr)

    stdout = (r["stdout"] or "").strip()
    parsed = _parse_response(spec.event, stdout)

    if parsed is None and fail_closed:
        # None is ambiguous: allow and legacy {} are intentional no-ops,
        # whereas missing output, typos and malformed modify args are not.
        try:
            data = json.loads(stdout)
        except (ValueError, RecursionError):
            return _fail_closed_block(
                spec, "unparseable stdout (expected a JSON object)",
            )
        if isinstance(data, dict):
            actions = [data[key] for key in ("action", "decision") if key in data]
            if not data or (actions and all(action == "allow" for action in actions)):
                return None
        return _fail_closed_block(spec, "invalid stdout (expected allow, block, or modify)")

    return parsed


def _serialize_payload(event: str, kwargs: Dict[str, Any]) -> str:
    """Render the stdin JSON payload.  Unserialisable values are
    stringified via ``default=str`` rather than dropped."""
    extras = {k: v for k, v in kwargs.items() if k not in _TOP_LEVEL_PAYLOAD_KEYS}
    try:
        cwd = str(Path.cwd())
    except OSError:
        cwd = ""
    payload = {
        "hook_event_name": event,
        "tool_name": kwargs.get("tool_name"),
        "tool_input": kwargs.get("args") if isinstance(kwargs.get("args"), dict) else None,
        "session_id": kwargs.get("session_id") or kwargs.get("parent_session_id") or "",
        "cwd": cwd,
        "extra": extras,
    }
    return json.dumps(payload, ensure_ascii=False, default=str)


def _block_message(primary: Any, secondary: Any) -> str:
    """Return a validated string block message, falling back to the default.

    Accepts two candidate fields (primary wins over secondary) so callers
    can express field-priority differences between the two hook wire formats
    without duplicating the type-check logic.
    """
    raw = primary or secondary
    return raw if isinstance(raw, str) and raw else _DEFAULT_BLOCK_MESSAGE


def _parse_response(event: str, stdout: str) -> Optional[Dict[str, Any]]:
    """Translate stdout JSON into a Hermes wire-shape dict.

    For ``pre_tool_call`` the Claude-Code-style ``{"decision": "block",
    "reason": "..."}`` payload is translated into the canonical Hermes
    ``{"action": "block", "message": "..."}`` shape expected by
    :func:`hermes_cli.plugins.get_pre_tool_call_block_message`.  This is
    the single most important correctness invariant in this module —
    skipping the translation silently breaks every ``pre_tool_call``
    block directive.

    For ``pre_tool_call`` the ``modify`` action (canonical: ``{"action":
    "modify", "args": {...}}``, Claude-Code-style: ``{"decision":
    "modify", "tool_input": {...}}``) is translated to
    ``{"action": "modify", "args": {...}}`` so callers can merge the
    returned fields into the tool's ``args`` before dispatch.

    For ``pre_llm_call``, ``{"context": "..."}`` is passed through
    unchanged to match the existing plugin-hook contract.

    Anything else returns ``None``.
    """
    stdout = (stdout or "").strip()
    if not stdout:
        return None

    try:
        data = json.loads(stdout)
    except (ValueError, RecursionError):
        logger.warning(
            "shell hook stdout was not valid JSON (event=%s): %s",
            event, stdout[:200],
        )
        return None

    if not isinstance(data, dict):
        return None

    if event == "pre_tool_call":
        if data.get("action") == "block":
            return {"action": "block", "message": _block_message(data.get("message"), data.get("reason"))}
        if data.get("decision") == "block":
            return {"action": "block", "message": _block_message(data.get("reason"), data.get("message"))}
        # "modify" action — transform tool_input before dispatch
        if data.get("action") == "modify":
            new_args = data.get("args")
            if isinstance(new_args, dict):
                return {"action": "modify", "args": new_args}
        if data.get("decision") == "modify":
            new_args = data.get("tool_input")
            if isinstance(new_args, dict):
                return {"action": "modify", "args": new_args}
        return None

    if event == "pre_verify":
        # "continue" (Hermes) / "block" (Claude-Code Stop: block the stop) both
        # mean keep going; the message/reason is the follow-up for the model. A
        # continue with no message is a no-op — let the turn finish.
        action = str(data.get("action") or data.get("decision") or "").strip().lower()
        if action in {"continue", "block"}:
            message = data.get("message") or data.get("reason")
            if isinstance(message, str) and message.strip():
                return {"action": "continue", "message": message.strip()}
        return None

    context = data.get("context")
    if isinstance(context, str) and context.strip():
        return {"context": context}

    return None


# ---------------------------------------------------------------------------
# Allowlist / consent
# ---------------------------------------------------------------------------

def allowlist_path() -> Path:
    """Path to the per-user shell-hook allowlist file."""
    return get_hermes_home() / ALLOWLIST_FILENAME


def load_allowlist() -> Dict[str, Any]:
    """Return the parsed allowlist, or an empty skeleton if absent."""
    try:
        raw = json.loads(allowlist_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"approvals": []}
    if not isinstance(raw, dict):
        return {"approvals": []}
    approvals = raw.get("approvals")
    if not isinstance(approvals, list):
        raw["approvals"] = []
    return raw


def save_allowlist(data: Dict[str, Any]) -> None:
    """Atomically persist the allowlist via per-process ``mkstemp`` +
    ``os.replace``.  Cross-process read-modify-write races are handled
    by :func:`_locked_update_approvals` (``fcntl.flock``).  On OSError
    the failure is logged; the in-process hook still registers but
    the approval won't survive across runs."""
    p = allowlist_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=f"{p.name}.", suffix=".tmp", dir=str(p.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(data, indent=2, sort_keys=True))
            atomic_replace(tmp_path, p)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except OSError as exc:
        logger.warning(
            "Failed to persist shell hook allowlist to %s: %s. "
            "The approval is in-memory for this run, but the next "
            "startup will re-prompt (or skip registration on non-TTY "
            "runs without --accept-hooks / HERMES_ACCEPT_HOOKS).",
            p, exc,
        )


def _is_allowlisted(event: str, command: str) -> bool:
    data = load_allowlist()
    return any(
        isinstance(e, dict)
        and e.get("event") == event
        and e.get("command") == command
        for e in data.get("approvals", [])
    )


@contextmanager
def _locked_update_approvals() -> Iterator[Dict[str, Any]]:
    """Serialise read-modify-write on the allowlist across processes.

    Holds an exclusive ``flock`` on a sibling lock file for the duration
    of the update so concurrent ``_record_approval``/``revoke`` callers
    cannot clobber each other's changes (the race Codex reproduced with
    20–50 simultaneous writers).  Falls back to an in-process lock on
    platforms without ``fcntl``.
    """
    p = allowlist_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    lock_path = p.with_suffix(p.suffix + ".lock")

    if fcntl is None:  # pragma: no cover — non-POSIX fallback
        with _allowlist_write_lock:
            data = load_allowlist()
            yield data
            save_allowlist(data)
        return

    with open(lock_path, "a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            data = load_allowlist()
            yield data
            save_allowlist(data)
        finally:
            try:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
            except (OSError, IOError):
                pass


def _prompt_and_record(
    event: str, command: str, *, accept_hooks: bool,
) -> bool:
    """Decide whether to approve an unseen ``(event, command)`` pair.
    Returns ``True`` iff the approval was granted and recorded.
    """
    if accept_hooks:
        _record_approval(event, command)
        logger.info(
            "shell hook auto-approved via --accept-hooks / env / config: "
            "%s -> %s", event, command,
        )
        return True

    if not sys.stdin.isatty():
        return False

    print(
        f"\n⚠ Hermes is about to register a shell hook that will run a\n"
        f"  command on your behalf.\n\n"
        f"    Event:   {event}\n"
        f"    Command: {command}\n\n"
        f"  Commands run with your full user credentials.  Only approve\n"
        f"  commands you trust."
    )
    try:
        answer = input("Allow this hook to run? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()  # keep the terminal tidy after ^C
        return False

    if answer in {"y", "yes"}:
        _record_approval(event, command)
        return True

    return False


def _record_approval(event: str, command: str) -> None:
    entry = {
        "event": event,
        "command": command,
        "approved_at": _utc_now_iso(),
        "script_mtime_at_approval": script_mtime_iso(command),
    }
    with _locked_update_approvals() as data:
        data["approvals"] = [
            e for e in data.get("approvals", [])
            if not (
                isinstance(e, dict)
                and e.get("event") == event
                and e.get("command") == command
            )
        ] + [entry]


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def revoke(command: str) -> int:
    """Remove every allowlist entry matching ``command``.

    Returns the number of entries removed.  Does not unregister any
    callbacks that are already live on the plugin manager in the current
    process — restart the CLI / gateway to drop them.
    """
    with _locked_update_approvals() as data:
        before = len(data.get("approvals", []))
        data["approvals"] = [
            e for e in data.get("approvals", [])
            if not (isinstance(e, dict) and e.get("command") == command)
        ]
        after = len(data["approvals"])
    return before - after


_SCRIPT_EXTENSIONS: Tuple[str, ...] = (
    ".sh", ".bash", ".zsh", ".fish",
    ".py", ".pyw",
    ".rb", ".pl", ".lua",
    ".js", ".mjs", ".cjs", ".ts",
)


def _command_script_path(command: str) -> str:
    """Return the script path from ``command`` for doctor / drift checks.

    Prefers a token ending in a known script extension, then a token
    containing ``/`` or leading ``~``, then the first token.  Handles
    ``python3 /path/hook.py``, ``/usr/bin/env bash hook.sh``, and the
    common bare-path form.
    """
    try:
        from hermes_cli._subprocess_compat import split_command_line

        parts = split_command_line(command)
    except ValueError:
        return command
    if not parts:
        return command
    for part in parts:
        if part.lower().endswith(_SCRIPT_EXTENSIONS):
            return part
    for part in parts:
        if "/" in part or part.startswith("~"):
            return part
    return parts[0]


# ---------------------------------------------------------------------------
# Helpers for accept-hooks resolution
# ---------------------------------------------------------------------------

def _resolve_effective_accept(
    cfg: Dict[str, Any], accept_hooks_arg: bool,
) -> bool:
    """Combine all three opt-in channels into a single boolean.

    Precedence (any truthy source flips us on):
      1. ``--accept-hooks`` flag (CLI) / explicit argument
      2. ``HERMES_ACCEPT_HOOKS`` env var
      3. ``hooks_auto_accept: true`` in ``cli-config.yaml``
    """
    if accept_hooks_arg:
        return True
    env = os.environ.get("HERMES_ACCEPT_HOOKS", "").strip().lower()
    if env in {"1", "true", "yes", "on"}:
        return True
    cfg_val = cfg.get("hooks_auto_accept", False)
    if isinstance(cfg_val, bool):
        return cfg_val
    if isinstance(cfg_val, str):
        return cfg_val.strip().lower() in {"1", "true", "yes", "on"}
    return False


# ---------------------------------------------------------------------------
# Introspection (used by `hermes hooks` CLI)
# ---------------------------------------------------------------------------

def allowlist_entry_for(event: str, command: str) -> Optional[Dict[str, Any]]:
    """Return the allowlist record for this pair, if any."""
    for e in load_allowlist().get("approvals", []):
        if (
            isinstance(e, dict)
            and e.get("event") == event
            and e.get("command") == command
        ):
            return e
    return None


def script_mtime_iso(command: str) -> Optional[str]:
    """ISO-8601 mtime of the resolved script path, or ``None`` if the
    script is missing."""
    path = _command_script_path(command)
    if not path:
        return None
    try:
        expanded = os.path.expanduser(path)
        return datetime.fromtimestamp(
            os.path.getmtime(expanded), tz=timezone.utc,
        ).isoformat().replace("+00:00", "Z")
    except OSError:
        return None


def script_is_executable(command: str) -> bool:
    """Return ``True`` iff ``command`` is runnable as configured.

    For a bare invocation (``/path/hook.sh``) the script itself must be
    executable.  For interpreter-prefixed commands (``python3
    /path/hook.py``, ``/usr/bin/env bash hook.sh``) the script just has
    to be readable — the interpreter doesn't care about the ``X_OK``
    bit.  Mirrors what ``_spawn`` would actually do at runtime."""
    path = _command_script_path(command)
    if not path:
        return False
    expanded = os.path.expanduser(path)
    if not os.path.isfile(expanded):
        return False
    try:
        from hermes_cli._subprocess_compat import split_command_line

        argv = split_command_line(command)
    except ValueError:
        return False
    is_bare_invocation = bool(argv) and argv[0] == path
    required = os.X_OK if is_bare_invocation else os.R_OK
    return os.access(expanded, required)


def run_once(
    spec: ShellHookSpec, kwargs: Dict[str, Any],
) -> Dict[str, Any]:
    """Fire a single shell-hook invocation with a synthetic payload.
    Used by ``hermes hooks test`` and ``hermes hooks doctor``.

    ``kwargs`` is the same dict that :func:`hermes_cli.plugins.invoke_hook`
    would pass at runtime.  It is routed through :func:`_serialize_payload`
    so the synthetic stdin exactly matches what a real hook firing would
    produce — otherwise scripts tested via ``hermes hooks test`` could
    diverge silently from production behaviour.

    Returns the :func:`_spawn` diagnostic dict plus a ``parsed`` field
    holding the canonical Hermes-wire-shape response — including exit-code-2
    blocking and ``fail_closed`` semantics, so what ``hermes hooks test``
    prints is exactly what the dispatcher would receive."""
    stdin_json = _serialize_payload(spec.event, kwargs)
    result = _spawn(spec, stdin_json)
    result["parsed"] = _evaluate_result(spec, result)
    return result
