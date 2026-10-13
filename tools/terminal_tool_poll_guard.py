"""Foreground polling-loop guard for messaging-gateway terminal calls.

The guard decides on the PARSED command, never on a substring. The old
version regex-blanked quoted spans and then grepped for ``watch``/``for``/
``sleep``. One apostrophe in a heredoc body (``the view's``) shifted every later
quote pairing, so review prose read as code (refused as "watch"), and a loop
inside ``(nohup bash -c '...' &)`` read as top level (t_2c070599).

What this module does instead:

* tokenises the command the way the shell does: quotes, backslashes, ``$(...)``
  and backticks, comments, redirections and heredoc bodies;
* treats arguments, quoted strings and heredoc bodies as DATA (a heredoc body
  is code only when its owner is a shell: bash/sh/zsh/dash/ksh/ssh);
* matches ``watch``, ``tail -f`` and ``sleep`` only in COMMAND position;
* decides by job control: anything in an and-or list ended by ``&`` (a
  backgrounded loop, ``( ... &)``, ``nohup ... &``) is not foreground.

It is a behavioural guardrail, not a sandbox. A command it cannot parse is a
shell syntax error that fails fast, so it is allowed. The foreground timeout
cap is the hard backstop for shapes it cannot see (script files, quoted
``bash -c`` bodies, python sleep loops).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

# A loop whose body sleeps >= this many seconds is a wait, not work; in a chat
# session it keeps the conversation deaf for the whole wait.
POLL_LOOP_MIN_SLEEP_S = 30

_SLEEP_ARG_RE = re.compile(r"^(\d+(?:\.\d+)?)([smhd]?)$", re.IGNORECASE)
_SLEEP_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_TAIL_FOLLOW_RE = re.compile(r"^(?:--follow(?:=.*)?|-[A-Za-z0-9]*[fF][A-Za-z0-9]*)$")
# Prefix words that run the NEXT word as the command.
_WRAPPERS = frozenset({"command", "builtin", "exec", "nohup", "setsid", "env", "time", "nice"})
# Heredoc owners that execute the body as shell code.
_SHELL_STDIN_CONSUMERS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "ssh"})
_LOOP_OPENERS = frozenset({"for", "while", "until"})
_CLOSERS = {"done": "loop", "fi": "if", "esac": "case", "}": "brace"}
_PREFIX_KEYWORDS = frozenset({"do", "then", "elif", "else", "!"})
_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
_OPERATOR_CHARS = ";&|()\n"
_OPERATORS = (";;&", ";;", ";&", "&&", "||", "|&", ";", "&", "|", "(", ")", "\n")
_REDIRECTS = ("&>>", "<<<", "<<-", "&>", ">>", ">&", ">|", "<<", "<&", "<>", ">", "<")


class _ParseError(Exception):
    pass


@dataclass
class _Word:
    text: str
    quoted: bool = False
    subs: list = field(default_factory=list)  # executed $(...) / `...` bodies


@dataclass
class _Op:
    text: str


@dataclass
class _Redir:
    text: str
    target: _Word | None = None
    heredoc_body: str | None = None


def _scan_balanced(src: str, i: int) -> int:
    """``src[i]`` follows an opening ``(``; return the index of its matching ``)``."""
    depth = 1
    while i < len(src):
        c = src[i]
        if c == "\\":
            i += 2
            continue
        if c == "'":
            end = src.find("'", i + 1)
            if end < 0:
                raise _ParseError("unterminated quote")
            i = end + 1
            continue
        if c == '"':
            i = _scan_double(src, i + 1, None)
            continue
        if c == "`":
            end = src.find("`", i + 1)
            if end < 0:
                raise _ParseError("unterminated backtick")
            i = end + 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise _ParseError("unterminated substitution")


def _scan_double(src: str, i: int, word: _Word | None) -> int:
    """Scan a double-quoted span starting after ``"``; collect substitutions."""
    while i < len(src):
        c = src[i]
        if c == "\\":
            if word is not None and i + 1 < len(src):
                word.text += src[i + 1]
            i += 2
            continue
        if c == '"':
            return i + 1
        if src.startswith("$(", i):
            end = _scan_balanced(src, i + 2)
            if word is not None and not src.startswith("$((", i):
                word.subs.append(src[i + 2:end])
            i = end + 1
            continue
        if c == "`":
            end = src.find("`", i + 1)
            if end < 0:
                raise _ParseError("unterminated backtick")
            if word is not None:
                word.subs.append(src[i + 1:end])
            i = end + 1
            continue
        if word is not None:
            word.text += c
        i += 1
    raise _ParseError("unterminated double quote")


def _read_heredoc(src: str, i: int, delimiter: str, strip_tabs: bool) -> tuple[str, int]:
    """Return ``(body, index after the terminator line)`` for a body starting at ``i``."""
    lines = []
    while i <= len(src):
        nl = src.find("\n", i)
        end = len(src) if nl < 0 else nl
        line = src[i:end]
        if (line.lstrip("\t") if strip_tabs else line) == delimiter:
            return "\n".join(lines), end + 1
        if nl < 0:
            raise _ParseError("unterminated heredoc")
        lines.append(line)
        i = nl + 1
    raise _ParseError("unterminated heredoc")


def _tokenise(src: str) -> list:
    """Shell-tokenise ``src`` into words, operators and redirections."""
    tokens: list = []
    pending_heredocs: list[_Redir] = []
    expect_target: _Redir | None = None
    word: _Word | None = None
    i = 0

    def flush():
        nonlocal word, expect_target
        if word is None:
            return
        if expect_target is not None:
            expect_target.target = word
            if expect_target.text in ("<<", "<<-"):
                pending_heredocs.append(expect_target)
            expect_target = None
        else:
            tokens.append(word)
        word = None

    while i < len(src):
        c = src[i]
        if c == "\\":
            if src.startswith("\\\n", i):
                i += 2
                continue
            word = word or _Word("")
            word.quoted = True
            word.text += src[i + 1:i + 2]
            i += 2
            continue
        if c == "'":
            end = src.find("'", i + 1)
            if end < 0:
                raise _ParseError("unterminated quote")
            word = word or _Word("")
            word.quoted = True
            word.text += src[i + 1:end]
            i = end + 1
            continue
        if c == '"':
            word = word or _Word("")
            word.quoted = True
            i = _scan_double(src, i + 1, word)
            continue
        if c == "`" or src.startswith("$(", i):
            word = word or _Word("")
            if c == "`":
                end = src.find("`", i + 1)
                if end < 0:
                    raise _ParseError("unterminated backtick")
                word.subs.append(src[i + 1:end])
            else:
                end = _scan_balanced(src, i + 2)
                if not src.startswith("$((", i):
                    word.subs.append(src[i + 2:end])
            word.text += src[i:end + 1]
            i = end + 1
            continue
        if c in " \t\r":
            flush()
            i += 1
            continue
        if c == "#" and word is None:
            nl = src.find("\n", i)
            i = len(src) if nl < 0 else nl
            continue
        if c in "<>" and src.startswith("(", i + 1):  # process substitution
            flush()
            end = _scan_balanced(src, i + 2)
            tokens.append(_Word(src[i:end + 1], subs=[src[i + 2:end]]))
            i = end + 1
            continue
        redirect = next((r for r in _REDIRECTS if src.startswith(r, i)), None)
        if redirect and (c != "&" or redirect.startswith("&>")):
            if word is not None and word.text.isdigit() and not word.quoted:
                word = None  # io-number (2>&1), not an argument
            flush()
            if expect_target is not None:
                raise _ParseError("redirect without target")
            redir = _Redir(redirect)
            tokens.append(redir)
            expect_target = redir
            i += len(redirect)
            continue
        if c in _OPERATOR_CHARS:
            flush()
            if expect_target is not None:
                raise _ParseError("redirect without target")
            op = next(o for o in _OPERATORS if src.startswith(o, i))
            tokens.append(_Op(op))
            i += len(op)
            if op == "\n":
                for redir in pending_heredocs:
                    body, i = _read_heredoc(
                        src, i, redir.target.text, redir.text == "<<-")
                    redir.heredoc_body = body
                pending_heredocs = []
            continue
        word = word or _Word("")
        word.text += c
        i += 1
    flush()
    if expect_target is not None or pending_heredocs:
        raise _ParseError("dangling redirect or heredoc")
    return tokens


@dataclass
class _Frame:
    kind: str  # top | loop | if | case | brace | subshell
    committed: list = field(default_factory=list)
    pending: list = field(default_factory=list)
    case_state: str = "pattern"


def _command_name(words: list[_Word]) -> tuple[str, list[str]]:
    """Return ``(basename, args)`` of the command a simple command runs."""
    texts = [w.text for w in words]
    idx = 0
    while idx < len(texts) and _ASSIGNMENT_RE.match(texts[idx]):
        idx += 1
    while idx < len(texts) and os.path.basename(texts[idx]) in _WRAPPERS:
        wrapper = os.path.basename(texts[idx])
        idx += 1
        while idx < len(texts) and (texts[idx].startswith("-")
                                    or (wrapper == "env" and "=" in texts[idx])):
            idx += 2 if wrapper == "nice" and texts[idx] == "-n" else 1
    if idx >= len(texts):
        return "", []
    return os.path.basename(texts[idx]), texts[idx + 1:]


def _simple_command_findings(words: list[_Word], redirs: list[_Redir], in_loop: bool) -> list[str]:
    findings: list[str] = []
    for w in words:
        for sub in w.subs:
            findings += _analyse(sub, in_loop)
    for r in redirs:
        if r.target is not None:
            for sub in r.target.subs:
                findings += _analyse(sub, in_loop)
    texts = [w.text for w in words]
    if "setsid" in map(os.path.basename, texts) and ("-f" in texts or "--fork" in texts):
        return findings  # `setsid -f` detaches; only its argv substitutions ran here
    name, args = _command_name(words)
    if name in _SHELLS and "-c" in args[:-1]:
        findings += _analyse(args[args.index("-c") + 1], in_loop)
    if name == "watch":
        findings.append("watch")
    elif name == "tail" and any(_TAIL_FOLLOW_RE.match(a) for a in args):
        findings.append("tail -f")
    elif name == "sleep" and in_loop and args:
        m = _SLEEP_ARG_RE.match(args[0])
        if m and float(m.group(1)) * _SLEEP_UNITS[m.group(2).lower()] >= POLL_LOOP_MIN_SLEEP_S:
            findings.append(f"sleep {args[0]} inside a for/while/until loop")
    if name in _SHELL_STDIN_CONSUMERS:
        for r in redirs:
            if r.heredoc_body is not None:
                findings += _analyse(r.heredoc_body, in_loop)
    return findings


def _analyse(src: str, in_loop: bool = False) -> list[str]:
    """Foreground poll findings in ``src`` (``in_loop``: nested inside a loop)."""
    tokens = _tokenise(src)
    stack = [_Frame("top")]
    words: list[_Word] = []
    redirs: list[_Redir] = []
    skip_for_header = False
    funcdef = False

    def looping() -> bool:
        return in_loop or any(f.kind == "loop" for f in stack)

    def end_simple():
        nonlocal words, redirs
        if words or redirs:
            stack[-1].pending += _simple_command_findings(words, redirs, looping())
        words, redirs = [], []

    def close(kind: str):
        if len(stack) == 1 or stack[-1].kind != kind:
            raise _ParseError(f"unbalanced {kind}")
        frame = stack.pop()
        stack[-1].pending += frame.committed + frame.pending

    for tok in tokens:
        frame = stack[-1]
        if skip_for_header:
            if isinstance(tok, _Word) and not tok.quoted and tok.text == "do":
                skip_for_header = False
            continue
        if frame.kind == "case" and frame.case_state == "pattern":
            if isinstance(tok, _Word) and not tok.quoted and tok.text == "esac":
                close("case")
            elif isinstance(tok, _Op) and tok.text == ")":
                frame.case_state = "body"
            continue
        if isinstance(tok, _Redir):
            redirs.append(tok)
            continue
        if isinstance(tok, _Word):
            keyword = not tok.quoted and not words and not redirs
            if keyword and tok.text in _LOOP_OPENERS:
                stack.append(_Frame("loop"))
                skip_for_header = tok.text == "for"
            elif keyword and tok.text == "if":
                stack.append(_Frame("if"))
            elif keyword and tok.text == "case":
                stack.append(_Frame("case", case_state="header"))
            elif keyword and tok.text == "{":
                stack.append(_Frame("brace"))
            elif keyword and tok.text in _CLOSERS:
                close(_CLOSERS[tok.text])
            elif keyword and tok.text in _PREFIX_KEYWORDS:
                pass
            elif frame.kind == "case" and frame.case_state == "header":
                if not tok.quoted and tok.text == "in":
                    frame.case_state = "pattern"
            else:
                words.append(tok)
            continue
        op = tok.text
        if op == "(":
            if words or redirs:
                funcdef = True  # `name()`: the name is defined, not run
                continue
            stack.append(_Frame("subshell"))
            continue
        if op == ")" and funcdef:
            funcdef = False
            words, redirs = [], []
            continue
        end_simple()
        if op == ")":
            close("subshell")
        elif op == "&":
            stack[-1].pending = []  # the whole and-or list runs in the background
        elif op in (";", "\n", ";;", ";&", ";;&"):
            top = stack[-1]
            top.committed += top.pending
            top.pending = []
            if op != ";" and op != "\n" and top.kind == "case":
                top.case_state = "pattern"
    end_simple()
    if len(stack) != 1 or skip_for_header or funcdef:
        raise _ParseError("unterminated compound command")
    return stack[0].committed + stack[0].pending


def _gateway_polling_loop_guidance(command: str) -> str | None:
    """Refusal text for a foreground polling loop, else None.

    Only consulted for messaging-gateway turns (CLI behaviour is unchanged).
    """
    if not command:
        return None
    try:
        findings = _analyse(command)
    except (_ParseError, RecursionError):
        return None
    if not findings:
        return None
    return (
        f"Refused: foreground polling loop ({findings[0]}) in a messaging-gateway "
        f"session. While a foreground tool call runs, this chat cannot answer "
        f"new messages — a 60-minute poll leaves the user unanswered for 60 "
        f"minutes. Do this instead: (1) re-send it with background=true (or "
        f"watch_patterns for the line you are waiting for) and collect it with "
        f"process(action='wait', timeout=...) in slices of a few minutes; (2) for "
        f"waits longer than ~10 minutes, hand the work to a kanban card or a "
        f"cron job; (3) for a one-shot status check, run the check once "
        f"without the loop."
    )
