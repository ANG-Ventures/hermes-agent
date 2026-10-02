"""Fixtures for the fork-only tests still under tests/run_agent/.

Upstream d10bb2ab6f moved this directory's conftest (the ``agent`` /
``agent_with_memory_tool`` fixtures and the retry-backoff fast path) into
tests/agent/conftest.py. Re-export them here so the fork files that were not
moved keep the same fixtures, with no second copy to drift.

The autouse fixtures are underscore-prefixed, so ``import *`` does NOT carry
them (parity 2026-10-01: the retry tests burned real backoff seconds and the
Retry-After fallback case saw a 2.4s jittered wait instead of the stubbed 0.0);
name them explicitly.
"""

from tests.agent.conftest import *  # noqa: F401,F403
from tests.agent.conftest import (  # noqa: F401 - autouse fixtures, not exported by *
    _block_real_claude_keychain,
    _fast_retry_backoff,
    _fresh_structured_output_memo,
    _reset_summary_refusal_latch,
)
