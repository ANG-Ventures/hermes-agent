"""Boot preload keeps a gateway on ONE code snapshot (Aegis ImportError, 2026-09-28).

A function-scoped first-party import reads the module from disk at first use.
After the checkout fast-forwards under a long-lived gateway, that module comes
from the new commit and binds against old cached dependencies -> ImportError.
``gateway.boot_preload`` imports every first-party module at boot so those lazy
imports hit ``sys.modules`` instead.  These tests pin:

* the class, statically: every function-scoped first-party import target in the
  tree is covered by the preload walk (AST lint);
* the mechanism, behaviourally: a fast-forwarded tree breaks a lazy import
  without the preload and does not with it;
* the preload is side-effect safe on the real tree (subprocess);
* the one-line skew record in ``gateway.code_skew``.
"""

from __future__ import annotations

import ast
import json
import logging
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from gateway import boot_preload, code_skew

REPO_ROOT = Path(__file__).resolve().parents[2]

# Function-scoped import targets the preload deliberately does NOT cover.
# Adding an exclusion that some runtime code imports lazily must land here,
# with the reason it can never be reached inside a gateway process.
KNOWN_UNCOVERED_LAZY: dict[str, str] = {
    "tui_gateway.server": (
        "importing it rebinds sys.stdout and installs signal handlers, so it cannot be "
        "preloaded into a messaging gateway; its lazy sites are dashboard/serve code and "
        "tools.delegate_tool's steer-authority probe, which is try/except-guarded and "
        "only meaningful inside a TUI backend that already imported it at boot"
    ),
    "tui_gateway.ws": (
        "imports tui_gateway.server at module level; lazily imported only by the "
        "dashboard/serve web server, never by gateway code"
    ),
}


def _module_index() -> dict[str, Path]:
    """Every first-party module name that exists on disk -> its file."""
    index: dict[str, Path] = {}
    for mod in boot_preload.top_level_modules(REPO_ROOT):
        path = REPO_ROOT / f"{mod}.py"
        if path.is_file():
            index[mod] = path
    for pkg in boot_preload.PRELOAD_PACKAGES:
        pkg_dir = REPO_ROOT / pkg
        for path in pkg_dir.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(REPO_ROOT).with_suffix("")
            parts = list(rel.parts)
            if parts[-1] == "__init__":
                parts = parts[:-1]
            if all(p.isidentifier() or "-" in p for p in parts):
                index[".".join(parts)] = path
    return index


def _function_scoped_targets(index: dict[str, Path]) -> dict[str, set[str]]:
    """``{target_module: {importing_module, ...}}`` for every import inside a def."""
    first_party_roots = set(boot_preload.PRELOAD_PACKAGES) | set(boot_preload.top_level_modules(REPO_ROOT))
    targets: dict[str, set[str]] = {}

    def add(target: str, site: str) -> None:
        if target.split(".", 1)[0] in first_party_roots or target.split(".", 1)[0] in index:
            targets.setdefault(target, set()).add(site)

    for site, path in index.items():
        if boot_preload.is_excluded(site):
            continue  # excluded modules never run in a gateway, so neither do their lazy imports
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        is_pkg = path.name == "__init__.py"
        package = site if is_pkg else site.rpartition(".")[0]
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        add(alias.name, site)
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        base = package.split(".")
                        base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
                        module = ".".join(base + ([node.module] if node.module else []))
                    else:
                        module = node.module or ""
                    if not module:
                        continue
                    add(module, site)
                    # ``from pkg import submodule`` imports the submodule too.
                    for alias in node.names:
                        add(f"{module}.{alias.name}", site)
    # Keep only targets that are real modules on disk (``from x import func``
    # yields ``x.func`` candidates; guarded imports of absent modules resolve nothing).
    return {t: s for t, s in targets.items() if t in index}


@pytest.fixture(scope="module")
def lazy_targets() -> dict[str, set[str]]:
    return _function_scoped_targets(_module_index())


def test_lint_finds_the_incident_import(lazy_targets):
    """Sanity: the scanner sees the exact lazy import that broke Aegis."""
    assert "run_agent" in lazy_targets["agent.chat_completion_helpers"]


def test_every_function_scoped_first_party_import_is_preloaded(lazy_targets):
    covered = set(boot_preload.iter_preload_names(REPO_ROOT))
    uncovered = {
        target: sorted(sites)[:3]
        for target, sites in lazy_targets.items()
        if target not in covered and target not in KNOWN_UNCOVERED_LAZY
    }
    assert not uncovered, (
        "function-scoped first-party imports not covered by gateway.boot_preload "
        "(add the package root to PRELOAD_PACKAGES, drop the exclusion, or justify it in "
        f"KNOWN_UNCOVERED_LAZY): {json.dumps(uncovered, indent=1, sort_keys=True)}"
    )


def test_known_uncovered_allowlist_does_not_rot(lazy_targets):
    covered = set(boot_preload.iter_preload_names(REPO_ROOT))
    for target in KNOWN_UNCOVERED_LAZY:
        assert target in lazy_targets, f"{target} is no longer imported lazily; drop it"
        assert target not in covered, f"{target} is preloaded now; drop it"


def test_preload_roots_match_the_installed_package_set():
    """PRELOAD_PACKAGES mirrors pyproject's first-party packages, so a new root can't slip by."""
    import tomllib

    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    include = data["tool"]["setuptools"]["packages"]["find"]["include"]
    roots = {entry.split(".", 1)[0] for entry in include}
    assert roots == set(boot_preload.PRELOAD_PACKAGES)


def test_exclusions_never_import_test_or_main_modules():
    names = list(boot_preload.iter_preload_names(REPO_ROOT))
    assert names, "walk found nothing"
    assert not [n for n in names if boot_preload.is_excluded(n)]
    for name in boot_preload.EXCLUDED_MODULES:
        assert name not in names


def _write(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source), encoding="utf-8")


@pytest.mark.parametrize("preload", [False, True])
def test_fast_forwarded_tree_breaks_lazy_import_only_without_preload(tmp_path, monkeypatch, preload):
    """The incident shape on a synthetic tree: boot, preload (or not), tree moves, lazy path runs."""
    pkg = "bootpre_fixture_pkg"
    root = tmp_path
    _write(root / pkg / "__init__.py", "")
    _write(root / pkg / "relay_headers.py", "def old_helper():\n    return 1\n")
    _write(
        root / pkg / "helpers.py",
        "from bootpre_fixture_pkg.relay_headers import old_helper\n"
        "def build_api_kwargs():\n    return old_helper()\n",
    )
    _write(
        root / pkg / "agent_core.py",
        "import bootpre_fixture_pkg.relay_headers\n"
        "def build():\n"
        "    from bootpre_fixture_pkg.helpers import build_api_kwargs\n"
        "    return build_api_kwargs()\n",
    )
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setattr(boot_preload, "PRELOAD_PACKAGES", (pkg,))
    for mod in [m for m in sys.modules if m.startswith(pkg)]:
        monkeypatch.delitem(sys.modules, mod)
    try:
        import importlib

        importlib.invalidate_caches()
        core = importlib.import_module(f"{pkg}.agent_core")  # "gateway boot"
        if preload:
            result = boot_preload.preload_first_party_modules(root)
            assert result["failed"] == 0 and result["modules"] >= 1
        # Tree fast-forwards: helpers now needs a symbol the cached relay_headers lacks.
        _write(root / pkg / "relay_headers.py", "def old_helper():\n    return 1\ndef stamp_bridge_lane():\n    return 2\n")
        _write(
            root / pkg / "helpers.py",
            "from bootpre_fixture_pkg.relay_headers import stamp_bridge_lane\n"
            "def build_api_kwargs():\n    return stamp_bridge_lane()\n",
        )
        importlib.invalidate_caches()
        if preload:
            assert core.build() == 1  # old coherent snapshot keeps running
        else:
            with pytest.raises(ImportError, match="stamp_bridge_lane"):
                core.build()
    finally:
        for mod in [m for m in sys.modules if m.startswith(pkg)]:
            sys.modules.pop(mod, None)


def test_failed_import_is_logged_and_does_not_abort(tmp_path, monkeypatch, caplog):
    pkg = "bootpre_fixture_broken"
    _write(tmp_path / pkg / "__init__.py", "")
    _write(tmp_path / pkg / "ok.py", "X = 1\n")
    _write(tmp_path / pkg / "boom.py", "raise SystemExit(2)\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(boot_preload, "PRELOAD_PACKAGES", (pkg,))
    try:
        with caplog.at_level(logging.WARNING, logger="gateway.boot_preload"):
            result = boot_preload.preload_first_party_modules(tmp_path)
        assert result["failed"] == 1
        assert f"{pkg}.ok" in sys.modules
        assert any(f"{pkg}.boom" in r.getMessage() for r in caplog.records)
    finally:
        for mod in [m for m in sys.modules if m.startswith(pkg)]:
            sys.modules.pop(mod, None)



def test_absent_optional_dependency_is_a_reasoned_skip_not_a_failure(tmp_path, monkeypatch, caplog):
    """t_d07ad201: acp_adapter.* failed=4 on the live fleet because the optional ``acp`` extra is
    not in the runtime venv. A third-party ModuleNotFoundError is skipped and named at INFO;
    a missing FIRST-PARTY module is still real breakage and stays in ``failed``."""
    pkg = "bootpre_fixture_optdep"
    _write(tmp_path / pkg / "__init__.py", "")
    _write(tmp_path / pkg / "needs_extra.py", "import bootpre_absent_extra_xyz\n")
    _write(tmp_path / pkg / "also_extra.py", "from bootpre_absent_extra_xyz.sub import thing\n")
    _write(tmp_path / pkg / "broken_inner.py", f"import {pkg}.deleted_module\n")
    _write(tmp_path / pkg / "ok.py", "X = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(boot_preload, "PRELOAD_PACKAGES", (pkg,))
    try:
        with caplog.at_level(logging.INFO, logger="gateway.boot_preload"):
            result = boot_preload.preload_first_party_modules(tmp_path)
        assert result["skipped"] == {
            f"{pkg}.also_extra": "bootpre_absent_extra_xyz",
            f"{pkg}.needs_extra": "bootpre_absent_extra_xyz",
        }
        assert result["failed"] == 1 and f"{pkg}.broken_inner" in result["errors"]
        skip_lines = [r for r in caplog.records if "skipped" in r.getMessage() and "optional" in r.getMessage()]
        assert len(skip_lines) == 1 and skip_lines[0].levelno == logging.INFO
        assert pkg in skip_lines[0].getMessage() and "bootpre_absent_extra_xyz" in skip_lines[0].getMessage()
        warned = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert not [m for m in warned if "extra" in m], warned
        phase = [r.getMessage() for r in caplog.records if "PHASE=boot_preload" in r.getMessage()]
        assert phase and "failed=1 skipped=2" in phase[0]
    finally:
        for mod in [m for m in sys.modules if m.startswith(pkg)]:
            sys.modules.pop(mod, None)

_SIDE_EFFECT_PROBE = r"""
import json, os, signal, sys, threading
sys.path.insert(0, sys.argv[1])
import hermes_cli.main, gateway.run, run_agent  # the gateway's own boot imports
def snap():
    sigs = {}
    for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGUSR1", "SIGPIPE"):
        if hasattr(signal, name):
            sigs[name] = repr(signal.getsignal(getattr(signal, name)))
    return {"signals": sigs, "path": list(sys.path), "env": dict(os.environ),
            "threads": threading.active_count(), "cwd": os.getcwd()}
before = snap()
from gateway.boot_preload import preload_first_party_modules
result = preload_first_party_modules()
after = snap()
print(json.dumps({"result": result,
                  "diff": {k: [before[k], after[k]] for k in before if before[k] != after[k] and k != "env"},
                  "env_changed": sorted(k for k in before["env"]
                                        if before["env"][k] != after["env"].get(k))}))
"""


def test_preload_on_real_tree_has_no_process_side_effects(tmp_path):
    """Import every covered module in a fresh interpreter; nothing process-global may move."""
    proc = subprocess.run(
        [sys.executable, "-c", _SIDE_EFFECT_PROBE, str(REPO_ROOT)],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(tmp_path),
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    diff = payload["diff"]
    # sys.path: a few adapters re-insert the project root (already present); only
    # entries outside the project root would change module identity.
    if "path" in diff:
        before, after = diff.pop("path")
        added = {p for p in after if p not in before}
        root = str(REPO_ROOT)
        assert all(p.rstrip("/") == root for p in added), added
    assert not diff, diff
    # No pre-existing env var may be clobbered or removed.  Newly-set keys are
    # allowed: cli.py (already lazily imported by gateway slash commands) runs
    # its gateway-aware config->env bridge at import, which only fills keys the
    # gateway's own bridge sets from the same config.yaml.
    assert not payload["env_changed"], payload["env_changed"]
    # A module may legitimately fail on a host missing one of its optional
    # dependencies (best-effort; logged at WARNING).  Anything other than an
    # import failure means the module DOES something at import -> exclude it.
    errors = payload["result"]["errors"]
    assert all(e in ("ImportError", "ModuleNotFoundError") for e in errors.values()), errors


class TestCodeSkewPreloadLine:
    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        monkeypatch.setattr(code_skew, "_boot_fingerprint", None)
        monkeypatch.setattr(code_skew, "_preloaded_modules", None)
        monkeypatch.setattr(code_skew, "_skew_logged", False)

    def _skew(self, monkeypatch):
        monkeypatch.setattr(code_skew, "_fingerprint", lambda: "git:refs/heads/main:abc1234567890")
        code_skew.record_boot_fingerprint()
        monkeypatch.setattr(code_skew, "_fingerprint", lambda: "git:refs/heads/main:def4567890123")

    def test_logs_once_with_module_count(self, monkeypatch, caplog):
        self._skew(monkeypatch)
        code_skew.record_preload(812)
        with caplog.at_level(logging.WARNING, logger="gateway.code_skew"):
            assert code_skew.detect_code_skew() == ("abc1234567", "def4567890")
            code_skew.detect_code_skew()
        lines = [r.getMessage() for r in caplog.records if "PHASE=code_skew_preloaded" in r.getMessage()]
        assert lines == ["PHASE=code_skew_preloaded modules=812 boot=abc1234567 disk=def4567890"]

    def test_reports_none_when_preload_never_ran(self, monkeypatch, caplog):
        self._skew(monkeypatch)
        with caplog.at_level(logging.WARNING, logger="gateway.code_skew"):
            code_skew.detect_code_skew()
        assert any("modules=none" in r.getMessage() for r in caplog.records)

    def test_no_line_without_skew(self, monkeypatch, caplog):
        monkeypatch.setattr(code_skew, "_fingerprint", lambda: "git:refs/heads/main:abc1234567890")
        code_skew.record_boot_fingerprint()
        with caplog.at_level(logging.WARNING, logger="gateway.code_skew"):
            assert code_skew.detect_code_skew() is None
        assert not caplog.records
