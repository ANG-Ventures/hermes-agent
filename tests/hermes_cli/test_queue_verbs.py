"""Table test for the shared /queue verb parser (hermes_cli/queue_verbs.py).

The arity rule must be identical on the CLI and every gateway surface; this file is that contract.
"""
import pytest

from hermes_cli.queue_verbs import QUEUE_VERB_ALIASES, QueueOp, parse_queue_payload


@pytest.mark.parametrize("payload, expected", [
    ("", QueueOp("list")),
    ("   ", QueueOp("list")),
    ("list", QueueOp("list")),
    ("LS", QueueOp("list")),
    ("show", QueueOp("list")),
    ("clear", QueueOp("clear")),
    ("run", QueueOp("run")),
    ("fire", QueueOp("run")),
    ("release", QueueOp("run")),
    ("go", QueueOp("run")),
    ("help", QueueOp("help")),
    ("rm 2", QueueOp("rm", index=2, text="2")),
    ("pop 2", QueueOp("rm", index=2, text="2")),
    ("delete 7", QueueOp("rm", index=7, text="7")),
    ("edit 1 new text here", QueueOp("edit", index=1, text="new text here")),
    ("set 3 x", QueueOp("edit", index=3, text="x")),
    ("edit 1\tnew text", QueueOp("edit", index=1, text="new text")),
    ("edit 1", QueueOp("edit", index=1, text="")),            # handler renders usage_edit
    ("move 2 1", QueueOp("move", index=2, to=1, text="2 1")),
    ("move 1", QueueOp("move", index=1, to=None, text="1")),  # handler renders usage_move
    # --- exact shape only (Ace 2026-10-13 R2): extra words after a verb's arguments = a prompt ---
    ("move 1 x", QueueOp("add", text="move 1 x")),
    ("move 1 2 now", QueueOp("add", text="move 1 2 now")),
    ("rm 3 things", QueueOp("add", text="rm 3 things")),
    ("rm 3 and 4", QueueOp("add", text="rm 3 and 4")),
    ("clear this thing", QueueOp("add", text="clear this thing")),
    # --- prompts, not verbs ---
    ("add rm 2", QueueOp("add", text="rm 2")),
    ("add list", QueueOp("add", text="list")),
    ("add", QueueOp("add", text="")),                          # handler renders usage_add
    ("rm two", QueueOp("add", text="rm two")),
    # --- rm with no number (Ace 2026-10-18): bare = list-to-pick, `all` = clear ---
    ("rm", QueueOp("rm")),
    ("remove", QueueOp("rm")),
    ("POP", QueueOp("rm")),
    ("rm   ", QueueOp("rm")),
    ("rm all", QueueOp("clear")),
    ("RM ALL", QueueOp("clear")),
    ("delete all", QueueOp("clear")),
    ("rm all of them", QueueOp("add", text="rm all of them")),
    ("rm everything", QueueOp("add", text="rm everything")),
    ("add rm", QueueOp("add", text="rm")),
    ("add rm all", QueueOp("add", text="rm all")),
    ("clear the cache", QueueOp("add", text="clear the cache")),
    ("run the tests", QueueOp("add", text="run the tests")),
    ("list the files", QueueOp("add", text="list the files")),
    ("go home", QueueOp("add", text="go home")),
    ("help me", QueueOp("add", text="help me")),
    ("/model opus", QueueOp("add", text="/model opus")),       # gateway refuses at /queue time
    ("summarize this thread", QueueOp("add", text="summarize this thread")),
])
def test_parse_table(payload, expected):
    assert parse_queue_payload(payload) == expected


def test_is_management_only_false_for_add():
    assert parse_queue_payload("list").is_management
    assert not parse_queue_payload("hello").is_management


def test_alias_table_is_the_only_definition():  # noqa: source-proxy structural single-definition contract (I10); the verbs are exercised behaviourally in this file
    """One table (I10): the CLI mixin must not carry its own verb dict any more."""
    import inspect
    import hermes_cli.cli_loops_mixin as m
    src = inspect.getsource(m)
    assert "_QUEUE_VERBS" not in src
    assert "parse_queue_payload" in src
    assert set(QUEUE_VERB_ALIASES) >= {"list", "clear", "edit", "rm", "move", "run"}
