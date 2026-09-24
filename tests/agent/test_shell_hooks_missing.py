"""Missing hook infrastructure must not impersonate a policy denial."""

import logging
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent import shell_hooks


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    shell_hooks.reset_for_tests()
    yield root
    shell_hooks.reset_for_tests()


def _spec(path, policy="fail_open_and_page"):
    return shell_hooks.ShellHookSpec(
        event="pre_tool_call", command=f"{sys.executable} {path}",
        fail_closed=True, missing_hook_policy=policy,
    )


def test_missing_script_allows_logs_and_pages_once(home, monkeypatch, caplog):
    pages = []
    monkeypatch.setattr(shell_hooks, "_page_missing_hook", lambda path, *args: pages.append(path) or True)
    path = home / "hooks" / "merge_attribution_policy.py"
    cb = shell_hooks._make_callback(_spec(path))
    with caplog.at_level(logging.ERROR, logger=shell_hooks.logger.name):
        assert cb(tool_name="terminal", args={"command": "pwd"}) is None
        assert cb(tool_name="terminal", args={"command": "pwd"}) is None
    assert pages == [str(path)]
    assert str(path) in caplog.text and "failing open" in caplog.text


def test_registered_missing_hook_allows_real_tool_dispatch(home, monkeypatch):
    from hermes_cli import plugins
    monkeypatch.setattr(plugins, "_plugin_manager", plugins.PluginManager())
    pages = []
    monkeypatch.setattr(shell_hooks, "_page_missing_hook", lambda path, *args: pages.append(path) or True)
    path = home / "hooks" / "merge_attribution_policy.py"
    cfg = {"hooks": {"missing_hook_policy": "fail_open_and_page", "pre_tool_call": [{"command": f"{sys.executable} {path}",
                                        "matcher": "terminal", "fail_closed": True}]}}
    assert shell_hooks.register_from_config(cfg, accept_hooks=True)
    assert plugins.get_pre_tool_call_block_message(tool_name="terminal", args={"command": "pwd"}) is None
    assert pages == [str(path)]


def test_page_dedup_survives_runner_restart(home, monkeypatch):
    pages = []
    monkeypatch.setattr(shell_hooks, "_page_missing_hook", lambda path, *args: pages.append(path) or True)
    path = home / "hooks" / "missing.py"
    assert shell_hooks._make_callback(_spec(path))(tool_name="terminal") is None
    shell_hooks.reset_for_tests()
    assert shell_hooks._make_callback(_spec(path))(tool_name="terminal") is None
    assert pages == [str(path)]


def test_failed_page_does_not_burn_dedup(home, monkeypatch):
    pages = []
    def deliver(path, *args):
        pages.append(path)
        return len(pages) > 1
    monkeypatch.setattr(shell_hooks, "_page_missing_hook", deliver)
    path = home / "hooks" / "missing.py"
    cb = shell_hooks._make_callback(_spec(path))
    assert cb(tool_name="terminal") is None
    assert cb(tool_name="terminal") is None
    assert cb(tool_name="terminal") is None
    assert pages == [str(path), str(path)]


def test_real_exit_two_still_blocks(home):
    path = home / "policy.py"
    path.write_text('import sys\nprint("real denial", file=sys.stderr)\nsys.exit(2)\n')
    result = shell_hooks._make_callback(_spec(path))(tool_name="terminal")
    assert result == {"action": "block", "message": "real denial"}


def test_interpreter_missing_fails_open(home, monkeypatch):
    pages = []
    monkeypatch.setattr(shell_hooks, "_page_missing_hook", lambda path, *args: pages.append(path) or True)
    path = home / "policy.py"
    path.write_text("print('ok')\n")
    spec = shell_hooks.ShellHookSpec(event="pre_tool_call", command=f"{home / 'absent-python'} {path}", fail_closed=True, missing_hook_policy="fail_open_and_page")
    assert shell_hooks._make_callback(spec)(tool_name="terminal") is None
    assert pages


def test_missing_interpreted_script_exit_two_fails_open(home, monkeypatch):
    pages = []
    monkeypatch.setattr(shell_hooks, "_page_missing_hook", lambda path, *args: pages.append(path) or True)
    path = home / "hooks" / "missing.py"
    result = shell_hooks._make_callback(_spec(path))(tool_name="terminal")
    assert result is None
    assert pages == [str(path)]


def test_interpreter_cannot_open_file_exit_two_is_not_a_verdict(home, monkeypatch):
    path = home / "hooks" / "missing.py"
    pages = []
    monkeypatch.setattr(shell_hooks, "_page_missing_hook", lambda p, *args: pages.append(p) or True)
    result = shell_hooks._evaluate_result(_spec(path), {
        "returncode": 2, "stdout": "", "stderr": f"python3: can't open file '{path}': [Errno 2] No such file or directory",
        "error": None, "timed_out": False, "elapsed_seconds": 0.01,
    })
    assert result is None and pages == [str(path)]


def test_policy_reason_mentioning_missing_file_still_blocks(home):
    path = home / "policy.py"
    path.write_text('import sys\nprint("No such file or directory is forbidden", file=sys.stderr)\nsys.exit(2)\n')
    assert shell_hooks._make_callback(_spec(path))(tool_name="terminal") == {
        "action": "block", "message": "No such file or directory is forbidden",
    }


def test_self_heal_restores_from_head_then_runs(home, monkeypatch):
    subprocess.run(["git", "init", "-q", str(home)], check=True, stdin=subprocess.DEVNULL)
    path = home / "hooks" / "policy.py"
    path.parent.mkdir()
    path.write_text('import json\nprint(json.dumps({"action":"block","message":"restored policy"}))\n')
    subprocess.run(["git", "-C", str(home), "add", "hooks/policy.py"], check=True, stdin=subprocess.DEVNULL)
    subprocess.run(["git", "-C", str(home), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "fixture"], check=True, stdin=subprocess.DEVNULL)
    path.unlink()
    pages = []
    monkeypatch.setattr(shell_hooks, "_page_missing_hook", lambda p, *args: pages.append(p) or True)
    assert shell_hooks._make_callback(_spec(path))(tool_name="terminal") == {"action": "block", "message": "restored policy"}
    assert path.exists() and pages == [str(path)]


def test_profile_hook_self_heals_from_shared_checkout(home, monkeypatch):
    subprocess.run(["git", "init", "-q", str(home)], check=True, stdin=subprocess.DEVNULL)
    path = home / "hooks" / "policy.py"
    path.parent.mkdir()
    path.write_text('import json\nprint(json.dumps({"action":"block","message":"restored policy"}))\n')
    subprocess.run(["git", "-C", str(home), "add", "hooks/policy.py"], check=True, stdin=subprocess.DEVNULL)
    subprocess.run(["git", "-C", str(home), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "fixture"], check=True, stdin=subprocess.DEVNULL)
    path.unlink()
    profile = home / "profiles" / "daedalus"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    pages = []
    monkeypatch.setattr(shell_hooks, "_page_missing_hook", lambda p, *args: pages.append(p) or True)
    assert shell_hooks._make_callback(_spec(path))(tool_name="terminal") == {"action": "block", "message": "restored policy"}
    assert path.exists() and pages == [str(path)]


def test_diagnostic_missing_hook_does_not_page(home, monkeypatch):
    path = home / "hooks" / "missing.py"
    monkeypatch.setattr(shell_hooks, "_page_missing_hook", lambda p, *args: pytest.fail("diagnostic must not page"))
    spec = shell_hooks.ShellHookSpec(event="pre_tool_call", command=f"{sys.executable} {path}", fail_closed=True)
    result = shell_hooks.run_once(spec, {"tool_name": "terminal"})
    assert result["infra_failure"] == str(path)
    assert result["parsed"]["action"] == "block"
    assert "infrastructure" in result["parsed"]["message"]


def test_unrecoverable_missing_hook_blocks_with_infra_reason_and_pages(home, monkeypatch):
    path = home / "hooks" / "absent.py"
    pages = []
    monkeypatch.setattr(shell_hooks, "_page_missing_hook", lambda p, *args: pages.append(p) or True)
    cb = shell_hooks._make_callback(shell_hooks.ShellHookSpec(
        event="pre_tool_call", command=f"{sys.executable} {path}", fail_closed=True,
    ))
    result = cb(tool_name="terminal")
    assert result["action"] == "block"
    assert "infrastructure" in result["message"] and str(path) in result["message"]
    assert "not a policy verdict" in result["message"]
    assert cb(tool_name="terminal")["action"] == "block"
    assert pages == [str(path)]


def test_untracked_hook_in_checkout_stays_closed(home, monkeypatch):
    subprocess.run(["git", "init", "-q", str(home)], check=True, stdin=subprocess.DEVNULL)
    (home / "README").write_text("fixture")
    subprocess.run(["git", "-C", str(home), "add", "README"], check=True, stdin=subprocess.DEVNULL)
    subprocess.run(["git", "-C", str(home), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "fixture"], check=True, stdin=subprocess.DEVNULL)
    path = home / "hooks" / "not-in-head.py"
    pages = []
    monkeypatch.setattr(shell_hooks, "_page_missing_hook", lambda p, *args: pages.append(p) or True)
    result = shell_hooks._make_callback(shell_hooks.ShellHookSpec(
        event="pre_tool_call", command=f"{sys.executable} {path}", fail_closed=True,
    ))(tool_name="terminal")
    assert result["action"] == "block"
    assert f"owning checkout {home}" in result["message"]
    assert not path.exists() and pages == [str(path)]


def test_alert_uses_shared_root_for_profile_and_never_live_home(home, monkeypatch):
    profile = home / "profiles" / "daedalus"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    notify = home / "scripts" / "notify"
    notify.parent.mkdir()
    output = home / "notice.txt"
    notify.write_text(f'#!/bin/sh\nprintf "%s" "$*" > "{output}"\n')
    notify.chmod(0o755)
    assert shell_hooks._page_missing_hook(str(home / "hooks" / "missing.py"))
    assert "--severity high" in output.read_text()
    assert "infrastructure failure" in output.read_text()


def test_config_knob_fail_closed(home, monkeypatch):
    path = home / "hooks" / "missing.py"
    (home / "config.yaml").write_text("hooks:\n  missing_hook_policy: fail_closed\n  pre_tool_call:\n    - command: " + f"'{sys.executable} {path}'\n" + "      fail_closed: true\n")
    from hermes_cli.config import load_config
    spec, = shell_hooks.iter_configured_hooks(load_config())
    pages = []
    monkeypatch.setattr(shell_hooks, "_page_missing_hook", lambda p, *args: pages.append(p) or True)
    result = shell_hooks._make_callback(spec)(tool_name="terminal")
    assert result["action"] == "block"
    assert pages == [str(path)]


@pytest.mark.parametrize("damage,profile", [
    ("entry", False), ("directory", False), ("directory", True),
    ("sparse", False), ("dependency", False),
])
def test_tracked_hook_closure_recovers_and_pages(home, monkeypatch, damage, profile):
    """Real subprocess imports its sibling after HEAD restoration, even under sparse checkout."""
    hooks = home / "hooks"
    hooks.mkdir()
    path = hooks / "policy.py"
    path.write_text('from sibling import decision\nprint(decision())\n')
    (hooks / "sibling.py").write_text('def decision():\n    return \'{ "action": "allow" }\'\n')
    subprocess.run(["git", "init", "-q", str(home)], check=True)
    subprocess.run(["git", "-C", str(home), "add", "hooks"], check=True)
    subprocess.run(["git", "-C", str(home), "-c", "user.name=Test", "-c", "user.email=test@example.com",
                    "commit", "-qm", "fixture"], check=True)
    if damage == "entry":
        path.unlink()
    elif damage == "dependency":
        (hooks / "sibling.py").unlink()
    elif damage == "directory":
        shutil.rmtree(hooks)
    else:
        subprocess.run(["git", "-C", str(home), "sparse-checkout", "init", "--cone"], check=True)
        subprocess.run(["git", "-C", str(home), "sparse-checkout", "set", "scripts"], check=True)
        assert not path.exists()
    if profile:
        profile_dir = home / "profiles" / "daedalus"
        profile_dir.mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(profile_dir))
    pages = []
    monkeypatch.setattr(shell_hooks, "_page_missing_hook", lambda p, *a: pages.append((p, a)) or True)
    assert shell_hooks._make_callback(_spec(path, "restore_then_fail_closed"))(tool_name="terminal", args={"command": "pwd"}) is None
    assert path.exists() and (hooks / "sibling.py").exists()
    assert len(pages) == 1 and pages[0][0] == str(path)
    assert "restored" in str(pages[0][1])
    if damage == "sparse":
        flags = subprocess.check_output(["git", "-C", str(home), "ls-files", "-v", "--", "hooks"])
        assert all(line.startswith(b"H ") for line in flags.splitlines())


def test_unrecoverable_hook_page_describes_failed_restore(home, monkeypatch):
    path = home / "hooks" / "absent.py"
    pages = []
    monkeypatch.setattr(shell_hooks, "_page_missing_hook", lambda p, *a: pages.append((p, a)) or True)
    result = shell_hooks._make_callback(_spec(path, "restore_then_fail_closed"))(tool_name="terminal")
    assert result["action"] == "block" and "infrastructure" in result["message"]
    assert len(pages) == 1 and "failed" in str(pages[0][1])


def test_restore_resolution_never_consults_hermes_home(tmp_path, monkeypatch):
    """Owning checkout comes from the hook path alone (profile homes differ from the checkout root)."""
    root = tmp_path / "checkout"
    hooks = root / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "policy.py").write_text("print('{}')\n")
    (hooks / "sibling.py").write_text("X = 1\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "hooks"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.com",
                    "commit", "-qm", "fixture"], check=True)
    shutil.rmtree(hooks)

    def forbidden():
        raise AssertionError("restore path consulted the profile home")

    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(shell_hooks, "get_hermes_home", forbidden)
    restored, outcome = shell_hooks._restore_missing_hook(hooks / "policy.py")
    assert restored, outcome
    assert (hooks / "policy.py").is_file() and (hooks / "sibling.py").is_file()
