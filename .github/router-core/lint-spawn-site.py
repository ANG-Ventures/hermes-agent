#!/usr/bin/env python3
# SCHEDULE: manual (CI lint: ci.yml lints-fast step lf_spawn_site_lint; no cron row)
"""lint-spawn-site: every place the fleet SPAWNS work on a host goes through the router (Router Core spec v5 §7.1, card t_3ca3dbc6).

WHY: the router (allocd, Card A) only balances what it is asked about. A spawn that never calls
alloc_client / placement.admit is invisible to the reservation ledger, so the next admit scores that host as
emptier than it is (the 10-12 failures: four generators, four private views of "free"). This lint is the
ratchet that makes the cutover (Card B) finish: today's ungated sites are grandfathered with an expiry, a NEW
ungated site is red.

SPAWN SITE (one per (file, enclosing function, kind); a function with three ssh calls is one key, count 3):
  ssh             subprocess run/Popen/call/check_*/os.system/asyncio.create_subprocess_* whose argv starts
                  with ssh (a literal, a `*ssh*` helper call or name, `ssh_cmd() + [...]`), or a shell line
                  running ssh. A literal non-fleet target (github.com, localhost, 127.0.0.1) is not a spawn.
                  A call through a parameter or local bound to one of those (`run=subprocess.run` default,
                  `r = subprocess.run`, `functools.partial(subprocess.run, ...)`, inherited by nested functions)
                  is that call; a parameter with no such default (a test fake) is not.
  docker-run      `docker run` / `docker container run` (argv or shell), INCLUDING a `docker run` script handed
                  to a `*ssh*` helper in the same function (the ci_runner_pool.start shape)
  systemd-run     systemd-run
  kickstart       launchctl kickstart
  workflow-run    gh workflow run
  runner-mint     a CI runner registration (generate-jitconfig / actions/runners/registration-token)
  kanban-dispatch `hermes|hermes kanban dispatch`
  named           a call to a known spawn entrypoint in another repo's tree (NAMED_SPAWNS: the dispatcher's
                  _default_spawn, Prism's start_arm, browserd's open_session)

GATED (AST/scope based, never line distance): the spawn carries a reservation bound in the SAME function.
  python  the function binds a name from an admit call (`d, rid = placement.admit(...)`,
          `rid = alloc_client.admit(...)`; receiver named *alloc* / *placement* / pl) and the spawn call
          references that name, or passes `reservation_id=`, or its env carries ALLOC_RESERVATION (a literal
          in the call, or an env name the function filled with ALLOC_RESERVATION).
  bash    the function (or top level) sets ALLOC_RESERVATION from `alloc-admit` BEFORE the spawn line.

ALLOWLIST fleet/spawn-allowlist.json: {"sites": [{path, scope, kind, count, reason, expires}]}. A row covers
up to `count` ungated spawns of that key until `expires` (YYYY-MM-DD, inclusive). Red when: an ungated site has
no row, or more spawns than its count; a row is expired; a row is stale (its key is gone or now has fewer
ungated spawns: lower the count / delete the row, so the list only shrinks). The seeded rows ARE the cutover
inventory for Card B. Never add a row for new code: call alloc_client.

S6: a lint that scans nothing is RED, never clean (empty scan root, or zero files read -> rc 1).
Exit 0 clean (silent), 1 findings (one line each), 2 blind (allowlist unreadable/invalid).
  --root DIR          lint another tree (default: the checkout this script lives in)
  --allowlist F       allowlist path (default <root>/fleet/spawn-allowlist.json)
  --today YYYY-MM-DD  the date expiry is judged against (default: today, local)
  --list              print every spawn site with its verdict, exit 0
  --seed-allowlist    write every currently ungated site as a row (keeps existing reasons/expiries). ONE-TIME
                      seeding tool; re-running it to make a new site green defeats the lint.
  --selftest          prove red and green on fixtures
"""
from __future__ import annotations

import argparse
import ast
import bisect
import copy
import datetime as dt
import heapq
import itertools
import json
import os
import re
import subprocess
import sys
import tempfile
import warnings
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from scan_scope import is_live_source  # noqa: E402  scripts/retired/ + scripts/attic/: not live source

HERE = Path(__file__).resolve().parent
DEFAULT_ROOT = HERE.parent
ALLOWLIST = "fleet/spawn-allowlist.json"
SEED_EXPIRES = "2027-01-15"
SEED_REASON = ("grandfathered when the spawn-site lint landed (t_3ca3dbc6); Card B cutover inventory: route it "
               "through alloc_client.admit / placement.admit (or ALLOC_RESERVATION from alloc-admit), then delete")

SCAN_EXT = (".py", ".sh", ".bash")
EXCLUDE = re.compile(r"(^|/)(tests?|fixtures|\.venv|venv|node_modules|retired|_archive|archive|__pycache__)/"
                     r"|^plans/|(^|/)test_[^/]*\.py$|_test\.py$|(^|/)conftest\.py$")
# cheap pre-filter: a file with none of these cannot hold a spawn
PREFILTER = re.compile(r"ssh|docker|systemd-run|kickstart|workflow|jitconfig|registration-token|kanban|"
                       r"_default_spawn|start_arm|open_session")
# Extensionless shebang scripts are scanned only under these top-level dirs. The CI trigger (.github/ci/suites.json
# lf_spawn_site_lint) and the autocommit gate (scripts/lib/main-gate-set.json spawn-site-lint#2) must list
# `<dir>/**` for each, plus **/*.py **/*.sh **/*.bash; lint() checks that parity, so scan surface == trigger surface.
EXTLESS_DIRS = ("scripts", "fleet", "bin", "profiles", "skills-shared", "var", "cron", "hooks")
TRIGGER_GLOBS = ("**/*.py", "**/*.sh", "**/*.bash") + tuple(f"{d}/**" for d in EXTLESS_DIRS)
ARGV_CAP = 32   # tokens kept per bound argv (cmd.append in a loop)
NAMED_SPAWNS = frozenset(("_default_spawn", "start_arm", "open_session"))
NON_FLEET = re.compile(r"^(?:[\w.-]+@)?(?:github\.com|gitlab\.com|localhost|127\.0\.0\.1|::1)$")

SUBP_ATTR = frozenset(("run", "Popen", "call", "check_call", "check_output"))
SUBP_MOD = frozenset(("subprocess", "sp", "_sp", "_subprocess"))
OS_ATTR = frozenset(("system", "popen", "execv", "execvp", "execve", "execvpe", "execl", "execlp", "execle",
                     "execlpe", "spawnv", "spawnvp", "spawnve", "spawnvpe", "spawnl", "spawnlp", "spawnle", "spawnlpe",
                     "posix_spawn", "posix_spawnp"))
ASYNC_ATTR = frozenset(("create_subprocess_exec", "create_subprocess_shell"))
BARE_SUBP = frozenset(("run", "call", "check_call", "check_output", "Popen", "getoutput", "getstatusoutput"))
EXEC_V = re.compile(r"^(?:execv|execvp|execve|execvpe|spawnv|spawnvp|spawnve|spawnvpe|posix_spawn|posix_spawnp)$")
EXEC_L = re.compile(r"^(?:execl|execlp|execle|execlpe|spawnl|spawnlp|spawnle|spawnlpe)$")

# `docker [global opts] run` / `docker [global opts] container run` (`docker --host X run img`, `docker -H X run`)
DOCKER_RUN = re.compile(r"\bdocker(?:\s+-\S+(?:\s+[^\s-]\S*)?)*\s+(?:container\s+)?run\b")
DOCKER_VALUE_OPTS = frozenset(("--config", "-c", "--context", "-H", "--host", "-l", "--log-level", "--tlscacert",
                               "--tlscert", "--tlskey"))
# shell-text kinds, ordered; the ssh rule is the generic one, so it is checked LAST
SHELL_KINDS = (
    ("docker-run", DOCKER_RUN),
    ("systemd-run", re.compile(r"(?:^|[\s;|&(`\"'])systemd-run(?:\s|$)")),
    ("kickstart", re.compile(r"\blaunchctl\s+kickstart\b")),
    ("workflow-run", re.compile(r"\bgh\s+workflow\s+run\b")),
    ("runner-mint", re.compile(r"generate-jitconfig|actions/runners/registration-token")),
    ("kanban-dispatch", re.compile(r"\b(?:hermes|hermes)\s+kanban\s+dispatch\b")),
)
SHELL_SSH = re.compile(r"(?:^|[\s;|&(`\"'])(?:sudo\s+)?(?:/usr/bin/)?ssh((?:\s+-\S+(?:\s+[^\s-]\S*)?)*)\s+([^\s;|&)]+)")
SH_NOISE = re.compile(r"^\s*(?:echo|printf|say|log|_log|warn|info|die|err|usage|note|fail)\b")
SH_FUNC = re.compile(r"^(\s*)(?:function\s+)?([A-Za-z_][\w:.-]*)\s*\(\)\s*\{?\s*(?:#.*)?$|^(\s*)function\s+([A-Za-z_][\w:.-]*)\s*\{\s*$")
SH_SUBST = re.compile(r"\$\([^()]*\)|`[^`]*`")
SH_GATE = re.compile(r"ALLOC_RESERVATION=.*\balloc-admit\b")
# python literal proof: the reservation is being SET (shell `ALLOC_RESERVATION=<x>` or a dict key), not merely named
PY_LITERAL_GATE = re.compile(r"(?<![-\w])ALLOC_RESERVATION\s*[=:]|[\"']ALLOC_RESERVATION[\"']\s*:")


# ---------------------------------------------------------------- file discovery
def scan_files(root: Path, strict: bool = False) -> list[str]:
    try:
        p = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, timeout=120)
        tracked = [f for f in p.stdout.decode("utf-8", "replace").split("\0") if f] if p.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        tracked = None
    if tracked is None:   # not a git tree (fixtures): walk it
        tracked = [str(Path(dp, n).relative_to(root)) for dp, _, ns in os.walk(root) for n in ns]
    out = []
    for f in sorted(tracked):
        if EXCLUDE.search(f) or not is_live_source(f):
            continue
        if f.endswith(SCAN_EXT):
            out.append(f)
        elif "." not in f.rsplit("/", 1)[-1] and f.split("/", 1)[0] in EXTLESS_DIRS and "/" in f:
            if (root / f).is_dir():   # a gitlink (submodule) is a directory, not a script; never a read error
                continue
            try:
                with open(root / f, "rb") as fh:
                    head = fh.readline(200)
            except OSError:
                if strict:
                    raise
                continue
            if head.startswith(b"#!") and (b"python" in head or b"sh" in head):
                out.append(f)
    return out


def is_python(path: str, text: str) -> bool:
    return path.endswith(".py") or (not path.endswith(SCAN_EXT) and text.startswith("#!") and "python" in text.split("\n", 1)[0])


# ---------------------------------------------------------------- python
def _name(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _name(node.func)
    return ""


def _recv(node) -> str:
    return node.value.id if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) else ""


def _literal_text(node) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "\x00" for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        a, b = _literal_text(node.left), _literal_text(node.right)
        if a is not None or b is not None:
            return (a or "\x00") + (b or "\x00")
    return None


def _all_text(node) -> str:
    """Every string literal under node, joined (for 'docker run' / runner-mint inside a built script)."""
    parts = []
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            parts.append(n.value)
    return "\n".join(parts)


def _is_subprocess_call(call: ast.Call, bare: frozenset = frozenset()) -> bool:
    f = call.func
    if isinstance(f, ast.Attribute):
        r = _recv(f)
        if f.attr in SUBP_ATTR and (r in SUBP_MOD or f.attr == "Popen"):
            return True
        if f.attr in OS_ATTR and r == "os":
            return True
        if f.attr in ASYNC_ATTR:
            return True
    if isinstance(f, ast.Name) and (f.id in ("Popen", "check_output", "check_call", "create_subprocess_exec",
                                             "create_subprocess_shell") or f.id in bare):
        return True
    return False


def _is_partial(call) -> bool:
    return isinstance(call, ast.Call) and _name(call.func) == "partial" and bool(call.args)


def _runner_ref(node, bare: frozenset, known: dict) -> list:
    """[(subprocess ref, baked positional args)]: every subprocess callable ``node`` may evaluate to: `subprocess.run`,
    a bare-imported `run`, a name already bound to one (or more) in ``known``, or `functools.partial(<one of those>,
    *args)` (t_c59fff64: `def collect_remote(host, log_dir, run=subprocess.run)` spawned ssh through `run(...)`
    unseen). Empty for anything else. AST only: nothing is evaluated."""
    if isinstance(node, ast.Name) and node.id in known:
        return list(known[node.id])
    if isinstance(node, (ast.Name, ast.Attribute)) and \
            _is_subprocess_call(ast.Call(func=node, args=[], keywords=[]), bare):
        return [(node, [])]
    if _is_partial(node):
        return [(r, [*b, *node.args[1:]]) for r, b in _runner_ref(node.args[0], bare, known)]
    if isinstance(node, ast.Subscript) and isinstance(node.value, (ast.ListComp, ast.GeneratorExp, ast.SetComp)):
        # `[lr for _ in xs][0]`: an element of a comprehension; its own targets shadow (t_8ad18b12)
        own = {t.id for g in node.value.generators for t in ast.walk(g.target) if isinstance(t, ast.Name)}
        return _runner_ref(node.value.elt, bare - own, {k: v for k, v in known.items() if k not in own})
    return []


def _add_alts(known: dict, nm: str, alts: list) -> bool:
    if not alts:   # an assignment with no runner alternative records nothing (an empty entry would mask the
        return False   # bare-import fallback in _runner_ref, Prism b100b2d9f80a)
    cur = known.setdefault(nm, [])
    seen = {(ast.dump(r), tuple(map(ast.dump, b))) for r, b in cur}
    grew = False
    for r, b in alts:
        k = (ast.dump(r), tuple(map(ast.dump, b)))
        if k not in seen:   # no cap: a dropped alternative could be the subprocess one (Prism b68d2fca7e3b)
            seen.add(k)
            cur.append((r, b))
            grew = True
    return grew


_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _own_assigns(body: list) -> list:
    """[(name, value)] of the plain-name assignments a scope body makes itself (nested def/class bodies excluded)."""
    out = []
    for st in body:
        if isinstance(st, _DEFS):
            continue   # its body is its own scope (_walk_own(st) would yield the def's own statements)
        for n in [st, *_walk_own(st)]:
            if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None:
                for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                    if isinstance(t, ast.Name):
                        out.append((t.id, n.value))
    return out


def _fn_locals(f) -> set:
    """Names local to function ``f``: its parameters and the names it assigns without `global` / `nonlocal`."""
    a = f.args
    params = {x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg) if x is not None}
    return params | ({nm for nm, _ in _own_assigns(f.body)} - _declared(f.body, (ast.Global, ast.Nonlocal)))


def _declared(body: list, kind) -> set:
    """Names a scope body declares `global` / `nonlocal` (``kind``: one ast class or a tuple)."""
    return {g for st in body if not isinstance(st, _DEFS) for n in [st, *_walk_own(st)]
            if isinstance(n, kind) for g in n.names}


def _scope_runners(node, body: list, bare: frozenset, inherited: dict) -> dict:
    """name -> [(subprocess ref, baked args)] for the callables a scope reaches subprocess through: a parameter whose
    DEFAULT is one, a local `x = <one>` / `x = partial(<one>, ..)`, or an enclosing scope's binding not shadowed
    here. MAY: every assignment in the scope is one alternative (one branch binding `run = partial(.., ['echo'])`
    leaves the other branch's `run = subprocess.run`, Prism 8fd7ab674141). A name assigned in a function is local
    there, so it never sees the enclosing binding (`runner = fake; invoke = runner` is not subprocess, Prism
    ce17a6dacbd1); at module level an assignment may not run, so the import binding stays an alternative. A
    parameter with no default (or another default) shadows the enclosing binding: a test fake `run` is not a spawn.
    Nested def/class bodies are their own scopes and never bind here (Prism 95af992489bd)."""
    known = {k: list(v) for k, v in inherited.items()}
    is_fn = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
    params: set[str] = set()
    if is_fn:
        a = node.args
        pos = [*a.posonlyargs, *a.args]
        defaults = [None] * (len(pos) - len(a.defaults)) + list(a.defaults)
        for arg, d in [*zip(pos, defaults), *zip(a.kwonlyargs, a.kw_defaults)]:
            params.add(arg.arg)
            ref = _runner_ref(d, bare, inherited) if d is not None else []   # defaults run in the enclosing scope
            known.pop(arg.arg, None)
            if ref:
                known[arg.arg] = ref
        for x in (a.vararg, a.kwarg):
            if x is not None:
                params.add(x.arg)
                known.pop(x.arg, None)
    assigns = _own_assigns(body)
    own_names = {nm for nm, _ in assigns}   # shadowing is decided by THIS scope's assignments only: a nested def's
    # `nonlocal x` may bind past this function to an outer x, so it never makes x local here (Prism de3afa4559bd)
    # a nested function's assignment to a name it declares `global` (any depth) binds the module's name, and one it
    # declares `nonlocal` binds this function's: both are alternatives HERE (`def install(): global runner;
    # runner = subprocess.run`, Prism f64f05e33ae3). Its own locals never are (Prism 95af992489bd).
    kind = ast.Nonlocal if is_fn else ast.Global
    exports = []   # (def, enclosing functions, names it binds here): the value resolves in the DEFINING scope
    # (Prism 093e422a62c5), i.e. through every function between here and the def (Prism 5434b042f044)
    # ONLY there: the exported right-hand side is never also resolved here, where a name it reads may be bound to
    # something else (`lr = subprocess.run` here, `lr = print` local to the exporter, Prism b7d07f333edc)
    export_names: set[str] = set()
    for d, chain in _walk_chain(node):
        # a class body may declare `global` / `nonlocal` too (`class Install: global runner; runner = ..`): same export
        if d is not node and isinstance(d, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = _declared(d.body, kind)
            if is_fn:   # `nonlocal x` two defs deep binds HERE unless a function between binds x (t_8ad18b12)
                names -= {nm for f in chain for nm in _fn_locals(f)}
            own = {nm for nm, _ in _own_assigns(d.body) if nm in names}
            export_names |= own
            if own:
                exports.append((d, chain, own))
    if is_fn:
        # `global x` / `nonlocal x` names are not local: the enclosing binding stays reachable (Prism 087ccd93f08c)
        decl = _declared(body, (ast.Global, ast.Nonlocal))
        for nm in own_names - params - decl:
            known.pop(nm, None)   # local throughout the function: the enclosing binding is unreachable
    else:
        decl = set()
    for nm in own_names | export_names if not is_fn else decl:
        if nm not in known:
            plain = _runner_ref(ast.Name(id=nm, ctx=ast.Load()), bare, {})
            if plain:   # `from subprocess import run`, then a `run = ...` that may not run: the import stays an
                known[nm] = plain   # alternative (module level, or a `global run` in a function, Prism b100b2d9f80a)
    # alias chains (`r = run; rr = partial(r, ..)`) resolve in any source order: each round extends every chain by at
    # least one binding, so one round per binding reaches the fixpoint; a fixed count drops long exported chains
    # (five reverse-ordered installers, Prism e694e43ce56f). Still bounded: a `r = partial(r, ..)` cycle stops.
    for _ in range(len(assigns) + sum(len(n) for _d, _c, n in exports) + 1):
        grew = False
        for nm, v in assigns:
            grew = _add_alts(known, nm, _runner_ref(v, bare, known)) or grew
        for d, chain, names in exports:   # `def install(): global runner; lr = subprocess.run; runner = lr`
            enc = known
            for f in chain:   # `def outer(): lr = subprocess.run; def install(): global runner; runner = lr`
                enc = _scope_runners(f, f.body, bare, enc)
            if not is_fn:   # names the exporter declares `global` resolve in the MODULE, not a shadowing enclosing local
                enc = dict(enc)
                for g in _declared(d.body, ast.Global):
                    if g in known:
                        enc[g] = known[g]
                    else:
                        enc.pop(g, None)
            dk = _scope_runners(d, d.body, bare, enc)
            for nm in names:
                grew = _add_alts(known, nm, dk.get(nm, [])) or grew
        if not grew:
            break
    return {k: v for k, v in known.items() if v}


def _call_argv(call: ast.Call) -> list:
    """The node(s) that hold the spawned argv: positional or `args=`; exec-style calls (os.execl*, spawnl*,
    create_subprocess_exec) pass one token per positional argument, os.execv*/spawnv*/posix_spawn* pass a list."""
    attr = _name(call.func)
    pos = list(call.args)
    if not pos:
        kw = next((k.value for k in call.keywords if k.arg in ("args", "cmd", "command")), None)
        return [kw] if kw is not None else []
    if EXEC_V.match(attr):
        idx = 2 if attr.startswith("spawn") else 1
        return [pos[idx]] if len(pos) > idx else pos[:1]
    if EXEC_L.match(attr):
        return pos[2:] if attr.startswith("spawn") else pos[1:]
    if attr == "create_subprocess_exec":
        return pos
    return pos[:1]


def _argv_head(node, known: dict | None = None, prog: bool = False) -> list:
    """The FULL argv tokens (never truncated: an endpoint can sit after the 4th token): str for a literal, 'SSH' for
    an ssh helper, None for unknown. ``known`` maps a local name to the argv it was bound to (`cmd = [...]`).
    ``prog``: a leading name bound to one token is that token (`[cmd, 'ci-box', ..]`); _heads takes this view AS
    WELL AS the plain one, never instead of it, so a binding the flow cannot prove keeps main's reading (t_8ad18b12)."""
    known = known or {}
    if isinstance(node, ast.Name) and node.id in known:
        return list(known[node.id])
    if _sub_key(node) in known:   # `cmd['a']` with cmd = {'a': ['ssh', ..]} (t_8ad18b12)
        return list(known[_sub_key(node)])
    if isinstance(node, (ast.List, ast.Tuple)):
        out = []
        for e in node.elts:
            t = _literal_text(e)
            b = known.get(e.id) if isinstance(e, ast.Name) else None
            if t is not None and "\x00" not in t:
                out.append(t)
            elif prog and b is not None and len(b) == 1 and isinstance(b[0], str) and not out:
                out.append(b[0])   # `[prog, 'ci-box', 'job']` with prog = '/usr/bin/ssh' (t_8ad18b12)
            elif isinstance(e, ast.Starred):
                sub = _argv_head(e.value, known, prog)
                out += sub if sub else [None]
            elif "ssh" in _name(e).lower() and not isinstance(e, ast.Constant) and not out:
                out.append("SSH")
            else:
                out.append(None)
        return out
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _argv_head(node.left, known, prog) + _argv_head(node.right, known, prog)
    if isinstance(node, (ast.Call, ast.Name, ast.Attribute)) and "ssh" in _name(node).lower():
        return ["SSH"]
    t = _literal_text(node)
    if t is not None:
        return [t]   # a shell string: classified as text
    return []


def _sub_key(node) -> str | None:
    """The argv-binding key of `name['k']` (a constant str/int key), or None. A dict literal bound to a name binds
    each such element under this key, so `run(cmd['a'])` sees the argv the dict carried (t_8ad18b12)."""
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and isinstance(node.slice, ast.Constant) \
            and isinstance(node.slice.value, (str, int)):
        return f"{node.value.id}[{node.slice.value!r}]"
    return None


def _classify_argv(head: list) -> str | None:
    if not head:
        return None
    first = head[0]
    if first == "SSH":
        return "ssh"
    if not isinstance(first, str):
        return None
    if len(head) == 1 and (" " in first.strip() or "\n" in first):   # a shell command string
        return _classify_shell_text(first)
    base = os.path.basename(first.strip())
    rest = [h for h in head[1:] if isinstance(h, str)]
    if base == "ssh":
        tgt = next((h for h in head[1:] if isinstance(h, str) and not h.startswith("-")), None)
        if tgt is not None and NON_FLEET.match(tgt) and head.index(tgt) == 1:
            return None
        return "ssh"
    if base == "docker":
        i = 1   # skip docker's global options (and their operands) before the subcommand
        while i < len(head) and isinstance(head[i], str) and head[i].startswith("-"):
            i += 2 if head[i] in DOCKER_VALUE_OPTS else 1
        if head[i:i + 1] == ["run"] or head[i:i + 2] == ["container", "run"]:
            return "docker-run"
    if base == "systemd-run":
        return "systemd-run"
    if base == "launchctl" and head[1:2] == ["kickstart"]:
        return "kickstart"
    if base == "gh" and head[1:3] == ["workflow", "run"]:
        return "workflow-run"
    if base in ("hermes", "hermes") and head[1:3] == ["kanban", "dispatch"]:
        return "kanban-dispatch"
    if base == "gh" and any("generate-jitconfig" in h or "runners/registration-token" in h for h in rest):
        return "runner-mint"
    return None


def _classify_shell_text(text: str) -> str | None:
    return _classify_shell_at(text)[0]


def _classify_shell_at(text: str) -> tuple[str | None, int]:
    """(kind, offset of the matched spawn in text): the offset maps a joined continuation back to its physical line."""
    for kind, rx in SHELL_KINDS:
        m = rx.search(text)
        if m:
            return kind, m.start()
    m = SHELL_SSH.search(text)
    if m and not NON_FLEET.match(m.group(2).strip("\"'")):
        return "ssh", m.start()
    return None, 0


class _PyScope:
    def __init__(self, name: str, node, body: list):
        self.name, self.node, self.body = name, node, body


class _St:
    """Abstract state at one program point. MAY facts (a spawn is classified if ANY path could bind it): argv
    (name -> set of possible argv heads), docker, mint. MUST facts (a spawn is gated only if EVERY path binds the
    reservation): admit, env. Joins: may = union, must = intersection."""
    __slots__ = ("argv", "docker", "mint", "admit", "env", "alias")

    def __init__(self, argv=None):
        self.argv: dict[str, set] = {k: set(v) for k, v in (argv or {}).items()}
        self.docker: set[str] = set()
        self.mint: set[str] = set()
        self.admit: set[str] = set()
        self.env: set[str] = set()
        self.alias: set[frozenset] = set()   # MAY: {a, b} may name the same mapping (`alias = env`)

    def copy(self) -> "_St":
        c = _St(self.argv)
        c.docker, c.mint, c.admit, c.env = set(self.docker), set(self.mint), set(self.admit), set(self.env)
        c.alias = set(self.alias)
        return c

    def join(self, o: "_St") -> "_St":
        c = self.copy()
        for k, v in o.argv.items():
            c.argv.setdefault(k, set()).update(v)
        c.docker |= o.docker
        c.mint |= o.mint
        c.admit &= o.admit
        c.env &= o.env
        c.alias |= o.alias
        return c

    def key(self):
        return (tuple(sorted((k, tuple(sorted(map(repr, v)))) for k, v in self.argv.items())),
                tuple(sorted(self.docker)), tuple(sorted(self.mint)), tuple(sorted(self.admit)), tuple(sorted(self.env)),
                tuple(sorted(tuple(sorted(a)) for a in self.alias)))


def _rebound(targets) -> set[str]:
    """Names an assignment REBINDS (a plain name, or one unpacked from a tuple/list); `env[k] = v` / `o.a = v`
    mutate, they do not rebind."""
    out: set[str] = set()
    todo = list(targets)
    while todo:
        t = todo.pop()
        if isinstance(t, ast.Name):
            out.add(t.id)
        elif isinstance(t, (ast.Tuple, ast.List)):
            todo += t.elts
        elif isinstance(t, ast.Starred):
            todo.append(t.value)
    return out


def _whole_names(v) -> set[str]:
    """Names whose whole mapping flows into v: an alias (`e2 = env`), `{**env}`, `dict(env, ...)`, `dict(**env)`,
    `env.copy()`, `copy.copy(env)`, `env | {...}`. A selected key (`{"PATH": env["PATH"]}`) does not."""
    if isinstance(v, ast.Name):
        return {v.id}
    if isinstance(v, ast.Dict):
        return {x.id for k, x in zip(v.keys, v.values) if k is None and isinstance(x, ast.Name)}
    if isinstance(v, ast.BinOp) and isinstance(v.op, ast.BitOr):
        return _whole_names(v.left) | _whole_names(v.right)
    if isinstance(v, ast.Call):
        fn = _name(v.func)
        if fn in ("copy", "deepcopy") and isinstance(v.func, ast.Attribute) and isinstance(v.func.value, ast.Name) \
                and v.func.value.id not in ("copy",):
            return {v.func.value.id}   # env.copy()
        if fn in ("dict", "copy", "deepcopy"):
            return {a.id for a in v.args[:1] if isinstance(a, ast.Name)} | {
                k.value.id for k in v.keywords if k.arg is None and isinstance(k.value, ast.Name)}
    return set()


class _PickArm(ast.NodeTransformer):
    """Replace each `a if c else b` with one arm: bit i of ``bits`` picks the i-th IfExp's orelse (default body)."""

    def __init__(self, bits: tuple):
        self.bits, self.i = bits, 0

    def visit_IfExp(self, n):
        b = self.bits[self.i] if self.i < len(self.bits) else 0
        self.i += 1
        return self.visit(n.orelse if b else n.body)


def _arms(node) -> list:
    """node with every conditional expression resolved each way (MAY alternatives; first 3 IfExps, 8 combos)."""
    k = sum(isinstance(x, ast.IfExp) for x in ast.walk(node))
    if not k:
        return [node]
    return [_PickArm(bits).visit(copy.deepcopy(node)) for bits in itertools.product((0, 1), repeat=min(k, 3))]


def _heads(node, st: _St) -> list:
    """Every argv head node can have: one per conditional arm and per combination of the possible bindings of the
    names it uses (capped)."""
    out, seen = [], set()
    for arm in _arms(node):
        used = sorted({k for x in ast.walk(arm) for k in [x.id if isinstance(x, ast.Name) else _sub_key(x)]
                       if k is not None and st.argv.get(k)})
        views: list[dict] = [{}]
        for nm in used:
            views = [dict(v, **{nm: list(h)}) for v in views for h in st.argv[nm]]
            if len(views) > 32:
                break
        if len(views) > 32:   # truncating: rank EVERY composed view of the arm, then keep 32 (t_540a3e9a). Ranking
            # one name's alternatives against a single view loses a spawn that composes only under a later view
            # of another name (Prism 8ca6f3c667ce, 753c64420f1d). Lazy product, O(32) memory, hash-seed-free key.
            def rank(combo):
                v = dict(zip(used, map(list, combo)))
                return [((not _classify_argv(c), not _spawnish(c), repr(c), repr(combo)), v)
                        for c in (_argv_head(arm, v), _argv_head(arm, v, True))]
            alts = [sorted(st.argv[nm], key=repr) for nm in used]
            best: dict = {}   # one view per distinct composed head BEFORE the cap (Prism 7f9bc8fca3a7), BOTH
            # views' heads ranked, so a program-name head never hides behind its view's plain head (6772d0283d54)
            for key, v in itertools.chain.from_iterable(map(rank, itertools.product(*alts))):
                best[key[2]] = min(best.get(key[2], (key, v)), (key, v), key=lambda kv: kv[0])
            views = [v for _, v in heapq.nsmallest(32, best.values(), key=lambda kv: kv[0])]
        for v in views:
            for h in (_argv_head(arm, v), _argv_head(arm, v, True)):
                if repr(h) not in seen:
                    seen.add(repr(h))
                    out.append(h)
    return out


def _drop(st: _St, names) -> None:
    for nm in names:
        st.argv.pop(nm, None)
        for k in [k for k in st.argv if k.startswith(nm + "[")]:   # its dict-element bindings (`cmd['a']`)
            del st.argv[k]
    st.docker -= names
    st.mint -= names
    st.admit -= names
    st.env -= names
    # rebinding the middle of a chain (`a = env; b = a; a = {}`) leaves env and b naming one mapping: connect the
    # rebound name's neighbours before its edges go (Prism 91c6c5c1db77). Eliminate one name at a time and exclude
    # only that name, so names rebound in the SAME call (`a, b = {}, {}` over env~a~b~c) still bridge env~c
    # (Prism 73953ef77e50)
    for nm in names:
        nb = {o for a in st.alias if nm in a for o in a} - {nm}
        st.alias |= {frozenset((x, y)) for x in nb for y in nb if x < y}
        st.alias = {a for a in st.alias if nm not in a}


def _unreserve(st: _St, names) -> None:
    """The reservation is removed from the mapping these names hold (`del env[K]`, `env.pop(K)`, `env.clear()`):
    every name that MAY alias that mapping loses it too (`alias = env; del env[K]; run(env=alias)`, Prism
    302a431d5e5b)."""
    hit, todo = set(names), list(names)
    while todo:
        nm = todo.pop()
        for a in st.alias:
            if nm in a:
                for o in a - hit:
                    hit.add(o)
                    todo.append(o)
    st.env -= hit


def _grow(st: _St, nm: str, ext, may: bool) -> None:
    """argv[nm] += ext, BOUNDED: an append/`+=` in a loop grows the argv every pass, and only the head decides the
    kind, so ARGV_CAP tokens and 8 alternatives keep the loop fixpoint finite and cheap. A head whose first token
    is unknown can never classify; it is dropped, so a generic `rows = []; rows.append(x)` is not carried."""
    grown = {(tuple(h) + tuple(e))[:ARGV_CAP] for h in st.argv[nm] for e in ext}
    alts = {h for h in ((st.argv[nm] | grown) if may else grown) if not h or isinstance(h[0], str)}
    if alts:
        st.argv[nm] = set(sorted(alts, key=repr)[:8])
    else:
        st.argv.pop(nm, None)


def _apply_assign(st: _St, n) -> None:
    if isinstance(n, ast.AugAssign):   # cmd += [...]
        if isinstance(n.target, ast.Name) and isinstance(n.op, ast.Add) and st.argv.get(n.target.id):
            _grow(st, n.target.id, _heads(n.value, st) or [[]], False)
        if isinstance(n.target, ast.Name):
            nm, txt = n.target.id, _all_text(n.value)
            if DOCKER_RUN.search(txt):
                st.docker.add(nm)
            if re.search(r"generate-jitconfig|runners/registration-token", txt):
                st.mint.add(nm)
        return
    if n.value is None:
        return
    targets = n.targets if isinstance(n, ast.Assign) else [n.target]
    names = {t.id for tg in targets for t in ast.walk(tg) if isinstance(t, ast.Name)}
    v = n.value
    # facts of the VALUE, judged before the rebinding drops the targets' old facts (`env = dict(env, X=1)` keeps it)
    heads = {tuple(h) for h in _heads(v, st) if h}
    if isinstance(v, (ast.List, ast.Tuple)) and not v.elts:
        heads = {()}   # `cmd = []` then cmd.extend([...]) / cmd.append(..) builds the argv
    # `a, cmd = 1, cmd`: element-wise argv bindings, judged before the rebinding (#4330 r5 ba99645724e9)
    pairs = {}
    for tg in targets:
        if isinstance(tg, (ast.Tuple, ast.List)) and isinstance(v, (ast.Tuple, ast.List)) \
                and len(tg.elts) == len(v.elts) and not any(isinstance(e, ast.Starred) for e in (*tg.elts, *v.elts)):
            for t, e in zip(tg.elts, v.elts):
                if isinstance(t, ast.Name):
                    h = {tuple(x) for x in _heads(e, st) if x}
                    if h:
                        pairs[t.id] = h
    elems = {}   # `cmd = {'a': ['ssh', ..]}`: each constant-keyed element binds `cmd['a']` (t_8ad18b12)
    if isinstance(v, ast.Dict):
        for tg in targets:
            if isinstance(tg, ast.Name):
                for k, e in zip(v.keys, v.values):
                    sk = _sub_key(ast.Subscript(value=tg, slice=k)) if k is not None else None
                    h = {tuple(x) for x in _heads(e, st) if x} if sk else set()
                    if h:
                        elems[sk] = h
    vnames = {x.id for x in ast.walk(v) if isinstance(x, ast.Name)}
    is_admit = isinstance(v, ast.Call) and _name(v.func) in ("admit", "alloc_admit") and (
        isinstance(v.func, ast.Name) or bool(re.search(r"alloc|placement|^pl$", _recv(v.func))))
    txt = _all_text(v)
    # the value SETS the reservation (a dict key, an ALLOC_RESERVATION= keyword, an assignment-shaped literal) or
    # carries a whole reservation env; a bare mention in a string value is not proof (Prism 208bec3a985c, #4308)
    carries = (bool(_whole_names(v) & st.env) or bool(PY_LITERAL_GATE.search(txt))
               or any(isinstance(k, ast.keyword) and k.arg == "ALLOC_RESERVATION" for k in ast.walk(v))
               or any(isinstance(d, ast.Dict) and any(isinstance(k, ast.Constant) and k.value == "ALLOC_RESERVATION"
                                                      for k in d.keys) for d in ast.walk(v)))
    # docker/mint script facts flow through any use of the old value (`script = script + " --rm img"`)
    docker = bool(DOCKER_RUN.search(txt) or vnames & st.docker)
    mint = bool(re.search(r"generate-jitconfig|runners/registration-token", txt) or vnames & st.mint)
    rb = _rebound(targets)
    _drop(st, rb)
    for tg in targets:
        if isinstance(tg, ast.Name) and heads:
            st.argv[tg.id] = set(heads)
    st.argv.update(pairs)
    st.argv.update(elems)
    for tg in targets:   # `cmd['a'] = [...]`: ADDS an alternative, never replaces one (Apollo r7 on #6662)
        if _sub_key(tg) and heads:
            st.argv[_sub_key(tg)] = st.argv.get(_sub_key(tg), set()) | heads
    if is_admit:
        st.admit |= names
    if carries:   # a whole binding only: `env['NOTE'] = note` stores a value, it reserves no key (Prism 89766342164e)
        st.env |= rb
    if isinstance(v, ast.Name):   # `alias = env`: one mapping, two names (a mutation through either reaches both)
        st.alias |= {frozenset((t, v.id)) for t in rb if t != v.id}
    if docker:
        st.docker |= names
    if mint:
        st.mint |= names
    for tg in targets:   # env["ALLOC_RESERVATION"] = rid
        if isinstance(tg, ast.Subscript) and "ALLOC_RESERVATION" in _all_text(tg.slice):
            st.env |= {t.id for t in ast.walk(tg.value) if isinstance(t, ast.Name)}


LOOP_ELTS_CAP = 64   # distinct literal elements judged per `for`; past it the target keeps no must fact
SPAWN_WORDS = frozenset({"ssh", "docker", "run", "systemd-run", "launchctl", "kickstart", "gh", "workflow",
                         "hermes", "kanban", "dispatch"})


def _spawnish(head) -> bool:
    """A head that is a spawn argv, or may become one once composed (`['docker'] + cmd` with cmd = ['run', ..]):
    it classifies, or one of its tokens is a word _classify_argv keys on, or a shell string that classifies."""
    if _classify_argv(list(head)):
        return True
    return any(t == "SSH" or isinstance(t, str) and (
        os.path.basename(t.strip()) in SPAWN_WORDS or "generate-jitconfig" in t or "runners/registration-token" in t
        or (" " in t.strip() and _classify_shell_text(t) is not None)) for t in head)


def _elt_spawnish(e, st: _St) -> bool:
    """A loop element past LOOP_ELTS_CAP is still judged when any argv inside it (`(1, ['ssh', ..])`) is spawn-ish."""
    return any(_spawnish(h) for sub in ast.walk(e) if isinstance(sub, ast.expr) for h in _heads(sub, st))


def _loop_bind(target, it, st: _St, cur: _St | None = None):
    """The facts a `for` target can hold on any pass: the join, over the elements of an explicit literal iterable,
    of assigning each element to the target (`for cmd in [cmd]`, `for i, cmd in [(1, ['ssh', ..])]`, Prism
    9660245e676f). None when the elements are not known (a name, a call, a starred element, an empty literal):
    the target is then dropped. The iterable runs once, at entry, so its elements are judged in the entry state
    ``st``, except a bare NAME element: it is a reference to a mapping the body may have mutated on an earlier pass
    (`for e in [env, env]: run(env=e); e.pop(K)`), so it is judged in ``cur``, this pass's state (Prism
    55945507686d). The first LOOP_ELTS_CAP distinct elements are judged; past it the gating (must) facts of the
    target are dropped, never kept from a prefix (Prism 11d54303a061), and only the spawn-ish elements are judged,
    so an ssh list after 70 echo lists still binds the target (t_540a3e9a)."""
    if not isinstance(it, (ast.List, ast.Tuple, ast.Set)) or not it.elts \
            or any(isinstance(e, ast.Starred) for e in it.elts):
        return None
    own = _rebound([target])
    uniq: dict = {}
    for e in it.elts:
        uniq.setdefault(ast.dump(e), e)
    elts = list(uniq.values())
    out = None
    for i, e in enumerate(elts):
        live = cur is not None and isinstance(e, ast.Name) and e.id not in own
        if i >= LOOP_ELTS_CAP and not _elt_spawnish(e, cur if live else st):
            continue
        s2 = (cur if live else st).copy()
        _apply_assign(s2, ast.Assign(targets=[target], value=e))
        out = s2 if out is None else out.join(s2)
    if len(elts) > LOOP_ELTS_CAP:
        out.admit -= own
        out.env -= own
    return out


def _rebind_from(st: _St, names, bound) -> None:
    """Rebind ``names`` to their facts in ``bound`` (a _loop_bind result), or drop them when bound is None."""
    _drop(st, names)
    if bound is None:
        return
    for nm in names:
        for k, v in bound.argv.items():
            if v and (k == nm or k.startswith(nm + "[")):
                st.argv[k] = set(v)
    for fact in ("docker", "mint", "admit", "env"):
        getattr(st, fact).update(names & getattr(bound, fact))
    st.alias |= {a for a in bound.alias if a & names}


def _apply_mutation(st: _St, call: ast.Call, may: bool = False) -> None:
    """`env.pop("ALLOC_RESERVATION")` / `env.clear()` remove the reservation; `env.update(ALLOC_RESERVATION=..)` /
    `env.setdefault("ALLOC_RESERVATION", ..)` add it; `cmd.extend([..])` / `cmd.append(x)` grow a bound argv.
    A MAY call (under a short-circuit, a conditional expression, a comprehension) removes facts but never adds a
    must fact: `rid and env.update(ALLOC_RESERVATION=rid)` does not gate a later spawn (#4330 r5)."""
    f = call.func
    if not (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)):
        return
    nm = f.value.id

    def is_key(a) -> bool:
        return isinstance(a, ast.Constant) and a.value == "ALLOC_RESERVATION"
    if f.attr in ("extend", "append") and nm in st.argv and call.args:
        ext = _heads(call.args[0], st) or [[]] if f.attr == "extend" else \
            [[t if (t := _literal_text(call.args[0])) is not None and "\x00" not in t else None]]
        # bounded: an append in a loop grows the argv every pass; the head decides the kind, so 32 tokens and 16
        # alternatives keep the fixpoint finite and cheap
        _grow(st, nm, ext, may)
    elif f.attr == "clear" or (f.attr == "pop" and call.args and is_key(call.args[0])):
        _unreserve(st, {nm})
    elif may:
        return
    elif f.attr == "setdefault" and call.args and is_key(call.args[0]):
        st.env.add(nm)
    # update adds the key only when it is an actual KEY: a dict-literal key, a keyword, or a whole reservation env;
    # the string as a VALUE ({'NOTE': 'ALLOC_RESERVATION'}) is a mention (Prism 5583389c6af3)
    elif f.attr == "update" and (any(k.arg == "ALLOC_RESERVATION" for k in call.keywords)
                                 or any(isinstance(a, ast.Dict) and any(map(is_key, a.keys)) for a in call.args)
                                 or any(_whole_names(a) & st.env for a in call.args)):
        st.env.add(nm)


def _walk_own(node):
    """ast.walk that does not descend into nested function/class definitions (they are their own scopes)."""
    stack = list(ast.iter_child_nodes(node))
    while stack:
        n = stack.pop()
        yield n
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(n))


def _walk_chain(node, chain=()):
    """(descendant, enclosing functions below ``node``) for every descendant of ``node``; class bodies are not
    enclosing scopes for what they contain (Python's lexical rule), so they never join the chain."""
    for ch in ast.iter_child_nodes(node):
        yield ch, chain
        yield from _walk_chain(ch, chain + (ch,) if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)) else chain)


_COMPS = (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)


def _def_header(n) -> list:
    """The expressions a def / class / lambda evaluates in the ENCLOSING scope when it is defined."""
    out = list(getattr(n, "decorator_list", []))
    if isinstance(n, ast.ClassDef):
        return out + list(n.bases) + [k.value for k in n.keywords]
    a = n.args
    out += [*a.defaults, *(d for d in a.kw_defaults if d is not None)]
    if not isinstance(n, ast.Lambda):
        out += [x.annotation for x in (*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg)
                if x is not None and x.annotation is not None]
        if n.returns is not None:
            out.append(n.returns)
    return out


def _calls_in(*nodes) -> list:
    """[(call, may)] in EVALUATION order: a call's receiver and arguments run before the call itself
    (`env.update(X=run(.., env=env))` runs the inner spawn first, Prism 6bc087d9b475). `may` marks a call that runs
    on some paths only: a non-first BoolOp operand, a conditional-expression arm, a comprehension body."""
    out: list = []

    def go(n, may: bool) -> None:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            # the body is its own scope; the header (decorators, defaults, annotations, class bases) runs HERE,
            # when the definition executes (`def f(x=subprocess.run([...]))`, Prism 1c99d56b55fc / e4b36576fb93)
            for h in _def_header(n):
                go(h, may)
            return
        if isinstance(n, ast.BoolOp):
            for i, v in enumerate(n.values):
                go(v, may or i > 0)
            return
        if isinstance(n, ast.IfExp):
            go(n.test, may)
            go(n.body, True)
            go(n.orelse, True)
            return
        if isinstance(n, _COMPS):
            go(n.generators[0].iter, may)
            for ch in ast.iter_child_nodes(n):
                if ch is not n.generators[0]:
                    go(ch, True)
            for ch in ast.iter_child_nodes(n.generators[0]):
                if ch is not n.generators[0].iter:
                    go(ch, True)
            return
        for ch in ast.iter_child_nodes(n):
            go(ch, may)
        if isinstance(n, ast.Call):
            out.append((n, may))
    for nd in nodes:
        if nd is not None:
            go(nd, False)
    return out


def _gated(call: ast.Call, st: _St) -> bool:
    # flow rule: the reservation must REACH the spawn on every path (st.admit/st.env are must-facts at this call)
    # an admitted id may appear anywhere in the call (argv text, env value); a reservation ENV must flow in as a
    # whole mapping (env=env, env={**env}, env=dict(env, ..)): a selected key ({'PATH': env['PATH']}) drops it
    # (Prism 6796b1769443)
    names = {n.id for n in ast.walk(call) if isinstance(n, ast.Name)}
    if names & st.admit:
        return True
    if any(_whole_names(v) & st.env for v in (*call.args, *(k.value for k in call.keywords))):
        return True
    if any(k.arg in ("reservation_id", "ALLOC_RESERVATION") for k in call.keywords):
        return True
    # env={"ALLOC_RESERVATION": rid, ...} as a dict literal anywhere in the call
    for n in ast.walk(call):
        if isinstance(n, ast.Dict) and any(isinstance(k, ast.Constant) and k.value == "ALLOC_RESERVATION" for k in n.keys):
            return True
    # A literal is proof only when it ASSIGNS the reservation (`ALLOC_RESERVATION=` / `"ALLOC_RESERVATION": `), the
    # same bar as SH_GATE on the bash side. A bare mention (`env -u ALLOC_RESERVATION ...`, a log line, a comment
    # string) removes or merely names it and must not gate (Prism 10dea09c29d3).
    return bool(PY_LITERAL_GATE.search(_all_text(call)))


def _classify_kinds(n: ast.Call, st: _St, bare: frozenset, runners: dict | None = None) -> set:
    """Every spawn kind a call may be. A call through an injected/aliased runner IS that subprocess call (a partial's
    baked args lead the argv); MAY: the name can hold any of its alternatives, so each is classified and every kind
    is kept, so an allowlisted docker-run alternative never hides an ssh one (Prism 12a2f225f299)."""
    if runners and isinstance(n.func, ast.Name) and n.func.id in runners:
        return {k for ref, baked in runners[n.func.id]
                for k in _classify_call(ast.Call(func=ref, args=[*baked, *n.args], keywords=n.keywords), st, bare)}
    return _classify_call(n, st, bare)


def _classify_call(n: ast.Call, st: _St, bare: frozenset) -> set:
    """Every spawn kind one call's argv may be (a loop target can hold an ssh AND a docker-run list: both are kept,
    so an allowlisted kind never hides another, Prism 12a2f225f299). Empty for a non-spawn."""
    argv = _call_argv(n) if _is_subprocess_call(n, bare) else []
    if argv:
        if len(argv) == 1:
            a0 = argv[0].value if isinstance(argv[0], ast.Starred) else argv[0]
            heads = _heads(a0, st)
        else:
            # one argv across the expansion boundary: ('docker', *ARGS) / (*base, 'run', img) resolve in place
            heads = _heads(ast.List(elts=list(argv), ctx=ast.Load()), st)
        kinds = {k for k in map(_classify_argv, heads) if k}
        if kinds:
            return kinds
        names = {x.id for a in argv for x in ast.walk(a) if isinstance(x, ast.Name)}
        return {"runner-mint"} if names & st.mint else set()
    if "ssh" in _name(n.func).lower() and not isinstance(n.func, ast.Attribute) or \
            (isinstance(n.func, ast.Attribute) and "ssh" in n.func.attr.lower()):
        # a call to an ssh helper: a spawn only when it ships a `docker run` script (the pool's start)
        names = {x.id for a in n.args for x in ast.walk(a) if isinstance(x, ast.Name)}
        if names & st.docker or any(DOCKER_RUN.search(_all_text(a)) for a in n.args):
            return {"docker-run"}
        return set()
    if _name(n.func) in NAMED_SPAWNS:
        return {"named"}
    if _name(n.func) in ("run", "check_output", "gh", "_gh", "gh_api", "api"):
        vals = [*n.args, *(k.value for k in n.keywords)]
        if any(re.search(r"generate-jitconfig|runners/registration-token", _all_text(a)) for a in vals) or \
                {x.id for a in vals for x in ast.walk(a) if isinstance(x, ast.Name)} & st.mint:
            return {"runner-mint"}   # every mint call counts (no per-scope suppression)
    return set()


class _Flow:
    """Statement-level dataflow over one scope: branches join, loops iterate to a fixpoint (a binding made later
    in a loop body reaches an earlier call through the backedge). Each call's verdict is merged over every visit:
    its kind is the union of what any visit saw, it is gated only if EVERY visit saw it gated."""

    def __init__(self, bare: frozenset, runners: dict | None = None):
        self.bare = bare
        self.runners = runners or {}
        self.hits: dict[int, list] = {}   # id(call) -> [call, kinds, gated]
        self.raises: list[list] = []      # one [joined state] per enclosing try body: its potential raise points

    def _raise_point(self, st: _St) -> None:
        # an exception may leave any enclosing try body HERE: its handlers (and finally) see this state joined in
        for acc in self.raises:
            acc[0] = st.copy() if acc[0] is None else acc[0].join(st)

    def _visit(self, calls, st: _St) -> None:
        for c, may in calls:
            self._raise_point(st)   # a call can raise before its own effects
            ks = _classify_kinds(c, st, self.bare, self.runners)
            h = self.hits.setdefault(id(c), [c, set(), True])
            if ks:
                h[1] |= ks
                h[2] = h[2] and _gated(c, st)
            _apply_mutation(st, c, may)
            self._raise_point(st)

    def block(self, stmts, st: _St) -> _St:
        for s in stmts:
            st = self.stmt(s, st)
            self._raise_point(st)
        return st

    def stmt(self, s, st: _St) -> _St:
        if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            self._visit(_calls_in(s), st)   # header only: _calls_in never enters the body (its own scope)
            _drop(st, {s.name})
            return st
        if isinstance(s, ast.If):
            self._visit(_calls_in(s.test), st)
            return self.block(s.body, st.copy()).join(self.block(s.orelse, st.copy()))
        if isinstance(s, (ast.For, ast.AsyncFor, ast.While)):
            loop_test = s.test if isinstance(s, ast.While) else None
            self._visit(_calls_in(s.iter if loop_test is None else loop_test), st)
            # the target binds one element per pass (from the iterable as built at entry); calls inside the target
            # (`for out[run([...])] in xs`) run on every pass (Prism 9660245e676f)
            cur = st
            for _ in range(8):   # fixpoint over the backedge
                body_in = cur.copy()
                if loop_test is None:
                    self._visit(_calls_in(s.target), body_in)
                    # rebound per pass: a name element sees the body's mutations from the earlier passes
                    _rebind_from(body_in, _rebound([s.target]), _loop_bind(s.target, s.iter, st, body_in))
                after = self.block(s.body, body_in)
                if loop_test is not None:   # a while condition runs again after every pass (Prism cec4be516dc4)
                    self._visit(_calls_in(loop_test), after)
                nxt = st.join(after)
                if nxt.key() == cur.key():
                    break
                cur = nxt
            return self.block(s.orelse, cur.copy())
        if isinstance(s, (ast.Try, getattr(ast, "TryStar", ast.Try))):
            # a handler is entered from ANY raise point in the body (every call and statement), not only from its
            # start or end: `del env[K]; prepare(); env[K] = rid` raising in prepare() reaches the handler with the
            # reservation gone (Prism 4f3ee8208e67). acc = the join of the entry state and every raise point.
            acc = [st.copy()]
            self.raises.append(acc)
            try:
                body = self.block(s.body, st.copy())
            finally:
                self.raises.pop()
            mid = acc[0]
            outs = [self.block(s.orelse, body.copy())]
            for hd in s.handlers:
                hin = mid.copy()
                # the handler TYPE runs when an exception is matched, before the body (Prism c649f6e76616)
                self._visit(_calls_in(hd.type), hin)
                if hd.name:
                    _drop(hin, {hd.name})
                outs.append(self.block(hd.body, hin))
            res = outs[0]
            for o in outs[1:]:
                res = res.join(o)
            # finally also runs on an exception that no handler caught, from any raise point
            return self.block(s.finalbody, res.join(mid))
        if isinstance(s, (ast.With, ast.AsyncWith)):
            for it in s.items:
                self._visit(_calls_in(it.context_expr), st)
                if it.optional_vars is not None:
                    self._visit(_calls_in(it.optional_vars), st)   # `with cm as out[run([...])]`
                    _drop(st, _rebound([it.optional_vars]))
            return self.block(s.body, st)
        if isinstance(s, getattr(ast, "Match", ())):
            self._visit(_calls_in(s.subject), st)
            res = st
            for case in s.cases:
                cin = st.copy()
                self._visit(_calls_in(case.guard), cin)   # `case 1 if run([...])` (Prism 94d9ca2fbb61)
                res = res.join(self.block(case.body, cin))
            return res
        if isinstance(s, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            self._visit(_calls_in(s.value), st)
            # calls inside a target (`out[run([...])] = v`) run after the value (Prism 9f6c5aaee3d4)
            self._visit(_calls_in(*(s.targets if isinstance(s, ast.Assign) else [s.target])), st)
            _apply_assign(st, s)
            return st
        if isinstance(s, ast.Delete):
            for t in s.targets:
                # a target's subexpressions run before its deletion (`del out[run([...])]`, Prism 2060cd33d39f)
                self._visit(_calls_in(t), st)
                if isinstance(t, ast.Name):
                    _drop(st, {t.id})
                elif isinstance(t, ast.Subscript) and "ALLOC_RESERVATION" in _all_text(t.slice):
                    _unreserve(st, {x.id for x in ast.walk(t.value) if isinstance(x, ast.Name)})
            return st
        self._visit(_calls_in(s), st)
        return st


def py_sites(path: str, text: str) -> list[tuple[str, str, int, bool]]:
    """[(scope, kind, line, gated)]"""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            return [("<module>", "unparseable", 1, False)]
    scopes = [_PyScope("<module>", tree, tree.body)]
    # `from subprocess import run` makes a bare run(...) a subprocess call
    bare = frozenset(a.asname or a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                     and n.module in ("subprocess", "os", "asyncio") for a in n.names if a.name in BARE_SUBP | OS_ATTR
                     | ASYNC_ATTR)

    runners = {id(tree): _scope_runners(tree, tree.body, bare, {})}

    def collect(node, prefix, outer):
        # every function, class body and lambda is its own scope (none inherits another's admission facts).
        # Runner bindings (t_c59fff64) follow Python's lexical rule: a function sees its enclosing functions'
        # names and the module's, never a class body's.
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                lam = isinstance(ch, ast.Lambda)
                q = (f"{prefix}.<lambda>" if prefix else "<lambda>") if lam else \
                    (f"{prefix}.{ch.name}" if prefix else ch.name)
                body = [ast.Expr(ch.body)] if lam else ch.body
                mine = _scope_runners(ch, body, bare, outer)
                runners[id(ch)] = mine
                scopes.append(_PyScope(q, ch, body))
                collect(ch, q, outer if isinstance(ch, ast.ClassDef) else mine)
            else:
                collect(ch, prefix, outer)
    collect(tree, "", runners[id(tree)])
    out = []
    module_argv: dict = {}
    for sc in scopes:
        fl = _Flow(bare, runners.get(id(sc.node)))
        end = fl.block(sc.body, _St(module_argv))
        if sc is scopes[0]:   # module-level argv constants (BASE = ["ssh", ...]) are visible everywhere;
            module_argv = end.argv   # admission facts are never shared between scopes
        for c, kinds, gated in fl.hits.values():
            for k in sorted(kinds):   # one site per kind: an allowlist row covers its own kind only (Prism 12a2f225f299)
                out.append((sc.name, k, c.lineno, gated))
    out.sort(key=lambda r: r[2])
    return out


# ---------------------------------------------------------------- shell
def _sh_commands(s: str) -> list[str]:
    """Split one shell line into its commands on unquoted ; & | (&&, || included). A quoted remote command
    (ssh h 'a; b') stays one command; an unquoted `#` after whitespace starts a comment."""
    out, cur, q, i = [], [], None, 0
    while i < len(s):
        c = s[i]
        if q:
            cur.append(c)
            if c == q:
                q = None
            elif c == "\\" and q == '"' and i + 1 < len(s):
                cur.append(s[i + 1])
                i += 1
        elif c in "'\"":
            q = c
            cur.append(c)
        elif c == "\\" and i + 1 < len(s):
            cur.append(s[i:i + 2])
            i += 1
        elif c in ";&|":
            out.append("".join(cur))
            cur = []
        elif c == "#" and (not cur or cur[-1].isspace()):
            break
        else:
            cur.append(c)
        i += 1
    out.append("".join(cur))
    return [x for x in out if x.strip()]


def _sh_ends_in_comment(s: str) -> bool:
    """True when the end of shell text s sits inside an unquoted `#` comment (bash ends a comment at the newline, so
    a trailing `\\` there is comment text, not a continuation). Same quote/escape/`#` rules as _sh_commands."""
    q, i, prev = None, 0, ""
    while i < len(s):
        c = s[i]
        if q:
            if c == q:
                q = None
            elif c == "\\" and q == '"':
                i += 1
        elif c in "'\"":
            q = c
        elif c == "\\":
            i += 1
        elif c == "#" and (not prev or prev.isspace() or prev in ";&|()"):
            return True
        prev = c
        i += 1
    return False


def _sh_logical_lines(text: str):
    """(first physical line number, logical line, breaks): a trailing unquoted backslash continues the command onto
    the next physical line (`docker \\` + `run img` is one `docker run img`), so classification sees the joined
    command; ``breaks`` are the offsets in the logical line where each following physical line starts (a spawn is
    reported on the physical line it sits on, the same line the per-line lint reported).
    Bash semantics for heredoc bodies: a QUOTED heredoc (<<'EOF') body is literal, never folded, so a body line
    ending in `\\` cannot swallow the terminator; an unquoted body folds `\\`-newline like bash does."""
    buf, start, heredoc, quoted = [], 0, None, False
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if heredoc and quoted:
            if line.strip() == heredoc:
                heredoc = None
            yield i, line, []
            continue
        if not buf:
            start = i
        body = raw.rstrip("\r\n")   # bash continues only on `\\` IMMEDIATELY before the newline (`\\ ` does not)
        tail = len(body) - len(body.rstrip("\\"))
        # odd count: an escaped newline, not a literal `\\`; inside an unquoted comment it is comment text (Prism
        # 26f2305413bc/d1f06dc28543); an unquoted heredoc body has no comments
        if tail % 2 == 1 and (heredoc or not _sh_ends_in_comment("".join(buf) + body[:-1])):
            buf.append(body[:-1])
            continue
        buf.append(line)
        # bash deletes `\\`-newline and inserts nothing: `docker ru\\` + `n img` is `docker run img` (Prism 4c22408394e3)
        logical = "".join(buf)
        breaks = list(itertools.accumulate(len(b) for b in buf[:-1]))
        buf = []
        if heredoc:   # unquoted body: the terminator is a whole logical line
            if logical.strip() == heredoc:
                heredoc = None
        elif not logical.lstrip().startswith("#"):
            hd = re.search(r"(?<![<\d])<<(?!<)-?\s*(['\"]?)([A-Za-z_]\w*)\1", logical)
            if hd:   # the body starts on the NEXT physical line
                heredoc, quoted = hd.group(2), bool(hd.group(1))
        yield start, logical, breaks
    if buf:
        yield start, "".join(buf), list(itertools.accumulate(len(b) for b in buf[:-1]))


def _sh_spawn_offsets(text: str, kind: str) -> list[int]:
    """Every offset in one shell command where a spawn of ``kind`` starts (a command whose quoted arguments carry two
    ssh invocations on two continued lines is a spawn on each line, as the per-line lint saw it)."""
    if kind == "ssh":
        return [m.start() for m in SHELL_SSH.finditer(text) if not NON_FLEET.match(m.group(2).strip("\"'"))]
    rx = dict(SHELL_KINDS)[kind]
    return [m.start() for m in rx.finditer(text)]


def _sh_blank(m) -> str:
    """`$(body)` -> `$(    )`, `` `body` `` -> `` `    ` ``: same length, so offsets in the command stay valid."""
    g = m.group()
    k = 2 if g.startswith("$(") else 1
    return g[:k] + " " * (len(g) - k - 1) + g[-1]


def _sh_admit_end(s: str, at: int) -> int:
    """Offset in s where the gating `ALLOC_RESERVATION=<admit>` value ENDS (the command at offset ``at`` carries it):
    the matching `)` of a `$(` value (nested substitutions included), the closing backtick, else the end of the
    `alloc-admit` word. Only a substitution that starts after this offset runs with the reservation set."""
    m = SH_GATE.search(s, at)
    a = s.find("alloc-admit", m.start())
    tok = s.rfind("ALLOC_RESERVATION=", m.start(), a)
    i = tok + len("ALLOC_RESERVATION=")
    if s.startswith('"', i):
        i += 1
    if s.startswith("$(", i):
        depth, q, k = 0, None, i
        while k < len(s):
            c = s[k]
            if q:
                if c == q:
                    q = None
                elif c == "\\" and q == '"':
                    k += 1
            elif c in "'\"" and depth:
                q = c
            elif c == "\\":
                k += 1
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    return k
            k += 1
        return len(s)
    if s.startswith("`", i):
        e = s.find("`", i + 1)
        return e if e >= 0 else len(s)
    return a + len("alloc-admit")


def sh_sites(text: str) -> list[tuple[str, str, int, bool]]:
    out = []
    stack: list[tuple[str, str]] = []           # (function name, indent)
    gated_in: dict[str, bool] = defaultdict(bool)
    for i, line, breaks in _sh_logical_lines(text):   # heredoc bodies are still classified (fed to bash/ssh, they run)
        m = SH_FUNC.match(line)
        if m:
            stack.append((m.group(2) or m.group(4), m.group(1) or m.group(3) or ""))
            continue
        if stack and re.match(r"^" + re.escape(stack[-1][1]) + r"\}\s*(?:#.*)?$", line):
            stack.pop()
            continue
        scope = stack[-1][0] if stack else "<top>"
        s = line.lstrip()
        lead = len(line) - len(s)
        if not s or s.startswith("#"):
            continue
        cmds = _sh_commands(s)
        # each command's offset in s, by a cursor: two identical commands on one line get their own positions
        # (Prism fecf5ae12966)
        pos, cur = [], 0
        for c in cmds:
            at = s.find(c, cur)
            pos.append(at)
            cur = at + len(c)
        # gate in execution order: the reservation covers commands from the one that sets it ON, never a command
        # before it on the same line (`ssh h job; ALLOC_RESERVATION=$(alloc-admit ..)` is ungated)
        gate_at = min((p for p, c in zip(pos, cmds) if SH_GATE.search(c)), default=None)
        # a substitution runs when its word is expanded: it is gated only AFTER the admit's own value is done, never
        # one before the ALLOC_RESERVATION= token nor one inside the admit's own $(..) argv (#4330 r6, Prism
        # 01a4add5b51b / ad6631a44e67)
        subst_gate = _sh_admit_end(s, gate_at) if gate_at is not None else None
        n_outer = len(cmds)
        for m in re.finditer(r"\$\(([^()]*)\)|`([^`]*)`", s):   # $(...) / `...` run even inside echo/printf
            body_at = m.start(1) if m.group(1) is not None else m.start(2)
            body = m.group(1) if m.group(1) is not None else m.group(2)
            cur = 0
            for c in _sh_commands(body):
                at = body.find(c, cur)
                cur = at + len(c)
                cmds.append(c)
                pos.append(body_at + at)
        for j, cmd in enumerate(cmds):   # one site per command (pipeline stages included): `a | ssh h x` per stage
            if SH_NOISE.match(cmd.lstrip()):
                continue
            # a substitution body is its own command (appended above): blank it (length-preserving) in the outer
            # command so `X=$(ssh h job)` counts once, not as the outer command AND the substitution
            view = SH_SUBST.sub(_sh_blank, cmd) if j < n_outer else cmd
            kind = _classify_shell_text(view)
            if not kind:
                continue
            if j < n_outer:
                g = gated_in[scope] or (gate_at is not None and pos[j] >= gate_at)
            else:
                g = gated_in[scope] or (subst_gate is not None and pos[j] > subst_gate)
            # one site per PHYSICAL line the command's spawns sit on (a continued command reports where each spawn
            # is, never only its first line): discovery stays a superset of the per-line lint
            offs = _sh_spawn_offsets(view, kind) or [0]
            for ln in sorted({i + bisect.bisect_right(breaks, lead + pos[j] + o) for o in offs}):
                out.append((scope, kind, ln, g))
        if gate_at is not None:
            gated_in[scope] = True
    return out


# ---------------------------------------------------------------- lint
def collect_sites(root: Path, strict: bool = False):
    """strict (the seed): a file that cannot be read raises, never shrinks the inventory silently (Prism
    33f6bb1fe4e4: one unreadable file would otherwise delete its rows from a re-seeded allowlist)."""
    files = scan_files(root, strict)
    read = 0
    sites = []   # (path, scope, kind, line, gated)
    for f in files:
        try:
            text = (root / f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            if strict:
                raise
            continue
        read += 1
        if not PREFILTER.search(text):
            continue
        rows = py_sites(f, text) if is_python(f, text) else sh_sites(text)
        sites += [(f, sc, k, ln, g) for sc, k, ln, g in rows]
    return read, sites


def load_allowlist(path: Path) -> list[dict]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    rows = doc.get("sites")
    if not isinstance(rows, list):
        raise ValueError("allowlist has no 'sites' list")
    for r in rows:
        for k in ("path", "scope", "kind", "count", "reason", "expires"):
            if k not in r:
                raise ValueError(f"allowlist row missing {k!r}: {r}")
        if not isinstance(r["count"], int) or r["count"] < 1:
            raise ValueError(f"allowlist row count must be an int >= 1: {r}")
        if not str(r["reason"]).strip():
            raise ValueError(f"allowlist row has an empty reason: {r}")
        dt.date.fromisoformat(r["expires"])
    return rows


def trigger_findings(root: Path) -> list[str]:
    """The CI trigger and the autocommit gate must cover the scan surface (else a change the lint would flag never
    runs it). Checked only when the files exist (fixture trees have neither)."""
    out = []
    s = root / ".github/ci/suites.json"
    if s.exists():
        doc = json.loads(s.read_text(encoding="utf-8"))
        paths = (doc.get("suites") or doc).get("lf_spawn_site_lint", {}).get("paths", [])
        miss = [g for g in TRIGGER_GLOBS if g not in paths]
        if miss:
            out.append(f".github/ci/suites.json: lf_spawn_site_lint paths miss {miss} (the scan surface)")
    m = root / "scripts/lib/main-gate-set.json"
    if m.exists():
        g = next((g for g in json.loads(m.read_text(encoding="utf-8")).get("gates", [])
                  if g.get("id") == "spawn-site-lint#2"), None)
        miss = [x for x in TRIGGER_GLOBS if g is None or x not in g.get("paths", [])]
        if miss:
            out.append(f"scripts/lib/main-gate-set.json: spawn-site-lint#2 paths miss {miss} (the scan surface)")
    return out


def lint(root: Path, allowlist: Path, today: dt.date) -> list[str]:
    read, sites = collect_sites(root)
    if read == 0:
        return [f"lint-spawn-site: scanned 0 files under {root}: a lint that scans nothing is red (S6)"]
    rows = load_allowlist(allowlist)
    ungated = Counter((p, s, k) for p, s, k, _, g in sites if not g)
    lines = defaultdict(list)
    for p, s, k, ln, g in sites:
        if not g:
            lines[(p, s, k)].append(ln)
    allowed = {}
    findings = trigger_findings(root)
    for r in rows:
        key = (r["path"], r["scope"], r["kind"])
        if key in allowed:
            findings.append(f"{ALLOWLIST}: duplicate row {key}")
        allowed[key] = r
        if dt.date.fromisoformat(r["expires"]) < today:
            findings.append(f"{r['path']}: {r['scope']} {r['kind']}: allowlist row EXPIRED {r['expires']} "
                            f"(route it through alloc_client, or renew the row with a reason)")
    for key, n in sorted(ungated.items()):
        p, s, k = key
        r = allowed.get(key)
        at = ",".join(map(str, lines[key][:5]))
        if r is None:
            findings.append(f"{p}:{at}: {s}: ungated {k} spawn ({n}x) not via alloc_client/placement.admit and "
                            f"not in {ALLOWLIST}")
        elif n > r["count"]:
            findings.append(f"{p}:{at}: {s}: {n} ungated {k} spawns > allowlisted {r['count']} (a NEW site)")
    for key, r in allowed.items():
        n = ungated.get(key, 0)
        if n < r["count"]:
            findings.append(f"{r['path']}: {r['scope']} {r['kind']}: allowlist row is stale (count {r['count']}, "
                            f"{n} ungated now): lower the count or delete the row (the list only shrinks)")
    return findings


def seed(root: Path, allowlist: Path) -> int:
    try:
        read, sites = collect_sites(root, strict=True)
    except OSError as e:
        print(f"lint-spawn-site: read error during seed ({e}); refusing to write {allowlist}", file=sys.stderr)
        return 2
    if read == 0:   # never overwrite an inventory from a scan that read nothing
        print(f"lint-spawn-site: scanned 0 files under {root}; refusing to write {allowlist}", file=sys.stderr)
        return 1
    old = {}
    if allowlist.exists():
        old = {(r["path"], r["scope"], r["kind"]): r for r in json.loads(allowlist.read_text(encoding="utf-8"))["sites"]}
    c = Counter((p, s, k) for p, s, k, _, g in sites if not g)
    rows = []
    for (p, s, k), n in sorted(c.items()):
        o = old.get((p, s, k), {})
        rows.append({"path": p, "scope": s, "kind": k, "count": n, "reason": o.get("reason", SEED_REASON),
                     "expires": o.get("expires", SEED_EXPIRES)})
    doc = {"_doc": ("Spawn-site allowlist for scripts/lint-spawn-site.py (Router Core spec v5 §7.1, card t_3ca3dbc6). "
                    "Each row grandfathers up to `count` spawns of (path, scope, kind) that do not go through "
                    "alloc_client / placement.admit, until `expires`. The seeded rows are the Card B cutover "
                    "inventory. Never add a row for new code; the lint fails a stale row, so the list only shrinks."),
           "schema": 1, "sites": rows}
    allowlist.parent.mkdir(parents=True, exist_ok=True)
    allowlist.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    print(f"seeded {len(rows)} rows ({sum(c.values())} spawns) -> {allowlist}")
    return 0


def differential(roots, base_ref: str) -> tuple[str, list]:
    """The ratchet's own invariant: this lint finds every spawn site the BASE lint finds (distinct (path, line,
    kind)) under each root: the real tree AND the selftest fixture corpus, so a shape class the tree happens not to
    contain is still measured (#4330 r5, r7 c649f6e76616). Returns (status, missing): status is 'skip: ...' when
    the base lint cannot be read or is this file byte for byte."""
    roots = [roots] if isinstance(roots, Path) else list(roots)
    root = roots[0]
    try:
        p = subprocess.run(["git", "-C", str(root), "show", f"{base_ref}:scripts/lint-spawn-site.py"],
                           capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"skip: git show {base_ref}: {e}", []
    if p.returncode != 0:
        return f"skip: {base_ref}:scripts/lint-spawn-site.py not readable", []
    if p.stdout == Path(__file__).read_bytes():
        return f"skip: base lint at {base_ref} is this file", []
    mod = type(sys)("lint_spawn_site_base")
    mod.__dict__["__file__"] = __file__
    exec(compile(p.stdout, f"{base_ref}:scripts/lint-spawn-site.py", "exec"), mod.__dict__)
    missing: set = set()
    for r in roots:
        _, base = mod.collect_sites(r)
        _, mine = collect_sites(r)
        have = {(f, ln, k) for f, _, k, ln, _ in mine}
        missing |= {(f, ln, k) for f, _, k, ln, _ in base} - have
    return "ran", sorted(missing)


# ---------------------------------------------------------------- selftest
UNGATED_DOCKER = "#!/usr/bin/env bash\nstart() {\n  ssh ci-box 'docker run -d --rm runner:latest'\n}\n"
GATED_DOCKER = ("#!/usr/bin/env bash\nstart() {\n  ALLOC_RESERVATION=$(alloc-admit --consumer ci --job \"$1\")\n"
                "  ssh ci-box \"ALLOC_RESERVATION=$ALLOC_RESERVATION docker run -d --rm runner:latest\"\n}\n")
UNGATED_PY_SSH = "import subprocess\n\ndef go(h):\n    subprocess.run(['ssh', '-o', 'BatchMode=yes', h, 'make test'])\n"
# Prism 10dea09c29d3: a bare MENTION of the reservation (here: removing it) is not proof of gating
PR2_ENV_U_MENTION = "import subprocess\n\ndef go(h):\n    subprocess.run(['ssh', h, 'env -u ALLOC_RESERVATION docker run image'])\n"
PR2_DICT_ENV = ("import subprocess\nimport alloc_client\n\ndef go(job):\n    d, rid = alloc_client.admit('x', job_id=job)\n"
               "    subprocess.run(['docker', 'run', 'img'], env={'ALLOC_RESERVATION': rid})\n")
# Prism r3 208bec3a985c: a string VALUE naming the reservation does not make the dict a reservation env
PR3_NOTE_DICT = "import subprocess\n\ndef go(h):\n    env = {'NOTE': 'ALLOC_RESERVATION'}\n    subprocess.run(['ssh', h, 'job'], env=env)\n"
# Prism r3 d65c1a6e74e9: an admit AFTER the spawn on the same line does not gate it
PR3_LATE_ADMIT_SH = "#!/usr/bin/env bash\nssh fleet-box job; ALLOC_RESERVATION=$(alloc-admit --consumer ci --job x)\n"
PR3_PREFIX_ADMIT_SH = "#!/usr/bin/env bash\nALLOC_RESERVATION=$(alloc-admit --consumer ci --job x) ssh fleet-box job\n"
GATED_PY = ("import os, subprocess\nimport alloc_client\n\ndef go(job):\n"
            "    d, rid = alloc_client.admit('test-gate', job_id=job)\n"
            "    env = dict(os.environ, ALLOC_RESERVATION=rid)\n"
            "    subprocess.run(['ssh', d.host, 'make test'], env=env)\n")
ADMIT_ELSEWHERE = ("import subprocess, placement\n\ndef pick():\n    d, rid = placement.admit(None, 'x', {}, ttl_s=60)\n"
                   "    return d\n\ndef go(h):\n    subprocess.run(['ssh', h, 'make'])\n")
POOL_SHAPE = ("import subprocess\n\ndef ssh(alias, script):\n    return run_it(alias, script)\n\n"
              "class Pool:\n    def start(self, host, name):\n"
              "        script = f\"docker run -d --rm --name {name} img\"\n        ssh(host, script)\n")
HELPER_SSH = ("import subprocess\nSSH_OPTS=['-o','BatchMode=yes']\n\ndef _ssh_cmd():\n    return ['ssh'] + SSH_OPTS\n\n"
              "def go(h, r):\n    subprocess.Popen(_ssh_cmd() + [h, r])\n")
NOT_SPAWNS = ("#!/usr/bin/env bash\n# ssh ci-box 'docker run x'\necho \"run: ssh ci-box uptime\"\n"
              "git push git@github.com:o/r.git\nssh -T git@github.com\nssh-keygen -t ed25519\n")
RUNNER_MINT = ("class Gh:\n    def jit(self, name):\n"
               "        args = ['-X', 'POST', f'orgs/{self.org}/actions/runners/generate-jitconfig', '-f', name]\n"
               "        return self._gh(args)\n")
# Prism round 1 (#4308) false negatives, one fixture each
PR1_KW_ARGS = "import subprocess\n\ndef go(h):\n    subprocess.run(args=['ssh', h, 'make test'])\n"
PR1_LATE_ENDPOINT = ("import subprocess\n\ndef go():\n    subprocess.run(['gh', 'api', '--method', 'POST', "
                     "'orgs/acme/actions/runners/registration-token'])\n")
PR1_TWO_MINTS = ("class Gh:\n    def jit(self, a, b):\n"
                 "        self._gh(['-X', 'POST', 'orgs/o/actions/runners/generate-jitconfig', a])\n"
                 "        self._gh(['-X', 'POST', 'orgs/o/actions/runners/generate-jitconfig', b])\n")
PR1_CLASS_BODY = "import subprocess\n\nclass Worker:\n    process = subprocess.Popen(['ssh', 'ci-box', 'job'])\n"
PR1_LAMBDA = "import subprocess\n\ndef mk(h):\n    return lambda: subprocess.run(['ssh', h, 'job'])\n"
PR1_TWO_ON_LINE = "#!/usr/bin/env bash\nstart() {\n  docker run image; docker run another-image\n}\n"
PR1_LOCAL_ARGV = ("import subprocess\n\ndef go(box):\n    cmd = ['ssh', '-o', 'BatchMode=yes', box, 'uptime']\n"
                  "    subprocess.run(cmd, capture_output=True)\n")
PR1_BARE_RUN = "from subprocess import run\n\ndef go(h):\n    run(['ssh', h, 'job'])\n"
PR1_EXEC_STYLE = ("import asyncio, os\n\nasync def go():\n    await asyncio.create_subprocess_exec('docker', 'run', 'img')\n\n"
                  "def go2(h):\n    os.execvp('ssh', ['ssh', h, 'job'])\n")
PR1_STARRED = "import subprocess\nBASE = ['ssh', 'ci-box']\n\ndef go():\n    subprocess.run([*BASE, 'job'])\n"
# Prism round 2 (#4308)
PR2_LATE_ENV = ("import os, subprocess\n\ndef go(h, rid):\n    env = dict(os.environ)\n"
                "    subprocess.run(['ssh', h, 'start'], env=env)\n    env['ALLOC_RESERVATION'] = rid\n")
PR2_ASYNC = "import asyncio\n\nasync def go():\n    await asyncio.create_subprocess_exec('docker', 'run', 'img')\n"
PR2_PRINTF_PIPE = "#!/usr/bin/env bash\nmint() {\n  printf '%s' \"$JIT\" | ssh ci-box 'docker run --rm runner:latest'\n}\n"
PR2_ECHO_SUBST = "#!/usr/bin/env bash\necho \"up: $(ssh ci-box uptime)\"\n"
# t_ed9b147b: #4308 Prism r3/r4 residue, one fixture each (RED on the #4308 lint, GREEN-as-RED after)
PR3_LATE_ARGV = ("import subprocess\n\ndef go():\n    cmd = ['ssh', 'ci-box', 'job']\n    subprocess.run(cmd)\n"
                 "    cmd = ['echo', 'done']\n")
PR3_ENV_OVERWRITE = ("import os, subprocess\n\ndef go(h, rid):\n    env = dict(os.environ, ALLOC_RESERVATION=rid)\n"
                     "    env = {}\n    subprocess.run(['ssh', h, 'job'], env=env)\n")
PR3_ENV_DICT_OVERWRITE = ("import subprocess\n\ndef go(h, rid):\n    env = {'ALLOC_RESERVATION': rid}\n    env = {}\n"
                          "    subprocess.run(['ssh', h, 'job'], env=env)\n")
PR3_ENV_COPY_GATED = ("import os, subprocess\nimport alloc_client\n\ndef go(job):\n"
                      "    d, rid = alloc_client.admit('test-gate', job_id=job)\n"
                      "    env = dict(os.environ, ALLOC_RESERVATION=rid)\n    env = dict(env, TERM='dumb')\n"
                      "    subprocess.run(['ssh', d.host, 'make test'], env=env)\n")
PR3_CONTINUATION = "#!/usr/bin/env bash\nstart() {\n  docker \\\n    run --rm runner:latest\n}\n"
PR3_GATE_AFTER = "#!/usr/bin/env bash\nstart() {\n  ssh ci-box job; ALLOC_RESERVATION=$(alloc-admit --consumer ci)\n}\n"
PR3_GATE_BEFORE = ("#!/usr/bin/env bash\nstart() {\n  ALLOC_RESERVATION=$(alloc-admit --consumer ci); "
                   "ssh ci-box \"ALLOC_RESERVATION=$ALLOC_RESERVATION job\"\n}\n")
PR3_DOCKER_GLOBAL_PY = ("import subprocess\n\ndef go():\n"
                        "    subprocess.run(['docker', '--host', 'unix:///var/run/docker.sock', 'run', 'image'])\n")
PR3_DOCKER_GLOBAL_SH = "#!/usr/bin/env bash\nstart() {\n  docker --host ssh://ci-box run image\n}\n"
# #4330 Prism r1
PR5_SELECTIVE_COPY = ("import os, subprocess\n\ndef go(h, rid):\n    env = dict(os.environ, ALLOC_RESERVATION=rid)\n"
                      "    child_env = {'PATH': env['PATH']}\n    subprocess.run(['ssh', h, 'job'], env=child_env)\n")
PR5_SPREAD_COPY_GATED = ("import os, subprocess\n\ndef go(h, rid):\n    env = dict(os.environ, ALLOC_RESERVATION=rid)\n"
                         "    child_env = {**env, 'TERM': 'dumb'}\n    subprocess.run(['ssh', h, 'job'], env=child_env)\n")
PR5_SELF_CONCAT = ("def go(client):\n    script = 'docker run'\n    script = script + ' --rm image'\n"
                   "    client.ssh(script)\n")
PR5_HEREDOC_TAIL = "#!/usr/bin/env bash\ncat <<'EOF'\nliteral \\\nEOF\nssh ci-box job\n"
PR5_HEREDOC_UNQUOTED = "#!/usr/bin/env bash\nssh ci-box bash <<EOF\ndocker \\\n  run --rm img\nEOF\n"
# #4330 Prism r2
PR6_POP = ("import os, subprocess\n\ndef go(h, rid):\n    env = dict(os.environ, ALLOC_RESERVATION=rid)\n"
           "    child_env = env.copy()\n    child_env.pop('ALLOC_RESERVATION', None)\n"
           "    subprocess.run(['ssh', h, 'job'], env=child_env)\n")
PR6_BS_SPACE = "#!/usr/bin/env bash\necho ready \\ \nssh ci-box job\n"
PR6_LOOP_DOCKER = ("def go(client):\n    script = 'echo ready'\n    for _ in range(2):\n        client.ssh(script)\n"
                   "        script = 'docker run img'\n")
PR6_LOOP_ARGV = ("import subprocess\n\ndef go():\n    cmd = ['echo', 'ready']\n    for _ in range(2):\n"
                 "        subprocess.run(cmd)\n        cmd = ['ssh', 'ci-box', 'job']\n")
PR6_SUBST_ONCE = "#!/usr/bin/env bash\nOUT=$(ssh -o BatchMode=yes \\\n  ci-box uptime)\n"
PR6_COND_REBIND = ("def go(client, dry_run):\n    script = 'docker run image'\n    if dry_run:\n"
                   "        script = 'echo skipped'\n    client.ssh(script)\n")
PR6_COND_GATE = ("import os, subprocess\nimport alloc_client\n\ndef go(h, fast):\n    env = dict(os.environ)\n"
                 "    if not fast:\n        d, rid = alloc_client.admit('x', job_id='j')\n"
                 "        env = dict(os.environ, ALLOC_RESERVATION=rid)\n    subprocess.run(['ssh', h, 'job'], env=env)\n")
# #4330 Apollo r4 (Prism 36dce92f8a82 2f094bae65c8 0419f8ff71c3 5583389c6af3 6796b1769443 26f2305413bc d1f06dc28543
# fecf5ae12966 4c22408394e3)
PR7_STAR_TAIL = ("import asyncio\nARGS = ['run', 'img']\n\nasync def go():\n"
                 "    await asyncio.create_subprocess_exec('docker', *ARGS)\n")
PR7_STAR_HEAD = ("import asyncio\n\nasync def go():\n    base = ['docker']\n"
                 "    await asyncio.create_subprocess_exec(*base, 'run', 'image')\n")
PR7_IFEXP = "import subprocess\n\ndef go(h, remote):\n    cmd = ['ssh', h, 'job'] if remote else ['true']\n    subprocess.run(cmd)\n"
PR7_IFEXP_INLINE = "import subprocess\n\ndef go(h, remote):\n    subprocess.run(['ssh', h, 'job'] if remote else ['true'])\n"
PR7_UPDATE_NOTE = ("import subprocess\n\ndef go(h):\n    env = {}\n    env.update({'NOTE': 'ALLOC_RESERVATION'})\n"
                   "    subprocess.run(['ssh', h, 'job'], env=env)\n")
PR7_UPDATE_GATED = ("import os, subprocess\n\ndef go(h, rid):\n    env = dict(os.environ)\n"
                    "    env.update({'ALLOC_RESERVATION': rid})\n    subprocess.run(['ssh', h, 'job'], env=env)\n")
PR7_INLINE_SELECT = ("import os, subprocess\n\ndef go(rid):\n    env = dict(os.environ, ALLOC_RESERVATION=rid)\n"
                     "    subprocess.run(['docker', 'run', 'img'], env={'PATH': env['PATH']})\n")
PR7_COMMENT_BS = "#!/usr/bin/env bash\nls  # see foo \\\nssh ci-box job\n"
PR7_QUOTED_HASH_BS = "#!/usr/bin/env bash\ndocker --config \"/tmp/#c\" \\\nrun img\n"
PR7_REPEAT_CMD = "#!/usr/bin/env bash\nssh ci-box job;ALLOC_RESERVATION=$(alloc-admit --consumer ci);ssh ci-box job\n"
PR7_TOKEN_JOIN = "#!/usr/bin/env bash\ndocker ru\\\nn --rm image\n"
# #4330 Apollo r5: discovery must be a superset of main's (every ast.Call is a candidate); MAY facts never gate
PR8_TARGET_CALL = "import subprocess\n\ndef go(outputs):\n    outputs[subprocess.check_output(['ssh', 'ci-box', 'job'])] = True\n"
PR8_MATCH_GUARD = ("import subprocess\n\ndef go(a):\n    match a:\n        case 1 if subprocess.run(['ssh', 'ci-box', 'job']):\n"
                   "            pass\n")
PR8_UNPACK = "import subprocess\n\ndef go():\n    cmd = ['ssh', 'ci-box', 'job']\n    a, cmd = 1, cmd\n    subprocess.run(cmd)\n"
PR8_SHORT_CIRCUIT = ("import os, subprocess\n\ndef go(rid):\n    env = dict(os.environ)\n"
                     "    rid and env.update(ALLOC_RESERVATION=rid)\n    subprocess.run(['ssh', 'ci-box', 'job'], env=env)\n")
PR8_IFEXP_MUT = ("import os, subprocess\n\ndef go(rid):\n    env = dict(os.environ)\n"
                 "    env.update(ALLOC_RESERVATION=rid) if rid else None\n    subprocess.run(['ssh', 'ci-box', 'job'], env=env)\n")
PR8_EVAL_ORDER = ("import subprocess\n\ndef go():\n    env = {}\n"
                  "    env.update(ALLOC_RESERVATION=subprocess.check_output(['ssh', 'ci-box', 'job'], env=env))\n")
PR8_WHILE_TEST = ("import subprocess\n\ndef go():\n    cmd = ['echo', 'ready']\n    while subprocess.call(cmd) == 0:\n"
                  "        cmd = ['ssh', 'ci-box', 'true']\n")
PR8_DEFAULT_ARG = "import subprocess\n\ndef launch(proc=subprocess.Popen(['ssh', 'ci-box', 'job'])):\n    return proc\n"
PR8_DECORATOR = "import subprocess\n\n@register(subprocess.run(['docker', 'run', 'img']))\ndef go():\n    pass\n"
PR8_EXTEND = "import subprocess\n\ndef go():\n    cmd = []\n    cmd.extend(['ssh', 'ci-box', 'job'])\n    subprocess.run(cmd)\n"
PR8_STAR_ECHO = "import asyncio\n\nasync def go():\n    await asyncio.create_subprocess_exec('echo', *['ssh', 'ci-box', 'job'])\n"
PR9_SUBST_BEFORE_ADMIT = ("#!/usr/bin/env bash\nX=$(ssh fleet-box pre) ALLOC_RESERVATION=$(alloc-admit --consumer ci --job x) "
                          "ssh fleet-box work\n")
PR9_SUBST_IN_ADMIT = ("#!/usr/bin/env bash\nALLOC_RESERVATION=$(alloc-admit --consumer ci --job $(ssh fleet-box getjob)) "
                      "ssh fleet-box work\n")
PR9_SUBST_AFTER_ADMIT = ("#!/usr/bin/env bash\nALLOC_RESERVATION=$(alloc-admit --consumer ci --job x) Y=$(ssh fleet-box post) "
                         "ssh fleet-box work\n")
PR9_ALIAS_DEL = ("import os, subprocess\n\ndef go(rid):\n    env = dict(os.environ, ALLOC_RESERVATION=rid)\n    alias = env\n"
                 "    del env['ALLOC_RESERVATION']\n    subprocess.run(['ssh', 'ci-box', 'job'], env=alias)\n")
PR9_ALIAS_KEPT = ("import os, subprocess\n\ndef go(rid):\n    env = dict(os.environ, ALLOC_RESERVATION=rid)\n    alias = env\n"
                  "    subprocess.run(['ssh', 'ci-box', 'job'], env=alias)\n")
# #4330 Apollo r7: an except-handler TYPE runs at match time (c649f6e76616); a handler is entered from any raise point
# in the body, so a reservation deleted before a raise and re-set after it is NOT present there (4f3ee8208e67)
PR10_HANDLER_TYPE = ("import subprocess\n\ndef go():\n    try:\n        raise ValueError\n"
                     "    except (subprocess.run(['ssh', 'ci-box', 'error-type']) or Exception):\n        pass\n")
PR10_HANDLER_MIDRAISE = ("import os, subprocess\n\ndef go(rid):\n    env = dict(os.environ, ALLOC_RESERVATION=rid)\n"
                         "    try:\n        del env['ALLOC_RESERVATION']\n        prepare()\n"
                         "        env['ALLOC_RESERVATION'] = rid\n    except Exception:\n"
                         "        subprocess.run(['ssh', 'ci-box', 'job'], env=env)\n")
PR10_HANDLER_KEPT = ("import os, subprocess\n\ndef go(rid):\n    env = dict(os.environ, ALLOC_RESERVATION=rid)\n"
                     "    try:\n        prepare()\n    except Exception:\n"
                     "        subprocess.run(['ssh', 'ci-box', 'job'], env=env)\n")
PR10_FINALLY_MIDRAISE = ("import os, subprocess\n\ndef go(rid):\n    env = dict(os.environ, ALLOC_RESERVATION=rid)\n"
                         "    try:\n        env.pop('ALLOC_RESERVATION')\n        prepare()\n"
                         "        env['ALLOC_RESERVATION'] = rid\n    finally:\n"
                         "        subprocess.run(['ssh', 'ci-box', 'job'], env=env)\n")
# t_0548c81c: calls inside del/for/with targets run (Prism 2060cd33d39f); a for target binds the elements of an
# explicit iterable (Prism 9660245e676f)
PR11_DEL_TARGET = "import subprocess\n\ndef go(outputs):\n    del outputs[subprocess.check_output(['ssh', 'ci-box', 'job'])]\n"
PR11_FOR_TARGET = ("import subprocess\n\ndef go(out, xs):\n"
                   "    for out[subprocess.check_output(['ssh', 'ci-box', 'job'])] in xs:\n        pass\n")
PR11_WITH_TARGET = ("import subprocess\n\ndef go(out, cm):\n"
                    "    with cm as out[subprocess.check_output(['ssh', 'ci-box', 'job'])]:\n        pass\n")
PR11_FOR_REUSE = "import subprocess\ncmd = ['ssh', 'ci-box', 'job']\nfor cmd in [cmd]:\n    subprocess.run(cmd)\n"
PR11_FOR_TUPLE = ("import subprocess\n\ndef go():\n    for i, cmd in [(1, ['echo']), (2, ['ssh', 'ci-box', 'job'])]:\n"
                  "        subprocess.run(cmd)\n")
PR11_FOR_ECHO = "import subprocess\n\ndef go():\n    for cmd in [['echo', 'a'], ['true']]:\n        subprocess.run(cmd)\n"
PR11_FOR_GATED_ONE = ("import os, subprocess\n\ndef go(rid):\n"
                      "    for env in [dict(os.environ, ALLOC_RESERVATION=rid), dict(os.environ)]:\n"
                      "        subprocess.run(['ssh', 'ci-box', 'job'], env=env)\n")
PR8_SH_CONT_LINE = ("#!/usr/bin/env bash\ntick \\\n  --feed-cmd \"ssh ci-box 'cat log'\" \\\n"
                    "  --alerts-cmd \"ssh ci-box 'cscli alerts'\"\n")
# t_c59fff64: a spawn through an INJECTED runner (`run=subprocess.run` test seam) is that subprocess call
INJ_DEFAULT = ("import subprocess\n\ndef collect_remote(host, run=subprocess.run):\n    def attempt(*extra):\n"
               "        return run(['ssh', '-o', 'BatchMode=yes', *extra, host, 'job'], check=True)\n"
               "    return attempt()\n")
INJ_DEFAULT_DIRECT = "import subprocess\n\ndef go(h, runner=subprocess.check_output):\n    runner(['ssh', h, 'job'])\n"
INJ_KWONLY_POPEN = "import subprocess\n\ndef go(h, *, spawn=subprocess.Popen):\n    spawn(['docker', 'run', 'img'])\n"
INJ_PARTIAL = ("import functools, subprocess\n\ndef go(h):\n    run = functools.partial(subprocess.run, check=True)\n"
               "    run(['ssh', h, 'job'])\n")
INJ_PARTIAL_ARGV = ("import subprocess\nfrom functools import partial\nSSH = partial(subprocess.call, ['ssh', 'ci-box', 'job'])\n"
                    "\ndef go():\n    SSH()\n")
INJ_LOCAL_ALIAS = "import subprocess\n\ndef go(h):\n    r = subprocess.run\n    r(['ssh', h, 'job'])\n"
INJ_FAKE_NO_DEFAULT = "def go(h, run):\n    run(['ssh', h, 'job'])\n"
INJ_FAKE_OTHER_DEFAULT = "def fake(*a, **k):\n    return 0\n\ndef go(h, run=fake):\n    run(['ssh', h, 'job'])\n"
INJ_SHADOWED = ("import subprocess\n\ndef outer(h, run=subprocess.run):\n    def inner(run):\n"
                "        run(['ssh', h, 'job'])\n    return inner\n")
INJ_ECHO = "import subprocess\n\ndef go(run=subprocess.run):\n    run(['echo', 'ssh'])\n"
# t_f7b70a42: the 10 Prism findings of the router-core re-pin wave (hermes-agent#2259, prism-router#683,
# fleet-ops-scripts#285), each fixture verbatim from the reader repro (var/oneshot/desk-t_6b4d3881/repro.py)
RP_DEF_BODY_LEAK = ("import subprocess\nfrom functools import partial\nfrom subprocess import run\ndef unused():\n"
                    "    run = partial(subprocess.run, ['echo'])\ndef go():\n    run(['ssh', 'ci-box', 'job'])\n")
RP_SUBSCRIPT_KEY = ("import subprocess\ndef go():\n    note = 'ALLOC_RESERVATION=rid'\n    env = {}\n"
                    "    env['NOTE'] = note\n    subprocess.run(['ssh', 'ci-box', 'job'], env=env)\n")
RP_SURVIVING_ALIAS = ("import subprocess\ndef go():\n    env = {'ALLOC_RESERVATION': 'rid'}\n    a = env\n    b = a\n"
                      "    a = {}\n    env.pop('ALLOC_RESERVATION')\n    subprocess.run(['ssh', 'ci-box', 'job'], env=b)\n")
# Prism 73953ef77e50: two adjacent middle names rebound in ONE call (tuple / multi-name for-target) must still
# reconnect env~c through the chain as it stood before the call
RP_TUPLE_MID_REBIND = ("import os, subprocess\ndef go(rid):\n    env = dict(os.environ, ALLOC_RESERVATION=rid)\n"
                       "    a = env\n    b = a\n    c = b\n    a, b = {}, {}\n    env.pop('ALLOC_RESERVATION')\n"
                       "    subprocess.run(['ssh', 'ci-box', 'job'], env=c)\n")
RP_FOR_MID_REBIND = ("import os, subprocess\ndef go(rid, pairs):\n    env = dict(os.environ, ALLOC_RESERVATION=rid)\n"
                     "    a = env\n    b = a\n    c = b\n    for a, b in pairs:\n        env.pop('ALLOC_RESERVATION')\n"
                     "        subprocess.run(['ssh', 'ci-box', 'job'], env=c)\n")
# the Prism repro as a function is UnboundLocalError on both branches (`run` is local); the same text at module
# scope runs, and the function shape that runs binds `run` on both branches
RP_BRANCH_RUNNER = ("from functools import partial\nfrom subprocess import run\nif flag:\n"
                    "    run = partial(run, ['echo'])\n    run()\nelse:\n    run(['ssh', 'ci-box', 'job'])\n")
RP_BRANCH_RUNNER_FN = ("import subprocess\nfrom functools import partial\ndef go(flag):\n    if flag:\n"
                       "        run = partial(subprocess.run, ['echo'])\n    else:\n        run = subprocess.run\n"
                       "    run(['ssh', 'ci-box', 'job'])\n")
RP_LOOP_STALE = ("import os, subprocess\ndef go(rid):\n    env = dict(os.environ, ALLOC_RESERVATION=rid)\n"
                 "    for e in [env, env]:\n        subprocess.run(['ssh', 'ci-box', 'job'], env=e)\n"
                 "        e.pop('ALLOC_RESERVATION', None)\n")
RP_LOOP_KEPT = ("import os, subprocess\ndef go(rid):\n    env = dict(os.environ, ALLOC_RESERVATION=rid)\n"
                "    for e in [env, env]:\n        subprocess.run(['ssh', 'ci-box', 'job'], env=e)\n")
RP_TRUNCATED = ("import os, subprocess\ndef go(rid):\n    g = dict(os.environ, ALLOC_RESERVATION=rid)\n"
                "    for env in [" + "g, " * 16 + "{}]:\n        subprocess.run(['ssh', 'ci-box', 'job'], env=env)\n")
RP_TRUNCATED_ARGV = ("import subprocess\ndef go():\n    for cmd in [" + "['echo'], " * 16 + "['ssh', 'ci-box', 'job']]:\n"
                     "        subprocess.run(cmd)\n")
RP_SHADOWED_ALIAS = ("import subprocess\nrunner = subprocess.run\ndef fake(*a, **k):\n    pass\ndef go():\n"
                     "    runner = fake\n    invoke = runner\n    invoke(['ssh', 'ci-box', 'job'])\n")
RP_LOCAL_ALIAS_CHAIN = ("import subprocess\ndef go():\n    runner = subprocess.run\n    invoke = runner\n"
                        "    invoke(['ssh', 'ci-box', 'job'])\n")
PY_NOT = "import subprocess\nsubprocess.run(['ssh-keygen', '-t', 'ed25519'])\nsubprocess.run(['git', 'push'])\n"


# Prism round 1 on #6512
RP_GLOBAL_RUNNER = ("import subprocess\nfrom functools import partial\nrunner = subprocess.run\ndef go():\n"
                    "    global runner\n    runner = partial(runner, ['ssh', 'ci-box', 'job'])\n    runner()\n")
RP_TWO_KINDS = ("import subprocess\nfrom functools import partial\ndef go(flag):\n    if flag:\n"
                "        run = partial(subprocess.run, ['ssh', 'ci-box', 'job'])\n    else:\n"
                "        run = partial(subprocess.run, ['docker', 'run', 'image'])\n    run()\n")
RP_NINE_ALTS = ("import subprocess\nfrom functools import partial\n"
                + "".join(f"r = partial(subprocess.run, ['echo', '{i}'])\n" for i in range(8))
                + "def go(run=r):\n    run = subprocess.run\n    run(['ssh', 'ci-box', 'job'])\n")


# Prism round 2 on #6512
RP_GLOBAL_INSTALL = ("import subprocess\ndef install():\n    global runner\n    runner = subprocess.run\ninstall()\n"
                     "runner(['ssh', 'ci-box', 'job'])\n")
RP_GLOBAL_IMPORT_KEPT = ("from subprocess import run\ndef fake(*a, **k):\n    pass\ndef go(flag):\n    global run\n"
                         "    if flag:\n        run = fake\n    invoke = run\n    invoke(['ssh', 'ci-box', 'job'])\n")
RP_NONLOCAL_INSTALL = ("import subprocess\ndef outer():\n    runner = None\n    def install():\n        nonlocal runner\n"
                       "        runner = subprocess.run\n    install()\n    runner(['ssh', 'ci-box', 'job'])\n")


# desk read r1 on #6512 (t_f7b70a42): an export through a local alias resolves in the defining scope; a loop target
# holding ssh AND docker-run lists keeps both kinds
RP_GLOBAL_EXPORT_VIA_LOCAL = ("import subprocess\ndef install():\n    global runner\n    local_run = subprocess.run\n"
                              "    runner = local_run\ninstall()\nrunner(['ssh', 'ci-box', 'job'])\n")
RP_NONLOCAL_EXPORT_VIA_LOCAL = ("import subprocess\ndef outer():\n    runner = print\n    def inner():\n"
                                "        nonlocal runner\n        lr = subprocess.run\n        runner = lr\n    inner()\n"
                                "    runner(['ssh', 'h', 'j'])\n")
RP_LOOP_TWO_KINDS = ("import subprocess\n\n\ndef go():\n    for cmd in ["
                     + ", ".join(["['ssh', 'ci-box', 'job']"] * 16 + ["['docker', 'run', 'image']"])
                     + "]:\n        subprocess.run(cmd)\n")
# desk r4 (093e422a62c5 re-opened): a class body exporting through `global` / `nonlocal` reaches the enclosing scope
RP_GLOBAL_CLASS_INSTALL = ("import subprocess\nclass Install:\n    global runner\n    runner = subprocess.run\n"
                           "runner(['ssh', 'ci-box', 'job'])\n")
RP_NONLOCAL_CLASS_INSTALL = ("import subprocess\ndef outer():\n    runner = print\n    class Install:\n"
                             "        nonlocal runner\n        runner = subprocess.run\n    runner(['ssh', 'ci-box', 'job'])\n")
RP_NONLOCAL_PAST_MIDDLE = ("import subprocess\ndef outer():\n    runner = subprocess.run\n    def middle(flag):\n"
                           "        if flag:\n            def inner():\n                nonlocal runner\n"
                           "                runner = print\n        invoke = runner\n        invoke(['ssh', 'h', 'job'])\n"
                           "    middle(False)\n")
RP_NONLOCAL_PARTIAL_DIRECT = ("import subprocess\nfrom functools import partial\ndef outer():\n"
                              "    runner = subprocess.run\n    def mid():\n        def inner():\n"
                              "            nonlocal runner\n            runner = partial(subprocess.run, ['echo'])\n"
                              "        runner(['ssh', 'ci-box', 'job'])\n    mid()\n")
# t_540a3e9a: elements past LOOP_ELTS_CAP are classified when spawn-ish (desk probe P3c on #6512)
RP_LOOP_PAST_CAP = ("import subprocess\ndef go():\n    for cmd in ["
                    + ", ".join([f"['echo', '{i}']" for i in range(70)] + ["['ssh', 'h', 'j']"])
                    + "]:\n        subprocess.run(cmd)\n")
RP_LOOP_PAST_CAP_TUPLE = ("import subprocess\ndef go():\n    for i, cmd in ["
                          + ", ".join([f"({i}, ['echo', '{i}'])" for i in range(70)] + ["(70, ['docker', 'run', 'img'])"])
                          + "]:\n        subprocess.run(cmd)\n")
RP_LOOP_PAST_CAP_NONE = ("import subprocess\ndef go():\n    for cmd in ["
                         + ", ".join(f"['echo', '{i}']" for i in range(71)) + "]:\n        subprocess.run(cmd)\n")
# Prism 723aa276d5fc: a spawn head outranks heads that only carry a spawn word ('run') when _heads truncates
RP_LOOP_SPAWNWORD_33 = ("import subprocess\ndef go():\n    for cmd in [['ssh', 'h', 'j'], "
                        + ", ".join(f"['echo', 'run', '{i}']" for i in range(32)) + "]:\n        subprocess.run(cmd)\n")
# Prism 8ca6f3c667ce: the _heads cap ranks the COMPOSED head: ['run','img'] spawns only under ['docker'] + cmd
RP_LOOP_COMPOSED_DOCKER = ("import subprocess\ndef go():\n    for cmd in ["
                           + ", ".join(f"['ssh', 'h', '{i}']" for i in range(32))
                           + ", ['run', 'img']]:\n        subprocess.run(['docker'] + cmd)\n")
# Prism 5434b042f044: a nested `global` exporter resolves in its DEFINING scope (outer()'s local_run), not the module's
RP_GLOBAL_EXPORT_NESTED = ("import subprocess\ndef outer():\n    local_run = subprocess.run\n    def install():\n"
                           "        global runner\n        runner = local_run\n    install()\nouter()\n"
                           "runner(['ssh', 'ci-box', 'job'])\n")
# Prism 4433291449e8: a name the exporter declares `global` resolves in the MODULE, never in a shadowing outer() local
RP_GLOBAL_EXPORT_SHADOWED = ("import subprocess\nlr = subprocess.run\ndef outer():\n    lr = print\n    def install():\n"
                             "        global runner, lr\n        x = lr\n        runner = x\n    install()\nouter()\n"
                             "runner(['ssh', 'ci-box', 'job'])\n")
# Prism b7d07f333edc: an exported RHS resolves ONLY in the exporter's scope; install()'s local `lr = print` is what
# `runner = lr` binds, never the module's `lr = subprocess.run`
RP_EXPORT_RHS_LOCAL = ("import subprocess\nlr = subprocess.run\nrunner = print\ndef outer():\n    def install():\n"
                       "        global runner\n        lr = print\n        runner = lr\n    install()\nouter()\n"
                       "runner(['ssh', 'h', 'j'])\n")
RP_EXPORT_RHS_LOCAL_NONLOCAL = ("import subprocess\ndef outer():\n    lr = subprocess.run\n    runner = print\n"
                                "    def install():\n        nonlocal runner\n        lr = print\n        runner = lr\n"
                                "    install()\n    runner(['ssh', 'h', 'j'])\n")
# control: with no exporter-local `lr`, the exporter's `lr` IS the module's subprocess.run
RP_EXPORT_RHS_MODULE = ("import subprocess\nlr = subprocess.run\nrunner = print\ndef outer():\n    def install():\n"
                        "        global runner\n        runner = lr\n    install()\nouter()\nrunner(['ssh', 'h', 'j'])\n")
# Prism e694e43ce56f: five exporters defined in reverse dependency order need five rounds; the alias loop runs to a
# fixpoint, never a fixed round count
RP_EXPORT_CHAIN_REVERSED = ("import subprocess\n"
                            "def install5(): global runner; runner = r4\ndef install4(): global r4; r4 = r3\n"
                            "def install3(): global r3; r3 = r2\ndef install2(): global r2; r2 = r1\n"
                            "def install1(): global r1; r1 = subprocess.run\n"
                            "install1(); install2(); install3(); install4(); install5()\n"
                            "runner(['ssh', 'h', 'job'])\n")

# t_8ad18b12: four pre-existing misses (desk-t_e3dbf19f a4/a5, desk-t_540a3e9a C4/C5), each with a GREEN control
RP_EXPORT_COMP = ("import subprocess\ndef outer():\n    lr = subprocess.run\n    def install():\n        global runner\n"
                  "        runner = [lr for _ in range(1)][0]\n    install()\nouter()\nrunner(['ssh', 'ci-box', 'job'])\n")
RP_EXPORT_COMP_SHADOW = ("import subprocess\ndef outer():\n    lr = subprocess.run\n    def install():\n"
                         "        global runner\n        runner = [lr for lr in [print]][0]\n    install()\nouter()\n"
                         "runner(['ssh', 'ci-box', 'job'])\n")
RP_NONLOCAL_TWO_DEEP = ("import subprocess\ndef outer():\n    runner = print\n    def mid():\n        lr = subprocess.run\n"
                        "        def install():\n            nonlocal runner\n            runner = lr\n        install()\n"
                        "    mid()\n    runner(['ssh', 'ci-box', 'job'])\nouter()\n")
RP_NONLOCAL_TWO_DEEP_MID = ("import subprocess\ndef outer():\n    runner = print\n    def mid():\n        runner = print\n"
                            "        lr = subprocess.run\n        def install():\n            nonlocal runner\n"
                            "            runner = lr\n        install()\n    mid()\n    runner(['ssh', 'ci-box', 'job'])\n"
                            "outer()\n")
RP_LOOP_PROG_NAME = ("import subprocess\ndef go():\n    for cmd in [" + ", ".join(f"'echo{i}'" for i in range(70))
                     + ", '/usr/bin/ssh']:\n        subprocess.run([cmd, 'ci-box', 'job'])\n")
RP_LOOP_PROG_NAME_NONE = ("import subprocess\ndef go():\n    for cmd in [" + ", ".join(f"'echo{i}'" for i in range(70))
                          + "]:\n        subprocess.run([cmd, 'ci-box', 'job'])\n")
RP_LOOP_DICT_ARGV = ("import subprocess\ndef go():\n    for cmd in [" + ", ".join(f"{{'a': ['echo', '{i}']}}" for i in range(70))
                     + ", {'a': ['ssh', 'h', 'j']}]:\n        subprocess.run(cmd['a'])\n")
RP_COMP_SHADOW_BARE = ("from subprocess import run\nrunner = [run for run in [print]][0]\nrunner(['ssh', 'h', 'job'])\n")
RP_PROG_PARAM_SHADOW = ("import subprocess\nssh_prog = 'echo'\ndef go(ssh_prog):\n    subprocess.run([ssh_prog, 'ci-box', 'job'])\n"
                        "go('ssh')\n")
RP_DICT_ELEM_STORE = ("import subprocess\ncmd = {}\ncmd['a'] = ['ssh', 'h', 'job']\nsubprocess.run(cmd['a'])\n")
RP_DICT_ELEM_REBOUND = ("import subprocess\ncmd = {'a': ['docker', 'run', 'img']}\ncmd['a'] = ['echo', 'hi']\n"
                        "subprocess.run(cmd['a'])\n")
RP_LOOP_DICT_OTHER_KEY = ("import subprocess\ndef go():\n    for cmd in [{'a': ['echo', '0']}, {'b': ['ssh', 'h', 'j']}]:\n"
                          "        subprocess.run(cmd['a'])\n")


# fixtures where the BASE lint reports a site that is not a spawn, fixed on purpose: kept out of the differential
# corpus (each is a GREEN case above, so the fix itself is still gated)
DIFF_FP_FIXED = frozenset((RP_SHADOWED_ALIAS, RP_EXPORT_RHS_LOCAL, RP_EXPORT_RHS_LOCAL_NONLOCAL))


def selftest() -> int:
    today = dt.date(2026, 10, 15)

    def tree(files: dict[str, str], rows: list | None) -> Path:
        d = Path(tempfile.mkdtemp(prefix="spawn-site-"))
        for n, b in files.items():
            (d / n).parent.mkdir(parents=True, exist_ok=True)
            (d / n).write_text(b)
        if rows is not None:
            (d / "fleet").mkdir(exist_ok=True)
            (d / ALLOWLIST).write_text(json.dumps({"sites": rows}))
        return d

    def row(p, s, k, n=1, exp="2027-01-15"):
        return {"path": p, "scope": s, "kind": k, "count": n, "reason": "fixture", "expires": exp}

    cases = [
        ("S6: empty scan root is RED", {}, [], 1),
        ("planted ungated docker run on a fleet host is RED", {"pool.sh": UNGATED_DOCKER}, [], 1),
        ("ungated python ssh spawn is RED", {"go.py": UNGATED_PY_SSH}, [], 1),
        ("ssh via a *ssh* helper (`_ssh_cmd() + [...]`) is RED", {"h.py": HELPER_SSH}, [], 1),
        ("pool shape: docker-run script handed to ssh() is RED", {"p.py": POOL_SHAPE}, [], 1),
        ("CI runner mint (generate-jitconfig via a built argv) is RED", {"m.py": RUNNER_MINT}, [], 1),
        ("Prism r1: argv via args= keyword is RED", {"k.py": PR1_KW_ARGS}, [], 1),
        ("Prism r1: runner-mint endpoint after the 4th token is RED", {"e.py": PR1_LATE_ENDPOINT}, [], 1),
        ("Prism r1: second mint in an allowlisted function is RED", {"m.py": PR1_TWO_MINTS},
         [row("m.py", "Gh.jit", "runner-mint")], 1),
        ("Prism r1: spawn in a class body is RED", {"c.py": PR1_CLASS_BODY}, [], 1),
        ("Prism r2: `env -u ALLOC_RESERVATION` mention is RED, not proof", {"u.py": PR2_ENV_U_MENTION}, [], 1),
        ("Prism r2: env={'ALLOC_RESERVATION': rid} dict literal is GREEN", {"g.py": PR2_DICT_ENV}, [], 0),
        ("Prism r3: {'NOTE': 'ALLOC_RESERVATION'} value is RED, not an env", {"n.py": PR3_NOTE_DICT}, [], 1),
        ("Prism r3: admit AFTER the spawn on one line is RED", {"l.sh": PR3_LATE_ADMIT_SH}, [], 1),
        ("Prism r3: prefix-assignment admit on the spawn is GREEN", {"p.sh": PR3_PREFIX_ADMIT_SH}, [], 0),
        ("Prism r1: spawn in a lambda is RED", {"l.py": PR1_LAMBDA}, [], 1),
        ("Prism r1: two docker runs on one line vs count 1 is RED", {"t.sh": PR1_TWO_ON_LINE},
         [row("t.sh", "start", "docker-run")], 1),
        ("Prism r1: argv bound to a local name (cmd = [...]) is RED", {"v.py": PR1_LOCAL_ARGV}, [], 1),
        ("Prism r1: bare run() from `from subprocess import run` is RED", {"b.py": PR1_BARE_RUN}, [], 1),
        ("Prism r1: exec-style create_subprocess_exec/os.execvp is RED (2 sites)", {"x.py": PR1_EXEC_STYLE},
         [row("x.py", "go", "docker-run")], 1),
        ("Prism r1: exec-style both allowlisted is GREEN", {"x.py": PR1_EXEC_STYLE},
         [row("x.py", "go", "docker-run"), row("x.py", "go2", "ssh")], 0),
        ("Prism r1: Starred module-level argv is RED", {"s.py": PR1_STARRED}, [], 1),
        ("Prism r2: ALLOC_RESERVATION set AFTER the spawn does not gate -> RED", {"f.py": PR2_LATE_ENV}, [], 1),
        ("Prism r2: await create_subprocess_exec('docker','run',...) is RED", {"a.py": PR2_ASYNC}, [], 1),
        ("Prism r2: printf ... | ssh h 'docker run' is RED", {"p.sh": PR2_PRINTF_PIPE}, [], 1),
        ("Prism r2: echo \"$(ssh h ...)\" is RED", {"e.sh": PR2_ECHO_SUBST}, [], 1),
        ("admit in ANOTHER function does not gate (same-scope rule) -> RED", {"a.py": ADMIT_ELSEWHERE}, [], 1),
        ("bash ALLOC_RESERVATION from alloc-admit in scope is GREEN", {"pool.sh": GATED_DOCKER}, [], 0),
        ("python alloc_client.admit -> env ALLOC_RESERVATION is GREEN", {"go.py": GATED_PY}, [], 0),
        ("r3: argv rebound AFTER the spawn does not hide it (cmd = ['echo']) -> RED", {"l.py": PR3_LATE_ARGV}, [], 1),
        ("r3: env carrying ALLOC_RESERVATION then env = {} does not gate -> RED", {"o.py": PR3_ENV_OVERWRITE}, [], 1),
        ("r3: env = {'ALLOC_RESERVATION': rid} then env = {} does not gate -> RED", {"o.py": PR3_ENV_DICT_OVERWRITE},
         [], 1),
        ("r3: env = dict(env, ...) keeps the reservation -> GREEN", {"g.py": PR3_ENV_COPY_GATED}, [], 0),
        ("r3: `docker \\` + `run img` on the next line is RED", {"c.sh": PR3_CONTINUATION}, [], 1),
        ("r4: `ssh h job; ALLOC_RESERVATION=$(alloc-admit)` (gate after) is RED", {"o.sh": PR3_GATE_AFTER}, [], 1),
        ("r4: `ALLOC_RESERVATION=$(alloc-admit); ssh h job` (gate before) is GREEN", {"o.sh": PR3_GATE_BEFORE}, [], 0),
        ("r4: docker --host X run (argv) is RED", {"d.py": PR3_DOCKER_GLOBAL_PY}, [], 1),
        ("r4: docker --host X run (shell) is RED", {"d.sh": PR3_DOCKER_GLOBAL_SH}, [], 1),
        ("r4: docker --host X run, both allowlisted is GREEN", {"d.py": PR3_DOCKER_GLOBAL_PY, "d.sh": PR3_DOCKER_GLOBAL_SH},
         [row("d.py", "go", "docker-run"), row("d.sh", "start", "docker-run")], 0),
        ("#4330 r1: a selected key ({'PATH': env['PATH']}) carries no reservation -> RED",
         {"s.py": PR5_SELECTIVE_COPY}, [], 1),
        ("#4330 r1: {**env, ...} keeps the reservation -> GREEN", {"s.py": PR5_SPREAD_COPY_GATED}, [], 0),
        ("#4330 r1: script = script + '...' keeps the docker-run fact -> RED", {"c.py": PR5_SELF_CONCAT}, [], 1),
        ("#4330 r1: heredoc body ending in `\\` does not swallow EOF; ssh after it is RED",
         {"h.sh": PR5_HEREDOC_TAIL}, [], 1),
        ("#4330 r1: unquoted heredoc body folds `docker \\` + `run` (bash semantics): unallowlisted docker-run RED",
         {"u.sh": PR5_HEREDOC_UNQUOTED}, [row("u.sh", "<top>", "ssh")], 1),
        ("#4330 r2: env.copy() then .pop('ALLOC_RESERVATION') does not gate -> RED", {"p.py": PR6_POP}, [], 1),
        ("#4330 r2: `echo ready \\ ` (backslash-space) does not swallow the next ssh -> RED",
         {"b.sh": PR6_BS_SPACE}, [], 1),
        ("#4330 r2: loop-carried docker script reaches client.ssh -> RED", {"l.py": PR6_LOOP_DOCKER}, [], 1),
        ("#4330 r2: loop-carried argv (cmd rebound to ssh later in the body) -> RED", {"l.py": PR6_LOOP_ARGV}, [], 1),
        ("#4330 r2: X=$(ssh ... \\ host) counts once (count 1 is GREEN)", {"s.sh": PR6_SUBST_ONCE},
         [row("s.sh", "<top>", "ssh")], 0),
        ("#4330 r2: conditional rebinding keeps the docker fact -> RED", {"c.py": PR6_COND_REBIND}, [], 1),
        ("#4330 r2: reservation bound on only one branch does not gate -> RED", {"g.py": PR6_COND_GATE}, [], 1),
        ("#4330 r4: create_subprocess_exec('docker', *ARGS) resolves the starred tail -> RED", {"s.py": PR7_STAR_TAIL},
         [], 1),
        ("#4330 r4: create_subprocess_exec(*base, 'run', ..) resolves the starred head -> RED", {"s.py": PR7_STAR_HEAD},
         [], 1),
        ("#4330 r4: cmd = [ssh ..] if remote else [..] keeps the ssh arm -> RED", {"i.py": PR7_IFEXP}, [], 1),
        ("#4330 r4: inline IfExp argv keeps the ssh arm -> RED", {"i.py": PR7_IFEXP_INLINE}, [], 1),
        ("#4330 r4: env.update({'NOTE': 'ALLOC_RESERVATION'}) is a mention, not a key -> RED",
         {"u.py": PR7_UPDATE_NOTE}, [], 1),
        ("#4330 r4: env.update({'ALLOC_RESERVATION': rid}) sets the key -> GREEN", {"u.py": PR7_UPDATE_GATED}, [], 0),
        ("#4330 r4: inline env={'PATH': env['PATH']} carries no reservation -> RED", {"s.py": PR7_INLINE_SELECT}, [], 1),
        ("#4330 r4: `ls # c \\` does not fold the next ssh into the comment -> RED", {"c.sh": PR7_COMMENT_BS}, [], 1),
        ("#4330 r4: a quoted `#` does not stop `\\` folding (docker .. \\ + run) -> RED", {"q.sh": PR7_QUOTED_HASH_BS},
         [], 1),
        ("#4330 r4: identical ssh before and after the admit: only the first is ungated (count 1 GREEN)",
         {"r.sh": PR7_REPEAT_CMD}, [row("r.sh", "<top>", "ssh")], 0),
        ("#4330 r4: `docker ru\\` + `n img` joins with no space (bash) -> RED", {"t.sh": PR7_TOKEN_JOIN}, [], 1),
        ("#4330 r5: a spawn inside an assignment target is found -> RED", {"t.py": PR8_TARGET_CALL}, [], 1),
        ("#4330 r5: a spawn in a match-case guard is found -> RED", {"m.py": PR8_MATCH_GUARD}, [], 1),
        ("#4330 r5: argv through tuple unpacking (a, cmd = 1, cmd) is found -> RED", {"u.py": PR8_UNPACK}, [], 1),
        ("#4330 r5: `rid and env.update(ALLOC_RESERVATION=rid)` is MAY, does not gate -> RED",
         {"s.py": PR8_SHORT_CIRCUIT}, [], 1),
        ("#4330 r5: env.update(..) under a conditional expression is MAY, does not gate -> RED",
         {"i.py": PR8_IFEXP_MUT}, [], 1),
        ("#4330 r5: the spawn inside update(..)'s argument runs BEFORE the update -> RED", {"e.py": PR8_EVAL_ORDER}, [], 1),
        ("#4330 r5: a while condition re-runs with the body's argv -> RED", {"w.py": PR8_WHILE_TEST}, [], 1),
        ("#4330 r5: a spawn in a default argument runs at def time -> RED", {"d.py": PR8_DEFAULT_ARG}, [], 1),
        ("#4330 r5: a spawn in a decorator runs at def time -> RED", {"d.py": PR8_DECORATOR}, [], 1),
        ("#4330 r5: cmd.extend([...ssh...]) grows the bound argv -> RED", {"x.py": PR8_EXTEND}, [], 1),
        ("#4330 r5: a starred operand is not classified on its own (echo *[ssh..]) -> GREEN",
         {"s.py": PR8_STAR_ECHO}, [], 0),
        ("#4330 r5: two ssh on two continued lines are 2 sites, as main's per-line lint counts (count 1 is RED)",
         {"c.sh": PR8_SH_CONT_LINE}, [row("c.sh", "<top>", "ssh")], 1),
        ("#4330 r5: two ssh on two continued lines, count 2 is GREEN", {"c.sh": PR8_SH_CONT_LINE},
         [row("c.sh", "<top>", "ssh", 2)], 0),
        ("#4330 r6: $(ssh ..) BEFORE the ALLOC_RESERVATION= token runs pre-admit: count 1 for it is GREEN",
         {"b.sh": PR9_SUBST_BEFORE_ADMIT}, [row("b.sh", "<top>", "ssh")], 0),
        ("#4330 r6: $(ssh ..) BEFORE the admit is ungated (no row) -> RED", {"b.sh": PR9_SUBST_BEFORE_ADMIT}, [], 1),
        ("#4330 r6: $(ssh ..) INSIDE the admit's own argv is ungated -> RED", {"i.sh": PR9_SUBST_IN_ADMIT}, [], 1),
        ("#4330 r6: $(ssh ..) INSIDE the admit's argv, count 1 is GREEN (only it is ungated)",
         {"i.sh": PR9_SUBST_IN_ADMIT}, [row("i.sh", "<top>", "ssh")], 0),
        ("#4330 r6: $(ssh ..) AFTER the admit's value is gated -> GREEN", {"a.sh": PR9_SUBST_AFTER_ADMIT}, [], 0),
        ("#4330 r6: alias = env; del env[K]; run(env=alias) is ungated -> RED (302a431d5e5b)",
         {"a.py": PR9_ALIAS_DEL}, [], 1),
        ("#4330 r6: alias = env (no del) keeps the reservation -> GREEN", {"a.py": PR9_ALIAS_KEPT}, [], 0),
        ("#4330 r7: a spawn in an except-handler TYPE is found -> RED (c649f6e76616)", {"h.py": PR10_HANDLER_TYPE},
         [], 1),
        ("#4330 r7: del K; raise; K = rid: the handler sees no reservation -> RED (4f3ee8208e67)",
         {"h.py": PR10_HANDLER_MIDRAISE}, [], 1),
        ("#4330 r7: reservation set before the try reaches the handler -> GREEN", {"h.py": PR10_HANDLER_KEPT}, [], 0),
        ("#4330 r7: pop K; raise; K = rid: finally sees no reservation -> RED", {"f.py": PR10_FINALLY_MIDRAISE}, [], 1),
        ("t_0548c81c: a spawn inside a del target is found -> RED (2060cd33d39f)", {"d.py": PR11_DEL_TARGET}, [], 1),
        ("t_0548c81c: a spawn inside a for target is found -> RED", {"f.py": PR11_FOR_TARGET}, [], 1),
        ("t_0548c81c: a spawn inside a with-as target is found -> RED", {"w.py": PR11_WITH_TARGET}, [], 1),
        ("t_0548c81c: `for cmd in [cmd]` keeps the ssh argv -> RED (9660245e676f)", {"f.py": PR11_FOR_REUSE}, [], 1),
        ("t_0548c81c: tuple for target binds each element's argv -> RED", {"f.py": PR11_FOR_TUPLE}, [], 1),
        ("t_0548c81c: for over echo/true argvs is not a spawn -> GREEN", {"f.py": PR11_FOR_ECHO}, [], 0),
        ("t_0548c81c: a for env gated on one element only does not gate -> RED", {"g.py": PR11_FOR_GATED_ONE}, [], 1),
        ("t_c59fff64: ssh via an injected run=subprocess.run in a nested attempt() is RED",
         {"d.py": INJ_DEFAULT}, [], 1),
        ("t_c59fff64: the injected-default site is keyed to the nested scope (row for it is GREEN)",
         {"d.py": INJ_DEFAULT}, [row("d.py", "collect_remote.attempt", "ssh")], 0),
        ("t_c59fff64: plain subprocess.run ssh stays RED (unchanged)", {"go.py": UNGATED_PY_SSH}, [], 1),
        ("t_c59fff64: runner=subprocess.check_output default is RED", {"d.py": INJ_DEFAULT_DIRECT}, [], 1),
        ("t_c59fff64: kw-only spawn=subprocess.Popen docker run is RED", {"k.py": INJ_KWONLY_POPEN}, [], 1),
        ("t_c59fff64: run = functools.partial(subprocess.run, ..) is RED", {"p.py": INJ_PARTIAL}, [], 1),
        ("t_c59fff64: partial(subprocess.call, [ssh ..]) bakes the argv -> RED", {"p.py": INJ_PARTIAL_ARGV}, [], 1),
        ("t_c59fff64: local r = subprocess.run alias is RED", {"a.py": INJ_LOCAL_ALIAS}, [], 1),
        ("t_c59fff64: a `run` param with no default (test fake) is not a spawn -> GREEN",
         {"f.py": INJ_FAKE_NO_DEFAULT}, [], 0),
        ("t_c59fff64: a `run` param defaulting to a non-subprocess fake is not a spawn -> GREEN",
         {"f.py": INJ_FAKE_OTHER_DEFAULT}, [], 0),
        ("t_c59fff64: an inner `run` param shadows the outer injected runner -> GREEN", {"s.py": INJ_SHADOWED}, [], 0),
        ("t_c59fff64: an injected runner running echo is not a spawn -> GREEN", {"e.py": INJ_ECHO}, [], 0),
        ("t_f7b70a42: a runner bound inside an unused def does not leak to the module -> RED (95af992489bd)",
         {"r.py": RP_DEF_BODY_LEAK}, [], 1),
        ("t_f7b70a42: env['NOTE'] = <reservation-shaped string> does not reserve env -> RED (89766342164e)",
         {"r.py": RP_SUBSCRIPT_KEY}, [], 1),
        ("t_f7b70a42: rebinding the middle alias keeps env~b; env.pop reaches b -> RED (91c6c5c1db77)",
         {"r.py": RP_SURVIVING_ALIAS}, [], 1),
        ("t_e3dbf19f: `a, b = {}, {}` over chain env~a~b~c keeps env~c; env.pop reaches c -> RED (73953ef77e50)",
         {"r.py": RP_TUPLE_MID_REBIND}, [], 1),
        ("t_e3dbf19f: `for a, b in ...:` body: chain env~a~b~c keeps env~c; env.pop reaches c -> RED",
         {"r.py": RP_FOR_MID_REBIND}, [], 1),
        ("t_f7b70a42: one branch's runner rebinding does not cover the other branch -> RED (8fd7ab674141)",
         {"r.py": RP_BRANCH_RUNNER}, [], 1),
        ("t_f7b70a42: run bound to echo on one branch, subprocess.run on the other: the ssh call is RED",
         {"r.py": RP_BRANCH_RUNNER_FN}, [], 1),
        ("t_f7b70a42: e.pop on pass 1 reaches pass 2 through the backedge -> RED (55945507686d, 8246d4b3283e)",
         {"r.py": RP_LOOP_STALE}, [], 1),
        ("t_f7b70a42: the same loop without the pop stays gated -> GREEN", {"r.py": RP_LOOP_KEPT}, [], 0),
        ("t_f7b70a42: a 17th ungated element of a literal iterable fails closed -> RED (11d54303a061, 8a0c5638db5a)",
         {"r.py": RP_TRUNCATED}, [], 1),
        ("t_f7b70a42: a 17th ssh argv element of a literal iterable is still classified -> RED",
         {"r.py": RP_TRUNCATED_ARGV}, [], 1),
        ("t_f7b70a42: a local rebinding shadows an inherited runner alias -> GREEN (ce17a6dacbd1)",
         {"r.py": RP_SHADOWED_ALIAS}, [], 0),
        ("#6512 r1: `global runner` keeps the module binding; its partial ssh call is RED (087ccd93f08c)",
         {"r.py": RP_GLOBAL_RUNNER}, [], 1),
        ("#6512 r1: a docker-run row does not hide the ssh alternative -> RED (12a2f225f299)",
         {"r.py": RP_TWO_KINDS}, [row("r.py", "go", "docker-run")], 1),
        ("#6512 r1: rows for both kinds -> GREEN", {"r.py": RP_TWO_KINDS},
         [row("r.py", "go", "docker-run"), row("r.py", "go", "ssh")], 0),
        ("#6512 r1: a 9th runner alternative is never dropped -> RED (b68d2fca7e3b)", {"r.py": RP_NINE_ALTS}, [], 1),
        ("#6512 r2: a `global runner` bound in install() reaches the module call -> RED (f64f05e33ae3)",
         {"r.py": RP_GLOBAL_INSTALL}, [], 1),
        ("#6512 r2: `global run; if flag: run = fake` keeps the imported run -> RED (b100b2d9f80a)",
         {"r.py": RP_GLOBAL_IMPORT_KEPT}, [], 1),
        ("#6512 r2: a `nonlocal runner` bound in a nested def reaches the outer call -> RED",
         {"r.py": RP_NONLOCAL_INSTALL}, [], 1),
        ("desk r1: a `global runner` exported through a local alias in install() is RED (093e422a62c5)",
         {"r.py": RP_GLOBAL_EXPORT_VIA_LOCAL}, [], 1),
        ("desk r1: a `nonlocal runner` exported through a local alias is RED", {"r.py": RP_NONLOCAL_EXPORT_VIA_LOCAL},
         [], 1),
        ("desk r1: 16 ssh + 1 docker-run loop elements, docker-run row only -> RED (12a2f225f299)",
         {"r.py": RP_LOOP_TWO_KINDS}, [row("r.py", "go", "docker-run")], 1),
        ("desk r1: the same loop with rows for both kinds -> GREEN", {"r.py": RP_LOOP_TWO_KINDS},
         [row("r.py", "go", "docker-run"), row("r.py", "go", "ssh")], 0),
        ("desk r2: an inner `nonlocal runner` never shadows the outer runner in middle -> RED (de3afa4559bd)",
         {"r.py": RP_NONLOCAL_PAST_MIDDLE}, [], 1),
        ("t_4c2c8b0b: an exported `runner = lr` reads install()'s local lr (print), not the module's -> GREEN "
         "(b7d07f333edc)", {"r.py": RP_EXPORT_RHS_LOCAL}, [], 0),
        ("t_4c2c8b0b: same shape through `nonlocal runner` in a function -> GREEN",
         {"r.py": RP_EXPORT_RHS_LOCAL_NONLOCAL}, [], 0),
        ("t_4c2c8b0b: control: no exporter-local lr, the module's subprocess.run reaches runner -> RED",
         {"r.py": RP_EXPORT_RHS_MODULE}, [], 1),
        ("t_4c2c8b0b: five reverse-ordered `global` exporters still reach the module call -> RED (e694e43ce56f)",
         {"r.py": RP_EXPORT_CHAIN_REVERSED}, [], 1),
        ("desk r4: a class body `global runner` install reaches the module call -> RED (093e422a62c5)",
         {"r.py": RP_GLOBAL_CLASS_INSTALL}, [], 1),
        ("desk r4: a class body `nonlocal runner` install reaches the outer call -> RED",
         {"r.py": RP_NONLOCAL_CLASS_INSTALL}, [], 1),
        ("Prism 053b9f69ae12: a grandchild `nonlocal` echo partial keeps the outer runner in mid -> RED",
         {"r.py": RP_NONLOCAL_PARTIAL_DIRECT}, [], 1),
        ("t_540a3e9a: an ssh list after 70 distinct echo lists is classified -> RED (desk P3c)",
         {"r.py": RP_LOOP_PAST_CAP}, [], 1),
        ("t_540a3e9a: a docker-run tuple element past the cap is classified -> RED",
         {"r.py": RP_LOOP_PAST_CAP_TUPLE}, [], 1),
        ("t_540a3e9a: 71 echo lists stay a non-spawn -> GREEN", {"r.py": RP_LOOP_PAST_CAP_NONE}, [], 0),
        ("t_8ad18b12: a comprehension in the exporter (`[lr for _ in ..][0]`) reaches the module call -> RED",
         {"r.py": RP_EXPORT_COMP}, [], 1),
        ("t_8ad18b12: control: the comprehension's own `lr` (print) shadows outer()'s -> GREEN",
         {"r.py": RP_EXPORT_COMP_SHADOW}, [], 0),
        ("t_8ad18b12: a `nonlocal runner` exporter two defs deep binds outer()'s runner -> RED",
         {"r.py": RP_NONLOCAL_TWO_DEEP}, [], 1),
        ("t_8ad18b12: control: mid() binds its own runner, outer()'s stays print -> GREEN",
         {"r.py": RP_NONLOCAL_TWO_DEEP_MID}, [], 0),
        ("t_8ad18b12: a loop variable as the program name (`[cmd, 'ci-box', ..]`, ssh past the cap) -> RED",
         {"r.py": RP_LOOP_PROG_NAME}, [], 1),
        ("t_8ad18b12: control: 70 echo program names stay a non-spawn -> GREEN", {"r.py": RP_LOOP_PROG_NAME_NONE}, [], 0),
        ("t_8ad18b12: an argv taken by subscript from a dict element (`cmd['a']`, past the cap) -> RED",
         {"r.py": RP_LOOP_DICT_ARGV}, [], 1),
        ("t_8ad18b12: control: the ssh argv sits under another key -> GREEN", {"r.py": RP_LOOP_DICT_OTHER_KEY}, [], 0),
        ("Prism 6e4add456480: a comprehension target shadows a bare-imported run -> GREEN",
         {"r.py": RP_COMP_SHADOW_BARE}, [], 0),
        ("Apollo r7: the prog-name view is added to main's reading (a param named ssh_prog stays ssh) -> RED",
         {"r.py": RP_PROG_PARAM_SHADOW}, [], 1),
        ("Apollo r7: `cmd['a'] = [ssh ..]` binds the element -> RED", {"r.py": RP_DICT_ELEM_STORE}, [], 1),
        ("Apollo r7: an element store adds, never replaces (docker run kept: allowlist, not silence) -> RED",
         {"r.py": RP_DICT_ELEM_REBOUND}, [], 1),
        ("Prism 723aa276d5fc: ssh + 32 ['echo','run',i] lists is classified -> RED",
         {"r.py": RP_LOOP_SPAWNWORD_33}, [], 1),
        ("Prism 8ca6f3c667ce: ['docker'] + cmd over 32 ssh lists + ['run','img'] is classified -> RED",
         {"r.py": RP_LOOP_COMPOSED_DOCKER}, [], 1),
        ("#6549: a nested `global runner` bound to outer()'s local runner reaches the module call -> RED "
         "(5434b042f044)", {"r.py": RP_GLOBAL_EXPORT_NESTED}, [], 1),
        ("#6549: an exporter's `global lr` resolves to the module subprocess.run, not outer()'s local print -> RED "
         "(4433291449e8)", {"r.py": RP_GLOBAL_EXPORT_SHADOWED}, [], 1),
        ("t_f7b70a42: a local runner alias chain still resolves -> RED", {"r.py": RP_LOCAL_ALIAS_CHAIN}, [], 1),
        ("comments, echo, github.com ssh, ssh-keygen are not spawns", {"n.sh": NOT_SPAWNS, "n.py": PY_NOT}, [], 0),
        ("allowlisted (unexpired) site is GREEN", {"pool.sh": UNGATED_DOCKER}, [row("pool.sh", "start", "docker-run")], 0),
        ("allowlist row past expires is RED", {"pool.sh": UNGATED_DOCKER},
         [row("pool.sh", "start", "docker-run", exp="2026-10-14")], 1),
        ("a second spawn in an allowlisted function is RED (count ratchet)",
         {"pool.sh": UNGATED_DOCKER.replace("}\n", "  ssh ci-box 'docker run -d img2'\n}\n")},
         [row("pool.sh", "start", "docker-run")], 1),
        ("stale row (site now gated) is RED", {"pool.sh": GATED_DOCKER}, [row("pool.sh", "start", "docker-run")], 1),
    ]
    bad = 0
    for name, files, rows, want in cases:
        d = tree(files, rows)
        try:
            got_f = lint(d, d / ALLOWLIST, today)
            got = 1 if got_f else 0
        except (OSError, ValueError, KeyError) as e:
            got, got_f = 2, [str(e)]
        ok = got == want
        bad += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {name} (rc {got}, want {want})" + ("" if ok else f": {got_f}"))
    # --seed-allowlist from an empty tree refuses and leaves the existing inventory untouched
    d = tree({}, [row("pool.sh", "start", "docker-run")])
    before = (d / ALLOWLIST).read_text()
    rc = main(["--root", str(d), "--seed-allowlist"])
    ok = rc == 1 and (d / ALLOWLIST).read_text() == before
    print(f"{'ok  ' if ok else 'FAIL'} Prism r1: seed from an empty tree refuses, allowlist unchanged (rc {rc}, want 1)")
    bad += not ok
    # a read error during --seed-allowlist refuses (rc 2) and leaves the inventory untouched (#4330 r6, Prism
    # 33f6bb1fe4e4): one unreadable file must never silently delete its rows
    d = tree({"pool.sh": UNGATED_DOCKER, "x.sh": UNGATED_DOCKER}, [row("pool.sh", "start", "docker-run"),
                                                                  row("x.sh", "start", "docker-run")])
    before = (d / ALLOWLIST).read_text()
    (d / "x.sh").chmod(0)
    try:
        unreadable = not os.access(d / "x.sh", os.R_OK)
        rc = main(["--root", str(d), "--seed-allowlist"])
    finally:
        (d / "x.sh").chmod(0o644)
    ok = (rc == 2 and (d / ALLOWLIST).read_text() == before) if unreadable else True
    print(f"{'ok  ' if ok else 'FAIL'} #4330 r6: seed with an unreadable file refuses, allowlist unchanged "
          f"(rc {rc}, want 2)" + ("" if unreadable else " [skip: running as root, chmod 0 is still readable]"))
    bad += not ok
    # a quoted heredoc body is literal: its trailing `\\` is not folded into the terminator (#4330 r1 b0be01460a00)
    got = [(ln, t) for ln, t, _ in _sh_logical_lines(PR5_HEREDOC_TAIL)]
    ok = (4, "EOF") in got
    print(f"{'ok  ' if ok else 'FAIL'} #4330 r1: quoted heredoc body is not folded into its terminator: {got[2:4]}")
    bad += not ok
    # _heads past its 32-view cap keeps the spawn head whatever the set order (t_540a3e9a: PYTHONHASHSEED=2 lost
    # the ssh at element 63). A list in the worst order (spawn last) stands in for an unlucky hash seed.
    hst = _St()
    hst.argv["cmd"] = [("echo", str(i)) for i in range(40)] + [("ssh", "h", "j")]  # type: ignore  # a list: fixed worst order
    got = [h for h in _heads(ast.parse("cmd", mode="eval").body, hst) if _classify_argv(list(h))]
    ok = got == [["ssh", "h", "j"]]
    print(f"{'ok  ' if ok else 'FAIL'} t_540a3e9a: _heads past the 32-view cap keeps the ssh head in any order: {got}")
    bad += not ok
    # Prism 723aa276d5fc: a head _classify_argv accepts outranks heads that only carry a spawn word ('run')
    hst = _St()
    hst.argv["cmd"] = [("ssh", "h", "j")] + [("echo", "run", str(i)) for i in range(32)]  # type: ignore  # list order
    got = [h for h in _heads(ast.parse("cmd", mode="eval").body, hst) if _classify_argv(list(h))]
    ok = got == [["ssh", "h", "j"]]
    print(f"{'ok  ' if ok else 'FAIL'} Prism 723aa276d5fc: _heads cap keeps ssh over 32 echo-run heads: {got}")
    bad += not ok
    # Prism 8ca6f3c667ce: the cap ranks the COMPOSED head, so ('run','img') under ['docker'] + cmd (a spawn only once
    # composed) is not displaced by 32 ssh alternatives that compose to a non-spawning 'docker ssh ..'
    hst = _St()
    hst.argv["cmd"] = [("ssh", "h", str(i)) for i in range(32)] + [("run", "img")]  # type: ignore  # list order
    got = [h for h in _heads(ast.parse("['docker'] + cmd", mode="eval").body, hst) if _classify_argv(list(h))]
    ok = got == [["docker", "run", "img"]]
    print(f"{'ok  ' if ok else 'FAIL'} Prism 8ca6f3c667ce: _heads cap ranks the composed head (docker run kept): {got}")
    bad += not ok
    # Prism 753c64420f1d: two argv names. ('run','img') spawns only under a=('docker',), a LATER view than
    # views[0] (a=('echo',)); ranking b against views[0] alone sorted it last and the cap dropped it
    hst = _St()
    hst.argv["a"] = [("echo",), ("docker",)]  # type: ignore  # list order: echo is views[0]
    hst.argv["b"] = [("docker", str(i)) for i in range(16)] + [("run", "img")]  # type: ignore
    got = [h for h in _heads(ast.parse("a + b", mode="eval").body, hst) if _classify_argv(list(h))]
    ok = got == [["docker", "run", "img"]]
    print(f"{'ok  ' if ok else 'FAIL'} Prism 753c64420f1d: _heads cap ranks composed views across names: {got}")
    bad += not ok
    # Prism 7f9bc8fca3a7: `base + [cfg['a']]` does not read cfg['a'] into the head, so its 32 alternatives compose
    # to ONE docker head per base view; 32 identical docker views filled the cap and dropped every ssh head
    hst = _St()
    hst.argv["base"] = [("docker", "run", "img"), ("ssh", "h", "j")]  # type: ignore
    hst.argv["cfg['a']"] = [("x", str(i)) for i in range(32)]  # type: ignore
    got = sorted(h[0] for h in _heads(ast.parse("base + [cfg['a']]", mode="eval").body, hst) if _classify_argv(list(h)))
    ok = got == ["docker", "ssh"]
    print(f"{'ok  ' if ok else 'FAIL'} Prism 7f9bc8fca3a7: _heads dedups composed heads before the 32 cap: {got}")
    bad += not ok
    # Prism 6772d0283d54: every view's plain head is ['SSH', ..] (the *ssh* name), which out-ranked the program-name
    # head, so dedup by the winning head collapsed 32 echo views + 1 systemd-run view into one echo view
    hst = _St()
    hst.argv["ssh_prog"] = [(f"echo{i}",) for i in range(32)] + [("systemd-run",)]  # type: ignore
    got = sorted({k for h in _heads(ast.parse("[ssh_prog, 'ci-box', 'job']", mode="eval").body, hst)
                  for k in [_classify_argv(list(h))] if k})
    ok = got == ["ssh", "systemd-run"]
    print(f"{'ok  ' if ok else 'FAIL'} Prism 6772d0283d54: _heads cap keeps the program-name view's kind: {got}")
    bad += not ok
    # unreadable allowlist is blind (rc 2), never clean
    d = tree({"pool.sh": GATED_DOCKER}, None)
    (d / "fleet").mkdir()
    (d / ALLOWLIST).write_text("{not json")
    rc = main(["--root", str(d), "--today", "2026-10-15"])
    print(f"{'ok  ' if rc == 2 else 'FAIL'} unreadable allowlist is blind (rc {rc}, want 2)")
    bad += rc != 2
    # differential arm: discovery is a superset of the base lint's over the real tree AND over a corpus of every
    # selftest fixture file (SPAWN_LINT_BASE, default origin/main): a shape class absent from the tree is still
    # measured (#4330 r7 c649f6e76616). Skipped, and said so, when there is no base ref or the lint is unchanged.
    corpus = Path(tempfile.mkdtemp(prefix="spawn-site-corpus-"))
    for i, (_, files, _, _) in enumerate(cases):
        for n, b in files.items():
            if b in DIFF_FP_FIXED:
                continue   # a base false positive this lint removes on purpose: not a missing site
            (corpus / f"c{i:03d}" / n).parent.mkdir(parents=True, exist_ok=True)
            (corpus / f"c{i:03d}" / n).write_text(b)
    ref = os.environ.get("SPAWN_LINT_BASE", "origin/main")
    status, missing = differential([DEFAULT_ROOT, corpus], ref)
    ok = not missing
    print(f"{'ok  ' if ok else 'FAIL'} differential vs {ref}: {status}, {len(missing)} base-lint sites missing"
          + ("" if ok else f": {missing[:10]}"))
    bad += not ok
    print(f"selftest: {len(cases) + 7 - bad}/{len(cases) + 7} pass")
    return 1 if bad else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    ap.add_argument("--allowlist", type=Path)
    ap.add_argument("--today")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--seed-allowlist", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    root = a.root.resolve()
    allow = a.allowlist or root / ALLOWLIST
    if a.seed_allowlist:
        return seed(root, allow)
    if a.list:
        read, sites = collect_sites(root)
        for p, s, k, ln, g in sites:
            print(f"{'gated  ' if g else 'UNGATED'} {k:15} {p}:{ln} {s}")
        print(f"# {read} files read, {len(sites)} spawn sites, {sum(not x[4] for x in sites)} ungated")
        return 0
    today = dt.date.fromisoformat(a.today) if a.today else dt.date.today()
    if not root.is_dir():
        print(f"lint-spawn-site: scan root {root} is not a directory: a lint that scans nothing is red (S6)")
        return 1
    try:
        findings = lint(root, allow, today)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as e:
        print(f"lint-spawn-site: blind: {e}", file=sys.stderr)
        return 2
    for f in findings:
        print(f)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
