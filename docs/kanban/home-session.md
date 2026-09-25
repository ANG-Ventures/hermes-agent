# Kanban home session

A card's `session_id` is its **home session** — the chat that created it.
Status/ownership mutations from any other chat session are refused by the
home-session guard (`kanban_db.check_home_session`); comment instead, or
override with `--foreign-ok "<reason>"` (audit comment posted).

## Home = session lineage

Gateway chats rotate their session id without changing chat
(`resume_pending_expired`, `session_switch`, most `/new`). The successor row
carries `parent_session_id` and the same `session_key`. So "home" is not the
exact id; it is `kanban_db.home_ids(session_id)`:

- the id itself, plus
- `parent_session_id` ancestors and descendants sharing its non-NULL
  `session_key`, at most 10 hops each way.

A chain under a different `session_key` never matches; a `/new` with no parent
link stands alone. The lookup is read-only against the gateway `state.db`
(2 s busy timeout) and fails open to `{session_id}` (exact-id behaviour) on
any error, including a missing `state.db`.

Consumers — keep them on this one helper: `kanban list --home`, `kanban show`'s
`home:` label, the guard's same-home test, and the kanban-home-cards plugin.

## Birth stamp, unhomed, session-first list, takeover

- **Every card is born homed.** `kanban_db.create_task` (the one choke point
  for CLI, tool, swarm, dashboard) stamps `session_id`: explicit > first homed
  parent's home > `"unhomed"`. Never NULL. It also prepends
  `origin: <platform> <chat_name> (<chat_id>) · session <id> · <date>` unless
  the body already has an `origin:` line.
- **`unhomed`** (cron/script/shell births) is foreign to every chat session;
  it loads as `Task.session_id=None, unhomed=True`, so notification/wake
  routing is unchanged. Raw-SQL readers of `tasks.session_id` must treat it
  like NULL: `backfill_notify_sub_user_ids` (`kanban notify-repair`) maps it to
  "no creator provenance", so a user-less card never adopts the lone human in
  its chat.
- **`kanban list`** with a caller session prints `THIS SESSION (n)` then
  `OTHER SESSIONS (m)`, one line each. `--this-session` (= `--home`) filters,
  `--all` gives the flat view. The `kanban_list` tool does the same: this
  session's cards in full under `tasks`, others collapsed to
  `{id, status, title[, unhomed]}` under `other_sessions`; `all=true` is flat.
- **Re-scope.** There is no `edit` verb. The title/body writers are
  `specify` (guarded like every other mutator) and the dashboard's
  `PATCH /tasks/:id`, which — like every dashboard mutation — runs with no
  session identity (the human operator's surface) and is therefore unguarded.
- **`--takeover REASON`** (alias of `--foreign-ok`) acts on a foreign or
  unhomed card; it writes a `takeover` event and a `takeover:` comment.
  `comment` is never guarded. Sessionless callers (cron sweeps) stay unguarded.
- **`kanban home-lint`** prints nothing and exits 0 when every open card has a
  home. Otherwise it lists the ids and exits 1. `--backfill [--dry-run]` stamps
  them `unhomed` and adds a comment.

## Config

```yaml
kanban:
  home_guard: refuse   # refuse (default) | warn — warn allows with one stderr line
```

## Changelog

- 2026-09-24: guard armed — `kanban.home_guard` added with default `refuse`
  (`warn` = escape hatch); home is the session lineage (`home_ids`), not the
  exact session id.
- 2026-09-24: birth stamping (never NULL, `unhomed` sentinel, origin line),
  session-first `list`, `--takeover` + `takeover` event, `home-lint`.
