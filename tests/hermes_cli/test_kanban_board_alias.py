"""Retired board slugs must resolve to the live board, not a phantom.

Regression cover for the ``ban-forensics`` incident (card t_a1e6e877):
after a board was renamed, consumers still pinned to the old slug (a
worker's spawn env, a script) called ``connect(board=<old>)``. Because
``connect()`` mkdirs + ``init_db()``s whatever path it is handed, each
such call re-created ``boards/<old-slug>/`` holding a fully-schema'd but
EMPTY kanban.db — which ``list_boards()`` then surfaced as a real board.

That is a silent card-loss path: a card created against the stale slug
lands in the empty DB and is invisible on the real board, which is
strictly worse than the old slug hard-failing.

Two independent defences are covered here:

1. ``board-aliases.json`` redirects a retired slug at ``board_dir()`` —
   the single choke point every board path resolves through.
2. ``list_boards()`` skips a dir with a kanban.db, no board.json, and
   zero tasks, so a phantom created by some *other* path stays invisible.
"""

import json

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolate kanban paths under a temp HERMES_HOME.

    ``HERMES_KANBAN_SANDBOX`` neutralises the ``HERMES_KANBAN_*`` path
    pins the dispatcher injects — without it these tests would resolve to
    (and write to) the LIVE board. See ``kanban_db_path``'s docstring.
    """
    monkeypatch.setenv("HERMES_KANBAN_SANDBOX", "1")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for key in [k for k in list(kb.os.environ) if k.startswith("HERMES_KANBAN_")]:
        if key != "HERMES_KANBAN_SANDBOX":
            monkeypatch.delenv(key, raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb._BOARD_ALIAS_CACHE = (None, {})
    yield tmp_path
    kb._INITIALIZED_PATHS.clear()
    kb._BOARD_ALIAS_CACHE = (None, {})


def _write_aliases(mapping):
    path = kb.board_aliases_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mapping), encoding="utf-8")
    return path


def test_connect_on_unaliased_dead_slug_creates_phantom(kanban_home):
    """Baseline: this is the defect. Without an alias the phantom appears.

    Locks in the mechanism so a future refactor that silently changes it
    is visible, and proves the alias test below is actually doing work.
    """
    boards = kanban_home / "kanban" / "boards"
    assert not (boards / "deadslug").exists()

    conn = kb.connect(board="deadslug")
    try:
        assert (boards / "deadslug" / "kanban.db").exists()
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        # The phantom has no board.json — it never went through create_board().
        assert not (boards / "deadslug" / "board.json").exists()
    finally:
        conn.close()


def test_alias_redirects_retired_slug_to_live_board(kanban_home):
    """A card written via the OLD slug must land on the LIVE board."""
    kb.create_board("account-health", name="Account Health")
    _write_aliases({"ban-forensics": "account-health"})

    live = kb.connect(board="account-health")
    kb.create_task(live, title="real card", assignee="daedalus")
    live.commit()

    # Every board path must resolve through the alias.
    assert kb.board_dir("ban-forensics") == kb.board_dir("account-health")
    assert kb.kanban_db_path("ban-forensics") == kb.kanban_db_path("account-health")
    assert kb.workspaces_root("ban-forensics") == kb.workspaces_root("account-health")
    assert kb.attachments_root("ban-forensics") == kb.attachments_root("account-health")

    # A consumer pinned to the dead slug sees the REAL cards, not an empty DB.
    aliased = kb.connect(board="ban-forensics")
    try:
        assert aliased.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
    finally:
        aliased.close()
        live.close()

    # And no phantom directory was created beside the real board.
    assert not (kanban_home / "kanban" / "boards" / "ban-forensics").exists()


def test_alias_does_not_double_list_the_board(kanban_home):
    """The alias must not make one board appear twice.

    This is why an alias was chosen over a symlink: a symlink-to-dir
    satisfies ``Path.is_dir()``, so ``list_boards()`` enumerates it as a
    second, separate board pointing at the same DB.
    """
    kb.create_board("account-health", name="Account Health")
    _write_aliases({"ban-forensics": "account-health"})

    slugs = [b.get("slug") for b in kb.list_boards()]
    assert slugs.count("account-health") == 1
    assert "ban-forensics" not in slugs


def test_alias_chain_and_cycle_are_bounded(kanban_home):
    """Chains resolve; a cycle degrades instead of hanging."""
    _write_aliases({"a": "b", "b": "c"})
    assert kb.resolve_board_alias("a") == "c"

    kb._BOARD_ALIAS_CACHE = (None, {})
    _write_aliases({"x": "y", "y": "x"})
    # Must terminate and yield a real slug rather than looping forever.
    assert kb.resolve_board_alias("x") in {"x", "y"}


def test_malformed_alias_file_is_ignored(kanban_home):
    """A broken aliases file must not break board resolution."""
    path = kb.board_aliases_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")
    assert kb.resolve_board_alias("account-health") == "account-health"

    kb._BOARD_ALIAS_CACHE = (None, {})
    path.write_text('["a", "list", "not", "a", "map"]', encoding="utf-8")
    assert kb.resolve_board_alias("account-health") == "account-health"


def test_list_boards_skips_empty_phantom_without_board_json(kanban_home):
    """A kanban.db with 0 tasks and no board.json is not a board."""
    kb.create_board("real", name="Real")
    conn = kb.connect(board="real")
    kb.create_task(conn, title="a card", assignee="daedalus")
    conn.commit()
    conn.close()

    # Phantom: exactly what connect() on a dead slug leaves behind.
    phantom = kb.connect(board="phantom")
    phantom.close()
    assert (kanban_home / "kanban" / "boards" / "phantom" / "kanban.db").exists()

    slugs = [b.get("slug") for b in kb.list_boards()]
    assert "real" in slugs
    assert "phantom" not in slugs


def test_list_boards_keeps_board_with_cards_but_no_board_json(kanban_home):
    """Never hide real data: cards present ⇒ still listed.

    A board that lost its board.json is a recoverable metadata problem,
    not a phantom. Hiding it would be data loss by a different route.
    """
    kb.create_board("hascards", name="Has Cards")
    conn = kb.connect(board="hascards")
    kb.create_task(conn, title="a real card", assignee="daedalus")
    conn.commit()
    conn.close()

    kb.board_metadata_path("hascards").unlink()
    assert not (kanban_home / "kanban" / "boards" / "hascards" / "board.json").exists()

    slugs = [b.get("slug") for b in kb.list_boards()]
    assert "hascards" in slugs


def test_board_db_is_empty_fails_closed_on_unreadable_db(kanban_home, tmp_path):
    """An unreadable/corrupt DB must NOT be reported empty."""
    bogus = tmp_path / "not-a-db.sqlite"
    bogus.write_text("this is not a sqlite file", encoding="utf-8")
    assert kb._board_db_is_empty(bogus) is False
    assert kb._board_db_is_empty(tmp_path / "does-not-exist.db") is False
