"""approvals.mode=off must bypass EVERY approval gate, not just the ones that
happen to spell out the config check.

Incident (fork, 2026-09-08): after an upstream parity merge, two gate sites in
``tools/approval.py`` (``_run_approval_gate`` and ``check_dangerous_command``)
checked only ``_YOLO_MODE_FROZEN or is_current_session_yolo_enabled()`` while
two others also checked ``_get_approval_mode() == "off"``. Result: a user with
``approvals: {mode: 'off'}`` in config.yaml still got Discord approval buttons
for dangerous terminal commands, every time the fork re-synced.

Two contracts here:
1. AST: no bypass site in approval.py may hand-roll the yolo check — every
   ``_YOLO_MODE_FROZEN or is_current_session_yolo_enabled()`` expression must
   go through ``is_approval_bypass_active*`` (which folds in mode=off).
2. Behavior: with mode=off, a command that matches DANGEROUS_PATTERNS is
   approved without ever reaching a human callback.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

APPROVAL_PY = pathlib.Path(__file__).resolve().parents[2] / "tools" / "approval.py"


def _yolo_boolop_sites(tree: ast.AST) -> list[int]:
    """Line numbers of ``_YOLO_MODE_FROZEN or is_current_session_yolo_enabled()``
    BoolOps that are NOT inside ``is_approval_bypass_active_for_session``."""
    sites: list[int] = []
    allowed_funcs = {"is_approval_bypass_active_for_session"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name in allowed_funcs:
            continue
        for sub in ast.walk(node):
            if not (isinstance(sub, ast.BoolOp) and isinstance(sub.op, ast.Or)):
                continue
            names = set()
            for v in sub.values:
                if isinstance(v, ast.Name):
                    names.add(v.id)
                elif isinstance(v, ast.Call) and isinstance(v.func, ast.Name):
                    names.add(v.func.id)
            if "_YOLO_MODE_FROZEN" in names and "is_current_session_yolo_enabled" in names:
                sites.append(sub.lineno)
    return sites


def test_no_handrolled_yolo_bypass_outside_the_canonical_helper():
    tree = ast.parse(APPROVAL_PY.read_text())
    sites = _yolo_boolop_sites(tree)
    assert sites == [], (
        "hand-rolled yolo bypass (drops approvals.mode=off) at approval.py lines "
        f"{sites}; use is_approval_bypass_active() instead"
    )


def test_mode_off_bypasses_dangerous_command_gate(monkeypatch):
    import tools.approval as ap

    monkeypatch.setattr(ap, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(ap, "_get_approval_mode", lambda: "off")
    monkeypatch.setattr(ap, "is_session_yolo_enabled", lambda _k: False)
    calls: list = []
    monkeypatch.setattr(ap, "_run_approval_gate", lambda **kw: calls.append(kw) or {"approved": False, "message": "asked"})

    dangerous = "rm -rf /tmp/some-dir"
    is_dangerous, _, _ = ap.detect_dangerous_command(dangerous)
    assert is_dangerous, "fixture must match DANGEROUS_PATTERNS"

    res = ap.check_dangerous_command(dangerous, "local", approval_callback=lambda *a, **k: pytest.fail("human asked"))
    assert res["approved"] is True
    assert calls == [], "mode=off must never reach the human approval gate"


def test_mode_off_bypasses_shared_gate(monkeypatch):
    """SSH-config writes use this gate with fail_closed + a gateway notify.

    A non-interactive pytest process fail-OPENs the shared gate, so we have
    to pin a gateway context: without the mode=off bypass the test would
    await a Discord button (the 2026-09-20 symptom).
    """
    import tools.approval as ap

    monkeypatch.setattr(ap, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(ap, "_get_approval_mode", lambda: "off")
    monkeypatch.setattr(ap, "is_session_yolo_enabled", lambda _k: False)
    monkeypatch.setattr(ap, "is_current_session_yolo_enabled", lambda: False)
    monkeypatch.setattr(ap, "get_current_session_key", lambda default="": "test-session")
    monkeypatch.setattr(ap, "_is_interactive_cli", lambda: False)
    monkeypatch.setattr(ap, "_is_gateway_approval_context", lambda: True)
    ap._gateway_notify_cbs["test-session"] = lambda *a, **k: None
    monkeypatch.setattr(
        ap, "_await_gateway_decision",
        lambda *a, **k: pytest.fail("mode=off must not await a Discord button"),
    )
    try:
        res = ap._run_approval_gate(
            pattern_key="ssh_config_write",
            description="Write to SSH client config",
            display_target="<write to ~/.ssh/config>",
            cron_deny_message="c",
            single_query_deny_message="s",
            autoapprove_log_prefix="ssh_config_write",
            fail_closed_when_no_human=True,
        )
    finally:
        ap._gateway_notify_cbs.pop("test-session", None)
    assert res == {"approved": True, "message": None}
