from __future__ import annotations

import base64

import pytest

import plugins.native_content_slimmer as native_content_slimmer
from plugins.native_content_slimmer.classifier import (
    DEFAULT_ALLOW_TOOLS,
    UNKNOWN_SECRET_FIXTURES_HARD_GATE,
    Classification,
    classify_tool_result,
    contains_secret,
    deterministic_preview,
)


def _aws_key() -> str:
    return "AKIA" + ("A" * 16)


def _jwt() -> str:
    return ".".join([
        "eyJ" + "hbGciOiJIUzI1NiJ9",
        "eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0",
        "sig" + "A" * 12,
    ])


def _pem_private_key() -> str:
    fence = "-" * 5
    label = "PRIVATE" + " " + "KEY"
    return "\n".join([
        fence + "BEGIN " + label + fence,
        "not-a-real-key",
        fence + "END " + label + fence,
    ])


def _dsn_with_password() -> str:
    return "postgresql" + "://" + "user" + ":" + "pw" + "@" + "db.example/app"


def _high_entropy_base64_blob() -> str:
    raw = bytes(((idx * 37 + 11) % 256 for idx in range(96)))
    return base64.b64encode(raw).decode("ascii")


def _pem_private_key_crlf() -> str:
    fence = "-" * 5
    label = "PRIVATE" + " " + "KEY"
    return "\r\n".join([
        fence + "BEGIN " + label + fence,
        "not-a-real-key",
        fence + "END " + label + fence,
    ])


@pytest.mark.parametrize(
    ("label", "text"),
    [
        ("onepassword", "use op://Engineering/app/password"),
        ("bearer", "Authorization: " + "Bearer " + "token-value-123456"),
        ("aws", "AWS_ACCESS_KEY_ID=" + _aws_key()),
        ("pem", _pem_private_key()),
        ("jwt", "token=" + _jwt()),
        ("cookie", "Cookie: sessionid=abc123; csrftoken=def456"),
        ("dsn", _dsn_with_password()),
    ],
)
def test_secret_shapes_are_no_store(label: str, text: str) -> None:
    classified = classify_tool_result(
        tool_name="terminal",
        result=("safe prefix\n" + text + "\n") * 600,
        status="success",
        min_bytes=100,
        preview_bytes=80,
    )

    assert classified == Classification(
        eligible=False,
        reason="secret_classified_no_store",
        raw_bytes=len((("safe prefix\n" + text + "\n") * 600).encode("utf-8")),
        content_class="secret",
        preview=None,
        secret_match=label,
    )


def test_plugin_docstring_documents_secret_detection_is_heuristic() -> None:
    doc = native_content_slimmer.__doc__ or ""

    assert "heuristic" in doc
    assert "non-matching secret" in doc
    assert "active_lossless" in doc


def test_documented_secret_patterns_are_detected() -> None:
    documented_examples = {
        "onepassword": "op://Engineering/app/password",
        "bearer": "Authorization: Bearer token-value-123456",
        "aws": "AWS_ACCESS_KEY_ID=" + _aws_key(),
        "pem": _pem_private_key(),
        "jwt": "token=" + _jwt(),
        "cookie": "Set-Cookie: sessionid=abc123",
        "dsn": _dsn_with_password(),
    }

    assert {label: contains_secret(text) for label, text in documented_examples.items()} == {
        label: label for label in documented_examples
    }


@pytest.mark.parametrize(
    ("label", "text"),
    [
        ("pem", _pem_private_key_crlf()),
        ("high_entropy", "blob=" + _high_entropy_base64_blob()),
        ("authorization", "authorization=" + "tokenvalue123456789"),
    ],
)
def test_unknown_ish_secret_fixtures_are_hard_no_store_gate(label: str, text: str) -> None:
    classified = classify_tool_result(
        tool_name="terminal",
        result=("safe prefix\n" + text + "\n") * 50,
        status="success",
        min_bytes=100,
        preview_bytes=80,
    )

    assert UNKNOWN_SECRET_FIXTURES_HARD_GATE is True
    assert classified.eligible is False
    assert classified.reason == "secret_classified_no_store"
    assert classified.content_class == "secret"
    assert classified.secret_match == label


def test_contains_secret_returns_first_matching_label() -> None:
    label = contains_secret("prefix op://Vault/item and AWS_ACCESS_KEY_ID=" + _aws_key())

    assert label == "onepassword"


def test_large_allowed_success_is_eligible_with_deterministic_preview() -> None:
    body = "HEAD\n" + ("middle line\n" * 2000) + "TAIL\n"

    first = classify_tool_result(
        tool_name="terminal",
        result=body,
        status="success",
        min_bytes=100,
        preview_bytes=120,
    )
    second = classify_tool_result(
        tool_name="terminal",
        result=body,
        status="success",
        min_bytes=100,
        preview_bytes=120,
    )

    assert first.eligible is True
    assert first.reason == "eligible_lossless_offload"
    assert first.content_class == "text"
    assert first.preview == second.preview
    assert first.preview is not None
    assert first.preview.startswith("HEAD\n")
    assert first.preview.rstrip().endswith("TAIL")
    assert "omitted" in first.preview


def test_small_or_disallowed_results_are_not_eligible() -> None:
    assert classify_tool_result(
        tool_name="terminal",
        result="short",
        status="success",
        min_bytes=100,
    ).reason == "below_min_bytes"

    assert classify_tool_result(
        tool_name="ha_call_service",
        result="x" * 1000,
        status="success",
        min_bytes=100,
    ).reason == "tool_denied"

    assert classify_tool_result(
        tool_name="terminal",
        result="x" * 1000,
        status="error",
        min_bytes=100,
    ).reason == "status_denied"


def test_default_allow_tools_match_prd_cut() -> None:
    assert DEFAULT_ALLOW_TOOLS == frozenset({"terminal", "web_extract", "browser_console"})


def test_deterministic_preview_preserves_head_tail_and_is_unicode_safe() -> None:
    text = "alpha😺\n" + ("middle😺\n" * 80) + "omega😺\n"

    first = deterministic_preview(text, preview_bytes=90)
    second = deterministic_preview(text, preview_bytes=90)

    assert first == second
    assert first.startswith("alpha😺")
    assert first.rstrip().endswith("omega😺")
    assert "omitted" in first
    first.encode("utf-8")
