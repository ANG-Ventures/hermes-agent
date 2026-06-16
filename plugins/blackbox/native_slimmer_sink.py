"""Blackbox persistence sink for native-content-slimmer telemetry (PRD #1.5).

Wiring: ``register()`` injects an instance as the slimmer hook's ``telemetry``.
The hook calls ``sink.emit(event)`` **synchronously inside** its rollback-protected
``try`` (Phase 0 (f)) — so if ``emit`` raises, the certified machinery discards the
ledger entry, deletes the un-telemetried artifact, and returns the ORIGINAL
uncompressed result. The marker is only returned if ``emit`` succeeded.

Design consequence: this sink does NOT swallow its own write error. Raising on a
real persistence failure is the contract — it triggers rollback-to-original, so a
marker is never emitted without a persisted savings row. (Sink *construction* is
fail-open — see ``build_sink`` — but a write failure must propagate.)

``model``/``provider``/``base_url`` are not in the hook payload (Phase 0 (g)); the
sink resolves them once from the agent config at construction and stamps each row,
so the digest can dollarize per-row at the model the session actually ran.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)


def _resolve_model_provider() -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Best-effort: read the active (model, provider, base_url) from config.

    Mirrors how the rest of the agent resolves the running model. Returns
    ``(None, None, None)`` on any failure — those rows dollarize to ``unknown``
    ("—"); the token saving still counts.
    """

    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        model_block = cfg.get("model")
        provider = cfg.get("provider")
        base_url = cfg.get("base_url") or cfg.get("api_base")
        model: Optional[str] = None
        if isinstance(model_block, Mapping):
            model = model_block.get("default") or model_block.get("model")
            provider = provider or model_block.get("provider")
        elif isinstance(model_block, str) and model_block:
            model = model_block
        return (
            str(model) if model else None,
            str(provider) if provider else None,
            str(base_url) if base_url else None,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("native slimmer sink could not resolve model: %s", exc)
        return (None, None, None)


class BlackboxNativeSlimmerSink:
    """Synchronous ``emit(event)`` sink that UPSERTs savings rows into turns.db.

    A write failure PROPAGATES (does not swallow) — required by the hook's
    rollback-on-emit-failure contract.
    """

    def __init__(
        self,
        *,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        base_url: Optional[str] = None,
        clock=time.time,
    ) -> None:
        if model is None and provider is None and base_url is None:
            model, provider, base_url = _resolve_model_provider()
        self.model = model
        self.provider = provider
        self.base_url = base_url
        self._clock = clock
        # in-memory mirror so existing tests / fail-open callers that read
        # ``records`` keep working (parity with NativeSlimmerTelemetryBuffer).
        self.records: list[dict[str, Any]] = []

    def emit(self, event: Mapping[str, Any]) -> None:
        from plugins.blackbox import native_slimmer_store as nss

        nss.insert_event(
            event,
            model=self.model,
            provider=self.provider,
            base_url=self.base_url,
            created_at=float(self._clock()),
        )
        self.records.append(dict(event))


def build_sink(**kwargs: Any):
    """Construct the Blackbox sink, FAILING OPEN to the in-memory buffer.

    Sink *construction* must never break plugin registration. If the persistent
    sink can't be built (import error, etc.), fall back to the buffer + log.
    Note: this only guards CONSTRUCTION — once built, ``emit`` write failures
    propagate per the rollback contract.
    """

    try:
        return BlackboxNativeSlimmerSink(**kwargs)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("native slimmer Blackbox sink unavailable, using buffer: %s", exc)
        from plugins.native_content_slimmer.telemetry import NativeSlimmerTelemetryBuffer

        return NativeSlimmerTelemetryBuffer()
