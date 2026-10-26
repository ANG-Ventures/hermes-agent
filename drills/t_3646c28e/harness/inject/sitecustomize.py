# t_3646c28e fault injection (scratch drill only). Inert unless T3646_BLOCK_CANCEL_ARCHIVE=1 and this
# process is the gateway: the /stop cancel's archive never lands (models "gateway exits before the
# archive thread runs", 9ef07565e630). The harness SIGKILLs this gateway once the registry says cancelled.
import os
import sys

if os.environ.get("T3646_BLOCK_CANCEL_ARCHIVE") == "1" and "gateway.run" in " ".join(getattr(sys, "orig_argv", None) or sys.argv):
    try:
        import threading
        import tools.async_delegation_kanban as _akb

        def _blocked(task_id):
            sys.stderr.write(f"T3646_INJECT archive of {task_id} blocked (gateway will be killed)\n")
            threading.Event().wait()

        _akb._archive_quietly = _blocked
        sys.stderr.write("T3646_INJECT installed: cancel archive blocked\n")
    except Exception as exc:  # never break the gateway boot
        sys.stderr.write(f"T3646_INJECT failed: {exc!r}\n")
