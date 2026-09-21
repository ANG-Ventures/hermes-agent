"""Fallback walker must not try subs the usage registry knows are exhausted.

THE BUG (2026-09-21 01:18, #apollo): the primary sub hits its 5h/7d cap
mid-turn and the walker marches the WHOLE chain — claude-apx-1, -2, … -15 —
paying a full request round-trip and one "Rate limited — switching to fallback
provider..." status line for each, even though the locally-published usage
registry already recorded 14 of them as ``status: rejected`` with resets days
out. ~60s of wall clock and ten status lines to learn what was on disk.

The gate is registry-driven and conservative:

* a sub is INELIGIBLE only when a window is measurably exhausted AND its reset
  is more than the horizon away — a sub resetting in 90s is still a candidate;
* missing/unreadable/stale registry data is NOT evidence of exhaustion (fail
  open — the historical walk is preserved);
* the pruning happens in ONE pass so the user sees one "skipping N exhausted"
  line instead of N "switching…" lines.
"""

from __future__ import annotations

import json
import time

import pytest

from agent.quota_registry_gate import (
    QuotaState,
    SKIP_HORIZON_SECONDS,
    load_registry_snapshot,
    prune_exhausted_entries,
    provider_quota_state,
)


def _win(key, pct, status, resets_in):
    return {
        "key": key,
        "pct": pct,
        "status": status,
        "resets_at": time.time() + resets_in,
    }


def _snapshot(accounts):
    return {
        slug: {"windows": wins, "observed_at": time.time()}
        for slug, wins in accounts.items()
    }


# ── provider_quota_state truth table ────────────────────────────────────


class TestProviderQuotaState:
    def test_exhausted_7d_with_distant_reset_is_ineligible(self):
        snap = _snapshot({"claude-apx-1": [
            _win("five_hour", 0.0, "allowed", 3600),
            _win("seven_day", 100.0, "rejected", 3.4 * 86400),
        ]})
        state = provider_quota_state("claude-apx-1", snap)
        assert state.eligible is False
        assert state.window == "seven_day"
        assert state.reset_at == pytest.approx(time.time() + 3.4 * 86400, abs=5)

    def test_exhausted_5h_with_distant_reset_is_ineligible(self):
        snap = _snapshot({"claude-apx-11": [
            _win("five_hour", 101.0, "rejected", 1800),
            _win("seven_day", 93.0, "allowed_warning", 4 * 86400),
        ]})
        assert provider_quota_state("claude-apx-11", snap).eligible is False

    def test_exhausted_but_resetting_inside_the_horizon_stays_eligible(self):
        """A sub that comes back in 90s is worth the one request."""
        snap = _snapshot({"claude-apx-8": [
            _win("seven_day", 100.0, "rejected", 90),
        ]})
        state = provider_quota_state("claude-apx-8", snap)
        assert state.eligible is True

    def test_reset_exactly_at_the_horizon_is_still_eligible(self):
        snap = _snapshot({"x": [_win("seven_day", 100.0, "rejected",
                                     SKIP_HORIZON_SECONDS)]})
        assert provider_quota_state("x", snap).eligible is True

    def test_healthy_sub_is_eligible(self):
        snap = _snapshot({"claude-apx-17": [
            _win("five_hour", 5.0, "allowed", 3600),
            _win("seven_day", 17.0, "allowed", 5 * 86400),
        ]})
        assert provider_quota_state("claude-apx-17", snap).eligible is True

    # ── fail-open cases: absence of data is not evidence of exhaustion ──

    def test_unknown_provider_is_eligible(self):
        assert provider_quota_state("openai-codex", _snapshot({})).eligible is True

    def test_empty_snapshot_is_eligible(self):
        assert provider_quota_state("claude-apx-1", {}).eligible is True

    def test_none_snapshot_is_eligible(self):
        assert provider_quota_state("claude-apx-1", None).eligible is True

    def test_rejected_without_a_reset_time_is_not_actionable(self):
        """We can only skip when we know WHEN it comes back."""
        snap = {"claude-apx-3": {"windows": [
            {"key": "seven_day", "pct": 100.0, "status": "rejected"},
        ]}}
        assert provider_quota_state("claude-apx-3", snap).eligible is True

    def test_malformed_reset_value_is_not_actionable(self):
        snap = {"claude-apx-3": {"windows": [
            {"key": "seven_day", "pct": 100.0, "status": "rejected",
             "resets_at": "not-a-date"},
        ]}}
        assert provider_quota_state("claude-apx-3", snap).eligible is True

    def test_scoped_model_window_does_not_gate_the_whole_sub(self):
        """A Fable allowance rejection is NOT a subscription-wide cap."""
        snap = {"claude-apx-6": {"windows": [
            _win("five_hour", 0.0, "allowed", 3600),
            _win("seven_day", 40.0, "allowed", 4 * 86400),
            dict(_win("seven_day_overage_included", 100.0, "rejected",
                      4 * 86400), scoped=True, model="Fable"),
        ]}}
        assert provider_quota_state("claude-apx-6", snap).eligible is True

    def test_stale_observation_is_not_trusted(self):
        snap = {"claude-apx-1": {
            "observed_at": time.time() - 48 * 3600,
            "windows": [_win("seven_day", 100.0, "rejected", 3 * 86400)],
        }}
        assert provider_quota_state("claude-apx-1", snap).eligible is True


# ── one-pass chain pruning ──────────────────────────────────────────────


class TestPruneExhaustedEntries:
    def _chain(self, *slugs):
        return [{"provider": s, "model": "claude-opus-5"} for s in slugs]

    def test_prunes_the_exhausted_run_and_reports_the_count(self):
        snap = _snapshot({
            "claude-apx-1": [_win("seven_day", 100.0, "rejected", 3 * 86400)],
            "claude-apx-2": [_win("seven_day", 100.0, "rejected", 2 * 86400)],
            "claude-apx-17": [_win("seven_day", 17.0, "allowed", 5 * 86400)],
        })
        chain = self._chain("claude-apx-1", "claude-apx-2", "claude-apx-17")
        result = prune_exhausted_entries(chain, snapshot=snap)
        assert [e["provider"] for e in result.eligible] == ["claude-apx-17"]
        assert result.skipped_count == 2
        assert "skipping 2 exhausted" in result.summary_line

    def test_summary_line_is_singular_for_one_skip(self):
        snap = _snapshot({"a": [_win("five_hour", 100.0, "rejected", 86400)]})
        result = prune_exhausted_entries(self._chain("a", "b"), snapshot=snap)
        assert "skipping 1 exhausted" in result.summary_line

    def test_no_skips_produces_no_summary_line(self):
        result = prune_exhausted_entries(self._chain("a", "b"), snapshot={})
        assert result.skipped_count == 0
        assert result.summary_line is None
        assert len(result.eligible) == 2

    def test_all_exhausted_reports_the_soonest_reset(self):
        """Fail fast, and say when the first sub comes back."""
        snap = _snapshot({
            "claude-apx-1": [_win("seven_day", 100.0, "rejected", 3 * 86400)],
            "claude-apx-2": [_win("seven_day", 100.0, "rejected", 6 * 3600)],
        })
        result = prune_exhausted_entries(
            self._chain("claude-apx-1", "claude-apx-2"), snapshot=snap
        )
        assert result.eligible == []
        assert result.soonest_reset_at == pytest.approx(time.time() + 6 * 3600, abs=5)
        assert "6h" in result.soonest_reset_text

    def test_soonest_reset_is_none_when_something_is_eligible(self):
        snap = _snapshot({"a": [_win("seven_day", 100.0, "rejected", 86400)]})
        result = prune_exhausted_entries(self._chain("a", "b"), snapshot=snap)
        assert result.soonest_reset_at is None

    def test_entry_order_is_preserved(self):
        snap = _snapshot({"b": [_win("five_hour", 100.0, "rejected", 86400)]})
        chain = self._chain("a", "b", "c", "d")
        result = prune_exhausted_entries(chain, snapshot=snap)
        assert [e["provider"] for e in result.eligible] == ["a", "c", "d"]

    def test_empty_chain_is_handled(self):
        result = prune_exhausted_entries([], snapshot={})
        assert result.eligible == []
        assert result.skipped_count == 0

    def test_a_malformed_entry_is_never_pruned_by_the_gate(self):
        """Invalid entries keep their existing downstream skip path."""
        result = prune_exhausted_entries([{"model": "x"}], snapshot={})
        assert result.skipped_count == 0
        assert len(result.eligible) == 1


# ── registry loading ────────────────────────────────────────────────────


class TestLoadRegistrySnapshot:
    def test_reads_the_published_portal_payload(self, tmp_path):
        payload = {
            "schema": 6,
            "providers": [
                {"id": "claude", "accounts": [
                    {"provider_slug": "claude-apx-1", "observed_at": time.time(),
                     "windows": [_win("seven_day", 100.0, "rejected", 86400)]},
                    {"provider_slug": "claude-apx-17", "observed_at": time.time(),
                     "windows": [_win("seven_day", 17.0, "allowed", 86400)]},
                ]},
                {"id": "openai", "accounts": [
                    {"label": "kyzcreig", "windows": []},
                ]},
            ],
        }
        p = tmp_path / "usage.json"
        p.write_text(json.dumps(payload))
        snap = load_registry_snapshot(p)
        assert set(snap) == {"claude-apx-1", "claude-apx-17"}
        assert provider_quota_state("claude-apx-1", snap).eligible is False
        assert provider_quota_state("claude-apx-17", snap).eligible is True

    def test_missing_file_returns_empty_not_an_error(self, tmp_path):
        assert load_registry_snapshot(tmp_path / "nope.json") == {}

    def test_corrupt_file_returns_empty_not_an_error(self, tmp_path):
        p = tmp_path / "usage.json"
        p.write_text("{not json")
        assert load_registry_snapshot(p) == {}

    def test_accounts_without_a_provider_slug_are_skipped(self, tmp_path):
        p = tmp_path / "usage.json"
        p.write_text(json.dumps({"providers": [
            {"id": "claude", "accounts": [{"key": "sub-vps-1", "windows": []}]},
        ]}))
        assert load_registry_snapshot(p) == {}


class TestQuotaStateShape:
    def test_eligible_state_carries_no_window(self):
        state = QuotaState(eligible=True)
        assert state.window is None and state.reset_at is None
