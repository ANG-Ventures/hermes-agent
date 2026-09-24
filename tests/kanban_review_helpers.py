"""Valid reviewer evidence for legacy lifecycle tests not focused on the gate."""
import json

from hermes_cli import kanban_db as kb


def record_review_coverage(conn, task_id):
    task = kb.get_task(conn, task_id)
    if task is not None and task.current_run_id is not None:
        prior = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'changes_requested'",
            (task_id,),
        ).fetchone()[0]
        kb.add_comment(conn, task_id, 'reviewer', 'review_coverage: ' + json.dumps({
            'lenses': {name: 'done' for name in ('contract', 'execution', 'cross-vendor', 'mutation')},
            'findings': 1, 'items': ['Concrete defect in test fixture'],
            'review_minutes': 1, 'battery': 'battery-fixture.zip' if prior else 'seeded',
            'batch_id': 'fixture-batch',
        }), run_id=task.current_run_id)


def covered_request_changes(conn, task_id, **kwargs):
    record_review_coverage(conn, task_id)
    return kb.request_changes(conn, task_id, **kwargs)
