"""Native kimi-oauth provider (Kimi Code membership, RFC 8628 device flow).

Covers the device-flow login, rotating-refresh write-back, rotated-store
adoption by the credential pool (the #730 class), terminal-refresh
quarantine, the per-request bearer swap in build_anthropic_client, the
notional K3 pricing, 402 entitlement classification and context metadata.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import hermes_cli.auth as auth_mod
from hermes_cli.auth import (
    KIMI_OAUTH_CLIENT_ID,
    KIMI_OAUTH_DEVICE_GRANT_TYPE,
    KIMI_OAUTH_INFERENCE_BASE_URL,
    KIMI_OAUTH_TOKEN_URL,
    AuthError,
    get_kimi_oauth_auth_status,
    get_provider_auth_state,
)


def _jwt(exp: int, client_id: str = KIMI_OAUTH_CLIENT_ID, tag: str = "a") -> str:
    def enc(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{enc({'alg': 'HS256'})}.{enc({'exp': exp, 'client_id': client_id, 'jti': tag})}.sig"


def _iso(offset: float) -> str:
    return datetime.fromtimestamp(time.time() + offset, tz=timezone.utc).isoformat()


@pytest.fixture()
def home(tmp_path, monkeypatch):
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    return root


def _write_store(home: Path, state: dict, active: str = "anthropic") -> Path:
    path = home / "auth.json"
    path.write_text(json.dumps({
        "version": 1, "active_provider": active, "providers": {"kimi-oauth": state},
    }))
    return path


def _logged_in_state(*, access_ttl: float, refresh: str = "r-1", access_tag: str = "a1") -> dict:
    return {
        "provider": "kimi-oauth",
        "access_token": _jwt(int(time.time() + access_ttl), tag=access_tag),
        "refresh_token": refresh,
        "expires_at": _iso(access_ttl),
        "device_id": "11111111-2222-4333-8444-555555555555",
        "inference_base_url": KIMI_OAUTH_INFERENCE_BASE_URL,
    }


class _FakeForm:
    """Scripted replacement for ``_kimi_post_form`` that records every call."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, client, url, *, data, device_id):
        self.calls.append({"url": url, "data": dict(data), "device_id": device_id})
        status, payload = self.replies.pop(0)
        return status, payload, json.dumps(payload)


# ---------------------------------------------------------------------------
# Device-flow login
# ---------------------------------------------------------------------------


def test_device_flow_login_persists_0600_state_without_clobbering_active(home, monkeypatch):
    (home / "auth.json").write_text(json.dumps({"version": 1, "active_provider": "anthropic", "providers": {}}))
    fake = _FakeForm([
        (200, {"device_code": "dc", "user_code": "ABCD-EFGH",
               "verification_uri_complete": "https://www.kimi.com/code/authorize_device?user_code=ABCD-EFGH",
               "expires_in": 1800, "interval": 5}),
        (400, {"error": "authorization_pending"}),
        (400, {"error": "slow_down"}),
        (200, {"access_token": _jwt(int(time.time()) + 900), "refresh_token": "r-1",
               "expires_in": 900, "token_type": "Bearer", "scope": "kimi-code"}),
    ])
    monkeypatch.setattr(auth_mod, "_kimi_post_form", fake)
    sleeps = []

    state = auth_mod._kimi_oauth_login(open_browser=False, sleep=sleeps.append)

    assert sleeps == [5.0, 5.0, 10.0]  # slow_down adds 5 s
    token_call = fake.calls[-1]
    assert token_call["url"] == KIMI_OAUTH_TOKEN_URL
    assert token_call["data"] == {
        "grant_type": KIMI_OAUTH_DEVICE_GRANT_TYPE, "device_code": "dc", "client_id": KIMI_OAUTH_CLIENT_ID,
    }
    # One device id for the whole login, persisted for later calls.
    assert {c["device_id"] for c in fake.calls} == {state["device_id"]}
    stored = json.loads((home / "auth.json").read_text())
    assert stored["active_provider"] == "anthropic"
    assert stored["providers"]["kimi-oauth"]["refresh_token"] == "r-1"
    assert stored["providers"]["kimi-oauth"]["device_id"] == state["device_id"]
    assert stat.S_IMODE(os.stat(home / "auth.json").st_mode) == 0o600
    assert get_kimi_oauth_auth_status()["logged_in"] is True


def test_device_flow_denied_raises(home, monkeypatch):
    monkeypatch.setattr(auth_mod, "_kimi_post_form", _FakeForm([
        (200, {"device_code": "dc", "user_code": "X", "verification_uri": "u", "interval": 1}),
        (400, {"error": "access_denied"}),
    ]))
    with pytest.raises(AuthError) as exc:
        auth_mod._kimi_oauth_login(open_browser=False, sleep=lambda _s: None)
    assert exc.value.code == "authorization_denied"


# ---------------------------------------------------------------------------
# Refresh + rotation
# ---------------------------------------------------------------------------


def test_refresh_rotates_and_writes_back_refresh_token(home, monkeypatch):
    _write_store(home, _logged_in_state(access_ttl=30, refresh="r-old"))
    fake = _FakeForm([(200, {"access_token": _jwt(int(time.time()) + 900, tag="a2"),
                             "refresh_token": "r-new", "expires_in": 900})])
    monkeypatch.setattr(auth_mod, "_kimi_post_form", fake)

    state = auth_mod.refresh_kimi_oauth_state()

    assert fake.calls[0]["data"] == {
        "grant_type": "refresh_token", "refresh_token": "r-old", "client_id": KIMI_OAUTH_CLIENT_ID,
    }
    assert fake.calls[0]["device_id"] == "11111111-2222-4333-8444-555555555555"
    stored = json.loads((home / "auth.json").read_text())
    assert stored["providers"]["kimi-oauth"]["refresh_token"] == "r-new"
    assert stored["providers"]["kimi-oauth"]["access_token"] == state["access_token"]
    assert stored["active_provider"] == "anthropic"  # refresh never flips the active provider


def test_refresh_is_noop_while_token_is_fresh(home, monkeypatch):
    _write_store(home, _logged_in_state(access_ttl=800))
    fake = _FakeForm([])
    monkeypatch.setattr(auth_mod, "_kimi_post_form", fake)
    auth_mod.refresh_kimi_oauth_state()
    assert fake.calls == []


def test_terminal_refresh_quarantines_state(home, monkeypatch):
    _write_store(home, _logged_in_state(access_ttl=10, refresh="r-dead"))
    monkeypatch.setattr(auth_mod, "_kimi_post_form", _FakeForm([(400, {"error": "invalid_grant"})]))
    with pytest.raises(AuthError) as exc:
        auth_mod.refresh_kimi_oauth_state()
    assert exc.value.relogin_required is True
    state = get_provider_auth_state("kimi-oauth")
    assert "refresh_token" not in state and "access_token" not in state
    assert state["last_auth_error"]["code"] == "invalid_grant"
    assert get_kimi_oauth_auth_status()["logged_in"] is False


def test_token_provider_refreshes_near_expiry_and_reads_store_each_call(home, monkeypatch):
    _write_store(home, _logged_in_state(access_ttl=60, refresh="r-1", access_tag="old"))
    new_access = _jwt(int(time.time()) + 900, tag="new")
    fake = _FakeForm([(200, {"access_token": new_access, "refresh_token": "r-2", "expires_in": 900})])
    monkeypatch.setattr(auth_mod, "_kimi_post_form", fake)
    provide = auth_mod.build_kimi_oauth_token_provider()
    assert provide() == new_access
    assert provide() == new_access  # fresh now → no second POST
    assert len(fake.calls) == 1


# ---------------------------------------------------------------------------
# Credential pool: rotated single-entry store adoption (#730 class)
# ---------------------------------------------------------------------------


def test_pool_adopts_rotated_store_entry(home, monkeypatch):
    from agent.credential_pool import load_pool

    _write_store(home, _logged_in_state(access_ttl=800, refresh="r-1", access_tag="gen1"))
    pool = load_pool("kimi-oauth")
    entries = pool.entries()
    assert len(entries) == 1 and entries[0].source == "oauth"
    assert entries[0].refresh_token == "r-1"

    # Another process (the keeper) rotates the pair in auth.json.
    rotated = _logged_in_state(access_ttl=900, refresh="r-2", access_tag="gen2")
    store = json.loads((home / "auth.json").read_text())
    store["providers"]["kimi-oauth"].update(
        access_token=rotated["access_token"], refresh_token="r-2", expires_at=rotated["expires_at"],
    )
    (home / "auth.json").write_text(json.dumps(store))

    entries = load_pool("kimi-oauth").entries()
    assert len(entries) == 1, "rotation must replace the entry, not add a second one"
    assert entries[0].refresh_token == "r-2"
    assert entries[0].access_token == rotated["access_token"]


def test_pool_refresh_uses_store_authority(home, monkeypatch):
    from agent.credential_pool import load_pool

    _write_store(home, _logged_in_state(access_ttl=800, refresh="r-1"))
    fake = _FakeForm([(200, {"access_token": _jwt(int(time.time()) + 900, tag="forced"),
                             "refresh_token": "r-2", "expires_in": 900})])
    monkeypatch.setattr(auth_mod, "_kimi_post_form", fake)
    pool = load_pool("kimi-oauth")
    pool.select()
    refreshed = pool.try_refresh_current()
    assert refreshed is not None and refreshed.refresh_token == "r-2"
    assert get_provider_auth_state("kimi-oauth")["refresh_token"] == "r-2"


# ---------------------------------------------------------------------------
# Runtime + client wiring
# ---------------------------------------------------------------------------


def test_runtime_provider_resolves_anthropic_messages(home, monkeypatch):
    from hermes_cli.runtime_provider import resolve_runtime_provider

    _write_store(home, _logged_in_state(access_ttl=800))
    runtime = resolve_runtime_provider(requested="kimi-oauth")
    assert runtime["provider"] == "kimi-oauth"
    assert runtime["api_mode"] == "anthropic_messages"
    assert runtime["base_url"].rstrip("/") == KIMI_OAUTH_INFERENCE_BASE_URL


def test_build_anthropic_client_swaps_kimi_jwt_for_bearer_hook(home):
    from agent.anthropic_adapter import build_anthropic_client

    state = _logged_in_state(access_ttl=800)
    _write_store(home, state)
    client = build_anthropic_client(state["access_token"], KIMI_OAUTH_INFERENCE_BASE_URL)
    # Bearer-hook path: the SDK only holds the sentinel, never the live token.
    assert client.auth_token == "entra-id-bearer-via-http-hook"
    headers = client.default_headers
    assert headers["X-Msh-Device-Id"] == state["device_id"]
    assert headers["X-Msh-Platform"] == auth_mod.KIMI_OAUTH_MSH_PLATFORM
    assert headers["User-Agent"].startswith("HermesAgent/")


def test_build_anthropic_client_keeps_static_sk_kimi_key():
    from agent.anthropic_adapter import build_anthropic_client

    client = build_anthropic_client("sk-kimi-" + "x" * 40, KIMI_OAUTH_INFERENCE_BASE_URL)
    assert client.api_key == "sk-kimi-" + "x" * 40
    assert "X-Msh-Device-Id" not in client.default_headers


def test_foreign_jwt_is_not_swapped():
    from agent.anthropic_adapter import _kimi_oauth_token_provider_for

    assert _kimi_oauth_token_provider_for(_jwt(int(time.time()) + 900, client_id="someone-else")) is None
    assert _kimi_oauth_token_provider_for("not-a-jwt") is None


# ---------------------------------------------------------------------------
# Pricing, entitlement, metadata
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider,model", [
    ("kimi-oauth", "k3"),
    ("kimi-oauth", "k3-256k"),
    ("kimi-code", "kimi-k3"),
    ("custom:kimi-code", "kimi-k3"),
])
def test_kimi_membership_lanes_price_as_estimated_k3(provider, model):
    from decimal import Decimal

    from agent.usage_pricing import CanonicalUsage, estimate_usage_cost

    usage = CanonicalUsage(input_tokens=1_000_000, output_tokens=1_000_000, cache_read_tokens=1_000_000)
    cost = estimate_usage_cost(model, usage, provider=provider)
    assert cost.status == "estimated"
    assert cost.amount_usd == Decimal("3.00") + Decimal("15.00") + Decimal("0.30")


def test_non_k3_membership_model_stays_unpriced():
    from agent.usage_pricing import CanonicalUsage, estimate_usage_cost

    cost = estimate_usage_cost("kimi-for-coding", CanonicalUsage(input_tokens=10), provider="kimi-oauth")
    assert cost.status == "unknown"


@pytest.mark.parametrize("model", ["k3", "k3-256k", "kimi-k3", "kimi-for-coding"])
def test_kimi_ids_share_the_one_vendor_map(model):
    from agent.usage_pricing import _infer_vendor_from_model

    assert _infer_vendor_from_model(model) == "moonshotai"


def test_non_kimi_model_on_kimi_lane_is_not_routed_to_moonshot():
    from agent.usage_pricing import resolve_billing_route

    assert resolve_billing_route("gpt-5.5", provider="kimi-code").provider != "moonshotai"


def test_membership_402_is_billing_not_auth_and_falls_back():
    from agent.error_classifier import FailoverReason, classify_api_error

    class _Err(Exception):
        status_code = 402
        body = {"error": {"message": "We're unable to verify your membership benefits at this time"}}

    result = classify_api_error(
        _Err("We're unable to verify your membership benefits at this time"),
        provider="kimi-oauth", model="k3",
    )
    assert result.reason == FailoverReason.billing
    assert result.retryable is False
    assert result.should_fallback is True


def test_membership_model_context_lengths():
    from agent.model_metadata import _endpoint_scoped_context_length

    assert _endpoint_scoped_context_length("k3", KIMI_OAUTH_INFERENCE_BASE_URL) == 1_048_576
    assert _endpoint_scoped_context_length("k3-256k", KIMI_OAUTH_INFERENCE_BASE_URL) == 262_144


# ---------------------------------------------------------------------------
# FleetReview #1392 follow-ups (t_ca39d148)
# ---------------------------------------------------------------------------


def _account_jwt(exp: int, *, user_id: str, tag: str, client_id: str = KIMI_OAUTH_CLIENT_ID) -> str:
    def enc(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    claims = {"exp": exp, "client_id": client_id, "jti": tag, "user_id": user_id, "sub": user_id}
    return f"{enc({'alg': 'HS256'})}.{enc(claims)}.sig"


def test_login_label_rides_in_the_single_login_write(home, monkeypatch):
    (home / "auth.json").write_text(json.dumps({"version": 1, "active_provider": "anthropic", "providers": {}}))
    monkeypatch.setattr(auth_mod, "_kimi_post_form", _FakeForm([
        (200, {"device_code": "dc", "user_code": "X", "verification_uri": "u", "interval": 1}),
        (200, {"access_token": _jwt(int(time.time()) + 900), "refresh_token": "r-1", "expires_in": 900}),
    ]))
    writes = []
    real_write = auth_mod._kimi_oauth_write_state

    def _spy(state, **kw):
        writes.append(dict(state))
        return real_write(state, **kw)

    monkeypatch.setattr(auth_mod, "_kimi_oauth_write_state", _spy)
    auth_mod._kimi_oauth_login(open_browser=False, sleep=lambda _s: None, label="work")
    assert len(writes) == 1 and writes[0]["label"] == "work"
    assert get_provider_auth_state("kimi-oauth")["label"] == "work"


def test_auth_add_label_does_not_restore_a_rotated_refresh_token(home, monkeypatch):
    import hermes_cli.auth_commands as auth_commands

    rotated = _logged_in_state(access_ttl=900, refresh="r-rotated", access_tag="gen2")

    def _fake_login(*, open_browser, timeout_seconds, label=None):
        written = _logged_in_state(access_ttl=900, refresh="r-spent", access_tag="gen1")
        if label:
            written["label"] = label
        # The login persisted `written`; another process then rotated the pair.
        _write_store(home, dict(rotated, label=written.get("label")))
        return written

    monkeypatch.setattr(auth_mod, "_kimi_oauth_login", _fake_login)

    class _Args:
        provider = "kimi-oauth"
        auth_type = "oauth"
        label = "work"
        no_browser = True
        timeout = None

    auth_commands.auth_add_command(_Args())
    stored = get_provider_auth_state("kimi-oauth")
    assert stored["refresh_token"] == "r-rotated"
    assert stored["label"] == "work"


def test_foreign_account_kimi_jwt_is_not_swapped_for_the_stored_login(home):
    from agent.anthropic_adapter import _kimi_oauth_token_provider_for, build_anthropic_client

    exp = int(time.time()) + 900
    state = _logged_in_state(access_ttl=800)
    state["access_token"] = _account_jwt(exp, user_id="acct-local", tag="mine")
    _write_store(home, state)
    foreign = _account_jwt(exp, user_id="acct-other", tag="theirs")

    assert _kimi_oauth_token_provider_for(foreign) is None
    client = build_anthropic_client(foreign, KIMI_OAUTH_INFERENCE_BASE_URL)
    assert client.auth_token != "entra-id-bearer-via-http-hook"
    assert _kimi_oauth_token_provider_for(state["access_token"]) is not None


def test_forged_jwt_with_matching_account_claims_is_not_swapped(home):
    from agent.anthropic_adapter import _kimi_oauth_token_provider_for

    exp = int(time.time()) + 900
    state = _logged_in_state(access_ttl=800)
    state["access_token"] = _account_jwt(exp, user_id="acct-local", tag="mine")
    _write_store(home, state)
    # Unsigned payload copying the public client_id and the local user_id/sub.
    forged = _account_jwt(exp, user_id="acct-local", tag="forged")
    assert _kimi_oauth_token_provider_for(forged) is None


def test_earlier_rotation_of_the_stored_login_is_still_swapped(home, monkeypatch):
    from agent.anthropic_adapter import _kimi_oauth_token_provider_for

    old = _logged_in_state(access_ttl=30, refresh="r-1", access_tag="gen1")
    _write_store(home, old)
    monkeypatch.setattr(auth_mod, "_kimi_post_form", _FakeForm([
        (200, {"access_token": _jwt(int(time.time()) + 900, tag="gen2"),
               "refresh_token": "r-2", "expires_in": 900}),
    ]))
    auth_mod.refresh_kimi_oauth_state()
    stored = get_provider_auth_state("kimi-oauth")
    assert stored["access_token"] != old["access_token"]
    assert _kimi_oauth_token_provider_for(old["access_token"]) is not None
    assert old["access_token"] not in json.dumps(stored["issued_access_sha256"])


def test_kimi_jwt_without_a_stored_login_keeps_the_static_path(home):
    from agent.anthropic_adapter import _kimi_oauth_token_provider_for, build_anthropic_client

    token = _account_jwt(int(time.time()) + 900, user_id="acct-x", tag="t")
    assert _kimi_oauth_token_provider_for(token) is None
    client = build_anthropic_client(token, KIMI_OAUTH_INFERENCE_BASE_URL)
    assert client.auth_token != "entra-id-bearer-via-http-hook"


def test_auxiliary_resolver_builds_a_kimi_oauth_client(home):
    from agent.auxiliary_client import AnthropicAuxiliaryClient, resolve_provider_client

    _write_store(home, _logged_in_state(access_ttl=800))
    client, model = resolve_provider_client("kimi-oauth", "k3")
    assert isinstance(client, AnthropicAuxiliaryClient)
    assert model == "k3"
    assert client._real_client.auth_token == "entra-id-bearer-via-http-hook"
    assert client.base_url.rstrip("/") == KIMI_OAUTH_INFERENCE_BASE_URL


def test_auxiliary_resolver_without_kimi_login_returns_none(home):
    from agent.auxiliary_client import resolve_provider_client

    (home / "auth.json").write_text(json.dumps({"version": 1, "providers": {}}))
    assert resolve_provider_client("kimi-oauth", "k3") == (None, None)


def test_auxiliary_resolver_uses_explicit_key_without_a_stored_login(home):
    from agent.auxiliary_client import AnthropicAuxiliaryClient, resolve_provider_client

    (home / "auth.json").write_text(json.dumps({"version": 1, "providers": {}}))
    key = _account_jwt(int(time.time()) + 900, user_id="acct-x", tag="t")
    client, model = resolve_provider_client("kimi-oauth", "k3", explicit_api_key=key)
    assert isinstance(client, AnthropicAuxiliaryClient)
    assert model == "k3"
    assert client._real_client.auth_token != "entra-id-bearer-via-http-hook"
    assert client.base_url.rstrip("/") == KIMI_OAUTH_INFERENCE_BASE_URL


def _capture_messages_headers(client):
    import httpx

    seen = {}

    def _handler(request):
        seen.update(request.headers)
        return httpx.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "k3",
            "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
            "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1},
        })

    # Swap the transport in place: with_options() would rebuild the client
    # and re-read ANTHROPIC_API_KEY from the env.
    client._client = httpx.Client(transport=httpx.MockTransport(_handler))
    client.messages.create(model="k3", max_tokens=8, messages=[{"role": "user", "content": "hi"}])
    return seen


def test_unmanaged_kimi_jwt_is_sent_as_bearer_not_x_api_key(home, monkeypatch):
    from agent.auxiliary_client import resolve_provider_client

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env-leak")
    (home / "auth.json").write_text(json.dumps({"version": 1, "providers": {}}))
    key = _account_jwt(int(time.time()) + 900, user_id="acct-x", tag="t")
    client, _model = resolve_provider_client("kimi-oauth", "k3", explicit_api_key=key)
    seen = _capture_messages_headers(client._real_client)
    assert seen["authorization"] == f"Bearer {key}"
    assert "x-api-key" not in seen


def test_static_sk_kimi_key_still_sent_as_x_api_key():
    from agent.anthropic_adapter import build_anthropic_client

    key = "sk-kimi-" + "x" * 40
    seen = _capture_messages_headers(build_anthropic_client(key, KIMI_OAUTH_INFERENCE_BASE_URL))
    assert seen["x-api-key"] == key
    assert "authorization" not in seen


def _device_jwt(exp: int, *, device_id: str, tag: str) -> str:
    def enc(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    claims = {"exp": exp, "client_id": KIMI_OAUTH_CLIENT_ID, "jti": tag, "device_id": device_id}
    return f"{enc({'alg': 'HS256'})}.{enc(claims)}.sig"


def test_device_id_claim_alone_is_not_trusted(home):
    from agent.anthropic_adapter import _kimi_oauth_token_provider_for

    state = _logged_in_state(access_ttl=800)
    _write_store(home, state)
    forged = _device_jwt(int(time.time()) - 3600, device_id=state["device_id"], tag="forged")
    assert _kimi_oauth_token_provider_for(forged) is None


def _relogin(monkeypatch, new_access):
    monkeypatch.setattr(auth_mod, "_kimi_post_form", _FakeForm([
        (200, {"device_code": "dc", "user_code": "X", "verification_uri": "u", "interval": 1}),
        (200, {"access_token": new_access, "refresh_token": "r-2", "expires_in": 900}),
    ]))
    auth_mod._kimi_oauth_login(open_browser=False, sleep=lambda _s: None)


def test_same_account_relogin_keeps_issued_token_history(home, monkeypatch):
    from agent.anthropic_adapter import _kimi_oauth_token_provider_for

    exp = int(time.time()) + 900
    old = _logged_in_state(access_ttl=800, refresh="r-1")
    old["access_token"] = _account_jwt(exp, user_id="acct-a", tag="pre-login")
    old["issued_access_sha256"] = ["f" * 64]
    _write_store(home, old)
    _relogin(monkeypatch, _account_jwt(exp, user_id="acct-a", tag="post-login"))
    history = get_provider_auth_state("kimi-oauth")["issued_access_sha256"]
    assert "f" * 64 in history
    assert _kimi_oauth_token_provider_for(old["access_token"]) is not None


def test_other_account_relogin_drops_issued_token_history(home, monkeypatch):
    from agent.anthropic_adapter import _kimi_oauth_token_provider_for

    exp = int(time.time()) + 900
    old = _logged_in_state(access_ttl=800, refresh="r-1")
    old["access_token"] = _account_jwt(exp, user_id="acct-a", tag="pre-login")
    old["issued_access_sha256"] = ["f" * 64]
    _write_store(home, old)
    _relogin(monkeypatch, _account_jwt(exp, user_id="acct-b", tag="post-login"))
    assert get_provider_auth_state("kimi-oauth")["issued_access_sha256"] == []
    assert _kimi_oauth_token_provider_for(old["access_token"]) is None


def test_aux_with_options_copy_stays_bearer_only(home, monkeypatch):
    import httpx

    from agent.auxiliary_client import resolve_provider_client

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env-leak")
    (home / "auth.json").write_text(json.dumps({"version": 1, "providers": {}}))
    key = _account_jwt(int(time.time()) + 900, user_id="acct-x", tag="t")
    client, _model = resolve_provider_client("kimi-oauth", "k3", explicit_api_key=key)
    seen = {}

    events = [
        ("message_start", {"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": "k3", "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 0}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": "ok"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                           "usage": {"output_tokens": 1}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    sse = "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events)

    def _handler(request):
        seen.update(request.headers)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse.encode())

    client._real_client._client = httpx.Client(transport=httpx.MockTransport(_handler))
    # timeout forces the adapter's with_options() copy.
    client.chat.completions.create(
        model="k3", messages=[{"role": "user", "content": "hi"}], max_tokens=8, timeout=5,
    )
    assert seen["authorization"] == f"Bearer {key}"
    assert "x-api-key" not in seen
