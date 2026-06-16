from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Mapping, Protocol, runtime_checkable

DEFAULT_COMPRESSOR_TIMEOUT_MS = 50


@dataclass(frozen=True)
class CompressedView:
    view_text: str
    view_bytes: int
    lossy_view: bool = True
    recoverable: bool = True
    strategy_name: str = ""


@runtime_checkable
class Compressor(Protocol):
    """Pure deterministic compressor interface.

    Implementations must not import or call models, provider clients, or network
    services. They receive only the raw text plus explicit params and return the
    view that will be placed inside the existing artifact marker.
    """

    def compress(self, raw: str, *, params: Mapping[str, object]) -> CompressedView | None:
        ...


def run_with_timeout_guard(
    compressor: Compressor,
    raw: str,
    *,
    params: Mapping[str, object] | None = None,
    timeout_ms: int = DEFAULT_COMPRESSOR_TIMEOUT_MS,
) -> CompressedView | None:
    """Run one compressor behind the hard wall-clock fail-open guard.

    Returns ``None`` on timeout, exception, or a non-``CompressedView`` result so
    callers can fall back to the shipped deterministic preview path.
    """

    result_queue: queue.Queue[CompressedView | BaseException | None] = queue.Queue(maxsize=1)
    call_params = dict(params or {})

    def _target() -> None:
        try:
            result = compressor.compress(raw, params=call_params)
        except BaseException as exc:  # fail-open; caller returns lossless preview
            _offer_result(result_queue, exc)
            return
        if isinstance(result, CompressedView):
            _offer_result(result_queue, result)
            return
        _offer_result(result_queue, None)

    worker = threading.Thread(
        target=_target,
        name="native-content-slimmer-compressor",
        daemon=True,
    )
    worker.start()
    worker.join(max(0, int(timeout_ms or 0)) / 1000)
    if worker.is_alive():
        return None
    try:
        result = result_queue.get_nowait()
    except queue.Empty:
        return None
    if isinstance(result, BaseException):
        return None
    return result


def _offer_result(
    result_queue: queue.Queue[CompressedView | BaseException | None],
    value: CompressedView | BaseException | None,
) -> None:
    try:
        result_queue.put_nowait(value)
    except queue.Full:  # pragma: no cover - only possible after timeout race
        pass
