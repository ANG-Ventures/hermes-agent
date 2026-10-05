"""§4.8 in-chat fallback/return notice contract (same-family fallback spec rev12).

Covers the pure rider layer: the property test (every class x hop x seat
known/unknown x attempts 1/3 carries all four fields), rider/row hop
correctness, the pinned spec examples, the head-label override, and the
recovery rider fields. The AST guard over ``_emit_fallback_announce`` lands
with the announce wiring (it needs ``format_cause_rider`` to be called there).
"""

from __future__ import annotations

import datetime as dt
import itertools
import re

import pytest

from agent import fallback_policy as fp

UTC = dt.timezone.utc


def _ts(h, m, s):
    return dt.datetime(2026, 9, 25, h, m, s, tzinfo=UTC).timestamp()


_CAUSE_TEXT = {
    "conn": "Connection reset by peer",
    "pool_pressure": "pool at capacity",
    "quota_model": "no eligible sub for the requested model",
    "quota_seat": "Claude Fable limit reached",
    "rate_upstream": "exceed your account's rate limit",
    "refusal": "content_policy_blocked",
    "auth": "OAuth access token has been revoked",
    "provider_invalid_response": None,
    "lane_incapable": "interactive mode on this box serves tool-less turns only; tools[] must be empty (52 tools)",
    "unclassified": "weird thing",
}
_CAUSE_RE = re.compile(r"[a-zA-Z]{3,}")
_HOP_RE = re.compile(r"(to the relay|at the relay|bridge|\(Anthropic [^)]+\)|proxy|CLI|hop unknown|hop=relay-200)")
_SUB_RE = re.compile(r"(sub-vps-\d+|claude-[abc]px-\d+|all subs|sub unknown|sub=unknown)")
_TIME_RE = re.compile(r"\d\d:\d\d:\d\d(-\d\d(:\d\d:\d\d)?)?$")
_COUNT_RE = re.compile(r"^\d+x ")


def _row(cls, hop, seat, attempts, provider="claude-bpr"):
    return {
        "trigger_class": cls, "hop": hop, "seat": seat, "attempts": attempts,
        "first_err_ts": _ts(14, 2, 11), "last_err_ts": _ts(14, 2, 19),
        "err_head": _CAUSE_TEXT[cls], "http_status": 429, "from_provider": provider,
    }


@pytest.mark.parametrize("cls,hop,seat,attempts", list(itertools.product(
    fp.TRIGGER_CLASSES, fp.HOPS + (None,), ("sub-vps-9", None), (1, 3))))
def test_rider_carries_all_four_fields(cls, hop, seat, attempts):
    rider = fp.format_cause_rider(_row(cls, hop, seat, attempts), tz=UTC)
    assert _CAUSE_RE.search(rider)
    assert _HOP_RE.search(rider), rider
    assert _SUB_RE.search(rider), rider
    assert _TIME_RE.search(rider), rider
    assert bool(_COUNT_RE.match(rider)) is (attempts > 1)
    assert "?" not in rider, rider  # t_246ce7d6: words, never a bare "?"
    if cls == "pool_pressure" and seat is None and hop in (None, "relay"):
        # Pool-wide relay refusal: the hop IS the relay and no seat exists (t_e17de574).
        assert rider.endswith(("relay busy: all subs at capacity (at the relay), 14:02:11",
                               "relay busy: all subs at capacity (at the relay), 14:02:11-19")), rider
        return
    if cls == "provider_invalid_response":
        # t_d35beb85: a rejected billed 200 -> the relay answered, so the hop
        # is known by construction; the seat comes from x-pool-served-by.
        assert "hop unknown" not in rider and "hop=relay-200" in rider, rider
        assert ("sub=sub-vps-9" if seat else "sub=unknown") in rider, rider
        return
    if hop is None:
        assert "hop unknown" in rider
    if seat is None and cls != "quota_model":
        assert "sub unknown" in rider


def test_pinned_spec_examples():
    r1 = {"trigger_class": "conn", "hop": "relay->bridge", "seat": "sub-vps-9", "attempts": 3,
          "first_err_ts": _ts(14, 2, 11), "last_err_ts": _ts(14, 2, 19),
          "err_head": "Connection reset by peer"}
    assert fp.format_cause_rider(r1, tz=UTC) == "3x connection reset to sub-vps-9 bridge, 14:02:11-19"
    r2 = {"trigger_class": "quota_model", "hop": "bridge→anthropic", "seat": None, "attempts": 1,
          "first_err_ts": _ts(14, 5, 40), "http_status": 429,
          "err_head": "Claude Fable weekly limit reached", "from_provider": "claude-bpr"}
    assert fp.format_cause_rider(r2, tz=UTC) == (
        "Fable weekly limit (Anthropic 429) on all subs, 14:05:40")
    r3 = {"trigger_class": "quota_model", "hop": "relay", "attempts": 1,
          "first_err_ts": _ts(9, 12, 3), "err_head": "this model's budget is capped",
          "from_provider": "claude-apr"}
    assert fp.format_cause_rider(r3, tz=UTC) == (
        "model budget capped at the relay on all subs, 09:12:03")


def test_pool_wide_relay_busy_names_the_relay_not_unknown():
    """t_e17de574: the 05:44-05:45 rows (pool_pressure, 503, class_source=text,
    hop/seat NULL, err_head NULL) rendered "relay busy (hop unknown, sub unknown)"."""
    row = {"trigger_class": "pool_pressure", "class_source": "text", "http_status": 503,
           "hop": None, "seat": None, "err_head": None, "from_provider": "claude-bpr",
           "first_err_ts": _ts(12, 45, 27)}
    assert fp.format_cause_rider(row, tz=UTC) == "relay busy: all subs at capacity (at the relay), 12:45:27"
    row["err_head"] = "pool at capacity"
    assert fp.format_cause_rider(row, tz=UTC) == "relay busy: all subs at capacity (at the relay), 12:45:27"
    # A seat-attributed or upstream pool_pressure keeps its hop/seat fields.
    over = dict(row, err_head="Overloaded", http_status=529, hop="bridge→anthropic",
                seat="sub-vps-2")
    assert fp.format_cause_rider(over, tz=UTC) == (
        "upstream overloaded (Anthropic 529) on sub-vps-2, 12:45:27")
    # A genuinely unattributable non-pool-pressure row still says so.
    unk = dict(row, trigger_class="conn")
    assert "(hop unknown, sub unknown)" in fp.format_cause_rider(unk, tz=UTC)


def test_relay_ascii_hops_normalize():
    assert fp.normalize_hop("relay->bridge") == "relay→bridge"
    assert fp.normalize_hop("bridge->upstream") == "bridge→anthropic"
    assert fp.normalize_hop("relay") == "relay"
    assert fp.normalize_hop("bogus") is None


def test_hop_agrees_with_row():
    # relay_synthetic rows carry relay / relay→bridge; an unattributable relay
    # response renders hop unknown, never bridge→anthropic (pass-4 B4).
    synthetic = fp.format_cause_rider({"trigger_class": "conn", "hop": "relay→bridge",
                                       "relay_synthetic": 1, "seat": "sub-vps-2",
                                       "first_err_ts": _ts(1, 2, 3)}, tz=UTC)
    assert "to sub-vps-2 bridge" in synthetic
    unknown = fp.format_cause_rider({"trigger_class": "unclassified", "hop": None,
                                     "class_source": "text", "seat": None,
                                     "first_err_ts": _ts(1, 2, 3)}, tz=UTC)
    assert "(hop unknown, sub unknown)" in unknown and "Anthropic" not in unknown


def test_seat_names_off_renders_a_sub():
    r = fp.format_cause_rider(_row("conn", "relay→bridge", "sub-vps-9", 1), seat_names=False,
                              tz=UTC)
    assert "a sub" in r and "sub-vps-9" not in r


def test_head_label_override_only_for_relay_sourced_transients():
    assert fp.head_label_override({"trigger_class": "conn", "class_source": "relay_header"}) == \
        "connection issue"
    assert fp.head_label_override({"trigger_class": "pool_pressure",
                                   "relay_synthetic": 1}) == "relay busy"
    assert fp.head_label_override({"trigger_class": "conn", "class_source": "text"}) is None
    assert fp.head_label_override({"trigger_class": "quota_seat",
                                   "class_source": "relay_header"}) is None


@pytest.mark.parametrize("branch", ["warm_seat", "fallback_cold", "compaction", "fallback_failed"])
def test_recovery_rider_fields(branch):
    row = {"return_branch": branch, "seat": "sub-vps-6", "since_primary_call_s": 720,
           "expected_warm": branch == "warm_seat", "fallback_idle_s": 64 * 60,
           "dwell_s": 18 * 60, "dwell_turns": 7, "from_model": "claude-opus-5-5",
           "trigger_class": "quota_seat"}
    r = fp.format_recovery_rider(row)
    assert "sub-vps-6" in r and "expected" in r and "after 18m / 7 turns on Opus" in r
    marker = {"warm_seat": "primary eligible", "fallback_cold": "fallback idle 64m",
              "compaction": "compaction", "fallback_failed": "fallback failed"}[branch]
    assert marker in r


def test_mutation_dropping_a_field_is_caught(monkeypatch):
    """Dropping any one field from the rider must turn the property red."""
    row = _row("conn", "relay→bridge", "sub-vps-9", 3)
    monkeypatch.setattr(fp, "_count_window", lambda r, tz: ("", ""))
    assert not _TIME_RE.search(fp.format_cause_rider(row, tz=UTC))
    monkeypatch.undo()
    monkeypatch.setattr(fp, "_seat_token", lambda r, s: "")
    assert not _SUB_RE.search(fp.format_cause_rider(row, tz=UTC))
    monkeypatch.undo()
    monkeypatch.setattr(fp, "_hop_segment", lambda h, s, st: s)
    assert not _HOP_RE.search(fp.format_cause_rider(row, tz=UTC))



# ── AST guard (§4.8 lint): route-change notices are built in ONE place ──────

import ast as _ast
import pathlib as _pathlib

_REPO = _pathlib.Path(__file__).resolve().parents[2]
_NOTICE_WORDS = ("Model fallback", "Model recovery")
# Recognizers READ a finished notice (prefix match); they never build one.
_NOTICE_RECOGNIZERS = {("gateway/run.py", "_is_model_route_change_status")}
_BUILDER = ("agent/chat_completion_helpers.py", "_emit_fallback_announce")


def _notice_strings(path, rel):
    """(lineno, enclosing function) for every non-docstring str constant
    (f-string pieces included) naming a route-change notice."""
    tree = _ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {id(n.value) for n in _ast.walk(tree)
                  if isinstance(n, _ast.Expr) and isinstance(n.value, _ast.Constant)}
    out = []

    def visit(node, fn):
        for child in _ast.iter_child_nodes(node):
            name = child.name if isinstance(child, (_ast.FunctionDef, _ast.AsyncFunctionDef)) else fn
            if (isinstance(child, _ast.Constant) and isinstance(child.value, str)
                    and id(child) not in docstrings
                    and any(w in child.value for w in _NOTICE_WORDS)):
                out.append((child.lineno, name))
            visit(child, name)

    visit(tree, None)
    return out


def _violations(files):
    bad = []
    for path, rel in files:
        for lineno, fn in _notice_strings(path, rel):
            if (rel, fn) == _BUILDER or (rel, fn) in _NOTICE_RECOGNIZERS:
                continue
            bad.append(f"{rel}:{lineno} in {fn}")
    return bad


def _scope():
    for sub in ("agent", "gateway"):
        for p in sorted((_REPO / sub).rglob("*.py")):
            yield p, p.relative_to(_REPO).as_posix()


def test_ast_guard_notice_strings_live_in_the_announce():
    assert _violations(_scope()) == []


def test_ast_guard_announce_calls_format_cause_rider():
    rel, fn = _BUILDER
    tree = _ast.parse((_REPO / rel).read_text(encoding="utf-8"))
    [func] = [n for n in _ast.walk(tree) if isinstance(n, _ast.FunctionDef) and n.name == fn]
    called = {(c.func.attr if isinstance(c.func, _ast.Attribute) else getattr(c.func, "id", None))
              for c in _ast.walk(func) if isinstance(c, _ast.Call)}
    assert "format_cause_rider" in called
    # and the builder really constructs the verbs (the guard is not vacuous)
    assert _notice_strings(_REPO / rel, rel)


def test_ast_guard_notice_self_test_flags_planted_construction(tmp_path):
    planted = tmp_path / "planted.py"
    planted.write_text('def announce(agent, icon, a, b):\n'
                       '    agent._emit_status(f"{icon} Model fallback: {a} → {b}")\n'
                       'def other():\n    verb = "Model recovery"\n    return verb\n')
    assert _violations([(planted, "agent/planted.py")]) == [
        "agent/planted.py:2 in announce", "agent/planted.py:4 in other"]


# ── recovery rider: "sub" only on relay lanes (Ace 2026-09-28, #kimi-k3) ──────
def _kimi_recovery_row(**over):
    row = {"return_branch": "compaction", "seat": None, "to_provider": "cpa", "to_model": "kimi-k3",
           "from_provider": "claude-bpr", "from_model": "claude-opus-5-5", "since_primary_call_s": None,
           "expected_warm": False, "fallback_idle_s": 90, "dwell_s": 30 * 60, "dwell_turns": 2,
           "trigger_class": "quota_session"}
    row.update(over)
    return row


def test_recovery_rider_no_seat_clause_on_plain_provider():
    """The real 2026-09-28 13:28 return cpa/kimi-k3 <- Opus rendered "on sub ?" — Kimi has no seats."""
    r = fp.format_recovery_rider(_kimi_recovery_row())
    assert "sub" not in r and "?" not in r
    assert r == ("compaction rewrote the prefix; both caches cold, one full cache write "
                 "(expected cold), after 30m / 2 turns on Opus")


@pytest.mark.parametrize("prov", ["openai-codex", "openrouter", "xai-oauth", "gemini-bridge"])
def test_recovery_rider_plain_providers_never_say_sub(prov):
    for branch in ("warm_seat", "fallback_cold", "compaction", "cap_expiry", "fallback_failed"):
        r = fp.format_recovery_rider(_kimi_recovery_row(return_branch=branch, to_provider=prov))
        assert "sub" not in r, (prov, branch, r)


def test_recovery_rider_relay_lane_lost_seat_says_unknown_in_words():
    r = fp.format_recovery_rider(_kimi_recovery_row(to_provider="claude-bpr", to_model="claude-opus-5-5"))
    assert "on sub unknown" in r and "sub ?" not in r


def test_recovery_rider_relay_lane_known_seat_unchanged():
    r = fp.format_recovery_rider(_kimi_recovery_row(to_provider="claude-bpr", seat="sub-vps-6"))
    assert "on sub-vps-6" in r
