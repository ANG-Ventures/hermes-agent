"""Regression: a NON-EXECUTABLE data file named at command position is not a script.

Apollo 2026-09-21: `bash /tmp/ac8-close.sh` was hard-blocked with
`blocked_by: gateway_lifecycle_guard` although the script contains no lifecycle
command at all. Its Python heredoc holds

    c = sqlite3.connect(os.path.expanduser('~/.hermes/kanban.db'), timeout=60)

`_iter_command_segments` splits on parentheses, so that line yields a segment
whose only token is `~/.hermes/kanban.db` at command position. The bare-token
"directly executed script" branch of `_references_at` yielded it, and
`_read_referenced_script` then saw a 31 MB SQLite file — no binary magic, no
shebang, over `_MAX_REFERENCED_SCRIPT_BYTES` — and fail-closed as an
"oversized script".

Same class as #77131 (bare `/` token) and pc-fb1bd018 (directory token): a path
token bash could not actually execute is fail-closed as an unscannable script.
A regular file without the executable bit cannot be run at command position
(`./file` → "Permission denied"), so it is not a script reference.

The fail-closed verdict must survive for the real case: an EXECUTABLE oversized
file, and every `bash <file>` / `source <file>` path (neither needs the x bit).
"""

from pathlib import Path

from cron.lifecycle_guard import (
    _MAX_REFERENCED_SCRIPT_BYTES,
    contains_gateway_lifecycle_command_or_referenced_script,
)


def _oversized(path: Path, mode: int) -> Path:
    path.write_bytes(b"A" * (_MAX_REFERENCED_SCRIPT_BYTES + 1))
    path.chmod(mode)
    return path


def test_oversized_non_executable_data_file_at_command_position_is_not_a_script(
    tmp_path,
):
    """The direct form: a script whose body just names a >1MiB 0644 data file."""
    data = _oversized(tmp_path / "data.db", 0o644)
    script = tmp_path / "close.sh"
    script.write_text(f"#!/usr/bin/env bash\n{data}\n")
    script.chmod(0o755)
    assert not contains_gateway_lifecycle_command_or_referenced_script(
        f"bash {script}", cwd=str(tmp_path)
    )


def test_sqlite_connect_heredoc_naming_oversized_db_is_not_blocked(tmp_path):
    """The measured repro shape: a Python heredoc naming a big SQLite file."""
    data = _oversized(tmp_path / "kanban.db", 0o644)
    script = tmp_path / "ac8-close.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "python3 <<'PY'\n"
        "import sqlite3, os\n"
        f"c = sqlite3.connect(os.path.expanduser('{data}'), timeout=60)\n"
        "c.close()\n"
        "PY\n"
    )
    script.chmod(0o755)
    assert not contains_gateway_lifecycle_command_or_referenced_script(
        f"bash {script}", cwd=str(tmp_path)
    )


def test_oversized_EXECUTABLE_file_at_command_position_still_fails_closed(tmp_path):
    """Negative control: an unscannable file the shell COULD execute stays blocked."""
    data = _oversized(tmp_path / "huge.sh", 0o755)
    script = tmp_path / "runner.sh"
    script.write_text(f"#!/usr/bin/env bash\n{data}\n")
    script.chmod(0o755)
    assert contains_gateway_lifecycle_command_or_referenced_script(
        f"bash {script}", cwd=str(tmp_path)
    )


def test_scannable_non_executable_script_at_command_position_is_still_scanned(
    tmp_path,
):
    """Narrowness control: only OVERSIZED non-executable files are skipped.

    `chmod +x x.sh && ./x.sh` is scanned while x.sh is still 0644, so a blanket
    "skip every non-executable file" exemption would be a guard bypass.
    """
    inner = tmp_path / "later-chmodded.sh"
    inner.write_text("hermes gateway restart\n")
    inner.chmod(0o644)
    assert contains_gateway_lifecycle_command_or_referenced_script(
        f"{inner}", cwd=str(tmp_path)
    )


def test_bash_argument_needs_no_x_bit_and_is_still_scanned(tmp_path):
    """`bash <file>` executes without the x bit — the scan must not be skipped."""
    script = tmp_path / "plain.sh"
    script.write_text("hermes gateway restart\n")
    script.chmod(0o644)
    assert contains_gateway_lifecycle_command_or_referenced_script(
        f"bash {script}", cwd=str(tmp_path)
    )


def test_source_argument_needs_no_x_bit_and_is_still_scanned(tmp_path):
    """`source <file>` executes without the x bit — the scan must not be skipped."""
    script = tmp_path / "sourced.sh"
    script.write_text("hermes gateway restart\n")
    script.chmod(0o644)
    assert contains_gateway_lifecycle_command_or_referenced_script(
        f"source {script}", cwd=str(tmp_path)
    )


def test_executable_script_at_command_position_is_still_scanned(tmp_path):
    """The exemption must not mask a real lifecycle line in an executable script."""
    inner = tmp_path / "inner.sh"
    inner.write_text("#!/usr/bin/env bash\nhermes gateway restart\n")
    inner.chmod(0o755)
    outer = tmp_path / "outer.sh"
    outer.write_text(f"#!/usr/bin/env bash\n{inner}\n")
    outer.chmod(0o755)
    assert contains_gateway_lifecycle_command_or_referenced_script(
        f"bash {outer}", cwd=str(tmp_path)
    )
