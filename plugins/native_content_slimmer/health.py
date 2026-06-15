"""Health probe for the native-content-slimmer artifact store."""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from plugins.native_content_slimmer.gc import artifact_usage, gc_status_path
from plugins.native_content_slimmer.store import default_artifact_root


def check_artifact_store_health(
    root: str | Path | None = None,
    *,
    max_bytes: int | None = None,
    active_session_id: str | None = None,
) -> dict[str, Any]:
    """Return a structured health status for the filesystem artifact store."""

    root_path = Path(root) if root is not None else default_artifact_root()
    writable, write_error = _probe_writable(root_path)
    usage = artifact_usage(root_path, active_session_id=active_session_id)
    profile_bytes = usage["profile_bytes"]
    cap_usage_ratio = None
    over_cap = False
    if max_bytes is not None and int(max_bytes) > 0:
        cap_usage_ratio = profile_bytes / int(max_bytes)
        over_cap = profile_bytes > int(max_bytes)

    last_gc = _load_last_gc(root_path)
    free_bytes = _disk_free_bytes(root_path)

    if not writable:
        status = "error"
    elif over_cap or last_gc.get("last_gc_error"):
        status = "degraded"
    else:
        status = "ok"

    result: dict[str, Any] = {
        "ok": status == "ok",
        "status": status,
        "root": str(root_path),
        "writable": writable,
        "free_bytes": free_bytes,
        "max_bytes": int(max_bytes) if max_bytes is not None else None,
        "profile_bytes": profile_bytes,
        "artifact_count": usage["artifact_count"],
        "active_session_id": active_session_id,
        "active_session_bytes": usage["active_session_bytes"],
        "ended_session_bytes": usage["ended_session_bytes"],
        "cap_usage_ratio": cap_usage_ratio,
        "over_cap": over_cap,
        "last_gc_time": last_gc.get("last_gc_time"),
        "last_gc_error": last_gc.get("last_gc_error"),
    }
    if write_error:
        result["error"] = write_error
    return result


def _probe_writable(root: Path) -> tuple[bool, str | None]:
    tmp_path = root / f".health.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    try:
        root.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, b"ok")
            os.fsync(fd)
        finally:
            os.close(fd)
        tmp_path.unlink()
        return True, None
    except Exception as exc:
        try:
            tmp_path.unlink()
        except Exception:
            pass
        return False, str(exc)


def _disk_free_bytes(root: Path) -> int | None:
    probe = root
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError:
        return None
    return int(usage.free)


def _load_last_gc(root: Path) -> dict[str, Any]:
    path = gc_status_path(root)
    if not path.exists():
        return {"last_gc_time": None, "last_gc_error": None}
    try:
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"last_gc_time": None, "last_gc_error": "invalid_gc_status"}
        return {
            "last_gc_time": data.get("completed_at"),
            "last_gc_error": data.get("last_error"),
        }
    except Exception as exc:
        return {"last_gc_time": None, "last_gc_error": str(exc)}


artifact_store_health = check_artifact_store_health
health_status = check_artifact_store_health
