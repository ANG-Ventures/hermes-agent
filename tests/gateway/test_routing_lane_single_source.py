"""LINT: a routing-lane tuple must never be hand-built from raw fields.

``effective_routing_lane`` is the single source of truth for the 4-tuple
``(platform, chat_id, chat_type, thread_id)`` that decides whether two records
describe the SAME conversation. It canonicalizes — most importantly a Discord
guild ``channel`` becomes ``group``, because that is the spelling
``build_session_key`` keys on.

Three call sites compared lanes. Two of them canonicalized ONE operand and
hand-built the other from raw strings:

* ``gateway/kanban_watchers.py::_live_chat_participants`` — the live wake path
* ``hermes_cli/kanban.py::_cmd_notify_repair`` — the repair path

Both produced a structural 0% match rate for any ``channel``-spelled
subscription row: the wake found no identity, keyed a bare ``group:<chat>``
session, and minted a shadow session that replied into the user's channel at
the config-default model and reasoning effort. The repair tool that exists to
FIX such rows reported ``skipped_no_evidence`` for exactly the rows it was
pointed at — the defence and the defect shared one root.

#659 linted the SOURCE axis (no platform adapter may LABEL a Discord chat
``channel``). It could not see this, because nothing here labels anything —
these sites only COMPARE. This is the comparison axis: every lane tuple must
come out of the canonicalizer, so the two halves of a compared pair can never
drift apart again.

Incident: 2026-09-12, Discord #curator.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: Modules that compare routing lanes. A new consumer belongs here.
LANE_CONSUMERS = (
    "gateway/kanban_watchers.py",
    "hermes_cli/kanban.py",
    "gateway/routing_identity.py",
)

#: The canonicalizer itself must build the tuple by hand — it IS the source of
#: truth. Any other function doing so is the bug class.
CANONICALIZER = "effective_routing_lane"

#: Field-name fragments whose co-occurrence in one tuple literal means
#: "routing lane". Chosen by testing the predicate against the EXACT tuples
#: that shipped the 2026-09-12 incident:
#:
#:   (str(platform_value), want_chat, want_chat_type, want_thread)   # wake
#:   (platform, chat_id, chat_type, thread_id)                       # repair
#:
#: An earlier draft keyed on ``chat_id`` + ``chat_type`` and was INERT — the
#: wake site spells its chat ``want_chat``, so the rule silently matched
#: nothing and passed forever. A lint at zero hits is indistinguishable from a
#: clean tree; always drive a new predicate against the literal line that
#: shipped the bug before trusting it.
_LANE_FIELD_MARKERS = ("chat_type", "platform")


def _enclosing_functions(tree):
    """Map every node to the qualified function that encloses it."""
    owners = {}

    def walk(node, owner):
        for child in ast.iter_child_nodes(node):
            name = owner
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{owner}.{child.name}" if owner else child.name
            owners[child] = name
            walk(child, name)

    walk(tree, "")
    return owners


def _looks_like_a_lane_tuple(node: ast.Tuple) -> bool:
    """A 4-tuple mentioning both a chat id and a chat type in raw form.

    Deliberately narrow. Matching on arity alone would flag every unrelated
    4-tuple; matching on one marker alone would flag ordinary kwargs bundles.
    Requiring BOTH markers plus arity 4 is what keeps this rule sharp enough
    that nobody is tempted to mute it.
    """
    if len(node.elts) != 4:
        return False
    names = set()
    for element in node.elts:
        for sub in ast.walk(element):
            if isinstance(sub, ast.Name):
                names.add(sub.id)
            elif isinstance(sub, ast.Attribute):
                names.add(sub.attr)
    return all(
        any(marker in name for name in names)
        for marker in _LANE_FIELD_MARKERS
    )


def _hand_built_lane_sites(path: Path):
    """Yield ``(lineno, owner)`` for every hand-built lane tuple.

    Two shapes are legitimate and must never be flagged (§1c: a guard that
    flags correct code is a guard people mute):

    * the canonicalizer's own ``return`` — it IS the source of truth;
    * an assignment TARGET that unpacks a canonicalizer call, e.g.
      ``platform, chat, kind, thread = effective_routing_lane(...)``. That is
      a consumer doing exactly the right thing; only the VALUE side of an
      assignment can be a hand-built lane.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    owners = _enclosing_functions(tree)

    unpack_targets = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Tuple):
                    unpack_targets.add(id(target))
        elif isinstance(node, (ast.For, ast.comprehension)):
            if isinstance(getattr(node, "target", None), ast.Tuple):
                unpack_targets.add(id(node.target))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Tuple) or not _looks_like_a_lane_tuple(node):
            continue
        if id(node) in unpack_targets:
            continue
        owner = owners.get(node, "")
        if CANONICALIZER in owner:
            continue
        yield node.lineno, owner


def test_no_consumer_hand_builds_a_routing_lane():
    offenders = []
    for relative in LANE_CONSUMERS:
        path = ROOT / relative
        for lineno, owner in _hand_built_lane_sites(path):
            offenders.append(f"{relative}:{lineno} ({owner or '<module>'})")
    assert not offenders, (
        "a routing lane was built from raw fields instead of "
        f"{CANONICALIZER}() — the two halves of a lane comparison will drift "
        "and a legacy chat_type spelling will silently match nothing "
        f"(2026-09-12 shadow-session incident): {offenders}"
    )


def test_the_lint_can_actually_see_a_violation():
    """Positive control, driven by the LITERAL lines that shipped the bug.

    A rule that matches NOTHING always passes. The first draft of this lint
    keyed on ``chat_id``, which the wake site spells ``want_chat`` — so it was
    inert and a planted copy #4 sailed straight through. These two strings are
    verbatim from the pre-fix source; if the predicate stops seeing either of
    them the guard is dead and this test says so.
    """
    shipped_the_incident = (
        # gateway/kanban_watchers.py::_live_chat_participants
        "def _live_chat_participants(self):\n"
        "    want_lane = (\n"
        "        str(platform_value), want_chat, want_chat_type, want_thread,\n"
        "    )\n",
        # hermes_cli/kanban.py::_cmd_notify_repair
        "def _resolve(row):\n"
        "    lane = (platform, chat_id, chat_type, thread_id)\n"
        "    return lane\n",
    )
    for source in shipped_the_incident:
        tree = ast.parse(source)
        owners = _enclosing_functions(tree)
        hits = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Tuple) and _looks_like_a_lane_tuple(node)
        ]
        assert hits, (
            "the lane predicate no longer recognises a tuple that ACTUALLY "
            f"shipped the 2026-09-12 incident — the lint is inert:\n{source}"
        )
        assert CANONICALIZER not in owners[hits[0]]


def test_the_lint_does_not_flag_unrelated_tuples():
    """Negative control: a guard that flags correct code gets muted."""
    benign = ast.parse(
        "def f():\n"
        "    a = (user_id, user_id_alt, scope_id, session_key)\n"
        "    b = (platform, chat_id)\n"
        "    c = (1, 2, 3, 4)\n"
        "    return a, b, c\n"
    )
    hits = [
        node for node in ast.walk(benign)
        if isinstance(node, ast.Tuple) and _looks_like_a_lane_tuple(node)
    ]
    assert not hits, "the lane predicate is too broad and will be tuned out"


def test_unpacking_the_canonicalizer_is_not_a_violation():
    """Negative control: the CORRECT consumer shape must stay silent.

    ``routing_key_carries_identity`` does exactly the right thing —
    ``platform, chat, kind, thread = effective_routing_lane(...)`` — and an
    earlier draft of this lint flagged it, which is how a guard earns a
    reputation for being wrong and gets muted (incident-to-lint-ratchet §1c).
    """
    import tempfile

    correct = (
        "def consumer():\n"
        "    platform_value, chat, key_chat_type, effective_thread = "
        "effective_routing_lane(\n"
        "        platform=platform, chat_id=chat_id, chat_type=kind,\n"
        "    )\n"
        "    return platform_value\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as handle:
        handle.write(correct)
        temp = Path(handle.name)
    try:
        assert list(_hand_built_lane_sites(temp)) == [], (
            "unpacking the canonicalizer is the CORRECT shape and must not "
            "be flagged"
        )
    finally:
        temp.unlink()


def test_the_canonicalizer_is_still_the_thing_being_pointed_at():
    """Guard the guard: a rename must not silently empty the allowlist."""
    from gateway import routing_identity

    assert hasattr(routing_identity, CANONICALIZER), (
        f"{CANONICALIZER} was renamed — this lint's exemption no longer "
        "matches anything and every consumer will read as an offender"
    )
