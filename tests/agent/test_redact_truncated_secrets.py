"""Truncated private-key blocks and elided tokens are redacted whole (t_7c575aa0).

A tool output cut before a PEM block's END line carried a private key past the
redactor, and tokens whose middle the tool elided (``ghs_..._eyJhbG...aDOQ``)
kept their visible head and tail. All material below is invented.
"""

from agent.redact import redact_sensitive_text

_BODY = "\n".join(
    ["MIIEowIBAAKCAQEAinventedKEYmaterialNOTreal0123456789abcdefghij"]
    + ["Zm9vYmFyYmF6cXV4aW52ZW50ZWRrZXltYXRlcmlhbG5vdHJlYWwwMTIzNDU2"] * 6
)
_FULL = f"-----BEGIN RSA PRIVATE KEY-----\n{_BODY}\n-----END RSA PRIVATE KEY-----"
_CUT = f"-----BEGIN RSA PRIVATE KEY-----\n{_BODY}\n... [output truncated]"


def _r(text: str) -> str:
    return redact_sensitive_text(text, force=True)


def test_full_block_redacted():
    out = _r(f"before\n{_FULL}\nafter")
    assert out == "before\n[REDACTED PRIVATE KEY]\nafter"


def test_block_cut_before_end_redacted_to_end_of_output():
    out = _r(f"==> /var/lib/x/state.opkf-1 <==\n{_CUT}")
    assert out == "==> /var/lib/x/state.opkf-1 <==\n[REDACTED PRIVATE KEY]"
    assert "Zm9vYmFy" not in out and "MIIE" not in out


def test_two_blocks_second_cut():
    out = _r(f"{_FULL}\n==> next <==\n{_CUT}")
    assert out == "[REDACTED PRIVATE KEY]\n==> next <==\n[REDACTED PRIVATE KEY]"
    assert "Zm9vYmFy" not in out and "MIIE" not in out


def test_openssh_block_cut_mid_line():
    out = _r("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9u")
    assert out == "[REDACTED PRIVATE KEY]"


def test_token_with_ascii_elision_marker_redacted_whole():
    out = _r("token ghs_5078517_eyJhbGciOiJSUzI1NiIs...aDOQ end")
    assert "aDOQ" not in out and "5078517" not in out and "eyJhbG" not in out
    assert out.startswith("token ") and out.endswith(" end")


def test_token_with_unicode_elision_marker_redacted_whole():
    for tok in ("ghp_AbCdEfGh…WxYz", "github_pat_11ABCDEFG0_xyz…Q9rS", "sk-ant-api03-AbCd…WxYz"):
        out = _r(f"x {tok} y")
        assert out.startswith("x ") and out.endswith(" y"), out
        assert tok.split("…")[1] not in out, out
        assert tok.split("…")[0][-4:] not in out, out


def test_ordinary_text_untouched():
    for text in (
        "Rotate the private key and paste the PRIVATE KEY header later.",
        "The BEGIN marker ----- is documented; no key here.",
        "GitHub tokens start with ghp_... or ghs_... and look opaque.",
        "loading...done",
    ):
        assert _r(text) == text


def test_sentinel_is_stable_on_second_pass():
    once = _r("ghs_5078517_eyJhbGciOiJSUzI1NiIs...aDOQ")
    assert _r(once) == once
