"""One guard per start, exactly one, never zero (t_2158f095; Ace msg 1569308234441949345; Apollo ruling 11-11).

The count and the load bar come off a take only where that same take is admitted by the fleet allocator
(``_take_allocd`` -> ``structural_free``). A path that returns a host without an allocd/may_start verdict
keeps its old bound: the v0.1 load bar (Prism 2dcecd5ae3a9) and the real free count for pinned takes and
the no-placement-table branch (Prism cba00e400ff4).
"""
import dataclasses
import json
from types import SimpleNamespace

from hermes_cli import kanban_headroom as kh
from hermes_cli import kanban_worker_pool as kwp

from tests.hermes_cli.test_kanban_target_pressure import HOSTS, T0, sig  # noqa: F401  (fixture)

AI = dataclasses.replace(HOSTS[0], slots=2)  # a legacy counted host: 2 slots


def _pressure(at):
    return json.dumps({"host": "ace-ai", "at": at, "ncpu": 24, "load1": 2.4, "load_ratio": 0.1,
                       "band": {"kanban": "ok"}, "band_reason": {"kanban": "ok"}})


def _probe(h, **_kw):
    return kwp.HostSample(2.0, 24, T0, _pressure(T0))


def _allocd_on(sig, *, table):
    sig.allocd = kh.ALLOCD_ON
    sig.placement_table = table
    return sig


def test_structural_candidates_ignore_count_and_load_threshold():
    host = SimpleNamespace(enabled=True, state='active', slots=1)
    plan = SimpleNamespace(hosts={'box': host}, detail={'box': {'load1': 999, 'running': 20}}, taken={})
    assert kh.structural_free(plan, 'box') == kwp.UNCOUNTED_TICK_FREE
    host.enabled = False
    assert kh.structural_free(plan, 'box') == 0
    host.enabled = True
    plan.detail['box']['load1'] = None
    assert kh.structural_free(plan, 'box') == 0  # no probe, not a pressure threshold


def test_v01_no_signal_keeps_the_load_bar():
    """Prism 2dcecd5ae3a9: no signal = no allocd/may_start call, so the load bar is the only guard."""
    hot = kwp.plan([dataclasses.replace(HOSTS[0], slots=4)], {"ace-ai": 1}, probe=lambda h: (500.0, 16))
    assert hot.budget == 0
    assert hot.take("alpha") is None and hot.refusal == "pool_full"


def test_allocd_on_pinned_take_keeps_the_real_free_count(sig, monkeypatch):
    """Prism cba00e400ff4: a pinned take books via book_pin with no allocd call, so the count still binds."""
    monkeypatch.setattr(kwp, "_kanban_unified_admit", lambda *a, **k: False)
    booked = []
    monkeypatch.setattr(kh, "book_pin", lambda plan, name, *a, **k: booked.append(name) or "rid")
    p = kwp.plan([AI], {"ace-ai": 2}, probe=_probe, signal=_allocd_on(sig, table=object()))
    assert p.slots["ace-ai"] == 0
    assert p.take("alpha", pin="ace-ai", job_id="t_x") is None
    assert p.refusal == "pin_host_full" and booked == []


def test_allocd_on_without_a_placement_table_keeps_the_real_free_count(sig, monkeypatch):
    """The band-pick branch never asks allocd: it reads plan.slots, which must be the real count."""
    monkeypatch.setattr(kwp, "_kanban_unified_admit", lambda *a, **k: False)
    p = kwp.plan([AI], {"ace-ai": 2}, probe=_probe, signal=_allocd_on(sig, table=None))
    assert p.slots["ace-ai"] == 0 and p.budget == 0
    assert p.take("alpha") is None


def test_allocd_admitted_take_is_not_capped_by_the_count(sig, monkeypatch):
    """The one guard: inside _take_allocd allocd alone decides, past the legacy count."""
    monkeypatch.setattr(kwp, "_kanban_unified_admit", lambda *a, **k: False)
    monkeypatch.setattr(kh, "unified_filter", lambda plan, names, job: (list(names), {}))
    monkeypatch.setattr(kh, "pool_key", lambda table, cands, h: h if h in cands else None)
    asked = []

    def fake_admit(plan, cands, job, job_id):
        asked.append(list(cands))
        return SimpleNamespace(host=cands[0], reservation_id=f"r{len(asked)}", reason=None, rule="admit")

    monkeypatch.setattr(kh, "allocd_admit", fake_admit)
    p = kwp.plan([AI], {"ace-ai": 2}, probe=_probe, signal=_allocd_on(sig, table=object()))
    assert p.budget > 0
    got = [p.take("alpha", job_id=f"t_{i}") for i in range(5)]
    assert [h.name if h else p.refusal for h in got] == ["ace-ai"] * 5
    assert asked == [["ace-ai"]] * 5
