"""Ace ruling 2026-09-27 00:39 PT (msg 1553672387516440636, review page calm-ember-1f75.docs.ace), card t_f29230a1.

Flips DROP rows to KEEP in lead/rows.json where the slice worker falsified the DROP premise. Idempotent:
a row already flipped is left alone; a row in any other state aborts. Prints verdict totals before/after.
Re-render FINAL.md / ROLLUP.md with lead/render.py afterwards.

Superseded as the source of truth (t_04cd162a): the table lives in rulings.POST_RULING_2026_09_27 and final.build()
applies it, so lead/build_all.py reproduces these rows. On a rebuilt rows.json this script is a no-op.
"""
import collections, json, os, sys

L = os.path.dirname(os.path.abspath(__file__)) + '/'
from rulings import POST_RULING_TAG as TAG, POST_RULING_2026_09_27 as FLIPS  # canonical table; final.build() applies it


def _scalar(line):
    try:
        return json.loads('{' + line.strip().rstrip(',') + '}')
    except ValueError:
        return None


def write_preserving(rows):
    """Dump rows.json but keep every old line whose decoded value is unchanged.

    rows.json carries hand escapes (e.g. a JSON \\u003a that keeps a gitleaks generic-api-key rule quiet);
    a plain json.dump would decode them.
    """
    old = open(L + 'rows.json', encoding='utf-8').read().split('\n')
    new = json.dumps(rows, indent=1, default=str).split('\n')
    assert len(old) == len(new), (len(old), len(new))
    out = [o if o == n or (_scalar(o) is not None and _scalar(o) == _scalar(n)) else n for o, n in zip(old, new)]
    open(L + 'rows.json', 'w', encoding='utf-8').write('\n'.join(out))


def main():
    rows = json.load(open(L + 'rows.json', encoding='utf-8'))
    before = collections.Counter(v['final'] for v in rows.values())
    for k, (card, ev) in FLIPS.items():
        v = rows[k]
        why = TAG + ev + f' (slice card {card})'
        if v['final'] == 'KEEP' and v['why'] == why:
            continue
        assert v['final'] == 'DROP', (k, v['final'])
        v['final'], v['why'] = 'KEEP', why
    write_preserving(rows)
    after = collections.Counter(v['final'] for v in rows.values())
    assert sum(after.values()) == 1116
    print('before', dict(before))
    print('after ', dict(after))
    print('flipped', len(FLIPS), 'KEEP', before['KEEP'], '->', after['KEEP'], 'DROP', before['DROP'], '->', after['DROP'])


if __name__ == '__main__':
    sys.exit(main())
