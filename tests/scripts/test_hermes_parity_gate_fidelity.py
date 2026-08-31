"""Regression tests for hermes_parity tool bugs found on the 2026-08-30 parity sync.

Both bugs produced FALSE FAILURES that read as merge defects:
1. conflict_marker_lines matched RST/Sphinx docstring section underlines
   (`=======...`) via a prefix regex -> 5 phantom "marker" FAILs on a clean tree.
2. manifest_nodeids emitted duplicate paths when two features share a test file
   -> pytest "Empty parameter set" collection error that read as manifest rot.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from hermes_parity import forkdelta, gitops  # noqa: E402


@pytest.fixture()
def scratch_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


def _commit_file(repo: Path, name: str, content: str) -> None:
    (repo / name).write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", name], check=True)


class TestConflictMarkerLines:
    def test_real_markers_are_caught(self, scratch_repo: Path) -> None:
        """Positive control: a tightened detector must still catch real markers."""
        _commit_file(
            scratch_repo,
            "conflicted.txt",
            "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> other\n",
        )
        lines = gitops.conflict_marker_lines(scratch_repo)
        assert len(lines) == 3, f"expected all three marker lines, got {lines}"

    def test_diff3_base_marker_is_caught(self, scratch_repo: Path) -> None:
        """The pre-fix regex was BLIND to the diff3 `|||||||` marker entirely."""
        _commit_file(
            scratch_repo,
            "diff3.txt",
            "<<<<<<< ours\na\n||||||| base\nb\n=======\nc\n>>>>>>> theirs\n",
        )
        lines = gitops.conflict_marker_lines(scratch_repo)
        assert any("|||||||" in line for line in lines), (
            "diff3 base marker must be detected"
        )

    def test_rst_docstring_underline_is_not_a_marker(self, scratch_repo: Path) -> None:
        """The 2026-08-30 false positive: Sphinx section underlines are not markers."""
        _commit_file(
            scratch_repo,
            "module.py",
            '"""\nTerminal Environment Provider ABC\n'
            "=================================\n\nDocs body.\n"
            "===========================  ======\ncol-a                        col-b\n"
            '"""\n',
        )
        assert gitops.conflict_marker_lines(scratch_repo) == []

    def test_eight_equals_is_not_a_marker(self, scratch_repo: Path) -> None:
        """Only the exactly-7-char `=======` line is a git marker."""
        _commit_file(scratch_repo, "eq.txt", "========\n======\n")
        assert gitops.conflict_marker_lines(scratch_repo) == []


class TestManifestNodeids:
    def test_duplicate_nodeids_across_features_are_deduped(self, tmp_path: Path) -> None:
        """Two features sharing a test file must not emit the path twice.

        Duplicate paths on one pytest invocation trip a spurious
        'Empty parameter set' collection error on parametrized tests
        (observed live 2026-08-30 on tests/gateway/test_fast_command.py).
        """
        manifest = tmp_path / "fork-features.json"
        manifest.write_text(
            """[
  {"feature": "a", "tests": ["tests/x.py", "tests/y.py::test_one"],
   "paths": [], "why": "w"},
  {"feature": "b", "tests": ["tests/x.py", "tests/z.py"],
   "paths": [], "why": "w"}
]""",
            encoding="utf-8",
        )
        ids = forkdelta.manifest_nodeids(manifest)
        assert ids == ["tests/x.py", "tests/y.py::test_one", "tests/z.py"]
        assert len(ids) == len(set(ids)), "manifest_nodeids must never emit duplicates"

    def test_order_is_preserved(self, tmp_path: Path) -> None:
        manifest = tmp_path / "fork-features.json"
        manifest.write_text(
            '[{"feature": "a", "tests": ["tests/b.py", "tests/a.py"],'
            ' "paths": [], "why": "w"}]',
            encoding="utf-8",
        )
        assert forkdelta.manifest_nodeids(manifest) == ["tests/b.py", "tests/a.py"]
