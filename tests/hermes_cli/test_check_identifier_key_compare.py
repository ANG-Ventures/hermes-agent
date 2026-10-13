"""check_identifier_key_compare: identifier keys compare byte-exact (t_75fdedd4, Operator-Surface S3).

The lint is green on the tree, red on each planted violation shape, and the sites this card made
byte-exact behave byte-exact.
"""

import importlib.util
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("check_identifier_key_compare", ROOT / "scripts" / "check_identifier_key_compare.py")
lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lint)


def test_lint_is_green_on_the_tree(capsys):
    assert lint.main([]) == 0, capsys.readouterr().out


PLANTED = {
    # the #1921 shape: SQLite LIKE (ASCII-case-insensitive) on a model token
    "sql": 'def f(conn, model):\n    return conn.execute("SELECT 1 FROM task_comments WHERE body LIKE ?", (model + "%",))\n',
    "fold": "def f(task, model):\n    return task.model_override.lower() == model.lower()\n",
    "affix": "def f(assignee):\n    return assignee.startswith('daedalus')\n",
    "substr": "def f(head_sha, body):\n    return head_sha in body\n",
    "sql_nocase": 'Q = "SELECT id FROM tasks WHERE assignee = ? COLLATE NOCASE"\n',
}


@pytest.mark.parametrize("kind", sorted(PLANTED))
def test_planted_violation_is_red(kind, tmp_path, capsys):
    for sub in ("hermes_cli", "tools"):
        (tmp_path / sub).mkdir()
        for src in (ROOT / sub).glob("kanban*.py"):
            shutil.copy(src, tmp_path / sub / src.name)
    (tmp_path / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "identifier_key_compare_ledger.json", tmp_path / "scripts")
    assert lint.main(["--root", str(tmp_path), "--ledger", str(tmp_path / "scripts" / "identifier_key_compare_ledger.json")]) == 0, \
        capsys.readouterr().out
    (tmp_path / "hermes_cli" / "kanban_planted.py").write_text(PLANTED[kind])
    rc = lint.main(["--root", str(tmp_path), "--ledger", str(tmp_path / "scripts" / "identifier_key_compare_ledger.json")])
    out = capsys.readouterr().out
    assert rc == 1 and "kanban_planted.py" in out, out


def test_byte_exact_compare_is_not_reported():
    src = ('def f(conn, task_id, model, token):\n'
           '    conn.execute("SELECT 1 FROM task_comments WHERE task_id=? AND substr(body, 1, ?) = ?", (task_id, len(token), token))\n'
           '    return model == "CustomModel" or model in {"a", "b"}\n')
    assert lint.scan_source("hermes_cli/kanban_x.py", src) == []


def test_every_ledger_entry_carries_a_reason():
    import json
    ledger = json.loads((ROOT / "scripts" / "identifier_key_compare_ledger.json").read_text())
    assert ledger["sites"]
    assert all(v["verdict"] and len(v["why"]) > 40 for v in ledger["sites"].values())


# ---- the sites made byte-exact -------------------------------------------------------------

def test_card_owner_in_branch_is_byte_exact(monkeypatch):
    from hermes_cli import kanban_db as kb
    monkeypatch.setattr(kb, "_PR_HEAD_REF_CACHE", {("o/r", 7): "daedalus/T_0000ABCD-fix"})
    assert kb._pr_owned_by_card("O/R", 7, "t_0000abcd") is False
    monkeypatch.setattr(kb, "_PR_HEAD_REF_CACHE", {("o/r", 7): "daedalus/t_0000abcd-fix"})
    assert kb._pr_owned_by_card("O/R", 7, "t_0000abcd") is True
    assert kb._pr_belongs_to_other_card("O/R", 7, "t_0000abcd") is False


def test_model_pin_is_byte_exact():
    from hermes_cli import kanban_worker_route as wr
    assert wr._model_key(" CustomModel ") == "CustomModel"
    assert wr._is_sibling_pool_same_model("claude-bpr", "claude-apr", "CustomModel", "custommodel") is False
    assert wr._is_sibling_pool_same_model("claude-bpr", "CLAUDE-APR", "claude-opus-5-5", "claude-opus-5-5") is True
