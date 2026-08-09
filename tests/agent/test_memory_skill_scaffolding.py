"""MemoryManager strips slash-skill scaffolding for every provider.

When a user invokes a /skill or /bundle, Hermes expands the turn into a
model-facing message that embeds the full skill body. Feeding that verbatim to
memory providers pollutes their stores/embeddings with prompt scaffolding
instead of what the user actually asked. The strip lives once in MemoryManager
so it covers the whole provider fan-out — not per backend.

See: agent.skill_commands.extract_user_instruction_from_skill_message and
MemoryManager._strip_skill_scaffolding.
"""

from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider
from agent.skill_commands import extract_user_instruction_from_skill_message


_SINGLE_SKILL_TURN = (
    '[IMPORTANT: The user has invoked the "skill-creator" skill, indicating they want '
    "you to follow its instructions. The full skill content is loaded below.]\n\n"
    "# Skill Creator\n\n"
    "Large skill body that must not be searched or embedded.\n\n"
    "The user has provided the following instruction alongside the skill invocation: "
    "make a skill for release triage"
)

_BUNDLE_TURN = (
    '[IMPORTANT: The user has invoked the "backend-dev" skill bundle, '
    "loading 2 skills together. Treat every skill below as active guidance for this turn.]\n\n"
    "Bundle: backend-dev\n"
    "Skills loaded: test-driven-development, code-review\n\n"
    "User instruction: fix the failing retrieval test\n\n"
    '[Loaded as part of the "backend-dev" skill bundle.]\n\n'
    "Large bundled skill body that must not be searched or embedded."
)

_BARE_SKILL_TURN = (
    '[IMPORTANT: The user has invoked the "skill-creator" skill, indicating they want '
    "you to follow its instructions. The full skill content is loaded below.]\n\n"
    "# Skill Creator\n\n"
    "Large skill body, no user instruction."
)


class _RecordingProvider(MemoryProvider):
    """Captures exactly what user text each fan-out method received."""

    _name = "recording"

    def __init__(self):
        self.prefetched = []
        self.queued = []
        self.synced = []

    @property
    def name(self) -> str:
        return self._name

    def initialize(self, session_id: str = "", **kwargs) -> None:
        pass

    def is_available(self) -> bool:
        return True

    def system_prompt_block(self) -> str:
        return ""

    def prefetch(self, query, *, session_id: str = "") -> str:
        self.prefetched.append(query)
        return ""

    def queue_prefetch(self, query, *, session_id: str = "") -> None:
        self.queued.append(query)

    def sync_turn(self, user_content, assistant_content, *, session_id: str = "", messages=None) -> None:
        self.synced.append(user_content)

    def get_tool_schemas(self):
        return []


def _manager_with_recorder():
    mgr = MemoryManager()
    provider = _RecordingProvider()
    mgr.add_provider(provider)
    return mgr, provider


class TestExtractUserInstruction:
    def test_non_string_returns_none(self):
        assert extract_user_instruction_from_skill_message(None) is None
        assert extract_user_instruction_from_skill_message(123) is None
        assert extract_user_instruction_from_skill_message([{"text": "hi"}]) is None



    def test_bundle_with_instruction(self):
        assert (
            extract_user_instruction_from_skill_message(_BUNDLE_TURN)
            == "fix the failing retrieval test"
        )




class TestMemoryManagerStripsScaffolding:

    def test_prefetch_all_skips_bare_skill(self):
        mgr, provider = _manager_with_recorder()
        result = mgr.prefetch_all(_BARE_SKILL_TURN)
        assert result == ""
        assert provider.prefetched == []

    def test_queue_prefetch_all_strips_bundle(self):
        mgr, provider = _manager_with_recorder()
        mgr.queue_prefetch_all(_BUNDLE_TURN)
        mgr.flush_pending(timeout=5.0)
        assert provider.queued == ["fix the failing retrieval test"]



    def test_sync_all_skips_bare_skill(self):
        mgr, provider = _manager_with_recorder()
        mgr.sync_all(_BARE_SKILL_TURN, "Done.")
        mgr.flush_pending(timeout=5.0)
        assert provider.synced == []

    def test_plain_message_passes_through_unchanged(self):
        mgr, provider = _manager_with_recorder()
        mgr.sync_all("what's the weather", "Sunny.")
        mgr.flush_pending(timeout=5.0)
        assert provider.synced == ["what's the weather"]


# ---------------------------------------------------------------------------
# Harness-metadata strip (2026-08-08): gateway surfaces decorate the user's
# message with per-turn scaffolding — a leading timestamp, the Discord
# [Triggering message id: ...] block, [Replying to: "..."] pointers,
# voice-channel notes, [New message] backfill markers, and a [sender] prefix.
# The backticked message id tripped the mem0 exact-token rerank bypass on
# EVERY Discord turn (W2 ratio cratered to 2%), and the wrapper's content
# tokens defeated the ack/ping specificity gate. The strip lives in
# MemoryManager so it covers the whole provider fan-out.
# ---------------------------------------------------------------------------

_WRAPPED_DISCORD_PING = (
    "[Sat 2026-08-08 15:30:26 PDT] [Triggering message id: `1535777234898657281` — use as "
    "`message_id` for reply/react/pin via the discord tools.]\n\nstatus?"
)


def test_harness_metadata_stripped_variants():
    S = MemoryManager._strip_harness_metadata
    assert S(_WRAPPED_DISCORD_PING) == "status?"
    assert S(
        '[Replying to: "earlier text with [brackets] inside"]\n\nwhat about this?'
    ) == "what about this?"
    assert S(
        '[Replying to your previous message: "old line"]\n\nyes do it'
    ) == "yes do it"
    assert S(
        "[Voice channel now: not connected to a voice channel]\n\nplay some music"
    ) == "play some music"
    # channel-context backfill: only text after the LAST [New message] survives,
    # and the group-chat sender tag is peeled.
    assert S("[history line]\nolder\n\n[New message]\n[Ace] status?") == "status?"
    # stacked wrappers in any order
    assert S(
        '[Sat 2026-08-08 09:00:00 PDT] [Replying to: "q"]\n\n'
        "[Triggering message id: `9` — use as `message_id`.]\n\nstatus update?"
    ) == "status update?"


def test_harness_strip_is_fail_safe():
    S = MemoryManager._strip_harness_metadata
    # plain messages pass through untouched
    assert S("restart plex container") == "restart plex container"
    # wrapper-only text (nothing left after strip) → return input unchanged
    only = "[Triggering message id: `5` — use as `message_id`.]"
    assert S(only) == only
    assert S("") == ""
    assert S(None) is None


def test_provider_fanout_receives_unwrapped_query():
    mgr, provider = _manager_with_recorder()
    mgr.prefetch_all(_WRAPPED_DISCORD_PING)
    mgr.queue_prefetch_all(_WRAPPED_DISCORD_PING)
    mgr.flush_pending(timeout=5.0)
    assert provider.prefetched == ["status?"]
    assert provider.queued == ["status?"]


def test_sync_all_receives_unwrapped_user_content():
    mgr, provider = _manager_with_recorder()
    mgr.sync_all(_WRAPPED_DISCORD_PING, "assistant reply")
    mgr.flush_pending(timeout=5.0)
    assert provider.synced == ["status?"]
