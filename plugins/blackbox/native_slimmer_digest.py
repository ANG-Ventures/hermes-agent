"""Native-slimmer savings digest renderer + runner (PRD #1.5 Phase 4, build-new).

Phase 0 (d) established PRD #1's `compression-savings-digest` cron does NOT exist
on disk, so this is the build-new digest for the native-slimmer compressor. It
reads the day's persisted savings rows, dollarizes per-row (Phase 3), and renders
an honest line:

  - shadow "would have saved" and active "saved" as DISTINCT figures (never summed)
  - tokens (exact-ish) + dollars as a per-submission LOWER BOUND (banner), or
    "$0 subscription" when included, or "—" when unpriced
  - coarse dollar rounding (no cents-precision on a bytes/4-derived figure)
  - zero-savings day → "no native-slimmer savings recorded" (not a crash, not fake 0)

Run read-only:  python -m plugins.blackbox.native_slimmer_digest [--days 1] [--dry-run]
"""

from __future__ import annotations

import argparse
import time
from typing import Any

from plugins.blackbox import native_slimmer_store as nss
from plugins.blackbox.native_slimmer_dollarize import dollarize_rollup

BANNER = (
    "savings = bytes/4 token estimate, priced once at uncached input rate "
    "(per-submission lower bound; estimate, not billed)"
)


def _fmt_usd(amount: float | None, status: str) -> str:
    if amount is None:
        return "—"
    if status == "included" and amount == 0.0:
        return "$0 (subscription)"
    # coarse band — no cents precision on a bytes/4-derived figure
    if amount < 0.01:
        return "<$0.01"
    if amount < 1:
        return f"~${amount:.2f}"
    if amount < 100:
        return f"~${amount:.1f}"
    return f"~${round(amount):d}"


def _fmt_tokens(tok: int) -> str:
    if tok >= 1_000_000:
        return f"{tok/1_000_000:.1f}M"
    if tok >= 1_000:
        return f"{tok/1_000:.0f}k"
    return str(tok)


def render_digest(rollup: dict[str, Any]) -> str:
    """Render the honest native-slimmer digest line from a dollarized rollup."""

    saved = rollup["saved"]
    would = rollup["would_save"]
    if saved["event_count"] == 0 and would["event_count"] == 0:
        return "native-slimmer: no native-slimmer savings recorded"

    lines = ["native-slimmer savings (today):"]
    if saved["event_count"]:
        lines.append(
            "  • ACTIVE saved "
            f"{_fmt_tokens(saved['saved_vs_status_quo_tokens_est'])} tok "
            f"({_fmt_usd(saved['saved_usd_vs_status_quo'], saved['price_status'])})"
            f" across {saved['event_count']} result(s)"
            + (f" [+{saved['unpriced_count']} unpriced]" if saved.get("unpriced_count") else "")
        )
    if would["event_count"]:
        lines.append(
            "  • SHADOW would have saved "
            f"{_fmt_tokens(would['saved_vs_status_quo_tokens_est'])} tok "
            f"({_fmt_usd(would['saved_usd_vs_status_quo'], would['price_status'])})"
            f" across {would['event_count']} result(s)"
            + (f" [+{would['unpriced_count']} unpriced]" if would.get("unpriced_count") else "")
        )
    lines.append(f"  ({BANNER})")
    return "\n".join(lines)


def build_digest(days: int = 1, *, now: float | None = None) -> str:
    """Read the last `days` of savings rows and render the digest (read-only)."""

    end = float(now if now is not None else time.time())
    start = end - max(1, int(days)) * 86400
    rows = nss.fetch_between(start, end)
    rollup = dollarize_rollup(rows)
    return render_digest(rollup)


def main() -> int:
    ap = argparse.ArgumentParser(description="native-slimmer savings digest (read-only)")
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true", help="print only; never deliver")
    args = ap.parse_args()
    line = build_digest(args.days)
    print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
