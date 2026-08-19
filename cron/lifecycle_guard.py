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
stop|restart`` separately refuse to self-target from inside the gateway.
Blocking cron specs at creation time as well means the agent gets an immediate,
informative rejection instead of scheduling a job that will only fail
(silently) when it fires.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import stat
from pathlib import Path
from typing import Callable, Iterator, Optional

logger = logging.getLogger(__name__)


class GatewayLifecycleBlocked(ValueError):
    """Raised when a cron job spec contains a gateway-lifecycle command."""


# Shell-level command shapes that target the gateway lifecycle. Each branch
# is anchored on a concrete command identifier so a match can only fire on
# actual shell-command-shaped strings, not on prose.
_GATEWAY_LIFECYCLE_PATTERN = re.compile(
    r"(?i)"
    # Branch A: `hermes gateway restart|stop` — the canonical foot-gun.
    # `start` is intentionally excluded: starting a gateway from inside a
    # gateway is benign (a no-op or "already running" error), and a
    # legitimate cron job might start a sibling profile's gateway.
    r"(?:hermes\s+gateway\s+(?:restart|stop))"
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
    r"|(?:launchctl\s+(?:kickstart|unload|load|stop|restart|submit|bootstrap)\b[^\n]*\bhermes[.\-]?gateway)"
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
    r"|(?:\b(?:pkill|kill)\b[^\n;|&]*\bhermes\b[^\n;|&]*\bgateway)"
    r"|(?:\b(?:pkill|kill)\b[^\n;|&]*\bgateway\b[^\n;|&]*\bhermes)"
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


def _match_is_ssh_remote(text: str, match_start: int) -> bool:
    """Return True if the lifecycle match at *match_start* sits inside an
    ssh invocation targeting a non-loopback host."""
    line_start = text.rfind("\n", 0, match_start) + 1
    prefix = text[line_start:match_start]
    # The command context for the match is the last shell segment before it.
    segment = _SEGMENT_SPLIT_RE.split(prefix)[-1]
    if not _SSH_COMMAND_RE.search(segment):
        return False
    if _LOOPBACK_HOST_RE.search(segment):
        return False
    return True


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


def contains_gateway_lifecycle_command(text: str) -> bool:
    """Return True if *text* contains a gateway lifecycle command pattern.

    Matches that are ssh-wrapped to a remote (non-loopback) host are
    exempt — restarting a *different* machine's gateway is legitimate fleet
    maintenance and cannot SIGTERM-loop this process (see
    ``_match_is_ssh_remote``).
    """
    if not text:
        return False
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
        return True
    return False


_SHELL_EXECUTABLES = frozenset({"sh", "bash", "dash", "ksh", "zsh"})
_SHELL_OPTIONS_WITH_VALUES = frozenset({"-O", "+O", "-o", "+o"})
_MAX_REFERENCED_SCRIPT_BYTES = 1024 * 1024
_MAX_REFERENCED_SCRIPT_DEPTH = 8
_CONTROL_CHARS = frozenset(";&|()")

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


def _iter_command_segments(command: str) -> Iterator[list[str]]:
    """Yield shell-tokenized command segments, honoring quotes and comments."""
    normalized = command.replace("\\\n", "")
    for line in normalized.splitlines() or [normalized]:
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


def _command_token_index(segment: list[str]) -> Optional[int]:
    """Return the executable token index after simple env assignments."""
    for index, token in enumerate(segment):
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
            continue
        return index
    return None


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
        if Path(segment[index]).name == "launchctl":
            arguments = segment[index + 1 :]
            if arguments and arguments[0].lower() in {"submit", "bootstrap"}:
                return True
    return False


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
                    argument.startswith(".")
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
    """
    effective_cwd = cwd
    for segment in _iter_command_segments(command):
        index = _command_token_index(segment)
        if index is None:
            continue
        executable = segment[index]
        executable_name = Path(executable).name

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

        if executable_name in {".", "source"}:
            if len(segment) > index + 1:
                resolved = _resolve_terminal_script_path(
                    segment[index + 1], effective_cwd
                )
                if resolved is not None:
                    yield resolved
            continue

        if executable_name in _SHELL_EXECUTABLES:
            arguments = segment[index + 1 :]
            arg_index = 0
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
                if argument.startswith("-"):
                    arg_index += 1
                    continue
                break
            if arg_index < len(arguments) and arguments[arg_index] not in {
                "-c",
                "--command",
            }:
                resolved = _resolve_terminal_script_path(
                    arguments[arg_index], effective_cwd
                )
                if resolved is not None:
                    yield resolved
            continue

        # A bare "/" token is pathlib's division operator in Python sources
        # (e.g. `Path.home() / ".hermes"`), not an executable reference.
        # Resolving it walks to the filesystem root and fails the
        # regular-file check below, hard-blocking innocent .py scripts
        # (#77131). Skip pure-separator tokens.
        if executable.strip("/"):
            if "/" in executable or executable.endswith((".sh", ".bash", ".zsh")):
                resolved = _resolve_terminal_script_path(executable, effective_cwd)
                if resolved is not None:
                    yield resolved


def _iter_shell_command_payloads(command: str) -> Iterator[str]:
    """Yield code passed through ``sh|bash|... -c`` for recursive scanning."""
    for segment in _iter_command_segments(command):
        index = _command_token_index(segment)
        if index is None or Path(segment[index]).name not in _SHELL_EXECUTABLES:
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


def _read_referenced_script(path: Path) -> tuple[Optional[str], bool]:
    """Return ``(text, unsafe)`` using bounded, regular-file-only reads."""
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
        if not stat.S_ISREG(metadata.st_mode):
            return None, True
        # Sniff a small prefix first: files that are clearly compiled
        # binaries (executable magic, or NUL bytes in the head) are never
        # shell scripts, so skip them WITHOUT reading the rest — reading a
        # megabyte of machine code just to discard it wastes the guard's
        # budget and (pre-#77703) fed decoded garbage into the recursion.
        data = os.read(descriptor, _BINARY_SNIFF_BYTES)
        if data.startswith(_BINARY_MAGIC_PREFIXES) or b"\x00" in data:
            return None, False
        # Read the remainder (bounded). Loop because os.read may return
        # short for non-regular-file-backed descriptors.
        while len(data) <= _MAX_REFERENCED_SCRIPT_BYTES:
            chunk = os.read(
                descriptor, _MAX_REFERENCED_SCRIPT_BYTES + 1 - len(data)
            )
            if not chunk:
                break
            data += chunk
    except OSError:
        return None, False
    finally:
        os.close(descriptor)
    # A NUL byte in the first chunk means this is a binary (ELF/Mach-O/
    # PE), not a shell script — scanning its decoded contents would
    # tokenize machine code and feed junk paths into the recursion
    # (including a `ValueError: embedded null byte` from Path.resolve,
    # #76762). Treat it as "nothing to scan" rather than unsafe: a binary
    # executed by the user is not a referenced *shell script*.
    if b"\x00" in data:
        return None, False
    if len(data) > _MAX_REFERENCED_SCRIPT_BYTES:
        return None, True
    return data.decode("utf-8", errors="replace"), False


def _sanitize_remote_script_text(text: Optional[str]) -> tuple[Optional[str], bool]:
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
    if len(text.encode("utf-8", errors="replace")) > _MAX_REFERENCED_SCRIPT_BYTES:
        return None, True
    return text, False


def _contains_unsafe_gateway_action(
    command: str,
    *,
    cwd: Optional[str],
    depth: int,
    visited: set[Path],
    read_remote_script: Optional[_ReadRemoteScriptFn] = None,
) -> bool:
    if _direct_lifecycle_scan(command):
        return True
    if depth >= _MAX_REFERENCED_SCRIPT_DEPTH:
        return True

    for payload in _iter_shell_command_payloads(command):
        if _contains_unsafe_gateway_action(
            payload,
            cwd=cwd,
            depth=depth + 1,
            visited=visited,
            read_remote_script=read_remote_script,
        ):
            return True

    for script_path in _iter_referenced_shell_scripts(command, cwd=cwd):
        try:
            resolved = script_path.resolve(strict=False)
        except (OSError, ValueError):
            # OSError: unreadable/long paths. ValueError: embedded NUL byte
            # from a binary's decoded contents tokenized as a path — a
            # guarded path must never crash the guard (#76762).
            resolved = script_path
        if resolved in visited:
            continue
        visited.add(resolved)
        script_text, unsafe = _read_referenced_script(script_path)
        if unsafe:
            return True
        if script_text is None and read_remote_script is not None:
            # Local path missing; try the remote backend if one is available.
            # The callback's output crosses the same trust boundary as a
            # local read — sanitize it identically before it enters the
            # recursion (binary skip + size fail-closed).
            script_text, unsafe = _sanitize_remote_script_text(
                read_remote_script(str(script_path))
            )
            if unsafe:
                return True
        if not script_text:
            continue
        # Relative references inside a script resolve against that script's
        # directory, not the original command's cwd.
        script_dir = _resolve_script_directory(str(resolved)) or cwd
        if _contains_unsafe_gateway_action(
            script_text,
            cwd=script_dir,
            depth=depth + 1,
            visited=visited,
            read_remote_script=read_remote_script,
        ):
            return True
    return False


def contains_gateway_lifecycle_command_or_referenced_script(
    command: str,
    *,
    cwd: Optional[str] = None,
    read_remote_script: Optional[_ReadRemoteScriptFn] = None,
) -> bool:
    """Detect lifecycle/submit commands, including bounded nested scripts.

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
    try:
        # Includes the direct regex/submit scans at depth 0.
        return _contains_unsafe_gateway_action(
            command,
            cwd=cwd,
            depth=0,
            visited=set(),
            read_remote_script=read_remote_script,
        )
    except Exception:
        logger.warning(
            "lifecycle guard referenced-script walk failed; "
            "falling back to direct-scan verdict",
            exc_info=True,
        )
        # Pure string scans of the top-level command — cannot raise.
        try:
            return _direct_lifecycle_scan(command)
        except Exception:
            # The data-argument masker tokenizes arbitrary text; if even
            # that fails, fall to the raw regex + submit scan so the guard
            # stays total.
            return contains_gateway_lifecycle_command(
                command
            ) or contains_launchctl_submit_command(command)




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


def _read_script_for_scanning(script_path: str) -> str:
    """Read a cron script with the bounded terminal-script scanner.

    Non-regular or oversized inputs fail closed by returning a lifecycle-shaped
    sentinel, while missing/unreadable/unresolvable paths remain empty so
    ordinary scheduler path validation can report them.
    """
    resolved = _resolve_script_path(script_path)
    if resolved is None:
        return ""
    script_text, unsafe = _read_referenced_script(resolved)
    if unsafe:
        return "hermes gateway restart"
    return script_text or ""


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
    if script:
        resolved_script = _resolve_script_path(script)
        python_script = resolved_script is not None and resolved_script.suffix == ".py"
        script_text = _read_script_for_scanning(script)
        if script_text:
            combined = f"{combined}\n{script_text}"

    if python_script:
        # Python is executed by the interpreter, never through a POSIX
        # shell: the shell-script reference walk is a false-positive
        # generator on Python sources (pathlib's "/" operator resolves to
        # the filesystem root and trips the regular-file check, blocking
        # every innocent .py cron script, #77131). The direct command
        # regex below still scans the full text, so a literal
        # `hermes gateway restart` embedded in a .py script is still
        # blocked. Non-regular/oversized script files still fail closed
        # via the lifecycle-shaped sentinel in _read_script_for_scanning.
        unsafe = _lifecycle_command_scan_with_data_exemption(combined)
    else:
        script_dir = _resolve_script_directory(script) if script else None
        unsafe = contains_gateway_lifecycle_command_or_referenced_script(
            combined,
            cwd=script_dir,
        )
    if unsafe:
        raise GatewayLifecycleBlocked(
            "Blocked: cron job contains a gateway lifecycle command or persistent "
            "launchctl submit operation. This is blocked to prevent agent-driven "
            "SIGTERM-respawn loops under launchd/systemd supervision "
            "(#30719). Run `hermes gateway restart` from a shell outside "
            "the running gateway instead."
        )
