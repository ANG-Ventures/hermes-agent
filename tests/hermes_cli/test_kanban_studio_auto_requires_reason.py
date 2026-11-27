"""The two board writers of a ``requires: studio-state`` line give it a reason (t_d0852f2c).

hermes-agent checks the CALLER's body with hermes-home ``create_refusal()`` before create (t_4743853f), but
``kanban_studio_pin.prepare_body`` (CARD_NEEDS auto-derive, create + edit) and ``kanban_placement_block.pin_body``
(dispatch re-route of a placement block) then wrote a bare line themselves, which the daily Studio-share count
reports as bare. Each line they write must pass ``create_refusal()``. Kill switch is the hook's own
STUDIO_REQUIRES_REASON=0; no new knob.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_access_bound_pin as kabp
from hermes_cli import kanban_host_capabilities as khc
from hermes_cli import kanban_placement_block as kpb
from hermes_cli import kanban_studio_capability as ksc
from hermes_cli import kanban_studio_pin as ksp
from hermes_cli import kanban_worker_pool as kwp

# Stand-in for hermes-home hooks/access_bound_create_policy.py create_refusal() (hermes-home#10978 @ 8faed36a):
# the same line grammar, note cut and >= 3-word reason, against a fixed Studio-only set.
HOOK = r'''
import os, re
_REQ_LINE_RE = re.compile(r"^[ \t]*requires:[ \t]*(.*?)[ \t]*\r?$", re.IGNORECASE | re.MULTILINE)
_REQ_NOTE_RE = re.compile(r"\s(?:—|–|--|#)|[(#]")
_REQ_SPLIT_RE = re.compile(r"[,\s]+")
_NOTE_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'./~_-]*")
ONLY = {"studio-state", "fleet-ssh", "docker", "browser-session", "control-plane-read"}
def _words(v):
    return [w for w in (w.strip("`'\".;") for w in _REQ_SPLIT_RE.split(v.strip().strip("`").lower())) if w]
def pool_hosts(root):
    return set()
def verdict(body, hosts):
    return None
def create_refusal(body, root):
    if os.environ.get("STUDIO_REQUIRES_REASON", "").strip().lower() in ("0", "off", "false", "no"):
        return None
    for m in _REQ_LINE_RE.finditer(body or ""):
        raw = m.group(1)
        parts = _REQ_NOTE_RE.split(raw, maxsplit=1)
        hit = [w for w in (_words(parts[0]) or _words(raw)) if w in ONLY]
        if hit and len(_NOTE_WORD_RE.findall(parts[1] if len(parts) > 1 else "")) < 3:
            return "BLOCKED by studio-requires-reason: `%s` gives no reason" % m.group(0).strip()
    return None
'''


@pytest.fixture
def hook(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    (root / "hooks").mkdir(parents=True)
    (root / "hooks" / "access_bound_create_policy.py").write_text(HOOK, encoding="utf-8")
    monkeypatch.setattr(kabp, "_fleet_root", lambda: root)
    monkeypatch.delenv("STUDIO_REQUIRES_REASON", raising=False)
    assert kabp.studio_refusal("x\nrequires: studio-state\n")          # the stub refuses a bare line
    return root


def _requires_lines(body):
    return [ln for ln in body.splitlines() if ln.lstrip().lower().startswith("requires:")]


@pytest.mark.parametrize("body", [
    "host: studio\nlaunchctl print gui/501",                 # no requires: line: one is inserted
    "host: studio\nrequires: none\nlaunchctl print gui/501",  # an existing line gets the word and the reason
])
def test_prepare_body_auto_derived_line_passes_create_refusal(hook, monkeypatch, body):
    import hermes_cli.config as cfg
    monkeypatch.setattr(cfg, "load_config", lambda: {"kanban": {"studio_pin_requires_reason": "notice"}})
    monkeypatch.setattr(khc, "load_map", lambda fleet: None)
    monkeypatch.setattr(ksc, "create_reasons", lambda cmap: frozenset({"studio-state", "docker"}))
    out = ksp.prepare_body(body)
    lines = _requires_lines(out)
    print(lines)
    assert len(lines) == 1 and "studio-state" in khc.declared_words(out)
    assert "auto: card names studio_launchd" in lines[0]
    assert kabp.studio_refusal(out) is None
    monkeypatch.setenv("STUDIO_REQUIRES_REASON", "0")                   # the one kill switch still disarms
    assert kabp.studio_refusal("x\nrequires: studio-state\n") is None


@pytest.mark.parametrize("body", ["drain", "requires: none\ndrain", "host: studio\ndrain"])
@pytest.mark.parametrize("reason", [
    "The launchd job and its logs live on the Studio. Re-run on the Studio.",
    "docker",
    "",
    "Can't do the drain from here. This run was placed on ci-box (kanbanw), and the local side 6201024 only "
    "exists in mac-studio-m3u's ~/.hermes. Re-run on the Studio.\nsecond line # with a cut",
])
def test_pin_body_reroute_line_passes_create_refusal(hook, body, reason):
    # strict=False: the kill-switch-off path, which still writes the default word (t_9392860c).
    out = kpb.pin_body(body, kwp.PIN_STUDIO, reason=reason, studio_reasons=None, pool=_Pool(), strict=False)
    lines = _requires_lines(out)
    print(lines)
    assert len(lines) == 1 and "re-routed after capability block" in lines[0]
    assert kwp.studio_pin_justified(out, frozenset({kpb.DEFAULT_STUDIO_REQUIRES}))
    assert kabp.studio_refusal(out) is None


class _Pool:
    hosts = ("ace-ai", "ci-box")
    pin_aliases = None
