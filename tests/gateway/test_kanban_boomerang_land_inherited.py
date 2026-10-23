"""Boomerang wake ``land_inherited`` (t_47d1968c): the executor re-verifies an inherited-red proof, then enqueues.

The #6313 fixture is the real bytes of the 2026-10-23 01:04 wake (hermes-home#6313, ci-shard-4
``mutate_anchor_lint``): the PR's file list, the anchored source at the PR head and at main b8b00e0a94, the harness
at main and the family's suites.json row. GitHub and the landing queue are a fake ``io``; the board, the claim, the
executor and the receipt are real.
"""
from __future__ import annotations

import asyncio
import copy
import json
import time
from pathlib import Path

import pytest

from gateway import kanban_boomerang_land_inherited as li
from gateway import kanban_boomerang_wake as ah
from tests.gateway.test_kanban_boomerang_wake_dispatch import (  # noqa: F401 -- the env fixture
    _card, _claimed_entry, _handback, _verdict, _writes, env,
)
from hermes_cli import kanban_db as kb

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "boomerang_land_inherited_6313.json").read_text())
REPO, PR, HEAD, MAIN = FIXTURE["repo"], FIXTURE["pr"], FIXTURE["head"], FIXTURE["main_sha"]
SRC = "scripts/kanban-review-merge-pass.py"
HARNESS = "scripts/tests/mutate_auto_enqueue_pass.sh"


class FakeIo:
    def __init__(self, fx: dict) -> None:
        self.files = dict(fx["files"])
        self.pr_files_list = list(fx["pr_files"])
        self.p0p1 = (0, 0, "p0=0 p1=0")
        self.row = None
        self.streak_doc = None
        self.enqueued: list = []
        self.rc = 0
        self.head = fx["head"]

    def pr(self, repo, number):
        return {"state": "open", "merged": False, "head": {"sha": self.head}, "base": {"ref": "main"}}

    def pr_files(self, repo, number):
        return list(self.pr_files_list)

    def branch_sha(self, repo, branch):
        return MAIN

    def raw(self, repo, path, ref):
        tag = "head" if ref == self.head else ("main" if ref == MAIN else ref)
        return self.files.get(f"{path}@{tag}")

    def prism_open(self, repo, number, head, card):
        return self.p0p1

    def streak(self):
        return self.streak_doc

    def queue_row(self, repo, number):
        return self.row

    def enqueue(self, argv, actor=""):
        self.enqueued.append(list(argv))
        return self.rc, "landing-queue: QUEUED"


@pytest.fixture
def fio(env, monkeypatch):
    fake = FakeIo(FIXTURE)
    monkeypatch.setattr(li, "io", fake)
    env["logs"] = []
    monkeypatch.setattr(ah, "post_logs", lambda text: env["logs"].append(text))
    return fake


def _card_6313(env, coverage=True) -> tuple:
    """A real boomerang-wake card handed back on #6313 at the fixture head, claimed on own_repo_review_red."""
    env["health"][(REPO, PR)] = {"state": "open", "mergeable_state": "clean", "failing": ["ci-shard-4"], "head": HEAD}
    tid = _card(env)
    with kb.connect_closing() as conn, \
            kb.mutation_actor(session_ids=["20261019_000001_worker"], profile="worker", surface="worker"):
        kb.request_review(conn, tid, force=True, summary=f"PR {REPO}#{PR} ready")
    if coverage:
        with kb.connect_closing() as conn:
            kb.add_comment(conn, tid, "apollo", FIXTURE["coverage_comment"])
    entry = _claimed_entry(env, tid, row="own_repo_review_red", kind="review_requested")
    assert entry["pr"] == f"{REPO}#{PR}" and entry["head"] == HEAD, entry
    return tid, entry


def _final(entry, verdict: dict) -> str:
    body = json.dumps(verdict)
    return f"Outcome: inherited\n<<<BOOMERANG-VERDICT nonce={entry['verdict_nonce']}>>>\n{body}\n<<<END-VERDICT>>>"


def _receipts(tid) -> list:
    with kb.connect_closing() as conn:
        return [e for e in kb.list_events(conn, tid) if e.kind == ah.RECEIPT_KIND]


# --- the #6313 replay ---------------------------------------------------------------------------


def test_6313_replay_enqueues_with_inherited_waiver(env, fio):
    """Red on base: ``land_inherited`` was not an action (VerdictError -> surface). Fix: ONE enqueue, signed."""
    tid, entry = _card_6313(env)
    res = ah.execute_verdict(entry, _final(entry, FIXTURE["child_verdict"]))
    assert res["ok"], res
    assert res["action"] == "land_inherited"
    assert len(fio.enqueued) == 1
    argv = fio.enqueued[0]
    assert argv[:2] == ["enqueue", "--repo"] and argv[argv.index("--head") + 1] == HEAD
    assert argv[argv.index("--card") + 1] == tid
    waiver = argv[argv.index("--allow-red-ci") + 1]
    assert waiver.startswith(f"inherited: mutate_anchor_lint red on main at {MAIN[:10]} (boomerang-wake land_inherited")
    assert not set(argv) & set(li.FORBIDDEN_FM)
    rec = _receipts(tid)
    assert len(rec) == 1 and rec[0].payload["action"] == "land_inherited" and rec[0].payload["operator_kind"] == "machine"
    with kb.connect_closing() as conn:
        ours = [c.body for c in kb.list_comments(conn, tid) if c.author == ah.AUTHOR]
    assert len(ours) == 1 and ours[0].startswith("[boomerang-wake] land_inherited") and "(b) anchor present" in ours[0]
    from hermes_constants import get_hermes_home

    rows = [json.loads(ln) for ln in (get_hermes_home() / "state" / li.LEDGER_FILE).read_text().splitlines()]
    assert [(r["card"], r["pr"], r["head"], r["family"], r["main_sha"]) for r in rows] == \
        [(tid, f"{REPO}#{PR}", HEAD, "mutate_anchor_lint", MAIN)]
    assert len(env["logs"]) == 1 and "land_inherited" in env["logs"][0]
    with kb.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "review", "land_inherited never closes the card"


def test_6313_replay_through_run_entry_posts_one_line_no_turn(env, fio, monkeypatch):
    from tests.gateway.test_kanban_boomerang_wake_dispatch import _run

    tid, entry = _card_6313(env)
    _run(env, entry, "completed", _final(entry, FIXTURE["child_verdict"]))
    assert len(fio.enqueued) == 1
    assert [s for s in env["adapter"].sent if s.startswith(f"[boomerang-wake] {tid}")]
    assert env["adapter"].handled == []


# --- degrade paths: every one writes nothing and surfaces --------------------------------------


def _degrades(env, fio, verdict=None, coverage=True, **mut) -> dict:
    tid, entry = _card_6313(env, coverage=coverage)
    for k, v in mut.items():
        setattr(fio, k, v)
    before = _writes(tid)
    res = ah.execute_verdict(entry, _final(entry, verdict or FIXTURE["child_verdict"]))
    assert not res["ok"] and res.get("surface") and res["action"] == "surface", res
    assert fio.enqueued == [] and _writes(tid) == before and _receipts(tid) == []
    return res


def test_mutant_child_claims_inherited_for_a_file_the_pr_touches(env, fio):
    """(a) the PR changes the anchored source: whatever the child says, it surfaces."""
    res = _degrades(env, fio, pr_files_list=FIXTURE["pr_files"] + [SRC])
    assert f"(a) the PR changes {SRC}" in res["reason"]


def test_a_fails_when_the_pr_touches_the_harness(env, fio):
    res = _degrades(env, fio, pr_files_list=FIXTURE["pr_files"] + [HARNESS])
    assert "(a) the PR changes" in res["reason"]


def test_b_fails_when_the_anchor_is_still_on_main(env, fio):
    files = dict(fio.files)
    files[f"{SRC}@main"] = files[f"{SRC}@head"]
    res = _degrades(env, fio, files=files)
    assert "(b) anchor present 1x" in res["reason"]


def test_b_fails_when_the_anchor_is_not_real_at_the_pr_head(env, fio):
    v = copy.deepcopy(FIXTURE["child_verdict"])
    v["inherited"]["anchor_or_test"]["anchor"] = "fixed_lines=never_existed(),"
    res = _degrades(env, fio, verdict=v)
    assert "not a real anchor" in res["reason"]


def test_b_fails_when_main_harness_no_longer_names_the_anchor(env, fio):
    files = dict(fio.files)
    files[f"{HARNESS}@main"] = "mut \"other\" 'x' 'y'\n"
    res = _degrades(env, fio, files=files)
    assert "does not carry the anchor" in res["reason"]


def test_family_unknown_on_main_surfaces(env, fio):
    v = copy.deepcopy(FIXTURE["child_verdict"])
    v["inherited"]["family"] = "no_such_suite"
    res = _degrades(env, fio, verdict=v)
    assert "neither" in res["reason"]


def test_no_structured_proof_surfaces(env, fio):
    v = {k: x for k, x in FIXTURE["child_verdict"].items() if k != "inherited"}
    res = _degrades(env, fio, verdict=v)
    assert "no structured inherited proof" in res["reason"]


def test_no_operator_coverage_at_head_surfaces(env, fio):
    res = _degrades(env, fio, coverage=False)
    assert "review_coverage" in res["reason"]


@pytest.mark.parametrize("counts", [(1, 0), (0, 1), (None, None)])
def test_open_prism_p0_p1_or_unread_surfaces(env, fio, counts):
    res = _degrades(env, fio, p0p1=(*counts, "line"))
    assert "Prism" in res["reason"]


@pytest.mark.parametrize("flag", li.FORBIDDEN_FM)
def test_row_with_a_forbidden_fleet_merge_flag_surfaces(env, fio, flag):
    res = _degrades(env, fio, row={"fm_args": [flag, "why"], "by": "apollo/operator"})
    assert flag in res["reason"]


def test_kill_switch_file_surfaces(env, fio):
    li.kill_path().parent.mkdir(parents=True, exist_ok=True)
    li.kill_path().write_text("")
    try:
        res = _degrades(env, fio)
    finally:
        li.kill_path().unlink()
    assert "disabled" in res["reason"]


def test_head_moved_on_github_surfaces(env, fio):
    res = _degrades(env, fio, head="b" * 40)
    assert "head moved" in res["reason"]


def test_land_inherited_not_allowed_off_the_red_row(env, fio):
    tid = _card(env)
    _handback(tid, 3199)
    entry = _claimed_entry(env, tid, row="changes_requested", kind="review_requested")
    res = ah.execute_verdict(entry, _final(entry, FIXTURE["child_verdict"]))
    assert not res["ok"] and "not allowed" in res["reason"] and fio.enqueued == []


# --- grade mode (b): family red on main's newest grade -------------------------------------------


def _grade_verdict() -> dict:
    v = copy.deepcopy(FIXTURE["child_verdict"])
    v["inherited"]["anchor_or_test"] = "test_live_tree_is_clean"
    return v


def _streak(families) -> dict:
    return {"repo": REPO, "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "red_families": list(families), "red_run_sha": MAIN, "red_detail": {}}


UNOWNED = ["scripts/lib/landing_queue.py", "scripts/tests/test_landing_queue.py"]


def test_6313_whole_family_check_would_refuse_it(env, fio):
    """#6313 changes scripts/lib/approve_carry.py, which mutate_anchor_lint's paths own. A whole-family (a) would
    surface the measured case. That is why anchor mode narrows (a) to the two files that decide the failure."""
    res = _degrades(env, fio, verdict=_grade_verdict(), streak_doc=_streak(["mutate_anchor_lint"]))
    assert "(a) the PR changes 1 file(s) mutate_anchor_lint owns: scripts/lib/approve_carry.py" in res["reason"]


def test_grade_mode_family_red_on_main_enqueues(env, fio):
    tid, entry = _card_6313(env)
    fio.pr_files_list = UNOWNED
    fio.streak_doc = _streak(["mutate_anchor_lint", "git-env-scrub"])
    fio.row = {"fm_args": ["--allow-red-ci", "old reason", "--allow-stale-base", "x"], "by": "apollo/operator"}
    res = ah.execute_verdict(entry, _final(entry, _grade_verdict()))
    assert res["ok"], res
    extra = fio.enqueued[0][fio.enqueued[0].index("--") + 1:]
    assert extra[:2] == ["--allow-stale-base", "x"] and extra.count("--allow-red-ci") == 1


def test_grade_mode_family_green_on_main_surfaces(env, fio):
    res = _degrades(env, fio, verdict=_grade_verdict(), streak_doc=_streak(["git-env-scrub"]), pr_files_list=UNOWNED)
    assert "(b) mutate_anchor_lint is not red" in res["reason"]


def test_grade_mode_pr_touches_owned_path_surfaces(env, fio):
    res = _degrades(env, fio, verdict=_grade_verdict(), streak_doc=_streak(["mutate_anchor_lint"]),
                    pr_files_list=["scripts/lib/approve_carry.py"])   # owned by mutate_anchor_lint
    assert "(a) the PR changes 1 file(s) mutate_anchor_lint owns" in res["reason"]


# --- pure helpers ----------------------------------------------------------------------------------


def test_owned_glob_semantics():
    assert li.owned("scripts/tests/mutate_x.sh", ["scripts/tests/mutate_*.sh"])
    assert not li.owned("scripts/tests/sub/mutate_x.sh", ["scripts/tests/mutate_*.sh"])
    assert li.owned("scripts/a/b.py", ["scripts/**"]) and not li.owned("scripts/a/b.py", ["scripts/**", "!scripts/a/*"])
    assert li.owned("x/y.json", ["@g"], {"g": ["x/*.json"]})


def test_enqueue_refuses_a_forbidden_flag_even_if_a_plan_carries_one():
    plan = {"repo": REPO, "pr": PR, "head": HEAD, "family": "f", "main_sha": MAIN, "by": "apollo/operator",
            "fm_args": ["--break-glass"]}
    with pytest.raises(RuntimeError):
        li.enqueue_argv(plan, "t_00000000", "s")
