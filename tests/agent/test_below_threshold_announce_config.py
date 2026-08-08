"""The operator kill switch must actually read config.yaml.

The classic failure here is a knob that looks wired but is stuck at its default
because nothing parses it (see skill hermes-config-knob-authoring). This binds
against the REAL loader and a REAL temp config.yaml, both directions.
"""
import importlib

import pytest

import agent.context_engine as ce


@pytest.fixture(autouse=True)
def _clear_config_cache():
    """The loader is mtime+size cached per path; reimport to dodge staleness."""
    yield
    try:
        import hermes_cli.config as hc

        importlib.reload(hc)
    except Exception:
        pass


def _write_cfg(tmp_path, body):
    (tmp_path / "config.yaml").write_text(body)


def test_defaults_to_announcing_when_key_absent(tmp_path, monkeypatch):
    _write_cfg(tmp_path, "compression:\n  threshold: 0.75\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import hermes_cli.config as hc

    importlib.reload(hc)
    assert ce._below_threshold_announce_enabled() is True


def test_explicit_false_suppresses(tmp_path, monkeypatch):
    _write_cfg(
        tmp_path,
        "compression:\n  announce_below_threshold_compaction: false\n",
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import hermes_cli.config as hc

    importlib.reload(hc)
    assert ce._below_threshold_announce_enabled() is False


def test_explicit_true_announces(tmp_path, monkeypatch):
    _write_cfg(
        tmp_path,
        "compression:\n  announce_below_threshold_compaction: true\n",
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import hermes_cli.config as hc

    importlib.reload(hc)
    assert ce._below_threshold_announce_enabled() is True


def test_missing_compression_section_defaults_on(tmp_path, monkeypatch):
    _write_cfg(tmp_path, "model:\n  default: x\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import hermes_cli.config as hc

    importlib.reload(hc)
    assert ce._below_threshold_announce_enabled() is True


def test_config_read_failure_defaults_to_announcing(monkeypatch):
    """Fail OPEN: a broken config must not silence the explanation."""

    def _boom():
        raise RuntimeError("config exploded")

    monkeypatch.setattr("hermes_cli.config.load_config_readonly", _boom)
    assert ce._below_threshold_announce_enabled() is True
