"""Dispatch evidence bundle (t_978ec761): a card's named local files and source-message attachments reach a pool
worker through the card attachment store; secrets are refused with a manifest line; off means off."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_evidence_bundle as eb


@pytest.fixture(autouse=True)
def _reset_session_id_contextvar():
    from gateway.session_context import _SESSION_ID, _UNSET
    _SESSION_ID.set(_UNSET)
    yield
    _SESSION_ID.set(_UNSET)


@pytest.fixture
def board(monkeypatch, tmp_path):
    """A board under a temp HERMES_HOME, a fake operator home with files, and the policy file (off)."""
    vhome = tmp_path / ".hermes"
    vhome.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(vhome))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    policy = tmp_path / "landing-policy.json"
    policy.write_text(json.dumps({"dispatch_evidence_bundle": "off"}), encoding="utf-8")
    monkeypatch.setenv("KANBAN_LANDING_POLICY", str(policy))
    monkeypatch.setenv(eb.KILL_FILE_ENV, str(tmp_path / "kill.disabled"))
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    files = vhome / "var" / "oneshot"
    files.mkdir(parents=True)
    spec = files / "M1-result.md"
    spec.write_text("# spec\nthe worker needs this text\n", encoding="utf-8")
    return {"home": tmp_path, "vhome": vhome, "policy": policy, "spec": spec, "kb": kb, "kbc": kbc}


def _set_policy(board, value):
    board["policy"].write_text(json.dumps({"dispatch_evidence_bundle": value}), encoding="utf-8")


def _card(board, body, assignee="test-worker"):
    conn = board["kbc"].connect()
    try:
        tid = board["kb"].create_task(conn, title="card", body=body, assignee=assignee)
        return tid, board["kb"].get_task(conn, tid)
    finally:
        conn.close()


def _attachments(board, tid):
    conn = board["kbc"].connect()
    try:
        return {a.filename: a for a in board["kb"].list_attachments(conn, tid)}
    finally:
        conn.close()


def _manifest(board, tid):
    att = _attachments(board, tid)[eb.MANIFEST_NAME]
    return json.loads(Path(att.stored_path).read_bytes())


def _attach(board, task, **kw):
    conn = board["kbc"].connect()
    try:
        return eb.maybe_attach(conn, task, host="ace-ai", **kw)
    finally:
        conn.close()


def test_named_local_file_is_bundled_with_bytes_and_hash(board):
    _set_policy(board, "on")
    spec = board["spec"]
    tid, task = _card(board, f"Read {spec} first.\nAlso ~/.hermes/var/oneshot/M1-result.md (same file).")
    manifest = _attach(board, task)
    assert manifest is not None
    atts = _attachments(board, tid)
    item = next(i for i in manifest["items"] if i["status"] == "bundled")
    assert item["sha256"] == hashlib.sha256(spec.read_bytes()).hexdigest()
    assert Path(atts[item["attachment"]].stored_path).read_bytes() == spec.read_bytes()
    stored = _manifest(board, tid)
    assert stored["items"][0]["attachment_id"] == atts[item["attachment"]].id
    # The ~/ spelling names the same file: its own manifest line, pointing at the one stored copy.
    assert [i["path"] for i in stored["items"]] == [str(spec), "~/.hermes/var/oneshot/M1-result.md"]
    dup = stored["items"][1]
    assert dup["duplicate_of"] == str(spec) and dup["attachment_id"] == stored["items"][0]["attachment_id"]
    assert sorted(atts) == sorted([eb.MANIFEST_NAME, item["attachment"]])


def test_root_relative_studio_path_is_bundled_and_custody_path_refused(board):
    """Cards name Studio files as ``var/oneshot/x.md`` (t_e432fde8); a token that is no file there is no item."""
    _set_policy(board, "on")
    key = board["vhome"] / "state" / "github-app" / "private-key.pem"
    key.parent.mkdir(parents=True)
    key.write_text("x", encoding="utf-8")
    body = ("spec at var/oneshot/M1-result.md on the Studio; key state/github-app/private-key.pem; "
            "see plans/ci-99-program/ and hermes-agent/var/other.md and state/missing.jsonl")
    tid, task = _card(board, body)
    conn = board["kbc"].connect()
    try:
        manifest = eb.attach_bundle(conn, task, host="ace-ai", hermes_root=board["vhome"],
                                    message_lookup=lambda *a: None)
    finally:
        conn.close()
    items = {i["path"]: i for i in manifest["items"]}
    assert set(items) == {"var/oneshot/M1-result.md", "state/github-app/private-key.pem"}
    assert items["var/oneshot/M1-result.md"]["status"] == "bundled"
    assert items["state/github-app/private-key.pem"]["status"] == "refused"


def test_secret_like_file_is_refused_with_a_manifest_line(board):
    _set_policy(board, "on")
    env = board["vhome"] / ".env"
    env.write_text("DISCORD_BOT_TOKEN=abc\n", encoding="utf-8")
    pem = board["vhome"] / "notes.txt"
    begin, end = "-----BEGIN " + "OPENSSH PRIVATE KEY-----", "-----END " + "OPENSSH PRIVATE KEY-----"
    pem.write_text(f"{begin}\nfake-test-key-1234567890\n{end}\n", encoding="utf-8")  # split: no gitleaks hit
    leak = board["vhome"] / "var" / "log.txt"
    leak.write_text("export OPENAI_API_KEY=sk-proj-test-1234567890-fake-fixture-value\n", encoding="utf-8")
    custody = board["vhome"] / "state" / "github-app" / "app.pem"
    custody.parent.mkdir(parents=True)
    custody.write_text("x", encoding="utf-8")
    tid, task = _card(board, f"see {env} {pem} {leak} {custody} and /home/someone-else/notes.md")
    _attach(board, task)
    items = {i["path"]: i for i in _manifest(board, tid)["items"]}
    assert all(i["status"] == "refused" for i in items.values()), items
    assert "credential" in items[str(env)]["reason"]
    assert items[str(pem)]["reason"] == "PEM private key in the content"
    assert items[str(leak)]["reason"] == "secret scanner match"
    assert "pem" in items[str(custody)]["reason"]
    assert items["/home/someone-else/notes.md"]["reason"] == "outside the home tree"
    assert [n for n in _attachments(board, tid) if n != eb.MANIFEST_NAME] == []


def test_secret_after_a_bad_utf8_byte_is_still_scanned(board):
    """Prism 7aefee143055: one invalid UTF-8 byte must not let a file through unscanned."""
    _set_policy(board, "on")
    log = board["vhome"] / "var" / "report.log"
    log.write_bytes(b"export OPENAI_API_KEY=sk-proj-test-1234567890-fake-fixture-value\n\xff")
    tid, task = _card(board, f"log {log}")
    _attach(board, task)
    (item,) = _manifest(board, tid)["items"]
    assert (item["status"], item["reason"], item["content_scan"]) == ("refused", "secret scanner match", "lossy")


def test_agent_read_denylist_credential_stores_are_refused(board):
    """Prism 5af21df77b15: files the agent's own read guard denies (bws cache, google oauth) are never shipped,
    even when their JSON passes the content scanner."""
    _set_policy(board, "on")
    bws = board["vhome"] / "cache" / "bws_cache.json"
    oauth = board["vhome"] / "auth" / "google_oauth.json"
    for p in (bws, oauth):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"secrets": {"SERVICE_LOGIN": "correct horse battery staple"}, "fetched_at": 1}',
                     encoding="utf-8")
    from agent.redact import redact_sensitive_text
    plain = oauth.read_text(encoding="utf-8")
    assert redact_sensitive_text(plain, force=True) == plain  # the scanner alone would pass it
    tid, task = _card(board, f"see {bws} and {oauth}")
    _attach(board, task)
    items = {i["path"]: i for i in _manifest(board, tid)["items"]}
    assert {i["status"] for i in items.values()} == {"refused"}, items
    assert items[str(oauth)]["reason"] == "credential store (agent read denylist)"
    assert list(_attachments(board, tid)) == [eb.MANIFEST_NAME]


def test_over_the_cap_is_listed_by_name_and_hash_only(board, monkeypatch):
    _set_policy(board, "on")
    monkeypatch.setattr(eb, "CAP_BYTES", 64)
    big = board["vhome"] / "cache" / "big.log"
    big.parent.mkdir(parents=True)
    big.write_bytes(b"y" * 100)
    tid, task = _card(board, f"log at {big}")
    _attach(board, task)
    (item,) = _manifest(board, tid)["items"]
    assert item["status"] == "listed" and "cap" in item["reason"]
    assert item["sha256"] == hashlib.sha256(b"y" * 100).hexdigest() and item["size"] == 100
    assert list(_attachments(board, tid)) == [eb.MANIFEST_NAME]


def test_source_message_attachments_are_bundled(board):
    _set_policy(board, "on")
    img = board["vhome"] / "cache" / "images" / "img_abc.png"
    img.parent.mkdir(parents=True)
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(256)))
    body = ("origin: discord Daemonarchy / #fix-sdlc (1561038985004974203) · owner go Discord msg "
            "1571908845658177596 (07:24 PT)\nfix it")
    seen = []

    def lookup(session_id, message_id):
        seen.append((session_id, message_id))
        return f"[If you need a closer look, use vision_analyze with image_url: {img}]"

    tid, task = _card(board, body)
    task.session_id = "20261017_083333_13cf5395"
    conn = board["kbc"].connect()
    try:
        manifest = eb.attach_bundle(conn, task, host="ace-ai", message_lookup=lookup)
    finally:
        conn.close()
    assert seen == [("20261017_083333_13cf5395", "1571908845658177596")]
    src = manifest["source"]
    assert (src["platform"], src["channel_id"], src["message_id"], src["message_found"]) == (
        "discord", "1561038985004974203", "1571908845658177596", True)
    assert src["permalink"] is None and "guild" in src["permalink_note"]
    (item,) = manifest["items"]
    assert item["origin"] == "message" and item["status"] == "bundled" and item["content_scan"] == "lossy"


def test_discord_permalink_in_body_is_carried():
    ref = eb.source_ref("from https://discord.com/channels/111111111111111111/222222222222222222/333333333333333333")
    assert ref["permalink"].endswith("/333333333333333333") and ref["message_id"] == "333333333333333333"


def test_toggle_off_bundles_nothing_and_reads_nothing(board, monkeypatch):
    reads = []

    def boom(*a, **k):  # maybe_attach swallows exceptions, so record the call instead of raising
        reads.append(a)
        raise AssertionError("no reads while the toggle is off")

    monkeypatch.setattr(eb, "build_bundle", boom)
    monkeypatch.setattr(eb, "state_db_lookup", boom)
    monkeypatch.setattr(eb, "_hash_file", boom)
    tid, task = _card(board, f"Read {board['spec']}")
    for value in ("off", "true", None):
        if value is None:
            board["policy"].write_text("{}", encoding="utf-8")
        else:
            _set_policy(board, value)
        assert _attach(board, task) is None
    _set_policy(board, "on")
    (board["home"] / "kill.disabled").write_text("", encoding="utf-8")
    assert _attach(board, task) is None
    assert _attachments(board, tid) == {}
    assert reads == []


def test_local_spawn_and_second_dispatch_do_not_bundle(board):
    _set_policy(board, "on")
    tid, task = _card(board, f"Read {board['spec']}")
    conn = board["kbc"].connect()
    try:
        assert eb.maybe_attach(conn, task, host=None) is None  # Studio-local worker reads the file itself
    finally:
        conn.close()
    assert _attachments(board, tid) == {}
    assert _attach(board, task) is not None
    before = set(_attachments(board, tid))
    assert _attach(board, task) is None  # one bundle per card
    assert set(_attachments(board, tid)) == before


def test_pool_worker_reads_the_bundle_through_attachment_read(board, monkeypatch):
    """The read path a pool worker uses (kanban_attachment_read over the board connection) returns the bytes."""
    _set_policy(board, "on")
    tid, task = _card(board, f"Read {board['spec']}")
    _attach(board, task)
    conn = board["kbc"].connect()
    try:
        board["kb"].claim_task(conn, tid)
        run_id = board["kb"]._current_run_id(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    from tools import kanban_tools as kt
    listed = json.loads(kt._handle_attachments({"task_id": tid}))
    by_name = {a["filename"]: a["id"] for a in listed["attachments"]}
    man = json.loads(kt._handle_attachment_read({"attachment_id": by_name[eb.MANIFEST_NAME]}))
    assert man["ok"] is True, man
    item = json.loads(man["text"])["items"][0]
    got = json.loads(kt._handle_attachment_read({"attachment_id": item["attachment_id"], "encoding": "base64"}))
    assert base64.b64decode(got["content_base64"]) == board["spec"].read_bytes()
    assert got["sha256"] == item["sha256"]


def test_dispatch_launch_calls_the_hook_only_for_pool_hosts():
    """The hook sits on the pool-host branch of _launch_claimed, next to the local-start booking."""
    import inspect
    from hermes_cli import kanban_db_dispatch as kd
    src = inspect.getsource(kd._launch_claimed)
    local = src.index("if host is None:")
    hook = src.index("maybe_attach(conn, claimed, host=host, board=board)")
    spawn = src.index("_priority_spawn(conn, claimed")
    assert local < hook < spawn
