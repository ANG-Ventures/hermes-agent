"""Fork canary: the fork-only / fork-load-bearing slash commands stay registered.

Surface: CLI + gateway command registry (``hermes_cli/commands.py``), which is
the single source of truth every consumer derives from (``/help``, CLI
autocomplete, the Telegram command menu, the Slack mapping, and the gateway
dispatcher). A parity merge that resolves ``commands.py`` in upstream's favour
silently deletes a fork command from EVERY surface at once, with no import
error and no test failure anywhere else in the suite — the command simply stops
existing.

Covered commands and why each is fork-load-bearing:

* ``/undo`` + ``/redo`` — half-turn rewind with a redo branch. The fork ships
  ``hermes_undo.py`` + ``tests/test_undo_redo_stack.py``; the registry rows are
  the only thing that routes a user's ``/undo`` to it.
* ``/branch`` (alias ``/fork``) — session branching; on Discord it spawns a
  context-inheriting thread (``tests/gateway/test_discord_branch_thread_merge.py``).
* ``/merge`` — folds a branch summary back into the parent session. It is
  ``gateway_only=True``: that flag is the contract that keeps it OUT of the CLI
  surface, so both its presence AND its gating are asserted.
* ``/fast`` — priority/fast-processing toggle.

These are contract assertions (name + category + flags + aliases), not a
snapshot of the whole registry, so adding unrelated upstream commands never
makes this file red.
"""

import pytest


def _registry():
    from hermes_cli.commands import COMMAND_REGISTRY

    return {c.name: c for c in COMMAND_REGISTRY}


# --------------------------------------------------------------------------- #
# Presence
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "name,category",
    [
        ("undo", "Session"),
        ("redo", "Session"),
        ("branch", "Session"),
        ("merge", "Session"),
        ("fast", "Configuration"),
    ],
)
def test_fork_slash_command_is_registered(name, category):
    """RED-PROVABLE: delete the ``CommandDef("<name>", ...)`` row from
    ``COMMAND_REGISTRY`` in hermes_cli/commands.py (e.g. line ~123 for "undo",
    ~131 for "branch", ~133 for "merge", ~242 for "fast") and this parametrized
    case fails on the missing key."""
    reg = _registry()
    assert name in reg, (
        f"/{name} disappeared from COMMAND_REGISTRY — every consumer "
        f"(help, autocomplete, Telegram menu, Slack map, gateway dispatch) "
        f"derives from this registry, so the command is gone from all surfaces."
    )
    assert reg[name].category == category, (
        f"/{name} moved out of the {category!r} category; the category drives "
        f"grouping in /help and the paged /commands browser."
    )


# --------------------------------------------------------------------------- #
# Per-command contracts
# --------------------------------------------------------------------------- #

def test_undo_and_redo_accept_a_count_argument():
    """Both take an optional N (half-turns). Losing the args_hint means the
    completer stops offering it and /help stops documenting it.

    RED-PROVABLE: remove ``args_hint="[N]"`` from the ``undo`` (or ``redo``)
    CommandDef in hermes_cli/commands.py (~L124 / ~L126)."""
    reg = _registry()
    for name in ("undo", "redo"):
        assert "N" in reg[name].args_hint, (
            f"/{name} lost its [N] count argument hint"
        )


def test_branch_keeps_the_fork_alias():
    """``/fork`` is the documented alias for ``/branch``.

    RED-PROVABLE: drop ``aliases=("fork",)`` from the ``branch`` CommandDef in
    hermes_cli/commands.py (~L132)."""
    reg = _registry()
    assert "fork" in reg["branch"].aliases, (
        "/fork alias for /branch was dropped — documented in the slash-command "
        "reference and used by existing muscle memory."
    )


def test_merge_is_gateway_only_and_branch_is_not():
    """``/merge`` folds a *messaging* branch (e.g. a Discord thread) back into
    its parent, so it is deliberately gateway-only; ``/branch`` works on both
    surfaces. This asymmetry is the contract.

    RED-PROVABLE: flip ``gateway_only=True`` to ``False`` (or delete it) on the
    ``merge`` CommandDef in hermes_cli/commands.py (~L134) — the first assert
    fails. Adding ``gateway_only=True`` to ``branch`` fails the second."""
    reg = _registry()
    assert reg["merge"].gateway_only is True, (
        "/merge stopped being gateway_only — it would start appearing in the "
        "CLI surface where it has no branch/parent thread to fold into."
    )
    assert reg["branch"].gateway_only is False, (
        "/branch became gateway_only — it must stay available in the CLI "
        "(tests/cli/test_branch_command.py covers the CLI path)."
    )


def test_fast_exposes_its_mode_subcommands():
    """``/fast`` is a tri-state toggle (normal|fast|status) plus a --global
    scope flag; the subcommands tuple is what makes them tab-completable.

    RED-PROVABLE: empty the ``subcommands=(...)`` tuple on the ``fast``
    CommandDef in hermes_cli/commands.py (~L244)."""
    subs = set(_registry()["fast"].subcommands)
    assert {"normal", "fast", "status"} <= subs, (
        f"/fast lost its mode subcommands; got {sorted(subs)}"
    )
    assert "--global" in subs, "/fast lost its --global persistence scope flag"


def test_no_fork_command_collides_with_an_alias():
    """Registry integrity: a merge that re-adds an upstream command whose name
    equals one of our aliases (or vice versa) makes dispatch ambiguous and one
    of the two silently unreachable.

    RED-PROVABLE: add ``aliases=("undo",)`` to any other CommandDef in
    hermes_cli/commands.py — the collision assert fires."""
    from hermes_cli.commands import COMMAND_REGISTRY

    names = {c.name for c in COMMAND_REGISTRY}
    for cmd in COMMAND_REGISTRY:
        for alias in cmd.aliases:
            assert alias not in names, (
                f"alias {alias!r} on /{cmd.name} collides with the real "
                f"command /{alias} — one of them becomes unreachable."
            )
