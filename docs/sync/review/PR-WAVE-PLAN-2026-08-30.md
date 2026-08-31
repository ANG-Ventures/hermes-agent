# Upstream PR Wave Plan — TIER-A + TIER-B re-verification

**Date:** 2026-08-30
**Input census:** `docs/sync/review/ABSORPTION-CENSUS-2026-08-29.md` §3 OUTSTANDING, tiers A + B
**Oracle:** `origin/main` @ `2a598aad1c398e95b3325a0f100f5c28efa63d12` (2026-08-30 01:03:21 -0700)
**Repo:** `/Users/alexgierczyk/.hermes/hermes-agent` (origin = NousResearch/hermes-agent), freshly fetched
**Mode:** READ-ONLY. No checkout, no commit, no push, no PR mutation. All premise checks run against a
`git archive origin/main` snapshot at `/tmp/mainsnap`; all conflict measurements are `patch --dry-run`
against a throwaway copy.

Upstream landed **50 commits in the last 24h**, so every census row was re-verified from scratch rather
than trusted.

---

## 0. Method — how each verdict was produced

Four independent probes per PR:

1. **PR state** — `gh pr view <n> --json state,mergeable,mergeStateStatus,updatedAt,additions,deletions,changedFiles`.
2. **Blob-staleness probe** — every `diff --git` hunk carries `index <old>..<new>`. `<old>` is the blob
   the PR was authored against. Compared against `git rev-parse origin/main:<path>`. Equal ⇒ that file is
   byte-identical to the PR's base ⇒ applies verbatim. This is what `mergeable: CONFLICTING` *cannot*
   tell you — GitHub reports a single bit for the whole PR.
3. **Hunk-level conflict probe** — `patch -p1 --dry-run` of the real PR diff against the pristine
   `origin/main` snapshot. Yields *which hunks in which files* fail, i.e. the true rebase cost.
   (Note: GitHub's `CONFLICTING` is frequently much more pessimistic than reality — see #90734, which
   GitHub calls CLEAN and which applies 100%, and #71933, whose only conflict is one test hunk.)
4. **Premise probe** — `grep`/`ast` for the exact symbol/line the census row cited, in the snapshot,
   to answer: *does the defect still exist on main today?*

**Conflict-scope shorthand used below:** `N/M hunks fail in <file>`.

---

## 1. 🔴 HEADLINE FINDING — #90734's premise survived, but its neighbourhood changed

The single most important delta since the census: upstream merged two commits into `hermes_state.py`
**hours after the census was written**, by a third-party contributor, that explicitly reference this PR:

- `0534f1033b` — `perf(state): route 39 pure-read SessionDB methods off the writer lock + gate`
  (kshitijk4poor, 2026-08-29 10:26 +0530)
- `112baae665` — `fix(state): close gate blind spots — alias + variable-SQL readers` (same author, 12 min later)

That work added `tests/state/test_no_locked_readers_gate.py`, whose module docstring says, verbatim:

> "(Pattern C of the 2026-08 perf triage; **#90734 shipped the unlocked-reader subset**, this gate covers
> the locked-reader subset)"

**This is upstream naming our PR as prior art in a merged commit.** It is the strongest possible
corroboration for #90734 — but note the phrasing "#90734 shipped" is *aspirational*: the four readers
#90734 actually fixes are **still on the writer connection on main today**. Verified:

| reader | main location | still `self._conn.execute` (unlocked)? |
|---|---|---|
| `get_compression_lock_holder` | `hermes_state.py:8119` | ✅ yes — blame `a30480bd2b1` (2026-05-28) |
| `clear_session_activity_labels` (no-op fast path) | `hermes_state.py:8197` | ✅ yes |
| `get_handoff_state` | `hermes_state.py:15162` | ✅ yes — blame `878611a79df` |
| `list_pending_handoffs` | `hermes_state.py:15184` | ✅ yes — blame `00ce5f04d9c` |

The new gate does **not** catch them, by construction: it only flags readers *under* `with self._lock:`
(Pattern C). These four are the opposite defect — reads on the shared writer connection with **no lock at
all** (Pattern B), which is what produces the bare `SystemError` that escapes `_execute_write`'s
`sqlite3.Error` retry net. The gate's own allowlist/scan logic (`_scan_locked_readers`, requires
`_is_self_lock_with`) structurally cannot see them.

**Consequence:** #90734 is now *more* landable than the census assessed, not less. Lead the re-request
with the gate docstring citation.

---

## 2. Per-PR verdicts — TIER A (externally corroborated)

### #90734 — `fix(state)`: unlocked reads on the shared SessionDB writer connection → `session_persistence_failed`

| field | value |
|---|---|
| **State** | OPEN, not draft, **MERGEABLE / CLEAN** |
| **Updated** | 2026-08-29T13:23:22Z |
| **Size** | +288/−30, 4 files |
| **Conflict scope** | **NONE — all hunks apply clean** to `2a598aad1c` despite `hermes_state.py` having moved (`9fdc820035a8` → `dac6fcd2af4b`) |
| **Premise** | ✅ **STILL LIVE** — all four unlocked readers present verbatim (table above) |
| **Verdict** | 🟢 **SUBMIT-AS-IS** |

**Why the blob moved but the patch still applies:** `hermes_state.py` grew ~1,400 lines (the PR's hunk at
`6722` now sits at `8111`), but every one of the PR's four context windows is untouched. `patch` relocates
them with zero fuzz. GitHub agrees: `MERGEABLE/CLEAN`.

**Second half of the PR — also still live.** `hermes_state_search.py::_try_incremental_merge_fts` on main
(`:83-95`) still catches only `sqlite3.Error`:

```python
except sqlite3.Error as exc:
    logger.warning("FTS incremental merge failed: %s", exc)
```

A bare `SystemError` from the racing connection therefore still escapes *post-commit* and makes the caller
replay an already-durable write. The PR widens it to `except Exception` with an explicit
"already committed before this cadence runs" rationale. Unchanged premise.

**Action:** no code change. Re-request review, and **add one comment** citing
`tests/state/test_no_locked_readers_gate.py:6` — upstream's own merged test names this PR as the
unlocked-reader half of the same triage, and the two halves are complementary (Pattern B vs Pattern C),
not duplicative. Also cite tevanc14's field repro (`TrackedConnection returned NULL without setting an
exception`, 15/15 focused tests green).

**Priority: #1 overall.** Data-loss class, external repro, upstream-cited, zero conflicts, zero fix work.

---

### #71904 — `fix(gateway)`: persist the platform message id on every user turn

| field | value |
|---|---|
| **State** | OPEN, **CONFLICTING / DIRTY** |
| **Updated** | 2026-08-30T03:45:20Z (most recently touched PR in the set) |
| **Size** | +262/−20, 6 files |
| **Conflict scope** | **8 failed hunks across 4 files** — `agent/conversation_loop.py` 3/3, `agent/turn_context.py` 1/3, `gateway/run.py` 1/1, `run_agent.py` 3/5. Test files apply clean. |
| **Premise** | ✅ **STILL LIVE** |
| **Verdict** | 🟠 **REBASE-THEN-SUBMIT** |

**Premise evidence:** main carries `persist_user_timestamp` end-to-end
(`agent/conversation_loop.py:1842,1861,1919`; `agent/turn_context.py:516,629,729,735`;
`gateway/run.py:6468,21295,21358,29106`; `run_agent.py:8623,8997`) but there is **no
`persist_user_platform_id` anywhere in the tree** — zero hits. The parallel plumbing this PR adds is
still absent.

**Notable:** `gateway/run.py:21808` on main already calls
`await self.async_session_store.has_platform_message_id(...)` — i.e. **main has a consumer that queries
for a platform message id that nothing on the user-turn path ever writes.** That is a much stronger
argument than the census recorded, and it should lead the rebase description: the reader exists, the
writer doesn't.

**Rebase is mechanical, not semantic.** All 8 failures are context drift in files that moved heavily
(`run_agent.py` `1adf170e5e01`→`1b87729159f7`, `gateway/run.py` `aac6a192555f`→`24abe13daf52`). The change
itself is a single optional kwarg threaded through 4 call sites plus a metadata stamp. Re-apply by hand
against current line numbers; no design question is open.

**Action:** rebase onto `2a598aad1c`, re-thread the kwarg, keep both tests. Lead the description with
(a) calvindotsg's tagged-release repro (v0.20.5 / 2026.8.19, Docker digest) and (b) the orphaned
`has_platform_message_id` consumer at `gateway/run.py:21808`.

**Priority: #2 overall** (data-loss-adjacent, strongest external corroboration in the census).

---

### #58144 — `fix(video_analyze)`: route video to a video-capable provider + ingest page URLs via yt-dlp

| field | value |
|---|---|
| **State** | OPEN, **CONFLICTING / DIRTY** |
| **Updated** | 2026-07-26T14:16:10Z |
| **Size** | +570/−12, 9 files |
| **Conflict scope** | **4 failed hunks**: `hermes_cli/config.py` 1/1, `pyproject.toml` 1/1, `tests/tools/test_video_analyze.py` 1/2, `uv.lock` 1/10. Source file `tools/vision_tools.py` (+293/−8, the actual feature) **applies 100% clean.** |
| **Premise** | ✅ **STILL LIVE** |
| **Verdict** | 🟠 **REBASE-THEN-SUBMIT** — and the census's action item is already DONE |

**Census action item resolved.** The census said "action: merge the stacked PR (Kyzcreig/hermes-agent#414)".
That is **already merged** — `ANG-Ventures/hermes-agent#414` reports `state: MERGED`, and its commit rides
on this PR's branch (`ANG-Ventures/fix/video-analyze-provider-and-ytdlp`):

```
659c45e1b1 | adambiggs | fix(video_analyze): complete runtime and native Gemini support (#414)
```

The PR already carries all five commits (4× Kyzcreig + adambiggs' stack), including `_ytdlp_js_runtime_args`
(deno/JS-runtime detection) and native Gemini `video_url` serialization. **No further merge work.**

**Premise evidence — all still absent from main:**

| feature | probe | on main? |
|---|---|---|
| yt-dlp ingestion | `grep yt_dlp tools/lazy_deps.py` | ❌ 0 hits |
| yt-dlp dependency | `grep 'yt-dlp\|ejs' pyproject.toml` | ❌ 0 hits |
| `video_provider`/`video_model` config | `grep hermes_cli/config_defaults.py` | ❌ 0 hits |
| video-capable provider routing | `tools/vision_tools.py:2046-2160` | ❌ still sends video through the generic vision client |

**🔴 The one structural conflict you must know about:** the `hermes_cli/config.py` hunk fails because
**upstream extracted `DEFAULT_CONFIG` into a brand-new file** on 2026-07-29:

```
1fe06115d1 | teknium1 | 2026-07-29 08:55:42 -0700 | refactor: extract DEFAULT_CONFIG + OPTIONAL_ENV_VARS to config_defaults.py
```

The `auxiliary.vision` block the PR edits now lives at **`hermes_cli/config_defaults.py:1139`**. The string
`"vision": {` no longer appears in `hermes_cli/config.py` at all.

**Exact remediation:**
1. Move the 7-line `video_provider` / `video_model` / `ytdlp_cookies` / `ytdlp_cookies_from_browser`
   addition from `hermes_cli/config.py` → **`hermes_cli/config_defaults.py`**, inserting after
   `"model": ""` (`config_defaults.py:1141`), before `"base_url"`.
2. Re-apply the `pyproject.toml` hunk against current line numbers (yt-dlp + EJS dependency group).
3. Regenerate `uv.lock` with `uv lock` — do **not** hand-merge the failed lockfile hunk.
4. Re-apply the 1 failed hunk in `tests/tools/test_video_analyze.py`.
5. `tools/vision_tools.py`, `tools/lazy_deps.py`, `agent/gemini_native_adapter.py`,
   `tests/agent/test_gemini_native_adapter.py`, `tests/test_project_metadata.py` — **no work, clean.**

**Note:** the same relocation invalidates **#62925**'s planned fix location (below). Any census row whose
remediation touches `DEFAULT_CONFIG` is stale in exactly this way.

---

### #62703 — `feat(desktop)`: startup render cache — paint last-known-good UI immediately (SWR)

| field | value |
|---|---|
| **State** | OPEN, **CONFLICTING / DIRTY** |
| **Updated** | 2026-07-26T14:17:28Z |
| **Size** | +2255/−2, 19 files (12 new) |
| **Conflict scope** | **12 failed hunks / 6 files** — `electron/main.ts` 4/8, `preload.ts` 1/1, `package.json` 1/1, `use-gateway-boot.ts` 2/3, `use-session-actions/index.ts` 2/4, `use-session-state-cache.ts` 2/3. All 12 NEW files apply clean. |
| **Premise** | ✅ **STILL LIVE** — no render cache on main: `apps/desktop/electron/` contains no `render-cache*` or `boot-clock*`; zero hits for `renderCache` / `transcript-preload` |
| **Verdict** | 🔴 **FIX-THEN-SUBMIT** — substantial rework, **which we already committed to in writing** |

**🔴 Do NOT rebase-and-push this one.** The last comment on the PR is **our own**, and it publicly commits
to a rework rather than a patch. Re-offering the current branch would contradict our own stated position to
the census's most engaged maintainer. Verbatim (Kyzcreig, 2026-07-26):

> "This PR needs a rework rather than a patch (it's built on a base that main has moved well past, and the
> two changes above are structural, not local). I'd rather hand you something coherent than have you burn a
> test cycle on the current state."

**The rework list we owe SHL0MS — exact steps, all self-identified in that same comment:**

1. **Split the two caches.** `readSessions()` and the transcript read currently share one
   `hermes:render-cache:read` hop with a shared enable/disable gate and no separate eviction policy. Give
   session-info (titles + ids + last-active) an independently-scoped cache that outlives the transcript
   cache, so aggressive transcript eviction never costs the sidebar paint.
2. **Route hydration through the merge path, not a wholesale replace.** This is **correctness**, not polish:
   `sessionsToKeep()` in `use-session-list-actions.ts` deliberately preserves four classes of row the
   aggregator legitimately omits — in-flight first turns (`message_count 0`), pinned rows aged off the page,
   the actively-viewed chat (whose `working` flag clears a beat before the aggregator sees the persisted
   row), and just-settled sessions. A wholesale replace drops rows the merge path exists to retain.
3. **Fix the cache key's local-launch gap.** `gatewayUrl` scopes `renderCaches` per-connection, but
   resolution is **remote-only**: on a local launch there is no stable pre-spawn URL, `url` comes back
   empty, and **local backends never hydrate at all** — the feature currently does nothing in the most
   common launch mode. Add per-profile scoping to the key while fixing it.
4. **Resolve `pushStatusToRenderCache`** — no production caller (tests only). Wire it or delete it;
   dead-wired surface is an explicit AGENTS.md rejection class.
5. **Add a real integration test for transcript hydration** — it writes only `$messages`, which the
   route-resume path clears without a warm runtime state. A unit assertion does not cover this.
6. **Then ping SHL0MS**, who offered to test builds on a slow backend where he reproduces the 20s+ window
   reliably. Ask specifically: does the sidebar paint survive the live flush without row churn, and does the
   divergence counter stay near zero on a no-change boot.

**Priority: LAST of Tier A.** High value, but multi-day, blocks nobody, and shipping it half-done burns the
best maintainer relationship in the census.

---

### #80167 — `feat(kanban)`: expose the per-task reasoning effort on the CLI

| field | value |
|---|---|
| **State** | OPEN, **MERGEABLE / CLEAN** |
| **Updated** | 2026-08-17T00:56:08Z |
| **Size** | +345/−13, 3 files |
| **Conflict scope** | **NONE — all hunks apply clean** (both `hermes_cli/kanban.py` and the docs page moved; every context window intact) |
| **Premise** | ✅ **STILL LIVE, and cleanly so** |
| **Verdict** | 🟢 **SUBMIT-AS-IS** |

**Premise evidence — the "storage exists, CLI doesn't" gap is exact:**

| layer | symbol | on main? |
|---|---|---|
| storage field | `kanban_db.py:1109` `reasoning_effort: Optional[str] = None` | ✅ |
| storage validator | `kanban_db.py:138 normalize_reasoning_effort()` | ✅ |
| storage setter | `kanban_db.py:3796 set_reasoning_effort()` | ✅ |
| schema + migration | `kanban_db.py:1390 reasoning_effort TEXT`, migration `:2647` | ✅ |
| REST | `plugins/kanban/dashboard/plugin_api.py:618,853,992` (incl. `clear_reasoning_effort`) | ✅ |
| **CLI** | `grep -- '--effort' hermes_cli/kanban.py` | ❌ **0 hits** |

26 `reasoning_effort` hits in `kanban_db.py`, 16 in the dashboard REST API, **zero** `--effort` in the CLI.
weeix's complaint ("we can only override the model, there's no way to set effort") is verbatim accurate on
today's main.

**Design note that will read well on review:** `_effort_choices()` derives argparse `choices` from the same
`hermes_constants.VALID_REASONING_EFFORTS` constant that `normalize_reasoning_effort` validates against, so
the CLI enum provably cannot drift from the storage layer — "behavior contracts over snapshots", satisfied
structurally rather than by assertion.

**Action:** none. Re-request review, quote weeix's stated consumer (anti-speculative-infrastructure rubric).

**Priority: #3 overall.**

---

## 3. Per-PR verdicts — TIER B (maintainer-endorsed; sweeper `keep_open`)

### #64464 — `fix(desktop)`: surface `/model` in the slash palette

| field | value |
|---|---|
| **State** | OPEN, **MERGEABLE / CLEAN** |
| **Updated** | 2026-08-29T23:36:25Z |
| **Size** | **+2/−2, 2 files** — smallest item in the census |
| **Conflict scope** | **NONE — all hunks apply clean** |
| **Premise** | ✅ **STILL LIVE** |
| **Verdict** | 🟢 **SUBMIT-AS-IS** |

**Premise evidence, exact:**
- `apps/desktop/src/lib/desktop-slash-commands.ts:217` — the `/model` spec still carries `hidden: true`.
- `:516-532` — `isDesktopSlashSuggestion()` still returns
  `spec.surface.kind !== 'unavailable' && !spec.hidden` (`:527`), so a `hidden` spec is excluded.
- `:656`, `:662` — catalog filtering calls it on both the sectioned and the flat path, so `/model` is
  filtered out of the palette on every surface.

**Action:** none. Two-line change, clean apply, maintainer already traced the whole path.

**Priority: #4 overall** — free win; ideal wave-opener beside the heavyweights.

---

### #71471 — `fix(discord)`: stop the typing indicator sticking on the stale-result path

| field | value |
|---|---|
| **State** | OPEN, **MERGEABLE / CLEAN** |
| **Updated** | 2026-08-29T23:36:26Z |
| **Size** | +154/−1, 2 files |
| **Conflict scope** | **NONE — all hunks apply clean** (`adapter.py` moved `6e66a2c269a4`→`3fb770a5e367`; context intact) |
| **Premise** | ✅ **STILL LIVE — verified verbatim** |
| **Verdict** | 🟢 **SUBMIT-AS-IS** |

**Premise evidence.** The race is present line-for-line on main. *(The census's line numbers have drifted —
the code has not; cite these instead.)*

```python
# plugins/platforms/discord/adapter.py:5636-5644
            finally:
                self._typing_tasks.pop(chat_id, None)      # ← unconditional pop

        self._typing_tasks[chat_id] = asyncio.create_task(_typing_loop())

    async def stop_typing(self, chat_id: str) -> None:
        task = self._typing_tasks.pop(chat_id, None)        # remove…
        if task:
            task.cancel()                                   # …then cancel
```

`stop_typing` removes the entry *then* cancels; the cancelled loop's `finally` pops **unconditionally**, so
a replacement loop registered in the window between is silently deregistered and runs forever with no owner
able to stop it. The PR's fix is the minimal identity check
(`if self._typing_tasks.get(chat_id) is asyncio.current_task():`).

**Action:** none. Note on re-request that this is the survivor of the typing cluster — #34146/#34295 were
EQUIVALENT-SHIPPED; this distinct race was not.

**Priority: #5 overall.**

---

### #71906 — `fix(gateway)`: synthetic internal events must never impersonate the user

| field | value |
|---|---|
| **State** | OPEN, **MERGEABLE / CLEAN** |
| **Updated** | 2026-08-29T23:44:06Z |
| **Size** | +199/−2, 2 files |
| **Conflict scope** | **NONE — all hunks apply clean** (`gateway/run.py` moved `aac6a192555f`→`24abe13daf52`) |
| **Premise** | ✅ **STILL LIVE — both paths** |
| **Verdict** | 🟢 **SUBMIT-AS-IS** |

**Premise evidence — both target sites unchanged in substance, only relocated:**

| site | main today | vulnerable? |
|---|---|---|
| log ladder | `gateway/run.py:19813` — `source.user_name or source.user_id or "unknown"` | ✅ yes |
| shared-session prefix | `gateway/run.py:19133` — `if _is_shared_multi_user and source.user_name:` | ✅ yes |
| the fix's helper | `describe_inbound_event_author` — **0 hits on main** | ✅ absent |

Neither site tests `event.internal`, so a synthetic gateway event (boot auto-resume, queued continuation,
background-process completion notice) is still stamped with a human participant's display name.

**Lead with the second harm — it is the sharper argument:** prefixing an **empty** internal event makes it
non-empty, which silently skips the downstream blank-text recovery-note substitution that only fires on
empty text. That is a behavioral bug, not just forensic noise.

**Supporting precedent:** upstream shipped `8dc9401d7` (persist internal synthetic turns typed as
`internal_notification`) — upstream already agrees internal events are a distinct class; this applies the
same distinction two layers up.

**Priority: #6 overall.**

---

### #72012 — `fix(auxiliary_client)`: apply the slash-compat guard on the cold cache path

| field | value |
|---|---|
| **State** | OPEN, **CONFLICTING / DIRTY** |
| **Updated** | 2026-07-30T13:46:10Z |
| **Size** | +114/−1, 2 files |
| **Conflict scope** | **1 failed hunk, 1 file** — `agent/auxiliary_client.py` 1/1. The new test file applies clean. **This is a one-line change; the "conflict" is pure line drift.** |
| **Premise** | ✅ **STILL LIVE — verified verbatim** |
| **Verdict** | 🟠 **REBASE-THEN-SUBMIT** (smallest rebase in the set — one line) |

**Premise evidence — the asymmetry is exact on main:**

| path | main | applies `_compat_model`? |
|---|---|---|
| cache-HIT (warm) | `agent/auxiliary_client.py:8232` `effective = _compat_model(cached_client, model, cached_default)` | ✅ |
| cache-HIT (warm, 2nd site) | `agent/auxiliary_client.py:8243` — same call | ✅ |
| **cache-MISS (cold)** | `agent/auxiliary_client.py:8291` `return client, model or default_model` | ❌ **raw model** |

So the FIRST auxiliary call with a namespaced id (e.g. `anthropic/claude-haiku-4-5`) sends it un-stripped
and 404s, while every later call with identical arguments succeeds. The fix reuses the **existing**
`_compat_model` helper (`:8159`) — no new surface, and caller-wins is preserved for clients that do accept
slash ids (OpenRouter / namespaced defaults).

**Exact remediation (2 minutes):**
1. At `agent/auxiliary_client.py:8291`, replace
   `return client, model or default_model` with
   `return client, _compat_model(client, model, default_model)`.
2. Carry the PR's comment block explaining the warm/cold contract.
3. Add `tests/agent/test_aux_cached_client_model_symmetry.py` unchanged (applies clean).

**Priority: #7 overall** — sweeper `keep_open salvageability=HIGH`, no objections, one-line diff.

---

### #71443 — `fix(gateway)`: `/stop` must cancel pending clarify prompts so the next message isn't swallowed

| field | value |
|---|---|
| **State** | OPEN, **CONFLICTING / DIRTY** |
| **Updated** | 2026-07-30T12:05:38Z |
| **Size** | +232/−0, 2 files |
| **Conflict scope** | **1 failed hunk, 1 file** — `gateway/run.py` 1/1 (pure insertion into a helper that moved). Test file applies clean. |
| **Premise** | ✅ **STILL LIVE** |
| **Verdict** | 🟠 **REBASE-THEN-SUBMIT** |

**Premise evidence:**
- The primitive still exists: `tools/clarify_gateway.py:486 clear_session(session_key) -> int` — resolves
  and drops every pending clarify for a session, first-writer-wins, and **wakes the blocked waiter**
  (`:495-500` documents exactly that contract). The census cited `:357-378`; it is now `:486-511`.
- The gap still exists: `_interrupt_and_clear_session` on main is at **`gateway/run.py:27845`** and its body
  (`:27845-27925`) contains **no clarify call at all** — it interrupts, bumps the run generation, reaps
  processes, calls `adapter.interrupt_session_activity`, discards the pending message, clears
  `pending_command_text`, and releases/evicts the agent. A thread parked in
  `clarify_gateway.wait_for_response` is never woken.

**Exact remediation:**
1. Insert the PR's `try/except` block into `_interrupt_and_clear_session` at **`gateway/run.py:27845`**,
   immediately after `adapter.get_pending_message(session_key)` / the
   `_iac_state.persistent.pending_command_text = None` line (~`:27913`) and **before**
   `if release_running_state:` (~`:27916`).
2. Keep the placement rationale in the comment — the helper is shared by `/stop` **and** `/new`/`/reset`, so
   one insertion covers every interrupt-and-clear path. That is the "fix the whole bug class, sibling call
   paths included" rubric item, and it should be stated in the PR description.
3. No other change. `clear_session` is idempotent, so the turn's own `finally` calling it again is a no-op.

**Priority: #8 overall.**

---

### #34537 → **superseded by #72014** — `fix(discord)`: let free-response channels quote bot mentions

| field | value |
|---|---|
| **#34537 state** | **CLOSED** 2026-08-06. Blob-stale on both files; 3 failed hunks. |
| **#72014 state** | OPEN, **MERGEABLE / CLEAN**, +304/−12, 2 files, updated 2026-08-17T00:55:47Z |
| **#72014 conflict scope** | **NONE — all hunks apply clean** |
| **Premise** | ✅ **STILL LIVE** (logic relocated + renamed, not fixed) |
| **Verdict** | 🔴 **DROP #34537 / FIX-THEN-SUBMIT #72014** |

**🔴 The census's #34537 row is now unusable, for a reason worth recording.** A `grep` for the census's
cited symbol `_other_bots_mentioned` returns **zero hits on main** — which looks like "premise gone" but is
not. Upstream **extracted the admission logic into a new method and dropped the underscore prefixes**:

```
cc8e5ec2af  refactor(gateway): migrate Discord adapter to bundled plugin
2278f2cb7e  fix(discord): harden reconnect message recovery   (Teknium, 2026-07-17)
```

The logic now lives in `_discord_message_admission()` at **`plugins/platforms/discord/adapter.py:1608-1630`**,
with `_other_bots_mentioned` → `other_bots_mentioned` and `_raw_self_mention` → `raw_self_mention`. The
defect is verbatim intact:

```python
# plugins/platforms/discord/adapter.py:1613-1629
            other_bots_mentioned = any(
                mentioned.bot and mentioned != self._client.user
                for mentioned in message.mentions
            )
            if other_bots_mentioned and not raw_self_mention:
                return False, False          # ← returns BEFORE free-response is consulted
            ignore_no_mention = os.getenv("DISCORD_IGNORE_NO_MENTION", "true")...
            if ignore_no_mention and not raw_self_mention and not other_bots_mentioned:
                ...
                free_channels = self._discord_free_response_channels()   # ← only reached here
```

Free-response membership is resolved **only inside the second branch**, so the first `return False, False`
still fires in a free-response channel. Premise confirmed.

**Act on #72014, not #34537.** #72014 is the successor, is already written against the extracted method, is
**MERGEABLE/CLEAN**, and applies with zero failed hunks.

**The census's named defect for #72014 is REAL and still unfixed — fix before submitting.**
Enough1122's finding: at the PR's `self_user = self._client.user if self._client is not None else None`,
when `self._client is None` then `self_user is None`, so `mentioned != self_user` is **always true** and
EVERY bot mention counts as an "other bot" mention. Main's code reads `self._client.user` directly (would
raise `AttributeError`); the PR silently changes semantics. Confirmed: main has **no `self._client is None`
guard** anywhere in `_discord_message_admission`.

**Exact remediation for #72014:**
1. In `plugins/platforms/discord/adapter.py`, replace the silent `None` fallback with an explicit guard.
   Either (a) keep main's raising behaviour — `self_user = self._client.user` — or, preferred,
   (b) short-circuit admission when the client is absent:
   ```python
   if self._client is None:
       return False, False          # not connected: admit nothing, don't guess
   self_user = self._client.user
   ```
   Option (b) is safer than (a) and states the invariant instead of relying on an exception.
2. Add a regression test asserting that with `self._client = None`, a message mentioning another bot does
   **not** get classified as an other-bot mention (the current diff would).
3. Everything else in #72014 is good and applies clean.
4. Leave #34537 closed. Reference it in #72014's description as the origin.

**Priority: #9 overall** (after the fix).

---

### #64325 — `feat(picker)`: support glob patterns in `model.picker.hide`

| field | value |
|---|---|
| **State** | OPEN, **CONFLICTING / DIRTY** |
| **Updated** | 2026-07-16T03:16:17Z |
| **Size** | +342/−1, 6 files |
| **Conflict scope** | **6 failed hunks / 5 files** — `inventory.py` 2/4, `web_server.py` 1/1, `test_inventory.py` 1/1, `test_list_picker_providers.py` 1/1, `tui_gateway/server.py` 1/1 |
| **Premise** | ✅ live, but **BLOCKED ON AN UNLANDED DEPENDENCY** |
| **Verdict** | 🔴 **FIX-THEN-SUBMIT — and hard-sequence it behind #72048** |

**🔴 Dependency discovered that the census did not record.** `model.picker` **does not exist on upstream
main at all**:
- `grep -rn 'model\.picker' --include=*.py hermes_cli/` → **0 hits**
- `grep picker hermes_cli/config_defaults.py` → only unrelated hits (`hermes tools` picker, model-list URL)
- `list_picker_providers` exists (`hermes_cli/model_switch.py:3953`) but its body (`:3963-4010`) only does
  OpenRouter live-catalog filtering and empty-row dropping — **no hide, no order, no prefs**.

#64325 adds *glob* support to a `model.picker.hide` key that **#72048 introduces**. #72048 is OPEN and
`CONFLICTING/DIRTY` (+360/−1, 5 files). So #64325 is a stack on an unlanded base — offering it now is
incoherent in the same way the census's #59463-after-#58144 note describes.

**The census's named blocking defect is also confirmed, with a corrected location.** The census said
`cli.py:8090`; on today's main the classic CLI picker call is at **`cli.py:11884`**:

```python
                providers = build_models_payload(
                    ctx,
                    probe_custom_providers=force_refresh,
                    probe_current_custom_provider=not force_refresh,
                )["providers"]
```

— no `apply_picker_prefs=True`. And `build_models_payload`'s signature on main
(`hermes_cli/inventory.py:120-136`) has **no `apply_picker_prefs` parameter at all** (it has
`for_picker`, which is a different lever: exhausted-credential-pool visibility, documented at `:186-190`).
Both #72048 and #64325 add that parameter.

**Exact remediation:**
1. **Do not submit #64325 standalone.** Land **#72048** first (it is the designated picker-cluster carrier
   and already includes the `contributors/emails/…` file that unblocked the contributor-check CI).
2. Rebase **#72048** onto current main first — it is also `CONFLICTING`.
3. In #72048, wire the classic CLI path: pass `apply_picker_prefs=True` at **`cli.py:11884`** so the
   classic picker honours the same config as desktop/TUI. (Census called this out against #64325, but the
   parameter is introduced by #72048, so the wiring belongs there.)
4. Only then rebase #64325 (glob support) on top, re-applying its 6 failed hunks.
5. Framing for #72048: upstream's `b239ee212` shipped `excluded_providers` (providers only, no models, no
   ordering). Present `model.picker` as **generalizing their existing knob**, not competing with it.

**Priority: deferred — wave 3, behind #72048.**

---

### #62925 — `feat(delegation)`: compact skill index for subagents + per-task skill promotion

| field | value |
|---|---|
| **State** | OPEN, **CONFLICTING / DIRTY** |
| **Updated** | 2026-07-12T10:12:52Z (oldest in the set) |
| **Size** | +225/−10, 5 files |
| **Conflict scope** | **8 failed hunks / 4 files** — `agent/prompt_builder.py` 2/4, `agent/system_prompt.py` 1/1, `run_agent.py` 1/1, `tools/delegate_tool.py` 4/8 |
| **Premise** | ✅ **STILL LIVE** |
| **Verdict** | 🔴 **FIX-THEN-SUBMIT** (the census's named defect is real — **and its fix location has moved**) |

**Premise evidence:**
- `grep -rn compact_skill_index --include=*.py .` → **0 hits on main**. Nothing absorbed.
- `agent/system_prompt.py:619-648` builds the skills index with no subagent-specific branch — it only
  consults `coding_compact_skill_categories(...)` (`:634-641`).
- The *mechanism* the PR extends already exists and is a strong precedent to cite:
  `build_skills_system_prompt(..., compact_categories=...)` at `agent/prompt_builder.py:1748-1773`, whose
  docstring already states the demote-never-hide contract ("every skill name stays visible and loadable via
  `skill_view`/`skills_list`; only the descriptions are dropped"). The PR adds a `"*"` sentinel to that same
  parameter plus a `promoted_skills` allowlist — an extension of existing infrastructure, not new surface.
  Lead with this; it satisfies "extend, don't duplicate."

**🔴 The census's named defect is confirmed AND its remediation target has moved.** The census said:
"our `delegation.compact_skill_index` gate is read but never added to `DEFAULT_CONFIG["delegation"]` in
`hermes_cli/config.py`". Both halves re-verified:

- **Read-but-never-declared:** the only mention of `compact_skill_index` in the whole diff is the *read* at
  `agent/system_prompt.py` (`(_delegation_config() or {}).get("compact_skill_index", True)`). The diff
  contains **zero** `DEFAULT_CONFIG` edits (`grep DEFAULT_CONFIG 62925.diff` → 0 hits). Defect real.
- **But the target file changed.** `DEFAULT_CONFIG` was extracted on 2026-07-29 by `1fe06115d1`
  (teknium1). The `"delegation"` block now lives at **`hermes_cli/config_defaults.py:2071-2101`**, not
  `hermes_cli/config.py`.

**Exact remediation:**
1. Add the key to **`hermes_cli/config_defaults.py`**, inside the `"delegation"` dict (`:2071`). Place it
   next to `inherit_mcp_toolsets` (`:2094`) since both are subagent-shaping booleans:
   ```python
   # Give delegated subagents a names-only skill index (descriptions
   # dropped) and re-promote only the skills the dispatching brief named
   # via delegate_task(skills=[...]). Demote-never-hide: every skill stays
   # loadable with skill_view(). Set false to give children the full index.
   "compact_skill_index": True,
   ```
2. Verify the read path resolves through the new location — `tools/delegate_tool.py::_load_config` must see
   the defaulted key, not just a user-supplied one. Add an E2E assertion against a temp `HERMES_HOME` with
   **no** `config.yaml` proving the default is `True` (AGENTS.md explicitly wants real-path validation over
   mocks here).
3. Rebase the 8 failed hunks. `tools/delegate_tool.py` (4/8 failed) drifted most — re-apply by hand.
4. Add a documentation line for the new key wherever `delegation.*` keys are documented.
5. **Do not introduce a `HERMES_*` env var** for this — `config.yaml` only (the #38976 rejection class).

**Priority: wave 3** (real fix work + heaviest rebase in Tier B).

---

### #71712 — `fix(context-engine)`: host calls two methods missing from the ContextEngine ABC

| field | value |
|---|---|
| **State** | OPEN, **MERGEABLE / CLEAN** |
| **Updated** | 2026-07-30T12:12:59Z |
| **Size** | +280/−5, 3 files |
| **Conflict scope** | **NONE — all hunks apply clean** |
| **Premise** | ✅ **STILL LIVE — both unguarded reads confirmed** |
| **Verdict** | 🟠 **FIX-THEN-SUBMIT** (one-line doc addition; otherwise ready) |

**Premise evidence — exact:**

| field | declared on ABC (`agent/context_engine.py`)? | read unguarded by host? |
|---|---|---|
| `summary_target_ratio` | ❌ **0 hits** | ✅ `agent/turn_context.py:894` — `_compressor.threshold_tokens * _compressor.summary_target_ratio` |
| `last_real_prompt_tokens` | ❌ **0 hits** | ✅ `agent/turn_context.py:1031` — `f"{_compressor.last_real_prompt_tokens:,}"` |

Both are bare attribute reads with no `getattr` default, so a third-party engine (or the minimal compressor
double) raises `AttributeError` and **aborts the turn**. The PR both declares the fields on the ABC and
makes the host reads defensive — belt and braces, which is right for a plugin contract.

**The census's blocking nit is confirmed and trivially fixable.** The public plugin contract doc exists at
`website/docs/developer-guide/context-engine-plugin.md`, and its **"Class attributes your engine must
maintain"** block currently lists only six fields:

```python
last_prompt_tokens, last_completion_tokens, last_total_tokens,
threshold_tokens, context_length, compression_count
```

**Exact remediation:**
1. In `website/docs/developer-guide/context-engine-plugin.md`, extend the "Class attributes your engine must
   maintain" code block with the two new fields and their semantics:
   ```python
   summary_target_ratio: float = 0.20   # post-compaction target as a fraction of threshold_tokens
   last_real_prompt_tokens: int = 0     # last REAL provider prompt_tokens (not the -1 sentinel)
   ```
2. Note in the doc that `summary_target_ratio`'s default mirrors `ContextCompressor.summary_target_ratio`,
   so built-in host behaviour is unchanged (the PR's own ABC comment already says this — mirror it).
3. Nothing else. Source hunks apply clean.

**Priority: #10 overall.** Batch with the context-engine siblings **#71651** and **#71713** (census tier C)
as one coherent group — they touch the same subsystem and reviewers benefit from seeing the contract fix,
the observability fix, and the tool-guard sibling-path fix together.

---

### #71933 — `fix(state)`: sessions repair should detect incomplete FTS schema, not just unopenable DBs

| field | value |
|---|---|
| **State** | OPEN, **CONFLICTING / DIRTY** |
| **Updated** | 2026-07-30T13:44:27Z |
| **Size** | +132/−2, 2 files |
| **Conflict scope** | **1 failed hunk, 1 file — `tests/test_hermes_state.py` only.** The `hermes_state.py` source hunk **applies clean.** The test hunk appends at line 7704, but the file is now **5,352 lines** — upstream shrank it, so the append anchor is gone. |
| **Premise** | ✅ **STILL LIVE** |
| **Verdict** | 🔴 **FIX-THEN-SUBMIT** (census's narrative defect is real; test rework needed) |

**Premise evidence.** `_db_opens_cleanly()` on main is at `hermes_state.py:3010`, and the false-negative is
verbatim intact at **`:3120-3121`**:

```python
            if "no such table" in msg or "no such column" in msg:
                return None          # ← reports a write-broken store as HEALTHY
```

A populated store whose FTS trigger outlived its virtual table fails every `INSERT INTO messages` with that
exact text, and the probe calls it clean. Detector change still needed.

**🔴 Census defect #3 — "wrong narrative re Strategy 0" — CONFIRMED, and here is the precise correction.**

The PR body still claims (verbatim, PR body lines 94-96):

> "Once the check reports the fault, the existing repair pipeline handles it: **Strategy 0** (`rebuild_fts`)
> re-creates the FTS schema from the canonical `messages` table, which is the correct, least-destructive
> recovery for this shape."

**That is false on current main.** Strategy 0 (`hermes_state.py:3586-3618`) issues
`INSERT INTO <table>(<table>) VALUES('rebuild')` per FTS table and **skips absent tables**:

```python
# hermes_state.py:3599-3606
                try:
                    conn.execute(f"INSERT INTO {table_name}({table_name}) VALUES('rebuild')")
                except sqlite3.OperationalError:
                    # Table absent (FTS disabled / trigram off / cjk not
                    # present or tokenizer unavailable) — skip it.
                    continue
```

A *missing* table is exactly the `OperationalError` this `continue` swallows, so Strategy 0 cannot recreate
it and its post-pass `_db_opens_cleanly()` check (`:3609`) still fails. Recovery actually falls through
past Strategy 0.5 (`REINDEX`, `:3620-3635`) to the **`drop_fts_rebuild`** strategy at
**`hermes_state.py:3686-3710`**, which does `PRAGMA writable_schema=ON` →
`DELETE FROM sqlite_master WHERE name LIKE 'messages_fts%'` → `_bump_schema_cookie` → `VACUUM`, then
reports `report["strategy"] = "drop_fts_rebuild"` (`:3704`).

**Exact remediation:**
1. **Rewrite the PR body's recovery paragraph.** Replace the Strategy 0 claim with: *"Recovery falls through
   Strategy 0 (`rebuild_fts` skips absent tables via the `OperationalError: continue` at
   `hermes_state.py:3603-3606`) and Strategy 0.5 (`REINDEX`) to the FTS-schema drop at `:3686-3710`
   (`strategy = "drop_fts_rebuild"`), which removes the orphaned `messages_fts%` schema entries and lets the
   indexes rebuild from `messages` on next open."* Getting this wrong invites a "cannot reproduce" close.
2. **Rebase the test hunk.** The append target (line 7704) no longer exists — `tests/test_hermes_state.py`
   is 5,352 lines. Re-anchor `TestRepairDetectsIncompleteFtsSchema` at the current end of file.
3. **Extend the tests past `_db_opens_cleanly()`.** Current coverage stops at the detector. Add a case that
   drives the *whole* repair pipeline on a seeded DB with an orphaned FTS trigger and asserts
   `report["strategy"] == "drop_fts_rebuild"` and `report["repaired"] is True` — i.e. prove the real
   recovery path, which is also what makes the corrected narrative credible.
4. Source change in `hermes_state.py` needs no edits — it applies clean.

**Priority: wave 3.**

---

### #71465 — `test(cli)`: restore real coverage for the bpo-9338 subparser-routing fallback

| field | value |
|---|---|
| **State** | OPEN, **MERGEABLE / CLEAN** |
| **Updated** | 2026-08-29T23:36:26Z |
| **Size** | +152/−61, **1 file** |
| **Conflict scope** | **NONE.** Blob probe: the PR's pre-image blob `29c9b6a4b147` **equals** `origin/main:tests/hermes_cli/test_subparser_routing_fallback.py` — byte-identical base. All hunks apply clean. **The only PR in this set that is verbatim-based on current main.** |
| **Premise** | ✅ **STILL LIVE** |
| **Verdict** | 🟢 **SUBMIT-AS-IS** |

**Premise evidence.** The target behaviour is live at **`hermes_cli/main.py:14664-14700`** (census said
`:12319-12339`; the block moved, the code did not). Both branches the PR covers are present:

```python
    # ── Defensive subparser routing (bpo-9338 workaround) ──   :14664
        except SystemExit as exc:
            sys.stderr = _saved_stderr
            if exc.code == 0:
                raise                        # ← exit-code-0 re-raise (#10230)
            subparsers.required = False      # ← nonzero SystemExit fallback
            args = parser.parse_args(_processed_argv)
```

The existing test file on main is only 2.4 KB and does not exercise either branch directly.

**Action:** none. Single file, byte-identical base, clean apply.

**Priority: #11 overall** — free, and a good filler for any wave with spare slots.

---

### #71910 — `fix(gateway)`: reject a blank `prompt.submit` before it costs a full API call

| field | value |
|---|---|
| **State** | OPEN, **CONFLICTING / DIRTY** |
| **Updated** | 2026-07-30T13:43:57Z |
| **Size** | +187/−0, 2 files |
| **Conflict scope** | **1 failed hunk, 1 file** — `tui_gateway/server.py` 1/1. **The hunk cannot be rebased in place: the handler no longer lives in that file.** |
| **Premise** | ✅ **STILL LIVE** |
| **Verdict** | 🔴 **FIX-THEN-SUBMIT** (census's stale-location defect **CONFIRMED**) |

**🔴 Census defect CONFIRMED, with the exact transplant target.** The PR patches
`tui_gateway/server.py:10702`, but the handler was moved by:

```
f67ca220ab | teknium1 | 2026-07-29 11:46:31 -0700 |
  refactor(tui): split @method handlers into methods_* modules
  (mechanical move, registry set-equality verified)
```

On main, `@method("prompt.submit")` is at **`tui_gateway/methods_prompt.py:287`**. `tui_gateway/server.py`
no longer contains the handler at all. Applying the patch as-is would insert a guard into dead code —
exactly the "inert fix, wrong execution path" failure mode.

**Premise still live:** `grep` of `methods_prompt.py` shows error `4029` used only for the *truncation*
refusal (`:455`, `:473`) and `confirm_empty_truncate` (`:648-661`) — there is **no blank-text guard** on the
ordinary submit path. A stale/reconnect-looping client firing `prompt.submit(text="")` still builds a full
agent and pays a full-context API call.

**Exact remediation:**
1. Open **`tui_gateway/methods_prompt.py`**. The handler starts at `:287`; `text` is computed at `:293`
   (`text = sanitize_user_prompt_text(raw_text) ...`) and the session is resolved at `:335`
   (`session, err = _sess_nowait(params, rid)`; `if err: return err`).
2. Insert the guard **immediately after the `if err: return err` block** (~`:337`) and **before**
   `_ensure_active_session_slot(...)` at `:338`:
   ```python
   if isinstance(text, str) and not text.strip() and not session.get("attached_images"):
       return _err(rid, 4029, "prompt text is empty")
   ```
   This placement matters: after session resolution (so `attached_images` is readable), before the session
   slot / agent build / DB write — which is the whole point of the fix.
3. **Do not place it before the typed-stop-phrase branch** (`:305-327`): that branch legitimately handles a
   non-empty `text`, and moving the guard earlier would not change behaviour but would obscure ordering.
4. Retarget `tests/tui_gateway/test_prompt_submit_blank_text.py` at the new module path.
5. Frame it against the sibling validation upstream already accepted on this very RPC:
   `4d79bd3d02` (StanleyStetson, 2026-08-10) reject boolean ordinals / bare `confirm_truncate`, and
   `24f346ee77` (Brooklyn Nicholson, 2026-08-01) fail on full disk. The concern is demonstrably welcome;
   only the location was wrong.

**Priority: wave 3** (small fix, but requires a real transplant + test retarget).

---

### #37513 — `fix`: block sensitive bare-path auto attachments

| field | value |
|---|---|
| **State** | **OPEN** (census said OPEN — confirmed), **CONFLICTING / DIRTY** |
| **Updated** | 2026-07-26T14:18:41Z |
| **Size** | +314/−13, 3 files |
| **Conflict scope** | **2 failed hunks, both in TESTS** — `tests/gateway/test_extract_local_files.py` 1/3, `tests/gateway/test_tts_media_routing.py` 1/1. **The source file `gateway/platforms/base.py` (+94/−10) applies 100% clean.** |
| **Premise** | ✅ **STILL LIVE** |
| **Verdict** | 🟠 **REBASE-THEN-SUBMIT** (tests only — **the census's named defect is already fixed inside the PR**) |

**Premise evidence:**
- `grep -c 'BARE_LOCAL_FILE_EXTS\|_SENSITIVE_BARE_FILE_NAMES\|_SENSITIVE_BARE_FILE_WORDS'
  gateway/platforms/base.py` → **0** on main. Nothing absorbed.
- `extract_local_files` on main still derives its extension set from the broad shared tuple:
  `gateway/platforms/base.py:5277` — `_LOCAL_MEDIA_EXTS = MEDIA_DELIVERY_EXTS`, which includes `.json`
  (`:1914`, `:1947`) and YAML. Bare `.json` paths are still auto-attachable.
- Main does carry a specific-secret denylist (`auth.json`, `.anthropic_oauth.json`, `google_token.json`,
  `webhook_subscriptions.json`, `mcp-tokens/*.json`, `:1428-1450`), but that is a *named-file* list — a
  generated `credentials.json` / `secrets.json` outside it is still admitted, which is the gap.

**🟢 Good news the census predates: the substring false-positive defect is ALREADY FIXED in the PR.** The
census warned "substring matching false-positives `tokenization.json`, `secret-santa.pdf` — match sensitive
words as filename components." The current diff **does exactly that**:
- `_FILENAME_COMPONENT_RE = re.compile(r"[.\-_ ]+")` + `_sensitive_name_components()` split on dots/dashes/
  underscores/spaces and match **whole components**, not substrings.
- `_SENSITIVE_WORD_SCOPED_EXTS` restricts the word heuristic to text/config formats, and the inline comment
  names the census's own two examples: *"rejects legitimate artifacts whose names merely contain one of
  these words — `tokenization.json`, `tokenizer.json` — and applying it to binary render formats rejects
  `token_counts.png` / `secret-santa.pdf`."*

**Exact remediation (tests only):**
1. Re-apply hunk 2 of 3 in `tests/gateway/test_extract_local_files.py` against current line numbers.
2. Re-apply the single hunk in `tests/gateway/test_tts_media_routing.py`.
3. `gateway/platforms/base.py` — **no work**, clean.
4. In the description, state up front that the word heuristic is component-scoped and extension-scoped, and
   name `tokenization.json` / `secret-santa.pdf` as covered cases — that is the first thing a reviewer will
   probe.

**Priority: #12 overall.** Note the sweeper rated this `salvageability=medium` (not HIGH) — it is the
weakest Tier-B row, so do not spend a scarce wave slot on it ahead of the clean carries.

---

### #40157 (CLOSED) / #72013 (OPEN) — `fix(banner)`: include local HEAD in the update-check cache key

| field | #40157 | #72013 |
|---|---|---|
| **State** | **CLOSED** 2026-08-06, CONFLICTING | **OPEN**, CONFLICTING / DIRTY |
| **Size** | +68/−12, 2 files | +89/−8, 2 files |
| **Conflict scope** | 4 failed hunks (`banner.py` 1/4, tests 3/5) | **3 failed hunks** — `hermes_cli/banner.py` 1/4, `tests/hermes_cli/test_update_check.py` 2/4 |
| **Premise** | ✅ STILL LIVE | ✅ STILL LIVE |
| **Verdict** | 🔴 **DROP #40157** (closed; #72013 is the strictly better successor) | 🟠 **REBASE-THEN-SUBMIT — but coordinate with #20653 FIRST** |

**Premise evidence — the staleness path is intact on main.** `hermes_cli/banner.py:448-453`:

```python
            if (
                now - cached.get("ts", 0) < _UPDATE_CHECK_CACHE_SECONDS
                and cached.get("rev") == embedded_rev
                and cached.get("ver") == VERSION
            ):
                return cached.get("behind")
```

No `head` component, and `_local_head_sha` is absent from main. For a source install tracking a fork, both
`embedded_rev` and `VERSION` are typically `None`/unchanged across a `git pull`, so the cached "commits
behind" count is served stale for the full 6-hour TTL after the user has already updated.

**🔴 The contested-duplicate blocker is REAL and still unresolved.** Triage flagged #72013 as a duplicate of
**#20653** (`bfoster59`, *"fix: preserve update cache correctness for source installs"*). Re-checked:
**#20653 is still OPEN** and also `CONFLICTING/DIRTY` (last updated 2026-07-19). So neither has landed and
the collision is live. Pushing ours without addressing it re-litigates a known duplicate.

**Exact remediation for #72013:**
1. **Coordinate before rebasing.** Comment on #20653 (or #72013 referencing it) proposing one of:
   (a) concede to #20653 and contribute our test coverage to it, or (b) merge ours as the carrier with
   attribution. Do not silently push a rival.
2. The differentiator to offer either way is the **test gap the sweeper named on #40157**:
   `tests/hermes_cli/test_update_check.py` has no case for **fresh-cache-with-old-HEAD** (cache inside TTL,
   `rev`/`ver` unchanged, HEAD moved → must re-check, not serve stale). Add it. That is real value neither
   PR currently has and it makes ours the natural carrier.
3. Rebase the 3 failed hunks. The `banner.py` failure is one of four hunks — the `_local_head_sha` helper
   insertion and the cache-key line both need current anchors (`:448-453` for the key; place the helper near
   `_check_via_local_git`, `:~200`).
4. Keep the docker short-circuit ordering the PR already documents (compute `head_sha` **after** it, so
   containers with no `.git` never shell out to git).
5. Distinguish from `3f39f8035` (local-ahead handling) — different case, as the census correctly noted.
6. **Leave #40157 closed.** It is strictly dominated by #72013 (same fix, more test coverage, still open).

**Priority: wave 4** — blocked on a coordination step, not on code.

---

## 4. Summary table — all 20 re-verified rows

Sorted by verdict, then by submission priority. Every verdict cites a `symbol@file:line` or a commit SHA.

| # | PR | verdict | GH mergeable | **real conflict scope** (hunk-level) | premise evidence on `origin/main` |
|---|---|---|---|---|---|
| 1 | **#90734** state/unlocked readers | 🟢 **SUBMIT-AS-IS** | MERGEABLE | **0 failed hunks** | `get_compression_lock_holder@hermes_state.py:8119`, `clear_session_activity_labels@:8197`, `get_handoff_state@:15162`, `list_pending_handoffs@:15184` all still `self._conn.execute` unlocked; `_try_incremental_merge_fts@hermes_state_search.py:91` still `except sqlite3.Error` |
| 2 | **#71904** platform message id | 🟠 **REBASE** | CONFLICTING | **8 hunks / 4 files**: `conversation_loop.py` 3/3, `turn_context.py` 1/3, `run.py` 1/1, `run_agent.py` 3/5 | `persist_user_platform_id` = **0 hits**; orphaned consumer `has_platform_message_id@gateway/run.py:21808` |
| 3 | **#80167** kanban `--effort` | 🟢 **SUBMIT-AS-IS** | MERGEABLE | **0 failed hunks** | `reasoning_effort@kanban_db.py:1109,3796` + `plugin_api.py:618,853` exist; `--effort@hermes_cli/kanban.py` = **0 hits** |
| 4 | **#64464** desktop `/model` | 🟢 **SUBMIT-AS-IS** | MERGEABLE | **0 failed hunks** | `hidden: true@desktop-slash-commands.ts:217` + `!spec.hidden@:527`, filtered at `:656,:662` |
| 5 | **#71471** discord typing race | 🟢 **SUBMIT-AS-IS** | MERGEABLE | **0 failed hunks** | unconditional `self._typing_tasks.pop@adapter.py:5638` vs `stop_typing@:5642-5644` remove-then-cancel |
| 6 | **#71906** synthetic impersonation | 🟢 **SUBMIT-AS-IS** | MERGEABLE | **0 failed hunks** | `source.user_name or source.user_id@gateway/run.py:19813`; `_is_shared_multi_user and source.user_name@:19133`; `describe_inbound_event_author` = 0 hits |
| 7 | **#71465** bpo-9338 coverage | 🟢 **SUBMIT-AS-IS** | MERGEABLE | **0 failed hunks — blob-identical base** (`29c9b6a4b147`) | fallback live at `hermes_cli/main.py:14664-14700` |
| 8 | **#71712** ContextEngine ABC | 🟠 **FIX** (1 doc block) | MERGEABLE | **0 failed hunks** | `summary_target_ratio`/`last_real_prompt_tokens` = 0 hits on ABC; read unguarded `@turn_context.py:894,1031`; doc block `context-engine-plugin.md` lists only 6 fields |
| 9 | **#72012** aux cold-path guard | 🟠 **REBASE** (1 line) | CONFLICTING | **1 hunk / 1 file** | warm `_compat_model@auxiliary_client.py:8232,8243` vs cold `return client, model or default_model@:8291` |
| 10 | **#71443** /stop cancels clarify | 🟠 **REBASE** | CONFLICTING | **1 hunk / 1 file** | primitive `clear_session@tools/clarify_gateway.py:486`; `_interrupt_and_clear_session@gateway/run.py:27845` has no clarify call |
| 11 | **#37513** sensitive bare paths | 🟠 **REBASE** (tests only) | CONFLICTING | **2 hunks, both TESTS**; `gateway/platforms/base.py` **clean** | `BARE_LOCAL_FILE_EXTS` = 0 hits; `_LOCAL_MEDIA_EXTS = MEDIA_DELIVERY_EXTS@base.py:5277` still admits `.json` |
| 12 | **#58144** video provider + yt-dlp | 🟠 **REBASE** | CONFLICTING | **4 hunks**: `config.py` 1/1, `pyproject.toml` 1/1, tests 1/2, `uv.lock` 1/10; `vision_tools.py` **clean** | `yt_dlp@tools/lazy_deps.py` = 0; `video_provider@config_defaults.py` = 0. **Stacked PR ANG-Ventures#414 already MERGED** (`659c45e1b1`) |
| 13 | **#72014** (⟵ #34537) discord bot mentions | 🔴 **FIX** | **MERGEABLE** | **0 failed hunks** | defect intact at `_discord_message_admission@adapter.py:1613-1629`; **known bug real**: `self._client is None` ⇒ every bot mention counts as "other bot" |
| 14 | **#71933** FTS schema detection | 🔴 **FIX** | CONFLICTING | **1 hunk, TESTS only**; `hermes_state.py` **clean** | `"no such table" → return None@hermes_state.py:3120-3121`. **PR narrative wrong**: Strategy 0 skips absent tables (`continue@:3603-3606`); real path is `drop_fts_rebuild@:3686-3710` |
| 15 | **#71910** blank prompt.submit | 🔴 **FIX** (transplant) | CONFLICTING | **1 hunk — untransplantable in place** | handler moved by `f67ca220ab` → `@method("prompt.submit")@tui_gateway/methods_prompt.py:287`; no blank guard there |
| 16 | **#62925** subagent compact index | 🔴 **FIX** | CONFLICTING | **8 hunks / 4 files**: `prompt_builder.py` 2/4, `system_prompt.py` 1/1, `run_agent.py` 1/1, `delegate_tool.py` 4/8 | `compact_skill_index` = 0 hits. **Defect confirmed + relocated**: `DEFAULT_CONFIG["delegation"]` moved to `config_defaults.py:2071` by `1fe06115d1` |
| 17 | **#64325** picker globs | 🔴 **FIX + SEQUENCE** | CONFLICTING | **6 hunks / 5 files** | **`model.picker` does not exist on main at all** (0 hits); `list_picker_providers@model_switch.py:3953` has no hide/order. Stacks on unlanded **#72048**. CLI wiring site is `cli.py:11884` (census said 8090) |
| 18 | **#72013** banner HEAD cache key | 🟠 **REBASE + COORDINATE** | CONFLICTING | **3 hunks**: `banner.py` 1/4, tests 2/4 | cache key lacks `head`@`hermes_cli/banner.py:448-453`. **Rival #20653 still OPEN** (CONFLICTING) — collision live |
| 19 | **#62703** desktop render cache | 🔴 **FIX** (major rework) | CONFLICTING | **12 hunks / 6 files** | no `render-cache*`/`boot-clock*` in `apps/desktop/electron/`; **we publicly committed to a rework** in the PR's last comment |
| 20 | **#40157** banner (closed) | ⛔ **DROP** | CONFLICTING | 4 hunks | CLOSED 2026-08-06; strictly dominated by #72013 (same fix, more coverage, still open) |

**Verdict totals:** 6 SUBMIT-AS-IS · 6 REBASE-THEN-SUBMIT · 7 FIX-THEN-SUBMIT · 1 DROP · 0 NOW-ABSORBED.

**Nothing in Tier A/B was absorbed upstream since the census.** Every premise re-verified as live. Two rows
changed shape materially: **#34537 → #72014** (successor is now MERGEABLE/CLEAN) and **#64325** (revealed as
stacked on unlanded #72048).

### 4.1 Census corrections worth folding back

| row | census said | actually true on `2a598aad1c` |
|---|---|---|
| #90734 | "top priority, external repro" | ✅ + upstream's merged `test_no_locked_readers_gate.py:6` **names this PR as prior art** |
| #58144 | "action: merge the stacked PR #414" | already merged — `659c45e1b1` is on the branch |
| #64325 | "wire `cli.py:8090`" | site is **`cli.py:11884`**; and `apply_picker_prefs` comes from **#72048**, which hasn't landed |
| #62925 | "wire `DEFAULT_CONFIG` in `hermes_cli/config.py`" | file changed — now **`hermes_cli/config_defaults.py:2071`** (`1fe06115d1`) |
| #71910 | "transplant to `methods_prompt.py`" | ✅ confirmed — exact insert point is after `_sess_nowait` at **`:335-337`** |
| #71933 | "Strategy 0 narrative wrong" | ✅ confirmed — `continue@:3603-3606`; real path `drop_fts_rebuild@:3686-3710` |
| #37513 | "must fix substring false-positives" | **already fixed in the PR** — `_FILENAME_COMPONENT_RE` + `_SENSITIVE_WORD_SCOPED_EXTS` |
| #34537 | "premise confirmed at `adapter.py:1187`" | logic moved+renamed into `_discord_message_admission@:1608`; use successor **#72014** |
| #71443 | "`clarify_gateway.py:357-378`" | now **`:486-511`**; insertion target **`gateway/run.py:27845`** |
| #71465 | "`main.py:12319-12339`" | now **`main.py:14664-14700`** |

---

## 5. Submission order — 4 waves, ≤6 PRs each

**Design rules applied:** (a) externally-corroborated data-loss fixes first; (b) never submit a PR whose
dependency is unlanded; (c) cluster PRs that touch one subsystem so a reviewer sees the whole bug class;
(d) no wave mixes more than two "needs real fix" items; (e) waves ≤6 — no firehosing.

### 🌊 Wave 1 — "zero-risk + the data-loss headline" (6 PRs)

**Every item here is SUBMIT-AS-IS or a one-line rebase. Five of six have ZERO failed hunks.**

| order | PR | why now | work required |
|---|---|---|---|
| 1 | **#90734** | data-loss, external repro (tevanc14), **upstream cites it in a merged test** | none |
| 2 | **#71904** | data-loss-adjacent, repro'd on a **tagged release** (v0.20.5), orphaned consumer at `run.py:21808` | mechanical rebase, 8 hunks |
| 3 | **#64464** | +2/−2, clean, maintainer traced the path | none |
| 4 | **#80167** | clean, named consumer (weeix), storage+REST already upstream | none |
| 5 | **#71471** | clean, sweeper HIGH, race verbatim on main | none |
| 6 | **#71906** | clean, sweeper HIGH, upstream precedent `8dc9401d7` | none |

**Rationale:** opens with the strongest evidence in the census and spends almost no rebase budget. If the
maintainers only look at one batch, this is the one that should land whole.

### 🌊 Wave 2 — "context-engine cluster + cheap carries" (6 PRs)

| order | PR | cluster role | work required |
|---|---|---|---|
| 7 | **#71712** | context-engine trio — the ABC contract fix | add 2 fields to `context-engine-plugin.md` |
| 8 | **#71651** | context-engine trio — `register()` failures at WARNING (**MERGEABLE/CLEAN**, 0 failed hunks) | none |
| 9 | **#71713** | context-engine trio — memory-registry tool guards on the sibling path (**MERGEABLE/CLEAN**, 0 failed hunks) | none |
| 10 | **#71465** | free carry, blob-identical base | none |
| 11 | **#72012** | one-line cold-path guard | 1-line rebase |
| 12 | **#71443** | `/stop` clarify cancellation | 1-hunk rebase into `run.py:27845` |

**Rationale:** the context-engine trio is the census's most coherent group — contract, observability, and
sibling-call-path guard in one subsystem. Verified all three apply clean today. #71713 is exactly the
"fix the whole bug class, sibling call paths included" item the AGENTS.md rubric asks for; presenting it
beside #71712 makes that legible.

### 🌊 Wave 3 — "fix-then-submit" (5 PRs)

| order | PR | the fix, in one line |
|---|---|---|
| 13 | **#72014** | guard `self._client is None` explicitly (don't let `self_user=None` make every bot mention "other") |
| 14 | **#71910** | transplant the blank guard to `methods_prompt.py:337`; retarget the test |
| 15 | **#71933** | rewrite the Strategy-0 paragraph → `drop_fts_rebuild`; re-anchor + extend tests to the real recovery |
| 16 | **#37513** | re-apply 2 test hunks; lead with the component-scoped word matching |
| 17 | **#62925** | add `compact_skill_index` to `config_defaults.py:2071`; E2E the default; rebase 8 hunks |

**Rationale:** each carries a named, now-verified defect. Sending them before the fix invites the exact
close the census is trying to avoid.

### 🌊 Wave 4 — "sequenced / blocked on a decision" (4 PRs + 1 dependency)

| order | PR | blocker to clear first |
|---|---|---|
| 18 | **#58144** | move config hunk to `config_defaults.py:1141`; `uv lock` |
| 19 | **#59463** | ⚠️ **hard dependency — must follow #58144** (stacks on it; census rule preserved) |
| 20 | **#72048** | rebase; add the `cli.py:11884` wiring |
| 21 | **#64325** | ⚠️ **hard dependency — must follow #72048** (`model.picker` doesn't exist on main) |
| 22 | **#72013** | coordinate with the still-open rival **#20653** before pushing |

**Deferred out of all waves:**
- **#62703** — multi-day rework we publicly committed to; ship only when the split-cache + merge-path
  version is real, then ping SHL0MS.
- **#40157** — DROP (closed, dominated by #72013).

### 5.1 Cross-wave sequencing constraints (do not violate)

```
#58144 ──────▶ #59463          (SSRF proxy stacks on the yt-dlp ingestion)
#72048 ──────▶ #64325          (globs extend a model.picker key that #72048 introduces)
#80617 ──────▶ #71907, #83463  (slash-command extraction moves the handlers they patch)
```
The pricing trio **#71441 / #71468 / #71469** is order-free internally but should be offered as **one
coherent group** (all three CONFLICTING, all three untouched since 2026-07-30 except #71441). Lead that
group with #71441's behavioral argument, which re-verified as live today:
`_estimate_attempt_cost@agent/empty_response_guard.py:147-170` → `estimate_usage_cost`, and
`empty_retry_budget@:259-261` returns `DEFAULT_EMPTY_RETRY_BUDGET` when `cost is None` — so an unpriced
dated id (e.g. `claude-opus-4-7-20250507`, `usage_pricing.py:270`) makes the guard **fail open**. That is
behavioral, not cosmetic.

---

## 6. First-wave shortlist — submit these six

```
#90734  fix(state): unlocked reads on the shared SessionDB writer connection   SUBMIT-AS-IS
#71904  fix(gateway): persist the platform message id on every user turn       REBASE (8 hunks, mechanical)
#64464  fix(desktop): surface /model in the slash palette                      SUBMIT-AS-IS  (+2/-2)
#80167  feat(kanban): expose the per-task reasoning effort on the CLI          SUBMIT-AS-IS
#71471  fix(discord): typing indicator sticking on the stale-result path       SUBMIT-AS-IS
#71906  fix(gateway): synthetic internal events must never impersonate user    SUBMIT-AS-IS
```

**Combined cost:** 5 PRs need **zero** code changes; 1 needs a mechanical rebase.
**Combined evidence:** 2 independent field repros, 1 upstream commit citing our PR by number, 1 named user
request, 2 sweeper `keep_open salvageability=HIGH`.

**Per-PR talking point to lead with:**

| PR | lead with |
|---|---|
| #90734 | `tests/state/test_no_locked_readers_gate.py:6` — upstream's own merged test says "#90734 shipped the unlocked-reader subset". Ours is Pattern B (no lock); the merged gate covers Pattern C (locked). Complementary, not duplicate. |
| #71904 | `has_platform_message_id@gateway/run.py:21808` already **reads** a platform message id that nothing on the user-turn path ever **writes**. Plus calvindotsg's repro on tagged v0.20.5 by Docker digest. |
| #64464 | `hidden: true@:217` + `!spec.hidden@:527` — two-line removal, dispatch already opens the picker for bare `/model`. |
| #80167 | 26 `reasoning_effort` hits in `kanban_db.py`, 16 in the REST API, **0** `--effort` in the CLI. `_effort_choices()` derives from `VALID_REASONING_EFFORTS`, so the enum cannot drift. |
| #71471 | `stop_typing` pops-then-cancels while the loop's `finally` pops unconditionally — the replacement loop is orphaned. Survivor of the typing cluster (#34146/#34295 shipped; this race did not). |
| #71906 | Prefixing an **empty** internal event makes it non-empty, which skips the blank-text recovery-note substitution that only fires on empty text. Behavioral, not just forensic. |

---

## 7. Verification appendix — reproduce any claim in this document

```bash
cd /Users/alexgierczyk/.hermes/hermes-agent
git fetch origin && git rev-parse origin/main      # expect 2a598aad1c...

# premise oracle (read-only, no checkout)
rm -rf /tmp/mainsnap && mkdir -p /tmp/mainsnap
git archive origin/main | tar -x -C /tmp/mainsnap

# hunk-level conflict scope for PR N (throwaway copy; never the live tree)
cp -R /tmp/mainsnap /tmp/patchtest
gh pr diff N --repo NousResearch/hermes-agent > /tmp/N.diff
cd /tmp/patchtest && patch -p1 --dry-run --force -i /tmp/N.diff

# blob-staleness probe: is <file> byte-identical to the PR's base?
#   compare `index <old>..` in the diff against:
git rev-parse origin/main:<path>
```

**Caveat recorded for the next census.** `patch --dry-run` output differs between BSD and GNU `patch`
("N out of M hunks failed" vs "Hunk #N FAILED"); an initial pass that regexed only the GNU phrasing reported
**0 failed hunks for every PR** while `rc=1`. Any conflict-scope tooling must assert
`rc == 0 ⟺ no failure text`, or it will silently report clean applies for conflicted patches.

*Generated 2026-08-30 against `origin/main@2a598aad1c398e95b3325a0f100f5c28efa63d12`. Read-only: no
checkout, commit, push, or PR mutation was performed.*
