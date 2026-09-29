"""Callee-resolution precision of the loop-reachability walker (C5 #35-#39).

Each arm builds a tiny tree and asks ``find_onloop_sink_sites`` whether a
coroutine reaches the sink ``do_net``. See ``_loop_atomic_write_reachability``.
"""

from __future__ import annotations

from pathlib import Path

from tests.gateway._loop_atomic_write_reachability import find_onloop_sink_sites


def _sites(tmp_path: Path, files: dict, **kw) -> list[str]:
    for rel, src in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(src, encoding="utf-8")
    (tmp_path / "pkg" / "__init__.py").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    mods = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*.py"))
    kw.setdefault("sink_names", {"do_net"})
    kw.setdefault("sink_dotted", ())
    return find_onloop_sink_sites(tmp_path, mods, **kw)


_NET = {
    "pkg/net.py": "def helper(x):\n    do_net()\n",
    "pkg/other.py": "def helper(x):\n    return x\n",
}


def test_function_local_import_shadows_same_file_def(tmp_path):
    """#39: the local import binds ``helper``; the same-file def is a fallback."""
    files = dict(_NET)
    files["m.py"] = (
        "def helper(x):\n    return x\n\n"
        "async def f():\n"
        "    from pkg.net import helper\n"
        "    helper(1)\n"
    )
    assert _sites(tmp_path, files) == ["m.py f -> do_net via f>helper"]


def test_function_local_import_does_not_leak_to_sibling(tmp_path):
    """#37: ``a``'s lazy import does not bind ``helper`` inside ``f``."""
    files = dict(_NET)
    files["m.py"] = (
        "def a():\n"
        "    from pkg.net import helper\n"
        "    return helper\n\n"
        "async def f():\n"
        "    helper(1)\n"
    )
    assert _sites(tmp_path, files) == []


def test_module_level_import_still_resolves(tmp_path):
    """Control for #37: a module-level import binds in every function."""
    files = dict(_NET)
    files["m.py"] = "from pkg.net import helper\n\nasync def f():\n    helper(1)\n"
    assert _sites(tmp_path, files) == ["m.py f -> do_net via f>helper"]


def test_sink_dotted_alone_is_honoured(tmp_path):
    """#38: ``sink_dotted`` without ``sink_names`` must not fall back to defaults."""
    files = {"m.py": "import requests\n\nasync def f():\n    requests.get('u')\n"}
    got = _sites(tmp_path, files, sink_names=None, sink_dotted={"requests.get"})
    assert got == ["m.py f -> requests.get via f"]


def test_eager_offload_argument_is_attributed(tmp_path):
    """#35 sibling: a call inside offload args runs on the loop; a lambda does not."""
    files = {
        "m.py": (
            "import asyncio\n\n"
            "async def eager():\n"
            "    await asyncio.to_thread(print, do_net())\n\n"
            "async def deferred():\n"
            "    await asyncio.to_thread(lambda: do_net())\n"
        )
    }
    assert _sites(tmp_path, files) == ["m.py eager -> do_net via eager"]
