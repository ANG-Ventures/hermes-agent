#!/usr/bin/env python3
# SCHEDULE: manual (CI lint: hermes-home lints-fast + the spawn-site CI job in fleet-ops-scripts / prism-router / hermes-agent fork; no cron row)
"""p0-label-lint: main / merge-queue CI on the fleet pool requests the `p0` label pool, PR CI never does (Router Core spec v5 §4.1, card t_8b05b16b).

WHY: the router admits CI per runner SLOT (class P0 for the p0 pool, P2 for the ordinary pool), but a slot cannot know
which job GitHub will hand it. The class reaches the job through a label: p0 runners register with
`--no-default-labels --labels p0` (label set exactly {p0}), main/merge-queue jobs request `runs-on: p0`, PR jobs do not.
A PR job asking for p0 eats the reserve main CI exists for; a main job not asking for it waits behind batch PRs.
Ace ruling 2026-10-15 01:15: p0 pool 2/1/1/1 = ci-box 2, ace-media 1, ace-ai 1, mac-studio 1. The Studio's runner is
macOS ARM and takes only jobs whose matrix includes macos/arm; it carries STUDIO_LABEL (not bare `p0`, or a Linux p0
job could land on it). The three Linux runners share `p0`.

WORKFLOWS (.github/workflows/*.yml). Each job's runs-on (or a reusable caller's `with: runner`, or a local callee's
jobs) is EVALUATED per event: pull_request(_target), push to main, merge_group. github.event_name / github.ref are
known; vars / inputs / needs are opaque; `${{ matrix.X }}` enumerates the job's static matrix. The VENUE of a job is
named by its literal outcomes (the house `vars.CI_RUNNER || ... || 'hermes-ci'` fallback names the pool;
`vars.CI_LIGHT_RUNNER || 'ubuntu-latest'` names a hosted runner); a job with ONLY opaque outcomes is the pool (fail
closed). Hosted labels (ubuntu-*, macos-*, windows-*, blacksmith-*) are not the pool and are exempt.
  R1 pr-requests-p0      a PR-event outcome contains a p0 label. RED, never allowlistable.
  R2 main-not-p0         a push:main / merge_group outcome is the pool without a p0 label. RED unless allowlisted.
  R3 studio-not-mac-arm  a job requesting STUDIO_LABEL whose matrix/labels do not include macos + arm.
  R4 p0-on-macos         a job requesting bare `p0` together with a macOS label (the Linux p0 runners cannot run it).
  R6 p0-unsatisfiable    a job requesting a p0 label plus any other label ([self-hosted, p0]): the p0 runners carry
                         exactly that one label, so no runner can take it and main CI queues forever.
REGISTRATION (fleet-ops-scripts ci-runners/host-config/<host>/*.conf, only where such files exist): a pool conf whose
LABELS carries a p0 label must set NO_DEFAULT_LABELS=1 (-> --no-default-labels), LABELS exactly `p0` (or exactly
STUDIO_LABEL on mac-studio), and POOL_SIZE = the ruling's count for that host (R5).

ALLOWLIST fleet/p0-label-allowlist.json {"jobs": [{path, job, reason, expires}]}: R2 only; the seeded rows are the
Card B migration inventory. Red when a row is expired, duplicated, or stale (the job is gone or now passes R2), so the
list only shrinks. A NEW main pool job without p0 is red.
S6: zero workflow files read is RED (rc 1), never clean. Exit 0 clean (silent), 1 findings, 2 blind (bad allowlist).
  --root DIR  --allowlist F  --today YYYY-MM-DD  --list  --seed-allowlist (one-time)  --selftest
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import itertools
import json
import re
import shlex
import sys
import tempfile
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    print("p0-label-lint: pyyaml missing", file=sys.stderr)
    sys.exit(2)

ALLOWLIST = "fleet/p0-label-allowlist.json"
SEED_EXPIRES = "2027-01-15"
SEED_REASON = ("grandfathered when p0-label-lint landed (t_8b05b16b); Card B migration inventory: request runs-on p0 "
               "for push:main / merge_group (PR events keep the ordinary pool), then delete this row")
P0 = "p0"
STUDIO_LABEL = "p0-macos-arm64"
P0_LABELS = (P0, STUDIO_LABEL)
POOL_SIZES = {"ci-box": 2, "ace-media": 1, "ace-ai": 1, "mac-studio": 1}   # Ace ruling 2026-10-15 01:15
HOSTED = re.compile(r"^(ubuntu|macos|windows|blacksmith)-[\w.-]+$", re.I)
EVENTS = {
    # PR refs / branch names vary per PR: opaque (only the event name is a fact), so both outcomes are checked.
    "pr": {"github.event_name": "pull_request"},
    "pr_target": {"github.event_name": "pull_request_target"},
    "push": {"github.event_name": "push", "github.ref": "refs/heads/main", "github.ref_name": "main",
             "github.base_ref": "", "github.head_ref": ""},
    "merge_group": {"github.event_name": "merge_group", "github.base_ref": "", "github.head_ref": ""},
}
PR_EVENTS = ("pr", "pr_target")
OPAQUE_LABEL = "<opaque>"   # an unknown component of a runs-on array; the known labels beside it are kept
EXPR = re.compile(r"\$\{\{(.*?)\}\}", re.S)
TOKEN = re.compile(r"\s*(&&|\|\||==|!=|!|\(|\)|,|'(?:[^']|'')*'|-?\d+(?:\.\d+)?|[A-Za-z_][\w.\-*\[\]']*)")
OPAQUE = ("opaque",)
OPAQUE_T = ("opaque", "truthy")   # an opaque value kept by `a || b` only when truthy (else b replaced it)


# ---------------------------------------------------------------- expression evaluation
def _gh_truthy(v) -> bool:
    """GitHub Actions truthiness: false, 0, -0, '', null and NaN are falsy; everything else (incl. [] and {})
    is truthy."""
    if v is None or v is False or v == "":
        return False
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return not (v == 0 or v != v)
    return True


def _eval(expr: str, known: dict, matrix_vals: dict) -> list:
    """Every possible value of one ${{ }} body: str / list / bool / None literals, or OPAQUE."""
    toks = TOKEN.findall(expr)
    if "".join(toks).replace(" ", "") != re.sub(r"\s", "", expr):
        return [OPAQUE]
    pos = [0]

    def peek():
        return toks[pos[0]] if pos[0] < len(toks) else None

    def take(t=None):
        v = peek()
        if v is None or (t is not None and v != t):
            raise ValueError
        pos[0] += 1
        return v

    def atom():
        t = take()
        if t == "(":
            v = orx()
            take(")")
            return v
        if t == "!":
            return [OPAQUE if isinstance(v, tuple) else (not _gh_truthy(v)) for v in atom()]
        if t.startswith("'"):
            return [t[1:-1].replace("''", "'")]
        if t in ("true", "false"):
            return [t == "true"]
        if t == "null":
            return [None]
        if re.fullmatch(r"-?\d+(?:\.\d+)?", t):
            return [float(t)]
        if peek() == "(":   # function call
            take("(")
            args = []
            if peek() != ")":
                args.append(orx())
                while peek() == ",":
                    take(",")
                    args.append(orx())
            take(")")
            if t == "fromJSON" and len(args) == 1:
                out = []
                for v in args[0]:
                    try:
                        out.append(json.loads(v) if isinstance(v, str) else OPAQUE)
                    except ValueError:
                        out.append(OPAQUE)
                return out
            if t in ("contains", "startsWith", "endsWith") and len(args) == 2:
                out = []
                for a, b in itertools.product(args[0], args[1]):
                    if isinstance(a, tuple) or isinstance(b, tuple) or not isinstance(b, str):
                        out += [True, False]
                    elif t == "contains":
                        if isinstance(a, str):
                            out.append(b.lower() in a.lower())
                        elif isinstance(a, list):
                            if any(isinstance(x, tuple) for x in a):
                                out += [True, False]
                            else:
                                out.append(any(isinstance(x, str) and x.lower() == b.lower() for x in a))
                        else:
                            out.append(False)
                    else:
                        out.append(isinstance(a, str) and getattr(a.lower(), t.lower())(b.lower()))
                return out
            return [OPAQUE]
        if t in known:
            return [known[t]]
        if t.startswith("matrix.") and t in matrix_vals:
            return list(matrix_vals[t]) or [OPAQUE]
        return [OPAQUE]

    def cmp():
        a = atom()
        if peek() in ("==", "!="):
            op = take()
            b = atom()
            out = []
            for x, y in itertools.product(a, b):
                if isinstance(x, tuple) or isinstance(y, tuple):
                    out += [True, False]
                    continue
                eq = (x.lower() == y.lower()) if isinstance(x, str) and isinstance(y, str) else x == y
                out.append(eq if op == "==" else not eq)
            return out
        return a

    def truthy(v):
        if v is OPAQUE_T:
            return True
        return None if isinstance(v, tuple) else _gh_truthy(v)

    def combine(vs, ws, keep_left_when):
        out = []
        for v in vs:
            t = truthy(v)
            if t is None or t == keep_left_when:
                # `opaque || b` keeps the opaque value only when it is truthy
                out.append(OPAQUE_T if (t is None and keep_left_when) else v)
            if t is None or t != keep_left_when:
                out.extend(ws)
        return out

    def andx():
        v = cmp()
        while peek() == "&&":
            take()
            v = combine(v, cmp(), keep_left_when=False)
        return v

    def orx():
        v = andx()
        while peek() == "||":
            take()
            v = combine(v, andx(), keep_left_when=True)
        return v

    try:
        v = orx()
        if pos[0] != len(toks):
            return [OPAQUE]
        return v
    except (ValueError, IndexError):
        return [OPAQUE]


def _labels(v) -> tuple | None:
    """A runs-on value -> its label tuple; None = opaque."""
    if isinstance(v, tuple):
        return None
    if isinstance(v, str):
        return (v,) if v else None
    if isinstance(v, list) and all(isinstance(x, str) for x in v):
        return tuple(v)
    if isinstance(v, dict):   # runs-on: {group:, labels:}
        lab = v.get("labels")
        return _labels(lab if lab is not None else OPAQUE)
    return None


def outcomes(runs_on, known: dict, matrix_vals: dict) -> list:
    """Possible label tuples (None = opaque) of one runs-on value for one event."""
    if isinstance(runs_on, list):
        parts = [outcomes(x, known, matrix_vals) for x in runs_on]
        out = []
        for combo in itertools.product(*parts):
            if all(c is None for c in combo):
                out.append(None)
            else:   # an opaque element never hides the explicit labels beside it (a known p0 stays visible)
                out.append(tuple(lab for c in combo for lab in (c if c is not None else (OPAQUE_LABEL,))))
        return out
    if isinstance(runs_on, dict):
        return outcomes(runs_on.get("labels", "${{ opaque }}"), known, matrix_vals)
    if not isinstance(runs_on, str):
        return [None]
    s = runs_on.strip()
    m = EXPR.fullmatch(s)
    if m:
        return [_labels(v) for v in _eval(m.group(1).strip(), known, matrix_vals)]
    if EXPR.search(s):
        return [None]   # a label with an embedded expression: opaque
    return [(s,)]


def _matrix_vals(job: dict) -> dict:
    """matrix.<key>[.<sub>] -> the set of static values, from strategy.matrix (values + include)."""
    mx = ((job.get("strategy") or {}).get("matrix")) if isinstance(job.get("strategy"), dict) else None
    if not isinstance(mx, dict):
        return {}
    vals: dict = {}

    def add(prefix, v):
        if isinstance(v, dict):
            for k, x in v.items():
                add(f"{prefix}.{k}", x)
        vals.setdefault(prefix, [])
        if v not in vals[prefix]:
            vals[prefix].append(v)

    for k, v in mx.items():
        if k == "exclude":
            continue
        if k == "include" and isinstance(v, list):
            for entry in v:
                if isinstance(entry, dict):
                    for ik, iv in entry.items():
                        add(f"matrix.{ik}", iv)
            continue
        if isinstance(v, list):
            for x in v:
                add(f"matrix.{k}", x)
        else:   # an expression (fromJSON(needs...)) -> no static values
            vals.setdefault(f"matrix.{k}", [])
    return vals


def _matrix_text(job: dict) -> str:
    return json.dumps((job.get("strategy") or {}).get("matrix") if isinstance(job.get("strategy"), dict) else None)


# ---------------------------------------------------------------- triggers
def _triggers(doc: dict) -> dict:
    on = doc.get(True, doc.get("on"))
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        return {k: None for k in on}
    return on if isinstance(on, dict) else {}


def _branch_match(pats, branch: str) -> bool:
    ok = False
    for p in pats:
        p = str(p)
        neg = p.startswith("!")
        if fnmatch.fnmatchcase(branch, p[1:] if neg else p):
            ok = not neg
    return ok


def events_of(doc: dict) -> list[str]:
    trig = _triggers(doc)
    ev = []
    if "pull_request" in trig:
        ev.append("pr")
    if "pull_request_target" in trig:
        ev.append("pr_target")
    if "push" in trig:
        cfg = trig["push"] if isinstance(trig["push"], dict) else {}
        if "branches" in cfg:
            if _branch_match(cfg["branches"] or [], "main"):
                ev.append("push")
        elif "branches-ignore" in cfg:
            if not any(fnmatch.fnmatchcase("main", str(p)) for p in cfg["branches-ignore"] or []):
                ev.append("push")
        elif "tags" not in cfg and "tags-ignore" not in cfg:
            ev.append("push")
    if "merge_group" in trig:
        ev.append("merge_group")
    return ev


# ---------------------------------------------------------------- workflow scan
def _is_p0(t: tuple) -> bool:
    """GitHub matches runner labels case-insensitively."""
    return any(str(lab).lower() in P0_LABELS for lab in t)


def _venue(outs: list) -> str:
    """p0 | hosted | pool, from an event's outcomes. Literal outcomes name the venue; only-opaque = pool."""
    lits = [o for o in outs if o is not None]
    if not lits:
        return "pool"
    if all(_is_p0(o) for o in lits):
        return "p0"
    if all(o and all(HOSTED.match(lab) for lab in o) for o in lits if not _is_p0(o)) and not any(_is_p0(o) for o in lits):
        return "hosted"
    return "pool"


def _job_runs_on(job: dict):
    if "runs-on" in job:
        return job["runs-on"]
    w = job.get("with") if isinstance(job.get("with"), dict) else {}
    for k in ("runner", "runs-on", "runs_on"):
        if k in w:
            return w[k]
    return None


def _never_runs(job: dict, known: dict) -> bool:
    """True only when the job-level `if` is false for every outcome under this event (opaque -> it may run)."""
    cond = job.get("if")
    if cond is None:
        return False
    if isinstance(cond, bool):
        return not cond
    if not isinstance(cond, str):
        return False
    s = cond.strip()
    m = EXPR.fullmatch(s)
    if m:
        s = m.group(1).strip()
    elif EXPR.search(s):
        return False   # text around an embedded expression: a non-empty string, truthy
    vals = _eval(s, known, {})
    return all(not isinstance(v, tuple) and not _gh_truthy(v) for v in vals)


def scan(root: Path) -> tuple[int, list, list]:
    """-> (workflow files read, rows [(path, job, event, venue, outcomes)], findings for R1/R3/R4)."""
    wf_dir = root / ".github" / "workflows"
    files = sorted(list(wf_dir.glob("*.yml")) + list(wf_dir.glob("*.yaml"))) if wf_dir.is_dir() else []
    docs = {}
    read = 0
    for f in files:
        try:
            docs[f.name] = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            read += 1
        except (OSError, yaml.YAMLError):
            docs[f.name] = None
    rows, findings = [], []
    for name, doc in sorted(docs.items()):
        rel = f".github/workflows/{name}"
        if doc is None:
            findings.append(f"{rel}: unparseable workflow: cannot prove its labels (fail closed)")
            continue
        evs = events_of(doc)
        if not evs:
            continue
        for jname, job in (doc.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            targets = _expand(docs, jname, job, (name,), findings, rel)
            for tname, tjob in targets:
                ro = _job_runs_on(tjob)
                if ro is None:
                    continue   # remote reusable workflow with no runner input: the callee's default (hosted)
                mv = _matrix_vals(tjob)
                for ev in evs:
                    if _never_runs(job, EVENTS[ev]) or (tjob is not job and _never_runs(tjob, EVENTS[ev])):
                        continue   # the job-level `if` excludes this event (caller's or callee job's own)
                    outs = outcomes(ro, EVENTS[ev], mv)
                    venue = _venue(outs)
                    rows.append((rel, tname, ev, venue, outs))
                    lits = [o for o in outs if o is not None]
                    if ev in PR_EVENTS and any(_is_p0(o) for o in lits):
                        findings.append(f"{rel}: {tname}: pull_request outcome requests p0 {_fmt(outs)}: PR CI never "
                                        f"runs on the p0 pool (R1)")
                    for o in lits:
                        low = [str(x).lower() for x in o]
                        if STUDIO_LABEL in low:
                            txt = (_matrix_text(tjob) + json.dumps([x for x in low if x != STUDIO_LABEL])).lower()
                            if "macos" not in txt or not re.search(r"arm|aarch64", txt):
                                findings.append(f"{rel}: {tname}: requests {STUDIO_LABEL} but its matrix/labels do "
                                                f"not include macos + arm (R3)")
                        known_l = [x for x in low if x != OPAQUE_LABEL.lower()]
                        for want in P0_LABELS:
                            if want in known_l and set(known_l) != {want}:
                                findings.append(f"{rel}: {tname}: requests {list(o)}: the {want} runners register "
                                                f"with exactly {{{want}}} (--no-default-labels), so no runner can "
                                                f"take it; request [{want}] alone (R6)")
                        if P0 in low and any("macos" in lab for lab in low):
                            findings.append(f"{rel}: {tname}: requests bare p0 with a macOS label {list(o)}: the Linux "
                                            f"p0 runners cannot run it; use {STUDIO_LABEL} (R4)")
    return read, rows, sorted(set(findings))


def _expand(docs: dict, jname: str, job: dict, stack: tuple, findings: list, rel: str) -> list:
    """[(qualified name, job)] with local reusable callees expanded recursively under the caller's events."""
    uses = job.get("uses")
    if not (isinstance(uses, str) and uses.startswith("./.github/workflows/")):
        return [(jname, job)]
    cname = uses.rsplit("/", 1)[-1].split("@")[0]
    if cname in stack:
        findings.append(f"{rel}: {jname}: local reusable workflow cycle {' -> '.join(stack + (cname,))}")
        return []
    callee = docs.get(cname)
    if not isinstance(callee, dict):
        findings.append(f"{rel}: {jname}: local callee {uses} missing or unparseable: cannot prove its labels "
                        f"(fail closed)")
        return []
    given = job.get("with") if isinstance(job.get("with"), dict) else {}
    defaults = {}
    wc = _triggers(callee).get("workflow_call")
    if isinstance(wc, dict) and isinstance(wc.get("inputs"), dict):
        for k, spec in wc["inputs"].items():
            if isinstance(spec, dict) and "default" in spec:
                defaults[k] = spec["default"]
    vals = {**defaults, **given}
    out = []
    for cj, cjob in (callee.get("jobs") or {}).items():
        if isinstance(cjob, dict):
            out += _expand(docs, f"{jname}/{cj}", _bind_inputs(cjob, vals), stack + (cname,), findings, rel)
    return out


def _input_expr(v) -> str:
    """A caller's `with:` value as an expression operand: a full ${{ }} keeps its body, a literal is quoted."""
    if isinstance(v, str):
        m = EXPR.fullmatch(v.strip())
        if m:
            return "(" + m.group(1).strip() + ")"
        if EXPR.search(v):
            return "inputs.__opaque__"
        return "'" + v.replace("'", "''") + "'"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return "inputs.__opaque__"


def _bind_inputs(cjob: dict, vals: dict) -> dict:
    """The callee job with `inputs.X` in its runs-on replaced by the caller's value (unknown inputs stay opaque)."""
    ro = cjob.get("runs-on")
    if ro is None:
        return cjob

    def sub(x):
        if isinstance(x, list):
            return [sub(e) for e in x]
        if isinstance(x, str) and "inputs." in x:
            return EXPR.sub(lambda m: "${{ " + re.sub(r"\binputs\.([A-Za-z_][\w-]*)",
                                                      lambda i: _input_expr(vals[i.group(1)]) if i.group(1) in vals
                                                      else i.group(0), m.group(1)) + " }}", x)
        return x
    return {**cjob, "runs-on": sub(ro)}


def _fmt(outs) -> str:
    return "[" + ", ".join("<opaque>" if o is None else "/".join(o) for o in outs) + "]"


def main_offenders(rows) -> dict:
    """(path, job) -> first offending event, for R2."""
    out = {}
    for rel, job, ev, venue, outs in rows:
        if ev in ("push", "merge_group") and venue == "pool":
            out.setdefault((rel, job), (ev, outs))
    return out


# ---------------------------------------------------------------- registration (fleet-ops-scripts host confs)
def check_registration(root: Path) -> list[str]:
    out = []
    base = root / "ci-runners" / "host-config"
    if not base.is_dir():
        return out
    for conf in sorted(base.glob("*/*.conf")):
        env = {}
        for ln in conf.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"^\s*Environment=(.*)$", ln)
            if not m:
                continue
            try:   # systemd: one directive may carry several space-separated (optionally quoted) assignments
                parts = shlex.split(m.group(1))
            except ValueError:
                out.append(f"{conf.relative_to(root)}: unparseable Environment= line: {ln.strip()}")
                continue
            if not parts:
                env = {}   # systemd: an empty Environment= resets every earlier assignment
            for part in parts:
                k, sep, v = part.partition("=")
                if sep and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
                    env[k] = v   # later assignments win, as in systemd
        labels = [x.strip() for x in env.get("LABELS", "").split(",") if x.strip()]
        labels_l = [x.lower() for x in labels]
        if not any(lab in P0_LABELS for lab in labels_l):
            continue
        host = conf.parent.name
        rel = str(conf.relative_to(root))
        want = STUDIO_LABEL if host == "mac-studio" else P0
        if env.get("NO_DEFAULT_LABELS") != "1":
            out.append(f"{rel}: p0 pool without NO_DEFAULT_LABELS=1: the runner also carries self-hosted/linux/x64, "
                       f"so PR jobs can land on it (R5)")
        if labels_l != [want]:
            out.append(f"{rel}: p0 pool LABELS={','.join(labels)}: must be exactly {want} on {host} (R5)")
        if host in POOL_SIZES and env.get("POOL_SIZE") != str(POOL_SIZES[host]):
            out.append(f"{rel}: p0 pool POOL_SIZE={env.get('POOL_SIZE')}: the 2026-10-15 ruling gives {host} "
                       f"{POOL_SIZES[host]} (R5)")
        elif host not in POOL_SIZES:
            out.append(f"{rel}: p0 pool on {host}, which the 2026-10-15 ruling does not list (R5)")
    return out


# ---------------------------------------------------------------- lint
def load_allowlist(path: Path) -> list[dict]:
    if not path.exists():
        return []
    doc = json.loads(path.read_text(encoding="utf-8"))
    rows = doc.get("jobs")
    if not isinstance(rows, list):
        raise ValueError("allowlist has no 'jobs' list")
    for r in rows:
        for k in ("path", "job", "reason", "expires"):
            if k not in r:
                raise ValueError(f"allowlist row missing {k!r}: {r}")
        if not str(r["reason"]).strip():
            raise ValueError(f"allowlist row has an empty reason: {r}")
        dt.date.fromisoformat(r["expires"])
    return rows


def lint(root: Path, allowlist: Path, today: dt.date) -> list[str]:
    read, rows, findings = scan(root)
    if read == 0:
        return [f"p0-label-lint: read 0 workflow files under {root}/.github/workflows: a lint that scans nothing is "
                f"red (S6)"]
    findings = list(findings) + check_registration(root)
    allowed = {}
    for r in load_allowlist(allowlist):
        key = (r["path"], r["job"])
        if key in allowed:
            findings.append(f"{ALLOWLIST}: duplicate row {key}")
        allowed[key] = r
        if dt.date.fromisoformat(r["expires"]) < today:
            findings.append(f"{r['path']}: {r['job']}: allowlist row EXPIRED {r['expires']} (request p0 on main, or "
                            f"renew the row with a reason)")
    offenders = main_offenders(rows)
    for (rel, job), (ev, outs) in sorted(offenders.items()):
        if (rel, job) not in allowed:
            findings.append(f"{rel}: {job}: {ev} runs on the fleet pool {_fmt(outs)} without p0, and is not in "
                            f"{ALLOWLIST} (R2)")
    for key in allowed:
        if key not in offenders:
            findings.append(f"{key[0]}: {key[1]}: allowlist row is stale (the job is gone or now requests p0 on "
                            f"main): delete it (the list only shrinks)")
    return findings


def seed(root: Path, allowlist: Path) -> int:
    read, rows, fnd = scan(root)
    broken = [f for f in fnd if "unparseable" in f or "cannot prove" in f or "cycle" in f]
    if read == 0 or broken:
        print(f"p0-label-lint: refusing to seed from a partial read ({read} workflows read; "
              f"{'; '.join(broken) or 'none'}): the allowlist is left untouched", file=sys.stderr)
        return 1
    old = {(r["path"], r["job"]): r for r in load_allowlist(allowlist)}
    jobs = []
    for rel, job in sorted(main_offenders(rows)):
        o = old.get((rel, job), {})
        jobs.append({"path": rel, "job": job, "reason": o.get("reason", SEED_REASON),
                     "expires": o.get("expires", SEED_EXPIRES)})
    doc = {"_doc": ("p0-label allowlist for p0-label-lint.py (Router Core spec v5 §4.1, card t_8b05b16b). Each row "
                    "grandfathers one push:main / merge_group job that runs on the fleet pool without requesting "
                    "p0, until `expires`. The seeded rows are the Card B migration inventory. Never add a row for a "
                    "new job; a stale row fails the lint, so the list only shrinks."),
           "schema": 1, "jobs": jobs}
    allowlist.parent.mkdir(parents=True, exist_ok=True)
    allowlist.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    print(f"seeded {len(jobs)} rows -> {allowlist}")
    return 0


# ---------------------------------------------------------------- selftest
WF_HEAD = "name: x\non:\n  pull_request:\n  push:\n    branches: [main]\n  merge_group:\njobs:\n"
GOOD_SPLIT = WF_HEAD + ("  t:\n    runs-on: ${{ github.event_name == 'pull_request' && (vars.CI_RUNNER || 'hermes-ci') "
                        "|| 'p0' }}\n    steps: [{run: 'true'}]\n")
HOSTED_JOB = WF_HEAD + "  t:\n    runs-on: ${{ vars.CI_LIGHT_RUNNER || 'ubuntu-latest' }}\n    steps: [{run: 'true'}]\n"
POOL_MAIN = WF_HEAD + ("  t:\n    runs-on: ${{ vars.CI_RUNNER || vars.CI_FALLBACK_RUNNER || 'hermes-ci' }}\n"
                       "    steps: [{run: 'true'}]\n")
PR_P0 = "name: x\non: [pull_request]\njobs:\n  t:\n    runs-on: p0\n    steps: [{run: 'true'}]\n"
PR_ONLY_POOL = "name: x\non: [pull_request]\njobs:\n  t:\n    runs-on: hermes-ci\n    steps: [{run: 'true'}]\n"
OPAQUE_MAIN = "name: x\non:\n  push:\n    branches: [main]\njobs:\n  t:\n    runs-on: ${{ inputs.r }}\n    steps: []\n"
STUDIO_OK = ("name: x\non:\n  push:\n    branches: [main]\njobs:\n  t:\n    strategy:\n      matrix:\n        os: [macos-arm64]\n"
             "    runs-on: p0-macos-arm64\n    steps: []\n")
STUDIO_BAD = STUDIO_OK.replace("os: [macos-arm64]", "os: [linux-x64]")
P0_MACOS = "name: x\non:\n  push:\n    branches: [main]\njobs:\n  t:\n    runs-on: [p0, macOS]\n    steps: []\n"
MATRIX_HOSTED = ("name: x\non:\n  push:\n    branches: [main]\njobs:\n  t:\n    strategy:\n      matrix:\n"
                 "        runner: [ubuntu-latest, macos-latest]\n    runs-on: ${{ matrix.runner }}\n    steps: []\n")
LOCAL_CALLEE = "name: c\non:\n  workflow_call:\njobs:\n  inner:\n    runs-on: hermes-ci\n    steps: []\n"
CALLER = "name: x\non:\n  push:\n    branches: [main]\njobs:\n  call:\n    uses: ./.github/workflows/c.yml\n"
PRT_P0 = ("name: x\non: [pull_request_target]\njobs:\n  t:\n    runs-on: ${{ github.event_name == "
          "'pull_request_target' && 'p0' || 'hermes-ci' }}\n    steps: []\n")
NESTED_A = "name: x\non: [pull_request]\njobs:\n  a:\n    uses: ./.github/workflows/b.yml\n"
NESTED_B = "name: b\non:\n  workflow_call:\njobs:\n  b:\n    uses: ./.github/workflows/c.yml\n"
NESTED_C = "name: c\non:\n  workflow_call:\njobs:\n  c:\n    runs-on: p0\n    steps: []\n"
CONTAINS_CASE = ("name: x\non: [pull_request]\njobs:\n  t:\n    runs-on: ${{ contains(github.event_name, "
                 "'PULL_REQUEST') && 'p0' || 'hermes-ci' }}\n    steps: []\n")
PR_P0_UPPER = PR_P0.replace("runs-on: p0", "runs-on: P0")
PR_P0_OPAQUE_ARRAY = PR_P0.replace("runs-on: p0", "runs-on: [p0, '${{ inputs.label }}']")
REG_MULTI = '[Service]\nEnvironment="LABELS=p0" "NO_DEFAULT_LABELS=0" "POOL_SIZE=2"\n'
HEAD_REF = PR_P0.replace("runs-on: p0", "runs-on: ${{ github.head_ref == 'release' && 'p0' || 'hermes-ci' }}")
CALLER_WITH = ("name: x\non: [pull_request]\njobs:\n  call:\n    uses: ./.github/workflows/cw.yml\n"
               "    with:\n      runner: ubuntu-latest\n")
CALLEE_WITH = ("name: cw\non:\n  workflow_call:\n    inputs:\n      runner: {type: string}\njobs:\n"
               "  a:\n    runs-on: ${{ inputs.runner }}\n    steps: []\n  b:\n    runs-on: p0\n    steps: []\n")
CALLEE_WITH_OK = CALLEE_WITH.replace("    runs-on: p0\n", "    runs-on: ${{ inputs.runner }}\n")
EMPTY_ARRAY = PR_P0.replace("runs-on: p0", "runs-on: ${{ fromJSON('[]') && 'p0' || 'hermes-ci' }}")
SELFHOSTED_P0 = POOL_MAIN.replace("${{ vars.CI_RUNNER || vars.CI_FALLBACK_RUNNER || 'hermes-ci' }}",
                                  "[self-hosted, p0]")
PUSH_ONLY_IF = WF_HEAD + "  t:\n    if: github.event_name == 'push'\n    runs-on: p0\n    steps: []\n"
PUSH_ONLY_IF_WRAPPED = PUSH_ONLY_IF.replace("if: github.event_name == 'push'",
                                            "if: \"${{ github.event_name != 'pull_request' }}\"")
OPAQUE_IF = PUSH_ONLY_IF.replace("if: github.event_name == 'push'", "if: always() && vars.X == 'y'")
PR_ONLY_IF_POOL = POOL_MAIN.replace("  t:\n", "  t:\n    if: github.event_name == 'pull_request'\n")
CALLER_IF = CALLER.replace("    uses:", "    if: github.event_name == 'pull_request'\n    uses:")
REG_RESET = ("[Service]\nEnvironment=NO_DEFAULT_LABELS=1\nEnvironment=\nEnvironment=LABELS=p0\n"
             "Environment=POOL_SIZE=2\n")
REG_GOOD = ("[Service]\nEnvironment=LABELS=p0\nEnvironment=NO_DEFAULT_LABELS=1\nEnvironment=POOL_SIZE=2\n")
REG_BAD = ("[Service]\nEnvironment=LABELS=self-hosted,p0\nEnvironment=POOL_SIZE=3\n")


def selftest() -> int:
    today = dt.date(2026, 10, 15)

    def tree(wfs: dict, rows: list | None = None, extra: dict | None = None) -> Path:
        d = Path(tempfile.mkdtemp(prefix="p0-label-"))
        for n, b in wfs.items():
            (d / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
            (d / ".github" / "workflows" / n).write_text(b)
        for n, b in (extra or {}).items():
            (d / n).parent.mkdir(parents=True, exist_ok=True)
            (d / n).write_text(b)
        if rows is not None:
            (d / "fleet").mkdir(exist_ok=True)
            (d / ALLOWLIST).write_text(json.dumps({"jobs": rows}))
        return d

    def row(job="t", exp="2027-01-15", path=".github/workflows/w.yml"):
        return {"path": path, "job": job, "reason": "fixture", "expires": exp}

    cases = [
        ("S6: no workflows is RED", {}, None, None, 1),
        ("PR->pool, main->p0 split is GREEN", {"w.yml": GOOD_SPLIT}, None, None, 0),
        ("hosted-runner job is exempt (GREEN)", {"w.yml": HOSTED_JOB}, None, None, 0),
        ("PR-only pool job is GREEN", {"w.yml": PR_ONLY_POOL}, None, None, 0),
        ("main job on the pool without p0 is RED (R2)", {"w.yml": POOL_MAIN}, None, None, 1),
        ("only-opaque runs-on on main is RED (fail closed)", {"w.yml": OPAQUE_MAIN}, None, None, 1),
        ("PR job requesting p0 is RED (R1)", {"w.yml": PR_P0}, None, None, 1),
        ("PR job requesting p0 is RED even when allowlisted", {"w.yml": PR_P0}, [row()], None, 1),
        ("allowlisted main pool job is GREEN", {"w.yml": POOL_MAIN}, [row()], None, 0),
        ("expired allowlist row is RED", {"w.yml": POOL_MAIN}, [row(exp="2026-10-14")], None, 1),
        ("stale allowlist row (job now p0) is RED", {"w.yml": GOOD_SPLIT}, [row()], None, 1),
        ("Studio label with a macos/arm matrix is GREEN", {"w.yml": STUDIO_OK}, None, None, 0),
        ("Studio label without macos/arm is RED (R3)", {"w.yml": STUDIO_BAD}, None, None, 1),
        ("bare p0 with macOS is RED (R4)", {"w.yml": P0_MACOS}, None, None, 1),
        ("static hosted matrix is exempt (GREEN)", {"w.yml": MATRIX_HOSTED}, None, None, 0),
        ("local reusable callee on the pool is RED", {"w.yml": CALLER, "c.yml": LOCAL_CALLEE}, None, None, 1),
        ("callee allowlisted as caller/inner is GREEN", {"w.yml": CALLER, "c.yml": LOCAL_CALLEE},
         [row(job="call/inner")], None, 0),
        ("p0 pool conf: exact label, no-default-labels, ruled size is GREEN", {"w.yml": GOOD_SPLIT}, None,
         {"ci-runners/host-config/ci-box/override-p0.conf": REG_GOOD}, 0),
        ("pull_request_target job requesting p0 is RED (R1)", {"w.yml": PRT_P0}, None, None, 1),
        ("two-deep local callee requesting p0 from a PR is RED", {"w.yml": NESTED_A, "b.yml": NESTED_B,
                                                                  "c.yml": NESTED_C}, None, None, 1),
        ("contains() is case-insensitive (PR -> p0 is RED)", {"w.yml": CONTAINS_CASE}, None, None, 1),
        ("runs-on: P0 on a PR is RED (labels case-insensitive)", {"w.yml": PR_P0_UPPER}, None, None, 1),
        ("[p0, <opaque>] on a PR is RED (known label kept)", {"w.yml": PR_P0_OPAQUE_ARRAY}, None, None, 1),
        ("multi-assignment Environment= with NO_DEFAULT_LABELS=0 is RED (R5)", {"w.yml": GOOD_SPLIT}, None,
         {"ci-runners/host-config/ci-box/override-p0.conf": REG_MULTI}, 1),
        ("PR head_ref is opaque (release -> p0 is RED)", {"w.yml": HEAD_REF}, None, None, 1),
        ("callee with a runner input still expanded: its hard p0 job is RED",
         {"w.yml": CALLER_WITH, "cw.yml": CALLEE_WITH}, None, None, 1),
        ("callee runner input bound to the caller's hosted value is GREEN",
         {"w.yml": CALLER_WITH, "cw.yml": CALLEE_WITH_OK}, None, None, 0),
        ("fromJSON('[]') is truthy (PR -> p0 is RED)", {"w.yml": EMPTY_ARRAY}, None, None, 1),
        ("[self-hosted, p0] on main is RED (R6: unsatisfiable)", {"w.yml": SELFHOSTED_P0}, None, None, 1),
        ("empty Environment= resets NO_DEFAULT_LABELS (R5 RED)", {"w.yml": GOOD_SPLIT}, None,
         {"ci-runners/host-config/ci-box/override-p0.conf": REG_RESET}, 1),
        ("p0 pool conf with default labels / wrong size is RED (R5)", {"w.yml": GOOD_SPLIT}, None,
         {"ci-runners/host-config/ace-ai/override-p0.conf": REG_BAD}, 1),
        ("push-only job `if` with runs-on p0 is GREEN (no PR outcome)", {"w.yml": PUSH_ONLY_IF}, None, None, 0),
        ("${{ }}-wrapped event `if` excluding PR is GREEN", {"w.yml": PUSH_ONLY_IF_WRAPPED}, None, None, 0),
        ("opaque job `if` may run on a PR: p0 is RED (R1)", {"w.yml": OPAQUE_IF}, None, None, 1),
        ("PR-only job `if` on the pool is GREEN (no main outcome)", {"w.yml": PR_ONLY_IF_POOL}, None, None, 0),
        ("caller job `if` (PR only) skips the callee's main pool job (GREEN)",
         {"w.yml": CALLER_IF, "c.yml": LOCAL_CALLEE}, None, None, 0),
    ]
    bad = 0
    for name, wfs, rows, extra, want in cases:
        d = tree(wfs, rows, extra)
        try:
            got_f = lint(d, d / ALLOWLIST, today)
            got = 1 if got_f else 0
        except (OSError, ValueError, KeyError) as e:
            got, got_f = 2, [str(e)]
        ok = got == want
        bad += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {name} (rc {got}, want {want})" + ("" if ok else f": {got_f}"))
    d = tree({"w.yml": GOOD_SPLIT})
    (d / "fleet").mkdir()
    (d / ALLOWLIST).write_text("{not json")
    rc = main(["--root", str(d), "--today", "2026-10-15"])
    print(f"{'ok  ' if rc == 2 else 'FAIL'} unreadable allowlist is blind (rc {rc}, want 2)")
    bad += rc != 2
    d = tree({"w.yml": POOL_MAIN, "broken.yml": "jobs: [unclosed\n"}, [row(exp="2026-12-01")])
    before = (d / ALLOWLIST).read_text()
    rc = seed(d, d / ALLOWLIST)
    ok = rc == 1 and (d / ALLOWLIST).read_text() == before
    print(f"{'ok  ' if ok else 'FAIL'} seed refuses a partial read and leaves the allowlist untouched (rc {rc})")
    bad += not ok
    print(f"selftest: {len(cases) + 2 - bad}/{len(cases) + 2} pass")
    return 1 if bad else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=Path("."))
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
    try:
        if a.seed_allowlist:
            return seed(root, allow)
        if a.list:
            read, rows, _ = scan(root)
            for rel, job, ev, venue, outs in rows:
                print(f"{venue:6} {ev:11} {rel} {job} {_fmt(outs)}")
            print(f"# {read} workflow files read, {len(rows)} job/event rows")
            return 0
        today = dt.date.fromisoformat(a.today) if a.today else dt.date.today()
        findings = lint(root, allow, today)
    except (OSError, ValueError, KeyError) as e:
        print(f"p0-label-lint: blind: {e}", file=sys.stderr)
        return 2
    for f in findings:
        print(f)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
