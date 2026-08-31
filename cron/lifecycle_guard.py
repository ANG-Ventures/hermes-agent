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
    r"(?:(?<![/\w.\-])hermes\s+gateway\s+(?:restart|stop|uninstall)\b)"
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
    # Any global flags before the profile selector (each may carry a value).
    r"(?:-{1,2}\S+(?:\s+\S+)?\s+)*"
    # The selector itself: `--profile=<name>` or the space-separated
    # `-p <name>` / `--profile <name>` — exactly the shapes the CLI's
    # `_apply_profile_override` accepts.
    r"(?:--profile=([^\s]+)|(?:-p|--profile)\s+([^\s]+))"
    # Any global flags between the selector and the subcommand.
    r"(?:\s+-{1,2}\S+(?:\s+\S+)?)*"
    r"\s+gateway\s+(?:restart|stop)"
)


def _current_profile_name() -> Optional[str]:
    """Return the name of the profile running the guard, if determinable.

    Prefers the explicit ``HERMES_PROFILE_NAME`` / ``HERMES_PROFILE`` env
    (set by the profile launcher and kanban worker spawns), falling back to
    ``hermes_cli.profiles.get_active_profile_name`` (derived from
    ``HERMES_HOME``, which the gateway process inherits from its launch
    profile). Returns ``None`` when neither source yields a name.
    """
    for env_name in ("HERMES_PROFILE_NAME", "HERMES_PROFILE"):
        value = os.environ.get(env_name)
        if value and value.strip():
            return value.strip()
    try:
        from hermes_cli.profiles import get_active_profile_name

        return get_active_profile_name() or None
    except Exception:
        return None


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
    for _line in normalized.splitlines() or [normalized]:
        if not _contains_launchctl_gateway_lifecycle(_line):
            continue
        if _SSH_COMMAND_RE.search(_line) and not _LOOPBACK_HOST_RE.search(_line):
            continue
        return True
    return False


_SHELL_EXECUTABLES = frozenset({"sh", "bash", "dash", "ksh", "zsh"})
_SHELL_OPTIONS_WITH_VALUES = frozenset({"-O", "+O", "-o", "+o"})
_MAX_REFERENCED_SCRIPT_BYTES = 1024 * 1024
_MAX_REFERENCED_SCRIPT_DEPTH = 8
_CONTROL_CHARS = frozenset(";&|()")


# Directory names that sit directly under a `Library` path component and
# mark a FileProvider-backed subtree: `Mobile Documents` is iCloud Drive;
# `CloudStorage` hosts every third-party FileProvider domain (Dropbox,
# OneDrive, Google Drive, Box, ...) on modern macOS.
_CLOUD_PLACEHOLDER_MARKERS = frozenset({"Mobile Documents", "CloudStorage"})


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
            if resolved is not None:
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


def _read_referenced_script(path: Path) -> tuple[Optional[str], bool]:
    """Return ``(text, unsafe)`` using bounded, regular-file-only reads.

    This is the shared choke point for every local script read the guard
    performs (the terminal walk in ``_contains_unsafe_gateway_action`` AND
    the cron-script scan in ``_read_script_for_scanning``), so the
    cloud-placeholder refusal lives here: a FileProvider path must never be
    opened — not even to discover whether the file is hydrated — because an
    evicted placeholder's ``open()`` can hang preflight indefinitely
    (#88052). The lexical check covers direct cloud paths; the resolved
    check covers local launchers that are symlinks into a cloud subtree.
    """
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
    if len(data) > _MAX_REFERENCED_SCRIPT_BYTES:
        return None, True
    if b"\x00" in data:
        data = data.replace(b"\x00", b"")
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
        # Do not touch a FileProvider path even to discover whether the file
        # is hydrated. The lexical check covers direct cloud paths; the
        # resolved check below covers local launchers that are symlinks into
        # a cloud subtree. _read_referenced_script repeats both checks as the
        # shared choke point, so every caller stays covered even if this
        # walk-level short-circuit is bypassed.
        if _is_cloud_placeholder_path(script_path):
            return True
        try:
            resolved = script_path.resolve(strict=False)
        except (OSError, ValueError):
            # OSError: unreadable/long paths. ValueError: embedded NUL byte
            # from a binary's decoded contents tokenized as a path — a
            # guarded path must never crash the guard (#76762).
            resolved = script_path
        if _is_cloud_placeholder_path(resolved):
            return True
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
        if resolved_script is not None:
            try:
                real_script = resolved_script.resolve(strict=False)
            except (OSError, ValueError):
                real_script = resolved_script
            if _is_cloud_placeholder_path(resolved_script) or _is_cloud_placeholder_path(
                real_script
            ):
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
