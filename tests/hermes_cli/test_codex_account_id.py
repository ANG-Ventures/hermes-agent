"""Codex account identity has one parser shared by sync and HTTP callers."""
import ast
import base64
import json
from pathlib import Path

import pytest


def _jwt(claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


@pytest.mark.parametrize("account,expected", [
    ("account-A", "account-A"), ("  account-A  ", "account-A"),
    ("", None), ("  ", None), (None, None), (123, None), ([], None),
])
def test_codex_account_id(account, expected):
    from hermes_cli.auth import get_codex_account_id

    assert get_codex_account_id(_jwt({"https://api.openai.com/auth": {
        "chatgpt_account_id": account,
    }})) == expected


@pytest.mark.parametrize("token", [
    None, 123, "", "opaque-token", "bad.jwt.token", "header.payload",
    _jwt([]), _jwt({}), _jwt({"https://api.openai.com/auth": None}),
    _jwt({"https://api.openai.com/auth": []}),
])
def test_codex_account_id_unknown(token):
    from hermes_cli.auth import get_codex_account_id

    assert get_codex_account_id(token) is None


@pytest.mark.parametrize("account", ["account-A", "  account-A  ", "", 123])
def test_codex_account_id_consumers_agree(account, monkeypatch):
    import hermes_cli.auth as auth
    from agent.auxiliary_client import _codex_cloudflare_headers
    from agent.model_metadata import _extract_chatgpt_account_id as metadata_account
    from hermes_cli.codex_models import _extract_chatgpt_account_id as catalog_account

    token = _jwt({"https://api.openai.com/auth": {"chatgpt_account_id": account}})
    expected = auth.get_codex_account_id(token)
    assert metadata_account(token) == expected
    assert catalog_account(token) == expected
    assert _codex_cloudflare_headers(token).get("ChatGPT-Account-ID") == expected

    import httpx
    from types import SimpleNamespace

    headers = []

    def transport(request):
        headers.append(request.headers.get("ChatGPT-Account-Id"))
        return httpx.Response(200, json={
            "rate_limit": {"primary_window": {"used_percent": 0}},
        })

    monkeypatch.setattr(auth, "httpx", SimpleNamespace(
        Client=lambda **kw: httpx.Client(transport=httpx.MockTransport(transport), **kw),
    ))
    assert auth._probe_codex_quota_restored(token, min_interval_seconds=0) is True
    assert headers == [expected]


def _account_claim_readers(root):
    readers = []
    for directory in ("agent", "hermes_cli"):
        for path in (root / directory).rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Constant) and node.value == "chatgpt_account_id":
                    readers.append(path.relative_to(root).as_posix())
    return sorted(readers)


def test_codex_account_id_parser_is_single_sourced():
    root = Path(__file__).resolve().parents[2]
    assert _account_claim_readers(root) == ["hermes_cli/auth.py"]


def test_codex_account_id_drift_detector_sees_new_copy(tmp_path):
    (tmp_path / "agent").mkdir()
    copy = tmp_path / "agent" / "new_client.py"
    copy.write_text('account = claims.get("chatgpt_account_id")\n')
    assert _account_claim_readers(tmp_path) == ["agent/new_client.py"]
