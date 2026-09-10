"""Real filesystem/loader regressions; HTTP is the only simulated boundary."""

import json
from dataclasses import replace

import pytest

import agent.credential_pool as cp
import hermes_cli.auth as auth

P = "openai-codex"


def row(token="old", source="manual:device_code", ident="row"):
    return dict(
        id=ident,
        source=source,
        auth_type="oauth",
        priority=0,
        access_token="access-" + token,
        refresh_token=token,
        label="test",
    )


@pytest.fixture
def stores(tmp_path, monkeypatch):
    root = tmp_path / "root"
    profile = root / "profiles" / "a"
    profile.mkdir(parents=True)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setattr(auth, "_auth_file_path", lambda: profile / "auth.json")
    monkeypatch.setattr(auth, "_auth_lock_path", lambda: profile / "auth.lock")
    monkeypatch.setattr(auth, "_global_auth_file_path", lambda: root / "auth.json")
    monkeypatch.setattr(cp, "_load_config_safe", lambda: {})
    return root / "auth.json", profile / "auth.json"


def put(path, rows, **extra):
    path.write_text(json.dumps(dict(version=1, credential_pool={P: rows}, **extra)))


def rows(path):
    return json.loads(path.read_text())["credential_pool"][P]


@pytest.fixture
def posts(monkeypatch):
    calls = []

    class Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, url, **kw):
            calls.append(kw["data"]["refresh_token"])
            return auth.httpx.Response(
                200, json=dict(access_token="access-new", refresh_token="new")
            )

    monkeypatch.setattr(auth.httpx, "Client", Client)
    return calls


def test_inherited_waiter_adopts_and_never_creates_shadow(stores, posts):
    root, local = stores
    put(root, [row()])
    first, second = cp.load_pool(P), cp.load_pool(P)
    winner = first._refresh_entry(first._entries[0], force=True)
    waiter = second._refresh_entry(second._entries[0], force=True)
    assert winner.refresh_token == waiter.refresh_token == "new"
    assert posts == ["old"]
    for _ in range(3):
        pool = cp.load_pool(P)
        pool._mark_exhausted(pool._entries[0], 429)
    assert rows(root)[0]["refresh_token"] == "new"
    assert not local.exists()


def test_same_profile_manual_waiter(stores, posts):
    root, local = stores
    put(local, [row()])
    first, second = cp.load_pool(P), cp.load_pool(P)
    first._refresh_entry(first._entries[0], force=True)
    assert second._refresh_entry(second._entries[0], force=True).refresh_token == "new"
    assert posts == ["old"]
    assert not root.exists()


def test_stale_status_cannot_clobber_newer_tokens_or_removed_rows(stores, posts):
    root, local = stores
    put(
        root, [row(), row("other", ident="other")], providers={"unrelated": {"keep": 1}}
    )
    stale, winner = cp.load_pool(P), cp.load_pool(P)
    winner._refresh_entry(winner._entries[0], force=True)
    stale._mark_exhausted(stale._entries[0], 429)
    assert rows(root)[0]["refresh_token"] == "new"
    state = json.loads(root.read_text())
    state["credential_pool"][P] = [state["credential_pool"][P][1]]
    root.write_text(json.dumps(state))
    stale._persist()
    assert [r["id"] for r in rows(root)] == ["other"]
    with pytest.raises(auth.AuthError):
        stale._refresh_entry(stale._entries[0], force=True)
    assert posts == ["old"]
    assert json.loads(root.read_text())["providers"]["unrelated"] == {"keep": 1}


def test_uncertain_response_is_durable_and_new_login_recovers(stores, monkeypatch):
    root, local = stores
    put(root, [row()])
    calls = []

    class Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, *a, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise auth.httpx.ReadTimeout("synthetic")
            return auth.httpx.Response(
                200,
                json={
                    "access_token": "recovered",
                    "refresh_token": "recovered-refresh",
                },
            )

    monkeypatch.setattr(auth.httpx, "Client", Client)
    pool = cp.load_pool(P)
    with pytest.raises(Exception):
        pool._refresh_entry(pool._entries[0], force=True)
    again = cp.load_pool(P)
    with pytest.raises(auth.AuthError):
        again._refresh_entry(again._entries[0], force=True)
    assert calls == [1]
    put(root, [row("fresh-login")])
    fresh = cp.load_pool(P)
    assert fresh._entries[0].refresh_token == "fresh-login"
    assert fresh._entries[0].last_status != cp.STATUS_DEAD
    assert (
        fresh._refresh_entry(fresh._entries[0], force=True).refresh_token
        == "recovered-refresh"
    )
    receipts = list(root.with_name("auth.json.codex-refresh").glob("*.json"))
    assert receipts
    for receipt in receipts:
        assert json.loads(receipt.read_text()) == {"version": 1, "outcome": "uncertain"}
        assert "fresh-login" not in receipt.name


def test_local_manual_grant_does_not_adopt_same_account_singleton(stores, posts):
    root, local = stores
    put(root, [row("root")])
    put(
        local,
        [row("independent")],
        providers={
            P: {
                "tokens": {
                    "access_token": "different-access",
                    "refresh_token": "different-grant",
                }
            }
        },
    )
    pool = cp.load_pool(P)
    manual = next(e for e in pool._entries if e.source == "manual:device_code")
    pool._refresh_entry(manual, force=True)
    assert posts == ["independent"]
    assert rows(root)[0]["refresh_token"] == "root"


def test_reauth_does_not_join_manual_alias_by_token_equality(stores):
    _, local = stores
    manual = row("same")
    singleton = row("same", source="device_code", ident="singleton")
    put(
        local,
        [manual, singleton],
        providers={
            P: {"tokens": {"access_token": "access-same", "refresh_token": "same"}}
        },
    )
    auth._save_codex_tokens({
        "access_token": "login-access",
        "refresh_token": "login-refresh",
    })
    assert rows(local)[0] == manual
    assert rows(local)[1]["refresh_token"] == "login-refresh"


def test_singleton_runtime_and_pool_share_one_transaction(stores, posts):
    root, local = stores
    put(
        root,
        [],
        providers={
            P: {"tokens": {"access_token": "access-old", "refresh_token": "old"}}
        },
    )
    waiter = cp.load_pool(P)
    result = auth.resolve_codex_runtime_credentials(force_refresh=True)
    assert result["api_key"] == "access-new"
    assert waiter._refresh_entry(waiter._entries[0], force=True).refresh_token == "new"
    assert posts == ["old"]
    assert (
        json.loads(root.read_text())["providers"][P]["tokens"]["refresh_token"] == "new"
    )
    assert rows(root)[0]["refresh_token"] == "new"
    assert not local.exists()


def test_owner_admin_refuses_implicit_mixing(stores):
    root, local = stores
    put(root, [row()])
    pool = cp.load_pool(P)
    with pytest.raises(auth.AuthError, match="Inherited Codex pool"):
        pool.add_entry(cp.PooledCredential.from_dict(P, row("local", ident="new")))
    with pytest.raises(auth.AuthError, match="Inherited Codex pool"):
        pool.remove_index(1)
    assert not local.exists()
    assert len(rows(root)) == 1


def test_receipt_creation_failure_prevents_post(stores, posts, monkeypatch):
    from agent import codex_owner

    root, _ = stores
    put(root, [row()])
    pool = cp.load_pool(P)

    def fail_sync(path):
        raise OSError("synthetic fsync failure")

    monkeypatch.setattr(codex_owner, "_sync_dir", fail_sync)
    with pytest.raises(OSError):
        pool._refresh_entry(pool._entries[0], force=True)
    assert posts == []


def test_definite_quota_rejection_allows_later_retry(stores, monkeypatch):
    root, _ = stores
    put(root, [row()])
    calls = []

    class Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def post(self, *a, **kw):
            calls.append(1)
            if len(calls) == 1:
                return auth.httpx.Response(429, json={"error": "rate_limited"})
            return auth.httpx.Response(
                200, json={"access_token": "ok", "refresh_token": "fresh"}
            )

    monkeypatch.setattr(auth.httpx, "Client", Client)
    pool = cp.load_pool(P)
    with pytest.raises(auth.AuthError) as exc:
        pool._refresh_entry(pool._entries[0], force=True)
    assert exc.value.code == auth.CODEX_RATE_LIMITED_CODE
    pool = cp.load_pool(P)
    assert pool._refresh_entry(pool._entries[0], force=True).refresh_token == "fresh"
    assert len(calls) == 2


def test_removing_owned_singleton_cannot_reseed(stores, posts):
    _, local = stores
    put(
        local,
        [row(source="device_code")],
        providers={
            P: {"tokens": {"access_token": "access-old", "refresh_token": "old"}}
        },
    )
    pool, stale = cp.load_pool(P), cp.load_pool(P)
    pool.remove_index(1)
    assert cp.load_pool(P)._entries == []
    with pytest.raises(auth.AuthError):
        stale._refresh_entry(stale._entries[0], force=True)
    stale._persist()
    assert rows(local) == []
    assert posts == []


def test_replaced_row_source_not_resurrected(stores, posts):
    root, _ = stores
    put(root, [row()])
    pool = cp.load_pool(P)
    put(root, [row(source="device_code")])
    pool._mark_exhausted(pool._entries[0], 429)
    assert rows(root)[0]["source"] == "device_code"
    with pytest.raises(auth.AuthError):
        pool._refresh_entry(pool._entries[0], force=True)
    assert posts == []


def test_priority_and_concurrent_account_addition_survive_status(stores):
    root, _ = stores
    put(root, [dict(row(), priority=7)])
    pool = cp.load_pool(P)
    put(root, [dict(row(), priority=12), row("second", ident="second")])
    pool._mark_exhausted(pool._entries[0], 429)
    assert [(r["id"], r["priority"]) for r in rows(root)] == [
        ("row", 12),
        ("second", 0),
    ]


def test_suppressed_singleton_cannot_fall_through_to_legacy_refresh(stores, posts):
    root, local = stores
    put(
        root,
        [],
        providers={
            P: {"tokens": {"access_token": "access-old", "refresh_token": "old"}}
        },
        suppressed_sources={P: ["device_code"]},
    )
    with pytest.raises(auth.AuthError):
        auth.resolve_codex_runtime_credentials(force_refresh=True)
    assert posts == []
    assert not local.exists()


@pytest.mark.parametrize("status", ["dead", "exhausted"])
def test_peer_generation_quarantine_not_adopted_as_usable(stores, posts, status):
    import time

    root, _ = stores
    put(root, [row()])
    pool = cp.load_pool(P)
    put(
        root,
        [
            dict(
                row("peer"),
                last_status=status,
                last_status_at=time.time(),
                last_error_code=429,
                last_error_reset_at=time.time() + 1000,
            )
        ],
    )
    with pytest.raises(auth.AuthError):
        pool._refresh_entry(pool._entries[0], force=True)
    assert posts == []


def test_runtime_rechecks_status_after_authoritative_sync(stores, monkeypatch):
    from agent import codex_owner

    root, _ = stores
    put(root, [row()])
    original_sync = codex_owner.sync

    def racing_sync(pool, entry):
        put(root, [dict(row(), last_status="dead")])
        return original_sync(pool, entry)

    monkeypatch.setattr(codex_owner, "sync", racing_sync)
    with pytest.raises(auth.AuthError):
        auth.resolve_codex_runtime_credentials(refresh_if_expiring=False)


@pytest.mark.parametrize("method", ["_refresh_entry", "_refresh_entry_impl"])
def test_unowned_in_memory_codex_refresh_is_refused(stores, posts, method):
    entry = cp.PooledCredential.from_dict(P, row())
    pool = cp.CredentialPool(P, [entry])
    with pytest.raises(auth.AuthError, match="explicit auth-store owner"):
        getattr(pool, method)(entry, force=True)
    assert posts == []


def test_status_delta_preserves_unknown_fields(stores):
    root, _ = stores
    put(root, [dict(row(), future_field={"keep": True})])
    pool = cp.load_pool(P)
    pool._mark_exhausted(pool._entries[0], 429)
    assert rows(root)[0]["future_field"] == {"keep": True}
