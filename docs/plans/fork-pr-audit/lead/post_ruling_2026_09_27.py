"""Ace ruling 2026-09-27 00:39 PT (msg 1553672387516440636, review page calm-ember-1f75.docs.ace), card t_f29230a1.

Flips DROP rows to KEEP in lead/rows.json where the slice worker falsified the DROP premise. Idempotent:
a row already flipped is left alone; a row in any other state aborts. Prints verdict totals before/after.
Re-render FINAL.md / ROLLUP.md with lead/render.py afterwards.
"""
import collections, json, os, sys

L = os.path.dirname(os.path.abspath(__file__)) + '/'
TAG = 'ACE RULING 2026-09-27 (DROP -> KEEP, premise falsified by the slice worker): '
FLIPS = {
    'nopr:4ed79b6dad': ('t_2e862bc6', 'the root config and 12 profile configs (13) set telegram network_retry_max: 20 / '
                        'network_retry_max_delay: 120 and fork/main passes them to the adapter; a revert silently drops every '
                        'profile to upstream\'s hardcoded 10 retries / 60s cap.'),
    '#109': ('t_6b6df0eb', '#164 renamed the A-floor to _signature_partition, not removed it; live on origin/main '
             '(compaction_stats.py:959-982), fired 6x in prod logs 09-19..25, and #106 (KEEP) depends on it.'),
    '#932': ('t_7bc9ce6e', '#954 (merged after the audit) imports ci_overflow_acceptance from ci_overflow_integration.py:30; '
             'the rebased revert fails with ModuleNotFoundError (reproduced). Branch audit/scripts_misc/revert-ci-overflow-phase0 '
             'must not merge.'),
    '#562': ('t_7ea0fe09', 'structural base of merged #566/#568/#682 (resolve_wake_participant / _live_chat_participants); '
             'git revert conflicts, and #682 records a field-observed phantom session this path prevents.'),
    '#589': ('t_5d546222', 'follows #562: #589 only single-sources the creator-stamp rule of the #562/#568 stack (slice worker '
             'caveat). Branch audit/gateway/revert-589 (c2c77036a7) must not merge.'),
    'nopr:36134d8944': ('t_b865b149', 'PR #1200 (the revert) went CI-red: af43fd64ff test_cron_auto_model relies on the '
                        'gpt-5.5 -> openai-codex alias (4 failures). Close #1200 unmerged; branch '
                        'audit/hermes_cli/revert-36134d8944 must not merge.'),
    'nopr:051c2076e1': ('t_c335830a', 'a full revert corrupts FTS (probe on a copy of shopper lcm.db: "database disk image is '
                        'malformed"); the commit\'s drop-then-recreate-on-missing keeps trigger order correct, so reverting '
                        'would ship the drifted trigger spec to new DBs.'),
    '#22': ('t_8ff394b6', 'apollo@daemonarchy.local entry only: not stale, Apollo committed under it on 2026-09-22 and PR #872\'s '
            'attribution check passed only via this mapping. The 2 nopr:40a6040932 lines stay DROP on '
            'audit/scripts_misc/revert-author-map-stale @4053ee9081.'),
}


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
