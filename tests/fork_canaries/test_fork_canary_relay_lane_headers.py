"""Fork canary: relay-pool session affinity + interactive/background lane headers.

Surface: ``agent/fork_ext/relay_headers.py``, re-exported through
``agent.chat_completion_helpers`` and stamped on every outbound request built
for the fork's Claude API-proxy pool.

Fork-permanent feature. Three properties, each with a distinct blast radius if
a parity merge erodes it:

* **Pool scoping** — the headers are routing metadata for a loopback relay hop
  and are stripped before upstream dispatch. They must be stamped ONLY for the
  ``claude-apr`` pool. Widening the gate egresses fork-internal identifiers to a
  direct Anthropic endpoint or a third party, which violates the project's
  no-outbound-attribution rubric outright.
* **Per-request session affinity** — the id is read off the LIVE agent at
  call-build time, so it rotates when compaction mints a child id. Hoisting it
  into a static default header (or reading a ContextVar across the httpx worker
  boundary) causes cross-conversation prompt-cache key bleed.
* **Lane classification** — interactive vs background, with the B1 rule that
  critical auxiliary work on a live turn's path stays ``interactive`` (never
  damp the compaction a user is blocked on) and the fail-safe rule that an
  unknown/absent source is ``background`` (a headless burst must not claim
  interactive headroom).

The registry points at ``tests/agent/test_pool_affinity_header.py``; this file
adds the merge-seam assertions that file does not make — notably the
rename-proof frozenset gate and the no-signal fail-safe.
"""

from types import SimpleNamespace

import pytest

# Import through the public consumer surface, exactly as the shipped sibling
# test does — this also proves the fork_ext helpers are still re-exported.
from agent.chat_completion_helpers import (
    _pool_affinity_headers,
    _pool_lane,
    _pool_lane_src,
)


def _agent(provider="claude-apr", session_id="20260830_120000_abc123",
           delegate_depth=0, platform="discord"):
    return SimpleNamespace(
        provider=provider,
        session_id=session_id,
        _delegate_depth=delegate_depth,
        platform=platform,
    )


# --------------------------------------------------------------------------- #
# Pool scoping — the egress boundary
# --------------------------------------------------------------------------- #

def test_headers_stamped_for_the_api_proxy_pool():
    """RED-PROVABLE: in agent/fork_ext/relay_headers.py (~L118) change the gate
    to ``if provider in _POOL_AFFINITY_PROVIDERS: return {}`` (invert it)."""
    headers = _pool_affinity_headers(_agent())
    assert headers.get("x-hermes-session") == "20260830_120000_abc123"
    assert headers.get("x-hermes-lane") == "interactive"
    assert "x-hermes-lane-src" in headers


@pytest.mark.parametrize(
    "provider",
    [
        "anthropic",        # direct Anthropic endpoint — must never see them
        "claude-bpr",       # bridge pool, chat_completions branch — out of scope
        "openrouter",       # third party
        "openai-codex",     # third party
        "nous",             # third party
        "",                 # unset
    ],
)
def test_headers_never_egress_to_a_non_pool_provider(provider):
    """These are loopback routing metadata. Stamping them on a direct or
    third-party endpoint is an outbound-attribution leak.

    RED-PROVABLE: in agent/fork_ext/relay_headers.py (~L83) widen
    ``_POOL_AFFINITY_PROVIDERS`` to include the provider under test, or delete
    the membership check at ~L118."""
    assert _pool_affinity_headers(_agent(provider=provider)) == {}, (
        f"fork relay headers leaked to provider {provider!r} — routing metadata "
        f"must never egress beyond the claude-apr loopback hop."
    )


def test_pool_gate_is_a_set_membership_not_a_bare_literal():
    """A stale single ``==`` literal silently killed affinity/lane stamping
    when the pool was renamed claude-app→claude-apr (#241, 2026-07-08). The
    frozenset keeps the gate rename-auditable in one place.

    RED-PROVABLE: in agent/fork_ext/relay_headers.py (~L83) replace
    ``_POOL_AFFINITY_PROVIDERS = frozenset({"claude-apr"})`` with a bare string
    constant and an ``==`` comparison at the call site."""
    from agent.fork_ext import relay_headers

    gate = relay_headers._POOL_AFFINITY_PROVIDERS
    assert isinstance(gate, (frozenset, set)), (
        f"the pool gate degraded to {type(gate).__name__}; a bare literal is "
        f"how the claude-app→claude-apr rename silently broke stamping."
    )
    assert "claude-apr" in gate


def test_provider_matching_is_case_and_whitespace_insensitive():
    """RED-PROVABLE: drop ``.strip().lower()`` from the provider read in
    ``_pool_affinity_headers`` (agent/fork_ext/relay_headers.py ~L117)."""
    assert _pool_affinity_headers(_agent(provider="  Claude-APR  "))


# --------------------------------------------------------------------------- #
# Per-request session affinity
# --------------------------------------------------------------------------- #

def test_session_id_is_read_live_on_every_call():
    """Compaction mints a child session id mid-conversation; a header captured
    once would pin the new conversation to the old subscription's cache key.

    RED-PROVABLE: in ``_pool_affinity_headers``
    (agent/fork_ext/relay_headers.py ~L120) hoist ``sid`` to a module-level
    cached value instead of ``getattr(agent, "session_id", None)``."""
    agent = _agent(session_id="sess-A")
    assert _pool_affinity_headers(agent)["x-hermes-session"] == "sess-A"
    agent.session_id = "sess-B"  # simulate a compaction-minted child id
    assert _pool_affinity_headers(agent)["x-hermes-session"] == "sess-B", (
        "the affinity id was captured rather than read live — this is the "
        "cross-conversation prompt-cache key-bleed failure mode."
    )


def test_lane_is_still_stamped_when_no_session_id_exists():
    """Lane routing must not depend on affinity being available.

    RED-PROVABLE: in ``_pool_affinity_headers`` move the ``x-hermes-lane``
    assignments (~L123) inside the ``if sid and isinstance(sid, str):``
    block."""
    headers = _pool_affinity_headers(_agent(session_id=None))
    assert "x-hermes-session" not in headers
    assert headers.get("x-hermes-lane") == "interactive"


def test_non_string_session_id_is_not_stamped():
    """RED-PROVABLE: drop the ``isinstance(sid, str)`` guard
    (agent/fork_ext/relay_headers.py ~L122) — a non-str id would be stamped and
    break header encoding."""
    assert "x-hermes-session" not in _pool_affinity_headers(_agent(session_id=12345))


# --------------------------------------------------------------------------- #
# Lane classification
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "platform,depth,aux,expected",
    [
        # Live human-facing turn.
        ("discord", 0, None, "interactive"),
        ("telegram", 0, None, "interactive"),
        ("tui", 0, None, "interactive"),
        ("desktop", 0, None, "interactive"),
        # Subagent → background regardless of surface.
        ("discord", 1, None, "background"),
        # Headless / scheduled principal → background.
        ("cron", 0, None, "background"),
        ("cli", 0, None, "background"),
        # B1: CRITICAL aux on a live top-level turn's path stays interactive —
        # never damp the compaction the user's turn is blocked on.
        ("discord", 0, "compression", "interactive"),
        ("discord", 0, "title", "interactive"),
        # OFF-PATH aux (subagent or headless principal) → background.
        ("discord", 1, "compression", "background"),
        ("cron", 0, "compression", "background"),
    ],
)
def test_lane_classification(platform, depth, aux, expected):
    """RED-PROVABLE for the B1 rule: in ``_pool_lane``
    (agent/fork_ext/relay_headers.py ~L25) change the aux branch to an
    unconditional ``return "background"`` — the two ``("discord", 0, aux)``
    interactive rows fail. RED-PROVABLE for the depth rule: delete the
    ``delegated`` term at ~L28."""
    lane = _pool_lane(_agent(platform=platform, delegate_depth=depth), aux)
    assert lane == expected, (
        f"lane for platform={platform!r} depth={depth} aux={aux!r} was "
        f"{lane!r}, expected {expected!r}"
    )


def test_no_source_signal_fails_safe_to_background(monkeypatch):
    """A request with no platform and no HERMES_SESSION_SOURCE must NOT be
    granted interactive headroom — otherwise a scheduled/headless burst can
    starve live turns under contention.

    RED-PROVABLE: in ``_is_noninteractive_principal``
    (agent/fork_ext/relay_headers.py ~L57) change the no-signal
    ``return True`` to ``return False``."""
    monkeypatch.delenv("HERMES_SESSION_SOURCE", raising=False)
    assert _pool_lane(_agent(platform="")) == "background"


def test_unknown_platform_is_background_not_interactive(monkeypatch):
    """Allow-list semantics: an unrecognised surface is background. A
    deny-list would false-negative every new headless runner into interactive
    (the Greptile #206 regression).

    RED-PROVABLE: in ``_is_noninteractive_principal``
    (agent/fork_ext/relay_headers.py ~L59) invert the membership test to
    ``src in _INTERACTIVE_PLATFORMS``."""
    monkeypatch.delenv("HERMES_SESSION_SOURCE", raising=False)
    assert _pool_lane(_agent(platform="some-new-batch-runner")) == "background"


def test_session_source_env_is_the_documented_fallback(monkeypatch):
    """RED-PROVABLE: delete the ``HERMES_SESSION_SOURCE`` fallback read in
    ``_is_noninteractive_principal`` (agent/fork_ext/relay_headers.py ~L55)."""
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "discord")
    assert _pool_lane(_agent(platform="")) == "interactive"
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "cron")
    assert _pool_lane(_agent(platform="")) == "background"


# --------------------------------------------------------------------------- #
# lane-src: the lane must be validatable against its inputs
# --------------------------------------------------------------------------- #

def test_lane_src_carries_the_classifier_inputs():
    """The relay logs the raw signals next to the verdict so the lane can be
    audited against its inputs rather than against itself.

    RED-PROVABLE: in ``_pool_lane_src`` (agent/fork_ext/relay_headers.py ~L77)
    drop any of the three ``key=value`` segments from the returned f-string."""
    src = _pool_lane_src(_agent(platform="discord", delegate_depth=2), "compression")
    assert "platform=discord" in src
    assert "delegate_depth=2" in src
    assert "aux_task=compression" in src


def test_lane_src_reflects_the_same_source_the_classifier_used(monkeypatch):
    """If lane-src showed a partial view (e.g. blank platform) while the
    classifier used the env fallback, the logged inputs would not explain the
    verdict.

    RED-PROVABLE: in ``_pool_lane_src`` remove the ``HERMES_SESSION_SOURCE``
    fallback (agent/fork_ext/relay_headers.py ~L74) so platform renders ``-``
    while the lane still says interactive."""
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "discord")
    agent = _agent(platform="")
    assert "platform=discord" in _pool_lane_src(agent), (
        "lane-src disagrees with the source the lane classifier actually used"
    )
    assert _pool_lane(agent) == "interactive"


def test_main_turn_renders_a_placeholder_aux_task():
    """RED-PROVABLE: in ``_pool_lane_src``
    (agent/fork_ext/relay_headers.py ~L76) change the ``aux_task`` default
    from ``"-"`` to ``None`` — the header renders ``aux_task=None``, which the
    relay's log parser reads as a real task name."""
    assert "aux_task=-" in _pool_lane_src(_agent(), None)
