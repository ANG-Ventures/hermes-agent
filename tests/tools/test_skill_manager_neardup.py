"""Near-duplicate gate on skill_manage create (tools/skill_manager_neardup.py, fork t_f78aa714).

The gate's contract with the Hermes root's scorer (``<root>/scripts/lib/skill_similarity.py``):
``THRESHOLD``, ``check(name, content, roots=, threshold=)``, ``refusal_message``,
``distinct_from_entries``, ``ledger_path``, ``append_ledger``. The scorer lives in the root repo, so
these tests plant a word-overlap stub with that interface in a temp fleet home and drive the real
create path end to end (operations shape, flat shape, config, frontmatter, ledger).
"""

import json
import pytest
from contextlib import contextmanager
from unittest.mock import patch

import hermes_yaml as yaml

import tools.skill_manager_tool as smt
from tools.skill_manager_tool import skill_manage

STUB_SCORER = '''
import json, re
from pathlib import Path
THRESHOLD = 0.5
ROOTS_SEEN = []

def _words(t):
    return set(re.findall(r"[a-z0-9]+", t.lower()))

class Corpus:
    def __init__(self, docs):
        self.docs = docs

    @classmethod
    def load(cls, roots=None):
        docs, seen = [], set()
        for root in roots or []:
            for p in sorted(Path(root).rglob("SKILL.md")):
                if p.parent.name not in seen:
                    seen.add(p.parent.name)
                    docs.append({"name": p.parent.name, "path": str(p), "text": p.read_text()})
        return cls(docs)

def check(name, content, roots=None, threshold=None, corpus=None):
    thr = THRESHOLD if threshold is None else threshold
    ROOTS_SEEN.append([str(r) for r in roots or []])
    cand, matches = _words(content), []
    for d in (corpus or Corpus.load(roots)).docs:
        if d["name"] == name:
            continue
        w = _words(d["text"])
        matches.append({"name": d["name"], "path": d["path"], "score": round(len(cand & w) / len(cand | w), 3)})
    matches.sort(key=lambda m: -m["score"])
    return {"near_duplicate": bool(matches) and matches[0]["score"] >= thr, "threshold": thr,
            "matches": matches[:3]}

def refusal_message(name, v):
    return (f"Skill '{name}' looks like a near-duplicate: "
            + ", ".join(m["name"] for m in v["matches"]) + ". Patch it, or pass justification=.")

def distinct_from_entries(v, why):
    return [{"name": m["name"], "score": m["score"], "why": why}
            for m in v["matches"] if m["score"] >= v["threshold"]]

def ledger_path(home):
    return Path(home) / "state" / "skill-neardup-ledger.jsonl"

def append_ledger(row, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps(row) + "\\n")
    return path
'''

_TIER_STUB = "def load():\n    return []\ndef tier(path, rules):\n    return 'prose'\n"

EXISTING = """\
---
name: postgres-vacuum-tuning
description: Tune Postgres autovacuum for bloated tables.
---

# postgres-vacuum-tuning

Measure dead tuples, raise the vacuum cost limit, lower the scale factor, watch freeze age.
"""
NEAR = """\
---
name: pg-autovacuum
description: Tune Postgres autovacuum for bloated tables.
---

# pg-autovacuum

Measure dead tuples, raise the vacuum cost limit, lower the scale factor, watch freeze age.
"""
DISTINCT = """\
---
name: garage-door-lockout
description: Clear a Tailwind garage door controller lockout.
---

# garage-door-lockout

Read the lockout state from the controller, power-cycle the opener, confirm the sensor.
"""


@contextmanager
def _fleet_home(tmp_path, *, scorer=True, config=None, existing_group="devops"):
    home = tmp_path / "home"
    local = home / "skills"
    shared = home / "skills-shared"
    local.mkdir(parents=True)
    for g in ("devops", "general", "devops-aux"):
        (shared / g).mkdir(parents=True)
    (home / "scripts" / "lib").mkdir(parents=True)
    (home / "scripts" / "lib" / "autocommit_tier.py").write_text(_TIER_STUB)
    if scorer:
        (home / "scripts" / "lib" / "skill_similarity.py").write_text(STUB_SCORER)
    d = shared / existing_group / "postgres-vacuum-tuning"
    d.mkdir()
    (d / "SKILL.md").write_text(EXISTING)
    with patch.object(smt, "HERMES_HOME", home), \
         patch.object(smt, "SKILLS_DIR", local), \
         patch.object(smt, "_author_to_shared_setting", return_value=None), \
         patch("agent.skill_utils.get_all_skills_dirs",
               return_value=[local, shared / "devops", shared / "general"]), \
         patch("agent.skill_utils.get_project_skills_dirs", return_value=[]), \
         patch("hermes_cli.config.load_config", return_value=config or {}):
        yield home, shared


def _create(name, content, **extra):
    op = {"action": "create", "name": name, "content": content, "category": "general", **extra}
    return json.loads(skill_manage(action="", name="", operations=[op]))


def _ledger(home):
    p = home / "state" / "skill-neardup-ledger.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def _frontmatter(path):
    text = path.read_text()
    return yaml.safe_load(text[3:text.find("\n---", 3)])


def test_near_duplicate_create_is_refused_with_top_matches(tmp_path):
    with _fleet_home(tmp_path) as (home, shared):
        res = _create("pg-autovacuum", NEAR)
        assert res["success"] is False
        assert "postgres-vacuum-tuning" in res["error"]
        assert "justification" in res["error"]
        top = res["near_duplicates"]
        assert 1 <= len(top) <= 3
        assert top[0]["name"] == "postgres-vacuum-tuning"
        assert {"name", "path", "score"} <= set(top[0])
        assert not (shared / "general" / "pg-autovacuum").exists()
        assert [(r["event"], r["name"]) for r in _ledger(home)] == [("refuse", "pg-autovacuum")]


def test_identical_name_keeps_the_old_error_and_writes_no_ledger_row(tmp_path):
    with _fleet_home(tmp_path) as (home, shared):
        res = _create("postgres-vacuum-tuning", EXISTING)
        assert res["success"] is False
        assert "A skill named 'postgres-vacuum-tuning' already exists" in res["error"]
        assert _ledger(home) == []


def test_justified_create_records_distinct_from_and_a_ledger_row(tmp_path):
    with _fleet_home(tmp_path) as (home, shared):
        res = _create("pg-autovacuum", NEAR, justification="ops runbook, not tuning theory")
        assert res["success"] is True, res
        fm = _frontmatter(shared / "general" / "pg-autovacuum" / "SKILL.md")
        rows = fm["metadata"]["hermes"]["distinct_from"]
        assert rows[0]["name"] == "postgres-vacuum-tuning"
        assert rows[0]["why"] == "ops runbook, not tuning theory"
        assert rows[0]["score"] >= 0.5
        assert fm["name"] == "pg-autovacuum" and fm["description"].startswith("Tune Postgres")
        led = _ledger(home)
        assert [(r["event"], r["name"]) for r in led] == [("justified_create", "pg-autovacuum")]
        assert led[0]["why"] == "ops runbook, not tuning theory"


def test_blank_justification_is_no_justification(tmp_path):
    with _fleet_home(tmp_path) as (home, shared):
        res = _create("pg-autovacuum", NEAR, justification="   ")
        assert res["success"] is False


def test_distinct_skill_passes_without_a_ledger_row(tmp_path):
    with _fleet_home(tmp_path) as (home, shared):
        res = _create("garage-door-lockout", DISTINCT)
        assert res["success"] is True, res
        assert (shared / "general" / "garage-door-lockout" / "SKILL.md").is_file()
        assert _ledger(home) == []


def test_flat_shape_threads_justification(tmp_path):
    with _fleet_home(tmp_path) as (home, shared):
        refused = json.loads(skill_manage("create", "pg-autovacuum", content=NEAR, category="general"))
        assert refused["success"] is False
        ok = json.loads(skill_manage("create", "pg-autovacuum", content=NEAR, category="general",
                                     justification="distinct audience"))
        assert ok["success"] is True, ok


def test_aux_folder_skills_are_compared_even_when_not_loaded(tmp_path):
    with _fleet_home(tmp_path, existing_group="devops-aux") as (home, shared):
        res = _create("pg-autovacuum", NEAR)
        assert res["success"] is False
        assert res["near_duplicates"][0]["path"].endswith("devops-aux/postgres-vacuum-tuning/SKILL.md")


def test_kill_switch_restores_identical_name_only(tmp_path):
    cfg = {"skills": {"neardup_gate": {"enabled": False}}}
    with _fleet_home(tmp_path, config=cfg) as (home, shared):
        res = _create("pg-autovacuum", NEAR)
        assert res["success"] is True, res
        assert _ledger(home) == []


def test_threshold_is_read_from_config(tmp_path):
    cfg = {"skills": {"neardup_gate": {"threshold": 0.999}}}
    with _fleet_home(tmp_path, config=cfg) as (home, shared):
        res = _create("pg-autovacuum", NEAR)
        assert res["success"] is True, res


def test_root_without_a_scorer_has_no_gate(tmp_path):
    with _fleet_home(tmp_path, scorer=False) as (home, shared):
        res = _create("pg-autovacuum", NEAR)
        assert res["success"] is True, res


# --- Prism r1 on agent#2513 (64302917a09b) ---------------------------------------------------

def test_dash_prefixed_key_does_not_split_frontmatter(tmp_path):
    """cf115a6ad9aa: split on the validator's closing fence, never on any line starting '---'."""
    content = NEAR.replace("bloated tables.\n---", "bloated tables.\n---custom: keep\nmetadata: {owner: ops}\n---", 1)
    with _fleet_home(tmp_path) as (home, shared):
        res = _create("pg-autovacuum", content, justification="ops runbook")
        assert res["success"] is True, res
        text = (shared / "general" / "pg-autovacuum" / "SKILL.md").read_text()
        fm_text = text[3:text.find("\n---\n", 3)]
        assert [ln for ln in fm_text.splitlines() if ln.startswith("metadata:")] == ["metadata:"]
        fm = yaml.safe_load(fm_text)
        assert fm["---custom"] == "keep"
        assert fm["metadata"]["owner"] == "ops"
        assert fm["metadata"]["hermes"]["distinct_from"][0]["name"] == "postgres-vacuum-tuning"


def test_non_list_distinct_from_fails_and_batch_rolls_back(tmp_path):
    """d92a62873586: a malformed existing annotation is an ordinary failure, so the batch rolls back."""
    content = NEAR.replace("bloated tables.\n---", "bloated tables.\nmetadata: {hermes: {distinct_from: 1}}\n---", 1)
    with _fleet_home(tmp_path) as (home, shared):
        existing = shared / "devops" / "postgres-vacuum-tuning" / "SKILL.md"
        ops = [{"action": "patch", "name": "postgres-vacuum-tuning",
                "old_string": "watch freeze age.", "new_string": "watch freeze age closely."},
               {"action": "create", "name": "pg-autovacuum", "content": content, "category": "general",
                "justification": "different audience"}]
        res = json.loads(skill_manage(action="", name="", operations=ops))
        assert res["success"] is False, res
        assert "distinct_from" in json.dumps(res)
        assert existing.read_text() == EXISTING
        assert not (shared / "general" / "pg-autovacuum").exists()


def test_scorer_reload_failure_after_write_keeps_the_create(tmp_path):
    """bab2ff5781b7: the ledger step is audit only; a scorer that breaks after the write cannot fail it."""
    from tools import skill_manager_neardup as nd
    real = nd._load_scorer
    calls = []

    def flaky(root):
        calls.append(root)
        if len(calls) > 1:
            raise SyntaxError("scorer replaced mid-create")
        return real(root)

    with _fleet_home(tmp_path) as (home, shared), patch.object(nd, "_load_scorer", flaky):
        res = _create("pg-autovacuum", NEAR, justification="ops runbook")
        assert res["success"] is True, res
        assert (shared / "general" / "pg-autovacuum" / "SKILL.md").is_file()
        assert len(calls) == 2


def test_justification_cannot_push_skill_md_past_the_size_limit(tmp_path):
    """90ee9a3bba3e: the rewritten content is validated again before anything is written."""
    with _fleet_home(tmp_path) as (home, shared), patch.object(smt, "MAX_SKILL_CONTENT_CHARS", len(NEAR) + 50):
        res = _create("pg-autovacuum", NEAR, justification="x" * 500)
        assert res["success"] is False, res
        assert "limit" in res["error"]
        assert not (shared / "general" / "pg-autovacuum").exists()


@pytest.mark.parametrize("frontmatter, field", [
    ("metadata:\n  hermes: existing-value\n", "metadata.hermes"),
    ("metadata: existing-value\n", "metadata"),
])
def test_non_mapping_metadata_container_is_refused_not_replaced(frontmatter, field):
    """346629c28142: a non-mapping metadata / metadata.hermes is the author's data; a justified create
    must refuse it, never silently replace it with {} and report success. Prism repro, verbatim."""
    from tools.skill_manager_neardup import DistinctFromError, add_distinct_from
    content = f"---\nname: example\n{frontmatter}---\nbody\n"
    with pytest.raises(DistinctFromError, match=field):
        add_distinct_from(content, [{"name": "other"}])


def test_justified_create_with_scalar_hermes_metadata_fails_and_keeps_nothing(tmp_path):
    """The tool path: the same malformed container fails the create; no skill dir is written."""
    content = NEAR.replace("bloated tables.\n---", "bloated tables.\nmetadata:\n  hermes: existing-value\n---", 1)
    with _fleet_home(tmp_path) as (home, shared):
        res = json.loads(skill_manage(action="create", name="pg-autovacuum", content=content,
                                      category="general", justification="different audience"))
        assert res["success"] is False, res
        assert "metadata.hermes" in json.dumps(res)
        assert not (shared / "general" / "pg-autovacuum").exists()


def test_non_string_justification_is_a_structured_failure_and_the_batch_rolls_back(tmp_path):
    """fa7fa70165e2: a truthy non-string justification must be an ordinary operation failure, so an atomic
    batch rolls back its earlier ops instead of escaping with AttributeError past _rollback."""
    with _fleet_home(tmp_path) as (home, shared):
        target = shared / "devops" / "postgres-vacuum-tuning" / "SKILL.md"
        before = target.read_text()
        res = json.loads(skill_manage("", "", operations=[
            {"action": "patch", "name": "postgres-vacuum-tuning",
             "old_string": "watch freeze age", "new_string": "watch freeze age closely"},
            {"action": "create", "name": "pg-autovacuum", "category": "general", "content": NEAR,
             "justification": ["different audience"]}]))
        assert res["success"] is False, res
        assert "justification" in json.dumps(res)
        assert target.read_text() == before, "earlier op not rolled back"
        assert not (shared / "general" / "pg-autovacuum").exists()


def test_distinct_from_does_not_leak_through_yaml_aliases():
    """b8f3589b6894: metadata.hermes aliased to another mapping must not receive distinct_from twice."""
    from tools.skill_manager_neardup import add_distinct_from
    out = add_distinct_from("---\nname: demo\ndescription: demo\nmetadata:\n  template: &h {tag: keep}\n  hermes: *h\n---\nbody\n",
                            [{"name": "existing"}])
    fm = yaml.safe_load(out.split("---")[1])
    assert "distinct_from" not in fm["metadata"]["template"], out
    assert fm["metadata"]["hermes"]["distinct_from"] == [{"name": "existing"}]
    assert fm["metadata"]["template"] == {"tag": "keep"}


# --- Carried Prism residue from agent#2513 (t_7c67781d) -------------------------------------------

def test_scorer_is_registered_in_sys_modules_while_it_executes(tmp_path):
    """a51c70d89c38: a scorer that resolves its own module (dataclasses, pickle) during import must load."""
    import sys
    from tools import skill_manager_neardup as nd
    with _fleet_home(tmp_path) as (home, shared):
        lib = home / "scripts" / "lib" / "skill_similarity.py"
        lib.write_text("import sys\nassert sys.modules[__name__] is not None\n" + STUB_SCORER)
        nd._scorer_cache.clear()
        res = _create("pg-autovacuum", NEAR)
        assert res["success"] is False and res["near_duplicates"], res


def test_failed_scorer_import_leaves_no_module_behind(tmp_path):
    import sys
    from tools import skill_manager_neardup as nd
    with _fleet_home(tmp_path) as (home, shared):
        (home / "scripts" / "lib" / "skill_similarity.py").write_text("raise RuntimeError('broken scorer')\n")
        nd._scorer_cache.clear()
        sys.modules.pop(nd._SCORER_MODULE, None)
        res = _create("pg-autovacuum", NEAR)
        assert res["success"] is True, res             # fail-open, as before
        assert nd._SCORER_MODULE not in sys.modules


def test_trusted_project_skills_are_compared(tmp_path):
    """9393a1595eda: a trusted project .agents/skills dir outranks the others at load time, so it is compared."""
    proj = tmp_path / "proj" / ".agents" / "skills"
    (proj / "pg-proj").mkdir(parents=True)
    (proj / "pg-proj" / "SKILL.md").write_text(EXISTING.replace("postgres-vacuum-tuning", "pg-proj"))
    with _fleet_home(tmp_path) as (home, shared), \
            patch("agent.skill_utils.get_project_skills_dirs", return_value=[proj]):
        (shared / "devops" / "postgres-vacuum-tuning" / "SKILL.md").unlink()
        res = _create("pg-autovacuum", NEAR)
        assert res["success"] is False, res
        assert res["near_duplicates"][0]["name"] == "pg-proj"


def test_compare_and_write_run_under_one_cross_process_lock(tmp_path):
    """adbf7b9e2588: while another process holds <root>/state/skill-neardup-create.lock, a create waits."""
    import fcntl
    import threading
    import time
    with _fleet_home(tmp_path) as (home, shared):
        lock = home / "state" / "skill-neardup-create.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        done = []
        with open(lock, "a+") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            t = threading.Thread(target=lambda: done.append(_create("garage-door-lockout", DISTINCT)))
            t.start()
            time.sleep(0.8)
            assert not done and not (shared / "general" / "garage-door-lockout").exists()
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        t.join(20)
        assert done and done[0]["success"] is True, done


def test_batch_rollback_removes_a_create_in_an_unsubscribed_group(tmp_path):
    """f26dc060e98b: devops-aux is not a loaded root, so _find_skill cannot see the create; rollback must."""
    with _fleet_home(tmp_path) as (home, shared):
        res = json.loads(skill_manage("", "", operations=[
            {"action": "create", "name": "garage-door-lockout", "category": "devops-aux", "content": DISTINCT},
            {"action": "patch", "name": "postgres-vacuum-tuning", "old_string": "NO SUCH TEXT", "new_string": "x"}]))
        assert res["success"] is False, res
        assert not (shared / "devops-aux" / "garage-door-lockout").exists(), "batch create survived rollback"


@pytest.mark.parametrize("broken", ["refusal_message", "distinct_from_entries"])
def test_scorer_message_helpers_fail_open(tmp_path, broken):
    """aada50d66930: refusal_message / distinct_from_entries raising is scorer failure: fail open."""
    with _fleet_home(tmp_path) as (home, shared):
        lib = home / "scripts" / "lib" / "skill_similarity.py"
        lib.write_text(STUB_SCORER + f"\ndef {broken}(*a, **k):\n    raise KeyError('boom')\n")
        from tools import skill_manager_neardup as nd
        nd._scorer_cache.clear()
        res = _create("pg-autovacuum", NEAR, **({"justification": "why"} if broken == "distinct_from_entries" else {}))
        assert res["success"] is True, res


def test_pending_diff_renders_the_justified_content(tmp_path):
    """d879f184cd83: with skills.write_approval on, the staged create preview is what approve will write:
    the justification's distinct_from rows included, in a batch too (later ops diff against it)."""
    from tools.write_approval import skill_pending_diff
    with _fleet_home(tmp_path) as (home, shared):
        op = {"action": "create", "name": "pg-autovacuum", "content": NEAR, "category": "general",
              "justification": "ops runbook"}
        single = skill_pending_diff({"payload": op})
        assert "distinct_from" in single and "postgres-vacuum-tuning" in single, single
        batch = skill_pending_diff({"payload": {"action": "batch", "operations": [
            op, {"action": "patch", "name": "pg-autovacuum", "old_string": "watch freeze age.",
                 "new_string": "watch freeze age closely."}]}})
        assert "distinct_from" in batch.split("## op 2/2")[0], batch
        assert "(patch would fail" not in batch, batch
        plain = skill_pending_diff({"payload": {**op, "justification": None}})
        assert plain == NEAR
        assert _ledger(home) == [], "a preview must not write ledger rows"


# --- Prism round 1 on agent#2541 (t_7c67781d) ------------------------------------------------------

@pytest.mark.parametrize("lead", [[], [{"action": "patch", "name": "postgres-vacuum-tuning",
                                        "old_string": "NO SUCH TEXT", "new_string": "x"}]])
def test_failed_batch_create_never_touches_a_foreign_skill_in_an_unsubscribed_group(tmp_path, lead):
    """ccb4d869f7f9: devops-aux/garage-door-lockout predates the batch and find_skill cannot see it. A create
    that fails ('already exists') or never runs must not let rollback unlink that skill's SKILL.md."""
    with _fleet_home(tmp_path) as (home, shared):
        foreign = shared / "devops-aux" / "garage-door-lockout"
        foreign.mkdir()
        (foreign / "SKILL.md").write_text(DISTINCT)
        res = json.loads(skill_manage("", "", operations=lead + [
            {"action": "create", "name": "garage-door-lockout", "category": "devops-aux", "content": DISTINCT}]))
        assert res["success"] is False, res
        assert (foreign / "SKILL.md").read_text() == DISTINCT, "rollback deleted a skill the batch never wrote"


def test_batch_create_preview_scores_against_the_earlier_ops_staged_tree(tmp_path):
    """a4cf78828e81 / e485146dd10e: approve scores op 2 after op 1 has rewritten the only near match, so the
    preview must too: no distinct_from for a skill that will no longer be near. And a skill an earlier op
    CREATES is a near match the preview must see."""
    from tools.write_approval import skill_pending_diff
    rewritten = DISTINCT.replace("garage-door-lockout", "postgres-vacuum-tuning")
    create = {"action": "create", "name": "pg-autovacuum", "content": NEAR, "category": "general",
              "justification": "ops runbook"}
    with _fleet_home(tmp_path) as (home, shared):
        batch = skill_pending_diff({"payload": {"action": "batch", "operations": [
            {"action": "edit", "name": "postgres-vacuum-tuning", "content": rewritten}, create]}})
        op2 = batch.split("## op 2/2")[1]
        assert "distinct_from" not in op2, batch
        (shared / "devops" / "postgres-vacuum-tuning" / "SKILL.md").unlink()
        sibling = NEAR.replace("pg-autovacuum", "pg-vacuum-ops")
        batch = skill_pending_diff({"payload": {"action": "batch", "operations": [
            {"action": "create", "name": "pg-vacuum-ops", "content": sibling, "category": "general"}, create]}})
        op2 = batch.split("## op 2/2")[1]
        assert "distinct_from" in op2 and "pg-vacuum-ops" in op2, batch
        assert _ledger(home) == []


def test_batch_create_preview_does_not_score_a_skill_an_earlier_op_deletes(tmp_path):
    """8377c7154a79 / aaa4ebf59b72: approve deletes postgres-vacuum-tuning before it scores the create, so the
    preview must not annotate distinct_from against it. Control: the same create after deleting an unrelated name
    IS annotated (the exclusion did not just make the gate fail open)."""
    from tools.write_approval import skill_pending_diff
    create = {"action": "create", "name": "pg-autovacuum", "content": NEAR, "category": "general",
              "justification": "ops runbook"}
    with _fleet_home(tmp_path) as (home, shared):
        batch = skill_pending_diff({"payload": {"action": "batch", "operations": [
            {"action": "delete", "name": "postgres-vacuum-tuning"}, create]}})
        assert "distinct_from" not in batch.split("## op 2/2")[1], batch
        batch = skill_pending_diff({"payload": {"action": "batch", "operations": [
            {"action": "delete", "name": "garage-door-lockout"}, create]}})
        op2 = batch.split("## op 2/2")[1]
        assert "distinct_from" in op2 and "postgres-vacuum-tuning" in op2, batch
        assert _ledger(home) == []


def test_create_preview_never_writes_outside_its_overlay(tmp_path):
    """92bc336dabf3: a staged name that is not a valid skill name (absolute, `..`) is never a path."""
    from tools.write_approval import _create_preview
    escape = tmp_path / "preview-escape"
    with _fleet_home(tmp_path):
        out = _create_preview({"name": "next", "content": NEAR, "justification": "reason"},
                              {str(escape): {"SKILL.md": "unapproved"}, "../up": {"SKILL.md": "unapproved"}})
    assert not escape.exists(), "preview wrote outside its temp overlay"
    assert not (tmp_path / "up").exists()
    assert out.startswith("---")


_OLD_SCORER = STUB_SCORER.replace(
    "def check(name, content, roots=None, threshold=None, corpus=None):",
    "def check(name, content, roots=None, threshold=None):").replace(
    "(corpus or Corpus.load(roots))", "Corpus.load(roots)")


def test_scorer_without_corpus_kwarg_still_gates(tmp_path):
    """370d9676504e: the root's scorer may predate check(corpus=). Passing corpus= to it raised TypeError, which the
    fail-open boundary turned into NO gate: every near-duplicate create went through."""
    assert "corpus" not in _OLD_SCORER.split("def check(")[1].split(")")[0]
    with _fleet_home(tmp_path) as (home, shared):
        (home / "scripts" / "lib" / "skill_similarity.py").write_text(_OLD_SCORER)
        res = _create("pg-autovacuum", NEAR)
        assert res["success"] is False, res
        assert "postgres-vacuum-tuning" in res["error"]
        assert not (shared / "general" / "pg-autovacuum").exists()


def test_scorer_without_corpus_kwarg_still_gates_a_batch_preview_with_a_delete(tmp_path):
    """370d9676504e, exclude_names path: an older scorer cannot take the filtered corpus, so the preview scores the
    on-disk tree (stricter) instead of failing open; the justified create is still annotated."""
    from tools.write_approval import skill_pending_diff
    create = {"action": "create", "name": "pg-autovacuum", "content": NEAR, "category": "general",
              "justification": "ops runbook"}
    with _fleet_home(tmp_path) as (home, shared):
        (home / "scripts" / "lib" / "skill_similarity.py").write_text(_OLD_SCORER)
        batch = skill_pending_diff({"payload": {"action": "batch", "operations": [
            {"action": "delete", "name": "garage-door-lockout"}, create]}})
        op2 = batch.split("## op 2/2")[1]
        assert "distinct_from" in op2 and "postgres-vacuum-tuning" in op2, batch
