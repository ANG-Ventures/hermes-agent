"""Truncated private-key blocks and elided tokens are redacted (t_7c575aa0).

A tool output cut before a PEM block's END line carried a private key past the
redactor, and tokens whose middle the tool elided (``ghs_..._eyJhbG...aDOQ``)
kept their visible fragments. A BEGIN header with no END redacts only its key
body, so a header quoted in source, a regex, docs or grep output keeps the text
after it. All material below is invented.
"""

import json

from agent.redact import redact_sensitive_text

_LINES = ["MIIEowIBAAKCAQEAinventedKEYmaterialNOTreal0123456789abcdefghij"] + [
    "Zm9vYmFyYmF6cXV4aW52ZW50ZWRrZXltYXRlcmlhbG5vdHJlYWwwMTIzNDU2"
] * 6
_BODY = "\n".join(_LINES)
_FULL = f"-----BEGIN RSA PRIVATE KEY-----\n{_BODY}\n-----END RSA PRIVATE KEY-----"
# Cut mid-line, then the tool's own truncation note on the next line.
_CUT = f"-----BEGIN RSA PRIVATE KEY-----\n{_BODY}\nZm9vYmFy\n... [output truncated]"
_R = "[REDACTED PRIVATE KEY]"


def _r(text: str) -> str:
    return redact_sensitive_text(text, force=True)


def _no_key(out: str) -> None:
    assert "Zm9vYmFy" not in out and "MIIE" not in out, out


def test_full_block_redacted():
    out = _r(f"before\n{_FULL}\nafter")
    assert out == f"before\n{_R}\nafter"


def test_block_cut_before_end_redacts_body_keeps_text_after():
    out = _r(f"==> /var/lib/x/state.opkf-1 <==\n{_CUT}\nexit 0")
    assert out == f"==> /var/lib/x/state.opkf-1 <==\n{_R}\n... [output truncated]\nexit 0"
    _no_key(out)


def test_block_cut_before_end_json_escaped():
    # The 11-24 shape: tool output arrives as a JSON string, newlines escaped.
    cut = "-----BEGIN RSA PRIVATE KEY-----\n" + _BODY + "\nZm9vYm"
    payload = json.dumps({"output": "==> f <==\n" + cut, "exit_code": 0})
    out = _r(payload)
    _no_key(out)
    assert out.startswith('{"output": "==> f <==\\n[REDACTED PRIVATE KEY]"') and out.endswith('"exit_code": 0}'), out
    payload = json.dumps({"output": "==> f <==\n" + cut + "\n... [truncated]\nnext line"})
    out = _r(payload)
    _no_key(out)
    assert out.endswith('\\n... [truncated]\\nnext line"}'), out


def test_encrypted_block_cut_before_end_redacted():
    text = f"-----BEGIN RSA PRIVATE KEY-----\nProc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,00AA\n\n{_BODY}"
    assert _r(text + "\nexit 0") == f"{_R}\nexit 0"


def test_two_blocks_second_cut():
    out = _r(f"{_FULL}\n==> next <==\n{_CUT}")
    assert out == f"{_R}\n==> next <==\n{_R}\n... [output truncated]"
    _no_key(out)


def test_openssh_block_cut_mid_line():
    assert _r("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9u") == _R


def test_header_without_key_lines_untouched():
    for text in (
        'PEM = "-----BEGIN OPENSSH PRIVATE KEY-----\\n"\n\ndef f():\n    return 1\n',
        'pattern = re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----")\nother = 2\n',
        "Paste it:\n\n```\n-----BEGIN RSA PRIVATE KEY-----\n...\n```\n\nThen run `make`.\n",
        'a.py:3:X = "-----BEGIN RSA PRIVATE KEY-----"\nb.py:9:unrelated\nexit 0',
        "k.pem:1:-----BEGIN RSA PRIVATE KEY-----\nk2.pem:1:-----BEGIN RSA PRIVATE KEY-----\nexit 0",
    ):
        assert _r(text) == text
        assert redact_sensitive_text(text, force=True, code_file=True) == text
        assert redact_sensitive_text(text, file_read=True) == text


def test_file_with_full_fixture_and_code_after_only_block_goes():
    src = f'FIXTURE = """\n{_FULL}\n"""\n\n\ndef load():\n    return FIXTURE.strip()\n'
    want = f'FIXTURE = """\n{_R}\n"""\n\n\ndef load():\n    return FIXTURE.strip()\n'
    assert redact_sensitive_text(src, file_read=True) == want
    assert redact_sensitive_text(src, force=True, code_file=True) == want


def test_token_with_two_elision_markers_redacted_whole():
    for text in ("token ghs_..._eyJhbG...aDOQ end", "token ghs_5078517_eyJhbG...NiIs...aDOQ end"):
        out = _r(text)
        assert out.startswith("token ") and out.endswith(" end"), out
        for frag in ("aDOQ", "eyJhbG", "5078517", "NiIs", "..."):
            assert frag not in out, out


def test_token_with_unicode_elision_marker_redacted_whole():
    for tok in ("ghp_AbCdEfGh…WxYz", "github_pat_11AB…Q9rS", "sk-ant-api03-AbCd…x9Yz", "glpat-AbCdEfGh…WxYz"):
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
        "SGTM...ok",
    ):
        assert _r(text) == text


def test_sentinel_is_stable_on_second_pass():
    once = _r("ghs_5078517_eyJhbG...NiIs...aDOQ")
    assert _r(once) == once
