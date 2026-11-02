"""`kanban create` default priority bands (hermes-home t_645c6b17, spec 2026-11-02 Q1 A): a human's bare create
lands at human_default 100 (p2), above every machine minter; a minter (--created-by <id>, or a cron/worker/script
process) keeps 0; an explicit --priority always wins; KANBAN_PRIORITY_BANDS=off restores 0 (I4)."""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_parser as kp


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("KANBAN_PRIORITY_BANDS", raising=False)
    kb.init_db()
    return home


def _create_priority(*argv) -> int:
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    assert kc._cmd_create(root.parse_args(["kanban", "create", *argv, "--home", "operator"])) == 0
    with kbc.connect_closing() as conn:
        return next(t.priority for t in kb.list_tasks(conn) if t.title == argv[0])


@pytest.mark.parametrize("origin,want", [("hand", 100), ("gateway", 100), ("cron", 0), ("worker", 0),
                                         ("script", 0)])
def test_human_bare_create_lands_at_human_default(kanban_home, monkeypatch, origin, want):
    monkeypatch.setattr(kb, "classify_create_origin", lambda: origin)
    assert _create_priority("bare card") == want


def test_minter_and_explicit_priority_keep_their_value(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "classify_create_origin", lambda: "hand")
    assert _create_priority("minted", "--created-by", "pager-owner-card") == 0
    assert _create_priority("explicit", "--priority", "40") == 40
    assert _create_priority("explicit zero", "--priority", "0") == 0      # an explicit 0 is not the default
    assert _create_priority("tier", "--priority", "p1") == 200


def test_kill_switch_restores_zero(kanban_home, monkeypatch):
    monkeypatch.setattr(kb, "classify_create_origin", lambda: "hand")
    monkeypatch.setenv("KANBAN_PRIORITY_BANDS", "off")
    assert _create_priority("bare card") == 0
    assert kp.create_default_priority("user", "hand") == 0


def test_create_help_documents_one_scale(capsys):
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    with pytest.raises(SystemExit):
        root.parse_args(["kanban", "create", "--help"])
    out = " ".join(capsys.readouterr().out.split())
    assert "higher = first" in out and "human_default 100" in out and "pager_unowned 40" in out
