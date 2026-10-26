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

@pytest.mark.parametrize("command", [  # kanban-identity: ok — strings fed to the guard scanner, never spawned
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


# --- Apollo r1 (t_4fded0bd): the write-then-run class the first masker opened, pinned ---------
# base BLOCKED `printf '<stop>' > x.sh; bash x.sh`; the first quoted-argument masker masked the
# printf operand and ALLOWED it. A quoted value that can reach a file, a variable, or a
# program-running option is never data.

@pytest.mark.parametrize("command", [
    f"printf '{BOOTOUT}' > /tmp/zz_t4f.sh; bash /tmp/zz_t4f.sh",
    f"echo '{BOOTOUT}' > /tmp/zz_t4f.sh && sh /tmp/zz_t4f.sh",
    f"echo '{BOOTOUT}' | tee /tmp/zz_t4f.sh; bash /tmp/zz_t4f.sh",
    f"C='{RESTART}'; $C",
    f'cmd="{RESTART}"; eval "$cmd"',
    f"printf -v C '{BOOTOUT}'; $C",
    f"vim '+!{RESTART}' +qa! /dev/null",
    f"git -c alias.x='!{RESTART}' x",
    f"tar --to-command='{RESTART}' -xf a.tar",
    # a python heredoc that WRITES the stop into a file the shell then runs
    f"python3 - <<'PY'\nopen('/tmp/zz_t4f.sh', 'w').write('{RESTART}')\nPY\nbash /tmp/zz_t4f.sh",
    f"python3 - <<'PY'\nwith open('/tmp/zz_t4f.sh', 'w') as f:\n    f.write('{BOOTOUT}')\nPY\nbash /tmp/zz_t4f.sh",
])
def test_quoted_value_reaching_a_file_or_variable_still_blocks(command):
    assert _blocked(command)


# --- Apollo r1 (t_4fded0bd): a quoted JSON value / stderr redirect is still a content argument ---

@pytest.mark.parametrize("command", [
    # a --coverage JSON value, then a second quoted value: the `[`/`,` inside the JSON are quoted,
    # not a Python argv list, so the review text after it is still data
    'hermes kanban request-changes t_x --coverage \'{"items": ["no ' + RESTART + '"]}\' "also ' + RESTART + ' here"',
    'hermes kanban request-changes t_x --coverage "{\\"items\\": [\\"no ' + RESTART + '\\"]}"',
    # redirecting stderr/stdout to /dev/null is not writing a file
    f'hermes kanban comment t_x --body "see: {RESTART}" >/dev/null 2>&1',
    f'hermes kanban comment t_x --body "see: {RESTART}" 2>/dev/null',
    f"echo 'a' && hermes kanban create 'x' --body 'never {BOOTOUT}' 2>&1 | tail -1",
    # fp5 (hit while filing the review): a python heredoc BUILDING JSON and writing it to a
    # non-executed file
    f"python3 - <<'PY'\nimport json\nd = {{'items': ['never {BOOTOUT}', 'no {RESTART}']}}\nopen('/tmp/x.json', 'w').write(json.dumps(d))\nPY",
    f"python3 - <<'PY'\nimport json\nd = {{'items': ['never {BOOTOUT}']}}\nwith open('/tmp/x.json', 'w') as f:\n    json.dump(d, f)\nPY",
])
def test_quoted_json_values_and_null_redirects_pass(command):
    assert not _blocked(command)


# --- Apollo r5 (t_4fded0bd): a quoted ESCAPE operand of a data sink is an executor, never data ---
# sqlite3 is not a re-parser, so the quoted-argument view masked `".shell <verb>"` as a content
# argument on 3e1ce485; sqlite3 dot-commands and psql `\!` EXECUTE their argument. The data-sink
# masker has always refused them (`_DOT_COMMAND_ARGUMENT`); this view refuses them too.

@pytest.mark.parametrize("command", [
    f'sqlite3 db ".shell {RESTART}"',
    f'sqlite3 ./db ".shell {RESTART}"',
    f'sqlite3 ./db ".system {SYSTEMCTL}"',
    f"sqlite3 ./db '.system {SYSTEMCTL}'",
    f'psql ./x -c "\\\\! {SYSTEMCTL}"',
])
def test_quoted_escape_operand_of_a_data_sink_blocks(command):
    assert _blocked(command)


@pytest.mark.parametrize("command", [
    # the same sink with a plain SQL/data operand stays a content argument
    f'sqlite3 ./db "SELECT msg FROM log WHERE msg LIKE \'%{RESTART}%\'"',
    f"sqlite3 ./stats.db 'SELECT 1 -- never {SYSTEMCTL}'",
    f"grep -r '{SYSTEMCTL}' .",
])
def test_quoted_sql_operand_of_a_data_sink_passes(command):
    assert not _blocked(command)


def test_referenced_script_whose_python_heredoc_prints_json_passes(tmp_path):
    """fp4 (hit while filing the review): a .sh that only prints JSON quoting the incident."""
    script = tmp_path / "sendback.sh"
    script.write_text(
        f"#!/usr/bin/env bash\npython3 - <<'PY'\nimport json\nprint(json.dumps({{'items': ['never {BOOTOUT}', 'no {RESTART}']}}))\nPY\n",
        encoding="utf-8",
    )
    assert not _blocked(f"bash {script}")
    real = tmp_path / "real.sh"
    real.write_text(f"#!/usr/bin/env bash\n{BOOTOUT}\n", encoding="utf-8")
    assert _blocked(f"bash {real}")


# --- Apollo r2 (t_4fded0bd): the lifecycle VERB written to a file and run in the same command ----
# `printf '<restart>' > x.sh && bash x.sh` PASSED on 85b61694 (and on base): printf is a text
# consumer, so Branch A's quoted-data exemption fired; a `cat > x.sh <<'EOF'` body was stripped as
# an inert heredoc. When the command WRITES and a later segment RUNS, no data exemption applies.

@pytest.mark.parametrize("command", [
    f"printf '{RESTART}' > /tmp/zz_t4f.sh && bash /tmp/zz_t4f.sh",
    f"printf '{RESTART}' > /tmp/zz_t4f.sh\nbash /tmp/zz_t4f.sh",
    f"echo '{RESTART}' 1> /tmp/zz_t4f.sh; sh /tmp/zz_t4f.sh",
    f"echo '{RESTART}' &> /tmp/zz_t4f.sh; sh /tmp/zz_t4f.sh",
    f"echo '{RESTART}' >> /tmp/zz_t4f.sh; zsh /tmp/zz_t4f.sh",
    f"printf '{RESTART}' > /tmp/zz_t4f.sh && chmod +x /tmp/zz_t4f.sh && /tmp/zz_t4f.sh",
    f"printf '{RESTART}' > /tmp/zz_t4f.sh && source /tmp/zz_t4f.sh",
    f"printf '{RESTART}' > /tmp/zz_t4f.sh && . /tmp/zz_t4f.sh",
    f"printf '{RESTART}' > /tmp/zz_t4f.sh && sudo bash /tmp/zz_t4f.sh",
    f"printf '{RESTART}' > /tmp/zz_t4f.py && python3 /tmp/zz_t4f.py",
    f"printf '{RESTART}' | tee /tmp/zz_t4f.sh; bash /tmp/zz_t4f.sh",
    f"cat > /tmp/zz_t4f.sh <<'EOF'\n{RESTART}\nEOF\nbash /tmp/zz_t4f.sh",
    f"cat <<'EOF' > /tmp/zz_t4f.sh\n{RESTART}\nEOF\nbash /tmp/zz_t4f.sh",
    f"tee /tmp/zz_t4f.sh <<'EOF'\n{RESTART}\nEOF\nsh /tmp/zz_t4f.sh",
    f"cat > /tmp/zz_t4f.sh <<'EOF'\n{BOOTOUT}\nEOF\nbash /tmp/zz_t4f.sh",
    f"git commit -m '{RESTART}' > /tmp/zz_t4f.sh; bash /tmp/zz_t4f.sh",
    f"read -r C <<< '{RESTART}'; $C",
])
def test_lifecycle_verb_written_then_run_blocks(command):
    assert _blocked(command)


@pytest.mark.parametrize("command", [
    # written, then only READ / linted / left alone
    f"echo '{RESTART}' >> ~/.zshrc",
    f"echo '{RESTART}' > /tmp/notes.md; cat /tmp/notes.md",
    f"echo '{RESTART}' > /tmp/zz_t4f.sh; echo done",
    f"echo '{RESTART}' > /tmp/zz_t4f.sh; bash -n /tmp/zz_t4f.sh",
    f"echo '{RESTART}' > /tmp/zz_t4f.sh; shellcheck /tmp/zz_t4f.sh",
    f"git commit -m 'run {RESTART} later' > /tmp/log.txt",
    f"cat > /tmp/notes.md <<'EOF'\nnever {RESTART}\nEOF\ngit add /tmp/notes.md",
    f"printf '{RESTART}' > /tmp/x; wc -l /tmp/x && grep -c gateway /tmp/x",
])
def test_lifecycle_verb_written_then_only_read_passes(command):
    assert not _blocked(command)


# --- verb-family ownership: these shapes PASS here BY DESIGN ------------------------------------
# This guard owns lifecycle verbs aimed at OUR OWN gateway from inside it; a SIBLING label or a
# remote host is fleet maintenance and exempt. The raw stop of ANY gateway label/unit is blocked
# by the hermes-home hook hooks/gateway_stop_policy.py (gateway-stop-guard, t_c892ebee), which
# runs on every worker tool call. Apollo probed these six as misses on r1; they are the hand-off.

SIBLING_LABEL = "ai.hermes." + "gateway-apollo"
SIBLING_UNIT = "hermes-" + "gateway-clanker"
SIBLING_BOOTOUT = "launch" + "ctl boot" + "out gui/502/" + SIBLING_LABEL
SIBLING_DISABLE = "systemctl --user disable --now " + SIBLING_UNIT


@pytest.mark.parametrize("command", [
    SIBLING_BOOTOUT,
    f'bash -c "{SIBLING_DISABLE}"',
    f"bash <<EOF\n{SIBLING_DISABLE}\nEOF",
    f"python3 - <<'PY'\nimport subprocess\nsubprocess.run('{SIBLING_DISABLE}', shell=True)\nPY",
    f"ssh ace-ai 'systemctl --user restart {SIBLING_UNIT}'",
    f"sudo -s '{SIBLING_BOOTOUT}'",
])
def test_sibling_gateway_stops_are_the_stop_guards_job_not_this_ones(command):
    # EXPECTED verdict of THIS guard: allowed (sibling exemption). gateway_stop_policy blocks them.
    assert not _blocked(command)


@pytest.mark.parametrize("command", [
    f"sudo -s '{BOOTOUT}'",
    f'bash -c "{SYSTEMCTL}"',
    f"bash <<EOF\n{SYSTEMCTL}\nEOF",
])
def test_same_shapes_against_our_own_gateway_block(command):
    assert _blocked(command)


# Same verdict on base (f9e98c15) and on this PR: this guard's systemd verb set is stop|kill|restart
# of OUR unit; `disable --now` / `mask` of a gateway unit are the hermes-home stop guard's family
# (hooks/gateway_stop_policy.py, #3822 adds them there). Pinned so the next probe reads them as the
# hand-off, not as a regression of this PR.
@pytest.mark.parametrize("command", [
    f'bash -c "systemctl --user disable --now {UNIT}"',
    f"systemctl --user mask {UNIT}",
])
def test_systemd_disable_and_mask_are_the_stop_guards_family(command):
    assert not _blocked(command)


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
