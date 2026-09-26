import sys, os, re, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
import render, bodies

ROW, CARDS = render.ROW, render.CARDS
LIVE_REVERT = {l.split('|')[0].replace('origin/', '') for l in open(L + 'revert_stats.txt').read().splitlines()}


def body(c):
    ks = c['keys']
    L_ = [f"Lead t_03e35f0e FINAL: **{c['verdict']}** — {', '.join(ks)}" +
          (f" (+auto rows: {', '.join(c['followers'])})" if c['followers'] else '') +
          '. Full rows: docs/plans/fork-pr-audit/FINAL.md (PR #1128, branch audit/final).', '']
    for k in ks[:3]:
        v = ROW[k]; e = (v.get('x') or {}).get('evidence') or {}
        L_.append(f"- **{k}** [{v['census_tranche']}] {render.cell(v['subject'], 70)} | cost {v['loc']} loc, "
                  f"{len(v['conflict_files'])} cf ({v['conflict_syncs']}) | upstream: {render.cell(e.get('upstream_state'), 60)} | "
                  f"fires: {render.cell(e.get('fires'), 60)} | auditor {v['auditor']}, adversary {v['adv']} | "
                  f"FINAL: {render.cell(v['why'], 200)}")
    if len(ks) > 3:
        L_.append(f"- … {len(ks) - 3} more: {', '.join(ks[3:])[:400]}")
    brs = c['branches']
    if c['verdict'] == 'UPSTREAM':
        brs = [b for b in brs if '/revert-' not in b]
    note = ''
    if not brs:
        note = ('NOT BUILT: build `audit/<tranche>/' + ('revert' if c['verdict'] == 'DROP' else 'upstream') + '-<key>` ('
                + ('revert off origin/main' if c['verdict'] == 'DROP' else 'port onto upstream/main; PR body must not mention the fork') + ').')
    L_ += ['', 'Branch: ' + (', '.join(f'`{b}`' for b in brs) or note)]
    if any('REBUILD' in ROW[k]['why'] or 'rebuilt' in ROW[k]['why'] for k in ks):
        L_.append('Existing branch must be REBUILT per the lead ruling.')
    vc = bodies.verify_cmd(ks)
    L_.append('Verify: ' + ('; '.join(f'`{v}`' for v in vc) if vc else
                            'none recorded — write the narrow test pinning the behaviour, RED→GREEN, run via `~/.hermes/scripts/test-gate <venv python> -m pytest -q -p no:randomly <file>`.'))
    L_.append('Registry (D2b): ' + ('; '.join(c['registry']) + (' → change lifecycle/paths in the same PR' if c['verdict'] == 'DROP' else ' → set upstream_ref once filed')
                                   if c['registry'] else 'none'))
    if any('ACE-GATED' in ROW[k]['why'] for k in ks):
        L_.append('**ACE-GATED**: fork-permanent registry entry — build + CI only; no merge request until Ace retires it.')
    L_ += ['', (bodies.UPSTREAM_DONE + ' ' if c['verdict'] == 'UPSTREAM' else
                'Rebase on current origin/main; CI green; then kanban_request_review (slice cards complete on CI under review_policy milestone_only). ')
           + 'Merges via fleet-merge after Ace\'s go.']
    return '\n'.join(L_)


if __name__ == '__main__':
    out = [{'name': c['name'], 'title': bodies.title(c), 'assignee': c['assignee'], 'body': body(c)} for c in CARDS]
    json.dump(out, open(L + 'card_specs.json', 'w'), indent=1)
    import statistics
    print(len(out), statistics.mean(len(o['body']) for o in out))
