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


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param({1: "a"}, id="int-keys-become-strings"),
        pytest.param(("a", "b"), id="tuples-become-lists"),
        pytest.param({1: "int", "1": "str"}, id="colliding-keys-discard-a-value"),
        pytest.param(float("nan"), id="non-finite-floats"),
    ],
)
def test_inexact_archive_is_unavailable_not_silently_transformed(
    monkeypatch, tmp_path, extra
):
    """An archive that would not round-trip exactly must be omitted.

    The declared contract is: the optional raw archive holds the EXACT
    JSON-safe execution result, or it is unavailable. A JSON round trip
    silently rewrites int keys to strings, tuples to lists, and can drop a
    value outright on key collision -- returning that transformed object as
    the original result would be a wrong answer, not a lossy convenience.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(helpers.process_registry, "completion_queue", queue.Queue())
    try:
        result = {"status": "completed", "summary": "inexact-archive", "extra": extra}
        delegation_id, worker, _ = helpers.dispatch(monkeypatch, result=result)
        worker()
        stored = ad.get_durable_delegation(delegation_id)
        # The flattened delivery answer still survives ...
        assert stored["state"] == "completed"
        event = helpers.process_registry.completion_queue.get_nowait()
        assert event["summary"] == "inexact-archive"
        # ... and the raw archive is absent rather than silently rewritten,
        # on BOTH durable paths (canonical registry and the SQLite mirror).
        with store.locked_registry() as registry:
            terminal = registry["records"][delegation_id]["terminal"]
        assert "result" not in terminal
        assert stored["result"] is None
    finally:
        ad._reset_for_tests()


def test_exact_json_native_archive_is_still_retained(monkeypatch, tmp_path):
    """The guard must not throw away results that DO round-trip exactly."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(helpers.process_registry, "completion_queue", queue.Queue())
    try:
        result = {
            "status": "completed",
            "summary": "exact-archive",
            "structured_output": {"rows": [1, 2.5, None, True], "nested": {"k": "v"}},
        }
        delegation_id, worker, _ = helpers.dispatch(monkeypatch, result=result)
        worker()
        with store.locked_registry() as registry:
            terminal = registry["records"][delegation_id]["terminal"]
        assert terminal["result"] == result
        assert terminal["result"]["structured_output"] == result["structured_output"]
        # The mirror keeps the exact same value, not a lossy projection.
        assert ad.get_durable_delegation(delegation_id)["result"] == result
    finally:
        ad._reset_for_tests()


class _HostileEq(dict):
    """JSON-serializable, but every comparison raises."""

    def __eq__(self, other):  # noqa: D105
        raise RuntimeError("hostile __eq__")

    def __ne__(self, other):  # noqa: D105
        raise RuntimeError("hostile __ne__")

    __hash__ = None


def test_hostile_equality_degrades_archive_without_losing_the_completion(
    monkeypatch, tmp_path
):
    """A raising __eq__ must not abort terminal persistence or delivery.

    exact_json_archive runs caller-supplied comparison code. If that escapes,
    append_terminal dies before writing the terminal record or queueing the
    event, and the completed work is lost entirely -- a far worse outcome than
    an unavailable OPTIONAL archive.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(helpers.process_registry, "completion_queue", queue.Queue())
    try:
        result = {
            "status": "completed",
            "summary": "hostile-eq",
            "extra": _HostileEq({"a": 1}),
        }
        delegation_id, worker, _ = helpers.dispatch(monkeypatch, result=result)
        worker()  # must not raise
        with store.locked_registry() as registry:
            record = registry["records"][delegation_id]
        assert record["state"] == "done"
        assert record["terminal"]["status"] == "completed"
        assert "result" not in record["terminal"]
        event = helpers.process_registry.completion_queue.get_nowait()
        assert event["summary"] == "hostile-eq"
        assert ad.get_durable_delegation(delegation_id)["result"] is None
    finally:
        ad._reset_for_tests()


def test_sqlite_mirror_archives_exact_and_rejects_lossy_on_the_normal_path(
    monkeypatch, tmp_path
):
    """Verify the SQLite archive directly, without forcing a mirror failure."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(helpers.process_registry, "completion_queue", queue.Queue())
    try:
        exact = {"status": "completed", "summary": "mirror-exact",
                 "structured_output": {"rows": [1, 2.5, None, True]}}
        did_exact, worker, _ = helpers.dispatch(monkeypatch, result=exact)
        worker()
        assert ad.get_durable_delegation(did_exact)["result"] == exact

        lossy = {"status": "completed", "summary": "mirror-lossy", "extra": {1: "a"}}
        did_lossy, worker, _ = helpers.dispatch(monkeypatch, result=lossy)
        worker()
        stored = ad.get_durable_delegation(did_lossy)
        assert stored["result"] is None
        assert stored["state"] == "completed"
    finally:
        ad._reset_for_tests()


class _LyingEq(dict):
    """JSON-serializable, integer keys, and __eq__ that always claims equal."""

    def __eq__(self, other):  # noqa: D105
        return True

    __hash__ = None


class _LyingBool:
    def __bool__(self):
        raise RuntimeError("hostile __bool__")


class _BoolTrapEq(dict):
    """__eq__ returns an object whose __bool__ raises."""

    def __eq__(self, other):  # noqa: D105
        return _LyingBool()

    __hash__ = None


@pytest.mark.parametrize("factory", [_LyingEq, _BoolTrapEq],
                         ids=["lying-eq", "raising-bool"])
def test_caller_equality_cannot_certify_a_transformed_archive(
    monkeypatch, tmp_path, factory
):
    """Exactness must be decided structurally, not by caller-controlled __eq__.

    A dict subclass serializes fine, gets rewritten by JSON (int key 1 -> "1"),
    and can still claim equality -- which would archive transformed data as the
    original execution result. A __eq__ returning an object with a raising
    __bool__ must likewise degrade, not abort the completion.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(helpers.process_registry, "completion_queue", queue.Queue())
    try:
        result = {"status": "completed", "summary": "lying-archive",
                  "extra": factory({1: "a"})}
        delegation_id, worker, _ = helpers.dispatch(monkeypatch, result=result)
        worker()  # must not raise
        with store.locked_registry() as registry:
            record = registry["records"][delegation_id]
        assert record["state"] == "done"
        assert "result" not in record["terminal"]
        assert ad.get_durable_delegation(delegation_id)["result"] is None
        event = helpers.process_registry.completion_queue.get_nowait()
        assert event["summary"] == "lying-archive"
    finally:
        ad._reset_for_tests()


@pytest.mark.parametrize(
    "mutate,label",
    [
        (lambda rec: rec.__setitem__("created_at", "bad"), "created_at"),
        (lambda rec: rec["attempt"].__setitem__("redispatch_count", "bad"),
         "redispatch_count"),
        (lambda rec: rec["attempt"].__setitem__("generation", "bad"), "generation"),
        (lambda rec: rec.__setitem__("attempt", [1]), "attempt-not-dict"),
        (lambda rec: rec.__setitem__("terminal", {"status": "completed",
                                                  "completed_at": True}),
         "bool-timestamp"),
    ],
)
def test_malformed_scalars_are_quarantined_not_profile_fatal(
    monkeypatch, tmp_path, mutate, label
):
    """One bad record must not abort recovery for every healthy delegation."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(helpers.process_registry, "completion_queue", queue.Queue())
    try:
        bad, _, _ = helpers.dispatch(monkeypatch)
        good, _, _ = helpers.dispatch(monkeypatch)
        with store.locked_registry() as registry:
            mutate(registry["records"][bad])
        claimed, summary = store.claim_recoveries(
            current_boot_id="200:2", resume_enabled=True, owner_alive=lambda _: False
        )
        assert [record["delegation_id"] for record in claimed] == [good]
        assert summary["failed_validation"] == 1
        # The replay rail must survive the same record.
        store.enqueue_pending_outbox(current_boot_id="replay-probe")
    finally:
        ad._reset_for_tests()


def test_historical_terminal_without_self_id_still_replays(monkeypatch, tmp_path):
    """Sparse historical records omit the redundant record-level id.

    The loader permits that, so canonical_terminal must too -- otherwise a real
    completed result is rejected on both replay rails and never delivered.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(helpers.process_registry, "completion_queue", queue.Queue())
    try:
        delegation_id, worker, _ = helpers.dispatch(monkeypatch)
        worker()
        with store.locked_registry() as registry:
            record = registry["records"][delegation_id]
            record.pop("delegation_id", None)
            for event in record.get("outbox", []):
                event["state"] = "pending"
            record["integrity"] = store._record_checksum(record)
        payloads = store.enqueue_pending_outbox(current_boot_id="replay-probe")
        assert [p["delegation_id"] for p in payloads] == [delegation_id]
    finally:
        ad._reset_for_tests()


def test_conflicting_self_id_is_still_rejected(monkeypatch, tmp_path):
    """Permitting an ABSENT id must not permit a WRONG one."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(helpers.process_registry, "completion_queue", queue.Queue())
    try:
        delegation_id, worker, _ = helpers.dispatch(monkeypatch)
        worker()
        with store.locked_registry() as registry:
            record = registry["records"][delegation_id]
            record["delegation_id"] = "deleg_someone_else"
            for event in record.get("outbox", []):
                event["state"] = "pending"
            record["integrity"] = store._record_checksum(record)
        payloads = store.enqueue_pending_outbox(current_boot_id="replay-probe")
        assert payloads == []
    finally:
        ad._reset_for_tests()
