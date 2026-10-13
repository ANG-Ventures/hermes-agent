"""Shared ``/queue`` verb parser — the ONE table for the CLI and the gateway.

The CLI (``hermes_cli/cli_loops_mixin.py``) and every gateway surface (``gateway/run_busy.py``,
``gateway/run_inbound.py``) parse ``/queue <payload>`` identically, so a prompt that is a management
verb on one surface can never be enqueued as a prompt on another (duplicated-normalizer drift).

Arity rule (unchanged from the CLI, upstream ce318290bdd): a leading verb is management ONLY when
its arguments fit — ``list``/``clear``/``run`` stand alone; ``edit N …``/``rm N``/``move A B`` need a
leading number. Anything else (``clear the cache``, ``run the tests``, ``rm``) is a prompt.
``add <prompt>`` forces enqueueing a prompt that starts with a verb word.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

QueueOpKind = Literal["add", "list", "edit", "rm", "move", "clear", "run", "help"]

# verb -> (kind, takes a leading item index)
QUEUE_VERB_ALIASES: dict[str, tuple[QueueOpKind, bool]] = {
    "list": ("list", False), "ls": ("list", False), "show": ("list", False),
    "clear": ("clear", False),
    "run": ("run", False), "fire": ("run", False), "release": ("run", False), "go": ("run", False),
    "help": ("help", False),
    "edit": ("edit", True), "set": ("edit", True),
    "rm": ("rm", True), "remove": ("rm", True), "delete": ("rm", True), "del": ("rm", True), "pop": ("rm", True),
    "move": ("move", True),
}


@dataclass(frozen=True)
class QueueOp:
    kind: QueueOpKind
    index: Optional[int] = None      # edit/rm: the 1-based item; move: source
    to: Optional[int] = None         # move: destination
    text: str = ""                   # add: the prompt; edit: the replacement text; rm/move: raw rest

    @property
    def is_management(self) -> bool:
        return self.kind != "add"


def parse_queue_payload(payload: str) -> QueueOp:
    """Classify the text after ``/queue``. Empty payload lists; ``add X`` forces a prompt."""
    payload = (payload or "").strip()
    if not payload:
        return QueueOp("list")
    verb, _, rest = payload.partition(" ")
    verb, rest = verb.lower(), rest.strip()
    if verb == "add":
        return QueueOp("add", text=rest)
    entry = QUEUE_VERB_ALIASES.get(verb)
    if entry is None:
        return QueueOp("add", text=payload)
    kind, takes_index = entry
    if not takes_index:
        return QueueOp(kind) if not rest else QueueOp("add", text=payload)
    first = rest.split(None, 1)[0] if rest else ""
    if not first.isdigit():
        return QueueOp("add", text=payload)
    index = int(first)
    if kind == "move":
        bits = rest.split()
        if len(bits) == 2 and bits[1].isdigit():
            return QueueOp("move", index=index, to=int(bits[1]), text=rest)
        return QueueOp("move", index=index, to=None, text=rest)  # handler renders usage_move
    if kind == "edit":
        _, _, new_text = rest.partition(" ")
        return QueueOp("edit", index=index, text=new_text.strip())  # empty text -> handler renders usage_edit
    return QueueOp(kind, index=index, text=rest)
