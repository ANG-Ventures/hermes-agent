"""Refuse ``pkill``/``pgrep`` with an option AFTER the pattern (t_c6d2e782).

BSD ``pkill``/``pgrep`` (macOS) stop option parsing at the first operand, so in
``pkill -f '^eslogger exec$' -P 60886`` the ``-P`` and ``60886`` become two MORE
patterns. Under ``-f`` the pattern ``-P`` matches every process whose command
line contains ``-P``: every kanban worker (``python -P -m hermes_cli.main``) and
every terminal-tool shell (``pwd -P`` in its wrapper). On 2026-11-17 a worker
ran exactly that under ``sudo`` on the Mac Studio twice (11:06:16, 20:57:03) and
SIGTERM'd 12 and 17 in-flight workers. GNU procps permutes argv, so the same
line is harmless on Linux, which is why it reads as correct.

Static text check only: it never runs anything. Options-first spelling
(``pkill -P 60886 -f PATTERN``) and ``--`` before a dash-leading pattern pass.
"""

from __future__ import annotations

import os
import shlex
from typing import Iterable, Optional

BLOCK_MARKER = "pkill_argorder_guard"

_TOOLS = frozenset({"pkill", "pgrep"})
# BSD pgrep/pkill options that take a value (``pgrep(1)`` on macOS; GNU adds none
# that matter here): the value is not an operand.
_VALUE_OPTS = frozenset("FGMNPUcgstuJ")
# Command prefixes that keep the next word at command position, with the options of
# each that consume the following token (``sudo -n`` takes none, ``nice -n`` does).
_WRAPPERS: dict[str, frozenset] = {
    "sudo": frozenset({"-u", "-g", "-C", "-h", "-p", "-U", "-r", "-t", "-D", "-R", "-T"}),
    "doas": frozenset({"-u", "-C"}),
    "env": frozenset({"-u", "-C", "-P", "-S"}),
    "nice": frozenset({"-n"}),
    "timeout": frozenset({"-s", "-k"}),
    "xargs": frozenset({"-n", "-I", "-L", "-P", "-s", "-E", "-J", "-R", "-S"}),
    "taskpolicy": frozenset({"-c", "-d", "-g", "-t", "-l", "-p"}),
    "command": frozenset(), "exec": frozenset(), "nohup": frozenset(), "time": frozenset(),
    "caffeinate": frozenset({"-t", "-w"}),
}
_SEPARATORS = frozenset({";", "&", "&&", "|", "||", "|&", "(", ")", "`", "!", "{", "}", "do", "then",
                         "else", "elif"})
_MAX_DEPTH = 4
_MAX_TEXT = 256 * 1024


def _tokens(line: str) -> list[str]:
    lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:  # unbalanced quote: fall back to a plain split
        return line.split()


def _misordered_args(args: Iterable[str]) -> Optional[str]:
    """The first option that follows an operand, else None."""
    operand_seen = False
    it = iter(args)
    for arg in it:
        if arg in _SEPARATORS or arg.startswith(("$(", "`")):
            return None
        if arg == "--":
            return None
        if arg.startswith("-") and len(arg) > 1:
            if operand_seen:
                return arg
            cluster = arg[1:]
            for i, ch in enumerate(cluster):
                if ch in _VALUE_OPTS:
                    if i == len(cluster) - 1:
                        next(it, None)  # value is the next token
                    break
            continue
        if arg == "$":  # ``$(`` split by punctuation_chars: the substitution ends the argv we can see
            return None
        operand_seen = True
    return None


def _scan_line(line: str, depth: int) -> Optional[str]:
    toks = _tokens(line)
    at_cmd = True
    skip_value = False
    wrapper_opts: frozenset = frozenset()
    for i, tok in enumerate(toks):
        if depth < _MAX_DEPTH and any(c.isspace() for c in tok):
            hit = find_misordered_pkill(tok, _depth=depth + 1)
            if hit:
                return hit
        if tok in _SEPARATORS:
            at_cmd, skip_value, wrapper_opts = True, False, frozenset()
            continue
        if not at_cmd:
            continue
        if skip_value:
            skip_value = False
            continue
        base = os.path.basename(tok)
        if base in _TOOLS:
            bad = _misordered_args(toks[i + 1:])
            if bad:
                tail = " ".join(toks[i:i + 8])
                return f"{tail!r} (option {bad!r} comes after the pattern)"
            at_cmd = False
            continue
        if base in _WRAPPERS:
            wrapper_opts = _WRAPPERS[base]
            continue  # still at command position
        if "=" in tok and not tok.startswith("-"):
            continue  # VAR=value prefix
        if tok.startswith("-") or tok.isdigit():
            skip_value = tok in wrapper_opts
            continue  # a wrapper's own option / numeric arg (timeout 30, nice -n 5)
        at_cmd = False
    return None


def find_misordered_pkill(text: str, *, _depth: int = 0) -> Optional[str]:
    """Describe the first ``pkill``/``pgrep`` call in ``text`` with an option after its pattern.

    ``text`` is a shell command or a whole script. Quoted payloads (``ssh host "..."``,
    ``bash -c '...'``) are scanned too. Returns None when nothing is misordered.
    """
    if not text or len(text) > _MAX_TEXT or ("pkill" not in text and "pgrep" not in text):
        return None
    for line in text.replace("\\\n", " ").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "pkill" not in stripped and "pgrep" not in stripped:
            continue
        hit = _scan_line(stripped, _depth)
        if hit:
            return hit
    return None


def refusal_message(hit: str) -> str:
    return (
        f"Blocked: {hit}. BSD pkill/pgrep (macOS) stop reading options at the first pattern, so "
        "every later word is ANOTHER pattern: with -f, '-P' alone matches every kanban worker "
        "(python -P) and every terminal-tool shell (pwd -P). This exact line SIGTERM'd 12 and 17 "
        "in-flight workers on the Studio on 2026-11-17 (t_c6d2e782). Put every option before "
        "the pattern, e.g. `pkill -P 60886 -f '^/usr/bin/eslogger exec$'`, or use `kill <pid>`."
    )
