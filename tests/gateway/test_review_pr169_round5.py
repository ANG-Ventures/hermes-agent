
"""Round 5 public adapter-path reviewer probes; scratch only."""
import asyncio
import threading
import types
import pytest
import hermes_yaml as yaml
from hermes_state import SessionDB as RealDB
from tests.gateway.test_model_switch_reply_first import _Harness, _ev, _source, _switched, _wait_for, _drained, FOLLOWUP


def real_store(h, tmp_path, monkeypatch):
    monkeypatch.setattr('hermes_state.SessionDB', RealDB)
    db = RealDB(db_path=tmp_path / 'real-state.db')
    h.runner.session_store._db = db
    return db


@pytest.mark.asyncio
async def test_cross_profile_global_selections_are_independent(tmp_path, monkeypatch):
    h = _Harness(tmp_path, monkeypatch)
    db = real_store(h, tmp_path, monkeypatch)
    homes = {}
    for name in ('A', 'B'):
        home = tmp_path / name
        home.mkdir()
        (home / 'config.yaml').write_text(yaml.safe_dump({'model': {'default': 'old-model', 'provider': 'openrouter'}}))
        homes[name] = home
    h.runner.config = types.SimpleNamespace(multiplex_profiles=True)
    h.runner._resolve_profile_home_for_source = lambda source: homes[source.profile]
    h.runner._adapter_for_source = lambda source: h.adapter
    original = h.db.update_session_model
    async def gate_a(sid, model, **kw):
        if model == 'model-A':
            await original(sid, model, **kw)
        await asyncio.to_thread(db.update_session_model, sid, model, **kw)
    h.db.update_session_model = gate_a
    a, b = _ev('/model model-A --global', '111'), _ev('/model model-B --global', '222')
    a.source.profile, b.source.profile = 'A', 'B'
    try:
        await h.adapter.handle_message(a)
        await asyncio.wait_for(h.db.entered.wait(), 5)
        await h.adapter.handle_message(b)
        await _wait_for(lambda: yaml.safe_load((homes['B'] / 'config.yaml').read_text())['model']['default'] == 'model-B', 'B persisted')
        h.db.release.set()
        await _wait_for(lambda: not h.adapter._session_tasks, 'both drains')
        actual = yaml.safe_load((homes['A'] / 'config.yaml').read_text())['model']['default']
        print('PROFILE_A_CONFIG', actual)
        assert actual == 'model-A'
    finally:
        h.db.release.set()
        await _wait_for(lambda: not h.adapter._session_tasks, 'cleanup')
        db.close()


@pytest.mark.asyncio
async def test_cost_callback_keeps_initial_deferral_decision(tmp_path, monkeypatch):
    h = _Harness(tmp_path, monkeypatch)
    db = real_store(h, tmp_path, monkeypatch)
    original_update = h.db.update_session_model
    async def update(sid, model, **kw):
        await original_update(sid, model, **kw)
        await asyncio.to_thread(db.update_session_model, sid, model, **kw)
    h.db.update_session_model = update
    captured = {}
    async def confirm(**kw):
        captured['handler'] = kw['handler']
        return 'cost confirmation requested'
    h.runner._request_slash_confirm = confirm
    monkeypatch.setattr('hermes_cli.model_selection_guards.combined_selection_warning', lambda model, **kw: types.SimpleNamespace(title='cost', message='cost') if model == 'model-B' else None)
    task = None
    try:
        await h.adapter.handle_message(_ev('/model model-B'))
        await _wait_for(lambda: 'handler' in captured, 'cost callback registered')
        await _drained(h)
        await h.adapter.handle_message(_ev('/model model-A'))
        await asyncio.wait_for(h.db.entered.wait(), 5)
        task = asyncio.create_task(captured['handler']('once'))
        await asyncio.sleep(0.05)
        assert not task.done(), 'cost callback bypassed the owning save'
        h.db.release.set()
        reply = await asyncio.wait_for(task, 10)
        assert _switched('model-B') in reply
        await _drained(h, 10)
        assert not h.sent_with(FOLLOWUP)
        actual = h.runner.session_store.get_chat_model_pin(h.key())
        assert actual[0] and actual[1] and actual[1]['model'] == 'model-B'
    finally:
        h.db.release.set()
        if task and not task.done():
            await asyncio.wait_for(task, 10)
        await _wait_for(lambda: not h.adapter._session_tasks, 'cleanup')
        db.close()


@pytest.mark.asyncio
async def test_reset_cannot_be_undone_by_started_legacy_write(tmp_path, monkeypatch):
    h = _Harness(tmp_path, monkeypatch)
    db = real_store(h, tmp_path, monkeypatch)
    h.db.release.set()
    entered, release = threading.Event(), threading.Event()
    store = h.runner.session_store
    original = store.set_model_override
    def delayed(key, override):
        if override and override.get('model') == 'model-B':
            entered.set()
            assert release.wait(15)
        return original(key, override)
    monkeypatch.setattr(store, 'set_model_override', delayed)
    try:
        await h.adapter.handle_message(_ev('/model model-B'))
        await _wait_for(entered.is_set, 'legacy worker entered')
        reset = asyncio.create_task(h.adapter.handle_message(_ev('/model reset')))
        await asyncio.sleep(0.05)
        assert not reset.done(), 'reset bypassed started writer'
        release.set()
        await asyncio.wait_for(reset, 10)
        await _wait_for(lambda: h.sent_with('Model session override cleared'), 'reset confirmed')
        assert store.get_chat_model_pin(h.key()) == (True, None)
        await _drained(h, 10)
        pin = store.get_chat_model_pin(h.key())
        await h.adapter.handle_message(_ev('hello after reset'))
        await _wait_for(lambda: h.turns, 'next ordinary message')
        model = h.turns[-1][1]
        print('AFTER_RESET_PIN', pin, 'NEXT_MESSAGE_MODEL', model)
        assert model == 'old-model'
    finally:
        release.set()
        await _wait_for(lambda: not h.adapter._session_tasks, 'cleanup')
        db.close()
