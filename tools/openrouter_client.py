"""OpenRouter API key probe + per-credential async client shared by Hermes tools (MoA)."""

import hashlib
import os

# One client per OpenRouter credential, never one per process: in a multiplexed
# gateway each profile has its own secret scope, and a single cached client would
# send every later profile's prompts under the FIRST profile's key (Backfill C3).
_clients: dict = {}


def _current_key() -> str:
    """Return the ACTIVE profile's OpenRouter key.

    Fail-closed under multiplex: ``UnscopedSecretError`` propagates. The only
    consumer (MoA) runs in-turn, so an unscoped call there is a missing scope,
    not a startup probe, and ``os.environ`` would be the host profile's key.
    """
    try:
        from agent.secret_scope import get_secret
    except Exception:
        return os.getenv("OPENROUTER_API_KEY") or ""
    return get_secret("OPENROUTER_API_KEY") or ""


def get_async_client():
    """Return an async OpenAI-compatible client for OpenRouter, for the ACTIVE
    profile's credential.

    Clients are created lazily and cached per credential (keyed by a digest, so a
    later profile never reuses an earlier profile's client). Uses the centralized
    provider router for auth and client construction.
    Raises ValueError if OPENROUTER_API_KEY is not set.
    """
    key = _current_key()
    if not key:
        raise ValueError("OPENROUTER_API_KEY environment variable not set")
    fp = hashlib.sha256(key.encode()).hexdigest()
    client = _clients.get(fp)
    if client is None:
        from agent.auxiliary_client import resolve_provider_client
        client, _model = resolve_provider_client("openrouter", async_mode=True)
        if client is None:
            raise ValueError("OPENROUTER_API_KEY environment variable not set")
        _clients[fp] = client
    return client


def check_api_key() -> bool:
    """Return True if OPENROUTER_API_KEY is present.

    Scope-aware: an installed profile secret scope is authoritative under
    multiplex; unscoped CLI probes fall back to the plain env read. Any other
    scope failure propagates -- a failed scoped read must never silently
    borrow another profile's key from ``os.environ``.
    """
    from agent.secret_scope import UnscopedSecretError, get_secret

    try:
        return bool(get_secret("OPENROUTER_API_KEY"))
    except UnscopedSecretError:
        return bool(os.getenv("OPENROUTER_API_KEY"))
