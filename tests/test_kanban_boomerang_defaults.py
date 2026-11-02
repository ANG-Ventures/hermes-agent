"""Default subscriptions and quiet, single-line boomerang handbacks."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from gateway import kanban_boomerang_wake as bw
from hermes_cli import kanban_db as kb


@pytest.mark.parametrize("configured,explicit,worker,expected", [
    (None, None, False, True), (False, None, False, False),
    (True, False, False, False), (True, None, True, None),
    (False, True, True, True),
])
def test_create_boomerang_default(tmp_path, monkeypatch, configured, explicit, worker, expected):
    from tools import kanban_tools as kt
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["kanban"]["auto_subscribe_boomerang"] is True
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = {} if configured is None else {"kanban": {"auto_subscribe_boomerang": configured}}
    (tmp_path / "config.yaml").write_text(json.dumps(config))
    assert kt._auto_boomerang(explicit, worker=worker, subscribed=True) is expected
    if explicit is None:
        assert kt._auto_boomerang(None, worker=worker, subscribed=False) is None


def test_create_cli_opt_out():
    import argparse
    from hermes_cli.kanban_parser import build_parser
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="cmd"))
    assert parser.parse_args(["kanban", "create", "c", "--assignee", "peer", "--no-boomerang-wake"]).boomerang_wake is False
    assert parser.parse_args(["kanban", "create", "c", "--assignee", "peer"]).boomerang_wake is None


@pytest.mark.parametrize("surface,stale", [(False, False), (True, False), (True, True)])
def test_handback_shape(tmp_path, monkeypatch, surface, stale):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    kb.init_db()
    with kb.connect_closing() as conn:
        tid = kb.create_task(conn, title="scratch", assignee="human:apollo")
    entry = dict(task_id=tid, board=None, row="review_deadman", delegation_id="d1", route={})
    sent = []
    async def send(chat_id, text, metadata=None):
        sent.append(text)
    runner = SimpleNamespace(_kanban_route_adapter=lambda *a: SimpleNamespace(send=send))
    result = dict(summary="CI is pending\nnot a page", action="surface" if surface else "comment_why_waits")
    assert asyncio.run(bw.report_handled(runner, entry, result, stale=stale)) is (not stale)
    assert asyncio.run(bw.report_handled(runner, entry, result, stale=stale)) is False
    assert sent == ([] if stale else [f"boomerang handled {tid} (review_deadman): CI is pending"])
    with kb.connect_closing() as conn:
        comments = kb.list_comments(conn, tid)
    assert len(comments) == 1
    assert comments[0].body.startswith("boomerang: review_deadman CI is pending")
