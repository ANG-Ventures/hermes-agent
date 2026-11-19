#!/usr/bin/env python3
# SCHEDULE: manual (CI lint, suite admission_tests; operators run it with --update after moving a decider)
"""admission-decider-lint: refuse a NEW place that decides "may this work start on host H now" outside the ONE
admission library (t_e5124426; same shape as the reserved-lane lint of t_a115fc8a).

The library is scripts/lib/admission.py (admit / admit_class), scripts/lib/admission_demand.py and
scripts/lib/placement.py (may_start / choose / admit, which call it). Any other tracked script that compares a host
load metric to a threshold (cpu_busy, load1/load5, procs_running, runnable_ewma, occupancy, PSI cpu, load_ratio,
getloadavg, ncpu // N) is a candidate private admission rule. The ones that existed when this lint landed are
grandfathered in fleet/admission-decider-baseline.json with their hit COUNT (most are pagers, watchdogs and
forensics, not deciders; the inventory plans/2026-10-27_unified-admission-INVENTORY.md classifies them). The lint is
a ratchet:
  * a file NOT in the baseline with any hit             -> FAIL (route the decision through placement.may_start /
                                                            admission.admit_class, or, for a monitor that never
                                                            gates work, add `# admission-lint: not-a-decider <why>`
                                                            on the line)
  * a baselined file whose hit count GREW               -> FAIL (same remedy)
  * a baselined file whose count shrank / that is gone  -> FAIL until the SAME PR lowers the baseline (--update
                                                            rewrites it down, never up), so a removed decider
                                                            cannot silently grow back into its old allowance
  * --base-ref SHA (CI on a PR): the baseline itself may only go DOWN against the PR base. A row added or raised
    is refused unless the base TREE already carried that many hits in that file (recording code that is already on
    main, never new code); a widened `library` or narrowed `scan` is refused; no baseline at base = a repo's first
    baseline, allowed.

One lint, four repos (t_22955b73): hermes-home owns it; fleet-ops-scripts, prism-router and hermes-agent vendor it
byte-identical under .github/router-core/ (PIN.json) with their own fleet/admission-decider-baseline.json. A baseline
may carry `scan` (dirs, "." = whole tree); `library` is required (repo paths of that repo's admission library copy).
Absent scan uses hermes-home's defaults below. scripts/admission-decider-census.py sums the four baselines into one line.

  admission-decider-lint.py [--root DIR] [--update] [--base-ref SHA]

Count comparison operator occurrences, not statements. Literal metric keys count conservatively.
Comments do not count; non-comparison load sizing shapes remain candidates.
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from scan_scope import is_live_source  # noqa: E402  scripts/retired/ + scripts/attic/: not live source

# hermes-home: scripts/; a vendored copy: .github/router-core/ (two levels below the repo root either way)
ROOT = Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "scripts" else 2]
BASELINE_REL = Path("fleet") / "admission-decider-baseline.json"
SCAN = ("scripts", "fleet", "hooks", "bin", "cron")
LIBRARY = frozenset({"scripts/lib/admission.py", "scripts/lib/admission_demand.py", "scripts/lib/placement.py",
                     "scripts/admission-decider-lint.py"})
LOAD_VARIABLE = r"\b(?:[A-Za-z_]\w*_)?load(?:[15]|_\w+)?\b"
METRIC = (r"\b(?:cpu_busy|load1|load5|procs_running|runnable_ewma|occupancy|psi_cpu_some10|psi_cpu_some60|"
          r"load_ratio|loadavg|getloadavg)\b")
OP = r"(?:>=|<=|(?<![>=-])>(?!>)|(?<!<)<(?![<=])|\s-(?:gt|ge|lt|le)\s)"
# Prism 56a668146742: a shift inside the operand (`(procs_running >> 1) > 8`) is not a comparison; only a lone
# `<` / `>` ends the operand, a `>>` / `<<` pair is part of it.
OPERAND = r"(?:[^\n=;<>]|>>|<<){0,40}?"
# Textual shapes, not general dataflow: cores minus a load-named variable,
# or awk's explicit load-variable binding followed by subtraction of that alias.
SIZING = (r"\b(?:cores|ncpu|threads|n)\s*-\s*[\"$\\{]*" + LOAD_VARIABLE +
          r"|\bawk\b[^\n]*?-v\s+(?P<load_alias>[A-Za-z_]\w*)\s*=\s*[\"$\\{]*" +
          LOAD_VARIABLE + r"[^\n]*?\b(?:cores|ncpu|threads|n)\s*-\s*(?P=load_alias)\b")
RX = re.compile(METRIC + OPERAND + OP + r"|" + OP + OPERAND + METRIC +
                r"|\bncpu\s*//\s*\d|\bos\.getloadavg\(|" + SIZING +
                r"|\$\{?" + LOAD_VARIABLE + r"\}?[\"']?\s*" + OP)
SKIP = re.compile(r"(^|/)(tests?|fixtures|\.venv)/|(^|/)test_[^/]*$|_test\.py$|selftest"
                  r"|\.(?:test|spec)\.[cm]?[jt]sx?$|\.(md|json|ya?ml|txt|golden|plist|service|timer|conf|nft|env)$")
WAIVER = "admission-lint: not-a-decider"
# the lint and its census, wherever a repo keeps them (their regex/doc text matches RX)
SELF = frozenset({"scripts/admission-decider-lint.py", "scripts/admission-decider-census.py",
                  ".github/router-core/admission-decider-lint.py"})


_GIT_ENV = ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")


class ScanError(Exception):
    """A tracked file exists but cannot be read: scanning and --update abort (Prism b63467246d5d)."""


def _git_env():
    return {k: v for k, v in os.environ.items() if k not in _GIT_ENV}


def tracked(root: Path, scan_dirs=SCAN, require_git=False):
    """git ls-files under SCAN when ``root`` IS the git worktree root; any other root (test-gate's synced remote tree,
    an export nested inside an unrelated checkout, Prism f121859a42a0) is walked."""
    config({"scan": scan_dirs, "library": []})
    env = dict(_git_env(), GIT_LITERAL_PATHSPECS="1")
    top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"], capture_output=True, env=env)
    same = top.returncode == 0 and Path(top.stdout.decode().strip()).resolve() == root.resolve()
    if same:
        out = subprocess.run(["git", "--literal-pathspecs", "-C", str(root), "ls-files", "-z", "--", *scan_dirs], capture_output=True, env=env)
        if out.returncode == 0:
            return [p for p in out.stdout.decode().split("\0") if p]
    if same or require_git:
        raise ScanError("git-tracked scan required; walk fallback is read-only")
    return walk(root, scan_dirs)


def walk(root: Path, scan_dirs=SCAN):
    """Every regular file under SCAN. Path.rglob() silently skips a directory it cannot list, which would read an
    unreadable subtree as absent code and let --update drop its baseline entries (Prism 5cf1d79ce0fc); os.walk()
    reports each enumeration failure, and any failure aborts the scan as ScanError."""
    errs = []
    out = []
    for d in scan_dirs:
        top = root / d
        if not top.is_dir():
            continue
        for dirpath, dirs, files in os.walk(top, onerror=errs.append):
            if ".git" in dirs:
                dirs.remove(".git")
            for f in files:
                p = Path(dirpath) / f
                if p.is_file():
                    out.append(p.relative_to(root).as_posix())
    if errs:
        e = errs[0]
        where = getattr(e, "filename", None) or "?"
        raise ScanError(f"{where}: cannot list ({e}){f' (+{len(errs) - 1} more)' if len(errs) > 1 else ''}")
    return sorted(out)


def _waiver_owner(row, start_row, end_row, bare):
    """Allow only a complete, single-statement physical line before a trailing comment.

    All adapters use this rule. Standalone, multiline and compound/multiple
    statements fail closed; punctuation inside literals is already blanked.
    """
    code = bare.strip().removesuffix(";").rstrip()
    if (start_row == end_row == row and code and
            not any(token in code for token in (";", "{", "}", "&&", "||", "\\")) and
            (":" not in code or code.endswith(":"))):
        return row
    return None


# The comparison OP is the unit of identity. Looking out from each operator
# avoids non-overlapping regex matches consuming a neighbouring comparison.
_COMPARISON = re.compile(OP)
_METRIC_NAME = re.compile(METRIC + r"|(?<![.\w])" + LOAD_VARIABLE + r"(?!\s*\()")
_BOUNDARY = re.compile(r"[;=]|(?<!>)>(?!>)|(?<!<)<(?!<)|&&|\|\||\b(?:and|or)\b")
_SPECIAL = re.compile(r"\bncpu\s*//\s*\d|\bos\.getloadavg\(|" + SIZING)
# t_27c11e86: Go exports struct fields (m.Load1, snap.CPUBusy), so a metric operand matches regardless of case and of
# the snake_case underscore. The word boundaries still hold (m.Upload1, Load1Count do not match).
_METRIC_ANY_CASE = re.compile(METRIC.replace("_", "_?"), re.IGNORECASE)
# t_89091ec3: _METRIC_ANY_CASE without its word boundaries, so a match in any operand (a substring of source) is a
# match of this in source. Every comparison hits_in counts names a metric (this), a shell load variable ("load"), a
# bound name or an awk alias (each bound only from a metric read or a metric/load name). No match: no comparison can
# count, and the tokenizer, bracket pairing and operand walks are skipped (#10384 made them ~3x the scan: 41 -> 115 s).
_MAY_COMPARE = re.compile(METRIC.replace("_", "_?").replace(r"\b", ""), re.IGNORECASE)
# Every count in hits_in (comparison, awk field, ncpu // n, getloadavg(), sizing) needs _MAY_COMPARE, a LOAD_VARIABLE
# or `ncpu` in the comment-stripped source, which is built from substrings of the text: absent from the text, it is 0.
_MAY_COUNT = re.compile(_MAY_COMPARE.pattern + "|" + LOAD_VARIABLE + "|ncpu", re.IGNORECASE)
# t_27c11e86: an awk program reading /proc/loadavg compares load fields ($1..$3) inside one quoted literal, where no
# metric name ever appears. Same command only: the file argument, `< /proc/loadavg`, or a pipe from it.
_AWK_CMD = re.compile(r"\bawk\b")
_AWK_PIPED_FROM_LOADAVG = re.compile(r"/proc/loadavg\b[^\n;&|]*\|\s*$")
# awk options whose value is the NEXT word (`-F ' '`, `-W posix`); an attached value (`-F' '`, `--assign=x=1`) is part
# of the option word. POSIX -F -v -f; mawk/gawk -W; gawk -E -i -l and the long forms getopt also accepts detached.
# Never -e/--source: that value IS the program text (Prism c-ec900d6cb1a6e6e6, c-7339dcf869edcf47).
_AWK_VALUE_OPTS = ("-F", "-v", "-f", "-W", "-E", "-i", "-l",
                   "--field-separator", "--assign", "--file", "--exec", "--include", "--load")


def _awk_loadavg_programs(source: str):
    """(offset, text) of each single-quoted awk PROGRAM fed from /proc/loadavg in the same command.

    The program is the first operand word, never an option value (Prism c-a4131cd75d88ece3: `awk -F' ' '$1 > 8'`
    and `awk -F ' ' ...` took the separator ' ' as the program). Words are read shell-style up to an unquoted
    `|`, `;`, `&` or newline."""
    for awk in _AWK_CMD.finditer(source):
        line_start = source.rfind("\n", 0, awk.start()) + 1
        piped = bool(_AWK_PIPED_FROM_LOADAVG.search(source, line_start, awk.start()))
        i, n, words = awk.end(), len(source), []
        while i < n and source[i] not in "\n|;&":
            if source[i] in " \t":
                i += 1
                continue
            start, quoted = i, []
            while i < n and source[i] not in " \t\n|;&":
                if source[i] in "'\"":
                    close = source.find(source[i], i + 1)
                    if close < 0 or "\n" in source[i:close]:
                        i = n
                        break
                    quoted.append((i + 1, source[i + 1:close]))
                    i = close + 1
                else:
                    i += 1
            words.append((source[start:i], quoted))
        program, expect_value = None, False
        for k, (word, quoted) in enumerate(words):
            if program is None:
                if expect_value:
                    expect_value = False
                elif word in _AWK_VALUE_OPTS:
                    expect_value = True
                elif word.startswith("-"):
                    pass
                else:
                    program = (k, quoted[0] if word.startswith("'") and len(quoted) == 1 else None)
        if program is None or program[1] is None:
            continue
        rest = " ".join(w for w, _ in words[program[0] + 1:])
        if piped or re.search(r"/proc/loadavg\b", rest):
            yield program[1]
_AWK_LOAD_FIELD = re.compile(r"\$[123](?!\d)")


# Word operators that join two atoms into ONE expression (`x as T`, `a in b`).
_JOINING_WORD = r"(?:if|elif|while|until|return|test|and|or|not|as|satisfies|is|in|instanceof|typeof|await)"


def _operand(source: str, pos: int, direction: int, quotes=None, nest=None, lang: str = "py", braces=()) -> str:
    """Walk an operand to syntax boundaries, without truncating identifiers.

    Balanced groups retain internal boolean fallbacks. Unmatched enclosing
    delimiters, argument/statement separators and adjacent atoms end the walk;
    literal atoms retain metric keys without exposing their punctuation. A
    newline inside an open (/[ group (``nest``) continues the expression.
    Python has no brace blocks or pipelines: there code braces (``braces``,
    never an f-string replacement field) group like `()` (dict and set
    displays) and `|` is bitwise OR, an operator inside the operand (Go too).
    """
    limit = 0 if direction < 0 else len(source)
    cursor, depth = pos, 0
    while cursor != limit:
        index = cursor - 1 if direction < 0 else cursor
        char = source[index]
        if quotes and index in quotes:
            other = quotes[index]
            if (other - index) * direction < 0:
                break
            cursor = other if direction < 0 else other + 1
            continue
        opening, closing = (")]", "([") if direction < 0 else ("([", ")]")
        if index in braces:
            opening, closing = (opening + "}", closing + "{") if direction < 0 else (opening + "{", closing + "}")
        if char in opening:
            depth += 1
        elif char in closing:
            if depth == 0:
                break
            depth -= 1
        if depth == 0:
            if char == '\n' and not (nest and nest[index]):
                fragment = source[index + 1:pos] if direction < 0 else source[pos:index]
                if fragment.strip():
                    break
            # Python and Go bind `|` tighter than a comparison (Prism c-b7b8d1e87411456b): there it is inside the
            # operand. Elsewhere it is a pipe or a lower-precedence bitwise OR.
            if char in ',:;{}' and index not in braces or char == '|' and lang not in ("py", "go"):
                break
            if char.isspace():
                # Only the nearest non-blank neighbours matter; never copy the
                # whole prefix/suffix (quadratic on large files).
                b_end = index
                while b_end > 0 and source[b_end - 1].isspace():
                    b_end -= 1
                a_start = index + 1
                while a_start < len(source) and source[a_start].isspace():
                    a_start += 1
                before = source[max(0, b_end - 16):b_end]
                after = source[a_start:a_start + 16]
                # Adjacent atoms are separate arguments/statements, not an
                # arithmetic operand (e.g. `sed "...load..." 2>/dev/null`).
                if (before and after and
                        (before[-1].isalnum() or before[-1] in "_)]}\"'") and
                        (after[0].isalnum() or after[0] in "_$\"'") and
                        not re.search(r'\b' + _JOINING_WORD + r'$', before) and
                        not re.match(_JOINING_WORD + r'\b', after)):
                    break
            boundary = _BOUNDARY.match(source, index)
            if boundary is not None or source.startswith(('>=', '<='), index - 1):
                break
        cursor += direction
    return source[cursor:pos] if direction < 0 else source[pos:cursor]


def _py_tokens(text: str, lines: list, source: str):
    """(bracket pairs, string spans) as offsets into ``source``, from the
    Python tokenizer over the ORIGINAL text, so docstrings, f-strings and
    comments never pair; None when the text does not tokenize. ``lines`` are
    the comment-stripped physical lines ``source`` was joined from (columns
    before a stripped comment are unchanged; backslash-newline folds shift)."""
    import io
    import tokenize
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line) + 1)
    joined = "\n".join(lines)
    folds = [m.start() for m in re.finditer(r"\\\n", joined)]   # t_89091ec3: one regex pass, not a startswith per char

    def offset(row, col, end=False):
        if row - 1 >= len(lines):
            return None
        col = min(col, len(lines[row - 1])) if end else col
        if col > len(lines[row - 1]) or (not end and col == len(lines[row - 1])):
            return None
        at = starts[row - 1] + col
        return min(at - bisect.bisect_left(folds, at), len(source))

    stack, pairs, strings, fstack = [], {}, [], []
    f_start = {getattr(tokenize, n) for n in ("FSTRING_START", "TSTRING_START") if hasattr(tokenize, n)}
    f_end = {getattr(tokenize, n) for n in ("FSTRING_END", "TSTRING_END") if hasattr(tokenize, n)}
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.OP and tok.string in "()[]":
                at = offset(*tok.start)
                at = at if at is not None and source[at:at + 1] == tok.string else None
                if tok.string in "([":
                    stack.append(at)
                elif stack:
                    start = stack.pop()
                    if start is not None and at is not None:
                        pairs[start] = at
            elif tok.type == tokenize.STRING:
                a, b = offset(*tok.start), offset(*tok.end, end=True)
                if a is not None and b is not None:
                    strings.append((a, b))
            elif tok.type in f_start:
                fstack.append(offset(*tok.start))
            elif tok.type in f_end and fstack:
                a, b = fstack.pop(), offset(*tok.end, end=True)
                if a is not None and b is not None:
                    strings.append((a, b))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        return None
    return pairs, sorted(strings)


def hits_in(text: str, lang: str = "py") -> int:
    """Count every host-load comparison occurrence, independent of statements.

    Literal and computed metric names intentionally count too. This ratchet
    tolerates conservative overcounting; unknown syntax must not hide a gate.
    Only complete single-line trailing telemetry waivers can remove source.
    """
    if not _MAY_COUNT.search(text):
        return 0   # t_89091ec3: nothing here can count; skip the per-line comment strip
    lines, bind_lines = [], []
    depth = 0
    for row, (code, bare, comment, clean) in enumerate(_code_lines(text, lang)):
        before_depth = depth
        for char in bare:
            if char in "([":
                depth += 1
            elif char in ")]":
                depth = max(0, depth - 1)
        # The waiver is the text of the REAL trailing comment the one pass found (Prism c-6b306bcf46008466: never
        # the first marker substring, which may sit inside a string).
        if (clean and comment is not None and WAIVER in comment and before_depth == depth == 0 and
                not bare.rstrip().endswith("\\") and _waiver_owner(row, row, row, bare) is not None):
            lines.append("")
        else:
            lines.append(code)
        bind_lines.append(code)
    source = "\n".join(lines).replace("\\\n", " ")
    # A waived line is not a comparison, but its assignment still binds a name (Prism c-399008e58416e4d9): binding
    # discovery reads every line, so `busy = s.get("cpu_busy")  # waiver` then `if busy < 90:` still counts.
    bind_source = "\n".join(bind_lines).replace("\\\n", " ")
    quotes = {}
    # Pair quote delimiters forward once; walking backwards from an operator
    # inside a literal must stop at its opener, not jump into another statement.
    # A shell parameter expansion `${...}` is one atom, like a literal: its
    # braces are not the statement braces that end an operand.
    atom = r'''"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\\n])*`'''
    if lang == "sh":
        atom = r"\$\{[^{}\n]*\}|" + atom
    for literal in re.finditer(atom, source):
        start, end = literal.start(), literal.end() - 1
        quotes[start], quotes[end] = end, start
    # Innermost MATCHED (/[ pair around each index, literals skipped: a newline
    # inside that pair (a parenthesized multi-line condition) does not end the
    # expression. Only matched pairs count, so a stray bracket in prose cannot
    # shift every later line (a running depth counter drifts by file end).
    may = bool(_MAY_COMPARE.search(source)) or (lang == "sh" and "load" in source) or (
        bind_source != source and bool(_MAY_COMPARE.search(bind_source)))
    pairs, stack, i = {}, [], 0
    # Python: bracket tokens from the tokenizer (docstrings, f-strings and
    # prose cannot pair). Other languages, or untokenizable text: the scanner.
    toks = _py_tokens(text, lines, source) if lang == "py" and may else None
    if toks is not None:
        pairs.update(toks[0])
        i = len(source)
    skip = quotes
    while may and i < len(source):
        if i in skip and skip[i] > i:
            i = skip[i] + 1
            continue
        char = source[i]
        if char in "([":
            stack.append(i)
        elif char in ")]" and stack and source[stack[-1]] == "([" [")]".index(char)]:
            pairs[stack.pop()] = i
        i += 1
    nest, open_until = [False] * len(source), []
    for i in range(len(source) if may else 0):
        while open_until and open_until[-1] < i:
            open_until.pop()
        if i in pairs:
            open_until.append(pairs[i])
        nest[i] = bool(open_until) and i not in pairs and open_until[-1] != i
    count = 0
    compared_spans = []
    # A name bound straight from a load metric IS that metric in the file:
    # `load = os.getloadavg()[0]`, `l1, l5, l15 = os.getloadavg()`, and in
    # Python `cb = s.get("cpu_busy")` / `cb, run = s.get("cpu_busy"), x`
    # (targets pair with top-level RHS items; a single RHS binds every target
    # only when it is a bare getloadavg() call).
    bound = set()
    for assign in re.finditer(r"^[ \t]*([A-Za-z_][\w, \t]*?)[ \t]*=(?!=)[ \t]*([^\n]*)", bind_source, re.M):
        names = [n for n in re.split(r"[\s,]+", assign.group(1)) if n]
        rhs = assign.group(2)
        items, level, last, quote, escaped = [], 0, 0, None, False
        for k, char in enumerate(rhs):
            # `;` and `,` inside a string literal neither end the statement
            # nor split tuple items: `label, busy = "host;cpu", s.get(...)`.
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
                continue
            if char in "\"'":
                quote = char
                continue
            level += char in "([{"
            level -= char in ")]}"
            if char == ";" and level == 0:
                rhs = rhs[:k]   # the statement ends here; what follows binds nothing
                break
            if char == "," and level == 0:
                items.append(rhs[last:k])
                last = k + 1
        items.append(rhs[last:])
        loadavg = re.compile(r"^\s*(?:os\.)?getloadavg\(\)")
        # The item IS a metric read: getloadavg(), or (Python) a keyed read of a
        # metric optionally wrapped in a number cast: `s.get("cpu_busy")`,
        # `float(d["load1"] or 0)`. Prose and expressions that merely MENTION
        # a metric do not bind.
        keyed = re.compile(r"^\s*(?:(?:float|int|_num)\()?[A-Za-z_][\w.]*(?:\.get\(\s*|\[\s*)[\"']" +
                           METRIC + r"[\"']")
        metric_read = lambda item: bool(loadavg.match(item) or (lang == "py" and keyed.match(item)) or  # noqa: E731
                                        (lang == "sh" and re.search(r"/proc/loadavg|\bvm\.loadavg\b", item)))
        if len(names) == 1:
            bound.update(names if metric_read(rhs) else [])
        elif len(items) == 1 and loadavg.match(rhs) and not rhs[loadavg.match(rhs).end():].strip():
            bound.update(names)
        elif len(items) == len(names):
            bound.update(n for n, item in zip(names, items) if metric_read(item))
    if lang == "sh":
        # Shell `read` from /proc/loadavg (Prism c-c72ba317243173bb): `read -r one five fifteen _ < /proc/loadavg`
        # binds the three load fields; a read fed by a command that reads /proc/loadavg (`< <(ssh h 'read l _
        # </proc/loadavg; echo "$l $(nproc)"')`) binds its first name, the field the command echoes first.
        for read in re.finditer(r"\bread\b((?:[ \t]+-[A-Za-z]+)*)((?:[ \t]+[A-Za-z_]\w*)+)[ \t]*(<[^\n]*)",
                                bind_source):
            # Slice by field position first, then drop `_` placeholders (Prism 3a5d25cf0633: filtering first shifted
            # `read _ _ _ procs pid` onto the load fields).
            names = read.group(2).split()
            rest = read.group(3)
            if re.match(r"<\s*/proc/loadavg\b", rest):
                bound.update(n for n in names[:3] if n != "_")
            elif re.search(r"/proc/loadavg\b", rest):
                bound.update(n for n in names[:1] if n != "_")
    bound = {n for n in bound if len(n) > 1}   # `p`, `n`: loop scratch names get rebound
    # Shell: an awk program is a literal, but `awk -v l="$load1" '... l < m ...'`
    # compares the load under an alias. Aliases of a load variable (metric-named
    # or bound above) count inside that awk command's own line.
    awk_aliases = []
    if lang == "sh":
        for awk in re.finditer(r"\bawk\b[^\n]*", source):
            names = [m.group(1) for m in re.finditer(
                r"-v\s+([A-Za-z_]\w*)\s*=\s*[\"']?\$\{?([A-Za-z_]\w*)", awk.group())
                if m.group(2) in bound or re.fullmatch(METRIC + "|" + LOAD_VARIABLE, m.group(2))]
            if names:
                awk_aliases.append((awk.start(), awk.end(), re.compile(
                    r"(?<![\w$])(?:" + "|".join(map(re.escape, names)) + r")\b")))
    # A bound name is code: it counts outside string literals only (unlike a
    # literal metric KEY, which counts conservatively anywhere).
    spans = []
    for a, b in (toks[1] if toks is not None else sorted((a, b) for a, b in quotes.items() if b > a)):
        if spans and a <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(spans[-1][1], b))   # f-string nesting: keep the outer span
        else:
            spans.append((a, b))
    starts_ = [a for a, _ in spans]

    def in_literal(at):
        k = bisect.bisect_right(starts_, at) - 1
        return k >= 0 and spans[k][0] <= at < spans[k][1]
    braces = frozenset(i for i, char in enumerate(source)
                       if char in "{}" and not in_literal(i)) if lang == "py" and may else frozenset()
    bound_rx = re.compile(r"(?<![.\w])(?:" + "|".join(map(re.escape, sorted(bound))) + r")\b(?!\s*\()") if bound else None
    counted_ops = set()
    for op in _COMPARISON.finditer(source) if may else ():
        if op.start() and source[op.start() - 1:op.start() + 1] in ('<<', '>>'):
            continue  # <= matched at the second character of <<= is not relational
        # Shell file-descriptor redirects are not relational operators.
        if lang == 'sh' and (source[op.end():].lstrip().startswith(('/', '&', '<(', '>(')) or
                (op.group() == '<' and source[op.end():].lstrip().startswith('(') and
                 source[:op.start()].rstrip().endswith('<'))):
            continue
        # Boolean fallbacks inside a balanced operand are not separators.
        left = _operand(source, op.start(), -1, quotes, nest, lang, braces)
        right = _operand(source, op.end(), 1, quotes, nest, lang, braces)
        if _METRIC_ANY_CASE.search(left) or _METRIC_ANY_CASE.search(right) or (lang == "sh" and
                (_METRIC_NAME.search(left) or _METRIC_NAME.search(right))) or (
                bound_rx and not in_literal(op.start()) and (bound_rx.search(left) or bound_rx.search(right))) or any(
                start <= op.start() < end and (alias.search(left) or alias.search(right))
                for start, end, alias in awk_aliases):
            count += 1
            compared_spans.append((op.start() - len(left), op.end() + len(right)))
            counted_ops.add(op.start())
    if lang == "sh":
        for base, program in _awk_loadavg_programs(source):
            for op in _COMPARISON.finditer(program):
                if base + op.start() in counted_ops:
                    continue
                left = _operand(program, op.start(), -1)
                right = _operand(program, op.end(), 1)
                if _AWK_LOAD_FIELD.search(left) or _AWK_LOAD_FIELD.search(right):
                    count += 1
    # Only a getloadavg() operand already represented by a counted comparison
    # is redundant. Sizing signals remain independent of other comparisons.
    for special in _SPECIAL.finditer(source):
        if not (special.group().startswith('os.getloadavg(') and
                any(start <= special.start() < end for start, end in compared_spans)):
            count += 1
    return count


# JS/TS: a `/` starts a regex literal where an operand is expected: at line start, after an operator/opening punctuator,
# or after a keyword that takes an expression. Anywhere else (after an identifier, a number, `)` or `]`) it divides.
# A doubled `++`/`--` is a postfix operator on a complete operand, so a `/` after it divides too (Prism c-d55cc5e55f7c).
_JS_REGEX_PREV = re.compile(r"(?:^|[(,=:\[!&|?{};*%<>~^]|(?<![+\-])[+\-]|"
                            r"\b(?:return|typeof|case|do|else|in|of|new|delete|void|"
                            r"throw|instanceof|yield|await))\s*$")
# A `)` closing a control-statement head (`if (ok) /re/.test(s)`) is followed by a statement, so a `/` there starts a
# regex, not a division (Prism c-481aaa7c3b8a1df5 / c-825d17c9def9d4bc; fa048d07 counted the gate below it).
_JS_CONTROL_HEAD = re.compile(r"(?<![.\w$])(?:if|while|for|with)\s*$")


def _js_after_control_head(bare: str) -> bool:
    """``bare`` (the line's code outside literals) ends with the `)` of an if/while/for/with head on this line."""
    bare = bare.rstrip()
    if not bare.endswith(")"):
        return False
    depth = 0
    for k in range(len(bare) - 1, -1, -1):
        depth += {")": 1, "(": -1}.get(bare[k], 0)
        if depth == 0:
            m = _JS_CONTROL_HEAD.search(bare, 0, k)
            # `obj. if(x) / n` is a property call, not a control head (Prism c-dbaa89196860cb6c): `.`/`?.` may be
            # separated from the keyword by whitespace.
            return bool(m) and not bare[:m.start()].rstrip().endswith(".")
    return False


def _js_regex_end(line: str, i: int):
    """Index just past the regex literal opening at ``line[i] == '/'``, or None when it does not close on the line.
    `\\` escapes one char; a `/` inside a `[...]` class does not close it."""
    k, in_class = i + 1, False
    while k < len(line):
        c = line[k]
        if c == "\\":
            k += 2
            continue
        if c == "[":
            in_class = True
        elif c == "]":
            in_class = False
        elif c == "/" and not in_class:
            k += 1
            while k < len(line) and (line[k].isalnum() or line[k] == "_"):
                k += 1   # flags
            return k
        k += 1
    return None


def _code_lines(text: str, lang: str) -> list:
    """ONE comment/string pass over the file (t_91299393): per physical line (code, bare, comment, clean).

    ``code`` is the line with comments removed and string literals KEPT (a literal metric key still counts); a block
    comment is blanked to spaces, so `cpu_busy /* 0..100 */ > 90` keeps its operand and Python columns never move.
    ``bare`` is the code outside every literal (delimiter counting, waiver ownership). ``comment`` is the text of the
    line's real trailing comment, or None. ``clean``: the line opens and closes outside any multi-line literal/comment.
    Carried across lines: Python triple quotes (Prism c-4aa8c4b368f46149), C-family block comments (c-c278ea1076b23728)
    and backtick literals (Go raw strings, JS templates). Other quotes close at end of line, as before.
    Markers: Python `#`; shell `#` starting a word (never `$#`, `${#a[@]}`, `foo#bar`); C-family `//` and `/* */`
    (neither after `\\`, a JS regex); JS/TS also a `#!` first line."""
    cfam = lang in ("c", "go", "js", "ts", "tsx")
    out = []
    state = None                      # '"""' / "'''" (py), '*/' (block comment), '`' (go/js)
    for line in text.splitlines():
        opened = state
        code, bare, comment, q, ansi = [], [], None, None, False
        subst = []                    # sh: open `$(` inside "..." as [paren depth]; its body is code again (c-42258d231e80ff9e)
        i, n = 0, len(line)
        if lang in ("js", "ts", "tsx") and state is None and line.lstrip().startswith("#!"):
            out.append(("", "", line.lstrip()[2:], True))
            continue
        while i < n:
            c = line[i]
            if state == "*/":
                if line.startswith("*/", i):
                    code.append("  ")
                    i, state = i + 2, None
                else:
                    code.append(" ")
                    i += 1
                continue
            if state is not None or q is not None:
                close = state or q
                # Shell '...' has no escapes; ANSI-C $'...' does (Prism c-05e376818095d77a: `$'can\'t #'`).
                if c == "\\" and not (close == "`" and lang == "go") and not (
                        close == "'" and lang == "sh" and not ansi):
                    code.append(line[i:i + 2])
                    i += 2
                    continue
                if q == '"' and lang == "sh" and line.startswith("$(", i):
                    # `"$(echo "$x # y")"`: the inner `"` opens a new quote, so its ` #` is literal (Prism
                    # c-42258d231e80ff9e). The body is scanned as code until the matching `)` returns to the `"`.
                    subst.append(0)
                    code.append("$(")
                    i, q = i + 2, None
                    continue
                if line.startswith(close, i):
                    code.append(close)
                    i += len(close)
                    state, q = (None, q) if state else (state, None)
                    continue
                code.append(c)
                i += 1
                continue
            if lang == "py" and line.startswith(('"""', "'''"), i):
                state = line[i:i + 3]
            elif c == "`" and (lang == "go" or lang in ("js", "ts", "tsx")):
                state = c
            elif c in "'\"" or (c == "`" and lang == "sh"):
                q = c
                # `$` is escaped only by an ODD run of backslashes (Prism 5dd7adcb5739: `\\$'...'` is still ANSI-C).
                ansi = (lang == "sh" and c == "'" and line[i - 1:i] == "$" and
                        (len(line[:i - 1]) - len(line[:i - 1].rstrip("\\"))) % 2 == 0)
            elif (lang in ("js", "ts", "tsx") and c == "/" and line[i + 1:i + 2] not in ("/", "*") and
                  (_JS_REGEX_PREV.search("".join(code)) or _js_after_control_head("".join(bare))) and
                  (end := _js_regex_end(line, i)) is not None):
                # A regex literal where an operand is expected (Prism 2bd37a842cdd): its body (`/[/*]/`) never opens a
                # comment. Kept in ``code`` like any literal; an unclosed one falls through as a plain `/`.
                code.append(line[i:end])
                i = end
                continue
            elif cfam and line.startswith("/*", i) and not (i and line[i - 1] == "\\"):
                state = "*/"
                code.append("  ")
                i += 2
                continue
            elif cfam and line.startswith("//", i) and not (i and line[i - 1] == "\\"):
                comment = line[i + 2:]
                break
            elif not cfam and c == "#" and (lang != "sh" or i == 0 or line[i - 1] in " \t;("):
                comment = line[i + 1:]
                break
            if state is not None or q is not None:
                opener = state if state and state != "*/" else c
                code.append(opener)
                i += len(opener)
                continue
            if subst and c in "()":
                if c == ")" and not subst[-1]:
                    subst.pop()
                    q = '"'           # back inside the enclosing double quote
                    code.append(c)
                    i += 1
                    continue
                subst[-1] += 1 if c == "(" else -1
            code.append(c)
            if not subst:             # a substitution body sits inside a literal: never bare
                bare.append(c)
            i += 1
        out.append(("".join(code), "".join(bare), comment, opened is None and state is None))
    return out


_LANG_BY_SUFFIX = {".py": "py", ".sh": "sh", ".bash": "sh", ".zsh": "sh", ".js": "js", ".ts": "ts", ".go": "go",
                   ".mjs": "js", ".cjs": "js", ".jsx": "js", ".tsx": "tsx"}


def _lang_of_text(rel, raw: bytes) -> str:
    """py / sh / c from the suffix, else from the shebang, else py (the conservative marker set: `#` only)."""
    import os
    suf = os.path.splitext(str(rel))[1].lower()
    if suf in _LANG_BY_SUFFIX:
        return _LANG_BY_SUFFIX[suf]
    first = raw.split(b"\n", 1)[0].decode("utf-8", "replace")
    if first.startswith("#!") and ("sh" in first.rsplit("/", 1)[-1] or "bash" in first):
        return "sh"
    return "py"
    if first.startswith("#!") and ("sh" in first.split("/")[-1] or "bash" in first):
        return "sh"
    return "py"


def index_blob(root: Path, rel: str) -> bytes:
    """Missing tracked source is not a deletion until removed from the index."""
    env = _git_env()
    entry = subprocess.run(["git", "--literal-pathspecs", "-C", str(root), "ls-files", "--stage", "-z", "--", rel],
                           capture_output=True, env=env)
    records = entry.stdout.rstrip(b"\0").split(b"\0")
    header, _, name = records[0].partition(b"\t")
    fields = header.split()
    if (entry.returncode or len(records) != 1 or len(fields) != 3 or fields[2] != b"0" or
            name != rel.encode() or fields[0] not in (b"100644", b"100755")):
        raise ScanError(f"{rel}: unreadable worktree file; no regular index blob (missing symlink target or conflict)")
    blob = subprocess.run(["git", "-C", str(root), "cat-file", "blob", fields[1].decode("ascii")],
                          capture_output=True, env=env)
    if blob.returncode:
        raise ScanError(f"{rel}: index blob not readable")
    return blob.stdout


def scan(root: Path, scan_dirs=SCAN, library=LIBRARY, require_git=False) -> dict:
    out = {}
    for rel in tracked(root, scan_dirs, require_git=require_git):
        if rel in library or rel in SELF or SKIP.search(rel) or not is_live_source(rel):
            continue
        try:
            raw = (root / rel).read_bytes()
        except FileNotFoundError:
            raw = index_blob(root, rel)
        except OSError as exc:
            raise ScanError(f"{rel}: unreadable ({exc})") from exc
        if b"\0" in raw[:2048]:
            continue
        try:
            n = hits_in(raw.decode("utf-8", errors="replace"), _lang_of_text(rel, raw))
        except ScanError as exc:
            raise ScanError(f"{rel}: {exc}") from exc
        if n:
            out[rel] = n
    return out


def config(doc: dict):
    """(scan dirs, library paths) a baseline declares; hermes-home's defaults when absent."""
    dirs = doc.get("scan", SCAN)
    if not isinstance(dirs, (list, tuple)) or not dirs:
        raise ScanError("scan must be a nonempty list of literal relative directories")
    for d in dirs:
        if (not isinstance(d, str) or not d or d.startswith("/") or
                any(c in d for c in ":*?[]\\\n\r\0") or ".." in d.split("/") or
                (d != "." and any(part in ("", ".") for part in d.split("/")))):
            raise ScanError(f"scan path must be a literal relative directory: {d!r}")
    library = doc.get("library")
    if not isinstance(library, list) or not all(isinstance(p, str) for p in library):
        raise ScanError("library is required and must be a list of repo-relative paths")
    return tuple(dirs), frozenset(library)


def _show(root: Path, ref: str, rel: str, _seen=()):
    """File text at ref; only a confirmed absent tree entry returns None. Unreadable objects abort."""
    if rel in _seen or len(_seen) >= 40:
        raise ScanError(f"--base-ref {ref}: symlink cycle at {rel}")
    env = _git_env()
    ok = subprocess.run(["git", "-C", str(root), "cat-file", "-e", f"{ref}^{{commit}}"], capture_output=True, env=env)
    if ok.returncode != 0:
        raise ScanError(f"--base-ref {ref}: commit not readable (fetch it first)")
    tree = subprocess.run(["git", "--literal-pathspecs", "-C", str(root), "ls-tree", "-z", ref, "--", rel],
                          capture_output=True, env=env)
    if tree.returncode != 0:
        raise ScanError(f"--base-ref {ref}: {rel}: tree not readable (fetch it first)")
    if not tree.stdout:
        return None
    entries = tree.stdout.rstrip(b"\0").split(b"\0")
    header, _, name = entries[0].partition(b"\t")
    fields = header.split()
    if len(entries) != 1 or len(fields) != 3 or fields[1] != b"blob" or name != rel.encode("utf-8"):
        raise ScanError(f"--base-ref {ref}: {rel}: expected one blob entry")
    p = subprocess.run(["git", "-C", str(root), "cat-file", "blob", fields[2].decode("ascii")],
                       capture_output=True, env=env)
    if p.returncode != 0:
        raise ScanError(f"--base-ref {ref}: {rel}: blob not readable (fetch it first)")
    text = p.stdout.decode("utf-8", errors="replace")
    if fields[0] == b"120000":
        import posixpath
        target = posixpath.normpath(posixpath.join(posixpath.dirname(rel), text))
        if target.startswith(("/", "../")) or target == "..":
            raise ScanError(f"--base-ref {ref}: symlink escapes tree: {rel}")
        return _show(root, ref, target, (*_seen, rel))
    return text


def _moved_from(root: Path, ref: str) -> dict:
    """{new path: source path at ref} for files git sees as renamed or copied between ref and the work tree."""
    p = subprocess.run(["git", "-C", str(root), "diff", "--no-ext-diff", "--find-copies", "--name-status", "-z", ref],
                       capture_output=True, env=_git_env())
    if p.returncode != 0:
        raise ScanError(f"--base-ref {ref}: rename detection failed ({p.stderr.decode(errors='replace').strip()})")
    fields, out, i = p.stdout.decode("utf-8", "surrogateescape").split("\0"), {}, 0
    while i < len(fields) and fields[i]:
        if fields[i][0] in "RC":
            out[fields[i + 2]] = fields[i + 1]
            i += 3
        else:
            i += 2
    return out


def baseline_drift(root: Path, ref: str, head_doc: dict) -> list:
    """The baseline may only go DOWN against the PR base (t_22955b73). A row added or raised is allowed only up to the
    hits that file ALREADY had in the base tree (recording code that is on main, never code this PR adds)."""
    config(head_doc)  # validate even when the base has no baseline yet
    raw = _show(root, ref, BASELINE_REL.as_posix())
    if raw is None:
        print(f"admission-decider-lint: no baseline at base {ref[:12]}: first baseline, nothing to compare")
        return []
    try:
        base_doc = json.loads(raw)
    except ValueError as exc:
        raise ScanError(f"baseline at base {ref[:12]} unparseable ({exc})") from exc
    bad = []
    # Pre-migration home baselines omitted library; preserve the old fixed scope only
    # for historical comparison. Current baselines must always declare it.
    if "library" not in base_doc:
        base_doc["library"] = sorted(LIBRARY)
    (b_scan, b_lib), (h_scan, h_lib) = config(base_doc), config(head_doc)
    if h_lib - b_lib:
        bad.append(f"library widened against base: {sorted(h_lib - b_lib)}")
    if "." not in h_scan and set(b_scan) - set(h_scan):
        bad.append(f"scan narrowed against base: {sorted(set(b_scan) - set(h_scan))}")
    b_files = base_doc.get("files", {})
    moved = _moved_from(root, ref)
    for rel, n in sorted(head_doc.get("files", {}).items()):
        was = b_files.get(rel)
        if was is not None and n <= was:
            continue
        text = _show(root, ref, rel)
        if text is None and rel in moved:  # a rename/copy (t_7be48220): the moved code was on main under its old name
            text = _show(root, ref, moved[rel])
        on_base = hits_in(text, _lang_of_text(rel, text.encode("utf-8", "replace"))) if text is not None else 0
        if n <= on_base:
            continue                  # code already on main, now recorded
        bad.append(f"{rel}: baseline row added ({n})" if was is None else f"{rel}: baseline raised {was} -> {n}")
    return bad


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--update", action="store_true", help="rewrite the baseline DOWN to the current counts")
    ap.add_argument("--base-ref", default="", help="PR base commit: refuse a baseline that grew against it")
    a = ap.parse_args(argv)
    bp = a.root / BASELINE_REL
    try:
        doc = json.loads(bp.read_text(encoding="utf-8"))
        base = doc.get("files", {})
    except (OSError, ValueError) as exc:
        print(f"admission-decider-lint: FAIL baseline {bp} unreadable ({exc})")
        return 2
    try:
        scan_dirs, library = config(doc)
        if a.base_ref and set(a.base_ref) == {"0"}:
            raise ScanError(f"--base-ref {a.base_ref}: null sha")
        now = scan(a.root, scan_dirs, library, require_git=a.update)
        grew = baseline_drift(a.root, a.base_ref, doc) if a.base_ref else []
    except ScanError as exc:
        print(f"admission-decider-lint: FAIL scan aborted, baseline untouched: {exc}")
        return 2
    bad = []
    for rel, n in sorted(now.items()):
        was = base.get(rel)
        if was is None:
            bad.append(f"{rel}: {n} new host-load comparison(s), not in the baseline")
        elif n > was:
            bad.append(f"{rel}: {n} host-load comparisons, baseline {was}")
    stale = [f"{rel}: {now.get(rel, 0)} host-load comparisons, baseline {n} (lower the baseline in this PR: --update)"
             for rel, n in sorted(base.items()) if now.get(rel, 0) < n]
    if grew:
        print("admission-decider-lint: FAIL, the baseline may only go DOWN (t_22955b73):")
        for b in grew:
            print(f"  {b}")
        return 1
    if a.update:
        if bad:
            print("admission-decider-lint: --update refuses to raise the baseline:")
            for b in bad:
                print(f"  {b}")
            return 1
        doc = json.loads(bp.read_text(encoding="utf-8"))
        doc["files"] = dict(sorted(now.items()))
        bp.write_text(json.dumps(doc, indent=1, sort_keys=False) + "\n", encoding="utf-8")
        print(f"admission-decider-lint: baseline rewritten ({len(now)} files, {sum(now.values())} hits)")
        return 0
    if bad or stale:
        if bad:
            print("admission-decider-lint: FAIL, a host-admission decision outside the ONE library (t_e5124426):")
            for b in bad:
                print(f"  {b}")
            print("  remedy: ask placement.may_start(host, consumer) / admission.admit_class(); a monitor that never "
                  f"gates work marks the line `# {WAIVER} <why>`")
        if stale:
            print("admission-decider-lint: FAIL, the baseline is above the code; lower it in the same PR (t_22955b73):")
            for b in stale:
                print(f"  {b}")
        return 1
    print(f"admission-decider-lint: ok ({len(now)} grandfathered files, {sum(now.values())} hits == baseline)")
    return 0


if __name__ == "__main__":
    sys.exit(main())