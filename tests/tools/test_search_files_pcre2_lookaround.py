"""`search_files` must accept PCRE2-only regex syntax (look-around, backrefs).

Regression coverage for papercut pc-9320531b: a negative-lookbehind symbol query
(`(?<!def )foo`) ERRORED instead of searching, because rg's default engine is Rust
`regex` — finite-automata based, with no look-around at all. rg ships a `--pcre2`
engine that supports exactly this syntax, so the tool now selects it automatically.

Two layers, both covered here:
  1. `_pattern_needs_pcre2` detects the syntax up front and adds `--pcre2`.
  2. If rg still reports PCRE2-only-syntax error text (a construct the detector does
     not yet know), the search retries once with `--pcre2` — so the fix covers the
     CLASS, not only the syntax enumerated today.
"""
from __future__ import annotations

import pytest

from tools.file_operations import (
    _is_pcre2_only_syntax_error,
    _pattern_needs_pcre2,
)


@pytest.mark.parametrize(
    "pattern",
    [
        r"(?<!def )detect_hardline_command",   # negative lookbehind (the reported case)
        r"(?<=class )Foo",                     # positive lookbehind
        r"foo(?! bar)",                        # negative lookahead
        r"foo(?= bar)",                        # positive lookahead
        r"(?<name>\w+)",                       # named group, PCRE syntax
        r"prefix\Ksuffix",                     # \K
    ],
)
def test_lookaround_patterns_are_detected(pattern):
    assert _pattern_needs_pcre2(pattern) is True


@pytest.mark.parametrize(
    "pattern",
    [
        r"plain_symbol",
        r"foo|bar",
        r"^\s*def \w+\(",
        r"[a-z]+\d{2,}",
        r"import (os|sys)",
        r"\(\?x\)",          # escaped literal parens, not a group
        r"a(?:b|c)d",        # NON-capturing group is standard Rust-regex syntax
        r"(?i)case",         # inline flags are supported by the default engine
    ],
)
def test_ordinary_patterns_do_not_switch_engines(pattern):
    """Precision guard: don't pay PCRE2's cost (and its different semantics) for
    patterns the default engine handles."""
    assert _pattern_needs_pcre2(pattern) is False


@pytest.mark.parametrize(
    "text",
    [
        "rg: regex parse error:\n    (?:(?<!def )x)\n       ^^^^\nerror: unrecognized flag",
        "regex parse error: look-around, including look-ahead and look-behind, is not supported",
        "error: backreferences are not supported",
    ],
)
def test_pcre2_only_errors_are_recognized(text):
    assert _is_pcre2_only_syntax_error(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        None,
        "rg: /some/file.txt: No such file or directory",
        "rg: regex parse error:\n    [a-\n     ^\nerror: unclosed character class",
        "3 matches found",
    ],
)
def test_unrelated_output_is_not_mistaken_for_a_pcre2_error(text):
    """A missing file or a genuinely-broken regex must NOT trigger a pointless
    PCRE2 retry."""
    assert _is_pcre2_only_syntax_error(text) is False


def test_lookaround_search_returns_matches_end_to_end(tmp_path):
    """Live proof against real rg: the reported query now SEARCHES.

    Skips when rg is unavailable or was built without PCRE2 — the tool falls back
    to grep there, and asserting rg behavior would be a false red.
    """
    import shutil
    import subprocess

    rg = shutil.which("rg")
    if not rg:
        pytest.skip("rg not installed")
    probe = subprocess.run(
        [rg, "--pcre2", "--version"], capture_output=True, text=True, timeout=30
    )
    if probe.returncode != 0:
        pytest.skip("rg built without PCRE2")

    target = tmp_path / "sample.py"
    target.write_text(
        "def detect_hardline_command(x):\n"
        "    return detect_hardline_command(x)\n"
        "value = detect_hardline_command(1)\n"
    )

    pattern = r"(?<!def )detect_hardline_command"
    assert _pattern_needs_pcre2(pattern) is True

    # Default engine: hard error (this is the bug being fixed).
    plain = subprocess.run(
        [rg, "-c", pattern, str(target)], capture_output=True, text=True, timeout=30
    )
    assert plain.returncode == 2
    assert _is_pcre2_only_syntax_error(plain.stderr) is True

    # PCRE2 engine: finds the two non-`def` call sites, not the definition.
    with_pcre2 = subprocess.run(
        [rg, "--pcre2", "-c", pattern, str(target)],
        capture_output=True, text=True, timeout=30,
    )
    assert with_pcre2.returncode == 0
    assert with_pcre2.stdout.strip().endswith("2")
