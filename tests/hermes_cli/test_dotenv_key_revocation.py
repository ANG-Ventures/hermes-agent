"""Removing (or blanking) a credential line in ``.env`` revokes it (t_8dccb8ef).

Editing a value in ``.env`` already rotated a running process's key on the next
read (``get_env_prefer_dotenv`` prefers the file).  Removing the line did not:
the resolver fell back to the copy the boot/per-turn dotenv load had left in
``os.environ`` / the secret scope, so the removed key kept being sent until a
restart.  These tests drive the real resolution chain against a temp
``HERMES_HOME`` — real ``.env`` file, real ``load_env`` memo, real
``load_hermes_dotenv`` — and pin that removal == revocation for values that
came from the file, while values that never came from it (shell / systemd /
external secret source) still fall through unchanged.
"""

import os

import pytest

import agent.credential_pool as cp
from hermes_cli import config as cfg
from hermes_cli import env_loader

KEY = "CLAUDE_API_PROXY_F1_KEY"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv(KEY, raising=False)
    monkeypatch.setattr(env_loader, "_DOTENV_SEEN", {})
    cfg.invalidate_env_cache()
    yield tmp_path
    cfg.invalidate_env_cache()


def _write(home, text):
    (home / ".env").write_text(text, encoding="utf-8")
    cfg.invalidate_env_cache()


def _boot_load(home, monkeypatch):
    """What a gateway does at boot / per turn: dotenv -> os.environ (override)."""
    env_loader.load_hermes_dotenv(hermes_home=home, load_external_secrets=False)
    # the loader wrote into the real os.environ; let monkeypatch restore it
    monkeypatch.setenv(KEY, os.environ.get(KEY, ""))


@pytest.mark.parametrize("after", ["", f"{KEY}=\n", f"{KEY}=   \n"])
def test_removed_or_blanked_line_is_revoked_after_use(home, monkeypatch, after):
    _write(home, f"{KEY}=sk-old-value-1\n")
    _boot_load(home, monkeypatch)
    assert cp.get_env_prefer_dotenv(KEY) == "sk-old-value-1"   # a turn used it

    _write(home, after)
    assert os.environ.get(KEY) == "sk-old-value-1"              # the stale copy is still there
    assert cp.get_env_prefer_dotenv(KEY) == ""
    assert cfg.get_env_value_prefer_dotenv(KEY) is None


def test_removed_before_first_read_is_revoked(home, monkeypatch):
    """Seeded by load_hermes_dotenv itself, not only by a prior load_env read."""
    _write(home, f"{KEY}=sk-boot-only\n")
    _boot_load(home, monkeypatch)
    _write(home, "OTHER=1\n")
    assert os.environ.get(KEY) == "sk-boot-only"
    assert cp.get_env_prefer_dotenv(KEY) == ""


def test_rotation_still_takes_effect(home, monkeypatch):
    _write(home, f"{KEY}=sk-old\n")
    _boot_load(home, monkeypatch)
    assert cp.get_env_prefer_dotenv(KEY) == "sk-old"
    _write(home, f"{KEY}=sk-new\n")
    assert cp.get_env_prefer_dotenv(KEY) == "sk-new"
    _write(home, "")                                            # then removed
    assert cp.get_env_prefer_dotenv(KEY) == ""


def test_re_adding_after_removal_restores(home, monkeypatch):
    _write(home, f"{KEY}=sk-a\n")
    _boot_load(home, monkeypatch)
    _write(home, "")
    assert cp.get_env_prefer_dotenv(KEY) == ""
    _write(home, f"{KEY}=sk-a\n")
    assert cp.get_env_prefer_dotenv(KEY) == "sk-a"


def test_shell_export_never_in_dotenv_still_resolves(home, monkeypatch):
    _write(home, "OTHER=1\n")
    monkeypatch.setenv(KEY, "sk-from-shell")
    assert cp.get_env_prefer_dotenv(KEY) == "sk-from-shell"
    assert cfg.get_env_value_prefer_dotenv(KEY) == "sk-from-shell"


def test_different_process_value_is_not_revoked(home, monkeypatch):
    """A fallback that differs from what the file carried did not come from it
    (e.g. an external secret source wrote it later) and is left alone."""
    _write(home, f"{KEY}=sk-file\n")
    assert cp.get_env_prefer_dotenv(KEY) == "sk-file"
    _write(home, "")
    monkeypatch.setenv(KEY, "sk-from-secret-manager")
    assert cp.get_env_prefer_dotenv(KEY) == "sk-from-secret-manager"


def test_removed_op_reference_revokes_its_resolution(home, monkeypatch):
    _write(home, f"{KEY}=op://Vault/Item/field\n")
    monkeypatch.setenv(KEY, "sk-resolved-by-1password")
    assert cp.get_env_prefer_dotenv(KEY) == "sk-resolved-by-1password"
    _write(home, "")
    assert cp.get_env_prefer_dotenv(KEY) == ""
