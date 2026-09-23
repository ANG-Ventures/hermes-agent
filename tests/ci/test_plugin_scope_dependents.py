"""Plugin-scoped CI must also run the plugin's cross-tree consumers.

#905 (b01891f52c) changed only ``plugins/blackbox/store.py``. Its merge-group
run (35831081688) resolved ``test_scope=plugin:blackbox`` and ran the plugin
slice + core smoke — green. The push-to-main full run on the same commit
(35831538817) failed ``tests/test_request_composition.py::
test_migration_adds_comp_columns_to_legacy_db`` with ``no such column:
ts_start``: #905 created ``idx_blackbox_turns_ts_start`` before the guarded
column migration, and the only test that opens a legacy ledger lives OUTSIDE
``tests/plugins/blackbox/``, so the scoped matrix never selected it.

``_scoped_plugin_matrix`` now adds a ``plugin <name> dependents`` slice of every
test under ``tests/`` that references ``plugins.<name>`` / ``plugins/<name>``,
and fails open to ``full`` when a plugin has more consumers than the cap.
"""

from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RUNNER = _REPO_ROOT / "scripts" / "run_tests_parallel.py"
_CONSUMER = "tests/test_request_composition.py"
_CONSUMER_NODE = "test_migration_adds_comp_columns_to_legacy_db"


@pytest.fixture(scope="module")
def runner():
    spec = importlib.util.spec_from_file_location("_rtp_dependents", _RUNNER)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _selected(matrix) -> dict[str, list[str]]:
    return {s["name"]: s["files"].split(":") for s in matrix["slice"]}


# ── the #905 changed-file shape, on the real tree ───────────────────────────


def test_905_shape_selects_the_cross_tree_blackbox_consumer(runner):
    scope = runner._plugin_scope_from_changes(["plugins/blackbox/store.py"])
    assert scope == "plugin:blackbox"  # still scoped, not widened to full

    matrix = runner._scoped_plugin_matrix(scope, _REPO_ROOT)
    assert matrix is not None
    selected = _selected(matrix)
    assert _CONSUMER in selected["plugin blackbox dependents"]
    # The node that went red on main must exist in the file that is selected;
    # a rename would otherwise turn this into a file-name-only check.
    tree = ast.parse((_REPO_ROOT / _CONSUMER).read_text(encoding="utf-8"))
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert _CONSUMER_NODE in names


def test_dependents_never_duplicate_the_plugins_own_tree(runner):
    matrix = runner._scoped_plugin_matrix("plugin:blackbox", _REPO_ROOT)
    selected = _selected(matrix)
    own = set(selected["plugin blackbox"])
    deps = set(selected["plugin blackbox dependents"])
    assert not own & deps
    assert not any(f.startswith("tests/plugins/blackbox/") for f in deps)


# ── controls ────────────────────────────────────────────────────────────────


def test_unrelated_plugin_stays_scoped_without_blackbox_consumers(runner):
    scope = runner._plugin_scope_from_changes(["plugins/web/providers.py"])
    assert scope == "plugin:web"
    matrix = runner._scoped_plugin_matrix(scope, _REPO_ROOT)
    assert matrix is not None
    every = {f for files in _selected(matrix).values() for f in files}
    assert _CONSUMER not in every
    assert not any(f.startswith("tests/plugins/blackbox/") for f in every)
    assert not any(f.startswith("tests/blackbox/") for f in every)


@pytest.mark.parametrize(
    "changed",
    [
        ["plugins/blackbox/store.py", "agent/usage_pricing.py"],
        ["plugins/blackbox/store.py", "plugins/web/providers.py"],
        ["plugins/blackbox/store.py", "tests/test_request_composition.py"],
        ["plugins/blackbox/store.py", "tests/conftest.py"],
    ],
)
def test_mixed_or_cross_plugin_changes_select_full(runner, changed):
    assert runner._plugin_scope_from_changes(changed) == "full"


def test_plugin_with_too_many_consumers_fails_open_to_full(runner, tmp_path):
    repo = _synthetic_repo(tmp_path, runner, broken=False)
    for i in range(runner._MAX_PLUGIN_DEPENDENT_TESTS + 1):
        (repo / "tests" / f"test_many_{i}.py").write_text(
            "import plugins.fakeplug.store\n", encoding="utf-8"
        )
    assert runner._scoped_plugin_matrix("plugin:fakeplug", repo) is None


# ── RED proof: the dependents slice actually fails on the #905 ordering ─────

_STORE_905_ORDER = """
import sqlite3

SCHEMA = '''
CREATE TABLE IF NOT EXISTS turns (turn_id TEXT PRIMARY KEY, ts_start REAL, ts_end REAL);
CREATE INDEX IF NOT EXISTS idx_turns_ts_start ON turns(ts_start);
'''

def connect(path):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)  # index before the guarded ALTER: #905's order
    cols = {r[1] for r in conn.execute("PRAGMA table_info(turns)")}
    if "ts_start" not in cols:
        conn.execute("ALTER TABLE turns ADD COLUMN ts_start REAL")
    return conn
"""

_STORE_FIXED_ORDER = """
import sqlite3

def connect(path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE IF NOT EXISTS turns (turn_id TEXT PRIMARY KEY, ts_start REAL, ts_end REAL)")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(turns)")}
    if "ts_start" not in cols:
        conn.execute("ALTER TABLE turns ADD COLUMN ts_start REAL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_turns_ts_start ON turns(ts_start)")
    return conn
"""


def _synthetic_repo(tmp_path: Path, runner, *, broken: bool) -> Path:
    repo = tmp_path / "repo"
    (repo / "plugins" / "fakeplug").mkdir(parents=True)
    (repo / "plugins" / "fakeplug" / "store.py").write_text(
        _STORE_905_ORDER if broken else _STORE_FIXED_ORDER, encoding="utf-8"
    )
    own = repo / "tests" / "plugins" / "fakeplug"
    own.mkdir(parents=True)
    # The plugin's own test only ever opens a FRESH db — green either way,
    # exactly like tests/plugins/blackbox/ on #905.
    (own / "test_store_fresh.py").write_text(
        textwrap.dedent(
            """
            from plugins.fakeplug import store

            def test_fresh_db(tmp_path):
                store.connect(str(tmp_path / "x.db")).close()
            """
        ),
        encoding="utf-8",
    )
    # The cross-tree consumer opens a LEGACY db, like test_request_composition.
    (repo / "tests" / "test_consumer_legacy.py").write_text(
        textwrap.dedent(
            """
            import sqlite3
            from plugins.fakeplug import store

            def test_legacy_db_migrates(tmp_path):
                db = str(tmp_path / "legacy.db")
                legacy = sqlite3.connect(db)
                legacy.execute("CREATE TABLE turns (turn_id TEXT PRIMARY KEY, ts_end REAL)")
                legacy.commit()
                legacy.close()
                store.connect(db).close()
            """
        ),
        encoding="utf-8",
    )
    for rel in runner._CORE_SMOKE_TESTS:
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_smoke():\n    pass\n", encoding="utf-8")
    (repo / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    return repo


def _run_slice(repo: Path, files: list[str]) -> tuple[int, str]:
    """Run each file in its own pytest process, as scripts/run_tests.sh does in CI.

    Returns (worst returncode, combined output).
    """
    worst, out = 0, []
    for rel in files:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "--rootdir", str(repo), "-c", str(repo / "pytest.ini"), rel],
            cwd=repo, capture_output=True, text=True, timeout=120,
        )
        worst = max(worst, proc.returncode)
        out.append(proc.stdout + proc.stderr)
    return worst, "\n".join(out)


def test_scoped_matrix_goes_red_on_the_905_migration_ordering(runner, tmp_path):
    repo = _synthetic_repo(tmp_path, runner, broken=True)
    scope = runner._plugin_scope_from_changes(["plugins/fakeplug/store.py"])
    assert scope == "plugin:fakeplug"
    matrix = runner._scoped_plugin_matrix(scope, repo)
    assert matrix is not None
    selected = _selected(matrix)

    # The two slices the pre-fix runner emitted are GREEN over the defect —
    # this is the #905 merge-group result.
    pre_fix = selected["plugin fakeplug"] + selected["core smoke"]
    rc, out = _run_slice(repo, pre_fix)
    assert rc == 0, out

    # The dependents slice is what turns the gate RED, for the real reason.
    deps = selected["plugin fakeplug dependents"]
    assert deps == ["tests/test_consumer_legacy.py"]
    rc, out = _run_slice(repo, deps)
    assert rc != 0
    assert "no such column: ts_start" in out


def test_dependents_slice_green_once_the_ordering_is_fixed(runner, tmp_path):
    repo = _synthetic_repo(tmp_path, runner, broken=False)
    matrix = runner._scoped_plugin_matrix("plugin:fakeplug", repo)
    for files in _selected(matrix).values():
        rc, out = _run_slice(repo, files)
        assert rc == 0, out
