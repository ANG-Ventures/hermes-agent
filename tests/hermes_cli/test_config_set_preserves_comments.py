"""``config set`` must not strip comments from config.yaml (t_b8816229).

Comments in profile configs carry rulings that lints cite; a one-line set
used to re-dump the whole file through PyYAML and delete every comment.
"""

import os
from unittest.mock import patch

import pytest

from hermes_cli import config as config_mod
from hermes_cli.config import set_config_value


FIXTURE = """\
# Top-of-file note: RULED values below are cited by fleet-config-lint.
model:
  default: gpt-x  # pinned by ops
  provider: openrouter
agent:
  # RULED 3600 (t_115415a6): do not lower without a ruling.
  idle_compact_after_seconds: 3600
  reasoning_effort: medium
  max_turns: 90
display:
  skin: default
custom_providers:
  # first provider stays commented
  - name: local
    api_key: old  # rotated quarterly
"""


@pytest.fixture(autouse=True)
def _isolated_hermes_home(tmp_path):
    """Point HERMES_HOME at a temp dir so tests never touch real config."""
    env_file = tmp_path / ".env"
    env_file.touch()
    with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
        yield tmp_path


def _cfg(home):
    return home / "config.yaml"


def _write(home, text=FIXTURE):
    _cfg(home).write_text(text, encoding="utf-8")


def test_set_existing_scalar_is_byte_identical_except_that_line(_isolated_hermes_home):
    _write(_isolated_hermes_home)
    before = FIXTURE.splitlines(keepends=True)

    set_config_value("agent.reasoning_effort", "high")

    after = _cfg(_isolated_hermes_home).read_text(encoding="utf-8").splitlines(keepends=True)
    assert len(after) == len(before)
    changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
    assert changed == [before.index("  reasoning_effort: medium\n")]
    assert after[changed[0]] == "  reasoning_effort: high\n"


def test_set_scalar_with_inline_comment_keeps_the_comment(_isolated_hermes_home):
    _write(_isolated_hermes_home)

    set_config_value("model.default", "gpt-y")

    text = _cfg(_isolated_hermes_home).read_text(encoding="utf-8")
    assert text == FIXTURE.replace("default: gpt-x  # pinned", "default: gpt-y  # pinned")


def test_new_key_is_inserted_without_touching_other_lines(_isolated_hermes_home):
    _write(_isolated_hermes_home)

    set_config_value("agent.max_iterations_note", "7")

    text = _cfg(_isolated_hermes_home).read_text(encoding="utf-8")
    assert text == FIXTURE.replace(
        "  max_turns: 90\n", "  max_turns: 90\n  max_iterations_note: 7\n"
    )


def test_list_index_write_keeps_comments(_isolated_hermes_home):
    _write(_isolated_hermes_home)

    set_config_value("custom_providers.0.api_key", "new")

    text = _cfg(_isolated_hermes_home).read_text(encoding="utf-8")
    for line in FIXTURE.splitlines():
        if "#" in line and "api_key" not in line:
            assert line in text
    assert "rotated quarterly" in text
    assert config_mod.yaml.safe_load(text)["custom_providers"][0]["api_key"] == "new"


def _force_lossy_render(monkeypatch):
    """Simulate a render path that cannot keep comments (PyYAML fallback)."""
    import utils

    monkeypatch.setattr(config_mod, "_targeted_config_edit", lambda *a, **k: None)

    def _boom(*_a, **_k):
        raise RuntimeError("ruamel unavailable")

    monkeypatch.setattr(utils, "roundtrip_yaml_render", _boom)


def test_refuses_with_diff_when_comments_would_be_lost(_isolated_hermes_home, monkeypatch, capsys):
    _write(_isolated_hermes_home)
    _force_lossy_render(monkeypatch)

    with pytest.raises(SystemExit) as exc:
        set_config_value("agent.reasoning_effort", "high")

    assert exc.value.code == 1
    assert _cfg(_isolated_hermes_home).read_text(encoding="utf-8") == FIXTURE
    err = capsys.readouterr().err
    assert "drop" in err and "comment" in err
    assert "-  # RULED 3600 (t_115415a6): do not lower without a ruling." in err
    assert "--force" in err


def test_force_accepts_comment_loss(_isolated_hermes_home, monkeypatch):
    _write(_isolated_hermes_home)
    _force_lossy_render(monkeypatch)

    set_config_value("agent.reasoning_effort", "high", force=True)

    text = _cfg(_isolated_hermes_home).read_text(encoding="utf-8")
    assert "RULED" not in text
    assert config_mod.yaml.safe_load(text)["agent"]["reasoning_effort"] == "high"


def test_custom_toplevel_notice_only_for_custom_toplevel_keys(_isolated_hermes_home, capsys):
    _write(_isolated_hermes_home)

    set_config_value("agent.not_a_real_subkey_xyz", "1")
    out = capsys.readouterr().out
    assert "not a recognized config key" in out
    assert "Custom top-level keys" not in out

    set_config_value("my_custom_toplevel_xyz", "1")
    assert "Custom top-level keys" in capsys.readouterr().out
