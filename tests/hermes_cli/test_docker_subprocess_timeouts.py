"""Every subprocess call in tests/hermes_cli/ that runs `docker` carries a timeout.

On ci-box 2026-11-03 an untimed `docker rm -f` cleanup in test_kanban_resources.py outlived its
pytest under a stalled daemon (load 300-400, 240 containers) and spun at ~8 cores for 7h03m,
orphaned to PID 1 (t_b37774b1). A docker client call without `timeout=` can hang forever.
"""
from __future__ import annotations

import ast
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_FUNCS = {"run", "call", "check_call", "check_output"}


def _docker_calls_without_timeout(source: str, filename: str) -> list[str]:
    out = []
    for node in ast.walk(ast.parse(source, filename)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in _FUNCS and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess" and node.args):
            continue
        argv = node.args[0]
        if not (isinstance(argv, (ast.List, ast.Tuple)) and argv.elts
                and isinstance(argv.elts[0], ast.Constant) and argv.elts[0].value == "docker"):
            continue
        if not any(kw.arg == "timeout" for kw in node.keywords):
            out.append(f"{filename}:{node.lineno}")
    return out


def test_every_docker_subprocess_call_has_a_timeout():
    bad = []
    for path in sorted(_DIR.rglob("*.py")):
        bad += _docker_calls_without_timeout(path.read_text(encoding="utf-8"), str(path.relative_to(_DIR)))
    assert bad == [], f"docker subprocess calls with no timeout= (can hang forever on a stalled daemon): {bad}"


def test_the_scan_catches_an_untimed_docker_call():
    src = (
        "import subprocess\n"
        "subprocess.run(['docker', 'rm', '-f', 'x'], capture_output=True)\n"
        "subprocess.run(['docker', 'info'], capture_output=True,\n"
        "               timeout=20)\n"
        "subprocess.run(['git', 'status'])\n"
    )
    assert _docker_calls_without_timeout(src, "f.py") == ["f.py:2"]
