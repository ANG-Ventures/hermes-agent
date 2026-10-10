"""The installed Anthropic SDK is the one uv.lock pins, and the transport can build a client on it.

anthropic 1.x moved its HTTP stack to ``httpx2`` and rejects the ``httpx.Timeout`` /
``httpx.Client`` objects ``agent.anthropic_adapter`` hands it ("httpx.Timeout is from the httpx
package, but this SDK uses httpx2"). A venv that floats past the lock (an unpinned
``uv pip install anthropic``, t_7e82c050) then fails every real client construction, and the
symptom surfaces as a scatter of unrelated-looking e2e failures. These two tests name the cause.
"""

import tomllib
from importlib.metadata import version
from pathlib import Path

import pytest

pytest.importorskip("anthropic")

from agent.anthropic_adapter import build_anthropic_client  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def _locked_version(name: str) -> str:
    locked = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8-sig"))
    versions = {row["version"] for row in locked["package"] if row["name"] == name}
    assert len(versions) == 1, f"uv.lock resolves {name} to {sorted(versions)}"
    return versions.pop()


def test_installed_anthropic_sdk_is_the_locked_version():
    installed, locked = version("anthropic"), _locked_version("anthropic")
    assert installed == locked, (
        f"anthropic {installed} is installed but uv.lock pins {locked}; install from the lock "
        "(`uv sync --frozen --extra anthropic` or `--group test`), never a bare `pip install anthropic`"
    )


@pytest.mark.parametrize("credential", ["static", "token_provider"])
def test_transport_builds_a_client_on_the_installed_sdk(monkeypatch, credential):
    """Both constructor paths: kwargs ``timeout`` (static key) and a hand-built ``http_client``
    (Entra bearer hook). Each must be accepted by the SDK that is actually installed."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    api_key = "sk-ant-api03-gate" if credential == "static" else (lambda: "entra-gate-token")
    client = build_anthropic_client(api_key, "https://example.invalid/anthropic", timeout=30)
    assert client.max_retries == 0
    assert client.timeout.read == 30.0
