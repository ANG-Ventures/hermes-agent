"""Write-time shell lint for ``tools.file_operations_lint``.

Agent-authored one-shot scripts run unattended under macOS ``/bin/bash`` 3.2, where
``set -u`` + an empty ``"${arr[@]}"`` aborts ("unbound variable") and bash-4 syntax
(``declare -A``, ``mapfile``, ``${x,,}``) parses but fails at run time. ``bash -n``
catches neither, so the static rules below report them as a WARNING on the write
result; nothing here ever blocks a write.

Rule semantics are ported from hermes-home ``scripts/fleet-config-lint.py``
``lint_bash32_arrays`` (CI lint for tracked scripts); untracked one-shots never reach it.
"""

import functools
import os
import re
import subprocess
from typing import List, Optional, Tuple

from tools.file_operations_common import LintResult

SHELL_EXTS = frozenset({'.sh', '.bash'})

_NAME = r'[A-Za-z_][A-Za-z_0-9]*'
_ARRAY = re.compile(r'\$\{(' + _NAME + r')\[(?:@|\*)\]\}')
# Forms that already survive an empty array under nounset on bash 3.2.
_GUARDED = re.compile(
    r'\$\{([A-Za-z_][A-Za-z_0-9]*)\[@\]\+"\$\{\1\[@\]\}"\}'
    r'|"\$\{([A-Za-z_][A-Za-z_0-9]*)\[@\]\+\$\{\2\[@\]\}\}"'
    r'|"\$\{([A-Za-z_][A-Za-z_0-9]*)\[@\]:\+\$\{\3\[@\]\}\}"'
    r'|\$\{([A-Za-z_][A-Za-z_0-9]*)\[\*\]\+"\$\{\4\[\*\]\}"\}'
    r'|\$\{[A-Za-z_][A-Za-z_0-9]*\[\*\]-\}'
)
_SET = re.compile(r'(?<![\w-])set\s+([^;\n#]+)')
_SHOPT_NOUNSET = re.compile(r'(?<![\w-])shopt\s+(?:-so|-os|-s\s+-o|-o\s+-s)\s+(?:[\w-]+\s+)*nounset\b')
_NOUNSET_FLAGS = re.compile(r'(?:^|\s)-(?:[A-Za-z]*u[A-Za-z]*|o\s+nounset)(?:\s|$)')
_BASH_SHEBANG = re.compile(r'^#!.*(?:/bash|/env\s+(?:-S\s+)?bash)(?:\s|$)')
# An absolute Homebrew/usr-local bash is >= 4: no empty-array bug, bash-4 syntax is fine.
_MODERN_BASH_SHEBANG = re.compile(r'^#!\s*/(?:opt/homebrew|usr/local)/\S*/bash(?:\s|$)')
_ARRAY_OK_MARKER = re.compile(r'# bash32-array-ok:\s*\S')
_BASH4_RULES: Tuple[Tuple[re.Pattern, str], ...] = (
    (re.compile(r'(?<![\w-])(?:declare|local|typeset)\s+(?:-[A-Za-z]*\s+)*-[A-Za-z]*A'),
     'associative array (declare/local/typeset -A) is bash 4+; /bin/bash 3.2 fails at run time'),
    (re.compile(r'(?<![\w-])(?:mapfile|readarray)(?![\w-])'),
     'mapfile/readarray is bash 4+; use `while IFS= read -r line; do arr+=("$line"); done`'),
    (re.compile(r'\$\{' + _NAME + r'(?:\[[^]]*\])?(?:,,?|\^\^?)'),
     '${x,,}/${x^^} case conversion is bash 4+; use `tr "[:upper:]" "[:lower:]"`'),
)
_BASH4_FIX = 'or use a #!/opt/homebrew/bin/bash shebang'


def shell_lint_enabled() -> bool:
    """``file_lint.shell`` (default on) for the profile serving this call."""
    from hermes_cli.config import load_config_readonly

    section = load_config_readonly().get("file_lint")
    return not isinstance(section, dict) or section.get("shell", True) is not False


def _comment(line: str) -> str:
    """Trailing shell comment of ``line``; never a # inside quotes or a word."""
    quote = None
    escaped = False
    for i, char in enumerate(line):
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif char == quote:
            quote = None
        elif char in "\"'" and quote is None:
            quote = char
        elif char == "#" and quote is None and (i == 0 or line[i - 1].isspace()):
            return line[i:]
    return ""


def _sets_nounset(lines: List[str]) -> bool:
    if lines and _BASH_SHEBANG.match(lines[0]) and _NOUNSET_FLAGS.search(lines[0][2:]):
        return True
    for line in lines:
        code = line[:len(line) - len(_comment(line))]
        if _SHOPT_NOUNSET.search(code):
            return True
        if any(_NOUNSET_FLAGS.search(m.group(1)) for m in _SET.finditer(code)):
            return True
    return False


def shell_dialect(ext: str, first_line: str) -> Optional[str]:
    """``"bash"``/``"sh"`` when this file gets the shell lint, else None. A ``.sh``/``.bash``
    file with no shebang is bash; one whose shebang names another interpreter is not linted."""
    if first_line.startswith("#!"):
        if _BASH_SHEBANG.match(first_line):
            return "bash"
        if ext in SHELL_EXTS and re.match(r'^#!\s*(?:\S*/)?(?:env\s+)?(?:sh|dash|ash)(?:\s|$)', first_line):
            return "sh"
        return None
    return "bash" if ext in SHELL_EXTS else None


def bash32_findings(content: str) -> List[Tuple[int, str]]:
    """``(line, message)`` for every bash-3.2 hazard in a bash script."""
    lines = content.splitlines()
    if lines and _MODERN_BASH_SHEBANG.match(lines[0]):
        return []
    nounset = _sets_nounset(lines)
    findings: List[Tuple[int, str]] = []
    for number, line in enumerate(lines, 1):
        if line.lstrip().startswith("#"):
            continue
        comment = _comment(line)
        code = line[:len(line) - len(comment)]
        if nounset and not _ARRAY_OK_MARKER.search(comment):
            names = sorted({m.group(1) for m in _ARRAY.finditer(_GUARDED.sub("", code))})
            if names:
                fixes = ", ".join(f'${{{n}[@]+"${{{n}[@]}}"}}' for n in names)
                findings.append((number, (
                    "unguarded array expansion under set -u; bash 3.2 aborts on an empty array "
                    f"(use {fixes}, or a trailing # bash32-array-ok: <reason>)")))
        for pattern, message in _BASH4_RULES:
            if pattern.search(code):
                findings.append((number, f"{message} ({_BASH4_FIX})"))
    return findings


@functools.lru_cache(maxsize=1)
def _parse_bash() -> Tuple[Optional[str], str]:
    """(bash executable, label) for ``bash -n``. /bin/bash when it is 3.x (the interpreter
    unattended macOS runs use); else whatever bash is present, labelled with its version."""
    from hermes_platform.resolver import locate_command

    def version(exe: str) -> Optional[str]:
        try:
            return subprocess.run(
                [exe, "-c", 'echo "${BASH_VERSINFO[0]}.${BASH_VERSINFO[1]}"'], stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=5, env={"LC_ALL": "C"}).stdout.strip() or "?"
        except (OSError, subprocess.SubprocessError):
            return None

    system = version("/bin/bash") if os.access("/bin/bash", os.X_OK) else None
    if system and system.startswith("3."):
        return "/bin/bash", f"bash {system}"
    exe = next(iter(locate_command("bash").command), None)
    exe, found = (exe, version(exe)) if exe else ("/bin/bash", system)
    if not found:
        return None, ""
    return exe, f"bash {found} at {exe}, not 3.2: bash-4-only syntax is not parse-checked"


def _parse_check(content: str, dialect: str) -> Tuple[Optional[bool], str]:
    """``bash -n`` (``--posix`` for sh) over ``content`` on stdin. (None, reason) when no bash."""
    exe, label = _parse_bash()
    if exe is None:
        return None, "bash not available: parse check skipped"
    argv = [exe, "--posix", "-n"] if dialect == "sh" else [exe, "-n"]
    try:
        proc = subprocess.run(
            argv, input=content.encode("utf-8", "surrogateescape"), capture_output=True,
            timeout=10, env={"LC_ALL": "C"})
    except subprocess.TimeoutExpired:
        return None, f"bash -n ({label}) timed out: parse check skipped"
    except OSError as exc:
        return None, f"bash -n ({label}) failed to start: {exc}"
    if proc.returncode == 0:
        return True, label
    err = proc.stderr.decode("utf-8", "replace").strip()
    err = re.sub(r'^' + re.escape(exe) + r': ', '', err, flags=re.M)
    return False, f"bash -n ({label}):\n{err}"


def lint_shell(path: str, content: str, dialect: str) -> LintResult:
    """Parse check (error) + bash-3.2 rules (warning) for one shell file."""
    ok, parse_msg = _parse_check(content, dialect)
    warnings = ""
    if dialect == "bash":
        warnings = "\n".join(f"{path}:{line}: {msg}" for line, msg in bash32_findings(content))
    if ok is False:
        return LintResult(success=False, output="\n".join(filter(None, (parse_msg, warnings))))
    message = parse_msg if ok is None else ""
    return LintResult(success=True, warnings=warnings, message=message)
