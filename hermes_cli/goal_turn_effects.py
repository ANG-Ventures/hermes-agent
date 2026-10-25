"""Turn-effect classifier for the /goal continuation gate.

A finished turn is ``write`` (at least one tool call with a side effect), ``read`` (tool calls, all
reads) or ``none`` (no tool calls: a NO_REPLY, a page acknowledged in chat). The goal loop re-fires
its continuation only after a ``write`` turn or a human message; a ``read``/``none`` turn leaves the
next nudge to the idle check-in (Ace ruling 2026-10-22, option b). Unknown tools and commands
classify as ``write``: a misclassified read costs one extra nudge (today's behaviour), a
misclassified write would stall the goal.

``TOOL_EFFECTS`` is the one table. A value is a class, or a callable on the parsed arguments for
tools whose verb decides (terminal command, cronjob action).
"""
from __future__ import annotations

import json
import re
import shlex
from typing import Any, Callable, Dict, Iterable, List, Optional, Union

TURN_EFFECT_WRITE = "write"
TURN_EFFECT_READ = "read"
TURN_EFFECT_NONE = "none"

_READ_COMMANDS = frozenset({
    "ls", "cat", "head", "tail", "less", "grep", "egrep", "rg", "wc", "echo", "printf", "pwd", "date",
    "awk", "jq", "yq", "sort", "uniq", "cut", "tr", "diff", "stat", "file", "which", "type", "ps",
    "pgrep", "sleep", "true", "test", "[", "env", "whoami", "hostname", "uptime", "df", "du", "tree",
    "basename", "dirname", "realpath", "readlink", "column", "nl", "comm", "md5", "shasum", "sha256sum",
    "lsof", "launchctl", "journalctl", "systemctl", "tmux", "cd",
})
# `launchctl`/`systemctl`/`tmux` only on their read verbs.
_VERB_READS = {
    "launchctl": frozenset({"list", "print", "blame", "print-disabled"}),
    "systemctl": frozenset({"status", "show", "is-active", "is-enabled", "list-units", "list-timers", "cat"}),
    "tmux": frozenset({"capture-pane", "list-sessions", "ls", "list-windows", "list-panes", "has-session"}),
}
_GIT_READS = frozenset({
    "status", "log", "diff", "show", "rev-parse", "ls-files", "ls-remote", "fetch", "reflog", "describe",
    "blame", "grep", "merge-base", "cat-file", "shortlog", "rev-list", "name-rev", "for-each-ref",
})
_GH_READ_VERBS = {
    "pr": frozenset({"view", "list", "checks", "status", "diff"}),
    "run": frozenset({"view", "list", "watch"}),
    "issue": frozenset({"view", "list", "status"}),
    "repo": frozenset({"view", "list"}),
    "release": frozenset({"view", "list"}),
    "workflow": frozenset({"view", "list"}),
    "auth": frozenset({"status"}),
}
_KANBAN_CLI_READS = frozenset({"show", "list", "ls", "tail", "log", "stats", "watch", "context", "runs"})
_SQL_WRITE = re.compile(r"\b(insert|update|delete|create|drop|alter|replace|vacuum|attach)\b", re.I)
# Redirection into a file (not 2>&1, not /dev/null).
_FILE_REDIRECT = re.compile(r"(?<![0-9&])>>?\s*(?!&|/dev/null)\S")
_SEGMENT_SPLIT = re.compile(r"\|\||&&|[;|\n]")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _gh_segment_is_read(words: List[str]) -> bool:
    if len(words) < 2:
        return False
    group = words[1]
    if group == "api":
        rest = words[2:]
        for i, w in enumerate(rest):
            if w in ("-f", "-F", "--field", "--raw-field", "--input"):
                return False
            if w in ("-X", "--method") and i + 1 < len(rest) and rest[i + 1].upper() != "GET":
                return False
            if w.startswith("--method=") and w.split("=", 1)[1].upper() != "GET":
                return False
        return True
    return len(words) >= 3 and words[2] in _GH_READ_VERBS.get(group, frozenset())


def _segment_is_read(segment: str) -> bool:
    try:
        words = shlex.split(segment, posix=True)
    except ValueError:
        return False
    while words and _ENV_ASSIGN.match(words[0]):
        words = words[1:]
    while words and words[0] in ("sudo", "timeout", "command", "nice"):
        words = words[1:]
        if words and re.fullmatch(r"[0-9.]+[smh]?", words[0]):  # timeout's duration
            words = words[1:]
    if not words:
        return True
    cmd = words[0].rsplit("/", 1)[-1]
    if cmd in _VERB_READS:
        return len(words) >= 2 and words[1] in _VERB_READS[cmd]
    if cmd in _READ_COMMANDS:
        return True
    if cmd == "sed":
        return not any(w == "-i" or w.startswith("-i") or w == "--in-place" for w in words[1:])
    if cmd == "find":
        return not any(w in ("-delete", "-exec", "-execdir", "-ok", "-fprint") for w in words[1:])
    if cmd == "git":
        rest = words[1:]
        while rest and rest[0] in ("-C", "-c", "--git-dir", "--work-tree"):
            rest = rest[2:]
        args = [w for w in rest if not w.startswith("-")]
        if not args:
            return False
        sub = args[0]
        if sub in _GIT_READS:
            return True
        # `git branch` / `git remote` / `git tag` with no positional arg, or `<x> list`, only list.
        return sub in ("branch", "remote", "tag", "worktree", "stash") and (
            (len(args) == 1 and sub in ("branch", "remote", "tag")) or (len(args) >= 2 and args[1] == "list")
        )
    if cmd == "gh":
        return _gh_segment_is_read(words)
    if cmd in ("hermes", "hermes"):
        rest = [w for w in words[1:] if not w.startswith("-")]
        if rest and rest[0] == "kanban":
            return len(rest) >= 2 and rest[1] in _KANBAN_CLI_READS
        return len(rest) >= 2 and rest[0] in ("cron", "status", "logs") and rest[1] in ("list", "status", "show")
    if cmd == "sqlite3":
        return not _SQL_WRITE.search(segment)
    if cmd == "curl":
        return not any(w in ("-X", "--request", "-d", "--data", "--data-raw", "--data-binary", "-F", "--form", "-T", "--upload-file", "-o", "--output", "-O") for w in words[1:])
    return False


def classify_terminal_command(command: Any) -> str:
    """``read`` when every pipeline/chain segment is a known read and nothing redirects into a file."""
    text = str(command or "")
    if not text.strip():
        return TURN_EFFECT_READ
    if _FILE_REDIRECT.search(text) or "<<" in text or "$(" in text or "`" in text:
        return TURN_EFFECT_WRITE
    segments = [s for s in _SEGMENT_SPLIT.split(text) if s.strip()]
    return TURN_EFFECT_READ if all(_segment_is_read(s) for s in segments) else TURN_EFFECT_WRITE


def _cronjob_effect(args: Dict[str, Any]) -> str:
    return TURN_EFFECT_READ if str(args.get("action") or "").lower() in ("list", "show", "get", "status") else TURN_EFFECT_WRITE


_R, _W = TURN_EFFECT_READ, TURN_EFFECT_WRITE
TOOL_EFFECTS: Dict[str, Union[str, Callable[[Dict[str, Any]], str]]] = {
    # board
    "kanban_show": _R, "kanban_list": _R, "kanban_attachments": _R, "kanban_heartbeat": _R,
    "kanban_create": _W, "kanban_comment": _W, "kanban_complete": _W, "kanban_block": _W,
    "kanban_unblock": _W, "kanban_request_changes": _W, "kanban_request_review": _W,
    "kanban_link": _W, "kanban_attach": _W, "kanban_attach_url": _W,
    # files
    "read_file": _R, "search_files": _R, "write_file": _W, "patch": _W,
    # shell / scheduling: the verb decides
    "terminal": lambda a: classify_terminal_command(a.get("command")),
    "read_terminal": _R,
    "cronjob": _cronjob_effect, "cronjob_manage": _cronjob_effect,
    # lookups
    "web_search": _R, "web_extract": _R, "x_search": _R, "session_search": _R, "skill_view": _R,
    "skills_list": _R, "vision_analyze": _R, "video_analyze": _R, "todo": _R,
    "browser_snapshot": _R, "browser_get_images": _R, "browser_vision": _R,
    "ha_get_state": _R, "ha_list_entities": _R, "ha_list_services": _R,
    "mem0_search": _R, "mem0_profile": _R,
    "lcm_grep": _R, "lcm_describe": _R, "lcm_expand": _R, "lcm_expand_query": _R,
    "lcm_load_session": _R, "lcm_status": _R, "lcm_doctor": _R,
    # outbound side effects
    "send_message": _W, "delegate_task": _W, "ha_call_service": _W,
}


def classify_tool_call(name: Any, arguments: Any) -> str:
    effect = TOOL_EFFECTS.get(str(name or ""))
    if effect is None:
        return TURN_EFFECT_WRITE
    if callable(effect):
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except ValueError:
                return TURN_EFFECT_WRITE
        return effect(arguments if isinstance(arguments, dict) else {})
    return effect


def _iter_tool_calls(messages: Iterable[Dict[str, Any]]):
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        for call in msg.get("tool_calls") or ():
            fn = call.get("function") if isinstance(call, dict) else None
            if isinstance(fn, dict):
                yield fn.get("name"), fn.get("arguments")
            elif isinstance(call, dict):
                yield call.get("name"), call.get("arguments") or call.get("input")


def classify_turn_effect(messages: Iterable[Dict[str, Any]]) -> str:
    """``write`` | ``read`` | ``none`` for one turn's messages (assistant tool calls only)."""
    seen = False
    for name, arguments in _iter_tool_calls(messages):
        seen = True
        if classify_tool_call(name, arguments) == TURN_EFFECT_WRITE:
            return TURN_EFFECT_WRITE
    return TURN_EFFECT_READ if seen else TURN_EFFECT_NONE


def turn_effect_for_result(agent_result: Dict[str, Any], messages: List[Dict[str, Any]]) -> Optional[str]:
    """The effect of the turn behind a gateway agent result, or None (unknown) when the run compacted
    the session: the compacted message list no longer marks where this turn began (a folded todo
    snapshot is a synthetic user row after retained tool calls), so earlier writes could be lost and
    a required continuation suppressed. Unknown is never held."""
    if agent_result.get("compacted_during_turn"):
        return None
    return classify_turn_effect(current_turn_messages(messages, agent_result.get("history_offset")))


def current_turn_messages(messages: List[Dict[str, Any]], history_offset: Optional[int]) -> List[Dict[str, Any]]:
    """This turn's slice of an agent result: past ``history_offset`` when it is usable, else after
    the last user message (compaction resets the offset to 0)."""
    msgs = list(messages or [])
    if history_offset and 0 < history_offset <= len(msgs):
        return msgs[history_offset:]
    for i in range(len(msgs) - 1, -1, -1):
        if isinstance(msgs[i], dict) and msgs[i].get("role") == "user":
            return msgs[i + 1:]
    return msgs
