"""Lead rulings (t_03e35f0e). Every entry: key -> (FINAL verdict, reason). Recorded verbatim in FINAL.md."""

ACE14 = ('DROP', 'LEAD: measurement supports the drop (/undo 2, /redo 0 native invocations 2026-05-10..09-25) but registry '
         'entry 14 is lifecycle=fork-permanent (Ace ruling). DROP is ACE-GATED: the slice card must not merge until Ace '
         'retires entry 14 (D2b write-back: entry 14 -> retired). Revert unit = audit/gateway/revert-49-undo; must also '
         'remove tui_gateway/server.py _undo/_redo_session_core + callers.')
ACE4 = ('DROP', 'LEAD: dormant (send_message last tool call 2026-06-23, mixture_of_agents 2026-07-10, 0 since, all 14 '
        'profile state.dbs) but registry entry 4 is fork-permanent. DROP is ACE-GATED on retiring entry 4; '
        'branch audit/cron_tools/revert-f504b1c928 (+3/-735, 70 passed 6 skipped). If Ace keeps entry 4, row -> KEEP.')
SKEW = ('SUPERSEDED-BY-UPSTREAM', 'LEAD: auditor D3 case accepted (usage anchor makes skew calibration moot on anchored '
        'turns; upstream compression thresholds serve). Execute as ONE cross-tranche change with plugins/context_engine/lcm '
        '(engine.py consumes should_compress_calibrated) and drop compression.skew_floor from the 4 configs in the same '
        'change. No slice card (SUPERSEDED rows ride the next parity sync).')
ARMAB = ('DROP', 'LEAD: one-shot June N=180 campaign runner, 0 invocations in 60d (state.db tool calls). DROP together with '
         'the skill edit: lcm-context-engine SKILL.md:874,902 and ~/.hermes/scripts/lcm-arm-a-autofire-watch.sh:21 must '
         'stop naming lcm_armab_campaign.sh. revert-lcm-campaign-harness must be REBUILT without the skill-prescribed gate '
         'tooling (lcm_qa_battery / lcm_live_recovery / arm_b, adversary-overturned to KEEP) and without files co-owned '
         'by plugins #168/#480/nopr:efe50db808.')
K2 = ('DROP', 'LEAD: DROP both halves together: scripts/lcm_k2_disambig_campaign.sh AND its unscheduled external callers '
      '~/.hermes/scripts/lcm_k2_autofire.sh + lcm_k2_autofire_net.sh (0 cron/launchd entries). Fleet-script half is an '
      'ops edit outside the repo, listed on the card.')
F12 = ('KEEP', 'LEAD: F1/F2 restart-breaker unit. Knobs that exist only for this unit are set in live config '
       '(restart_initiated_ttl_secs 600, restart_loop_threshold 3, restart_loop_window_secs 300 at ~/.hermes/config.yaml:75-77) '
       'and bridged by gateway/fork_ext/restart_policy.py = registry entry 9 (golden). F1 is part of #295 self-restart '
       'contract (measured, 1,190 fires). F2 breaker: 0 decisions in 544 log files -> trim candidate, recorded as a '
       'follow-up, not a revert (golden must be regenerated with it).')

NEEDS_LEAD = {
    # gateway
    '#315': ('KEEP', 'LEAD: route-identity symbols consumed outside the PR (chat_model_pins = registry 0, route_identity = registry 10, '
             '/fast = registry 17 fork-permanent), 0 hits upstream; revert breaks chat-model pins. Sticky-reset knob alone is a trim candidate.'),
    '#49': ACE14, '#353': ACE14, '#339': ACE14,
    '#356': ('UNRESOLVED', 'LEAD: removed from the #49 revert unit. The empty-resume guard is not undo-only: upstream synthesizes an '
             'empty-text internal resume event (up gateway/run_startup.py:611) with no empty-row guard. Needs a repro on upstream '
             'before either DROP or UPSTREAM; unmeasured either way.'),
    '#221': ('SUPERSEDED-BY-UPSTREAM', 'LEAD: /branch half -> take upstream (dbcbd9d9db, ancestor verified by adversary). The /merge '
             'half is carried by #231 (registry 16), ruled separately.'),
    '#231': ('DROP', 'LEAD: /merge 0 successful uses in window; registry entry 16 fork-permanent -> ACE-GATED DROP (write-back: entry 16 -> retired).'),
    '#229': ('DROP', 'LEAD: flag desktop_auto_resume is on (config.yaml:371, aegis:323) but the feature has produced no state: '
             'state.db desktop_resume_markers = 0 rows, desktop_resume_breakers = 0 rows (read-only, 2026-09-25). On a retired '
             'desktop surface (D9). DROP stands; branch audit/gateway/revert-229 (d96e727829, 22+160 passed); remove the config key with it.'),
    '#129': ('DROP', 'LEAD: journal is written (reactions.jsonl seq 40092) but has NO reader anywhere (adversary grep): write-only '
             'output is not value. DROP stands; branch audit/gateway/revert-129; the card must also delete discord.reaction_journal '
             'from ~/.hermes/config.yaml:567 (ops edit) and stop the file growth.'),
    '#70': F12, '#72': F12, '#80': F12, '#86': F12, '#7536': F12, 'nopr:0f64a3653b': F12,
    '#97': ('SUPERSEDED-BY-UPSTREAM', 'LEAD: take upstream for the run.py env clobber; KEEP tools.kanban_tools._current_session_id '
            '(imported by hermes_cli/kanban_identity.py:48, fork-only) when the sync takes upstream.'),
    '#358': ('UNRESOLVED', 'LEAD: auditor premise falsified (upstream prompt.submit has no empty-text check); fork guard fires '
             'unmeasured. Needs a count of 4020 rejections before KEEP/UPSTREAM.'),
    # agent
    '#1000': ('KEEP', 'LEAD: KEEP the page half (infra-labelled verdict + #alerts page on a missing hook script fired on the '
              'real 2026-09-25 incident, run 10180); the restore half is dead on the fleet layout (hooks-installed untracked) '
              '-> trim follow-up, not a revert.'),
    '#111': SKEW, '#392': SKEW, '#529': SKEW, '#539': SKEW, '#541': SKEW, '#554': SKEW,
    'nopr:1921a02047': SKEW, 'nopr:a7c5c51156': SKEW,
    '#225': ('SUPERSEDED-BY-UPSTREAM', 'LEAD: upstream announces restore unconditionally (= announce_recovery: true). Take upstream '
             'at sync; re-point the cron consumer and delete model.announce_recovery from 13 configs in the same change.'),
    '#257': ('UPSTREAM', 'LEAD: D9 premise fails - web dashboard web/src/lib/api.ts:829 calls /api/sessions/search, upstream search '
             'has no title/platform lane; registry entry 12 (upstream-intended). Generic -> UPSTREAM, not DROP.'),
    '#303': ('UPSTREAM', 'LEAD: rides #257 (same server half, _PLATFORM_SEARCH_ALIASES live).'),
    '#342': ('KEEP', 'LEAD: KEEP-by-dependency: #787 (KEEP) consumes the per-advisor pricing seam. Leaves WITH the MoA toolset if '
             'Ace retires registry entry 4 (see nopr:f504b1c928).'),
    '#37': ('DROP', 'LEAD: 0 grace-turn denials in 555,617 turn_tool_calls (06-11..09-25). DROP, but the revert must cut the grace '
            'branch out of agent/fork_ext/tool_gate.py (registry 11) and regenerate tests/golden/tool_gate (7 grace cases).'),
    '#65': ACE14, 'nopr:14b186d6fa': ACE14, 'nopr:17910311e6': ACE14,
    '#99': ('KEEP', 'LEAD: KEEP-by-dependency: standalone revert breaks tests of KEEP rows #100/#101/#406; drop is a no-op unless '
            'that family is re-cut.'),
    # hermes_cli
    '#313': ('KEEP', 'LEAD: fork agent/background_review.py:1590 arms the ContextVar whitelist this row added; standalone revert '
             're-opens the leak. Converges only with a background_review take-upstream.'),
    # plugins
    '#107': ('KEEP', 'LEAD: ledger DD-2 wins: keep the timestamp-provenance columns (977,334 superseded_by rows in lcm.db read by '
             'the fork filter). Split-then-drop of the dedup column is a follow-up authored change, not a revert.'),
    'nopr:8b332be03c': ('SUPERSEDED-BY-UPSTREAM', 'LEAD: take upstream SelfHostedBackend ONLY after migrating all 9 mem0.json '
                        'from admin_api_key to api_key (else 401). Migration is a prerequisite of the sync, recorded in ROLLUP.'),
    # cron_tools
    'nopr:f504b1c928': ACE4, 'nopr:091b3f915d': ACE4, 'nopr:7e505547a7': ACE4,
    # scripts_misc
    '#188': ('DROP', 'LEAD: dashboard platform = 4 turns ever, last 2026-07-04 (83 days dormant). DROP + skill edit '
             '(hermes-client-source-attribution SKILL.md:61-83,121 and its reference test).'),
    '#612': ('KEEP', 'LEAD: scripts/certify/dashboard_loop_certify.py is named by dashboard-eventloop-starvation-triage as THE '
             'hardened harness and is co-owned by gateway #616. KEEP; revert-oneshot-probes must be rebuilt without it.'),
    'nopr:0c96621b85': ARMAB, 'nopr:15e1d95f14': ARMAB, 'nopr:1f8392335e': ARMAB, 'nopr:9f0c416088': ARMAB,
    'nopr:a3fa06a6e4': ARMAB, 'nopr:df420d15df': ARMAB, 'nopr:fb3008a7ba': ARMAB,
    'nopr:834f0a0111': K2, 'nopr:fdee6d93f2': K2,
}

# KEEP-UNPROVEN rows where the lead overrides the mechanical measurement classifier (measure.py)
KU_OVERRIDE = {
    '#207': ('DROP', 'LEAD: classifier false positive (config.yaml line numbers). 0 gemini-bridge turns in 30d blackbox -> idle lane.'),
    '#208': ('DROP', 'LEAD: rides #207; idle lane (0 turns 30d).'),
    '#312': ('DROP', 'LEAD: rides #207; idle lane (0 turns 30d).'),
    '#226': ('DROP', 'LEAD: 0 yunwu turns in 30d (fallback-list only); classifier false positive on config.yaml:16.'),
    '#495': ('DROP', 'LEAD: hook with no consumer: git grep origin/main finds only providers/base.py:144 default + tests; no '
             'provider profile or ~/.hermes/plugins overrides process_response_text.'),
    '#623': ('KEEP', 'LEAD: KEEP-by-unit with #1001 (measured relay failover family).'),
    '#227': ('KEEP', 'LEAD: KEEP-by-unit with the relay/pool family (#1001); rate-limit class fires 9,325 lines.'),
    '#739': ('KEEP', 'LEAD: registry entry 25 (usage caps classify as quota) upstream-intended; reported live 2026-09-19.'),
    '#251': ('DROP', 'LEAD: 0 own log lines; the 7 loose "correction" hits are not this gate.'),
    '#320': ('DROP', 'LEAD: 0 own log lines; 134 loose "orphan" hits are blackbox orphans, not capture recovery.'),
    '#407': ('DROP', 'LEAD: staging half is off (staging_mode=false); transient gate 0 own lines.'),
    'nopr:07d219143b': ('DROP', 'LEAD: stale_quantity_in_body never emitted: 0 matches in any kanban.db table/column (read-only scan '
                        '2026-09-25); "working tree clean" lines are unrelated.'),
    '#980': ('UPSTREAM', 'LEAD: fired once in 5 months (1 real "/model name:" line); generic parse fix -> offer upstream, drop ours on merge.'),
    '#492': ('DROP', 'LEAD: no fleet Anthropic-wire model id contains a dot; guarded rewrite has no live input.'),
    'nopr:051c2076e1': ('DROP', 'LEAD: repair path for DBs that drifted in June, all repaired; 0 fires since. New DBs are created with the correct schema.'),
    '#903': ('KEEP', 'LEAD: regression guard for two measured Apollo freezes 2026-09-22 (#887/#902); tests + one stall signal, 0 conflict files.'),
    '#509': ('DROP', 'LEAD: no-op compaction class not observed in 201 "compression done" lines; 3-sync conflict on agent/turn_context.py.'),
    '#117': ('KEEP', 'LEAD: 8-loc comment, follows #192.'),
    '#1032': ('KEEP', 'LEAD: KEEP-by-unit with #388 (auto-pin, KEEP).'),
    '#389': ('DROP', 'LEAD: warnings overlap #388 auto-pin and #397 hard refusal; 0 log lines. Auditor marked WEAK.'),
    '#689': ('DROP', 'LEAD: deletes a helper upstream still ships -> recurring conflict hunk (1 file x3 syncs); re-adopt upstream.'),
    '#1026': ('KEEP', 'LEAD: problem measured (2026-09-23 ACE-AI outage, 26 pages); gate only fires during host-down, merged the audit day.'),
    '#473': ('DROP', 'LEAD: telemetry-only, consumer unmeasurable (turns.db 0 B here); auditor WEAK; upstream deliberately clears chat_id.'),
    'nopr:e77a87d63a': ('KEEP', 'LEAD: structural host for #1027/#126 helpers; follows them.'),
    '#204': ('UPSTREAM', 'LEAD: bugfix to /boomerang #203 (UPSTREAM); ships with #203.'),
    '#1029': ('KEEP', 'LEAD: CI test half runs on every PR (measured); mutation-gate half manual.'),
    '#695': ('UNRESOLVED', 'LEAD: merge-queue red NOT reproduced on either tree (17 passed x3 + 6/6 concurrent both); evidence = PR body only.'),
    '#708': ('KEEP', 'LEAD: KEEP-by-unit with #709 (stop-loop nudge ~154 fires).'),
    'nopr:7ac18ba0de': ('KEEP', 'LEAD: moves fork code out of the 3/3-sync hermes_state.py; structural cost reducer while hermes_state rows survive.'),
}

# gateway KEEP-UNPROVEN rows not measured by the classifier: family/registry rulings
_ROUTE = 'LEAD: route-announce family; registry entries 23/24 (upstream-intended) + model.announce_recovery live in 13 configs. KEEP-by-registry; UPSTREAM with the family.'
_FOOT = 'LEAD: footer family; registry entry 22 (upstream PR #80661 outstanding) + display.runtime_footer enabled with fork fields provider_model/reasoning/context_full (~/.hermes/config.yaml:342-350) -> renders on every reply.'
_COMP = 'LEAD: compaction-announce family; registry entry 3 + announce_on_hygiene: true (config.yaml:215); "Context compacted" 217 fires (nopr:2870fd4994).'
_REST = 'LEAD: KEEP-by-unit: follows a measured restart/resume head (#295 1,190 / #289 1,410 / #761 183 / #790 151 / #904 455 / #937 132 fires).'
_DRAIN = 'LEAD: drain-budget unit (#738 SUPERSEDED, #861 UPSTREAM). KEEP until the unit converges onto #861; teardown timings are in gateway-exit-diag.log (23 drain/budget lines), not the audited logs.'
GW = {}
for k in ['#228', '#657', '#249', 'nopr:29059863f8', 'nopr:5378b19f73', '#557', '#598', 'nopr:cf58afc340', '#742', '#183', '#197']:
    GW[k] = ('KEEP', _ROUTE)
for k in ['nopr:2f530dd026', 'nopr:11f8a67f01', '#403', '#405', '#520', 'nopr:4c595fc6f1', '#357', '#333', '#175']:
    GW[k] = ('KEEP', _FOOT)
for k in ['nopr:5074cd02b0', '#452', '#316', '#627', '#626', '#404', '#173']:
    GW[k] = ('KEEP', _COMP)
for k in ['#137', '#163', '#138', '#832', '#843', 'nopr:19c2e7cc30', 'nopr:217d61ddce', 'nopr:0da8ba5356', '#750',
          'nopr:a11aecf67b', 'nopr:c05a81d90a', '#290', 'nopr:f29eede0e2', '#801', '#961', '#945', '#1004', '#769']:
    GW[k] = ('KEEP', _REST)
for k in ['#821', '#838', '#705', '#460', '#140']:
    GW[k] = ('KEEP', _DRAIN)
GW.update({
    '#456': ('KEEP', 'LEAD: registry entry 9 (restart_policy golden); conflict-reduction module.'),
    'nopr:b728ad49f9': ('KEEP', 'LEAD: registry entry 10 (route_identity golden); conflict-reduction module.'),
    '#639': ('KEEP', 'LEAD: registry entry 19 (telegram intake sentinel, upstream-intended).'),
    'nopr:3d5ecfa0ae': ('KEEP', 'LEAD: restores the systemd exit-0 branch = registry entry 2 fork-permanent.'),
    '#666': ('KEEP', 'LEAD: KEEP-by-unit with #659 (Ace-ruled invariant).'),
    '#751': ('KEEP', 'LEAD: KEEP-by-unit with #659.'),
    '#691': ('KEEP', 'LEAD: KEEP-by-unit with #659.'),
    '#967': ('KEEP', 'LEAD: follows #157 (adversary-overturned KEEP: 425 re-injected messages).'),
    '#939': ('KEEP', 'LEAD: follows #827 admission throttle (2,266 fires); port with #936.'),
    '#758': ('KEEP', 'LEAD: CI lint gate for the off-loop family, runs on every PR, 0 conflict files.'),
    '#683': ('DROP', 'LEAD: guard success path is silent, 0 failure-literal fires 2026-05-10..09-25; no measurement found. '
             'INCIDENT-BACKED (PR body: replay after boots) - the slice card must first check upstream async delegation for the '
             'replay (52 delegations in state.db) and convert to UPSTREAM if RED. 1,387 loc / 5 conflict files.'),
    '#687': ('DROP', 'LEAD: follows #683.'),
    '#433': ('SUPERSEDED-BY-UPSTREAM', 'LEAD: dedups the fork copy of #198 (SUPERSEDED); dissolves when upstream compute host is taken.'),
    '#710': ('KEEP', 'LEAD: fleet preference applied to every Discord thread created (kanban threads); not a guard, a default. Registry write owed (D2b).'),
})
KU_OVERRIDE.update(GW)
