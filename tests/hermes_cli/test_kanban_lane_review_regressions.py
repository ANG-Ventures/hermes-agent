"""Independent review regressions for lane routing and batch controls."""
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc, kanban_db as kb
from tests.hermes_cli.test_kanban_batch_set_model import _create


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / '.hermes'
    home.mkdir()
    monkeypatch.setenv('HERMES_KANBAN_SANDBOX', '1')
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    assert kb.kanban_db_path().resolve().is_relative_to(home.resolve())
    kb.init_db()
    (home / 'config.yaml').write_text(
        'providers:\n  batch-provider:\n    base_url: http://127.0.0.1:9999/v1\n    api_key: test\n'
    )
    return home


def test_explicit_batch_rejects_archived_atomically(kanban_home):
    first = _create('first', 'worker')
    second = _create('second', 'worker')
    with kb.connect() as conn:
        kb.archive_task(conn, second)
    out = kc.run_slash(f'set-model {first} {second} model-a --provider batch-provider')
    assert 'archived' in out
    with kb.connect() as conn:
        assert kb.get_task(conn, first).model_override is None


def test_explicit_batch_rejects_malformed_id_atomically(kanban_home):
    first = _create('first', 'worker')
    out = kc.run_slash(f'set-model {first} t-broken model-a --provider batch-provider')
    assert 't-broken' in out and ('invalid' in out or 'no such' in out)
    with kb.connect() as conn:
        assert kb.get_task(conn, first).model_override is None


def test_json_override_round_trip_and_ttl_rejected_on_card(kanban_home):
    first = _create('first', 'worker')
    payload = json.dumps({'model': 'model-a', 'provider': 'batch-provider', 'reasoning_effort': 'high'}, separators=(',', ':'))
    out = kc.run_slash(f"set-model {first} --model-json '{payload}'")
    assert 'unrecognized arguments' not in out
    with kb.connect() as conn:
        task = kb.get_task(conn, first)
        assert (task.model_override, task.provider_override, task.reasoning_effort) == ('model-a', 'batch-provider', 'high')
    out = kc.run_slash(f'''set-model {first} --model-json '{{"model":"model-b","ttl":"2h"}}' ''')
    assert 'ttl' in out.lower()
    with kb.connect() as conn:
        assert kb.get_task(conn, first).model_override == 'model-a'


def test_json_effort_only_preserves_card_model(kanban_home):
    first = _create('first', 'worker')
    kc.run_slash(f'set-model {first} model-a --provider batch-provider')
    out = kc.run_slash(f'''set-model {first} --model-json '{{"reasoning_effort":"low"}}' ''')
    assert 'low' in out
    with kb.connect() as conn:
        task = kb.get_task(conn, first)
        assert (task.model_override, task.provider_override, task.reasoning_effort) == ('model-a', 'batch-provider', 'low')


def test_model_flag_and_provider_only_json_preserve_model(kanban_home):
    first = _create('first', 'worker')
    assert 'model-a' in kc.run_slash(f'set-model {first} --model model-a --provider batch-provider')
    assert 'model-a' in kc.run_slash(f'''set-model {first} --model-json '{{"provider":"batch-provider"}}' ''')
    with kb.connect() as conn:
        task = kb.get_task(conn, first)
        assert (task.model_override, task.provider_override) == ('model-a', 'batch-provider')


def test_lane_json_round_trip_and_firepower_guard(kanban_home):
    payload = '{"model":"claude-fable-5","provider":"batch-provider","ttl":"2h","reasoning_effort":"high"}'
    out = kc.run_slash(f"lane-model set --model-json '{payload}' --reason capacity")
    assert 'firepower' in out.lower()
    with kb.connect() as conn:
        assert kb.get_lane_model_override(conn) is None
    payload = '{"model":"model-a","provider":"batch-provider","ttl":"2h","reasoning_effort":"high"}'
    out = kc.run_slash(f"lane-model set --model-json '{payload}' --reason capacity")
    assert 'route=batch-provider/model-a' in out
    with kb.connect() as conn:
        assert kb.get_lane_model_override(conn).reasoning_effort == 'high'


@pytest.mark.parametrize('route_arg', [
    'astra --provider batch-provider',
    '--model astra --provider batch-provider',
    '''--model-json '{"model":"astra","provider":"batch-provider"}' ''',
])
def test_card_alias_to_flagship_requires_firepower(kanban_home, route_arg):
    with (kanban_home / 'config.yaml').open('a') as config:
        config.write('model:\n  aliases:\n    astra: batch-provider/gpt-6-astra-900k\n')
    card = _create('alias', 'worker')
    out = kc.run_slash(f'set-model {card} {route_arg}')
    assert 'orchestrator-only' in out
    with kb.connect() as conn:
        assert kb.get_task(conn, card).model_override is None
        assert kb.list_comments(conn, card) == []
    out = kc.run_slash(f'set-model {card} {route_arg} --firepower "urgent task"' if '--model-json' not in route_arg else
                       f'''set-model {card} --model-json '{{"model":"astra","provider":"batch-provider","firepower":"urgent task"}}' ''')
    assert 'gpt-6-astra-900k' in out
    with kb.connect() as conn:
        assert kb.get_task(conn, card).model_override == 'gpt-6-astra-900k'
        # Main's comment contract (what the dispatcher gate authorizes on).
        assert kb.list_comments(conn, card)[0].body == 'flagship override: urgent task'


@pytest.mark.parametrize('route_arg', [
    'batch-provider/astra --ttl 2h',
    '--model astra --provider batch-provider --ttl 2h',
    '''--model-json '{"model":"astra","provider":"batch-provider","ttl":"2h"}' ''',
])
def test_lane_alias_to_flagship_requires_firepower(kanban_home, route_arg):
    with (kanban_home / 'config.yaml').open('a') as config:
        config.write('model:\n  aliases:\n    astra: batch-provider/gpt-6-astra-900k\n')
    out = kc.run_slash(f'lane-model set {route_arg} --reason capacity')
    assert 'orchestrator-only' in out
    with kb.connect() as conn:
        assert kb.get_lane_model_override(conn) is None
    if '--model-json' in route_arg:
        authorised = '''--model-json '{"model":"astra","provider":"batch-provider","ttl":"2h","firepower":"urgent task"}' '''
    else:
        authorised = f'{route_arg} --firepower "urgent task"'
    out = kc.run_slash(f'lane-model set {authorised} --reason capacity')
    assert 'route=batch-provider/gpt-6-astra-900k' in out
    with kb.connect() as conn:
        row = kb.get_lane_model_override(conn)
        assert row.model == 'gpt-6-astra-900k'
        assert row.firepower == 'urgent task'


def test_provider_only_alias_in_profile_requires_firepower(kanban_home):
    with (kanban_home / 'config.yaml').open('a') as config:
        config.write('model:\n  aliases:\n    astra: batch-provider/gpt-6-astra-900k\n')
    profile = kanban_home / 'profiles' / 'worker'
    profile.mkdir(parents=True)
    (profile / 'config.yaml').write_text('model:\n  provider: batch-provider\n  default: astra\n')
    card = _create('provider-only alias', 'worker')
    out = kc.run_slash(f'set-model {card} --provider batch-provider')
    assert 'orchestrator-only' in out
    with kb.connect() as conn:
        assert kb.get_task(conn, card).model_override is None
    out = kc.run_slash(f'set-model {card} --provider batch-provider --firepower "urgent task"')
    assert 'gpt-6-astra-900k' in out
    with kb.connect() as conn:
        assert kb.get_task(conn, card).model_override == 'gpt-6-astra-900k'
        # Main's comment contract (what the dispatcher gate authorizes on).
        assert kb.list_comments(conn, card)[0].body == 'flagship override: urgent task'


def test_lane_reason_and_effort_round_trip(kanban_home):
    out = kc.run_slash('lane-model set --provider batch-provider --model model-a --ttl 2h')
    assert 'reason' in out.lower()
    with kb.connect() as conn:
        assert kb.list_lane_model_overrides(conn) == []
    out = kc.run_slash('lane-model set --provider batch-provider --model model-a --ttl 2h --reason test --effort high')
    assert 'unrecognized arguments' not in out
    with kb.connect() as conn:
        row = kb.get_lane_model_override(conn)
        assert row.reasoning_effort == 'high'


def test_clear_reports_effective_lane_route(kanban_home):
    first = _create('first', 'worker')
    kc.run_slash('lane-model set batch-provider/model-a --ttl 2h --reason test')
    kc.run_slash(f'set-model {first} model-b --provider batch-provider')
    out = kc.run_slash(f'set-model {first} none')
    assert 'batch-provider/model-a' in out


def test_capped_lane_blocks_even_when_profile_is_healthy(kanban_home, monkeypatch):
    from hermes_cli import kanban_provider_health as health
    import hermes_cli.profiles as profiles
    profile = kanban_home / 'profiles' / 'worker'
    profile.mkdir(parents=True)
    (profile / 'config.yaml').write_text('model:\n  provider: profile-provider\n  default: profile-model\n')
    monkeypatch.setattr(profiles, 'profile_exists', lambda _: True)
    lane_only = _create('lane-only', 'worker')
    with kb.connect() as conn:
        kb.set_lane_model_override(conn, provider='batch-provider', model='model-a', expires_at=9999999999, reason='capacity')
    monkeypatch.setattr(health, 'configured_probes', lambda: {'batch-provider': 'http://health.invalid/'})

    class Capped:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, count): return b'{"status":"all_capped"}'
    monkeypatch.setattr(health.urllib.request, 'urlopen', lambda url, timeout=None: Capped())
    launches = []
    with kb.connect() as conn:
        result = kb.dispatch_once(conn, spawn_fn=lambda task, workspace, **kw: launches.append(task.id) or 12345, max_spawn=20)
    assert lane_only not in launches
    assert (lane_only, 'provider_capped') in result.respawn_guarded


def test_capped_profile_admits_healthy_lane_and_blocks_capped_effective_route(kanban_home, monkeypatch):
    from hermes_cli import kanban_provider_health as health
    import hermes_cli.profiles as profiles
    profile = kanban_home / 'profiles' / 'worker'
    profile.mkdir(parents=True)
    (profile / 'config.yaml').write_text('model:\n  provider: profile-provider\n  default: profile-model\n')
    monkeypatch.setattr(profiles, 'profile_exists', lambda _: True)
    lane_only = _create('lane-only', 'worker')
    pinned_profile = _create('pinned-profile', 'worker')
    with kb.connect() as conn:
        kb.set_model_override(conn, pinned_profile, 'profile-model', provider='profile-provider')
        kb.set_lane_model_override(conn, provider='batch-provider', model='model-a', expires_at=9999999999, reason='capacity')
    monkeypatch.setattr(health, 'configured_probes', lambda: {'profile-provider': 'http://health.invalid/'})

    class Capped:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, count): return b'{"status":"all_capped"}'
    monkeypatch.setattr(health.urllib.request, 'urlopen', lambda url, timeout=None: Capped())
    launches = []
    with kb.connect() as conn:
        result = kb.dispatch_once(conn, spawn_fn=lambda task, workspace, **kw: launches.append(task.id) or 12345, max_spawn=20)
    assert lane_only in launches
    assert (pinned_profile, 'provider_capped') in result.respawn_guarded
