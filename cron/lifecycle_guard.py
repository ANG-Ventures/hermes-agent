"""Gateway lifecycle guard for cron job creation (#30719).

An agent running inside a gateway can schedule a cron job that calls
``hermes gateway restart`` (or ``launchctl kickstart ai.hermes.gateway``
or ``systemctl restart hermes-gateway``).  When the cron fires, the
gateway dies, the supervisor (launchd KeepAlive / systemd Restart=)
revives it, auto-resume picks up the offending session, and the resumed
turn re-runs the same logic — a SIGTERM-respawn loop every ~10 seconds
until manually broken.

This module rejects cron job specs whose prompt or script contains a
direct shell-level gateway-lifecycle command.  It is enforced at
``cron.jobs.create_job`` so it fires on every job-creation path: the
``hermes cron create`` CLI subcommand AND the agent's ``cronjob`` model
tool (which calls ``create_job`` directly, bypassing the CLI layer).

The pattern is intentionally command-shaped: it anchors on a concrete
command identifier (``hermes gateway``, ``launchctl ... hermes-gateway``,
``systemctl ... hermes-gateway``, ``pkill`` against the gateway) so it
cannot fire on prose.  A cron ``prompt`` is fed to a future LLM, not a
shell, so an over-broad substring match on English ("Kong API gateway
autoscaling and restart behavior") would produce a high false-positive
rate without preventing the actual foot-gun, which requires a real
command shape.

This is a defence-in-depth layer.  ``tools/terminal_tool.py`` blocks direct
commands and shell scripts they reference when ``_HERMES_GATEWAY=1``. It also
rejects ``launchctl submit`` in gateway sessions because launchd treats that
primitive as a persistent KeepAlive job, not a one-shot task. ``hermes gateway
stop|restart|uninstall`` separately refuse to self-target from inside the gateway.
Blocking cron specs at creation time as well means the agent gets an immediate,
informative rejection instead of scheduling a job that will only fail
(silently) when it fires.

The profile-flag form (``hermes -p <profile> gateway restart|stop``, #78028)
is handled profile-aware: it is blocked only when the named profile is the
profile running the guard. Sibling-profile restarts are legitimate fleet
operations and stay allowed.
"""

from __future__ import annotations

import ast
import logging
import os
import re
import shlex
import stat
import sys
import threading
from pathlib import Path
from typing import Callable, Iterator, Optional

logger = logging.getLogger(__name__)


class GatewayLifecycleBlocked(ValueError):
    """Raised when a cron job spec contains a gateway-lifecycle command."""


# Stable marker attached to every terminal-tool result that was refused by
# THIS guard. It exists so a consumer can distinguish "the guard refused"
# from "the command ran and failed" WITHOUT string-matching prose that is
# free to change. The execute_code sandbox stub keys on it to raise instead
# of handing the caller a result that looks like an ordinary non-zero exit
# (a script that does not inspect the returned dict otherwise completes
# silently with rc 0, which reads as success — measured 2026-09-20).
GATEWAY_LIFECYCLE_BLOCK_MARKER = "gateway_lifecycle_guard"


# Shell-level command shapes that target the gateway lifecycle. Each branch
# is anchored on a concrete command identifier so a match can only fire on
# actual shell-command-shaped strings, not on prose.
_GATEWAY_LIFECYCLE_PATTERN = re.compile(
    r"(?i)"
    # Branch A: destructive `hermes gateway` operations.
    # The destructive operations are restart, stop, and uninstall.
    # `start` is intentionally excluded: starting a gateway from inside a
    # gateway is benign (a no-op or "already running" error), and a
    # legitimate cron job might start a sibling profile's gateway.
    # The lookbehind (#77173): `hermes` must not be a path component or a
    # word tail. Excluding `/`, word chars, `.` and `-` keeps file paths
    # with embedded spaces (`/docs/hermes gateway restart-notes.md`) from
    # matching via the `/hermes` tail, while every real command position
    # (start of text, whitespace, `;`/`&`/`|`, `$(`, backtick, even a
    # U+FFFD from binary-content decoding) still matches.
    # Windows spells the CLI with a launcher suffix (`hermes.exe`, npm-style `hermes.cmd`/`.ps1`);
    # same command, so the suffix is optional here.
    r"(?:(?<![/\w.\-])hermes(?:\.(?:exe|cmd|bat|com|ps1))?\s+gateway\s+(?:restart|stop|uninstall)\b)"
    # Branch B: launchctl ops on a hermes-gateway label. macOS launchd
    # labels look like `ai.hermes.gateway` / `hermes-gateway`. Requiring the
    # gateway identifier prevents blocking unrelated hermes services (e.g.
    # `launchctl unload ai.hermes.update-checker.plist`).
    # `submit` and `bootstrap` are included alongside the direct verbs
    # (kickstart/etc.): `launchctl submit -l ai.hermes.gateway-<suffix> --
    # <helper-script>` (or `launchctl bootstrap gui/<uid> <plist>`) creates
    # a NEW keepalive job wrapping an arbitrary helper, which is how a
    # blocked direct restart/kill gets laundered into a persistent restart
    # loop instead (#62891) — same foot-gun, indirect shape. Neutral-label
    # submissions that dodge this text anchor are caught separately by
    # `contains_launchctl_submit_command` (execution-aware, label-independent).
    # `bootout`/`remove`/`disable` sit alongside `unload`: Apple deprecated
    # load/unload in favour of bootstrap/bootout, so `bootout` is the modern
    # spelling of an already-listed verb, `remove` is its legacy sibling, and
    # `disable` is what makes an unload durable across boots. Omitting them
    # left the bypassable approval layer (tools/approval.py, skipped on
    # force=True) as the only cover, while this hard block — documented as
    # "force=True cannot help here" — let them through (#80260).
    r"|(?:launchctl\s+(?:kickstart|unload|load|stop|restart|submit|bootstrap|bootout|remove|disable)\b[^\n]*\bhermes[.\-]?gateway)"
    # Branch C: systemctl ops on a hermes-gateway unit.
    r"|(?:systemctl\s+(?:-\S+\s+)*(?:restart|stop|start)\b[^\n]*\bhermes[.\-]?gateway)"
    # Branch D: pkill / kill targeting the hermes gateway process. Both
    # token orders because real reproductions show both.
    #   - LEADING \b on the command so `skill`/`skills` can't match the bare
    #     `kill` substring (the false-positive that blocked read-only commands
    #     mentioning skill paths: `skills-...safe-gateway...hermes-harness`).
    #   - the gap between kill and its target tokens is bounded to a single
    #     shell segment ([^\n;|&]*) so it can't greedily span across `;`/`&&`
    #     into unrelated `hermes`/`gateway` path tokens later on the line.
    #   - `taskkill` / `Stop-Process` are the Windows spellings of the same operation;
    #     `\bp?kill\b` cannot reach inside `taskkill`, so they are named outright.
    #     Service-control forms (`net stop`, `sc stop`) presuppose a service install
    #     this guard has no evidence of and stay uncovered.
    r"|(?:\b(?:pkill|kill|taskkill|stop-process)\b[^\n;|&]*\bhermes\b[^\n;|&]*\bgateway)"
    r"|(?:\b(?:pkill|kill|taskkill|stop-process)\b[^\n;|&]*\bgateway\b[^\n;|&]*\bhermes)"
)


# A lifecycle command wrapped in an ssh invocation targets a *remote* host's
# gateway, so the local foot-gun rationale does not apply: the command runs
# under the remote sshd, the local gateway is never SIGTERMed, and no
# supervisor respawn loop can form on this machine.  Fleet maintenance
# (restarting a sibling machine's gateway over ssh) is a legitimate, common
# operation and must not be blocked.
#
# Loopback targets (``ssh localhost ...``) are still blocked: the *effect*
# (this host's gateway dying, on a schedule for the cron path) can still
# produce the #30719 respawn loop even though the ssh client itself would
# survive.  We cannot resolve arbitrary hostnames in a text guard, so an ssh
# to this machine's own LAN hostname is an accepted residual gap.
# Branch E: process killers whose TARGET is the interpreter image hosting the gateway. A supervised
# gateway is literally `python.exe` / `python3.12` (`python -m hermes_cli.main gateway run`), so
# `taskkill /F /IM python.exe`, `pkill -9 python3` or `killall python` carry no hermes/gateway token
# yet terminate it (#113667). Token-aware rather than a line regex: option VALUES are never read as
# targets (`pkill -u <user> chrome`), `-f` cmdline patterns are judged as patterns, and other image
# names (`taskkill /F /IM agent-browser.exe`) stay available. Numeric-PID kills are out of scope:
# the explicit PID / `proc_*` id IS the ownership-scoped route the rejection points to.
_INTERPRETER_IMAGE_RE = re.compile(r"^pythonw?(?:\d+(?:\.\d+)*)?(?:\.exe)?$")
_HOST_INTERPRETER_NAME = Path(sys.executable).name.lower() if sys.executable else ""
_KILLER_VALUE_OPTIONS = {
    # procps pkill/pgrep options that consume the next token, so the value is never the pattern.
    "pkill": frozenset({
        "-g", "--pgroup", "-G", "--group", "-P", "--parent", "-s", "--session", "-t", "--terminal",
        "-u", "--euid", "-U", "--uid", "-F", "--pidfile", "--ns", "--nslist", "-d", "--delimiter",
        "--signal", "-O", "--older", "-r", "--runstates", "--cgroup", "-A", "--ignore-ancestors",
    }),
    "killall": frozenset({"-s", "--signal", "-u", "--user", "-o", "--older-than", "-y", "--younger-than",
                          "-n", "--ns", "-Z", "--context"}),
}
_KILLER_VALUE_OPTIONS["pgrep"] = _KILLER_VALUE_OPTIONS["pkill"]
_KILLER_VALUE_OPTIONS["pidof"] = frozenset({"-o", "--omit-pid", "-S", "--separator"})
# Regex metacharacters a pkill/pgrep ERE may carry around the interpreter name (`^python3?$`, `python.*`).
_ERE_TAIL = re.compile(r"[.*+?\\\[\](){}|].*$")
_ERE_WILDCARD_ONLY = re.compile(r"^[.*+?$\s]*$")
_NAME_KILLERS = frozenset({"pkill", "killall", "taskkill", "stop-process"})
_NAME_ENUMERATORS = frozenset({"pgrep", "pidof", "get-process"})
_KILL_VERB_RE = re.compile(r"(?i)\b(?:kill|taskkill|stop-process)\b")
# A `-f` pattern that does not start with the interpreter reaches the gateway cmdline
# (`python -m hermes_cli.main gateway run` / `hermes gateway run`) only through its own tokens;
# an unrelated script that merely contains "hermes" (`hermes-polis/run.sh`, `my_hermes_bot.py`)
# cannot match it. Same hermes+gateway pairing as Branch D, plus the module path.
_GATEWAY_CMDLINE_TOKEN_RE = re.compile(r"(?i)hermes_cli|\bhermes\b[^\n]*\bgateway\b|\bgateway\b[^\n]*\bhermes\b")
# Rejection text for Branch E, shared by every tool surface that runs the guard so the agent is
# pointed at the ownership-scoped route (proc_* id / explicit PID) rather than the shell.
HOST_INTERPRETER_KILL_REJECTION = (
    "Blocked: this command kills every process whose image/name matches the Python "
    "interpreter, which is the process hosting this gateway (and this command). "
    "Stop only the process you own instead: process(action=\"kill\", session_id=\"proc_…\") "
    "for a background job Hermes started, or kill/taskkill by its explicit PID."
)


def _is_interpreter_image(value: str, *, substring: bool = False) -> bool:
    """True when *value* (a process/image name, `*`-wildcard allowed) denotes the Python interpreter
    that hosts the gateway. *substring*: pkill/pgrep match an ERE anywhere in the name, so `py`
    reaches `python3` too."""
    name = value.strip().strip("\"'").lower().removesuffix(".exe")
    if not name:
        return False
    if name.endswith("*"):
        prefix = name.rstrip("*")
        return "python".startswith(prefix) or _HOST_INTERPRETER_NAME.startswith(prefix)
    if _INTERPRETER_IMAGE_RE.match(name) or name == _HOST_INTERPRETER_NAME.removesuffix(".exe"):
        return True
    return substring and len(name) >= 2 and "python".startswith(name)


def _pattern_reaches_host_interpreter(pattern: str, *, full_cmdline: bool, exact: bool) -> bool:
    """pkill/pgrep/killall operand semantics: an ERE against the process NAME (or, with `-f`, the full
    command line). `python -m hermes_cli.main …` is the gateway's own cmdline, so a `-f` pattern
    that names the interpreter and then only wildcards or a `hermes` token reaches it, while
    `python mt_add.py` (a specific script) does not."""
    core = pattern.strip().strip("\"'").lstrip("^")
    if core.endswith("$"):
        core = core[:-1]
    head, _, rest = core.partition(" ") if full_cmdline else (core, "", "")
    # A literal interpreter name first (`python3.12`: the dot is a version separator, not an ERE
    # wildcard); only then read the head as an ERE with a metacharacter tail (`python3?`, `python.*`).
    match = None if _is_interpreter_image(head, substring=not exact) else _ERE_TAIL.search(head)
    if match:
        rest = head[match.start():] + " " + rest
        head = head[: match.start()]
    if not _is_interpreter_image(head, substring=not exact):
        return full_cmdline and bool(_GATEWAY_CMDLINE_TOKEN_RE.search(core))
    return not rest.strip() or bool(_ERE_WILDCARD_ONLY.match(rest)) or "hermes" in rest.lower()


def _killer_targets_host_interpreter(name: str, args: list[str]) -> bool:
    """Whether killer/enumerator *name* with argv *args* would select the host interpreter."""
    if name in ("pkill", "pgrep", "killall", "pidof"):
        # pkill/pgrep: ERE anywhere in the name unless -x; killall: exact name unless -r; pidof: exact.
        exact = name == "pidof" or (name == "killall") != any(t in ("-r", "--regexp", "-x", "--exact") for t in args)
        full_cmdline = name in ("pkill", "pgrep") and any(t in ("-f", "--full") for t in args)
        operands: list[str] = []
        position = 0
        while position < len(args):
            token = args[position]
            if token == "--":
                operands += args[position + 1:]
                break
            if token in _KILLER_VALUE_OPTIONS[name]:
                position += 2
                continue
            if not token.startswith("-"):
                operands.append(token)
            position += 1
        return any(_pattern_reaches_host_interpreter(op, full_cmdline=full_cmdline, exact=exact) for op in operands)
    if name == "taskkill":
        for position, token in enumerate(args[:-1]):
            option = token.lower().lstrip("/-")
            value = args[position + 1]
            if option == "im" and _is_interpreter_image(value):
                return True
            if option == "fi":
                filter_match = re.match(r"(?i)\s*['\"]?imagename\s+eq\s+(\S+)", value)
                if filter_match and _is_interpreter_image(filter_match.group(1)):
                    return True
        return False
    # Stop-Process / Get-Process: `-Name`/`-ProcessName` (also `-Name:value`), comma lists, positional
    # names for Get-Process.
    values: list[str] = []
    for position, token in enumerate(args):
        option, _, inline_value = token.partition(":")
        if option.lower() in ("-name", "-processname", "-n"):
            values.append(inline_value if inline_value else (args[position + 1] if position + 1 < len(args) else ""))
        elif name == "get-process" and not token.startswith("-") and (position == 0 or not args[position - 1].startswith("-")):
            values.append(token)
    return any(_is_interpreter_image(part) for value in values for part in value.split(","))


def _segment_names_host_interpreter(tokens: list[str], killers: frozenset[str]) -> bool:
    """Whether a tokenized segment runs one of *killers* against the host interpreter. The
    executable is read at the wrapper-peeled position first, then at the first killer token anywhere
    in the segment (`xargs kill`, Python argv lists)."""
    index = _executed_command_index(tokens)
    candidates = [index] if index is not None else []
    candidates += [i for i, token in enumerate(tokens) if _executable_name(token).lower().removesuffix(".exe") in killers]
    for position in candidates:
        name = _executable_name(tokens[position]).lower().removesuffix(".exe")
        if name in killers and _killer_targets_host_interpreter(name, tokens[position + 1:]):
            return True
    return False


def contains_host_interpreter_kill(text: str) -> bool:
    """Branch E entrypoint: a process killer aimed at the interpreter image hosting the gateway, or a
    name-derived PID feed into one (`pgrep python | xargs kill`, `kill $(pidof python3)`,
    `Get-Process python | Stop-Process`). Segment-tokenized, so quoted/spliced spellings and Python
    argv lists resolve the same way the shell resolves them."""
    normalized = _SHELL_LINE_CONTINUATION.sub(" ", text)
    kill_verb_present = bool(_KILL_VERB_RE.search(normalized))
    for segment in _iter_command_segments(normalized):
        joined = " ".join(segment)
        stripped = _ARGV_LIST_PUNCTUATION.sub(" ", joined)
        # Python argv lists (`subprocess.run(["taskkill", "/IM", "python.exe"])`) re-split only when
        # list punctuation was present, so a quoted `-f 'python my_script.py'` stays one operand.
        for tokens in ([segment, stripped.split()] if stripped != joined else [segment]):
            if _segment_names_host_interpreter(tokens, _NAME_KILLERS):
                return True
            if kill_verb_present and _segment_names_host_interpreter(tokens, _NAME_ENUMERATORS):
                return True
    return False


_SSH_COMMAND_RE = re.compile(r"(?i)(?:^|\s)(?:/\S*/)?(?:ssh|autossh)\s")
_LOOPBACK_HOST_RE = re.compile(
    r"(?i)(?:^|[\s@\[:])(?:localhost|(?:::ffff:)?127\.\d{1,3}\.\d{1,3}\.\d{1,3}|::1|0\.0\.0\.0)\b"
)

# Rough shell-segment separators.  This is a heuristic split (it does not
# honour quoting), which errs on the side of BLOCKING: a separator inside an
# ssh remote-command string starts a "new segment" that no longer contains
# ``ssh``, so such a match falls back to blocked rather than allowed.
_SEGMENT_SPLIT_RE = re.compile(r"(?:\|\||&&|;|\||&|\$\(|`)")

# Command substitution INSIDE a double-quoted region still executes, so a
# lifecycle verb wrapped in `$(…)` or backticks is an invocation, not data.
_COMMAND_SUBSTITUTION_RE = re.compile(r"\$\(|`")


def _segment_is_ssh_remote(segment: str) -> bool:
    """True if *segment* (the shell segment leading up to a lifecycle match)
    is an ssh invocation targeting a non-loopback host."""
    if not _SSH_COMMAND_RE.search(segment):
        return False
    if _LOOPBACK_HOST_RE.search(segment):
        return False
    return True


def _open_quote_start(s: str) -> Optional[int]:
    """Index of the quote char that opens the region still OPEN at the end
    of *s*, or None when all quotes are balanced. Same scan rules as
    ``_open_quote_at``: inside an active region the other quote char is
    literal."""
    active: Optional[str] = None
    start: Optional[int] = None
    for i, ch in enumerate(s):
        if active is None:
            if ch in _OPENING_QUOTES:
                active, start = ch, i
        elif ch == active:
            active, start = None, None
    return start


def _match_is_ssh_remote(text: str, match_start: int) -> bool:
    """Return True if the lifecycle match at *match_start* sits inside an
    ssh invocation targeting a non-loopback host.

    Two shapes count. (1) The match's own shell segment starts with ssh
    (``ssh ace-ai 'systemctl restart hermes-gateway'``). (2) The match is
    INSIDE a quoted remote-command string that an ssh invocation opened
    earlier — on a previous line, or before a ``;`` in the same string
    (``ssh ace-ai 'cd ~/.hermes<NL>systemctl --user restart hermes-gateway'``).
    The local shell hands the whole quoted string to ssh, so a newline or
    separator inside it never starts a new LOCAL segment; the old line-scoped
    scan mis-read exactly that as a local restart and blocked a legitimate
    sibling-host fleet op (2026-09-25, ACE-AI's Agora unit, which happens to
    share the ``hermes-gateway`` unit name with this host's).

    Fail-closed in shape (2): a double-quoted region containing ``$(`` or a
    backtick before the match executes that substitution LOCALLY before ssh
    runs, so it is not exempted.
    """
    line_start = text.rfind("\n", 0, match_start) + 1
    prefix = text[line_start:match_start]
    # The command context for the match is the last shell segment before it.
    segment = _SEGMENT_SPLIT_RE.split(prefix)[-1]
    if _segment_is_ssh_remote(segment):
        return True
    # Shape (2): inside a quote region opened by an earlier ssh invocation.
    before = text[:match_start]
    qstart = _open_quote_start(before)
    if qstart is None:
        return False
    quoted = before[qstart + 1:]
    if before[qstart] == '"' and _COMMAND_SUBSTITUTION_RE.search(quoted):
        return False
    opener_line_start = before.rfind("\n", 0, qstart) + 1
    opener_segment = _SEGMENT_SPLIT_RE.split(before[opener_line_start:qstart])[-1]
    return _segment_is_ssh_remote(opener_segment)


# Text-only consumer commands: when a lifecycle phrase appears as a QUOTED
# ARGUMENT to one of these, it is DATA being printed/searched/read, not a
# gateway command being executed — so it cannot SIGTERM this process.
# Deliberately EXCLUDES shell interpreters (bash/sh/zsh/dash/eval/xargs/env
# etc.): `bash -c "hermes gateway restart"` re-executes the phrase and MUST
# stay blocked. Anchored at the start of the segment (optional leading path).
_TEXT_CONSUMER_RE = re.compile(
    r"(?i)(?:^|\s)(?:/\S*/)?(?:echo|printf|grep|egrep|fgrep|rg|cat|head|tail|"
    r"less|more|comm|diff|sed\s+-n|awk|jq|tee|column|sort|uniq|wc|"
    # Message-carrying VCS verbs. A commit/tag/stash message that merely
    # *documents* the lifecycle command (e.g. a fix whose commit body says
    # "run `hermes gateway restart` from a separate shell") is data, not an
    # invocation — the shell never executes the message text. Both conditions
    # in `_match_is_quoted_data` still apply, so this stays fail-closed:
    # `git commit -m "msg" && hermes gateway restart` keeps the lifecycle verb
    # OUTSIDE any open quote region and remains BLOCKED.
    r"git\s+(?:commit|tag|stash|notes|revert|merge|cherry-pick)"
    r")\b"
)

# Shell quote chars that open a data region. A `'` or `"` region makes the
# OTHER quote char (and backticks) literal until it closes — so we track the
# active region with a left-to-right scan rather than naive per-char counting
# (which mis-reads a backtick nested inside a single-quoted string).
_OPENING_QUOTES = ("'", '"')


def _open_quote_at(s: str) -> Optional[str]:
    """Left-to-right scan of *s*; return the quote char still OPEN at the end
    of the string, or None if all quotes are balanced. Inside an active
    single/double quote region the other quote char is literal."""
    active: Optional[str] = None
    for ch in s:
        if active is None:
            if ch in _OPENING_QUOTES:
                active = ch
        elif ch == active:
            active = None
    return active


def _match_is_quoted_data(text: str, match_start: int, match_str: str) -> bool:
    """Return True if the Branch-A `hermes gateway restart|stop` match at
    *match_start* is a QUOTED DATA argument to a text-only consumer command
    (echo/grep/printf/…), rather than an executed gateway command.

    Two conditions BOTH required (fail-closed — any doubt → not-data → blocked):
      1. The match sits inside an open single/double quote region (a proper
         left-to-right scan, so a backtick or the other quote nested inside is
         treated as literal), and that region closes after the match.
      2. The enclosing shell segment's leading command is a text-only consumer
         and NOT a shell interpreter (bash -c "…" stays blocked).

    The quote scan runs over the WHOLE text, not the match's line. A shell
    quote region spans newlines (`git commit -m "line1<NL><NL>line3"`), so a
    line-scoped scan sees no open quote on line 3 and mis-reads genuine quoted
    data as an executed command. That produced a real false positive: a commit
    whose message documented the lifecycle command was blocked (#papercut
    2026-08-16). Scanning the whole text mirrors what the shell actually does.

    Only applied to Branch A. The launchctl/systemctl/pkill branches are not
    exempted here — their command identifiers are distinctive enough that a
    quoted-data occurrence is vanishingly rare and not worth the bypass risk.
    """
    prefix = text[:match_start]
    suffix = text[match_start + len(match_str):]

    # Condition 0 (parity merge 2026-08-29): a text consumer whose output is
    # PIPED INTO A SHELL executes the very lines it matched --
    # `grep -r 'hermes gateway restart' . | sh` runs the command. Upstream's
    # data-sink masker has always fail-closed on this shape via
    # `_PIPE_TO_INTERPRETER`; the fork's quoted-data exemption predates that
    # masker and had no equivalent guard, so unioning the two exemptions
    # without this check let the pipe-to-shell form through. Scoped to the
    # match's own logical line so an unrelated pipe elsewhere in a multi-line
    # payload cannot revive the false positive this exemption exists to fix.
    _line_start = text.rfind("\n", 0, match_start) + 1
    _line_end = text.find("\n", match_start)
    _line = text[_line_start:] if _line_end == -1 else text[_line_start:_line_end]
    if _PIPE_TO_INTERPRETER.search(_line):
        return False

    # Condition 1: a single/double quote region is OPEN at the match, and it
    # closes somewhere in the suffix (data is bounded, not a trailing dangle).
    open_q = _open_quote_at(prefix)
    if open_q is None or open_q not in suffix:
        return False

    # Condition 1b: no COMMAND SUBSTITUTION between the opening quote and the
    # match. Inside a double-quoted region `$(…)` still executes, so
    # `git commit -m "$(hermes gateway restart)"` is an invocation wearing a
    # message's clothes. Single quotes suppress substitution, so only `"` needs
    # the check.
    #
    # BACKTICKS ARE DELIBERATELY NOT TREATED AS SUBSTITUTION HERE, and that is
    # a measured tradeoff rather than an oversight. Measured 2026-08-16, these
    # two are textually IDENTICAL by every local signal — same backtick count
    # before the match (1) and after it (1):
    #     prose : git commit -m "Run `hermes gateway restart` from a shell."
    #     subst : git commit -m "`hermes gateway restart`"
    # No parity or counting rule can separate them without a real shell parse.
    # Blocking both resurrects the false positive this fix exists to remove
    # (documenting a lifecycle command in a commit message is extremely common;
    # wrapping one in backticks *inside a commit message* to execute it is not
    # a way anyone actually restarts a gateway — and `$(…)`, the form someone
    # would reach for, IS blocked). The residual risk is bounded: reaching this
    # branch already requires the segment command to be a text-only consumer
    # from the allowlist, so a bare `` `cmd` `` at a shell prompt is unaffected.
    quoted_region = prefix[prefix.rfind(open_q) + 1:]
    if open_q == '"' and "$(" in quoted_region:
        return False

    # Condition 2: the segment's command is a text-only consumer, not an
    # interpreter. Split on shell separators OUTSIDE quotes isn't worth the
    # complexity here — the prefix up to the match is within one quoted arg, so
    # take the segment before the opening quote and check its leading command.
    seg_prefix = prefix[: prefix.rfind(open_q)]
    segment = _SEGMENT_SPLIT_RE.split(seg_prefix)[-1]
    return bool(_TEXT_CONSUMER_RE.search(segment))
# A backslash immediately followed by a newline is a POSIX shell line
# continuation — the shell joins the two lines before parsing. Every branch
# above uses `[^\n]*` between its verb and the gateway identifier so the
# match can't span unrelated lines of a longer cron prompt/script, but that
# also means a real multi-line shell invocation split across continuation
# lines (e.g. `launchctl submit \` / `  -l ai.hermes.gateway-... \` / `  -- ...`,
# the exact reported shape in #62891) would otherwise slip past. Collapse
# continuations to a single space before matching, mirroring what the shell
# itself does, rather than loosening `[^\n]*` and risking false positives
# across genuinely separate lines.
_SHELL_LINE_CONTINUATION = re.compile(r"\\\r?\n[ \t]*")

# Python argv-list punctuation (#68289): `subprocess.run(["launchctl",
# "bootout", ...])` separates the words the OS will exec with brackets and
# commas rather than spaces. Stripped before the token-join re-scan only —
# never from the raw text, so prose stays governed by the primary pattern.
_ARGV_LIST_PUNCTUATION = re.compile(r"[\[\],]+")


# Branch A2 (#78028): the same foot-gun written with an explicit profile
# selector — `hermes -p <profile> gateway restart|stop` / `--profile <name>`
# / `--profile=<name>`. The selector token between `hermes` and `gateway`
# breaks Branch A's literal adjacency. Unlike Branch A this form is NOT
# unconditionally self-targeting: issued from inside gateway `zeus`,
# `hermes -p venus gateway restart` operates on a sibling profile's gateway
# and is a legitimate fleet operation. The pattern captures the named
# profile so `contains_gateway_lifecycle_command` can block only the
# self-targeting shape (named profile == the profile running the guard).
# `start` stays excluded for the same reason as Branch A.
_PROFILE_FLAG_LIFECYCLE_PATTERN = re.compile(
    r"(?i)"
    r"hermes\s+"
    # Each flag has one parse: '-' plus its remainder, never two ways to split
    # '--'. A following flag cannot also be an optional value (#129281).
    # Possessive token/space runs avoid repartitioning whitespace on failure;
    # whole flag groups can still backtrack to expose the profile selector.
    # Do not cap the number of flags: that would silently allow long self-stops.
    r"(?:-\S++(?:\s++(?!-\S)\S++)?\s++)*"
    # The selector itself: `--profile=<name>` or the space-separated
    # `-p <name>` / `--profile <name>` — exactly the shapes the CLI's
    # `_apply_profile_override` accepts.
    r"(?:--profile=([^\s]+)|(?:-p|--profile)\s+([^\s]+))"
    # Any global flags between the selector and the subcommand.
    r"(?:\s++-\S++(?:\s++(?!-\S)\S++)?)*"
    r"\s+gateway\s+(?:restart|stop)"
)


def _current_profile_name() -> Optional[str]:
    """Profile running the guard (``hermes_cli.profiles.current_profile_name``); ``None`` if none."""
    from hermes_cli.profiles import current_profile_name

    return current_profile_name()


def _named_profile_is_current(named: str) -> bool:
    """True when *named* is the profile executing the guard (self-targeting)."""
    current = _current_profile_name()
    if not current:
        # No profile identity available: cannot prove self-targeting, so do
        # not block — sibling restarts must stay allowed (#78028).
        return False
    return named.strip().casefold() == current.strip().casefold()


# Branch B only catches `launchctl <verb> ... hermes[.-]?gateway` when the
# label literally appears AFTER the verb in the same `[^\n]*` span, and its
# verb list is missing `bootout`/`kill`/`disable`/`remove` entirely (2026-08-02
# incident). `bootout` is the one that actually unloads a job's registration
# — worse than `stop`/`kickstart`, which just bounce a still-registered job.
#
# A shell loop that builds the label from a list defined EARLIER in the same
# command — `for item in 'ai.hermes.gateway-apollo:...' 'ai.hermes.gateway:...';
# do label=${item%%:*}; launchctl bootout "gui/$uid/$label"; done` — puts the
# literal label text in a different `;`-separated segment than the verb, so
# no amount of same-segment tokenization sees it: the token next to `bootout`
# is the unexpanded variable `$label`, not the string "hermes.gateway". This
# incident command evaded Branch B on both counts (missing verb AND order)
# and unloaded all 4 profiles' launchd jobs with zero approval.
#
# Unlike `submit`/`bootstrap` (handled separately, fully label-independent,
# because a NEW job's label is attacker-chosen), these verbs act on an
# EXISTING job, so anchoring to the hermes-gateway label is still correct —
# `test_safe_commands` requires unrelated-label ops (e.g. `launchctl unload
# ai.hermes.update-checker.plist`) to stay unblocked. The fix is checking
# "verb anywhere AND label anywhere", not "label right after verb".
_LAUNCHCTL_LIFECYCLE_VERBS_RE = re.compile(
    r"(?i)\blaunchctl\s+(?:kickstart|unload|load|stop|restart|bootout|kill|disable|remove)\b"
)
_HERMES_GATEWAY_LABEL_RE = re.compile(r"(?i)\bhermes[.\-]?gateway\b")


def _contains_launchctl_gateway_lifecycle(normalized_text: str) -> bool:
    """Order-independent companion to Branch B — see comment above."""
    return bool(_LAUNCHCTL_LIFECYCLE_VERBS_RE.search(normalized_text)) and bool(
        _HERMES_GATEWAY_LABEL_RE.search(normalized_text)
    )


# ---------------------------------------------------------------------------
# Self-awareness for the launchd/systemd branches (2026-09-19)
# ---------------------------------------------------------------------------
#
# `_HERMES_GATEWAY_LABEL_RE` above is LABEL-BLIND: it matches *any* hermes
# gateway label, so a supervised gateway was blocked from running
# `launchctl bootout gui/501/ai.hermes.gateway` even when that label belongs
# to a DIFFERENT profile's gateway. The #30719 respawn loop this guard exists
# to prevent needs the command to kill THIS process — a sibling profile's
# launchd job cannot do that, and recovering a wedged sibling is precisely the
# break-glass job of a dedicated recovery profile (the 2026-09-19 state.db
# corruption recovery had to be laundered through `ssh localhost 'nohup bash
# script &'` because of this).
#
# The discrimination mirrors `_named_profile_is_current` (#78028), which
# already does exactly this for Branch A's `hermes -p <profile> gateway
# restart` form: block only when the named target IS us.
#
# Anchored, FULL-label patterns — deliberately stricter than
# `_HERMES_GATEWAY_LABEL_RE`, which matches the bare substring `hermes.gateway`
# anywhere. Sibling discrimination requires an explicit, complete service
# identifier; anything less stays blocked by the label-blind path.
_LAUNCHD_GATEWAY_LABEL_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_.\-])ai\.hermes\.gateway(?:-[A-Za-z0-9_-]+)?"
)
# systemd units are named by `hermes_cli.gateway.get_service_name()`:
# `hermes-gateway` for the default root, `hermes-gateway-<profile>` for
# `<root>/profiles/<profile>` (plus a short-hash suffix for arbitrary homes).
_SYSTEMD_GATEWAY_UNIT_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_.\-])hermes-gateway(?:-[A-Za-z0-9_-]+)?(?:\.service)?"
)

# An unexpanded shell variable / command substitution can expand to OUR OWN
# label at runtime, so a sibling-only verdict is unsound whenever one sits in
# the same command segment as a lifecycle verb.
_UNEXPANDED_SHELL_VALUE_RE = re.compile(r"\$\{?\w|\$\(|`|\$\{")
_LIFECYCLE_TOOL_RE = re.compile(r"(?i)\b(?:launchctl|systemctl)\b")
_SIBLING_SEGMENT_SPLIT_RE = re.compile(r"\|\||&&|;|\||&|\n")
_LAUNCHCTL_SUBMIT_MATCH_RE = re.compile(r"(?i)^\s*launchctl\s+submit\b")


def _normalize_service_name(token: str) -> str:
    """Canonicalize a launchd label / systemd unit token for comparison."""
    name = token.strip().strip("\"'").casefold()
    if name.endswith(".plist"):
        name = name[: -len(".plist")]
    if name.endswith(".service"):
        name = name[: -len(".service")]
    return name


def _systemd_self_unit() -> Optional[str]:
    """Return this process's own hermes-gateway systemd unit, if any.

    ``INVOCATION_ID`` is set by systemd for every service it starts, but it
    does not carry the unit NAME, so the name is read from the process's own
    cgroup path. Returns ``None`` off systemd (e.g. macOS/launchd) or when the
    owning unit is not a hermes gateway.
    """
    if not (os.environ.get("INVOCATION_ID") or "").strip():
        return None
    try:
        with open("/proc/self/cgroup", "r", encoding="utf-8", errors="replace") as fh:
            cgroup = fh.read()
    except (OSError, ValueError):
        return None
    match = _SYSTEMD_GATEWAY_UNIT_RE.search(cgroup)
    if match is None:
        return None
    return _normalize_service_name(match.group(0))


def _profile_derived_self_names() -> set[str]:
    """Self service names derived from this process's HERMES_HOME.

    Only trusted when HERMES_HOME is the default root or a named profile under
    it. An arbitrary HERMES_HOME (test sandbox, ad-hoc path) makes
    ``get_service_name()`` fall back to a short *hash* suffix, which is a
    fabricated identity rather than a real installed service — returning it
    would make the genuinely-installed ``ai.hermes.gateway`` look like a
    sibling. Fail closed (empty set) in that case.
    """
    try:
        from hermes_cli.gateway import (
            _home_owns_bare_service_name,
            _profile_name_from_home,
            get_hermes_home,
            get_launchd_label,
            get_service_name,
        )
        from hermes_constants import get_default_hermes_root

        home = Path(str(get_hermes_home())).resolve()
        default = Path(str(get_default_hermes_root())).resolve()
        # Same basis `_profile_suffix` uses: anything else gets the hash
        # suffix, i.e. a fabricated identity — fail closed.
        if not (
            _home_owns_bare_service_name(home)
            or _profile_name_from_home(home, default)
        ):
            return set()
        names: set[str] = set()
        label = get_launchd_label()
        if label:
            names.add(_normalize_service_name(label))
        unit = get_service_name()
        if unit:
            names.add(_normalize_service_name(unit))
        return names
    except Exception:
        return set()


def _self_gateway_service_names() -> set[str]:
    """Return the launchd labels / systemd units that mean THIS gateway.

    Sources, most authoritative first:

    * ``XPC_SERVICE_NAME`` — launchd tells a supervised job its own label
      verbatim, so when it names a hermes gateway it is definitive and the
      profile-derived launchd label is not consulted.
    * the systemd unit owning this process (``INVOCATION_ID`` + cgroup).
    * the profile-derived names for the current ``HERMES_HOME``.

    An EMPTY set means "identity undeterminable" and every caller must then
    fail closed (keep blocking) — a sibling verdict we cannot justify is worse
    than a false block.

    Note the self *watchdog* job (``ai.hermes.gateway-watchdog`` and profile
    equivalents) is deliberately NOT self: stopping the watchdog does not kill
    the gateway, so it is a legitimate sibling target.
    """
    names: set[str] = set()

    launchd_from_env: Optional[str] = None
    xpc = (os.environ.get("XPC_SERVICE_NAME") or "").strip()
    if xpc:
        candidate = _normalize_service_name(xpc)
        if _LAUNCHD_GATEWAY_LABEL_RE.fullmatch(candidate):
            launchd_from_env = candidate
            names.add(candidate)

    systemd_from_env = _systemd_self_unit()
    if systemd_from_env:
        names.add(systemd_from_env)

    for derived in _profile_derived_self_names():
        is_launchd = derived.startswith("ai.hermes.gateway")
        if is_launchd and launchd_from_env is not None:
            continue
        if not is_launchd and systemd_from_env is not None:
            continue
        names.add(derived)
    return names


def describe_self_gateway_identity() -> str:
    """Human-readable description of this gateway's own service identity."""
    names = sorted(_self_gateway_service_names())
    if not names:
        return ""
    launchd = [n for n in names if n.startswith("ai.hermes.gateway")]
    systemd = [n for n in names if not n.startswith("ai.hermes.gateway")]
    parts: list[str] = []
    if launchd:
        parts.append("launchd job " + ", ".join(launchd))
    if systemd:
        parts.append("systemd unit " + ", ".join(f"{n}.service" for n in systemd))
    return "this gateway runs as " + " / ".join(parts)


def _explicit_gateway_service_names(text: str) -> set[str]:
    """Every FULL gateway label / unit name written literally in *text*."""
    found: set[str] = set()
    for pattern in (_LAUNCHD_GATEWAY_LABEL_RE, _SYSTEMD_GATEWAY_UNIT_RE):
        for match in pattern.finditer(text):
            found.add(_normalize_service_name(match.group(0)))
    return found


def _lifecycle_verb_segment_has_unexpanded_value(text: str) -> bool:
    """True when a shell variable/substitution shares a segment with a verb."""
    for line in text.splitlines() or [text]:
        for segment in _SIBLING_SEGMENT_SPLIT_RE.split(line):
            if not _LIFECYCLE_TOOL_RE.search(segment):
                continue
            if _UNEXPANDED_SHELL_VALUE_RE.search(segment):
                return True
    return False


def _lifecycle_targets_only_sibling_gateways(text: str) -> bool:
    """True when every explicit lifecycle target is a SIBLING gateway.

    All four conditions must hold, otherwise the caller keeps blocking:

    1. this process's own service identity is determinable;
    2. *text* names at least one full gateway label / unit explicitly;
    3. none of those names is one of ours;
    4. no unexpanded shell value sits in the same segment as a lifecycle
       tool (it could expand to our own label).
    """
    self_names = _self_gateway_service_names()
    if not self_names:
        return False
    targets = _explicit_gateway_service_names(text)
    if not targets:
        return False
    if targets & self_names:
        return False
    if _lifecycle_verb_segment_has_unexpanded_value(text):
        return False
    return True


def _match_is_sibling_exemptable(matched: str) -> bool:
    """True for launchd/systemd branch matches eligible for sibling exemption.

    ``launchctl submit`` never qualifies: it registers a BRAND NEW KeepAlive
    job whose label is chosen by whoever writes the command, so the label text
    proves nothing about what the job will do (#62891). Branch A
    (``hermes gateway restart``) is excluded too — it has its own
    profile-aware discrimination (#78028) — as is Branch D (``pkill``), which
    targets by process pattern rather than by service label.
    """
    head = matched.lstrip().lower()
    if not (head.startswith("launchctl") or head.startswith("systemctl")):
        return False
    if _LAUNCHCTL_SUBMIT_MATCH_RE.match(matched):
        return False
    return True


def contains_gateway_lifecycle_command(text: str) -> bool:
    """Return True if *text* contains a gateway lifecycle command pattern.

    Matches that are ssh-wrapped to a remote (non-loopback) host are
    exempt — restarting a *different* machine's gateway is legitimate fleet
    maintenance and cannot SIGTERM-loop this process (see
    ``_match_is_ssh_remote``).

    Matches in two passes. The first is the raw-text regex above — cheap,
    and the only pass that can fire on non-shell inputs shlex can't
    tokenize (e.g. a Python source string). The second re-runs the same
    pattern against each command segment after shell tokenization, where
    quotes and backslash escapes have already been resolved.

    That second pass exists because a real shell resolves quote-splicing
    (``kick"start"``) and backslash-escaping (``kick\\start``) into one
    literal word — ``kickstart`` — before the command ever runs. The raw
    text still has the quote or backslash sitting between the verb's two
    halves, so the first pass alone lets a spliced verb reach
    ``launchctl``/``systemctl`` untouched while still executing as the
    blocked lifecycle command (#80269, reported against #80260's bootout
    parity fix). Tokenizing closes that gap while keeping the same
    gateway-label anchoring (``_GATEWAY_LIFECYCLE_PATTERN`` still requires
    a ``hermes``/``gateway`` token) — this function is the single choke
    point ``_contains_unsafe_gateway_action`` calls at every recursion
    level, so referenced-script and ``sh -c`` payload scanning inherit the
    fix automatically.
    """
    if not text:
        return False
    # Heredoc bodies that are provably inert data (quoted delimiter, data-sink
    # consumer like `cat > file <<'EOF'`) are masked before scanning (#88336):
    # a runbook line "a human can run: hermes gateway restart" inside such a
    # body is documentation, not a command this shell will execute. The
    # stripper fails open on ANY ambiguity (unquoted delimiter, shell
    # consumer, unterminated body), so executable heredocs are still scanned.
    from tools.shell_heredoc import strip_inert_heredoc_bodies

    text = strip_inert_heredoc_bodies(text)
    # Collapse POSIX shell line-continuations first (#62891) so a multi-line
    # invocation cannot slip past the `[^\\n]*` branches.
    normalized = _SHELL_LINE_CONTINUATION.sub(" ", text)
    for match in _GATEWAY_LIFECYCLE_PATTERN.finditer(normalized):
        # ssh-wrapped remote lifecycle commands are legitimate fleet ops.
        if _match_is_ssh_remote(normalized, match.start()):
            continue
        # Branch A only (`hermes gateway restart|stop`): exempt when the phrase
        # is quoted DATA fed to a text-only consumer (echo/grep/printf/…), not
        # an executed command. Interpreter re-exec (bash -c "…") is NOT exempt.
        matched = match.group(0)
        if matched.lower().startswith("hermes") and _match_is_quoted_data(
            normalized, match.start(), matched
        ):
            continue
        # Self-aware launchd/systemd exemption: a command whose only explicit
        # lifecycle targets are SIBLING gateway services cannot kill this
        # process, so the #30719 rationale does not apply.
        if _match_is_sibling_exemptable(matched) and _lifecycle_targets_only_sibling_gateways(
            normalized
        ):
            continue
        return True
    # Profile-flag form (#78028): `hermes -p <profile> gateway restart|stop`
    # bypasses Branch A because the selector sits between `hermes` and
    # `gateway`. It is only the same foot-gun when the named profile IS the
    # profile running the guard — sibling-profile restarts are legitimate
    # fleet operations and stay allowed.
    profile_match = _PROFILE_FLAG_LIFECYCLE_PATTERN.search(normalized)
    if profile_match:
        named = profile_match.group(1) or profile_match.group(2)
        if named:
            # Profile ids cannot contain quotes (hermes_cli.profiles
            # enforces `^[a-z0-9][a-z0-9_-]{0,63}$`), so a shell-quoted
            # `-p 'zeus'` compares equal to the bare name.
            named = named.strip().strip("\"'")
            if _named_profile_is_current(named):
                return True
    # Token-aware second pass (#80269): re-run the pattern on shell-tokenized
    # segments where quotes/escapes are resolved, closing splice bypasses
    # like `kick"start"`. Runs after the profile-flag check so both passes
    # apply independently. Tokens are additionally re-joined with Python
    # argv-list punctuation ([ ] ,) stripped (#68289): the same command
    # reaches this guard as `subprocess.run(["launchctl", "bootout", ...])`
    # from execute_code, where commas and brackets — not spaces — separate
    # the argv words the OS will actually see.
    #
    # The fork's raw-pass exemptions must hold here too — tokenization
    # RESOLVES the very quotes/ssh-prefixes the exemptions keyed on, so
    # without these guards the second pass re-blocked what the first pass
    # correctly allowed:
    #   * ssh-wrapped remote lifecycle command (non-loopback target) — the
    #     command runs under the REMOTE sshd; re-checked on the joined
    #     segment via the same _SSH_COMMAND_RE/_LOOPBACK_HOST_RE pair.
    #   * Branch-A phrase quoted as DATA to a text-only consumer
    #     (echo/grep/printf/…). Post-tokenization the quotes are gone, so
    #     the consumer identity carries the exemption: the segment's
    #     command must match _TEXT_CONSUMER_RE (which deliberately
    #     excludes every shell interpreter, so `bash -c "…"` stays
    #     blocked) and the match must be the Branch-A `hermes …` form.
    for segment in _iter_command_segments(normalized):
        joined = " ".join(segment)
        if not joined:
            continue
        if _SSH_COMMAND_RE.search(joined) and not _LOOPBACK_HOST_RE.search(joined):
            continue
        _seg_match = _GATEWAY_LIFECYCLE_PATTERN.search(joined)
        if _seg_match is None:
            stripped = _ARGV_LIST_PUNCTUATION.sub(" ", joined)
            if stripped != joined:
                _seg_match = _GATEWAY_LIFECYCLE_PATTERN.search(stripped)
        if _seg_match is not None:
            if _seg_match.group(0).lower().startswith("hermes") and _TEXT_CONSUMER_RE.match(
                joined
            ):
                continue
            if _match_is_sibling_exemptable(
                _seg_match.group(0)
            ) and _lifecycle_targets_only_sibling_gateways(joined):
                continue
            return True
    # Order-independent launchctl pass (#77083): a shell loop can build the
    # gateway label from a variable defined in an earlier `;`-separated
    # segment (`label=${item%%:*}; launchctl bootout "gui/$uid/$label"`), so
    # neither the same-span regex nor same-segment tokenization sees verb
    # and label together. Check "verb anywhere AND label anywhere" instead.
    #
    # Parity merge 2026-08-29: this upstream pass runs AFTER the match loop,
    # so it never saw the fork's ssh-remote exemption and re-blocked
    # `ssh macbook 'launchctl kickstart ... ai.hermes.gateway'` -- a remote
    # host's launchd job, which cannot SIGTERM this process (the #30719
    # respawn loop needs a LOCAL restart). Apply the same
    # _SSH_COMMAND_RE/_LOOPBACK_HOST_RE pair the loop and the tokenized
    # second pass already use, per line so a later local segment is still
    # caught by its own line.
    _offset = 0
    for _line in normalized.split("\n"):
        _line_start = _offset
        _offset += len(_line) + 1
        if not _contains_launchctl_gateway_lifecycle(_line):
            continue
        if _SSH_COMMAND_RE.search(_line) and not _LOOPBACK_HOST_RE.search(_line):
            continue
        # 2026-09-25: the line sits inside a quoted remote command that an
        # ssh invocation opened on an EARLIER line — the whole quoted string
        # runs under the remote sshd (same rule as _match_is_ssh_remote).
        _lc = re.search(r"(?i)launchctl", _line)
        if _lc is not None and _match_is_ssh_remote(normalized, _line_start + _lc.start()):
            continue
        # Self-aware exemption (2026-09-19): this pass is deliberately
        # label-BLIND, so it also caught sibling-only lines. Explicit
        # sibling-only targets are exempt; a variable-built label in a
        # lifecycle segment is not (it could expand to our own label).
        if _lifecycle_targets_only_sibling_gateways(_line):
            continue
        return True
    # Branch E (#113667): killers aimed at the interpreter image itself carry no hermes/gateway token.
    return contains_host_interpreter_kill(normalized)


_SHELL_EXECUTABLES = frozenset({"sh", "bash", "dash", "ksh", "zsh"})
_SHELL_OPTIONS_WITH_VALUES = frozenset({"-O", "+O", "-o", "+o"})
# `bash -n script` / `sh -n script` parses the script and exits WITHOUT executing it (POSIX sh(1)
# "-n: Read commands but do not execute them"), so the script's lifecycle commands never run; it
# is a syntax check, the same class as `shellcheck script` and `cat script`, which already pass.
# Scanning the operand here refused `bash -n` on every script written FOR a remote host that
# contains `systemctl restart <gateway>` (papercut, 2026-10-06, Apollo). Fail-CLOSED edges kept:
# `-n` combined with `-c` still breaks out below (the -c payload is scanned by the sh -c path),
# and `bash -n script; bash script` is two segments — the second is still walked.
_SHELL_NOEXEC_FLAGS = frozenset({"-n", "--noexec"})
_MAX_REFERENCED_SCRIPT_BYTES = 1024 * 1024
_MAX_REFERENCED_SCRIPT_DEPTH = 8
_CONTROL_CHARS = frozenset(";&|()")


# Directory names that sit directly under a `Library` path component and
# mark a FileProvider-backed subtree: `Mobile Documents` is iCloud Drive;
# `CloudStorage` hosts every third-party FileProvider domain (Dropbox,
# OneDrive, Google Drive, Box, ...) on modern macOS.
_CLOUD_PLACEHOLDER_MARKERS = frozenset({"Mobile Documents", "CloudStorage"})


def _resolve_lenient(path: Path) -> Path:
    """``path.resolve(strict=False)``, falling back to *path* on OSError (unreadable/long) or
    ValueError (embedded NUL from decoded binary tokenized as a path) — never crash the guard."""
    try:
        return path.resolve(strict=False)
    except (OSError, ValueError):
        return path


def _is_cloud_placeholder_path(path: Path) -> bool:
    """Return True for paths inside a macOS FileProvider-backed subtree.

    ``O_NONBLOCK`` does not make regular-file reads non-blocking.  Opening an
    evicted FileProvider placeholder below ``~/Library/Mobile Documents``
    (iCloud Drive) or ``~/Library/CloudStorage`` (Dropbox / OneDrive /
    Google Drive and other third-party providers) can therefore wait
    indefinitely for hydration.  The lifecycle guard runs before a terminal
    command's timeout starts, so it must identify this boundary from path
    metadata and fail closed without opening the file.
    """
    parts = path.parts
    return any(
        parts[index - 1] == "Library" and part in _CLOUD_PLACEHOLDER_MARKERS
        for index, part in enumerate(parts)
        if index
    )

def _on_cloud_path(path: Path) -> bool:
    """Lexical OR resolved cloud check: covers direct cloud paths and local symlinks into one."""
    return _is_cloud_placeholder_path(path) or _is_cloud_placeholder_path(_resolve_lenient(path))


# Executables whose arguments are DATA, not commands: search patterns, SQL
# statements, log filters. None of these can execute their argument text, so
# a lifecycle-shaped string inside their arguments (a grep pattern hunting
# for `systemctl restart hermes-gateway` in syslog, a SQL LIKE literal over a
# restart-events table) is diagnostics, not a lifecycle command. Deliberately
# conservative: no `awk` (system()), no `sed` (`s///e`), no `echo`/`printf`
# (routinely piped into a shell), no `mysql` (`\\!` and `system` escapes).
_DATA_SINK_EXECUTABLES = frozenset(
    {"grep", "egrep", "fgrep", "rg", "ag", "ack", "journalctl", "sqlite3", "psql"}
)
# Argument shapes that can smuggle execution back INTO a data sink: command
# and process substitution anywhere, sqlite3 dot-commands (`.shell ...`),
# psql backslash escapes (`\! ...`). Any hit disables masking for the whole
# segment — fail closed to the plain regex verdict.
_UNSAFE_DATA_ARG_MARKERS = ("`", "$(", "<(", ">(", "\\!")
# A leading dot also disables masking, because sqlite3 spells its escapes as
# dot-commands (`.shell`, `.system`, `.import`). But `.`, `./x` and `../x`
# are ordinary path operands, and `grep -r <pattern> .` is a far more common
# shape than any dot-command — treating those as escapes disabled the
# exemption for the single most ordinary way to run a recursive search,
# blocking `grep -r 'systemctl restart hermes-gateway' .` outright. Require a
# dot followed by a NAME character so a relative path stays a path.
_DOT_COMMAND_ARGUMENT = re.compile(r"^\.[A-Za-z]")
# A data sink piped into a shell/interpreter can feed matched lines straight
# to execution (`grep 'systemctl restart hermes-gateway' f | sh`); never mask
# such a line.
_PIPE_TO_INTERPRETER = re.compile(
    r"\|\s*&?\s*(?:sudo\s+)?(?:sh|bash|dash|ksh|zsh|xargs|eval|source)\b"
)

# Heredoc opener: `<<EOF`, `<<-EOF`, `<<'EOF'`, `<<"EOF"`. The delimiter's
# quoting matters — a QUOTED delimiter means the shell performs no expansion on
# the body at all, so `$(…)`/backticks in it are inert literal text.
_HEREDOC_OPEN_RE = re.compile(
    r"<<-?\s*(?:'(?P<q1>[A-Za-z_][A-Za-z0-9_]*)'"
    r"|\"(?P<q2>[A-Za-z_][A-Za-z0-9_]*)\""
    r"|(?P<bare>[A-Za-z_][A-Za-z0-9_]*))"
)


def _heredoc_delimiter(match: "re.Match[str]") -> str:
    return match.group("q1") or match.group("q2") or match.group("bare") or ""


# A heredoc whose RECEIVING command is a shell/interpreter executes its body
# (`bash <<EOF` / `sh <<'EOF'`), so such a body is code, never data.
_HEREDOC_TO_INTERPRETER = re.compile(
    r"(?i)(?:^|[|;&]|\s)(?:sudo\s+)?(?:/\S*/)?"
    r"(?:sh|bash|dash|ksh|zsh|eval|source)\b[^\n]*<<"
)

# Executable-image magic numbers: ELF, PE/COFF, Mach-O (universal + thin,
# both endiannesses). A referenced file starting with one of these is a
# compiled binary, never a shell script — don't read or scan it at all.
_BINARY_MAGIC_PREFIXES = (
    b"\x7fELF",
    b"MZ",
    b"\xca\xfe\xba\xbe",
    b"\xcf\xfa\xed\xfe",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xce",
    b"\xfe\xed\xfa\xcf",
)
_BINARY_SNIFF_BYTES = 4096




_ReadRemoteScriptFn = Callable[[str], Optional[str]]


# Whole-walk work limits. The per-file cap and depth bound above limit one read, not the walk: a
# command can reference arbitrarily many scripts, and the pure-Python shlex pass (quadratic on a
# giant token) once held the GIL for minutes. These caps bound one whole walk and are charged
# BEFORE any text reaches shlex. Exhaustion fails closed (an unscanned script could hide a
# lifecycle command) and is logged at WARNING so an operator can tell it from a real block. Sizes
# sit well above any legitimate wrapper graph; remote reads are a backend roundtrip each, so they
# get a far tighter cap.
# See #78398.
_MAX_LIFECYCLE_SCAN_BYTES = _MAX_REFERENCED_SCRIPT_BYTES  # 1 MiB across the walk
_MAX_LIFECYCLE_SCAN_LINES = 16384
_MAX_LIFECYCLE_SCAN_LINE_BYTES = 64 * 1024
_MAX_LIFECYCLE_SCAN_PATHS = 1024
_MAX_LIFECYCLE_SCAN_REMOTE_READS = 64


class _LifecycleScanBudget:
    """Shared work budget for one complete referenced-script walk. ``refusal`` records why the walk
    failed closed for a reason other than a lifecycle command (budget, size, device, live SQLite,
    cloud placeholder) so the caller can tell the model the real reason (#113944)."""

    __slots__ = ("bytes_remaining", "lines_remaining", "paths_remaining", "remote_reads_remaining",
                 "refusal")

    def __init__(self) -> None:
        # Read the module constants at construction so tests/operators can lower them at runtime.
        self.bytes_remaining = _MAX_LIFECYCLE_SCAN_BYTES
        self.lines_remaining = _MAX_LIFECYCLE_SCAN_LINES
        self.paths_remaining = _MAX_LIFECYCLE_SCAN_PATHS
        self.remote_reads_remaining = _MAX_LIFECYCLE_SCAN_REMOTE_READS
        self.refusal: Optional[str] = None

    def charge_text(self, text: str) -> bool:
        """Charge *text* before tokenization; False when it does not fit."""
        # UTF-8 is >= one byte per code point, so the char count is a free lower bound.
        if len(text) > self.bytes_remaining:
            return False
        encoded = len(text.encode("utf-8", errors="replace"))
        if encoded > self.bytes_remaining:
            return False
        lines = text.count("\n") + 1
        if lines > self.lines_remaining:
            return False
        # One huge token is the quadratic shlex case; bound the longest physical line (chars, a
        # lower bound on bytes — tight enough for a DoS bound without a per-line encode).
        longest = max((len(line) for line in text.split("\n")), default=0)
        if longest > _MAX_LIFECYCLE_SCAN_LINE_BYTES:
            return False
        self.bytes_remaining -= encoded
        self.lines_remaining -= lines
        return True

    def charge_path(self) -> bool:
        """Charge one unique referenced path before any local/remote read."""
        if self.paths_remaining <= 0:
            return False
        self.paths_remaining -= 1
        return True

    def charge_remote_read(self) -> bool:
        """Charge one remote-backend read (a network roundtrip each)."""
        if self.remote_reads_remaining <= 0:
            return False
        self.remote_reads_remaining -= 1
        return True


def _capped_read_limit(max_bytes: Optional[int]) -> int:
    """Per-read byte cap: never above the per-file cap, never negative. One definition so local and
    remote reads cannot diverge.

    See #76762, #77703.
    """
    if max_bytes is None:
        return _MAX_REFERENCED_SCRIPT_BYTES
    return min(_MAX_REFERENCED_SCRIPT_BYTES, max(0, int(max_bytes)))


def lifecycle_scan_root_within_budget(text: str) -> bool:
    """Whether *text* may safely enter an optional tokenizer pass (``tools/terminal_tool.py`` gates
    its launchctl pre-scan on this). A FRESH budget, independent of the full guard's walk: the
    pre-scan may pass while the walk later exhausts, still fail-closed — only the friendlier
    launchctl diagnostic is lost. ``False`` is not a verdict: callers must still run the full guard."""
    try:
        return _LifecycleScanBudget().charge_text(text)
    except Exception:
        return False


def _budget_exhausted(budget: _LifecycleScanBudget, what: str, depth: int) -> bool:
    logger.warning(
        "lifecycle guard scan budget exhausted (%s at depth %d); "
        "failing closed — see _MAX_LIFECYCLE_SCAN_* in cron/lifecycle_guard.py",
        what, depth,
    )
    budget.refusal = f"the scan budget was exhausted ({what} at depth {depth})"
    return True


def _unreadable_reason(path: Path) -> str:
    """Name why an *executed* script failed closed without being scanned (live SQLite, device,
    oversized). Message-only: the fail-closed verdict itself came from the bounded reader."""
    from hermes_cli.sqlite_safe_read import has_live_connection

    if has_live_connection(path):
        return f"`{path}` is a SQLite database open in this gateway process"
    try:
        metadata = os.stat(path)
    except OSError:
        return f"`{path}` could not be read"
    if not stat.S_ISREG(metadata.st_mode):
        return f"`{path}` is not a regular file"
    return f"`{path}` is larger than the scan cap ({_MAX_REFERENCED_SCRIPT_BYTES} bytes) or the remaining walk budget"


def _refuse_unreadable(budget: _LifecycleScanBudget, path: Path, reason: str) -> bool:
    logger.warning("lifecycle guard cannot scan referenced script %s: %s; failing closed", path, reason)
    budget.refusal = reason
    return True


def _split_logical_lines(text: str) -> list[str]:
    """Split text on newlines that are not inside quotes.

    A newline inside a quoted string (single or double quotes) is data,
    not a command separator. Handles escaped quotes within strings.
    """
    lines = []
    current = []
    in_single = False
    in_double = False
    escape = False

    for ch in text:
        if escape:
            current.append(ch)
            escape = False
            continue
        if ch == "\\":
            escape = True
            current.append(ch)
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            current.append(ch)
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            current.append(ch)
            continue
        if ch == "\n" and not in_single and not in_double:
            lines.append("".join(current))
            current = []
            continue
        current.append(ch)

    if current:
        lines.append("".join(current))
    return lines


def _iter_command_segments(command: str) -> Iterator[list[str]]:
    """Yield shell-tokenized command segments, honoring quotes and comments.

    A newline inside a quoted token is data, not a command separator.
    First split on logical lines (newlines outside quotes), then tokenize
    each logical line with shlex. If a logical line cannot be tokenized
    (unbalanced quotes), fall back to per-physical-line tokenization for
    that logical line.
    """
    normalized = command.replace("\\\n", "")
    logical_lines = _split_logical_lines(normalized)

    for line in logical_lines:
        # Try to tokenize the logical line as a whole.
        try:
            lexer = shlex.shlex(
                line,
                posix=True,
                punctuation_chars=";&|()",
            )
            lexer.whitespace_split = True
            lexer.commenters = "#"
            tokens = list(lexer)
        except ValueError:
            # Fall back to per-physical-line tokenization for this logical line.
            # This handles cases where quotes are unbalanced across lines.
            for physical_line in line.splitlines():
                try:
                    lexer = shlex.shlex(
                        physical_line,
                        posix=True,
                        punctuation_chars=";&|()",
                    )
                    lexer.whitespace_split = True
                    lexer.commenters = "#"
                    tokens = list(lexer)
                except ValueError:
                    continue

                segment: list[str] = []
                for token in tokens:
                    if token and set(token) <= _CONTROL_CHARS:
                        if segment:
                            yield segment
                            segment = []
                        continue
                    segment.append(token)
                if segment:
                    yield segment
            continue

        segment: list[str] = []
        for token in tokens:
            if token and set(token) <= _CONTROL_CHARS:
                if segment:
                    yield segment
                    segment = []
                continue
            segment.append(token)
        if segment:
            yield segment


def _executable_name(token: str) -> str:
    """Return the command name for a tokenized executable token.

    ``Path(token).name`` is right for real paths (``/usr/bin/bash`` →
    ``bash``), but pathlib has no name component for the pure-path tokens
    ``.``, ``..`` and ``/``, so it returns "" for them. The POSIX
    dot-source builtin is spelled ``.``, so keying the sourced-script
    branch on ``Path(token).name`` alone made it unreachable: ``source
    ./helper.sh`` was scanned but its exact synonym ``. ./helper.sh`` was
    not, letting a referenced script carrying a lifecycle command through
    both the cron guard and the in-gateway terminal guard. Fall back to the
    raw token so ``.`` survives.
    """
    return Path(token).name or token


# Prefixes that hand execution straight to their argument tail: the command
# that actually runs sits further right. A guard that reads only the first
# token sees `sudo`/`env`/`nohup` and never inspects what they run, so
# `sudo bash ~/restart.sh` walked past the same walk that stops
# `bash ~/restart.sh`, and `sudo launchctl submit ...` past the
# label-independent submit block (#62891). `_PIPE_TO_INTERPRETER` above
# already reads `sudo ` this way for the pipe case; this generalises that
# reading to the command position.
_TRANSPARENT_COMMAND_PREFIXES = frozenset({
    "sudo", "doas", "env", "nohup", "setsid", "nice", "ionice", "stdbuf",
    "timeout", "exec", "command", "builtin", "eatmydata",
    # Privilege and namespace wrappers. Same shape — options, then the
    # command they hand execution to.
    "pkexec", "su", "runuser", "setpriv", "systemd-run", "nsenter", "unshare",
})

# Options of those wrappers that consume the NEXT token as their value, so a
# value is never mistaken for the wrapped command (`sudo -u deploy bash x.sh`).
_TRANSPARENT_PREFIX_VALUE_OPTIONS = {
    "sudo": {"-u", "-g", "-U", "-C", "-p", "-r", "-t", "-T",
             "--user", "--group", "--prompt"},
    "doas": {"-u", "-C"},
    "env": {"-u", "--unset", "-S", "--split-string", "-C", "--chdir"},
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p", "--class", "--classdata"},
    "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "pkexec": {"--user"},
    "su": {"-s", "--shell", "-g", "--group", "-G", "--supp-group"},
    "runuser": {"-u", "--user", "-s", "--shell", "-g", "--group",
                "-G", "--supp-group"},
    "setpriv": {"--reuid", "--regid", "--groups", "--inh-caps",
                "--ambient-caps", "--bounding-set", "--selinux-label",
                "--apparmor-profile"},
    "systemd-run": {"-u", "--unit", "-p", "--property", "-E", "--setenv",
                    "--slice", "--description", "--uid", "--gid",
                    "--on-calendar", "--service-type"},
    "nsenter": {"-t", "--target", "-S", "--setuid", "-G", "--setgid",
                "-r", "--root", "-w", "--wd"},
    "unshare": {"--map-user", "--map-group", "--setgroups", "-R", "--root",
                "-w", "--wd"},
}

# Wrappers whose option carries a COMMAND STRING rather than an argv tail.
# The string is shell source and must be re-scanned like `sh -c` — skipping
# it as an opaque option value would hide whatever it runs
# (`env -S 'bash ~/restart.sh'`).
_STRING_COMMAND_OPTIONS = {
    "env": ("-S", "--split-string"),
    "su": ("-c", "--command"),
    "runuser": ("-c", "--command"),
}

# Wrappers whose first non-option operand is a VALUE, not the command
# (`timeout 60 bash x.sh`).
_TRANSPARENT_PREFIX_OPERANDS = {"timeout": 1}

_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# Bound the walk: a pathological token run must not spin here.
_MAX_PREFIX_PEELS = 8


def _peel_transparent_prefixes(segment: list[str], index: int) -> int:
    """Return the index of the command a wrapper chain actually executes.

    Returns *index* unchanged when the token there is not a wrapper, and may
    return ``len(segment)`` when a wrapper has no operand — callers must
    bounds-check before indexing.
    """
    for _ in range(_MAX_PREFIX_PEELS):
        if index >= len(segment):
            return index
        name = _executable_name(segment[index])
        if name not in _TRANSPARENT_COMMAND_PREFIXES:
            return index
        value_options = _TRANSPARENT_PREFIX_VALUE_OPTIONS.get(name, frozenset())
        index += 1
        while index < len(segment):
            token = segment[index]
            if token == "--":
                # POSIX end-of-options: the command starts at the next token.
                index += 1
                break
            if token in value_options:
                index += 2
                continue
            if token.startswith("-") or _ENV_ASSIGNMENT.match(token):
                index += 1
                continue
            break
        for _ in range(_TRANSPARENT_PREFIX_OPERANDS.get(name, 0)):
            if index < len(segment) and not segment[index].startswith("-"):
                index += 1
    return index


def _command_token_index(segment: list[str]) -> Optional[int]:
    """Return the executable token index after simple env assignments."""
    for index, token in enumerate(segment):
        if _ENV_ASSIGNMENT.match(token):
            continue
        return index
    return None


def _executed_command_index(segment: list[str]) -> Optional[int]:
    """Index of the command a segment actually executes (env assignments and wrappers peeled)."""
    index = _command_token_index(segment)
    if index is None:
        return None
    index = _peel_transparent_prefixes(segment, index)
    return index if index < len(segment) else None


def contains_launchctl_submit_command(command: str) -> bool:
    """Detect an executed ``launchctl submit``/``bootstrap``, not quoted text.

    Label-independent by design: the label of a submitted/bootstrapped job is
    chosen by whoever writes it, so a neutral name (``ai.hermes.svc-reload-tmp``)
    defeats any label-anchored regex (#62891, second reproduction). Both verbs
    register a NEW persistent launchd job (``submit`` jobs get KeepAlive
    semantics; ``bootstrap`` loads an arbitrary plist), which is never safe to
    do from inside the gateway process.
    """
    for segment in _iter_command_segments(command):
        index = _command_token_index(segment)
        if index is None:
            continue
        index = _peel_transparent_prefixes(segment, index)
        if index >= len(segment):
            continue
        if _executable_name(segment[index]) == "launchctl":
            arguments = segment[index + 1 :]
            if arguments and arguments[0].lower() in {"submit", "bootstrap"}:
                if arguments[0].lower() == "bootstrap" and (
                    _bootstrap_targets_sibling_plist(arguments[1:])
                    or _bootstrap_targets_readable_non_gateway_plist(arguments[1:])
                ):
                    # `launchctl bootstrap gui/<uid> <sibling>.plist` loads a
                    # DIFFERENT gateway's job. It cannot restart-loop this
                    # process, and it is exactly how a break-glass profile
                    # brings a wedged sibling back up. The second predicate
                    # covers the much more common case: bootstrapping a plist
                    # that is ALREADY on disk and is not a gateway job at all
                    # (reloading a fleet daemon after editing it). `submit`
                    # stays blocked unconditionally (its label is
                    # attacker-chosen, so the text proves nothing about the
                    # job it creates).
                    continue
                return True
    return False


def _bootstrap_targets_sibling_plist(arguments: list[str]) -> bool:
    """True when every plist argument names a SIBLING gateway service.

    Deliberately strict: the arguments must contain at least one ``.plist``
    path, every such path's basename must be a full gateway label that is not
    ours, and no argument may carry an unexpanded shell value.
    """
    self_names = _self_gateway_service_names()
    if not self_names:
        return False
    plists = [argument for argument in arguments if argument.lower().endswith(".plist")]
    if not plists:
        return False
    for argument in arguments:
        if _UNEXPANDED_SHELL_VALUE_RE.search(argument):
            return False
    for plist in plists:
        name = _normalize_service_name(Path(plist).name)
        if not _LAUNCHD_GATEWAY_LABEL_RE.fullmatch(name):
            return False
        if name in self_names:
            return False
    return True


# A launchd plist is a small configuration file; the real ones on this fleet
# are well under 8 KiB. Bound the read so a hostile/oversized file can never
# be streamed into the guard, mirroring `_MAX_REFERENCED_SCRIPT_BYTES`.
_MAX_PLIST_BYTES = 256 * 1024

# Tokens that mean "this job IS a hermes gateway" when they appear in a
# plist's Program / ProgramArguments. The label check alone is not enough: a
# plist can carry a neutral Label (`ai.hermes.svc-reload-tmp`) while its argv
# runs `python -m hermes_cli.main gateway run`, which is the #62891 laundering
# shape with a file instead of a `submit` line.
_PLIST_GATEWAY_ARGV_MARKERS = ("hermes_cli.main", "hermes-gateway", "hermes_gateway")

# `_plist_argv_runs_gateway_lifecycle` scans a plist's argv with the same
# scanner that resolves `launchctl bootstrap <plist>` — so a plist whose argv
# bootstraps another plist re-enters it. Bound the nesting per thread (the
# guard runs on the gateway's request threads) and fail CLOSED at the bound.
_MAX_PLIST_ARGV_SCAN_DEPTH = 2
_PLIST_ARGV_SCAN_STATE = threading.local()


def _plist_program_tokens(payload: dict) -> list[str]:
    """Flatten a plist's Program / ProgramArguments into scannable strings."""
    tokens: list[str] = []
    program = payload.get("Program")
    if isinstance(program, str):
        tokens.append(program)
    arguments = payload.get("ProgramArguments")
    if isinstance(arguments, (list, tuple)):
        tokens.extend(item for item in arguments if isinstance(item, str))
    return tokens


def _plist_argv_runs_gateway_lifecycle(payload: dict) -> bool:
    """True when loading this plist would EXECUTE a gateway-lifecycle command.

    ``_plist_declares_gateway_job`` asks whether the plist *is* a gateway job.
    That is not the question the guard exists to answer: a plist with a wholly
    neutral ``Label`` and a non-entrypoint argv can still run

        /bin/sh -c 'launchctl kickstart -k system/ai.hermes.gateway-daedalus'

    — the #62891 laundering shape with a file instead of a ``submit`` line, and
    it needs no root (``launchctl bootstrap gui/<uid> $TMPDIR/x.plist``). Run
    the flattened argv through the lifecycle scanner that already handles both
    inline commands and commands hidden in a referenced script.

    Each token is scanned on its own (a ``sh -c <payload>`` string is its own
    command text, and a bare script path is a referenced script) AND joined (a
    lifecycle command spelled across separate argv words). ``or`` of the two:
    every extra match is a REFUSAL, which is the safe direction.
    """
    tokens = _plist_program_tokens(payload)
    if not tokens:
        return False
    depth = getattr(_PLIST_ARGV_SCAN_STATE, "depth", 0)
    if depth >= _MAX_PLIST_ARGV_SCAN_DEPTH:
        # A plist whose argv bootstraps another plist re-enters this scan.
        # Two plists can reference each other, so bound it and fail CLOSED:
        # a plist that bootstraps a plist is not the routine maintenance
        # shape this allow-path exists for.
        return True
    _PLIST_ARGV_SCAN_STATE.depth = depth + 1
    try:
        if any(
            contains_gateway_lifecycle_command_or_referenced_script(token)
            for token in tokens
        ):
            return True
        return contains_gateway_lifecycle_command_or_referenced_script(" ".join(tokens))
    finally:
        _PLIST_ARGV_SCAN_STATE.depth = depth


def _plist_declares_gateway_job(payload: dict) -> bool:
    """True when *payload* describes, or drives, a hermes GATEWAY job.

    Three independent tells, any one is enough:

    * ``Label`` is a full hermes gateway label; or
    * ``Program``/``ProgramArguments`` reference a hermes gateway entrypoint
      (``hermes_cli.main ... gateway``, a ``hermes-gateway`` launcher, ...); or
    * that same argv EXECUTES a gateway-lifecycle command, inline or via a
      referenced script (see ``_plist_argv_runs_gateway_lifecycle``).
    """
    label = payload.get("Label")
    if not isinstance(label, str) or not label.strip():
        # No Label at all: launchd would reject it, and we cannot reason
        # about what it registers. Treat as gateway-ish (fail closed).
        return True
    if _LAUNCHD_GATEWAY_LABEL_RE.fullmatch(_normalize_service_name(label)):
        return True
    tokens = [token.lower() for token in _plist_program_tokens(payload)]
    if any(marker in token for token in tokens for marker in _PLIST_GATEWAY_ARGV_MARKERS):
        return True
    # `... -m hermes_cli.main gateway run` spelled across separate argv words.
    if "gateway" in tokens and any("hermes" in token for token in tokens):
        return True
    return _plist_argv_runs_gateway_lifecycle(payload)


def _read_plist_label_payload(path: Path) -> Optional[dict]:
    """Bounded, regular-file-only plist read. ``None`` means "cannot read".

    Fails closed on every ambiguity the caller must not paper over: cloud
    placeholders (an evicted FileProvider read can hang the guard, #88052),
    non-regular files, directories, oversized files, and anything ``plistlib``
    cannot parse into a mapping.
    """
    import plistlib

    if _is_cloud_placeholder_path(path):
        return None
    try:
        resolved = path.resolve(strict=False)
    except (OSError, ValueError):
        resolved = path
    if _is_cloud_placeholder_path(resolved):
        return None
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except (OSError, ValueError):
        return None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            # Directories (`launchctl bootstrap <domain> <dir>` loads EVERY
            # plist inside), FIFOs, devices: nothing we can read once and
            # reason about.
            return None
        if metadata.st_size > _MAX_PLIST_BYTES:
            return None
        data = b""
        while len(data) <= _MAX_PLIST_BYTES:
            chunk = os.read(descriptor, _MAX_PLIST_BYTES + 1 - len(data))
            if not chunk:
                break
            data += chunk
    except OSError:
        return None
    finally:
        os.close(descriptor)
    if len(data) > _MAX_PLIST_BYTES:
        return None
    try:
        payload = plistlib.loads(data)
    except Exception:
        # Malformed / not a plist at all. plistlib raises several unrelated
        # exception types depending on format; none of them mean "safe".
        return None
    return payload if isinstance(payload, dict) else None


def _bootstrap_targets_readable_non_gateway_plist(arguments: list[str]) -> bool:
    """True when every plist argument is an EXISTING, non-gateway plist.

    ``submit`` and ``bootstrap`` are otherwise handled label-independently
    because a NEW job's label is chosen by whoever writes the command
    (#62891). That reasoning holds for ``submit`` (pure text) and for
    bootstrapping a path that does not exist yet — but NOT for a plist already
    on disk: launchd reads ``Label`` out of that file, so the label is a
    readable fact, not an attacker's claim. Refusing those blocked routine
    fleet maintenance (reloading ``ai.hermes.fleetreview-router`` after a
    ``plutil -replace`` edit) and pushed operators onto the deprecated,
    label-gated ``launchctl load -w``.

    Deliberately strict — every condition must hold:

    1. at least one argument looks like a plist path;
    2. no argument carries an unexpanded shell value (the path we read would
       not be the path launchd loads);
    3. every plist argument resolves to a readable regular file within the
       size bound; and
    4. none of those files declares a hermes gateway job (by ``Label`` or by
       entrypoint).
    """
    plists = [argument for argument in arguments if argument.lower().endswith(".plist")]
    if not plists:
        return False
    for argument in arguments:
        if _UNEXPANDED_SHELL_VALUE_RE.search(argument):
            return False
    for plist in plists:
        candidate = _expand_candidate_path(plist)
        if candidate is None:
            return False
        payload = _read_plist_label_payload(candidate)
        if payload is None:
            return False
        if _plist_declares_gateway_job(payload):
            return False
    return True


def _mask_data_sink_arguments(text: str) -> str:
    """Replace data-sink executables' arguments with a neutral placeholder.

    The lifecycle regex is command-shaped, but it cannot tell an EXECUTED
    ``systemctl restart hermes-gateway`` from the same characters appearing
    as *data* — a grep/rg pattern, a journalctl filter, a SQL string literal
    passed to sqlite3/psql. Those diagnostics commands were being rejected
    (false positives blocking legitimate cron prompts), e.g.::

        grep -c 'systemctl restart hermes-gateway' /var/log/syslog
        sqlite3 db "SELECT msg FROM log WHERE msg LIKE '%systemctl restart hermes-gateway%'"

    This masker shell-tokenizes each line and, for command segments whose
    executable is a known data sink (``_DATA_SINK_EXECUTABLES``), replaces
    every argument with ``arg``. The caller then re-runs the lifecycle regex
    on the masked text: a match that survives masking sits OUTSIDE any data
    argument and is a real command.

    Strictly fail-closed: masking is skipped (leaving the original,
    regex-matching text in place) whenever the line pipes into a shell or
    interpreter, any argument carries an execution-capable marker
    (substitution, sqlite3 ``.``-commands, psql ``\\!``), or the line cannot
    be tokenized at all. Masking can therefore only ever ALLOW a command the
    plain regex would have blocked — never block one it would have allowed —
    so it runs solely as a second-pass exemption check.
    """
    lines_out: list[str] = []
    changed = False
    for line in text.splitlines() or [text]:
        if _PIPE_TO_INTERPRETER.search(line):
            lines_out.append(line)
            continue
        try:
            lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|()")
            lexer.whitespace_split = True
            lexer.commenters = "#"
            tokens = list(lexer)
        except ValueError:
            lines_out.append(line)
            continue

        segments: list[list[str]] = []
        current: list[str] = []
        for token in tokens:
            if token and set(token) <= _CONTROL_CHARS:
                segments.append(current)
                segments.append([token])
                current = []
                continue
            current.append(token)
        segments.append(current)

        rebuilt: list[str] = []
        for segment in segments:
            if not segment:
                continue
            index = _command_token_index(segment)
            if index is not None and Path(segment[index]).name in _DATA_SINK_EXECUTABLES:
                arguments = segment[index + 1 :]
                if not any(
                    _DOT_COMMAND_ARGUMENT.match(argument)
                    or any(marker in argument for marker in _UNSAFE_DATA_ARG_MARKERS)
                    for argument in arguments
                ):
                    changed = True
                    rebuilt.extend(segment[: index + 1])
                    rebuilt.extend("arg" for _ in arguments)
                    continue
            rebuilt.extend(segment)
        lines_out.append(" ".join(rebuilt))
    if not changed:
        return text
    return "\n".join(lines_out)


def _mask_heredoc_bodies(text: str) -> str:
    """Replace HEREDOC BODY lines with a neutral placeholder, fail-closed.

    A heredoc body is DATA being fed to a command's stdin — a README being
    written, a Python program's string literals, a commit message. When the
    delimiter is quoted (``<<'EOF'``) the shell performs no expansion at all, and
    even unquoted the body is still just stdin bytes. So a lifecycle phrase that
    appears only inside a heredoc body is prose, not an invocation::

        python3 - <<'PYEOF'
        s = "you may still need to restart the gateway"
        PYEOF

    That exact shape was blocked in production (papercut pc-fccd2771: patching a
    README through a python heredoc was refused because the literal WORDS
    "restart"/"stop" appeared in the quoted document text).

    STRICTLY FAIL-CLOSED — masking is skipped, leaving the original
    regex-matching text in place, whenever the body could actually be EXECUTED:

    * the heredoc feeds a shell/interpreter (``bash <<EOF``, ``sh <<EOF``), or
    * the opening line pipes the heredoc into one (``cat <<EOF | bash``), or
    * the delimiter is UNQUOTED and the body contains command substitution
      (``$(…)`` / backticks), which the shell WOULD expand before delivery.

    Like `_mask_data_sink_arguments`, this can only ever ALLOW something the
    plain regex would have blocked — never block something it would have allowed
    — so it runs solely as a second-pass exemption check.
    """
    if "<<" not in text:
        return text
    lines = text.splitlines()
    out: list[str] = []
    changed = False
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        match = _HEREDOC_OPEN_RE.search(line)
        if not match:
            continue
        quoted = bool(match.group("q1") or match.group("q2"))
        delimiter = _heredoc_delimiter(match)
        if not delimiter:
            continue

        # Collect the body up to the terminator (or EOF if never terminated).
        body: list[str] = []
        end = i
        while end < len(lines) and lines[end].strip() != delimiter:
            body.append(lines[end])
            end += 1
        terminator = lines[end] if end < len(lines) else None

        # Fail-closed conditions: the body may actually be executed.
        feeds_interpreter = bool(_HEREDOC_TO_INTERPRETER.search(line))
        piped_to_interpreter = bool(_PIPE_TO_INTERPRETER.search(line))
        expands = (not quoted) and any(
            marker in "\n".join(body) for marker in ("$(", "`")
        )
        if feeds_interpreter or piped_to_interpreter or expands:
            out.extend(body)
        else:
            changed = changed or bool(body)
            out.extend("heredoc-data" for _ in body)
        if terminator is not None:
            out.append(terminator)
        i = end + 1 if terminator is not None else end
    if not changed:
        return text
    return "\n".join(out)


def _lifecycle_command_scan_with_data_exemption(text: str) -> bool:
    """Lifecycle-regex scan that exempts matches living inside data arguments.

    Two-pass: the cheap regex first (the overwhelmingly common no-match case
    pays nothing extra); on a raw match, re-scan with data-sink arguments and
    heredoc bodies masked out. Only a match that survives masking — i.e. one in
    actual command position — blocks.
    """
    if not contains_gateway_lifecycle_command(text):
        return False
    normalized = _SHELL_LINE_CONTINUATION.sub(" ", text)
    masked = _mask_data_sink_arguments(_mask_heredoc_bodies(normalized))
    return contains_gateway_lifecycle_command(masked)


def _direct_lifecycle_scan(command: str) -> bool:
    """Pure-string direct scans: lifecycle regex (data-exempted) + submit."""
    return _lifecycle_command_scan_with_data_exemption(
        command
    ) or contains_launchctl_submit_command(command)


def _expand_candidate_path(candidate: str) -> Optional[Path]:
    """Sanitize a tokenized path candidate at the ingestion boundary.

    Candidate tokens come from shlex-splitting arbitrary command text —
    including text recursively decoded from binaries or remote reads — so
    they can carry NUL bytes or other junk no real filesystem path can
    contain. Every OS-facing ``Path`` operation downstream (``expanduser``,
    ``os.open``, ``resolve``) raises a *different* exception for the same
    junk (``ValueError: embedded null byte``, ``RuntimeError: Could not
    determine home directory`` when HOME is unset under launchd, OSError
    for over-long paths). Rejecting here — once, before any OS call — is
    the whole-class fix; catching per-syscall was the whack-a-mole that
    produced #76762, #77703, #77780, and #78256.

    Returns ``None`` for candidates that cannot be a real path (nothing to
    scan), otherwise the ``expanduser()``-expanded ``Path``.
    """
    if not candidate or "\x00" in candidate:
        return None
    try:
        return Path(candidate).expanduser()
    except (ValueError, RuntimeError, OSError):
        return None


def _resolve_terminal_script_path(candidate: str, cwd: Optional[str]) -> Optional[Path]:
    path = _expand_candidate_path(candidate)
    if path is None:
        return None
    if not path.is_absolute():
        try:
            path = Path(cwd or Path.cwd()) / path
        except OSError:
            # Path.cwd() can raise when the process cwd was deleted.
            return None
    return path


def _iter_option_values(
    segment: list[str], start: int, option: str
) -> Iterator[str]:
    """Yield values given to *option*, in both ``--opt v`` and ``--opt=v`` form."""
    prefix = option + "="
    for position in range(start + 1, len(segment)):
        token = segment[position]
        if token == option and position + 1 < len(segment):
            yield segment[position + 1]
        elif token.startswith(prefix):
            yield token[len(prefix):]


def _is_unscannable_non_executable_file(path: Path) -> bool:
    """True when *path* is an OVERSIZED regular file lacking the execute bit.

    Two facts have to hold together before the guard may drop a reference:

    * The shell could not execute it at command position — ``./file`` on a
      0644 regular file is "Permission denied", never a script run.
    * It is unscannable, so keeping the reference means fail-closing rather
      than scanning. A *scannable* non-executable file is still scanned: a
      ``chmod +x x.sh && ./x.sh`` one-liner is scanned while the file is
      still 0644, so skipping every non-executable file would be a bypass.

    The cloud-placeholder refusal is repeated here, BEFORE any syscall, so an
    evicted FileProvider path is never stat'ed on the way to this answer
    (#88052 contract). A placeholder returns False, leaving the reference for
    ``_read_referenced_script`` to fail closed lexically.

    Anything else — directories, missing paths, FIFOs, devices, symlink loops,
    unreadable parents — returns False and keeps its existing behaviour.
    """
    if _is_cloud_placeholder_path(path):
        return False
    try:
        resolved = path.resolve(strict=False)
    except (OSError, ValueError):
        resolved = path
    if _is_cloud_placeholder_path(resolved):
        return False
    try:
        metadata = os.stat(path)
    except (OSError, ValueError):
        return False
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o111:
        return False
    return metadata.st_size > _MAX_REFERENCED_SCRIPT_BYTES


def _references_at(
    segment: list[str], index: int, cwd: Optional[str]
) -> Iterator[Path]:
    """Yield the scripts the token at *index* executes, if any."""
    if index >= len(segment):
        return
    executable = segment[index]
    executable_name = _executable_name(executable)

    if executable_name in {".", "source"}:
        if len(segment) > index + 1:
            resolved = _resolve_terminal_script_path(segment[index + 1], cwd)
            if resolved is not None:
                yield resolved
        return

    if executable_name in _SHELL_EXECUTABLES:
        arguments = segment[index + 1 :]
        arg_index = 0
        noexec = False
        while arg_index < len(arguments):
            argument = arguments[arg_index]
            if argument == "--":
                arg_index += 1
                break
            if argument in {"-c", "--command"}:
                break
            if argument in _SHELL_OPTIONS_WITH_VALUES:
                arg_index += 2
                continue
            if argument in _SHELL_NOEXEC_FLAGS or (
                argument.startswith("-") and not argument.startswith("--")
                and len(argument) > 2 and "n" in argument[1:] and "c" not in argument[1:]
            ):
                noexec = True   # `-n`, `-ne`, `-xn`: parse-only; the operand never executes
            if argument.startswith("-"):
                arg_index += 1
                continue
            break
        if noexec:
            return
        if arg_index < len(arguments) and arguments[arg_index] not in {
            "-c",
            "--command",
        }:
            resolved = _resolve_terminal_script_path(arguments[arg_index], cwd)
            if resolved is not None:
                yield resolved
        return

    # A bare "/" token is pathlib's division operator in Python sources
    # (e.g. `Path.home() / ".hermes"`), not an executable reference.
    # Resolving it walks to the filesystem root and fails the
    # regular-file check below, hard-blocking innocent .py scripts
    # (#77131). Skip pure-separator tokens.
    if executable.strip("/"):
        if "/" in executable or executable.endswith((".sh", ".bash", ".zsh")):
            resolved = _resolve_terminal_script_path(executable, cwd)
            # A regular file WITHOUT the execute bit cannot be run at command
            # position (`./file` → "Permission denied"), so when it is also
            # too large to scan it is not a script reference — it is a data
            # file, and fail-closing on it blocked every command that merely
            # NAMES one inside a nested script or heredoc. A
            # `sqlite3.connect(os.path.expanduser('~/.hermes/kanban.db'))`
            # line parses to a lone path token at command position, and the
            # 31 MB DB (no shebang, no binary magic, over the size cap) came
            # back "oversized script" → blocked. Same class as the bare `/`
            # token (#77131) and the directory token (pc-fb1bd018).
            # Deliberately narrow: an oversized EXECUTABLE file still fails
            # closed (the real unscannable-script case), a scannable
            # non-executable file is still scanned (`chmod +x x.sh && ./x.sh`
            # is scanned while x.sh is still 0644), and this branch is the
            # ONLY one affected — `bash file` and `source file` execute
            # without the x bit and must keep being scanned.
            if resolved is not None and not _is_unscannable_non_executable_file(
                resolved
            ):
                yield resolved


def _iter_referenced_shell_scripts(
    command: str,
    *,
    cwd: Optional[str] = None,
) -> Iterator[Path]:
    """Yield scripts executed directly or through a POSIX shell.

    Tracks ``cd`` segments so a relative script reference after a
    directory change resolves against the directory the shell would
    actually be in. Without this, ``cd /path/proj && ./proj ...``
    resolved ``./proj`` against the *original* cwd, landing on the
    project directory ``/path/proj`` itself — a non-regular file — and
    the fail-closed read hard-blocked every launcher-script invocation
    of that common shape with a bogus gateway-lifecycle error.

    Each segment is read twice: once at the token the walk has always used,
    and again at the command a wrapper chain hands off to. Additive on
    purpose — peeling must never REMOVE a reference the un-peeled read would
    have found. A local script named ``./timeout`` is a script, not the
    coreutils wrapper, and reading only the peeled index would skip it.
    """
    effective_cwd = cwd
    for segment in _iter_command_segments(command):
        index = _command_token_index(segment)
        if index is None:
            continue
        executable_name = _executable_name(segment[index])
        if executable_name == "cd":
            # Model the directory change for subsequent segments. `cd` with
            # no argument goes $HOME; `cd -` is untrackable (previous dir
            # unknown) so conservatively stop trusting the cwd from here on.
            if len(segment) <= index + 1:
                effective_cwd = str(Path.home())
            else:
                target = segment[index + 1]
                if target == "-":
                    effective_cwd = None
                else:
                    resolved_target = _resolve_terminal_script_path(
                        target, effective_cwd
                    )
                    effective_cwd = (
                        str(resolved_target)
                        if resolved_target is not None
                        else None
                    )
            continue
        yield from _references_at(segment, index, effective_cwd)
        peeled = _peel_transparent_prefixes(segment, index)
        if peeled != index:
            yield from _references_at(segment, peeled, effective_cwd)


def _iter_shell_command_payloads(command: str) -> Iterator[str]:
    """Yield code passed through ``sh|bash|... -c`` for recursive scanning."""
    for segment in _iter_command_segments(command):
        index = _command_token_index(segment)
        if index is None:
            continue
        # Command-string options are read at the ORIGINAL token: peeling past
        # `su`/`env` would discard the very option carrying the command.
        for option in _STRING_COMMAND_OPTIONS.get(
            _executable_name(segment[index]), ()
        ):
            yield from _iter_option_values(segment, index, option)
        index = _peel_transparent_prefixes(segment, index)
        if index >= len(segment):
            continue
        if _executable_name(segment[index]) not in _SHELL_EXECUTABLES:
            continue
        arguments = segment[index + 1 :]
        for arg_index, argument in enumerate(arguments[:-1]):
            if argument in {"-c", "--command"}:
                yield arguments[arg_index + 1]
                break


def _resolve_script_directory(script_path: str) -> Optional[str]:
    """Return the directory *script_path* resolves to, handling relative names."""
    try:
        path = _resolve_script_path(script_path)
        if path is not None and path.is_absolute():
            return str(path.parent)
    except Exception:
        pass
    return None


_BINARY_MAGICS = (
    b"\x7fELF",              # ELF — Linux/BSD executables and shared objects
    b"\xfe\xed\xfa\xce",     # Mach-O 32-bit
    b"\xfe\xed\xfa\xcf",     # Mach-O 64-bit
    b"\xce\xfa\xed\xfe",     # Mach-O 32-bit, byte-swapped
    b"\xcf\xfa\xed\xfe",     # Mach-O 64-bit, byte-swapped
    b"\xca\xfe\xba\xbe",     # Mach-O universal ("fat") binary
    b"MZ",                   # PE/COFF — Windows .exe/.dll
    b"!<arch>",              # static archive (.a)
    b"\x1f\x8b",             # gzip
    b"PK\x03\x04",           # zip (also .jar/.whl/.egg)
)


def _has_binary_magic(data: bytes) -> bool:
    """Return True when *data* starts with a known compiled-binary signature.

    Deliberately narrower than "contains a NUL byte": a shell script that
    happens to hold a NUL is still executed by ``bash``, so treating every
    NUL-bearing file as an unscannable binary lets a padded script bypass the
    lifecycle scan entirely.

    A shebang always wins — an interpreted script is never a binary, however
    odd its payload. File extensions are deliberately *not* consulted: a
    suffixless shell script must still be scanned (and, if oversized, still
    fail closed).
    """
    if data.startswith(b"#!"):
        return False
    return data.startswith(_BINARY_MAGICS)


def _read_referenced_script(
    path: Path, *, max_bytes: Optional[int] = None
) -> tuple[Optional[str], bool]:
    """Read a referenced script without racing SQLite connection lifecycle.

    The registry check must cover the complete ``open``/``read``/``close``
    sequence. A separate ``has_live_connection`` check would leave a race in
    which another thread opens SQLite after the check but before this function
    closes its descriptor, cancelling that connection's POSIX locks.
    """
    from hermes_cli.sqlite_safe_read import LiveConnectionError, offline_file_access

    try:
        with offline_file_access(path, what="read referenced script"):
            return _read_referenced_script_unlocked(path, max_bytes=max_bytes)
    except LiveConnectionError:
        return None, True
    except (OSError, ValueError):
        # Invalid path values, including embedded NULs, are not scripts.
        return None, False


def _read_referenced_script_unlocked(
    path: Path, *, max_bytes: Optional[int] = None
) -> tuple[Optional[str], bool]:
    """Return ``(text, unsafe)`` using bounded, regular-file-only reads.

    This is the shared choke point for every local script read the guard
    performs (the terminal walk in ``_contains_unsafe_gateway_action`` AND
    the cron-script scan in ``_read_script_for_scanning``), so the
    cloud-placeholder refusal lives here: a FileProvider path must never be
    opened — not even to discover whether the file is hydrated — because an
    evicted placeholder's ``open()`` can hang preflight indefinitely
    (#88052). The lexical check covers direct cloud paths; the resolved
    check covers local launchers that are symlinks into a cloud subtree.
    ``max_bytes`` lowers the per-file cap to what the calling walk can still afford.
    """
    byte_limit = _capped_read_limit(max_bytes)
    if _is_cloud_placeholder_path(path):
        return None, True
    try:
        resolved = path.resolve(strict=False)
    except (OSError, ValueError):
        # OSError: unreadable/long paths. ValueError: embedded NUL byte
        # from a binary's decoded contents tokenized as a path — a
        # guarded path must never crash the guard (#76762).
        resolved = path
    if _is_cloud_placeholder_path(resolved):
        return None, True
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except (OSError, ValueError):
        # OSError: unreadable / missing / over-long paths. ValueError: an
        # embedded NUL byte in *path* itself — a binary's decoded bytes
        # tokenized into a bogus script path by the recursion (#77703). A
        # guarded read must never crash the guard, so treat either as
        # "nothing to scan" (mirrors the resolve() ValueError guard below).
        return None, False
    try:
        metadata = os.fstat(descriptor)
        if stat.S_ISDIR(metadata.st_mode):
            # A directory is definitively not a shell script — bash cannot
            # execute one, so there is nothing to scan and nothing to fear.
            # Failing closed here blocked every command referencing a script
            # that merely *names* a directory path (e.g. acceptance.sh's
            # REOLINK_DIR="$HOME/.hermes/skills-shared/..." default), which
            # trained agents to route around the guard (pc-fb1bd018).
            return None, False
        if not stat.S_ISREG(metadata.st_mode):
            # Directories are not scripts. Docker Desktop writes
            # ``fpath=(~/.docker/completions …)`` into ``~/.zshrc``; the
            # walk then treats that dir as a referenced script and used
            # to fail-closed, blocking ``source ~/.zshrc`` (#86753).
            if stat.S_ISDIR(metadata.st_mode):
                return None, False
            # FIFOs / devices / sockets stay fail-closed: their content is
            # unbounded/side-effectful and cannot be safely scanned.
            return None, True
        # Sniff a small prefix first: files that are clearly compiled
        # binaries (executable magic) are never shell scripts, so skip them
        # WITHOUT reading the rest — reading a megabyte of machine code just
        # to discard it wastes the guard's budget and (pre-#77703) fed
        # decoded garbage into the recursion. Deliberately NOT keyed on the
        # mere presence of a NUL byte (#77927): bash executes a text script
        # straight past an embedded NUL, so NUL-bearing text must fall
        # through to the magic-number check + NUL-strip below.
        data = os.read(descriptor, _BINARY_SNIFF_BYTES)
        if data.startswith(_BINARY_MAGIC_PREFIXES):
            return None, False
        # Read the remainder (bounded). Loop because os.read may return
        # short for non-regular-file-backed descriptors.
        while len(data) <= byte_limit:
            chunk = os.read(descriptor, byte_limit + 1 - len(data))
            if not chunk:
                break
            data += chunk
    except OSError:
        return None, False
    finally:
        os.close(descriptor)
    # Identify binaries by MAGIC NUMBER, not by the mere presence of a NUL.
    #
    # "contains a NUL" and "is a compiled binary" are different questions, and
    # the gap between them is a guard bypass: `bash` executes a *text* script
    # straight past an embedded NUL, so a single pad byte in a shell script made
    # the scan skip a file that still runs its lifecycle command. Match on the
    # signature instead (ELF/Mach-O/PE/static archive/compressed), and treat a
    # NUL-bearing *text* file as a script whose NULs are stripped before
    # scanning — stripping can only splice tokens together, never apart, so it
    # fails closed.
    if _has_binary_magic(data):
        return None, False
    # Check the size BEFORE stripping: stripping shrinks the buffer, so doing it
    # first would let an oversized file slip under the threshold and skip this
    # fail-closed branch.
    if len(data) > byte_limit:
        return None, True
    if b"\x00" in data:
        data = data.replace(b"\x00", b"")
    return data.decode("utf-8", errors="replace"), False


def _sanitize_remote_script_text(
    text: Optional[str], *, max_bytes: Optional[int] = None
) -> tuple[Optional[str], bool]:
    """Apply the local-read contract to text from a ``read_remote_script`` callback.

    The recursion boundary must not trust its callbacks: any backend (SSH,
    Modal, Daytona, or a future one) can hand back raw binary bytes decoded
    as text, or arbitrarily large output. Mirror
    ``_read_referenced_script``'s semantics exactly — NUL bytes mean binary
    (nothing to scan, checked first, #77703), oversized text fails closed
    like an oversized local file (#76762) — so remote and local reads can
    never diverge again. The size check re-encodes to compare *bytes*
    (matching the local read and the ``head -c`` wire bound): a >1 MiB
    multibyte file truncated at the byte cap decodes to fewer characters
    than bytes, and a character-count check would scan the truncated text
    instead of failing closed. Enforced here rather than inside each
    callback so the guarantee holds for every callback, not just the ones
    we hardened.
    """
    if not text:
        return None, False
    if "\x00" in text:
        return None, False
    byte_limit = _capped_read_limit(max_bytes)
    if len(text.encode("utf-8", errors="replace")) > byte_limit:
        return None, True
    return text, False


# Whole-body allowlist for ``_mask_read_only_python_paths``. A deny-list of
# rebinding shapes cannot close "a spelling is not an identity": a trusted
# object can be mutated IN PLACE through a Load-context call that binds
# nothing. So the mask is granted only when every node of the body is
# recognised; anything else keeps the conservative shell-reference scan.
#
# Soundness argument: with no class/def/lambda, no dunder access, no
# reflective builtins, no import beyond json/subprocess/pathlib.Path, imported
# names loaded ONLY as the callee of an allowlisted call, and callback keywords
# limited to constants/pure data builtins, every value in the body is a
# built-in data value (str/list/dict/set/file/CompletedProcess), so the
# allowlisted methods below are data operations and cannot reach or replace a
# callable the loop recognizer trusts.
_MASK_SAFE_NODE_TYPES = (
    ast.Module, ast.Import, ast.ImportFrom, ast.alias,
    ast.Assign, ast.AugAssign, ast.Expr, ast.For, ast.If, ast.Try,
    ast.ExceptHandler, ast.Continue, ast.Break, ast.Pass,
    ast.Name, ast.Load, ast.Store, ast.Constant, ast.Attribute,
    ast.Subscript, ast.Slice, ast.Call, ast.keyword,
    ast.List, ast.Tuple, ast.Dict, ast.Set,
    ast.JoinedStr, ast.FormattedValue,
    ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Compare, ast.IfExp,
    ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.comprehension,
    ast.boolop, ast.operator, ast.unaryop, ast.cmpop,
)
_MASK_SAFE_MODULES = frozenset({"json", "subprocess"})
_MASK_SAFE_MODULE_CALLS = frozenset({("json", "loads"), ("json", "dumps"), ("subprocess", "run")})
_MASK_SAFE_BUILTINS = frozenset({
    "open", "reversed", "len", "print", "set", "list", "dict", "tuple",
    "sorted", "str", "int", "float", "bool", "min", "max", "sum", "abs",
    "round", "enumerate", "range", "zip", "any", "all", "isinstance",
})
_MASK_SAFE_EXCEPTIONS = frozenset({
    "Exception", "ValueError", "KeyError", "TypeError", "IndexError",
    "AttributeError", "OSError", "FileNotFoundError", "UnicodeDecodeError",
})
_MASK_SAFE_METHODS = frozenset({
    "read", "read_text", "read_bytes", "splitlines", "split", "rsplit",
    "strip", "lstrip", "rstrip", "lower", "upper", "startswith", "endswith",
    "replace", "join", "get", "items", "keys", "values", "add", "append",
    "extend", "count", "find", "encode", "decode", "isdigit",
})
_MASK_SUBPROCESS_KEYWORDS = frozenset({"capture_output", "text", "check", "timeout", "encoding", "errors"})
# Keywords whose value is CALLED by the callee (sorted/min/max key, json hooks,
# print file=). Allowed values: a constant or one of the pure data builtins.
_MASK_CALLBACK_KEYWORDS = frozenset({
    "key", "default", "cls", "object_hook", "object_pairs_hook",
    "parse_float", "parse_int", "parse_constant", "file", "opener",
})
_MASK_PURE_CALLBACKS = frozenset({
    "len", "str", "int", "float", "bool", "abs", "round", "sorted",
    "list", "tuple", "set", "dict", "min", "max", "sum",
})
_MASK_FORBIDDEN_NAMES = frozenset({
    "sys", "builtins", "importlib", "types", "object", "type", "os",
    "getattr", "setattr", "delattr", "exec", "eval", "compile",
    "globals", "locals", "vars", "__import__", "__builtins__",
})


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _python_body_is_mask_safe(tree: ast.AST) -> bool:
    """Return True only when EVERY node of *tree* is in the enumerated safe set."""
    statements = list(getattr(tree, "body", ()))
    # The extracted heredoc range ends with its delimiter line (``PY``/``EOF``),
    # which parses as a trailing bare-name expression. Python never sees it
    # (the shell consumes the delimiter), and a bare name load calls nothing.
    if (statements and isinstance(statements[-1], ast.Expr)
            and isinstance(statements[-1].value, ast.Name)
            and not _is_dunder(statements[-1].value.id)
            and statements[-1].value.id not in _MASK_FORBIDDEN_NAMES):
        tree = ast.Module(body=statements[:-1], type_ignores=[])
    imported: set[str] = set()
    bound: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, _MASK_SAFE_NODE_TYPES):
            return False
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in _MASK_SAFE_MODULES or alias.asname is not None:
                    return False
                imported.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module != "pathlib" or node.level or any(
                alias.name != "Path" or alias.asname is not None for alias in node.names
            ):
                return False  # also refuses ``from x import *``
            imported.add("Path")
        elif isinstance(node, ast.ExceptHandler) and node.name is not None:
            bound.add(node.name)
        elif isinstance(node, ast.Name):
            if _is_dunder(node.id) or node.id in _MASK_FORBIDDEN_NAMES:
                return False
            if isinstance(node.ctx, ast.Store):
                bound.add(node.id)
        elif isinstance(node, ast.Attribute):
            if _is_dunder(node.attr) or not isinstance(node.ctx, ast.Load):
                return False
        elif isinstance(node, ast.Subscript):
            if not isinstance(node.ctx, ast.Load):
                return False
    protected = _MASK_SAFE_BUILTINS | _MASK_SAFE_EXCEPTIONS | _MASK_SAFE_MODULES | {"Path"}
    if bound & protected:
        return False
    known = bound | imported | _MASK_SAFE_BUILTINS | _MASK_SAFE_EXCEPTIONS
    # An imported name may be loaded ONLY as the callee of an allowlisted call:
    # ``Path(...)`` or the receiver of ``json.loads``/``json.dumps``/
    # ``subprocess.run``. Anywhere else it leaks a callable as a value
    # (``sorted(out, key=subprocess.os.system)``, ``s = subprocess``).
    callee_uses: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "Path":
            callee_uses.add(id(func))
        elif (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
              and (func.value.id, func.attr) in _MASK_SAFE_MODULE_CALLS):
            callee_uses.add(id(func.value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id not in known:
            return False
        if (isinstance(node, ast.Name) and node.id in imported
                and id(node) not in callee_uses):
            return False
        if not isinstance(node, ast.Call):
            continue
        # Every call's keywords: no ``**`` splat, and a callback keyword gets a
        # constant or a pure data builtin, never a callable reference.
        for kw in node.keywords:
            if kw.arg is None:
                return False
            if kw.arg in _MASK_CALLBACK_KEYWORDS and not (
                isinstance(kw.value, ast.Constant)
                or (isinstance(kw.value, ast.Name) and kw.value.id in _MASK_PURE_CALLBACKS)
            ):
                return False
        func = node.func
        if isinstance(func, ast.Name):
            if func.id == "Path":
                if "Path" not in imported:
                    return False
            elif func.id not in _MASK_SAFE_BUILTINS:
                return False
            if func.id == "open" and not (
                1 <= len(node.args) <= 2
                and all(
                    isinstance(arg, ast.Constant) and arg.value in {"r", "rb", "rt"}
                    for arg in node.args[1:]
                )
                and all(kw.arg in {"encoding", "errors"} for kw in node.keywords)
            ):
                return False
            continue
        if not isinstance(func, ast.Attribute):
            return False
        receiver = func.value
        if isinstance(receiver, ast.Name) and receiver.id in imported:
            if (receiver.id, func.attr) not in _MASK_SAFE_MODULE_CALLS:
                return False
            if (receiver.id, func.attr) == ("subprocess", "run") and not (
                len(node.args) == 1
                and isinstance(node.args[0], (ast.List, ast.Tuple))
                and all(
                    isinstance(item, ast.Constant) and isinstance(item.value, str)
                    for item in node.args[0].elts
                )
                and all(
                    kw.arg in _MASK_SUBPROCESS_KEYWORDS and isinstance(kw.value, ast.Constant)
                    for kw in node.keywords
                )
            ):
                return False
            continue
        if func.attr not in _MASK_SAFE_METHODS:
            return False
        root = receiver
        while isinstance(root, (ast.Attribute, ast.Subscript)):
            root = root.value
        if isinstance(root, ast.Name) and root.id in imported:
            return False
    return True


def _mask_read_only_python_paths(body: str) -> str:
    """Exclude literal paths read as diagnostic data, never executable input.

    Unknown Python expressions retain the conservative referenced-script scan.
    A path handed to os.system/subprocess remains visible to that scan.
    """
    if not body.isascii():  # AST columns are UTF-8 byte offsets; fail closed.
        return body
    try:
        tree = ast.parse(body)
    except SyntaxError:
        return body
    # Whole-body allowlist, not a deny-list of rebinding shapes: see
    # _python_body_is_mask_safe for why in-place mutation defeats deny-lists.
    if not _python_body_is_mask_safe(tree):
        return body
    has_path = any(
        isinstance(node, ast.ImportFrom) and node.module == "pathlib"
        and any(alias.name == "Path" and alias.asname is None for alias in node.names)
        for node in ast.walk(tree)
    )
    lines = body.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    def only_binding(name: str, predicate) -> bool:
        writes = [n for n in ast.walk(tree) if isinstance(n, ast.Name)
                  and n.id == name and isinstance(n.ctx, (ast.Store, ast.Del))]
        return len(writes) == 1 and isinstance(writes[0].ctx, ast.Store) and predicate(parents.get(writes[0]))

    open_shadowed = any(
        (isinstance(other, ast.Name) and other.id == "open" and isinstance(other.ctx, ast.Store))
        or (isinstance(other, (ast.FunctionDef, ast.ClassDef)) and other.name == "open")
        or (isinstance(other, ast.arg) and other.arg == "open")
        or (isinstance(other, (ast.Import, ast.ImportFrom)) and any(
            alias.asname == "open" or alias.name == "open" for alias in other.names
        )) for other in ast.walk(tree)
    )
    spans = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "open" and len(node.args) == 1
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and not open_shadowed):
            read_attr = parents.get(node)
            read_call = parents.get(read_attr) if read_attr is not None else None
            split_attr = parents.get(read_call) if read_call is not None else None
            split_call = parents.get(split_attr) if split_attr is not None else None
            reverse_call = parents.get(split_call) if split_call is not None else None
            loop = parents.get(reverse_call) if reverse_call is not None else None
            parsers = {
                target.id
                for statement in loop.body for assignment in ast.walk(statement)
                if isinstance(loop, ast.For) and isinstance(assignment, ast.Assign)
                and isinstance(assignment.value, ast.Call)
                and isinstance(assignment.value.func, ast.Attribute)
                and isinstance(assignment.value.func.value, ast.Name)
                and (assignment.value.func.value.id, assignment.value.func.attr) == ("json", "loads")
                for target in assignment.targets if isinstance(target, ast.Name)
            } if isinstance(loop, ast.For) else set()
            parser = next(iter(parsers)) if len(parsers) == 1 else None
            parser_safe = parser is not None and isinstance(loop, ast.For) and isinstance(loop.target, ast.Name) and only_binding(
                parser,
                lambda a: isinstance(a, ast.Assign) and isinstance(a.value, ast.Call)
                and isinstance(a.value.func, ast.Attribute)
                and isinstance(a.value.func.value, ast.Name)
                and (a.value.func.value.id, a.value.func.attr) == ("json", "loads")
                and len(a.targets) == 1 and len(a.value.args) == 1 and isinstance(a.value.args[0], ast.Name)
                and a.value.args[0].id == loop.target.id and not a.value.keywords
            )
            methods = {(c.func.value.id, c.func.attr) for stmt in loop.body
                       for c in ast.walk(stmt) if isinstance(c, ast.Call)
                       and isinstance(c.func, ast.Attribute)
                       and isinstance(c.func.value, ast.Name)} if isinstance(loop, ast.For) else set()
            containers_safe = all(
                only_binding(name, lambda a: isinstance(a, ast.Assign) and (
                    isinstance(a.value, ast.List) and not a.value.elts if name == "out" else
                    isinstance(a.value, ast.Call) and isinstance(a.value.func, ast.Name)
                    and a.value.func.id == "set" and not a.value.args and not a.value.keywords
                )) for name, method in (("seen", "add"), ("out", "append"))
                if (name, method) in methods
            )
            if (isinstance(read_attr, ast.Attribute) and read_attr.attr == "read"
                    and isinstance(read_call, ast.Call) and not read_call.args and not read_call.keywords
                    and isinstance(split_attr, ast.Attribute) and split_attr.attr == "splitlines"
                    and isinstance(split_call, ast.Call) and not split_call.args and not split_call.keywords
                    and isinstance(reverse_call, ast.Call) and isinstance(reverse_call.func, ast.Name)
                    and reverse_call.func.id == "reversed" and reverse_call.args == [split_call]
                    and isinstance(loop, ast.For) and loop.iter is reverse_call
                    and node.end_lineno is not None and node.end_col_offset is not None
                    and parser_safe and containers_safe
                    and all(
                        isinstance(call.func, ast.Name) and call.func.id in {"len", "print"}
                        or isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name)
                        and (call.func.value.id, call.func.attr) in {
                            ("json", "loads"), (parser, "get"),
                            ("seen", "add"), ("out", "append")
                        }
                        or isinstance(call.func, ast.Attribute) and call.func.attr == "lower"
                        and isinstance(call.func.value, ast.Subscript)
                        and isinstance(call.func.value.value, ast.Name)
                        and call.func.value.value.id in parsers
                        for statement in loop.body for call in ast.walk(statement)
                        if isinstance(call, ast.Call)
                    )):
                spans.append((offsets[node.lineno - 1] + node.col_offset,
                              offsets[node.end_lineno - 1] + node.end_col_offset))
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in {"read_text", "read_bytes"}:
            continue
        parent = parents.get(node)
        if not has_path or not (isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name)
                and parent.func.id == "print"):
            continue
        path_call = node.func.value
        if not (isinstance(path_call, ast.Call) and isinstance(path_call.func, ast.Name)
                and path_call.func.id == "Path" and len(path_call.args) == 1
                and isinstance(path_call.args[0], ast.Constant)
                and isinstance(path_call.args[0].value, str)
                and path_call.end_lineno is not None and path_call.end_col_offset is not None):
            continue
        start = offsets[path_call.lineno - 1] + path_call.col_offset
        end = offsets[path_call.end_lineno - 1] + path_call.end_col_offset
        spans.append((start, end))
    for start, end in sorted(spans, reverse=True):
        body = body[:start] + "read_only_path" + body[end:]
    return body


def _contains_unsafe_gateway_action(
    command: str,
    *,
    cwd: Optional[str],
    depth: int,
    visited: set[Path],
    budget: _LifecycleScanBudget,
    read_remote_script: Optional[_ReadRemoteScriptFn] = None,
    executed: bool = True,
) -> bool:
    """``executed=False`` means *command* is the content of a file that is only MENTIONED in inert
    (masked) text: it is still scanned for a literal lifecycle command, but "could not scan" (budget,
    depth, size, device, live SQLite, cloud) is "nothing to scan" there, never a block (#113944)."""
    # Charge BEFORE _direct_lifecycle_scan: every scan in it tokenizes with shlex.
    if not budget.charge_text(command):
        return _budget_exhausted(budget, "text", depth) if executed else False
    if _direct_lifecycle_scan(command):
        return True
    if depth >= _MAX_REFERENCED_SCRIPT_DEPTH:
        return executed

    from tools.shell_heredoc import (
        inert_python_heredoc_bodies,
        strip_inert_heredoc_bodies,
    )

    def recurse(text: str, cwd: Optional[str], executed: bool) -> bool:
        return _contains_unsafe_gateway_action(
            text,
            cwd=cwd,
            depth=depth + 1,
            visited=visited,
            budget=budget,
            read_remote_script=read_remote_script,
            executed=executed,
        )

    # Python stdin is executable Python source, but not a sequence of shell
    # commands. Scan its lifecycle-shaped calls like a .py file, then exclude
    # its path strings from the shell's referenced-script walk.
    python_bodies = inert_python_heredoc_bodies(command, semicolon_chain=True)
    for body in python_bodies:
        if _direct_lifecycle_scan(body):
            return True
    # The walks below must see the same masked view `_direct_lifecycle_scan` sees (#110422): a
    # path or `sh -c` payload inside a provably-inert heredoc body is never shell-executed, and an
    # oversized data file mentioned there otherwise fails closed as a "script".
    shell_command = strip_inert_heredoc_bodies(command, python_semicolon_chain=True)
    masked_bodies = [_mask_read_only_python_paths(body) for body in python_bodies]
    referenced_command = shell_command + "\n" + "\n".join(masked_bodies)

    for payload in _iter_shell_command_payloads(shell_command):
        if recurse(payload, cwd, executed):
            return True

    # Paths named only inside a masked body are still READ: an interpreter body that hands
    # `/x/restart.sh` to os.system() executes it. Only the fail-closed verdicts (cloud placeholder,
    # oversized/binary, budget) stay restricted to the executed view — a mere data mention must not
    # trip them. Executed candidates come first so a mention never starves a real script's budget.
    candidates = [
        (path, executed) for path in _iter_referenced_shell_scripts(referenced_command, cwd=cwd)
    ]
    if shell_command != command:
        # Fork: a path `_mask_read_only_python_paths` removed is a file the body provably only
        # READS as data (#1017/#1348) — its contents are never executed, so it is not even a
        # mention. Everything else named in the raw command stays a mention candidate.
        masked_away: set = set()
        for body, masked in zip(python_bodies, masked_bodies):
            if masked != body:
                masked_away |= (
                    set(_iter_referenced_shell_scripts(body, cwd=cwd))
                    - set(_iter_referenced_shell_scripts(masked, cwd=cwd))
                )
        candidates += [
            (path, False) for path in _iter_referenced_shell_scripts(command, cwd=cwd)
            if path not in masked_away
        ]

    for script_path, candidate_executed in candidates:
        # Do not touch a FileProvider path even to discover whether the file
        # is hydrated. The lexical check covers direct cloud paths; the
        # resolved check covers local launchers that are symlinks into
        # a cloud subtree. _read_referenced_script repeats both checks as the
        # shared choke point, so every caller stays covered even if this
        # walk-level short-circuit is bypassed.
        if _on_cloud_path(script_path):
            if candidate_executed:
                return _refuse_unreadable(
                    budget, script_path,
                    f"`{script_path}` lives on a cloud-synced path (iCloud Drive / "
                    "~/Library/CloudStorage) that the guard refuses to open",
                )
            continue
        resolved = _resolve_lenient(script_path)
        if resolved in visited:
            continue
        if not budget.charge_path():
            if candidate_executed:
                return _budget_exhausted(budget, "paths", depth)
            break  # remaining candidates are all mentions
        visited.add(resolved)
        # Never read more than the walk can still afford to tokenize; a file larger than the
        # remainder fails closed exactly like an oversized one.
        script_text, unsafe = _read_referenced_script(script_path, max_bytes=budget.bytes_remaining)
        if unsafe:
            if candidate_executed:
                return _refuse_unreadable(budget, script_path, _unreadable_reason(script_path))
            continue
        if script_text is None and read_remote_script is not None:
            # Local path missing; try the remote backend if one is available.
            # The callback's output crosses the same trust boundary as a
            # local read — sanitize it identically before it enters the
            # recursion (binary skip + size fail-closed).
            if not budget.charge_remote_read():
                if candidate_executed:
                    return _budget_exhausted(budget, "remote reads", depth)
                break
            script_text, unsafe = _sanitize_remote_script_text(
                read_remote_script(str(script_path)), max_bytes=budget.bytes_remaining
            )
            if unsafe:
                if candidate_executed:
                    return _refuse_unreadable(
                        budget, script_path,
                        f"`{script_path}` read from the backend exceeds the scan cap "
                        f"({_MAX_REFERENCED_SCRIPT_BYTES} bytes) or the remaining walk budget",
                    )
                continue
        if not script_text:
            continue
        # Relative references inside a script resolve against that script's
        # directory, not the original command's cwd.
        script_dir = _resolve_script_directory(str(resolved)) or cwd
        if recurse(script_text, script_dir, candidate_executed):
            return True
    return False


def scan_gateway_lifecycle(
    command: str,
    *,
    cwd: Optional[str] = None,
    read_remote_script: Optional[_ReadRemoteScriptFn] = None,
) -> tuple[bool, Optional[str]]:
    """``(unsafe, refusal)``: *refusal* names a non-lifecycle reason the walk failed closed (budget,
    size, device, live SQLite, cloud) so callers can tell the model; ``None`` when the verdict is a
    real lifecycle command or the command is allowed.

    Total by construction: this function returns a verdict for *every*
    input and never raises. The direct scans below are pure string
    operations; the referenced-script walk touches the filesystem, remote
    backends, and shlex on arbitrary decoded bytes, so it is best-effort
    defense-in-depth — any unexpected failure inside it is logged and
    treated as "walk found nothing" rather than crashing the caller.

    This is the contract #76762 established ("a guarded path must never
    crash the guard") enforced at the boundary instead of per-syscall: a
    guard crash propagates out of ``tools/terminal_tool.py`` and breaks
    every terminal command until the gateway restarts (#77780, #78256),
    which is strictly worse than either verdict.
    """
    budget = _LifecycleScanBudget()
    try:
        # Includes the direct regex/submit scans at depth 0.
        unsafe = _contains_unsafe_gateway_action(
            command,
            cwd=cwd,
            depth=0,
            visited=set(),
            budget=budget,
            read_remote_script=read_remote_script,
        )
        return unsafe, budget.refusal if unsafe else None
    except Exception:
        logger.warning(
            "lifecycle guard referenced-script walk failed; "
            "falling back to direct-scan verdict",
            exc_info=True,
        )
        # Pure string scans of the top-level command — cannot raise.
        try:
            return _direct_lifecycle_scan(command), None
        except Exception:
            # The data-argument masker tokenizes arbitrary text; if even
            # that fails, fall to the raw regex + submit scan so the guard
            # stays total.
            return (
                contains_gateway_lifecycle_command(command)
                or contains_launchctl_submit_command(command)
            ), None


def contains_gateway_lifecycle_command_or_referenced_script(
    command: str,
    *,
    cwd: Optional[str] = None,
    read_remote_script: Optional[_ReadRemoteScriptFn] = None,
) -> bool:
    """Detect lifecycle/submit commands, including bounded nested scripts (see
    ``scan_gateway_lifecycle`` for the contract)."""
    return scan_gateway_lifecycle(command, cwd=cwd, read_remote_script=read_remote_script)[0]


def _resolve_script_path(script_path: str) -> Optional[Path]:
    """Resolve a cron ``script`` value the same way the scheduler does.

    The scheduler (``cron.scheduler``) resolves a bare/relative script path
    under ``<HERMES_HOME>/scripts/`` and only accepts absolute paths as-is.
    We MUST mirror that here so the guard scans the file that will actually
    run — otherwise a job whose script lives at the scheduler's real location
    (``~/.hermes/scripts/restart.sh``) but is passed as the bare name
    ``restart.sh`` would read as a nonexistent relative path and silently
    scan prompt-only content, letting the command through.

    Returns ``None`` for values that cannot be a real path (NUL bytes,
    unexpandable ``~``) — the same ingestion contract as
    ``_expand_candidate_path``; such a value can never name a file the
    scheduler would execute, so there is nothing to scan.
    """
    from hermes_constants import get_hermes_home

    raw = _expand_candidate_path(script_path)
    if raw is None:
        return None
    if raw.is_absolute():
        return raw
    try:
        return get_hermes_home() / "scripts" / raw
    except (RuntimeError, OSError):
        # get_hermes_home() falls back to Path.home(), which raises when
        # neither HERMES_HOME nor HOME is resolvable (launchd/systemd
        # environments) — same ingestion contract: nothing to scan.
        return None


def _read_script_for_scanning(script_path: str) -> tuple[str, Optional[str]]:
    """``(text, refusal)``: read a cron script with the bounded terminal-script scanner.

    Non-regular/oversized/live-SQLite inputs fail closed with a NAMED *refusal*
    (never a lifecycle-shaped verdict); missing/unreadable/unresolvable paths
    remain empty so ordinary scheduler path validation can report them.
    """
    resolved = _resolve_script_path(script_path)
    if resolved is None:
        return "", None
    script_text, unsafe = _read_referenced_script(resolved)
    if unsafe:
        return "", _unreadable_reason(resolved)
    return script_text or "", None


def check_gateway_lifecycle(
    prompt: Optional[str],
    script: Optional[str] = None,
) -> None:
    """Raise ``GatewayLifecycleBlocked`` if *prompt* or *script* contains a
    gateway-lifecycle command pattern.

    ``prompt`` is scanned directly.  ``script``, when supplied, is read from
    disk and concatenated for the scan.  Both are considered together so a
    job cannot slip through by splitting the command across the prompt and
    the script.

    Callers should let the exception propagate when they want the create to
    fail with a ``ValueError``-shaped error (the agent's ``cronjob`` tool
    surfaces this as a tool error; the CLI prints it in red and exits 1).
    """
    combined = prompt or ""
    python_script = False
    refusal: Optional[str] = None
    if script:
        resolved_script = _resolve_script_path(script)
        if resolved_script is not None and _on_cloud_path(resolved_script):
            # Attribute the refusal correctly: the script is not known to
            # contain a lifecycle command — it lives on a cloud-synced
            # FileProvider path (iCloud Drive / ~/Library/CloudStorage)
            # that the guard refuses to open because an evicted
            # placeholder can hang preflight indefinitely (#88052).
            # Fail closed with the real reason instead of implying a
            # dangerous lifecycle command.
            raise GatewayLifecycleBlocked(
                "Blocked: the cron script lives on a cloud-synced path "
                "(iCloud Drive / ~/Library/CloudStorage). Opening an "
                "evicted FileProvider placeholder can hang the guard's "
                "preflight scan indefinitely, so it is refused without "
                "being read. Move the script to a local, non-cloud path "
                "(e.g. ~/.hermes/scripts/) and recreate the job."
            )
        python_script = resolved_script is not None and resolved_script.suffix == ".py"
        script_text, refusal = _read_script_for_scanning(script)
        if script_text:
            combined = f"{combined}\n{script_text}"

    if refusal:
        unsafe = True
    elif python_script:
        # Python is executed by the interpreter, never through a POSIX
        # shell: the shell-script reference walk is a false-positive
        # generator on Python sources (pathlib's "/" operator resolves to
        # the filesystem root and trips the regular-file check, blocking
        # every innocent .py cron script, #77131). The direct command
        # regex below still scans the full text, so a literal
        # `hermes gateway restart` embedded in a .py script is still
        # blocked. Non-regular/oversized script files fail closed above
        # (named refusal). The data-exemption masker tokenizes with shlex,
        # so it is charged against the walk budget (#78398).
        budget = _LifecycleScanBudget()
        if not budget.charge_text(combined):
            unsafe = _budget_exhausted(budget, "text", 0)
            refusal = budget.refusal
        else:
            unsafe = _lifecycle_command_scan_with_data_exemption(combined)
    else:
        script_dir = _resolve_script_directory(script) if script else None
        unsafe, refusal = scan_gateway_lifecycle(
            combined,
            cwd=script_dir,
        )
    if unsafe and refusal:
        raise GatewayLifecycleBlocked(
            f"Blocked: the lifecycle guard could not scan this cron job or referenced script: {refusal}. "
            "Nothing in the job is known to contain a gateway lifecycle command, but a script "
            "the job executes must be scannable (a regular text file under 1 MiB) before it can run."
        )
    if unsafe:
        raise GatewayLifecycleBlocked(
            "Blocked: cron job contains a gateway lifecycle command or persistent "
            "launchctl submit operation. This is blocked to prevent agent-driven "
            "SIGTERM-respawn loops under launchd/systemd supervision "
            "(#30719). Run `hermes gateway restart` from a shell outside "
            "the running gateway instead."
        )
