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
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import warnings
from collections import Counter, defaultdict
from pathlib import Path

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
NAMED_SPAWNS = frozenset(("_default_spawn", "start_arm", "open_session"))
NON_FLEET = re.compile(r"^(?:[\w.-]+@)?(?:github\.com|gitlab\.com|localhost|127\.0\.0\.1|::1)$")

SUBP_ATTR = frozenset(("run", "Popen", "call", "check_call", "check_output"))
SUBP_MOD = frozenset(("subprocess", "sp", "_sp", "_subprocess"))
OS_ATTR = frozenset(("system", "popen", "execvp", "execv", "execvpe", "spawnvp", "posix_spawnp", "posix_spawn"))
ASYNC_ATTR = frozenset(("create_subprocess_exec", "create_subprocess_shell"))

# shell-text kinds, ordered; the ssh rule is the generic one, so it is checked LAST
SHELL_KINDS = (
    ("docker-run", re.compile(r"\bdocker\s+(?:container\s+)?run\b")),
    ("systemd-run", re.compile(r"(?:^|[\s;|&(`\"'])systemd-run(?:\s|$)")),
    ("kickstart", re.compile(r"\blaunchctl\s+kickstart\b")),
    ("workflow-run", re.compile(r"\bgh\s+workflow\s+run\b")),
    ("runner-mint", re.compile(r"generate-jitconfig|actions/runners/registration-token")),
    ("kanban-dispatch", re.compile(r"\b(?:hermes|hermes)\s+kanban\s+dispatch\b")),
)
SHELL_SSH = re.compile(r"(?:^|[\s;|&(`\"'])(?:sudo\s+)?(?:/usr/bin/)?ssh((?:\s+-\S+(?:\s+[^\s-]\S*)?)*)\s+([^\s;|&)]+)")
SH_NOISE = re.compile(r"^\s*(?:echo|printf|say|log|_log|warn|info|die|err|usage|note|fail)\b")
SH_FUNC = re.compile(r"^(\s*)(?:function\s+)?([A-Za-z_][\w:.-]*)\s*\(\)\s*\{?\s*(?:#.*)?$|^(\s*)function\s+([A-Za-z_][\w:.-]*)\s*\{\s*$")
SH_GATE = re.compile(r"ALLOC_RESERVATION=.*\balloc-admit\b")


# ---------------------------------------------------------------- file discovery
def scan_files(root: Path) -> list[str]:
    try:
        p = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, timeout=120)
        tracked = [f for f in p.stdout.decode("utf-8", "replace").split("\0") if f] if p.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        tracked = None
    if tracked is None:   # not a git tree (fixtures): walk it
        tracked = [str(Path(dp, n).relative_to(root)) for dp, _, ns in os.walk(root) for n in ns]
    out = []
    for f in sorted(tracked):
        if EXCLUDE.search(f):
            continue
        if f.endswith(SCAN_EXT):
            out.append(f)
        elif "." not in f.rsplit("/", 1)[-1]:
            try:
                with open(root / f, "rb") as fh:
                    head = fh.readline(200)
            except OSError:
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


def _is_subprocess_call(call: ast.Call) -> bool:
    f = call.func
    if isinstance(f, ast.Attribute):
        r = _recv(f)
        if f.attr in SUBP_ATTR and (r in SUBP_MOD or f.attr == "Popen"):
            return True
        if f.attr in OS_ATTR and r == "os":
            return True
        if f.attr in ASYNC_ATTR:
            return True
    if isinstance(f, ast.Name) and f.id in ("Popen", "check_output", "check_call", "create_subprocess_exec"):
        return True
    return False


def _argv_head(node) -> list:
    """The leading argv tokens: str for a literal, 'SSH' for an ssh helper, None for unknown."""
    if isinstance(node, (ast.List, ast.Tuple)):
        out = []
        for e in node.elts:
            t = _literal_text(e)
            if t is not None and "\x00" not in t:
                out.append(t)
            elif isinstance(e, ast.Starred) and "ssh" in _name(e.value).lower():
                out.append("SSH")
            elif "ssh" in _name(e).lower() and not isinstance(e, ast.Constant):
                out.append("SSH")
            else:
                out.append(None)
            if len(out) >= 4:
                break
        return out
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _argv_head(node.left)
        return left + _argv_head(node.right) if len(left) < 4 else left
    if isinstance(node, (ast.Call, ast.Name, ast.Attribute)) and "ssh" in _name(node).lower():
        return ["SSH"]
    t = _literal_text(node)
    if t is not None:
        return [t]   # a shell string: classified as text
    return []


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
    if base == "docker" and len(head) > 1 and (head[1] == "run" or head[1:3] == ["container", "run"]):
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
    for kind, rx in SHELL_KINDS:
        if rx.search(text):
            return kind
    m = SHELL_SSH.search(text)
    if m and not NON_FLEET.match(m.group(2).strip("\"'")):
        return "ssh"
    return None


class _PyScope:
    def __init__(self, name: str, node):
        self.name, self.node = name, node
        self.admit_names: set[str] = set()
        self.env_names: set[str] = set()
        self.docker_names: set[str] = set()   # names bound to a 'docker run' script
        self.mint_names: set[str] = set()


def _scope_facts(sc: _PyScope) -> None:
    for n in _walk_own(sc.node):
        if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None:
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            names = {t.id for tg in targets for t in ast.walk(tg) if isinstance(t, ast.Name)}
            v = n.value
            if isinstance(v, ast.Call) and _name(v.func) in ("admit", "alloc_admit") and (
                    isinstance(v.func, ast.Name) or re.search(r"alloc|placement|^pl$", _recv(v.func))):
                sc.admit_names |= names
            txt = _all_text(v)
            if "ALLOC_RESERVATION" in txt or any(isinstance(k, ast.keyword) and k.arg == "ALLOC_RESERVATION"
                                                  for k in ast.walk(v)):
                sc.env_names |= names
            if re.search(r"\bdocker\s+(?:container\s+)?run\b", txt):
                sc.docker_names |= names
            if re.search(r"generate-jitconfig|runners/registration-token", txt):
                sc.mint_names |= names
            for tg in targets:   # env["ALLOC_RESERVATION"] = rid
                if isinstance(tg, ast.Subscript) and "ALLOC_RESERVATION" in _all_text(tg.slice):
                    sc.env_names |= {t.id for t in ast.walk(tg.value) if isinstance(t, ast.Name)}


def _walk_own(node):
    """ast.walk that does not descend into nested function/class definitions (they are their own scopes)."""
    stack = list(ast.iter_child_nodes(node))
    while stack:
        n = stack.pop()
        yield n
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(n))


def _gated(call: ast.Call, sc: _PyScope) -> bool:
    names = {n.id for n in ast.walk(call) if isinstance(n, ast.Name)}
    if names & (sc.admit_names | sc.env_names):
        return True
    if any(k.arg in ("reservation_id", "ALLOC_RESERVATION") for k in call.keywords):
        return True
    return "ALLOC_RESERVATION" in _all_text(call)


def py_sites(path: str, text: str) -> list[tuple[str, str, int, bool]]:
    """[(scope, kind, line, gated)]"""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            return [("<module>", "unparseable", 1, False)]
    scopes = [_PyScope("<module>", tree)]

    def collect(node, prefix):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q = f"{prefix}.{ch.name}" if prefix else ch.name
                if not isinstance(ch, ast.ClassDef):
                    scopes.append(_PyScope(q, ch))
                collect(ch, q)
            else:
                collect(ch, prefix)
    collect(tree, "")
    out = []
    for sc in scopes:
        _scope_facts(sc)
        seen_mint = False
        for n in _walk_own(sc.node):
            if not isinstance(n, ast.Call):
                continue
            kind = None
            if _is_subprocess_call(n) and n.args:
                kind = _classify_argv(_argv_head(n.args[0]))
                if kind is None:
                    names = {x.id for x in ast.walk(n.args[0]) if isinstance(x, ast.Name)}
                    if names & sc.mint_names:
                        kind = "runner-mint"
            elif "ssh" in _name(n.func).lower() and not isinstance(n.func, ast.Attribute) or \
                    (isinstance(n.func, ast.Attribute) and "ssh" in n.func.attr.lower()):
                # a call to an ssh helper: a spawn only when it ships a `docker run` script (the pool's start)
                names = {x.id for a in n.args for x in ast.walk(a) if isinstance(x, ast.Name)}
                if names & sc.docker_names or any(re.search(r"\bdocker\s+(?:container\s+)?run\b", _all_text(a))
                                                  for a in n.args):
                    kind = "docker-run"
            elif _name(n.func) in NAMED_SPAWNS:
                kind = "named"
            elif not seen_mint and _name(n.func) in ("run", "check_output", "gh", "_gh", "gh_api", "api") and (
                    re.search(r"generate-jitconfig|runners/registration-token", _all_text(n))
                    or {x.id for x in ast.walk(n) if isinstance(x, ast.Name)} & sc.mint_names):
                kind, seen_mint = "runner-mint", True
            if kind:
                out.append((sc.name, kind, n.lineno, _gated(n, sc)))
    return out


# ---------------------------------------------------------------- shell
def sh_sites(text: str) -> list[tuple[str, str, int, bool]]:
    out = []
    stack: list[tuple[str, str]] = []           # (function name, indent)
    gated_in: dict[str, bool] = defaultdict(bool)
    heredoc = None
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if heredoc:
            if line.strip() == heredoc:
                heredoc = None
            # a heredoc body is still executed when fed to bash/ssh; classify it like any line below
        m = SH_FUNC.match(line)
        if m:
            stack.append((m.group(2) or m.group(4), m.group(1) or m.group(3) or ""))
            continue
        if stack and re.match(r"^" + re.escape(stack[-1][1]) + r"\}\s*(?:#.*)?$", line):
            stack.pop()
            continue
        scope = stack[-1][0] if stack else "<top>"
        s = line.lstrip()
        if not s or s.startswith("#") or SH_NOISE.match(s):
            continue
        hd = re.search(r"<<-?\s*['\"]?(\w+)['\"]?", s)
        if hd and not heredoc:
            heredoc = hd.group(1)
        if SH_GATE.search(s):
            gated_in[scope] = True
        kind = _classify_shell_text(s)
        if kind:
            out.append((scope, kind, i, gated_in[scope] or "ALLOC_RESERVATION=" in s and "alloc-admit" in s))
    return out


# ---------------------------------------------------------------- lint
def collect_sites(root: Path):
    files = scan_files(root)
    read = 0
    sites = []   # (path, scope, kind, line, gated)
    for f in files:
        try:
            text = (root / f).read_text(encoding="utf-8", errors="replace")
        except OSError:
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
    findings = []
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
    _, sites = collect_sites(root)
    old = {}
    if allowlist.exists():
        old = {(r["path"], r["scope"], r["kind"]): r for r in json.loads(allowlist.read_text())["sites"]}
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
    allowlist.write_text(json.dumps(doc, indent=1) + "\n")
    print(f"seeded {len(rows)} rows ({sum(c.values())} spawns) -> {allowlist}")
    return 0


# ---------------------------------------------------------------- selftest
UNGATED_DOCKER = "#!/bin/bash\nstart() {\n  ssh ci-box 'docker run -d --rm runner:latest'\n}\n"
GATED_DOCKER = ("#!/bin/bash\nstart() {\n  ALLOC_RESERVATION=$(alloc-admit --consumer ci --job \"$1\")\n"
                "  ssh ci-box \"ALLOC_RESERVATION=$ALLOC_RESERVATION docker run -d --rm runner:latest\"\n}\n")
UNGATED_PY_SSH = "import subprocess\n\ndef go(h):\n    subprocess.run(['ssh', '-o', 'BatchMode=yes', h, 'make test'])\n"
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
NOT_SPAWNS = ("#!/bin/bash\n# ssh ci-box 'docker run x'\necho \"run: ssh ace-ai uptime\"\n"
              "git push git@github.com:o/r.git\nssh -T git@github.com\nssh-keygen -t ed25519\n")
RUNNER_MINT = ("class Gh:\n    def jit(self, name):\n"
               "        args = ['-X', 'POST', f'orgs/{self.org}/actions/runners/generate-jitconfig', '-f', name]\n"
               "        return self._gh(args)\n")
PY_NOT = "import subprocess\nsubprocess.run(['ssh-keygen', '-t', 'ed25519'])\nsubprocess.run(['git', 'push'])\n"


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
        ("admit in ANOTHER function does not gate (same-scope rule) -> RED", {"a.py": ADMIT_ELSEWHERE}, [], 1),
        ("bash ALLOC_RESERVATION from alloc-admit in scope is GREEN", {"pool.sh": GATED_DOCKER}, [], 0),
        ("python alloc_client.admit -> env ALLOC_RESERVATION is GREEN", {"go.py": GATED_PY}, [], 0),
        ("comments, echo, github.com ssh, ssh-keygen are not spawns", {"n.sh": NOT_SPAWNS, "n.py": PY_NOT}, [], 0),
        ("allowlisted (unexpired) site is GREEN", {"pool.sh": UNGATED_DOCKER}, [row("pool.sh", "start", "docker-run")], 0),
        ("allowlist row past expires is RED", {"pool.sh": UNGATED_DOCKER},
         [row("pool.sh", "start", "docker-run", exp="2026-10-14")], 1),
        ("a second spawn in an allowlisted function is RED (count ratchet)",
         {"pool.sh": UNGATED_DOCKER.replace("}\n", "  ssh ace-ai 'docker run -d img2'\n}\n")},
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
    # unreadable allowlist is blind (rc 2), never clean
    d = tree({"pool.sh": GATED_DOCKER}, None)
    (d / "fleet").mkdir()
    (d / ALLOWLIST).write_text("{not json")
    rc = main(["--root", str(d), "--today", "2026-10-15"])
    print(f"{'ok  ' if rc == 2 else 'FAIL'} unreadable allowlist is blind (rc {rc}, want 2)")
    bad += rc != 2
    print(f"selftest: {len(cases) + 1 - bad}/{len(cases) + 1} pass")
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
