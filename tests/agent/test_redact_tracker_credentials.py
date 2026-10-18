"""Private-tracker credential fields (IPTorrents session cookie, MyAnonamouse mam_id, passkeys,
Cloudflare clearance) are masked in tool output.

Incident t_95b396e6: a worker ran ``sqlite3 prowlarr.db "select * from IndexerStatus"`` and the
``Cookies`` column (JSON: ``{"uid": ..., "pass": ...}``, ``{"mam_id": ...}``,
``{"cf_clearance": ...}``) reached the session transcript verbatim, because none of those key
names were in the JSON / repr key lists.
"""

import json

import pytest

from agent.redact import (
    is_secret_field_name, redact_sensitive_json, redact_sensitive_text, redact_terminal_output)

# Synthetic shapes matching the real values (lengths/alphabets), never real credentials. Built at
# import time so no credential-shaped literal sits in the tree for secret scanners to flag.
_IPT_PASS = "0f1e2d3c" * 4  # 32 hex
_MAM_ID = "%2F".join(["AbCdEf0123456789"] * 4)
_CF = "-".join(["aBcD1234efGh5678ijKl9012mnOp3456", "1700000000", "1.2.1.1", "AbCdEf0123456789"])
_PASSKEY = "a1b2c3d4" * 4

_SECRETS = (_IPT_PASS, _MAM_ID, _CF, _PASSKEY)


def _assert_no_secret(out: str) -> None:
    for s in _SECRETS:
        assert s not in out
        assert s[:16] not in out


@pytest.mark.parametrize("key,value", [
    ("pass", _IPT_PASS),
    ("mam_id", _MAM_ID),
    ("mamId", _MAM_ID),
    ("cf_clearance", _CF),
    ("passkey", _PASSKEY),
    ("cookie", f"uid=1234567; pass={_IPT_PASS}"),
    ("cookies", f"uid=1234567; pass={_IPT_PASS}"),
])
def test_json_field_masked(key, value):
    out = redact_sensitive_text(json.dumps({key: value}), force=True)
    _assert_no_secret(out)
    assert f'"{key}"' in out  # field name kept: the reader still sees WHAT was there


def test_sqlite_indexerstatus_row_shape_masked():
    # The literal shape sqlite3 prints for IndexerStatus rows (pipe-separated, multi-line JSON cell).
    row = (
        "1|3|2026-10-15 11:53:47Z|2026-10-15 12:43:18Z|0|||{\n"
        '  "uid": "1234567",\n'
        f'  "pass": "{_IPT_PASS}"\n'
        "}|2026-11-17 19:08:20Z\n"
        "5|5|2026-09-14 07:42:14Z|2026-09-15 21:27:52Z|0|||{\n"
        f'  "mam_id": "{_MAM_ID}"\n'
        "}|2026-11-17 15:43:41Z\n"
        "8|8|2026-10-18 16:06:55Z|2026-10-18 16:06:55Z|0|||{\n"
        f'  "cf_clearance": "{_CF}",\n'
        '  "cf_chl_rc_ni": "3"\n'
        "}|2026-11-17 19:08:46Z\n"
    )
    out = redact_sensitive_text(row, force=True)
    _assert_no_secret(out)
    assert "2026-11-17 19:08:20Z" in out  # non-secret columns untouched


def test_terminal_boundary_sqlite_dump_masked():
    # The production path: terminal output for a sqlite3 command runs source-preserving
    # (code_file=True), which skips the JSON assignment pass (Prism P1 on #2085).
    row = ('1|3|{\n  "uid": "1234567",\n  "pass": "%s"\n}|x\n5|5|{\n  "mam_id": "%s"\n}|y\n'
           '{"cf_clearance": "%s", "cookie": "uid=1234567; pass=%s"}' % (_IPT_PASS, _MAM_ID, _CF, _IPT_PASS))
    out = redact_terminal_output(row, 'sqlite3 -readonly prowlarr.db "select * from IndexerStatus"', force=True)
    _assert_no_secret(out)
    assert '"uid": "1234567"' in out


def test_terminal_boundary_keeps_source_literals():
    src = 'cfg = {"pass": "yes", "cookie": "none"}\n'
    assert redact_terminal_output(src, "cat app.py", force=True) == src


def test_python_repr_fields_masked():
    text = repr({"cookie": f"uid=1; pass={_IPT_PASS}", "mamId": _MAM_ID, "passkey": _PASSKEY, "pass": _IPT_PASS,
                 "baseUrl": "http://prowlarr:9696/3/"})
    out = redact_sensitive_text(text, force=True)
    _assert_no_secret(out)
    assert "http://prowlarr:9696/3/" in out


def test_structured_leaf_masked():
    out = redact_sensitive_json({"mam_id": _MAM_ID, "cookie": f"pass={_IPT_PASS}", "name": "IPTorrents"},
                                force=True)
    _assert_no_secret(json.dumps(out))
    assert out["name"] == "IPTorrents"


@pytest.mark.parametrize("key", ["passkey", "cookie", "mam_id", "mamId", "cf_clearance", "pass"])
def test_field_name_policy(key):
    assert is_secret_field_name(key)


@pytest.mark.parametrize("key", ["passed", "password_policy", "cookie_count", "mam_id_len", "bypass"])
def test_non_credential_names_untouched(key):
    assert not is_secret_field_name(key)
