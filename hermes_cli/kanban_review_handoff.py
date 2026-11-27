"""Review-entry provenance for operator-reopened candidates."""
from __future__ import annotations


def record_reopened_candidate(conn, task_id, *, actor, reviewer, reason, run_id):
    from hermes_cli import kanban_db as kb

    prior = kb._latest_event(conn, task_id, "review_requested")
    payload = kb._json_dict(kb._row_get(prior, "payload"))
    run = conn.execute(
        "SELECT profile, metadata FROM task_runs WHERE task_id = ? AND id = ?",
        (task_id, run_id),
    ).fetchone() if run_id is not None else None
    row = conn.execute("SELECT assignee FROM tasks WHERE id = ?", (task_id,)).fetchone()
    implementer = kb._nonblank_str(run["profile"]) if run is not None else None
    if implementer is None or kb.is_human_reviewer(implementer) or implementer == reviewer:
        implementer = kb._nonblank_str(payload.get("implementer"))
    # A never-run candidate is bound to the operator-selected implementer, not
    # to the reviewer the reopen is about to assign.
    if implementer is None:
        implementer = kb._nonblank_str(row["assignee"])
    if implementer is None or kb.is_human_reviewer(implementer) or implementer == reviewer:
        raise ValueError("review reopen requires a non-reviewer implementer binding")
    # Retaining the implementer as assignee is not a new reviewer assignment.
    # Record the effective lane owner without rejecting that existing binding.
    reviewer = reviewer or kb._nonblank_str(row["assignee"])
    metadata = kb._json_dict(run["metadata"]) if run is not None else {}
    # Candidate identity is indivisible: a partial new identity must never pick
    # up the other half from an older review.
    identity = metadata if metadata.get("pr_url") or metadata.get("head_sha") else payload
    kb._append_event(conn, task_id, "review_requested", {
        "summary": kb._first_line(reason, 400),
        "implementer": implementer,
        "implementer_run_id": run_id,
        "reviewer": reviewer,
        "provenance": "implementation_run" if run is not None else "operator-bound",
        "actor": actor,
        **{key: identity[key] for key in ("pr_url", "head_sha") if identity.get(key)},
    }, run_id=run_id)


def missing_handoff(conn, task_id):
    return conn.execute(
        "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'review_requested' LIMIT 1",
        (task_id,),
    ).fetchone() is None
