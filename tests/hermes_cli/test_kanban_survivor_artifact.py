"""A no-PR dir/scratch card with an attached artifact completes; repo workspaces still refuse."""
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_survivor as survivor


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with kb.connect_closing() as conn:
        yield conn


def _card(board, tmp_path, body, attach=True, by="worker"):
    ws = tmp_path / "ws"
    ws.mkdir()
    tid = kb.create_task(board, title="ab cell", body=body, workspace_kind="dir",
                         workspace_path=str(ws))
    (ws / "result.json").write_text('{"ok": 1}')
    if attach:
        art = tmp_path / "result.json"
        art.write_text('{"ok": 1}')
        kb.add_attachment(board, tid, filename="result.json", stored_path=str(art),
                          content_type="application/json", size=art.stat().st_size,
                          uploaded_by=by)
    return tid, ws


def test_dir_no_pr_with_attachment_completes_as_artifact(board, tmp_path):
    tid, _ = _card(board, tmp_path, "A/B cell. No-PR by design; do not use git.")
    assert kb.complete_task(board, tid, metadata={"changed_files": ["result.json"]})
    saved = kb.latest_run(board, tid).metadata["survivor"]
    assert saved["kind"] == "artifact"
    assert saved["artifacts"][0]["path"].endswith("result.json")
    assert len(saved["artifacts"][0]["sha256"]) == 64


def test_dir_without_no_pr_declaration_still_refuses(board, tmp_path):
    tid, _ = _card(board, tmp_path, "implement a thing")
    with pytest.raises(survivor.SurvivorUnavailable):
        kb.complete_task(board, tid, metadata={"changed_files": ["result.json"]})


def test_dir_no_pr_without_attachment_still_refuses(board, tmp_path):
    tid, _ = _card(board, tmp_path, "No-PR by design.", attach=False)
    with pytest.raises(survivor.SurvivorUnavailable):
        kb.complete_task(board, tid, metadata={"changed_files": ["result.json"]})


def test_repo_workspace_no_pr_never_records_artifact(board, tmp_path):
    tid, ws = _card(board, tmp_path, "No-PR by design.")
    subprocess.run(["git", "init", "-q", str(ws)], check=True, stdin=subprocess.DEVNULL)
    try:
        kb.complete_task(board, tid, metadata={"changed_files": ["result.json"]})
    except survivor.SurvivorUnavailable:
        return
    assert kb.latest_run(board, tid).metadata["survivor"]["kind"] != "artifact"


@pytest.mark.parametrize("by", ["dashboard", "harness", "kanban_complete", None])
def test_non_worker_input_attachment_is_not_a_survivor(board, tmp_path, by):
    tid, _ = _card(board, tmp_path, "No-PR by design.", by=by)
    with pytest.raises(survivor.SurvivorUnavailable):
        kb.complete_task(board, tid, metadata={"changed_files": ["result.json"]})


def test_agent_tagged_upload_is_a_survivor(board, tmp_path):
    tid, _ = _card(board, tmp_path, "No-PR by design.", by="agent")
    assert kb.complete_task(board, tid, metadata={"changed_files": ["result.json"]})
