"""Real file-path and malformed-record isolation boundaries."""
import json
import queue
import time

import pytest

from tests.tools import test_async_delegation_terminal_receipts as helpers
from tools import async_delegation as ad
from tools import async_delegation_store as store


@pytest.mark.parametrize("retarget", ["cwd", "symlink"])
def test_registry_and_lock_share_one_resolved_home(monkeypatch, tmp_path, retarget):
    first = tmp_path / "first"
    second = tmp_path / "second"
    for home in (first, second):
        (home / "profile" / "state").mkdir(parents=True)
    monkeypatch.chdir(first)
    link = tmp_path / "profile-link"
    if retarget == "cwd":
        home_arg = "profile"
    else:
        link.symlink_to(first / "profile", target_is_directory=True)
        home_arg = str(link)
    monkeypatch.setenv("HERMES_HOME", home_arg)
    original = store.registry_path

    def resolve_then_retarget(profile_home=None):
        captured = original(profile_home)
        if retarget == "cwd":
            monkeypatch.chdir(second)
        else:
            link.unlink()
            link.symlink_to(second / "profile", target_is_directory=True)
        return captured

    monkeypatch.setattr(store, "registry_path", resolve_then_retarget)
    with store.locked_registry():
        assert (first / "profile/state/async-delegations.lock").exists()
        assert not (second / "profile/state/async-delegations.lock").exists()
    assert (first / "profile/state/async-delegations.json").exists()
    assert not (second / "profile/state/async-delegations.json").exists()


@pytest.mark.parametrize("damage", ["missing-id", "null-outbox", "non-object-entry"])
def test_bad_active_record_is_quarantined_before_recovery_and_replay(
    monkeypatch, tmp_path, damage
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(helpers.process_registry, "completion_queue", queue.Queue())
    try:
        bad, _, _ = helpers.dispatch(monkeypatch)
        good, _, _ = helpers.dispatch(monkeypatch)
        with store.locked_registry() as registry:
            record = registry["records"][bad]
            record["created_at"] = time.time() - store.ACTIVE_STALE_SECONDS - 1
            if damage == "missing-id":
                record.pop("delegation_id")
            elif damage == "null-outbox":
                record["outbox"] = None
            else:
                record["outbox"] = [None]
        before = json.loads(store.registry_path().read_text())["records"][bad]
        claimed, summary = store.claim_recoveries(
            current_boot_id="200:2", resume_enabled=True, owner_alive=lambda _: False
        )
        assert [record["delegation_id"] for record in claimed] == [good]
        assert summary["failed_validation"] == 1
        payloads = store.enqueue_pending_outbox(current_boot_id="replay-probe")
        assert [event["delegation_id"] for event in payloads] == [good]
        after = json.loads(store.registry_path().read_text())["records"][bad]
        assert after == before  # Quarantine does not repair or reseal the bad record.
    finally:
        ad._reset_for_tests()


@pytest.mark.parametrize("state", [[], {}, None, 1])
@pytest.mark.parametrize("valid_checksum", [False, True])
def test_malformed_state_does_not_block_healthy_replay(
    monkeypatch, tmp_path, state, valid_checksum
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(helpers.process_registry, "completion_queue", queue.Queue())
    try:
        bad, worker, _ = helpers.dispatch(monkeypatch)
        worker()
        good, worker, _ = helpers.dispatch(monkeypatch)
        worker()
        for _ in range(2):
            helpers.process_registry.completion_queue.get_nowait()
        path = store.registry_path()
        registry = json.loads(path.read_text())
        damaged = registry["records"][bad]
        damaged["state"] = state
        if valid_checksum:
            damaged["integrity"] = store._record_checksum(damaged)
        path.write_text(json.dumps(registry))

        assert ad.enqueue_pending_outbox(current_boot_id="state-replay") == 1
        events = list(helpers.process_registry.completion_queue.queue)
        assert [event["delegation_id"] for event in events] == [good]
        claimed, summary = store.claim_recoveries(
            current_boot_id="200:2", resume_enabled=True, owner_alive=lambda _: False
        )
        assert claimed == []
        assert summary["failed_validation"] == 1
        restored = queue.Queue()
        assert ad.restore_undelivered_completions(restored) == 1
        assert [event["delegation_id"] for event in restored.queue] == [good]
        claim = ad.claim_event_delivery(events[0], "state-consumer")
        assert claim
        ad.complete_event_delivery(events[0], claim)
        assert helpers.row(good)["delivery_state"] == "delivered"
        assert ad.enqueue_pending_outbox(current_boot_id="next-boot") == 0
        assert ad.restore_undelivered_completions(queue.Queue()) == 0
        assert json.loads(path.read_text())["records"][bad] == damaged
        with pytest.raises(store.RegistryError):
            store.read_registry()
    finally:
        ad._reset_for_tests()
