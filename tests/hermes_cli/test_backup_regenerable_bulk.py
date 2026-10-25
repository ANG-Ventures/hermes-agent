"""Full-tier excludes for regenerable bulk found by the 2026-10-18 census (t_5bea54fb).

The 10-18 Sunday full listed 6,973,690 files and was killed 8 h 49 m in. A names-only census through the
real walker put var/scratch (offbox-ci tree exports) at 3.79 M and the gh-shim per-call state at 1.14 M.
"""
import zipfile
from pathlib import Path

from hermes_cli.backup import _matches_path_glob, _should_exclude, _write_full_zip_backup_locked


def test_excludes_regenerable_bulk():
    for p in (
        "var/scratch/offbox-ci/hermes-agent-1918-33b1c5cd/run_agent.py",
        "var/gh-app/readcache/0a1b.json", "var/gh-app/callers/pid-1.json", "var/gh-app/raw/pgid-7.json",
        "var/gh-app/etag/4f/abc.json", "var/oneshot/t_6d07a184/rt/contributors/emails/x",  # 2026-10-21
        "var/lines-portal/repos/hermes-agent/README.md", "var/graphify/fleetreview-hermes-agent/g.json",
        "mirrors/ANG-Ventures/hermes-agent.git/HEAD", "runtime/hermes-agent/run_agent.py",
        "kanban/workspaces-aside/t_1a75a536-home-slice-sparse-clone/a.py",
        "state/test-gate/cache/00/abc.pass",
    ):
        assert _should_exclude(Path(p)), p
    # the walk prunes the dir itself (prune sites pass a trailing "_" sentinel)
    for d in ("var/scratch", "var/gh-app/readcache", "var/gh-app/etag", "var/oneshot/t_any", "mirrors",
              "runtime/hermes-agent"):
        assert _matches_path_glob((*Path(d).parts, "_")), d


def test_excludes_operator_worktrees_at_root_only():
    """Ace 2026-10-25 (t_c10109a6): root wt/ and worktrees/ are out of the full tier (548,785 of the 1,432,791
    files that broke the 3600 s ceiling). Same-named dirs deeper in the tree are still carried."""
    for p in ("wt/t_b2fe497d/scripts/x.py", "worktrees/lq-stopped-retire/.git/HEAD", "worktrees/a/b/c"):
        assert _should_exclude(Path(p)), p
    for d in ("wt", "worktrees"):
        assert _matches_path_glob((d, "_")), d
    for p in ("skills/foo/worktrees/README.md", "profiles/aegis/worktrees/x/y.py", "projects/wt/notes.md",
              "wt.md", "worktrees.json"):
        assert not _should_exclude(Path(p)), p


def test_keeps_siblings_and_deeper_same_names():
    for p in ("var/gh-app/ang-fleet-ci-actuators.164943290.json", "var/lines-portal/generate.py",
              "runtime/active_sessions.json", "state/test-gate/remote-000b.lock", "var/scratch-notes.md",
              "var/oneshot/land-5882.sh", "var/gh-app/cap/watchers.1.json",  # loose one-shots + cap state stay
              "skills/x/mirrors/SKILL.md", "profiles/p/runtime/hermes-agent/a.py"):
        assert not _should_exclude(Path(p)), p


def test_excludes_root_db_rollback_copies_only_at_root():
    for p in ("kanban.db.bak-notify-sweep-20261018-110107", "state.db.bak-haiku55", "kanban.db.bak-1790817214",
              "kanban.db.close-ac10-1790243395.bak", "x.sqlite.bak"):
        assert _should_exclude(Path(p)), p
    for p in ("kanban.db", "state.db", "config.yaml.bak-configset", "profiles/p/state.db.bak-x",
              "skills/x/state.db.bak-fixture"):
        assert not _should_exclude(Path(p)), p


def test_full_writer_never_archives_them(tmp_path):
    home = tmp_path / "home"
    keep = ("config.yaml", "var/gh-app/app.json", "var/lines-portal/generate.py", "runtime/active_sessions.json")
    drop = ("var/scratch/offbox-ci/t/a.py", "var/gh-app/readcache/k.json", "mirrors/o/r.git/HEAD",
            "runtime/hermes-agent/x.py", "kanban.db.bak-notify-sweep-20261018-110107")
    for rel in keep + drop:
        f = home / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x")
    out = tmp_path / "out.zip"
    assert _write_full_zip_backup_locked(out, home) == out
    names = set(zipfile.ZipFile(out).namelist())
    assert set(keep) <= names
    assert not names & set(drop), names & set(drop)
