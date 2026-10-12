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


def test_rotated_out_multiline_value_stays_redacted_when_flattened():
    # A displaced credential must carry the same CR/LF-stripped form a vault fill registers (Prism 9dd9c5c14589):
    # the model usually sees the old value only after a tool flattened it.
    old = "old-admin-\r\n" + "5c1e" * 10 + "\n"
    old_flat = "old-admin-" + "5c1e" * 10
    redact.register_credential_redaction_value("mem0.admin_api_key", old)
    redact.register_credential_redaction_value("mem0.admin_api_key", NEW)

    out = redact.redact_registered_vault_values(f"current={NEW} backup={old_flat} raw={old}")

    assert old_flat not in out
    assert old not in out
    assert NEW not in out


def test_current_multiline_credential_is_redacted_when_flattened():
    # Same class on the non-evictable slot: the current credential's flattened form must scrub too.
    cur = "cur-admin-\r\n" + "7d2a" * 10 + "\n"
    cur_flat = "cur-admin-" + "7d2a" * 10
    redact.register_credential_redaction_value("mem0.admin_api_key", cur)

    out = redact.redact_registered_vault_values(f"flat={cur_flat} raw={cur}")

    assert cur_flat not in out
    assert cur not in out


def test_crlf_only_credential_does_not_corrupt_unrelated_output():
    # A CR/LF-only value normalizes to "", which matches every text (Prism 5d5afe2c8d8e).
    redact.register_credential_redaction_value("mem0.admin_api_key", "\r\n")

    assert redact.redact_registered_vault_values("hello world") == "hello world"


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
