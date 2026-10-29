"""Fleet env names whose ``PASS`` is a merge/review "pass" are not passwords; nothing else changes.

_ENV_ASSIGN_RE embeds the keyword anywhere in an all-caps key, so the agent's own stored
assistant turn ``MERGE_PASS_CARD_EVENT=0`` was rewritten to ``=***`` and the replayed history
broke prompt-cache continuity (t_b924fb6d). The exemption is an EXACT-name list: a word list
of "safe" qualifiers leaked DB_PASS_STAGE / DB_PASS_CARD (Prism #2461), so every name not on
the list must mask exactly as before.
"""

import pytest

from agent.redact import _PASS_WORD_ENV_NAMES, redact_sensitive_text


@pytest.fixture(autouse=True)
def _ensure_redaction_enabled(monkeypatch):
    monkeypatch.setattr("agent.redact._REDACT_ENABLED", True)


@pytest.mark.parametrize("name", sorted(_PASS_WORD_ENV_NAMES))
def test_listed_names_untouched(name):
    for text in (f"{name}=0", f"export {name}=0 && run", f"{name}=hunter2"):
        assert redact_sensitive_text(text) == text
        assert redact_sensitive_text(text, force=True) == text


def test_list_holds_only_exact_pass_names():
    # Every exemption is one exact all-caps env name containing a PASS part, never a word or pattern.
    for name in _PASS_WORD_ENV_NAMES:
        assert name == name.upper() and "_PASS_" in f"_{name}_", name


@pytest.mark.parametrize("text,secret", [
    ("DB_PASS=hunter2", "hunter2"),
    ("MYSQL_PASSWORD=x", "=x"),
    ("PASS=hunter2", "hunter2"),
    ("DB_PASSWD=hunter2", "hunter2"),
    ("MYSQL_PASSWORD_ROOT=hunter2", "hunter2"),
    ("GPG_PASS_PHRASE=hunter2", "hunter2"),
    ("SMTP_PASS_WORD=hunter2", "hunter2"),
    ("VAULT_PASS_KEY=hunter2", "hunter2"),
    ("MERGE_PASS_SECRET=hunter2", "hunter2"),
    # Qualifiers a word list exempted or could exempt (Prism #2461 rounds 1-3).
    ("DB_PASS_STAGE=hunter2", "hunter2"),
    ("MYSQL_PASS_STAGE=hunter2", "hunter2"),
    ("DB_PASS_STEP=hunter2", "hunter2"),
    ("DB_PASS_ROUND=hunter2", "hunter2"),
    ("DB_PASS_CARD=hunter2", "hunter2"),
    ("DB_PASS_PROD=hunter2", "hunter2"),
    ("DB_PASS_BACKUP=hunter2", "hunter2"),
    ("ADMIN_PASS_NEW=hunter2", "hunter2"),
    ("ROOT_PASS_OLD=hunter2", "hunter2"),
    ("MYSQL_PASS_2=hunter2", "hunter2"),
    ("USER_PASS_HASH=hunter2", "hunter2"),
    # Near-misses of listed names, and names the old word list exempted, are not on the list.
    ("MERGE_PASS_CARD_EVENT_X=hunter2", "hunter2"),
    ("X_MERGE_PASS_CARD_EVENT=hunter2", "hunter2"),
    ("REVIEW_PASS_COUNT=hunter2", "hunter2"),
    ("SECOND_PASS_ENABLED=hunter2", "hunter2"),
    ("UNIFI_FORGE_PASS_FILE=hunter2", "hunter2"),
])
def test_password_names_still_masked(text, secret):
    out = redact_sensitive_text(text, force=True)
    assert "***" in out, out
    assert secret not in out, out


@pytest.mark.parametrize("line", [
    "DB_PASS_CARD_PROD=x",          # Prism e565ccfc9b25: a known word followed by a qualifier
    "DB_PASS_EVENT_SECRET=x",
    "ROOT_PASS_COUNT_OLD=x",
    "MYSQL_PASS_CARD_=x",
])
def test_multi_part_password_names_mask(line):
    out = redact_sensitive_text(line, force=True)
    assert out != line and "=x" not in out, out
