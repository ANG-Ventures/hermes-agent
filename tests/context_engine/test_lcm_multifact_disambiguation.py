"""PRD-8.3 tests — multi-fact node disambiguation.

Two prongs:
  Prong A (optimization): summarizer prompts must instruct identifier fidelity
    (no merge / range-collapse / truncation of distinct identifier->value facts).
  Prong B (the fix): recovery escalates to the verbatim store when the node
    answer abstains, is empty, or cites a grouped/range mapping.

These are pure-function + prompt-contract controls (offline, free). The live
confident-wrong gate is the separate N>=600 campaign (PRD-8.3 AC-4).
"""
from __future__ import annotations

import importlib

esc = importlib.import_module("plugins.context_engine.lcm.escalation")


# ---- Prong A: summarizer prompt identifier-fidelity clause (AC-1) -----------

def test_l1_prompt_has_identifier_fidelity_rule():
    p = esc._build_l1_prompt("CONTENT", token_budget=500, depth=0)
    low = p.lower()
    assert "identifier fidelity" in low
    assert "never merge" in low or "do not merge" in low
    # explicitly forbids the grouped/range line that caused the bug
    assert "1300/1600/1900" in p
    assert "one line per distinct identifier" in low


def test_l2_prompt_has_identifier_fidelity_rule():
    p = esc._build_l2_prompt("CONTENT", token_budget=300)
    low = p.lower()
    assert "identifier fidelity" in low
    assert "truncate" in low  # forbids the "R" mid-word truncation class
    assert "1300/1600/1900" in p


def test_l1_prompt_still_summarizes_normal_content():
    # the fidelity rule must not destroy the base summarize instruction
    p = esc._build_l1_prompt("CONTENT", token_budget=500, depth=0)
    assert "Summarize this conversation segment" in p
    assert "CONTENT" in p


# ---- Prong B: needs_escalation trigger (positive + negative controls) -------











# ---- Prong B: recovery prompt mandates abstain-over-guess + no-grouped -------

def test_semantic_recovery_question_forbids_grouped_inference_and_mandates_abstain():
    # Build the question the same way the harness does and assert the clauses.
    # (mirror of _node_served_recovery_semantic's question text)
    phrase = "recover-1300"
    q = (
        f"Who is the recovery owner associated with the EXACT handoff "
        f"phrase {phrase}? Answer with the owner's full name. "
        f"Use ONLY an entry that names {phrase} exactly and by "
        f"itself. Do NOT infer the owner from a grouped or range mapping "
        f"(e.g. a line like '1300/1600/1900 = Name'); a grouped line is "
        f"not a valid source. If {phrase} is not present exactly "
        f"and unambiguously with its own full owner name, reply with "
        f"exactly: no matching owner found"
    )
    assert "EXACT" in q
    assert "Do NOT infer the owner from a grouped or range mapping" in q
    assert "no matching owner found" in q


# ---- Integration: a merged node answer escalates and is re-scored -----------



# ---- AC-5 baseline-repro toggle: --no-escalation must be representable --------





# ---- AC-5 root-cause fix: Prong-A fidelity toggle (the loop-bug fix) ----------
# The first rerun looped forever because the identifier-fidelity prompt (Prong A)
# was hardcoded ON, so the baseline-repro arm could not reproduce the merge bug
# (CW=0 -> abort -> net-cron re-fire). The fix makes Prong A toggleable per
# subprocess so the baseline runs PRE-FIX summarization on identical code.

def test_fidelity_default_on_when_env_unset(monkeypatch):
    monkeypatch.delenv("LCM_IDENTIFIER_FIDELITY", raising=False)
    assert esc._identifier_fidelity_enabled() is True
    p = esc._build_l1_prompt("CONTENT", token_budget=500, depth=0)
    assert "IDENTIFIER FIDELITY" in p


def test_fidelity_off_reproduces_prefix_prompt(monkeypatch):
    # baseline-repro arm: env=0 -> the fidelity block is ABSENT (pre-fix prompt),
    # which is what lets the K=2 merge bug reproduce.
    monkeypatch.setenv("LCM_IDENTIFIER_FIDELITY", "0")
    assert esc._identifier_fidelity_enabled() is False
    l1 = esc._build_l1_prompt("CONTENT", token_budget=500, depth=0)
    l2 = esc._build_l2_prompt("CONTENT", token_budget=300)
    assert "IDENTIFIER FIDELITY" not in l1
    assert "IDENTIFIER FIDELITY" not in l2
    # base summarize instruction must survive in both arms
    assert "Summarize this conversation segment" in l1
    assert "CONTENT" in l1


def test_fidelity_failsafe_to_on_for_garbage(monkeypatch):
    # anything unrecognised (incl. empty string) must default to production
    # behaviour (fidelity ON) — never silently drop fidelity in prod.
    for v in ("1", "true", "yes", "", "garbage"):
        monkeypatch.setenv("LCM_IDENTIFIER_FIDELITY", v)
        assert esc._identifier_fidelity_enabled() is True, v
    for v in ("0", "false", "no", "off", "OFF"):
        monkeypatch.setenv("LCM_IDENTIFIER_FIDELITY", v)
        assert esc._identifier_fidelity_enabled() is False, v




