"""``kanban_identity``: a stated kanban caller identity on a throwaway board (t_f4c584e2).

``hermes kanban create`` refuses a scripted, sessionless create (``kanban: refused create
(script): no home session``; ``kanban_db.create_task(require_home=True)``). Whether a test's
spawned CLI hits that refusal must not depend on the shell running the suite: an agent or
worker shell carries ``HERMES_SESSION_ID``, CI and the off-box lane do not (hermes-agent#1825
read 5/5 locally, 2/5 off-box). This fixture states the identity explicitly instead.

It arms the guard (drops the suite-compat ``HERMES_KANBAN_ALLOW_UNHOMED_CREATE`` escape), sets
``HERMES_SESSION_ID`` (the identity ``--session`` defaults to, per the guard's own message),
and initialises the board in the per-test ``HERMES_HOME``. ``env`` is a copy of the
resulting process env for ``subprocess.run(..., env=...)``.
``scripts/check_kanban_test_identity.py`` fails a test that spawns ``kanban
create|promote|claim`` without this fixture or an explicit ``--parent/--session/--home/--unhomed``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import pytest

KANBAN_TEST_SESSION_ID = "20261010_000000_kanbantest"


@dataclass
class KanbanIdentity:
    home: Path
    session_id: str
    env: dict[str, str] = field(default_factory=dict)


@pytest.fixture
def kanban_identity(monkeypatch) -> KanbanIdentity:
    monkeypatch.delenv("HERMES_KANBAN_ALLOW_UNHOMED_CREATE", raising=False)
    monkeypatch.setenv("HERMES_SESSION_ID", KANBAN_TEST_SESSION_ID)
    from hermes_cli import kanban_db as kb

    kb.init_db()
    return KanbanIdentity(
        home=Path(os.environ["HERMES_HOME"]),
        session_id=KANBAN_TEST_SESSION_ID,
        env=dict(os.environ),
    )
