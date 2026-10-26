"""Write-time shell lint: ``bash -n`` + bash-3.2 hazards on write_file/patch results.

Agent-authored one-shot scripts are untracked, so CI's bash32-array lint never sees
them; the hazard surfaced only when the unattended run aborted.
"""

import pytest
import yaml

from tests.tools.test_file_operations import make_real_subprocess_env
from tools.file_operations import ShellFileOperations
from tools.file_operations_lint_shell import bash32_findings


def _write(tmp_path, name, content):
    ops = ShellFileOperations(make_real_subprocess_env(str(tmp_path)))
    result = ops.write_file(str(tmp_path / name), content)
    assert result.error is None, result.error
    assert (tmp_path / name).read_text() == content  # a finding never blocks the write
    return result.lint


@pytest.mark.parametrize("body, needle", [
    ('a=()\necho "${a[@]}"\n', 'a[@]+"${a[@]}"'),
    ('a=()\nprintf %s "${a[*]}"\n', 'a[@]+"${a[@]}"'),
    ('declare -A m\n', 'declare/local/typeset -A'),
    ('f() { local -rA m; }\n', 'declare/local/typeset -A'),
    ('mapfile -t lines < f\n', 'mapfile/readarray'),
    ('readarray lines < f\n', 'mapfile/readarray'),
    ('x=AB\necho "${x,,}"\n', 'case conversion'),
    ('x=ab\necho "${x^^}"\n', 'case conversion'),
])
def test_each_rule_warns_with_line_and_fix(tmp_path, body, needle):
    lint = _write(tmp_path, "s.sh", "#!/usr/bin/env bash\nset -euo pipefail\n" + body)
    assert lint["status"] == "warning", lint
    line = 2 + body.count("\n")
    assert f"s.sh:{line}: " in lint["warnings"] and needle in lint["warnings"], lint["warnings"]


@pytest.mark.parametrize("content", [
    '#!/usr/bin/env bash\nset -u\na=()\necho ${a[@]+"${a[@]}"}\n',
    '#!/usr/bin/env bash\nset -o nounset\na=(x)\necho "${a[@]}"  # bash32-array-ok: always non-empty\n',
    '#!/opt/homebrew/bin/bash\nset -u\ndeclare -A m\nmapfile -t l < f\na=()\necho "${a[@]}" "${x,,}"\n',
    '#!/usr/local/bin/bash\nset -u\nreadarray l < f\n',
    '#!/usr/bin/env bash\na=()\necho "${a[@]}"\n',  # no nounset: an empty array expands to nothing
    '#!/usr/bin/env bash\nset -u\n# echo "${a[@]}" in a comment\n',
])
def test_guarded_marked_modern_and_non_nounset_are_clean(tmp_path, content):
    assert _write(tmp_path, "s.sh", content)["status"] == "ok"


def test_nounset_detected_in_every_spelling():
    for prelude in ("set -u", "set -eu", "set -o nounset", "set -euo pipefail", "shopt -so nounset"):
        assert bash32_findings(f"#!/usr/bin/env bash\n{prelude}\necho \"${{a[@]}}\"\n"), prelude
    assert bash32_findings('#!/usr/bin/env bash -eu\necho "${a[@]}"\n')
    assert not bash32_findings('#!/usr/bin/env bash\nset -e\necho "${a[@]}"\n')


def test_parse_error_is_an_error_and_keeps_the_write(tmp_path):
    lint = _write(tmp_path, "broken.bash", "#!/usr/bin/env bash\nif then\nfi\n")
    assert lint["status"] == "error" and "bash -n" in lint["output"] and "line 2" in lint["output"], lint


def test_patch_that_introduces_the_hazard_warns(tmp_path):
    _write(tmp_path, "p.sh", '#!/usr/bin/env bash\nset -u\na=()\necho ok\n')
    ops = ShellFileOperations(make_real_subprocess_env(str(tmp_path)))
    result = ops.patch_replace(str(tmp_path / "p.sh"), "echo ok", 'echo "${a[@]}"')
    assert result.success and result.lint["status"] == "warning", result.lint
    assert "p.sh:4: " in result.lint["warnings"]


def test_bash_shebang_without_extension_is_linted(tmp_path):
    lint = _write(tmp_path, "runme", '#!/usr/bin/env bash\nset -u\na=()\necho "${a[@]}"\n')
    assert lint["status"] == "warning", lint


def test_posix_sh_gets_only_the_parse_check(tmp_path):
    clean = _write(tmp_path, "p.sh", '#!/bin/sh\nset -u\necho "${a[@]}"\ndeclare -A m\n')
    assert clean["status"] == "ok" and "warnings" not in clean, clean
    broken = _write(tmp_path, "q.sh", "#!/bin/sh\nif then\nfi\n")
    assert broken["status"] == "error", broken


@pytest.mark.platforms("macos")
def test_parse_check_uses_system_bash_32_on_macos(tmp_path):
    """bash-4 `;;&` parses under bash 5 but not under the /bin/bash the unattended runs use."""
    lint = _write(tmp_path, "c.sh", "#!/usr/bin/env bash\ncase a in a) echo;;& esac\n")
    assert lint["status"] == "error" and "bash 3." in lint["output"], lint


def test_kill_switch_disables_shell_lint(tmp_path, monkeypatch):
    import os
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    os.makedirs(home, exist_ok=True)
    (home / "config.yaml").write_text(yaml.safe_dump({"file_lint": {"shell": False}}))
    lint = _write(tmp_path, "s.sh", '#!/usr/bin/env bash\nset -u\necho "${a[@]}"\nif then\n')
    assert lint["status"] == "skipped" and "No linter for .sh" in lint["message"], lint
