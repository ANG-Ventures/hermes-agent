"""No card is STORED with a ``requires:`` line hermes-home's studio-requires-reason refuses (t_1aea9763).

``create_task`` appended every hard parent word to the child as a bare ``requires:`` line after the
caller's body had passed the rule (53 of 107 bare studio-state lines on 2026-11-12). Now a Studio-only
word (served by no pool host) is not inherited, the create says so, and create_task re-checks the
body it is about to INSERT. The fail-fast re-route writes a reasoned line too.

Hermetic: a temp board with its own capability map and a stand-in for the hook's create_refusal()
(same line grammar, note cut, >= 3-word reason, Studio-only set computed from the map).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_access_bound_pin as kabp
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_host_capabilities as khc
from hermes_cli import kanban_placement_failfast as pff
from hermes_cli import kanban_requires_inherit as kri
from hermes_cli import kanban_worker_exit as kwe
from hermes_cli import kanban_worker_hosts as kwh

HOOK = r'''
import json, os, re
from pathlib import Path
_REQ_LINE_RE = re.compile(r"^[ \t]*requires:[ \t]*(.*?)[ \t]*\r?$", re.IGNORECASE | re.MULTILINE)
_REQ_NOTE_RE = re.compile(r"\s(?:—|–|--|#)|[(#]")
_REQ_SPLIT_RE = re.compile(r"[,\s]+")
_NOTE_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'./~_-]*")
def _words(v):
    return [w for w in (w.strip("`'\".;") for w in _REQ_SPLIT_RE.split(v.strip().strip("`").lower())) if w]
def studio_only_caps(root):
    hosts = json.loads((Path(root) / "fleet" / "host-capabilities.json").read_text())["hosts"]
    pool = set().union(*(set(r["capabilities"]) for h, r in hosts.items() if h != "studio"))
    return (set(hosts["studio"]["capabilities"]) - pool) | {"control-plane-read"}
def pool_hosts(root):
    return set()
def verdict(body, hosts):
    return None
def create_refusal(body, root):
    if os.environ.get("STUDIO_REQUIRES_REASON", "").strip().lower() in ("0", "off", "false", "no"):
        return None
    only = studio_only_caps(root)
    for m in _REQ_LINE_RE.finditer(body or ""):
        raw = m.group(1)
        parts = _REQ_NOTE_RE.split(raw, maxsplit=1)
        hit = [w for w in (_words(parts[0]) or _words(raw)) if w in only]
        if hit and len(_NOTE_WORD_RE.findall(parts[1] if len(parts) > 1 else "")) < 3:
            return "BLOCKED by studio-requires-reason: `%s` gives no reason" % m.group(0).strip()
    return None
'''

# studio-state is Studio-only; docker is also served by ace-ai, so it is not.
MAP = {
    "schema": 1,
    "capabilities": {"studio-state": {}, "docker": {}, "browser-session": {}},
    "hosts": {"studio": {"capabilities": ["studio-state", "docker", "browser-session"]},
              "ace-ai": {"capabilities": ["docker"]}, "ace-media": {"capabilities": []}},
}
PARENT = ("parent work\n"
          "requires: studio-state, docker (reads the live session database file; builds an image)\n")
REFUSAL = "BLOCKED by studio-requires-reason"


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "hooks").mkdir(parents=True)
    (home / "hooks" / "access_bound_create_policy.py").write_text(HOOK, encoding="utf-8")
    (home / "fleet").mkdir()
    (home / "fleet" / khc.MAP_FILE).write_text(json.dumps(MAP), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.delenv("STUDIO_REQUIRES_REASON", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_OWNER_PID", raising=False)
    monkeypatch.setattr(kabp, "_fleet_root", lambda: home)
    monkeypatch.setattr(kwh, "BOOT_PLACED_HOST", None)
    monkeypatch.setattr(kwh, "remote_workspace_has_content", lambda raw, runner=None: False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    assert kabp.studio_refusal("x\nrequires: studio-state\n")      # the stand-in refuses a bare line
    return home


def _all_stored_pass():
    """THE invariant: no stored body is one the rule refuses."""
    with kbc.connect_closing() as conn:
        rows = conn.execute("SELECT id, body FROM tasks").fetchall()
    bad = [(r["id"], kabp.studio_refusal(r["body"])) for r in rows if kabp.studio_refusal(r["body"])]
    assert bad == [], bad
    return len(rows)


def _parent():
    with kbc.connect_closing() as conn:
        return kb.create_task(conn, title="parent", assignee="peer", body=PARENT, session_id="none",
                              session_explicit=True)


def _body(tid):
    with kbc.connect_closing() as conn:
        return kb.get_task(conn, tid).body


def _comments(tid):
    with kbc.connect_closing() as conn:
        return [c.body for c in kb.list_comments(conn, tid)]


def _assert_not_inherited(child, parent, err):
    body = _body(child)
    assert "studio-state" not in khc.declared_words(body), body
    assert "docker" in khc.declared_words(body), body      # a pool-served word still inherits
    line = f"{kri.NOT_INHERITED_PREFIX}studio-state (from {parent})"
    assert line in err, err
    assert [c for c in _comments(child) if c.startswith(line)], _comments(child)


def test_kernel_child_of_studio_pinned_parent_drops_studio_only_word(board, capsys):
    parent = _parent()
    with kbc.connect_closing() as conn:
        child = kb.create_task(conn, title="child", assignee="peer", body="child work",
                               parents=[parent], parents_kind="derived-from")
    _assert_not_inherited(child, parent, capsys.readouterr().err)
    assert _all_stored_pass() == 2


def test_cli_child_create_drops_studio_only_word(board, tmp_path, capsys):
    parent = _parent()
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    args = root.parse_args(["kanban", "create", "child", "--body", "child work", "--parent", parent,
                            "--parent-kind", "derived-from", "--unhomed", "--assignee", "peer"])
    assert kc._cmd_create(args) == 0
    err = capsys.readouterr().err
    with kbc.connect_closing() as conn:
        child = [t.id for t in kb.list_tasks(conn) if t.id != parent][0]
    _assert_not_inherited(child, parent, err)
    _all_stored_pass()


def test_tool_child_create_drops_studio_only_word(board, capsys):
    from tools import kanban_tools as kt
    parent = _parent()
    out = json.loads(kt._handle_create({"board": "default", "title": "child", "assignee": "peer",
                                        "body": "child work", "parents": [parent],
                                        "parents_kind": "derived-from", "unhomed": True}))
    assert out.get("ok") is True, out
    _assert_not_inherited(out["task_id"], parent, capsys.readouterr().err)
    _all_stored_pass()


def test_continues_still_inherits_with_a_reason_the_rule_accepts(board, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: True)
    parent = _parent()
    with kbc.connect_closing() as conn:
        conn.execute("UPDATE tasks SET workspace_path=?, origin_host=? WHERE id=?",
                     (str(tmp_path / "state"), "ace-media", parent))
        child = kb.create_task(conn, title="successor", assignee="peer", continues=parent)
    body = _body(child)
    assert {"studio-state", "docker"} <= set(khc.declared_words(body)), body
    assert f"inherited from {parent} by --continues" in body
    assert kri.NOT_INHERITED_PREFIX not in capsys.readouterr().err
    _all_stored_pass()


def test_unreadable_map_inherits_everything_as_before(board):
    parent = _parent()
    (board / "fleet" / khc.MAP_FILE).unlink()           # vocabulary unknown: the hook stand-in fails open too
    (board / "hooks" / "access_bound_create_policy.py").unlink()
    with kbc.connect_closing() as conn:
        child = kb.create_task(conn, title="child", assignee="peer", parents=[parent])
    assert {"studio-state", "docker"} <= set(khc.declared_words(_body(child)))


def test_stored_body_check_refuses_a_bare_pin_appended_inside_create_task(board, monkeypatch):
    parent = _parent()
    # A future writer inside create_task that appends a bare Studio pin after the caller check.
    monkeypatch.setattr(kri, "inherited_line", lambda *a, **k: ("requires: studio-state", []))
    with kbc.connect_closing() as conn, pytest.raises(kri.StoredBodyRefused, match=REFUSAL):
        kb.create_task(conn, title="child", assignee="peer", body="child work", parents=[parent])
    assert _all_stored_pass() == 1                       # nothing was written
    monkeypatch.setenv("STUDIO_REQUIRES_REASON", "0")    # the one kill switch disarms it
    with kbc.connect_closing() as conn:
        kb.create_task(conn, title="child", assignee="peer", body="child work", parents=[parent])


def test_stored_body_check_fails_open_without_the_hook(board, monkeypatch):
    (board / "hooks" / "access_bound_create_policy.py").unlink()
    monkeypatch.setattr(kri, "inherited_line", lambda *a, **k: ("requires: studio-state", []))
    with kbc.connect_closing() as conn:
        kb.create_task(conn, title="x", assignee="peer", body="work")


def test_tool_and_cli_refuse_a_bare_caller_line_through_the_stored_check(board, monkeypatch, capsys):
    """The caller-side checks are bypassed here, so only the stored-body check can refuse."""
    from tools import kanban_tools as kt
    monkeypatch.setattr(kabp, "studio_refusal", lambda body, _real=kabp.studio_refusal: None)
    monkeypatch.setattr(kri, "stored_refusal", lambda body: "BLOCKED by studio-requires-reason: stub"
                        if "requires: studio-state\n" in (body or "") + "\n" else None)
    out = json.loads(kt._handle_create({"board": "default", "title": "p", "assignee": "peer",
                                        "body": "w\nrequires: studio-state", "unhomed": True}))
    assert not out.get("ok") and REFUSAL in json.dumps(out), out
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    args = root.parse_args(["kanban", "create", "p", "--body", "w\nrequires: studio-state", "--unhomed"])
    assert kc._cmd_create(args) == 2
    assert REFUSAL in capsys.readouterr().err
    assert _all_stored_pass() == 0


def test_failfast_reroute_writes_a_reasoned_line(board, monkeypatch):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="drain", assignee="peer", body="host: ace-ai\ndrain the queue")
        assert kb.claim_task(conn, tid) is not None
        run = kb.latest_run(conn, tid).id
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run))
    monkeypatch.setenv(kwh.PLACEMENT_ENV, json.dumps({"host": "ace-ai", "env": {"TERMINAL_ENV": "ssh"}}))
    line = f"{pff.token_for('studio-state')} cap=studio-state host=ace-ai: the live board is on the Studio"
    assert kwe.block_on_rail_absent(line) is True
    body = _body(tid)
    assert "studio-state" in khc.declared_words(body), body
    assert body.rstrip().endswith("requires: studio-state (re-routed after STUDIO_STATE_ABSENT on ace-ai)")
    _all_stored_pass()


# ── t_41763e36: three gaps after agent PR 303 ────────────────────────────────────────────────────

def test_tool_result_carries_the_not_inherited_line(board):
    """The creating agent reads the tool result, not the process stderr."""
    from tools import kanban_tools as kt
    parent = _parent()
    out = json.loads(kt._handle_create({"board": "default", "title": "child", "assignee": "peer",
                                        "body": "child work", "parents": [parent],
                                        "parents_kind": "derived-from", "unhomed": True}))
    assert out.get("ok") is True, out
    note = out.get("requires_not_inherited") or ""
    assert note.startswith(f"{kri.NOT_INHERITED_PREFIX}studio-state (from {parent})"), out
    plain = json.loads(kt._handle_create({"board": "default", "title": "orphan", "assignee": "peer",
                                          "body": "work", "unhomed": True}))
    assert "requires_not_inherited" not in plain, plain


def test_failure_ownership_card_with_a_bare_pin_is_refused_like_create_task(board):
    """kanban_failure_ownership inserts its owner card itself; the stored-body check still runs."""
    from hermes_cli import kanban_failure_ownership as fo
    key = json.dumps([1, "fleet", "", "github.com", 1, 2, ".github/workflows/ci.yml", [],
                      [["os", "linux"]], "suite", "unit:invocation", "pytest-node", "tests/t.py::t"],
                     separators=(",", ":"))
    obs = fo.Observation(key=key, source_key="red", result="failure", sha="a" * 40, run_id=1,
                         attempt=1, job_id=1, lineage=("a" * 40,), policy_generation=1)
    with kbc.connect_closing() as conn:
        with kb.write_txn(conn):
            conn.execute("UPDATE failure_producers SET enabled=1,enrolled=1")
            conn.execute("UPDATE failure_authority SET mode='on'")
        ref = fo.publish_evidence(conn, json.dumps(fo.asdict(obs)).encode(), verifier=lambda _: obs)
        card = fo.prepare_card(conn, title="repair", body="fix the red\nrequires: studio-state",
                               assignee=None, session_id="operator")
        result = fo.observe_failure(conn, ref, producer="ci-red-owner", card_spec=card)
        assert result.action == "EVIDENCE_PENDING" and result.card_id is None, result
        assert REFUSAL in result.reason, result
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
        with pytest.raises(kri.StoredBodyRefused, match=REFUSAL):    # the same refusal create_task gives
            kb.create_task(conn, title="repair", assignee="peer", body="fix the red\nrequires: studio-state")
        reasoned = fo.prepare_card(conn, title="repair", assignee=None, session_id="operator",
                                   body="fix the red\nrequires: studio-state (reads the live board file)")
        assert fo.reconcile_pending(conn, producer="ci-red-owner", card_spec=reasoned)[0].card_id
    _all_stored_pass()


_INSERT_TASKS_RE = __import__("re").compile(r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+[\"'`]?tasks\b", __import__("re").I)


def _unchecked_task_inserts(root: Path):
    """Every ``INSERT INTO tasks`` in shipped Python outside ``kanban_db._insert_task_row``."""
    import ast
    hits = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if rel.parts[0] in {"tests", ".venv", "venv", "node_modules", ".git", "website", "web"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if not _INSERT_TASKS_RE.search(text):
            continue
        allowed = set()
        if rel.as_posix() == "hermes_cli/kanban_db.py":
            for node in ast.walk(ast.parse(text)):
                if isinstance(node, ast.FunctionDef) and node.name == "_insert_task_row":
                    allowed = set(range(node.lineno, node.end_lineno + 1))
        for n, line in enumerate(text.splitlines(), 1):
            if _INSERT_TASKS_RE.search(line) and n not in allowed:
                hits.append(f"{rel}:{n}")
    return hits


def test_every_task_insert_goes_through_the_checked_path(board):
    """Source contract: _insert_task_row is the one INSERT INTO tasks; it runs the stored-body check.
    A new raw insert anywhere in shipped code fails here: route it through kanban_db._insert_task_row."""
    repo = Path(__file__).resolve().parents[2]
    assert _unchecked_task_inserts(repo) == []
    with kbc.connect_closing() as conn:
        with kb.write_txn(conn), pytest.raises(kri.StoredBodyRefused, match=REFUSAL):
            kb._insert_task_row(conn, {"id": "t_00000001", "title": "x", "body": "w\nrequires: studio-state"})
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_source_contract_catches_a_planted_insert(tmp_path):
    (tmp_path / "hermes_cli").mkdir()
    (tmp_path / "hermes_cli" / "kanban_db.py").write_text(
        "def _insert_task_row(conn, f):\n    conn.execute('INSERT INTO tasks (id) VALUES (?)', (1,))\n")
    assert _unchecked_task_inserts(tmp_path) == []
    (tmp_path / "hermes_cli" / "new_writer.py").write_text(
        "def mint(conn):\n    conn.execute(\n        \"INSERT OR IGNORE INTO tasks (id, body) VALUES (?, ?)\", (1, 'b'))\n")
    assert _unchecked_task_inserts(tmp_path) == ["hermes_cli/new_writer.py:3"]


def test_parent_rehomed_before_the_write_lock_keeps_the_inherited_line(board, monkeypatch):
    """kanban_db create_task re-resolves the home under the write lock and rebuilds the body from the
    caller's text; the inherited ``requires:`` line used to be lost on that rebuild."""
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="peer", body=PARENT, session_id="sess-a")
    real = kb._resolve_birth_session
    calls = []

    def racing(c, *a, **k):
        out = real(c, *a, **k)
        calls.append(out)
        if len(calls) == 1:
            with kbc.connect_closing() as other:
                other.execute("UPDATE tasks SET session_id = 'sess-b' WHERE id = ?", (parent,))
                other.commit()
        return out

    monkeypatch.setattr(kb, "_resolve_birth_session", racing)
    with kbc.connect_closing() as conn:
        child = kb.create_task(conn, title="child", assignee="peer", body="child work", parents=[parent])
        assert kb.get_task(conn, child).session_id == "sess-b"       # the race really happened
    assert len(calls) == 2 and calls[0] != calls[1], calls
    assert "docker" in khc.declared_words(_body(child)), _body(child)
