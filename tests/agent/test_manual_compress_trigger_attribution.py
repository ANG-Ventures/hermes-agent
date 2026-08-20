"""Manual compression banners must name their trigger (2026-08-20).

Every AUTOMATIC compaction names its trigger in the announce head via
``_compaction_reason_clause`` (threshold, hygiene, overflow, ...). The manual
surfaces historically did not, so a user-invoked /compress rendered a stats
block indistinguishable on the surface from the system compacting on its own
— reported as "why did you compress here, there was no reason stated" when
the user themselves had run /compress minutes earlier (the reason existed as
``trigger=manual_compress_command`` in agent.log; it just wasn't rendered).

Contract: every compaction banner names its initiator, manual or not. The fix
lives at the CHOKEPOINT — ``summarize_manual_compression`` appends
``MANUAL_TRIGGER_CLAUSE`` to every headline it returns — so all four manual
surfaces (gateway /compress, TUI session.compress RPC, TUI slash mirror, CLI)
are covered by one line, and a new surface built on the helper inherits
attribution for free. These tests pin the clause across every headline shape
(classic, enhanced A/B/C, aborted, fallback) and that no caller
double-appends it.
"""

import inspect
import re

from agent.manual_compression_feedback import (
    MANUAL_TRIGGER_CLAUSE,
    summarize_manual_compression,
)


def _msgs(n):
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i} " * 50}
        for i in range(n)
    ]


def test_clause_names_the_command():
    assert "/compress" in MANUAL_TRIGGER_CLAUSE
    # Leading space: concatenates cleanly as a headline suffix. The desktop
    # e2e contract (session-compression-and-queue-stop.spec.ts) matches
    # /Compressed|No changes from compression/ — a suffix cannot break it.
    assert MANUAL_TRIGGER_CLAUSE.startswith(" ")


def test_every_headline_shape_carries_the_trigger():
    cases = [
        # classic compressed
        dict(before=_msgs(10), after=_msgs(4), bt=1000, at=400),
        # classic no-op
        dict(before=_msgs(4), after=_msgs(4), bt=400, at=400),
        # enhanced CASE B (both axes shrank)
        dict(
            before=_msgs(10), after=_msgs(4), bt=1000, at=400,
            kw=dict(non_chat_count=50, non_chat_tokens=5000,
                    transcript_rewritten=True, full_before_count=60),
        ),
        # enhanced CASE C (true no-op)
        dict(
            before=_msgs(4), after=_msgs(4), bt=400, at=400,
            kw=dict(non_chat_count=50, non_chat_tokens=5000,
                    transcript_rewritten=False, full_before_count=54),
        ),
        # enhanced CASE A (chat compact, stored rows dropped)
        dict(
            before=_msgs(4), after=_msgs(4), bt=400, at=400,
            kw=dict(non_chat_count=50, non_chat_tokens=5000,
                    transcript_rewritten=True, full_before_count=54),
        ),
    ]
    for c in cases:
        s = summarize_manual_compression(
            c["before"], c["after"], c["bt"], c["at"], **c.get("kw", {})
        )
        assert s["headline"].endswith(MANUAL_TRIGGER_CLAUSE), s["headline"]
        # exactly once — no double attribution
        assert s["headline"].count(MANUAL_TRIGGER_CLAUSE.strip()) == 1


def test_aborted_and_fallback_headlines_carry_the_trigger():
    class _Aborted:
        _last_compress_aborted = True

    class _Fallback:
        _last_compress_aborted = False
        _last_summary_fallback_used = True
        _last_summary_dropped_count = 3

    for state in (_Aborted(), _Fallback()):
        s = summarize_manual_compression(
            _msgs(12), _msgs(4), 1200, 400, compression_state=state
        )
        assert s["headline"].endswith(MANUAL_TRIGGER_CLAUSE), s["headline"]


def test_no_caller_double_appends():
    """The clause is appended at the chokepoint ONLY. A caller re-appending it
    (as the first iteration of this fix did in gateway/slash_commands.py)
    would render '... (you ran /compress) (you ran /compress)'."""
    import gateway.slash_commands as sc
    import tui_gateway.methods_session as ms
    import tui_gateway.methods_tools as mt

    for mod in (sc, ms, mt):
        src = inspect.getsource(mod)
        assert "MANUAL_TRIGGER_CLAUSE}" not in re.sub(
            r"#[^\n]*", "", src
        ), f"{mod.__name__} appends the clause itself — chokepoint already does"
