"""DEFINITIVE orphan-tail detector.

For each unmerged .py: synthesize the full --ours and --theirs resolutions,
then check what actually lands at MODULE scope. The retry_utils defect shape
(function body kept, its `def` header consumed by auto-merge above the marker)
surfaces as a bare `assert`/`return`/`await`/augmented-assign at module level,
or a SyntaxError. ast.parse ALONE does not catch it: a module-level `assert`
is valid Python that NameErrors at import.
"""
import ast, subprocess

files = [f for f in subprocess.run(["git","diff","--name-only","--diff-filter=U"],
         capture_output=True,text=True).stdout.split() if f.endswith(".py")]

def synth(lines, side):
    out, i = [], 0
    while i < len(lines):
        l = lines[i]
        if l.startswith("<<<<<<< "):
            b  = next((j for j in range(i+1,len(lines)) if lines[j].startswith("||||||| ")), None)
            eq = next((j for j in range(i+1,len(lines)) if lines[j].startswith("=======")), None)
            e  = next((j for j in range(i+1,len(lines)) if lines[j].startswith(">>>>>>> ")), None)
            if e is None: break
            hi = b if (b is not None and b < eq) else eq
            out += lines[i+1:hi] if side=="ours" else lines[eq+1:e]
            i = e+1
        else:
            out.append(l); i += 1
    return "\n".join(out)

BAD = (ast.Assert, ast.Return, ast.Continue, ast.Break, ast.AugAssign, ast.Raise)
flagged = 0
for f in files:
    lines = open(f, encoding="utf-8", errors="replace").read().split("\n")
    for side in ("ours","theirs"):
        src = synth(lines, side)
        try:
            tree = ast.parse(src)
        except SyntaxError as ex:
            print(f"  {f}  --{side}: SyntaxError L{ex.lineno}: {ex.msg}")
            flagged += 1
            continue
        bad = [n for n in tree.body if isinstance(n, BAD)]
        if bad:
            print(f"  {f}  --{side}: {len(bad)} module-level "
                  f"{[type(n).__name__ for n in bad]} at lines {[n.lineno for n in bad]}")
            flagged += 1
print(f"\nfiles scanned: {len(files)}   FLAGGED SIDES: {flagged}")
