"""Refuse commands that would recursively delete the Hermes home's protected roots.

2026-10-13: a test script ran ``rm -rf "$HERMES_HOME"`` while the variable still held
the live home (``export HERMES_HOME=$(mktemp -d) HERMES_HOME=$HERMES_HOME`` expands
every word before assigning, so the old value won). The text-level floors never matched:
variables were not expanded, and the ``rm`` lived inside a script invoked by path.

This module answers one question before a terminal command runs: *would it delete a
protected root?* It is a static analyzer. Nothing is evaluated: no ``eval``, no
subshell, no ``bash -n``. It

1. tokenizes the command with a small shell lexer that honors quoting, and expands
   simple ``$VAR`` / ``${VAR}`` / ``${VAR:-default}`` / ``~`` references against the
   process environment plus assignments made earlier in the same command (with the
   declaration-builtin "expand all words first" rule that bit 06:02). Command
   substitution, arithmetic and positional parameters are opaque: a target built from
   them is never refused;
2. reads a local script the command invokes by path (``bash|sh|zsh|source <file>``,
   ``python <file>``, or a directly executed file) — depth 1, regular file, < 1 MiB,
   no NUL bytes — and analyzes it with the same rules (Python via ``ast``, parse only);
3. recognizes recursive-delete forms: ``rm -r…`` (any spelling/path of rm), ``find
   <root> … -delete`` / ``-exec rm`` without a name/path filter, ``git clean`` with
   ``-x``/``-X``, and Python ``shutil.rmtree`` / ``os.removedirs``;
4. refuses when a resolved target **equals or is an ancestor of** a protected root:
   the home itself (``HERMES_HOME``, its fleet root, and ``$HOME/.hermes``), every
   ``profiles/<name>``, ``profiles/``, ``runtime/`` (and the ``runtime/hermes-agent`` checkout), ``hooks-installed/``,
   ``state.db`` and ``.git``. Deleting anything *below* a root is allowed (I2).

Fails OPEN on analyzer errors and on oversized input: this is a guard against
accidents, layered over the approval floor, not a sandbox. See
``tests/agent/test_home_deletion_guard.py`` for the pinned behavior and
``UNCOVERED`` below for what it deliberately does not see.
"""

from __future__ import annotations

import ast
import fnmatch
import logging
import os
import re
import stat
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional

logger = logging.getLogger(__name__)

BLOCK_MARKER = "home_deletion_guard"

# Shapes this analyzer deliberately does not cover (kept in code so the list ships with it).
UNCOVERED = (
    "paths built from command substitution, arithmetic, positional args or `read` (opaque)",
    "deletes via xargs, eval, here-strings fed to a shell, or `sh < file`",
    "scripts nested more than one level deep, or > 1 MiB, or non-local backends",
    "deleters other than rm/find/git clean/shutil.rmtree/os.removedirs "
    "(perl, node, rsync --delete, mv to trash, subprocess/os.system inside Python)",
    "find -delete with a -name/-path/-regex filter (treated as a targeted cleanup)",
    "git clean without -x/-X (removes untracked, non-ignored files only)",
    "shell control flow (if/loops/functions) is analyzed linearly, not executed",
    "a target that only becomes protected after the analyzed command ran (TOCTOU)",
    "commands/scripts with more than _MAX_TARGET_CHECKS delete targets (fails open)",
    "non-local terminal backends (docker/ssh/modal: paths are not on this filesystem)",
)

_MAX_TEXT = 2 * 1024 * 1024
# Deletion-shaped segments resolved per call before the analyzer gives up (fails open).
# A 1 MiB script of nothing but `rm` lines stays under ~150 ms with this cap.
_MAX_TARGET_CHECKS = 2000
# Cheap pre-filter: text with none of these cannot contain a form this module refuses.
_RELEVANT_RE = re.compile(r"\brm\b|\bunlink\b|rmtree|removedirs|-delete|-exec|\bclean\b|\bbash\b|\bsh\b|"
                          r"\bzsh\b|\bdash\b|\bksh\b|\bsource\b|python|/|\.\s")
_MAX_SCRIPT_BYTES = 1024 * 1024
_MAX_INLINE_DEPTH = 3

_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\+?=")
_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
_PY_RE = re.compile(r"^python(?:\d+(?:\.\d+)?)?$")
_DECL_BUILTINS = frozenset({"export", "declare", "typeset", "local", "readonly"})
_TRANSPARENT = frozenset({
    "command", "builtin", "nohup", "exec", "time", "then", "do", "else", "elif", "if",
    "while", "until", "!", "{", "}", "caffeinate",
})
_FIND_FILTERS = frozenset({"-name", "-iname", "-path", "-ipath", "-wholename", "-iwholename",
                           "-regex", "-iregex", "-lname", "-ilname"})
_UNKNOWN = object()  # an opaque value (command substitution, positional args, ...)
_OPAQUE_CWD = "\x00unknown"  # cwd after `cd <opaque>`: only absolute targets resolve


# ── lexer ──────────────────────────────────────────────────────────────────────────────
# A word is a list of parts: ("u", text) unquoted literal, ("q", text) quoted literal,
# ("v", name, default_parts_or_None, quoted) variable, ("x",) opaque, ("s",) a field break
# inside an unquoted ``${X:-a b}`` default. Separators are None.

def _match_close(s: str, i: int, open_c: str, close_c: str) -> int:
    """Index just past the ``close_c`` balancing ``s[i] == open_c`` (quote-aware)."""
    depth, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "'":
            j = s.find("'", i + 1)
            i = n if j < 0 else j + 1
            continue
        if c == '"':
            i += 1
            while i < n and s[i] != '"':
                i += 2 if s[i] == "\\" else 1
            i += 1
            continue
        if c == open_c:
            depth += 1
        elif c == close_c:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _dollar(s: str, i: int, quoted: bool) -> tuple[tuple, int]:
    """Parse the expansion at ``s[i] == '$'``; return (part, next index)."""
    n = len(s)
    nxt = s[i + 1] if i + 1 < n else ""
    if nxt == "(":
        return ("x",), _match_close(s, i + 1, "(", ")")
    if nxt == "{":
        end = _match_close(s, i + 1, "{", "}")
        body = s[i + 2:end - 1]
        m = _NAME_RE.match(body)
        if m and m.end() == len(body):
            return ("v", body, None, quoted), end
        if m and body[m.end():m.end() + 2] in (":-", ":=") or (m and body[m.end():m.end() + 1] in ("-", "=")):
            op_len = 2 if body[m.end()] == ":" else 1
            return ("v", m.group(0), _default_parts(body[m.end() + op_len:], quoted), quoted), end
        return ("x",), end
    m = _NAME_RE.match(s, i + 1)
    if m:
        return ("v", m.group(0), None, quoted), m.end()
    if nxt and (nxt.isdigit() or nxt in "@*#?$!-"):
        return ("x",), i + 2
    return (("q" if quoted else "u"), "$"), i + 1


def _requote(parts: list) -> list:
    """Parts as they behave inside double quotes: no field splitting, no globbing."""
    out = []
    for p in parts:
        if p[0] == "u":
            out.append(("q", p[1]))
        elif p[0] == "s":
            out.append(("q", " "))
        elif p[0] == "v":
            out.append(("v", p[1], _requote(p[2]) if p[2] is not None else None, True))
        else:
            out.append(p)
    return out


def _default_parts(text: str, quoted: bool) -> list:
    """Lex the default word of ``${X:-word}`` with the shell's quoting rules. Inside double
    quotes the word is one literal-preserving string (exact whitespace, no splitting, no
    globbing); unquoted, its blanks are field breaks."""
    if quoted:
        return _requote([p for w in _lex('"' + text + '"') if w is not None for p in w])
    parts: list = []
    for w in _lex(text):
        if w is None:
            continue
        if parts:
            parts.append(("s",))
        parts.extend(w)
    return parts


def _lex(s: str) -> list:
    """Words (part lists) and ``None`` separators. Redirection targets are dropped."""
    out: list = []
    cur: Optional[list] = None
    drop_next = False
    i, n = 0, len(s)

    def end_word():
        nonlocal cur, drop_next
        if cur is not None:
            if drop_next:
                drop_next = False
            else:
                out.append(cur)
        cur = None

    while i < n:
        c = s[i]
        if c in " \t\r":
            end_word()
            i += 1
        elif c in "\n;&|()":
            end_word()
            out.append(None)
            i += 1
        elif c == "#" and cur is None:
            j = s.find("\n", i)
            i = n if j < 0 else j
        elif c in "<>":
            # `2>` / `&>`: drop a bare fd-number word that is glued to the operator.
            if cur is not None and len(cur) == 1 and cur[0][0] == "u" and cur[0][1].isdigit():
                cur = None
            end_word()
            while i < n and s[i] in "<>&":
                i += 1
            drop_next = True
        elif c == "'":
            j = s.find("'", i + 1)
            j = n if j < 0 else j
            cur = (cur or []) + [("q", s[i + 1:j])]
            i = j + 1
        elif c == '"':
            cur = cur or []
            i += 1
            buf = []
            while i < n and s[i] != '"':
                ch = s[i]
                if ch == "\\" and i + 1 < n and s[i + 1] in '$`"\\\n':
                    buf.append(s[i + 1])
                    i += 2
                elif ch == "$":
                    if buf:
                        cur.append(("q", "".join(buf)))
                        buf = []
                    part, i = _dollar(s, i, True)
                    cur.append(part)
                elif ch == "`":
                    if buf:
                        cur.append(("q", "".join(buf)))
                        buf = []
                    j = s.find("`", i + 1)
                    i = n if j < 0 else j + 1
                    cur.append(("x",))
                else:
                    buf.append(ch)
                    i += 1
            if buf or not cur:
                cur.append(("q", "".join(buf)))
            i += 1
        elif c == "\\":
            cur = (cur or []) + [("q", s[i + 1:i + 2])]
            i += 2
        elif c == "$":
            part, i = _dollar(s, i, False)
            cur = (cur or []) + [part]
        elif c == "`":
            j = s.find("`", i + 1)
            i = n if j < 0 else j + 1
            cur = (cur or []) + [("x",)]
        else:
            j = i
            while j < n and s[j] not in " \t\r\n;&|()<>'\"\\$`":
                j += 1
            cur = (cur or []) + [("u", s[i:j])]
            i = j
    end_word()
    return out


def _segments(words: list) -> Iterable[list]:
    seg: list = []
    for w in words:
        if w is None:
            if seg:
                yield seg
            seg = []
        else:
            seg.append(w)
    if seg:
        yield seg


# ── expansion ──────────────────────────────────────────────────────────────────────────

_GLOB_CHARS = re.compile(r"[*?\[]")


@dataclass
class _Word:
    text: Optional[str]          # expanded text; None when any part is opaque
    pattern: Optional[str] = None  # glob pattern when unquoted glob chars are present


def _escape_glob(text: str) -> str:
    return re.sub(r"([*?\[])", r"[\1]", text)


def _lookup(table: Mapping, name: str, default: Optional[list]):
    """The variable's value, or its default word's parts (a list) when unset/empty."""
    value = table.get(name)
    if value is None or value == "":
        return default if default is not None else ("" if value is None else value)
    return value


def _ifs_split_re(table: Mapping):
    ifs = table.get("IFS", " \t\n")
    if not isinstance(ifs, str):
        return None  # opaque IFS: the caller treats the expansion as opaque
    return re.compile("[" + re.escape(ifs) + "]+") if ifs else False


def _expand_fields(parts: list, table: Mapping, home: str, *, split: bool = True,
                   assignment_value: bool = False) -> list:
    """Expand one shell word into the fields the shell would pass. With ``split``, unquoted
    variable values are field-split on IFS and stay subject to pathname expansion (quoted
    values are neither); without it (assignment values) the word is always one field.
    Any opaque part makes the whole word a single opaque field."""
    fields: list[_Word] = []
    text: list[str] = []
    pattern: list[str] = []
    state = {"globbed": False, "content": False}

    def emit():
        if state["content"] or text:
            joined = "".join(text)
            fields.append(_Word(joined, "".join(pattern) if state["globbed"] else None))
        text.clear()
        pattern.clear()
        state["globbed"] = state["content"] = False

    def add_unquoted(chunk: str):
        if _GLOB_CHARS.search(chunk):
            state["globbed"] = True
        text.append(chunk)
        pattern.append(chunk)

    def walk(seq: list, word_start: bool) -> bool:
        for idx, part in enumerate(seq):
            kind = part[0]
            at_start = word_start and idx == 0
            if kind == "x":
                return False
            if kind == "s":
                # A blank inside an unquoted default word is a field break ONLY under the
                # shell's IFS rules: ``IFS=; rm -rf ${U:-/tmp/hermes home}`` passes ONE path
                # (Prism d0b8d9a3d9eb). Empty IFS joins; opaque IFS is opaque.
                if not split:
                    text.append(" ")
                    pattern.append(" ")
                    continue
                splitter = _ifs_split_re(table)
                if splitter is None:
                    return False
                if splitter and splitter.match(" "):
                    emit()
                else:
                    add_unquoted(" ")
                continue
            if kind == "v":
                value = _lookup(table, part[1], part[2])
                if value is _UNKNOWN:
                    return False
                if isinstance(value, list):  # the default word: expanded with its own quoting
                    if part[3]:
                        state["content"] = True  # `"${UNSET:-}"` is still one (empty) field
                    if not walk(value, at_start):
                        return False
                    continue
                if part[3] or not split:
                    state["content"] = state["content"] or part[3]
                    text.append(value)
                    pattern.append(_escape_glob(value))
                    continue
                splitter = _ifs_split_re(table)
                if splitter is None:
                    return False
                pieces = splitter.split(value) if splitter else [value]
                for k, piece in enumerate(pieces):
                    if k:
                        emit()
                    if piece:
                        add_unquoted(piece)
                continue
            chunk = part[1]
            if kind == "u":
                if at_start and (chunk == "~" or chunk.startswith("~/")) and home:
                    chunk = home + chunk[1:]
                if assignment_value and at_start and chunk.startswith("~") and home:
                    chunk = home + chunk[1:]
                add_unquoted(chunk)
            else:
                state["content"] = True
                text.append(chunk)
                pattern.append(_escape_glob(chunk))
        return True

    if not walk(parts, True):
        return [_Word(None)]
    emit()
    if not split and not fields:
        fields.append(_Word(""))
    return fields


def _expand(parts: list, table: Mapping, home: str, *, assignment_value: bool = False) -> _Word:
    """One unsplit field (assignment values, heads of declarations)."""
    return _expand_fields(parts, table, home, split=False, assignment_value=assignment_value)[0]


def _assignment(parts: list) -> Optional[tuple[str, list]]:
    if not parts or parts[0][0] != "u":
        return None
    m = _ASSIGN_RE.match(parts[0][1])
    if not m:
        return None
    rest = parts[0][1][m.end():]
    return m.group(1), ([("u", rest)] if rest else []) + list(parts[1:])


# ── protected roots ────────────────────────────────────────────────────────────────────

def _real(path: str) -> str:
    try:
        return os.path.realpath(path)
    except Exception:
        return os.path.normpath(path)


@dataclass
class _Roots:
    dirs: dict = field(default_factory=dict)     # resolved path -> label
    files: dict = field(default_factory=dict)
    profile_parents: set = field(default_factory=set)

    def hit(self, target: str, recursive: bool) -> Optional[str]:
        if not target:
            return None
        if target in self.files:
            return self.files[target]
        if not recursive:
            return None
        sep = os.sep
        prefix = target.rstrip(sep) + sep
        for root, label in list(self.dirs.items()) + list(self.files.items()):
            if root == target or root.startswith(prefix):
                return label
        if os.path.dirname(target) in self.profile_parents:
            return target
        return None

    def candidates(self) -> set[str]:
        """Every protected path and all of its ancestors (for glob matching)."""
        out: set[str] = set()
        for root in list(self.dirs) + list(self.files):
            p = root
            while True:
                out.add(p)
                parent = os.path.dirname(p)
                if parent == p:
                    break
                p = parent
        for parent in self.profile_parents:
            try:
                out.update(os.path.join(parent, e) for e in os.listdir(parent))
            except OSError:
                pass
        return out


def _protected_roots(env: Mapping[str, str]) -> _Roots:
    roots = _Roots()
    home = env.get("HOME")
    home = (home if isinstance(home, str) else "") or os.path.expanduser("~")
    bases: list[str] = []
    hermes_home = env.get("HERMES_HOME")
    hermes_home = hermes_home.strip() if isinstance(hermes_home, str) else ""
    if hermes_home:
        vh = _real(os.path.expanduser(hermes_home))
        bases.append(vh)
        parent = os.path.dirname(vh)
        if os.path.basename(parent) == "profiles":
            bases.append(os.path.dirname(parent))
    if home:
        bases.append(_real(os.path.join(home, ".hermes")))
    for base in dict.fromkeys(bases):
        if base in ("", os.sep):
            continue
        roots.dirs[base] = base
        for rel in ("profiles", "runtime", "runtime/hermes-agent", "hooks-installed", ".git"):
            p = os.path.join(base, rel)
            roots.dirs[_real(p)] = p
        roots.files[_real(os.path.join(base, "state.db"))] = os.path.join(base, "state.db")
        roots.profile_parents.add(_real(os.path.join(base, "profiles")))
    return roots


def _resolve(path: str, cwd: str, real=_real) -> str:
    """Absolute, symlink-resolved path for the entry ``rm`` would act on: the final
    component stays unresolved (``rm link`` removes the link), unless a trailing slash
    or ``.``/``..`` makes the shell follow it."""
    if not os.path.isabs(path):
        path = os.path.join(cwd, path)
    follow = path.endswith(("/", "/.", "/..")) or os.path.basename(path) in (".", "..")
    norm = os.path.normpath(path)
    if follow or norm == os.sep:
        return real(norm)
    return os.path.join(real(os.path.dirname(norm)), os.path.basename(norm))


def _glob_hits(pattern: str, cwd: str, roots: _Roots, recursive: bool) -> Optional[str]:
    if not os.path.isabs(pattern):
        pattern = os.path.join(cwd, pattern)
    comps = [c for c in pattern.split(os.sep) if c not in ("",)]
    first = next((k for k, c in enumerate(comps) if _GLOB_CHARS.search(c)), len(comps))
    prefix = _real(os.sep + os.sep.join(comps[:first])) if first else os.sep
    pat_comps = [c for c in prefix.split(os.sep) if c] + comps[first:]
    for cand in roots.candidates():
        cand_comps = [c for c in cand.split(os.sep) if c]
        if len(cand_comps) != len(pat_comps):
            continue
        if all(fnmatch.fnmatchcase(c, p) and (not c.startswith(".") or p.startswith("."))
               for c, p in zip(cand_comps, pat_comps)):
            hit = roots.hit(cand, recursive)
            if hit:
                return hit
    return None


# ── analysis ───────────────────────────────────────────────────────────────────────────

class _Budget(Exception):
    """Raised when a call exceeds ``_MAX_TARGET_CHECKS``; the caller fails open."""


@dataclass
class _Ctx:
    roots: _Roots
    home: str
    local_files: bool
    checks: int = 0
    real_cache: dict = field(default_factory=dict)

    def real(self, path: str) -> str:
        hit = self.real_cache.get(path)
        if hit is None:
            hit = self.real_cache[path] = _real(path)
        return hit


def _refusal(target: str, root: str) -> str:
    return f"{target} (protected: {root})"


def _check_target(word: _Word, cwd: str, ctx: _Ctx, recursive: bool) -> Optional[str]:
    if word.text is None or word.text == "":
        return None
    if cwd == _OPAQUE_CWD and not os.path.isabs(word.text):
        return None  # a relative target under an opaque cwd cannot be resolved
    ctx.checks += 1
    if ctx.checks > _MAX_TARGET_CHECKS:
        raise _Budget()
    if word.pattern is not None:
        hit = _glob_hits(word.pattern, cwd, ctx.roots, recursive)
        return _refusal(word.text, hit) if hit else None
    resolved = _resolve(word.text, cwd, ctx.real)
    hit = ctx.roots.hit(resolved, recursive)
    return _refusal(resolved, hit) if hit else None


def _check_rm(args: list[_Word], cwd: str, ctx: _Ctx) -> Optional[str]:
    recursive, operands, flags_done = False, [], False
    for w in args:
        t = w.text
        if not flags_done and t == "--":
            flags_done = True
        elif not flags_done and t and t.startswith("-") and len(t) > 1:
            if t == "--recursive" or (not t.startswith("--") and ("r" in t or "R" in t)):
                recursive = True
        else:
            operands.append(w)
    for w in operands:
        hit = _check_target(w, cwd, ctx, recursive)
        if hit:
            return hit
    return None


def _check_find(args: list[_Word], cwd: str, ctx: _Ctx) -> Optional[str]:
    texts = [w.text or "" for w in args]
    i = 0
    while i < len(texts) and texts[i] in ("-H", "-L", "-P", "-E", "-X", "-d", "-s", "-x"):
        i += 1
    roots = []
    while i < len(texts) and not texts[i].startswith(("-", "(", "!")):
        roots.append(args[i])
        i += 1
    rest = texts[i:]
    deletes = "-delete" in rest or any(
        t in ("-exec", "-execdir", "-ok", "-okdir") and k + 1 < len(rest)
        and os.path.basename(rest[k + 1]) == "rm" for k, t in enumerate(rest))
    if not deletes or any(t in _FIND_FILTERS for t in rest):
        return None
    for w in roots or [_Word(".")]:
        hit = _check_target(w, cwd, ctx, True)
        if hit:
            return hit
    return None


def _check_git(args: list[_Word], cwd: str, ctx: _Ctx) -> Optional[str]:
    texts = [w.text for w in args]
    i = 0
    while i < len(texts):
        t = texts[i]
        if t == "-C" and i + 1 < len(texts):
            sub_dir = texts[i + 1]
            if sub_dir is None:
                return None
            if cwd != _OPAQUE_CWD or os.path.isabs(sub_dir):
                cwd = os.path.join(cwd, sub_dir)
            i += 2
        elif t in ("-c", "--git-dir", "--work-tree", "--namespace") and i + 1 < len(texts):
            i += 2
        elif t and t.startswith("-"):
            i += 1
        else:
            break
    if i >= len(texts) or texts[i] != "clean":
        return None
    # Parse clean's own options first: `-e <pattern>` / `--exclude <pattern>` take an
    # argument that is NOT a pathspec (and that may itself start with `-`).
    shorts, longs, pathspec_words = "", [], []
    k, sub_args = 0, args[i + 1:]
    while k < len(sub_args):
        t = sub_args[k].text
        if t is None:
            pass  # an opaque pathspec: fall back to the cwd-wide check below
        elif t == "--":
            pathspec_words.extend(w for w in sub_args[k + 1:] if w.text is not None)
            break
        elif t.startswith("--"):
            longs.append(t.split("=", 1)[0])
            if t == "--exclude":
                k += 1
        elif t.startswith("-") and len(t) > 1:
            cluster = t[1:]
            e_at = cluster.find("e")
            if e_at >= 0:
                cluster = cluster[:e_at]
                if e_at == len(t) - 2:
                    k += 1  # `-e PAT` / `-xe PAT`: the next word is the pattern
            shorts += cluster
        else:
            pathspec_words.append(sub_args[k])
        k += 1
    if "x" not in shorts and "X" not in shorts:
        return None
    if "n" in shorts or "--dry-run" in longs:
        return None
    for w in pathspec_words or [_Word(cwd)]:
        hit = _check_target(_Word(w.text, w.pattern), cwd, ctx, True)
        if hit:
            return hit
    return None


def _read_script(path: str, cwd: str) -> Optional[str]:
    try:
        full = path if os.path.isabs(path) else os.path.join(cwd, path)
        st = os.stat(full)
        if not stat.S_ISREG(st.st_mode) or st.st_size > _MAX_SCRIPT_BYTES:
            return None
        with open(full, "rb") as fh:
            data = fh.read(_MAX_SCRIPT_BYTES + 1)
        if len(data) > _MAX_SCRIPT_BYTES or b"\x00" in data:
            return None
        return data.decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return None


def _shell_script_target(args: list[_Word]) -> tuple[Optional[str], Optional[str]]:
    """(inline ``-c`` body, script path) for ``bash [opts] (-c BODY | FILE)``."""
    i = 0
    while i < len(args):
        t = args[i].text
        if t is None:
            return None, None
        if t == "-c":
            return (args[i + 1].text if i + 1 < len(args) else None), None
        if t in ("-o", "+o", "-O", "+O", "--rcfile", "--init-file"):
            i += 2
            continue
        if t == "--":
            i += 1
            break
        if t.startswith(("-", "+")) and len(t) > 1:
            if not t.startswith("--") and "c" in t[1:]:
                rest = [a.text for a in args[i + 1:]]
                return (rest[0] if rest else None), None
            i += 1
            continue
        break
    return None, (args[i].text if i < len(args) else None)


def _python_target(args: list[_Word]) -> tuple[Optional[str], Optional[str]]:
    i = 0
    while i < len(args):
        t = args[i].text
        if t is None:
            return None, None
        if t == "-c":
            return (args[i + 1].text if i + 1 < len(args) else None), None
        if t in ("-m",):
            return None, None
        if t in ("-W", "-X", "--check-hash-based-pycs"):
            i += 2
            continue
        if t.startswith("-") and len(t) > 1:
            i += 1
            continue
        return None, (None if t == "-" else t)
    return None, None


def _analyze_shell(text: str, table: dict, cwd: str, ctx: _Ctx, depth: int) -> Optional[str]:
    from tools.shell_heredoc import strip_inert_heredoc_bodies

    if not _RELEVANT_RE.search(text):
        return None
    try:
        text = strip_inert_heredoc_bodies(text)
    except Exception:
        pass
    for seg in _segments(_lex(text)):
        hit = _analyze_segment(seg, table, cwd, ctx, depth)
        if isinstance(hit, str):
            return hit
        if isinstance(hit, tuple):  # ("cd", new_cwd)
            cwd = hit[1]
    return None


def _analyze_segment(seg: list, table: dict, cwd: str, ctx: _Ctx, depth: int):
    idx, prefix = 0, []
    while idx < len(seg):
        a = _assignment(seg[idx])
        if a is None:
            break
        prefix.append(a)  # ordered, duplicates kept: `A=x A=$A` is sequential
        idx += 1
    if idx == len(seg):  # plain assignments: sequential, each sees the previous ones
        for name, parts in prefix:
            w = _expand(parts, table, ctx.home, assignment_value=True)
            table[name] = _UNKNOWN if w.text is None else w.text
        return None
    words = [f for p in seg[idx:] for f in _expand_fields(p, table, ctx.home)]
    child_env = dict(table)
    for name, parts in prefix:
        w = _expand(parts, child_env, ctx.home, assignment_value=True)
        child_env[name] = _UNKNOWN if w.text is None else w.text

    # Strip transparent prefixes (sudo -u x, env -i A=b, nohup, time, ...).
    while words:
        head = words[0].text
        if head is None:
            return None
        name = os.path.basename(head)
        if name in _TRANSPARENT:
            words = words[1:]
        elif name in ("sudo", "doas"):
            words = words[1:]
            while words and (words[0].text or "").startswith("-"):
                takes_arg = words[0].text in ("-u", "-g", "-C", "-h", "-p", "-U", "-r", "-t")
                words = words[2:] if takes_arg else words[1:]
        elif name == "env":
            words = words[1:]
            while words and words[0].text is not None and (
                    words[0].text.startswith("-") or _ASSIGN_RE.match(words[0].text)):
                m = _ASSIGN_RE.match(words[0].text)
                if m:
                    child_env[m.group(1)] = words[0].text[m.end():]
                words = words[1:]
        elif name == "nice":
            words = words[1:]
            if words and words[0].text == "-n":
                words = words[2:]
        else:
            break
    if not words:
        return None
    head = words[0].text or ""
    name = os.path.basename(head)
    args = words[1:]

    if name in _DECL_BUILTINS:
        # Every word is expanded BEFORE any assignment (the 06:02 trap).
        snapshot = dict(table)
        for parts in seg[idx + 1:]:
            a = _assignment(parts)
            if a is not None:
                w = _expand(a[1], snapshot, ctx.home, assignment_value=True)
                table[a[0]] = _UNKNOWN if w.text is None else w.text
        return None
    if name == "unset":
        for w in args:
            if w.text:
                table.pop(w.text, None)
        return None
    if name in ("cd", "pushd"):
        target = next((w for w in args if w.text is None or not w.text.startswith("-")), None)
        if target is None:
            return ("cd", ctx.home or cwd)
        if target.text is None:
            return ("cd", _OPAQUE_CWD)
        if os.path.isabs(target.text):
            return ("cd", os.path.normpath(target.text))
        if cwd == _OPAQUE_CWD:
            return None
        return ("cd", os.path.normpath(os.path.join(cwd, target.text)))
    # An opaque cwd (``cd "$(mktemp -d)"``) only hides RELATIVE targets: every check
    # below resolves an absolute target without it, and ``_resolve`` refuses to join a
    # relative one onto the opaque marker (fails open for that target only).
    if name in ("rm", "unlink"):
        return _check_rm(args, cwd, ctx)
    if name == "find":
        return _check_find(args, cwd, ctx)
    if name == "git":
        return _check_git(args, cwd, ctx)
    if name in _SHELLS:
        body, script = _shell_script_target(args)
        if body is not None and depth < _MAX_INLINE_DEPTH:
            return _analyze_shell(body, dict(child_env), cwd, ctx, depth + 1)
        if script and depth == 0 and ctx.local_files:
            src = _read_script(script, cwd)
            return _analyze_shell(src, dict(child_env), cwd, ctx, depth + 1) if src else None
        return None
    if name in ("source", "."):
        if args and args[0].text and depth == 0 and ctx.local_files:
            src = _read_script(args[0].text, cwd)
            return _analyze_shell(src, table, cwd, ctx, depth + 1) if src else None
        return None
    if _PY_RE.match(name):
        code, script = _python_target(args)
        if code is None and script and depth == 0 and ctx.local_files:
            code = _read_script(script, cwd)
        return _analyze_python(code, child_env, cwd, ctx) if code else None
    if "/" in head and depth == 0 and ctx.local_files:
        src = _read_script(head, cwd)
        if src:
            first = src.split("\n", 1)[0]
            if first.startswith("#!") and "python" in first:
                return _analyze_python(src, child_env, cwd, ctx)
            return _analyze_shell(src, dict(child_env), cwd, ctx, depth + 1)
    return None


# ── Python (ast, parse only) ───────────────────────────────────────────────────────────

class _PyEval:
    """Evaluate the small subset of path expressions we can resolve statically."""

    def __init__(self, env: Mapping, home: str):
        self.env = env
        self.home = home
        self.names: dict[str, object] = {}

    def _env_get(self, key, default=None):
        value = self.env.get(key) if isinstance(key, str) else None
        if value is _UNKNOWN:
            return None
        return value if value is not None else default

    def _expanduser(self, p):
        if isinstance(p, str) and (p == "~" or p.startswith("~/")) and self.home:
            return self.home + p[1:]
        return p

    def ev(self, node) -> Optional[str]:
        try:
            return self._ev(node)
        except Exception:
            return None

    def _ev(self, node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            v = self.names.get(node.id)
            return v if isinstance(v, str) else None
        if isinstance(node, ast.JoinedStr):
            out = []
            for v in node.values:
                s = self._ev(v.value if isinstance(v, ast.FormattedValue) else v)
                if s is None:
                    return None
                out.append(s)
            return "".join(out)
        if isinstance(node, ast.BinOp):
            left, right = self._ev(node.left), self._ev(node.right)
            if left is None or right is None:
                return None
            if isinstance(node.op, ast.Div):
                return os.path.join(left, right)
            if isinstance(node.op, ast.Add):
                return left + right
            return None
        if isinstance(node, ast.Subscript):
            if _dotted(node.value) == "os.environ":
                return self._env_get(self._ev(node.slice))
            return None
        if isinstance(node, ast.Call):
            fn = _dotted(node.func)
            argv = [self._ev(a) for a in node.args]
            if fn in ("os.environ.get", "os.getenv", "environ.get", "getenv"):
                return self._env_get(argv[0] if argv else None, argv[1] if len(argv) > 1 else None)
            if fn in ("os.path.expanduser", "expanduser"):
                return self._expanduser(argv[0]) if argv else None
            if fn in ("os.path.join", "join") and argv and None not in argv:
                return os.path.join(*argv)
            if fn in ("os.path.abspath", "os.path.realpath", "os.path.normpath", "str", "os.fspath",
                      "os.path.expandvars"):
                return argv[0] if argv else None
            if fn in ("Path", "pathlib.Path", "PurePath", "pathlib.PurePath", "PosixPath"):
                if argv and None not in argv:
                    return os.path.join(*argv)
                return None
            if fn in ("Path.home", "pathlib.Path.home"):
                return self.home or None
            if isinstance(node.func, ast.Attribute):
                attr = node.func.attr
                base = self._ev(node.func.value)
                if base is None:
                    return None
                if attr == "expanduser":
                    return self._expanduser(base)
                if attr in ("resolve", "absolute", "__str__"):
                    return base
                if attr == "joinpath" and None not in argv:
                    return os.path.join(base, *argv)
            return None
        if isinstance(node, ast.Attribute) and node.attr == "parent":
            base = self._ev(node.value)
            return os.path.dirname(os.path.normpath(base)) if base else None
        return None


def _dotted(node) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


_PY_DELETERS = {"shutil.rmtree": True, "rmtree": True, "os.removedirs": True, "removedirs": True}


def _analyze_python(code: str, env: Mapping, cwd: str, ctx: _Ctx) -> Optional[str]:
    if "rmtree" not in code and "removedirs" not in code:
        return None
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return None
    ev = _PyEval(env, ctx.home)
    # ast.walk is breadth-first: module-level assignments are seen before the calls nested
    # in later statements, which is the order the common ``p = ...; rmtree(p)`` needs.
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            ev.names[node.targets[0].id] = ev.ev(node.value)
        elif isinstance(node, ast.Call) and _dotted(node.func) in _PY_DELETERS:
            arg = node.args[0] if node.args else next(
                (k.value for k in node.keywords if k.arg in ("path", "name")), None)
            target = ev.ev(arg) if arg is not None else None
            if target:
                hit = _check_target(_Word(target), cwd, ctx, True)
                if hit:
                    return hit
    return None


# ── entry point ────────────────────────────────────────────────────────────────────────

def snapshot_exports(path: str) -> dict:
    """Exported variables a session snapshot will restore before the next command runs.

    The snapshot starts with an ``export -p`` dump (``declare -x NAME="value"`` lines, then
    functions and aliases). Only that leading block is read, with this module's lexer:
    nothing is sourced or evaluated. A value that is not a plain literal is recorded as
    opaque. Missing, oversized or unreadable files yield ``{}``.
    """
    src = _read_script(path, os.sep) if path else None
    if not src:
        return {}
    out: dict = {}
    in_array = False  # inside `declare -ax A=([0]="x" ...)`: the lexer splits it at the parens
    try:
        for seg in _segments(_lex(src)):
            head = seg[0]
            if not (len(head) == 1 and head[0][0] == "u" and head[0][1] in ("declare", "export", "typeset")):
                if in_array:
                    in_array = False  # the one `[0]="x" [1]="y"` element segment
                    continue
                break  # past the export dump (functions/aliases follow)
            in_array = False
            for parts in seg[1:]:
                a = _assignment(parts)
                if a is None:
                    continue
                if not a[1]:  # `A=(`: an array initializer follows; its value is opaque
                    out[a[0]] = _UNKNOWN
                    in_array = True
                    continue
                literal = all(p[0] in ("u", "q") for p in a[1])
                out[a[0]] = "".join(p[1] for p in a[1]) if literal else _UNKNOWN
    except Exception:
        logger.debug("home deletion guard: snapshot parse failed", exc_info=True)
        return {}
    return out


def command_deletes_protected_home(
    command: str, *, env: Optional[Mapping[str, str]] = None, cwd: Optional[str] = None,
    local_files: bool = True,
) -> Optional[str]:
    """``"<target> (protected: <root>)"`` when ``command`` would delete a protected root.

    ``env`` is the environment the command will run with (defaults to ``os.environ``);
    ``cwd`` its working directory. ``local_files=False`` disables script reads (remote
    backends, whose paths are not on this filesystem). Never raises; fails open.
    """
    if not command or not isinstance(command, str) or len(command) > _MAX_TEXT:
        return None
    try:
        env = dict(os.environ if env is None else env)
        home = env.get("HOME")
        home = home if isinstance(home, str) else ""
        ctx = _Ctx(roots=_protected_roots(env), home=home, local_files=local_files)
        return _analyze_shell(command, dict(env), cwd or os.getcwd(), ctx, 0)
    except _Budget:
        logger.info("home deletion guard: work budget exhausted, failing open")
        return None
    except Exception:  # a guard bug must not brick the terminal tool
        logger.debug("home deletion guard failed open", exc_info=True)
        return None


def refusal_message(hit: str) -> str:
    return (
        f"Blocked: this command would delete {hit}. The Hermes home, its profiles, "
        "runtime/, hooks-installed/, state.db and .git (and any ancestor of them) are "
        "protected from deletion by agent commands; deleting paths BELOW them is allowed. "
        "Variables were expanded from the command's environment, so check what they hold "
        "(e.g. HERMES_HOME). For a disposable home use a fresh temp dir: "
        "S=$(mktemp -d); trap 'rm -rf \"$S\"' EXIT"
    )
