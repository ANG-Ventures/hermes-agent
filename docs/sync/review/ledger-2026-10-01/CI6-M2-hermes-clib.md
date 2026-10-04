# CI round-6 lane M2-hermes-clib — ledger (card t_c5630b21)

Base: fold head 93556253a7 on `sync/upstream-2026-10-01`. Branch `sync/upstream-2026-10-01-ci-M2-hermes-clib`.
CI evidence: run 37038759229. Manifest: 38 test files. Proof: `scripts/test-gate` narrow runs (<=3 files/call),
sandboxed HOME; py3.11 (fork venv) and, where the fixture demands it, py3.14 (CI's interpreter).

Legend: FIXED-CODE = merge dropped fork behaviour, restored in src / FIXED-TEST = stale patch target or fixture
after an upstream restructure, fork contract unchanged / PINNED-UPSTREAM-CONTRACT = test updated to a contract
the merge legitimately adopted / FOLLOWUP->lane = out of this lane's src surface.

## Batch 1 (feed89096a) — commit body carries the per-file detail
| file | verdict | note |
|---|---|---|
| test_kanban_route_race_regressions.py | FIXED-CODE | agent/turn_iteration_prep: fork `apply_pending_live_route` at the loop boundary (merge dropped the `set-model --live` switch point). 15 passed |
| test_kanban_set_model_live.py | FIXED-CODE | same seam; 30 passed with turn_iteration_prep |
| test_kanban_worker_route_pin.py | FIXED-TEST | patch targets after upstream splits (kanban_db_dispatch, models_validate). 31 passed w/ runtime_pin |
| test_kanban_worker_route_runtime_pin.py | FIXED-TEST | as above |
| test_model_switch_inline_provider_syntax.py | FIXED-TEST | models.validate_requested_model -> models_validate. 27 passed w/ slash_form |
| test_model_switch_provider_slash_form.py | FIXED-TEST | as above |
| test_kanban_survivor_binding.py | PINNED-UPSTREAM-CONTRACT | a task grant must name THIS card + its live run (upstream refuses cross-card mutation). 37 passed |
| test_kanban_worker_chat_identity.py | FIXED-TEST | cli.AIAgent -> run_agent.AIAgent; _FakeCLI attrs _init_agent reads. 1 passed |
| test_kanban_worker_spawn_pythonpath.py | PINNED-UPSTREAM-CONTRACT | upstream pins only the dispatcher's own import root. 2 passed |
| test_kanban_worker_exit_trailer.py | FIXED-CODE | kanban_db_dispatch.detect_crashed_workers reads the worker log trailer (rc 0/75 classified, not bare crash). 5 passed 1 skipped w/ exit_decode |
| test_kanban_worker_exit_decode.py | FIXED-CODE | as above |
| test_kanban_zero_byte_db.py | FIXED-TEST | hermes_state.apply_wal_with_fallback -> hermes_state_wal. 14 passed |

## Batch 2 (4eb96082b4)
| file | verdict | note |
|---|---|---|
| test_kanban_progress_stall.py | FIXED-TEST | kanban_tools._connect -> _board seam. 16 passed |
| test_kanban_reassign_review_hold.py | FIXED-CODE | kanban._cmd_reassign returns 0 on success (fold dropped the return); `assigned` payload carries `from` (upstream). 9 passed |
| test_kanban_review_lifecycle.py | FIXED-CODE | kanban_db.request_review never records the reviewer as implementer on a never-claimed card; fork review-coverage gate kept. 26 passed |
| test_kanban_review_surfaces.py | FIXED-TEST | install the reviewer profile the handoff names (argus). 8 passed |
| test_model_alias_credentials.py | FIXED-TEST | fork `_load_direct_aliases()` returns (merged, ok). 68 passed w/ startup_model_routing |
| test_startup_model_routing.py | FIXED-TEST | as above |
| test_model_catalog.py | FIXED-CODE | models_catalog_static: fork claude-opus-5.5-fast SKU + no import-time sync_plugin_provider_catalog; regenerated website/static/api/model-catalog.json. 23 passed |
| test_local_runtime_processes.py | NOT-RED | in the manifest but never red on CI (run 37038759229: 8 skipped, 0 failed; platform-gated) and 8 skipped here. No change |
| test_kanban_worker_exit_trailer.py / exit_decode | (batch 1) | |

## Batch 3 (7227e27de3)
| file | verdict | note |
|---|---|---|
| test_model_validation.py | FIXED-CODE | models_validate._validate_anthropic_messages probes (not fetch) so timeout/connection word the soft-accept honestly and name provider+endpoint (fork TestAnthropicMessagesWarningHonesty); models.probe_api_models neg-cache -> failure=timeout. Fixtures: provider_seam additive registry forbids patch.dict/setitem undo -> restore generation. 96 passed w/ notify_sub |
| test_notify_sub_chat_type_chokepoint.py | FIXED-TEST | add_notify_sub lives in kanban_db_notify after upstream's kanban_db split |

## Batch 4 (f2e43c2e38)
| file | verdict | note |
|---|---|---|
| test_ollama_cloud_auth.py | FIXED-TEST | test seeds `_DIRECT_ALIAS_LOADED` so the hot-reload test exercises the loader-owned-cache path (upstream #16767 keeps caller-seeded dicts; fork hot-reload still fires on every call for loader-owned contents). 79 passed w/ direct_alias_reload + model_alias_credentials |
| test_oneshot_reasoning_and_tier.py | FIXED-TEST | FakeAgent.run_conversation accepts upstream's conversation_history= kwarg. 9 passed |
| test_plugin_dev.py | FIXED-CODE | plugin_dev._load_model_provider undoes registry additions via provider_seam._restore (fork additive facades forbid pop/clear). 10 passed w/ provider_discovery |
| test_provider_discovery_in_progress.py | FIXED-CODE | CI red was the import-time sync_plugin_provider_catalog() circular import (removed in batch 2, models_catalog_static). 1 passed on py3.11 and py3.14 |
| test_root_equality_containment_lint.py | FIXED-CODE | 7 upstream-new relative_to sites (main_desktop, credential_files, skills_hub_install, generate-skill-docs) annotated `# noqa: root-equality <reason>` — all FILE paths or non-derived rmtree targets, no behaviour change. 3 passed |
| test_plugin_install_ref.py | FIXED-CODE | plugins_cmd_update: re-thread fork `_shell_quoted_source` (#1230 C4) dropped by upstream's cmd_update/dashboard_update extraction. 33 passed on py3.14 with uv on PATH (py3.11 local: fixture needs ==3.14 interpreter, env-only) |

## Batch 5 (1aa91d0f80)
| file | verdict | note |
|---|---|---|
| test_setup_blank_slate.py | FIXED-CODE | setup_quick._blank_slate_minimal_toolsets: restore fork tool-granularity overlap filter (file_read/skills_read never disabled) dropped by upstream extraction. green (666 passed w/ subprocess_text_encoding) |
| test_subprocess_text_encoding.py | FIXED-CODE | kanban_worker_hosts: encoding="utf-8", errors="replace" on both text=True calls (#55658). green |
| test_shared_metrics_efficiency.py | FIXED-CODE | v3 schema tool_enabled_unused.toolset enum += fork toolsets file_read, messaging, moa, skills_read (#1299; schema is a frozen upstream artifact). green |
| test_shared_metrics_model_attribution.py | FIXED-CODE + FIXED-TEST | cli_session_mixin.undo_last: warm-only (no SessionDB) path per upstream contract so /undo N counts real user turns; fork hermes_undo core still owns the durable path. Test: PROVIDER_REGISTRY is an additive seam facade -> restore generation instead of type(x)(x). 21 passed. Blast radius (undo_redo_half_turn, rewind_surfaces_invariant, cli_retry): 10 failed/23 passed identical with and without the change (inherited, other lanes) |
| test_startup_model_arg_alias.py | FIXED-TEST | source scan repointed to HermesCLI._init_model_and_provider (upstream split __init__ into cli_init_mixin; the fork wiring is intact there). 25 passed w/ startup_model_routing |

## Batch 6 (276f2b91c3)
| file | verdict | note |
|---|---|---|
| test_web_server.py | FIXED-CODE | web_server_config: delegation.reasoning_effort options = ["", "none", *VALID_REASONING_EFFORTS] (fork #359; upstream's extraction hard-coded the list and dropped none). 194 passed (the 2 update-guidance reds are the home-io guard refusing a sandbox literally named `.hermes`; same on fork/main; CI-green) |
| test_web_server_ws_ping.py | FIXED-TEST | _open_session_db_for_profile moved to web_server_sessions; get_session_stats imported from web_routers.sessions (legacy re-export gone upstream). 8 passed 3 skipped |
| test_kanban_workspace_retention.py | FIXED-TEST | link the child BEFORE it runs (upstream b95513df7c refuses links to running children; parent already done so the blocks edge does not demote). The 2 CI reds pass; remaining 14 local reds are the identical set on fork/main (macOS-only, CI-green) |
| test_serve_skill_maintenance.py | FIXED-TEST | the first `_close_session_by_id` in a process runs lifecycle.finalize_session -> plugins.discover_and_load() cold (profiled: base 0.49s, merged 0.86s, CI 2.87s) and `idle_for < 2` timed that import; warm it on a throwaway session first. 2 passed on py3.11 and py3.14 |

## Batch 7 (final sweep)
| file | verdict | note |
|---|---|---|
| test_kanban_sub_pin_optin.py | PINNED-UPSTREAM-CONTRACT | upstream #111569: the module-form launcher (`sys.executable -m hermes_cli.main`) wins over a PATH shim, so the spawn argv's FIRST `-m` is `hermes_cli.main`; the test now reads the model after the LAST `-m`. The fork pin behaviour (provider/model/effort carried on argv) is intact. 28 passed. (turn_stop_gates tools= change in batch 1 was for route_race/set_model_live, not this file.) |

## Final re-proof on the lane head (groups of <=3 via test-gate, py3.11 fork venv)
g1 progress_stall+reassign_review_hold+review_lifecycle 51 passed; g2 review_surfaces+route_race+set_model_live 44 passed;
g3 sub_pin_optin+survivor_binding+worker_chat_identity 65 passed + sub_pin_optin re-run 28 passed; g4 exit_decode+exit_trailer+route_pin
16 passed 1 skipped; g5 route_runtime_pin+spawn_pythonpath+workspace_retention 62 passed, 14 failed = fork/main's exact
macOS-only retention set (diffed, CI-green); g6 zero_byte_db+local_runtime_processes+model_alias_credentials 70 passed 8 skipped;
g7 model_catalog+inline_provider+slash_form 50 passed; g8 model_validation+notify_sub+ollama_cloud_auth 113 passed;
g9 oneshot_reasoning+plugin_dev+provider_discovery 19 passed; g10 root_equality+serve_skill_maintenance+setup_blank_slate 9 passed;
g11 shared_metrics_efficiency+model_attribution+startup_model_arg_alias 50 passed; g12 startup_model_routing+subprocess_text_encoding
+web_server_ws_ping 682 passed 3 skipped; plugin_install_ref (py3.14 + uv on PATH) 33 passed; web_server (sandbox HOME not named
`.hermes`) 194 passed.

## FOLLOWUP
- FOLLOWUP->plugins lane (M6-misc / whoever owns hermes_cli/plugins.py): merged `plugins.discover_and_load()` calls
  `config._load_config_impl` 35x during discovery (~0.3s of the 0.86s cold path); fork/main's discovery shows no
  such per-plugin config reloads in the profile. Not in this lane's src surface; measured with
  cProfile on `_close_session_by_id` (see batch 6 note).
- FOLLOWUP->harness: tests/hermes_cli/test_shared_metrics_harness.py::test_foreground_terminal_commands_count_by_kind_and_outcome
  reds locally only (`ls` missing-path exit 1 on macOS vs 2 on Linux); not in this manifest, CI-green.
