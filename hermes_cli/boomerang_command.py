"""Shared ``/boomerang`` argument parsing and seed building (CLI + gateway).

Entry-point contract (docs/boomerang-v2/README.md §1): ``/boomerang <task>`` requests
``inherit_mode="fork"`` (D-1); ``--summary`` selects the v1 fold; ``--fork`` and ``--branch``
are explicit aliases of the default (D-16). Eligibility still decides at dispatch time.
"""
from __future__ import annotations

from typing import Callable, Optional, Tuple

USAGE = (
    "Usage: /boomerang [--summary|--fork|--branch] <task>\n"
    "Runs the task autonomously in an isolated subagent that inherits this session's context; "
    "a summary returns here when it finishes. Default inherits as a fork (warm cache, falls back "
    "to summary when ineligible); --summary uses the compressed-context fold."
)

_MODE_FLAGS = {"--summary": "summary", "--fork": "fork", "--branch": "fork"}


def parse_boomerang_args(raw: str) -> Tuple[str, str]:
    """Return ``(inherit_mode, task)``. Leading mode flags are consumed; ``task`` may be empty.

    Raises ``ValueError`` when flags request both ``summary`` and ``fork``.
    """
    modes = set()
    rest = (raw or "").strip()
    while rest:
        head, _, tail = rest.partition(" ")
        mode = _MODE_FLAGS.get(head.lower())
        if mode is None:
            break
        modes.add(mode)
        rest = tail.strip()
    if len(modes) > 1:
        raise ValueError("/boomerang: --summary conflicts with --fork/--branch; pick one.")
    return (modes.pop() if modes else "fork"), rest


def build_boomerang_seed(
    mode: str, task: str, skill_message: Callable[[str], Optional[str]],
) -> str:
    """The user-turn text that drives the boomerang. ``skill_message(instruction)`` renders the
    boomerang skill invocation (None when the skill is not installed)."""
    directive = (
        f'Dispatch with delegate_task(inherit_context=true, inherit_mode="{mode}").'
    )
    msg = skill_message(f"{task}\n\n{directive}")
    if msg:
        return msg
    return (
        f"Boomerang this task: {task}\n\n"
        f'Use delegate_task with inherit_context=true and inherit_mode="{mode}" so the subagent '
        "inherits this conversation's context. Instruct it to execute autonomously (make "
        "reasonable assumptions, do NOT stop to ask; if blocked, complete what you can and report "
        "the blocker), and to end with a structured summary (Outcome / Changed / "
        "Commands+validation / Blockers). Its summary will re-enter this session when it "
        "finishes — acknowledge the dispatch and continue."
    )
