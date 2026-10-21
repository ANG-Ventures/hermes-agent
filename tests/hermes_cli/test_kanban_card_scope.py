"""Card handback scope: ``## Declared file scope``, ``scope: any``, create-time warning.

t_21755425: card t_a964f306 named one example path in prose, that single token
became the card's whole declared scope, and the branch-base guard refused a
finished 16-file handback as out of scope. The scope is now ONLY the heading's
entries when the heading exists, ``scope: any`` makes it unrestricted, and a
body whose scope is merely inferred from prose warns at create time.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_branch_base as bb
from hermes_cli import kanban_db as kb
from tools import kanban_tools as kt

REPO = Path(__file__).resolve().parents[2]

# The t_a964f306 shape: one example path in prose, no declaration.
PROSE_BODY = (
    "GOAL: lint every capability timing file such as capabilities/device_timing.yaml,\n"
    "and page when one drifts. Build the lint, the CI workflow, and the pager.\n"
)

HEADING_BODY = (
    "Fix the bug; see the example in capabilities/device_timing.yaml.\n\n"
    "## Declared file scope\n"
    "ANG-Ventures/hermes-agent:\n"
    "- hermes_cli/kanban_db.py\n"
    "- `tests/hermes_cli/`\n"
    "- docs/*.md\n\n"
    "## Acceptance\n"
    "mentions scripts/other.py which must not widen the scope\n"
)


def test_prose_only_body_infers_scope_and_warns():
    scope, source = kb.card_declared_scope(PROSE_BODY)
    assert source == "inferred"
    assert scope == ["capabilities/device_timing.yaml"]
    warning = kb.inferred_scope_warning(PROSE_BODY)
    assert warning is not None
    assert "capabilities/device_timing.yaml" in warning
    assert "## Declared file scope" in warning and "scope: any" in warning


def test_heading_scope_is_only_its_entries():
    scope, source = kb.card_declared_scope(HEADING_BODY)
    assert source == "heading"
    assert scope == sorted(["docs/*.md", "tests/hermes_cli/", "hermes_cli/kanban_db.py"])
    assert kb.inferred_scope_warning(HEADING_BODY) is None


@pytest.mark.parametrize("body", [
    PROSE_BODY + "\nscope: any\n",
    PROSE_BODY + "\n## Declared file scope\nany\n",
    PROSE_BODY + "\n## Declared file scope\n- any (exploratory)\n",
])
def test_scope_any_is_unrestricted(body):
    assert kb.card_declared_scope(body) == ([], "any")
    assert kb.inferred_scope_warning(body) is None


def test_nested_headings_stay_inside_the_section():
    body = ("## Declared file scope\n### ANG-Ventures/hermes-agent\n- hermes_cli/a.py\n"
            "### ANG-Ventures/hermes-home\n- scripts/b.py\n## Acceptance\n- gateway/run.py\n")
    assert kb.card_declared_scope(body) == (sorted(["scripts/b.py", "hermes_cli/a.py"]), "heading")


def test_fenced_examples_do_not_declare_scope():
    body = ("Example for authors:\n```\n## Declared file scope\n- docs/x.md\nscope: any\n```\n\n"
            "## Declared file scope\n- hermes_cli/kanban_db.py\n")
    assert kb.card_declared_scope(body) == (["hermes_cli/kanban_db.py"], "heading")
    only_fenced = "edit hermes_cli/a.py\n```\nscope: any\n```\n"
    assert kb.card_declared_scope(only_fenced)[1] == "inferred"


@pytest.mark.parametrize("example", [
    # Prism cc6692a839eb: ``` inside a ~~~ example closed the fence early
    "~~~markdown\n```\nscope: any\n~~~\n",
    # a longer opener is not closed by a shorter run
    "````\n```\nscope: any\n````\n",
    # a fence line with an info string never closes
    "```\n```python\nscope: any\n```\n",
])
def test_fence_closes_only_on_matching_delimiter(example):
    body = f"## Declared file scope\n- hermes_cli/a.py\n## Examples\n{example}"
    assert kb.card_declared_scope(body) == (["hermes_cli/a.py"], "heading")


@pytest.mark.parametrize("line", [
    "ANG-Ventures/hermes-agent: hermes_cli/a.py",
    "Files: hermes_cli/a.py",
    "- Files: `hermes_cli/a.py`",
])
def test_inline_label_keeps_paths(line):
    """Prism 994ce9334b80: ``label: path`` lines were skipped whole -> empty scope."""
    body = f"## Declared file scope\n{line}\n## Acceptance\nalso scripts/b.py\n"
    assert kb.card_declared_scope(body) == (["hermes_cli/a.py"], "heading")


def test_empty_declaration_is_not_unrestricted():
    """Prism 994ce9334b80: a heading with only labels returned ([], heading),
    which check_checkout() reads as unrestricted; fall back to inference."""
    body = ("edit hermes_cli/a.py\n## Declared file scope\n### ANG-Ventures/hermes-agent\n"
            "ANG-Ventures/hermes-home:\n## Acceptance\nDone\n")
    scope, source = kb.card_declared_scope(body)
    assert (scope, source) == (["hermes_cli/a.py"], "inferred")
    assert kb.inferred_scope_warning(body) is not None


@pytest.mark.parametrize("junk", ["- ` `", "- `", "-", "* ``", "  "])
def test_degenerate_heading_lines_do_not_crash(junk):
    body = f"## Declared file scope\n{junk}\n- hermes_cli/a.py\n"
    assert kb.card_declared_scope(body) == (["hermes_cli/a.py"], "heading")


def test_no_paths_no_warning_and_explicit_path_cards_unchanged():
    assert kb.inferred_scope_warning("just do the thing") is None
    assert kb.inferred_scope_warning(None) is None
    body = "edit `hermes_cli/foo.py` and gateway/run.py"
    # backward compatible: no heading -> the historic explicit-path extraction
    assert kb.card_declared_scope(body) == (
        sorted(kb._extract_explicit_dispatch_file_paths(body)), "inferred")


# --- handback E2E through complete_task -> enforce_handoff ------------------


def _git(cwd: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if k not in bb._SCRUB_ENV}
    return subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.name=T", "-c", "user.email=t@example.com",
         "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
        check=True, capture_output=True, text=True, env=env).stdout.strip()


def _commit(repo: Path, files: dict[str, str], msg: str) -> None:
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _fresh_broad_workspace(tmp_path: Path) -> Path:
    """A fresh branch (not stale) touching files far outside the prose path."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    dev = tmp_path / "maintainer"
    subprocess.run(["git", "clone", "-q", str(bare), str(dev)], check=True, capture_output=True)
    _git(dev, "checkout", "-q", "-b", "main")
    _commit(dev, {"README.md": "base\n"}, "init")
    _git(dev, "push", "-q", "origin", "main")
    ws = tmp_path / "ws"
    work = ws / "repo"
    subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True, capture_output=True)
    _git(work, "checkout", "-q", "-b", "wt/t_x")
    _commit(work, {"scripts/lint_timing.py": "x = 1\n",
                   ".github/workflows/timing.yml": "on: push\n"}, "card work")
    return ws


def _create_and_complete(body: str, ws: Path) -> str:
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="scope", assignee="worker", body=body,
                             workspace_kind="scratch", workspace_path=str(ws))
        # created_at must precede the card's commits (foreign-commit cutoff)
        conn.execute("UPDATE tasks SET created_at=? WHERE id=?", (int(time.time()) - 60, tid))
        kb.claim_task(conn, tid)
        run = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (tid,)).fetchone()[0]
        # structured metadata is a receipt, so the default-on receipt gate
        # does not refuse before the scope guard is reached
        kb.complete_task(conn, tid, summary="done", expected_run_id=run,
                         metadata={"changed_files": ["scripts/lint_timing.py"]})
        return conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0]


def test_e2e_prose_scope_still_refuses_broad_handback(board, tmp_path):
    ws = _fresh_broad_workspace(tmp_path)
    with pytest.raises(bb.StaleBaseError) as ei:
        _create_and_complete(PROSE_BODY, ws)
    assert "outside the card's declared scope" in str(ei.value)


def test_e2e_scope_any_lets_broad_handback_pass(board, tmp_path):
    ws = _fresh_broad_workspace(tmp_path)
    assert _create_and_complete(PROSE_BODY + "\nscope: any\n", ws) == "done"


def test_e2e_heading_covers_files_prose_example_does_not(board, tmp_path):
    ws = _fresh_broad_workspace(tmp_path)
    body = PROSE_BODY + "\n## Declared file scope\n- scripts/\n- .github/workflows/*.yml\n"
    assert _create_and_complete(body, ws) == "done"


def test_e2e_inline_label_scope_refuses_unrelated_file(board, tmp_path):
    """Prism 994ce9334b80 repro: ``repo: path`` parsed to an empty scope and a
    fresh branch touching unrelated files passed the guard."""
    ws = _fresh_broad_workspace(tmp_path)
    body = "## Declared file scope\nANG-Ventures/hermes-agent: hermes_cli/a.py\n"
    with pytest.raises(bb.StaleBaseError) as ei:
        _create_and_complete(body, ws)
    assert "outside the card's declared scope" in str(ei.value)


# --- surfaces: CLI stderr + tool return ------------------------------------


def test_cli_create_prints_scope_warning(tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("HERMES_KANBAN", "HERMES_SESSION_", "HERMES_UI_SESSION",
                                "HERMES_CRON", "HERMES_DELEGATED", "_HERMES_GATEWAY"))}
    env.update(HERMES_HOME=str(home), HOME=str(tmp_path), PYTHONPATH=str(REPO))
    proc = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "kanban", "--board", "default", "create", "prose card",
         "--assignee", "worker1", "--home", "operator", "--body", PROSE_BODY],
        env=env, capture_output=True, text=True, timeout=180, cwd=str(REPO))
    assert proc.returncode == 0, proc.stderr
    assert "handback will be limited to: capabilities/device_timing.yaml" in proc.stderr


def test_tool_create_returns_scope_warning(board, monkeypatch):
    for k in list(os.environ):
        if k.startswith(("HERMES_KANBAN", "HERMES_SESSION_", "HERMES_DELEGATED")):
            monkeypatch.delenv(k, raising=False)
    d = json.loads(kt._handle_create({"title": "prose card", "assignee": "peer",
                                      "body": PROSE_BODY, "unhomed": True}))
    assert d["ok"] is True, d
    assert "capabilities/device_timing.yaml" in d["scope_warning"]
    d2 = json.loads(kt._handle_create({"title": "declared card", "assignee": "peer",
                                       "body": HEADING_BODY, "unhomed": True}))
    assert d2["ok"] is True, d2
    assert "scope_warning" not in d2
