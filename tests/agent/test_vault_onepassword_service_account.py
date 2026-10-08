"""1Password vault backend under service-account auth: every item read names its owning vault.

A service account refuses an unscoped ``op item get`` ("a vault query must be provided"), so
``resolve_password``/``resolve_otp`` must pass ``--vault`` taken from the item listing. The fake
``op`` below enforces that contract (and refuses a vault that does not own the item), so a
regression to an unscoped read fails here exactly as it failed live.
"""

from __future__ import annotations

import json
import stat
from unittest.mock import patch

import pytest

from agent.vault_backends.onepassword import OnePasswordLoginBackend

pytestmark = pytest.mark.platforms("posix")  # fake op is a shebang script

_FAKE_OP = r'''#!/usr/bin/env python3
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
argv = sys.argv[1:]
with open(os.path.join(here, "op.log"), "a") as log:
    log.write(json.dumps({"argv": argv, "sa": bool(os.environ.get("OP_SERVICE_ACCOUNT_TOKEN"))}) + "\n")
if not os.environ.get("OP_SERVICE_ACCOUNT_TOKEN"):
    sys.stderr.write("not signed in\n"); sys.exit(1)
items = json.load(open(os.path.join(here, "items.json")))
secrets = {"aaa": ("pw in vault one", "123456"), "bbb": ("pw in vault two", "654321"), "nootp": ("pw3", None)}
if argv[:2] == ["item", "list"]:
    print(json.dumps(items)); sys.exit(0)
if argv[:2] == ["item", "get"]:
    if "--vault" not in argv:
        sys.stderr.write('[ERROR] a vault query must be provided when using service accounts\n'); sys.exit(1)
    item_id, vault = argv[2], argv[argv.index("--vault") + 1]
    owner = {i["id"]: i["vault"]["id"] for i in items}.get(item_id)
    if owner != vault:
        sys.stderr.write(f'[ERROR] "{item_id}" isn\'t an item in the "{vault}" vault\n'); sys.exit(1)
    pw, otp = secrets[item_id]
    if "--otp" in argv:
        if otp is None:
            sys.stderr.write("no one-time password field\n"); sys.exit(1)
        print(otp); sys.exit(0)
    if argv[argv.index("--fields") + 1] == "label=password" and "--reveal" in argv:
        print(pw); sys.exit(0)
sys.exit(2)
'''


def _item(item_id, vault_id, title, url="https://example.com/login"):
    return {"id": item_id, "title": title, "created_at": "2026-01-01T00:00:00Z",
            "additional_information": "jane@example.com", "urls": [{"href": url}],
            "vault": {"id": vault_id, "name": vault_id.upper()}}


_ITEMS = [
    _item("aaa", "vone", "Example"),
    _item("bbb", "vtwo", "Example"),  # same title, different vault: resolution is by id + owning vault
    _item("nootp", "vone", "No OTP"),
]


@pytest.fixture
def op_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    exe = tmp_path / "op"
    exe.write_text(_FAKE_OP, encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    items = tmp_path / "items.json"
    items.write_text(json.dumps(_ITEMS), encoding="utf-8")
    backend = OnePasswordLoginBackend({"enabled": True, "binary_path": str(exe)})
    backend._service_token = "sa-token-for-test"  # service-account auth: no unlock, every read scoped
    log = tmp_path / "op.log"

    def calls():
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []

    return backend, items, calls


def _gets(calls):
    return [c["argv"] for c in calls() if c["argv"][:2] == ["item", "get"]]


def test_fake_op_rejects_unscoped_item_get(op_backend):
    """The fake reproduces the live failure, so the tests below can only pass with --vault."""
    backend, _, _ = op_backend
    with pytest.raises(RuntimeError, match="a vault query must be provided"):
        backend._run("item", "get", "aaa", "--fields", "label=password", "--reveal")


def test_password_and_otp_carry_the_exact_owning_vault(op_backend):
    backend, _, calls = op_backend
    assert backend.resolve_password("op:aaa") == "pw in vault one"
    assert backend.resolve_password("op:bbb") == "pw in vault two"
    assert backend.resolve_otp("op:aaa") == "123456"
    assert backend.resolve_otp("op:bbb") == "654321"
    assert backend.resolve_otp("op:nootp") is None  # item without a TOTP field: the user is asked
    for argv in _gets(calls):
        owner = {i["id"]: i["vault"]["id"] for i in _ITEMS}[argv[2]]
        assert argv[argv.index("--vault") + 1] == owner
    assert all(c["sa"] for c in calls())


@pytest.mark.parametrize("handle", ["op:missing", "op:", "op:--vault", "bw:aaa"])
def test_missing_or_foreign_item_fails_closed_without_any_item_read(op_backend, handle):
    backend, _, calls = op_backend
    with pytest.raises(RuntimeError, match="not listed"):
        backend.resolve_password(handle)
    assert backend.resolve_otp(handle) is None
    assert _gets(calls) == []


def test_ambiguous_listing_fails_closed(op_backend):
    """The same id listed twice (two vaults) gives no single owner: refuse rather than pick one."""
    backend, items, calls = op_backend
    items.write_text(json.dumps(_ITEMS + [_item("aaa", "vtwo", "Example copy")]), encoding="utf-8")
    with pytest.raises(RuntimeError, match="ambiguous"):
        backend.resolve_password("op:aaa")
    assert backend.resolve_otp("op:aaa") is None
    assert _gets(calls) == []


@pytest.mark.parametrize("vault", [None, {}, {"id": ""}, {"id": "--reveal"}, "vone"])
def test_item_without_usable_vault_fails_closed(op_backend, vault):
    backend, items, calls = op_backend
    broken = dict(_item("aaa", "vone", "Example"))
    broken["vault"] = vault
    items.write_text(json.dumps([broken]), encoding="utf-8")
    with pytest.raises(RuntimeError, match="no resolvable vault"):
        backend.resolve_password("op:aaa")
    assert _gets(calls) == []


def test_browser_fill_resolves_scoped_password_on_bound_origin_only(op_backend):
    """End to end through browser_vault_fill: the secret reaches only the fill script, the read is
    vault-scoped, and a wrong origin is refused before any item read."""
    backend, _, calls = op_backend
    from tools.browser_vault_tool import browser_vault_fill

    controls = json.dumps([{"tag": "input", "type": "password", "name": "password", "id": "pw",
                            "autocomplete": "current-password", "visible": True}])
    with patch("agent.vault_backends.base.enabled_backends", return_value=[backend]), \
         patch("agent.vault_backends.enabled_backends", return_value=[backend]), \
         patch("tools.browser_vault_tool._eval_js", return_value={"success": True, "result": controls}), \
         patch("tools.browser_vault_tool._eval_js_secret",
               return_value={"success": True, "result": json.dumps({"filled": 1})}) as secret_eval:
        with patch("tools.browser_vault_tool._current_page_origin", return_value="https://evil.example"):
            refused = json.loads(browser_vault_fill("op:bbb", task_id="t"))
        assert refused["success"] is False and refused["error_type"] == "origin_mismatch"
        assert _gets(calls) == []
        with patch("tools.browser_vault_tool._current_page_origin", return_value="https://example.com"):
            out = json.loads(browser_vault_fill("op:bbb", task_id="t"))
    assert out["success"] is True and out["backend"] == "onepassword"
    assert "pw in vault two" not in json.dumps(out)
    assert "pw in vault two" in secret_eval.call_args.args[1]
    assert _gets(calls) == [["item", "get", "bbb", "--vault", "vtwo", "--fields", "label=password", "--reveal"]]
