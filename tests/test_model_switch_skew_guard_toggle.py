"""Tests for the model-switch stale-code guard's config toggle.

Companion to ``tests/test_code_skew.py`` (which covers the DETECTOR). These
cover the ``model.stale_code_switch_guard`` toggle that decides whether a
detected skew actually REFUSES the switch.
"""

import pytest

from gateway import slash_commands


@pytest.fixture
def _skew_present(monkeypatch):
    """Force ``detect_code_skew`` to report a skew."""
    from gateway import code_skew

    monkeypatch.setattr(
        code_skew, "detect_code_skew", lambda: ("abc1234567", "def4567890")
    )


def _write_config(tmp_path, body: str, monkeypatch):
    (tmp_path / "config.yaml").write_text(body, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # read_raw_config caches on (mtime_ns, size); a fresh temp path is a fresh key.
    from hermes_cli import config as cfg_mod

    cfg_mod._RAW_CONFIG_CACHE.clear()


class TestStaleCodeSwitchGuardToggle:
    def test_default_is_enabled_when_key_absent(self, tmp_path, monkeypatch):
        _write_config(tmp_path, "model:\n  default: some-model\n", monkeypatch)
        assert slash_commands._stale_code_switch_guard_enabled() is True

    def test_default_is_enabled_when_no_config_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        from hermes_cli import config as cfg_mod

        cfg_mod._RAW_CONFIG_CACHE.clear()
        assert slash_commands._stale_code_switch_guard_enabled() is True

    def test_explicit_false_disables(self, tmp_path, monkeypatch):
        _write_config(
            tmp_path, "model:\n  stale_code_switch_guard: false\n", monkeypatch
        )
        assert slash_commands._stale_code_switch_guard_enabled() is False

    def test_explicit_true_enables(self, tmp_path, monkeypatch):
        _write_config(
            tmp_path, "model:\n  stale_code_switch_guard: true\n", monkeypatch
        )
        assert slash_commands._stale_code_switch_guard_enabled() is True

    @pytest.mark.parametrize("raw,expected", [("off", False), ("on", True),
                                              ("no", False), ("yes", True),
                                              ("0", False), ("1", True)])
    def test_string_forms(self, tmp_path, monkeypatch, raw, expected):
        _write_config(
            tmp_path,
            f'model:\n  stale_code_switch_guard: "{raw}"\n',
            monkeypatch,
        )
        assert slash_commands._stale_code_switch_guard_enabled() is expected

    def test_malformed_value_fails_safe_to_enabled(self, tmp_path, monkeypatch):
        _write_config(
            tmp_path, "model:\n  stale_code_switch_guard: banana\n", monkeypatch
        )
        assert slash_commands._stale_code_switch_guard_enabled() is True

    def test_malformed_model_section_fails_safe_to_enabled(
        self, tmp_path, monkeypatch
    ):
        _write_config(tmp_path, "model: not-a-mapping\n", monkeypatch)
        assert slash_commands._stale_code_switch_guard_enabled() is True

    def test_config_read_exception_fails_safe_to_enabled(self, monkeypatch):
        def _boom():
            raise RuntimeError("config unavailable")

        monkeypatch.setattr("hermes_cli.config.read_raw_config", _boom)
        assert slash_commands._stale_code_switch_guard_enabled() is True


class TestGuardWiring:
    """The toggle must actually gate the refusal message."""

    def test_skew_refuses_when_enabled(self, _skew_present, monkeypatch):
        monkeypatch.setattr(
            slash_commands, "_stale_code_switch_guard_enabled", lambda: True
        )
        msg = slash_commands._model_switch_skew_guard()
        assert msg is not None
        assert "abc1234567" in msg and "def4567890" in msg

    def test_skew_allows_when_disabled(self, _skew_present, monkeypatch):
        monkeypatch.setattr(
            slash_commands, "_stale_code_switch_guard_enabled", lambda: False
        )
        assert slash_commands._model_switch_skew_guard() is None

    def test_disabled_logs_a_warning_breadcrumb(
        self, _skew_present, monkeypatch, caplog
    ):
        monkeypatch.setattr(
            slash_commands, "_stale_code_switch_guard_enabled", lambda: False
        )
        with caplog.at_level("WARNING"):
            assert slash_commands._model_switch_skew_guard() is None
        assert any(
            "stale-code guard DISABLED" in r.getMessage() for r in caplog.records
        )

    def test_no_skew_never_consults_the_toggle(self, monkeypatch):
        from gateway import code_skew

        monkeypatch.setattr(code_skew, "detect_code_skew", lambda: None)

        def _should_not_be_called():
            raise AssertionError("toggle read on a no-skew path")

        monkeypatch.setattr(
            slash_commands,
            "_stale_code_switch_guard_enabled",
            _should_not_be_called,
        )
        assert slash_commands._model_switch_skew_guard() is None
