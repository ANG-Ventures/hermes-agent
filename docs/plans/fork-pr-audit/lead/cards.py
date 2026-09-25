"""Group FINAL DROP/UPSTREAM rows into slice cards -> lead/cards.json."""
import sys, os, re, json, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *

ROW = json.load(open(L + 'rows.json'))
REG = json.loads(sh('git show origin/main:docs/sync/fork-features.json'))
GOD = {'gateway/run.py', 'agent/chat_completion_helpers.py', 'agent/agent_runtime_helpers.py', 'hermes_cli/commands.py',
       'cli.py', 'gateway/session.py', 'agent/conversation_loop.py', 'plugins/platforms/discord/adapter.py'}

UNITS = [
    ('undo-redo', ['#49', '#353', '#339', '#65', 'nopr:14b186d6fa', 'nopr:17910311e6']),
    ('moa-messaging-toolsets', ['nopr:f504b1c928', 'nopr:091b3f915d', 'nopr:7e505547a7']),
    ('lcm-armab-campaign', ['nopr:0c96621b85', 'nopr:15e1d95f14', 'nopr:1f8392335e', 'nopr:9f0c416088',
                            'nopr:a3fa06a6e4', 'nopr:df420d15df', 'nopr:fb3008a7ba']),
    ('lcm-k2-campaign', ['nopr:834f0a0111', 'nopr:fdee6d93f2']),
    ('gemini-bridge-pricing', ['#207', '#208', '#312']),
    ('delegation-outbox-ack', ['#683', '#687']),
    ('session-search-title-lane', ['#257', '#303']),
    ('boomerang', ['#203', '#204']),
]


def branches(v):
    return re.findall(r'audit/[\w\-/.]+[\w]', str((v.get('x') or {}).get('branch') or ''))


def main():
    card_rows = {k: v for k, v in ROW.items() if v['final'] in ('DROP', 'UPSTREAM') and not v.get('follows')}
    assigned = {}
    groups = []
    for name, ks in UNITS:
        ks = [k for k in ks if k in card_rows]
        if ks:
            groups.append((name, ks)); assigned.update({k: name for k in ks})
    desk = [k for k, v in card_rows.items() if v['census_tranche'] == 'auto-desktop-retired']
    groups.append(('desktop-retired', desk)); assigned.update({k: 'desktop-retired' for k in desk})
    bybr = collections.defaultdict(list)
    for k, v in card_rows.items():
        if k in assigned:
            continue
        bs = branches(v)
        # only group on a shared branch within the same tranche and the same verdict
        bybr[(v['tranche'], v['final'], bs[0] if bs else 'nobranch:' + k)].append(k)
    for (t, fv, b), ks in bybr.items():
        groups.append((b if not b.startswith('nobranch:') else ks[0], ks))
    followers = collections.defaultdict(list)
    for k, v in ROW.items():
        if v.get('follows') and v['final'] in ('DROP', 'UPSTREAM'):
            followers[v['follows']].append(k)
    cards = []
    for name, ks in groups:
        vs = [ROW[k] for k in ks]
        fv = collections.Counter(v['final'] for v in vs).most_common(1)[0][0]
        tr = sorted({v['census_tranche'] for v in vs})
        fol = sorted(f for k in ks for f in followers.get(k, []))
        paths = sorted({p for k in ks + fol for p in ROW[k]['paths']})
        cs = max(ROW[k]['conflict_syncs'] or 0 for k in ks + fol)
        regs = []
        for i, e in enumerate(REG):
            ep = set(e.get('paths') or []) - GOD
            if ep & set(paths):
                regs.append(f"entry {i} '{e['feature'][:70]}' lifecycle={e['lifecycle']}")
        brs = sorted({b for k in ks for b in branches(ROW[k])})
        cards.append({'name': name, 'verdict': fv, 'keys': ks, 'followers': fol, 'tranches': tr, 'branches': brs,
                      'conflict_syncs': cs, 'registry': regs,
                      'assignee': 'daedalus-opus' if (fv == 'DROP' and cs >= 2) else 'daedalus'})
    json.dump(cards, open(L + 'cards.json', 'w'), indent=1)
    return cards


if __name__ == '__main__':
    c = main()
    print(len(c), collections.Counter(x['verdict'] for x in c), collections.Counter(x['assignee'] for x in c))
    print(sum(len(x['keys']) for x in c), 'keys;', sum(len(x['followers']) for x in c), 'auto followers')
    for x in c:
        if len(x['keys']) > 1:
            print(x['name'], x['verdict'], x['keys'][:8], len(x['keys']))
