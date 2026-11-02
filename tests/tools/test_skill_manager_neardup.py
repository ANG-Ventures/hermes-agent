"""Near-duplicate gate on skill_manage create (tools/skill_manager_neardup.py, fork t_f78aa714).

The gate's contract with the Hermes root's scorer (``<root>/scripts/lib/skill_similarity.py``):
``THRESHOLD``, ``check(name, content, roots=, threshold=)``, ``refusal_message``,
``distinct_from_entries``, ``ledger_path``, ``append_ledger``. The scorer lives in the root repo, so
these tests plant a word-overlap stub with that interface in a temp fleet home and drive the real
create path end to end (operations shape, flat shape, config, frontmatter, ledger).
"""

import json
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

def check(name, content, roots=None, threshold=None):
    thr = THRESHOLD if threshold is None else threshold
    ROOTS_SEEN.append([str(r) for r in roots or []])
    cand, seen, matches = _words(content), set(), []
    for root in roots or []:
        for p in sorted(Path(root).rglob("SKILL.md")):
            n = p.parent.name
            if n == name or n in seen:
                continue
            seen.add(n)
            w = _words(p.read_text())
            matches.append({"name": n, "path": str(p), "score": round(len(cand & w) / len(cand | w), 3)})
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
