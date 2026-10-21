"""Shared-tree git safety contract + e2e (agent/curator_shared.py).

Covers spec Phase 3 + Invariant 3/AC4/AC5 against a REAL git repo in a temp
HERMES_HOME (real imports, no mocks for the resolution chain):

- git_precheck_shared clean/dirty
- snapshot_shared writes a separate shared tar + manifest with the
  intended-write set recorded BEFORE mutation
- commit_shared: explicit pathspec only; a concurrent NON-lock-taking
  sibling write is never absorbed (drift-abort or pathspec confinement)
- the shared pass end-to-end: split fires with consolidate:false, commit is
  tagged `curator:` and confined to skills-shared/, dirty tree skips with
  zero edits, dry-run makes zero writes and zero commits
- concurrent-write e2e (AC4): a second process writes into skills-shared/
  DURING the pass; the sibling file is absent from the curator commit
- curator-vs-curator: second lock acquisition fails -> skip
- crash recovery: exact-set match restores self-inflicted dirt; a sibling
  (un-manifested) dirty path -> skip-and-report, never clobbered
- unsubscribe_shared_skill: idle -> <group>-aux/<name>/ via skill-group.py, never .archive (t_776ae7bc)
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
    )


def _mk_big_skill(root: Path, group: str, name: str, kb: int = 140) -> Path:
    d = root / group / name
    d.mkdir(parents=True, exist_ok=True)
    filler = ("filler content line for the oversized skill body " * 20).strip()
    parts = [f"---\nname: {name}\ndescription: big\n---\n\nIntro.\n\n"]
    i = 0
    while sum(len(p) for p in parts) < kb * 1024:
        parts.append(f"## Section {i:03d}\n\n" + (filler + "\n") * 12 + "\n")
        i += 1
    (d / "SKILL.md").write_text("".join(parts), encoding="utf-8")
    return d


@pytest.fixture
def git_env(tmp_path, monkeypatch):
    """Temp HERMES_HOME that IS a real git repo with a skills-shared/ tree."""
    home = tmp_path / ".hermes"
    (home / "skills").mkdir(parents=True)
    shared = home / "skills-shared"
    (shared / "smart-home").mkdir(parents=True)
    (shared / "devops").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    _git(home, "init", "-q")
    _git(home, "config", "user.email", "curator-test@example.com")
    _git(home, "config", "user.name", "Curator Test")

    big = _mk_big_skill(shared, "smart-home", "clanker-e2e")
    small = shared / "devops" / "cron-alert-discipline"
    small.mkdir(parents=True)
    (small / "SKILL.md").write_text(
        "---\nname: cron-alert-discipline\ndescription: small\n---\nsmall\n",
        encoding="utf-8",
    )

    (home / "config.yaml").write_text(textwrap.dedent(f"""\
        skills:
          external_dirs:
            - {shared / 'smart-home'}
            - {shared / 'devops'}
        curator:
          include_shared_dirs: true
          split_over_kb: 100
          consolidate: false
        """), encoding="utf-8")

    _git(home, "add", "-A")
    _git(home, "commit", "-q", "-m", "baseline")

    import agent.skill_utils as su
    importlib.reload(su)
    import agent.curator_shared as cs
    importlib.reload(cs)

    yield {"home": home, "shared": shared, "big": big, "small": small, "cs": cs}


class TestPrecheckAndSnapshot:
    def test_precheck_clean(self, git_env):
        cs = git_env["cs"]
        ok, reason, dirty = cs.git_precheck_shared(git_env["shared"])
        assert ok and reason == "clean" and dirty == []

    def test_precheck_dirty(self, git_env):
        cs = git_env["cs"]
        (git_env["big"] / "SKILL.md").write_text("dirty!", encoding="utf-8")
        ok, reason, dirty = cs.git_precheck_shared(git_env["shared"])
        assert not ok and reason == "dirty working tree"
        assert any("clanker-e2e" in p for p in dirty)

    def test_precheck_scoped_out_of_scope_dirt_passes(self, git_env):
        """Dirt on a sibling skill must NOT block a pass scoped elsewhere."""
        cs = git_env["cs"]
        (git_env["big"] / "SKILL.md").write_text("sibling dirt", encoding="utf-8")
        ok, reason, ignored = cs.git_precheck_shared(
            git_env["shared"], scope_paths=[git_env["small"]]
        )
        assert ok
        assert reason == "clean (out-of-scope dirt ignored)"
        # the ignored dirt is REPORTED so callers can thread it into
        # commit_shared(precheck_dirty=...)
        assert any("clanker-e2e" in p for p in ignored)

    def test_precheck_scoped_in_scope_dirt_still_blocks(self, git_env):
        cs = git_env["cs"]
        (git_env["big"] / "SKILL.md").write_text("in-scope dirt", encoding="utf-8")
        ok, reason, dirty = cs.git_precheck_shared(
            git_env["shared"], scope_paths=[git_env["big"]]
        )
        assert not ok and reason == "dirty working tree"
        assert any("clanker-e2e" in p for p in dirty)

    def test_precheck_scope_outside_repo_falls_back_conservative(self, git_env, tmp_path):
        """A scope path outside the repo -> whole-tree gate (fail on any dirt)."""
        cs = git_env["cs"]
        (git_env["big"] / "SKILL.md").write_text("dirt", encoding="utf-8")
        outside = tmp_path / "not-in-repo"
        outside.mkdir(exist_ok=True)
        ok, reason, dirty = cs.git_precheck_shared(
            git_env["shared"], scope_paths=[outside]
        )
        assert not ok and reason == "dirty working tree"

    def test_precheck_scoped_clean_tree_is_plain_clean(self, git_env):
        cs = git_env["cs"]
        ok, reason, dirty = cs.git_precheck_shared(
            git_env["shared"], scope_paths=[git_env["small"]]
        )
        assert ok and reason == "clean" and dirty == []

    def test_snapshot_writes_separate_tar_and_manifest(self, git_env):
        cs = git_env["cs"]
        snap = cs.snapshot_shared(
            [git_env["big"]],
            ["skills-shared/smart-home/clanker-e2e/SKILL.md"],
            shared_root=git_env["shared"],
        )
        assert snap is not None
        tars = list(snap.glob("shared-*.tar.gz"))
        assert len(tars) == 1
        manifest = json.loads(
            (snap / cs.SHARED_MANIFEST_NAME).read_text(encoding="utf-8")
        )
        assert manifest["intended_writes"] == [
            "skills-shared/smart-home/clanker-e2e/SKILL.md"
        ]
        assert manifest["baseline_rev"]
        # the pre-mutation SKILL.md is inside the tar
        import tarfile
        with tarfile.open(tars[0]) as tf:
            names = tf.getnames()
        assert any(n.endswith("SKILL.md") for n in names)


class TestCommitShared:
    def test_explicit_pathspec_commit(self, git_env):
        cs = git_env["cs"]
        home = git_env["home"]
        target = git_env["small"] / "SKILL.md"
        target.write_text(target.read_text() + "\nedit\n", encoding="utf-8")
        ok, msg = cs.commit_shared(
            "test edit", [target],
            ["skills-shared/devops/cron-alert-discipline/SKILL.md"],
            shared_root=git_env["shared"],
        )
        assert ok, msg
        log = _git(home, "log", "-1", "--pretty=%s").stdout.strip()
        assert log.startswith("curator: ")
        show = _git(home, "show", "--stat", "--pretty=", "HEAD").stdout
        assert "cron-alert-discipline" in show
        assert "clanker-e2e" not in show

    def test_drift_abort_on_unexpected_sibling_path(self, git_env):
        """A NON-lock-taking sibling write during the pass -> abort, nothing
        staged, sibling file untouched."""
        cs = git_env["cs"]
        home = git_env["home"]
        target = git_env["small"] / "SKILL.md"
        target.write_text(target.read_text() + "\ncurator edit\n",
                          encoding="utf-8")
        # sibling writes DURING the pass (after precheck, before commit)
        sibling = git_env["shared"] / "smart-home" / "sibling-new"
        sibling.mkdir()
        (sibling / "SKILL.md").write_text("sibling work — hands off",
                                          encoding="utf-8")
        ok, msg = cs.commit_shared(
            "test edit", [target], [], shared_root=git_env["shared"],
        )
        assert not ok
        assert "drifted" in msg
        # nothing was committed; sibling intact
        log = _git(home, "log", "-1", "--pretty=%s").stdout.strip()
        assert log == "baseline"
        assert (sibling / "SKILL.md").read_text(encoding="utf-8") == \
            "sibling work — hands off"

    def test_refuses_paths_outside_repo(self, git_env, tmp_path):
        cs = git_env["cs"]
        outside = tmp_path / "outside.txt"
        outside.write_text("x", encoding="utf-8")
        ok, msg = cs.commit_shared(
            "bad", [outside], [], shared_root=git_env["shared"],
        )
        assert not ok and "outside the repo" in msg


class TestSharedPassE2E:
    def _run_pass(self, monkeypatch, dry_run=False, threshold=100,
                  cfg_extra=None):
        import agent.curator as curator
        importlib.reload(curator)
        cfg = {"include_shared_dirs": True, "split_over_kb": threshold}
        cfg.update(cfg_extra or {})
        monkeypatch.setattr(curator, "_load_config", lambda: cfg)
        return curator._run_shared_pass(dry_run, threshold)

    def test_split_fires_with_consolidate_false_and_commits(
        self, git_env, monkeypatch,
    ):
        """AC3+AC4 core: the oversized shared skill splits (consolidate is
        false in config), the commit is tagged curator:, confined to
        skills-shared/, and SKILL.md drops under the cap."""
        report = self._run_pass(monkeypatch)
        assert report["skipped"] is None, report
        assert "clanker-e2e" in report["split"]
        assert report["commit"], report
        home = git_env["home"]
        log = _git(home, "log", "-1", "--pretty=%s").stdout.strip()
        assert log.startswith("curator: split")
        # diff confined to skills-shared/
        files = _git(home, "show", "--name-only", "--pretty=",
                     "HEAD").stdout.strip().splitlines()
        assert files and all(f.startswith("skills-shared/") for f in files)
        post = (git_env["big"] / "SKILL.md").stat().st_size
        assert post < 100 * 1024
        # revertable: git revert restores the original
        r = _git(home, "revert", "--no-edit", "HEAD")
        assert r.returncode == 0, r.stderr
        assert (git_env["big"] / "SKILL.md").stat().st_size > 100 * 1024

    def test_out_of_scope_sibling_dirt_no_longer_skips(self, git_env, monkeypatch):
        """Scoped precheck (2026-07-25): dirt OUTSIDE the candidate skill dirs
        must not block the pass — and must survive uncommitted/untouched."""
        marker = git_env["shared"] / "devops" / "sibling-file.md"
        marker.write_text("sibling", encoding="utf-8")
        report = self._run_pass(monkeypatch)
        # pass ran (not skipped on precheck)
        assert not (report.get("skipped") or "").startswith("precheck failed")
        assert any("clanker-e2e" in s for s in report["split"])
        # sibling dirt untouched and not absorbed into the curator commit
        assert marker.read_text(encoding="utf-8") == "sibling"
        files = _git(git_env["home"], "show", "--name-only", "--pretty=",
                     "HEAD").stdout
        assert "sibling-file" not in files

    def test_in_scope_dirty_candidate_skips_with_zero_edits(self, git_env, monkeypatch):
        """Dirt INSIDE a candidate skill dir still hard-blocks the pass."""
        marker = git_env["big"] / "wip-notes.md"
        marker.write_text("in-scope dirt", encoding="utf-8")
        before = (git_env["big"] / "SKILL.md").read_bytes()
        report = self._run_pass(monkeypatch)
        assert report["skipped"] is not None
        assert "precheck failed" in report["skipped"]
        assert (git_env["big"] / "SKILL.md").read_bytes() == before
        assert marker.read_text(encoding="utf-8") == "in-scope dirt"
        log = _git(git_env["home"], "log", "-1", "--pretty=%s").stdout.strip()
        assert log == "baseline"

    def test_dry_run_zero_writes_zero_commits(self, git_env, monkeypatch):
        before = (git_env["big"] / "SKILL.md").read_bytes()
        report = self._run_pass(monkeypatch, dry_run=True)
        assert report["skipped"] == "dry-run (no mutations)"
        assert any("clanker-e2e" in s for s in report["split"])
        assert (git_env["big"] / "SKILL.md").read_bytes() == before
        st = _git(git_env["home"], "status", "--porcelain").stdout.strip()
        assert st == ""
        log = _git(git_env["home"], "log", "-1", "--pretty=%s").stdout.strip()
        assert log == "baseline"

    def test_concurrent_sibling_write_never_absorbed(self, git_env, monkeypatch):
        """AC4 concurrent-write e2e: a second PROCESS (not taking the lock)
        writes into skills-shared/ DURING the pass. The curator commit must
        not contain the sibling file, and the sibling file must survive
        un-reverted. We interpose on commit_shared to spawn the writer at
        the worst possible moment (post-split, pre-commit)."""
        import agent.curator_shared as cs
        sibling_path = git_env["shared"] / "devops" / "concurrent-sibling.md"
        real_commit = cs.commit_shared

        def commit_with_concurrent_writer(summary, written, *a, **kw):
            # a real second process writes DURING the pass
            code = (
                "import pathlib,sys; "
                f"pathlib.Path({str(sibling_path)!r}).write_text('sibling mid-pass')"
            )
            subprocess.run([sys.executable, "-c", code], check=True)
            return real_commit(summary, written, *a, **kw)

        monkeypatch.setattr(cs, "commit_shared", commit_with_concurrent_writer)
        report = self._run_pass(monkeypatch)

        # Either the drift-abort fired (commit None) or the pathspec commit
        # excluded the sibling — both satisfy AC4. In BOTH cases the sibling
        # file survives untouched and is absent from any curator commit.
        assert sibling_path.exists()
        assert sibling_path.read_text(encoding="utf-8") == "sibling mid-pass"
        home = git_env["home"]
        if report.get("commit"):
            files = _git(home, "show", "--name-only", "--pretty=",
                         "HEAD").stdout
            assert "concurrent-sibling" not in files
        else:
            assert any("drifted" in e for e in report["errors"]) or \
                report["skipped"]

    def test_second_curator_lock_contention_skips(self, git_env, monkeypatch):
        """Curator-vs-curator: while one process holds the fcntl lock, the
        pass skips with a lock-contention report."""
        import agent.curator_shared as cs
        lock_file = git_env["shared"] / cs.LOCK_NAME
        # hold the lock from a second real process
        holder = subprocess.Popen(
            [sys.executable, "-c", (
                "import fcntl,sys,time; "
                f"f=open({str(lock_file)!r},'a+'); "
                "fcntl.flock(f, fcntl.LOCK_EX); "
                "print('locked',flush=True); time.sleep(20)"
            )],
            stdout=subprocess.PIPE, text=True,
        )
        try:
            assert holder.stdout.readline().strip() == "locked"
            report = self._run_pass(monkeypatch)
            assert report["skipped"] is not None
            assert "lock contention" in report["skipped"]
        finally:
            holder.kill()
            holder.wait()

    def test_snapshot_failure_hard_gates(self, git_env, monkeypatch):
        import agent.curator_shared as cs
        monkeypatch.setattr(cs, "snapshot_shared", lambda *a, **k: None)
        before = (git_env["big"] / "SKILL.md").read_bytes()
        report = self._run_pass(monkeypatch)
        assert report["skipped"] == "shared snapshot failed (hard gate)"
        assert (git_env["big"] / "SKILL.md").read_bytes() == before


class TestCrashRecovery:
    def test_self_inflicted_dirt_restored(self, git_env):
        """Every dirty path manifested as an intended write -> auto-restore."""
        cs = git_env["cs"]
        target = git_env["small"] / "SKILL.md"
        rel = "skills-shared/devops/cron-alert-discipline/SKILL.md"
        snap = cs.snapshot_shared([git_env["small"]], [rel],
                                  shared_root=git_env["shared"])
        assert snap is not None
        original = target.read_text(encoding="utf-8")
        target.write_text("half-applied crash leftovers", encoding="utf-8")
        recovered, why = cs.attempt_crash_recovery(git_env["shared"])
        assert recovered, why
        assert target.read_text(encoding="utf-8") == original
        ok, reason, _ = cs.git_precheck_shared(git_env["shared"])
        assert ok

    def test_unmanifested_sibling_dirt_skips(self, git_env):
        """Any un-manifested dirty path -> skip-and-report, NEVER clobber."""
        cs = git_env["cs"]
        rel = "skills-shared/devops/cron-alert-discipline/SKILL.md"
        snap = cs.snapshot_shared([git_env["small"]], [rel],
                                  shared_root=git_env["shared"])
        assert snap is not None
        # sibling dirt in a path the manifest never recorded
        sibling = git_env["shared"] / "smart-home" / "clanker-e2e" / "SKILL.md"
        sibling_text = sibling.read_text(encoding="utf-8") + "\nsibling edit\n"
        sibling.write_text(sibling_text, encoding="utf-8")
        recovered, why = cs.attempt_crash_recovery(git_env["shared"])
        assert not recovered
        assert "sibling edit present" in why
        assert sibling.read_text(encoding="utf-8") == sibling_text

    def test_superset_dirt_skips(self, git_env):
        """Manifested dirt PLUS an extra un-manifested path -> skip."""
        cs = git_env["cs"]
        target = git_env["small"] / "SKILL.md"
        rel = "skills-shared/devops/cron-alert-discipline/SKILL.md"
        snap = cs.snapshot_shared([git_env["small"]], [rel],
                                  shared_root=git_env["shared"])
        assert snap is not None
        target.write_text("crash leftovers", encoding="utf-8")
        extra = git_env["shared"] / "devops" / "extra-sibling.md"
        extra.write_text("sibling", encoding="utf-8")
        recovered, why = cs.attempt_crash_recovery(git_env["shared"])
        assert not recovered
        assert extra.read_text(encoding="utf-8") == "sibling"

    def test_no_snapshot_skips(self, git_env):
        cs = git_env["cs"]
        (git_env["small"] / "SKILL.md").write_text("dirt", encoding="utf-8")
        recovered, why = cs.attempt_crash_recovery(git_env["shared"])
        assert not recovered
        assert "no shared snapshot" in why


# Fleet-side pieces unsubscribe_shared_skill drives: scripts/lib/skill_tier.py (DEFAULT_IDLE_DAYS +
# load_last_used) and skills-shared/hermes-harness/skill-finder/scripts/skill-group.py (`move --in-place`).
# They live in hermes-home; these stubs keep the same contract (t_776ae7bc).
_TIER_STUB = textwrap.dedent("""\
    import datetime, json
    DEFAULT_IDLE_DAYS = 60
    def _iso(s):
        try:
            return datetime.datetime.fromisoformat(str(s)).timestamp() if s else None
        except ValueError:
            return None
    def load_last_used(usage_path, stats_path):
        last, created = {}, {}
        try:
            usage = json.load(open(usage_path))
        except (OSError, ValueError):
            usage = {}
        for n, r in usage.items():
            if _iso(r.get("last_used_at")):
                last[n] = _iso(r.get("last_used_at"))
            if _iso(r.get("created_at")):
                created[n] = _iso(r.get("created_at"))
        return last, created
    """)
_MOVER_STUB = textwrap.dedent("""\
    import argparse, json, os, sys
    ap = argparse.ArgumentParser()
    ap.add_argument("--home"); ap.add_argument("cmd"); ap.add_argument("skill"); ap.add_argument("to_group")
    ap.add_argument("--in-place", action="store_true"); ap.add_argument("--create", action="store_true")
    ap.add_argument("--json", action="store_true"); ap.add_argument("--operator")
    a = ap.parse_args()
    sh = os.path.join(a.home, "skills-shared")
    src = [g for g in os.listdir(sh) if os.path.isfile(os.path.join(sh, g, a.skill, "SKILL.md"))]
    if not a.in_place or not a.operator or len(src) != 1:
        print(json.dumps({"ok": False, "error": "refused"})); sys.exit(6)
    dst = os.path.join(sh, a.to_group)
    created = not os.path.isdir(dst)
    if created:
        os.makedirs(dst); open(os.path.join(dst, "README.md"), "w").write("# aux\\n")
    os.rename(os.path.join(sh, src[0], a.skill), os.path.join(dst, a.skill))
    print(json.dumps({"ok": True, "created_group": created, "in_place": True}))
    """)


def plant_fleet_tier(fleet_root: Path) -> None:
    lib = fleet_root / "scripts" / "lib"
    lib.mkdir(parents=True, exist_ok=True)
    (lib / "skill_tier.py").write_text(_TIER_STUB, encoding="utf-8")
    mover = fleet_root / "skills-shared" / "hermes-harness" / "skill-finder" / "scripts"
    mover.mkdir(parents=True, exist_ok=True)
    (mover / "skill-group.py").write_text(_MOVER_STUB, encoding="utf-8")


def _record(days_idle: int) -> dict:
    ts = (datetime.now(timezone.utc) - timedelta(days=days_idle)).isoformat()
    return {"created_by": "agent", "created_at": ts, "last_used_at": ts, "use_count": 1}


@pytest.fixture
def tier_env(git_env):
    plant_fleet_tier(git_env["home"])
    _git(git_env["home"], "add", "-A")
    _git(git_env["home"], "commit", "-q", "-m", "fleet tier + mover")
    return git_env


class TestSharedUnsubscribe:
    """t_776ae7bc: a shared skill is never archived; idle >= skill_tier.DEFAULT_IDLE_DAYS -> <group>-aux."""

    @pytest.mark.live_system_guard_bypass
    def test_31_day_idle_is_refused_and_stays_put(self, tier_env):
        cs = tier_env["cs"]
        ok, msg, _ = cs.unsubscribe_shared_skill(tier_env["small"], shared_root=tier_env["shared"],
                                                 record=_record(31))
        assert not ok and "stays subscribed" in msg
        assert (tier_env["small"] / "SKILL.md").is_file()
        assert not list(tier_env["shared"].glob("*/.archive"))

    @pytest.mark.live_system_guard_bypass
    def test_61_day_idle_lands_in_aux_with_curator_commit(self, tier_env):
        cs = tier_env["cs"]
        ok, msg, touched = cs.unsubscribe_shared_skill(tier_env["small"], shared_root=tier_env["shared"],
                                                       record=_record(61))
        assert ok, msg
        dest = tier_env["shared"] / "devops-aux" / "cron-alert-discipline"
        assert (dest / "SKILL.md").is_file() and not tier_env["small"].exists()
        assert not list(tier_env["shared"].glob("*/.archive"))
        home = tier_env["home"]
        assert _git(home, "log", "-1", "--format=%s").stdout.startswith("curator: unsubscribe idle shared skill")
        status = _git(home, "show", "-M", "--name-status", "--format=", "HEAD").stdout
        assert ("R100\tskills-shared/devops/cron-alert-discipline/SKILL.md\t"
                "skills-shared/devops-aux/cron-alert-discipline/SKILL.md") in status
        assert "A\tskills-shared/devops-aux/README.md" in status
        left = [ln for ln in _git(home, "status", "--porcelain", "--", "skills-shared").stdout.splitlines()
                if not ln.endswith(cs.LOCK_NAME)]
        assert left == []  # the move is fully committed (the lockfile is infra, never content)

    def test_missing_tier_module_refuses(self, git_env):
        cs = git_env["cs"]
        ok, msg, _ = cs.unsubscribe_shared_skill(git_env["small"], shared_root=git_env["shared"],
                                                 record=_record(200))
        assert not ok and "never archived" in msg
        assert git_env["small"].is_dir()

    def test_rejects_outside_shared(self, git_env, tmp_path):
        cs = git_env["cs"]
        stray = tmp_path / "stray-skill"
        stray.mkdir()
        ok, msg, _ = cs.unsubscribe_shared_skill(stray, shared_root=git_env["shared"])
        assert not ok

    @pytest.mark.live_system_guard_bypass
    def test_succeeds_despite_sibling_dirt(self, tier_env):
        """Unrelated dirt elsewhere must not block the move, nor be absorbed into the curator commit."""
        cs = tier_env["cs"]
        home = tier_env["home"]
        (tier_env["big"] / "SKILL.md").write_text("sibling edit", encoding="utf-8")
        (home / "cron-state.json").write_text("{}", encoding="utf-8")
        ok, msg, _ = cs.unsubscribe_shared_skill(tier_env["small"], shared_root=tier_env["shared"],
                                                 record=_record(90))
        assert ok, msg
        assert (tier_env["big"] / "SKILL.md").read_text() == "sibling edit"
        committed = set(_git(home, "show", "--name-only", "--format=", "HEAD").stdout.split())
        assert not any("clanker-e2e" in p or "cron-state" in p for p in committed)

    @pytest.mark.live_system_guard_bypass
    @pytest.mark.parametrize("days,lands_in_aux", [(31, False), (61, True)])
    def test_archive_skill_never_archives_shared(self, tier_env, days, lands_in_aux):
        """tools.skill_usage.archive_skill (the chokepoint the curator's LLM delete and the time-based
        pass both reach) never writes skills-shared/**/.archive: 31d idle is refused, 61d goes to -aux."""
        import agent.skill_utils as su
        importlib.reload(su)
        import tools.skill_usage as usage
        importlib.reload(usage)
        data = usage.load_usage()
        data["cron-alert-discipline"] = {**usage._empty_record(), **_record(days)}
        usage.save_usage(data)
        ok, msg = usage.archive_skill("cron-alert-discipline")
        shared = tier_env["shared"]
        assert not list(shared.glob("*/.archive")), msg
        assert not (tier_env["home"] / "skills" / ".archive" / "cron-alert-discipline").exists()
        assert ok is lands_in_aux, msg
        assert (shared / "devops-aux" / "cron-alert-discipline").is_dir() is lands_in_aux
        assert tier_env["small"].is_dir() is not lands_in_aux

    def test_archive_skill_still_readonly_flag_off(self, git_env, monkeypatch):
        (git_env["home"] / "config.yaml").write_text(
            (git_env["home"] / "config.yaml").read_text().replace(
                "include_shared_dirs: true", "include_shared_dirs: false"
            ),
            encoding="utf-8",
        )
        import agent.skill_utils as su
        importlib.reload(su)
        import tools.skill_usage as usage
        importlib.reload(usage)
        ok, msg = usage.archive_skill("cron-alert-discipline")
        assert not ok
        assert "read-only" in msg
        assert git_env["small"].exists()
