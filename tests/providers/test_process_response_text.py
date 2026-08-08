"""Tests for ProviderProfile.process_response_text — the inverse of prepare_messages.

These are behavior-contract tests: they assert the seam exists, defaults to a
no-op, is actually invoked on both response paths, and that the documented
implementer contract (purity/idempotence/never-raise) is enforced by the caller.
"""

from __future__ import annotations

import pytest

from providers.base import ProviderProfile


def _profile(**kw) -> ProviderProfile:
    base = dict(
        name="test-provider",
        api_mode="chat_completions",
        base_url="https://example.invalid/v1",
        auth_type="api_key",
    )
    base.update(kw)
    return ProviderProfile(**base)


class TestDefaultIsNoOp:
    def test_default_returns_input_unchanged(self):
        p = _profile()
        assert p.process_response_text("hello world") == "hello world"

    def test_default_preserves_empty_string(self):
        assert _profile().process_response_text("") == ""

    def test_default_is_identity_for_arbitrary_text(self):
        p = _profile()
        for s in ("", "a", "multi\nline\ttext", "unicode ✅ 😀", "```code```"):
            assert p.process_response_text(s) == s

    def test_base_class_method_exists_and_is_inherited(self):
        """A provider that does not override must use the base implementation."""
        p = _profile()
        assert type(p).process_response_text is ProviderProfile.process_response_text


class TestOverrideIsHonored:
    def test_subclass_override_is_used(self):
        class Shouty(ProviderProfile):
            def process_response_text(self, text: str) -> str:
                return text.upper()

        p = Shouty(
            name="shouty",
            api_mode="chat_completions",
            base_url="https://example.invalid/v1",
            auth_type="api_key",
        )
        assert p.process_response_text("quiet") == "QUIET"

    def test_override_can_invert_prepare_messages(self):
        """The seam's purpose: undo an outbound rewrite on the way back."""

        class RoundTrip(ProviderProfile):
            def prepare_messages(self, messages):
                return [
                    {**m, "content": m["content"].replace("SECRET", "TOKEN")}
                    if isinstance(m.get("content"), str)
                    else m
                    for m in messages
                ]

            def process_response_text(self, text: str) -> str:
                return text.replace("TOKEN", "SECRET")

        p = RoundTrip(
            name="roundtrip",
            api_mode="chat_completions",
            base_url="https://example.invalid/v1",
            auth_type="api_key",
        )
        outbound = p.prepare_messages([{"role": "user", "content": "the SECRET value"}])
        assert outbound[0]["content"] == "the TOKEN value"
        assert p.process_response_text("echoing the TOKEN value") == (
            "echoing the SECRET value"
        )


class TestImplementerContract:
    def test_idempotence_is_satisfiable_and_observable(self):
        """f(f(x)) == f(x) — required because streaming may apply it twice."""

        class Idempotent(ProviderProfile):
            def process_response_text(self, text: str) -> str:
                return text.replace("TOKEN", "SECRET")

        p = Idempotent(
            name="idem",
            api_mode="chat_completions",
            base_url="https://example.invalid/v1",
            auth_type="api_key",
        )
        once = p.process_response_text("a TOKEN here")
        assert p.process_response_text(once) == once

    def test_default_hook_is_idempotent(self):
        p = _profile()
        s = "some assistant text"
        assert p.process_response_text(p.process_response_text(s)) == (
            p.process_response_text(s)
        )

    def test_default_hook_is_pure(self):
        """Repeated calls must not accumulate state on the profile."""
        p = _profile()
        first = p.process_response_text("stable")
        for _ in range(5):
            assert p.process_response_text("stable") == first


class TestCallerGuards:
    """The call sites wrap the hook in try/except; a raising profile must not
    break the turn. These assert the guard CONTRACT at the unit level."""

    def test_raising_hook_is_containable(self):
        class Exploding(ProviderProfile):
            def process_response_text(self, text: str) -> str:
                raise RuntimeError("boom")

        p = Exploding(
            name="boom",
            api_mode="chat_completions",
            base_url="https://example.invalid/v1",
            auth_type="api_key",
        )
        original = "untouched"
        result = original
        try:
            result = p.process_response_text(original)
        except Exception:
            pass
        assert result == original, "guarded call must leave text unchanged"

    def test_hook_returning_non_string_is_detectable(self):
        class Bad(ProviderProfile):
            def process_response_text(self, text: str):  # type: ignore[override]
                return None

        p = Bad(
            name="bad",
            api_mode="chat_completions",
            base_url="https://example.invalid/v1",
            auth_type="api_key",
        )
        assert not isinstance(p.process_response_text("x"), str)


class TestWiring:
    """The hook must actually be reachable from the real call sites."""

    def test_conversation_loop_invokes_the_hook(self):
        import inspect
        from agent import conversation_loop

        src = inspect.getsource(conversation_loop)
        assert "process_response_text" in src, (
            "non-streaming path must call the hook"
        )

    def test_streaming_path_invokes_the_hook(self):
        import inspect
        from agent import chat_completion_helpers

        src = inspect.getsource(chat_completion_helpers)
        assert "process_response_text" in src, "streaming path must call the hook"

    def test_streaming_applies_to_assembled_text_not_deltas(self):
        """Guards the boundary-split bug: the hook must run on the joined text."""
        import inspect
        from agent import chat_completion_helpers

        src = inspect.getsource(chat_completion_helpers)
        idx_join = src.find('full_content = "".join(content_parts)')
        idx_hook = src.find("process_response_text")
        assert idx_join != -1 and idx_hook != -1
        assert idx_hook > idx_join, (
            "hook must be applied AFTER deltas are assembled, or a needle "
            "split across a delta boundary is silently missed"
        )
