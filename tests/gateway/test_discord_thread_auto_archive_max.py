"""Guard: every thread Hermes creates is stamped at Discord's MAXIMUM auto_archive_duration.

Ace, 2026-09-18 (voice): the idle-thread auto-archiver was retired — he does not mind
threads staying open indefinitely, and they cost no tokens or resources.

Two DIFFERENT mechanisms were reaping threads, and only one of them was a cron:

1. ``~/.hermes/scripts/discord-thread-hygiene.py`` — our own weekly sweep (cron
   ``d4583fad95bc``, now disabled + tripwired). Not covered by this test.
2. ``auto_archive_duration`` — Discord-NATIVE, stamped by the bot on every thread it
   creates. Per Discord's own docs this field is deprecated as an archiver and now only
   controls how long a thread stays in the channel LIST, but it is still the value the
   bot writes on every thread, and leaving it at 1440 (24h) kept sweeping our threads
   out of the channel list a day after creation.

This is a SOURCE-CONTRACT test: it fails on any new call site that hardcodes a
non-maximum duration, rather than only checking the handful that existed at
retirement time. A lexical default check would pass while a fresh
``create_thread(..., auto_archive_duration=1440)`` slipped in next to it.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# Discord's documented ceiling, in minutes (7 days).
DISCORD_MAX_AUTO_ARCHIVE_MINUTES = 10080

SOURCES = (
    REPO_ROOT / "plugins" / "platforms" / "discord" / "adapter.py",
    REPO_ROOT / "tools" / "discord_tool.py",
)


def _parse(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def _is_int_const(node: ast.AST) -> bool:
    """True for an integer literal only.

    ``auto_archive_duration`` also appears as a STRING under
    ``@app_commands.describe(...)`` (the human-readable slash-command help). That is
    documentation, not a stamp, so only integer literals are candidates for the guard.
    """
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int)
        and not isinstance(node.value, bool)
    )


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_named_max_constant_is_discord_ceiling(path: pathlib.Path) -> None:
    """The module defines the named default, and it equals Discord's max."""
    tree = _parse(path)
    found: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == "DEFAULT_THREAD_AUTO_ARCHIVE_MINUTES"
                    and isinstance(node.value, ast.Constant)
                ):
                    found[target.id] = node.value.value

    assert found, (
        f"{path.name} must define a module-level DEFAULT_THREAD_AUTO_ARCHIVE_MINUTES "
        "so thread-creation call sites cannot drift back to a hardcoded value."
    )
    assert found["DEFAULT_THREAD_AUTO_ARCHIVE_MINUTES"] == DISCORD_MAX_AUTO_ARCHIVE_MINUTES, (
        "Threads must be stamped at Discord's ceiling (10080 = 7 days). "
        f"{path.name} declares {found['DEFAULT_THREAD_AUTO_ARCHIVE_MINUTES']}."
    )


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_no_call_site_hardcodes_a_non_max_duration(path: pathlib.Path) -> None:
    """No ``auto_archive_duration=<int>`` keyword argument below the ceiling.

    Covers BOTH shapes that stamp a thread:
      * function/method *defaults*  (``def f(..., auto_archive_duration: int = N)``)
      * *call* keyword arguments    (``create_thread(..., auto_archive_duration=N)``)
    """
    tree = _parse(path)
    offenders: list[str] = []

    for node in ast.walk(tree):
        # Call keyword arguments: create_thread(..., auto_archive_duration=1440)
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "auto_archive_duration" and _is_int_const(kw.value):
                    if kw.value.value != DISCORD_MAX_AUTO_ARCHIVE_MINUTES:
                        offenders.append(
                            f"{path.name}:{kw.value.lineno} call kwarg ="
                            f" {kw.value.value!r}"
                        )

        # Signature defaults: def f(..., auto_archive_duration: int = 1440)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            positional = args.posonlyargs + args.args
            paired = list(zip(positional[len(positional) - len(args.defaults):], args.defaults))
            paired += [
                (a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None
            ]
            for arg, default in paired:
                if arg.arg == "auto_archive_duration" and _is_int_const(default):
                    if default.value != DISCORD_MAX_AUTO_ARCHIVE_MINUTES:
                        offenders.append(
                            f"{path.name}:{default.lineno} default in"
                            f" {node.name}() = {default.value!r}"
                        )

    assert not offenders, (
        "Thread auto-archive was RETIRED (Ace, 2026-09-18) — threads must be stamped at "
        f"Discord's max ({DISCORD_MAX_AUTO_ARCHIVE_MINUTES}). Use "
        "DEFAULT_THREAD_AUTO_ARCHIVE_MINUTES instead of a literal. Offending sites:\n  "
        + "\n  ".join(offenders)
    )


def test_guard_would_catch_a_regression() -> None:
    """Mutation proof: the AST check actually fires on a reintroduced 1440 stamp.

    Without this, a guard that silently matched nothing would still be green.
    """
    bad = ast.parse(
        "async def go():\n"
        "    await chan.create_thread(name='x', auto_archive_duration=1440)\n"
    )
    hits = [
        kw
        for node in ast.walk(bad)
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "auto_archive_duration"
        and _is_int_const(kw.value)
        and kw.value.value != DISCORD_MAX_AUTO_ARCHIVE_MINUTES
    ]
    assert len(hits) == 1, "the AST predicate must flag a hardcoded 1440 stamp"
