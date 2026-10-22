"""t_6e9f48cc: a review handback answers every Prism key open at its head, or it surfaces.

The home's ``scripts/prism_soft_gate.py`` is the one owner of both the grammar and the
``round`` key list; the fixture writes a stand-in with the same CLI and constants into the
temp HERMES_HOME and runs it as a real subprocess (no seam on the default reader).
"""
from __future__ import annotations

import textwrap

import pytest

from gateway import kanban_prism_answers as pa

HEAD = "f" * 40
K1, K2 = "5ef38967cc14", "f1a50a36ff7f"

STUB = textwrap.dedent('''
    import os, re, sys
    VERDICT_LINE = re.compile(r"prism-verdict:\\s*([A-Za-z0-9_.\\-]+)\\s+(not-a-bug|false|by-design)\\b", re.I)
    FIXED_LINE = re.compile(r"prism-verdict:\\s*([A-Za-z0-9_.\\-]+)\\s+fixed\\s+(?:in\\s+)?([0-9a-f]{7,40})\\b", re.I)
    if __name__ == "__main__" and sys.argv[1:2] == ["round"]:
        sys.stdout.write(open(os.path.join(os.path.dirname(__file__), "round.out")).read())
        sys.exit(int(open(os.path.join(os.path.dirname(__file__), "round.rc")).read()))
''')


@pytest.fixture
def home(monkeypatch):
    from hermes_constants import get_default_hermes_root

    scripts = get_default_hermes_root() / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "prism_soft_gate.py").write_text(STUB)
    monkeypatch.setattr(pa, "_GRAMMAR", {})

    def set_round(out: str, rc: int = 0) -> None:
        (scripts / "round.out").write_text(out)
        (scripts / "round.rc").write_text(str(rc))
    return set_round


def _round(*keys, state="HOLD", tail_kind="unanswered"):
    lines = [f"{tail_kind}: {k} P1 scripts/x.py:1 title" for k in keys]
    return "\n".join(lines + [f"round=2 held=1 fr_cap=3 p0=no unanswered={len(keys)} state={state}"]) + "\n"


SUMMARY = f"Both fixed.\nprism-verdict: {K1} fixed {HEAD}\nprism-verdict: {K2} by-design the gate is advisory\n"


def test_every_open_key_answered_is_true(home):
    home(_round(K1, K2) + f"open-at-head: {K2} P1 a.py:2 t (new at head)\n")
    assert pa.all_answered("ANG-Ventures/hermes-home", 6048, HEAD, "t_464b8b64", [SUMMARY]) is True


@pytest.mark.parametrize("kind", ["unanswered", "pending-carry", "open-at-head"])
def test_one_key_missing_is_false(home, kind):
    home(_round(K1, "0123456789ab", tail_kind=kind))
    assert pa.all_answered("o/r", 1, HEAD, "t_1", [SUMMARY]) is False


@pytest.mark.parametrize("text", [
    "all findings fixed",                           # no grammar line at all
    f"{K1} fixed: moved the guard",                  # prose: answers nothing
    f"prism-verdict {K1}: fixed {HEAD}",             # misspelt grammar
])
def test_summary_without_grammar_lines_is_false(home, text):
    home(_round(K1))
    assert pa.all_answered("o/r", 1, HEAD, "t_1", [text]) is False


def test_zero_open_keys_still_needs_a_line(home):
    home(_round())
    assert pa.all_answered("o/r", 1, HEAD, "t_1", ["ready, no findings"]) is False
    assert pa.all_answered("o/r", 1, HEAD, "t_1", [SUMMARY]) is True


@pytest.mark.parametrize("out,rc", [
    (_round(K1, state="WAIT"), 0),                   # no Prism record at this head yet
    ("round=? o/r#1 @ ffffffff: could not evaluate (X: y)\n", 3),
    (_round(K1, state="FR-UNKNOWN"), 0),
    ("", 0),
])
def test_unreadable_round_is_none(home, out, rc):
    home(out, rc)
    assert pa.all_answered("o/r", 1, HEAD, "t_1", [SUMMARY]) is None


def test_no_home_script_or_bad_head_is_none(home):
    from hermes_constants import get_default_hermes_root

    assert pa.all_answered("o/r", 1, "abc", "t_1", [SUMMARY]) is None
    (get_default_hermes_root() / "scripts" / "prism_soft_gate.py").unlink()
    assert pa.all_answered("o/r", 1, HEAD, "t_1", [SUMMARY]) is None
