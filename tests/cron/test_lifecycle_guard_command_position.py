"""Lifecycle guard judges COMMAND POSITION, never content arguments (card t_4fded0bd).

Three false positives on 2026-10-11/12, one class: a guard string-matched the entire
tool-call text, so lifecycle words inside a `--body`/`--reason` value, a python
heredoc that PRINTED a slice of a spec, and a kanban card describing the incident
were refused. The real incident shapes (t_c892ebee bare bootout, `bash -c` restart,
a heredoc piped to bash) must still block, and the refusal must name the token.

Deny-shaped literals are assembled at runtime so reading or committing this file
through an agent tool does not trip the gates it tests.
"""

from __future__ import annotations

import json

import pytest

from cron.lifecycle_guard import (
    describe_gateway_lifecycle_command,
    scan_gateway_lifecycle,
    scan_gateway_lifecycle_detail,
)
from tools.terminal_tool_guards import gateway_lifecycle_block

LABEL = "ai.hermes." + "gateway-zeus"
UNIT = "hermes-" + "gateway-zeus"
BOOTOUT = "launch" + "ctl boot" + "out gui/502/" + LABEL
SYSTEMCTL = "systemctl --user " + "st" + "op " + UNIT
RESTART = "hermes gateway " + "re" + "start"


@pytest.fixture(autouse=True)
def _own_gateway(monkeypatch):
    # The lifecycle targets above must be OUR gateway, or the sibling exemption answers first.
    # Pin both the launchd label and the systemd unit (same shape as
    # test_lifecycle_guard_self_aware_targets.py), and drop the profile-derived names of
    # whichever profile the test process happens to run under.
    import cron.lifecycle_guard as lifecycle_guard

    monkeypatch.setenv("XPC_SERVICE_NAME", LABEL)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.setattr(lifecycle_guard, "_profile_derived_self_names", lambda: {LABEL, UNIT})


def _blocked(command: str) -> bool:
    unsafe, refusal = scan_gateway_lifecycle(command, cwd="/tmp")
    assert refusal is None, refusal
    return unsafe


# --- the three original false positives, plus the shapes the card lists as PASS ----------

@pytest.mark.parametrize("command", [
    # (3) the card about the false positive quoting the incident command in --body
    f"hermes kanban create 'guard false positive' --body 'the incident was {BOOTOUT}; keep it blocked'",
    f'hermes kanban create "guard false positive" --body "the incident was a bare {BOOTOUT}; keep it blocked"',
    f'hermes kanban request-changes t_x --reason "never run {SYSTEMCTL} by hand"',
    f'hermes kanban comment t_x --body "the fix is NOT {RESTART}; use the front door"',
    # (2) a python heredoc that only prints text naming a unit stop
    f"python3 - <<'PY'\nseg = 'run: {SYSTEMCTL}'\nprint(seg[0:80])\nPY",
    f"python3 - <<PY\nspec = 'docs say {BOOTOUT}'\nprint(spec)\nPY",
    # heredoc written to a file, never executed
    f"cat > /tmp/notes.md <<'EOF'\nnever run {BOOTOUT} by hand\nEOF",
    # --body-file: the file's content is not in the command at all
    "hermes kanban create 'x' --body-file /tmp/body.md",
    # text consumers
    f"echo '{BOOTOUT}'",
    f"grep -n '{SYSTEMCTL}' /var/log/syslog",
])
def test_lifecycle_words_in_content_arguments_pass(command):
    assert not _blocked(command)


def test_python_heredoc_reading_a_document_that_names_a_stop_passes(tmp_path):
    """(2) the document being READ names a stop; the body only slices and prints it."""
    spec = tmp_path / "spec.md"
    spec.write_text(f"## ops\\nrun `{SYSTEMCTL}` then `{BOOTOUT}`\\n", encoding="utf-8")
    for body in (
        f"spec = open('{spec}').read()\ni = spec.find('ops')\nseg = spec[i:i+400]\nprint(seg[0:600])\n",
        f"with open('{spec}') as f:\n    t = f.read()\nprint(t[0:200])\n",
        f"from pathlib import Path\nt = Path('{spec}').read_text()\nprint(t[5:900])\n",
    ):
        assert not _blocked(f"python3 - <<'PY'\n{body}PY"), body
    # the same document EXECUTED still blocks
    assert _blocked(f"python3 - <<'PY'\nexec(open('{spec}').read())\nPY")
    assert _blocked(f"python3 - <<'PY'\nimport os\nos.system('{spec}')\nPY")
    assert _blocked(f"bash {spec}")


# --- the real incident shapes stay blocked ---------------------------------------------

@pytest.mark.parametrize("command", [
    BOOTOUT,
    f"true && {BOOTOUT}",
    f"sudo {SYSTEMCTL}",
    RESTART,
    f'bash -c "{SYSTEMCTL}"',
    f"bash <<EOF\n{BOOTOUT}\nEOF",
    f"sh <<'EOF'\n{RESTART}\nEOF",
    f"L={LABEL}; launch" + "ctl boot" + "out gui/502/$L",
    f'ssh localhost "{BOOTOUT}"',
    f'xargs -I{{}} sh -c "{BOOTOUT}"',
    f"python3 - <<'PY'\nimport subprocess\nsubprocess.run(['systemctl', '--user', 'stop', '{UNIT}'])\nPY",
    # $( ) and backticks inside double quotes DO run (the 2026-10-04 --handoff incident class)
    f'hermes kanban create "x" --body "$({RESTART})"',
    f'hermes kanban create "x" --body "the incident was `{BOOTOUT}`"',
    # a quoted argument handed to a re-parser is code
    f"env -S '{RESTART}'",
    # quoted data that reaches an executor another way
    f"echo '{BOOTOUT}' | sh",
    f"echo '{BOOTOUT}' |& bash",
    f"echo '{BOOTOUT}' | tee >(bash)",
    f"echo '{BOOTOUT}' > >(bash)",
    f"bash <<< '{BOOTOUT}'",
    f"echo '{BOOTOUT}' | awk '{{system($0)}}'",
    # a comment's apostrophe must not open a span over the next line
    f"# don't\n{BOOTOUT}\necho 'x'",
    f"echo 'a' # it's\n{BOOTOUT}\necho 'b'",
])
def test_real_incident_shapes_still_block(command):
    assert _blocked(command)


# --- the refusal names the tripping token ------------------------------------------------

def test_describe_names_token_and_position():
    hit = describe_gateway_lifecycle_command(f"echo hi\n  {BOOTOUT}")
    assert hit is not None and "launch" + "ctl" in hit and "line 2" in hit and "column 3" in hit
    assert describe_gateway_lifecycle_command("echo hi") is None


def test_detail_reports_hit_for_blocked_and_none_for_allowed():
    unsafe, refusal, hit = scan_gateway_lifecycle_detail(f"true && {BOOTOUT}", cwd="/tmp")
    assert unsafe and refusal is None and hit and "boot" + "out" in hit
    unsafe, refusal, hit = scan_gateway_lifecycle_detail(f"echo '{BOOTOUT}'", cwd="/tmp")
    assert (unsafe, refusal, hit) == (False, None, None)


def test_terminal_refusal_names_the_tripping_token(tmp_path, monkeypatch):
    from tools import process_registry

    monkeypatch.setattr(process_registry, "_is_supervised_gateway_process", lambda: True)
    blocked = gateway_lifecycle_block(
        command=f"echo ok\n{SYSTEMCTL}", env=None, env_type="local", cwd=str(tmp_path),
        workdir=None, session_key="t_4fded0bd",
    )
    assert blocked is not None
    error = json.loads(blocked)["error"]
    assert "Tripped by" in error and "systemctl" in error and "line 2" in error
    allowed = gateway_lifecycle_block(
        command=f'hermes kanban create "x" --body "see {SYSTEMCTL}"', env=None, env_type="local",
        cwd=str(tmp_path), workdir=None, session_key="t_4fded0bd",
    )
    assert allowed is None
