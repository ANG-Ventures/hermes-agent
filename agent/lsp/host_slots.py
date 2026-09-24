"""Per-OS-user cap on running language servers (``lsp.max_servers_per_host``).

Every agent process owns its own :class:`agent.lsp.manager.LSPService`, so a box running 30 kanban
workers would otherwise run 30 pyright processes (~200-950 MB each).  A slot is an exclusive
``flock`` on ``<gateway lock dir>/lsp-slots/slot-<n>.lock`` held for the life of one server process;
the kernel drops it when the holder dies, so a crashed worker can never leak a slot.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import IO, Optional

try:
    import fcntl
except ImportError:  # Windows: msvcrt byte-range lock on the first byte instead
    fcntl = None  # type: ignore[assignment]
    import msvcrt


def slots_dir() -> Path:
    from gateway.status import _get_lock_dir
    return _get_lock_dir() / "lsp-slots"


def _try_lock(handle: IO[bytes]) -> bool:
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        return True
    except OSError:
        return False


class HostSlot:
    """One held slot; :meth:`release` is idempotent."""

    def __init__(self, handle: Optional[IO[bytes]]) -> None:
        self._handle = handle

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            handle.close()  # closing the fd drops the lock


def acquire(cap: int) -> Optional[HostSlot]:
    """A held slot, or ``None`` when all ``cap`` slots are taken.  ``cap <= 0`` = unlimited."""
    if cap <= 0:
        return HostSlot(None)
    directory = slots_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for n in range(cap):
        handle = open(directory / f"slot-{n}.lock", "a+b")
        if _try_lock(handle):
            return HostSlot(handle)
        handle.close()
    return None


def held_count(cap: int) -> int:
    """How many of the first ``cap`` slots are held right now (status display / tests)."""
    directory = slots_dir()
    held = 0
    for n in range(max(cap, 0)):
        path = directory / f"slot-{n}.lock"
        if not path.exists():
            continue
        with open(path, "a+b") as handle:
            if not _try_lock(handle):
                held += 1
    return held


__all__ = ["HostSlot", "acquire", "held_count", "slots_dir"]
