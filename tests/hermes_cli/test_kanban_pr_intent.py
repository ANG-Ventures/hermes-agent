"""The card's Intent block at the top of the worker's own PR body at handback (t_deee5435, parent t_897eeabb)."""

from __future__ import annotations

import json
import sqlite3
import subprocess

import pytest

from hermes_cli import kanban_open_pr as op
from hermes_cli import kanban_pr_freshness as fr
from hermes_cli import kanban_pr_intent as pi

TASK = "t_deee5435"
PR_URL = "https://github.com/o/r/pull/5"
HEAD = "a" * 40
CARD_BODY = ("origin: discord\n\nWrite the Intent block into worker PR bodies.\n\n"
             "Acceptance: the PR body starts with the block.\n\n## Declared file scope\nscope: hermes_cli/")


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    monkeypatch.setattr(op, "FLEET_OWNERS", op.FLEET_OWNERS | {"o"})
    cfg = tmp_path / "worker-pr-draft.json"
    cfg.write_text(json.dumps({"enabled": False, "repos": ["O/R"]}))
    monkeypatch.setenv("KANBAN_WORKER_PR_DRAFT", str(cfg))
    monkeypatch.delenv("KANBAN_PR_INTENT", raising=False)
    monkeypatch.delenv("KANBAN_HANDOFF_PR_INTENT", raising=False)
    monkeypatch.setattr(pi, "library_path", lambda: None)   # in-repo renderer unless a test says otherwise


class FakeGh:
    """One PR on o/r: ``gh api`` for the intent writer (CompletedProcess) and the freshness gh (dict)."""

    def __init__(self, body="Worker notes.", head_ref=f"daedalus-opus/{TASK}-x", draft=True):
        self.body, self.head_ref, self.draft = body, head_ref, draft
        self.log = []   # ordered: ("patch", body) / ("ready", repo, n)

    def api(self, *args):   # kanban_pr_intent seam
        if args[0] == "-X":
            body = next(a[len("body="):] for a in args if a.startswith("body="))
            self.log.append(("patch", body))
            self.body = body
            return subprocess.CompletedProcess(args, 0, "{}", "")
        return subprocess.CompletedProcess(args, 0, json.dumps({"body": self.body}), "")

    def fresh(self, *args):   # kanban_pr_freshness seam
        if args[0] == "-X":
            return {"message": "ok"}
        path = args[-1]
        if path.endswith("/pulls/5"):
            return {"draft": self.draft, "head": {"sha": HEAD, "ref": self.head_ref}, "base": {"ref": "main"},
                    "mergeable_state": "clean", "body": self.body}
        if "/compare/" in path:
            return {"behind_by": 0}
        if path.endswith("check-runs?per_page=100"):
            return {"check_runs": []}
        return None

    def ready(self, repo, n):
        self.log.append(("ready", repo, n))
        return 0


def _card(body=CARD_BODY):
    return {"id": TASK, "title": "Intent block into worker PRs", "body": body}


def _check(g, intent):
    return fr.check(op.extract_pr_refs(PR_URL), task_id=TASK, allow_arm=True, gh=g.fresh, ready=g.ready, intent=intent, event_land=False)


# ---- handback wiring (t_12cdc480's acceptance, carried here) -------------------------------------------------------

def test_handback_patches_body_with_block_at_top_before_the_ready_flip():
    g = FakeGh()
    rep = _check(g, pi.applier(_card(), ["live-evidence: no", "tests: 12 passed"], gh=g.api))
    assert [e[0] for e in g.log] == ["patch", "ready"]
    body = g.log[0][1]
    assert body.startswith("<!-- pr-intent:start")
    assert "Acceptance: the PR body starts with the block." in body
    assert "live-evidence: no" in body and body.rstrip().endswith("Worker notes.")
    assert rep["prs"]["o/r#5"]["intent"].startswith("written")


def test_reapply_replaces_in_place_not_appends():
    g = FakeGh()
    _check(g, pi.applier(_card(), ["tests: 3 passed"], gh=g.api))
    g.draft = False
    _check(g, pi.applier(_card(), ["tests: 12 passed", "live-evidence: no"], gh=g.api))
    assert g.body.count(pi.START) == 1 and g.body.count(pi.END) == 1
    assert "12 passed" in g.body and "3 passed" not in g.body
    assert g.body.rstrip().endswith("Worker notes.")
    before = len(g.log)
    rep = _check(g, pi.applier(_card(), ["tests: 12 passed", "live-evidence: no"], gh=g.api))
    assert len(g.log) == before   # identical re-apply writes nothing
    assert rep["prs"]["o/r#5"]["intent"].startswith("unchanged")


@pytest.mark.parametrize("var", ["KANBAN_PR_INTENT", "KANBAN_HANDOFF_PR_INTENT"])
def test_kill_switch_keeps_old_body(monkeypatch, var):
    monkeypatch.setenv(var, "0")
    g = FakeGh()
    intent = pi.applier(_card(), ["x"], gh=g.api)
    assert intent is None
    _check(g, intent)
    assert g.body == "Worker notes." and [e[0] for e in g.log] == ["ready"]


def test_config_kill_switch(monkeypatch):
    import hermes_cli.config as cfg
    monkeypatch.setattr(cfg, "load_config", lambda: {"kanban": {"pr_intent": False}})
    assert pi.enabled() is False


def test_foreign_pr_never_written():
    g = FakeGh(head_ref="daedalus/t_deadbeef-other", draft=False)
    _check(g, pi.applier(_card(), ["x"], gh=g.api))
    assert g.log == [] and g.body == "Worker notes."


@pytest.mark.parametrize("fail", ["read", "write", "raise"])
def test_failure_is_logged_in_metadata_and_never_blocks(fail):
    g = FakeGh()

    def api(*args):
        if fail == "raise":
            raise OSError("boom")
        if (fail == "read") != (args[0] == "-X"):
            return subprocess.CompletedProcess(args, 1, "", "HTTP 502")
        return g.api(*args)
    rep = _check(g, pi.applier(_card(), ["x"], gh=api))
    assert rep["prs"]["o/r#5"]["intent"].startswith("error")
    assert ("ready", "o/r", 5) in g.log and rep["draft_flipped"] == ["o/r#5"]


def test_body_changed_twice_writes_nothing():
    g = FakeGh()
    n = {"reads": 0}

    def api(*args):
        if args[0] != "-X":
            n["reads"] += 1
            g.body = "edit %d" % n["reads"]
        return g.api(*args)
    rep = _check(g, pi.applier(_card(), ["x"], gh=api))
    assert not any(e[0] == "patch" for e in g.log)
    assert "changed twice" in rep["prs"]["o/r#5"]["intent"]


def test_refused_handoff_writes_no_body():
    g = FakeGh(head_ref="daedalus/t_deadbeef-other")   # foreign draft refuses
    with pytest.raises(fr.DraftPrError):
        _check(g, pi.applier(_card(), ["x"], gh=g.api))
    assert g.log == []


def test_no_library_falls_back_and_bad_library_falls_back(monkeypatch, tmp_path):
    bad = tmp_path / "pr_intent.py"
    bad.write_text("import sys; sys.exit(2)\n")
    monkeypatch.setattr(pi, "library_path", lambda: bad)
    section, renderer = pi.render(_card(), ["x"])
    assert renderer == "fallback" and section.startswith(pi.START) and section.endswith(pi.END)


def test_library_renderer_is_used_when_present(monkeypatch, tmp_path):
    lib = tmp_path / "pr_intent.py"
    lib.write_text("import json,sys\nd=json.load(open(sys.argv[3]))\n"
                   "print('%s\\nLIB ' + d['task']['id'] + '\\n%s')\n" % (pi.START, pi.END))
    monkeypatch.setattr(pi, "library_path", lambda: lib)
    section, renderer = pi.render(_card(), [])
    assert renderer == "library" and f"LIB {TASK}" in section


def test_library_stdout_is_utf8_under_a_legacy_code_page(monkeypatch, tmp_path):
    """Prism b3697fd74088: the parent decodes the helper's stdout as UTF-8, so the helper must write UTF-8 even where
    the locale's encoding is a legacy code page (Windows cp1252). PYTHONIOENCODING=cp1252 in the parent env stands in
    for that locale; without the child-env override, '·' arrives as U+FFFD."""
    lib = tmp_path / "pr_intent.py"
    lib.write_text("import json,sys\nd=json.load(open(sys.argv[3], encoding='utf-8'))\n"
                   "print('%s\\n' + d['task']['title'] + '\\n%s')\n" % (pi.START, pi.END), encoding="utf-8")
    monkeypatch.setattr(pi, "library_path", lambda: lib)
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    card = dict(_card(), title="origin · séance — 漢")
    section, renderer = pi.render(card, [])
    assert renderer == "library"
    assert "origin · séance — 漢" in section and "\ufffd" not in section


# ---- rendering ---------------------------------------------------------------------------------------------------

def test_card_without_acceptance_has_no_empty_heading():
    s = pi.build_section(TASK, "T", "Just the ask.", [])
    assert "Acceptance" not in s and "Just the ask." in s
    assert len(pi.build_section(TASK, "T", "x " * 5000, ["y"] * 400)) <= pi.INTENT_MAX


def test_end_marker_in_card_text_round_trips():
    s = pi.build_section(TASK, "T", "Keep <!-- pr-intent:end --> literally.", [])
    b = pi.merge_body("notes", s)
    assert pi.merge_body(b, s) == b and b.count(pi.END) == 1


def test_rulings_come_from_ruling_comments_and_verification_from_handback(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE tasks (id TEXT, title TEXT, body TEXT)")
    conn.execute("CREATE TABLE task_comments (id INTEGER PRIMARY KEY, task_id TEXT, body TEXT, author TEXT DEFAULT 'apollo')")
    conn.execute("INSERT INTO tasks VALUES (?, ?, ?)", (TASK, "T", CARD_BODY))
    conn.executemany("INSERT INTO task_comments (task_id, body) VALUES (?, ?)",
                     [(TASK, "ruling: no router change\n\nsecond paragraph of the ruling"),
                      (TASK, "chatter about ruling: no")])
    card = pi.card_from_board(conn, TASK)
    ver = pi.verification_lines("Shipped it.", {"live_evidence": "no", "tests": {"unit": "12 OK"}})
    s = pi.build_section(card["id"], card["title"], card["body"], ver)
    rulings = s.split("**Rulings (verbatim)**", 1)[1].split("**Verification", 1)[0]
    assert "ruling: no router change" in rulings and "second paragraph of the ruling" in rulings
    assert "chatter" not in s
    assert "live-evidence: no" in s and '"unit": "12 OK"' in s and "Shipped it." in s


def test_complete_task_hands_check_an_intent_built_from_the_board(tmp_path, monkeypatch):
    """End to end through kanban_db.complete_task on a real board: the card row + its `ruling:` comment + the
    handback summary reach the PR body via the ``intent`` the freshness gate receives."""
    from pathlib import Path
    from hermes_cli import kanban_db as kb
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(op, "_default_query", lambda: (lambda repo, number: {"state": "OPEN"}))
    monkeypatch.setenv("KANBAN_PRE_PR_SCRIPT", str(tmp_path / "absent.py"))
    monkeypatch.setattr("hermes_cli.kanban_survivor.preserve", lambda *a, **k: None)
    g = FakeGh(draft=False)
    real_applier = pi.applier
    monkeypatch.setattr(pi, "applier", lambda card, ver, gh=None: real_applier(card, ver, gh=g.api))
    seen = {}

    def fake_check(refs, **kw):
        seen["intent"] = kw.get("intent")
        if kw["intent"] is not None:
            for ref in refs:
                seen["status"] = kw["intent"](ref.repo, ref.number)
        return {"prs": {}}
    monkeypatch.setattr(fr, "check", fake_check)
    kb.init_db()
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="impl", assignee="daedalus", body=CARD_BODY)
        kb.add_comment(conn, tid, "apollo", "ruling: keep Prism unchanged")
        kb.claim_task(conn, tid)
        kb.complete_task(conn, tid, summary="Did it.\nlive-evidence: no", survivor_pr="o/r#5")
    assert seen["status"].startswith("written")
    assert g.body.startswith(pi.START) and f"`{tid}`" in g.body
    assert "ruling: keep Prism unchanged" in g.body and "Did it." in g.body and "live-evidence: no" in g.body


@pytest.mark.parametrize("heading", ["Rulings", "Ruling", "Rulings:"])
def test_heading_only_rulings_survive_long_ask(heading):
    body = "## Ask\n" + "x" * 4000 + f"\n\n## {heading}\n\nKeep v1 support.\n\nNever delete rows."
    section = pi.build_section(TASK, "T", body)
    assert "Keep v1 support." in section and "Never delete rows." in section
    assert pi.parse_card(body)["rulings"] == ["Keep v1 support.", "Never delete rows."]


def test_heading_rulings_use_corrected_renderer_even_with_old_library(monkeypatch, tmp_path):
    lib = tmp_path / "pr_intent.py"
    lib.write_text("raise AssertionError('old library must not render heading rulings')\n")
    monkeypatch.setattr(pi, "library_path", lambda: lib)
    card = _card("## Ask\n" + "x" * 4000 + "\n\n## Rulings\n\nKeep v1 support.")
    section, renderer = pi.render(card, [])
    assert renderer == "fallback" and "Keep v1 support." in section


def test_untrusted_ruling_comments_are_not_published():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE tasks (id TEXT, title TEXT, body TEXT)")
    conn.execute("CREATE TABLE task_comments (id INTEGER PRIMARY KEY, task_id TEXT, author TEXT, body TEXT)")
    conn.execute("INSERT INTO tasks VALUES (?, ?, ?)", (TASK, "T", CARD_BODY))
    conn.executemany("INSERT INTO task_comments (task_id, author, body) VALUES (?, ?, ?)",
                     [(TASK, author, f"ruling: from {author}") for author in
                      ("apollo", "human:ace", "daedalus", "unverified:apollo", "", "apollo [subagent]")])
    card = pi.card_from_board(conn, TASK)
    assert "ruling: from apollo\n" in card["body"]
    assert "ruling: from human:ace" in card["body"]
    for author in ("daedalus", "unverified:apollo", "apollo [subagent]"):
        assert f"ruling: from {author}" not in card["body"]
    assert "ruling: from \n" not in card["body"]


def test_no_live_github_under_pytest_without_injected_gh():
    assert pi.applier(_card(), ["x"]) is None


def test_parity_with_hermes_home_library_when_present():
    """The fallback must render exactly what the library renders (same markers, cap, cut order)."""
    import importlib.util
    import os
    from pathlib import Path
    # The test home is isolated, so point PR_INTENT_LIB at a hermes-home checkout's scripts/lib/pr_intent.py.
    path = Path(os.environ["PR_INTENT_LIB"]) if os.environ.get("PR_INTENT_LIB") else None
    if path is None or not path.is_file():
        pytest.skip("set PR_INTENT_LIB=<hermes-home>/scripts/lib/pr_intent.py to run the parity check")
    spec = importlib.util.spec_from_file_location("_pr_intent_lib", path)
    lib = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lib)
    assert (lib.START, lib.END, lib.INTENT_MAX) == (pi.START, pi.END, pi.INTENT_MAX)
    for body, ver in [(CARD_BODY, ["a", "b"]), ("x " * 3000, ["y"] * 300), ("### Acceptance: h\n\nask", [])]:
        assert lib.build_section(TASK, "T", body, ver) == pi.build_section(TASK, "T", body, ver)
