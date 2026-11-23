"""t_94a8d819: a python heredoc body that is not provably read-only, naming a file too large to scan,
fails closed like `bash <file>` does, instead of running unscanned.

Live probes (runtime ffcfb208ff0b, 11-22 23:2x PT), same body each time:
`import json, os` / `json.load(open(P))` / print. A 147,700-byte one-line JSON was refused on the
64 KiB longest-line cap; a 2,508,900-byte one RAN, because `_references_at` dropped an oversized
file without the x bit as "not a script" (#36a78f6a), a shell-only fact. Main before #529
(e03d0a7a) behaved the same. `bash <1.2 MB file>` was refused on the scan cap.
"""

from pathlib import Path

from cron.lifecycle_guard import _MAX_REFERENCED_SCRIPT_BYTES, scan_gateway_lifecycle

SMALL = 147_700
BIG = 2_508_900


def _json_line(path: Path, size: int) -> Path:
    path.write_text('{"k": "' + "a" * (size - 9) + '"}')
    path.chmod(0o644)
    assert path.stat().st_size == size
    return path


def _heredoc(path: Path, imports: str = "json, os", extra: str = "") -> str:
    return (
        "python3 - <<'EOF'\n"
        f"import {imports}\n"
        f"d = json.load(open('{path}'))\n"
        f"{extra}"
        "print(len(d))\n"
        "EOF"
    )


def test_probe1_147kb_line_in_non_read_only_body_is_refused_on_line_cap(tmp_path):
    data = _json_line(tmp_path / "small.json", SMALL)
    unsafe, refusal = scan_gateway_lifecycle(_heredoc(data), cwd=str(tmp_path))
    assert unsafe
    assert str(data) in refusal and "longest-line cap" in refusal


def test_probe2_2_5mb_file_in_non_read_only_body_is_refused_on_scan_cap(tmp_path):
    data = _json_line(tmp_path / "big.json", BIG)
    unsafe, refusal = scan_gateway_lifecycle(_heredoc(data), cwd=str(tmp_path))
    assert unsafe
    assert f"`{data}` is larger than the scan cap ({_MAX_REFERENCED_SCRIPT_BYTES} bytes)" in refusal
    # python-body path: the refusal also says how to read it as data
    assert "json.load(open(p))" in refusal


def test_probe3_bash_oversized_file_is_refused_on_scan_cap(tmp_path):
    script = tmp_path / "big.sh"
    script.write_text("echo hi\n" * (1_200_000 // 8))
    script.chmod(0o644)
    unsafe, refusal = scan_gateway_lifecycle(f"bash {script}", cwd=str(tmp_path))
    assert unsafe
    assert f"`{script}` is larger than the scan cap ({_MAX_REFERENCED_SCRIPT_BYTES} bytes)" in refusal
    # the shell runs it: no read-as-data advice
    assert "json.load(open(p))" not in refusal


def test_exec_of_an_oversized_non_executable_file_fails_closed(tmp_path):
    """The bypass the size drop opened: python runs the file whatever its mode."""
    script = tmp_path / "big.py"
    script.write_text("x = 1\n" * (_MAX_REFERENCED_SCRIPT_BYTES // 6 + 10))
    script.chmod(0o644)
    command = f"python3 - <<'EOF'\nimport os\nexec(open('{script}').read())\nEOF"
    unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe
    assert "larger than the scan cap" in refusal


def test_read_only_body_still_passes_at_any_size(tmp_path):
    """#529's json/re/open/str allowance: the path is masked away, so size never matters."""
    for name, size in (("small.json", SMALL), ("big.json", BIG)):
        data = _json_line(tmp_path / name, size)
        assert scan_gateway_lifecycle(_heredoc(data, imports="json"), cwd=str(tmp_path)) == (False, None)


def test_mask_safe_subprocess_body_agrees_across_sizes(tmp_path):
    """`import json, subprocess` + `subprocess.run(['true'])` is inside the pre-existing mask
    allowlist (constant argv), so both sizes read as data and both run: the verdicts agree."""
    extra = "subprocess.run(['true'])\n"
    verdicts = {
        size: scan_gateway_lifecycle(
            _heredoc(_json_line(tmp_path / f"{size}.json", size), "json, subprocess", extra),
            cwd=str(tmp_path),
        )
        for size in (SMALL, BIG)
    }
    assert verdicts[SMALL] == verdicts[BIG] == (False, None)


def test_sqlite_connect_literal_used_elsewhere_is_not_carved_out(tmp_path):
    """#36a78f6a's sqlite3.connect allowance covers a literal used ONLY as the database."""
    data = tmp_path / "kanban.db"
    data.write_bytes(b"A" * (_MAX_REFERENCED_SCRIPT_BYTES + 1))
    data.chmod(0o644)
    only_db = (
        "python3 - <<'PY'\nimport sqlite3, os\n"
        f"c = sqlite3.connect(os.path.expanduser('{data}'), timeout=60)\nc.close()\nPY"
    )
    assert scan_gateway_lifecycle(only_db, cwd=str(tmp_path)) == (False, None)
    also_exec = (
        "python3 - <<'PY'\nimport sqlite3, os\n"
        f"c = sqlite3.connect('{data}')\nexec(open('{data}').read())\nPY"
    )
    assert scan_gateway_lifecycle(also_exec, cwd=str(tmp_path))[0]


def test_stat_exemption_does_not_cover_an_aliased_read_of_the_same_file(tmp_path):
    """Prism ecbd1c8f1080: the stat-only exemption compared literal spellings, so `os.stat` on the
    absolute path plus `open()` on another spelling of the same file skipped the scan-cap refusal.
    Uses are compared by the file they name, not by how it is spelled."""
    data = _json_line(tmp_path / "big.json", BIG)
    (tmp_path / "sub").mkdir()
    (tmp_path / "link.json").symlink_to(data)
    for spelling in ("./big.json", "sub/../big.json", str(tmp_path / "link.json")):
        command = (
            "python3 - <<'EOF'\nimport json, os\n"
            f"os.stat('{data}')\n"
            f"d = json.load(open('{spelling}'))\nprint(len(d))\nEOF"
        )
        unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path))
        assert unsafe, spelling
        assert "larger than the scan cap" in refusal, spelling


def test_cd_before_the_heredoc_sets_the_directory_the_body_reads_from(tmp_path):
    """Prism ecbd1c8f1080 (round 2): the oversized pass resolved the body's relative paths against
    the scan's starting cwd, not the directory a preceding `cd` moved to, so both the candidate and
    the stat-only exemption looked in the wrong place and `./big.json` ran unscanned."""
    start, work = tmp_path / "start", tmp_path / "work"
    start.mkdir()
    work.mkdir()
    data = _json_line(work / "big.json", BIG)
    for stat in ("", f"os.stat('{data}')\n"):
        for tail in ("", "\ncd /"):
            command = (
                f"cd {work} && python3 - <<'EOF'\nimport json, os\n{stat}"
                "d = json.load(open('./big.json'))\nprint(len(d))\nEOF" + tail
            )
            unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(start))
            assert unsafe, (stat, tail)
            assert f"`{data}` is larger than the scan cap" in refusal, (stat, tail)
