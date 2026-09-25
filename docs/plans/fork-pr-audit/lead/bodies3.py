import sys, os, re, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
import render, bodies

ROW, CARDS = render.ROW, render.CARDS


def body(c):
    ks = c['keys']
    out = [f"Lead t_03e35f0e FINAL {c['verdict']}. FINAL.md rows (docs PR #1128, branch audit/final, docs/plans/fork-pr-audit/FINAL.md):"]
    for k in ks[:3]:
        v = ROW[k]
        out.append(f"- {k} [{v['census_tranche']}] {v['loc']} loc, {len(v['conflict_files'])} cf ({v['conflict_syncs']}); "
                   f"auditor {v['auditor']}, adv {v['adv']}: {render.cell(v['why'], 170)}")
    if len(ks) > 3:
        out.append(f"- +{len(ks) - 3}: {', '.join(ks[3:])[:300]}")
    if c['followers']:
        out.append(f"- auto rows riding: {', '.join(c['followers'])}")
    brs = c['branches']
    if c['verdict'] == 'UPSTREAM':
        brs = [b for b in brs if '/revert-' not in b]
    out.append('Branch: ' + (', '.join(brs) or ('NOT BUILT: build audit/<tranche>/' + ('revert-<key> off origin/main' if c['verdict'] == 'DROP' else 'upstream-<key> on upstream/main (body must not mention the fork)'))))
    vc = bodies.verify_cmd(ks)
    out.append('Verify: ' + (vc[0][:150] if vc else 'none recorded; write a narrow RED->GREEN test, run via ~/.hermes/scripts/test-gate'))
    out.append('Registry D2b: ' + ('; '.join(r[:70] for r in c['registry']) if c['registry'] else 'none'))
    if any('ACE-GATED' in ROW[k]['why'] for k in ks):
        out.append('ACE-GATED: fork-permanent entry; no merge request until Ace retires it.')
    out.append('Rebase on current origin/main; CI green; then kanban_request_review (slice cards complete on CI under review_policy milestone_only).'
               + (" Upstream PR only after Ace's go." if c['verdict'] == 'UPSTREAM' else ''))
    return '\n'.join(out)


if __name__ == '__main__':
    out = [{'name': c['name'], 'title': bodies.title(c), 'assignee': c['assignee'], 'body': body(c)} for c in CARDS]
    json.dump(out, open(L + 'card_specs.json', 'w'))
    import statistics
    print(len(out), statistics.mean(len(o['body']) for o in out))
