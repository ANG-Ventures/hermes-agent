"""scripts/check_worktree_local_reasons.py (t_20fa16c7): no worktree card stays on the Studio for an
unenumerated reason. Ace 2026-11-10: "All machines can make any checkout always ... lint it so this can
never happen again."
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PORT = ROOT / "hermes_cli" / "kanban_worktree_port.py"
DISPATCH = ROOT / "hermes_cli" / "kanban_db_dispatch.py"


def _lint():
    spec = importlib.util.spec_from_file_location("check_worktree_local_reasons",
                                                  ROOT / "scripts" / "check_worktree_local_reasons.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_tree_passes_and_every_reason_has_a_justification():
    lint = _lint()
    assert lint.main([str(PORT), str(DISPATCH)]) == 0
    from hermes_cli import kanban_worktree_port as kwt
    assert set(kwt.LOCAL_ONLY_REASONS) == set(lint.ENUMERATED)


def test_why_is_the_choke_point_every_constant_is_listed_and_an_unknown_reason_is_recorded(caplog, tmp_path):
    """Prism b4c7ade40781: whatever the static lint misses, ``_why`` records a reason outside the table as
    ``unenumerated_local_reason`` (one loud log line naming it), and is the only writer of a reason list."""
    from hermes_cli import kanban_worktree_port as kwt
    consts = {n: v for n, v in vars(kwt).items() if n.startswith("WHY_")}
    assert consts and all(v in kwt.LOCAL_ONLY_REASONS for v in consts.values()), consts
    why: list = []
    with caplog.at_level("ERROR", logger=kwt.__name__):
        kwt._why(why, "operator_pin" + "_extra")
    assert why == ["unenumerated_local_reason"]
    assert [r for r in caplog.records if "'operator_pin_extra'" in r.getMessage()]
    known: list = []
    kwt._why(known, kwt.WHY_NO_POOL)
    assert known == ["no_pool_host"]
    # a write round the choke point fails the lint
    src = PORT.read_text(encoding="utf-8-sig")
    port = tmp_path / "kanban_worktree_port.py"
    port.write_text(src + '\n\ndef _new_rule(why):\n    why.append("checkout_lives_on_the_studio")\n',
                    encoding="utf-8")
    assert _lint().main([str(port), str(DISPATCH)]) == 1


def test_an_unlisted_local_only_reason_fails(tmp_path):
    src = PORT.read_text(encoding="utf-8-sig")
    port = tmp_path / "kanban_worktree_port.py"
    # A new code path keeping the card on the Studio because of where its checkout lives.
    port.write_text(src + '\n\ndef _new_rule(why):\n    _why(why, "checkout_lives_on_the_studio")\n',
                    encoding="utf-8")
    assert _lint().main([str(port), str(DISPATCH)]) == 1
    # ... and a WHY_ constant added to the module without the lint list.
    port.write_text(src.replace('WHY_PINNED = "operator_pin"', 'WHY_PINNED = "repo_on_the_list"'),
                    encoding="utf-8")
    assert _lint().main([str(port), str(DISPATCH)]) == 1


@pytest.mark.parametrize("body", [
    # Prism f202699b4161: the reason routed through a local variable
    '    reason = "checkout_lives_on_the_studio"\n    _why(why, reason)\n',
    '    reason = WHY_NO_POOL\n    if why:\n        reason = "checkout_lives_on_the_studio"\n    _why(why, reason)\n',
    '    for reason in ("checkout_lives_on_the_studio",):\n        _why(why, reason)\n',
    '    reason = _helper()\n    _why(why, reason)\n',
    '    _why(why, reason="checkout_lives_on_the_studio")\n',
])
def test_an_unlisted_reason_through_a_variable_fails(tmp_path, body):
    src = PORT.read_text(encoding="utf-8-sig")
    port = tmp_path / "kanban_worktree_port.py"
    port.write_text(src + '\n\ndef _helper():\n    return "checkout_lives_on_the_studio"\n\n\n'
                    'def _new_rule(why):\n' + body, encoding="utf-8")
    assert _lint().main([str(port), str(DISPATCH)]) == 1


def test_an_enumerated_reason_through_a_variable_passes(tmp_path):
    src = PORT.read_text(encoding="utf-8-sig")
    port = tmp_path / "kanban_worktree_port.py"
    port.write_text(src + '\n\ndef _new_rule(why, x):\n    reason = WHY_NO_POOL\n    if x:\n'
                    '        reason = "operator_pin"\n    _why(why, reason)\n', encoding="utf-8")
    assert _lint().main([str(port), str(DISPATCH)]) == 0


def test_a_suffix_after_the_workspace_kind_prefix_in_the_dispatcher_fails(tmp_path):
    """Prism 4418d7472cae: the literal head must be exactly the prefix; anything after it is a new reason."""
    src = DISPATCH.read_text(encoding="utf-8-sig")
    dispatch = tmp_path / "kanban_db_dispatch.py"
    dispatch.write_text(src + '\n\ndef _old(wt_why):\n    return "local", '
                        'f"not_portable:workspace_kind:checkout_lives_on_the_studio:{wt_why[0]}"\n', encoding="utf-8")
    assert _lint().main([str(PORT), str(dispatch)]) == 1


def test_a_fixed_repo_list_reason_in_the_dispatcher_fails(tmp_path):
    src = DISPATCH.read_text(encoding="utf-8-sig")
    dispatch = tmp_path / "kanban_db_dispatch.py"
    dispatch.write_text(src + '\n\ndef _old():\n    return "local", "not_portable:workspace_kind:worktree is '
                        'Studio-only; fleet/kanban-worktree-mirrors.json must list the repo"\n', encoding="utf-8")
    assert _lint().main([str(PORT), str(dispatch)]) == 1
