"""Provider discovery must not run at import time (t_bd6d58c3).

``hermes_cli.config`` (OPTIONAL_ENV_VARS) and ``hermes_cli.auth`` (PROVIDER_REGISTRY)
used to call ``providers.list_providers()`` at module load. Discovery imports
every model-provider plugin, so a script that only imported
``hermes_cli.kanban_db`` -- and every ``hermes kanban`` one-shot, which reaches
``auth`` through ``kanban_db.connect()`` -- paid for it. Both registries are now
``LazyFilledDict``s filled on first read.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from hermes_cli.lazy_registry import LazyFilledDict

REPO_ROOT = Path(__file__).resolve().parents[2]

# Generous: measured ~0.15 s idle for the module's cumulative import.
KANBAN_DB_IMPORT_BUDGET_US = 2_000_000


def _run(code: str, tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["HERMES_HOME"] = str(tmp_path)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, *args, "-c", textwrap.dedent(code)],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_importing_kanban_config_auth_does_not_discover_providers(tmp_path):
    proc = _run(
        """
        import hermes_cli.kanban_db, hermes_cli.config, hermes_cli.auth
        import providers
        print("DISCOVERED", providers._discovered)
        """,
        tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    assert "DISCOVERED False" in proc.stdout, proc.stdout + proc.stderr


def test_first_read_fills_both_registries_from_provider_profiles(tmp_path):
    proc = _run(
        """
        import providers
        from hermes_cli.config import OPTIONAL_ENV_VARS
        from hermes_cli.auth import PROVIDER_REGISTRY
        raw_env = dict.__len__(OPTIONAL_ENV_VARS)
        raw_reg = dict.__len__(PROVIDER_REGISTRY)
        assert not providers._discovered
        filled_env = len(OPTIONAL_ENV_VARS)
        filled_reg = len(PROVIDER_REGISTRY)
        assert providers._discovered
        missing = [
            v
            for p in providers.list_providers()
            if p.auth_type == "api_key"
            for v in p.env_vars
            if v not in OPTIONAL_ENV_VARS
        ]
        print("GREW", filled_env > raw_env, filled_reg > raw_reg, "MISSING", missing)
        """,
        tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    assert "GREW True True MISSING []" in proc.stdout, proc.stdout + proc.stderr


def test_kanban_db_import_time_budget(tmp_path):
    proc = _run("import hermes_cli.kanban_db", tmp_path, "-X", "importtime")
    assert proc.returncode == 0, proc.stderr
    cumulative = None
    for line in proc.stderr.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) == 3 and parts[2] == "hermes_cli.kanban_db":
            cumulative = int(parts[1])
    assert cumulative is not None, proc.stderr[-2000:]
    assert cumulative < KANBAN_DB_IMPORT_BUDGET_US, (
        f"import hermes_cli.kanban_db took {cumulative / 1e6:.2f}s cumulative "
        f"(budget {KANBAN_DB_IMPORT_BUDGET_US / 1e6:.1f}s)"
    )


class TestLazyFilledDict:
    def test_writes_do_not_fill_and_first_read_fills_once_in_order(self):
        calls = []
        d = LazyFilledDict(a=1)
        d.add_filler(lambda: (calls.append("one"), dict.__setitem__(d, "b", 2)))
        d.add_filler(lambda: calls.append("two"))
        d["c"] = 3
        assert calls == []
        assert "b" in d
        assert calls == ["one", "two"]
        assert dict(d) == {"a": 1, "b": 2, "c": 3}
        assert calls == ["one", "two"]

    def test_filler_reads_see_partial_view_without_recursing(self):
        d = LazyFilledDict(a=1)
        seen = []

        def filler():
            seen.append(("a" in d, "b" in d, len(d)))
            d["b"] = 2

        d.add_filler(filler)
        assert d["b"] == 2
        assert seen == [(True, False, 1)]

    def test_existing_keys_win_over_filler_that_skips_present_keys(self):
        d = LazyFilledDict()

        def filler():
            if "k" not in d:
                d["k"] = "filler"

        d.add_filler(filler)
        d["k"] = "explicit"
        assert d["k"] == "explicit"

    @pytest.mark.parametrize(
        "op",
        [
            lambda d: d.pop("x"),
            lambda d: d.__delitem__("x"),
            lambda d: d.setdefault("x", 0),
            lambda d: d.copy(),
            lambda d: list(d.items()),
            lambda d: d.get("x"),
            lambda d: d == {"x": 1},
        ],
    )
    def test_reads_and_deletes_fill_first(self, op):
        d = LazyFilledDict()
        d.add_filler(lambda: dict.__setitem__(d, "x", 1))
        op(d)
        assert d._pending == []
