"""Lease-host readiness gates joins before claim without affecting ordinary cards."""
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_browser_gate as gate
from hermes_cli import kanban_browser_readiness as readiness
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_needs as needs


@pytest.mark.parametrize('refusal', ['BROWSER_IMAGE_UNAVAILABLE', 'BROWSER_HOST_AT_CAP', None])
def test_gate_checks_plan_host_before_join(monkeypatch, refusal):
    row = {'id': 'fixture', 'assignee': 'alpha', 'body': 'needs: browser:ephemeral'}
    plan = gate.Plan(needs.EPHEMERAL, 'browser-phase1-ephemeral', 'act', host='ci-box')
    verdict = SimpleNamespace(legacy=[], refusal=None, wait=None, plans=[plan])
    monkeypatch.setattr(gate, 'load', lambda *a, **k: SimpleNamespace(mode=gate.MODE_ON, local_ephemeral=()))
    monkeypatch.setattr(gate, 'gate_rows', lambda *a, **k: {'fixture': verdict})
    monkeypatch.setattr(gate, 'minted_by_of', lambda *a: {})
    monkeypatch.setattr(dispatch, '_browser_join_on', lambda: True)
    monkeypatch.setattr(dispatch, '_browser_lookup', lambda: None)
    monkeypatch.setattr(dispatch, '_root_kanban_cfg', lambda: {})
    monkeypatch.setattr(dispatch, '_profile_exists_fn', lambda: lambda p: True)
    calls = []

    def probe(home, host):
        calls.append(host)
        return {'ok': refusal is None, 'host': host, 'refusal': refusal}

    monkeypatch.setattr(readiness, 'probe', probe)
    for _ in range(2):
        result = SimpleNamespace(placement_caps={}, browser_joins={})
        routes = {'fixture': ('pin', 'ace-ai'), 'ordinary': ('pin', 'ace-media')}
        dispatch._browser_gate(None, [row, {'id': 'ordinary', 'assignee': 'alpha', 'body': 'code only'}],
                               result, routes, True)
        assert routes['ordinary'] == ('pin', 'ace-media')
        assert ('fixture' in result.browser_joins) == (refusal is None)
        if refusal:
            assert routes['fixture'][0] == 'wait'
            assert result.placement_caps['fixture']['browser_wait']['host'] == 'ci-box'
    assert calls == ['ci-box', 'ci-box']  # fresh on each tick; never probes worker ace-ai


def test_unreachable_host_is_probed_once_per_tick_for_many_cards(monkeypatch):
    # Cards alternate between ci-box and a local-ephemeral host; every probe times out (~25 s),
    # so each host's entry ages past any short TTL while the other is probed. Each host must
    # still cost exactly one probe per tick however many cards plan onto it.
    hosts = ['ci-box', 'ace-ai'] * 3
    rows = [{'id': f'c{i}', 'assignee': 'alpha', 'body': 'needs: browser:ephemeral'} for i in range(len(hosts))]
    verdicts = {r['id']: SimpleNamespace(legacy=[], refusal=None, wait=None,
                                         plans=[gate.Plan(needs.EPHEMERAL, 'browser-phase1-ephemeral', 'act', host=h)])
                for r, h in zip(rows, hosts)}
    clock = [1000.0]
    monkeypatch.setattr('time.monotonic', lambda: clock[0])
    monkeypatch.setattr(gate, 'load', lambda *a, **k: SimpleNamespace(mode=gate.MODE_ON, local_ephemeral=('ace-ai',)))
    monkeypatch.setattr(gate, 'gate_rows', lambda *a, **k: verdicts)
    monkeypatch.setattr(gate, 'minted_by_of', lambda *a: {})
    monkeypatch.setattr(dispatch, '_browser_join_on', lambda: True)
    monkeypatch.setattr(dispatch, '_browser_lookup', lambda: None)
    monkeypatch.setattr(dispatch, '_root_kanban_cfg', lambda: {})
    monkeypatch.setattr(dispatch, '_profile_exists_fn', lambda: lambda p: True)
    calls = []

    def probe(home, host):
        calls.append(host)
        clock[0] += 25
        return {'ok': False, 'host': host, 'refusal': 'BROWSER_READINESS_UNAVAILABLE', 'detail': 'TimeoutExpired'}

    monkeypatch.setattr(readiness, 'probe', probe)
    result = SimpleNamespace(placement_caps={}, browser_joins={})
    routes = {r['id']: ('pin', 'ace-ai') for r in rows}
    dispatch._browser_gate(None, rows, result, routes, True)
    assert calls == ['ci-box', 'ace-ai']
    assert all(routes[r['id']] == ('wait', 'browser_not_ready') for r in rows)
