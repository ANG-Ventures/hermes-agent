"""``browser-session`` split into ``browser-identity``, ``browser-public`` and ``lan-reach`` (t_f935cb6f).

Design t_e7f581f6 (hermes-content docs/research/browser-rail-off-studio.md s3.3): one word stood for three
needs. A card whose browser calls only opened public pages was refused on every pool host as
``BROWSER_RAIL_ABSENT`` and re-routed to the Studio (22 of the 35 rail cards in 30 days). After the split:

* ``browser-identity`` is Ace's signed-in browser; ``browser-session`` is its alias for one release
  (``capabilities.browser-session.alias_of``), read as the target in every declaration and index row;
* ``browser-public`` is the agent's own Chromium, asked for by a browser_* call with no CDP override;
* ``lan-reach`` is a network check for named ``.ace`` hosts, no browser.

Hermetic: a temp board, the Phase 1 fixture map rewritten to the split shape, no network.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_host_capabilities as khc
from hermes_cli import kanban_placement_failfast as pff
from hermes_cli import kanban_worker_exit as kwe
from hermes_cli import kanban_worker_hosts as kwh

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "kanban_placement"
ENFORCE = {"placement": {"capabilities": "enforce"}}
POOL = ("ace-ai", "ace-media", "ci-box")


def _split_doc() -> dict:
    """The fixture map in the shape hermes-home ships with the split (fleet/host-capabilities.json)."""
    doc = json.loads((FIXTURES / khc.MAP_FILE).read_text(encoding="utf-8"))
    caps = doc["capabilities"]
    old = caps.pop("browser-session")
    doc["capabilities"] = {
        "browser-identity": {**old, "what": "a signed-in browser identity (Ace's cookies), by lease"},
        "browser-session": {"what": "alias of browser-identity for one release", "alias_of": "browser-identity"},
        "browser-public": {"what": "the agent's own headless Chromium for public pages", "act": ["act:browser"],
                           "soft": False},
        "lan-reach": {"what": "HTTP reach to the named .ace LAN hosts", "soft": False},
        **caps,
    }
    hosts = doc["hosts"]
    hosts["studio"]["capabilities"] = ["browser-identity", "browser-public", "lan-reach",
                                       *(c for c in hosts["studio"]["capabilities"] if c != "browser-session")]
    hosts["ace-ai"]["capabilities"] = ["browser-public", "lan-reach"]
    hosts["ace-media"]["capabilities"] = ["browser-public", "lan-reach"]
    hosts["ci-box"]["capabilities"] = ["lan-reach"]          # measured: agent-browser has no Chrome there
    return doc


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_OWNER_PID", raising=False)
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    monkeypatch.delenv(khc.CAPS_ENV, raising=False)
    monkeypatch.setattr(kwh, "BOOT_PLACED_HOST", None)
    monkeypatch.setattr(kwh, "remote_workspace_has_content", lambda raw, runner=None: False)
    fleet = home / "fleet"
    fleet.mkdir()
    (fleet / khc.MAP_FILE).write_text(json.dumps(_split_doc(), indent=2), encoding="utf-8")
    shutil.copy(FIXTURES / khc.INDEX_FILE, fleet / khc.INDEX_FILE)   # still says browser-session: the alias
    from tools import rail_preflight as rp
    rp._emitted.clear()
    yield fleet
    rp._emitted.clear()


def _placed(monkeypatch, fleet, task_id, run_id, host):
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setenv(kwh.PLACEMENT_ENV, json.dumps({"host": host, "env": {"TERMINAL_ENV": "ssh"}}))
    monkeypatch.setenv(khc.CAPS_ENV, khc.spawn_env(fleet, ENFORCE))   # what the dispatcher hands the worker


def _card(body="open https://example.com and summarize it"):
    with kb.connect_closing() as conn:
        return kb.create_task(conn, title="t", assignee="daedalus", body=body)


# -- the map: an alias reads as its target -------------------------------------------------

def test_alias_reads_as_its_target_in_declarations_index_rows_and_host_rows(board):
    cmap = khc.load_map(board)
    assert cmap.valid and cmap.aliases == {"browser-session": "browser-identity"}
    assert {"browser-identity", "browser-session"} <= cmap.hosts["studio"]
    assert "browser-identity" not in cmap.hosts["ace-ai"] and "browser-session" not in cmap.hosts["ace-ai"]
    reqs, _ = khc.derive("requires: browser-session\n", (), cmap)
    assert [(r.cap, r.term) for r in reqs] == [("browser-identity", "browser-session")]
    reqs, _ = khc.derive("", ["amazon-cart-ops-local"], cmap)            # index row still names the alias
    assert {r.cap for r in reqs} == {"browser-identity", "screen"}


def test_alias_and_target_share_one_mode_and_the_worker_env_enforces_both(board):
    ctx = khc.tick(board, ENFORCE, POOL)
    for cap in ("browser-identity", "browser-session", "browser-public", "lan-reach"):
        assert ctx.mode_of(cap) == khc.ENFORCE, cap
    env = json.loads(khc.spawn_env(board, ENFORCE))
    assert env["browser-identity"] == env["browser-session"] == env["browser-public"] == khc.ENFORCE


@pytest.mark.parametrize("soft_on", ["browser-identity", "browser-session"])
def test_a_soft_target_or_alias_makes_the_whole_group_warn_for_placement_and_worker(board, soft_on):
    # Prism 5f58fcfad85b: the kill switch on either name must not leave the other enforced in the worker env.
    doc = _split_doc()
    doc["capabilities"][soft_on]["soft"] = True
    (board / khc.MAP_FILE).write_text(json.dumps(doc), encoding="utf-8")
    ctx = khc.tick(board, ENFORCE, POOL)
    assert ctx.mode_of("browser-identity") == ctx.mode_of("browser-session") == khc.WARN
    env = json.loads(khc.spawn_env(board, ENFORCE))
    assert env.get("browser-identity", env.get("*")) != khc.ENFORCE
    assert env.get("browser-session", env.get("*")) != khc.ENFORCE
    assert env["browser-public"] == khc.ENFORCE


@pytest.mark.parametrize("alias_of", ["nope", "browser-session", 7])
def test_an_alias_to_nothing_itself_or_an_alias_is_a_malformed_map(board, alias_of):
    doc = _split_doc()
    doc["capabilities"]["browser-session"]["alias_of"] = alias_of
    (board / khc.MAP_FILE).write_text(json.dumps(doc), encoding="utf-8")
    assert "alias_of" in (khc.load_map(board).error or "")


# -- placement: identity stays held, public and LAN reach go to a pool host ----------------

def test_identity_card_is_held_and_a_public_or_lan_card_goes_to_the_pool(board):
    ctx = khc.tick(board, ENFORCE, POOL)
    held = khc.evaluate("requires: browser-session\n", (), ctx)
    assert held.hard_rule == "requires:browser-identity" and held.only == ()
    public = khc.evaluate("requires: browser-public\n", (), ctx)
    assert public.hard_rule is None and public.only == ("ace-ai", "ace-media")
    lan = khc.evaluate("requires: lan-reach\n", (), ctx)            # every pool host serves it: no restriction
    assert lan == khc.Verdict()
    nas = khc.tick(board, ENFORCE, (*POOL, "nas-fleet"))              # a host without it narrows the take
    assert khc.evaluate("requires: lan-reach\n", (), nas).only == POOL


def test_browser_remote_grants_the_identity_under_both_names(board):
    doc = _split_doc()
    doc["hosts"]["ace-ai"]["browser_remote"] = {"front_door": True, "ssh": True}
    (board / khc.MAP_FILE).write_text(json.dumps(doc), encoding="utf-8")
    ctx = khc.tick(board, ENFORCE, POOL)
    for body in ("requires: browser-session\n", "requires: browser-identity\n"):
        v = khc.evaluate(body, (), ctx)
        assert v.hard_rule is None and v.only == ("ace-ai",), body


# -- Layer 1b + the exit: a public page never reads as Ace's browser ------------------------

def test_public_page_call_on_a_host_without_browser_public_is_not_the_rail(board, monkeypatch):
    from tools import rail_preflight as rp
    from tools.registry import registry
    import model_tools  # noqa: F401
    tid = _card()
    _placed(monkeypatch, board, tid, 1, "ci-box")
    out = json.loads(registry.dispatch("browser_navigate", {"url": "https://example.com"}, task_id="t"))
    assert out["success"] is False
    assert out["error"].startswith("BROWSER_PUBLIC_ABSENT cap=browser-public host=ci-box")
    assert "BROWSER_RAIL_ABSENT" not in out["error"]
    assert rp.take_rail_absent() == out["error"]
    # A host whose agent-browser works serves it: no refusal at all.
    _placed(monkeypatch, board, tid, 1, "ace-ai")
    assert rp.browser_capability_error() is None


def test_a_cdp_override_is_still_the_identity_rail(board, monkeypatch):
    from tools import rail_preflight as rp
    tid = _card()
    _placed(monkeypatch, board, tid, 1, "ace-ai")
    monkeypatch.setenv("BROWSER_CDP_URL", "wss://cdp.browsers.ace/id-github-kyzcreig")
    line = rp.browser_capability_error()
    assert line.startswith("BROWSER_RAIL_ABSENT cap=browser-session host=ace-ai")


def test_a_real_profile_browser_is_identity_work(board, monkeypatch):
    # Prism a270aad9f004: browser.use_real_profile opens a copy of the user's signed-in profile, no CDP override.
    from tools import browser_tool_cloud as btc
    from tools import rail_preflight as rp
    tid = _card()
    _placed(monkeypatch, board, tid, 1, "ace-ai")                    # serves browser-public, not the identity
    assert rp.browser_capability_error() is None
    monkeypatch.setattr(btc, "_use_real_profile", lambda: True)
    assert rp.browser_capability_error().startswith("BROWSER_RAIL_ABSENT cap=browser-session host=ace-ai")


def test_a_vault_credential_is_identity_work_even_in_the_agents_own_browser(board, monkeypatch):
    # Opening public pages on a pool host must not open a way to type Ace's credentials into that host's Chromium.
    from tools import browser_vault_tool as bvt
    tid = _card()
    _placed(monkeypatch, board, tid, 1, "ace-ai")                    # serves browser-public, not the identity
    ran = []
    out = json.loads(bvt._fenced_page_op("t", lambda: ran.append(1) or "{}"))
    assert out["success"] is False and ran == []
    assert out["error"].startswith("BROWSER_RAIL_ABSENT cap=browser-session host=ace-ai")


def test_public_absent_reroutes_to_a_pool_host_never_the_studio(board, monkeypatch):
    tid = _card()
    with kb.connect_closing() as conn:
        assert kb.claim_task(conn, tid) is not None
        run = kb.latest_run(conn, tid).id
    _placed(monkeypatch, board, tid, run, "ci-box")
    from tools import rail_preflight as rp
    line = rp.browser_capability_error()
    assert kwe.block_on_rail_absent(line) is True
    with kb.connect_closing() as conn:
        t = kb.get_task(conn, tid)
    assert t.status == "ready"
    assert t.body.rstrip().endswith("requires: browser-public (re-routed after BROWSER_PUBLIC_ABSENT on ci-box)")
    v = khc.evaluate(t.body, (), khc.tick(board, ENFORCE, POOL))
    assert v.hard_rule is None and v.only == ("ace-ai", "ace-media")


def test_sandbox_failure_of_the_agents_own_browser_names_browser_public(board, monkeypatch):
    from tools import browser_tool_session as bts
    from tools.bot_desktop import runtime as bd
    tid = _card()
    _placed(monkeypatch, board, tid, 1, "ace-media")

    def refused():
        raise RuntimeError("Bot Desktop needs Xvnc inside the terminal backend's sandbox")
    monkeypatch.setattr(bd, "tool_placement", refused)
    err = bts._browser_command_preflight()["error"]
    assert pff.parse_absent(err) == ("BROWSER_PUBLIC_ABSENT", "browser-public", "ace-media")


# -- tokens ------------------------------------------------------------------------------

def test_tokens_only_the_identity_is_the_browser_rail():
    assert pff.token_for("browser-identity") == pff.token_for("browser-session") == "BROWSER_RAIL_ABSENT"
    assert pff.token_for("browser-public") == "BROWSER_PUBLIC_ABSENT"
    assert pff.token_for("lan-reach") == "LAN_REACH_ABSENT"
    capless = "BROWSER_RAIL_ABSENT: no browser session on ace-ai. This card needs Ace's logged-in browser."
    assert pff.parse_absent(capless) == ("BROWSER_RAIL_ABSENT", "browser-session", "ace-ai")
