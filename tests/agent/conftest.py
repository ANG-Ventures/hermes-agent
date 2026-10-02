"""Fast-path fixtures shared across tests/agent/.

Many tests in this directory exercise the retry/backoff paths in the
agent loop. Production code uses ``jittered_backoff(base_delay=5.0)``
with a ``while time.time() < sleep_end`` loop — a single retry test
spends 5+ seconds of real wall-clock time on backoff waits.

Mocking ``jittered_backoff`` to return 0.0 collapses the while-loop
to a no-op (``time.time() < time.time() + 0`` is false immediately),
which handles the most common case without touching ``time.sleep``.

We deliberately DO NOT mock ``time.sleep`` here — some tests
(test_interrupt_propagation, test_primary_runtime_restore, etc.) use
the real ``time.sleep`` for threading coordination or assert that it
was called with specific values. Tests that want to additionally
fast-path direct ``time.sleep(N)`` calls in production code should
monkeypatch ``run_agent.time.sleep`` locally (see
``test_anthropic_error_handling.py`` for the pattern).

Hermeticity (fork): The global hermetic fixture in ``tests/conftest.py`` blanks credential env
vars and redirects ``HERMES_HOME``, but it deliberately does NOT redirect
``HOME`` (doing so broke CI subprocesses). That leaves one machine-specific
leak for ``test_anthropic_adapter.py``:

``agent.anthropic_adapter.read_claude_code_credentials`` reads the macOS
Keychain entry ``"Claude Code-credentials"`` (via ``security
find-generic-password``) BEFORE the ``~/.claude/.credentials.json`` file.
The adapter tests stub ``Path.home`` for the *file* source, but nothing
intercepts the Keychain source. On a developer Mac running Claude Code
>= 2.1.114 the real OAuth token leaks past the ``Path.home`` stub and fails
~14 assertions that expect "no creds resolved". CI (Linux, no Keychain) and
Macs without Claude Code never see it — so it's invisible until you run the
suite on a logged-in Mac (e.g. the gateway host).

Default the Keychain source to empty for every agent test; the handful of
tests that exercise Keychain resolution set it explicitly.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _fresh_structured_output_memo(monkeypatch):
    """The aux client remembers routes that rejected ``response_format`` for the whole process;
    a rejection recorded by one test must not strip the field from the next test's request."""
    from agent import auxiliary_structured_output
    monkeypatch.setattr(auxiliary_structured_output, "_REJECTED_ROUTES", set())


@pytest.fixture(autouse=True)
def _fast_retry_backoff(request, monkeypatch):
    """Short-circuit retry backoff for all tests in this directory.

    Tests that assert on the real backoff value opt out with
    ``@pytest.mark.real_retry_backoff``.
    """
    if request.node.get_closest_marker("real_retry_backoff"):
        return
    # The agent.turn_* retry paths import ``jittered_backoff`` lazily from
    # ``agent.retry_utils``; patch it there so rate-limit / invalid-response /
    # server-error retries don't burn real wall-clock seconds.
    from agent import retry_utils as _retry_utils
    monkeypatch.setattr(_retry_utils, "jittered_backoff", lambda *a, **k: 0.0)



@pytest.fixture(autouse=True)
def _block_real_claude_keychain(request, monkeypatch):
    """Make anthropic_adapter see a non-Darwin platform by default so the
    macOS Keychain Claude-Code credential read can't fire.

    Honours the same ``allow_macos_keychain`` opt-out as the suite-wide
    ``_neutralize_macos_keychain_creds`` guard: a test that exercises the
    Keychain parser/mirror under ``platforms("macos")`` with ``subprocess.run``
    mocked needs the real platform answer (upstream's
    ``TestFindClaudeCodeKeychainItem`` / mirror tests, 2026-10-01 parity sync).

    ``read_claude_code_credentials`` early-returns when
    ``platform.system() != "Darwin"``, so defaulting the adapter's view of
    the platform to "Linux" blocks the real ``security find-generic-password``
    call without touching the credential-resolution functions themselves.

    Tests that *exercise* Keychain behaviour re-patch
    ``agent.anthropic_adapter.platform.system`` to ``"Darwin"`` (and mock
    ``subprocess.run``) inside the test body — those context-manager patches
    apply after this fixture and win, so this does not interfere with them.
    It only neutralises the ambient real-Keychain leak on a dev Mac.
    """
    if request.node.get_closest_marker("allow_macos_keychain"):
        return
    # Upstream moved the Keychain readers into agent.anthropic_credentials; patch the
    # module that actually calls platform.system() (a module-level ``import platform``
    # binds the stdlib module, so this patch is process-wide for the test).
    try:
        import agent.anthropic_credentials as _ac
    except Exception:
        return
    monkeypatch.setattr(_ac.platform, "system", lambda: "Linux", raising=False)


@pytest.fixture(autouse=True)
def _reset_summary_refusal_latch():
    """The compressor's safeguard-refusal latch is process-wide by design;
    clear it so one test's refused (route, content) pair cannot suppress a
    send in another test that reuses the same fixture text."""
    try:
        from agent.context_compressor import _SUMMARY_REFUSALS
    except Exception:
        yield
        return
    _SUMMARY_REFUSALS.clear()
    yield
    _SUMMARY_REFUSALS.clear()


# ---------------------------------------------------------------------------
# Shared AIAgent fixtures (moved here from the former monolithic
# test_run_agent.py when it was split into per-theme files). Fixtures in a
# conftest auto-inject into every test module in this directory by name.
# ---------------------------------------------------------------------------
from unittest.mock import MagicMock, patch  # noqa: E402

from run_agent import AIAgent  # noqa: E402

from tests.run_agent._run_agent_helpers import _make_tool_defs  # noqa: E402,F401


@pytest.fixture()
def agent():
    """Minimal AIAgent with mocked OpenAI client and tool loading."""
    with (
        patch(
            "model_tools.get_tool_definitions", return_value=_make_tool_defs("web_search")
        ),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        a.client = MagicMock()
        return a


@pytest.fixture()
def agent_with_memory_tool():
    """Agent whose valid_tool_names includes 'memory'."""
    with (
        patch(
            "model_tools.get_tool_definitions",
            return_value=_make_tool_defs("web_search", "memory"),
        ),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-k...7890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        a.client = MagicMock()
        return a
