"""t_5ae81e6e: a script a python heredoc body runs through a process call is walked like the same
command typed at the shell.

At 01b0b9b6 (hermes-agent#530) `subprocess.run(['bash', p])` in a body never had p read: the body
text puts p inside a list literal, never at command position, so an 8 KB p, a 1.2 MB p and a p with
a gateway lifecycle command all returned (False, None), while `bash p` typed at the shell is
scanned. A constant argv (and the constant string of os.system / shell=True) is now walked as that
typed command. A non-constant argv stays as before: in a 48 h replay of the default gateway's
terminal calls, refusing a shell started with one would have blocked 11 calls, all test rigs
running `bash -c <cmd from data>`.
"""

from pathlib import Path

import pytest

from cron.lifecycle_guard import _MAX_REFERENCED_SCRIPT_BYTES, scan_gateway_lifecycle

LIFECYCLE = "hermes gateway restart\n"


def _script(path: Path, size: int, tail: str = "") -> Path:
    filler = "echo hi\n" * ((size - len(tail)) // 8)
    path.write_text(filler + tail)
    path.chmod(0o644)
    return path


def _body(src: str, imports: str = "subprocess") -> str:
    return f"python3 - <<'EOF'\nimport {imports}\n{src}\nEOF"


@pytest.mark.parametrize("call", ["subprocess.run(['bash', {p!r}])", "subprocess.check_call(('sh', {p!r}))",
                                  "subprocess.Popen(['env', 'bash', {p!r}])", "subprocess.run([{p!r}])"])
def test_8kb_script_with_a_lifecycle_command_is_refused(tmp_path, call):
    script = _script(tmp_path / "s.sh", 8 * 1024, LIFECYCLE)
    script.chmod(0o755)  # `subprocess.run([p])` runs it directly, so it carries the x bit
    assert scan_gateway_lifecycle(_body(call.format(p=str(script))), cwd=str(tmp_path))[0]
    # and the same script typed at the shell agrees
    assert scan_gateway_lifecycle(f"bash {script}", cwd=str(tmp_path))[0]


def test_8kb_clean_script_runs(tmp_path):
    script = _script(tmp_path / "s.sh", 8 * 1024)
    assert scan_gateway_lifecycle(_body(f"subprocess.run(['bash', {str(script)!r}])"), cwd=str(tmp_path)) == (
        False, None)


def test_1_2mb_script_is_refused_on_the_scan_cap_like_bash(tmp_path):
    script = _script(tmp_path / "b.sh", 1_200_000)
    assert script.stat().st_size > _MAX_REFERENCED_SCRIPT_BYTES
    unsafe, refusal = scan_gateway_lifecycle(_body(f"subprocess.run(['bash', {str(script)!r}])"), cwd=str(tmp_path))
    assert unsafe and str(script) in refusal and "scan cap" in refusal
    assert scan_gateway_lifecycle(f"bash {script}", cwd=str(tmp_path)) == (True, refusal)


def test_small_lifecycle_script_in_a_body_that_is_not_mask_safe(tmp_path):
    script = _script(tmp_path / "l.sh", 64, LIFECYCLE)
    body = _body(f"print(os.getcwd())\nsubprocess.run(['bash', {str(script)!r}], check=True)", "os, subprocess")
    assert scan_gateway_lifecycle(body, cwd=str(tmp_path))[0]


def test_shell_strings_and_bash_c(tmp_path):
    script = _script(tmp_path / "l.sh", 64, LIFECYCLE)
    for src, imports in (
        (f"os.system('bash {script}')", "os"),
        (f"subprocess.run('bash {script}', shell=True)", "subprocess"),
        (f"subprocess.run(['bash', '-c', 'bash {script}'])", "subprocess"),
    ):
        assert scan_gateway_lifecycle(_body(src, imports), cwd=str(tmp_path))[0], src


def test_relative_operand_resolves_in_the_cd_dir_and_the_cwd_keyword(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    _script(work / "l.sh", 64, LIFECYCLE)
    elsewhere = str(tmp_path / "nowhere")
    assert scan_gateway_lifecycle(f"cd {work} && " + _body("subprocess.run(['bash', './l.sh'])"), cwd=elsewhere)[0]
    assert scan_gateway_lifecycle(_body(f"subprocess.run(['bash', 'l.sh'], cwd={str(work)!r})"), cwd=elsewhere)[0]


def test_non_constant_argv_stays_as_before(tmp_path):
    # `python3 x.py` typed at the shell is not walked either; `bash -c <cmd>` built at runtime
    # is how test rigs run their jobs
    for src in ("cmd = 'true'\nsubprocess.run(['bash', '-e', '-c', cmd])",
                "m = 'x.py'\nsubprocess.run(['python3', m, 'cmd'])", "r = '.'\nsubprocess.run(['git', '-C', r, 'status'])"):
        assert scan_gateway_lifecycle(_body(src), cwd=str(tmp_path)) == (False, None), src


def test_shell_true_list_runs_only_its_first_element(tmp_path):
    # Prism 1fb6e76054b8: `run(['sh', './big.sh'], shell=True)` is `sh -c sh ./big.sh`; big.sh is $0
    _script(tmp_path / "big.sh", 1_200_000)
    src = "subprocess.run(['sh', './big.sh'], shell=True, stdin=subprocess.DEVNULL)"
    assert scan_gateway_lifecycle(_body(src), cwd=str(tmp_path)) == (False, None)
    # the first element is still shell text
    assert scan_gateway_lifecycle(_body("subprocess.run(['bash ./big.sh'], shell=True)"), cwd=str(tmp_path))[0]


def test_an_inert_mention_in_a_child_does_not_hide_the_outer_executed_script(tmp_path):
    # Prism 2c9839444bc2: the child only prints ./big.sh; the outer shell runs it
    big = _script(tmp_path / "big.sh", 1_200_000)
    big.chmod(0o755)
    command = ("python3 - <<'PY'; bash ./big.sh\nimport os\n"
               "os.system(\"cat <<'DATA'\\n./big.sh\\nDATA\\n\")\nPY")
    unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe and "scan cap" in refusal


@pytest.mark.parametrize("command", [
    "python3 - <<-'PY'\n\timport subprocess\n\tsubprocess.run(['bash', './big.sh'])\n\tPY",
    "python3 - <<'PY END'\nimport subprocess\nsubprocess.run(['bash', './big.sh'])\nPY END",
])
def test_body_is_parsed_as_python_receives_it(tmp_path, command):
    # Prism bb95e5a5c478: `<<-` strips leading tabs, and the closing delimiter is not python
    _script(tmp_path / "big.sh", 1_200_000)
    unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe and "scan cap" in refusal
