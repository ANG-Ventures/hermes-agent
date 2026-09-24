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

## Config

```yaml
kanban:
  home_guard: refuse   # refuse (default) | warn — warn allows with one stderr line
```

## Changelog

- 2026-09-24: guard armed — `kanban.home_guard` added with default `refuse`
  (`warn` = escape hatch); home is the session lineage (`home_ids`), not the
  exact session id.
