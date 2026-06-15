"""Garbage collection for native-content-slimmer artifacts."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from plugins.native_content_slimmer.store import (
    ArtifactIntegrityError,
    ArtifactStore,
    atomic_write_json_file,
    default_artifact_root,
    parse_utc_iso,
    utc_now_iso,
)


@dataclass(frozen=True)
class ArtifactCandidate:
    artifact_id: str
    session_id: str
    path: Path
    size: int
    created_at: datetime
    last_expanded_at: datetime


def gc_status_path(root: str | Path | None = None) -> Path:
    base = Path(root) if root is not None else default_artifact_root()
    return base / ".gc_status.json"


def artifact_usage(
    root: str | Path | None = None,
    *,
    active_session_id: str | None = None,
) -> dict[str, int]:
    """Return artifact byte totals, excluding tombstones and status files."""

    candidates = _load_candidates(Path(root) if root is not None else default_artifact_root())
    profile_bytes = sum(candidate.size for candidate in candidates)
    active_bytes = sum(
        candidate.size for candidate in candidates if active_session_id and candidate.session_id == active_session_id
    )
    return {
        "profile_bytes": profile_bytes,
        "active_session_bytes": active_bytes,
        "ended_session_bytes": profile_bytes - active_bytes,
        "artifact_count": len(candidates),
    }


def collect_garbage(
    root: str | Path | None = None,
    *,
    ttl_seconds: int | float | None = None,
    ttl_days: int | float | None = None,
    max_bytes: int | None = None,
    active_session_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run TTL and size-cap artifact GC.

    Expired artifacts are removed first. Size-cap eviction then removes the
    least-recently-expanded inactive artifacts and never evicts artifacts from
    ``active_session_id`` on the cap path. If the active session alone exceeds
    the cap, ``over_cap`` is reported and active artifacts remain intact.
    """

    started = utc_now_iso()
    started_monotonic = time.monotonic()
    root_path = Path(root) if root is not None else default_artifact_root()
    store = ArtifactStore(root_path)
    now_dt = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    ttl_delta: timedelta | None = None
    if ttl_seconds is not None:
        ttl_delta = timedelta(seconds=float(ttl_seconds))
    elif ttl_days is not None:
        ttl_delta = timedelta(days=float(ttl_days))

    deleted: list[dict[str, Any]] = []
    last_error: str | None = None

    try:
        candidates = _load_candidates(root_path)
        bytes_before = sum(candidate.size for candidate in candidates)

        if ttl_delta is not None:
            expiry_cutoff = now_dt - ttl_delta
            for candidate in list(candidates):
                if candidate.created_at <= expiry_cutoff:
                    deleted.append(_delete_candidate(store, candidate, reason="ttl_expired", now=now_dt))
            if deleted:
                deleted_ids = {item["artifact_id"] for item in deleted}
                candidates = [c for c in _load_candidates(root_path) if c.artifact_id not in deleted_ids]

        total_after_ttl = sum(candidate.size for candidate in candidates)
        total = total_after_ttl
        if max_bytes is not None and total > int(max_bytes):
            inactive = [
                candidate
                for candidate in candidates
                if not active_session_id or candidate.session_id != active_session_id
            ]
            inactive.sort(key=lambda c: (c.last_expanded_at, c.created_at, c.artifact_id))
            for candidate in inactive:
                if total <= int(max_bytes):
                    break
                deleted_item = _delete_candidate(store, candidate, reason="size_cap", now=now_dt)
                deleted.append(deleted_item)
                total -= candidate.size

        remaining = _load_candidates(root_path)
        active_bytes = sum(
            candidate.size
            for candidate in remaining
            if active_session_id and candidate.session_id == active_session_id
        )
        bytes_after = sum(candidate.size for candidate in remaining)
        over_cap = bool(max_bytes is not None and bytes_after > int(max_bytes))
        completed = utc_now_iso()
        result = {
            "ok": last_error is None,
            "started_at": started,
            "completed_at": completed,
            "duration_ms": int((time.monotonic() - started_monotonic) * 1000),
            "deleted": deleted,
            "deleted_count": len(deleted),
            "bytes_deleted": sum(int(item.get("bytes", 0)) for item in deleted),
            "bytes_before": bytes_before,
            "bytes_after": bytes_after,
            "active_session_id": active_session_id,
            "active_session_bytes": active_bytes,
            "ended_session_bytes": bytes_after - active_bytes,
            "max_bytes": int(max_bytes) if max_bytes is not None else None,
            "ttl_seconds": int(ttl_delta.total_seconds()) if ttl_delta is not None else None,
            "over_cap": over_cap,
            "last_error": last_error,
        }
    except Exception as exc:
        last_error = str(exc)
        result = {
            "ok": False,
            "started_at": started,
            "completed_at": utc_now_iso(),
            "duration_ms": int((time.monotonic() - started_monotonic) * 1000),
            "deleted": deleted,
            "deleted_count": len(deleted),
            "bytes_deleted": sum(int(item.get("bytes", 0)) for item in deleted),
            "bytes_before": 0,
            "bytes_after": 0,
            "active_session_id": active_session_id,
            "active_session_bytes": 0,
            "ended_session_bytes": 0,
            "max_bytes": int(max_bytes) if max_bytes is not None else None,
            "ttl_seconds": int(ttl_delta.total_seconds()) if ttl_delta is not None else None,
            "over_cap": False,
            "last_error": last_error,
        }

    try:
        atomic_write_json_file(gc_status_path(root_path), result)
    except Exception:
        # The GC result itself is still useful to the caller; health will report
        # writability separately if the status file cannot be persisted.
        pass
    return result


def _load_candidates(root: Path) -> list[ArtifactCandidate]:
    store = ArtifactStore(root)
    candidates: list[ArtifactCandidate] = []
    for path in store.iter_artifact_paths():
        try:
            data = store.read_record(path.stem, session_id=path.parent.name)
        except ArtifactIntegrityError:
            # Corruption is loud on expansion/read. GC leaves corrupt files in
            # place rather than turning an integrity problem into silent loss.
            continue
        created = parse_utc_iso(data.get("created_at"))
        if created is None:
            created = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        last_expanded = parse_utc_iso(data.get("last_expanded_at")) or created
        candidates.append(
            ArtifactCandidate(
                artifact_id=str(data.get("artifact_id") or path.stem),
                session_id=str(data.get("session_id") or path.parent.name),
                path=path,
                size=path.stat().st_size,
                created_at=created,
                last_expanded_at=last_expanded,
            )
        )
    return candidates


def _delete_candidate(
    store: ArtifactStore,
    candidate: ArtifactCandidate,
    *,
    reason: str,
    now: datetime,
) -> dict[str, Any]:
    deleted_at = now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    metadata = {
        "raw_sha256": None,
        "bytes": candidate.size,
        "created_at": candidate.created_at.isoformat().replace("+00:00", "Z"),
    }
    try:
        data = store.read_record(candidate.artifact_id, session_id=candidate.session_id)
        metadata["raw_sha256"] = data.get("raw_sha256")
        metadata["tool_call_id"] = data.get("tool_call_id")
        metadata["tool_name"] = data.get("tool_name")
    except Exception:
        pass

    candidate.path.unlink()
    try:
        # Best effort directory durability for the unlink; tombstone write also
        # fsyncs its own rename below.
        from plugins.native_content_slimmer.store import _fsync_dir  # local private durability helper

        _fsync_dir(candidate.path.parent)
    except Exception:
        pass
    store.write_tombstone(
        artifact_id=candidate.artifact_id,
        session_id=candidate.session_id,
        reason=reason,
        deleted_at=deleted_at,
        metadata=metadata,
    )
    return {
        "artifact_id": candidate.artifact_id,
        "session_id": candidate.session_id,
        "bytes": candidate.size,
        "reason": reason,
        "deleted_at": deleted_at,
    }


# Backward-friendly aliases for callers/tests that use shorter names.
run_gc = collect_garbage
collect_artifact_garbage = collect_garbage
