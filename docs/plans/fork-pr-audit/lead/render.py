"""Render FINAL.md + ROLLUP.md from lead/rows.json + lead/cards.json (+ lead/card_ids.json once cards exist)."""
import sys, os, re, json, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *

ROW = json.load(open(L + 'rows.json', encoding='utf-8'))
CARDS = json.load(open(L + 'cards.json', encoding='utf-8'))
IDS = json.load(open(L + 'card_ids.json', encoding='utf-8')) if os.path.exists(L + 'card_ids.json') else {}
REG = json.loads(sh('git show origin/main:docs/sync/fork-features.json'))
key2card = {}
for c in CARDS:
    for k in c['keys'] + c['followers']:
        key2card[k] = IDS.get(c['name'], 'card:' + c['name'])
SPLIT = json.load(open(L + 'split_ids.json', encoding='utf-8')) if os.path.exists(L + 'split_ids.json') else {}
key2card.update(SPLIT)
ORDER = ['gateway', 'agent', 'hermes_cli', 'plugins', 'cron+tools', 'scripts+misc', 'auto', 'auto-cherry-pick', 'auto-desktop-retired']
VS = ['KEEP', 'UPSTREAM', 'SUPERSEDED-BY-UPSTREAM', 'DROP', 'UNRESOLVED']


def cell(s, n=220):
    s = re.sub(r'\s+', ' ', str(s if s is not None else '')).replace('|', '/')
    return s if len(s) <= n else s[:n - 1] + '…'


def row_line(k, v):
    x = v.get('x') or {}
    e = x.get('evidence') or {}
    prob = cell((x.get('problem') or v['subject']) + (' — orig: ' + str(e.get('original')) if e.get('original') else ''), 200)
    adv = v['adv'] if v['adv'] not in ('none',) else '—'
    if v.get('adv_new') and v['adv'] in ('overturned', 'needs-lead'):
        adv += ' → ' + cell(v['adv_new'], 40)
    fin = v['final'] + ((' — ' + cell(v['why'], 260)) if v['why'] and not v['why'].startswith('unchallenged') and v['why'] != 'adversary: stands' else '')
    br = ', '.join(re.findall(r'audit/[\w\-/.]+[\w]', str(x.get('branch') or ''))) or '—'
    cost = f"{v['loc']} loc, {len(v['conflict_files'])} cf ({v['conflict_syncs']})"
    return '| ' + ' | '.join([k, v['census_tranche'], prob, cell(e.get('upstream_state'), 140), cell(e.get('upstream_fix'), 60),
                             cell(e.get('fires'), 140), cost, str(v['auditor']), adv, fin, br,
                             key2card.get(k, '—')]) + ' |'


def final_md():
    out = ['# Fork-PR audit — FINAL table (lead t_03e35f0e)', '',
           'All 1,116 census rows (`census/census_v2.json`), one row each (asserted by `lead/build_all.py`: every census key exactly once).',
           'FINAL = adversary `new_verdict` when overturned; auditor verdict when stands/unchallenged; the LEAD ruling (reason inline) for '
           'needs-lead, KEEP-UNPROVEN and auditor-UNRESOLVED rows. `auto` rows follow the code row that owns their paths; '
           '`auto-desktop-retired` DROP by D9; `auto-cherry-pick` SUPERSEDED (cron+tools auditor #585 spot-check).', '',
           'Inputs: auditor branches `audit/<tranche>/verdicts` (latest, incl. post-adversary branch passes) + adversary annotations from '
           '`audit/<tranche>/adversary`. Where an auditor branch pass changed a verdict after the adversary forked (gateway #817/#868/#970 '
           'UPSTREAM→SUPERSEDED, t_32ef8a2c) the later auditor verdict is used.', '',
           '## Lead method',
           '- **KEEP-UNPROVEN (199 flags)**: kept only with a measurement found in the row evidence (positive fire count, executed upstream RED, '
           'live config knob / job store, registry entry, or KEEP-by-dependency = a measured KEEP row builds on it), by family ruling '
           '(route-announce = registry 23/24, footer = registry 22 + live runtime_footer fields, compaction-announce = registry 3 + '
           'announce_on_hygiene, restart/resume followers of measured heads), or by an explicit lead measurement; everything else → DROP. '
           'Classifier = `lead/measure.py`, overrides = `lead/rulings.py`. Classifier false positives caught and overridden: #207/#226 '
           '(config line numbers read as fires).',
           '- **Spot-check (step 2)**: 102 rows sampled (≥15% of each tranche\'s KEEPs and DROPs, seed 3) PLUS a 100% mechanical pass: every '
           'cited upstream sha (110) resolved and is an ancestor of upstream/main; every cited branch (132) exists on origin except '
           '2 D1a LCM branches that live on Kyzcreig/hermes-lcm (verified by ls-remote: upstream-902 d3c7af1, upstream-dc4245ab4c 15590f0). '
           '72 rows had commit-message-only `original`; of those the KEEPs that were unchallenged (3) all carry an independent measurement '
           '(fires/config), so none re-opened. DROP branches whose notes lack test output: listed in ROLLUP §branches.',
           '- **Not re-run by the lead**: auditor/adversary pytest outputs are quoted, not replayed. The lead ran one narrow suite '
           '(desktop branch, 152 passed) and read-only DB/config probes (state.db desktop_resume_* = 0 rows; kanban.db stale_quantity = 0).', '',
           '| key | tranche | problem + evidence | upstream RED/GREEN | upstream fix | fires (window) | cost | auditor | adversary | FINAL verdict | branch | card |',
           '|---|---|---|---|---|---|---|---|---|---|---|---|']
    for t in ORDER:
        for k in sorted([k for k, v in ROW.items() if v['census_tranche'] == t], key=lambda k: -ROW[k]['loc']):
            out.append(row_line(k, ROW[k]))
    out += ['', '## Conflicts — every auditor↔adversary disagreement and the lead ruling', '',
            '| key | tranche | auditor | adversary | FINAL | ruling |', '|---|---|---|---|---|---|']
    for k, v in ROW.items():
        if v['adv'] in ('overturned', 'needs-lead') or (v['adv'] == 'keep-unproven' and v['final'] != v['auditor']):
            out.append('| ' + ' | '.join([k, v['census_tranche'], str(v['auditor']), v['adv'] + (' → ' + cell(v['adv_new'], 50) if v.get('adv_new') else ''),
                                          v['final'], cell(v['why'], 400)]) + ' |')
    return '\n'.join(out) + '\n'


def ledger_sets():
    led = {}
    for f in ['docs/sync/review/RESOLUTION-LEDGER.md', 'docs/sync/review/RESOLUTION-LEDGER-20260807.md', 'docs/sync/review/RESOLUTION-LEDGER-2026-08-29.md']:
        txt = sh(f'git show origin/main:{f}')
        led[f.split('/')[-1]] = txt
    return led


def rollup_md():
    c = collections.Counter((v['census_tranche'], v['final']) for v in ROW.values())
    out = ['# Fork-PR audit — ROLLUP (lead t_03e35f0e)', '', '## 1. Verdicts per tranche', '',
           '| tranche | ' + ' | '.join(VS) + ' | total |', '|---' * (len(VS) + 2) + '|']
    for t in ORDER:
        out.append(f'| {t} | ' + ' | '.join(str(c[(t, s)]) for s in VS) + f' | {sum(c[(t, s)] for s in VS)} |')
    tot = collections.Counter(v['final'] for v in ROW.values())
    out.append('| **all** | ' + ' | '.join(f'**{tot[s]}**' for s in VS) + f' | **{sum(tot.values())}** |')
    # lines
    loc = collections.Counter()
    for v in ROW.values():
        loc[v['final']] += v['loc'] or 0
    alloc = sum(loc.values())
    out += ['', '## 2. Fork lines that DROP / SUPERSEDED would delete', '',
            f'Census `loc` (add+del of the original PR, an upper bound — later rows edit the same lines): DROP {loc["DROP"]:,}, '
            f'SUPERSEDED-BY-UPSTREAM {loc["SUPERSEDED-BY-UPSTREAM"]:,}, UPSTREAM (deleted once merged upstream) {loc["UPSTREAM"]:,}, '
            f'KEEP {loc["KEEP"]:,}, UNRESOLVED {loc["UNRESOLVED"]:,}; all rows {alloc:,}.', '',
            'Real `git diff --shortstat <merge-base> <branch>` of every revert branch on origin (lead run 2026-09-25):', '',
            '| branch | shortstat | status after adversary/lead |', '|---|---|---|']
    stats = dict(l.split('|', 1) for l in open(L + 'revert_stats.txt', encoding='utf-8').read().splitlines() if '|' in l)
    b2k = collections.defaultdict(list)
    for k, v in ROW.items():
        for b in re.findall(r'audit/[\w\-/.]+[\w]', str((v.get('x') or {}).get('branch') or '')):
            b2k[b].append(k)
    for b, s in stats.items():
        bb = b.replace('origin/', '')
        ks = b2k.get(bb, [])
        fin = collections.Counter(ROW[k]['final'] for k in ks)
        live = 'LIVE' if fin.get('DROP') else ('VOID (rows now ' + ', '.join(f'{a}×{n}' for a, n in fin.items()) + ')' if fin else 'no row cites it')
        extra = ''
        if 'lcm-campaign-harness' in bb or 'oneshot-probes' in bb:
            extra = ' — REBUILD: drops files the adversary/lead kept (see FINAL conflicts)'
        if 'desktop-retired' in bb:
            extra = ' — includes upstream desktop drift since the 08-07 merge-base (D9 takes it wholesale); fork-only part = 37 rows'
        out.append(f'| `{bb}` | {s.strip()} | {live}{extra} |')
    # upstream PRs
    out += ['', '## 3. Upstream PRs to open (one slice card each)', '', '| card | keys | branch(es) |', '|---|---|---|']
    for cd in CARDS:
        if cd['verdict'] == 'UPSTREAM':
            out.append(f"| {IDS.get(cd['name'], cd['name'])} | {', '.join(cd['keys'])} | {', '.join(b for b in cd['branches'] if '/revert-' not in b) or 'to build'} |")
    # conflicts
    led = ledger_sets()
    out += ['', '## 4. Expected reduction in parity-merge conflicts', '',
            'Census `conflict_files` is file-level (the PR touches a file named in a sync ledger). A ledger file stops costing a '
            'conflict only if NO surviving (KEEP / UPSTREAM-until-merged / UNRESOLVED) row still touches it. Both views:', '',
            '| ledger | ledger files touched by fork rows | touched by DROP/SUPERSEDED rows | freed (no surviving row touches it) |', '|---|---|---|---|']
    surv = set(p for v in ROW.values() if v['final'] in ('KEEP', 'UPSTREAM', 'UNRESOLVED') for p in v['conflict_files'])
    gone = set(p for v in ROW.values() if v['final'] in ('DROP', 'SUPERSEDED-BY-UPSTREAM') for p in v['conflict_files'])
    for name, txt in led.items():
        allf = set(p for v in ROW.values() for p in v['conflict_files'] if p in txt)
        g = {p for p in gone if p in txt}
        fr = {p for p in g if p not in surv}
        out.append(f'| {name} | {len(allf)} | {len(g)} | {len(fr)} |')
    rowsc = sum(1 for v in ROW.values() if v['conflict_files'])
    rowsd = sum(1 for v in ROW.values() if v['conflict_files'] and v['final'] in ('DROP', 'SUPERSEDED-BY-UPSTREAM'))
    cfd = sum(len(v['conflict_files']) for v in ROW.values() if v['final'] in ('DROP', 'SUPERSEDED-BY-UPSTREAM'))
    cft = sum(len(v['conflict_files']) for v in ROW.values())
    out += ['', f'Row view: {rowsd}/{rowsc} ledger-touching rows are DROP/SUPERSEDED; they hold {cfd:,}/{cft:,} ({100*cfd/cft:.0f}%) of all '
            f'row×conflict-file incidences. Freed files (all ledgers, deduped): {len(gone - surv)} — '
            + ', '.join(sorted(gone - surv)[:40]) + '. The god files (gateway/run.py, agent/*helpers, hermes_state.py, cron/scheduler.py) '
            'stay conflicted: KEEP rows still touch them, so the win there is fewer hunks, not fewer files.']
    # registry
    out += ['', '## 5. D2b registry write-backs owed (docs/sync/fork-features.json)', '',
            '- entry 14 /undo+/redo fork-permanent → **retire** if Ace approves the ACE-GATED DROP (6 rows, card undo-redo).',
            '- entry 16 /merge fork-permanent → **retire** if Ace approves #231; entry 15 /branch stays (Discord thread half).',
            '- entry 4 messaging+MoA toolsets fork-permanent → **retire** if Ace approves moa-messaging-toolsets; then #342 and the MoA half of #787 go with it.',
            '- entry 11 tool_gate (upstream-intended) → golden update: #37 grace branch removed (7 grace cases).',
            '- entry 12 state ext / platform session search → set `upstream_ref` when #257/#303 is filed.',
            '- entries 22/23/24 (footer / route announce / reasoning announce, upstream-intended) → still outstanding; 90+ gateway KEEP rows ride them. Filing them upstream is the largest remaining burden cut.',
            '- NEW entries owed (KEEP rows with no registry entry, D2b): fork kanban subsystem (hermes_cli auditor: every measured fork-kanban KEEP), '
            'blackbox plugin (plugins auditor), cron per-job model pin #411 and approvals-mode-off predicate #786 (cron+tools adversary), '
            'Discord thread auto_archive 10080 #710.',
            '- entry 9 restart policy: F1/F2 unit kept; mark the F2 breaker as a trim candidate (0 decisions in 544 logs).']
    # three lines
    keepk = [v for v in ROW.values() if v['final'] == 'KEEP']
    out += ['', '## 6. The three answers Ace asked for', '',
            f'**What still provides value.** {tot["KEEP"]} rows ({loc["KEEP"]:,} loc) KEEP on a measurement: the restart/resume/admission '
            'family (boot_resume_scheduled 1,190, dropbox_resume 1,410, turn_slot_acquire 2,208, restart_notice 455 fires), blackbox cost '
            'accounting (19k–47k turns/api-calls priced), relay-lane headers/pricing (16,913 bpx/bpr calls/7d), the footer and route/compaction '
            'announce families (registry 3/22/23/24, live config), Discord restart backfill (425 re-injected messages), cron fallback/pins '
            '(17 opt-in jobs, 521-job stores). Plus 93 UPSTREAM rows that are needed AND generic — value that should stop being ours.',
            '',
            f'**What solves problems that no longer exist.** {tot["SUPERSEDED-BY-UPSTREAM"]} rows are fixed upstream (take theirs at the next sync) '
            f'and {tot["DROP"]} rows DROP: never fired in the window, dormant surfaces (desktop D9 ×37, gemini/yunwu lanes, MoA/send_message '
            'tools dormant since July, /undo 2 uses, /merge 0), one-shot June LCM campaign harnesses, and a write-only reaction journal. '
            f'{tot["UNRESOLVED"]} rows stay UNRESOLVED (silent guards with no log/DB signal — need a canary, not a guess).',
            '',
            f'**What the maintenance burden actually is (measured).** {alloc:,} fork lines across 1,116 rows; 795 rows sit on files named in the '
            f'sync ledgers and 458 on files that conflicted in all three syncs. DROP+SUPERSEDED rows carry {cfd:,} of {cft:,} ({100*cfd/cft:.0f}%) '
            f'row×conflict-file incidences and {loc["DROP"]+loc["SUPERSEDED-BY-UPSTREAM"]:,} census loc; but only {len(gone - surv)} ledger files '
            'are fully freed because KEEP rows still sit on the god files. The real reduction comes in two steps: (1) this audit\'s '
            'DROP/SUPERSEDED (fewer hunks per sync), (2) landing the UPSTREAM rows + registry 22/23/24, which is what empties gateway/run.py of fork hunks.']
    out += ['', '## 7. Open prerequisites before the sync takes upstream', '',
            '- mem0: migrate all 9 `mem0.json` from `admin_api_key` to `api_key` before taking upstream SelfHostedBackend (nopr:8b332be03c).',
            '- skew family (8 agent rows): one change with plugins/context_engine/lcm engine.py + drop `compression.skew_floor` from 4 configs.',
            '- #225: re-point the cron consumer; delete `model.announce_recovery` from 13 configs.',
            '- #351 revert must not land before the sync takes tests/home_io_guard.py (cron+tools adversary).',
            '- #88 dependent: tools/terminal_tool.py:179-181 uses is_cron_session(); rewire at sync.',
            '- Skill edits owed with DROPs: lcm-context-engine SKILL.md:874/902, hermes-client-source-attribution, livesync-eyes-on-regression '
            '(#268/#272), trivial-pr-fast-path + greploop (#151), blackbox-turn-telemetry perclass ref (#87/#105).',
            '- Ref-namespace note: `audit/final` (this docs branch) blocks a ref named `audit/final/…`; the desktop revert is `audit/final-revert-desktop-retired`.']
    return '\n'.join(out) + '\n'


if __name__ == '__main__':
    open(D + 'FINAL.md', 'w', encoding='utf-8').write(final_md())
    open(D + 'ROLLUP.md', 'w', encoding='utf-8').write(rollup_md())
    txt = open(D + 'FINAL.md', encoding='utf-8').read()
    keys = [r['key'] for r in census()]
    body = txt.split('## Conflicts')[0]
    lines = [l for l in body.splitlines() if l.startswith('| ') and not l.startswith('| key')]
    got = [l.split(' | ')[0][2:] for l in lines]
    cnt = collections.Counter(got)
    assert len(got) == 1116 and set(got) == set(keys) and max(cnt.values()) == 1, (len(got), len(set(got)))
    print('FINAL rows', len(got), 'unique', len(set(got)), 'census', len(keys), 'OK')
