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
retirement time.

DISCOVERY IS INVERTED (round 2, card t_b0ad6792). The first cut pinned ``SOURCES`` to a
hardcoded 2-tuple of the files that happened to stamp threads on 2026-09-18. A NEW module
dropped into ``plugins/platforms/discord/`` was invisible to it BY CONSTRUCTION — proven
with a planted probe that both guards passed green. The file set is now WALKED from the
repo root, so a producer added anywhere in the tree is covered without anyone remembering
to list it.

ALL THREE STAMP IDIOMS are matched. The first cut's fleet-side sweep grepped only the
dict form ``{"auto_archive_duration": N}`` and therefore missed the kwarg form
``create_thread(..., auto_archive_duration=N)`` — which is the idiom the two biggest
producers actually use:

  * call keyword      ``create_thread(..., auto_archive_duration=1440)``
  * signature default ``def f(..., auto_archive_duration: int = 1440)``
  * dict literal      ``body={"auto_archive_duration": 1440}``

DOES NOT COVER (stated boundary, not a hidden gap — see ``test_documented_boundary``):
  * a duration computed at runtime (``auto_archive_duration=60 * 24``) or read from
    config — no literal to inspect. Nothing in the tree does this.
  * ``VALID_THREAD_AUTO_ARCHIVE_MINUTES = {60, 1440, 4320, 10080}`` — a VALIDATION set
    enumerating what Discord accepts, not a stamp. Membership literals are out of class.
  * ``tests/`` — test callers legitimately pass an explicit 1440 to exercise the
    ``/thread`` override path. The contract is on what the bot stamps BY DEFAULT.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# Discord's documented ceiling, in minutes (7 days).
DISCORD_MAX_AUTO_ARCHIVE_MINUTES = 10080

FIELD = "auto_archive_duration"

# Directories that never hold a production stamp. `tests` is excluded by design: a test
# may legitimately pass an explicit non-max duration to exercise the override path.
_SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "site-packages",
    "build",
    "dist",
    "tests",
}


def _discover_sources() -> list[pathlib.Path]:
    """Every production .py file in the repo — WALKED, never hardcoded.

    A hardcoded list is an allowlist wearing a wildcard's clothes: the module nobody
    thought to add is green by construction. That is the exact regression this round
    was opened for.
    """
    out: list[pathlib.Path] = []
    for path in REPO_ROOT.rglob("*.py"):
        if _SKIP_DIRS & set(path.relative_to(REPO_ROOT).parts):
            continue
        out.append(path)
    return sorted(out)


def _int_const(node: ast.AST | None) -> int | None:
    """Return the integer literal value of ``node``, else ``None``.

    ``auto_archive_duration`` also appears as a STRING under
    ``@app_commands.describe(...)`` (the human-readable slash-command help). That is
    documentation, not a stamp, so only integer literals are candidates for the guard.
    """
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int)
        and not isinstance(node.value, bool)
    ):
        return node.value
    return None


def _is_str_const(node: ast.AST | None, value: str) -> bool:
    return isinstance(node, ast.Constant) and node.value == value


def offending_stamps(source: str, label: str) -> list[str]:
    """Return every site in ``source`` stamping a non-max ``auto_archive_duration``.

    Shared predicate for all three idioms. Exposed as a module-level function so the
    mutation-proof tests exercise the same code path the real scan uses, rather than a
    re-implementation that can drift from it.
    """
    tree = ast.parse(source, filename=label)
    offenders: list[str] = []

    for node in ast.walk(tree):
        # 1. Call keyword: create_thread(..., auto_archive_duration=1440)
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg != FIELD:
                    continue
                value = _int_const(kw.value)
                if value is not None and value != DISCORD_MAX_AUTO_ARCHIVE_MINUTES:
                    offenders.append(
                        f"{label}:{kw.value.lineno} call kwarg = {value!r}"
                    )

        # 2. Dict literal: body={"auto_archive_duration": 1440}
        if isinstance(node, ast.Dict):
            for key, val_node in zip(node.keys, node.values):
                if not _is_str_const(key, FIELD):
                    continue
                value = _int_const(val_node)
                if value is not None and value != DISCORD_MAX_AUTO_ARCHIVE_MINUTES:
                    offenders.append(
                        f"{label}:{val_node.lineno} dict literal = {value!r}"
                    )

        # 3. Signature default: def f(..., auto_archive_duration: int = 1440)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            positional = args.posonlyargs + args.args
            paired = list(
                zip(positional[len(positional) - len(args.defaults) :], args.defaults)
            )
            paired += [
                (a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None
            ]
            for arg, default in paired:
                if arg.arg != FIELD:
                    continue
                value = _int_const(default)
                if value is not None and value != DISCORD_MAX_AUTO_ARCHIVE_MINUTES:
                    offenders.append(
                        f"{label}:{default.lineno} default in"
                        f" {node.name}() = {value!r}"
                    )

    return offenders


# The modules that stamp threads today. Asserted POSITIVELY below: "still clean" is
# indistinguishable from "clean because the walk found nothing".
_KNOWN_STAMP_MODULES = (
    "plugins/platforms/discord/adapter.py",
    "tools/discord_tool.py",
)


def test_discovery_actually_covers_the_known_producers() -> None:
    """Non-vacuity floor: the walk must reach the files it exists to guard.

    Without this, a broken skip-rule or a moved root silently empties the covered set
    and every other test in this module passes on nothing.
    """
    discovered = {
        str(p.relative_to(REPO_ROOT)) for p in _discover_sources()
    }
    missing = [m for m in _KNOWN_STAMP_MODULES if m not in discovered]
    assert not missing, (
        "source discovery no longer reaches the known thread-stamp modules "
        f"{missing} — the guard is scanning the wrong tree and is vacuously green."
    )
    assert len(discovered) > 100, (
        f"source discovery found only {len(discovered)} modules; the repo has "
        "thousands. The walk is broken and this guard covers almost nothing."
    )


def test_named_max_constant_is_discord_ceiling() -> None:
    """Each stamp module defines the named default, and it equals Discord's max."""
    for rel in _KNOWN_STAMP_MODULES:
        path = REPO_ROOT / rel
        tree = ast.parse(path.read_text(), filename=str(path))
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
            f"{rel} must define a module-level DEFAULT_THREAD_AUTO_ARCHIVE_MINUTES "
            "so thread-creation call sites cannot drift back to a hardcoded value."
        )
        assert (
            found["DEFAULT_THREAD_AUTO_ARCHIVE_MINUTES"]
            == DISCORD_MAX_AUTO_ARCHIVE_MINUTES
        ), (
            "Threads must be stamped at Discord's ceiling (10080 = 7 days). "
            f"{rel} declares {found['DEFAULT_THREAD_AUTO_ARCHIVE_MINUTES']}."
        )


def test_no_module_anywhere_stamps_a_non_max_duration() -> None:
    """No production module may stamp a below-ceiling ``auto_archive_duration``.

    Scans the DISCOVERED file set, so a producer added in a new module — the regression
    that got past round 1 — is covered without editing this test.
    """
    offenders: list[str] = []
    for path in _discover_sources():
        rel = str(path.relative_to(REPO_ROOT))
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            offenders.extend(offending_stamps(source, rel))
        except SyntaxError:
            # A module that does not parse under this interpreter cannot stamp a
            # thread at runtime either; skip rather than fail the retirement guard.
            continue

    assert not offenders, (
        "Thread auto-archive was RETIRED (Ace, 2026-09-18) — threads must be stamped at "
        f"Discord's max ({DISCORD_MAX_AUTO_ARCHIVE_MINUTES}). Use "
        "DEFAULT_THREAD_AUTO_ARCHIVE_MINUTES instead of a literal. Offending sites:\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.parametrize(
    "idiom,source",
    [
        (
            "call kwarg",
            "async def go(chan):\n"
            "    await chan.create_thread(name='x', auto_archive_duration=1440)\n",
        ),
        (
            "dict literal",
            "def go():\n"
            "    return _api('POST', '/threads', {'auto_archive_duration': 1440})\n",
        ),
        (
            "signature default",
            "def make(name, auto_archive_duration: int = 1440):\n    return name\n",
        ),
    ],
)
def test_guard_catches_every_stamp_idiom(idiom: str, source: str) -> None:
    """Mutation proof, one arm per idiom the codebase actually uses.

    Round 1 shipped a fleet-side sweep that matched only the dict form and therefore
    ignored the kwarg form the two biggest producers use. Each idiom now has its own
    arm so a predicate that silently stops matching one of them turns this red.
    """
    hits = offending_stamps(source, f"<{idiom}>")
    assert len(hits) == 1, f"the guard must flag a hardcoded 1440 stamp in the {idiom} form"


def test_guard_accepts_the_ceiling() -> None:
    """Control arm: the max value must NOT be reported.

    A predicate that flags everything would pass every mutation arm above while being
    useless — this is what separates teeth from noise.
    """
    clean = (
        "async def go(chan):\n"
        "    await chan.create_thread(name='x', auto_archive_duration=10080)\n"
        "    return {'auto_archive_duration': 10080}\n"
    )
    assert offending_stamps(clean, "<clean>") == []


def test_documented_boundary_is_out_of_class_by_design() -> None:
    """The stated DOES-NOT-COVER set, pinned so the boundary is honest, not hidden.

    These shapes are deliberately NOT offenders. If a future edit makes one of them
    trip the guard, this test goes red and forces the boundary to be re-decided
    explicitly rather than discovered as a false positive in CI.
    """
    # A validation set enumerating what Discord accepts is not a stamp.
    assert offending_stamps(
        "VALID_THREAD_AUTO_ARCHIVE_MINUTES = {60, 1440, 4320, 10080}\n", "<enum>"
    ) == []
    # Slash-command help text is documentation, not a stamp.
    assert offending_stamps(
        "@describe(auto_archive_duration='Minutes (60, 1440, 4320, 10080)')\n"
        "def cmd():\n    pass\n",
        "<describe>",
    ) == []
    # A computed duration has no literal to inspect — documented as uncovered.
    assert offending_stamps(
        "def go(chan):\n"
        "    return chan.create_thread(auto_archive_duration=60 * 24)\n",
        "<computed>",
    ) == []
    # The docstring must keep carrying the boundary; deleting it makes this red.
    assert "DOES NOT COVER" in (__doc__ or "")
