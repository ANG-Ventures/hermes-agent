"""Manual /compress banner must name its trigger (2026-08-20).

Every AUTOMATIC compaction names its trigger in the announce head via
``_compaction_reason_clause`` (threshold, hygiene, overflow, ...). The manual
/compress path historically did not, so a user-invoked compress rendered a
stats block indistinguishable from the system compacting on its own —
reported as "why did you compress here, there was no reason stated" when the
user themselves had run /compress minutes earlier.

Contract: every compaction banner names its initiator, manual or not. These
tests pin (a) the clause itself and (b) that BOTH manual banner build sites —
the granular reconciling block and the two-line fallback — actually render it.
A guard that exists but isn't wired into the emitting path is the classic
inert-fix shape, so the wiring is asserted directly against the source of the
handler rather than only the constant.
"""

import inspect
import re

from gateway import slash_commands


def test_manual_trigger_clause_names_the_command():
    clause = slash_commands._MANUAL_COMPRESS_TRIGGER_CLAUSE
    assert "/compress" in clause
    # Leading space so it concatenates cleanly after the headline.
    assert clause.startswith(" ")
    # Desktop e2e contract (session-compression-and-queue-stop.spec.ts)
    # matches /Compressed|No changes from compression/ against the reply —
    # the clause is a SUFFIX and must not restructure the headline.
    assert not clause.startswith("Compressed")


def test_both_manual_banner_sites_render_the_clause():
    src = inspect.getsource(slash_commands)
    # Site 1: the granular reconciling block head.
    # Site 2: the two-line fallback headline.
    sites = re.findall(
        r"summary\['headline'\]\}?\"?\s*\n?\s*f?\"?\{_MANUAL_COMPRESS_TRIGGER_CLAUSE\}",
        src,
    )
    headline_uses = src.count("summary['headline']}")
    clause_uses = src.count("{_MANUAL_COMPRESS_TRIGGER_CLAUSE}")
    # Every headline render site must carry the clause: if someone adds a new
    # banner site without attribution, or drops the clause from an existing
    # one, this fails.
    assert headline_uses >= 2, f"expected >=2 headline render sites, found {headline_uses}"
    assert clause_uses == headline_uses, (
        f"{headline_uses} headline render site(s) but only {clause_uses} carry "
        "_MANUAL_COMPRESS_TRIGGER_CLAUSE — a manual compaction banner would "
        "ship without naming its trigger"
    )
    assert len(sites) == headline_uses, (
        "clause must be adjacent to the headline (same banner head), "
        f"found {len(sites)} adjacent of {headline_uses}"
    )
