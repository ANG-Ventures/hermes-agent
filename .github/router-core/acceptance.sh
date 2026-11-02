#!/usr/bin/env bash
# Router Core Card C2 acceptance, on THIS repo's real tree (card t_8b05b16b, spec v5 §4.1 / §7.1).
# Each vendored lint must be green on the tree as committed, and red on: a planted ungated docker-run spawn,
# a planted ungated ssh-spawn, every allowlist row past its expiry, and an empty scan root (S6).
#   acceptance.sh [dispatcher|pool]   the queue loop this repo owns, if any (§7.1 [v4 RC-e])
# Exit 0 when every case gives the expected rc, 1 otherwise. Needs python3 with pyyaml ($PYTHON overrides).
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
root=$(git -C "$here" rev-parse --show-toplevel)
read -ra PY <<< "${PYTHON:-python3}"   # may be a multi-word launcher (uv run ... python)
tmp=$(mktemp -d) || { echo "acceptance: mktemp failed; refusing to run" >&2; exit 2; }
case "$tmp" in /?*/*) ;; *) echo "acceptance: unsafe temp dir '$tmp'" >&2; exit 2 ;; esac
[ -d "$tmp" ] || { echo "acceptance: temp dir '$tmp' missing" >&2; exit 2; }
trap 'rm -rf -- "$tmp"' EXIT
fail=0

expect() {   # expect <want-rc> <label> <cmd...>
  local want=$1 label=$2 rc
  shift 2
  "$@" >"$tmp/out" 2>&1
  rc=$?
  if [ "$rc" = "$want" ]; then
    printf 'ok   %s (rc %s)\n' "$label" "$rc"
  else
    printf 'FAIL %s (rc %s, want %s)\n' "$label" "$rc" "$want"
    sed 's/^/     /' "$tmp/out" | head -20
    fail=1
  fi
}

fresh_copy() {   # the index's files only, no .git: the lints walk the copy (one process, no xargs split)
  rm -rf -- "${tmp:?}/tree"
  git -C "$root" checkout-index -a -f --prefix="$tmp/tree/" || { echo "FAIL could not copy the tree"; fail=1; }
}

spawn="$here/lint-spawn-site.py"
p0="$here/p0-label-lint.py"
loops="$here/lint-queue-loops.py"
mkdir "$tmp/empty"

# ---- spawn-site lint (§7.1)
expect 0 "spawn-site: real tree is clean" "${PY[@]}" "$spawn" --root "$root"
fresh_copy
expect 0 "spawn-site: the copied tree is clean too (the copy is faithful)" "${PY[@]}" "$spawn" --root "$tmp/tree"
mkdir -p "$tmp/tree/planted"
printf '#!/usr/bin/env bash\nstart() {\n  ssh ci-box %s\n}\n' "'docker run -d --rm img:latest'" > "$tmp/tree/planted/pool.sh"
expect 1 "spawn-site: planted bare docker-run spawn is red" "${PY[@]}" "$spawn" --root "$tmp/tree"
rm "$tmp/tree/planted/pool.sh"
printf 'import subprocess\n\n\ndef go(h):\n    subprocess.run(["ssh", h, "make test"])\n' > "$tmp/tree/planted/go.py"
expect 1 "spawn-site: planted bare ssh-spawn (python) is red" "${PY[@]}" "$spawn" --root "$tmp/tree"
expect 1 "spawn-site: every allowlist row past its expiry is red" "${PY[@]}" "$spawn" --root "$root" --today 2027-01-16
expect 1 "spawn-site: empty scan root is rc 1 (S6)" "${PY[@]}" "$spawn" --root "$tmp/empty"

# ---- p0-label lint (§4.1)
expect 0 "p0-label: real tree is clean" "${PY[@]}" "$p0" --root "$root"
fresh_copy
mkdir -p "$tmp/tree/.github/workflows"
printf 'name: planted\non: [pull_request]\njobs:\n  t:\n    runs-on: p0\n    steps: [{run: "true"}]\n' \
  > "$tmp/tree/.github/workflows/planted-pr-p0.yml"
expect 1 "p0-label: planted PR job requesting p0 is red" "${PY[@]}" "$p0" --root "$tmp/tree"
rm "$tmp/tree/.github/workflows/planted-pr-p0.yml"
printf 'name: planted\non:\n  push:\n    branches: [main]\njobs:\n  t:\n    runs-on: universal-ci\n    steps: [{run: "true"}]\n' \
  > "$tmp/tree/.github/workflows/planted-main-pool.yml"
expect 1 "p0-label: planted main job on the pool without p0 is red" "${PY[@]}" "$p0" --root "$tmp/tree"
if [ -s "$root/fleet/p0-label-allowlist.json" ] && "${PY[@]}" -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1]))["jobs"] else 1)' "$root/fleet/p0-label-allowlist.json"; then
  expect 1 "p0-label: every allowlist row past its expiry is red" "${PY[@]}" "$p0" --root "$root" --today 2027-01-16
fi
expect 1 "p0-label: empty scan root is rc 1 (S6)" "${PY[@]}" "$p0" --root "$tmp/empty"

# ---- queue loops (§4.2 / §7.1 [v4 RC-e])
for check in "$@"; do
  expect 0 "queue-loops $check: real tree presents every ready item" "${PY[@]}" "$loops" --root "$root" --check "$check"
  fresh_copy
  "${PY[@]}" - "$tmp/tree" "$check" <<'PY'
import re, sys
from pathlib import Path
root, check = Path(sys.argv[1]), sys.argv[2]
rel, pat, step = {
    "dispatcher": ("hermes_cli/kanban_db_dispatch.py", r"^(\s*)for row_index, row in enumerate\(ready_rows\):\s*$", "    "),
    "pool": ("ci-runners/src/ci-runner-pool.sh", r'^(\s*)while\b.*"\$i"\s+-le\s+"\$effective_pool".*;\s*do\s*$', "  "),
}[check]
p = root / rel
lines = p.read_text().splitlines(keepends=True)
i = next(n for n, ln in enumerate(lines) if re.match(pat, ln))
indent = re.match(pat, lines[i]).group(1) + step
lines.insert(i + 1, f"{indent}break\n")
p.write_text("".join(lines))
PY
  [ $? = 0 ] || { echo "FAIL queue-loops $check: could not plant the break (target or loop missing)"; fail=1; }
  expect 1 "queue-loops $check: a planted early break is red" "${PY[@]}" "$loops" --root "$tmp/tree" --check "$check"
  expect 1 "queue-loops $check: empty root is rc 1 (S6)" "${PY[@]}" "$loops" --root "$tmp/empty" --check "$check"
done

[ "$fail" = 0 ] && echo "acceptance: all cases pass" || echo "acceptance: FAILED"
exit "$fail"
