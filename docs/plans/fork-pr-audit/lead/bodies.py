import sys, os, re, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
import render

ROW, CARDS = render.ROW, render.CARDS



UPSTREAM_DONE = "UPSTREAM branches are built on NousResearch upstream/main: rebase on current upstream/main, NOT origin/main. Do NOT dispatch fork CI (`gh workflow run ci.yaml --ref audit/*/upstream-*`): it runs upstream's workflows, which pin 32/96-core runner pools ANG-Ventures lacks, so it queues forever (pre-check: `python scripts/ci_runner_label_lint.py --git-ref <ref>` rc=1). Gate = the Verify command via ~/.hermes/scripts/test-gate showing RED on upstream/main -> GREEN on the branch; paste the output in the handoff, then kanban_request_review. Upstream CI judges the upstream PR, opened only after Ace's go."

def verify_cmd(keys):
    out = []
    for k in keys:
        x = ROW[k].get('x') or {}
        txt = ' '.join(str(s) for s in [x.get('notes'), x.get('branch'), (x.get('evidence') or {}).get('upstream_state')])
        for m in re.findall(r'((?:~/\.hermes/scripts/test-gate\s+)?\S*python\S*\s+-m\s+pytest[^;|\n]{0,240}|pytest\s+[^;|\n]{0,200})', txt):
            out.append(m.strip())
        for m in re.findall(r'(tests/[\w/.\-]+\.py(?:::[\w\[\]\-]+)?)[^;|\n]{0,40}?(\d+ passed[^;|\n]{0,60})', txt):
            out.append(f'{m[0]}  (auditor: {m[1].strip()})')
    seen = []
    for o in out:
        if o not in seen:
            seen.append(o)
    return [s[:160] for s in seen[:1]]


def body(c):
    ks = c['keys']
    lines = [f"Derived from lead card t_03e35f0e (fork-PR audit FINAL). Verdict: **{c['verdict']}**. Keys: {', '.join(ks)}"
             + (f"; auto rows riding this card: {', '.join(c['followers'])}" if c['followers'] else '') + '.', '',
             '## FINAL.md row(s)', '',
             '| key | tranche | problem + evidence | upstream RED/GREEN | upstream fix | fires (window) | cost | auditor | adversary | FINAL verdict | branch | card |',
             '|---|---|---|---|---|---|---|---|---|---|---|---|']
    for k in ks[:3]:
        v = ROW[k]; x = v.get('x') or {}; e = x.get('evidence') or {}
        lines.append('| ' + ' | '.join([k, v['census_tranche'], render.cell(x.get('problem') or v['subject'], 110),
                                        render.cell(e.get('upstream_state'), 70), render.cell(e.get('upstream_fix'), 30),
                                        render.cell(e.get('fires'), 70), f"{v['loc']} loc, {len(v['conflict_files'])} cf ({v['conflict_syncs']})",
                                        str(v['auditor']), v['adv'], v['final'] + ' — ' + render.cell(v['why'], 260),
                                        render.cell(', '.join(c['branches']) or '—', 60), 'this card']) + ' |')
    if len(ks) > 3:
        lines.append(f'| +{len(ks) - 3} more keys ({", ".join(ks[3:])[:400]}): full rows in docs/plans/fork-pr-audit/FINAL.md |')
    lines += ['', '## Branch', '', (', '.join(f'`{b}`' for b in c['branches']) or
              ('NOT BUILT — build `audit/<tranche>/' + ('revert' if c['verdict'] == 'DROP' else 'upstream') + '-<key>` first ('
               + ('revert off current origin/main' if c['verdict'] == 'DROP' else 'port onto upstream/main; PR body must not mention the fork') + ').'))]
    if any('REBUILD' in ROW[k]['why'] or 'rebuilt' in ROW[k]['why'] for k in ks):
        lines.append('**The existing branch must be REBUILT — see the lead ruling above.**')
    vc = verify_cmd(ks)
    lines += ['', '## Verify', '']
    lines += [f'- `{v}`' for v in vc] or ['- No verify command recorded by the auditor: write the narrow test that pins the behaviour removed/ported, show it RED→GREEN, and run it via `~/.hermes/scripts/test-gate <venv python> -m pytest -q -p no:randomly <file>`.']
    lines += ['', '## Registry write-back (D2b, docs/sync/fork-features.json)', '']
    lines += [f'- {r} → ' + ('lifecycle must change (retire, or trim paths/tests) in the same PR' if c['verdict'] == 'DROP'
                            else 'set upstream_ref to the upstream PR once filed') for r in c['registry']] or ['- none (no registry entry names these paths outside the god files)']
    if any('ACE-GATED' in ROW[k]['why'] for k in ks):
        lines += ['', '**ACE-GATED**: registry entry is fork-permanent. Build + CI only; do NOT request merge until Ace retires the entry.']
    lines += ['', '## Done', '',
              (UPSTREAM_DONE + ' The fork-side drop follows the upstream merge. ' if c['verdict'] == 'UPSTREAM' else
               'Rebase on current origin/main; CI green; then kanban_request_review (slice cards complete on CI under review_policy milestone_only). ')
              + 'Nothing merges without fleet-merge after Ace\'s go.']
    return '\n'.join(lines)


def title(c):
    k = c['keys'][0]
    subj = re.sub(r'\s*\(#\d+\)\s*$', '', ROW[k]['subject'])[:70]
    more = f' (+{len(c["keys"]) - 1} rows)' if len(c['keys']) > 1 else ''
    return f"fork-PR audit {c['verdict']}: {k} {subj}{more}"


if __name__ == '__main__':
    out = [{'name': c['name'], 'title': title(c), 'assignee': c['assignee'], 'body': body(c)} for c in CARDS]
    json.dump(out, open(L + 'card_specs.json', 'w'), indent=1)
    print(len(out), max(len(o['body']) for o in out))
    print(out[0]['title']); print(out[0]['body'][:3000])
