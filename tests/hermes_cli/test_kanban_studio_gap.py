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
    p.write_text("tok", encoding="utf-8")
    monkeypatch.setenv(kb.OPERATOR_TOKEN_ENV, "tok")
    monkeypatch.setattr(ksg, "studio_only_caps", lambda root: frozenset({"keychain", "browser-identity"}))
    monkeypatch.setattr(ksg, "pool_hosts", lambda root: HOSTS)
    return kanban_home


def _card(conn, body="render the explainer", title="s"):
    return kb.create_task(conn, title=title, assignee="alpha", body=body)


def _fix(conn, title="install the render toolchain on ci-box", keys=("operator_admit",), idem=None):
    """A real gap card: the body line the printed create command writes (operator read 3)."""
    body = "\n".join(f"capability-gap: {k}" for k in keys) + "\nInstall it on ci-box."
    return kb.create_task(conn, title=title, assignee="alpha", body=body, idempotency_key=idem)


def _ledger(home):
    return [json.loads(x) for x in ksa.ledger_path(home).read_text(encoding="utf-8-sig").splitlines()]


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
        fix = _fix(conn, title="install the render toolchain on ci-box")
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
        fix = _fix(conn, title="install the render toolchain on ci-box")
        p = ksa.override(conn, tid, "operator_admit", "render toolchain is Studio-only",
                         elsewhere=EW, why_not_now=WN, effort="hours", fix_card=fix)
    assert p["fix_card"] == fix and not p.get("counted")


def test_per_gap_key_later_overrides_on_any_card_pass_and_are_counted_on_the_fix_card(operator):
    """Case 2: while the key's fix card is open, any card's override for that key passes and is counted on it."""
    with kb.connect_closing() as conn:
        fix = _fix(conn, title="install the render toolchain on ci-box")
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
    # one line per gap key per day, its running total moved in place (operator ruling 8)
    assert len(comments) == 1 and " 4 Studio run(s) so far" in comments[0] and f"latest {others[-1]}" in comments[0]


def test_per_gap_key_done_fix_card_refuses_every_card(operator):
    """Case 3: once the key's fix card is done, an override for the key is refused on every card, naming it."""
    with kb.connect_closing() as conn:
        fix = _fix(conn, title="install the render toolchain on ci-box")
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
        old = _fix(conn, title="install the render toolchain on ci-box")
        ksa.override(conn, _card(conn), "operator_admit", "render toolchain is Studio-only",
                     elsewhere=EW, why_not_now=WN, effort="hours", fix_card=old)
        _done(conn, old)
        new = _fix(conn, title="reinstall the render toolchain on ci-box after the rebuild")
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
    assert len(ksg.refusals_path(operator).read_text(encoding="utf-8-sig").splitlines()) == 3


def test_archived_fix_card_does_not_close_the_gap(operator):
    """Prism 7a9922be2606: an archived (abandoned) fix card is not a closed gap; an open replacement passes."""
    with kb.connect_closing() as conn:
        fix_a = _fix(conn, title="install the render toolchain on ci-box")
        tid = _card(conn)
        ksa.override(conn, tid, "operator_admit", "render toolchain is Studio-only",
                     elsewhere=EW, why_not_now=WN, effort="hours", fix_card=fix_a)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (fix_a,))
        with pytest.raises(ksa.OverrideRefused, match=f"--fix-card {fix_a} is archived, not open"):
            ksa.override(conn, tid, "operator_admit", "render toolchain is Studio-only",
                         elsewhere=EW, why_not_now=WN, effort="hours", fix_card=fix_a)
        fix_b = _fix(conn, title="install the render toolchain on ci-box, take 2")
        p = ksa.override(conn, tid, "operator_admit", "render toolchain is Studio-only",
                         elsewhere=EW, why_not_now=WN, effort="hours", fix_card=fix_b)
    assert p["fix_card"] == fix_b


def test_open_fix_card_older_than_the_closing_one_does_not_count(operator):
    """Prism 434fc10f5122: fix cards A then B for one key, B done, A still open: no-answer overrides are refused."""
    with kb.connect_closing() as conn:
        a, b = _fix(conn, title="install A"), _fix(conn, title="install B")
        for fix in (a, b):
            ksa.override(conn, _card(conn), "operator_admit", "render toolchain is Studio-only",
                         elsewhere=EW, why_not_now=WN, effort="hours", fix_card=fix)
        _done(conn, b)
        with pytest.raises(ksa.OverrideRefused, match=f"the gap card {b} for operator_admit is done"):
            ksa.override(conn, _card(conn), "operator_admit", "render toolchain is Studio-only")
        # naming the older open card explicitly is the reopened path: it needs the answers
        with pytest.raises(ksa.OverrideRefused, match="--elsewhere is required"):
            ksa.override(conn, _card(conn), "operator_admit", "render toolchain is Studio-only", fix_card=a)
        p = ksa.override(conn, _card(conn), "operator_admit", "render toolchain is Studio-only",
                         elsewhere=EW, why_not_now=WN, effort="hours", fix_card=a)
        q = ksa.override(conn, _card(conn), "operator_admit", "render toolchain is Studio-only")
    assert not p.get("counted") and q["counted"] and q["fix_card"] == a


def _complete(conn, tid):
    """Done the way a real completion leaves it: the status AND a newer event on the card."""
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        kb._append_event(conn, tid, "completed", {})


def test_counted_run_does_not_reregister_an_older_fix_card(operator):
    """Prism be1f77072d51: A then B registered, a run counted on A explicitly, B done: A no longer counts runs."""
    with kb.connect_closing() as conn:
        a, b = _fix(conn, title="install A"), _fix(conn, title="install B")
        for fix in (a, b):
            ksa.override(conn, _card(conn), "operator_admit", "render toolchain is Studio-only",
                         elsewhere=EW, why_not_now=WN, effort="hours", fix_card=fix)
        c = ksa.override(conn, _card(conn), "operator_admit", "render toolchain is Studio-only", fix_card=a)
        assert c["counted"] and c["fix_card"] == a
        _complete(conn, b)
        with pytest.raises(ksa.OverrideRefused, match=f"the gap card {b} for operator_admit is done"):
            ksa.override(conn, _card(conn), "operator_admit", "render toolchain is Studio-only")


def test_closure_is_when_the_fix_card_became_done_not_when_it_was_named(operator):
    """Prism 434fc10f5122: A, B, then A re-registered; B done AFTER that: A's registration predates the closure."""
    with kb.connect_closing() as conn:
        a, b = _fix(conn, title="install A"), _fix(conn, title="install B")
        for fix in (a, b, a):
            ksa.override(conn, _card(conn), "operator_admit", "render toolchain is Studio-only",
                         elsewhere=EW, why_not_now=WN, effort="hours", fix_card=fix)
        _complete(conn, b)
        with pytest.raises(ksa.OverrideRefused, match=f"the gap card {b} for operator_admit is done"):
            ksa.override(conn, _card(conn), "operator_admit", "render toolchain is Studio-only")
        # registering A again after the closure is the reopened path, and then it counts
        ksa.override(conn, _card(conn), "operator_admit", "render toolchain is Studio-only",
                     elsewhere=EW, why_not_now=WN, effort="hours", fix_card=a)
        q = ksa.override(conn, _card(conn), "operator_admit", "render toolchain is Studio-only")
    assert q["counted"] and q["fix_card"] == a


def test_gc_kept_override_no_longer_grants_a_studio_run(operator):
    """Prism 066c453617e1: gc keeps the registration but prunes the spawn that spent it, so it must not be live."""
    with kb.connect_closing() as conn:
        fix, a = _fix(conn, title="install the render toolchain on ci-box"), _card(conn)
        ksa.override(conn, a, "operator_admit", "render toolchain is Studio-only",
                     elsewhere=EW, why_not_now=WN, effort="hours", fix_card=fix)
        assert ksa.live_override(conn, a, "operator_admit") is not None
        _done(conn, a)
        with kb.write_txn(conn):
            conn.execute("UPDATE task_events SET created_at = 1 WHERE task_id = ?", (a,))
        kb.gc_events(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (a,))
        assert ksa.live_override(conn, a, "operator_admit") is None
        # the gap registry still reads it: fix is the open card, a later run is counted on it
        q = ksa.override(conn, _card(conn), "operator_admit", "render toolchain is Studio-only")
    assert q["counted"] and q["fix_card"] == fix


def test_readable_pool_with_no_off_studio_host_refuses_a_gap(operator, monkeypatch):
    """Prism 0363103a464a: an empty (but readable) pool is not 'unreadable'; --elsewhere cannot name the Studio."""
    monkeypatch.setattr(ksg, "pool_hosts", lambda root: ())
    with kb.connect_closing() as conn:
        with pytest.raises(ksa.OverrideRefused, match="no off-Studio placement target") as e:
            ksa.override(conn, _card(conn), "operator_admit", "render toolchain is Studio-only",
                         elsewhere="Install the toolchain on mac-studio", why_not_now=WN, effort="days")
    assert "Pool hosts: (none off the Studio)" in str(e.value)
    monkeypatch.setattr(ksg, "pool_hosts", lambda root: None)  # unreadable: host check skipped, as before
    with kb.connect_closing() as conn:
        p = ksa.override(conn, _card(conn), "operator_admit", "render toolchain is Studio-only",
                         elsewhere="Install the toolchain on mac-studio", why_not_now=WN, effort="days")
    assert p["class"] == "gap"


def test_pool_hosts_absent_or_malformed_pool_file_is_none(tmp_path, monkeypatch):
    """Operator read 2: read_pool never raises; an absent or malformed roles file is a refused/empty PoolConfig,
    which must read as unreadable (None), not as 'no off-Studio host' (which refuses every gap override)."""
    from hermes_cli import kanban_db_dispatch as kbd
    monkeypatch.setattr(kbd, "_root_kanban_cfg", lambda: {})
    assert ksg.pool_hosts(tmp_path) is None                                  # no fleet dir at all
    (tmp_path / "fleet").mkdir()
    assert ksg.pool_hosts(tmp_path) is None                                  # fleet dir, no roles file
    (tmp_path / "fleet" / "fleet-roles.json").write_text("{not json", encoding="utf-8")
    assert ksg.pool_hosts(tmp_path) is None                                  # malformed roles file
    (tmp_path / "fleet" / "fleet-roles.json").write_text('{"schema": 1, "hosts": []}', encoding="utf-8")
    assert ksg.pool_hosts(tmp_path) is None                                  # structurally refused


def test_malformed_pool_file_does_not_refuse_a_fully_answered_days_override(kanban_home, monkeypatch):
    p = kb.operator_token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("tok", encoding="utf-8")
    monkeypatch.setenv(kb.OPERATOR_TOKEN_ENV, "tok")
    from hermes_cli import kanban_db_dispatch as kbd
    monkeypatch.setattr(kbd, "_root_kanban_cfg", lambda: {})
    fleet = Path(kanban_home) / "fleet"
    fleet.mkdir(parents=True, exist_ok=True)
    (fleet / "fleet-roles.json").write_text("{not json", encoding="utf-8")
    with kb.connect_closing() as conn:
        p = ksa.override(conn, _card(conn), "operator_admit", "render toolchain is Studio-only",
                         elsewhere=EW, why_not_now=WN, effort="days")
    assert p["class"] == "gap" and p["effort"] == "days"


def _write_map(fleet: Path, doc) -> None:
    fleet.mkdir(parents=True, exist_ok=True)
    (fleet / "host-capabilities.json").write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")


@pytest.mark.parametrize("doc", ["{not json", {"schema": 1, "hosts": {"ace-ai": ["docker"]}}],
                         ids=["malformed", "no-studio-row"])
def test_map_present_but_unusable_is_unreadable_not_all_gaps(tmp_path, doc):
    """Operator read 1: a map that exists but is malformed or has no Studio row must read as None (today's
    behaviour), not as an empty Studio-only set that turns keychain/browser-identity into gaps."""
    _write_map(tmp_path / "fleet", doc)
    so = ksg.studio_only_caps(tmp_path)
    assert so is None
    for cap in ("keychain", "browser-identity"):
        assert ksg.classify(cap, studio_only=so) == ksg.INHERENT


def test_gc_keeps_the_override_that_registers_a_fix_card(operator):
    """Prism 506ff9d7da6e: kanban gc past 30 days must not forget a closed gap."""
    with kb.connect_closing() as conn:
        fix, a = _fix(conn, title="install the render toolchain on ci-box"), _card(conn)
        ksa.override(conn, a, "operator_admit", "render toolchain is Studio-only",
                     elsewhere=EW, why_not_now=WN, effort="hours", fix_card=fix)
        _done(conn, a)
        _done(conn, fix)
        with kb.write_txn(conn):
            conn.execute("UPDATE task_events SET created_at = 1 WHERE task_id = ?", (a,))
        kb.gc_events(conn)
        kinds = [k for (k,) in conn.execute("SELECT kind FROM task_events WHERE task_id = ?", (a,))]
        with pytest.raises(ksa.OverrideRefused, match=f"the gap card {fix} for operator_admit is done"):
            ksa.override(conn, _card(conn), "operator_admit", "render toolchain is Studio-only",
                         elsewhere=EW, why_not_now=WN, effort="days")
    assert kinds == ["studio_override"]  # every other old event on the done card was pruned


def test_refusal_create_command_names_the_board(operator, kanban_identity):
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


# -- operator read 2026-11-27 (9bb399f7) ---------------------------------------------------------------------------

def test_unrelated_open_card_is_not_a_fix_card_and_does_not_open_the_key(operator):
    """Read 3: any open card used to be accepted as --fix-card and then counted every later run for the key."""
    with kb.connect_closing() as conn:
        unrelated = _card(conn, title="tidy the README", body="nothing to do with the Studio")
        with pytest.raises(ksa.OverrideRefused, match=f"--fix-card {unrelated} is not a gap card for operator_admit"):
            ksa.override(conn, _card(conn), "operator_admit", "render toolchain is Studio-only",
                         elsewhere=EW, why_not_now=WN, effort="hours", fix_card=unrelated)
        # it did not open the key: a later no-answer override is still refused
        with pytest.raises(ksa.OverrideRefused, match="--elsewhere is required"):
            ksa.override(conn, _card(conn), "operator_admit", "render toolchain is Studio-only")
        # a gap card for ANOTHER key does not serve this one either
        other = _fix(conn, keys=("workspace_kind",))
        with pytest.raises(ksa.OverrideRefused, match="is not a gap card for operator_admit"):
            ksa.override(conn, _card(conn), "operator_admit", "render toolchain is Studio-only",
                         elsewhere=EW, why_not_now=WN, effort="hours", fix_card=other)
        # the idempotency key alone is enough (the printed create command writes it)
        idem = kb.create_task(conn, title="gap", assignee="alpha", body="x", idempotency_key="studio-capgap:operator_admit")
        p = ksa.override(conn, _card(conn), "operator_admit", "render toolchain is Studio-only",
                         elsewhere=EW, why_not_now=WN, effort="hours", fix_card=idem)
    assert p["fix_card"] == idem


def test_registration_of_an_unrelated_card_recorded_earlier_is_ignored(operator):
    """Read 3: an event written by the old gate naming an unrelated card never opens the key."""
    with kb.connect_closing() as conn:
        unrelated, work = _card(conn, title="tidy the README"), _card(conn)
        with kb.write_txn(conn):
            kb._append_event(conn, work, ksa.OVERRIDE_EVENT, {"capability": "operator_admit", "fix_card_given": unrelated})
        assert ksg.key_fix_cards(conn, "operator_admit") == {}
        with pytest.raises(ksa.OverrideRefused, match="--elsewhere is required"):
            ksa.override(conn, _card(conn), "operator_admit", "render toolchain is Studio-only")


def _register_then_close(conn, old, completed_at):
    ksa.override(conn, _card(conn), "operator_admit", "render toolchain is Studio-only",
                 elsewhere=EW, why_not_now=WN, effort="hours", fix_card=old)
    with kb.write_txn(conn):
        conn.execute("UPDATE task_events SET created_at = ? WHERE kind = ?", (completed_at - 100, ksa.OVERRIDE_EVENT))
        conn.execute("UPDATE tasks SET status='done', completed_at=? WHERE id=?", (completed_at, old))


def test_comment_on_an_old_done_fix_card_does_not_close_a_newer_open_one(operator):
    """Read 4: closure is tasks.completed_at, not the done card's newest event."""
    import time as _t
    with kb.connect_closing() as conn:
        old = _fix(conn, title="install A")
        _register_then_close(conn, old, int(_t.time()) - 1000)
        new = _fix(conn, title="install A again")
        ksa.override(conn, _card(conn), "operator_admit", "render toolchain is Studio-only",
                     elsewhere=EW, why_not_now=WN, effort="hours", fix_card=new)
        kb.add_comment(conn, old, "someone", "a late note on the old done card")
        with kb.write_txn(conn):
            kb._append_event(conn, old, "commented", {})
        q = ksa.override(conn, _card(conn), "operator_admit", "render toolchain is Studio-only")
    assert q["counted"] and q["fix_card"] == new


def test_done_fix_card_events_deleted_keeps_the_closure_date(operator):
    """Read 4: gc pruning the done card's own events must not move its closure."""
    import time as _t
    with kb.connect_closing() as conn:
        old = _fix(conn, title="install A")
        _register_then_close(conn, old, int(_t.time()) - 1000)
        new = _fix(conn, title="install A again")
        ksa.override(conn, _card(conn), "operator_admit", "render toolchain is Studio-only",
                     elsewhere=EW, why_not_now=WN, effort="hours", fix_card=new)
        with kb.write_txn(conn):
            conn.execute("DELETE FROM task_events WHERE task_id = ?", (old,))
        q = ksa.override(conn, _card(conn), "operator_admit", "render toolchain is Studio-only")
    assert q["counted"] and q["fix_card"] == new


@pytest.mark.parametrize("bad", [b"[1, 2, 3]\n", b"\xff\xfe not utf-8 \x80\n", b'{"at": "soon"}\n', b"null\n"],
                         ids=["json-list", "invalid-utf8", "bad-at", "null"])
def test_bad_refusal_log_line_still_prints_the_refusal(operator, bad):
    """Read 5: one bad line in gap-refusals.jsonl must not turn every later refusal into a traceback."""
    path = ksg.refusals_path(operator)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bad)
    with kb.connect_closing() as conn:
        with pytest.raises(ksa.OverrideRefused, match="--elsewhere is required") as e:
            ksa.override(conn, _card(conn), "operator_admit", "render toolchain is Studio-only")
    assert "Pool hosts:" in str(e.value)


def test_refusal_log_is_rotated_to_the_repeat_window(operator, monkeypatch):
    """Ruling 9: the log is read on every refusal, so past REFUSALS_MAX_BYTES it keeps only the last 24 h."""
    monkeypatch.setattr(ksg, "REFUSALS_MAX_BYTES", 1000)
    path = ksg.refusals_path(operator)
    path.parent.mkdir(parents=True, exist_ok=True)
    old = json.dumps({"at": 1, "key": "k", "problem": "p", "task_id": "t_old"})
    path.write_text("\n".join([old] * 50) + "\n", encoding="utf-8")
    with kb.connect_closing() as conn:
        with pytest.raises(ksa.OverrideRefused):
            ksa.override(conn, _card(conn), "operator_admit", "render toolchain is Studio-only")
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["key"] == "operator_admit"


def test_create_command_is_shell_quoted_and_writes_both_markers():
    """Ruling 9: the capability is shell-quoted; the card it creates passes is_fix_card_for."""
    import shlex
    cmd = ksg.create_command("ssh-root:x;rm -rf ~", "media")
    argv = shlex.split(cmd)
    assert argv[:5] == ["hermes", "kanban", "--board", "media", "create"]
    assert argv[argv.index("--idempotency-key") + 1] == "studio-capgap:ssh-root:x;rm -rf ~"
    assert argv[argv.index("--body") + 1].startswith("capability-gap: ssh-root:x;rm")


def test_reopen_after_closure_prints_a_dated_key_that_is_accepted_end_to_end(operator):
    """Ruling 7: after a closure the same idempotency key returns the DONE card, so the refusal prints a dated key;
    a card created from that command is accepted as the new fix card, and later runs count on it."""
    import shlex
    with kb.connect_closing() as conn:
        old = kb.create_task(conn, title="gap", assignee="alpha", body="x",
                             idempotency_key="studio-capgap:operator_admit")
        ksa.override(conn, _card(conn), "operator_admit", "render toolchain is Studio-only",
                     elsewhere=EW, why_not_now=WN, effort="hours", fix_card=old)
        _done(conn, old)
        with pytest.raises(ksa.OverrideRefused) as e:
            ksa.override(conn, _card(conn), "operator_admit", "render toolchain is Studio-only")
        msg = str(e.value)
        cmd = msg[msg.index("hermes kanban "):]
        argv = shlex.split(cmd)
        idem = argv[argv.index("--idempotency-key") + 1]
        assert idem != "studio-capgap:operator_admit" and idem.startswith("studio-capgap:operator_admit:")
        # the same key returns the done card; the dated one makes a new card
        assert kb.create_task(conn, title="g", assignee="alpha", idempotency_key="studio-capgap:operator_admit") == old
        new = kb.create_task(conn, title=argv[argv.index("create") + 1], assignee="alpha",
                             body=argv[argv.index("--body") + 1], idempotency_key=idem)
        assert new != old
        p = ksa.override(conn, _card(conn), "operator_admit", "render toolchain is Studio-only",
                         elsewhere=EW, why_not_now=WN, effort="hours", fix_card=new)
        q = ksa.override(conn, _card(conn), "operator_admit", "render toolchain is Studio-only")
    assert p["fix_card"] == new and q["counted"] and q["fix_card"] == new


def test_studio_state_refusal_names_the_studio_host_route(operator):
    """Ruling 6: work ON the Studio machine itself has its own inherent word, studio-host."""
    with kb.connect_closing() as conn:
        with pytest.raises(ksa.OverrideRefused) as e:
            ksa.override(conn, _card(conn), "studio-state", "restart the Studio docker stack")
        assert "use requires: studio-host" in str(e.value)
        with pytest.raises(ksa.OverrideRefused) as f:
            ksa.override(conn, _card(conn), "operator_admit", "render toolchain is Studio-only")
    assert "studio-host" not in str(f.value)
    assert ksg.classify("studio-host", studio_only=frozenset({"studio-host", "keychain"})) == ksg.INHERENT


# -- 5. kill switch -----------------------------------------------------------------------------------------------

def test_kill_switch_file_restores_todays_behaviour(operator):
    path = ksg.kill_path(operator)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("off\n", encoding="utf-8")
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
    seq = json.loads(FIXTURE.read_text(encoding="utf-8-sig"))
    assert len(seq) == 17
    with kb.connect_closing() as conn:
        card = {orig: _card(conn) for orig in dict.fromkeys(t for t, _, _ in seq)}  # 11 cards
        seq = [(card[t], cap, why) for t, cap, why in seq]
        first_tid, first_cap, first_why = seq[0]
        # as it was run on 11-26 (no answers): refused, with the gap-card command
        with pytest.raises(ksa.OverrideRefused) as e:
            ksa.override(conn, first_tid, first_cap, first_why, until="+8h")
        assert "hermes kanban --board default create" in str(e.value) and "--fix-card" in str(e.value)
        fix = _fix(conn, title="install the explainer render toolchain on ci-box",
                   keys=("operator_admit", "workspace_kind"))
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
    assert seen == {"operator_admit", "workspace_kind"}
    # one count line per gap key per day; together they total the 17 runs
    totals = sorted(int(c.split(" Studio run(s)")[0].rsplit(" ", 1)[1]) for c in counts)
    assert len(counts) == 2 and sum(totals) == 17
