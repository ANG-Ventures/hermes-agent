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


@pytest.mark.parametrize("depth", [600], ids=["deep-acyclic"])
def test_deep_acyclic_result_never_loses_the_completion(monkeypatch, tmp_path, depth):
    """A deeply nested but ORDINARY result must not abort finalization.

    json.dumps handles this fine; only our structural traversal hits the
    recursion limit. The archive is optional, so the whole decision -- traversal
    included -- must sit inside the guard. Before containment this raised
    RecursionError and left the record running with no terminal, no outbox
    entry and nothing queued.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(helpers.process_registry, "completion_queue", queue.Queue())
    try:
        nested: object = "leaf"
        for _ in range(depth):
            nested = [nested]
        result = {"status": "completed", "summary": "deep-acyclic", "extra": nested}
        delegation_id, worker, _ = helpers.dispatch(monkeypatch, result=result)
        worker()  # must not raise
        with store.locked_registry() as registry:
            record = registry["records"][delegation_id]
        assert record["state"] == "done"
        assert record["terminal"]["status"] == "completed"
        assert len(record.get("outbox") or []) == 1
        event = helpers.process_registry.completion_queue.get_nowait()
        assert event["summary"] == "deep-acyclic"
        # Both durable paths agree: the flattened answer survives either way.
        stored = ad.get_durable_delegation(delegation_id)
        assert stored["state"] == "completed"
        assert stored["result"] in (None, result)
    finally:
        ad._reset_for_tests()


def test_lone_surrogate_result_degrades_instead_of_losing_the_completion(
    monkeypatch, tmp_path
):
    """The archive validator must use the registry's OWN encoding.

    json.dumps defaults to ensure_ascii=True, which encodes a lone surrogate
    happily. _record_checksum serializes with ensure_ascii=False and UTF-8
    encodes, which raises UnicodeEncodeError -- aborting the terminal write
    before the record or outbox event exist, losing a completed job.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(helpers.process_registry, "completion_queue", queue.Queue())
    try:
        result = {"status": "completed", "summary": "surrogate", "extra": "\ud800"}
        delegation_id, worker, _ = helpers.dispatch(monkeypatch, result=result)
        worker()  # must not raise
        with store.locked_registry() as registry:
            record = registry["records"][delegation_id]
        assert record["state"] == "done"
        assert "result" not in record["terminal"]
        assert len(record.get("outbox") or []) == 1
        event = helpers.process_registry.completion_queue.get_nowait()
        assert event["summary"] == "surrogate"
    finally:
        ad._reset_for_tests()


def test_batch_child_extra_cannot_abort_the_mandatory_envelope(monkeypatch, tmp_path):
    """The delivery envelope is mandatory and must always be persistable.

    A batch child carrying an unsupported value previously flowed straight into
    the outbox payload, so _record_checksum raised and the completed batch was
    left running with no terminal record and nothing queued.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(helpers.process_registry, "completion_queue", queue.Queue())
    try:
        result = {"results": [{"status": "completed", "summary": "child-ok",
                               "extra": object()}]}
        delegation_id, worker, _ = helpers.dispatch(
            monkeypatch, batch=True, result=result)
        worker()  # must not raise
        with store.locked_registry() as registry:
            record = registry["records"][delegation_id]
        assert record["state"] == "done"
        assert len(record.get("outbox") or []) == 1
        event = helpers.process_registry.completion_queue.get_nowait()
        assert event["is_batch"] is True
        # The child's usable answer survives even though its extra did not.
        assert event["results"][0]["summary"] == "child-ok"
        assert event["results"][0]["status"] == "completed"
    finally:
        ad._reset_for_tests()


def test_exact_batch_children_are_preserved_unchanged(monkeypatch, tmp_path):
    """Positive control: a fully JSON-native batch keeps its children intact."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(helpers.process_registry, "completion_queue", queue.Queue())
    try:
        children = [{"status": "completed", "summary": "a",
                     "structured_output": {"rows": [1, 2]}},
                    {"status": "completed", "summary": "b"}]
        delegation_id, worker, _ = helpers.dispatch(
            monkeypatch, batch=True, result={"results": children})
        worker()
        event = helpers.process_registry.completion_queue.get_nowait()
        assert event["results"] == children
        assert delegation_id
    finally:
        ad._reset_for_tests()


@pytest.mark.parametrize("field", ["created_at", "updated_at"])
def test_unconvertible_timestamp_is_quarantined_not_profile_fatal(
    monkeypatch, tmp_path, field
):
    """A checksum-VALID timestamp the consumers cannot convert must quarantine.

    10**400 is under the 4300-digit JSON limit, so it writes through
    locked_registry with a valid checksum and passes a strict load. The recovery
    loop then calls float() on it and raises OverflowError mid-scan, aborting
    recovery for every healthy delegation in the profile.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(helpers.process_registry, "completion_queue", queue.Queue())
    try:
        bad, _, _ = helpers.dispatch(monkeypatch)
        good, _, _ = helpers.dispatch(monkeypatch)
        with store.locked_registry() as registry:
            registry["records"][bad][field] = 10 ** 400
        # Precondition: this really is a valid, strictly-loadable record.
        raw = json.loads(store.registry_path().read_text())["records"][bad]
        assert raw["integrity"] == store._record_checksum(raw)
        claimed, summary = store.claim_recoveries(
            current_boot_id="200:2", resume_enabled=True, owner_alive=lambda _: False
        )
        assert good in [record["delegation_id"] for record in claimed]
        assert summary["failed_validation"] == 1
        store.enqueue_pending_outbox(current_boot_id="replay-probe")
    finally:
        ad._reset_for_tests()


def test_unencodable_summary_cannot_abort_the_completion(monkeypatch, tmp_path):
    """summary/error ride in the mandatory envelope, same class as batch extras."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(helpers.process_registry, "completion_queue", queue.Queue())
    try:
        result = {"status": "completed", "summary": "bad \ud800 summary"}
        delegation_id, worker, _ = helpers.dispatch(monkeypatch, result=result)
        worker()  # must not raise
        with store.locked_registry() as registry:
            record = registry["records"][delegation_id]
        assert record["state"] == "done"
        assert len(record.get("outbox") or []) == 1
        event = helpers.process_registry.completion_queue.get_nowait()
        assert event["status"] == "completed"
    finally:
        ad._reset_for_tests()


def test_ordinary_timestamps_and_summaries_are_untouched(monkeypatch, tmp_path):
    """Positive control: the guards must not refuse legitimate values."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(helpers.process_registry, "completion_queue", queue.Queue())
    try:
        result = {"status": "completed", "summary": "a normal summary"}
        delegation_id, worker, _ = helpers.dispatch(monkeypatch, result=result)
        worker()
        claimed, summary = store.claim_recoveries(
            current_boot_id="200:2", resume_enabled=True, owner_alive=lambda _: False
        )
        assert summary["failed_validation"] == 0
        event = helpers.process_registry.completion_queue.get_nowait()
        assert event["summary"] == "a normal summary"
        assert delegation_id
    finally:
        ad._reset_for_tests()


def test_unencodable_error_survives_dispatch_acceptance_and_no_replay(
    monkeypatch, tmp_path
):
    """A FAILING worker with an unencodable error must not lose the job.

    terminal.error is a second mandatory copy of the same string; guarding only
    the envelope left this path raising UnicodeEncodeError during the
    whole-record checksum, with state=running and nothing queued. Covers the
    full lane: dispatch -> claimed acceptance -> no replay.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(helpers.process_registry, "completion_queue", queue.Queue())
    try:
        result = {"status": "error", "error": "bad \ud800 error", "summary": None}
        delegation_id, worker, _ = helpers.dispatch(monkeypatch, result=result)
        worker()  # must not raise
        with store.locked_registry() as registry:
            record = registry["records"][delegation_id]
        assert record["state"] == "failed"
        assert record["terminal"]["status"] == "error"
        assert len(record.get("outbox") or []) == 1

        event = helpers.process_registry.completion_queue.get_nowait()
        assert event["status"] == "error"
        claim = ad.claim_event_delivery(event, "test-consumer")
        assert claim
        ad.complete_event_delivery(event, claim)
        assert ad.get_durable_delegation(delegation_id)["delivery_state"] == "delivered"

        # An accepted terminal must not replay on either rail.
        assert store.enqueue_pending_outbox(current_boot_id="replay-probe") == []
        assert ad.restore_undelivered_completions(queue.Queue()) == 0
    finally:
        ad._reset_for_tests()


def test_ordinary_error_string_is_preserved_verbatim(monkeypatch, tmp_path):
    """Positive control: a normal error must reach both copies unchanged."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(helpers.process_registry, "completion_queue", queue.Queue())
    try:
        result = {"status": "error", "error": "worker exploded", "summary": None}
        delegation_id, worker, _ = helpers.dispatch(monkeypatch, result=result)
        worker()
        with store.locked_registry() as registry:
            record = registry["records"][delegation_id]
        assert record["terminal"]["error"] == "worker exploded"
        event = helpers.process_registry.completion_queue.get_nowait()
        assert event["error"] == "worker exploded"
    finally:
        ad._reset_for_tests()


@pytest.mark.parametrize(
    "field", ["model", "exit_reason", "api_calls", "duration_seconds", "status"])
def test_no_runner_field_can_abort_the_mandatory_envelope(monkeypatch, tmp_path, field):
    """EVERY runner-controlled envelope field, not just summary/error.

    Guarding fields one at a time left model, exit_reason, api_calls,
    duration_seconds and status each able to abort the terminal write and lose
    a completed job. status reached the record through the lifecycle event
    message as well as the envelope.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(helpers.process_registry, "completion_queue", queue.Queue())
    try:
        result = {"status": "completed", "summary": "ok", field: "\ud800"}
        delegation_id, worker, _ = helpers.dispatch(monkeypatch, result=result)
        worker()  # must not raise
        with store.locked_registry() as registry:
            record = registry["records"][delegation_id]
        assert record["state"] in {"done", "failed"}
        assert len(record.get("outbox") or []) == 1
        assert helpers.process_registry.completion_queue.get_nowait()
    finally:
        ad._reset_for_tests()


def test_degraded_batch_child_keeps_its_task_index(monkeypatch, tmp_path):
    """task_index is the child's IDENTITY.

    Dropping it while degrading an unrepresentable child renders that child's
    result under a DIFFERENT task's goal -- silent misattribution.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(helpers.process_registry, "completion_queue", queue.Queue())
    try:
        result = {"results": [
            {"task_index": 0, "goal": "first goal", "status": "completed",
             "summary": "A", "extra": object()},
            {"task_index": 1, "goal": "second goal", "status": "completed",
             "summary": "B"},
        ]}
        delegation_id, worker, _ = helpers.dispatch(
            monkeypatch, batch=True, result=result)
        worker()
        event = helpers.process_registry.completion_queue.get_nowait()
        children = event["results"]
        assert [child["task_index"] for child in children] == [0, 1]
        assert children[0]["summary"] == "A"
        assert children[0]["goal"] == "first goal"
        assert delegation_id
    finally:
        ad._reset_for_tests()


def test_non_list_results_container_cannot_strand_the_completion(monkeypatch, tmp_path):
    """A JSON-valid but non-iterable ``results`` must not abort finalization.

    {"results": 1} raised TypeError while iterating -- in the batch classifier
    AND in the envelope builder -- before the terminal record and outbox
    existed, durably stranding a completed job as running.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(helpers.process_registry, "completion_queue", queue.Queue())
    try:
        result = {"status": "completed", "summary": "ok", "results": 1}
        delegation_id, worker, _ = helpers.dispatch(
            monkeypatch, batch=True, result=result)
        worker()  # must not raise
        with store.locked_registry() as registry:
            assert registry["records"][delegation_id]["state"] == "done"
        event = helpers.process_registry.completion_queue.get_nowait()
        assert event["status"] == "completed"
        # The scalar must survive as DATA, and every child must stay a mapping
        # so the real consumer can format it (see the formatter test below).
        assert len(event["results"]) == 1
        child = event["results"][0]
        assert type(child) is dict
        assert child.get("value") == 1
    finally:
        ad._reset_for_tests()


def test_unhashable_status_cannot_strand_the_completion(monkeypatch, tmp_path):
    """An unhashable status raised on the set-membership test."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(helpers.process_registry, "completion_queue", queue.Queue())
    try:
        result = {"status": ["completed"], "summary": "done"}
        delegation_id, worker, _ = helpers.dispatch(monkeypatch, result=result)
        worker()  # must not raise
        with store.locked_registry() as registry:
            record = registry["records"][delegation_id]
        assert record["state"] == "failed"
        assert len(record.get("outbox") or []) == 1
        assert helpers.process_registry.completion_queue.get_nowait()
    finally:
        ad._reset_for_tests()


def test_json_null_child_is_preserved_not_replaced_by_a_fabricated_error(
    monkeypatch, tmp_path
):
    """JSON null is a VALID result, distinct from an unavailable archive.

    exact_json_archive returns None for both, so using it as the validity test
    replaced a legitimate null child with a fabricated error object -- handing
    the user a wrong result.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(helpers.process_registry, "completion_queue", queue.Queue())
    try:
        result = {"results": [
            None, {"task_index": 1, "status": "completed", "summary": "B"}]}
        delegation_id, worker, _ = helpers.dispatch(
            monkeypatch, batch=True, result=result)
        worker()
        event = helpers.process_registry.completion_queue.get_nowait()
        # A valid JSON null must NOT become a fabricated error, and must stay
        # formattable: it is carried as data on a mapping child.
        first = event["results"][0]
        assert type(first) is dict
        assert first.get("value") is None
        assert "error" not in first
        assert event["results"][1]["summary"] == "B"
        assert event["status"] == "completed"
        assert delegation_id
    finally:
        ad._reset_for_tests()


def test_hostile_dict_subclass_child_cannot_abort_finalization(monkeypatch, tmp_path):
    """A dict SUBCLASS can override get/__eq__ and raise during extraction."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(helpers.process_registry, "completion_queue", queue.Queue())

    class Hostile(dict):
        def get(self, key, default=None):
            raise RuntimeError("hostile get")

    try:
        hostile = Hostile(task_index=0, status="completed", summary="A",
                          extra=object())
        healthy = {"task_index": 1, "status": "completed",
                   "summary": "HEALTHY-SIBLING"}
        delegation_id, worker, _ = helpers.dispatch(
            monkeypatch, batch=True, result={"results": [hostile, healthy]})
        worker()  # must not raise
        with store.locked_registry() as registry:
            assert registry["records"][delegation_id]["state"] == "done"

        event = helpers.process_registry.completion_queue.get_nowait()
        # A hostile neighbour must NOT destroy a healthy sibling's real answer.
        # Asserting only "an event exists" let this bug through once already.
        assert event["status"] == "completed"
        assert len(event["results"]) == 2
        survivor = event["results"][1]
        assert survivor["task_index"] == 1
        assert survivor["summary"] == "HEALTHY-SIBLING"
        assert event["results"][0]["status"] == "error"

        # ...and it must survive real acceptance with no replay on either rail.
        claim = ad.claim_event_delivery(event, "test-consumer")
        assert claim
        ad.complete_event_delivery(event, claim)
        assert ad.get_durable_delegation(delegation_id)["delivery_state"] == "delivered"
        assert store.enqueue_pending_outbox(current_boot_id="replay-probe") == []
        assert ad.restore_undelivered_completions(queue.Queue()) == 0
    finally:
        ad._reset_for_tests()


def test_raising_status_eq_cannot_destroy_a_healthy_sibling(monkeypatch, tmp_path):
    """Guarding the LOOKUP is not enough: the COMPARISON is untrusted too.

    A plain dict can carry a status whose __eq__ raises, so membership blew up
    after a successful get(). The handler then discarded every child, and the
    wrong result was accepted on both stores.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(helpers.process_registry, "completion_queue", queue.Queue())

    class RaisingEq(str):
        def __eq__(self, other):
            raise RuntimeError("hostile eq")

        def __ne__(self, other):
            raise RuntimeError("hostile ne")

        def __hash__(self):
            return 0

    try:
        hostile = {"task_index": 0, "status": RaisingEq("completed"),
                   "summary": "A"}
        healthy = {"task_index": 1, "status": "completed",
                   "summary": "HEALTHY-SIBLING"}
        delegation_id, worker, _ = helpers.dispatch(
            monkeypatch, batch=True, result={"results": [hostile, healthy]})
        worker()  # must not raise
        with store.locked_registry() as registry:
            assert registry["records"][delegation_id]["state"] == "done"

        event = helpers.process_registry.completion_queue.get_nowait()
        assert event["status"] == "completed"
        assert len(event["results"]) == 2
        survivor = event["results"][1]
        assert survivor["task_index"] == 1
        assert survivor["summary"] == "HEALTHY-SIBLING"

        claim = ad.claim_event_delivery(event, "test-consumer")
        assert claim
        ad.complete_event_delivery(event, claim)
        assert ad.get_durable_delegation(delegation_id)["delivery_state"] == "delivered"
        assert store.enqueue_pending_outbox(current_boot_id="replay-probe") == []
        assert ad.restore_undelivered_completions(queue.Queue()) == 0
    finally:
        ad._reset_for_tests()


def test_self_returning_str_subclass_cannot_destroy_a_sibling(monkeypatch, tmp_path):
    """str() is NOT a safe coercion.

    A str subclass whose __str__ returns self hands the hostile object back, so
    its raising __eq__ escaped the guard and destroyed every child again. The
    coercion must be VERIFIED to have produced an exact str.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(helpers.process_registry, "completion_queue", queue.Queue())

    class SelfStr(str):
        def __eq__(self, other):
            raise RuntimeError("hostile eq")

        def __ne__(self, other):
            raise RuntimeError("hostile ne")

        def __hash__(self):
            return 0

        def __str__(self):
            return self

    try:
        hostile = {"task_index": 0, "status": SelfStr("completed"),
                   "summary": "A"}
        healthy = {"task_index": 1, "status": "completed",
                   "summary": "HEALTHY-SIBLING"}
        delegation_id, worker, _ = helpers.dispatch(
            monkeypatch, batch=True, result={"results": [hostile, healthy]})
        worker()  # must not raise
        with store.locked_registry() as registry:
            assert registry["records"][delegation_id]["state"] == "done"

        event = helpers.process_registry.completion_queue.get_nowait()
        assert event["status"] == "completed"
        assert len(event["results"]) == 2
        survivor = event["results"][1]
        assert survivor["task_index"] == 1
        assert survivor["summary"] == "HEALTHY-SIBLING"

        claim = ad.claim_event_delivery(event, "test-consumer")
        assert claim
        ad.complete_event_delivery(event, claim)
        assert ad.get_durable_delegation(delegation_id)["delivery_state"] == "delivered"
        assert store.enqueue_pending_outbox(current_boot_id="replay-probe") == []
        assert ad.restore_undelivered_completions(queue.Queue()) == 0
    finally:
        ad._reset_for_tests()


class _HostileClass:
    """__class__ is a property that raises -- defeats isinstance().

    Deliberately NOT a str subclass: subclassing str makes ``type(x) is str``
    fail fast and never reach the isinstance() call, which is exactly why an
    earlier version of this test passed against the broken code.
    """

    @property
    def __class__(self):
        raise RuntimeError("hostile __class__")


class _HostileHash:
    def __hash__(self):
        raise RuntimeError("hostile hash")

    def __eq__(self, other):
        raise RuntimeError("hostile eq")


class _HostileBool:
    def __bool__(self):
        raise RuntimeError("hostile bool")


@pytest.mark.parametrize("make_status", [
    pytest.param(_HostileClass, id="hostile-class"),
    pytest.param(_HostileHash, id="hostile-hash"),
    pytest.param(lambda: _HostileBool(), id="hostile-bool"),
    pytest.param(lambda: None, id="none-status"),
], )
def test_no_hostile_child_status_can_destroy_a_sibling(
    monkeypatch, tmp_path, make_status
):
    """Close the CLASS, not one symptom at a time.

    Review escaped three successive guards: isinstance (via a __class__
    property), == (via __eq__), and str() (via a self-returning __str__). Only
    ``type(x) is str`` reads the real type slot and cannot be overridden.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(helpers.process_registry, "completion_queue", queue.Queue())
    try:
        hostile = {"task_index": 0, "status": make_status(), "summary": "A"}
        healthy = {"task_index": 1, "status": "completed",
                   "summary": "HEALTHY-SIBLING"}
        delegation_id, worker, _ = helpers.dispatch(
            monkeypatch, batch=True, result={"results": [hostile, healthy]})
        worker()  # must not raise

        event = helpers.process_registry.completion_queue.get_nowait()
        assert event["status"] == "completed"
        survivor = event["results"][1]
        assert survivor["task_index"] == 1
        assert survivor["summary"] == "HEALTHY-SIBLING"

        claim = ad.claim_event_delivery(event, "test-consumer")
        assert claim
        ad.complete_event_delivery(event, claim)
        assert ad.get_durable_delegation(delegation_id)["delivery_state"] == "delivered"
        assert store.enqueue_pending_outbox(current_boot_id="replay-probe") == []
        assert ad.restore_undelivered_completions(queue.Queue()) == 0
    finally:
        ad._reset_for_tests()


# --- Hostile-input contract for the terminal-write boundary -----------------
#
# A runner result is UNTRUSTED. Six review rounds each found one more way a
# user-defined dunder escaped a guard and destroyed a completed job or a
# healthy sibling's answer. These cases pin the whole class at once: for every
# shape below, finalization must not raise, the terminal record and outbox
# event must exist, and the healthy sibling's real answer must survive.


class _RaisingGet(dict):
    def get(self, key, default=None):
        raise RuntimeError("hostile get")


class _RaisingContains(dict):
    def __contains__(self, key):
        raise RuntimeError("hostile contains")


class _RaisingIter(dict):
    def __iter__(self):
        raise RuntimeError("hostile iter")


class _RaisingLen(list):
    def __len__(self):
        raise RuntimeError("hostile len")


class _LyingIndex(list):
    """Indexing is virtual: hands back a DIFFERENT object, fabricating answers."""

    def __getitem__(self, index):
        return {"task_index": 99, "status": "completed", "summary": "FAKE"}


class _LyingTuple(tuple):
    """len() is virtual on tuples too: hides every entry."""

    def __len__(self):
        return 0


class _RaisingBoolValue:
    def __bool__(self):
        raise RuntimeError("hostile bool")


class _HostileMeta(type):
    """Even ``type(x) == known`` is user-defined via the metaclass."""

    def __eq__(cls, other):
        raise RuntimeError("hostile metaclass eq")

    def __ne__(cls, other):
        raise RuntimeError("hostile metaclass ne")

    def __hash__(cls):
        return 0


class _HostileMetaValue(metaclass=_HostileMeta):
    pass


class _HostileKey:
    """A stored KEY whose __eq__ fires during an ordinary dict lookup."""

    def __hash__(self):
        return hash("model")

    def __eq__(self, other):
        raise RuntimeError("hostile extra-key equality")


class _LyingKey(str):
    """Impersonates a real field name to smuggle a value in."""

    def __hash__(self):
        return hash("summary")

    def __eq__(self, other):
        return True


class _RaisingClassObj:
    @property
    def __class__(self):
        raise RuntimeError("hostile class")


class _RaisingBoolObj:
    def __bool__(self):
        raise RuntimeError("hostile bool")


_KEEP = {"task_index": 1, "status": "completed", "summary": "KEEP-B"}


def _hostile_cases():
    return {
        "result-raising-get": _RaisingGet(results=[_KEEP], status="completed"),
        "result-raising-contains": _RaisingContains(
            results=[_KEEP], status="completed"),
        "result-raising-iter": _RaisingIter(results=[_KEEP], status="completed"),
        "results-raising-len": {"results": _RaisingLen([_KEEP])},
        "child-raising-class": {"results": [
            {"task_index": 0, "status": _RaisingClassObj()}, _KEEP]},
        "child-raising-bool": {"results": [
            {"task_index": 0, "status": _RaisingBoolObj()}, _KEEP]},
        "child-is-raising-get": {"results": [
            _RaisingGet(task_index=0, status="completed"), _KEEP]},
        "child-is-scalar": {"results": [42, _KEEP]},
        "child-is-none": {"results": [None, _KEEP]},
        "model-hostile": {"results": [_KEEP], "model": _RaisingClassObj()},
        "summary-hostile": {"results": [_KEEP], "summary": _RaisingClassObj()},
        "total-duration-hostile": {"results": [_KEEP],
                                   "total_duration_seconds": _RaisingClassObj()},
        "results-lying-index": {"results": _LyingIndex([_KEEP, _KEEP])},
        "results-lying-tuple": {"results": _LyingTuple((_KEEP,))},
        "model-raising-bool": {"results": [_KEEP],
                               "model": _RaisingBoolValue()},
        "model-hostile-metaclass": {"results": [_KEEP],
                                    "model": _HostileMetaValue()},
        "hostile-stored-key": {"results": [_KEEP], _HostileKey(): "x",
                               "status": "completed"},
        "lying-key-impersonation": {"results": [_KEEP],
                                    _LyingKey("zzz"): "IMPOSTOR",
                                    "status": "completed"},
        "summary-hostile-metaclass": {"results": [_KEEP],
                                      "summary": _HostileMetaValue()},
        "surrogate-everywhere": {"results": [_KEEP], "summary": "\ud800",
                                 "error": "\ud800", "model": "\ud800",
                                 "exit_reason": "\ud800"},
    }


@pytest.mark.parametrize("case", sorted(_hostile_cases()))
def test_hostile_runner_result_never_loses_work(monkeypatch, tmp_path, case):
    """No untrusted shape may strand a completion or destroy a sibling."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(helpers.process_registry, "completion_queue", queue.Queue())
    try:
        result = _hostile_cases()[case]
        delegation_id, worker, _ = helpers.dispatch(
            monkeypatch, batch=True, result=result)
        worker()  # must not raise

        with store.locked_registry() as registry:
            record = registry["records"][delegation_id]
        assert record["terminal"], f"{case}: no terminal record"
        assert len(record.get("outbox") or []) == 1, f"{case}: nothing queued"

        event = helpers.process_registry.completion_queue.get_nowait()
        survivors = [child for child in (event.get("results") or [])
                     if type(child) is dict and child.get("summary") == "KEEP-B"]
        assert survivors, f"{case}: healthy sibling destroyed"
        assert survivors[0]["task_index"] == 1

        claim = ad.claim_event_delivery(event, "test-consumer")
        assert claim
        ad.complete_event_delivery(event, claim)
        assert ad.get_durable_delegation(delegation_id)["delivery_state"] == "delivered"
        assert store.enqueue_pending_outbox(current_boot_id="replay-probe") == []
    finally:
        ad._reset_for_tests()


def test_unreadable_child_statuses_do_not_vote_as_failures(monkeypatch, tmp_path):
    """An unjudgeable status must not be counted as a failure.

    Children with a missing/non-string/hostile status yield None. Treating
    None as "not completed" reported a batch the runner said completed as an
    error -- a wrong aggregate answer handed to the parent.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(helpers.process_registry, "completion_queue", queue.Queue())
    try:
        result = {"results": [{"task_index": 0, "summary": "A"},
                              {"task_index": 1, "summary": "B"}]}
        delegation_id, worker, _ = helpers.dispatch(
            monkeypatch, batch=True, result=result)
        worker()
        event = helpers.process_registry.completion_queue.get_nowait()
        assert event["status"] == "completed"
        assert len(event["results"]) == 2
        assert delegation_id
    finally:
        ad._reset_for_tests()


def test_a_real_child_failure_is_still_reported(monkeypatch, tmp_path):
    """Negative control: readable failures must STILL classify as error."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(helpers.process_registry, "completion_queue", queue.Queue())
    try:
        result = {"results": [{"task_index": 0, "status": "error",
                               "summary": "boom"}]}
        delegation_id, worker, _ = helpers.dispatch(
            monkeypatch, batch=True, result=result)
        worker()
        event = helpers.process_registry.completion_queue.get_nowait()
        assert event["status"] == "error"
        assert delegation_id
    finally:
        ad._reset_for_tests()


def test_non_mapping_runner_result_is_not_reported_as_success(
    monkeypatch, tmp_path
):
    """A malformed runner return must not be delivered as a fabricated green.

    Regression: hardening the readers made a truthy non-dict return produce
    zero children, so the all-failed vote was False and the batch was
    delivered as COMPLETED. Base reported error; this restores that.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(helpers.process_registry, "completion_queue", queue.Queue())
    try:
        delegation_id, worker, _ = helpers.dispatch(
            monkeypatch, batch=True, result="a bare string, not a mapping")
        worker()  # must not raise
        event = helpers.process_registry.completion_queue.get_nowait()
        assert event["status"] == "error"
        assert delegation_id
    finally:
        ad._reset_for_tests()


def test_unhashable_status_cannot_abort_terminal_persistence(
    monkeypatch, tmp_path
):
    """terminal_state hashed the status for set membership before persisting."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(helpers.process_registry, "completion_queue", queue.Queue())

    class Unhashable(str):
        def __hash__(self):
            raise RuntimeError("hostile hash")

    try:
        result = {"status": Unhashable("completed"), "summary": "ok"}
        delegation_id, worker, _ = helpers.dispatch(monkeypatch, result=result)
        worker()  # must not raise
        with store.locked_registry() as registry:
            record = registry["records"][delegation_id]
        assert record["terminal"]
        assert len(record.get("outbox") or []) == 1
        assert helpers.process_registry.completion_queue.get_nowait()
    finally:
        ad._reset_for_tests()


def test_non_dict_child_stays_deliverable_through_the_formatter(
    monkeypatch, tmp_path
):
    """A JSON-safe SCALAR child is representable but not FORMATTABLE.

    Every consumer treats children as mappings
    (process_registry._format_async_delegation, tui_gateway) and calls .get()
    on each one, so emitting a bare scalar raised AttributeError in the
    formatter and the completion never reached the user. Children must always
    be dicts, and the healthy sibling must still arrive.
    """
    from tools import process_registry as real_registry

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(helpers.process_registry, "completion_queue", queue.Queue())
    try:
        result = {"results": [42, {"task_index": 1, "status": "completed",
                                   "summary": "KEEP-B"}]}
        delegation_id, worker, _ = helpers.dispatch(
            monkeypatch, batch=True, result=result)
        worker()
        event = helpers.process_registry.completion_queue.get_nowait()

        assert all(type(child) is dict for child in event["results"])
        assert any(child.get("summary") == "KEEP-B" for child in event["results"])

        # The real consumer must render it without raising.
        rendered = real_registry._format_async_delegation(event)
        assert "KEEP-B" in rendered
        assert delegation_id
    finally:
        ad._reset_for_tests()


@pytest.mark.parametrize("shape", ["raising", "lying", "hostile-metaclass"])
def test_unrepresentable_child_is_described_without_running_its_code(
    monkeypatch, tmp_path, shape
):
    """Describing a degraded child must not invoke runner-controlled code.

    ``repr(entry)`` is user-defined: a child whose ``__repr__`` raises aborted
    ``_terminal_payload`` inside ``append_terminal`` before the terminal record
    or the outbox existed -- both stores left ``running``, nothing queued, and
    the healthy sibling's completed answer destroyed. A ``__repr__`` that
    LIES (returns a hostile str subclass) smuggled a raising ``__eq__`` into
    the envelope instead. The description now reads the type name through the
    C-level slot, which neither the instance nor a hostile metaclass can
    intercept.
    """
    from tools import process_registry as real_registry

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ad._reset_for_tests()
    monkeypatch.setattr(helpers.process_registry, "completion_queue", queue.Queue())

    class RaisingEq(str):
        def __eq__(self, other):
            raise RuntimeError("hostile eq")

        def __ne__(self, other):
            raise RuntimeError("hostile ne")

        def __hash__(self):
            raise RuntimeError("hostile hash")

    class RaisingRepr:
        def __repr__(self):
            raise RuntimeError("hostile child repr")

    class LyingRepr:
        def __repr__(self):
            return RaisingEq("completed")

    class HostileMeta(type):
        @property
        def __name__(cls):
            raise RuntimeError("hostile type name")

        def __getattribute__(cls, name):
            raise RuntimeError("hostile type getattr")

    class HostileMetaChild(metaclass=HostileMeta):
        def __repr__(self):
            raise RuntimeError("hostile child repr")

    hostile = {"raising": RaisingRepr, "lying": LyingRepr,
               "hostile-metaclass": HostileMetaChild}[shape]()
    try:
        healthy = {"task_index": 1, "status": "completed",
                   "summary": "HEALTHY-SIBLING"}
        delegation_id, worker, _ = helpers.dispatch(
            monkeypatch, batch=True, result={"results": [hostile, healthy]})
        # Catch here rather than letting it propagate: pytest's own traceback
        # reporter calls repr()/type(obj).__name__ on the arguments of every
        # frame it renders, so an escaping failure would crash the REPORTER on
        # these objects (INTERNALERROR) instead of showing the real defect.
        aborted = None
        try:
            worker()
        except BaseException as exc:  # noqa: BLE001 - reported, not raised
            aborted = type(exc).__name__
        assert aborted is None, f"finalization aborted with {aborted}"

        with store.locked_registry() as registry:
            record = registry["records"][delegation_id]
        assert record["state"] == "done"
        assert record.get("terminal")
        assert len(record.get("outbox") or []) == 1

        event = helpers.process_registry.completion_queue.get_nowait()
        assert event["status"] == "completed"
        assert len(event["results"]) == 2
        assert all(type(child) is dict for child in event["results"])
        survivor = event["results"][1]
        assert survivor["task_index"] == 1
        assert survivor["summary"] == "HEALTHY-SIBLING"
        # The degraded child carries an exact-str description, never the
        # runner's own object.
        degraded = event["results"][0]
        assert degraded["status"] == "error"
        assert type(degraded["raw"]) is str

        # The real consumer renders it, and it accepts with no replay.
        assert "HEALTHY-SIBLING" in real_registry._format_async_delegation(event)
        claim = ad.claim_event_delivery(event, "test-consumer")
        assert claim
        ad.complete_event_delivery(event, claim)
        assert ad.get_durable_delegation(delegation_id)["delivery_state"] == "delivered"
        assert store.enqueue_pending_outbox(current_boot_id="replay-probe") == []
        assert ad.restore_undelivered_completions(queue.Queue()) == 0
    finally:
        ad._reset_for_tests()
