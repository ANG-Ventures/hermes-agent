"""Adversarial generation receipts and trusted response provenance."""
import pytest
import agent.codex_owner as owner
import agent.credential_pool as cp
import hermes_cli.auth as auth
from tests.agent.test_codex_owner_transaction import stores, posts, row, put, rows, P


@pytest.mark.parametrize('identity', ['changed', None])
def test_backup_row_labels_cannot_replay_generation(stores, posts, identity):
    _, local = stores
    original = row()
    put(local, [original])
    pool = cp.load_pool(P)
    pool._refresh_entry(pool._entries[0], force=True)
    if identity is None:
        original.pop('id')
    else:
        original['id'] = identity
    put(local, [original])
    pool = cp.load_pool(P)
    with pytest.raises(auth.AuthError):
        pool._refresh_entry(pool._entries[0], force=True)
    assert posts == ['old']
    original['refresh_token'] = 'fresh-grant'
    put(local, [original])
    pool = cp.load_pool(P)
    assert pool._refresh_entry(pool._entries[0], force=True).refresh_token == 'new'
    assert posts == ['old', 'fresh-grant']


@pytest.mark.parametrize('status', [429, 500])
def test_only_trusted_429_releases_receipt(stores, monkeypatch, status):
    _, local = stores
    put(local, [row()])
    calls = []
    class Client:
        def __init__(self, *a, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, *a, **kw):
            calls.append(kw['data']['refresh_token'])
            return auth.httpx.Response(status, json={'error': auth.CODEX_RATE_LIMITED_CODE})
    monkeypatch.setattr(auth.httpx, 'Client', Client)
    pool = cp.load_pool(P)
    entry = pool._entries[0]
    with pytest.raises(auth.AuthError):
        pool._refresh_entry(entry, force=True)
    assert owner._receipt(local, entry).exists() == (status != 429)
    if status == 429:
        assert rows(local)[0]['last_status'] == 'exhausted'
    with pytest.raises(auth.AuthError):
        pool._refresh_entry(entry, force=True)
    assert calls == ['old']


@pytest.mark.parametrize('status', [429, 500])
def test_runtime_failed_singleton_falls_back_to_healthy_manual(stores, monkeypatch, status):
    from tests.agent.test_codex_owner_selection_review import row as jwt_row, mock_http
    _, local = stores
    bad = jwt_row('bad', expired=True, source='device_code')
    healthy = jwt_row('healthy')
    put(local, [bad, healthy], providers={P: {'tokens': {
        'access_token': bad['access_token'], 'refresh_token': bad['refresh_token']}}})
    calls = mock_http(monkeypatch, status)
    monkeypatch.setattr(auth, '_probe_codex_quota_restored', lambda *a, **kw: False)
    for _ in range(2):
        result = auth.resolve_codex_runtime_credentials()
        assert result['api_key'] == healthy['access_token']
    assert calls == ['refresh-bad']


@pytest.mark.parametrize('consumer', ['select', 'acquire_lease'])
def test_transport_uncertainty_does_not_poison_healthy_row(stores, monkeypatch, consumer):
    from tests.agent.test_codex_owner_selection_review import row as jwt_row
    _, local = stores
    put(local, [jwt_row('bad', expired=True), jwt_row('healthy')])
    calls = []
    def fail(*a, **kw):
        calls.append(1)
        raise auth.httpx.ReadTimeout('synthetic timeout')
    monkeypatch.setattr(auth, 'refresh_codex_oauth_pure', fail)
    pool = cp.load_pool(P)
    for _ in range(2):
        result = getattr(pool, consumer)()
        assert (result.id if consumer == 'select' else result) == 'healthy'
    assert calls == [1]
