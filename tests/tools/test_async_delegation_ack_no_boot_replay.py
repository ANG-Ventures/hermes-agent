"""A completion accepted through the consumer claim path must not replay on the next boot.

Incident behind fork PR #683: delivered async-delegation completions were re-injected
after every gateway boot because the consumer acknowledged only one of the durable
stores. Three real interpreters against one temp HERMES_HOME = three process lifetimes.
The same file runs on upstream (SQLite ledger only) and on the fork (SQLite ledger +
durable JSON registry/outbox), so it answers "does the replay exist on this tree?".
"""
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Where the tree has a durable JSON registry (fork: ``durable_spec``), dispatch through it
# exactly as delegate_tool does for gateway background delegations.
DURABLE_SPEC = {
    "profile": "default",
    "source": {"kind": "single", "shared_context": None, "tasks": [{
        "goal": "replay", "context": None, "role": "leaf", "inherit_context": False}]},
    "execution": {
        "model": "m", "provider": "test-provider", "base_url": "https://example.invalid/v1",
        "api_mode": "chat_completions", "acp_command": None, "acp_args": [],
        "reasoning_config": None, "fallback_chain": None, "service_tier": None,
        "provider_preferences": None, "toolsets": ["file"], "max_iterations": 50,
        "parent_depth": 0, "workspace_hint": "/tmp",
        "credential_ref": {"provider": "test-provider", "custom_provider": None}},
    "route": {
        "session_key": "owner-session", "parent_session_id": "durable-parent",
        "origin_ui_session_id": "", "platform": "telegram", "chat_type": "dm",
        "chat_id": "123", "thread_id": None, "user_id": "u1", "user_name": "Ace",
        "profile": "default"},
}

PRODUCER = r'''
import inspect, json, sys, time
from tools import async_delegation as ad
extra = {}
if "durable_spec" in inspect.signature(ad.dispatch_async_delegation).parameters:
    extra = {"durable_spec": json.loads(sys.argv[1]), "current_boot_id": "boot-producer"}
r = ad.dispatch_async_delegation(
    goal="replay", context=None, toolsets=None, role="leaf", model="m",
    session_key="owner-session", parent_session_id="durable-parent",
    runner=lambda: {"status": "completed", "summary": "delivered once"}, **extra,
)
assert r.get("status") != "rejected", r
deadline = time.time() + 10
while ad.active_count() and time.time() < deadline:
    time.sleep(.01)
print(r["delegation_id"])
'''

# Boot replay = every durable store the gateway re-offers at startup: the SQLite ledger
# (process_registry import) plus, where the tree has one, the JSON outbox the gateway
# replays per boot (GatewayRunner async-delegation recovery -> enqueue_pending_outbox).
BOOT = r'''
import json, sys
from tools import async_delegation as ad
from tools.process_registry import process_registry
if hasattr(ad, "enqueue_pending_outbox"):
    ad.enqueue_pending_outbox(current_boot_id=sys.argv[1])
events = []
while not process_registry.completion_queue.empty():
    events.append(process_registry.completion_queue.get_nowait())
'''

CONSUMER = BOOT + r'''
accepted = []
for evt in events:
    claim = ad.claim_event_delivery(evt, "gateway")
    if claim is None:
        continue  # another copy of the same completion already holds/settled it
    ad.complete_event_delivery(evt, claim)
    accepted.append(evt["delegation_id"])
print(json.dumps(sorted(set(accepted))))
'''

NEXT_BOOT = BOOT + r'''
print(json.dumps([[e.get("type"), e.get("event_id")] for e in events]))
'''

# Retention prune of the delivered SQLite row (``_prune_durable_records`` deletes delivered
# rows first). Without a row, a claim is granted as "legacy", so any still-pending durable
# copy is injected again: the user-visible duplicate the incident reported.
PRUNE_DELIVERED_ROW = r'''
import sqlite3, sys
from tools import async_delegation as ad
with ad._transaction() as conn:
    n = conn.execute("DELETE FROM async_delegations WHERE delegation_id=? AND delivery_state='delivered'",
                     (sys.argv[1],)).rowcount
print(n)
'''


def _run(code, env, *args):
    out = subprocess.run([sys.executable, "-c", code, *args], cwd=REPO, env=env,
                         text=True, capture_output=True, timeout=30)
    assert out.returncode == 0, out.stderr[-4000:]
    return out.stdout.strip().splitlines()[-1]


def test_accepted_completion_is_not_replayed_on_next_boot(tmp_path):
    env = {**os.environ, "HERMES_HOME": str(tmp_path), "PYTHONPATH": REPO}
    delegation_id = _run(PRODUCER, env, json.dumps(DURABLE_SPEC))
    assert json.loads(_run(CONSUMER, env, "boot-consumer")) == [delegation_id]
    assert json.loads(_run(NEXT_BOOT, env, "boot-next")) == []


def test_accepted_completion_is_not_injected_again_after_ledger_prune(tmp_path):
    env = {**os.environ, "HERMES_HOME": str(tmp_path), "PYTHONPATH": REPO}
    delegation_id = _run(PRODUCER, env, json.dumps(DURABLE_SPEC))
    assert json.loads(_run(CONSUMER, env, "boot-consumer")) == [delegation_id]
    assert _run(PRUNE_DELIVERED_ROW, env, delegation_id) == "1"
    assert json.loads(_run(CONSUMER, env, "boot-next")) == []
