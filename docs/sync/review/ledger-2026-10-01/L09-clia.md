# Lane L09-clia ledger — parity sync 2026-10-01 (hermes_cli/* part A)

| path | hunks | mode | why | residual risk |
|---|---|---|---|---|
| hermes_cli/sqlite_runtime.py | 1 | UP | upstream renamed probe param `executable`→`python` and routes env via `isolated_interpreter_env()`; fork's only delta vs base (encoding="utf-8", errors="replace") is already in upstream's line. Gate AC5 "identical to theirs" is expected. | none |
| hermes_cli/managed_scope.py | 1 | F | fork `_under_pytest()` delegates to `hermes_test_context._in_test_context()` (session-wide pytest detection, test hermeticity); upstream only compacted the docstring. Gate: `_parse_env` "lost" is base code upstream replaced with `_parse_managed_env` (secret_scope.load_env_file); zero consumers remain. | none |
| hermes_cli/model_catalog.py | 1 | B | upstream docstring ("validated dict") + fork PYTEST_CURRENT_TEST hermeticity gate in `_fetch_manifest` (never hit live catalog from tests). | none |
| hermes_cli/tips.py | 1 | UP+PORT | upstream rewrote tips.py into an i18n facade (tip text now `locales/<lang>.yaml` tips.tNNN, strict key parity across 17 packs). Fork delta vs base was ONE tip string for the fork-only `mixture_of_agents` tool; ported as `tips.t381` in en + 16 locales (translated; script /tmp/e45-L09-clia/port_tip381.py). Full-catalog flatten parity verified OK (3516 keys/lang, placeholders match). Gate AC5 "identical to theirs" on tips.py is expected: fork content relocated to locales/. | locales/*.yaml (17 files, 1 line each) staged under this lane |
