# Tranche: auto-desktop-retired — 37 rows

absorb = substantive added src lines found verbatim on upstream/main (hit/total); sym = added def/class names found. Signal only — read README §0.

| key | subsystem | kind | loc | files | conflict-files (syncs) | absorb src | sym | date | subject |
|---|---|---|---|---|---|---|---|---|---|
| #287 | apps | code | 1801 | 16 | 0(0) | 15/722 | 0/0 | 2026-07-10 | feat(desktop): startup render cache — paint last-known-good UI in <1.5s (SWR) (#287) |
| #181 | apps | code | 609 | 11 | 0(0) | 19/216 | 0/0 | 2026-07-02 | fix(desktop): stage remote files for desktop opens (#181) |
| nopr:66989fbeca | apps | code | 536 | 20 | 1(1) | 38/196 | 0/0 | 2026-07-15 | fix(desktop): distinguish xhigh and max reasoning |
| #185 | apps | code | 504 | 18 | 0(0) | 17/177 | 0/0 | 2026-07-03 | fix(desktop): gate remote media + file reveals + D-8 lint rule (desktop PRD Phase 3) (#185) |
| #274 | apps | code | 417 | 49 | 0(0) | 14/94 | 0/0 | 2026-07-10 | test(desktop): fix ~55 ambient failing suites (electron node:test TS imports, jsdom harness, packagi |
| #368 | apps | code | 401 | 7 | 0(0) | 16/116 | 0/0 | 2026-07-16 | perf(desktop): shiki HTML cache + preload-hydrated stash — kill the highlight flash, instant first c |
| #363 | apps | code | 325 | 3 | 0(0) | 5/86 | 0/0 | 2026-07-15 | perf(desktop): two-phase transcript render budget for fast session switches (#363) |
| #421 | apps | code | 323 | 31 | 0(0) | 3/54 | 0/0 | 2026-07-25 | fmt(js): `npm run fix` on merge (#421) |
| #361 | apps | code | 277 | 4 | 0(0) | 1/115 | 0/0 | 2026-07-15 | fix(desktop): sweep zombie optimistic rows on the reconnect seam + keep them out of the render cache |
| #366 | apps | code | 258 | 3 | 0(0) | 3/66 | 0/0 | 2026-07-15 | perf(desktop): in-memory transcript stash — instant, flash-free session switches (#366) |
| #296 | apps | code | 258 | 9 | 0(0) | 3/101 | 0/0 | 2026-07-11 | feat(desktop): paint cached transcripts on session click (switch-paint) (#296) |
| #305 | apps | code | 233 | 5 | 0(0) | 4/84 | 0/0 | 2026-07-11 | fix(search): token-count-first ranking + origin-context lines in search results (#305) |
| #292 | apps | code | 227 | 3 | 0(0) | 3/95 | 0/0 | 2026-07-10 | feat(desktop): preload pinned+visible session transcripts into the render cache (#292) |
| #398 | apps | code | 192 | 2 | 0(0) | 1/67 | 0/0 | 2026-07-18 | fix(desktop): role-aware tail-first stamping (re-land of #364) (#398) |
| nopr:427d020ef2 | apps | code | 185 | 25 | 0(0) | 0/2 | 0/0 | 2026-07-16 | style(desktop): eslint --fix post-merge (import order/padding); no-control-regex disable on CSS.esca |
| #338 | apps | code | 172 | 8 | 0(0) | 6/72 | 0/0 | 2026-07-14 | fix(desktop): fix duplicate-toolCallId renderer crash + crash-loop breaker (#338) |
| #340 | apps | code | 129 | 7 | 0(0) | 8/35 | 0/0 | 2026-07-14 | feat(desktop): add desktop.reset_model_on_new_session config flag (#340) |
| #423 | apps | code | 127 | 2 | 0(0) | 0/56 | 0/0 | 2026-07-25 | fix(desktop): resolve app icon from asar.unpacked so the app can launch (#423) |
| #332 | apps | code | 114 | 8 | 0(0) | 5/53 | 0/0 | 2026-07-14 | feat(desktop): /reasoning command + /clear and /models aliases in the slash palette (#332) |
| #354 | apps | code | 109 | 3 | 0(0) | 1/31 | 0/0 | 2026-07-15 | fix(desktop): prevent duplicate message-id renderer crash (performOp/link) (#354) |
| #461 | apps | code | 108 | 1 | 0(0) | 1/53 | 0/0 | 2026-07-27 | test(e2e): fix the wall-clock race in tile-unread-bug's unread assertion (#461) |
| #362 | apps | code | 91 | 2 | 0(0) | 0/34 | 0/0 | 2026-07-15 | fix(desktop): transplant the runtime footer when the zombie sweep adopts a committed twin (#362) |
| #167 | apps | code | 88 | 3 | 0(0) | 5/22 | 0/0 | 2026-07-01 | fix(desktop): read composer image preview locally in remote mode (#167) |
| nopr:9fabd8a3da | apps | code | 85 | 3 | 0(0) | 4/11 | 0/0 | 2026-07-10 | fix(desktop): restore upstream exports/fields the parity merge dropped |
| #450 | apps | code | 84 | 3 | 0(0) | 1/19 | 0/0 | 2026-07-27 | test(e2e): kill the wall-clock race in the sidebar background-dot specs (#450) |
| #279 | apps | code | 61 | 3 | 0(0) | 1/25 | 0/0 | 2026-07-10 | fix(desktop): session.changes poll must use the STORED session id (A2 eyes-on catch) (#279) |
| #488 | apps | code | 48 | 1 | 0(0) | 3/21 | 0/0 | 2026-08-07 | fix(desktop-e2e): bound fixture cleanup with a hard deadline — naked app.close() can hang forever on |
| nopr:dd58e242b9 | apps | code | 28 | 1 | 0(0) | 0/3 | 0/0 | 2026-07-25 | fix(sync): size the compaction E2E filler past BOTH fork trigger behaviors |
| #283 | apps | code | 27 | 1 | 0(0) | 3/7 | 0/0 | 2026-07-10 | fix(desktop): cross-machine session-list sync — refresh sidebar on focus + 30s focused poll (#283) |
| #367 | apps | code | 17 | 2 | 0(0) | 0/0 | 0/0 | 2026-07-16 | style(desktop): eslint --fix import ordering + padding for the transcript-stash change (#367) |
| #187 | apps | code | 15 | 1 | 0(0) | 1/6 | 0/0 | 2026-07-03 | fix(desktop): align pane width persistence test (#187) |
| nopr:51147803c6 | apps | code | 12 | 1 | 0(0) | 0/1 | 0/0 | 2026-07-25 | fix(sync): recalibrate compaction E2E fixture for the fork's skew-calibrated trigger |
| #424 | apps | code | 10 | 2 | 0(0) | 1/4 | 0/0 | 2026-07-28 | fmt(js): `npm run fix` on merge (#424) |
| nopr:0159f3ec2d | apps | code | 10 | 1 | 0(0) | 0/0 | 0/0 | 2026-06-05 | fix(desktop): drop manual Content-Length in OAuth net.request (ERR_INVALID_ARGUMENT) |
| #331 | apps | code | 5 | 1 | 0(0) | 0/2 | 0/0 | 2026-07-14 | test(desktop): positive /model suggestion assertion + fix stale comment (Greptile P2s on #330) (#331 |
| nopr:923516adda | apps | code | 5 | 3 | 0(0) | 0/0 | 0/0 | 2026-07-24 | fix(sync): CI round-2 — desktop lint errors from the pin-atom cleanup |
| #330 | apps | code | 4 | 2 | 0(0) | 0/2 | 0/0 | 2026-07-14 | feat(desktop): surface /model in the slash palette (#330) |
