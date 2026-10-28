"""Environment parity between the gateway and the cron script runner (spec §5.6, slice S4).

AC-17: an AST lint over ``gateway/**/*.py``. Every ``os.environ`` write (``[k] =``, ``del [k]``,
``.setdefault/.update/.pop/.popitem/.clear``) and every ``os.putenv``/``os.unsetenv`` must be listed in
``tests/gateway/env_write_allowlist.txt`` with a reason. Child-visible start-path writes live in
``hermes_cli/process_bootstrap.py`` (outside ``gateway/``), which the gateway and the runner both run.

AC-7: given a fixture ``.env``, a fixture ``config.yaml`` and a scratch home, the gateway's start path
(``import gateway.run`` + ``start_gateway``'s env prelude) and the runner's start path
(``cron.script_runner._bootstrap_env``) leave equal values for every ``GATEWAY_CHILD_VISIBLE_ENV`` key the
fixture causes. ``_HERMES_GATEWAY`` is the exception: absent from the runner's own ``os.environ``,
present in the env of a script child the runner spawns. Both children's envs are compared too (I5).
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ALLOWLIST = Path(__file__).with_name("env_write_allowlist.txt")
_MUTATORS = {"setdefault", "update", "pop", "popitem", "clear", "__setitem__", "__delitem__"}


# --------------------------------------------------------------------------- AC-17 lint


def _is_os_environ(node: ast.AST) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "environ"
            and isinstance(node.value, ast.Name) and node.value.id == "os")


class _Sites(ast.NodeVisitor):
    def __init__(self, rel: str):
        self.rel = rel
        self.stack: list[str] = []
        self.sites: list[tuple[str, int]] = []

    def _add(self, node: ast.AST, op: str) -> None:
        qual = ".".join(self.stack) or "<module>"
        self.sites.append((f"{self.rel}::{qual}::{op}", node.lineno))

    def _scoped(self, node) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scoped

    def _targets(self, node, targets, verb: str) -> None:
        for target in targets:
            for sub in ast.walk(target):
                if isinstance(sub, ast.Subscript) and _is_os_environ(sub.value):
                    self._add(node, f"{verb} {ast.unparse(sub.slice)}")

    def visit_Assign(self, node):
        self._targets(node, node.targets, "set")
        self.generic_visit(node)

    def visit_AugAssign(self, node):
        self._targets(node, [node.target], "set")
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        self._targets(node, [node.target], "set")
        self.generic_visit(node)

    def visit_Delete(self, node):
        self._targets(node, node.targets, "del")
        self.generic_visit(node)

    def visit_Call(self, node):
        func = node.func
        first = ast.unparse(node.args[0]) if node.args else ""
        if isinstance(func, ast.Attribute):
            if _is_os_environ(func.value) and func.attr in _MUTATORS:
                self._add(node, f"{func.attr} {first}".rstrip())
            elif (func.attr in {"putenv", "unsetenv"} and isinstance(func.value, ast.Name)
                  and func.value.id == "os"):
                self._add(node, f"{func.attr} {first}".rstrip())
        self.generic_visit(node)


def find_env_write_sites(root: Path) -> list[tuple[str, int]]:
    """Every os.environ write under ``<root>/gateway`` as ``(site-id, lineno)``."""
    sites: list[tuple[str, int]] = []
    for path in sorted((root / "gateway").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        visitor = _Sites(rel)
        visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=rel))
        sites.extend(visitor.sites)
    return sites


def load_allowlist(path: Path = ALLOWLIST) -> dict[str, str]:
    """``site-id  # reason`` per line; blank lines and ``#`` comment lines are ignored."""
    entries: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        site, sep, reason = line.partition("  # ")
        assert sep and reason.strip(), f"allowlist entry without a reason: {raw!r}"
        entries[site.strip()] = reason.strip()
    return entries


def lint(root: Path, allow: dict[str, str]) -> tuple[list[str], list[str]]:
    sites = find_env_write_sites(root)
    seen = {site for site, _line in sites}
    unlisted = sorted(f"{site} (line {line})" for site, line in sites if site not in allow)
    stale = sorted(set(allow) - seen)
    return unlisted, stale


def test_ac17_every_gateway_env_write_is_allowlisted():
    unlisted, stale = lint(REPO, load_allowlist())
    assert not unlisted, (
        "os.environ writes in gateway/ outside hermes_cli/process_bootstrap.py and "
        f"tests/gateway/env_write_allowlist.txt (spec §5.6, AC-17): {unlisted}")
    assert not stale, f"allowlist rows with no matching write site (delete them): {stale}"


def test_ac17_lint_catches_a_new_write_in_a_scratch_copy(tmp_path):
    """Mutation: a fixture ``os.environ["X"] = "1"`` in a scratch copy of gateway/ → RED."""
    shutil.copytree(REPO / "gateway", tmp_path / "gateway",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    clean_unlisted, _ = lint(tmp_path, load_allowlist())
    assert clean_unlisted == []
    target = tmp_path / "gateway" / "status.py"
    target.write_text(target.read_text(encoding="utf-8") + '\nos.environ["X"] = "1"\n',
                      encoding="utf-8")
    unlisted, _ = lint(tmp_path, load_allowlist())
    assert unlisted == ["gateway/status.py::<module>::set 'X' (line %d)"
                        % len(target.read_text(encoding="utf-8").splitlines())]


@pytest.mark.parametrize("snippet,site", [
    ('os.environ.setdefault("A", "1")', "setdefault 'A'"),
    ('os.environ.update({"A": "1"})', "update {'A': '1'}"),
    ('os.environ.pop("A", None)', "pop 'A'"),
    ('del os.environ["A"]', "del 'A'"),
    ('os.putenv("A", "1")', "putenv 'A'"),
    ('os.environ["A"] += "x"', "set 'A'"),
])
def test_ac17_lint_shapes(tmp_path, snippet, site):
    (tmp_path / "gateway").mkdir()
    (tmp_path / "gateway" / "m.py").write_text(f"import os\ndef f():\n    {snippet}\n")
    assert find_env_write_sites(tmp_path) == [(f"gateway/m.py::f::{site}", 3)]


def test_ac17_lint_ignores_reads(tmp_path):
    (tmp_path / "gateway").mkdir()
    (tmp_path / "gateway" / "m.py").write_text(
        "import os\nx = os.environ['A']\ny = os.environ.get('B')\nenv = dict(os.environ)\nenv['C'] = '1'\n")
    assert find_env_write_sites(tmp_path) == []


def test_ac17_bootstrap_is_outside_the_linted_tree():
    assert (REPO / "hermes_cli" / "process_bootstrap.py").is_file()
    assert not any(site.startswith("hermes_cli/") for site, _ in find_env_write_sites(REPO))


# --------------------------------------------------------------------------- AC-7 parity

_DOTENV = "S4_FIXTURE_SECRET=from-dotenv\nHERMES_MAX_ITERATIONS=60\n"
_CONFIG = textwrap.dedent("""\
    S4_TOPLEVEL_SCALAR: top
    require_mention: true
    timezone: America/Los_Angeles
    agent:
      max_turns: 321
      gateway_timeout: 1234
      restart_loop_threshold: 7
    sessions:
      search_slow_ms: 777
    display:
      busy_input_mode: queue
      busy_steer_ack_enabled: false
    security:
      redact_secrets: false
    terminal:
      backend: local
      timeout: 99
    auxiliary:
      vision:
        model: s4-vision-model
    gateway:
      trust_recent_files_seconds: 42
      platform_connect_timeout: 17
""")
# Keys the fixture causes, plus the unconditional start-path writes.
_EXPECTED = {
    "S4_FIXTURE_SECRET": "from-dotenv",
    "S4_TOPLEVEL_SCALAR": "top",
    "HERMES_MAX_ITERATIONS": "321",
    "HERMES_AGENT_TIMEOUT": "1234",
    "HERMES_RESTART_LOOP_THRESHOLD": "7",
    "HERMES_SEARCH_SLOW_MS": "777",
    "HERMES_GATEWAY_BUSY_INPUT_MODE": "queue",
    "HERMES_GATEWAY_BUSY_STEER_ACK_ENABLED": "False",
    "HERMES_TIMEZONE": "America/Los_Angeles",
    "HERMES_REDACT_SECRETS": "false",
    "TERMINAL_ENV": "local",
    "TERMINAL_TIMEOUT": "99",
    "AUXILIARY_VISION_MODEL": "s4-vision-model",
    "HERMES_MEDIA_TRUST_RECENT_SECONDS": "42",
    "HERMES_GATEWAY_PLATFORM_CONNECT_TIMEOUT": "17",
    "HERMES_QUIET": "1",
    "HERMES_EXEC_ASK": "1",
    "TELEGRAM_REQUIRE_MENTION": "true",
}
_PRESENT = {"HERMES_TURN_LEASE_TIMEOUT", "TERMINAL_CWD"}

_PROBE_SCRIPT = "import json, os\nprint(json.dumps(dict(os.environ), sort_keys=True))\n"

_GATEWAY_START = textwrap.dedent("""\
    import asyncio, json, os, sys
    import gateway.run as gw
    import gateway.code_skew as cs
    cs.record_boot_fingerprint = lambda *a, **k: None
    class _Stop(Exception):
        pass
    async def _stop(*a, **k):
        raise _Stop()
    gw._host_attach_or_none = _stop
    try:
        asyncio.run(gw.start_gateway())
    except _Stop:
        pass
    gw.load_gateway_config_for_runner()  # what GatewayRunner.__init__ runs next
    parent = dict(os.environ)
    from cron.scheduler_script import _run_job_script
    ok, out = _run_job_script("probe.py")
    assert ok, out
    print("S4-RESULT " + json.dumps({"parent": parent, "child": json.loads(out)}))
""")

_RUNNER_START = textwrap.dedent("""\
    import json, os, sys
    from cron import script_runner
    script_runner._bootstrap_env()
    parent = dict(os.environ)
    from cron.scheduler_script import _run_job_script
    ok, out = _run_job_script("probe.py")
    assert ok, out
    print("S4-RESULT " + json.dumps({"parent": parent, "child": json.loads(out),
                                    "gateway_imported": "gateway.run" in sys.modules}))
""")


def _scratch_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / "scripts").mkdir(parents=True)
    (home / "cron").mkdir()
    (home / ".env").write_text(_DOTENV, encoding="utf-8")
    (home / "config.yaml").write_text(_CONFIG, encoding="utf-8")
    (home / "scripts" / "probe.py").write_text(_PROBE_SCRIPT, encoding="utf-8")
    return home


def _base_env(home: Path) -> dict:
    keep = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT", "USER")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.update({"HERMES_HOME": str(home), "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1"})
    return env


def _start(code: str, home: Path) -> dict:
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(home), env=_base_env(home),
                          capture_output=True, text=True, timeout=240)
    lines = [l for l in proc.stdout.splitlines() if l.startswith("S4-RESULT ")]
    assert proc.returncode == 0 and lines, (proc.returncode, proc.stdout[-2000:], proc.stderr[-4000:])
    return json.loads(lines[-1][len("S4-RESULT "):])


@pytest.fixture(scope="module")
def starts(tmp_path_factory):
    home = _scratch_home(tmp_path_factory.mktemp("s4"))
    return {"gateway": _start(_GATEWAY_START, home), "runner": _start(_RUNNER_START, home)}


def test_ac7_both_start_paths_carry_every_fixture_key(starts):
    for role in ("gateway", "runner"):
        parent = starts[role]["parent"]
        wrong = {k: (parent.get(k), v) for k, v in _EXPECTED.items() if parent.get(k) != v}
        assert not wrong, f"{role} start-path os.environ: (got, want) {wrong}"
        missing = sorted(k for k in _PRESENT if not parent.get(k))
        assert not missing, f"{role} start-path os.environ lacks {missing}"


def test_ac7_runner_and_gateway_agree_on_every_child_visible_key(starts):
    from hermes_cli.process_bootstrap import GATEWAY_CHILD_VISIBLE_ENV

    assert set(_EXPECTED) - {"S4_FIXTURE_SECRET", "S4_TOPLEVEL_SCALAR",
                             "AUXILIARY_VISION_MODEL"} <= GATEWAY_CHILD_VISIBLE_ENV
    gw, rn = starts["gateway"]["parent"], starts["runner"]["parent"]
    keys = (GATEWAY_CHILD_VISIBLE_ENV | set(_EXPECTED)) - {"_HERMES_GATEWAY"}
    diff = {k: (gw.get(k), rn.get(k)) for k in sorted(keys) if gw.get(k) != rn.get(k)}
    assert not diff, f"(gateway, runner) differ: {diff}"


def test_hermes_gateway_marker_is_child_only_in_the_runner(starts):
    assert starts["gateway"]["parent"].get("_HERMES_GATEWAY") == "1"
    assert "_HERMES_GATEWAY" not in starts["runner"]["parent"]
    assert starts["runner"]["gateway_imported"] is False
    assert starts["runner"]["child"].get("_HERMES_GATEWAY") == "1"
    assert starts["gateway"]["child"].get("_HERMES_GATEWAY") == "1"


def test_i5_script_child_env_is_identical(starts):
    gw, rn = starts["gateway"]["child"], starts["runner"]["child"]
    keys = {k for k in set(gw) | set(rn) if not k.startswith("XPC_")}
    diff = {k: (gw.get(k), rn.get(k)) for k in sorted(keys) if gw.get(k) != rn.get(k)}
    assert not diff, f"script child env (gateway, runner) differ: {diff}"


def test_child_marker_hook_is_inert_without_the_runner_role(tmp_path, monkeypatch):
    """In-process (no bootstrap ran): the hook adds nothing, so the gateway path stays inheritance."""
    from hermes_cli import process_bootstrap

    monkeypatch.setattr(process_bootstrap, "_ROLE", None)
    env: dict = {}
    process_bootstrap.apply_child_env(env)
    assert env == {}
    monkeypatch.setattr(process_bootstrap, "_ROLE", "runner")
    process_bootstrap.apply_child_env(env)
    assert env == {"_HERMES_GATEWAY": "1"}
