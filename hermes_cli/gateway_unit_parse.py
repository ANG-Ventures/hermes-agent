"""One reader of systemd unit-file grammar for the gateway service-definition guards (t_8749a807).

Vendored VERBATIM from the fleet lint ``hermes-home/scripts/lib/gateway_unit_lint.py`` (``unit_assignments``,
``_unit_words``, ``_env_file_vars``): the lint and the writers' ownership check must never disagree on what a
line means. Only ``pinned_hermes_home`` is new here; it applies the lint's ``parse_systemd`` environment
rules to one question: which ``HERMES_HOME`` does this unit's EFFECTIVE text pin?

Rules (systemd.syntax / systemd.exec): ``#``/``;`` lines are comments, also inside a backslash continuation;
only ``[Service]`` assignments count; ``Environment=`` holds several quoted/unquoted ``K=V`` words and a later
word wins; an empty ``Environment=`` resets every earlier one; ``EnvironmentFile=`` bodies (env-file grammar,
``-`` optional, wildcards in sorted order, literal values) override ``Environment=``; ``UnsetEnvironment=``
removes last; ``%%`` is a literal ``%``.
"""

from __future__ import annotations

import glob
import os
import re
from pathlib import Path


# ---- vendored verbatim from hermes-home scripts/lib/gateway_unit_lint.py ----
def unit_assignments(text):
    """[(section, key, value)] for ONE unit file, in order, as systemd's conf-parser reads it."""
    out, section, buf = [], None, None
    for raw in text.splitlines():
        line = raw.strip() if buf is None else raw.lstrip()
        if line[:1] in ("#", ";"):
            continue  # a comment never continues, and one inside a continuation is skipped (it goes on)
        if line.endswith("\\"):
            buf = (buf or "") + line[:-1] + " "
            continue
        line, buf = ((buf or "") + line).strip(), None
        if not line or line[0] in "#;":
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            out.append((section, k.strip(), v.strip()))
    if buf is not None and buf.strip() and buf.strip()[0] not in "#;":
        line = buf.strip()  # a continuation at EOF ends the logical line there; it never runs into the next file
        if "=" in line and not line.startswith("["):
            k, v = line.split("=", 1)
            out.append((section, k.strip(), v.strip()))
    return out


_C_ESC = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "s": " ",
          "\\": "\\", '"': '"', "'": "'"}


def _unit_words(s: str, relax: bool = False) -> list[str]:
    """Split a unit-file value into words like systemd's extract_first_word(EXTRACT_UNQUOTE|EXTRACT_CUNESCAPE):
    whitespace separates, '...' and "..." group (anywhere in a word) and are removed, and C escapes (\\xHH,
    \\nnn octal, \\uXXXX, \\n, \\s, ...) are decoded inside and outside quotes. An unbalanced quote raises
    ValueError (systemd refuses the line) unless *relax* (EXTRACT_RELAX: the word ends at the end)."""
    words, cur, inword, quote, i = [], [], False, None, 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            n = s[i + 1]
            m = (re.match(r"x([0-9a-fA-F]{2})", s[i + 1:]) or re.match(r"([0-7]{3})", s[i + 1:])
                 or re.match(r"u([0-9a-fA-F]{4})", s[i + 1:]) or re.match(r"U([0-9a-fA-F]{8})", s[i + 1:]))
            if m:
                base = 8 if m.group(0)[0] in "01234567" else 16
                cur.append(chr(int(m.group(1), base)))
                i += 1 + len(m.group(0))
            else:
                cur.append(_C_ESC.get(n, n))
                i += 2
            inword = True
            continue
        if quote:
            if c == quote:
                quote = None
            else:
                cur.append(c)
        elif c in "'\"":
            quote, inword = c, True
        elif c.isspace():
            if inword:
                words.append("".join(cur))
                cur, inword = [], False
        else:
            cur.append(c)
            inword = True
        i += 1
    if quote and not relax:
        raise ValueError(f"unbalanced {quote} quote")  # never echo the value: it may hold a credential
    if inword:
        words.append("".join(cur))
    return words


def _env_file_vars(text: str) -> dict:
    """KEY=VALUE pairs of an EnvironmentFile body, read with systemd's env-file grammar (env-file.c
    parse_env_file): a '...' or "..." opened at the START of a value (or right after a closing quote) may span
    lines, a backslash-newline continues an unquoted or double-quoted value, a backslash escapes the next char
    (inside "..." only one of `"\\`$`), `#`/`;` lines are comments, unquoted trailing whitespace is dropped, a
    line without `=` is ignored, and a value still open at EOF ends there."""
    env, st, key, val, trail = {}, "pre_key", "", "", 0

    def push():
        env[key] = val[:len(val) - trail] if trail else val

    for c in text:
        nl = c in "\r\n"
        if st == "pre_key":
            if c in "#;":
                st = "comment"
            elif not c.isspace():
                st, key = "key", c
        elif st == "key":
            if nl:
                st = "pre_key"  # no '=': not an assignment
            elif c == "=":
                st, key, val, trail = "pre_value", key.strip(), "", 0
            else:
                key += c
        elif st == "pre_value":
            if nl:
                push()
                st = "pre_key"
            elif c == "'":
                st, trail = "sq", 0
            elif c == '"':
                st, trail = "dq", 0
            elif c == "\\":
                st, trail = "value_esc", 0
            elif not c.isspace():
                st, val, trail = "value", val + c, 0
        elif st == "value":
            if nl:
                push()
                st = "pre_key"
            elif c == "\\":
                st, trail = "value_esc", 0
            else:  # a quote here is literal
                val += c
                trail = trail + 1 if c.isspace() else 0
        elif st == "value_esc":
            st = "value"
            if not nl:
                val += c
        elif st == "sq":
            if c == "'":
                st = "pre_value"
            else:
                val += c
        elif st == "dq":
            if c == '"':
                st = "pre_value"
            elif c == "\\":
                st = "dq_esc"
            else:
                val += c
        elif st == "dq_esc":
            st = "dq"
            if c in '"\\`$':
                val += c
            elif not nl:
                val += "\\" + c
        elif st == "comment":
            if c == "\\":
                st = "comment_esc"
            elif nl:
                st = "pre_key"
        elif st == "comment_esc":  # systemd >= 254: a backslash does not continue a comment line
            st = "pre_key" if nl else "comment"
    if st not in ("pre_key", "key", "comment", "comment_esc"):
        push()
    return env

# ---- end vendored ----


def pinned_hermes_home(unit_path: Path, account_home: str | None = None) -> str | None:
    """``HERMES_HOME`` pinned by the unit file at *unit_path* (plus its ``EnvironmentFile=`` bodies), as
    systemd resolves it; None when the file is unreadable or pins nothing."""
    try:
        text = unit_path.read_text(encoding="utf-8-sig")
    except (OSError, ValueError):
        return None
    return environment_of([text], account_home or manager_home_for_unit(unit_path)).get("HERMES_HOME") or None


_SYSTEM_UNIT_DIRS = ("/etc/systemd/system", "/run/systemd/system", "/usr/local/lib/systemd/system",
                     "/usr/lib/systemd/system", "/lib/systemd/system")


def manager_home_for_unit(unit_path: Path) -> str:
    """What systemd expands ``%h`` to for the unit at *unit_path*: the home of the user running the service
    manager. System manager: root's home (``User=`` does not change it). User manager: the ACCOUNT home, never
    the caller's ``HOME`` - a scratch process with ``HOME=/srv/scratch`` read another account's
    ``HERMES_HOME=%h/.hermes`` as its own scratch home and passed the ownership check (Prism on #1740).
    Both come from the passwd database by uid (``systemctl --user`` reaches the manager of this process's
    uid), never from ``HOME`` / ``HERMES_REAL_HOME``, which the caller controls."""
    import pwd
    path = str(unit_path)
    uid = 0 if any(path == d or path.startswith(d + "/") for d in _SYSTEM_UNIT_DIRS) else os.getuid()
    try:
        return pwd.getpwuid(uid).pw_dir
    except KeyError:
        # No passwd entry: no home systemd could expand %h to; a value no caller's home can equal.
        return "/nonexistent" if uid else "/root"


def _spec(v: str, account_home: str | None) -> str:
    """Expand the specifiers these fields use (``%h``, ``%%``) in one pass, so ``%%h`` stays a literal ``%h``."""
    return re.sub(r"%(.)", lambda m: (account_home or m.group(0)) if m.group(1) == "h"
                  else ("%" if m.group(1) == "%" else m.group(0)), v)


def environment_of(texts: list[str], account_home: str | None, env_files: dict | None = None) -> dict:
    """The effective ``[Service]`` environment of a unit's texts (fragment, then drop-ins in application order).
    *env_files* maps an expanded ``EnvironmentFile=`` path to its body (``None`` = missing) or, for a wildcard,
    to ``[[path, body], ...]``; when omitted the files are read from disk."""
    env: dict = {}
    refs: list[str] = []
    unset: list[str] = []
    for section, key, val in (a for t in texts for a in unit_assignments(t)):
        if section != "Service":
            continue
        try:
            if key == "Environment":
                if not val:
                    env.clear()
                for tok in _unit_words(val):
                    if "=" in tok:
                        k, v = tok.split("=", 1)
                        env[k] = _spec(v, account_home)
            elif key == "EnvironmentFile":
                refs = refs + [val] if val else []
            elif key == "UnsetEnvironment":
                if not val:
                    unset.clear()
                unset += _unit_words(val)
        except ValueError:
            continue  # unbalanced quote: systemd ignores the line
    for ref in refs:
        optional, path = ref.startswith("-"), _spec(ref.lstrip("-"), account_home)
        if not path:
            continue
        bodies = _env_file_bodies(path, env_files)
        if not bodies and not optional:
            continue  # systemd refuses to start the unit; nothing to pin
        for body in bodies:  # values are literal: systemd expands no specifiers inside an environment file
            env.update(_env_file_vars(body))
    for tok in unset:  # NAME removes the variable, NAME=value only that exact assignment
        k, eq, v = tok.partition("=")
        if not eq or env.get(k) == v:
            env.pop(k, None)
    return env


def _env_file_bodies(path: str, env_files: dict | None) -> list[str]:
    if env_files is not None:
        body = env_files.get(path)
        return [t for _, t in body] if isinstance(body, list) else ([] if body is None else [body])
    paths = sorted(p for p in glob.glob(path) if os.path.isfile(p)) if glob.has_magic(path) else [path]
    bodies = []
    for p in paths:
        try:
            with open(p, encoding="utf-8-sig") as fh:
                bodies.append(fh.read())
        except OSError:
            continue
    return bodies
