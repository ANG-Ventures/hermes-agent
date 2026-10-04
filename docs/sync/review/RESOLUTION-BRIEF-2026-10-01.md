# RESOLUTION BRIEF: parity sync 2026-10-01

Staged merge of upstream `origin/main` **612d8e44a2** (NousResearch/hermes-agent, FROZEN) into
fork `fork/main` **2eb646f755** (ANG-Ventures/hermes-agent). 20,300 upstream commits since the
merge-base **26350357d7** (the 2026-08-30 sync's target). Orchestrator card: **t_e45c8c8d**.

Worktree: `/Volumes/fleet-scratch/workspaces/default/t_e45c8c8d/wt`
Branch `sync/upstream-2026-10-01`. MERGE_HEAD exists. Conflict style is diff3
(`<<<<<<< HEAD` = fork / `||||||| <base>` = merge-base / `=======` / `>>>>>>> 612d8e44` = upstream).

## You are a LANE worker. Hard limits.
- You own ONLY the files listed in `docs/sync/review/lanes-2026-10-01/<YOUR-LANE>.txt`.
  Never edit, `git add`, or checkout any other path. Other lane workers are live on this tree.
- BARRED: `git commit`, `git merge` (any form, incl. `--abort`/`--continue`), `git stash`,
  `git reset`, `git checkout <ref>` / `git switch`, `git push`, `git rm` outside your list,
  `git clean`, editing anything outside this worktree, running a full test suite.
  The orchestrator commits and lands. You resolve.
- Allowed git: `git show :1:<f>` / `:2:<f>` / `:3:<f>` (base/fork/upstream stages),
  `git log`, `git diff`, `git blame`, `git grep`, and `git add <your-file>`.
  If `git add` fails on `index.lock`, sleep 2 and retry (siblings share the index). Never delete the lock.
- Scratch files: `/tmp/e45-<YOUR-LANE>/` only. Never write scratch into the worktree.

## Protocol
1. Read this brief, then your lane list. Skip any file that already has zero markers AND
   `git ls-files -u -- <f>` is empty (already staged).
2. Heartbeat: at each file START append `$(date +%T) <file>` to `/tmp/e45-hb-<YOUR-LANE>.txt`.
3. Hardest file first is fine, but finish files: a half-resolved file is worth nothing.
4. Per file, when done:
   - `grep -cE '^(<<<<<<<|=======|>>>>>>>|\|\|\|\|\|\|\|)( |$)' <f>` → 0
     (careful: `=======` lines inside markdown/rst are legit content; only real hunk markers count).
   - `.py`: `~/.hermes/hermes-agent/venv/bin/python -m py_compile <f>`.
   - `.py` (non-test): run the conflict-resolution gate:
     ```
     D=/tmp/e45-<LANE>/$(echo <f>|tr / _); mkdir -p $D
     git show :1:<f> > $D/base.py; git show :2:<f> > $D/ours.py; git show :3:<f> > $D/theirs.py
     python3 ~/.hermes/skills-shared/coding/upstream-parity-merge/scripts/conflict_resolution_gate.py \
       --base $D/base.py --ours $D/ours.py --theirs $D/theirs.py --merged <f>
     ```
     (Extract the stages BEFORE `git add`: once staged, `:1:/:2:/:3:` are gone. Do it at file START.)
     Every symbol it reports lost must be either restored or justified in the ledger.
   - YAML/JSON/TOML: parse it (`python -c 'import yaml,sys;yaml.safe_load(open(sys.argv[1]))' <f>`).
   - `git add <f>`.
   - Append one line to `docs/sync/review/ledger-2026-10-01/<YOUR-LANE>.md`:
     `| <path> | <hunks> | U/B/F/UP/TEST | why (name the fork feature / upstream change) | residual risk |`
     (U=union, B=both interleaved, F=fork side, UP=upstream side, TEST=test-contract driven.)
5. If you near your context ceiling: finish or abandon the CURRENT file cleanly (an abandoned file
   keeps its markers and stays unstaged), write the ledger, and stop. Report the exact files left.
   An honest ceiling-stop is the correct outcome. Never rush a god-file.

## How to resolve (method rules)
- Read BOTH sides' intent before choosing. For a hunk, find the commits:
  `git log --oneline 26350357..2eb646f755 -L<start>,<end>:<f>` is slow; prefer
  `git log --oneline -S'<distinctive token>' 26350357..HEAD -- <f>` (fork) and
  `... 26350357..612d8e44 -- <f>` (upstream). Commit messages carry the why.
- **Upstream refactor + fork behavior:** upstream structure wins; re-thread the fork behavior into it.
  Never blind-pick a side on a semantic hunk.
- **Upstream ABSORBED a fork feature** (fork code now present upstream, often authored by
  Kyzcreig / daedalus*): upstream's copy is canonical; re-apply only fork deltas made after it.
  Check: `git log --format='%h %an %s' 26350357..612d8e44 -- <f> | grep -iE 'kyzcreig|daedalus|ang'`.
- **Parallel invention** (upstream shipped an equivalent): converge on upstream unless the fork
  version carries behavior a fork TEST asserts; then port that delta onto upstream's code.
- **Fork-inline vs upstream-extracted:** if upstream moved a helper to a module and imports it,
  drop the fork inline copy, keep the import, and make sure the fork's edits to that helper
  are carried into the module copy (the module file may be outside your lane: if so, ledger it
  as `FOLLOWUP: <module> needs <delta>` and do not edit it).
- **Both sides added params / kwargs / imports:** keep both, then grep the whole file for the
  symbol: auto-merge outside markers can leave duplicate args/imports (SyntaxError/TypeError).
- **After taking upstream for a variable/declaration:** grep its use count on the fork side and
  restore any dropped consume site.
- **fork_ext call sites** (`agent/fork_ext/*`, `cron/fork_ext/*`, `gateway/fork_ext*`): must survive
  in every god-file. After resolving, `grep -n fork_ext <f>` and compare with
  `git show :2:<f> | grep -n fork_ext` (before staging). Every dropped call site is a defect.
- **Policy predicates duplicated across sites** (approval bypass, `approvals.mode == "off"`,
  YOLO): keep the term-set identical across every site; prefer the canonical helper.
- **Never** use whitespace-ignoring merges, `--ours`/`--theirs` wholesale on a .py with semantic
  hunks, or script-excise duplicates. Byte-identical duplicate defs: leave and ledger them.
- **Tests (`tests/**`):** the merged test file must hold the UNION of both sides' tests unless a test
  targets code that no longer exists. Drop a fork test only if its subject was deleted/replaced
  upstream AND you name the replacement in the ledger. Fork-authored tests otherwise survive.
  Check duplicate names: `python3 -c "import re,collections,sys;print([k for k,v in collections.Counter(re.findall(r'def (test_\w+)',open(sys.argv[1]).read())).items() if v>1])" <f>`.
- **DU (fork deleted, upstream modified) / UD (upstream deleted, fork modified):** read why the
  deleting side deleted (`git log --diff-filter=D -1 -- <f>` on that side). Deliberate fork
  retirements stay deleted (`git rm <f>`, in your lane only), and port any upstream fix in that file
  to where the fork moved the logic. Upstream deletion of a file the fork still runs: keep the fork
  file if anything in the merged tree imports/calls it (`git grep`), else accept the deletion.

## Known decisions (do not relitigate)
- `apps/desktop/**`: wholesale upstream (doctrine D9). Done by orchestrator.
- `locales/*.yaml`: structural 3-way merge done by orchestrator (fork-only keys preserved). Done.
- `contributors/emails/*`: done.
- `tests/run_agent/test_run_agent.py` / `tests/agent/test_run_agent.py`: the fork SPLIT the monolith
  (8f7c231c77, CI shard timeout). Never restore it. Port genuinely-new upstream tests into the
  matching split file under `tests/run_agent/`.
- `plugins/memory/mem0/_backend.py`, `_setup.py`, `_openai_llm.py`, `_oss_providers.py` (DU): the fork
  restructured mem0 last sync. Keep them deleted; port upstream's in-range semantic fixes into the
  fork's mem0 modules, or ledger NO-PORT-NEEDED with evidence.
- `.github/workflows/*`: the fork runs its own CI topology (self-hosted runner labels via
  `vars.CI_RUNNER_LABELS`, sliced matrix, merge_group triggers, fleet gates). Fork topology wins;
  adopt upstream's new steps only where they fit it. No literal upstream runner labels
  (`ubuntu-latest-32-core` etc.) may appear un-gated.

## Fork features that MUST survive (docs/sync/fork-features.json; canary tests gate the PR)
chat model pins + cross-session reply mismatch announcements · cron approval gate reads ContextVar ·
systemd restart exits 0 / launchd exits 75 · hygiene compaction announces in-chat · messaging + moa
toolsets · telegram polling never drops pending updates · cron per-job reasoning + script timeout ·
relay-pool session affinity + lane headers · gateway restart failure-count codec, policy, config
bridge, initiator breadcrumb · configured/persisted route identity helpers · tool-search unwrap scope
gate + delegate code_execution inheritance · state denorm gate + platform session search ·
compute-host turn isolation (absorbed) · /undo /redo · /branch (/fork) incl. Discord thread spawn ·
/merge · /fast · discord free-response coercion · telegram intake sentinel · cron cross-vendor refusal ·
runtime footer fork fields (provider_model, context_full, reasoning) · model/provider transition
announce once · reasoning-only change announce · Claude Code CLI caps = quota · lazy provider plugin
discovery · provider-registry generation seam (`hermes_cli/provider_seam.py`).
When a file you resolve touches one of these, name it in the ledger line.
