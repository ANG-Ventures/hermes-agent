"""scripts/check_kanban_lineage_readers.py: the lineage reap tag has exactly one reader.

Option C K6 (card t_7f2f8ecf). The tag survives scrub_kanban_env, so a reader that took it as
"my card" would hand a delegated child authority it was never granted.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_kanban_lineage_readers.py"


def _lint():
    spec = importlib.util.spec_from_file_location("check_kanban_lineage_readers", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_tree_is_clean():
    assert _lint().main([]) == 0


def test_planted_readers_fail():
    lint = _lint()
    planted = (
        'import os\n'
        'card = os.environ.get("HERMES_KANBAN_LINEAGE")\n'
        'from hermes_cli.kanban_db import KANBAN_LINEAGE_ENV\n'
        'own = env[KANBAN_LINEAGE_ENV].split(":")[0]\n'
        'if KANBAN_LINEAGE_ENV in os.environ: pass\n'
    )
    assert [p.split(":")[1] for p in lint.scan_source(planted, "x.py")] == ["2", "4", "5"]


def test_writes_pops_and_the_definition_pass():
    lint = _lint()
    ok = (
        'KANBAN_LINEAGE_ENV = "HERMES_KANBAN_LINEAGE"\n'
        'env[KANBAN_LINEAGE_ENV] = "t:1"\n'
        'env.pop(KANBAN_LINEAGE_ENV, None)\n'
        'x = env.get(KANBAN_LINEAGE_ENV)  # lineage-reader: ok — reaper\n'
    )
    assert lint.scan_source(ok, "x.py") == []


# The raw-text prefilter in front of the AST check must never skip a file the AST would flag:
# each spelling below decodes to a reader of the tag (t_44dcc531, Prism 55a085104aea).
_BS = chr(92)
_SPLIT_READERS = {
    "backslash-newline-in-literal": 'import os\nx = os.environ.get("HERMES_KANBAN_' + _BS + '\nLINEAGE")\n',
    "backslash-crlf-in-literal": 'import os\r\nx = os.environ.get("HERMES_KANBAN_' + _BS + '\r\nLINEAGE")\r\n',
    "backslash-cr-in-literal": 'import os\rx = os.environ.get("HERMES_KANBAN_' + _BS + '\rLINEAGE")\r',
    "implicit-join": 'import os\nx = os.environ.get("HERMES_KANBAN_" "LINEAGE")\n',
    "implicit-join-empty-literal": 'import os\nx = os.environ.get("HERMES_KAN" "" "BAN_LINEAGE")\n',
    "continuation-between-literals": 'import os\nx = os.environ.get("HERMES_KANBAN_" ' + _BS + '\n "LINEAGE")\n',
    "crlf-continuation-between-literals": 'import os\r\nx = os.environ.get("HERMES_" ' + _BS + '\r\n "KANBAN_LINEAGE")\r\n',
    "comment-between-literals": 'import os\nx = os.environ.get(("HERMES_KANBAN_"  # c\n "LINEAGE"))\n',
    "hex-escape": 'import os\nx = os.environ.get("HERMES_\\x4bANBAN_LINEAGE")\n',
    "nfkc-identifier": 'from hermes_cli.kanban_db import KANBAN_LINEAGE_\uff25\uff2e\uff36\ny = env[KANBAN_LINEAGE_\uff25\uff2e\uff36]\n',
}


import pytest  # noqa: E402


@pytest.mark.parametrize("src", list(_SPLIT_READERS.values()), ids=list(_SPLIT_READERS))
def test_a_split_spelling_of_the_tag_is_still_scanned(src):
    assert _lint().scan_source(src, "x.py"), "the prefilter skipped a file the AST flags"
