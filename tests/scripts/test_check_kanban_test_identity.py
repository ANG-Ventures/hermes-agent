"""``scripts/check_kanban_test_identity.py``: a test that spawns ``kanban create`` states its
identity (t_f4c584e2, the hermes-agent#1825 class)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "check_kanban_test_identity", ROOT / "scripts" / "check_kanban_test_identity.py")
lint = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lint)

BARE = '''
import subprocess, sys
def test_spawns(tmp_path):
    subprocess.run([sys.executable, "-m", "hermes_cli.main", "kanban", "create", "T"])
'''

WITH_FIXTURE = '''
import subprocess, sys
def test_spawns(kanban_identity):
    subprocess.run([sys.executable, "-m", "hermes_cli.main", "kanban", "create", "T"],
                   env=kanban_identity.env)
'''


def test_bare_spawned_create_is_red_and_the_fixture_makes_it_green():
    assert lint.check_source(BARE, "t.py")
    assert lint.check_source(WITH_FIXTURE, "t.py") == []


def test_every_guarded_verb_and_shell_form_is_caught():
    for verb in ("create", "promote", "claim"):
        assert lint.check_source(BARE.replace('"create"', f'"{verb}"'), "t.py"), verb
    shell = 'import subprocess\ndef test_x():\n    subprocess.run("hermes kanban create T", shell=True)\n'
    assert lint.check_source(shell, "t.py")


def test_an_explicit_identity_on_the_spawn_is_green():
    for flag in ("--parent", "--session", "--home", "--unhomed"):
        src = BARE.replace('"create", "T"', f'"create", "T", "{flag}", "x"')
        assert lint.check_source(src, "t.py") == [], flag


def test_in_process_calls_and_non_guarded_verbs_are_not_spawns():
    in_process = ('def test_x(parser, kc):\n'
                  '    parser.parse_args(["kanban", "create", "T"])\n'
                  '    kc.run_slash("create T")\n'
                  '    event("/kanban create T")\n')
    assert lint.check_source(in_process, "t.py") == []
    assert lint.check_source(BARE.replace('"create"', '"list"'), "t.py") == []


def test_the_1825_helper_shape_is_red_bare_and_green_with_an_identity():
    # hermes-agent#1825 before its fix: ``kanban`` lives in the helper, the verb at the call.
    helper = ('import subprocess, sys\n'
              'def _cli(home, *args):\n'
              '    return subprocess.run([sys.executable, "-m", "hermes_cli.main", "kanban", *args])\n'
              'def test_x(home):\n'
              '    _cli(home, "create", "after-verb", "--board", "beta")\n'
              '    _cli(home, "boards", "create", "beta")\n'
              '    _cli(home, "boards", "show")\n')
    problems = lint.check_source(helper, "t.py")
    assert len(problems) == 1 and ":5:" in problems[0]
    assert lint.check_source(helper.replace('"beta")', '"beta", "--unhomed")'), "t.py") == []
    assert lint.check_source(helper.replace("test_x(home)", "test_x(kanban_identity)"), "t.py") == []


def test_the_live_tests_tree_is_clean():
    assert lint.main([str(ROOT / "tests")]) == 0


# --- kanban sandbox (t_65791cd2) --------------------------------------------

def test_disarming_the_sandbox_needs_a_stated_reason():
    bare = 'def test_x(monkeypatch):\n    monkeypatch.delenv("HERMES_KANBAN_SANDBOX", raising=False)\n'
    assert lint.check_sandbox_disarm(bare, "t.py")
    for shape in ('monkeypatch.setenv("HERMES_KANBAN_SANDBOX", "0")',
                  'os.environ.pop("HERMES_KANBAN_SANDBOX", None)',
                  'del os.environ["HERMES_KANBAN_SANDBOX"]',
                  'os.environ["HERMES_KANBAN_SANDBOX"] = "0"',
                  'monkeypatch.setitem(os.environ, "HERMES_KANBAN_SANDBOX", "0")',
                  'monkeypatch.delitem(os.environ, "HERMES_KANBAN_SANDBOX")',
                  'os.environ.update({"HERMES_KANBAN_SANDBOX": ""})'):
        assert lint.check_sandbox_disarm(f"import os\ndef test_x(monkeypatch):\n    {shape}\n", "t.py"), shape
    stated = bare.rstrip("\n") + "  # kanban-sandbox: off — tests pin precedence\n"
    assert lint.check_sandbox_disarm(stated, "t.py") == []
    armed = 'def test_x(monkeypatch):\n    monkeypatch.setenv("HERMES_KANBAN_SANDBOX", "1")\n'
    assert lint.check_sandbox_disarm(armed, "t.py") == []
    for armed in ('os.environ["HERMES_KANBAN_SANDBOX"] = "1"',
                  'monkeypatch.setitem(os.environ, "HERMES_KANBAN_SANDBOX", "1")'):
        assert lint.check_sandbox_disarm(f"import os\ndef test_x(monkeypatch):\n    {armed}\n", "t.py") == [], armed


def test_conftest_must_arm_the_sandbox_in_an_autouse_fixture(tmp_path):
    good = tmp_path / "good.py"
    good.write_text('import pytest\n@pytest.fixture(autouse=True)\ndef _env(monkeypatch):\n'
                    '    monkeypatch.setenv("HERMES_KANBAN_SANDBOX", "1")\n')
    assert lint.check_conftest_sandbox(good) == []
    not_autouse = tmp_path / "plain.py"
    not_autouse.write_text(good.read_text().replace("(autouse=True)", ""))
    assert lint.check_conftest_sandbox(not_autouse)
    assert lint.check_conftest_sandbox(ROOT / "tests" / "conftest.py") == []
