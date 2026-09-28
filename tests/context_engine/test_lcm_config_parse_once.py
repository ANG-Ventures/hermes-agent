"""REGRESSION (card t_90850d58): one LCM engine load parses config.yaml at most once.

LCM boot-cost class #4. ``LCMConfig.from_env`` resolved each config-file knob
through its own helper, and every helper re-read and re-parsed
``~/.hermes/config.yaml`` with pure-Python ``yaml.safe_load``: 12 parses of a
23 KB file per engine load, 2.75 s measured, 5-10 s under host load, all inside
``plugins/context_engine/__init__.py::_LOAD_LOCK`` which every turn's
``init_agent`` waits on.

Contract (docs/lcm-init-boot-cost-contract.md, "Config parse"): nothing under
``_LOAD_LOCK`` may re-parse config per knob.
"""
from __future__ import annotations

from pathlib import Path

from plugins.context_engine.lcm import config as lcm_config
from plugins.context_engine.lcm.config import LCMConfig
from plugins.context_engine.lcm.engine import LCMEngine

# Enough knobs that every config-file helper on the from_env path has
# something to find (lcm.*, compression.*, auxiliary.compression.*).
_CONFIG_YAML = """\
compression:
  enabled: true
  threshold: 0.61
  target_ratio: 0.22
  skew_floor: 0.8
  calibration_hard_frac: 0.92
auxiliary:
  compression:
    timeout: 90
lcm:
  context_threshold: 0.66
  fresh_tail_token_budget: 1234
  some_unsupported_key: 1
"""


def _count_parses(monkeypatch) -> list[int]:
    calls = [0]
    real = lcm_config._load_hermes_config_yaml

    def counting(*args, **kwargs):
        calls[0] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(lcm_config, "_load_hermes_config_yaml", counting)
    return calls


def _home(tmp_path: Path, monkeypatch, text: str = _CONFIG_YAML) -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / "config.yaml").write_text(text, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    for key in (
        "LCM_CONTEXT_THRESHOLD",
        "LCM_SUMMARY_TIMEOUT_MS",
        "LCM_TARGET_RATIO",
        "LCM_SKEW_FLOOR",
        "LCM_CALIBRATION_HARD_FRAC",
        "LCM_FRESH_TAIL_TOKEN_BUDGET",
    ):
        monkeypatch.delenv(key, raising=False)
    # getattr: lets the count assertion (not an AttributeError) prove RED on
    # pre-fix code that has no cache.
    getattr(lcm_config, "_reset_config_yaml_cache", lambda: None)()
    return home


def test_engine_load_parses_config_yaml_at_most_once(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    calls = _count_parses(monkeypatch)

    # Same shape as plugins/context_engine/lcm/__init__.py::register.
    config = LCMConfig.from_env()
    config.database_path = str(tmp_path / "lcm.db")
    engine = LCMEngine(config=config, hermes_home=str(home))
    try:
        assert calls[0] <= 1, (
            f"one engine load parsed config.yaml {calls[0]}x; the contract is <= 1 "
            "(every knob must read the same parsed dict)"
        )
        # The single parse still feeds every knob.
        assert config.context_threshold == 0.66
        assert config.target_ratio == 0.22
        assert config.skew_floor == 0.8
        assert config.calibration_hard_frac == 0.92
        assert config.fresh_tail_token_budget == 1234
        assert config.summary_timeout_ms == 90_000
        assert "some_unsupported_key" in config.ignored_config_yaml_lcm_keys
    finally:
        engine._close_storage()


def test_unchanged_config_is_not_reparsed_and_an_edit_is_seen(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    LCMConfig.from_env()
    calls = _count_parses(monkeypatch)

    assert LCMConfig.from_env().context_threshold == 0.66
    assert calls[0] == 0, "an unchanged config.yaml must not be parsed again"

    # Same byte length, so a size/mtime key alone could miss it.
    edited = _CONFIG_YAML.replace("context_threshold: 0.66", "context_threshold: 0.77")
    assert len(edited) == len(_CONFIG_YAML)
    (home / "config.yaml").write_text(edited, encoding="utf-8")
    assert LCMConfig.from_env().context_threshold == 0.77
    assert calls[0] == 1


def test_cached_dict_is_not_shared_with_callers(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    first = lcm_config._hermes_config_yaml()
    first["lcm"]["context_threshold"] = 0.01
    assert lcm_config._hermes_config_yaml()["lcm"]["context_threshold"] == 0.66


def test_missing_config_file_is_empty(tmp_path, monkeypatch):
    home = _home(tmp_path, monkeypatch)
    (home / "config.yaml").unlink()
    assert lcm_config._hermes_config_yaml() == {}


def test_concurrent_misses_parse_once(tmp_path, monkeypatch):
    """FleetReview #85: the cache lock was released across the parse, so N
    threads missing together each parsed (and the slowest could overwrite a
    newer entry). The miss path is serialized now: one parse."""
    import threading
    import time

    _home(tmp_path, monkeypatch)
    lcm_config._reset_config_yaml_cache()
    calls = [0]
    real = lcm_config._load_hermes_config_yaml

    def slow(*args, **kwargs):
        calls[0] += 1
        time.sleep(0.2)
        return real(*args, **kwargs)

    monkeypatch.setattr(lcm_config, "_load_hermes_config_yaml", slow)
    go = threading.Barrier(6)
    out = []

    def read():
        go.wait()
        out.append(lcm_config._hermes_config_yaml())

    ts = [threading.Thread(target=read) for _ in range(6)]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    assert len(out) == 6 and all(o["lcm"]["context_threshold"] == 0.66 for o in out)
    assert calls[0] == 1


def test_miss_rereads_the_file_under_the_parse_lock(tmp_path, monkeypatch):
    """FleetReview on #1416: a thread that read the OLD text, then waited on the
    parse lock while another thread cached a NEWER edit, parsed and stored the
    old text over it (and returned it). The miss path re-reads under the lock."""
    import threading

    home = _home(tmp_path, monkeypatch)
    lcm_config._reset_config_yaml_cache()
    real_lock = lcm_config._CONFIG_YAML_PARSE_LOCK
    waiting = threading.Event()

    class _Lock:
        def __enter__(self):
            waiting.set()
            return real_lock.__enter__()

        def __exit__(self, *exc):
            return real_lock.__exit__(*exc)

    monkeypatch.setattr(lcm_config, "_CONFIG_YAML_PARSE_LOCK", _Lock())
    out = []
    newer = _CONFIG_YAML.replace("context_threshold: 0.66", "context_threshold: 0.77")
    with real_lock:
        t = threading.Thread(target=lambda: out.append(lcm_config._hermes_config_yaml()))
        t.start()
        assert waiting.wait(5)  # it has read the old text and wants the lock
        (home / "config.yaml").write_text(newer, encoding="utf-8")
        lcm_config._config_yaml_cache = (
            str(home / "config.yaml"), newer, lcm_config._load_hermes_config_yaml(newer))
    t.join(5)
    assert out and out[0]["lcm"]["context_threshold"] == 0.77
    assert lcm_config._config_yaml_cache[1] == newer
