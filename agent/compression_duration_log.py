"""One measurable log line per compression summariser call.

A compaction that takes minutes is the most user-visible stall the harness has, and
until now it was unattributable: the existing ``compression_attempt`` telemetry is
populated by ``ContextCompressor._record_aux_compression_call``, so an install running
a context-engine plugin (LCM calls ``auxiliary_client.call_llm(task="compression")``
directly) emitted an envelope with no provider, no model, and no auxiliary duration —
``{"commit_status":"committed","total_duration_ms":357904}`` and nothing to blame.

This module owns the *decision and the wording* for a duration line emitted at the
``call_llm`` choke point instead, which every context engine funnels through. Pure by
design: no logging, no config reads, no clock. The caller supplies measurements; this
decides whether they are worth a line and formats them. That keeps the whole policy
unit-testable without a summariser, a provider, or a gateway.
"""

from __future__ import annotations

from typing import Optional

# Only the compaction summariser gets a standing per-call duration line. Other auxiliary
# tasks (vision, title-gen, session_search) are not on the critical path a user waits on,
# and a line per call for all of them would be log spam rather than telemetry.
COMPRESSION_TASK = "compression"

#: Duration line prefix. Grep-stable: monitors key on this, so treat it as a contract.
DURATION_EVENT = "compression summariser call"


def should_log_duration(task: Optional[str]) -> bool:
    """Whether ``task`` earns a duration line.

    Compared case-insensitively on a stripped value so a caller passing ``"Compression"``
    or a padded task name is not silently dropped from telemetry.
    """
    return str(task or "").strip().lower() == COMPRESSION_TASK


def format_duration_line(
    *,
    provider: Optional[str],
    model: Optional[str],
    attempts: int,
    seconds: float,
    outcome: str,
    budget_seconds: Optional[float] = None,
) -> str:
    """Render the duration line.

    ``attempts`` is the number of physical provider requests this logical call made
    (same-provider retries and fallback rungs included), so ``attempts>1`` is the tell
    for the stall-then-retry shape that multiplies a user-visible compaction.

    ``budget_seconds`` is the timeout the call actually ran under. Reporting it next to
    the elapsed time is deliberate: a configured timeout can be silently overridden by a
    floor, and a duration alone cannot show that. ``seconds`` at or above the budget is a
    deadline kill, not a slow summary.
    """
    fields = [
        f"provider={provider or 'unknown'}",
        f"model={model or 'unknown'}",
        f"attempts={max(1, int(attempts))}",
        f"seconds={max(0.0, float(seconds)):.1f}",
        f"outcome={outcome or 'unknown'}",
    ]
    # Omitted rather than rendered as a fake 0/None when the caller could not determine
    # the budget — a wrong budget is worse than an absent one for a deadline judgement.
    if budget_seconds is not None and float(budget_seconds) > 0:
        fields.append(f"budget={float(budget_seconds):.0f}s")
    return f"{DURATION_EVENT}: " + " ".join(fields)
