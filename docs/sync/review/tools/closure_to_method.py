#!/usr/bin/env python3
"""closure_to_method.py — run69 tool for the gateway/run.py TurnRunner extraction.

PROBLEM
Upstream (1e5b5074) extracted three giant closures out of
GatewayRunner._run_agent_inner into a new `class TurnRunner`:
    progress_callback / send_progress_messages / run_sync
The fork edited those SAME closures heavily. git renders the biggest one
(run_sync) as a single ~990-line both-sides conflict that is not hand-splicable.

METHOD
Upstream's extraction is MECHANICAL: the bodies are byte-identical to the
original closures modulo three rewrites (their own docstring says so):
    closed-over local  ->  ctx.<field>      (fields listed in gateway/turn_context.py)
    self               ->  self._runner
    nonlocal <name>    ->  (dropped; became ctx.<name> writes)
So: apply that SAME transform to the fork side and to the MERGE BASE, then run a
real 3-way `git merge-file` of base-as-method / fork-as-method / upstream-method.
On run69 this took the 990-line conflict to **0 conflicts**, AST-clean, with every
fork-only feature verified present afterwards.

WHY AST, NOT REGEX
A regex version of this transform produces PLAUSIBLE GARBAGE: it rewrites
keyword-argument names (`message=` -> `ctx.message=`), attribute accesses, and
comment/docstring prose, manufacturing ~8 bogus conflicts. The tell is a
conflict body containing `def f(ctx.message: str)`. Only rewrite ast.Name loads.

USAGE
    python3 closure_to_method.py            # prints conflict count, writes /tmp/rs_merged.txt
Then paste the product in as the TurnRunner method body (indent already correct).

VERIFY AFTER (non-optional — the merge being clean does NOT mean nothing was lost):
  * ast.parse the whole file;
  * symbol audit fork-only names against the product (run69 found StreamConsumerConfig
    reading as "lost" — it was RELOCATED to _build_stream_consumer_config, fine — and
    separately found two REAL defects, see the ledger).
"""
import ast, io, os, re, subprocess, tempfile, textwrap

ROOT = "/Users/alexgierczyk/parity-merge-20260807"
BASE = "a7a696ba"          # merge base
FORK = "ee2fce2876"        # fork/main at merge time
UP   = "MERGE_HEAD"        # frozen upstream target 1e5b5074


def show(rev, path):
    return subprocess.run(["git", "show", f"{rev}:{path}"], cwd=ROOT,
                          capture_output=True, text=True).stdout


def block_at(lines, idx0):
    """Return the def-block starting at 0-based idx0 (by indentation)."""
    head = lines[idx0]
    ind = len(head) - len(head.lstrip())
    j = idx0 + 1
    while j < len(lines):
        s = lines[j]
        if s.strip() and (len(s) - len(s.lstrip())) <= ind and not s.lstrip().startswith("#"):
            break
        j += 1
    return lines[idx0:j]


def find_def(lines, pattern, occurrence=0):
    rx = re.compile(pattern)
    n = 0
    for i, l in enumerate(lines):
        if rx.match(l):
            if n == occurrence:
                return i
            n += 1
    raise LookupError(pattern)


def ctx_fields():
    src = io.open(os.path.join(ROOT, "gateway/turn_context.py"), encoding="utf-8").read()
    return set(re.findall(r"^\s{4}([a-zA-Z_][a-zA-Z0-9_]*)\s*[:=]", src, re.M))


def to_method(lines, fields):
    """Closure body -> TurnRunner method body. AST-accurate; loads only."""
    src = textwrap.dedent("\n".join(lines))
    tree = ast.parse(src)
    edits = []

    class V(ast.NodeVisitor):
        def visit_Name(self, node):
            if node.id in fields:
                edits.append((node.lineno, node.col_offset, node.end_col_offset, "ctx." + node.id))
            self.generic_visit(node)

        def visit_Attribute(self, node):
            v = node.value
            if isinstance(v, ast.Name) and v.id == "self":
                edits.append((v.lineno, v.col_offset, v.end_col_offset, "self._runner"))
            self.generic_visit(node)

        def visit_Nonlocal(self, node):
            edits.append((node.lineno, node.col_offset, node.end_col_offset,
                          "#nonlocal " + ", ".join(node.names)))
            self.generic_visit(node)

    V().visit(tree)
    out = src.split("\n")
    by_line = {}
    for ln, c0, c1, new in edits:
        by_line.setdefault(ln, []).append((c0, c1, new))
    for ln, es in by_line.items():
        s = out[ln - 1]
        for c0, c1, new in sorted(es, key=lambda e: -e[0]):
            s = s[:c0] + new + s[c1:]
        out[ln - 1] = s
    return ["    " + l if l.strip() else l for l in out]   # method-level indent


def merge_closure(pattern, base_occ=1, fork_occ=1, up_occ=0):
    fields = ctx_fields()
    b = show(BASE, "gateway/run.py").split("\n")
    f = show(FORK, "gateway/run.py").split("\n")
    u = show(UP,   "gateway/run.py").split("\n")
    bm = to_method(block_at(b, find_def(b, pattern, base_occ)), fields)
    fm = to_method(block_at(f, find_def(f, pattern, fork_occ)), fields)
    um = block_at(u, find_def(u, pattern.replace(r"\(", r"\(self"), up_occ))
    d = tempfile.mkdtemp()
    for nm, data in (("base", bm), ("ours", fm), ("theirs", um)):
        io.open(os.path.join(d, nm), "w", encoding="utf-8").write("\n".join(data) + "\n")
    r = subprocess.run(["git", "merge-file", "--diff3", "-p",
                        os.path.join(d, "ours"), os.path.join(d, "base"), os.path.join(d, "theirs")],
                       capture_output=True, text=True)
    return r.stdout


if __name__ == "__main__":
    out = merge_closure(r"^\s+def run_sync\(")
    io.open("/tmp/rs_merged.txt", "w", encoding="utf-8").write(out)
    print("conflicts:", out.count("<<<<<<<"))
    print("lines:", len(out.split("\n")))
