import os, re, json, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
import measure, rulings

VALID = ('KEEP', 'DROP', 'UPSTREAM', 'SUPERSEDED-BY-UPSTREAM', 'UNRESOLVED')


def norm(v):
    s = str(v or '').upper()
    for c in ('SUPERSEDED-BY-UPSTREAM', 'UPSTREAM', 'KEEP', 'DROP', 'UNRESOLVED'):
        if s.startswith(c):
            return c
    return None


def rule(t, k, x, VK):
    v = x.get('verdict')
    a = x.get('adversary') or {}
    r = advres(x)
    if r == 'overturned':
        return norm(a.get('new_verdict')), 'adversary overturned: ' + str(a.get('evidence'))[:300], 'overturned'
    if r == 'needs-lead':
        fv, why = rulings.NEEDS_LEAD[k]
        return fv, why, 'needs-lead'
    if r == 'keep-unproven':
        if k in rulings.KU_OVERRIDE:
            fv, why = rulings.KU_OVERRIDE[k]
            return fv, why, 'keep-unproven'
        ok, why = measure.measured(x, VK)
        if ok:
            return v if v in ('KEEP', 'UPSTREAM') else 'KEEP', 'LEAD measured: ' + why, 'keep-unproven'
        return 'DROP', 'LEAD: KEEP-UNPROVEN with ' + why + ' -> DROP per rule (unmeasured "still needed" is not a KEEP).', 'keep-unproven'
    if r == 'stands':
        return v, 'adversary: stands', 'stands'
    return v, 'unchallenged by adversary (KEEP/UPSTREAM rows are only challenged when flagged)', 'none'


def build():
    M = load_merged()
    VK = {k: x.get('verdict') for t in T for k, x in M[t].items()}
    out = {}
    for t in T:
        for k, x in M[t].items():
            fv, why, adv = rule(t, k, x, VK)
            assert fv in VALID, (t, k, fv)
            out[k] = {'tranche': t, 'auditor': x.get('verdict'), 'adv': adv,
                      'adv_new': (x.get('adversary') or {}).get('new_verdict'), 'final': fv, 'why': why, 'x': x}
    for k, (card, ev) in rulings.POST_RULING_2026_09_27.items():
        assert out[k]['final'] == 'DROP', (k, out[k]['final'])  # the ruling flips DROP rows only
        out[k]['final'], out[k]['why'] = 'KEEP', rulings.POST_RULING_TAG + ev + f' (slice card {card})'
    for k, x in rulings.UNRESOLVED_RULINGS.items():
        if k not in out:  # auto rows follow their code row in build_all.py
            continue
        assert out[k]['final'] == 'UNRESOLVED', (k, out[k]['final'])  # the ruling resolves UNRESOLVED rows only
        assert x['verdict'] in VALID and x['verdict'] != 'UNRESOLVED', (k, x['verdict'])
        out[k]['final'], out[k]['why'] = x['verdict'], rulings.unresolved_why(x, str(out[k]['why'] or ''))
    return out
