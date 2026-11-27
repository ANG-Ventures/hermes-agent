"""Process incarnation fencing for retry claims."""
from hermes_cli import goals


def test_reused_pid_is_not_live_claimant(monkeypatch):
    monkeypatch.setattr(goals.os, 'getpid', lambda: 100)
    monkeypatch.setattr(goals, '_PROCESS_INSTANCE', 'current')
    monkeypatch.setattr(goals, '_pid_alive', lambda pid: True)
    monkeypatch.setattr(goals, '_process_start_time', lambda pid: 2000)
    assert goals._orphaned_retry_claim('retry:200:1000:old:nonce')
    assert not goals._orphaned_retry_claim('retry:200:2000:old:nonce')
    assert not goals._orphaned_retry_claim('retry:200:corrupt:old:nonce')
    monkeypatch.setattr(goals, '_process_start_time', lambda pid: None)
    assert not goals._orphaned_retry_claim('retry:200:1000:old:nonce')


def test_new_claim_records_start_identity(monkeypatch):
    monkeypatch.setattr(goals.os, 'getpid', lambda: 100)
    monkeypatch.setattr(goals, '_process_start_time', lambda pid: 1234)
    assert goals._new_retry_claim().startswith('retry:100:1234:')
