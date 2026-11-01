"""`kanban create` runs hermes-home's ``access-bound-pool-pin`` check on the REAL body (t_998bce52).

The pre_tool_call hook decides from the command text, so a ``--body-file`` that the same shell
builds with ``sed`` is unreadable there. The CLI reads the final body and asks the fleet root's
hook module (``pool_hosts`` + ``verdict``), so the refusal is the hook's own text.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

# Stand-in for hermes-home hooks/access_bound_create_policy.py: same two entry points.
HOOK = '''
import json, re
RULE = "access-bound-pool-pin"
def pool_hosts(root):
    try:
        return set(json.loads((root / "fleet" / "kanban-pool.json").read_text())["priority"])
    except (OSError, ValueError, KeyError):
        return set()
def verdict(body, hosts):
    pin = re.search(r"^host:[ \\t]*(\\S+)[ \\t]*$", body, re.M)
    if pin and pin.group(1) in hosts and re.search(r"\\bsudo\\b", body):
        return f"BLOCKED by {RULE}: this card pins `host: {pin.group(1)}` (sudo)"
    return None
'''


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "fleet").mkdir(parents=True)
    (home / "hooks").mkdir()
    (home / "fleet" / "kanban-pool.json").write_text(json.dumps({"priority": ["ci-box"]}), encoding="utf-8")
    (home / "hooks" / "access_bound_create_policy.py").write_text(HOOK, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _sed_body(tmp_path: Path, host: str) -> Path:
    """Build the body file the way the operator calls did: template + ``sed`` in one shell."""
    out = tmp_path / f"body-{host}.md"
    subprocess.run(
        ["bash", "-c", 'printf "host: HOSTX\\nssh ci sudo setpci -s 00:01.0\\n" > "$1.tmpl" '
                       '&& sed "s/HOSTX/$2/" "$1.tmpl" > "$1"', "_", str(out), host],
        check=True,
    )
    return out


def _create(body_file: Path) -> int:
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    args = root.parse_args(["kanban", "create", "probe", "--body-file", str(body_file), "--unhomed"])
    return kc._cmd_create(args)


def _count() -> int:
    with kbc.connect_closing() as conn:
        return len(kb.list_tasks(conn))


def test_sed_built_pool_pinned_body_is_refused_with_the_hooks_text(fleet, tmp_path, capsys):
    assert _create(_sed_body(tmp_path, "ci-box")) == 2
    assert "BLOCKED by access-bound-pool-pin: this card pins `host: ci-box`" in capsys.readouterr().err
    assert _count() == 0
    # Same body pinned to the Studio is not a pool pin: it is created.
    assert _create(_sed_body(tmp_path, "studio")) == 0
    assert _count() == 1


def test_off_marker_and_absent_hook_fail_open(fleet, tmp_path, capsys):
    pinned = _sed_body(tmp_path, "ci-box")
    (fleet / "state").mkdir()
    (fleet / "state" / "access-bound-create-policy-off").touch()
    assert _create(pinned) == 0
    (fleet / "state" / "access-bound-create-policy-off").unlink()
    (fleet / "hooks" / "access_bound_create_policy.py").unlink()
    assert _create(pinned) == 0
    assert _count() == 2
