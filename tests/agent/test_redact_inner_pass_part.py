"""``PASS`` inside a longer env name (``MERGE_PASS_CARD_EVENT=0``) is a word, not a password.

_ENV_ASSIGN_RE embeds the keyword anywhere in an all-caps key, so the agent's own
stored assistant turn ``MERGE_PASS_CARD_EVENT=0`` was rewritten to ``=***`` and the
replayed history broke prompt-cache continuity (t_b924fb6d). Password-shaped names
must keep masking.
"""

import pytest

from agent.redact import redact_sensitive_text


@pytest.fixture(autouse=True)
def _ensure_redaction_enabled(monkeypatch):
    monkeypatch.setattr("agent.redact._REDACT_ENABLED", True)


@pytest.mark.parametrize("text", [
    "MERGE_PASS_CARD_EVENT=0",
    "export MERGE_PASS_CARD_EVENT=0 && run",
    "REVIEW_PASS_COUNT=3",
    "SECOND_PASS_ENABLED=true",
    "PASS_THROUGH=1",
])
def test_inner_pass_part_untouched(text):
    assert redact_sensitive_text(text) == text
    assert redact_sensitive_text(text, force=True) == text


@pytest.mark.parametrize("text,secret", [
    ("DB_PASS=hunter2", "hunter2"),
    ("MYSQL_PASSWORD=x", "=x"),
    ("PASS=hunter2", "hunter2"),
    ("MYSQL_PASSWORD_ROOT=hunter2", "hunter2"),
    ("DB_PASSWD=hunter2", "hunter2"),
    ("GPG_PASS_PHRASE=hunter2", "hunter2"),
    ("SMTP_PASS_WORD=hunter2", "hunter2"),
    ("VAULT_PASS_KEY=hunter2", "hunter2"),
    ("MERGE_PASS_SECRET=hunter2", "hunter2"),  # another keyword still counts
    # Unknown qualifiers are passwords (Prism #2461 P1s): allowlist, not denylist.
    ("DB_PASS_PROD=hunter2", "hunter2"),
    ("DB_PASS_BACKUP=hunter2", "hunter2"),
    ("ADMIN_PASS_NEW=hunter2", "hunter2"),
    ("ROOT_PASS_OLD=hunter2", "hunter2"),
    ("MYSQL_PASS_2=hunter2", "hunter2"),
    ("USER_PASS_HASH=hunter2", "hunter2"),
])
def test_password_names_still_masked(text, secret):
    out = redact_sensitive_text(text, force=True)
    assert "***" in out, out
    assert secret not in out, out
