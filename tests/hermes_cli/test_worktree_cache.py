"""Tests for ``hermes worktree-cache`` clone mode (card t_90fccfb9, spec section 9).

GitHub URLs are redirected to local bare repos with ``url.<x>.insteadOf`` (as in
test_kanban_clone.py), so the real git transport, the real mirror and the real
registration run end to end with no network.

Copy-on-write: tests marked ``real_cow`` need a clone-capable filesystem (APFS,
XFS reflink=1, btrfs, ZFS block cloning). pytest's tmp dir is used when it can
clone; otherwise ``HERMES_WORKTREE_CACHE_TEST_DIR`` names one (on ACE-AI /tmp
is ext4, the workspace volume is XFS). With neither, the real-clone tests skip
and the guard tests run against an injected copy primitive.
"""
from __future__ import annotations

import ast
import errno
import fcntl
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from hermes_cli import kanban_clone as kc
from hermes_cli import worktree_cache as wc

SLUG = "Kyzcreig/demo"
URL = f"https://github.com/{SLUG}.git"


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True, capture_output=True).stdout.strip()


def _commit(work, msg):
    _git("add", "-A", cwd=work)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", msg, cwd=work)
    return _git("rev-parse", "HEAD", cwd=work)


def _real_cow(directory: Path) -> bool:
    wc._PROBE_CACHE.clear()
    try:
        return wc.cow_probe(directory)
    finally:
        wc._PROBE_CACHE.clear()


@pytest.fixture
def base(tmp_path):
    override = os.environ.get("HERMES_WORKTREE_CACHE_TEST_DIR", "").strip()
    if override:
        root = Path(override) / f"wc-{uuid.uuid4().hex[:8]}"
        root.mkdir(parents=True)
        yield root
        shutil.rmtree(root, ignore_errors=True)
    else:
        yield tmp_path


@pytest.fixture
def env(base, tmp_path, monkeypatch):
    """Origin repos, mirrors, templates, ledger and log, all under *base*."""
    src = base / "src"
    work = src / "work" / "demo"
    work.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=work)
    (work / ".gitattributes").write_text("*.txt text eol=lf\n*.bat text eol=crlf\n")
    (work / ".gitignore").write_text("*.log\nbuild/\n")
    (work / "README.txt").write_text("hello\n")
    (work / "run.bat").write_text("echo hi\n")
    (work / "pkg" / "sub").mkdir(parents=True)
    for i in range(30):
        (work / "pkg" / f"m{i}.py").write_text(f"X = {i}\n" * 200)
    (work / "pkg" / "sub" / "big.bin").write_bytes(os.urandom(256 * 1024))
    tool = work / "tool.sh"
    tool.write_text("#!/bin/sh\necho tool\n")
    tool.chmod(0o755)
    os.symlink("pkg/m1.py", work / "link.py")
    c1 = _commit(work, "c1")
    (work / "pkg" / "m2.py").write_text("X = 'changed'\n")
    (work / "new.txt").write_text("new\n")
    c2 = _commit(work, "c2")
    _git("checkout", "-q", "-b", "feat", cwd=work)
    (work / "feat.txt").write_text("feature\n")
    (work / "pkg" / "m3.py").unlink()
    c3 = _commit(work, "feat")
    _git("checkout", "-q", "main", cwd=work)
    bare = src / "Kyzcreig" / "demo.git"
    bare.parent.mkdir(parents=True)
    _git("clone", "-q", "--bare", str(work), str(bare))

    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.file://{src}/.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/")
    monkeypatch.setenv(kc.MIRRORS_ENV, str(base / "mirrors"))
    monkeypatch.setenv(kc.HOOKS_ROOT_ENV, str(tmp_path / "no-hooks-root"))
    monkeypatch.setenv(wc.ENV_TEMPLATES, str(base / "templates"))
    monkeypatch.setenv(wc.LEDGER_ENV, str(base / "clone-fallback.jsonl"))
    monkeypatch.setenv(wc.ENV_LOG, str(base / "worktree-cache.jsonl"))
    monkeypatch.delenv(wc.ENV_SWITCH, raising=False)
    monkeypatch.setattr(wc, "_PROBE_CACHE", {})
    (base / "ws").mkdir()
    return {"base": base, "work": work, "bare": bare, "c1": c1, "c2": c2, "c3": c3,
            "s": wc.Settings(enabled=True, scopes=("kanban_clone",), repos=(SLUG,))}


@pytest.fixture
def cow(env, monkeypatch):
    """Real CoW when *base* can clone, else an injected byte-copy primitive (guard logic only)."""
    if _real_cow(env["base"]):
        return "real"

    def fake(src_dir, names, dst_dir):
        for n in names:
            s = Path(src_dir) / n
            if s.is_symlink():
                os.symlink(os.readlink(s), Path(dst_dir) / n)
            elif s.is_dir():
                shutil.copytree(s, Path(dst_dir) / n, symlinks=True)
            else:
                shutil.copy2(s, Path(dst_dir) / n)

    monkeypatch.setattr(wc, "_clone_entries", fake)
    monkeypatch.setattr(wc, "cow_probe", lambda d: True)
    return "fake"


@pytest.fixture
def real_cow(env):
    if not _real_cow(env["base"]):
        pytest.skip("no clone-capable filesystem here; set HERMES_WORKTREE_CACHE_TEST_DIR")
    return "real"


def _rows(path):
    p = Path(path)
    return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []


def ledger(env):
    return _rows(env["base"] / "clone-fallback.jsonl")


def log(env):
    return _rows(env["base"] / "worktree-cache.jsonl")


def snapshot(root: Path) -> dict:
    """relpath -> (mode, sha256 | symlink target) for every non-.git entry."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            rel = str(p.relative_to(root))
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                out[rel] = ("l", os.readlink(p))
            elif stat.S_ISDIR(st.st_mode):
                if (p / ".git").exists() or p.is_symlink():
                    continue
                out[rel] = ("d",)
            else:
                out[rel] = (oct(st.st_mode & 0o777), hashlib.sha256(p.read_bytes()).hexdigest())
    return out


def plain(env, name, **kw):
    dest = env["base"] / "ws" / name
    assert wc.plain_clone(URL, str(dest), **kw) == 0
    return dest


def built(env):
    res = wc.refresh(SLUG)
    assert res["status"] == "built", res
    return Path(res["gen"])


def no_staging_left(env):
    return not [p for p in (env["base"] / "ws").iterdir() if ".cow-" in p.name]


def _status(dest):
    return _git("status", "--porcelain=v1", "--untracked-files=all", cwd=dest)


# --------------------------------------------------------------------------- clone mode, end to end


@pytest.mark.parametrize("kw,head", [({}, "c2"), ({"branch": "feat"}, "c3"), ({"rev": "c1"}, "c1")])
def test_cow_clone_equals_plain_clone(env, cow, kw, head):
    built(env)
    kw = {k: (env[v] if k == "rev" else v) for k, v in kw.items()}
    dest = env["base"] / "ws" / "cow"
    assert wc.clone(URL, str(dest), caller="kanban_clone", s=env["s"], **kw) == 0
    ref = plain(env, "plain", **kw)
    assert snapshot(dest) == snapshot(ref)
    assert _git("rev-parse", "HEAD", cwd=dest) == env[head]
    assert _git("symbolic-ref", "-q", "HEAD", cwd=dest) if "rev" not in kw else True
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=dest) == _git("rev-parse", "--abbrev-ref", "HEAD", cwd=ref)
    assert _status(dest) == ""
    assert (dest / ".git" / "objects" / "info" / "alternates").exists()     # borrows from the mirror
    assert ledger(env) == []
    (row,) = log(env)
    assert row["cow"] is True and row["reason"] is None and row["T"] == env["c2"] and row["C"] == env[head]
    assert no_staging_left(env)
    tpl = wc.current_gen(wc.repo_dir(SLUG))
    assert wc.template_problem(tpl) is None


def test_refresh_moves_template_and_asserts_config(env):
    gen = built(env)
    assert _git("rev-parse", "HEAD", cwd=gen) == env["c2"]
    for key, value in wc.TEMPLATE_CONFIG:
        assert _git("config", "--get", key, cwd=gen) == value
    _git("config", "gc.auto", "1", cwd=gen)                       # drift
    (env["work"] / "later.txt").write_text("later\n")
    c4 = _commit(env["work"], "c4")
    _git("push", "-q", str(env["bare"]), "main", cwd=env["work"])
    res = wc.refresh(SLUG)
    assert res["status"] == "refreshed" and res["head"] == c4 and res["config_drift"] == ["gc.auto"]
    assert wc.read_meta(gen)["head"] == c4
    assert wc.template_problem(gen) is None


# --------------------------------------------------------------------------- 9.1 cross-volume


def test_cross_volume_never_copies(env, cow, monkeypatch):
    built(env)
    import tempfile

    here = os.stat(env["base"]).st_dev
    others = [p for p in (Path("/dev/shm"), Path(tempfile.gettempdir())) if p.is_dir() and os.stat(p).st_dev != here]
    if not others:
        pytest.skip("no second filesystem (/dev/shm, $TMPDIR) to put the destination on")
    other = others[0]
    dest = other / f"wc-xvol-{uuid.uuid4().hex[:8]}"
    called = []
    monkeypatch.setattr(wc, "_clone_entries", lambda *a: called.append(a))
    try:
        assert wc.clone(URL, str(dest), caller="kanban_clone", s=env["s"]) == 0
        assert called == []
        assert snapshot(dest) == snapshot(plain(env, "plain"))
        (row,) = ledger(env)
        assert row["reason"] == "cross_volume" and row["caller"] == "worktree_cache:kanban_clone"
        assert row["src_dev"] != row["dst_dev"] and row["repo"] == SLUG and row["bytes"] > 0
    finally:
        shutil.rmtree(dest, ignore_errors=True)


# --------------------------------------------------------------------------- 9.3 non-cloning filesystems


@pytest.mark.parametrize("code", [errno.EXDEV, errno.ENOTSUP, errno.EOPNOTSUPP])
def test_clone_errno_falls_back_with_one_row(env, cow, monkeypatch, code):
    built(env)

    def refuse(*_a):
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(wc, "_clone_entries", refuse)
    dest = env["base"] / "ws" / "cow"
    assert wc.clone(URL, str(dest), caller="kanban_clone", s=env["s"]) == 0
    assert snapshot(dest) == snapshot(plain(env, "plain"))
    (row,) = ledger(env)
    assert row["reason"] == "clone_errno" and row["errno"] == errno.errorcode[code]
    assert no_staging_left(env)
    assert log(env)[-1]["cow"] is False


def test_no_cow_fs_probe(env, monkeypatch):
    built(env)
    monkeypatch.setattr(wc, "cow_probe", lambda d: False)
    dest = env["base"] / "ws" / "cow"
    assert wc.clone(URL, str(dest), caller="kanban_clone", s=env["s"]) == 0
    assert snapshot(dest) == snapshot(plain(env, "plain"))
    assert [r["reason"] for r in ledger(env)] == ["no_cow_fs"]


def test_real_tmpfs_refuses_reflink(env, monkeypatch):
    """A real non-cloning filesystem: template and destination both on /dev/shm (tmpfs)."""
    shm = Path("/dev/shm")
    if not shm.is_dir():
        pytest.skip("no /dev/shm")
    root = shm / f"wc-tmpfs-{uuid.uuid4().hex[:8]}"
    root.mkdir()
    try:
        if _real_cow(root):
            pytest.skip("/dev/shm clones on this host")
        monkeypatch.setenv(wc.ENV_TEMPLATES, str(root / "templates"))
        built(env)
        dest = root / "cow"
        assert wc.clone(URL, str(dest), caller="kanban_clone", s=env["s"]) == 0
        assert snapshot(dest) == snapshot(plain(env, "plain"))
        assert [r["reason"] for r in ledger(env)] == ["no_cow_fs"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------- 9.4 isolation, no hardlinks


def test_edits_in_a_clone_never_reach_the_template(env, real_cow):
    gen = built(env)
    before = snapshot(gen)
    dest = env["base"] / "ws" / "cow"
    assert wc.clone(URL, str(dest), caller="kanban_clone", s=env["s"]) == 0
    assert log(env)[-1]["cow"] is True
    (dest / "README.txt").write_text("edited\n")
    with open(dest / "pkg" / "sub" / "big.bin", "r+b") as f:
        f.truncate(10)
    (dest / "tool.sh").chmod(0o600)
    os.utime(dest / "pkg" / "m4.py", (1, 1))
    (dest / "pkg" / "m5.py").unlink()
    (dest / "pkg" / "m6.py").write_bytes(b"overwritten in place")
    assert snapshot(gen) == before
    assert wc.template_problem(gen) is None
    st = os.stat(gen / "pkg" / "m4.py")
    assert st.st_nlink == 1 and st.st_mtime > 1


def test_helper_never_hardlinks_or_byte_copies():
    tree = ast.parse(Path(wc.__file__).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in ("link", "linkat", "copytree", "copy2", "copy"), node.attr
        if isinstance(node, ast.List) and node.elts and isinstance(node.elts[0], ast.Constant) \
                and node.elts[0].value == "cp":
            consts = [e.value for e in node.elts if isinstance(e, ast.Constant)]
            assert "--reflink=always" in consts and "-l" not in consts and "--link" not in consts


def no_registration(monkeypatch):
    """Record every helper git call; the caller asserts no ``clone --no-checkout`` ran."""
    calls = []
    real = wc._git

    def spy(*args, **kw):
        calls.append(args)
        return real(*args, **kw)

    monkeypatch.setattr(wc, "_git", spy)
    return lambda: not [a for a in calls if "clone" in a and "--no-checkout" in a]


# --------------------------------------------------------------------------- 9.5 verify failure


def test_verify_failure_falls_back_and_registers_nothing(env, cow, monkeypatch):
    built(env)
    real = wc._clone_entries

    def corrupting(src_dir, names, dst_dir):
        real(src_dir, names, dst_dir)
        target = Path(dst_dir) / "pkg" / "m7.py"
        target.unlink()                  # break the CoW link before writing
        target.write_text("corrupt\n")

    monkeypatch.setattr(wc, "_clone_entries", corrupting)
    dest = env["base"] / "ws" / "cow"
    unregistered = no_registration(monkeypatch)
    assert wc.clone(URL, str(dest), caller="kanban_clone", s=env["s"]) == 0
    assert snapshot(dest) == snapshot(plain(env, "plain"))
    assert [r["reason"] for r in ledger(env)] == ["verify_failed"]
    assert unregistered()
    assert no_staging_left(env)
    assert not list((env["base"] / "ws").glob(".*.idx"))


def test_extra_file_in_staging_fails_verify(env, cow, monkeypatch):
    built(env)
    real = wc._clone_entries

    def extra(src_dir, names, dst_dir):
        real(src_dir, names, dst_dir)
        (Path(dst_dir) / "debug.log").write_text("ignored but present\n")

    monkeypatch.setattr(wc, "_clone_entries", extra)
    dest = env["base"] / "ws" / "cow"
    unregistered = no_registration(monkeypatch)
    assert wc.clone(URL, str(dest), caller="kanban_clone", s=env["s"]) == 0
    assert not (dest / "debug.log").exists()
    assert [r["reason"] for r in ledger(env)] == ["verify_failed"]
    assert unregistered()


# --------------------------------------------------------------------------- 9.6 dirty / moved template


@pytest.mark.parametrize("dirt,reason", [("untracked", "template_dirty"), ("modified", "template_dirty"),
                                         ("moved", "template_moved")])
def test_dirty_template_is_retired_and_rebuilt(env, cow, dirt, reason):
    gen = built(env)
    if dirt == "untracked":
        (gen / "stray.txt").write_text("x\n")
    elif dirt == "modified":
        (gen / "README.txt").write_text("modified\n")
    else:
        _git("checkout", "-q", "--detach", env["c1"], cwd=gen)
    dest = env["base"] / "ws" / "cow"
    assert wc.clone(URL, str(dest), caller="kanban_clone", s=env["s"]) == 0
    assert snapshot(dest) == snapshot(plain(env, "plain"))
    assert [r["reason"] for r in ledger(env)] == [reason]
    rdir = wc.repo_dir(SLUG)
    assert not gen.exists() and list(rdir.glob("gen-1.bad-*"))
    assert wc.current_gen(rdir) is None
    res = wc.refresh(SLUG)
    assert res["status"] == "built" and res["gen"].endswith("gen-2")
    dest2 = env["base"] / "ws" / "cow2"
    assert wc.clone(URL, str(dest2), caller="kanban_clone", s=env["s"]) == 0
    assert log(env)[-1]["cow"] is True


def test_ignored_file_in_template_is_dirty(env):
    gen = built(env)
    (gen / "x.log").write_text("ignored\n")
    assert wc.template_problem(gen) == "template_dirty"


# --------------------------------------------------------------------------- 9.7 lock


def test_refresh_lock_makes_create_fall_back(env, cow, monkeypatch):
    built(env)
    monkeypatch.setattr(wc, "CREATE_LOCK_TIMEOUT_S", 0.5)
    monkeypatch.setattr(wc, "_clone_entries", lambda *a: pytest.fail("cloned under a refresh lock"))
    dest = env["base"] / "ws" / "cow"
    with wc._flock(wc.repo_dir(SLUG) / ".lock", fcntl.LOCK_EX, 1) as got:
        assert got
        t0 = time.monotonic()
        assert wc.clone(URL, str(dest), caller="kanban_clone", s=env["s"]) == 0
        assert time.monotonic() - t0 < 30
    assert snapshot(dest) == snapshot(plain(env, "plain"))
    assert [r["reason"] for r in ledger(env)] == ["lock_timeout"]


def test_refresh_skips_round_when_a_create_holds_the_lock(env):
    built(env)
    with wc._flock(wc.repo_dir(SLUG) / ".lock", fcntl.LOCK_SH, 1) as got:
        assert got
        assert wc.refresh(SLUG, lock_timeout=0.2)["status"] == "lock_timeout"


# --------------------------------------------------------------------------- 9.8 off switch


@pytest.mark.parametrize("how", ["disabled", "no_scope", "env"])
def test_off_switch_is_plain_git_without_a_row(env, cow, monkeypatch, how):
    built(env)
    s = env["s"]
    if how == "disabled":
        s = wc.Settings(enabled=False, scopes=("kanban_clone",), repos=(SLUG,))
    elif how == "no_scope":
        s = wc.Settings(enabled=True, scopes=("dispatcher_worktree",), repos=(SLUG,))
    else:
        monkeypatch.setenv(wc.ENV_SWITCH, "0")
    monkeypatch.setattr(wc, "_clone_entries", lambda *a: pytest.fail("cache ran while off"))
    dest = env["base"] / "ws" / "cow"
    assert wc.clone(URL, str(dest), caller="kanban_clone", s=s) == 0
    assert snapshot(dest) == snapshot(plain(env, "plain"))
    assert ledger(env) == [] and log(env) == []


def test_default_settings_are_off():
    s = wc.settings({})
    assert s.enabled is False and s.scopes == () and s.repos == wc.DEFAULT_REPOS
    assert wc.cache_active("kanban_clone", s) is False
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["worktree_cache"]["enabled"] is False
    assert DEFAULT_CONFIG["worktree_cache"]["scopes"] == []


# --------------------------------------------------------------------------- other fallbacks


def test_far_commit_finishes_like_plain_git(env, cow):
    built(env)
    s = wc.Settings(enabled=True, scopes=("kanban_clone",), repos=(SLUG,), max_diff_files=1)
    dest = env["base"] / "ws" / "cow"
    assert wc.clone(URL, str(dest), caller="kanban_clone", branch="feat", s=s) == 0
    assert snapshot(dest) == snapshot(plain(env, "plain", branch="feat"))
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=dest) == "feat"
    assert _status(dest) == ""
    assert [r["reason"] for r in ledger(env)] == ["far_commit"]
    assert no_staging_left(env)


def test_unknown_rev_is_a_real_git_failure(env, cow):
    built(env)
    dest = env["base"] / "ws" / "cow"
    assert wc.clone(URL, str(dest), caller="kanban_clone", rev="0" * 40, s=env["s"]) != 0


def test_unsupported_tree(env, cow):
    (env["work"] / ".gitattributes").write_text("*.txt text eol=lf\n*.secret filter=crypt\n")
    (env["work"] / "key.secret").write_text("not really\n")
    _commit(env["work"], "filter")
    _git("push", "-q", str(env["bare"]), "main", cwd=env["work"])
    gen = built(env)
    assert wc.read_meta(gen)["unsupported"] == "attr:filter"
    dest = env["base"] / "ws" / "cow"
    assert wc.clone(URL, str(dest), caller="kanban_clone", s=env["s"]) == 0
    assert snapshot(dest) == snapshot(plain(env, "plain"))
    assert [r["reason"] for r in ledger(env)] == ["unsupported_tree"]


@pytest.mark.parametrize("what", ["ident", "submodule"])
def test_target_adds_unsupported_tree(env, cow, what):
    """T is supported; the branch C adds an ident attribute (or a .gitmodules) for files
    that are unchanged, so the copied files would skip git's smudge path (Prism e400891faded)."""
    work = env["work"]
    (work / "f.txt").write_text("$Id$\n")
    _commit(work, "id file")
    _git("push", "-q", str(env["bare"]), "main", cwd=work)
    _git("checkout", "-q", "-b", "tgt", cwd=work)
    if what == "ident":
        (work / ".gitattributes").write_text("*.txt text eol=lf\n*.bat text eol=crlf\nf.txt ident\n")
    else:
        (work / ".gitmodules").write_text('[submodule "x"]\n\tpath = x\n\turl = ../x.git\n')
    _commit(work, what)
    _git("push", "-q", str(env["bare"]), "tgt", cwd=work)
    _git("checkout", "-q", "main", cwd=work)
    gen = built(env)
    assert wc.read_meta(gen)["unsupported"] is None
    dest = env["base"] / "ws" / "cow"
    assert wc.clone(URL, str(dest), caller="kanban_clone", branch="tgt", s=env["s"]) == 0
    ref = plain(env, "plain", branch="tgt")
    assert snapshot(dest) == snapshot(ref)
    if what == "ident":
        assert (ref / "f.txt").read_text() != "$Id$\n"        # git did expand it
    assert [r["reason"] for r in ledger(env)] == ["unsupported_tree"]
    assert no_staging_left(env)


def _push_eol_crlf(env, branch="main"):
    """A commit that changes only the eol rule of README.txt (blob unchanged)."""
    work = env["work"]
    (work / ".gitattributes").write_text("*.txt text eol=crlf\n*.bat text eol=crlf\n")
    c = _commit(work, "eol crlf")
    _git("push", "-q", str(env["bare"]), f"HEAD:{branch}", cwd=work)
    return c


def test_target_changes_conversion_attributes(env, cow):
    """C changes only an eol rule: git does not rewrite the unchanged blobs on a two-tree
    move, so the copied LF files would differ from a plain clone (Prism 035c5ac28415)."""
    built(env)
    _git("checkout", "-q", "-b", "crlf", cwd=env["work"])
    _push_eol_crlf(env, "crlf")
    _git("checkout", "-q", "main", cwd=env["work"])
    dest = env["base"] / "ws" / "cow"
    assert wc.clone(URL, str(dest), caller="kanban_clone", branch="crlf", s=env["s"]) == 0
    ref = plain(env, "plain", branch="crlf")
    assert (ref / "README.txt").read_bytes() == b"hello\r\n"         # git did convert it
    assert snapshot(dest) == snapshot(ref)
    assert [r["reason"] for r in ledger(env)] == ["unsupported_tree"]
    assert no_staging_left(env)


def test_refresh_rebuilds_when_attributes_change(env, cow):
    """The template's own checkout T -> C has the same blind spot: an attributes change
    builds a fresh generation instead of moving the old one."""
    gen = built(env)
    c = _push_eol_crlf(env)
    res = wc.refresh(SLUG)
    assert res["status"] == "built" and res["head"] == c, res
    assert res["retired"]["reason"] == "attributes_changed"
    new = Path(res["gen"])
    assert new != gen and (new / "README.txt").read_bytes() == b"hello\r\n"
    dest = env["base"] / "ws" / "cow"
    assert wc.clone(URL, str(dest), caller="kanban_clone", s=env["s"]) == 0
    assert snapshot(dest) == snapshot(plain(env, "plain"))
    assert ledger(env) == []


def test_tracked_symlink_named_like_the_meta_tmp_is_not_followed(env, tmp_path):
    """A repo tracking `.template.json.tmp -> <outside>` must not get the meta written
    through it (Prism 216606a5a11d / da864fb31967)."""
    victim = tmp_path / "victim"
    victim.write_text("keep\n")
    os.symlink(str(victim), env["work"] / f".{wc.META}.tmp")
    _commit(env["work"], "trap")
    _git("push", "-q", str(env["bare"]), "main", cwd=env["work"])
    gen = built(env)
    assert victim.read_text() == "keep\n"
    assert wc.read_meta(gen)["head"] == _git("rev-parse", "HEAD", cwd=env["work"])
    wc.refresh(SLUG)
    assert victim.read_text() == "keep\n"


def test_cli_parser_builds_without_fcntl():
    """Native Windows has no fcntl: building the CLI parser must not import it
    (Prism f4a156e1a646 / 63f40a41d7d6)."""
    code = ("import sys; sys.modules['fcntl'] = None\n"
            "from hermes_cli.main import _build_cli_parser\n"
            "p, _ = _build_cli_parser()\n"
            "p.parse_args(['worktree-cache', 'status'])\n"
            "print('ok')\n")
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=str(Path(wc.__file__).resolve().parents[1]))
    assert res.returncode == 0 and res.stdout.strip() == "ok", res.stderr[-2000:]


def test_pre_register_error_falls_back_with_one_row(env, cow, monkeypatch):
    """A templates dir the creating user cannot lock: plain clone, one ledger row, no traceback."""
    built(env)

    def denied(*_a, **_k):
        raise PermissionError(errno.EACCES, "Permission denied", "/templates/.lock")

    monkeypatch.setattr(wc, "_flock", denied)
    dest = env["base"] / "ws" / "cow"
    assert wc.clone(URL, str(dest), caller="kanban_clone", s=env["s"]) == 0
    assert snapshot(dest) == snapshot(plain(env, "plain"))
    (row,) = ledger(env)
    assert row["reason"] == "clone_errno" and row["errno"] == "EACCES"
    assert no_staging_left(env)


def test_malformed_template_meta_falls_back_with_one_row(env, cow):
    gen = built(env)
    meta = wc.read_meta(gen)
    meta["refreshed_at"] = "yesterday"
    (gen / wc.META).write_text(json.dumps(meta))
    dest = env["base"] / "ws" / "cow"
    assert wc.clone(URL, str(dest), caller="kanban_clone", s=env["s"]) == 0
    assert snapshot(dest) == snapshot(plain(env, "plain"))
    (row,) = ledger(env)
    assert row["reason"] == "clone_errno" and row["errno"] is None


def test_no_template(env, cow):
    dest = env["base"] / "ws" / "cow"
    assert wc.clone(URL, str(dest), caller="kanban_clone", s=env["s"]) == 0          # nothing built yet
    s = wc.Settings(enabled=True, scopes=("kanban_clone",), repos=("Kyzcreig/other",))
    assert wc.clone(URL, str(env["base"] / "ws" / "b"), caller="kanban_clone", s=s) == 0
    assert [r["reason"] for r in ledger(env)] == ["no_template", "no_template"]


def test_status_reports_templates_and_fallbacks(env, cow):
    built(env)
    wc.clone(URL, str(env["base"] / "ws" / "x"), caller="kanban_clone",
             s=wc.Settings(enabled=True, scopes=("kanban_clone",), repos=("Kyzcreig/other",)))
    data = wc.status(env["s"])
    (tpl,) = data["templates"]
    assert tpl["repo"] == SLUG and tpl["current"] == "gen-1" and tpl["head"] == env["c2"]
    assert [r["reason"] for r in data["last_fallbacks"]] == ["no_template"]


def test_cli_parses_clone_refresh_status():
    import argparse

    p = argparse.ArgumentParser()
    wc.add_arguments(p)
    a = p.parse_args(["clone", "--url", URL, "--dest", "d", "-b", "x", "--caller", "kanban_clone"])
    assert a.func is wc.cmd_clone and a.branch == "x" and a.caller == "kanban_clone"
    assert p.parse_args(["refresh", "--all"]).func is wc.cmd_refresh
    assert p.parse_args(["status", "--json"]).json is True


# --------------------------------------------------------------------------- build card 3: `hermes kanban clone`


def _kanban_cli(argv, settings_obj, monkeypatch):
    """`hermes kanban clone <argv>` through the real kanban parser and dispatch."""
    import argparse

    from hermes_cli import kanban

    monkeypatch.setattr(wc, "settings", lambda cfg=None: settings_obj)
    parser = argparse.ArgumentParser()
    kanban.build_parser(parser.add_subparsers(dest="cmd"))
    return kanban.kanban_command(parser.parse_args(["kanban", "clone", *argv]))


def test_kanban_clone_off_is_the_old_call_exactly(env, cow, monkeypatch):
    calls = []
    monkeypatch.setattr(kc, "clone", lambda url, dest, opts: calls.append((url, dest, list(opts))) or 0)
    for s in (wc.Settings(), wc.Settings(enabled=True, scopes=("port_clone",), repos=(SLUG,))):
        assert _kanban_cli(["-q", "--depth", "1", URL, "d"], s, monkeypatch) == 0
    assert calls == [(URL, "d", ["--quiet", "--depth=1"])] * 2
    assert ledger(env) == [] and log(env) == []


def test_kanban_clone_on_copies_from_the_template(env, cow, monkeypatch):
    built(env)
    dest = env["base"] / "ws" / "k"
    assert _kanban_cli(["-q", "-b", "feat", URL, str(dest)], env["s"], monkeypatch) == 0
    assert snapshot(dest) == snapshot(plain(env, "plain", branch="feat"))
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=dest) == "feat"
    assert (dest / ".git" / "objects" / "info" / "alternates").exists()
    assert ledger(env) == []
    (row,) = log(env)
    assert row["caller"] == "kanban_clone" and row["cow"] is True and row["C"] == env["c3"]


def test_kanban_clone_blob_none_filter_is_served_from_the_template(env, cow, monkeypatch):
    """5 of 11 ci-box card checkouts fell back on ``--filter=blob:none`` (t_ef5e004a): a full
    template copy holds every blob that filter would fetch lazily, so the cache serves it."""
    built(env)
    want = snapshot(plain(env, "plain", branch="feat"))
    seen = []
    monkeypatch.setattr(kc, "clone", lambda url, dest, o: seen.append(list(o)) or 1)
    dest = env["base"] / "ws" / "k"
    assert _kanban_cli(["-q", "--filter", "blob:none", "-b", "feat", URL, str(dest)], env["s"], monkeypatch) == 0
    assert seen == []                                                   # no plain git clone
    assert snapshot(dest) == want
    assert _git("rev-parse", "--abbrev-ref", "HEAD", cwd=dest) == "feat"
    assert ledger(env) == []
    (row,) = log(env)
    assert row["cow"] is True and row["reason"] is None and row["opts_served"] == ["--filter=blob:none"]


def test_kanban_clone_blob_none_fallback_still_forwards_the_filter(env, cow, monkeypatch):
    """No template for the repo: plain git gets the caller's filter, exactly as before."""
    seen = []
    monkeypatch.setattr(kc, "clone", lambda url, dest, o: seen.append(list(o)) or 0)
    dest = env["base"] / "ws" / "k"
    assert _kanban_cli(["--filter", "blob:none", URL, str(dest)], env["s"], monkeypatch) == 0
    assert len(seen) == 1 and "--filter=blob:none" in seen[0]
    (row,) = log(env)
    assert row["reason"] == "no_template" and row["opts_served"] == ["--filter=blob:none"]


def _refs_and_config(dest):
    refs = _git("for-each-ref", "--format=%(refname) %(objectname)", cwd=dest)
    cfg = subprocess.run(["git", "config", "--get-regexp", r"^remote\.origin\."], cwd=dest, text=True,
                         capture_output=True).stdout
    return refs, sorted(line for line in cfg.splitlines() if not line.startswith("remote.origin.url "))


@pytest.mark.parametrize("opts", [["--single-branch"], ["--no-tags"], ["--single-branch", "--no-tags", "-b", "feat"]])
def test_kanban_clone_single_branch_and_no_tags_are_served_from_the_template(env, cow, monkeypatch, opts):
    """4 --single-branch and 1 --no-tags clones fell back ``unsupported_opts`` on the Studio (t_877a0b28):
    both only shape the registering clone's refs and remote config, never the working tree, so the cache
    passes them to that clone and the result equals a plain clone with the same options."""
    _git("tag", "v1", env["c1"], cwd=env["bare"])
    built(env)
    want = env["base"] / "ws" / "plain"
    long_opts = [o for o in opts if o.startswith("--")] + (["--branch=feat"] if "-b" in opts else [])
    assert kc.clone(URL, str(want), ["--quiet", *long_opts]) == 0
    seen = []
    real = kc.clone
    monkeypatch.setattr(kc, "clone", lambda url, dest, o: seen.append(list(o)) or real(url, dest, o))
    dest = env["base"] / "ws" / "k"
    assert _kanban_cli(["-q", *opts, URL, str(dest)], env["s"], monkeypatch) == 0
    assert seen == []                                                   # no plain git clone
    assert snapshot(dest) == snapshot(want)
    assert _refs_and_config(dest) == _refs_and_config(want)
    assert _status(dest) == ""
    assert ledger(env) == []
    (row,) = log(env)
    assert row["cow"] is True and row["reason"] is None
    assert row["opts_passed"] == [o for o in opts if o.startswith("--")]


def test_kanban_clone_single_branch_fallback_forwards_the_options(env, cow, monkeypatch):
    """No template: plain git gets --single-branch/--no-tags unchanged."""
    seen = []
    monkeypatch.setattr(kc, "clone", lambda url, dest, o: seen.append(list(o)) or 0)
    dest = env["base"] / "ws" / "k"
    assert _kanban_cli(["--single-branch", "--no-tags", URL, str(dest)], env["s"], monkeypatch) == 0
    assert len(seen) == 1 and "--single-branch" in seen[0] and "--no-tags" in seen[0]
    (row,) = log(env)
    assert row["reason"] == "no_template"


@pytest.mark.parametrize("opts", [["--depth", "1"], ["--no-checkout"], ["--no-single-branch"], ["-o", "up"], ["-v"],
                                  ["--filter", "tree:0"], ["--filter", "blob:none", "--depth", "1"]])
def test_kanban_clone_options_the_cache_cannot_make_run_plain_with_a_row(env, cow, monkeypatch, opts):
    built(env)
    seen = []
    real = kc.clone
    monkeypatch.setattr(kc, "clone", lambda url, dest, o: seen.append(list(o)) or real(url, dest, o))
    dest = env["base"] / "ws" / "k"
    assert _kanban_cli([*opts, URL, str(dest)], env["s"], monkeypatch) == 0
    assert len(seen) == 1 and seen[0] and "--quiet" not in seen[0]      # forwarded unchanged
    (row,) = ledger(env)
    assert row["reason"] == "unsupported_opts" and row["caller"] == "worktree_cache:kanban_clone"
    assert row["repo"] == SLUG and row["dst"] == str(dest)
    assert [r["reason"] for r in log(env)] == ["unsupported_opts"]


def test_active_exit_code_follows_switch_scope_and_env(monkeypatch):
    import argparse

    p = argparse.ArgumentParser()
    wc.add_arguments(p)
    args = p.parse_args(["active", "--caller", "port_clone"])
    monkeypatch.delenv(wc.ENV_SWITCH, raising=False)
    for s, rc in ((wc.Settings(), 1), (wc.Settings(enabled=True, scopes=("kanban_clone",)), 1),
                  (wc.Settings(enabled=True, scopes=("port_clone",)), 0)):
        monkeypatch.setattr(wc, "settings", lambda cfg=None, _s=s: _s)
        assert args.func(args) == rc
    monkeypatch.setenv(wc.ENV_SWITCH, "0")
    assert args.func(args) == 1


def test_switch_and_templates_root_come_from_the_host_root_config(tmp_path, monkeypatch):
    """A worker runs with HERMES_HOME=<root>/profiles/<p>; the host switch and kanban.workspaces_root
    live in <root>/config.yaml only (no Studio worker profile carries them)."""
    import hermes_constants

    root = tmp_path / "fleet-root"
    prof = root / "profiles" / "daedalus"
    prof.mkdir(parents=True)
    ws = tmp_path / "scratch" / "workspaces"
    (root / "config.yaml").write_text(
        "worktree_cache:\n  enabled: true\n  scopes: [kanban_clone]\n"
        f"kanban:\n  workspaces_root: {ws}\n")
    (prof / "config.yaml").write_text("worktree_cache:\n  enabled: false\n")
    monkeypatch.setenv("HERMES_HOME", str(prof))
    monkeypatch.delenv(wc.ENV_TEMPLATES, raising=False)
    monkeypatch.setattr(hermes_constants, "_get_platform_default_hermes_home", lambda: tmp_path / "native")
    monkeypatch.setattr(hermes_constants, "_default_hermes_root_memo", None)
    s = wc.settings()
    assert s.enabled is True and s.scopes == ("kanban_clone",)
    assert wc.templates_root() == ws / ".templates"
    (root / "config.yaml").write_text(": not yaml [")
    assert wc.settings().enabled is False                               # unreadable host config = OFF


# --------------------------------------------------------------------------- templates root per volume (t_259301d7)
# nas-fleet: card workspaces on /srv/fleet (ZFS dataset nas-fleet-scratch), the default templates root
# on the container rootfs (another dataset): every real kanban_clone fell back cross_volume.


def _two_volumes(env, monkeypatch, *, extra_root: bool):
    """Paths under <base>/volB report a second st_dev; the host config names a root there."""
    vol_b = env["base"] / "volB"
    (vol_b / "ws").mkdir(parents=True)
    real_dev = wc._dev

    def fake_dev(path):
        dev = real_dev(path)
        if dev is not None and Path(os.path.abspath(path)).is_relative_to(vol_b):
            return dev + 7_000_000
        return dev

    monkeypatch.setattr(wc, "_dev", fake_dev)
    monkeypatch.delenv(wc.ENV_TEMPLATES, raising=False)
    cfg = {"worktree_cache": {"enabled": True, "scopes": ["kanban_clone"], "repos": [SLUG]},
           "kanban": {"workspaces_root": str(env["base"] / "ws-default")}}
    if extra_root:
        cfg["worktree_cache"]["templates_roots"] = [str(vol_b / "templates")]
    monkeypatch.setattr(wc, "host_config", lambda: cfg)
    return vol_b


def test_create_uses_the_templates_root_on_its_destination_volume(env, cow, monkeypatch):
    vol_b = _two_volumes(env, monkeypatch, extra_root=True)
    import argparse

    assert wc.cmd_refresh(argparse.Namespace(all=True, repo=None, force=True)) == 0   # a template in each root
    for dest in (vol_b / "ws" / "t_card" / "demo", env["base"] / "ws" / "port"):
        assert wc.clone(URL, str(dest), caller="kanban_clone", s=env["s"]) == 0
        row = log(env)[-1]
        assert row["cow"] is True and row["reason"] is None, row
        assert snapshot(dest) == snapshot(plain(env, f"plain-{dest.name}"))
    assert ledger(env) == []
    assert wc.templates_roots() == [vol_b / "templates", env["base"] / "ws-default" / ".templates"]
    pre = wc.dest_volume(vol_b / "ws" / "t_next" / "demo")             # the operator precondition
    assert pre["same_volume"] is True and pre["templates_root"] == str(vol_b / "templates")
    st = wc.status(dest=str(vol_b / "ws" / "x"))
    assert {t["root"] for t in st["templates"]} == {str(r) for r in wc.templates_roots()}
    assert st["dest"]["same_volume"] is True


def test_default_root_listed_first_in_templates_roots_stays_the_default(env, monkeypatch):
    """Prism 3ff4ce2b3cbb: the default root named first in templates_roots must not lose its role
    to the extra root (dedupe keeps the earlier position; roots[-1] was the extra root)."""
    vol_b = _two_volumes(env, monkeypatch, extra_root=False)
    default = env["base"] / "ws-default" / ".templates"
    cfg = wc.host_config()
    cfg["worktree_cache"]["templates_roots"] = [str(default), str(vol_b / "templates")]
    assert wc.templates_root() == default
    assert wc.status()["templates_root"] == str(default)
    assert wc.repo_dir(SLUG) == default / SLUG.replace("/", "__")
    assert wc.templates_root(vol_b / "ws" / "x") == vol_b / "templates"
    assert wc.templates_root(env["base"] / "ws" / "x") == default


def test_no_root_on_the_destination_volume_is_cross_volume(env, cow, monkeypatch):
    vol_b = _two_volumes(env, monkeypatch, extra_root=False)
    wc.refresh(SLUG)
    dest = vol_b / "ws" / "t_card" / "demo"
    pre = wc.dest_volume(dest)
    assert pre["same_volume"] is False and pre["templates_root"] == str(env["base"] / "ws-default" / ".templates")
    called = []
    monkeypatch.setattr(wc, "_clone_entries", lambda *a: called.append(a))
    assert wc.clone(URL, str(dest), caller="kanban_clone", s=env["s"]) == 0
    assert called == [] and ledger(env)[-1]["reason"] == "cross_volume"


# --------------------------------------------------------------------------- never-prune mirror (t_6de39491)
# Prism 1484b4563429: a clone that borrows from a mirror able to prune (format 0, or no
# extensions.preciousObjects) loses objects when the mirror is pruned. The helper repairs the
# mirror's never-prune keys under the mirror flock before it borrows, and dissociates when it cannot.


def _prunable_mirror(env) -> Path:
    mirror = kc.ensure_mirror(URL, "Kyzcreig", "demo")
    _git("--git-dir", str(mirror), "config", "--unset", "extensions.preciousObjects")
    _git("--git-dir", str(mirror), "config", "core.repositoryformatversion", "0")
    for k in ("gc.pruneExpire", "gc.reflogExpire", "gc.reflogExpireUnreachable"):
        _git("--git-dir", str(mirror), "config", "--unset", k)
    return mirror


def _prune_mirror(mirror: Path) -> None:
    """Drop every ref, then gc --prune=now: a format-0 mirror deletes the objects; a never-prune one refuses."""
    for sub in ("heads", "tags"):
        shutil.rmtree(mirror / "refs" / sub, ignore_errors=True)
        (mirror / "refs" / sub).mkdir()
    with __import__("contextlib").suppress(FileNotFoundError):
        (mirror / "packed-refs").unlink()
    subprocess.run(["git", "gc", "-q", "--prune=now"], cwd=mirror, capture_output=True)


def _readable(dest: Path) -> bool:
    return subprocess.run(["git", "-C", str(dest), "fsck", "--connectivity-only"],
                          capture_output=True).returncode == 0


def _never_prune(mirror: Path) -> bool:
    fmt = subprocess.run(["git", "--git-dir", str(mirror), "config", "--get", "core.repositoryformatversion"],
                         capture_output=True, text=True).stdout.strip()
    pre = subprocess.run(["git", "--git-dir", str(mirror), "config", "--bool", "--get", "extensions.preciousObjects"],
                         capture_output=True, text=True).stdout.strip()
    return fmt == "1" and pre == "true"


@pytest.mark.parametrize("path", ["cow", "fallback"])
def test_format0_mirror_is_repaired_before_a_clone_borrows(env, cow, path):
    if path == "cow":
        built(env)                  # the template exists before the mirror is degraded: the CLONE must repair
    mirror = _prunable_mirror(env)
    dest = env["base"] / "ws" / path
    if path == "cow":
        assert wc.clone(URL, str(dest), caller="kanban_clone", s=env["s"]) == 0
        assert log(env)[-1]["cow"] is True
    else:
        assert wc.plain_clone(URL, str(dest)) == 0
    assert (dest / ".git" / "objects" / "info" / "alternates").exists()    # borrowing stays cheap
    _prune_mirror(mirror)
    assert _readable(dest)                                                 # the prune took nothing it lends
    assert _never_prune(mirror)                                            # repaired under the mirror flock


@pytest.mark.parametrize("path", ["cow", "fallback"])
def test_unrepairable_mirror_is_dissociated(env, cow, path, monkeypatch):
    if path == "cow":
        built(env)
    mirror = _prunable_mirror(env)

    def cannot_write(_m):
        raise OSError(errno.EACCES, "read-only mirror config")

    monkeypatch.setattr(wc, "_write_never_prune_keys", cannot_write, raising=False)
    dest = env["base"] / "ws" / path
    if path == "cow":
        assert wc.clone(URL, str(dest), caller="kanban_clone", s=env["s"]) == 0
    else:
        assert wc.plain_clone(URL, str(dest)) == 0
    assert not _never_prune(mirror)
    assert not (dest / ".git" / "objects" / "info" / "alternates").exists()
    _prune_mirror(mirror)
    assert _readable(dest)
