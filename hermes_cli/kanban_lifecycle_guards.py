"""Persistent entry guards for lifecycle states with mandatory exit data."""
from __future__ import annotations

import re
import sqlite3


def has_event_gate(body: str | None) -> bool:
    return bool(re.search(r"(?m)^unblock_when:[ \t]*event:[A-Za-z0-9_.:-]+[ \t]*$", body or ""))


def install(conn: sqlite3.Connection) -> None:
    installed = dict(conn.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger'"))
    changed = {name: sql for name, sql in _trigger_definitions().items()
               if installed.get(name) != sql}
    if not changed:
        return
    # DDL must not commit the caller's transaction or leave a partial upgrade.
    conn.execute("SAVEPOINT lifecycle_guards_install")
    try:
        for name, sql in changed.items():
            conn.execute(f'DROP TRIGGER IF EXISTS "{name}"')
            conn.execute(sql)
    except BaseException:
        conn.execute("ROLLBACK TO lifecycle_guards_install")
        conn.execute("RELEASE lifecycle_guards_install")
        raise
    conn.execute("RELEASE lifecycle_guards_install")


def _trigger_definitions() -> dict[str, str]:
    definitions = {}
    # Existing legacy rows remain readable/repairable. Only entering scheduled,
    # or clearing an existing wake, is rejected; unrelated edits stay possible.
    for operation in ("INSERT", "UPDATE OF status, next_eligible_at, body"):
        name = "insert" if operation == "INSERT" else "update"
        definitions[f"tasks_scheduled_wake_{name}"] = f"""
            CREATE TRIGGER tasks_scheduled_wake_{name}
            BEFORE {operation} ON tasks
            WHEN NEW.status = 'scheduled' AND NEW.next_eligible_at IS NULL
              AND NOT EXISTS (
                WITH RECURSIVE lines(line, rest) AS (
                    SELECT '', COALESCE(NEW.body, '') || char(10)
                    UNION ALL SELECT substr(rest, 1, instr(rest, char(10))-1),
                                     substr(rest, instr(rest, char(10))+1)
                    FROM lines WHERE rest != ''
                )
                SELECT 1 FROM lines WHERE line LIKE 'unblock_when:%'
                AND trim(substr(line, 14)) LIKE 'event:%'
                AND length(trim(substr(line, 14))) > 6
                AND substr(trim(substr(line, 14)), 7) NOT GLOB '*[^A-Za-z0-9_.:-]*'
              )
            BEGIN
                SELECT RAISE(ABORT, 'scheduled needs a wake time');
            END
        """.strip()
    definitions["tasks_review_handoff_insert"] = """
        CREATE TRIGGER tasks_review_handoff_insert
        AFTER INSERT ON tasks
        WHEN NEW.status = 'review'
        BEGIN
            INSERT INTO task_events(task_id, kind, payload, created_at)
            VALUES(NEW.id, 'review_requested', json_object(
                'implementer', COALESCE(NEW.assignee, 'human:operator'),
                'reviewer', NEW.assignee, 'source', 'lifecycle_entry_repair',
                'review_exit_id', (SELECT COALESCE(max(id), 0) FROM task_status_audit
                    WHERE task_id=NEW.id AND old_status='review' AND new_status!='review')),
                CAST(strftime('%s','now') AS INTEGER));
        END
    """.strip()
    # A REVIEW run handing the card back to review (unbound-claim release, TTL reclaim) continues the same
    # episode: its review->running claim is not an exit, and that run's profile is the reviewer, never the
    # implementer, so no repair handoff is written for it (t_c2a562c9; regression from #177).
    definitions["tasks_review_handoff"] = """
        CREATE TRIGGER tasks_review_handoff
        AFTER UPDATE OF status ON tasks
        WHEN NEW.status = 'review' AND OLD.status != 'review'
          AND NOT EXISTS (
            SELECT 1 FROM task_events WHERE task_id=NEW.id
            AND kind='review_requested'
            AND json_valid(payload)
            AND length(json_extract(payload, '$.implementer')) > 0
            AND (COALESCE(json_extract(payload, '$.review_exit_id'), 0) =
                (SELECT COALESCE(max(id), 0) FROM task_status_audit
                 WHERE task_id=NEW.id AND old_status='review' AND new_status!='review')
              OR EXISTS (SELECT 1 FROM task_events c WHERE c.task_id=NEW.id
                         AND c.run_id=OLD.current_run_id AND c.kind='claimed'
                         AND json_valid(c.payload)
                         AND json_extract(c.payload, '$.source_status')='review'))
          )
        BEGIN
            INSERT INTO task_events(task_id, kind, payload, created_at)
            VALUES(NEW.id, 'review_requested', json_object(
                'implementer', COALESCE(
                    (SELECT profile FROM task_runs WHERE id=OLD.current_run_id
                     AND task_id=NEW.id AND length(profile)>0),
                    (SELECT profile FROM task_runs WHERE task_id=NEW.id
                     AND outcome IN ('completed','review_requested') AND profile IS NOT NULL
                     ORDER BY id DESC LIMIT 1),
                    CASE WHEN OLD.assignee != 'human' AND OLD.assignee NOT LIKE 'human:%'
                         THEN OLD.assignee END, 'human:operator'),
                'reviewer', NEW.assignee, 'source', 'lifecycle_entry_repair',
                'review_exit_id', (SELECT COALESCE(max(id), 0) FROM task_status_audit
                    WHERE task_id=NEW.id AND old_status='review' AND new_status!='review')),
                CAST(strftime('%s','now') AS INTEGER));
        END
    """.strip()
    for operation in ("INSERT", "UPDATE OF status, assignee"):
        name = "insert" if operation == "INSERT" else "update"
        definitions[f"tasks_human_ready_{name}"] = f"""
            CREATE TRIGGER tasks_human_ready_{name}
            AFTER {operation} ON tasks
            WHEN NEW.status = 'ready'
              AND (NEW.assignee = 'human' OR NEW.assignee LIKE 'human:%')
            BEGIN
                UPDATE tasks SET status='todo' WHERE id=NEW.id;
                INSERT INTO task_events(task_id,kind,payload,created_at)
                SELECT NEW.id, 'needs_operator',
                    json_object('assignee',NEW.assignee,'source','lifecycle_entry_repair'),
                    CAST(strftime('%s','now') AS INTEGER)
                WHERE NOT EXISTS (SELECT 1 FROM task_events
                    WHERE task_id=NEW.id AND kind='needs_operator');
            END
        """.strip()
    return definitions
