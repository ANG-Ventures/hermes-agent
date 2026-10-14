"""1Password backend: every category discoverable, Credit Card items fillable, nothing else.

All values are synthetic. The fake ``op`` below speaks the real CLI's shapes: ``item list --format json``
rows carry ``category`` + ``vault`` + ``additional_information``; a Credit Card ``item get --format json``
carries the built-in fields from ``op item template get "Credit Card"`` (ids ``cardholder``, ``type``,
``ccnum``, ``cvv``, ``expiry`` MONTH_YEAR, ``validFrom``, ``pin``...). It refuses an unscoped or
wrong-vault read the way a service account does, and logs every argv so tests can assert what was run.
"""

from __future__ import annotations

import json
import logging
import stat
from unittest.mock import patch

import pytest

from agent.vault_backends import onepassword as op_mod
from agent.vault_backends.base import VaultAccessDenied
from agent.vault_backends.onepassword import CardDataError, OnePasswordLoginBackend, parse_card_fields

pytestmark = pytest.mark.platforms("posix")  # fake op is a shebang script

PAN = "4111111111111111"          # Luhn-valid test number
CVV = "8642"
SENTINEL_NOTE = "NOTE-SENTINEL-9f3"
SENTINEL_ACCT = "ACCT-SENTINEL-55501234"
SENTINEL_KEY = "SHA256:KEYFP-SENTINEL"

_FAKE_OP = r'''#!/usr/bin/env python3
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
argv = sys.argv[1:]
with open(os.path.join(here, "op.log"), "a") as log:
    log.write(json.dumps({"argv": argv}) + "\n")
items = json.load(open(os.path.join(here, "items.json")))
cards = json.load(open(os.path.join(here, "cards.json")))
if argv[:2] == ["item", "list"]:
    print(json.dumps(items)); sys.exit(0)
if argv[:2] == ["item", "get"]:
    if "--vault" not in argv:
        sys.stderr.write("[ERROR] a vault query must be provided when using service accounts\n"); sys.exit(1)
    item_id, vault = argv[2], argv[argv.index("--vault") + 1]
    if os.path.exists(os.path.join(here, "deny")):
        sys.stderr.write(f'[ERROR] "{item_id}" isn\'t an item in the "{vault}" vault\n'); sys.exit(1)
    if "--format" in argv and item_id in cards:
        print(json.dumps(cards[item_id])); sys.exit(0)
sys.exit(2)
'''


def _row(item_id, category, title, vault="vfin", info="", urls=()):
    row = {"id": item_id, "title": title, "category": category, "created_at": "2026-01-01T00:00:00Z",
           "updated_at": "2026-02-01T00:00:00Z", "vault": {"id": vault, "name": vault.upper()},
           "tags": ["t1"], "additional_information": info}
    if urls:
        row["urls"] = [{"href": u} for u in urls]
    return row


def _card(item_id, *, ccnum=PAN, cvv=CVV, expiry="202907", cardholder="Test Holder", extra=()):
    fields = [
        {"id": "notesPlain", "type": "STRING", "purpose": "NOTES", "label": "notesPlain", "value": SENTINEL_NOTE},
        {"id": "cardholder", "type": "STRING", "label": "Karteninhaber", "value": cardholder},  # localised label
        {"id": "type", "type": "CREDIT_CARD_TYPE", "label": "type", "value": "visa"},
        {"id": "ccnum", "type": "CREDIT_CARD_NUMBER", "label": "number", "value": ccnum},
        {"id": "cvv", "type": "CONCEALED", "label": "verification number", "value": cvv},
        {"id": "expiry", "type": "MONTH_YEAR", "label": "expiry date", "value": expiry},
        {"id": "validFrom", "type": "MONTH_YEAR", "label": "valid from", "value": "202101"},
        {"id": "pin", "type": "CONCEALED", "section": {"id": "details"}, "label": "PIN", "value": "0000-PIN-SENTINEL"},
        {"id": "junk", "type": "STRING", "label": "number", "value": "9999-JUNK-SENTINEL"},  # label collides, id doesn't
        *extra,
    ]
    return {"id": item_id, "title": "Card", "category": "CREDIT_CARD", "vault": {"id": "vfin"}, "fields": fields}


_ITEMS = [
    _row("login1", "LOGIN", "Shop login", vault="veng", info="jane@example.com", urls=["https://shop.example.com/login"]),
    _row("card1", "CREDIT_CARD", "Corp Visa", info="•••• 1111"),
    _row("card2", "CREDIT_CARD", "Leaky card", info=PAN),                        # a full PAN in the summary
    _row("card3", "CREDIT_CARD", "Bound card", info="*1111", urls=["https://pay.example.com/checkout"]),
    _row("pw1", "PASSWORD", "Router admin", info=SENTINEL_NOTE),
    _row("doc1", "DOCUMENT", "Tax return", info="tax-SENTINEL.pdf"),
    _row("ssh1", "SSH_KEY", "Deploy key", info=SENTINEL_KEY),
    _row("bank1", "BANK_ACCOUNT", "Checking", info=SENTINEL_ACCT),
    _row("note1", "SECURE_NOTE", "Notes", info=SENTINEL_NOTE),
    _row("id1", "IDENTITY", "Me", info="Jane SENTINEL-NAME"),
]
_CARDS = {"card1": _card("card1"), "card2": _card("card2"), "card3": _card("card3")}


@pytest.fixture
def op_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    op_mod._LIST_CACHE.clear()
    exe = tmp_path / "op"
    exe.write_text(_FAKE_OP, encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    (tmp_path / "items.json").write_text(json.dumps(_ITEMS), encoding="utf-8")
    (tmp_path / "cards.json").write_text(json.dumps(_CARDS), encoding="utf-8")
    backend = OnePasswordLoginBackend({"enabled": True, "binary_path": str(exe)})
    backend._service_token = "sa-token-for-test"
    log = tmp_path / "op.log"

    def calls():
        return [json.loads(line)["argv"] for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []

    yield backend, tmp_path, calls
    op_mod._LIST_CACHE.clear()


# ── mapping ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("expiry,month,year", [("202907", "07", "2029"), ("2029/07", "07", "2029"),
                                               ("2029/7", "07", "2029"), ("07/2029", "07", "2029")])
def test_card_fields_map_by_id_and_every_other_field_is_dropped(expiry, month, year):
    out = parse_card_fields(_card("x", expiry=expiry)["fields"])
    assert out == {"card_number": PAN, "cvc": CVV, "exp_month": month, "exp_year": year,
                   "cardholder_name": "Test Holder"}
    assert not any("SENTINEL" in v for v in out.values())


def test_card_number_spacing_is_normalised_and_cardholder_optional():
    out = parse_card_fields(_card("x", ccnum="4111 1111-1111 1111", cardholder="")["fields"])
    assert out["card_number"] == PAN and "cardholder_name" not in out


@pytest.mark.parametrize("kw,match", [
    ({"ccnum": "4111111111111112"}, "card number"),       # Luhn fails
    ({"ccnum": "41111111abcd1111"}, "card number"),
    ({"ccnum": ""}, "card number"),
    ({"ccnum": "4" * 25}, "card number"),
    ({"ccnum": "４１１１１１１１１１１１１１１１"}, "card number"),  # fullwidth digits
    ({"cvv": "123456"}, "verification"),
    ({"cvv": "12"}, "verification"),
    ({"cvv": "12a"}, "verification"),
    ({"expiry": "202913"}, "expiry"),                  # month 13
    ({"expiry": "202900"}, "expiry"),
    ({"expiry": "29/07"}, "expiry"),                   # two-digit year is not a 1Password wire format
    ({"expiry": ""}, "expiry"),
    ({"cardholder": "A\x07B"}, "cardholder"),  # a newline is editor residue and is normalised (see below); a BEL is not
])
def test_malformed_card_values_are_refused_without_echoing_them(kw, match):
    with pytest.raises(CardDataError, match=match) as exc:
        parse_card_fields(_card("x", **kw)["fields"])
    for value in kw.values():
        if value:
            assert value not in str(exc.value)


def test_duplicate_builtin_field_is_refused():
    dup = {"id": "ccnum", "type": "CREDIT_CARD_NUMBER", "label": "number", "value": "5555555555554444"}
    with pytest.raises(CardDataError, match="more than once"):
        parse_card_fields(_card("x", extra=(dup,))["fields"])


# ── all-category discovery ─────────────────────────────────────────────────

def test_every_category_is_listed_with_two_tiers_and_no_summary_leaks(op_env):
    backend, _, calls = op_env
    metas = {m.id: m for m in backend.list_items()}
    assert set(metas) == {f"op:{i['id']}" for i in _ITEMS}
    login = metas["op:login1"]
    assert (login.kind, login.origin, login.identifier, login.fill_supported) == (
        "login", "https://shop.example.com", "jane@example.com", True)
    card = metas["op:card1"]
    assert (card.kind, card.origin, card.allowed_origins, card.last4, card.vault, card.category) == (
        "payment", None, (), "1111", "VFIN", "CREDIT_CARD")
    assert metas["op:card2"].last4 is None                      # full PAN summary: no last4 at all
    assert metas["op:card3"].origin == "https://pay.example.com"
    for ref in ("pw1", "doc1", "ssh1", "bank1", "note1", "id1"):
        m = metas[f"op:{ref}"]
        assert m.kind == "reference" and m.fill_supported is False and m.origin is None and m.identifier is None
    assert metas["op:id1"].category == "IDENTITY"              # identity: metadata only, not an address fill
    dumped = json.dumps([m.to_dict() for m in metas.values()])
    for sentinel in (SENTINEL_NOTE, SENTINEL_ACCT, SENTINEL_KEY, "tax-SENTINEL", "SENTINEL-NAME", PAN):
        assert sentinel not in dumped
    # listing never reads an item and never passes a category filter
    assert all(argv[:2] == ["item", "list"] and "--categories" not in argv for argv in calls())


def test_listing_is_cached_for_discovery_only_and_fill_metadata_is_fresh(op_env):
    backend, tmp, calls = op_env
    lists = lambda: sum(argv[:2] == ["item", "list"] for argv in calls())  # noqa: E731
    backend.list_items(); backend.list_items()
    assert lists() == 1                                          # discovery shares one listing within the TTL
    backend._service_token = "a-different-token"                # another credential never sees the cached list
    backend.list_items()
    assert lists() == 2
    # Prism 855923d1a39c: the login's website moves from shop.example.com to new.example.com while the
    # discovery listing is cached. Fill-time metadata must authorize the NEW site only.
    moved = [dict(i, urls=[{"href": "https://new.example.com"}]) if i["id"] == "login1" else i for i in _ITEMS]
    (tmp / "items.json").write_text(json.dumps(moved), encoding="utf-8")
    assert {m.id: m for m in backend.list_items()}["op:login1"].origin == "https://shop.example.com"  # cached view
    assert backend.get_meta("op:login1").allowed_origins == ("https://new.example.com",)
    # an item removed from 1Password is gone for the fill path at once, and its secret read refuses
    (tmp / "items.json").write_text(json.dumps([i for i in _ITEMS if i["id"] != "card1"]), encoding="utf-8")
    assert backend.get_meta("op:card1") is None
    with pytest.raises(RuntimeError, match="not listed"):
        backend.resolve_secret("op:card1")
    assert not [a for a in calls() if a[:2] == ["item", "get"]]


def test_card_bound_only_to_http_keeps_its_binding(op_env):
    """Prism 0fe3929b519b: a card whose only saved website is http:// is bound to it, never unbound."""
    backend, tmp, _ = op_env
    rows = _ITEMS + [_row("card4", "CREDIT_CARD", "Http card", info="*1111", urls=["http://merchant.example/pay"])]
    (tmp / "items.json").write_text(json.dumps(rows), encoding="utf-8")
    meta = backend.get_meta("op:card4")
    assert meta.origin == "http://merchant.example" and meta.allowed_origins == ("http://merchant.example",)


# ── secret reads ───────────────────────────────────────────────────────────

def test_card_resolve_is_vault_scoped_and_argv_holds_only_ids(op_env, caplog):
    backend, _, calls = op_env
    with caplog.at_level(logging.DEBUG):
        secret = backend.resolve_secret("op:card1")
    assert secret["card_number"] == PAN and secret["cvc"] == CVV
    gets = [a for a in calls() if a[:2] == ["item", "get"]]
    assert gets == [["item", "get", "card1", "--vault", "vfin", "--format", "json", "--reveal"]]
    assert PAN not in caplog.text and CVV not in caplog.text


@pytest.mark.parametrize("handle", ["op:pw1", "op:ssh1", "op:login1", "op:id1", "op:missing", "op:../card1",
                                    "op:vfin/card1", "op:card1/x", "op:-card1"])
def test_resolve_secret_refuses_everything_but_a_listed_card(op_env, handle):
    backend, _, calls = op_env
    with pytest.raises(RuntimeError):
        backend.resolve_secret(handle)
    assert not [a for a in calls() if a[:2] == ["item", "get"]]


def test_password_read_refuses_a_card_handle(op_env):
    backend, _, calls = op_env
    with pytest.raises(RuntimeError, match="not fillable"):
        backend.resolve_password("op:card1")
    assert backend.resolve_otp("op:card1") is None
    assert not [a for a in calls() if a[:2] == ["item", "get"]]


def test_vault_permission_gap_is_a_typed_scrubbed_error(op_env):
    backend, tmp, _ = op_env
    (tmp / "deny").write_text("1")
    with pytest.raises(VaultAccessDenied) as exc:
        backend.resolve_secret("op:card1")
    assert PAN not in str(exc.value)


def test_list_failure_on_one_backend_is_reported_and_others_still_list(op_env):
    from tools.browser_vault_tool import browser_vault_list

    backend, tmp, _ = op_env
    (tmp / "items.json").write_text("not json", encoding="utf-8")

    class _Other:
        name, display_name, needs_unlock = "local", "Hermes vault", False

        def list_items(self):
            from agent.vault_store import VaultItemMeta
            return [VaultItemMeta(id="vault_a", kind="login", label="A", origin="https://a.test", created_at="")]

    with patch("agent.vault_backends.enabled_backends", return_value=[_Other(), backend]):
        out = json.loads(browser_vault_list())
    assert [i["handle"] for i in out["items"]] == ["vault_a"]
    assert out["errors"][0]["backend"] == "onepassword"


def test_browser_list_marks_reference_items_unavailable(op_env):
    from tools.browser_vault_tool import browser_vault_list

    backend, _, _ = op_env
    with patch("agent.vault_backends.enabled_backends", return_value=[backend]):
        raw = browser_vault_list()
    items = {i["handle"]: i for i in json.loads(raw)["items"]}
    assert items["op:card1"]["available"] is True and items["op:card1"]["last4"] == "1111"
    assert "origin_binding" in items["op:card1"] and "origin_binding" not in items["op:card3"]
    assert items["op:ssh1"]["available"] is False and items["op:ssh1"]["fill_supported"] is False
    assert items["op:login1"]["available"] is True
    for sentinel in (SENTINEL_NOTE, SENTINEL_ACCT, SENTINEL_KEY, PAN):
        assert sentinel not in raw


@pytest.mark.parametrize("raw, expected", [
    ("Alex Example\n", "Alex Example"),            # trailing newline (seen on a real item, 2026-10-14)
    ("Alex\tExample", "Alex Example"),              # tab between names
    ("  Alex   Example  ", "Alex Example"),         # runs of spaces
    ("Alex\r\nExample", "Alex Example"),            # soft wrap
])
def test_cardholder_whitespace_residue_is_normalised_not_refused(raw, expected):
    out = op_mod.parse_card_fields(_card("x", cardholder=raw)["fields"])
    assert out["cardholder_name"] == expected


def test_cardholder_still_refused_when_nonprintable_after_normalising():
    with pytest.raises(op_mod.CardDataError, match="cardholder"):
        op_mod.parse_card_fields(_card("x", cardholder="Alex\x07Example")["fields"])
    with pytest.raises(op_mod.CardDataError, match="cardholder"):
        op_mod.parse_card_fields(_card("x", cardholder="A" * 101)["fields"])
