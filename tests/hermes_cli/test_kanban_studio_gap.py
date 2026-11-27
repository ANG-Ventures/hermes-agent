"""t_9d619fbb: a Studio override for a GAP capability must say how it would run elsewhere (owner ruling 2026-11-27).

Rules pinned here:
1. An inherent capability (Studio-only in the map, e.g. keychain) passes with no new flags.
2. A gap capability needs --elsewhere (20+ chars, names a pool host), --why-not-now (20+ chars), --effort hours|days.
3. hours needs an OPEN --fix-card and covers one run (--until refused); days passes, answers recorded.
4. A done fix card closes the gap: that card and any later override on the same card are refused.
5. The kill-switch file restores today's behaviour.
6. Replay of the 17 explainer-session overrides (2026-11-26): the first is refused with the gap-card command; with
   effort hours and an open fix card all 17 pass and the ledger carries the card id.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_studio_allowlist as ksa
from hermes_cli import kanban_studio_gap as ksg

from tests.hermes_cli.test_kanban_worker_pool import kanban_home  # noqa: F401

HOSTS = ("ace-ai", "ace-media", "ci-box", "nas-fleet")
EW = "ci-box once ffmpeg, Kokoro and Manim are installed there"
WN = "the render toolchain is not installed on ci-box yet"
FIXTURE = Path(__file__).parent / "fixtures" / "studio_gap_explainer_overrides.json"


@pytest.fixture
def operator(kanban_home, monkeypatch):
    p = kb.operator_token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("tok")
    monkeypatch.setenv(kb.OPERATOR_TOKEN_ENV, "tok")
    monkeypatch.setattr(ksg, "studio_only_caps", lambda root: frozenset({"keychain", "browser-identity"}))
    monkeypatch.setattr(ksg, "pool_hosts", lambda root: HOSTS)
    return kanban_home


def _card(conn, body="render the explainer", title="s"):
    return kb.create_task(conn, title=title, assignee="alpha", body=body)


def _ledger(home):
    return [json.loads(x) for x in ksa.ledger_path(home).read_text().splitlines()]


def _done(conn, tid):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))


# -- 1. classification --------------------------------------------------------------------------------------------

def test_classify():
    so = frozenset({"keychain", "browser-identity"})
    assert ksg.classify("keychain", studio_only=so) == ksg.INHERENT
    assert ksg.classify("browser-session", studio_only=so) == ksg.INHERENT  # alias of browser-identity
    assert ksg.classify("studio-state", studio_only=so) == ksg.GAP          # a pool host reaches it over ssh
    assert ksg.classify("studio-state", allow={"studio-state"}, studio_only=so) == ksg.INHERENT
    assert ksg.classify("studio-state", studio_only=None) == ksg.INHERENT   # map unreadable: today
    for stand_in in ("operator_admit", "workspace_kind", "workspace_kind:origin_not_ours", "pool_unavailable"):
        assert ksg.classify(stand_in, studio_only=None) == ksg.GAP


def test_inherent_capability_passes_with_no_fix_card(operator):
    with kb.connect_closing() as conn:
        tid = _card(conn, body="requires: keychain")
        p = ksa.override(conn, tid, "keychain", "reads the login keychain item")
    assert p["class"] == "inherent" and p["fix_card"] is None and "elsewhere" not in p
    assert _ledger(operator)[0]["class"] == "inherent"


# -- 2. the answers -----------------------------------------------------------------------------------------------

def test_gap_without_answers_is_refused_with_the_one_screen(operator):
    with kb.connect_closing() as conn:
        tid = _card(conn, body="requires: studio-state (the board)")
        with pytest.raises(ksa.OverrideRefused) as e:
            ksa.override(conn, tid, "studio-state", "reads the live board db")
    msg = str(e.value)
    assert "--elsewhere is required" in msg and "Pool hosts: ace-ai, ace-media, ci-box, nas-fleet" in msg
    assert "requires: studio-state (the board)" in msg
    assert "hermes kanban --board default create" in msg and "--idempotency-key studio-capgap:studio-state" in msg
    assert not ksa.ledger_path(operator).exists()


@pytest.mark.parametrize("kw,match", [
    (dict(elsewhere="not installed", why_not_now=WN, effort="days"), "--elsewhere is required"),
    (dict(elsewhere="it is simply not installed on the other computers", why_not_now=WN, effort="days"),
     "must name the pool host"),
    (dict(elsewhere=EW, why_not_now="busy", effort="days"), "--why-not-now is required"),
    (dict(elsewhere=EW, why_not_now=WN, effort=None), "--effort is required"),
    (dict(elsewhere=EW, why_not_now=WN, effort="weeks"), "--effort is required"),
    (dict(elsewhere=EW, why_not_now=WN, effort="hours"), "name the open gap card with --fix-card"),
    (dict(elsewhere=EW, why_not_now=WN, effort="hours", fix_card="t_nonexist"), "no such card"),
])
def test_gap_refusals(operator, kw, match):
    with kb.connect_closing() as conn:
        tid = _card(conn)
        with pytest.raises(ksa.OverrideRefused, match=match):
            ksa.override(conn, tid, "operator_admit", "render toolchain is Studio-only", **kw)


def test_days_passes_and_records_the_answers(operator):
    with kb.connect_closing() as conn:
        tid = _card(conn)
        p = ksa.override(conn, tid, "operator_admit", "render toolchain is Studio-only", until="+8h",
                         elsewhere=EW, why_not_now=WN, effort="days")
        assert ksa.live_override(conn, tid, "operator_admit") is not None
    row = _ledger(operator)[0]
    assert p["class"] == row["class"] == "gap" and row["effort"] == "days"
    assert row["elsewhere"] == EW and row["why_not_now"] == WN and row["fix_card_given"] is None


# -- 3/4. hours, the fix card, closing the gap -------------------------------------------------------------------

def test_hours_with_open_fix_card_passes_once_and_until_is_refused(operator):
    with kb.connect_closing() as conn:
        fix = _card(conn, title="install the render toolchain on ci-box")
        tid = _card(conn)
        with pytest.raises(ksa.OverrideRefused, match="covers ONE run"):
            ksa.override(conn, tid, "operator_admit", "render toolchain is Studio-only", until="+8h",
                         elsewhere=EW, why_not_now=WN, effort="hours", fix_card=fix)
        p = ksa.override(conn, tid, "operator_admit", "render toolchain is Studio-only",
                         elsewhere=EW, why_not_now=WN, effort="hours", fix_card=fix)
        assert p["once"] and p["fix_card"] == fix
        # a second override for the same gap passes while the fix card is open
        ksa.override(conn, tid, "operator_admit", "render toolchain is Studio-only",
                     elsewhere=EW, why_not_now=WN, effort="hours", fix_card=fix)
    assert [r["fix_card"] for r in _ledger(operator)] == [fix, fix]


def test_per_gap_key_first_override_needs_answers_and_an_open_fix_card(operator):
    """Case 1: the first hours override for a gap key needs both answers and an open fix card."""
    with kb.connect_closing() as conn:
        tid = _card(conn)
        with pytest.raises(ksa.OverrideRefused, match="--elsewhere is required"):
            ksa.override(conn, tid, "operator_admit", "render toolchain is Studio-only")
        with pytest.raises(ksa.OverrideRefused, match="name the open gap card with --fix-card"):
            ksa.override(conn, tid, "operator_admit", "render toolchain is Studio-only",
                         elsewhere=EW, why_not_now=WN, effort="hours")
        fix = _card(conn, title="install the render toolchain on ci-box")
        p = ksa.override(conn, tid, "operator_admit", "render toolchain is Studio-only",
                         elsewhere=EW, why_not_now=WN, effort="hours", fix_card=fix)
    assert p["fix_card"] == fix and not p.get("counted")


def test_per_gap_key_later_overrides_on_any_card_pass_and_are_counted_on_the_fix_card(operator):
    """Case 2: while the key's fix card is open, any card's override for that key passes and is counted on it."""
    with kb.connect_closing() as conn:
        fix = _card(conn, title="install the render toolchain on ci-box")
        first = _card(conn)
        ksa.override(conn, first, "operator_admit", "render toolchain is Studio-only",
                     elsewhere=EW, why_not_now=WN, effort="hours", fix_card=fix)
        others = [_card(conn) for _ in range(3)]
        got = [ksa.override(conn, t, "operator_admit", "render toolchain is Studio-only") for t in others]
        with pytest.raises(ksa.OverrideRefused, match="covers ONE run"):
            ksa.override(conn, _card(conn), "operator_admit", "render toolchain is Studio-only", until="+8h")
        # a different gap key is not covered by this fix card
        with pytest.raises(ksa.OverrideRefused, match="--elsewhere is required"):
            ksa.override(conn, _card(conn), "workspace_kind", "the worktree lives on the Studio")
        comments = [b for (b,) in conn.execute("SELECT body FROM task_comments WHERE task_id=? ORDER BY id", (fix,))]
    assert all(p["counted"] and p["fix_card"] == fix for p in got)
    assert len(comments) == 4 and comments[-1].startswith("studio-override #4 for gap operator_admit: " + others[-1])


def test_per_gap_key_done_fix_card_refuses_every_card(operator):
    """Case 3: once the key's fix card is done, an override for the key is refused on every card, naming it."""
    with kb.connect_closing() as conn:
        fix = _card(conn, title="install the render toolchain on ci-box")
        tid = _card(conn)
        ksa.override(conn, tid, "operator_admit", "render toolchain is Studio-only",
                     elsewhere=EW, why_not_now=WN, effort="hours", fix_card=fix)
        _done(conn, fix)
        for card in (tid, _card(conn)):
            with pytest.raises(ksa.OverrideRefused) as e:
                ksa.override(conn, card, "operator_admit", "render toolchain is Studio-only",
                             elsewhere=EW, why_not_now=WN, effort="days")
            assert f"the gap card {fix} for operator_admit is done" in str(e.value)
            assert "run it on a pool host: ace-ai, ace-media, ci-box, nas-fleet" in str(e.value)
        with pytest.raises(ksa.OverrideRefused, match=f"--fix-card {fix} is done: the gap operator_admit is closed"):
            ksa.override(conn, _card(conn), "operator_admit", "render toolchain is Studio-only",
                         elsewhere=EW, why_not_now=WN, effort="hours", fix_card=fix)


def test_per_gap_key_reopened_gap_takes_a_new_fix_card(operator):
    """Case 4: the gap reopened: a NEW open fix card with the answers is accepted, and later cards count on it."""
    with kb.connect_closing() as conn:
        old = _card(conn, title="install the render toolchain on ci-box")
        ksa.override(conn, _card(conn), "operator_admit", "render toolchain is Studio-only",
                     elsewhere=EW, why_not_now=WN, effort="hours", fix_card=old)
        _done(conn, old)
        new = _card(conn, title="reinstall the render toolchain on ci-box after the rebuild")
        p = ksa.override(conn, _card(conn), "operator_admit", "render toolchain is Studio-only",
                         elsewhere=EW, why_not_now=WN, effort="hours", fix_card=new)
        q = ksa.override(conn, _card(conn), "operator_admit", "render toolchain is Studio-only")
    assert p["fix_card"] == new and q["counted"] and q["fix_card"] == new


def test_repeated_identical_refusal_within_24h_is_one_line(operator):
    """Operator ruling: the second identical refusal (same gap key + problem) within 24 h is one line + fix card."""
    with kb.connect_closing() as conn:
        a, b = _card(conn), _card(conn)
        with pytest.raises(ksa.OverrideRefused) as first:
            ksa.override(conn, a, "studio-state", "reads the live board db", now=1_000_000.0)
        with pytest.raises(ksa.OverrideRefused) as second:
            ksa.override(conn, b, "studio-state", "reads the live board db", now=1_000_000.0 + 3600)
        with pytest.raises(ksa.OverrideRefused) as later:
            ksa.override(conn, b, "studio-state", "reads the live board db", now=1_000_000.0 + 3600 + 25 * 3600)
    assert "\n" in str(first.value) and "Pool hosts:" in str(first.value)
    short = str(second.value)
    assert "\n" not in short and "fix card: none" in short and f"full screen printed for {a} 60 min ago" in short
    assert "studio-capgap:studio-state" in short
    assert "Pool hosts:" in str(later.value)  # outside the window: the full screen again
    assert len(ksg.refusals_path(operator).read_text().splitlines()) == 3


def test_archived_fix_card_does_not_close_the_gap(operator):
    """Prism 7a9922be2606: an archived (abandoned) fix card is not a closed gap; an open replacement passes."""
    with kb.connect_closing() as conn:
        fix_a = _card(conn, title="install the render toolchain on ci-box")
        tid = _card(conn)
        ksa.override(conn, tid, "operator_admit", "render toolchain is Studio-only",
                     elsewhere=EW, why_not_now=WN, effort="hours", fix_card=fix_a)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (fix_a,))
        with pytest.raises(ksa.OverrideRefused, match=f"--fix-card {fix_a} is archived, not open"):
            ksa.override(conn, tid, "operator_admit", "render toolchain is Studio-only",
                         elsewhere=EW, why_not_now=WN, effort="hours", fix_card=fix_a)
        fix_b = _card(conn, title="install the render toolchain on ci-box, take 2")
        p = ksa.override(conn, tid, "operator_admit", "render toolchain is Studio-only",
                         elsewhere=EW, why_not_now=WN, effort="hours", fix_card=fix_b)
    assert p["fix_card"] == fix_b


def test_refusal_create_command_names_the_board(operator):
    """Prism 506ff9d7da6e: the printed create command carries the override's board."""
    with kb.connect_closing() as conn:
        tid = _card(conn)
        with pytest.raises(ksa.OverrideRefused) as e:
            ksa.override(conn, tid, "operator_admit", "render toolchain is Studio-only", board="media")
    assert "hermes kanban --board media create " in str(e.value)
    assert ksg.create_command("operator_admit").startswith("hermes kanban create ")


def test_pool_hosts_are_placement_targets_without_the_studio(tmp_path, monkeypatch):
    """Prism 9c2756fa53d1: a registered but targets:false Studio is not a pool host, so --elsewhere naming it fails."""
    from hermes_cli import kanban_db_dispatch as kbd
    from tests.hermes_cli.test_kanban_worker_pool import _studio_registered
    fleet = tmp_path / "fleet"
    fleet.mkdir()
    _studio_registered(fleet, {"mac-studio": {"class": "studio-source", "targets": False},
                               "ace-ai": {"class": "linux-shared"}, "ace-media": {"class": "linux-shared"}})
    monkeypatch.setattr(kbd, "_root_kanban_cfg", lambda: {})
    hosts = ksg.pool_hosts(tmp_path)
    assert hosts == ("ace-ai", "ace-media")
    assert not ksg._names_host("mac-studio after installing ffmpeg", hosts)
    # even when the Studio IS a placement target it never counts as "elsewhere"
    _studio_registered(fleet, {"mac-studio": {"class": "linux-shared", "targets": True}})
    assert ksg.pool_hosts(tmp_path) == ("ace-ai", "ace-media")


# -- 5. kill switch -----------------------------------------------------------------------------------------------

def test_kill_switch_file_restores_todays_behaviour(operator):
    path = ksg.kill_path(operator)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("off\n")
    with kb.connect_closing() as conn:
        tid = _card(conn)
        p = ksa.override(conn, tid, "operator_admit", "render toolchain is Studio-only", until="+8h")
    assert "class" not in p and not p["once"]
    assert "class" not in _ledger(operator)[0]


def test_parser_takes_the_gap_flags():
    import argparse
    from hermes_cli import kanban_parser
    top = argparse.ArgumentParser()
    sub = top.add_subparsers(dest="cmd")
    kanban_parser.build_parser(sub)
    ns = top.parse_args(["kanban", "studio-override", "t_x", "--capability", "operator_admit", "--why", "a b c",
                         "--elsewhere", EW, "--why-not-now", WN, "--effort", "hours", "--fix-card", "t_y"])
    assert (ns.elsewhere, ns.why_not_now, ns.effort, ns.fix_card) == (EW, WN, "hours", "t_y")
    with pytest.raises(SystemExit):
        top.parse_args(["kanban", "studio-override", "t_x", "--capability", "c", "--why", "a b c",
                        "--effort", "weeks"])


# -- 6. replay of the explainer session --------------------------------------------------------------------------

def test_replay_explainer_session_overrides(operator):
    seq = json.loads(FIXTURE.read_text())
    assert len(seq) == 17
    with kb.connect_closing() as conn:
        card = {orig: _card(conn) for orig in dict.fromkeys(t for t, _, _ in seq)}  # 11 cards
        seq = [(card[t], cap, why) for t, cap, why in seq]
        first_tid, first_cap, first_why = seq[0]
        # as it was run on 11-26 (no answers): refused, with the gap-card command
        with pytest.raises(ksa.OverrideRefused) as e:
            ksa.override(conn, first_tid, first_cap, first_why, until="+8h")
        assert "hermes kanban --board default create" in str(e.value) and "--fix-card" in str(e.value)
        fix = _card(conn, title="install the explainer render toolchain on ci-box")
        seen = set()
        for tid, cap, why in seq:
            if cap in seen:  # per gap key: the open fix card already covers this gap, no answers asked again
                p = ksa.override(conn, tid, cap, why)
                assert p["counted"]
            else:
                p = ksa.override(conn, tid, cap, why, elsewhere=EW, why_not_now=WN, effort="hours", fix_card=fix)
                seen.add(cap)
            assert p["class"] == "gap" and p["fix_card"] == fix
        counts = [b for (b,) in conn.execute("SELECT body FROM task_comments WHERE task_id=?", (fix,))]
    rows = _ledger(operator)
    assert len(rows) == 17 and {r["fix_card"] for r in rows} == {fix}
    assert seen == {"operator_admit", "workspace_kind"} and len(counts) == 17 and "studio-override #17" in counts[-1]
