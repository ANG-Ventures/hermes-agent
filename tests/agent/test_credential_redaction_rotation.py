"""Rotating a configured credential must not un-redact the value it displaces (Prism d352a3deb1bc).

During a rotation both credentials can be valid at once, so a later read of a backup config must still
scrub the old one. The displaced value moves to the bounded vault bucket; the current value keeps the
non-evictable slot.
"""

import pytest

from agent import redact


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(redact, "_VAULT_REDACTION_VALUES", {})
    monkeypatch.setattr(redact, "_CREDENTIAL_REDACTION_VALUES", {})


OLD = "old-admin-" + "5c1e" * 10
NEW = "new-admin-" + "9b7f" * 10


def test_rotated_out_value_stays_redacted():
    redact.register_credential_redaction_value("mem0.admin_api_key", OLD)
    redact.register_credential_redaction_value("mem0.admin_api_key", NEW)

    out = redact.redact_registered_vault_values(f"current={NEW} backup={OLD}")

    assert OLD not in out
    assert NEW not in out


def test_current_value_survives_vault_eviction_after_rotation():
    redact.register_credential_redaction_value("mcp:x.token", OLD)
    redact.register_credential_redaction_value("mcp:x.token", NEW)
    for i in range(redact._VAULT_REDACTION_MAX_PER_PROFILE * 2):
        redact.register_vault_redaction_value(f"otp-{i:06d}")

    # The displaced value is bounded (it ages out like any vault fill); the current one never does.
    assert NEW not in redact.redact_registered_vault_values(f"t={NEW}")
    assert OLD in redact.redact_registered_vault_values(f"t={OLD}")


def test_reregistering_same_value_does_not_consume_vault_bucket():
    for _ in range(3):
        redact.register_credential_redaction_value("mem0.admin_api_key", NEW)

    assert not redact._VAULT_REDACTION_VALUES.get(redact._vault_scope())
    assert NEW not in redact.redact_registered_vault_values(NEW)


def test_repeated_rotation_stays_bounded():
    for i in range(redact._VAULT_REDACTION_MAX_PER_PROFILE * 3):
        redact.register_credential_redaction_value("mem0.admin_api_key", f"rot-{i:04d}-" + "a" * 20)

    scope = redact._vault_scope()
    assert len(redact._CREDENTIAL_REDACTION_VALUES[scope]) == 1
    assert len(redact._VAULT_REDACTION_VALUES[scope]) <= redact._VAULT_REDACTION_MAX_PER_PROFILE
