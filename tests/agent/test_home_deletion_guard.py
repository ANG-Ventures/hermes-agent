"""Recursive deletes of the Hermes home's protected roots are refused before execution.

2026-10-13: a test script ran ``rm -rf "$HERMES_HOME"`` while the variable still held the
live home. Every text-level layer allowed it: the variable was never expanded, and the rm
lived inside a script invoked by path. These tests pin the pre-exec analyzer that expands
simple ``$VAR`` references against the environment (no eval, no subshell) and reads one
level of invoked scripts.

All roots are temp-dir fixtures; nothing here touches a real home.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from agent.home_deletion_guard import command_deletes_protected_home


@pytest.fixture
def home(tmp_path: Path) -> dict:
    """A fake fleet root with the protected layout, plus the env that points at it."""
    user = tmp_path / "user"
    root = user / ".hermes"
    for rel in ("profiles/aegis", "profiles/apollo", "runtime/hermes-agent", "hooks-installed",
                ".git", "cache/scratch/x", "kanban/workspaces/t_1"):
        (root / rel).mkdir(parents=True)
    (root / "state.db").write_bytes(b"")
    env = {"HOME": str(user), "HERMES_HOME": str(root), "PATH": os.environ.get("PATH", "")}
    return {"root": root, "user": user, "env": env, "cwd": str(user)}


def check(home: dict, command: str, **overrides):
    env = dict(home["env"])
    env.update(overrides.pop("env", {}))
    return command_deletes_protected_home(command, env=env, cwd=overrides.pop("cwd", home["cwd"]))


# ── the incident shapes ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("command", [
    'rm -rf "$HERMES_HOME"',
    "rm -rf ${HERMES_HOME}",
    "rm -rf $HERMES_HOME/",
    "/bin/rm -rf $HERMES_HOME",
    "rm -r -f -- $HERMES_HOME",
    "rm --recursive --force $HERMES_HOME",
    "sudo rm -Rf $HERMES_HOME",
    "rm -rf ~/.hermes",
    "rm -rf $HOME/.hermes",
    "cd /tmp && rm -fr ~/.hermes",
])
def test_recursive_delete_of_home_root_is_refused(home, command):
    hit = check(home, command)
    assert hit is not None
    assert str(home["root"].resolve()) in hit


def test_the_0602_declaration_shape_resolves_to_the_live_home(home):
    # `export A=$(…) A=$A` expands every word before assigning: A ends up as the OLD value.
    cmd = 'export HERMES_HOME=$(mktemp -d) HERMES_HOME=$HERMES_HOME; rm -rf "$HERMES_HOME"'
    assert check(home, cmd) is not None


def test_plain_sequential_assignment_to_a_temp_dir_is_allowed(home):
    # Plain assignments are sequential: the second word sees the (unknown) mktemp value.
    cmd = 'HERMES_HOME=$(mktemp -d) HERMES_HOME=$HERMES_HOME; rm -rf "$HERMES_HOME"'
    assert check(home, cmd) is None


def test_literal_assignment_in_the_same_command_is_tracked(home):
    cmd = f'D={home["root"]}/runtime; rm -rf "$D"'
    assert check(home, cmd) is not None


def test_script_invoked_by_path_is_read(home, tmp_path):
    script = tmp_path / "rd-1956.sh"
    script.write_text('#!/usr/bin/env bash\nset -e\necho hi\nrm -rf "$HERMES_HOME"\n')
    for cmd in (f"bash {script}", f"sh {script}", f"bash -x {script}"):
        assert check(home, cmd) is not None, cmd


def test_executable_script_invoked_directly_is_read(home, tmp_path):
    script = tmp_path / "go.sh"
    script.write_text('#!/bin/sh\nrm -rf "$HOME/.hermes"\n')
    script.chmod(0o755)
    assert check(home, f"{script}", cwd=str(tmp_path)) is not None
    assert check(home, "./go.sh", cwd=str(tmp_path)) is not None


def test_python_script_rmtree_of_env_home_is_refused(home, tmp_path):
    script = tmp_path / "cleanup.py"
    script.write_text(
        "import os, shutil\n"
        "home = os.environ['HERMES_HOME']\n"
        "shutil.rmtree(home)\n"
    )
    assert check(home, f"python3 {script}") is not None


@pytest.mark.parametrize("code", [
    "import shutil, os; shutil.rmtree(os.environ['HERMES_HOME'])",
    "import shutil, os; shutil.rmtree(os.getenv('HERMES_HOME'))",
    "import shutil, os; shutil.rmtree(os.path.expanduser('~/.hermes'))",
    "import shutil, pathlib, os; shutil.rmtree(pathlib.Path(os.environ['HERMES_HOME']) / 'runtime')",
    "import os; os.removedirs(os.environ['HERMES_HOME'] + '/profiles/aegis')",
])
def test_python_inline_rmtree_forms_are_refused(home, code):
    assert check(home, f'python3 -c "{code}"') is not None


def test_bash_c_inline_body_is_analyzed(home):
    assert check(home, "bash -c 'rm -rf \"$HERMES_HOME\"'") is not None


# ── every protected root, plus ancestors ────────────────────────────────────────────────

@pytest.mark.parametrize("rel", [
    "", "profiles", "profiles/aegis", "profiles/brand-new", "runtime", "runtime/",
    "hooks-installed", ".git", "state.db",
])
def test_each_protected_root(home, rel):
    target = home["root"] / rel if rel else home["root"]
    assert check(home, f"rm -rf {target}") is not None


def test_runtime_checkout_itself_is_protected(home):
    assert check(home, f"rm -rf {home['root']}/runtime/hermes-agent") is not None
    assert check(home, f"rm -rf {home['root']}/runtime/hermes-agent/build") is None


@pytest.mark.parametrize("target", ["{user}", "{user}/..", "/"])
def test_ancestors_of_the_home_are_refused(home, target):
    assert check(home, "rm -rf " + target.format(user=home["user"])) is not None


def test_profile_home_as_hermes_home_still_protects_the_fleet_root(home):
    profile = home["root"] / "profiles" / "aegis"
    assert check(home, f"rm -rf {home['root']}", env={"HERMES_HOME": str(profile)}) is not None
    assert check(home, 'rm -rf "$HERMES_HOME"', env={"HERMES_HOME": str(profile)}) is not None


def test_relative_target_resolves_against_cwd_and_cd(home):
    assert check(home, "rm -rf .hermes", cwd=str(home["user"])) is not None
    assert check(home, f"cd {home['root']} && rm -rf runtime") is not None
    assert check(home, f"cd {home['root']} && rm -rf cache/scratch/x") is None


def test_glob_that_matches_a_protected_root_is_refused(home):
    assert check(home, "rm -rf ~/.hermes/*") is not None
    assert check(home, "rm -rf ~/.hermes/profiles/*") is not None
    assert check(home, "rm -rf ~/.hermes/cache/*") is None
    # bash `*` does not match dotfiles, so ~/* never reaches ~/.hermes
    assert check(home, "rm -rf ~/*") is None


def test_symlink_to_the_home_is_resolved_with_trailing_slash(home, tmp_path):
    link = tmp_path / "h"
    link.symlink_to(home["root"])
    assert check(home, f"rm -rf {link}/") is not None
    # Without the slash rm removes the link itself, not its target.
    assert check(home, f"rm -rf {link}") is None


@pytest.mark.parametrize("command", [
    "find ~/.hermes -delete",
    "find $HERMES_HOME -type f -delete",
    "find ~/.hermes/runtime -exec rm -rf {} +",
])
def test_find_delete_of_a_protected_root_is_refused(home, command):
    assert check(home, command) is not None


@pytest.mark.parametrize("command", [
    "git clean -fdx",
    "git clean -xfd",
    "git -C ~/.hermes clean -fdX",
])
def test_git_clean_x_in_the_home_is_refused(home, command):
    assert check(home, command, cwd=str(home["root"])) is not None


# ── I2: sub-paths and unrelated deletes run normally ────────────────────────────────────

@pytest.mark.parametrize("command", [
    "rm -rf ~/.hermes/cache/scratch/x",
    'rm -rf "$HERMES_HOME/cache/scratch/x"',
    "rm -rf ~/.hermes/kanban/workspaces/t_1",
    "rm -rf ~/.hermes/runtime/hermes-agent/build",
    "rm -rf ~/.hermes/profiles/aegis/cache",
    "rm -rf \"$(mktemp -d)\"",
    "rm -rf /tmp/foo",
    "rm ~/.hermes/profiles/aegis/notes.txt",
    "rm -f ~/.hermes",  # not recursive: rm refuses a directory
    "ls -la ~/.hermes",
    "echo rm -rf ~/.hermes",
    "echo 'rm -rf $HERMES_HOME'",
    "grep -r rmtree ~/.hermes/scripts",
    "find ~/.hermes -name '*.pyc' -delete",
    "find ~/.hermes/cache -delete",
    "git clean -fd",
    "rm -rf $UNSET_VARIABLE_XYZ",
])
def test_sub_paths_and_non_deletes_are_allowed(home, command):
    assert check(home, command) is None


def test_git_clean_x_outside_the_home_is_allowed(home, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert check(home, "git clean -fdx", cwd=str(repo)) is None


def test_non_recursive_rm_of_state_db_is_refused(home):
    assert check(home, "rm -f ~/.hermes/state.db") is not None


def test_script_deleting_only_sub_paths_is_allowed(home, tmp_path):
    script = tmp_path / "ok.sh"
    script.write_text('S=$(mktemp -d); trap \'rm -rf "$S"\' EXIT\nrm -rf "$HERMES_HOME/cache/scratch/x"\n')
    assert check(home, f"bash {script}") is None


def test_python_rmtree_of_a_sub_path_is_allowed(home):
    code = "import shutil, os; shutil.rmtree(os.path.join(os.environ['HERMES_HOME'], 'cache'))"
    assert check(home, f'python3 -c "{code}"') is None


def test_oversized_or_binary_script_is_not_read(home, tmp_path):
    big = tmp_path / "big.sh"
    big.write_text('rm -rf "$HERMES_HOME"\n' + "#" * (1024 * 1024 + 10))
    assert check(home, f"bash {big}") is None
    binary = tmp_path / "bin.sh"
    binary.write_bytes(b'rm -rf "$HERMES_HOME"\n\x00\x01')
    assert check(home, f"bash {binary}") is None


def test_nested_script_is_not_followed_past_depth_one(home, tmp_path):
    inner = tmp_path / "inner.sh"
    inner.write_text('rm -rf "$HERMES_HOME"\n')
    outer = tmp_path / "outer.sh"
    outer.write_text(f"bash {inner}\n")
    assert check(home, f"bash {inner}") is not None
    assert check(home, f"bash {outer}") is None


# ── Prism #2006 findings: shell word semantics the analyzer must follow ───────────────

@pytest.mark.parametrize("command", [
    # An opaque cwd hides relative targets only; absolute ones still resolve.
    'cd "$(mktemp -d)" && rm -rf "$HERMES_HOME"',
    'cd "$(mktemp -d)" && git -C "$HERMES_HOME" clean -fdx',
    # Unquoted expansions are field-split: the second field is the home.
    'TARGETS="/tmp/cache $HERMES_HOME"; rm -rf $TARGETS',
    # `-e PAT` / `--exclude PAT` are option arguments, not pathspecs.
    "git -C \"$HERMES_HOME\" clean -fdx -e '*.log'",
    "git -C \"$HERMES_HOME\" clean -fdx --exclude '*.log'",
    "git -C \"$HERMES_HOME\" clean -fdxe '*.log'",
    "git -C \"$HERMES_HOME\" clean -fdx -e*.log",
    # A default word is expanded with the same rules as any other word.
    'unset SCRATCH; rm -rf "${SCRATCH:-$HERMES_HOME}"',
    "rm -rf ${SCRATCH:-$HERMES_HOME}",
    "rm -rf ${SCRATCH:-~/.hermes}",
])
def test_prism_2006_shapes_are_refused(home, command):
    hit = check(home, command, cwd="/tmp")
    assert hit is not None
    assert str(home["root"].resolve()) in hit


@pytest.mark.parametrize("command", [
    'cd "$(mktemp -d)" && rm -rf .hermes',              # relative under an opaque cwd: unknown
    'T="/tmp/cache $HERMES_HOME"; rm -rf "$T"',          # quoted: one nonexistent path
    'SCRATCH=/tmp/x; rm -rf "${SCRATCH:-$HERMES_HOME}"',  # the variable is set: default unused
    'rm -rf "${S:-~}/.hermes"',                          # quoted default: no tilde expansion
    "git -C \"$HERMES_HOME\" clean -fdx -e '*.log' cache",  # a real pathspec below the home
    "git -C \"$HERMES_HOME\" clean -ndx -e '*.log'",     # dry run
])
def test_prism_2006_neighbours_stay_allowed(home, command):
    assert check(home, command, cwd="/tmp") is None


def test_snapshot_exports_reads_only_the_export_dump(tmp_path):
    from agent.home_deletion_guard import _UNKNOWN, snapshot_exports

    snap = tmp_path / "snap.sh"
    snap.write_text(
        'declare -x D="/x/runtime"\ndeclare -x Q=\'a b\'\ndeclare -x OP="$(id)"\ndeclare -x FLAG\n'
        "f () \n{ \n    declare -x LATE=/nope\n}\nalias ll='ls -l'\n")
    out = snapshot_exports(str(snap))
    assert out["D"] == "/x/runtime" and out["Q"] == "a b" and out["OP"] is _UNKNOWN
    assert "LATE" not in out and "FLAG" not in out
    assert snapshot_exports(str(tmp_path / "missing.sh")) == {}


def test_snapshot_exports_continue_past_exported_arrays(tmp_path):
    from agent.home_deletion_guard import _UNKNOWN, snapshot_exports

    snap = tmp_path / "snap.sh"
    snap.write_text('declare -ax A=([0]="x" [1]="y z")\ndeclare -ax E=()\ndeclare -x D="/x/runtime"\n'
                    "f () \n{ \n    declare -x LATE=/nope\n}\n")
    out = snapshot_exports(str(snap))
    assert out["A"] is _UNKNOWN and out["D"] == "/x/runtime" and "LATE" not in out


def test_quoted_default_keeps_exact_whitespace(home, tmp_path):
    spaced = tmp_path / "hermes  home"
    spaced.mkdir()
    env = {"HERMES_HOME": str(spaced)}
    assert check(home, f'rm -rf "${{TARGET:-{spaced}}}"', env=env, cwd="/tmp") is not None


def test_quoted_empty_default_is_an_empty_argument(home):
    # Git treats `-C ""` as "stay here", so cwd = the home and the clean is real.
    assert check(home, 'git -C "${UNSET:-}" clean -fdx', cwd=str(home["root"])) is not None


# ── cost ───────────────────────────────────────────────────────────────────────────────

def test_ordinary_commands_stay_well_under_50ms(home):
    commands = [
        "ls -la", "git status --short", "python3 -m pytest tests/agent -q -o addopts=",
        "cd /tmp && rm -rf /tmp/build-123 && mkdir -p /tmp/build-123",
        "grep -rn 'def foo' . | head -20",
    ] * 10
    check(home, "ls")  # warm imports
    start = time.perf_counter()
    for cmd in commands:
        check(home, cmd)
    per_call_ms = (time.perf_counter() - start) * 1000 / len(commands)
    assert per_call_ms < 50, per_call_ms



def test_empty_ifs_joins_an_unquoted_default_word_with_blanks(tmp_path: Path):
    """Prism d0b8d9a3d9eb: with ``IFS=`` the blank inside ``${U:-/x/hermes home}`` is NOT a field
    break; the shell passes one path. A home whose path contains a space must still be refused."""
    user = tmp_path / "user x"
    root = user / "hermes home"
    (root / "profiles" / "aegis").mkdir(parents=True)
    (root / "state.db").write_bytes(b"")
    env = {"HOME": str(user), "HERMES_HOME": str(root), "PATH": os.environ.get("PATH", "")}
    cmd = "rm -rf ${UNSET:-%s}" % str(root)
    # default IFS: the blank splits, the two halves are not protected roots: allowed (shell behaviour)
    assert not command_deletes_protected_home(cmd, env=env, cwd=str(user))
    # IFS empty: one field = the protected root: refused
    assert command_deletes_protected_home("IFS=; " + cmd, env=env, cwd=str(user))
    assert command_deletes_protected_home(cmd, env=dict(env, IFS=""), cwd=str(user))
    # IFS set to something that does not contain a blank: still one field
    assert command_deletes_protected_home(cmd, env=dict(env, IFS=":"), cwd=str(user))


@pytest.mark.parametrize("name", ["hermes  home", "hermes;home", "hermes|home", "hermes&home",
                                  "hermes(home)", "hermes<home>", "hermes#home"])
def test_default_word_keeps_literal_whitespace_and_metacharacters(tmp_path: Path, name: str):
    """Prism fb6e5bf412c4: the default word of ``${U:-word}`` is one shell word, not command
    source. Bash keeps the two spaces and the ``;``/``|``/``(`` literally inside it, so the
    analyzer must resolve exactly that path, not a re-tokenized one."""
    user = tmp_path / "u"
    root = user / name
    (root / "profiles" / "aegis").mkdir(parents=True)
    env = {"HOME": str(user), "HERMES_HOME": str(root), "PATH": os.environ.get("PATH", "")}
    for cmd in ("IFS=; rm -rf ${UNSET:-%s}" % root,
                'rm -rf "${UNSET:-%s}"' % root,
                'rm -rf "${UNSET:-"%s"}"' % root,
                "rm -rf ${UNSET:-'%s'}" % root,
                'rm -rf ${UNSET:-"%s"}' % root):
        assert command_deletes_protected_home(cmd, env=env, cwd=str(user)), cmd
    if " " not in name:  # no blank to split on: the bare unquoted default is the root itself
        assert command_deletes_protected_home("rm -rf ${UNSET:-%s}" % root, env=env, cwd=str(user))


def test_default_word_literal_text_is_split_on_ifs(tmp_path: Path):
    """The default's unquoted literal text is field-split on IFS like a variable value:
    ``IFS=:`` turns ``/nope:<root>`` into two fields, the second being the protected root."""
    user = tmp_path / "u"
    root = user / ".hermes"
    (root / "profiles").mkdir(parents=True)
    env = {"HOME": str(user), "HERMES_HOME": str(root), "PATH": os.environ.get("PATH", "")}
    cmd = "rm -rf ${UNSET:-/nope:%s}" % root
    assert not command_deletes_protected_home(cmd, env=env, cwd=str(user))  # one field, not a root
    assert command_deletes_protected_home("IFS=:; " + cmd, env=env, cwd=str(user))
    # a quoted or escaped separator is not split
    assert not command_deletes_protected_home("IFS=:; rm -rf ${UNSET:-'/nope:'%s}" % root, env=env, cwd=str(user))



def test_tilde_in_a_default_word_is_not_field_split(tmp_path: Path):
    """Prism 40715861ef6b: the result of tilde expansion is never field-split, so with a
    blank in ``HOME`` ``${UNSET:-~/.hermes}`` is still ONE argument (bash 5) naming the
    protected root. The analyzer must not split the expanded home into unrelated paths."""
    user = tmp_path / "user name"
    root = user / ".hermes"
    (root / "profiles" / "aegis").mkdir(parents=True)
    env = {"HOME": str(user), "HERMES_HOME": str(root), "PATH": os.environ.get("PATH", "")}
    for cmd in ("rm -rf ${UNSET:-~/.hermes}", "rm -rf ${UNSET:-~}/.hermes", "rm -rf ~/.hermes",
                'D=~/.hermes; rm -rf "$D/"'):
        assert command_deletes_protected_home(cmd, env=env, cwd="/tmp"), cmd
    # only the text AFTER the home prefix is split: `/a b` past the tilde still breaks
    assert not command_deletes_protected_home("rm -rf ${UNSET:-~/.hermes/x y}", env=env, cwd="/tmp")


@pytest.mark.parametrize("template", [
    "rm -rf ${UNSET:-%s\\\n%s}",
    'rm -rf "${UNSET:-%s\\\n%s}"',
    'rm -rf ${UNSET:-"%s\\\n%s"}',
    "rm -rf %s\\\n%s",
    'rm -rf "%s\\\n%s"',
])
def test_backslash_newline_is_a_line_continuation(home, template):
    """Prism eabfb3a4f752: the shell deletes a backslash-newline pair before tokenizing,
    inside and outside double quotes and inside a ``${X:-word}`` default. Splitting the
    protected path with one must still resolve to the protected path."""
    root = str(home["root"])
    assert check(home, template % (root[:-3], root[-3:]), cwd="/tmp") is not None


def test_backslash_newline_inside_names_and_commands(home):
    for cmd in ('rm -rf "${HERMES\\\n_HOME}"', "r\\\nm -rf $HERMES_HOME", "rm -\\\nrf $HERMES_HOME"):
        assert check(home, cmd) is not None, cmd
    # inside single quotes the pair is literal; in a comment it ends the comment
    assert check(home, "rm -rf '%s\\\n'" % home["root"]) is None
    assert check(home, "# rm -rf \\\necho %s" % home["root"]) is None
