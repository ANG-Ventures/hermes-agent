"""agent.output_split -- the ONE finished/unfinished output-split producer.

Cases mirror tokens-ace tests/test_output_split.py (where the code lived before the move),
plus the fail-closed bucket, so the producer is gated in the repo that owns it.
"""
import json

import pytest

from agent import output_split as s

OLD_COMPOSITION = {"sys_tokens": 1, "fixed_tokens": 12, "total_tokens": 14}


def _calls(*outputs):
    return [{"composition": {"fixed_tokens": i}, "output_tokens": o, "reasoning_tokens": 0}
            for i, o in enumerate(outputs, 1)]


def test_split_is_last_call_finished_rest_unfinished():
    assert s.turn_output_split(_calls(2000, 2200, 600), 4800) == (600, 4200)
    assert s.turn_output_split(json.dumps(_calls(2000, 2200, 600)), 4800) == (600, 4200)


def test_zero_output_single_call_is_a_known_zero_split():
    zero = [{"composition": OLD_COMPOSITION, "output_tokens": 0, "reasoning_tokens": 0}]
    assert s.turn_output_split(zero, 0) == (0, 0)


@pytest.mark.parametrize("blob", [
    None, "", "not json", "{}", [], [OLD_COMPOSITION],
    [OLD_COMPOSITION, {"composition": {}, "output_tokens": 10, "reasoning_tokens": 0}],
])
def test_unknown_or_mixed_blobs_stay_unknown_never_zero(blob):
    assert s.turn_output_split(blob, 100) == (None, None)


def test_bucket_known_split_sums_to_billed():
    b = s.output_split_bucket(_calls(100, 23), 123)
    assert b == {"output_split_known": 1, "finished_output": 23, "unfinished_output": 100,
                 "output_pre_split": 0, "output_split_reclassified_count": 0}


def test_bucket_unknown_is_all_pre_split():
    b = s.output_split_bucket([OLD_COMPOSITION], 456)
    assert b["output_pre_split"] == 456 and b["output_split_known"] == 0
    assert b["finished_output"] == b["unfinished_output"] == 0


def test_bucket_fails_closed_when_parts_do_not_sum_to_billed():
    # Last call's output exceeds the turn's billed output: unfinished clamps to 0, so the
    # parts cannot sum -- the whole output goes to pre-split and the reclassification counts.
    b = s.output_split_bucket(_calls(10, 500), 300)
    assert b == {"output_split_known": 0, "finished_output": 0, "unfinished_output": 0,
                 "output_pre_split": 300, "output_split_reclassified_count": 1}


def test_bucket_resolves_turn_output_split_in_this_module(monkeypatch):
    monkeypatch.setattr(s, "turn_output_split", lambda _c, _b: (1, 2))
    b = s.output_split_bucket(_calls(4800), 4800)
    assert b["output_pre_split"] == 4800 and b["output_split_reclassified_count"] == 1


def test_add_bucket_accumulates_every_field():
    dst = {}
    s.add_output_split_bucket(dst, s.output_split_bucket(_calls(1, 2), 3))
    s.add_output_split_bucket(dst, s.output_split_bucket(None, 5))
    assert dst == {"output_split_known": 1, "finished_output": 2, "unfinished_output": 1,
                   "output_pre_split": 5, "output_split_reclassified_count": 0}
    assert tuple(dst) == s.OUTPUT_SPLIT_FIELDS


def test_module_is_stdlib_only():
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(s))
    mods = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    mods |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert mods <= {"__future__", "json", "typing"}, mods
