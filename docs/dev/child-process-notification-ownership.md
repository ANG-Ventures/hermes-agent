# Child processes and chat-route ownership

A process's `owner_task_id` controls cleanup and handoff. Its `parent_session_id`
is the durable session that launched it, not proof of ownership of a human chat.
In delegated execution this is the child session, even though inherited transport
metadata names the parent's chat. Never replace it with a global environment ID.

Delegated terminal spawns disable completion, pattern and heartbeat delivery before
spawning or registering watchers. Their output remains pollable; running work is
not killed by this policy. Yielded foreground commands use the same policy before
adoption starts its reader. Checkpoints therefore cannot recover an armed child
notification from these new spawns. Existing legacy child events cannot pin a chat:
the gateway checks `source=tool` and durable `_delegate_from` creation provenance,
including provenance inherited by a child's compression continuation.

Explicit process handoff is different: the live subagent registry and parent weak
reference validate the transfer. Under the process lock it changes the owner and
sets notification ownership to that verified parent's durable session ID, arming
completion delivery even when the child originally spawned silently. Gateway routing
is read from the active session-store entry for that verified parent, not from the
child's inherited transport context. A missing parent route refuses handoff. The
watcher is registered as part of the transfer and its routing is checkpointed.
Requested-notify child spawns keep file-backed output, even while chat delivery
is disabled. Their separate `completion_requested` obligation keeps unread
exit codes/output in the delegation result; reading the result discharges it.

A child-targeted durable completion is deferred before adapter admission, not
acknowledged as delivered or silently discarded. The output/obligation remains for
operator resolution. Missing sessions and user-closed sessions retain existing
terminal dispositions; compression and route CAS/generation guards remain in force.

Regression commands use `test-gate --remote <python> -m pytest` with these files:
`tests/tools/test_terminal_watcher_arming.py`,
`tests/tools/test_subagent_process_handoff.py`, and
`tests/gateway/test_async_delegation_session_binding.py`.
Restore each production boundary independently from the pre-fix base to reproduce
the false notification flag/queued event and leased-child route takeover.
