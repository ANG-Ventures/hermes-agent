"""CLI parity for `/queue rm` with no number and `/queue rm all` (Ace 2026-10-18).

Drives the real ``CLILoopsMixin._cmd_queue`` on a real ``queue.Queue`` and captures ``cli._cprint``.
"""
import queue

import pytest

import cli
from hermes_cli.cli_loops_mixin import CLILoopsMixin


class _Shell(CLILoopsMixin):
    def __init__(self, *items):
        self._pending_input = queue.Queue()
        for i in items:
            self._pending_input.put(i)


@pytest.fixture
def printed(monkeypatch):
    out = []
    monkeypatch.setattr(cli, "_cprint", lambda s="", *a, **k: out.append(str(s)))
    return out


def test_bare_rm_lists_and_keeps_queue(printed):
    sh = _Shell("a", "b")
    sh._cmd_queue("/queue rm")
    text = "\n".join(printed)
    assert "1. a" in text and "2. b" in text
    assert "Nothing removed." in text and "/queue rm all" in text
    assert sh._pending_input_items() == ["a", "b"]


def test_bare_rm_on_empty_queue_says_empty_and_queues_nothing(printed):
    sh = _Shell()
    sh._cmd_queue("/queue rm")
    assert any("Queue is empty." in p for p in printed)
    assert not any("Nothing removed." in p for p in printed)
    assert sh._pending_input_items() == []


def test_rm_all_clears(printed):
    sh = _Shell("a", "b", "c")
    sh._cmd_queue("/queue rm all")
    assert sh._pending_input_items() == []
    assert any("Cleared 3 queued prompts." in p for p in printed)


def test_rm_number_still_removes_one(printed):
    sh = _Shell("a", "b")
    sh._cmd_queue("/queue rm 1")
    assert sh._pending_input_items() == ["b"]
