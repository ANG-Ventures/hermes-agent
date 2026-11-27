"""Release constructed, unstarted children after background admission refusal."""

import json
import logging

logger = logging.getLogger(__name__)


def reject_background_admission(*, reason, children, task_list, live_deleg_id, live_writers,
                                dispatch=None):
    from hermes_cli.observability.shared_metrics_loop import finish_delegation_unit
    from tools.delegate_tool import _door_lock, _door_slot, _teardown
    from tools.delegation_live_log import update_manifest_statuses

    results = []
    cleanup_pending = []

    def cleanup(label, operation):
        try:
            if operation() is False:
                cleanup_pending.append(label)
        except Exception:
            cleanup_pending.append(label)
            logger.warning("Rejected delegation cleanup failed: %s", label, exc_info=True)

    for index, child in enumerate(children):
        def close_child():
            _teardown(child, "admission_rejected")
            # False from teardown also means an idempotent already-closed no-op.
            with _door_lock:
                return _door_slot(child)["closed"]

        cleanup(f"child_{index}", close_child)
        entry = {"task_index": index, "status": "rejected", "reason": reason,
                 "summary": "Background admission rejected; child did not start.",
                 "api_calls": 0, "duration_seconds": 0.0}
        results.append(entry)
        if index < len(live_writers) and live_writers[index] is not None:
            try:
                live_writers[index].finalize(entry)
            except Exception:
                logger.debug("Rejected transcript finalize failed", exc_info=True)
    cleanup("manifest", lambda: update_manifest_statuses(live_deleg_id, results))
    cleanup("metrics", lambda: finish_delegation_unit(task_list, results, background=True))
    if dispatch and dispatch.get("fallback_event_id"):
        from tools.async_delegation import acknowledge_outbox_event

        cleanup("outbox_ack", lambda: acknowledge_outbox_event(
            dispatch["fallback_event_id"], outcome="dropped", reason="admission_rejected",
            profile_home=dispatch.get("_registry_profile_home") or None,
        ))
    return json.dumps({
        "status": "rejected", "mode": "background", "started": False,
        "count": len(task_list), "goals": [task["goal"] for task in task_list],
        "error": (dispatch or {}).get("error") or "background delegation was not admitted",
        "reason": reason, "retryable": True,
        **({"cleanup_pending": cleanup_pending} if cleanup_pending else {}),
        "note": "No subagents started or queued. Continue the parent conversation; retry "
                "delegation after capacity or persistence recovers. Do not wait or poll.",
    }, ensure_ascii=False)
