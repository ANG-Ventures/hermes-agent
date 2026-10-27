"""Profile-scoped durable state for gateway background delegations.

The JSON registry is an intent/outbox store, not a second delivery consumer.
Live events continue to use ``process_registry.completion_queue``.
"""

from __future__ import annotations

import copy
import errno
import hashlib
import json
import math
import logging
import marshal
import os
import sys
import threading
import time
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from hermes_constants import get_hermes_home
from utils import atomic_json_write

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
MAX_REDISPATCH_ATTEMPTS = 2
MAX_COMPLETION_REPLAY_AGE_SECONDS = 48 * 3600.0
MAX_REGISTRY_RECORDS = 256
TERMINAL_RETENTION_SECONDS = 7 * 24 * 60 * 60
ABSOLUTE_RETENTION_SECONDS = 30 * 24 * 60 * 60
# An admitted wake unconfirmed this long is lost, not pending: same horizon as completion replay.
ADMITTED_MAX_AGE_SECONDS = MAX_COMPLETION_REPLAY_AGE_SECONDS
ACTIVE_STALE_SECONDS = 30 * 24 * 60 * 60
# Lifecycle breadcrumb trail cap per record (oldest entries dropped first).
MAX_LIFECYCLE_EVENTS = 50
# Caller-stack frames captured for cancel attribution (innermost frames).
MAX_ATTRIBUTION_FRAMES = 12
# sqlite3 binds Python ints as signed 64-bit INTEGER and raises OverflowError
# outside this range. Persisted timestamps are bound as parameters by the
# recovery/replay mirrors, so a wider int is not a usable number here.
_SQLITE_INT_MIN = -(2 ** 63)
_SQLITE_INT_MAX = 2 ** 63 - 1

class RegistryError(RuntimeError):
    """Registry cannot be safely read or mutated."""


def _capture_caller_stack(*, skip_frames: int = 3) -> list[str]:
    """Innermost caller frames as compact ``file:line:func`` strings.

    ``skip_frames`` drops this helper, ``_stamp_cancel_attribution``, and
    the store-internal cancel writer (``cancel_matching`` /
    ``transition_owned``) so the trail starts at the runtime code that
    requested the cancellation — the WHO for Phase-0 cancel forensics.
    """
    try:
        frames = traceback.extract_stack()
        if skip_frames:
            frames = frames[:-skip_frames]
        return [
            f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
            for frame in frames[-MAX_ATTRIBUTION_FRAMES:]
        ]
    except Exception:  # pragma: no cover - forensics must never break a cancel
        return []


def append_lifecycle_event(
    record: dict[str, Any],
    event: str,
    detail: str = "",
    *,
    now: float | None = None,
) -> None:
    """Append a breadcrumb to the record's additive ``lifecycle`` trail.

    Purely observational (Phase-0 cancel forensics): older readers ignore the
    key, and records written before the upgrade simply have no trail.
    """
    try:
        trail = record.setdefault("lifecycle", [])
        if not isinstance(trail, list):  # defensive against hand-edited state
            return
        entry: dict[str, Any] = {"event": str(event), "ts": now if now is not None else time.time()}
        if detail:
            entry["detail"] = str(detail)[:240]
        trail.append(entry)
        if len(trail) > MAX_LIFECYCLE_EVENTS:
            del trail[: len(trail) - MAX_LIFECYCLE_EVENTS]
    except Exception:  # pragma: no cover - forensics must never break a write
        pass


def _stamp_cancel_attribution(
    record: dict[str, Any],
    *,
    now: float,
    reason: str,
    caller: str,
    via: str,
    selector: dict[str, Any] | None = None,
) -> None:
    """Stamp WHO/WHY/WHEN onto a record entering ``state=cancelled``.

    Additive schema (Phase-0 of the parent-interrupt spec): every writer of
    ``state=cancelled`` records which terminal path asked for the cancel, the
    caller stack that reached it, and the wall-clock time — so the next
    "silent death" in ``async-delegations.json`` is attributable instead of
    mysterious. Never overwrites an earlier stamp (first cancel wins).
    """
    try:
        if isinstance(record.get("cancel_attribution"), dict):
            return
        attribution: dict[str, Any] = {
            "reason": str(reason or "unspecified"),
            "caller": str(caller or "unknown"),
            "via": str(via),
            "cancelled_at": now,
            "stack": _capture_caller_stack(skip_frames=3),
        }
        if selector:
            attribution["selector"] = {
                k: v for k, v in selector.items() if v not in ("", None, False)
            }
        record["cancel_attribution"] = attribution
        append_lifecycle_event(
            record,
            "cancelled",
            f"by={attribution['caller']} reason={attribution['reason']} via={via}",
            now=now,
        )
        logger.info(
            "async_delegation_cancelled delegation_id=%s reason=%s caller=%s via=%s",
            record.get("delegation_id"),
            attribution["reason"],
            attribution["caller"],
            via,
        )
    except Exception:  # pragma: no cover - forensics must never break a cancel
        pass


def registry_path(profile_home: Path | None = None) -> Path:
    home = Path(profile_home) if profile_home is not None else get_hermes_home()
    return home.resolve() / "state" / "async-delegations.json"


def lock_path(profile_home: Path | None = None) -> Path:
    return registry_path(profile_home).with_suffix(".lock")


def empty_registry() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "updated_at": 0.0, "records": {}}


def _canonical_record_bytes(record: dict[str, Any]) -> bytes:
    canonical = dict(record)
    canonical.pop("integrity", None)
    return json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _record_checksum(record: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_record_bytes(record)).hexdigest()


# Process-wide parse cache: {path: ((st_mtime_ns, st_size, st_ino, st_ctime_ns), content_sha256,
# cached_at, registry, strict_error)}. st_ctime_ns is kernel-owned: any write, or an os.utime that
# restores an old mtime, moves it, so a same-size in-place rewrite never keeps the stat key. The registry is the lenient (quarantining) parse; ``strict_error`` is
# the RegistryError a strict load of the same bytes raises (None when every record is valid).
# The cached object is NEVER handed out: every load returns a private marshal copy of it (~5 ms on
# the 2.8 MB compacted live registry vs ~90 ms parse + checksums), so a caller that mutates what
# it read cannot corrupt later reads (Prism 81679117ed4f). Integrity checksums are verified on every
# miss (the first load after any change), so a corrupted file is still caught once it changes.
_REGISTRY_CACHE: dict[str, tuple[tuple[int, int, int, int], str, float, dict[str, Any], str | None]] = {}
_REGISTRY_CACHE_LOCK = threading.Lock()
# A same-size rewrite inside one mtime tick keeps (mtime_ns, size, ino) equal on coarse
# filesystems; inside this window a hit is confirmed against the content hash.
_CACHE_HASH_WINDOW_SECONDS = 2.0


def _ctime_is_creation_time() -> bool:
    """On Windows ``st_ctime_ns`` is creation time, not the last metadata change: a same-size
    in-place rewrite that restores the mtime keeps the whole stamp, so every hit is content-verified."""
    return os.name == "nt" or sys.platform.startswith(("win", "cygwin"))


def _invalidate_cache(path: Path) -> None:
    with _REGISTRY_CACHE_LOCK:
        _REGISTRY_CACHE.pop(str(path), None)


def _copy_registry(registry: dict[str, Any]) -> dict[str, Any]:
    """Private copy of a JSON-native registry (marshal round trip is ~3x faster than deepcopy)."""
    try:
        return marshal.loads(marshal.dumps(registry))
    except (ValueError, TypeError):  # pragma: no cover - non-JSON-native value
        return copy.deepcopy(registry)


def _load(
    path: Path,
    *,
    allow_invalid_records: bool = False,
    private: bool = False,
    trusted_records: dict[str, tuple[str, bytes]] | None = None,
) -> dict[str, Any]:
    """Load a registry; strict mode raises on the first invalid record, lenient quarantines.

    Served from ``_REGISTRY_CACHE`` while the file's (mtime_ns, size, inode, ctime_ns) is unchanged.
    Every result is a private copy nobody else holds; ``private`` is kept for call-site clarity.
    ``trusted_records`` (the records this process just wrote and hashed) forces a re-parse and
    skips re-hashing a record only when the parsed record is EQUAL to the one written.
    """
    key = str(path)
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        _invalidate_cache(path)
        return empty_registry()
    with handle:
        st = os.fstat(handle.fileno())  # the inode we read, even if the path is replaced meanwhile
        stamp = (st.st_mtime_ns, st.st_size, st.st_ino, st.st_ctime_ns)
        now = time.time()
        with _REGISTRY_CACHE_LOCK:
            cached = _REGISTRY_CACHE.get(key)
        raw: bytes | None = None
        if (trusted_records is None and cached is not None
                and cached[0] == stamp):
            # The entry was taken within one coarse mtime tick of the file's mtime: a same-size
            # rewrite in that tick would leave the stat key unchanged, so confirm by content.
            # Once a confirmed hit lands past the window, any later rewrite moves the ctime.
            changed = max(st.st_mtime_ns, st.st_ctime_ns) / 1e9
            if _ctime_is_creation_time() or cached[2] - changed < _CACHE_HASH_WINDOW_SECONDS:
                raw = handle.read()
            if raw is None or hashlib.sha256(raw).hexdigest() == cached[1]:
                if raw is not None and now - changed >= _CACHE_HASH_WINDOW_SECONDS:
                    with _REGISTRY_CACHE_LOCK:
                        if _REGISTRY_CACHE.get(key) is cached:
                            _REGISTRY_CACHE[key] = cached[:2] + (now,) + cached[3:]
                registry, strict_error = cached[3], cached[4]
                if strict_error is not None and not allow_invalid_records:
                    raise RegistryError(strict_error)
                return _copy_registry(registry)
        if raw is None:
            raw = handle.read()
    data, strict_error = _parse_registry(raw, path, trusted_records=trusted_records)
    with _REGISTRY_CACHE_LOCK:
        _REGISTRY_CACHE[key] = (stamp, hashlib.sha256(raw).hexdigest(), now, data, strict_error)
    if strict_error is not None and not allow_invalid_records:
        raise RegistryError(strict_error)
    return _copy_registry(data)


def _parse_registry(
    raw: bytes, path: Path, *, trusted_records: dict[str, tuple[str, bytes]] | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Lenient parse + per-record validation; also the strict error a strict load would raise."""
    strict_error: str | None = None
    trusted_records = trusted_records or {}
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except Exception as exc:
        logger.error("async_delegation_registry_invalid path=%s reason=%s", path, exc)
        raise RegistryError(f"invalid async-delegation registry: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        reason = f"unsupported schema_version={data.get('schema_version') if isinstance(data, dict) else None}"
        logger.error("async_delegation_registry_invalid path=%s reason=%s", path, reason)
        raise RegistryError(reason)
    if not isinstance(data.get("records"), dict):
        raise RegistryError("registry records must be an object")
    invalid_record_ids: list[str] = []
    for delegation_id, record in data["records"].items():
        if not isinstance(record, dict):
            strict_error = strict_error or f"record {delegation_id} must be an object"
            invalid_record_ids.append(str(delegation_id))
            logger.error(
                "async_delegation_registry_invalid delegation_id=%s reason=record_not_object",
                delegation_id,
            )
            continue
        # Reuse the existing per-record quarantine for malformed structures.
        # Sparse historical terminal outboxes may lack an embedded self ID;
        # active records and any explicitly supplied self ID must match the key.
        outbox = record.get("outbox", [])
        state = record.get("state", "")
        invalid_state = not isinstance(state, str)
        invalid_identity = (
            ((not invalid_state and state in {"running", "recoverable"})
             or "delegation_id" in record)
            and record.get("delegation_id") != delegation_id
        )
        invalid_outbox = (
            not isinstance(outbox, list)
            or any(
                not isinstance(event, dict)
                or (event.get("payload") is not None
                    and not isinstance(event.get("payload"), dict))
                # The deliverable path binds the ENVELOPE's own completed_at:
                # _sync_completion writes payload["completed_at"] straight into
                # the mirror's REAL column. Validating only the record-level
                # terminal timestamp below let the same unbindable int reach
                # SQLite through this sibling field and abort recovery for every
                # healthy delegation in the profile.
                or (isinstance(event, dict)
                    and isinstance(event.get("payload"), dict)
                    and not _is_optional_number(event["payload"].get("completed_at")))
                # enqueue_pending_outbox ages the event with `now - created_at`
                # and supersedes restart notices with
                # `int(payload["attempt_generation"] or -1)`. Both conversions
                # are unguarded, sit in the same per-record replay loop as the
                # timestamps above, and raise on a value that merely LOOKS
                # numeric: `-(10 ** 400)` overflows float(), and inf/nan cannot
                # convert to int. One malformed sibling field would abort replay
                # for every healthy delegation in the profile.
                or (isinstance(event, dict)
                    and not _is_optional_number(event.get("created_at")))
                # The admitted-age ceiling computes `now - admitted_at` across the whole
                # registry; an unconvertible value would abort it for every sibling (7aaf4b17dca7).
                or (isinstance(event, dict)
                    and not _is_optional_number(event.get("admitted_at")))
                or (isinstance(event, dict)
                    and isinstance(event.get("payload"), dict)
                    and not _is_optional_number(event["payload"].get("attempt_generation")))
                for event in outbox
            )
        )
        # The recovery loops convert these without guards (float(created_at),
        # int(generation/redispatch_count)) and .get() into attempt. A
        # valid-checksum record carrying a bad scalar would raise out of the
        # per-record loop and abort the WHOLE profile's recovery/replay, so it
        # is quarantined here like any other malformed structure. bool is
        # rejected explicitly: it is an int subclass, so `completed_at: true`
        # would otherwise persist as timestamp 1.
        attempt_field = record.get("attempt")
        invalid_numeric = (
            (attempt_field is not None and not isinstance(attempt_field, dict))
            or not _is_optional_number(record.get("created_at"))
            or not _is_optional_number(record.get("updated_at"))
        )
        if isinstance(attempt_field, dict) and not invalid_numeric:
            invalid_numeric = (
                not _is_optional_number(attempt_field.get("generation"))
                or not _is_optional_number(attempt_field.get("redispatch_count"))
                or not _is_optional_number(attempt_field.get("started_at"))
                or not _is_optional_number(attempt_field.get("submitted_at"))
            )
        terminal_field = record.get("terminal")
        if not invalid_numeric and terminal_field is not None:
            invalid_numeric = (
                not isinstance(terminal_field, dict)
                or not _is_optional_number(terminal_field.get("completed_at"))
            )
        if invalid_state or invalid_identity or invalid_outbox or invalid_numeric:
            strict_error = strict_error or f"record {delegation_id} has invalid state/identity/outbox structure"
            invalid_record_ids.append(str(delegation_id))
            logger.error("async_delegation_registry_invalid delegation_id=%s reason=structure",
                         delegation_id)
            continue
        expected = record.get("integrity")
        trusted = trusted_records.get(delegation_id)
        # Compare canonical bytes, never Python equality: True == 1 == 1.0 but they checksum
        # differently, so a value-equal rewrite on disk must still fail integrity (Prism c74982e2a8ca).
        if expected and trusted is not None and trusted == (expected, _canonical_record_bytes(record)):
            continue  # exactly the bytes this process hashed while writing it
        if not expected or expected != _record_checksum(record):
            strict_error = strict_error or f"record {delegation_id} failed integrity validation"
            invalid_record_ids.append(str(delegation_id))
            logger.error(
                "async_delegation_registry_invalid delegation_id=%s reason=integrity",
                delegation_id,
            )
    if invalid_record_ids:
        data["_invalid_record_ids"] = invalid_record_ids
    return data, strict_error


def read_registry(profile_home: Path | None = None) -> dict[str, Any]:
    """The registry, as a private copy the caller may mutate freely."""
    return _load(registry_path(profile_home))


def _prepare_directory(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass


def _write(path: Path, registry: dict[str, Any]) -> None:
    _prepare_directory(path)
    registry["schema_version"] = SCHEMA_VERSION
    registry["updated_at"] = time.time()
    invalid_record_ids = set(registry.pop("_invalid_record_ids", []))
    written: dict[str, tuple[str, bytes]] = {}
    for delegation_id, record in registry.get("records", {}).items():
        if str(delegation_id) in invalid_record_ids:
            continue
        if isinstance(record, dict):
            canonical = _canonical_record_bytes(record)
            record["integrity"] = hashlib.sha256(canonical).hexdigest()
            written[delegation_id] = (record["integrity"], canonical)
    try:
        atomic_json_write(path, registry, mode=0o600)
    except BaseException:
        _invalidate_cache(path)
        raise
    # Refresh the read cache from the bytes actually on disk (never this caller's object, which it
    # may keep mutating). Records equal to what we just hashed are not re-hashed; anything else
    # (quarantined records, a value that did not round-trip) is fully re-validated.
    try:
        _load(path, allow_invalid_records=True, trusted_records=written)
    except Exception:
        _invalidate_cache(path)


@contextmanager
def locked_registry(
    profile_home: Path | None = None,
    *,
    timeout: float = 5.0,
    write: bool = True,
    path: Path | None = None,
) -> Iterator[dict[str, Any]]:
    """Lock, load, and optionally atomically persist one profile registry.

    ``write=False`` yields the process-wide cached registry: it is READ-ONLY (every in-tree
    write=False caller only reads, or copies before handing data out). ``write=True`` yields a
    private copy, so a writer can never mutate the cache in place. ``path`` (an already-resolved
    registry path) lets a caller that needs the path again (backup) use the very file locked here.
    """
    if path is None:
        path = registry_path(profile_home)
    elif profile_home is not None:
        raise ValueError("pass profile_home or path, not both")
    lpath = path.with_suffix(".lock")  # Same captured home, even if cwd/symlink changes.
    _prepare_directory(path)
    deadline = time.monotonic() + max(0.0, timeout)
    handle = lpath.open("a+b")
    acquired = False
    try:
        try:
            import fcntl  # type: ignore
        except ImportError:  # pragma: no cover - Windows
            import msvcrt  # type: ignore

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            while not acquired:
                try:
                    handle.seek(0)
                    getattr(msvcrt, "locking")(
                        handle.fileno(), getattr(msvcrt, "LK_NBLCK"), 1
                    )
                    acquired = True
                    break
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out acquiring {lpath}")
                time.sleep(0.01)
        else:
            while not acquired:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"timed out acquiring {lpath}")
                    time.sleep(0.01)
        registry = _load(path, allow_invalid_records=True, private=write)
        yield registry
        if write:
            _write(path, registry)
    finally:
        if acquired:
            try:
                import fcntl  # type: ignore
            except ImportError:  # pragma: no cover - Windows
                import msvcrt  # type: ignore

                try:
                    handle.seek(0)
                    getattr(msvcrt, "locking")(
                        handle.fileno(), getattr(msvcrt, "LK_UNLCK"), 1
                    )
                except OSError:
                    pass
            else:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
        handle.close()


def write_for_tests(registry: dict[str, Any], profile_home: Path | None = None) -> None:
    with locked_registry(profile_home) as current:
        current.clear()
        current.update(copy.deepcopy(registry))


def _terminal_pending(record: dict[str, Any]) -> bool:
    return any(
        event.get("state") in {"pending", "admitted"}
        for event in record.get("outbox", [])
        if isinstance(event, dict)
    )


def admitted_past_ceiling(event: dict[str, Any], now: float) -> bool:
    """An ``admitted`` row older than the replay horizon will never be confirmed or re-injected:
    its profile is no longer served or its transcript store is gone (t_506536db)."""
    admitted_at = event.get("admitted_at")
    return (event.get("state") == "admitted" and isinstance(admitted_at, (int, float))
            and now - admitted_at > ADMITTED_MAX_AGE_SECONDS)


def sweep_marker_path(profile_home: Path | None = None) -> Path:
    """Touched by the admitted-completion sweeper for every home it serves (and by the gateway's boot
    replay, before its first sweep). Its mtime is how the store tells a served home from an unserved one."""
    return registry_path(profile_home).with_suffix(".sweep")


def note_sweeper_serves(profile_home: Path | None = None) -> None:
    path = sweep_marker_path(profile_home)
    try:
        _prepare_directory(path)
        path.touch()
        stamp = time.time()  # the store's clock, not the filesystem's: expiry compares against it
        os.utime(path, (stamp, stamp))
    except OSError:
        logger.debug("could not touch the admitted-sweep marker %s", path, exc_info=True)


def _sweeper_serves(path: Path, now: float) -> bool:
    try:
        return now - path.with_suffix(".sweep").stat().st_mtime <= ADMITTED_MAX_AGE_SECONDS
    except OSError:
        return False


def _expire_admitted_unserved(registry: dict[str, Any], path: Path, now: float) -> None:
    """Housekeeping for a home NO sweeper serves: an ``admitted`` row past the ceiling is dropped as
    ``unverified``, not lost, since nothing checked its transcript. A served home is left to the sweep,
    which confirms against the transcript first and only then expires (Prism 2fcb0b3d9b47)."""
    if _sweeper_serves(path, now):
        return
    invalid_record_ids = set(registry.get("_invalid_record_ids", []))
    for delegation_id, record in registry["records"].items():
        if str(delegation_id) in invalid_record_ids or not isinstance(record, dict):
            continue
        for event in record.get("outbox", []):
            if isinstance(event, dict) and admitted_past_ceiling(event, now):
                event.update(state="dropped", drop_reason="admitted_age_ceiling_unverified", delivered_at=now)
                logger.warning(
                    "async_delegation_wake_unverified event_id=%s session_key=%s age_hours=%.1f: admitted, "
                    "no sweeper serves this profile; expired unverified at the age ceiling", event.get("event_id"),
                    event.get("admitted_session_key") or "-", (now - event["admitted_at"]) / 3600.0,
                )


_TERMINAL_STATES = frozenset({"done", "failed", "cancelled"})
# A settled terminal record's archived result keeps everything but the per-child tool traces
# once it is this old (D5): nothing reads tool_trace back out of the registry or its mirror.
COMPACT_TERMINAL_AFTER_SECONDS = 24 * 60 * 60
# Body fields of a DELIVERED outbox payload that no reader consults after delivery. Every
# identity/receipt field (event_id, type, delegation_id, attempt_id, attempt_generation, profile,
# status, completed_at, routing ids) stays: canonical_terminal, _same_completion_event and the
# SQLite mirror resync (_sync_completion) read those on every boot and every claim.
_DELIVERED_PAYLOAD_BODY_FIELDS = ("results", "summary", "goals", "goal", "context", "toolsets")
_TERMINAL_RESULT_CHILD_BODY_FIELDS = ("tool_trace",)


def _canonical_sha256(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()


def is_compacted_payload(payload: Any) -> bool:
    """The envelope's body was dropped at delivery (D5); only identity + ``payload_sha256`` remain."""
    return isinstance(payload, dict) and isinstance(payload.get("payload_sha256"), str)


def _compact_delivered_event(record: dict[str, Any], event: Any, *, dry_run: bool = False) -> bool:
    """Drop the body of one DELIVERED event of a TERMINAL record, stamping ``payload_sha256``
    (the canonical hash of the full envelope) into the payload so a consumer that needed the body
    fails loudly rather than silently. Never touches a pending/admitted/dropped event or a
    non-terminal record (I5)."""
    if (record.get("state") not in _TERMINAL_STATES or not isinstance(event, dict)
            or event.get("state") != "delivered"):
        return False
    payload = event.get("payload")
    if (not isinstance(payload, dict) or is_compacted_payload(payload)
            or not any(k in payload for k in _DELIVERED_PAYLOAD_BODY_FIELDS)):
        return False
    try:
        digest = _canonical_sha256(payload)
    except (TypeError, ValueError):
        return False
    if dry_run:
        return True
    for key in _DELIVERED_PAYLOAD_BODY_FIELDS:
        payload.pop(key, None)
    payload["payload_sha256"] = digest
    return True


def _compact_terminal_result(record: dict[str, Any], now: float, *, dry_run: bool = False) -> bool:
    """Strip per-child tool traces from a settled terminal record older than 24 h.

    The result text (``summary``), status, error and task identity stay: the SQLite mirror is
    resynced from ``terminal.result`` and ``failed_delegations_for_session`` /
    ``get_durable_delegation`` read those back."""
    if record.get("state") not in _TERMINAL_STATES or _terminal_pending(record):
        return False
    updated = record.get("updated_at") or record.get("created_at") or 0.0
    if not isinstance(updated, (int, float)) or now - updated < COMPACT_TERMINAL_AFTER_SECONDS:
        return False
    terminal = record.get("terminal")
    result = terminal.get("result") if isinstance(terminal, dict) else None
    children = result.get("results") if isinstance(result, dict) else None
    if not isinstance(children, list):
        return False
    changed = False
    for child in children:
        if isinstance(child, dict):
            for key in _TERMINAL_RESULT_CHILD_BODY_FIELDS:
                if key in child:
                    if dry_run:
                        return True
                    child.pop(key)
                    changed = True
    return changed


# errnos meaning "this platform/filesystem cannot fsync a directory", not an I/O failure.
_DIR_FSYNC_UNSUPPORTED = frozenset(
    e for e in (getattr(errno, n, None) for n in ("EINVAL", "ENOTSUP", "EOPNOTSUPP", "EBADF"))
    if e is not None)


def _fsync_directory(directory: Path) -> bool:
    """fsync ``directory`` so a rename inside it is durable. True when synced or when the platform
    cannot fsync a directory at all (Windows, EINVAL/ENOTSUP); False on any real error (EIO...)."""
    if os.name == "nt":
        return True
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError as exc:
        if exc.errno in _DIR_FSYNC_UNSUPPORTED:
            return True
        logger.warning("could not open %s to fsync it", directory, exc_info=True)
        return False
    try:
        os.fsync(dir_fd)
    except OSError as exc:
        if exc.errno in _DIR_FSYNC_UNSUPPORTED:
            return True
        logger.warning("directory fsync failed for %s", directory, exc_info=True)
        return False
    finally:
        os.close(dir_fd)
    return True


def _backup_before_first_compaction(path: Path) -> bool:
    """One-time ``<registry>.pre-compact-<ts>`` copy (0600) of the file as it was on disk.

    True only when a completed backup exists.
    The copy is written and fsynced under a hidden temp name and only then renamed to its
    ``pre-compact-*`` name, so a failed attempt (ENOSPC, EIO) never publishes a partial file that a
    later run would mistake for the backup; the temp file is removed on failure. Callers compact
    nothing unless this returns True."""
    tmp: Path | None = None
    try:
        if any(path.parent.glob(f"{path.name}.pre-compact-*")):
            return True
        if not path.exists():
            return False
        backup = path.with_name(f"{path.name}.pre-compact-{int(time.time())}")
        tmp = path.with_name(f".{path.name}.pre-compact-tmp-{os.getpid()}-{threading.get_ident()}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(path.read_bytes())
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, backup)
        tmp = None
        if not _fsync_directory(path.parent):
            # The rename is not durable: unpublish it so no later run trusts a backup that may
            # vanish on crash, and compact nothing (Prism 101f4851d89a).
            try:
                backup.unlink()
            except OSError:
                pass
            return False
        logger.info("async_delegation_registry_precompact_backup path=%s", backup)
        return True
    except OSError:
        logger.warning("could not write the pre-compaction registry backup for %s; not compacting",
                       path, exc_info=True)
        return False
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass


def _compact(registry: dict[str, Any], now: float, path: Path) -> int:
    """D5 compaction of every valid record; returns how many records changed.

    Nothing is compacted until a complete pre-compaction backup exists on disk."""
    invalid_record_ids = set(registry.get("_invalid_record_ids", []))
    candidates: list[dict[str, Any]] = []
    for delegation_id, record in registry["records"].items():
        if str(delegation_id) in invalid_record_ids or not isinstance(record, dict):
            continue
        outbox = record.get("outbox")
        if ((isinstance(outbox, list)
                and any(_compact_delivered_event(record, event, dry_run=True) for event in outbox))
                or _compact_terminal_result(record, now, dry_run=True)):
            candidates.append(record)
    if not candidates or not _backup_before_first_compaction(path):
        return 0
    changed = 0
    for record in candidates:
        outbox = record.get("outbox")
        hit = False
        if isinstance(outbox, list):
            hit = any([_compact_delivered_event(record, event) for event in outbox])
        if _compact_terminal_result(record, now):
            hit = True
        changed += hit
    return changed


def _prune(registry: dict[str, Any], now: float, path: Path) -> None:
    _expire_admitted_unserved(registry, path, now)
    _compact(registry, now, path)
    records = registry["records"]
    invalid_record_ids = set(registry.get("_invalid_record_ids", []))
    removable: list[tuple[float, str]] = []
    for delegation_id, record in records.items():
        if str(delegation_id) in invalid_record_ids:
            continue
        if not isinstance(record, dict):
            continue
        state = record.get("state")
        if state not in {"done", "failed", "cancelled"}:
            continue
        updated = float(record.get("updated_at") or record.get("created_at") or 0.0)
        age = now - updated
        if (not _terminal_pending(record) and age >= TERMINAL_RETENTION_SECONDS) or age >= ABSOLUTE_RETENTION_SECONDS:
            removable.append((updated, delegation_id))
    for _, delegation_id in sorted(removable):
        records.pop(delegation_id, None)
    if len(records) <= MAX_REGISTRY_RECORDS:
        return
    eligible = sorted(
        (float(r.get("updated_at") or 0.0), rid)
        for rid, r in records.items()
        if str(rid) not in invalid_record_ids
        and isinstance(r, dict)
        and r.get("state") in {"done", "failed", "cancelled"}
        and not _terminal_pending(r)
    )
    for _, delegation_id in eligible:
        if len(records) <= MAX_REGISTRY_RECORDS:
            break
        records.pop(delegation_id, None)


def _attempt_id(delegation_id: str, generation: int) -> str:
    import uuid

    return f"{delegation_id}:g{generation}:{uuid.uuid4().hex[:8]}"


_SECRET_FIELD_PARTS = (
    "api_key",
    "apikey",
    "password",
    "secret",
    "authorization",
    "access_token",
    "refresh_token",
)


def _assert_nonsecret_settings(durable_spec: dict[str, Any]) -> None:
    """Fail closed if execution metadata contains credential-shaped fields."""
    execution = durable_spec.get("execution") or {}

    def _walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = str(key).lower().replace("-", "_")
                if any(part in normalized for part in _SECRET_FIELD_PARTS):
                    raise RegistryError(
                        f"secret-shaped field cannot be persisted: {path}.{key}"
                    )
                _walk(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                _walk(child, f"{path}[{index}]")

    _walk(execution, "execution")
    args = [str(item).lower() for item in execution.get("acp_args") or []]
    if any(
        item.startswith(("--api-key", "--token", "--password"))
        or "authorization=" in item
        for item in args
    ):
        raise RegistryError("credential-shaped ACP argument cannot be persisted")
    if url_has_credentials(execution.get("base_url")):
        raise RegistryError("credential-bearing base_url cannot be persisted")
    # The kanban runtime's route copies the parent's own URL (parent_base_url, and explicit_base_url when
    # no delegation endpoint is set): the same value guard applies (Prism c631db36b404).
    route = execution.get("route") or {}
    if isinstance(route, dict):
        for field in ("parent_base_url", "explicit_base_url"):
            if url_has_credentials(route.get(field)):
                raise RegistryError(f"credential-bearing route.{field} cannot be persisted")


def _sensitive_query_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(part in normalized for part in _SECRET_FIELD_PARTS) or normalized in {"key", "token"}


def url_has_credentials(url: Any) -> bool:
    """True when a URL carries userinfo (``user:pass@``) or a credential-shaped query parameter."""
    text = str(url or "")
    if "://" not in text:
        return False
    from urllib.parse import parse_qsl, urlsplit

    try:
        parsed = urlsplit(text)
        userinfo = bool(parsed.username or parsed.password)
    except ValueError:
        return True  # unparseable: fail closed
    return userinfo or any(_sensitive_query_key(k) for k, _ in parse_qsl(parsed.query, keep_blank_values=True))


def redact_url(url: Any) -> str:
    """The URL with userinfo dropped and credential-shaped query values masked, safe for errors/logs."""
    text = str(url or "")
    if "://" not in text:
        return text
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    try:
        parsed = urlsplit(text)
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parsed.port:
            host = f"{host}:{parsed.port}"
    except ValueError:
        return "(unparseable url)"
    query = urlencode([(k, "***" if _sensitive_query_key(k) else v)
                       for k, v in parse_qsl(parsed.query, keep_blank_values=True)], safe="*")
    return urlunsplit((parsed.scheme, host, parsed.path, query, ""))


def persist_dispatch(
    *,
    delegation_id: str,
    durable_spec: dict[str, Any],
    boot_id: str,
    dispatched_at: float,
    profile_home: Path | None = None,
    max_records: int | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Persist generation-zero intent. Returns (record, rejection_reason)."""
    now = time.time()
    record_limit = MAX_REGISTRY_RECORDS if max_records is None else max_records
    path = registry_path(profile_home)
    try:
        _assert_nonsecret_settings(durable_spec)
        with locked_registry(path=path) as registry:
            _prune(registry, now, path)
            if len(registry["records"]) >= record_limit:
                logger.error(
                    "async_delegation_sync_fallback reason=registry_cap cap=%d path=%s",
                    record_limit,
                    path,
                )
                return None, "registry_cap"
            record = {
                "delegation_id": delegation_id,
                "state": "running",
                "created_at": dispatched_at,
                "updated_at": now,
                "profile": str(durable_spec.get("profile") or "default"),
                "source": copy.deepcopy(durable_spec.get("source")),
                "execution": copy.deepcopy(durable_spec.get("execution")),
                "route": copy.deepcopy(durable_spec.get("route")),
                "attempt": {
                    "attempt_id": _attempt_id(delegation_id, 0),
                    "generation": 0,
                    "redispatch_count": 0,
                    "owner_boot_id": boot_id,
                    "started_at": dispatched_at,
                    "submitted_at": None,
                    "last_interrupted_at": None,
                    "last_error": None,
                },
                "terminal": None,
                "outbox": [],
            }
            append_lifecycle_event(
                record,
                "spawned",
                f"boot={boot_id} profile={record['profile']}",
                now=dispatched_at,
            )
            registry["records"][delegation_id] = record
        logger.info(
            "async_delegation_persisted delegation_id=%s attempt_id=%s owner_boot_id=%s profile=%s",
            delegation_id,
            record["attempt"]["attempt_id"],
            boot_id,
            record["profile"],
        )
        return record, None
    except Exception as exc:
        logger.error(
            "async_delegation_persist_failed delegation_id=%s path=%s reason=%s",
            delegation_id,
            path,
            exc,
        )
        return None, "registry_error"


def mark_submitted(
    delegation_id: str,
    attempt_id: str,
    *,
    profile_home: Path | None = None,
) -> bool:
    with locked_registry(profile_home) as registry:
        if delegation_id in set(registry.get("_invalid_record_ids", [])):
            return False
        record = registry["records"].get(delegation_id)
        if not isinstance(record, dict) or record.get("state") != "running":
            return False
        if record.get("attempt", {}).get("attempt_id") != attempt_id:
            return False
        now = time.time()
        record["attempt"]["submitted_at"] = now
        record["updated_at"] = now
        append_lifecycle_event(record, "running", f"attempt={attempt_id}", now=now)
        return True


def _terminal_payload(record: dict[str, Any], result: dict[str, Any], status: str) -> dict[str, Any]:
    route = record.get("route") or {}
    source = record.get("source") or {}
    execution = record.get("execution") or {}
    tasks = source.get("tasks") or []
    tasks = [task for task in tasks if isinstance(task, dict)] if isinstance(tasks, list) else []
    goals = [str(task.get("goal") or "") for task in tasks if isinstance(task, dict)]
    dispatched_at = record.get("created_at") or time.time()
    completed_at = time.time()
    payload = {
        "type": "async_delegation",
        "delegation_id": record.get("delegation_id"),
        "attempt_id": record.get("attempt", {}).get("attempt_id"),
        "attempt_generation": record.get("attempt", {}).get("generation"),
        "redispatch_count": record.get("attempt", {}).get("redispatch_count", 0),
        # Did this attempt ever reach an executor? mark_submitted stamps
        # submitted_at as the first thing the worker does, before invoking the
        # runner, so None proves the runner never ran. Without this, a tombstone
        # for an owner that died mid-flight (real work lost, redispatch
        # warranted) is indistinguishable from one for an attempt that never
        # started (nothing lost, bookkeeping only) — and identical-looking
        # tombstones train operators to ignore all of them.
        "died_before_start": record.get("attempt", {}).get("submitted_at") is None,
        "session_key": route.get("session_key", ""),
        "origin_ui_session_id": route.get("origin_ui_session_id", ""),
        # Raw api_server wake target — see the route note in
        # delegate_tool._build_durable_background_spec. The gateway completion
        # router reads this off the event to self-post the wake when the
        # session_key is a raw (unparseable) id.
        "origin_session_id": route.get("origin_session_id", ""),
        "parent_session_id": route.get("parent_session_id"),
        "platform": route.get("platform"),
        "chat_type": route.get("chat_type"),
        "chat_id": route.get("chat_id"),
        "thread_id": route.get("thread_id"),
        "user_id": route.get("user_id"),
        "user_name": route.get("user_name"),
        "profile": route.get("profile") or record.get("profile"),
        "goal": goals[0] if len(goals) == 1 else f"{len(goals)} parallel subagents",
        "goals": goals,
        "context": source.get("shared_context"),
        "toolsets": execution.get("toolsets"),
        "role": (tasks[0].get("role") if tasks else "leaf"),
        # No truth test on runner data: `x or y` calls __bool__, which a
        # hostile value can make raise before any normalization happens.
        "model": _first_present(
            _safe_get(result, "model"), execution.get("model")),
        "status": status,
        "summary": _safe_get(result, "summary"),
        "error": _safe_get(result, "error"),
        "api_calls": _safe_get(result, "api_calls", 0),
        "duration_seconds": _safe_get(
            result, "duration_seconds", round(completed_at - dispatched_at, 2)),
        "dispatched_at": dispatched_at,
        "completed_at": completed_at,
        "exit_reason": _safe_get(result, "exit_reason"),
    }
    # The envelope is MANDATORY and whole-record checksummed. Any
    # runner-controlled value the durable registry cannot serialize would abort
    # the terminal write and lose a completed job, so sweep every field rather
    # than guarding them one at a time.
    for key, value in list(payload.items()):
        payload[key] = _envelope_safe(value)
    is_batch = (
        source.get("kind") == "batch"
        or len(goals) > 1
        # "in" is user-defined (__contains__); read the key instead.
        or _safe_get(result, "results", _MISSING) is not _MISSING
    )
    if is_batch:
        # The delivery envelope is MANDATORY: it must always be persistable.
        # Child entries come from arbitrary runner output, so keep each one only
        # if it is exactly JSON-representable; otherwise fall back to its
        # flattened, known-safe fields. Copying them wholesale lets one
        # unsupported child value abort the terminal write and lose the job.
        entries = []
        # Only an EXACT list is iterable safely: a JSON-valid scalar such as
        # {"results": 1} would raise TypeError here, before the terminal record
        # and outbox exist, losing the completed job.
        for entry in _safe_sequence(_safe_get(result, "results")):
            if type(entry) is dict and _envelope_representable(entry):
                entries.append(entry)
            elif entry is None or _envelope_representable(entry):
                # Representable but NOT formattable: every consumer calls
                # .get() on each child, so a bare scalar/None raises in
                # process_registry._format_async_delegation and the completion
                # never reaches the user. Wrap it, keeping the value as data.
                entries.append({"status": "completed", "summary": None,
                                "value": entry})
            elif type(entry) is dict:
                # Exact dict only: a dict SUBCLASS can override get/__eq__ and
                # raise during extraction. task_index is the child's IDENTITY;
                # without it a degraded child renders under another task's goal.
                entries.append({
                    key: _envelope_safe(_safe_get(entry, key))
                    for key in ("task_index", "status", "summary", "error",
                                "model", "role", "goal", "api_calls",
                                "duration_seconds", "exit_reason")
                    if _safe_get(entry, key, _MISSING) is not _MISSING
                })
            else:
                # Consumers (process_registry._format_async_delegation,
                # tui_gateway) call .get() on every child, so a bare scalar
                # would raise in the FORMATTER and the completion would never
                # reach the user. Always emit a dict, and describe the entry
                # WITHOUT running any runner-controlled code: repr()/str() are
                # user-defined, so a raising __repr__ aborted the terminal
                # write and lost the healthy siblings' answers.
                entries.append({
                    "status": "error",
                    "error": "result entry was not representable",
                    "raw": _envelope_safe(_untrusted_type_name(entry)),
                })
        payload.update({
            "is_batch": True,
            "results": entries,
            "total_duration_seconds": _envelope_safe(
                _safe_get(result, "total_duration_seconds")),
        })
    return payload


#: ``append_terminal``'s default ``queued_boot_id``: stamp the attempt's owner boot (in-process
#: units, whose owner gateway queues the payload itself).
_OWNER_BOOT = object()

#: ``execution.runtime`` of a unit that runs as a card on the kanban ``delegations`` board
#: (docs/dev/delegate-background-on-kanban-runtime.md). Never re-dispatched by a restart (I3).
KANBAN_RUNTIME = "kanban"


def is_kanban_record(record: Any) -> bool:
    """The record's unit runs on the kanban runtime (``execution.runtime == "kanban"``)."""
    if not isinstance(record, dict):
        return False
    execution = record.get("execution")
    return isinstance(execution, dict) and execution.get("runtime") == KANBAN_RUNTIME


def append_terminal(
    delegation_id: str,
    attempt_id: str,
    result: dict[str, Any],
    status: str,
    *,
    profile_home: Path | None = None,
    queued_boot_id: Any = _OWNER_BOOT,
) -> dict[str, Any] | None:
    """Write the attempt's terminal result + outbox event; None when the attempt is not current.

    ``queued_boot_id=None`` (a kanban unit's runner, another process than any gateway) leaves
    the event unqueued, so the owning profile's gateway queues it (boot replay or the card-end
    drain); the default stamps the attempt's owner boot."""
    with locked_registry(profile_home) as registry:
        if delegation_id in set(registry.get("_invalid_record_ids", [])):
            return None
        record = registry["records"].get(delegation_id)
        if not isinstance(record, dict):
            return None
        if record.get("state") != "running" or record.get("attempt", {}).get("attempt_id") != attempt_id:
            logger.info(
                "async_delegation_stale_finalize delegation_id=%s attempt_id=%s current_attempt_id=%s state=%s",
                delegation_id,
                attempt_id,
                record.get("attempt", {}).get("attempt_id"),
                record.get("state"),
            )
            return None
        # status is runner-controlled: an unhashable value (e.g. a list) would
        # raise on the set-membership test before the terminal record or outbox
        # exist, durably stranding a completed job as running.
        # type() not isinstance(), and only AFTER that is the value hashed:
        # a str subclass with a raising/unhashable __hash__ would otherwise
        # blow up the set-membership test before anything is persisted.
        terminal_state = (
            "done" if type(status) is str and status in {"completed", "success"}
            else "failed"
        )
        payload = _terminal_payload(record, result, status)
        event_id = f"{delegation_id}:terminal:g{record['attempt']['generation']}"
        payload["event_id"] = event_id
        # The full runner result is optional archival data, not the delivery
        # envelope. Unsupported extras must not discard a valid flattened answer.
        archived_result = exact_json_archive(result)
        if archived_result is None and result is not None:
            logger.warning("Async delegation %s: optional result archive unavailable",
                           delegation_id)
        record["state"] = terminal_state
        record["terminal"] = {
            # Guarded copies, as in the envelope: terminal_state above already
            # captured the real disposition, so an unencodable status string
            # must not be allowed to abort the write.
            "status": payload.get("status"),
            "completed_at": payload["completed_at"],
            # Same guarded value the envelope carries. Both copies are
            # mandatory and whole-record checksummed, so an unencodable error
            # here aborts the terminal write and loses the completed job.
            "error": payload.get("error"),
        }
        # canonical_terminal requires terminal.result to be a dict; storing a
        # JSON-native non-dict (["ok"], "done", 3) makes it reject the record
        # on restart and the already-queued completion never replays.
        if type(archived_result) is dict:
            record["terminal"]["result"] = archived_result
        elif archived_result is not None:
            logger.warning(
                "Async delegation %s: non-mapping result archive omitted",
                delegation_id)
        record["updated_at"] = payload["completed_at"]
        append_lifecycle_event(
            record,
            terminal_state,
            f"status={payload.get('status')} attempt={attempt_id}",
            now=payload["completed_at"],
        )
        if not any(event.get("event_id") == event_id for event in record.get("outbox", [])):
            record.setdefault("outbox", []).append({
                "event_id": event_id,
                "type": "async_delegation",
                "state": "pending",
                "queued_boot_id": (record["attempt"].get("owner_boot_id")
                                   if queued_boot_id is _OWNER_BOOT else queued_boot_id),
                "created_at": payload["completed_at"],
                "delivered_at": None,
                "drop_reason": None,
                "payload": payload,
            })
        return payload


def fail_attempt(
    delegation_id: str,
    attempt_id: str,
    error: str,
    *,
    profile_home: Path | None = None,
    queued_boot_id: Any = _OWNER_BOOT,
) -> dict[str, Any] | None:
    return append_terminal(
        delegation_id,
        attempt_id,
        {"status": "error", "error": error, "summary": None},
        "error",
        profile_home=profile_home,
        queued_boot_id=queued_boot_id,
    )


def set_kanban_block(
    delegation_id: str, attempt_id: str, block: dict[str, Any], *, profile_home: Path | None = None,
) -> bool:
    """Record the unit's kanban card (``{board, task_id}``) on the current running attempt."""
    with locked_registry(profile_home) as registry:
        record = registry["records"].get(delegation_id)
        if (not isinstance(record, dict) or record.get("state") != "running"
                or (record.get("attempt") or {}).get("attempt_id") != attempt_id):
            return False
        record["kanban"] = dict(block)
        record["updated_at"] = time.time()
        append_lifecycle_event(record, "kanban_spawned",
                               f"task={block.get('task_id')} attempt={attempt_id}")
        return True


def supersede_attempt(
    delegation_id: str, old_attempt_id: str, *, profile_home: Path | None = None,
) -> str | None:
    """A kanban spawn failed: hand the unit to an in-process attempt. Returns the new attempt id.

    Generation + 1 and a new ``attempt_id``; ``execution.runtime`` becomes ``in_process`` and the
    ``kanban`` block is dropped; the state stays ``running``. Nothing else of ``claim_recoveries``'
    bump: ``redispatch_count`` is unchanged and no restart event is written (a spawn failure is
    not a restart). A runner already launched for the old attempt then finds it not current and
    exits without a model call (or has its terminal write refused). None when the old attempt is
    no longer the current running one."""
    with locked_registry(profile_home) as registry:
        record = registry["records"].get(delegation_id)
        if (not isinstance(record, dict) or record.get("state") != "running"
                or not isinstance(record.get("attempt"), dict)
                or record["attempt"].get("attempt_id") != old_attempt_id):
            return None
        attempt = record["attempt"]
        generation = int(attempt.get("generation") or 0) + 1
        new_attempt_id = _attempt_id(delegation_id, generation)
        attempt.update({"attempt_id": new_attempt_id, "generation": generation, "submitted_at": None})
        execution = record.get("execution")
        if isinstance(execution, dict):
            execution["runtime"] = "in_process"
        record.pop("kanban", None)
        record["updated_at"] = time.time()
        append_lifecycle_event(record, "kanban_superseded",
                               f"old={old_attempt_id} new={new_attempt_id}")
        return new_attempt_id


def enqueue_record_outbox(
    delegation_id: str, current_boot_id: str, *, profile_home: Path | None = None,
) -> list[dict[str, Any]]:
    """``enqueue_pending_outbox`` restricted to ONE record (the card-end drain of a kanban unit).

    Its pending events whose ``queued_boot_id`` is not this boot are stamped with this boot and
    returned for the completion queue; no other record's events are touched."""
    queued: list[dict[str, Any]] = []
    now = time.time()
    with locked_registry(profile_home) as registry:
        if delegation_id in set(registry.get("_invalid_record_ids", [])):
            return queued
        record = registry["records"].get(delegation_id)
        if not isinstance(record, dict):
            return queued
        try:
            canonical_terminal(registry, delegation_id)
        except Exception:
            logger.warning("Could not drain delegation %s; invalid canonical evidence",
                           delegation_id, exc_info=True)
            return queued
        for event in record.get("outbox", []):
            if not isinstance(event, dict) or event.get("state") != "pending":
                continue
            if event.get("queued_boot_id") == current_boot_id:
                continue
            created_at = event.get("created_at")
            if isinstance(created_at, (int, float)) and now - created_at > MAX_COMPLETION_REPLAY_AGE_SECONDS:
                event.update(state="dropped", drop_reason="stale_completion", delivered_at=now)
                continue
            event["queued_boot_id"] = current_boot_id
            queued.append(copy.deepcopy(event.get("payload") or {}))
        if queued:
            append_lifecycle_event(record, "kanban_card_ended", f"boot={current_boot_id}", now=now)
    return queued


def transition_owned(
    delegation_ids: list[str],
    state: str,
    *,
    profile_home: Path | None = None,
    timeout: float = 5.0,
    reason: str = "unspecified",
    caller: str = "",
) -> bool:
    try:
        with locked_registry(profile_home, timeout=timeout) as registry:
            now = time.time()
            invalid_record_ids = set(registry.get("_invalid_record_ids", []))
            for delegation_id in delegation_ids:
                if delegation_id in invalid_record_ids:
                    continue
                record = registry["records"].get(delegation_id)
                if isinstance(record, dict) and record.get("state") == "running":
                    record["state"] = state
                    record["updated_at"] = now
                    record["attempt"]["last_interrupted_at"] = now
                    if state == "cancelled":
                        _stamp_cancel_attribution(
                            record,
                            now=now,
                            reason=reason,
                            caller=caller,
                            via="transition_owned",
                            selector={"delegation_ids": list(delegation_ids)},
                        )
                    else:
                        append_lifecycle_event(
                            record, state, f"by={caller or 'unknown'} reason={reason}", now=now
                        )
                    if state == "cancelled":
                        for event in record.get("outbox", []):
                            if (
                                isinstance(event, dict)
                                and event.get("state") == "pending"
                                and event.get("type") == "async_delegation_restarted"
                            ):
                                event["state"] = "dropped"
                                event["drop_reason"] = "cancelled"
                                event["delivered_at"] = now
            return True
    except (TimeoutError, RegistryError) as exc:
        logger.warning(
            "async_delegation_shutdown_persist_skipped state=%s count=%d reason=%s; "
            "running+dead-boot recovery is equivalent",
            state,
            len(delegation_ids),
            exc,
        )
        return False


def cancel_matching(
    *,
    session_key: str = "",
    parent_session_id: str = "",
    origin_ui_session_id: str = "",
    all_active: bool = False,
    owner_boot_id: str = "",
    profile_home: Path | None = None,
    reason: str = "unspecified",
    caller: str = "",
    include_kanban: bool = False,
) -> int:
    """Durably cancel active records, including resume-disabled records.

    ``include_kanban``: an ``all_active`` cancel never matches a kanban-runtime record unless
    this is set, whatever its ``owner_boot_id`` (I9: a surviving unit is cancelled only on
    purpose, by its own session's selectors or an explicit opt-in). No existing caller sets it.

    ``reason``/``caller`` are Phase-0 cancel forensics: they are stamped into
    each cancelled record's additive ``cancel_attribution`` block (WHO/WHY/
    WHEN) and change no cancellation behavior.

    ``owner_boot_id`` scopes the write to the CALLER's own boot. The registry
    is process-shared, so an unscoped ``all_active`` cancel from a short-lived
    process (a ``hermes -z`` one-shot's shutdown cleanup) silently killed every
    in-flight delegation the live gateway owned. When supplied, a record owned
    by a DIFFERENT boot that is still ALIVE is skipped; a record owned by a
    dead boot (crash recovery) or carrying no owner (legacy) stays cancellable.
    """
    if not registry_path(profile_home).exists():
        return 0
    count = 0
    with locked_registry(profile_home) as registry:
        now = time.time()
        invalid_record_ids = set(registry.get("_invalid_record_ids", []))
        for delegation_id, record in registry["records"].items():
            if str(delegation_id) in invalid_record_ids or not isinstance(record, dict):
                continue
            if record.get("state") not in {"running", "recoverable"}:
                continue
            route = record.get("route") or {}
            # Scoped-pair semantics mirror interrupt_for_session (Greptile P1
            # 2026-07-11): key+parent supplied together => a record recorded
            # under a DIFFERENT parent_session_id is a prior /new-reset
            # session's job and must not be durably cancelled by key alone.
            _scoped = bool(session_key and parent_session_id)
            _rec_parent = str(route.get("parent_session_id") or "")
            _key_ok = bool(
                session_key
                and str(route.get("session_key") or "") == session_key
                and not (_scoped and _rec_parent and _rec_parent != parent_session_id)
            )
            matches = all_active or (
                _key_ok
                or (
                    parent_session_id
                    and _rec_parent == parent_session_id
                )
                or (
                    origin_ui_session_id
                    and str(route.get("origin_ui_session_id") or "")
                    == origin_ui_session_id
                )
            )
            if not matches:
                continue
            if all_active and not include_kanban and is_kanban_record(record):
                continue
            if owner_boot_id:
                record_owner = str(
                    (record.get("attempt") or {}).get("owner_boot_id") or ""
                )
                if (
                    record_owner
                    and record_owner != owner_boot_id
                    and is_boot_id_alive(record_owner)
                ):
                    logger.info(
                        "async_delegation_cancel_skipped_foreign_boot "
                        "delegation_id=%s owner_boot_id=%s caller_boot_id=%s caller=%s",
                        delegation_id,
                        record_owner,
                        owner_boot_id,
                        caller,
                    )
                    continue
            record["state"] = "cancelled"
            record["updated_at"] = now
            _stamp_cancel_attribution(
                record,
                now=now,
                reason=reason,
                caller=caller,
                via="cancel_matching",
                selector={
                    "session_key": session_key,
                    "parent_session_id": parent_session_id,
                    "origin_ui_session_id": origin_ui_session_id,
                    "all_active": all_active,
                },
            )
            attempt = record.get("attempt")
            if isinstance(attempt, dict):
                attempt["last_interrupted_at"] = now
            for event in record.get("outbox", []):
                if (
                    isinstance(event, dict)
                    and event.get("state") == "pending"
                    and event.get("type") == "async_delegation_restarted"
                ):
                    event["state"] = "dropped"
                    event["drop_reason"] = "cancelled"
                    event["delivered_at"] = now
            count += 1
    return count


def is_boot_id_alive(boot_id: str) -> bool:
    """Use the gateway's canonical boot-id producer and liveness contract."""
    from gateway.status import is_boot_id_alive as _gateway_boot_is_alive

    return _gateway_boot_is_alive(boot_id)


def _restart_payload(
    record: dict[str, Any],
    interrupted_at: float,
    restarted_at: float,
    *,
    died_before_start: bool,
) -> dict[str, Any]:
    route = record.get("route") or {}
    tasks = (record.get("source") or {}).get("tasks") or []
    goals = [str(task.get("goal") or "") for task in tasks if isinstance(task, dict)]
    return {
        "type": "async_delegation_restarted",
        "delegation_id": record["delegation_id"],
        "attempt_id": record["attempt"]["attempt_id"],
        "attempt_generation": record["attempt"]["generation"],
        "redispatch_count": record["attempt"]["redispatch_count"],
        # Describes the SUPERSEDED attempt, not the replacement. The caller must
        # capture it before claiming rewrites attempt.submitted_at to None,
        # otherwise every restart notice would claim died_before_start=True.
        "died_before_start": died_before_start,
        "interrupted_at": interrupted_at,
        "restarted_at": restarted_at,
        "session_key": route.get("session_key", ""),
        "parent_session_id": route.get("parent_session_id"),
        "origin_ui_session_id": route.get("origin_ui_session_id", ""),
        # Same raw api_server wake target as the terminal payload — a restart
        # notice must be routable on an unparseable session_key too.
        "origin_session_id": route.get("origin_session_id", ""),
        "platform": route.get("platform"),
        "chat_type": route.get("chat_type"),
        "chat_id": route.get("chat_id"),
        "session_id": route.get("session_id"),
        "user_id": route.get("user_id"),
        "user_name": route.get("user_name"),
        "profile": route.get("profile") or record.get("profile"),
        "goal": goals[0] if len(goals) == 1 else f"{len(goals)} parallel subagents",
        "goals": goals,
        "is_batch": len(goals) > 1 or (record.get("source") or {}).get("kind") == "batch",
    }


def _fail_recovery_record(
    record: dict[str, Any], error: str, now: float, *, emit_event: bool,
    event_suffix: str | None = None,
) -> None:
    record["state"] = "failed"
    record["updated_at"] = now
    record["terminal"] = {"status": "error", "error": _envelope_safe(error),
                          "completed_at": now}
    # A checksum-valid record can carry a truthy NON-DICT route; `or {}` only
    # catches falsy values, so route.get() below would raise and abort the
    # whole claim_recoveries loop, stranding every later healthy delegation.
    route = record.get("route")
    if not isinstance_safe(route, dict):
        route = {}
    if (not emit_event
            or not all(str(route.get(key) or "").strip()
                       for key in ("platform", "session_key", "parent_session_id"))
            or (route.get("profile") or record.get("profile")) != record.get("profile")):
        logger.warning("Delegation %s failed (%s) without a safe notification route",
                       record.get("delegation_id"), error)
        return
    payload = _terminal_payload(record, {"error": error}, "error")
    payload["completed_at"] = now
    event_id = f"{record.get('delegation_id')}:terminal:{event_suffix or error}"
    payload["event_id"] = event_id
    if not any(event.get("event_id") == event_id for event in record.get("outbox", [])):
        entry = {
            "event_id": event_id,
            "type": "async_delegation",
            "state": "pending",
            "queued_boot_id": None,
            "created_at": now,
            "delivered_at": None,
            "drop_reason": None,
            "payload": payload,
        }
        # Validate before publishing; never normalize conflicting identity.
        candidate = {**record, "outbox": [*record.get("outbox", []), entry]}
        try:
            canonical_terminal({"records": {record["delegation_id"]: candidate}},
                               record["delegation_id"])
        except RegistryError:
            logger.warning("Delegation %s failed (%s) without a valid notification identity",
                           record.get("delegation_id"), error, exc_info=True)
            return
        record.setdefault("outbox", []).append(entry)


def claim_recoveries(
    *,
    current_boot_id: str,
    resume_enabled: bool,
    profile_home: Path | None = None,
    owner_alive=is_boot_id_alive,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    summary = {"scanned": 0, "eligible": 0, "claimed": 0, "exhausted": 0, "failed_validation": 0}
    if not resume_enabled:
        return [], summary
    claimed: list[dict[str, Any]] = []
    now = time.time()
    with locked_registry(profile_home) as registry:
        invalid_record_ids = set(registry.get("_invalid_record_ids", []))
        for delegation_id, record in list(registry["records"].items()):
            summary["scanned"] += 1
            if str(delegation_id) in invalid_record_ids:
                summary["failed_validation"] += 1
                continue
            if not isinstance(record, dict):
                summary["failed_validation"] += 1
                continue
            state = record.get("state")
            if state not in {"running", "recoverable"}:
                continue
            attempt = record.get("attempt")
            source = record.get("source")
            execution = record.get("execution")
            route = record.get("route")
            if (
                not isinstance(attempt, dict)
                or not isinstance(source, dict)
                or not isinstance(execution, dict)
                or not isinstance(route, dict)
            ):
                summary["failed_validation"] += 1
                continue
            route_ready = all(
                str(route.get(key) or "").strip()
                for key in ("platform", "session_key", "parent_session_id")
            )
            tasks = source.get("tasks")
            tasks_valid = (
                isinstance(tasks, list)
                and bool(tasks)
                and all(
                    isinstance(task, dict)
                    and bool(str(task.get("goal") or "").strip())
                    for task in tasks
                )
            )
            profile_matches = str(
                route.get("profile") or record.get("profile") or ""
            ) == str(record.get("profile") or "")
            if not route_ready or not tasks_valid or not profile_matches:
                error = "unsupported_route" if not route_ready else "invalid_record"
                _fail_recovery_record(
                    record,
                    error,
                    now,
                    emit_event=bool(route.get("platform") and route.get("session_key")),
                )
                summary["failed_validation"] += 1
                continue
            if isinstance(record.get("detached"), dict):
                continue  # a live detached child: adopted (claim_detached_adoptions), never re-run
            if isinstance(record.get("detached_fenced"), dict):
                continue  # a stood-down child not yet confirmed dead: never run a second generation beside it
            if is_kanban_record(record):
                continue  # its own process outlives the owner; never re-dispatched (I3)
            owner = str(attempt.get("owner_boot_id") or "")
            if state == "running" and owner == current_boot_id:
                continue
            if state == "running" and owner_alive(owner):
                continue
            summary["eligible"] += 1
            if now - float(record.get("created_at") or now) > ACTIVE_STALE_SECONDS:
                _fail_recovery_record(record, "stale_record", now,
                                      emit_event=True, event_suffix="stale")
                continue
            # RC-1: a dead boot's claim with no executor-submission telemetry did
            # not launch replacement work and therefore does not consume retry budget.
            redispatch_count = int(attempt.get("redispatch_count") or 0)
            if int(attempt.get("generation") or 0) > 0 and attempt.get("submitted_at") is None:
                redispatch_count = max(0, redispatch_count - 1)
            if redispatch_count >= MAX_REDISPATCH_ATTEMPTS:
                _fail_recovery_record(record, "restart_attempts_exhausted", now,
                                      emit_event=True, event_suffix="exhausted")
                summary["exhausted"] += 1
                logger.warning(
                    "async_delegation_retry_exhausted delegation_id=%s redispatch_count=%d",
                    delegation_id,
                    redispatch_count,
                )
                continue
            generation = int(attempt.get("generation") or 0) + 1
            interrupted_at = float(attempt.get("started_at") or record.get("updated_at") or now)
            # Capture BEFORE the attempt.update() below resets submitted_at to
            # None for the replacement attempt — the notice describes the attempt
            # being superseded.
            died_before_start = attempt.get("submitted_at") is None
            for event in record.get("outbox", []):
                if (
                    isinstance(event, dict)
                    and event.get("type") == "async_delegation_restarted"
                    and event.get("state") == "pending"
                    and int((event.get("payload") or {}).get("attempt_generation") or -1) < generation
                ):
                    event["state"] = "dropped"
                    event["drop_reason"] = "superseded"
                    event["delivered_at"] = now
            attempt.update({
                "attempt_id": _attempt_id(delegation_id, generation),
                "generation": generation,
                "redispatch_count": redispatch_count + 1,
                "owner_boot_id": current_boot_id,
                "started_at": now,
                "submitted_at": None,
                "last_interrupted_at": interrupted_at,
                "last_error": None,
            })
            record["state"] = "running"
            record["updated_at"] = now
            payload = _restart_payload(
                record, interrupted_at, now, died_before_start=died_before_start
            )
            event_id = f"{delegation_id}:restart:g{generation}"
            payload["event_id"] = event_id
            record.setdefault("outbox", []).append({
                "event_id": event_id,
                "type": "async_delegation_restarted",
                "state": "pending",
                "queued_boot_id": None,
                "created_at": now,
                "delivered_at": None,
                "drop_reason": None,
                "payload": payload,
            })
            claimed.append(copy.deepcopy(record))
            summary["claimed"] += 1
            logger.info(
                "async_delegation_recovery_claimed delegation_id=%s attempt_id=%s owner_boot_id=%s redispatch_count=%d",
                delegation_id,
                attempt["attempt_id"],
                current_boot_id,
                attempt["redispatch_count"],
            )
    return claimed, summary


def set_detached_block(
    delegation_id: str, attempt_id: str, block: dict[str, Any], *, profile_home: Path | None = None,
) -> bool:
    """Record the spawned detached child on the current attempt (False: cancelled/superseded). A record a
    racing shutdown already marked ``recoverable`` still takes the block: the next boot adopts it."""
    with locked_registry(profile_home) as registry:
        record = registry["records"].get(delegation_id)
        if (not isinstance(record, dict) or record.get("state") not in {"running", "recoverable"}
                or (record.get("attempt") or {}).get("attempt_id") != attempt_id):
            return False
        record["detached"] = dict(block)
        append_lifecycle_event(record, "detached_spawned", f"pid={block.get('pid')} attempt={attempt_id}")
        return True


def clear_detached(delegation_id: str, attempt_id: str, *, profile_home: Path | None = None) -> bool:
    """A unit that fell back to an in-process thread is no longer a detached unit."""
    with locked_registry(profile_home) as registry:
        record = registry["records"].get(delegation_id)
        if (not isinstance(record, dict)
                or (record.get("attempt") or {}).get("attempt_id") != attempt_id
                or not isinstance(record.get("execution"), dict)):
            return False
        record["execution"]["detached"] = False
        record.pop("detached", None)
        append_lifecycle_event(record, "detached_fallback", f"attempt={attempt_id}")
        return True


def stand_down_detached(
    delegation_id: str, attempt_id: str, owner_boot_id: str, *, profile_home: Path | None = None,
    block: dict[str, Any] | None = None,
) -> bool:
    """Fence an un-advertised child before its dispatcher kills it: False when another boot already
    adopted this attempt (the child is theirs now; leave it running). True otherwise, and adoption of
    this attempt is refused from now on (``detached_fence``). The child's identity stays on the record
    (``detached_fenced``) until its death is confirmed (``clear_detached_fenced``): until then no boot
    re-dispatches the unit, and any boot's adoption pass re-sends the kill."""
    with locked_registry(profile_home) as registry:
        record = registry["records"].get(delegation_id)
        if not isinstance(record, dict) or (record.get("attempt") or {}).get("attempt_id") != attempt_id:
            return True
        if str((record.get("attempt") or {}).get("owner_boot_id") or "") != str(owner_boot_id or ""):
            return False
        record["detached_fence"] = attempt_id
        record["detached_fenced"] = {**(record.pop("detached", None) or {}), **(block or {}),
                                     "attempt_id": attempt_id}
        append_lifecycle_event(record, "detached_stood_down", f"attempt={attempt_id}")
        return True


def clear_detached_fenced(delegation_id: str, attempt_id: str, *, profile_home: Path | None = None) -> bool:
    """The stood-down child of ``attempt_id`` is confirmed dead: the unit may be re-dispatched."""
    with locked_registry(profile_home) as registry:
        record = registry["records"].get(delegation_id)
        if not isinstance(record, dict) or (record.get("detached_fenced") or {}).get("attempt_id") != attempt_id:
            return False
        record.pop("detached_fenced", None)
        append_lifecycle_event(record, "detached_fenced_dead", f"attempt={attempt_id}")
        return True


def claim_detached_adoptions(
    *, current_boot_id: str, profile_home: Path | None = None, owner_alive=is_boot_id_alive,
    probe=None, identity=None, job_dir_for=None, stand_down_unknown=None, alive=None,
    undecided: list | None = None, survivors=None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Adopt detached units whose owner is gone (docs/dev/delegate-detached-background.md).

    Returns ``(adopted, kill)``: adopted record copies (owner rewritten to this boot, same attempt) and
    ``{pid, create_time}`` identities of children that must be signalled (cancelled or past the age cap).
    A detached unit whose child is dead with no result loses its ``detached`` block and is left to
    ``claim_recoveries`` (normal re-dispatch). Runs regardless of ``resume_on_restart``.

    ``stand_down_unknown(home)`` lists job dirs whose dispatcher could not fence its child (registry
    error): one no live record owns is an orphan, killed (``kill`` entry carries ``remove_job_dir``).

    ``undecided`` (out) receives every job dir this pass could not settle (identity unknown, or a kill
    whose effect is not confirmed yet): the caller re-runs the pass later in the same boot."""
    adopted: list[dict[str, Any]] = []
    kill: list[dict[str, Any]] = []
    if not registry_path(profile_home).exists():
        return adopted, kill
    home = registry_path(profile_home).parent.parent
    unfenced = stand_down_unknown(home) if stand_down_unknown else []
    with locked_registry(profile_home, write=False) as snapshot:
        any_detached = any(isinstance(r, dict) and isinstance(r.get("execution"), dict)
                           and r["execution"].get("detached") for r in snapshot["records"].values())
    if not any_detached and not unfenced:
        return adopted, kill  # nothing to adopt or kill: never rewrite a registry this pass does not own
    now = time.time()
    owned_dirs: set[str] = set()
    adopted_dirs: set[str] = set()
    killed_dirs: set[str] = set()
    retained_dirs: set[str] = set()  # identity unknown this pass: nothing about them is decided
    with locked_registry(profile_home) as registry:
        invalid_record_ids = set(registry.get("_invalid_record_ids", []))
        for delegation_id, record in registry["records"].items():
            if str(delegation_id) in invalid_record_ids or not isinstance(record, dict):
                continue
            execution = record.get("execution")
            attempt = record.get("attempt")
            if not isinstance(execution, dict) or not execution.get("detached") or not isinstance(attempt, dict):
                continue
            state = record.get("state")
            block = record.get("detached") if isinstance(record.get("detached"), dict) else None
            job_dir = Path(block["job_dir"]) if block and block.get("job_dir") else job_dir_for(
                home, str(attempt.get("attempt_id") or ""))
            fenced = record.get("detached_fenced")
            if isinstance(fenced, dict):
                # A stood-down child (any state) whose death nobody confirmed yet: its dispatcher may have
                # died between the fence and the kill. Kill it (again); claim_recoveries holds the unit back
                # until ``detached_fenced`` is cleared, so no second generation runs beside it.
                fenced_dir = Path(fenced["job_dir"]) if fenced.get("job_dir") else job_dir
                killed_dirs.add(str(fenced_dir))
                ident, known = _lookup_identity(identity, fenced_dir, fenced if fenced.get("pid") else None)
                if not known:
                    retained_dirs.add(str(fenced_dir))  # unknown is not dead: keep the fence, retry next pass
                    continue
                if ident and _child_alive(probe, alive, fenced_dir, fenced, ident):
                    kill.append({**ident, "job_dir": str(fenced_dir), "fenced": str(delegation_id),
                                 "attempt_id": fenced.get("attempt_id")})
                    # Its dir is removed only once the exit is confirmed (the fenced kill path), never by
                    # the stand-down-unknown sweep below while the child may still be alive.
                    retained_dirs.add(str(fenced_dir))
                else:
                    record.pop("detached_fenced", None)
                    append_lifecycle_event(record, "detached_fenced_dead", f"attempt={fenced.get('attempt_id')}",
                                           now=now)
                    kill.append({"job_dir": str(fenced_dir), "remove_job_dir": True})
                continue
            if state == "cancelled" or (state == "failed" and block is not None):
                # Terminal-without-result: a still-live child is an orphan. ``failed`` + block is the age cap
                # below. The block stays until the child is confirmed dead, so a kill a gateway died before
                # sending is re-sent by every later boot, not only the next one.
                killed_dirs.add(str(job_dir))
                ident, known = _lookup_identity(identity, job_dir, block)
                if not known:
                    retained_dirs.add(str(job_dir))  # unknown is not dead: keep the block, retry next pass
                    continue
                if ident and _child_alive(probe, alive, job_dir, block, ident):
                    kill.append({**ident, "job_dir": str(job_dir)})
                    # Persisted even when it was only recovered from the job dir (cancelled before the
                    # block was advertised): a boot that dies before signalling must not lose it.
                    record["detached"] = {**(block or {}), **ident, "job_dir": str(job_dir),
                                          "attempt_id": attempt.get("attempt_id")}
                    retained_dirs.add(str(job_dir))  # not confirmed dead: keep its dir and marker
                    append_lifecycle_event(record, "detached_orphan_killed", f"pid={ident.get('pid')}", now=now)
                else:
                    record.pop("detached", None)
                continue
            if state not in {"running", "recoverable"}:
                continue
            if record.get("detached_fence") == attempt.get("attempt_id"):
                continue  # its dispatcher stood this attempt down; claim_recoveries re-dispatches it
            owned_dirs.add(str(job_dir))
            owner = str(attempt.get("owner_boot_id") or "")
            if state == "running" and (owner == current_boot_id or owner_alive(owner)):
                continue
            if now - float(record.get("created_at") or now) > ACTIVE_STALE_SECONDS:
                killed_dirs.add(str(job_dir))
                ident, known = _lookup_identity(identity, job_dir, block)
                if ident:
                    kill.append({**ident, "job_dir": str(job_dir)})
                    record["detached"] = {**(block or {}), **ident, "job_dir": str(job_dir)}
                elif not known:
                    # Unknown is not dead: keep a block so the failed+block branch re-checks it every boot.
                    retained_dirs.add(str(job_dir))
                    record["detached"] = {**(block or {}), "job_dir": str(job_dir),
                                          "attempt_id": attempt.get("attempt_id")}
                else:
                    record.pop("detached", None)
                _fail_recovery_record(record, "stale_record", now, emit_event=True, event_suffix="stale")
                continue
            status = probe(job_dir, block)
            if status == "dead" and survivors is not None:
                # A dead leader is not a dead attempt while tools it started still run: keep the block (no
                # re-dispatch beside them), kill them, and re-check on the next pass.
                lead = block if block and block.get("create_time") is not None else None
                if lead is None:
                    lead, _ = _lookup_identity(identity, job_dir, block)
                left = survivors(job_dir, (lead or {}).get("pid"), (lead or {}).get("create_time")) if lead else []
                if left is None or left:
                    retained_dirs.add(str(job_dir))
                    if block is None:
                        record["detached"] = {"pid": (lead or {}).get("pid"), "create_time": (lead or {}).get(
                            "create_time"), "job_dir": str(job_dir), "attempt_id": attempt.get("attempt_id"),
                            "spawned_at": None}
                    for m_pid, m_ct in left or []:
                        kill.append({"pid": m_pid, "create_time": m_ct, "job_dir": str(job_dir)})
                    logger.warning("async_delegation_detached_leader_dead_tools_alive delegation_id=%s "
                                   "survivors=%s", delegation_id, "unknown" if left is None else len(left))
                    continue
            if status == "dead":
                record.pop("detached", None)
                append_lifecycle_event(record, "detached_lost", f"attempt={attempt.get('attempt_id')}", now=now)
                continue
            ident, known = ({}, True)
            if status != "unknown" and (block is None or block.get("create_time") is None):
                ident, known = _lookup_identity(identity, job_dir, block)
            if status == "unknown" or (not known and status != "result"):
                # Process inspection failed: neither adopt nor let claim_recoveries re-run the unit beside a
                # child that may be alive. A held block keeps it out of re-dispatch; the next pass retries.
                # A published result.json needs no inspection: it is adopted (unverified, never signalled).
                retained_dirs.add(str(job_dir))
                if block is None:
                    record["detached"] = {"pid": None, "create_time": None, "job_dir": str(job_dir),
                                          "attempt_id": attempt.get("attempt_id"), "spawned_at": None}
                logger.warning("async_delegation_detached_identity_unknown delegation_id=%s job_dir=%s: "
                               "left for the next pass", delegation_id, job_dir)
                continue
            if block is None or block.get("create_time") is None:
                # Adopt by a VERIFIED identity (pid + start time); an unverifiable one is never signalled.
                ident = ident or {}
                block = {**(block or {"spawned_at": None}), "pid": ident.get("pid"),
                         "create_time": ident.get("create_time"), "job_dir": str(job_dir),
                         "attempt_id": attempt.get("attempt_id")}
                record["detached"] = block
            attempt["owner_boot_id"] = current_boot_id
            record["state"] = "running"
            record["updated_at"] = now
            append_lifecycle_event(record, "adopted", f"boot={current_boot_id} child={status}", now=now)
            adopted.append(copy.deepcopy(record))
            adopted_dirs.add(str(job_dir))
            logger.info(
                "async_delegation_detached_adopted delegation_id=%s attempt_id=%s pid=%s child=%s",
                delegation_id, attempt.get("attempt_id"), block.get("pid"), status,
            )
        # Decided under the same lock, so no boot can adopt a dir between this check and the kill.
        for job_dir in (stand_down_unknown(home) if stand_down_unknown else []):
            if str(job_dir) in adopted_dirs:
                Path(job_dir, "stand-down-unknown.json").unlink(missing_ok=True)
            elif str(job_dir) in retained_dirs:
                continue  # its child's identity is unknown this pass: keep the dir and marker for a retry
            elif str(job_dir) not in owned_dirs:
                # Already signalled above (cancelled / age cap): only the dir is left to remove.
                ident, known = ({}, True) if str(job_dir) in killed_dirs else _lookup_identity(
                    identity, Path(job_dir), None)
                if not known:
                    # Unknown is not dead: removing the dir and marker now would strand a live child with
                    # no recovery or cancellation path. Keep both; the next pass retries.
                    logger.warning("async_delegation_detached_unfenced_orphan_unknown job_dir=%s: kept", job_dir)
                    retained_dirs.add(str(job_dir))
                    continue
                ident = ident or {}
                kill.append({**ident, "job_dir": str(job_dir), "remove_job_dir": True})
                logger.warning("async_delegation_detached_unfenced_orphan job_dir=%s pid=%s",
                               job_dir, ident.get("pid"))
                if ident.get("pid") is not None:
                    retained_dirs.add(str(job_dir))
    if undecided is not None:
        undecided.extend(sorted(retained_dirs))
    return adopted, kill


def _child_alive(probe, alive, job_dir: Path, block, ident: dict[str, Any]) -> bool:
    """Is the identified child still running? ``alive(ident)`` (pid + start time) when supplied.
    An ``unknown`` probe counts as alive: the kill is re-checked against pid + start time anyway."""
    if alive is not None:
        return bool(alive({**ident, "job_dir": ident.get("job_dir") or str(job_dir)}))
    return probe(job_dir, block) in {"alive", "unknown"}


def _lookup_identity(identity, job_dir: Path, block) -> tuple[dict[str, Any] | None, bool]:
    """``(identity, known)``. ``known`` False when the lookup itself failed (process inspection
    error): the child may be alive, so the caller must not treat it as dead."""
    try:
        return identity(job_dir, block), True
    except Exception:
        logger.warning("async_delegation_detached_identity_lookup_failed job_dir=%s", job_dir, exc_info=True)
        return None, False


_MISSING = object()
# The real ``__name__`` descriptor on ``type`` itself. Bound to a type object
# directly, it cannot be intercepted by a hostile metaclass ``__getattribute__``
# or ``__name__`` property.
_TYPE_NAME_SLOT = type.__dict__["__name__"]


def _first_present(primary: Any, fallback: Any) -> Any:
    """``primary or fallback`` without truth-testing untrusted data."""
    if primary is None:
        return fallback
    # `type(x) in (...)` compares with ==, which a hostile METACLASS can
    # override. Use identity only -- `is` cannot be intercepted.
    primary_type = type(primary)
    for known in (str, bytes, list, tuple, dict, set):
        if primary_type is known:
            try:
                return primary if len(primary) else fallback
            except Exception:  # noqa: BLE001
                return fallback
    return primary


def _safe_get(mapping: Any, key: str, default: Any = None) -> Any:
    """Read one key from an untrusted mapping without running its code.

    A runner result (or a child entry) can be a dict SUBCLASS overriding
    ``get``/``__contains__``/``keys``/``__iter__``, or an object whose
    ``__class__`` raises. Any of those turns a plain lookup into an exception
    -- or, worse, silently drops real data. Read through ``dict`` itself so
    only the C-level slot is used, and never let a lookup escape.
    """
    if not isinstance_safe(mapping, dict):
        return default
    # Even dict.get() is not safe: a lookup hashes the probe key and, on a
    # hash collision, compares it against STORED keys with ==. A hostile key
    # sitting in the runner's result therefore executes its own __eq__ during
    # an ordinary get() and aborts the terminal write.
    #
    # Walk the real items instead and match by identity/exact-str value, so no
    # stored key's __eq__ or __hash__ is ever invoked.
    try:
        items = list(dict.items(mapping))
    except Exception:  # noqa: BLE001 - never break the terminal write
        return default
    for stored_key, stored_value in items:
        if stored_key is key:
            return stored_value
        if type(stored_key) is str and str.__eq__(stored_key, key) is True:
            return stored_value
    return default


def isinstance_safe(value: Any, kind: type) -> bool:
    """``isinstance`` that cannot be defeated by a hostile ``__class__``."""
    try:
        return isinstance(value, kind)
    except Exception:  # noqa: BLE001
        return False


def _untrusted_type_name(value: Any) -> str:
    """Describe an unrepresentable entry without running its code.

    ``repr()``/``str()`` are user-defined: a runner child whose ``__repr__``
    raises aborted ``_terminal_payload`` before the terminal record or outbox
    existed, destroying its healthy siblings' answers. Read the name through
    ``type.__dict__["__name__"]`` -- the C-level slot on the real type object,
    which neither the instance nor a hostile metaclass can intercept -- and
    verify the result is an exact ``str`` before using it.
    """
    try:
        name = _TYPE_NAME_SLOT.__get__(type(value))
    except Exception:  # noqa: BLE001 - never break the terminal write
        return "<unrepresentable>"
    if type(name) is not str:
        return "<unrepresentable>"
    return f"<unrepresentable {name}>"


def _safe_sequence(value: Any) -> list:
    """Return an EXACT list of entries from an untrusted sequence value.

    ``len()``/iteration are user-defined on a list subclass, so a hostile
    ``__len__`` can make a populated list look empty and silently discard every
    child's real answer.
    """
    if type(value) is list:
        return value
    if value is None:
        return []
    # Rebuild through the CONCRETE type's own slots. len()/tuple()/value[i]
    # are all virtual: a subclass can lie about its length or hand back
    # different items on indexing, silently dropping or fabricating answers.
    if isinstance_safe(value, list):
        try:
            size = list.__len__(value)
            return [list.__getitem__(value, index) for index in range(size)]
        except Exception:  # noqa: BLE001
            return []
    if isinstance_safe(value, tuple):
        try:
            size = tuple.__len__(value)
            return [tuple.__getitem__(value, index) for index in range(size)]
        except Exception:  # noqa: BLE001
            return []
    return [value]


def _envelope_representable(value: Any) -> bool:
    """True if the durable registry can persist ``value`` exactly.

    ``exact_json_archive`` returns ``None`` both for a VALID JSON null and for
    an unavailable archive, so it cannot be used directly as a validity test --
    doing so replaces a legitimate ``None`` result with a fabricated error.
    """
    if value is None:
        return True
    return exact_json_archive(value) is not None


def _envelope_safe(value: Any) -> Any:
    """Return ``value`` if the durable registry can persist it, else a marker.

    The delivery envelope is mandatory: it must always be writable. A lone
    surrogate (or any value the registry's UTF-8 serialization rejects) would
    otherwise raise inside ``_record_checksum`` and lose a completed job.
    """
    if _envelope_representable(value):
        return value
    return "<unrepresentable>"


def _is_optional_number(value: Any) -> bool:
    """True for None or a real finite number the consumers can actually use.

    ``bool`` is a subclass of ``int``, so an ``isinstance`` check would accept
    ``completed_at: true`` and persist it as timestamp ``1``.

    An ``int`` must also survive the ``float()`` conversion the recovery and
    replay loops perform: a checksum-valid ``created_at`` of ``10**400`` passes
    a bare type check, then raises ``OverflowError`` mid-scan and aborts
    recovery for every healthy delegation in the profile. A value the consumer
    cannot convert is not a valid number.

    ``float()`` is not the only conversion, and it is not the tightest one:
    these numbers are bound as SQLite parameters, and sqlite3 refuses any int
    outside the signed 64-bit range with ``OverflowError: Python int too large
    to convert to SQLite INTEGER``. ``2 ** 100`` is a finite JSON integer whose
    ``float()`` succeeds, so the isfinite check alone let it through validation
    and it then raised out of the per-record recovery loop, aborting
    restoration of every healthy sibling. Bound ints to what the consumer can
    actually bind. Floats are NOT range-limited: sqlite3 binds any finite float
    as REAL.
    """
    if value is None:
        return True
    if type(value) is int:
        if not (_SQLITE_INT_MIN <= value <= _SQLITE_INT_MAX):
            return False
        try:
            return math.isfinite(float(value))
        except (OverflowError, ValueError):
            return False
    if type(value) is float:
        return math.isfinite(value)
    return False


def _is_exact_json_value(value: Any, _seen: frozenset[int] = frozenset()) -> bool:
    """True only for values JSON preserves EXACTLY, using exact types.

    ``type(x) is T`` (not ``isinstance``) so that ``dict``/``list``/``str``
    subclasses -- which may override ``__eq__`` -- are refused rather than
    trusted. ``bool`` is checked before ``int`` because ``bool`` is a subclass
    of ``int``; both round-trip exactly, so both are accepted. Containers track
    identity along the current path: a self-referential structure is simply not
    JSON-representable, and must be REFUSED rather than recursed into (a
    RecursionError here would abort the completion this archive is optional to).
    """
    if value is None or type(value) is bool or type(value) is str:
        return True
    if type(value) is int:
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list or type(value) is dict:
        if id(value) in _seen:
            return False
        nested = _seen | {id(value)}
        if type(value) is list:
            return all(_is_exact_json_value(item, nested) for item in value)
        return all(
            type(key) is str and _is_exact_json_value(item, nested)
            for key, item in value.items()
        )
    return False


def exact_json_archive(result: Any) -> Any | None:
    """Return ``result`` only if JSON preserves it EXACTLY, else ``None``.

    A round trip succeeds while silently rewriting non-JSON-native shapes:
    int keys become strings, tuples become lists, and a ``{1: ..., "1": ...}``
    collision discards a value outright. Presenting that transformed object as
    the original execution result is a wrong answer, so the archive is exact or
    it is unavailable.
    """
    # Exactness is decided STRUCTURALLY, never by caller-controlled __eq__:
    # a dict subclass can serialize fine, be rewritten by JSON (int keys ->
    # strings, tuples -> lists, colliding 1/"1" dropping a value), and still
    # return True from __eq__, which would certify transformed data as the
    # original execution result.
    #
    # The ENTIRE decision -- traversal included -- sits inside the guard. The
    # traversal itself can fail on input the caller controls (a deeply nested
    # but perfectly ordinary result exhausts the recursion limit), and this
    # archive is OPTIONAL: any failure must degrade to "unavailable", never
    # abort the terminal write and delivery of a completed job.
    try:
        if not _is_exact_json_value(result):
            return None
        # Validate with the registry's OWN serialization (ensure_ascii=False, then
        # UTF-8). json.dumps' default ensure_ascii=True happily encodes a lone
        # surrogate, so an archive accepted here could later raise
        # UnicodeEncodeError inside _record_checksum -- aborting the terminal
        # write and losing a completed job.
        json.dumps(result, allow_nan=False, ensure_ascii=False).encode("utf-8")
        return json.loads(json.dumps(result, allow_nan=False))
    except Exception:
        return None


def canonical_terminal(registry, delegation_id):
    """Validate terminal identity for both canonical and mirror consumers.

    Caller holds the registry lock. Sparse historical outboxes and terminal
    work intentionally emitted without an envelope have no execution evidence.
    """
    if delegation_id in registry.get("_invalid_record_ids", []):
        raise RegistryError(f"record {delegation_id} failed integrity validation")
    record = registry["records"].get(delegation_id)
    if record is None or record.get("state") not in {"done", "failed", "cancelled"}:
        return None
    if record.get("terminal") is None:
        return None
    attempt = record.get("attempt") or {}
    # Sparse historical records may omit the redundant record-level id (the
    # loader permits exactly that); a PRESENT id must still match the key.
    self_id_conflicts = ("delegation_id" in record
                        and record.get("delegation_id") != delegation_id)
    entries = [entry for entry in record.get("outbox", [])
               if entry.get("type") == "async_delegation"]
    if not entries:
        terminal = record["terminal"]
        if (self_id_conflicts
                or type(attempt.get("generation")) is not int
                or attempt["generation"] < 0
                or not attempt.get("attempt_id")
                or not isinstance(terminal, dict)
                or terminal.get("status") not in {"completed", "success", "error", "interrupted", "cancelled"}
                or (terminal.get("result") is not None
                    and not isinstance(terminal["result"], dict))
                or (terminal.get("completed_at") is not None
                    and not isinstance(terminal["completed_at"], (int, float)))):
            raise RegistryError(f"invalid non-deliverable terminal evidence for {delegation_id}")
        return None
    if len(entries) != 1:
        raise RegistryError(f"ambiguous terminal outbox for {delegation_id}")
    entry = entries[0]
    payload = entry.get("payload") or {}
    if (self_id_conflicts
            or not entry.get("event_id")
            or payload.get("event_id") != entry["event_id"]
            or payload.get("delegation_id") != delegation_id
            or payload.get("type") != "async_delegation"
            or type(payload.get("attempt_generation")) is not int
            or type(attempt.get("generation")) is not int
            or payload["attempt_generation"] < 0
            or payload["attempt_generation"] != attempt.get("generation")
            or payload.get("attempt_id") != attempt.get("attempt_id")
            or payload.get("profile") != record.get("profile")
            or payload.get("status") != record["terminal"].get("status")):
        raise RegistryError(f"invalid terminal identity for {delegation_id}")
    result = record["terminal"].get("result")
    if result is not None and not isinstance(result, dict):
        raise RegistryError(f"invalid terminal result for {delegation_id}")
    return {**entry, "execution_result": result}


def enqueue_pending_outbox(
    *,
    current_boot_id: str,
    profile_home: Path | None = None,
) -> list[dict[str, Any]]:
    queued: list[dict[str, Any]] = []
    now = time.time()
    # The gateway replaying this home's outbox also runs its admitted sweep, which owns expiry of
    # ``admitted`` rows (transcript first, then the age ceiling). Mark it served BEFORE any write can
    # prune, so a restart after a long outage never drops a consumed wake unchecked (2fcb0b3d9b47).
    note_sweeper_serves(profile_home)
    with locked_registry(profile_home) as registry:
        invalid_record_ids = set(registry.get("_invalid_record_ids", []))
        for delegation_id, record in registry["records"].items():
            if str(delegation_id) in invalid_record_ids:
                continue
            if not isinstance(record, dict):
                continue
            try:
                canonical_terminal(registry, delegation_id)
            except Exception:
                logger.warning("Could not replay delegation %s; invalid canonical evidence",
                               delegation_id, exc_info=True)
                continue
            generation = int((record.get("attempt") or {}).get("generation") or 0)
            for event in record.get("outbox", []):
                if not isinstance(event, dict) or event.get("state") != "pending":
                    continue
                payload = event.get("payload") or {}
                if event.get("type") == "async_delegation_restarted" and int(payload.get("attempt_generation") or -1) < generation:
                    event["state"] = "dropped"
                    event["drop_reason"] = "superseded"
                    event["delivered_at"] = time.time()
                    continue
                if event.get("queued_boot_id") == current_boot_id:
                    continue
                # JSON outbox events need not have a legacy SQLite row. Apply
                # the replay horizon here, before any consumer can inject them.
                # Age the event, not the dispatch: a long-running task may have
                # completed just now. Missing timestamps are not proof of age.
                created_at = event.get("created_at")
                if isinstance(created_at, (int, float)) and now - created_at > MAX_COMPLETION_REPLAY_AGE_SECONDS:
                    event["state"] = "dropped"
                    event["drop_reason"] = "stale_completion"
                    event["delivered_at"] = now
                    logger.warning(
                        "async_delegation_outbox_dropped event_id=%s reason=stale_completion age_hours=%.1f",
                        event.get("event_id"), (now - created_at) / 3600.0,
                    )
                    continue
                event["queued_boot_id"] = current_boot_id
                queued.append(copy.deepcopy(payload))
                logger.info(
                    "async_delegation_outbox_queued delegation_id=%s event_id=%s current_boot_id=%s",
                    delegation_id,
                    event.get("event_id"),
                    current_boot_id,
                )
    return queued


def admit(
    event_id: str,
    *,
    boot_id: str = "",
    session_key: str = "",
    session_id: str = "",
    profile_home: Path | None = None,
    admitted_at: float | None = None,
) -> bool:
    """Record ADAPTER ADMISSION of a gateway wake: queued or steered, not yet consumed (t_97a879af).

    ``admitted`` is not ``delivered``: an admitted wake can still be lost in memory (depth cap,
    eviction, /stop, crash before the drain). The consumption sweeper confirms it ``delivered`` once
    the parent transcript carries it, or re-opens it ``pending`` for re-injection. Only a pending
    event moves here, so a late admission never downgrades a settled receipt; an already admitted
    one answers True (idempotent for a retried receipt).
    """
    with locked_registry(profile_home) as registry:
        invalid_record_ids = set(registry.get("_invalid_record_ids", []))
        for delegation_id, record in registry["records"].items():
            if str(delegation_id) in invalid_record_ids or not isinstance(record, dict):
                continue
            for event in record.get("outbox", []):
                if isinstance(event, dict) and event.get("event_id") == event_id:
                    if event.get("state") == "admitted":
                        return True  # a retried receipt: already held
                    if event.get("state") != "pending":
                        return False
                    event.update(state="admitted",
                                 admitted_at=admitted_at if isinstance(admitted_at, (int, float)) else time.time(), admitted_boot_id=boot_id,
                                 admitted_session_key=session_key, admitted_session_id=session_id)
                    logger.info(
                        "async_delegation_outbox_admitted event_id=%s session_key=%s session_id=%s",
                        event_id, session_key, session_id or "-",
                    )
                    return True
    return False


def admitted_events(profile_home: Path | None = None) -> list[dict[str, Any]]:
    """Copies of every ``admitted`` outbox event (the consumption sweeper's work list)."""
    out: list[dict[str, Any]] = []
    with locked_registry(profile_home, write=False) as registry:
        invalid_record_ids = set(registry.get("_invalid_record_ids", []))
        for delegation_id, record in registry["records"].items():
            if str(delegation_id) in invalid_record_ids or not isinstance(record, dict):
                continue
            for event in record.get("outbox", []):
                if isinstance(event, dict) and event.get("state") == "admitted":
                    out.append({**copy.deepcopy(event), "delegation_id": str(delegation_id)})
    return out


def reopen_admitted(event_id: str, *, reason: str, profile_home: Path | None = None,
                    before_publish: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any] | None:
    """``admitted`` -> ``pending`` for one re-injection; returns the payload, else None (settled).

    ``before_publish(payload)`` runs under the registry lock before ``pending`` is written (the legacy
    SQLite mirror reset). If it raises, the event stays ``admitted`` and uncounted, so a crash or failure
    between the two writes can never leave JSON ``pending`` beside a ``delivered`` mirror (Prism
    9057f657dd27)."""
    with locked_registry(profile_home) as registry:
        invalid_record_ids = set(registry.get("_invalid_record_ids", []))
        for delegation_id, record in registry["records"].items():
            if str(delegation_id) in invalid_record_ids or not isinstance(record, dict):
                continue
            for event in record.get("outbox", []):
                if isinstance(event, dict) and event.get("event_id") == event_id:
                    if event.get("state") != "admitted":
                        return None
                    payload = copy.deepcopy(event.get("payload") or {})
                    if before_publish is not None:
                        before_publish(payload)
                    event["state"] = "pending"
                    event["reinjects"] = int(event.get("reinjects") or 0) + 1
                    logger.warning(
                        "async_delegation_outbox_reopened event_id=%s reason=%s reinjects=%d",
                        event_id, reason, event["reinjects"],
                    )
                    return payload
    return None


def acknowledge(
    event_id: str,
    *,
    outcome: str,
    reason: str | None = None,
    profile_home: Path | None = None,
    admission: dict[str, Any] | None = None,
) -> bool:
    if outcome == "admitted":
        return admit(event_id, profile_home=profile_home, **(admission or {}))
    if outcome not in {"delivered", "dropped"}:
        raise ValueError("outcome must be delivered, dropped or admitted")
    # Resolve ONCE: the lock, the pre-compaction backup and the persist must all hit the same file
    # even if a relative profile home's cwd changes mid-transaction (Prism f-0eee9075234c750b).
    path = registry_path(profile_home)
    with locked_registry(path=path) as registry:
        invalid_record_ids = set(registry.get("_invalid_record_ids", []))
        for delegation_id, record in registry["records"].items():
            if str(delegation_id) in invalid_record_ids:
                continue
            if not isinstance(record, dict):
                continue
            for event in record.get("outbox", []):
                if isinstance(event, dict) and event.get("event_id") == event_id:
                    event["state"] = outcome
                    event["delivered_at"] = time.time()
                    event["drop_reason"] = reason if outcome == "dropped" else None
                    # D5: a delivered event's body is never read again (identity stays).
                    # Never without a complete pre-compaction backup on disk first.
                    if (_compact_delivered_event(record, event, dry_run=True)
                            and _backup_before_first_compaction(path)):
                        _compact_delivered_event(record, event)
                    logger.info(
                        "async_delegation_outbox_%s event_id=%s reason=%s",
                        outcome,
                        event_id,
                        reason,
                    )
                    return True
    return False
