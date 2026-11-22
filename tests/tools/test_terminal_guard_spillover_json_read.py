"""Python heredocs that only READ a spillover JSON file pass the lifecycle guard (t_bb509376).

22 commands were refused in 24 h (to 11-22 12:20 PT) with "scan budget exhausted (text at depth
1)". Each named a tool-result spillover file (100-314 KB of JSON on ONE line) inside a python
heredoc. 18 of the 22 bodies only did ``json.load(open(p))`` plus ``re`` searches, but
``json.load`` and ``re`` were not on the read-only mask allowlist, so the file was walked as a
script and hit the 64 KiB longest-line cap. The body shapes below are the real ones, the paths
are not. A refusal that remains names the file, the cap it hit, and the shapes that pass.

Lifecycle literals are assembled at runtime so writing this file through an agent tool does not
trip the gates it tests.
"""

from __future__ import annotations

import json
import time

import pytest

from cron.lifecycle_guard import scan_gateway_lifecycle_detail
from tools.terminal_tool_guards import gateway_lifecycle_block

RESTART = "hermes gateway " + "re" + "start"


@pytest.fixture(autouse=True)
def _supervised(monkeypatch):
    from tools import process_registry

    monkeypatch.setattr(process_registry, "_is_supervised_gateway_process", lambda: True)


@pytest.fixture
def spill(tmp_path):
    """A spillover file shaped like the real ones: ~100 KB of JSON on one line."""
    path = tmp_path / "cache" / "spillover" / "toolu_test.txt"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"content": "x" * 100_000, "linked_files": None}), encoding="utf-8")
    return path


class _Env:
    def __init__(self, cwd):
        self.cwd = cwd
        self.env = {}

    def execute(self, command, **_kwargs):  # pragma: no cover - local reads never shell out
        raise AssertionError(f"local scan shelled out: {command}")


def _block(command, cwd):
    return gateway_lifecycle_block(
        command=command, env=_Env(str(cwd)), env_type="local", cwd=str(cwd), workdir=None,
        session_key=f"spill-{time.monotonic_ns()}",
    )


def _scan(command, cwd):
    return scan_gateway_lifecycle_detail(command, cwd=str(cwd), read_remote_script_is_local=True)


# Real body shapes from the refused tool calls (session/tool ids in the PR body).
_JSON_ONLY = "python3 - <<'EOF'\nimport json\nd=json.load(open('{p}'))\nc=d['content']; print(len(c)); print(c[:9000])\nEOF"
_JSON_RE = (
    "cd {cwd} && python3 - <<'EOF'\nimport json,re\nd=json.load(open('{p}'))\nc=d['content']\n"
    "for m in re.finditer(r'mechanical_wake', c):\n    s=max(0,m.start()-300); e=min(len(c), m.end()+900)\n"
    "    print('=====', m.start()); print(c[s:e])\nEOF"
)
_JSON_RE_FLAG = (
    "python3 - <<'EOF'\nimport json\nd=json.load(open(\"{p}\"))\nc=d[\"content\"]\nimport re\n"
    "print(\"== HEADINGS\")\nfor m in re.finditer(r\"^#{{1,3}} .*$\", c, re.M):\n    print(m.group(0)[:200])\nEOF"
)
_GREP_THEN_JSON_RE = (
    "grep -n own_repo_review_human {p} | cut -c1-300 | head -20; python3 - <<'EOF'\nimport json,re\n"
    "d=json.load(open('{p}'))\nc=d['content']\nfor m in re.finditer(re.escape('own_'), c):\n"
    "    print(c[max(0,m.start()-200):m.start()+400])\nEOF"
)


@pytest.mark.parametrize("shape", [_JSON_ONLY, _JSON_RE, _JSON_RE_FLAG, _GREP_THEN_JSON_RE],
                         ids=["json-only", "json-re", "json-re-flag", "grep-then-json-re"])
def test_read_only_json_re_heredoc_on_spillover_is_allowed(tmp_path, spill, shape):
    command = shape.format(p=spill, cwd=tmp_path)
    assert _scan(command, tmp_path) == (False, None, None)
    assert _block(command, tmp_path) is None


def test_body_that_can_execute_still_scans_the_file_and_names_the_cap(tmp_path, spill):
    """`os` keeps the body unproven, so the file is still walked; the refusal now says which
    file, which cap, and the shapes that pass (the old text said only "text at depth 1")."""
    command = f"python3 - <<'EOF'\nimport json,os\nd=json.load(open('{spill}'))\nos.system('true')\nEOF"
    blocked = _block(command, tmp_path)
    assert blocked is not None
    error = json.loads(blocked)["error"]
    assert f"text at depth 1: `{spill}` is a " in error
    assert "-char line, over the 65536-char longest-line cap" in error
    assert "sys.argv[1]" in error and "python3 that.py" in error


@pytest.mark.parametrize("call", ["re.sub(r'x', print, c)", "re.compile('x').sub('', c)"])
def test_re_calls_outside_the_allowlist_keep_the_scan(tmp_path, spill, call):
    command = f"python3 - <<'EOF'\nimport json,re\nc=json.load(open('{spill}'))['content']\n{call}\nEOF"
    unsafe, refusal, _hit = _scan(command, tmp_path)
    assert unsafe is True and "longest-line cap" in (refusal or "")


def test_the_named_way_through_passes(tmp_path, spill):
    """Each read shape the refusal names is allowed for the same body that was refused."""
    script = tmp_path / "read_spill.py"
    script.write_text(f"import json,os\nd=json.load(open('{spill}'))\nprint(len(d['content']))\n")
    argv = f"python3 - \"{spill}\" <<'EOF'\nimport json,os,sys\nd=json.load(open(sys.argv[1]))\nEOF"
    for command in (argv, f"python3 {script}"):
        assert _scan(command, tmp_path) == (False, None, None), command


def test_json_re_body_with_a_lifecycle_argv_is_still_blocked(tmp_path, spill):
    """The mask keeps `subprocess.run([...])` argv visible: a json/re body cannot hide a restart."""
    argv = ", ".join(repr(word) for word in RESTART.split())
    command = (
        f"python3 - <<'EOF'\nimport json,re,subprocess\nd=json.load(open('{spill}'))\n"
        f"if re.search('x', d['content']):\n    subprocess.run([{argv}])\nEOF"
    )
    assert _scan(command, tmp_path)[0] is True


def test_json_load_object_hook_must_be_a_pure_builtin(tmp_path, spill):
    """`json.load` calls its hooks: a non-allowlisted callable there keeps the scan."""
    command = (
        f"python3 - <<'EOF'\nimport json\nd=json.load(open('{spill}'), object_hook=exit)\nEOF"
    )
    assert _scan(command, tmp_path)[0] is True


def test_lines_cap_refusal_names_lines(tmp_path):
    many = tmp_path / "many.json"
    many.write_text("\n".join(["1,"] * 20000), encoding="utf-8")
    command = f"python3 - <<'EOF'\nimport json\nprint(type(json.load(open('{many}'))))\nEOF"
    unsafe, refusal, _hit = _scan(command, tmp_path)
    assert unsafe is True
    assert f"`{many}` is 20000 lines, over the " in refusal and "16384-line walk cap" in refusal


@pytest.mark.parametrize("module_file", ["re.py", "json.py", "re/__init__.py"])
def test_workspace_module_shadowing_an_import_keeps_the_body_visible(tmp_path, module_file):
    """`python3 -` puts its cwd first on sys.path: a workspace `re.py` gets the call's arguments,
    so the lifecycle literal it receives must stay visible (Prism aede5e966585)."""
    shadow = tmp_path / module_file
    shadow.parent.mkdir(parents=True, exist_ok=True)
    shadow.write_text("def search(*a): pass\ndef loads(*a): pass\n", encoding="utf-8")
    name = module_file.split("/")[0].removesuffix(".py")
    call = "re.search" if name == "re" else "json.loads"
    command = f"python3 -S - <<'EOF'\nimport {name}\n{call}('{RESTART}', 'sample')\nEOF"
    assert _scan(command, tmp_path)[0] is True
    # Same body, no workspace module: the string is data and the body passes.
    clean = tmp_path / "clean"
    clean.mkdir()
    assert _scan(command, clean) == (False, None, None)


def test_cd_into_a_shadowing_dir_keeps_the_body_visible(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "re.py").write_text("def search(*a): pass\n", encoding="utf-8")
    body = f"python3 - <<'EOF'\nimport re\nre.search('{RESTART}', 'x')\nEOF"
    assert _scan(f"cd work && {body}", tmp_path)[0] is True
    assert _scan(f'cd "$W" && {body}', tmp_path)[0] is True  # unknown cwd: fail closed
    assert _scan(f"PYTHONPATH=work {body}", tmp_path)[0] is True


def test_an_executed_script_over_the_cap_gets_the_split_hint_not_the_read_hint(tmp_path):
    """`bash big.sh` runs the file: the read-as-data shapes do not apply (Prism 8e4887a66a56)."""
    big = tmp_path / "big.sh"
    big.write_text("echo " + "x" * 70_000 + "\n", encoding="utf-8")
    unsafe, refusal, _hit = _scan(f"bash {big}", tmp_path)
    assert unsafe is True and "longest-line cap" in refusal
    assert "split it into smaller files" in refusal
    assert "sys.argv[1]" not in refusal
