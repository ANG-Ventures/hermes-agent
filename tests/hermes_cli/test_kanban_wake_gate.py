"""Wake -> notify downgrade under measured contention (t_74bf5296).

Ace 13:40: "Wake is really nice as well, except if we have resource
contention. In that case, we want to turn Wake off." The host half is measured
CPU busy (t_0e52c7ab); the load1 band (kill switch) is the wake gate's own
(t_b20850d2: nothing inherits from dispatch_load_gate). Hysteresis, per EVENT,
never the subscription.
"""
from __future__ import annotations

from hermes_cli.kanban_wake_gate import WakeGate, read_state

# The load1 band (kill switch ``host_axis: load1``); the default axis is cpu (t_0e52c7ab).
CFG = {"kanban": {"wake_load_gate": {"pause_above": 64, "resume_below": 48, "host_axis": "load1"}}}
CPU_CFG = {"kanban": {"wake_load_gate": {"cpu_pause": 0.90}}}


class Load:
    def __init__(self, v):
        self.v = v

    def __call__(self):
        return (self.v, self.v, self.v)


class Cpu:
    """A sample_cpu_busy stand-in: (busy, snapshot) like the real one."""

    def __init__(self, v):
        self.v = v

    def __call__(self, prev=None):
        return self.v, object()


def _gate(load, tmp_path, cfg=CFG, lane=lambda p: None, cpu=None):
    return WakeGate(cfg, ncpu=32, loadavg=load, lane_probe=lane,
                    cpu_sampler=cpu or Cpu(None),
                    state_file=tmp_path / "wake_gate.json")


def test_load1_band_is_the_wake_gates_own(tmp_path):
    g = _gate(Load(1), tmp_path)
    assert (g.pause_above, g.resume_below) == (64, 48)


def test_dispatch_load_gate_keys_are_not_inherited(tmp_path):
    """t_b20850d2: dispatch_load_gate's load/CPU keys are gone; a leftover one never drives the wake gate."""
    cfg = {"kanban": {"dispatch_load_gate": {"pause_above": 64, "resume_below": 48, "cpu_pause": 0.50},
                      "wake_load_gate": {"host_axis": "load1"}}}
    g = _gate(Load(500), tmp_path, cfg=cfg)
    assert g.pause_above is None and g.downgrade_reason(None, now=0) is None
    g2 = _gate(Load(1), tmp_path, cfg={"kanban": {"dispatch_load_gate": {"cpu_pause": 0.50}}}, cpu=Cpu(0.60))
    assert g2.cpu_pause == 0.90 and g2.downgrade_reason(None, now=0) is None


def test_downgrade_fires_above_the_gate_and_not_below(tmp_path):
    load = Load(71)
    g = _gate(load, tmp_path)
    assert g.downgrade_reason("apollo", now=0) == "host load 71"
    assert read_state(tmp_path / "wake_gate.json")["mode"] == "notify"
    load.v = 30
    assert g.downgrade_reason("apollo", now=100) is None
    assert read_state(tmp_path / "wake_gate.json")["mode"] == "wake"


def test_below_the_gate_never_downgrades(tmp_path):
    g = _gate(Load(60), tmp_path)
    assert g.downgrade_reason("apollo", now=0) is None


def test_hysteresis_holds_between_resume_and_pause(tmp_path):
    load = Load(70)
    g = _gate(load, tmp_path)
    assert g.downgrade_reason(None, now=0)
    load.v = 55  # under pause_above, over resume_below: still contended
    assert g.downgrade_reason(None, now=20) == "host load 55"
    load.v = 47
    assert g.downgrade_reason(None, now=40) is None


def test_samples_at_most_once_per_window(tmp_path):
    load = Load(70)
    g = _gate(load, tmp_path)
    assert g.downgrade_reason(None, now=0)
    load.v = 1
    assert g.downgrade_reason(None, now=5), "cached sample inside sample_seconds"
    assert g.downgrade_reason(None, now=16) is None


def test_lane_headroom_downgrades_that_profile_only(tmp_path):
    lane = lambda p: "lane claude-apr capped" if p == "athena" else None
    g = _gate(Load(1), tmp_path, lane=lane)
    assert g.downgrade_reason("athena", now=0) == "lane claude-apr capped"
    assert g.downgrade_reason("apollo", now=0) is None


def test_lane_probe_error_fails_open_toward_wake(tmp_path):
    def boom(p):
        raise RuntimeError("relay down")
    g = _gate(Load(1), tmp_path, lane=boom)
    assert g.downgrade_reason("athena", now=0) is None


def test_disabled_gate_never_downgrades(tmp_path):
    cfg = {"kanban": {"wake_load_gate": {"enabled": False, "pause_above": 64, "resume_below": 48}}}
    g = _gate(Load(200), tmp_path, cfg=cfg, lane=lambda p: "capped")
    assert g.downgrade_reason("athena", now=0) is None


def test_no_configured_band_means_no_host_downgrade_on_the_load1_axis(tmp_path):
    """The ncpu fallback is not a measured band: on the load1 axis, no pause_above = no host gate."""
    g = _gate(Load(500), tmp_path, cfg={"kanban": {"wake_load_gate": {"host_axis": "load1"}}})
    assert g.downgrade_reason(None, now=0) is None
    assert g.snapshot()["mode"] == "off"


def test_cpu_axis_is_on_with_no_config_at_all(tmp_path):
    """The default axis needs no band: an empty kanban block still downgrades at cpu_pause 0.90."""
    g = _gate(Load(1), tmp_path, cfg={"kanban": {}}, cpu=Cpu(0.95))
    assert g.downgrade_reason(None, now=0) == "host cpu 95% busy (load1 1)"


# -- cpu axis (default; t_0e52c7ab, Ace 2026-10-18 22:22) ----------------------


def test_high_load1_with_idle_cpu_does_not_downgrade(tmp_path):
    """The 22:22 Studio sample: load1 172 / 32c, CPU 47 % busy. Not overloaded."""
    g = _gate(Load(172), tmp_path, cfg=CPU_CFG, cpu=Cpu(0.47))
    assert g.downgrade_reason("apollo", now=0) is None
    assert g.downgrade_reason("apollo", now=0, human_owner=True) is None
    snap = read_state(tmp_path / "wake_gate.json")
    assert snap["mode"] == "wake" and snap["host_axis"] == "cpu" and snap["load1"] == 172


def test_saturated_cpu_downgrades_and_names_the_measurement(tmp_path):
    cpu = Cpu(0.95)
    g = _gate(Load(40), tmp_path, cfg=CPU_CFG, cpu=cpu)
    assert g.downgrade_reason("apollo", now=0) == "host cpu 95% busy (load1 40)"
    assert read_state(tmp_path / "wake_gate.json")["mode"] == "notify"
    cpu.v = 0.50
    assert g.downgrade_reason("apollo", now=100) is None


def test_cpu_axis_ignores_load_even_when_load_is_low(tmp_path):
    """Saturation is the CPU's answer, whatever load1 says."""
    g = _gate(Load(3), tmp_path, cfg=CPU_CFG, cpu=Cpu(0.91))
    assert g.downgrade_reason(None, now=0).startswith("host cpu 91% busy")


def test_unreadable_cpu_sample_fails_open_toward_wake(tmp_path):
    def boom(prev=None):
        raise RuntimeError("psutil gone")
    g = _gate(Load(500), tmp_path, cfg=CPU_CFG, cpu=boom)
    assert g.downgrade_reason(None, now=0) is None
    g2 = _gate(Load(500), tmp_path, cfg=CPU_CFG, cpu=Cpu(None))
    assert g2.downgrade_reason(None, now=0) is None


def test_cpu_pause_default_and_wake_block_overrides(tmp_path):
    assert _gate(Load(1), tmp_path, cfg=CPU_CFG).cpu_pause == 0.90
    cfg = {"kanban": {"wake_load_gate": {"cpu_pause": 0.80}}}
    g = _gate(Load(1), tmp_path, cfg=cfg, cpu=Cpu(0.85))
    assert g.cpu_pause == 0.80
    assert g.downgrade_reason(None, now=0) == "host cpu 85% busy (load1 1)"


def test_load1_kill_switch_restores_the_band(tmp_path):
    cfg = {"kanban": {"wake_load_gate": {"pause_above": 32, "resume_below": 24, "host_axis": "load1"}}}
    g = _gate(Load(172), tmp_path, cfg=cfg, cpu=Cpu(0.10))
    assert g.downgrade_reason(None, now=0) == "host load 172"


def test_human_owner_still_needs_dispatcher_proc_paused_on_cpu_axis(tmp_path):
    g = WakeGate(CPU_CFG, ncpu=32, loadavg=Load(172), lane_probe=lambda p: None,
                 cpu_sampler=Cpu(0.97), dispatcher_state=lambda: {"state": "spilling"},
                 state_file=tmp_path / "wake_gate.json")
    assert g.downgrade_reason("apollo", now=0) == "host cpu 97% busy (load1 172)"
    assert g.downgrade_reason("apollo", now=0, human_owner=True) is None


class CpuDelta:
    """A sampler that reports busy only from a REAL previous snapshot: (busy, snap)."""

    def __init__(self, busy):
        self.busy, self.calls = busy, []

    def __call__(self, prev=None):
        self.calls.append(prev)
        return (None if prev is None else self.busy), object()


def test_stale_cpu_baseline_is_dropped_not_averaged(tmp_path):
    """Prism ef086d30d8c4: wakes hours apart must not judge 'now' by an hours-long average."""
    cpu = CpuDelta(0.95)
    g = _gate(Load(1), tmp_path, cfg=CPU_CFG, cpu=cpu)
    assert g.downgrade_reason(None, now=0) is None          # first sample: no baseline
    assert g.downgrade_reason(None, now=60).startswith("host cpu 95% busy")  # fresh 60 s delta
    assert g.downgrade_reason(None, now=60 + 3 * 3600) is None  # 3 h gap: baseline dropped
    assert cpu.calls[-1] is None
    assert g.downgrade_reason(None, now=60 + 3 * 3600 + 30).startswith("host cpu 95%")


def test_failed_cpu_sample_does_not_refresh_the_baseline_age(tmp_path):
    """Prism eb61a0216806: a failed read must not stamp an old snapshot as fresh."""
    state = {"fail": False}
    snaps = []

    def sampler(prev=None):
        if state["fail"]:
            return None, prev           # the real sample_cpu_busy's failure shape
        snap = object()
        snaps.append(snap)
        return (None if prev is None else 0.95), snap

    g = _gate(Load(1), tmp_path, cfg=CPU_CFG, cpu=sampler)
    g.downgrade_reason(None, now=0)                      # baseline A at t=0
    state["fail"] = True
    g.downgrade_reason(None, now=200)                    # failed read; A must keep age 0
    g.downgrade_reason(None, now=290)                    # failed read again
    state["fail"] = False
    # t=400: A is 400 s old (> 300 s window) -> dropped, fail open; not a 400 s average
    assert g.downgrade_reason(None, now=400) is None


def test_raising_cpu_sampler_does_not_refresh_the_baseline_age(tmp_path):
    state = {"boom": False}

    def sampler(prev=None):
        if state["boom"]:
            raise RuntimeError("psutil hiccup")
        return (None if prev is None else 0.95), object()

    g = _gate(Load(1), tmp_path, cfg=CPU_CFG, cpu=sampler)
    g.downgrade_reason(None, now=0)
    state["boom"] = True
    g.downgrade_reason(None, now=250)
    state["boom"] = False
    assert g.downgrade_reason(None, now=400) is None


def test_max_interval_below_sample_cadence_still_trips(tmp_path):
    """Prism 6360f05606ba: cpu_max_interval < sample_seconds must not disable the CPU axis."""
    cfg = {"kanban": {"wake_load_gate": {"cpu_pause": 0.90, "cpu_max_interval": 1}}}
    g = _gate(Load(1), tmp_path, cfg=cfg, cpu=CpuDelta(0.95))
    assert g.cpu_max_interval >= 2 * g.sample_seconds
    results = [g.downgrade_reason(None, now=t) for t in range(1000, 1046)]
    assert any(r and r.startswith("host cpu 95%") for r in results)


def test_fallback_admission_binds_owner_scope_and_restores_caller(tmp_path, monkeypatch):
    """Derived sources must see the wake owner's secrets, never the caller's."""
    from agent import secret_scope
    from hermes_cli import fallback_config, kanban_provider_health as health, profiles
    from hermes_cli.kanban_wake_gate import _default_lane_probe
    from hermes_constants import get_hermes_home

    homes = {}
    for profile in ("owner-a", "owner-b"):
        home = tmp_path / profile
        home.mkdir()
        (home / "config.yaml").write_text(
            "model:\n  provider: zai\n  default: glm-5.1\n"
            "fallback_providers:\n  - source: wake-scope-test\n")
        (home / ".env").write_text(f"WAKE_TEST_SECRET={profile}\n")
        homes[profile] = home
    monkeypatch.setattr(profiles, "resolve_profile_env", lambda p: str(homes[p]))
    monkeypatch.setattr(health, "configured_probes", lambda: {"zai": "https://primary.invalid"})
    monkeypatch.setattr(health, "configured_pool_health_urls", lambda: {})
    monkeypatch.setattr(health, "configured_box_health", lambda: False)
    monkeypatch.setattr(health, "_fetch", lambda *a: {"eligible_count": 0})
    seen = []

    def source(entry):
        owner = secret_scope.get_secret("WAKE_TEST_SECRET")
        seen.append((owner, get_hermes_home(), secret_scope.current_secret_scope_home()))
        return [{"provider": "deepseek", "model": "deepseek-v4-flash"}] if owner else []

    monkeypatch.setitem(fallback_config._CHAIN_SOURCES, "wake-scope-test", source)
    multiplex = secret_scope.set_multiplex_context(True)
    caller = secret_scope.set_secret_scope({"WAKE_TEST_SECRET": "foreign"}, profile_home="foreign")
    original_home = get_hermes_home()
    try:
        for profile in ("owner-a", "owner-b", "owner-a"):
            assert _default_lane_probe(profile) is None
            assert secret_scope.get_secret("WAKE_TEST_SECRET") == "foreign"
            assert secret_scope.current_secret_scope_home() == "foreign"
            assert get_hermes_home() == original_home
        assert seen == [(p, homes[p], str(homes[p])) for p in ("owner-a", "owner-b", "owner-a")]
    finally:
        secret_scope.reset_secret_scope(caller)
        secret_scope.reset_multiplex_context(multiplex)


def test_fallback_admission_preserves_frozen_launch_credentials(tmp_path, monkeypatch):
    from agent import secret_scope
    from hermes_cli import fallback_config, kanban_provider_health as health, profiles
    from hermes_cli.kanban_wake_gate import _default_lane_probe
    from tui_gateway import launch_profile_policy

    launch = tmp_path / "launch"
    launch.mkdir()
    (launch / "config.yaml").write_text(
        "model:\n  provider: zai\n  default: glm-5.1\n"
        "fallback_providers:\n  - source: wake-launch-test\n")
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setenv("WAKE_LAUNCH_SECRET", "injected-at-launch")
    monkeypatch.setattr(launch_profile_policy, "_snapshot", None)
    monkeypatch.setattr(profiles, "resolve_profile_env", lambda p: str(launch))
    monkeypatch.setattr(health, "configured_probes", lambda: {"zai": "https://primary.invalid"})
    monkeypatch.setattr(health, "configured_pool_health_urls", lambda: {})
    monkeypatch.setattr(health, "configured_box_health", lambda: False)
    monkeypatch.setattr(health, "_fetch", lambda *a: {"eligible_count": 0})
    launch_profile_policy.capture_launch_env()
    monkeypatch.setenv("WAKE_LAUNCH_SECRET", "poisoned-after-launch")
    seen = []

    def source(entry):
        seen.append(secret_scope.get_secret("WAKE_LAUNCH_SECRET"))
        return [{"provider": "deepseek", "model": "deepseek-v4-flash"}] if seen[-1] else []

    monkeypatch.setitem(fallback_config._CHAIN_SOURCES, "wake-launch-test", source)
    multiplex = secret_scope.set_multiplex_context(True)
    caller = secret_scope.set_secret_scope(None)
    try:
        assert _default_lane_probe("default") is None
        assert seen == ["injected-at-launch"]
        assert secret_scope.current_secret_scope() is None
    finally:
        secret_scope.reset_secret_scope(caller)
        secret_scope.reset_multiplex_context(multiplex)


def test_fallback_admission_pinned_launch_ignores_secondary_env_mirror(tmp_path, monkeypatch):
    """A mirrored served home must not inherit the pinned launch's frozen secrets."""
    import os

    import hermes_constants
    from agent import secret_scope
    from hermes_cli import fallback_config, kanban_provider_health as health, profiles
    from hermes_cli.kanban_wake_gate import _default_lane_probe
    from tui_gateway import launch_profile_policy

    launch, secondary = tmp_path / "launch", tmp_path / "secondary"
    launch.mkdir()
    secondary.mkdir()
    (secondary / "config.yaml").write_text(
        "model:\n  provider: zai\n  default: glm-5.1\n"
        "fallback_providers:\n  - source: wake-pinned-test\n")
    (secondary / ".env").write_text("WAKE_SECONDARY_SECRET=file-only\n")
    monkeypatch.setenv("HERMES_HOME", str(launch))
    monkeypatch.setenv("WAKE_PINNED_SECRET", "launch-only-synthetic")
    monkeypatch.setattr(launch_profile_policy, "_snapshot", None)
    # Preserve any embedding host pin when monkeypatch restores this test's state.
    monkeypatch.setattr(hermes_constants, "_PINNED_PROCESS_HERMES_HOME", None)
    hermes_constants.pin_process_hermes_home(launch)
    launch_profile_policy.capture_launch_env()
    monkeypatch.setenv("HERMES_HOME", str(secondary))
    monkeypatch.setattr(profiles, "resolve_profile_env", lambda p: str(secondary))
    monkeypatch.setattr(health, "configured_probes", lambda: {"zai": "https://primary.invalid"})
    monkeypatch.setattr(health, "configured_pool_health_urls", lambda: {})
    monkeypatch.setattr(health, "configured_box_health", lambda: False)
    monkeypatch.setattr(health, "_fetch", lambda *a: {"eligible_count": 0})
    seen = []

    def source(entry):
        seen.append((secret_scope.get_secret("WAKE_PINNED_SECRET"),
                     secret_scope.get_secret("WAKE_SECONDARY_SECRET"),
                     hermes_constants.get_hermes_home(),
                     secret_scope.current_secret_scope_home()))
        return []

    monkeypatch.setitem(fallback_config._CHAIN_SOURCES, "wake-pinned-test", source)
    multiplex = secret_scope.set_multiplex_context(True)
    caller = secret_scope.set_secret_scope(
        {"WAKE_PINNED_SECRET": "caller-synthetic"}, profile_home="caller")
    home_token = hermes_constants.set_hermes_home_override(launch)
    try:
        assert hermes_constants.get_routing_process_hermes_home() == launch
        assert _default_lane_probe("secondary").startswith("lane zai capped;")
        assert seen == [(None, "file-only", secondary, str(secondary))]
        assert secret_scope.get_secret("WAKE_PINNED_SECRET") == "caller-synthetic"
        assert secret_scope.current_secret_scope_home() == "caller"
        assert hermes_constants.get_hermes_home() == launch
        assert os.environ["HERMES_HOME"] == str(secondary)
        assert hermes_constants.get_routing_process_hermes_home() == launch
    finally:
        hermes_constants.reset_hermes_home_override(home_token)
        secret_scope.reset_secret_scope(caller)
        secret_scope.reset_multiplex_context(multiplex)


def test_cpu_max_interval_is_configurable(tmp_path):
    cfg = {"kanban": {"wake_load_gate": {"cpu_pause": 0.90, "cpu_max_interval": 30}}}
    cpu = CpuDelta(0.95)
    g = _gate(Load(1), tmp_path, cfg=cfg, cpu=cpu)
    g.downgrade_reason(None, now=0)
    assert g.downgrade_reason(None, now=45) is None          # 45 s > 30 s window
