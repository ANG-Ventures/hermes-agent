"""Public consumers must not leak owner failures or hand out stale grants."""

import base64
import json
import socket
import time

import pytest

import agent.codex_owner as owner
import agent.credential_pool as cp
import hermes_cli.auth as auth

P = "openai-codex"


def row(ident, *, expired=False, source="manual:device_code"):
    payload = base64.urlsafe_b64encode(json.dumps({
        "exp": int(time.time()) + (-3600 if expired else 86400),
        "sub": ident,
    }).encode()).decode().rstrip("=")
    return dict(id=ident, source=source, auth_type="oauth", priority=0,
                access_token=f"e30.{payload}.sig", refresh_token=f"refresh-{ident}")


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "auth.json"
    monkeypatch.setattr(auth, "_auth_file_path", lambda: path)
    monkeypatch.setattr(auth, "_auth_lock_path", lambda: tmp_path / "auth.lock")
    monkeypatch.setattr(auth, "_global_auth_file_path", lambda: None)
    monkeypatch.setattr(cp, "_load_config_safe", lambda: {})
    monkeypatch.setattr(auth, "_probe_codex_quota_restored", lambda *a, **kw: False)

    def deny(*args, **kwargs):
        raise AssertionError("real network forbidden")

    monkeypatch.setattr(socket.socket, "connect", deny)
    return path


def put(path, rows):
    path.write_text(json.dumps({"version": 1, "credential_pool": {P: rows}}))


def mock_http(monkeypatch, status):
    calls = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, **kwargs):
            calls.append(kwargs["data"]["refresh_token"])
            return auth.httpx.Response(status, json={"error": "invalid_grant"})

    monkeypatch.setattr(auth.httpx, "Client", Client)
    return calls


@pytest.mark.parametrize("consumer", ["select", "acquire_lease"])
@pytest.mark.parametrize("status", [400, 429, 500])
def test_deferred_failed_refresh_does_not_poison_healthy_row(store, monkeypatch, consumer, status):
    put(store, [row("bad", expired=True), row("healthy")])
    calls = mock_http(monkeypatch, status)
    pool = cp.load_pool(P)
    for _ in range(2):
        selected = getattr(pool, consumer)()
        assert (selected.id if consumer == "select" else selected) == "healthy"
    assert calls == ["refresh-bad"]


@pytest.mark.parametrize("source", ["manual:device_code", "device_code"])
@pytest.mark.parametrize("change", ["receipt", "dead", "removed", "replaced"])
@pytest.mark.parametrize("consumer", ["select", "acquire_lease", "explicit_lease", "rotate"])
def test_resident_consumers_reconcile_owner_before_use(store, source, change, consumer):
    bad = row("bad", source=source)
    healthy = row("healthy")
    put(store, [bad, healthy, row("failed")])
    pool = cp.load_pool(P)
    if change == "receipt":
        owner._reserve(store, pool.entries()[0])
    elif change == "dead":
        put(store, [dict(bad, last_status="dead"), healthy])
    elif change == "removed":
        put(store, [healthy])
    else:
        put(store, [dict(bad, source="manual:replacement"), healthy])
    if consumer == "explicit_lease":
        assert pool.acquire_lease("bad") is None
        assert "bad" not in pool._active_leases
    elif consumer == "rotate":
        selected = pool.mark_exhausted_and_rotate(status_code=401, credential_id="failed")
        assert selected is not None and selected.id == "healthy"
    else:
        selected = getattr(pool, consumer)()
        assert (selected.id if consumer == "select" else selected) == "healthy"


def test_preloaded_dead_singleton_receipt_does_not_poison_selection(store):
    put(store, [dict(row("bad", source="device_code"), last_status="dead"), row("healthy")])
    pool = cp.load_pool(P)
    owner._reserve(store, pool.entries()[0])
    assert pool.select().id == "healthy"


@pytest.mark.parametrize("consumer", ["select", "acquire_lease"])
def test_public_selection_preserves_programming_errors(store, monkeypatch, consumer):
    put(store, [row("bad", expired=True), row("healthy")])
    pool = cp.load_pool(P)

    def broken(*args, **kwargs):
        raise TypeError("programming error")

    monkeypatch.setattr(owner, "refresh", broken)
    with pytest.raises(TypeError, match="programming error"):
        getattr(pool, consumer)()


def test_private_refresh_still_raises_transaction_auth_error(store, monkeypatch):
    put(store, [row("bad", expired=True)])
    calls = mock_http(monkeypatch, 400)
    pool = cp.load_pool(P)
    with pytest.raises(auth.AuthError):
        pool._refresh_entry(pool.entries()[0], force=True)
    assert calls == ["refresh-bad"]


@pytest.mark.parametrize("consumer", ["select", "acquire_lease", "explicit_lease"])
def test_peer_committed_generation_is_adopted_before_use(store, consumer):
    put(store, [row("good")])
    pool = cp.load_pool(P)
    old = pool.entries()[0]
    owner._reserve(store, old)
    fresh = row("new")
    fresh["id"] = "good"
    put(store, [fresh])
    if consumer == "select":
        selected = pool.select()
    else:
        assert pool.acquire_lease("good" if consumer == "explicit_lease" else None) == "good"
        selected = pool.current()
    assert selected.access_token == fresh["access_token"]
    assert selected.refresh_token == fresh["refresh_token"]


@pytest.mark.parametrize("consumer", ["select", "acquire_lease"])
def test_candidate_reserved_during_deferred_refresh_is_not_returned(store, monkeypatch, consumer):
    put(store, [row("expired", expired=True), row("cached"), row("healthy")])
    pool = cp.load_pool(P)

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def post(self, url, **kwargs):
            owner._reserve(store, pool.entries()[1])
            return auth.httpx.Response(400, json={"error": "invalid_grant"})

    monkeypatch.setattr(auth.httpx, "Client", Client)
    selected = getattr(pool, consumer)()
    assert (selected.id if consumer == "select" else selected) == "healthy"
    assert "cached" not in pool._active_leases


def test_owner_reconciliation_preserves_resident_least_used_counters(store):
    put(store, [row("first"), row("second")])
    pool = cp.load_pool(P)
    pool._strategy = cp.STRATEGY_LEAST_USED
    assert pool.select().id == "first"
    assert pool.select().id == "second"
