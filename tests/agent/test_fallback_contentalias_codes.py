"""t_40a9186f: a ``contentalias:*`` 400 rendered
``unclassified error (Anthropic 400) on sub-vps-3``.

``contentalias:<code>`` is OUR DPX content-alias layer (dpx-content-alias
internal/contentalias), never Anthropic, whatever hop the relay stamps
(``x-relay-error-hop: bridge->upstream``). Every code is named through ONE table
(``fallback_capability.CONTENTALIAS_ERROR_CODES``); an unlisted code takes the
generic content-alias row and never falls through to "Anthropic"/"unclassified".
"""

from __future__ import annotations

import pytest

from agent import fallback_policy as fp
from agent.fallback_capability import CONTENTALIAS_ERROR_CODES, bridge_error_code
from tests.agent.test_fallback_bridge_codes import UTC, _bridge_err, _row_for

# Codes seen in state/fallback-unclassified.jsonl and on prior cards, plus one
# the table does not list (the generic row).
SEEN = ("unknown_tool", "upstream_failure", "schema_changed", "history_tool",
        "stream_json", "ambiguous_schema", "native_route_required", "store_lock")
HDRS = {"x-relay-error-class": "upstream_passthrough",
        "x-relay-error-hop": "bridge->upstream", "x-relay-seat": "sub-vps-3"}


def _msg(code):
    return ("the interactive session reported an upstream error row for this turn "
            f"(API Error: 400 contentalias:{code})")


@pytest.mark.parametrize("wire", ["direct", "tui_upstream_error"])
@pytest.mark.parametrize("code", SEEN)
def test_every_contentalias_code_is_ours_never_anthropic(code, wire):
    full = f"contentalias:{code}"
    if wire == "direct":
        err, status = _bridge_err(full, 400, _msg(code), HDRS), 400
    else:  # the bridge's 502 wrapper names the aliaser code in its message
        err, status = _bridge_err("tui_upstream_error", 502, _msg(code), HDRS), 502
    row = _row_for(err, status)
    assert row["bridge_code"] == full
    assert row["trigger_class"] != "unclassified"
    assert row["hop"] == "bridge→dpx"
    text, floors = fp.cause_rider_with_floors(row, tz=UTC)
    assert f"{status} {full} at the DPX alias layer" in text, text
    assert "on sub-vps-3" in text, text
    assert "Anthropic" not in text.replace("ours, not Anthropic", ""), text
    assert "unclassified" not in text and not floors, (text, floors)
    head = fp.head_label_override(row)
    assert head and "Anthropic" not in head and head != "bad request", head


def test_live_050118_unknown_tool_line():
    row = _row_for(_bridge_err("contentalias:unknown_tool", 400, _msg("unknown_tool"), HDRS), 400)
    assert fp.head_label_override(row) == "content-alias refused"
    text = fp.format_cause_rider(row, tz=UTC)
    assert ("400 contentalias:unknown_tool at the DPX alias layer (ours, not Anthropic) "
            "on sub-vps-3") in text, text


def test_bridge_error_code_reads_the_wrapped_contentalias_code():
    body = {"error": {"code": "tui_upstream_error", "message": _msg("schema_changed")}}
    assert bridge_error_code(body) == "contentalias:schema_changed"
    # Positive control: a wrapper with no aliaser code stays the bridge code.
    assert bridge_error_code({"error": {"code": "tui_upstream_error", "message": "x"}}) \
        == "tui_upstream_error"


def test_table_covers_the_seen_codes():
    assert set(SEEN) - {"store_lock"} <= set(CONTENTALIAS_ERROR_CODES)
