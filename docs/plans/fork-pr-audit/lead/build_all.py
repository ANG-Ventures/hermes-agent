"""Assemble all 1,116 FINAL rows -> lead/rows.json. Deterministic; rerunnable."""
import sys, os, re, json, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
import final

F = final.build()
rows = census()
byk = {r['key']: r for r in rows}
code = [r for r in rows if TMAP[r['tranche']] in T]
path2rows = collections.defaultdict(list)
for r in code:
    for p in r['paths']:
        path2rows[p].append(r['key'])


def cand_src(p):
    m = re.match(r'test_(.+)\.py$', os.path.basename(p))
    return m.group(1) if m else None


amap = {}
for r in rows:
    if r['tranche'] != 'auto':
        continue
    hits = collections.Counter()
    for p in r['paths']:
        for kk in path2rows.get(p, []):
            hits[kk] += 3
        s = cand_src(p)
        if s:
            for cp, ks in path2rows.items():
                if os.path.basename(cp)[:-3] == s:
                    for kk in ks:
                        hits[kk] += 1
    if hits:
        amap[r['key']] = max(hits.items(), key=lambda kv: (kv[1], byk[kv[0]]['last_date']))[0]

DESK = ('D9: apps/desktop is upstream-owned since 2026-08-08. Combined path-based revert audit/final-revert-desktop-retired '
        '@4568f6e20a (git rm -r apps/desktop && git checkout upstream/main -- apps/desktop; git diff upstream/main -- apps/desktop '
        'is empty; tests/tui_gateway/test_desktop_runtime_footer.py + tests/test_hermes_state_search.py: 152 passed via test-gate).')
ROW = {}
for r in rows:
    k, tr = r['key'], r['tranche']
    base = {'census_tranche': tr, 'subject': r['subject'], 'loc': r['loc'], 'conflict_syncs': r['conflict_syncs'],
            'conflict_files': r['conflict_files'], 'paths': r['paths']}
    if k in F:
        v = dict(F[k]); v.update(base); ROW[k] = v
        continue
    if tr == 'auto-desktop-retired':
        v = {'tranche': tr, 'auditor': '(auto)', 'adv': 'n/a', 'adv_new': None, 'final': 'DROP', 'why': DESK,
             'x': {'branch': 'audit/final-revert-desktop-retired'}}
    elif tr == 'auto':
        o = amap.get(k)
        if o and o in F:
            fv = F[o]['final']
            v = {'tranche': tr, 'auditor': '(auto)', 'adv': 'n/a', 'adv_new': None, 'final': fv, 'follows': o,
                 'why': f'auto: follows {o} ({F[o]["tranche"]}) by path; ' + ('joins that card.' if fv in ('DROP', 'UPSTREAM') else 'same fate.'),
                 'x': {'branch': None}}
        else:
            v = {'tranche': tr, 'auditor': '(auto)', 'adv': 'n/a', 'adv_new': None, 'final': 'KEEP',
                 'why': 'auto: no tranche code row owns these paths (fork test-infra, fork docs/specs, fork-ext goldens); '
                        'stays with the fork code it serves; zero runtime cost.', 'x': {'branch': None}}
    else:
        raise SystemExit(f'unhandled {k} {tr}')
    v.update(base); ROW[k] = v

keys = [r['key'] for r in rows]
assert len(keys) == len(set(keys)) == 1116, len(keys)
assert set(ROW) == set(keys)
json.dump(ROW, open(L + 'rows.json', 'w', encoding='utf-8'), indent=1, default=str)
if __name__ == '__main__':
    print(collections.Counter(v['final'] for v in ROW.values()))
    print(sorted(collections.Counter((v['census_tranche'], v['final']) for v in ROW.values()).items()))
