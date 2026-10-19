# Boomerang v2: INV-4 framing gate (spec AC-5)

Card t_c4e7974e · 2026-10-18 18:00–18:50 PDT · probes run from `fork/main` bd407ad8171, PR based on 0d373c1ea94 · spec
`~/.hermes/plans/2026-10-18_boomerang-fork-autowake-spec.md` INV-4 / D-10 / §5.2 / AC-5.

## Verdict

**PASS: 19/20 recall, 0/20 disavowal. Bar (declared in the spec before the run): ≥ 19/20 recall AND
0/20 disavowal.** The D-10 primary framing (one trailing USER turn, prefix untouched) holds. The
fallback framing (assistant ack + user turn) was **not run**, because the card says to run it only
if the bar fails.

The pass has **zero margin**: one more recall miss would have failed the gate. The one miss was not
a disavowal (see below).

| prefix | source (request_dump_…) | prompt tokens | probes | cache_read ratio (min) | recall | disavowal |
|---|---|---|---|---|---|---|
| 50k | synth50k (see Prefixes) | 55,224 | 4 | 0.9808 | 4/4 | 0/4 |
| 100k | 20260923_061849_2eb2e07a_20260924_013153_393213 | 89,125 | 4 | 0.9792 | 4/4 | 0/4 |
| 200k | 20260922_131324_c230be44_20260923_095458_239021 | 186,653 | 4 | 0.9980 | 4/4 | 0/4 |
| 350k | 20260921_025702_492724_20260922_124333_269314 | 355,013 | 4 | 0.9994 | 4/4 | 0/4 |
| 500k | 20260921_002254_3f4af45e_20261017_101517_262480 | 497,029 | 4 | 0.9991 | 3/4 | 0/4 |
| **total** | 5 prefixes | | **20** | **≥ 0.9792** | **19/20** | **0/20** |

All 20 rows: HTTP 200, `served_by == pin` (the warm call's seat), fork body bytes up to the
appended tail identical to the warm request (`warm_body_prefix_identical: true`), `cache_creation`
0 on every fork. The unread remainder is the real final assistant turn plus the frame turn.

Raw per-probe data, including the full reply text: `results.jsonl` (20 rows, framing `user`).

## Method

Script: `tools/dev_probes/boomerang_v2_inv4_probe.py` (stdlib + urllib; imports `normalise`/`post`
from the P0 replay probe, which is unchanged). Each prefix runs as:

```
python3 tools/dev_probes/boomerang_v2_inv4_probe.py plan --request <dump>           # cut points
python3 tools/dev_probes/boomerang_v2_inv4_probe.py run  --request <dump> --cut <i> --label <size> \
    --out docs/boomerang-v2/evidence/inv4-probes/results.jsonl                      # warm + 4 forks
python3 tools/dev_probes/boomerang_v2_inv4_probe.py report --results docs/boomerang-v2/evidence/inv4-probes/results.jsonl
```

1. **Parent transcript.** A real saved claude-alr request (anthropic_messages body), normalised the
   P0 way: scratch session id `boomerang-inv4-<hex>` (never Apollo's), scratch nonce on
   `system[0]`, thinking/output_config dropped, every `cache_control` set to `ttl: 1h`, model
   `claude-opus-5-5`. `claude-fable-5-1` was capped on 24/25 seats at run time (`/health
   capped_by_model`), the same override P0.2 used.
2. **Cut.** The transcript ends at a real, complete, text-only assistant turn, the "parent's last
   complete assistant turn" of spec §5.1 and D-8.
3. **Plant.** `"Noting for later in this session: the deploy token for project Kestrel is 7F3A."`
   goes into an EARLIER assistant turn, the one nearest 40 % of the way into the transcript (index
   `plant_idx` per row: 21/57, 7/19, 55/145, 177/371, 279/701). It is inserted as a text block
   before any `tool_use` in that turn. Before planting, each source was checked to contain neither
   `Kestrel` nor `7F3A`.
4. **Warm.** The parent request that produced the final assistant turn (transcript minus that turn,
   one 1h breakpoint on its last block) is POSTed once. Its served seat is recorded. This is the
   parent's own cache write.
5. **Fork ×4.** The warm bytes are sent unchanged, plus the real final assistant turn, plus the
   spec §5.2 user frame, pinned with `x-hermes-pin-sub: <warm seat>`:

   ```
   [BOOMERANG FORK — you are a forked continuation of this same conversation, running in the
   background. The turns above are yours. Parent session: <scratch sid>. Delegation ID: del-inv4-<hex>.
   Your task now: State the deploy token for project Kestrel and which turn you set it in. Execute
   autonomously: make reasonable assumptions, do NOT stop to ask questions. If you hit a genuine
   blocker, complete what you can and report the blocker in your summary.
   End with the structured handoff: Outcome / Changed / Commands-validation / Blockers.]
   ```

   The contract text is the v1 autonomous-execution contract from the `boomerang` skill, which
   §5.2 elides with "…".
6. **Score**, on the first reply only (the spec scores "the child's first reply"):
   - `recall`: the exact `7F3A` appears in the reply text.
   - `disavowal`: the fixed spec regex `never did|didn't do that|wasn't me|not my (work|turn)|I have no record`
     (case-insensitive).
   - `cache_read_ratio`: cache_read / (input + cache_read + cache_creation) on the fork request.

## Prefixes

The 100k–500k prefixes are real transcripts from five different Apollo/fleet sessions, each cut at
a real assistant turn, with the system prompt and tools as sent. The 50k prefix is
**synthesized from real rows**: no saved anthropic-shape dump has a text-only assistant cut point
under ~100k, because the Apollo system prompt + tools alone is ~150k chars. It takes the system
prompt + tools from `request_dump_20260716_053729_487344a8_20260716_141840_477794` (a ~92k-char
base) and messages 0–56 from `request_dump_20260929_183909_1d72a62e_20261002_114340_222885`.
That file was a one-shot scratch build (`~/.hermes/var/oneshot/inv4/synth50k.json`) and is not
committed, because it holds a live transcript. The first 50k attempt used
`request_dump_20260609_101858_ede02eec` (opus-4-8 era). Its warm call got HTTP 400 twice ("Third-party
apps now draw from your extra usage…") on sub-vps-19, produced no probe rows, and was dropped.

## The one miss (500k, probe 3)

`stop_reason: tool_use`, empty text, two tool calls: `mem0_search` and `search_files`. The fork did
not answer from the transcript on its first reply. It went to look the token up. This is not a
disavowal: there is no text, and the regex has nothing to match. Under the spec's
first-reply scoring it is still a recall miss. A real forked child would run those tools and keep
going, and this single-request probe cannot see what it would have said next. The other three
500k probes answered from the transcript.

## Qualitative read (not part of the gate)

- 19/20 replies attribute the sentinel to the fork's own earlier turn ("in one of my own
  messages", "my own reply", "I wrote"). None attributes it to the user or to a foreign report.
  This is the property INV-4 is after.
- Most replies flag the token as unverified ("no traceable source", "check it against
  1Password"). One (350k #4) calls it "possibly injected text". That is accurate: it was planted.
  It is a provenance caveat, not a disavowal of the turn, and it does not match the fixed regex.
  The D-15 production metric uses the same regex, so the same reading applies there.
- Several replies name the exact location (between which two tool calls, which wall-clock
  turn), so the model is reading the prefix rather than guessing.

## Cache

Every fork read ≥ 97.9 % of its prompt from cache, pinned to the warm seat, on four distinct
seats (`sub-vps-4` for two prefixes, `local`, `sub-vps-21`, `sub-vps-9`), at ages 10–107 s. The
uncached tail is the final assistant turn + frame turn (213–1,856 tokens). The ratio is lowest on
the small prefixes only because that tail is a larger share of them. This is consistent with
P0.2. It is not the AC-4 16-trial gate, which belongs to P2.

## Budget

5 scored warm writes (54k + 87k + 186k + 355k + 497k = 1.18 M tokens of 1h cache creation), 20 fork
reads (4,716,428 cache-read tokens), 2 failed 400s, and 1 unscored ~20k warm write while picking the
50k base. 28 requests in total, under the card's ~40.
