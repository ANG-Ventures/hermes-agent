# SPEC: deliberate single-sub worker pin (`--pin-sub "<reason>"`)

Card: t_957ca870 · Ace 2026-09-27 08:31, 08:40, 08:45 PT · status: implemented in the companion PR

## Problem

Fork PR #1116 (t_141135aa, merged 2026-09-26) refused every `claude-apx-N` / `claude-bpx-N`
route and every pre-rename alias in every kanban route writer. It had no override. The
incident behind it was real but narrower: cards and lanes pinned to `claude-apx-0` put
workers on Ace's personal Max 20x sub, which was also reachable as `claude-api-proxy`. That
caused 95× 429 and 46× 401 in one day. The ban also removed a feature Ace wants:
"Fable on claude-bpx-24" for one card (t_887f9584) was refused on 09-27. Apollo had to run it
outside kanban.

## Ruling

Workers can be pinned to any provider + model + effort. This is a supported, documented
operator capability. Workers ride the pools by default. A pin needs `--pin-sub "<reason>"`.

## Contract

1. **Opt-in flag.** `--pin-sub REASON` on `kanban create`, `set-model` (including `--where`
   batches) and `lane-model set`. Without it, a single-sub route is refused. The refusal
   names the pool and the flag. It no longer says "workers never pin".
2. **Always refused, even with the flag:** the pre-rename aliases (`claude-api-proxy`,
   `claude-proxy`, `claude-subscription-proxy`, `claude-bridge`, `-fN` / `-failoverN` /
   `-fallbackN`). Name the real provider instead.
3. **Admission comes from the usage registry, not a hand list.** The check reads
   `~/.hermes/config/usage-registry.json` (`kanban_provider_health._usage_registry_path`,
   the file the relay pool loads). N=0 is key `local`; any other N is `sub-vps-N`.
   - The row must exist with `enabled: true`.
   - `claude-apx-N` is refused while `burn_in_until` is in the future, because apx is off
     during burn-in (the sub-vps-21 ban). `claude-bpx-N` serves burn-in subs.
   - An unreadable registry refuses the pin (fail closed; this is a write-time gate).
4. **Sub 0 is pinnable, as a last resort** (Ace 08:45). Its protection is on the pool side:
   the registry keeps `local` out of every pool (`pool_enabled: false`,
   `pool_lb_exclude: true`). Standing rule: pin sub 0 only when Ace asks or every other
   sub is capped. Apollo never pins it by default.
5. **The whole route is pinned.** Provider, model and effort reach the spawn as
   `-m <model> --provider claude-bpx-N --reasoning <level>`. A later route write without
   `--pin-sub` clears the pin, and so does `edit --model`. A pin never outlives the route
   it authorized.
6. **Governors still apply.** The dispatch load gate and the flagship gate still run
   (`--allow-flagship` is still required for Fable/Astra). The pinned sub's own health is
   checked too: box usage cap, credential cooldown and rate-limit circuit. When the sub is
   capped, the card WAITS: it is deferred with `pin` and `pin_fallback=wait` and never
   drifts onto the profile ladder. `--pin-sub-fallback` lets it ride the family pool
   (`bpx`→`claude-bpr`, `apx`→`claude-apr`), but only while that pool is itself admissible.
7. **Visibility.**
   - `show` / `list` print `[PIN claude-bpx-N: <reason>]`.
   - Each pin writes a `sub pin: <provider>; fallback=…; reason=…` comment.
   - The dispatcher route line reads `source=pin`, or `pin(lane-override(...))` for a lane
     pin.
   - `kanban pins [--json] [--stale-hours N]` lists live card and lane pins with their age.
     With `--stale-hours`, it exits 1 on a stale card pin. It is the daily lint against the
     #1116 failure mode: a forgotten pin silently hogging a sub.

## Storage

- `tasks.pin_sub_reason TEXT`
- `tasks.pin_sub_fallback INTEGER NOT NULL DEFAULT 0`
- `lane_model_overrides.pin_sub_reason TEXT`

All three are added by `_add_column_if_missing`. Existing rows read as unpinned.

## Tests (mutation-proven)

`tests/hermes_cli/test_kanban_sub_pin_optin.py` covers:

- the flag works per sub, including sub 0;
- aliases are refused with the flag;
- unregistered, disabled and apx-in-burn-in subs are refused;
- an unreadable registry fails closed;
- writers persist and clear the pin;
- the lane pin works;
- the CLI acceptance shape, the badge in show/list/pins, and the stale-lint exit code;
- dispatch uses `route=claude-bpx-24` with `source=pin`, and model+effort survive into the
  spawn argv;
- a capped pin waits, even with a healthy profile rung available;
- `--pin-sub-fallback` rides `claude-bpr`.

The #1116 suite (`test_kanban_pinned_sub_refusal.py`) stays green unchanged.
