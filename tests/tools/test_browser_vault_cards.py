"""browser_vault_fill for 1Password items: kind gate, per-fill approval bound to the exact page, redaction.

Synthetic values only. ``_eval_js`` / ``_eval_js_secret`` / ``_current_page_origin`` are the page seams;
the backend is a stub whose ``resolve_secret`` records whether it was ever reached.
"""

from __future__ import annotations

import json
import logging
from unittest.mock import patch

import pytest

from agent.vault_store import VaultItemMeta

PAN, CVV = "4111111111111111", "8642"
CARD = {"card_number": PAN, "cvc": CVV, "exp_month": "07", "exp_year": "2029", "cardholder_name": "Test Holder"}
# The Link Technologies checkout shape (top-level inputs, no autocomplete attributes).
LINKTECH_CONTROLS = [
    {"index": 0, "type": "text", "name": "CardholderName CardholderName", "label": ""},
    {"index": 1, "type": "text", "name": "CardNumber CardNumber", "label": ""},
    {"index": 2, "type": "select", "name": "ExpireMonth ExpireMonth", "label": ""},
    {"index": 3, "type": "select", "name": "ExpireYear ExpireYear", "label": ""},
    {"index": 4, "type": "text", "name": "CardCode CardCode", "label": ""},
    {"index": 5, "type": "email", "name": "Email", "label": "Email"},
]


def _meta(kind="payment", origin=None, allowed=(), fill_supported=True, last4: "str | None" = "1111",
          category: "str | None" = "CREDIT_CARD"):
    return VaultItemMeta(id="op:card1", kind=kind, label="Corp Visa", origin=origin, created_at="",
                         allowed_origins=allowed, category=category, last4=last4, fill_supported=fill_supported)


class _Backend:
    name, display_name, needs_unlock = "onepassword", "1Password", False

    def __init__(self, meta, secret=None, error=None):
        self.meta, self.secret, self.error, self.resolved = meta, secret or dict(CARD), error, 0

    def is_unlocked(self):
        return True

    def get_meta(self, handle):
        return self.meta if handle == self.meta.id else None

    def resolve_password(self, handle):
        self.resolved += 1
        return "pw"

    def resolve_secret(self, handle):
        self.resolved += 1
        if self.error:
            raise self.error
        return dict(self.secret)


def _run(backend, *, origins, consent="accept", controls=LINKTECH_CONTROLS, fill_result=None):
    """Drive one fill. ``origins`` is the sequence _current_page_origin returns (pre-check, post-approval)."""
    from tools import browser_vault_tool as t

    seen = {"secret_exprs": [], "prompts": [], "registered": [], "order": []}
    origin_iter = iter(origins)

    def consent_fn(message, description, **kw):
        seen["prompts"].append((message, description, kw.get("surface")))
        seen["order"].append("consent")
        return consent

    def eval_secret(task_id, expr):
        seen["order"].append("eval_secret")
        seen["secret_exprs"].append(expr)
        return {"success": True, "result": json.dumps(fill_result or {"filled": 5})}

    def register(value):
        seen["order"].append("register")
        seen["registered"].append(value)

    with patch("agent.vault_backends.backend_for_handle", return_value=backend), \
         patch.object(t, "_focus_bound_origin", lambda *a, **k: None), \
         patch.object(t, "_current_page_origin", side_effect=lambda tid: next(origin_iter)), \
         patch.object(t, "_eval_js", return_value={"success": True, "result": json.dumps(controls)}), \
         patch.object(t, "_eval_js_secret", side_effect=eval_secret), \
         patch("agent.redact.register_vault_redaction_value", side_effect=register), \
         patch("tools.approval_prompt.request_elicitation_consent", side_effect=consent_fn):
        raw = t.browser_vault_fill("op:card1", task_id="t")
    return json.loads(raw), raw, seen


@pytest.mark.parametrize("meta", [
    _meta(kind="reference", fill_supported=False, category="SSH_KEY"),
    _meta(kind="reference", fill_supported=False, origin="https://shop.test", category="PASSWORD"),
    _meta(kind="document", origin="https://shop.test"),          # unknown kind injected with an origin
    _meta(kind="ssh_key"),
    _meta(kind="payment", fill_supported=False),                 # a card marked metadata-only stays unfillable
])
def test_non_fillable_kinds_are_refused_before_any_secret_read_or_prompt(meta):
    backend = _Backend(meta)
    out, raw, seen = _run(backend, origins=["https://shop.test"] * 3)
    assert out["error_type"] == "not_fillable"
    assert backend.resolved == 0 and seen["prompts"] == [] and seen["secret_exprs"] == []


@pytest.mark.parametrize("kind", ["address", "payment"])
def test_local_item_without_origin_keeps_its_no_origin_refusal(kind):
    """Only a password-manager card gets the approved-page path; a local card or address saved
    without an origin stays unfillable, as before."""
    backend = _Backend(_meta(kind=kind, last4=None, category=None))
    out, _, seen = _run(backend, origins=["https://shop.test"] * 3)
    assert out["error_type"] == "no_origin" and backend.resolved == 0 and seen["prompts"] == []


@pytest.mark.parametrize("consent", ["decline", "cancel"])
def test_denied_or_unanswered_consent_never_resolves_the_card(consent):
    backend = _Backend(_meta())
    out, raw, seen = _run(backend, origins=["https://checkout.example"] * 3, consent=consent)
    assert out["error_type"] == "payment_declined"
    assert backend.resolved == 0 and seen["secret_exprs"] == [] and PAN not in raw


def _raise(*a, **k):
    raise RuntimeError("surface died")


@pytest.mark.parametrize("prompt", [lambda *a, **k: "deny", lambda *a, **k: "timeout", _raise])
def test_real_consent_router_deny_timeout_or_exception_never_resolves(monkeypatch, prompt):
    """The real consent router (CLI path): deny, no answer and a crashing prompt all fail closed."""
    from tools import approval_prompt
    from tools import browser_vault_tool as t

    backend = _Backend(_meta())
    monkeypatch.setattr(approval_prompt, "prompt_dangerous_approval", prompt)
    with patch("agent.vault_backends.backend_for_handle", return_value=backend), \
         patch.object(t, "_focus_bound_origin", lambda *a, **k: None), \
         patch.object(t, "_current_page_origin", return_value="https://checkout.example"), \
         patch.object(t, "_eval_js_secret") as secret_eval:
        out = json.loads(t.browser_vault_fill("op:card1", task_id="t"))
    assert out["error_type"] == "payment_declined" and backend.resolved == 0 and not secret_eval.called


def test_urlless_card_prompt_names_exact_page_and_last4_then_fills_link_technologies_fields():
    backend = _Backend(_meta())
    out, raw, seen = _run(backend, origins=["https://www.linktechnologies.example"] * 2)
    assert out["success"] is True and out["origin"] == "https://www.linktechnologies.example"
    assert out["fields"] == ["cc-csc", "cc-exp-month", "cc-exp-year", "cc-name", "cc-number"]
    [(message, description, surface)] = seen["prompts"]
    assert message == "Fill payment card 'Corp Visa ending 1111' on https://www.linktechnologies.example"
    assert "https://www.linktechnologies.example" in description and surface == "vault-payment"
    expr = seen["secret_exprs"][0]
    assert '"https://www.linktechnologies.example"' in expr and "window.location.origin" in expr
    assert '"index": 5' not in expr                                   # email is never a card target
    assert PAN not in raw and CVV not in raw
    # every card value registered for egress redaction before the page write; resolve after consent
    assert set(seen["registered"]) >= set(CARD.values())
    assert seen["order"].index("consent") < seen["order"].index("register") < seen["order"].index("eval_secret")


def test_second_fill_asks_again():
    backend = _Backend(_meta())
    _run(backend, origins=["https://a.example"] * 2)
    _, _, seen = _run(backend, origins=["https://a.example"] * 2)
    assert len(seen["prompts"]) == 1 and backend.resolved == 2


def test_http_page_is_refused_for_an_urlless_card():
    backend = _Backend(_meta())
    out, _, seen = _run(backend, origins=["http://checkout.example"] * 3)
    assert out["error_type"] == "origin_mismatch" and seen["prompts"] == [] and backend.resolved == 0


def test_navigation_while_approval_is_open_writes_nothing():
    backend = _Backend(_meta())
    out, raw, seen = _run(backend, origins=["https://checkout.example", "https://evil.example"])
    assert out["error_type"] == "origin_changed" and backend.resolved == 0 and seen["secret_exprs"] == []


def test_navigation_after_approval_is_caught_by_the_fill_script():
    backend = _Backend(_meta())
    out, raw, seen = _run(backend, origins=["https://checkout.example"] * 2,
                          fill_result={"refused": "origin_changed", "found": "https://evil.example"})
    assert out["error_type"] == "origin_changed" and PAN not in raw


def test_card_with_saved_website_stays_exact_match_and_still_asks():
    backend = _Backend(_meta(origin="https://pay.example", allowed=("https://pay.example",)))
    wrong, _, seen = _run(backend, origins=["https://other.example"] * 3)
    assert wrong["error_type"] == "origin_mismatch" and seen["prompts"] == [] and backend.resolved == 0
    ok, _, seen = _run(backend, origins=["https://pay.example"] * 2)
    assert ok["success"] is True and len(seen["prompts"]) == 1


def test_vault_access_denied_and_malformed_card_fill_nothing(caplog):
    from agent.vault_backends.base import VaultAccessDenied
    from agent.vault_backends.onepassword import CardDataError

    out, raw, seen = _run(_Backend(_meta(), error=VaultAccessDenied("1Password refused access: forbidden")),
                          origins=["https://c.example"] * 2)
    assert out["error_type"] == "vault_access_denied" and seen["secret_exprs"] == []
    with caplog.at_level(logging.DEBUG):
        out, raw, seen = _run(_Backend(_meta(), error=CardDataError("card number is missing or not a valid card number")),
                              origins=["https://c.example"] * 2)
    assert out["error_type"] == "resolve_failed" and seen["secret_exprs"] == [] and seen["registered"] == []
    assert PAN not in caplog.text


def test_combined_expiry_field_gets_mm_yy():
    controls = [{"index": 0, "type": "text", "autocomplete": "cc-number"},
                {"index": 1, "type": "text", "autocomplete": "cc-exp"},
                {"index": 2, "type": "text", "autocomplete": "cc-csc"}]
    out, _, seen = _run(_Backend(_meta()), origins=["https://c.example"] * 2, controls=controls)
    assert out["fields"] == ["cc-csc", "cc-exp", "cc-number"] and '"07/29"' in seen["secret_exprs"][0]


def test_multi_site_card_approval_narrows_the_fill_to_the_approved_page():
    """Prism 531673770839: the fill script is pinned to the approved origin, not to every saved site."""
    backend = _Backend(_meta(origin="https://a.example", allowed=("https://a.example", "https://b.example")))
    out, _, seen = _run(backend, origins=["https://b.example"] * 2)
    assert out["success"] is True and out["origin"] == "https://b.example"
    expr = seen["secret_exprs"][0]
    assert 'const expectedOrigin = "https://b.example"' in expr and "https://a.example" not in expr


def test_http_bound_card_fills_only_on_its_saved_page():
    backend = _Backend(_meta(origin="http://merchant.example", allowed=("http://merchant.example",)))
    other, _, seen = _run(backend, origins=["https://unrelated.example"] * 3)
    assert other["error_type"] == "origin_mismatch" and seen["prompts"] == [] and backend.resolved == 0


@pytest.mark.parametrize("number,shown", [("4111111111111111", "4111 1111 1111 1111"),
                                          ("4111111111111111", "4111-1111-1111-1111"),
                                          ("378282246310005", "3782 822463 10005")])
def test_formatted_card_number_readback_is_redacted(number, shown):
    """Prism dc738de7e461: a checkout that re-formats the number must not leak it through a later read."""
    from agent import redact
    from tools.browser_cdp_tool import _redact_cdp_output

    redact.clear_vault_redaction_values()
    backend = _Backend(_meta(), secret=dict(CARD, card_number=number))
    try:
        from tools import browser_vault_tool as t
        controls = LINKTECH_CONTROLS
        with patch("agent.vault_backends.backend_for_handle", return_value=backend), \
             patch.object(t, "_focus_bound_origin", lambda *a, **k: None), \
             patch.object(t, "_current_page_origin", return_value="https://c.example"), \
             patch.object(t, "_eval_js", return_value={"success": True, "result": json.dumps(controls)}), \
             patch.object(t, "_eval_js_secret", return_value={"success": True, "result": json.dumps({"filled": 5})}), \
             patch("tools.approval_prompt.request_elicitation_consent", return_value="accept"):
            assert json.loads(t.browser_vault_fill("op:card1", task_id="t"))["success"] is True
        echoed = _redact_cdp_output({"result": {"type": "string", "value": shown}})
        assert shown not in json.dumps(echoed)
        assert shown not in redact.redact_sensitive_text(f"field value: {shown}")
    finally:
        redact.clear_vault_redaction_values()
