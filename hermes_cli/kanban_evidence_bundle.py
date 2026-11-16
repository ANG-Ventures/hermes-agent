"""Dispatch evidence bundle (t_978ec761): carry what a card was written from to a worker on a pool host.

A worker placed off the Studio cannot open the chat attachments of the message the card was minted from, nor a
Studio-only file the body names by path. When the toggle is on, the dispatcher (on the Studio, before it spawns a
worker on a pool host) copies those bytes into the card's attachment store, which pool workers already read with
``kanban_attachment_read``:

* the source message: platform, channel and message id parsed from the body, a permalink when the body carries
  one, and every home-tree file path the message's stored text names (the gateway writes cached chat attachments
  into that text as ``.../.hermes/cache/{images,documents,audio,...}/...``);
* each file the body names by absolute path (``/Users/<u>/...``, ``/home/<u>/...`` or ``~/...``) under the home
  tree, and each ``var/``, ``cache/``, ``state/`` or ``plans/`` path that is a file under the hermes root (cards
  name Studio files that way more often than by absolute path);
* ``evidence-manifest.json``: one line per item with its sha256, size and status (``bundled`` / ``listed``: over
  the cap, name + sha256 only / ``refused``: with the reason). Nothing is skipped silently.

Never bundled (``refused``): ``.env`` files, key material by name or by a PEM private-key header, credential
directories, the trust-root names the Prism key custody scanner lists (hermes-home
``scripts/lib/prism_custody.py`` ``TRUST_ROOT_PATTERNS`` / ``TRUST_ROOT_PATH_PATTERNS``), and any text the secret
scanner (:func:`agent.redact.redact_sensitive_text`, forced) would change.

Toggle, read on every call: ``<hermes root>/config/landing-policy.json`` ``"dispatch_evidence_bundle": "on"``
(missing or anything else = off), and the kill file ``<hermes root>/state/dispatch-evidence-bundle.disabled``
wins over ``on``. Off = no reads beyond the policy file. One bundle per card: a card that already holds
``evidence-manifest.json`` is left alone. Best effort: a failure is logged and never stops the dispatch.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Callable, Optional

_log = logging.getLogger(__name__)

POLICY_KEY = "dispatch_evidence_bundle"
KILL_FILE_REL = "state/dispatch-evidence-bundle.disabled"
KILL_FILE_ENV = "KANBAN_EVIDENCE_KILL_FILE"  # tests and drills
MANIFEST_NAME = "evidence-manifest.json"
ITEM_PREFIX = "evidence-"
CAP_BYTES = 10 * 1024 * 1024  # total bytes bundled per card; past it an item is listed by name + sha256
HASH_MAX_BYTES = 256 * 1024 * 1024  # an over-cap item larger than this is listed without a sha256
EVENT_KIND = "evidence_bundle"

# An absolute home-tree path in prose. Trailing sentence punctuation is trimmed after the match.
_PATH_RE = re.compile(r"(?<![\w/.~-])((?:~|/Users/[^/\s]+|/home/[^/\s]+)/[^\s'\"`<>|\]\[(){}]+)")
_TRAIL = ".,;:!?'\")]}>`"
# A hermes-root-relative Studio path (``var/oneshot/x.md``), not part of a longer path or URL.
_REL_RE = re.compile(r"(?<![\w/.~-])((?:var|cache|state|plans)/[^\s'\"`<>|\]\[(){}]+)")
_MSG_RE = re.compile(r"\b(?:msg|message)(?: id)?[ :#]*`?(\d{15,21})\b", re.I)
_CHAN_RE = re.compile(r"\((\d{15,21})\)")
_DISCORD_URL_RE = re.compile(r"https://(?:ptb\.|canary\.)?discord(?:app)?\.com/channels/(\d+|@me)/(\d+)/(\d+)")

# Names that are key or credential material wherever they sit (lowercased basename, fnmatch).
_SECRET_NAMES = (
    ".env", ".env.*", "*.env", "*.pem", "*.pem.*", "*.key", "*.key.*", "*.p12", "*.pfx", "*.jks", "*.keystore",
    "*.gpg", "*.kdbx", "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*", ".netrc", ".npmrc", ".pypirc",
    "auth.json", "credentials", "credentials.json", "*.secret", "*.secrets", "secrets.*", "*token*.json",
    "*.ovpn", "*.mobileconfig", "bws_cache*", ".pgpass", ".git-credentials",
)
# Prism key custody (hermes-home scripts/lib/prism_custody.py, t_3d87f772): renamed or backed-up trust roots.
_CUSTODY_NAMES = ("*priv*.pem*", "*private-key*", "*priv*.key*", "*app-key*", "token-cache*", "token_cache*",
                  "tokencache*", *(f"*{t}*.{x}" for t in ("state", "github-app", "backup", "prism")
                                   for x in ("tar*", "tgz", "zip")))
_CUSTODY_PATHS = ("*github-app/*.pem*", "*github-app/*.key*")
# Directories whose contents are credentials (any path component, lowercased).
_SECRET_DIRS = {".ssh", ".gnupg", ".aws", ".kube", ".docker", "secrets", "credentials", "keychains", ".password-store"}
_PEM_PRIVATE = re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY( BLOCK)?-----")


def _hermes_root() -> Optional[Path]:
    try:
        from hermes_constants import get_default_hermes_root
        return Path(get_default_hermes_root())
    except Exception:  # noqa: BLE001 -- off
        return None


def kill_file() -> Optional[Path]:
    override = os.environ.get(KILL_FILE_ENV)
    if override:
        return Path(override)
    root = _hermes_root()
    return None if root is None else root / KILL_FILE_REL


def enabled() -> bool:
    """``landing-policy.json`` says ``on`` and the kill file is absent. Read per call; anything else is off."""
    from hermes_cli import kanban_db as kb
    if kb.landing_policy_knob(POLICY_KEY) != "on":
        return False
    kf = kill_file()
    return not (kf is not None and kf.exists())


def named_paths(text: str, home: Optional[Path] = None) -> list[str]:
    """Absolute home-tree paths ``text`` names (``/Users/<u>/``, ``/home/<u>/``, ``~/`` or ``home``'s own prefix),
    in order, deduplicated (as written, trailing punctuation off)."""
    out: list[str] = []
    rx = _PATH_RE
    if home is not None and not re.match(r"/(?:Users|home)/[^/]+$", str(home)):
        rx = re.compile(_PATH_RE.pattern.replace("(?:~|", f"(?:~|{re.escape(str(home))}|", 1))
    for m in rx.finditer(text or ""):
        p = m.group(1).rstrip(_TRAIL)
        if p and p not in out and p not in ("~", "~/"):
            out.append(p)
    return out


def source_ref(body: str) -> dict:
    """Platform / channel / message id and a permalink, as far as the card body states them."""
    body = body or ""
    ref: dict = {"platform": None, "channel_id": None, "message_id": None, "permalink": None}
    url = _DISCORD_URL_RE.search(body)
    if url:
        ref.update(platform="discord", channel_id=url.group(2), message_id=url.group(3), permalink=url.group(0))
        return ref
    origin = next((ln for ln in body.splitlines() if ln.strip().lower().startswith("origin:")), "")
    if "discord" in origin.lower():
        ref["platform"] = "discord"
    chan = _CHAN_RE.search(origin)
    if chan:
        ref["channel_id"] = chan.group(1)
    msg = _MSG_RE.search(origin) or _MSG_RE.search(body)
    if msg:
        ref["message_id"] = msg.group(1)
    if ref["message_id"]:
        ref["permalink_note"] = "unresolved: the card names no guild id, so no discord.com link can be built"
    return ref


def state_db_lookup(session_id: str, message_id: str) -> Optional[str]:
    """Stored text of the user message ``message_id`` in ``session_id`` (root state.db, then each profile's), read
    only. None when no DB has it."""
    root = _hermes_root()
    if root is None:
        return None
    for db in [root / "state.db", *sorted(root.glob("profiles/*/state.db"))]:
        if not db.is_file():
            continue
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            try:
                row = conn.execute(
                    "SELECT content FROM messages WHERE session_id = ? AND platform_message_id = ? "
                    "AND role = 'user' ORDER BY id LIMIT 1", (session_id, message_id)).fetchone()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            _log.debug("evidence bundle: %s unreadable: %s", db, exc)
            continue
        if row is not None:
            return row[0] or ""
    return None


def _name_refusal(path: Path) -> Optional[str]:
    name = path.name.lower()
    for pat in _SECRET_NAMES:
        if fnmatch.fnmatch(name, pat):
            return f"key or credential file by name ({pat})"
    for pat in _CUSTODY_NAMES:
        if fnmatch.fnmatch(name, pat):
            return f"custody scanner trust-root name ({pat})"
    low = str(path).lower()
    for pat in _CUSTODY_PATHS:
        if fnmatch.fnmatch(low, pat):
            return f"custody scanner trust-root path ({pat})"
    hit = _SECRET_DIRS.intersection(part.lower() for part in path.parts[:-1])
    if hit:
        return f"inside a credential directory ({sorted(hit)[0]})"
    return None


def _content_refusal(data: bytes) -> tuple[Optional[str], str]:
    """``(refusal or None, scan)``. Every item is scanned: bytes that are not clean UTF-8 (a log with one damaged
    byte, a binary) are decoded with replacement and the recoverable text is scanned (``scan`` = ``lossy``), so a
    decode error never passes a file unscanned (Prism 7aefee143055)."""
    if _PEM_PRIVATE.search(data):
        return "PEM private key in the content", "pem"
    try:
        text, scan = data.decode("utf-8"), "text"
    except UnicodeDecodeError:
        text, scan = data.decode("utf-8", errors="replace"), "lossy"
    from agent.redact import redact_sensitive_text
    if redact_sensitive_text(text, force=True) != text:
        return "secret scanner match", scan
    return None, scan


def _read_guard_refusal(path: Path) -> Optional[str]:
    """The agent's canonical read denylist (``agent/file_safety.get_read_block_error``: credential stores such as
    ``cache/bws_cache.json``, ``auth.json``, ``mcp-tokens/``, ``vault/``, ``browser-profile/``, ``.env``), so this
    bundle never ships a file the agent itself may not read (Prism 5af21df77b15)."""
    from agent.file_safety import get_read_block_error
    if get_read_block_error(str(path)):
        return "credential store (agent read denylist)"
    return None


def _hash_file(path: Path) -> tuple[int, str]:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
            size += len(block)
    return size, h.hexdigest()


def root_relative_files(text: str, root: Optional[Path]) -> list[str]:
    """``var/...``, ``cache/...``, ``state/...``, ``plans/...`` tokens in ``text`` that are FILES under the hermes
    root (cards name Studio files this way: ``var/oneshot/x.md on the Studio``). A token that is no file there is
    not a local-file reference and is not an item."""
    if root is None:
        return []
    out: list[str] = []
    for m in _REL_RE.finditer(text or ""):
        rel = m.group(1).rstrip(_TRAIL)
        if rel not in out and (root / rel).is_file():
            out.append(rel)
    return out


def build_bundle(body: str, *, session_id: Optional[str], home: Path,
                 message_lookup: Callable[[str, str], Optional[str]] = state_db_lookup,
                 cap_bytes: Optional[int] = None, hermes_root: Optional[Path] = None) -> tuple[dict, list]:
    """``(manifest, [(attachment name, bytes)])`` for a card body. Pure apart from reading the named files and
    the message lookup; stores nothing."""
    cap_bytes = CAP_BYTES if cap_bytes is None else cap_bytes
    ref = source_ref(body)
    message_text = None
    if ref.get("message_id") and session_id:
        message_text = message_lookup(session_id, ref["message_id"])
    ref["session_id"] = session_id
    ref["message_found"] = message_text is not None
    candidates = [("message", p, None) for p in named_paths(message_text or "", home)]
    candidates += [("body", p, None) for p in named_paths(body, home) if p not in {c[1] for c in candidates}]
    candidates += [("body", rel, hermes_root / rel) for rel in root_relative_files(body, hermes_root)]
    home_real = Path(os.path.realpath(home))
    items: list[dict] = []
    blobs: list[tuple[str, bytes]] = []
    total = 0
    seen: dict[Path, dict] = {}
    for origin, written, resolved in candidates:
        item: dict = {"origin": origin, "path": written}
        items.append(item)
        expanded = resolved or (home / written[2:] if written.startswith("~/") else Path(written))
        real = Path(os.path.realpath(expanded))
        if not (real == home_real or home_real in real.parents):
            item.update(status="refused", reason="outside the home tree")
            continue
        first = seen.get(real)
        if first is not None:  # the same file under another spelling: point at the first item's result
            item.update({k: v for k, v in first.items() if k not in ("origin", "path")}, duplicate_of=first["path"])
            continue
        seen[real] = item
        refusal = (_name_refusal(expanded) or (_name_refusal(real) if real != expanded else None)
                   or _read_guard_refusal(expanded) or (_read_guard_refusal(real) if real != expanded else None))
        if refusal:
            item.update(status="refused", reason=refusal)
            continue
        if not real.exists():
            item.update(status="refused", reason="not found on the dispatch host")
            continue
        if not real.is_file():
            item.update(status="refused", reason="not a regular file")
            continue
        try:
            size = real.stat().st_size
            if total + size > cap_bytes:
                item.update(size=size, status="listed",
                            reason=f"over the {cap_bytes // (1024 * 1024)} MB bundle cap; name and sha256 only")
                if size <= HASH_MAX_BYTES:
                    item["sha256"] = _hash_file(real)[1]
                else:  # hashing a multi-GB store would stall the dispatch tick
                    item.update(sha256=None, reason=item["reason"] + f" (no sha256: over "
                                f"{HASH_MAX_BYTES // (1024 * 1024)} MB, not hashed at dispatch)")
                continue
            data = real.read_bytes()
        except OSError as exc:
            item.update(status="refused", reason=f"unreadable: {exc.__class__.__name__}")
            continue
        size = len(data)
        item.update(size=size, sha256=hashlib.sha256(data).hexdigest())
        if total + size > cap_bytes:  # grew between stat and read
            item.update(status="listed", reason=f"over the {cap_bytes // (1024 * 1024)} MB bundle cap; "
                                                 "name and sha256 only")
            continue
        refusal, scan = _content_refusal(data)
        item["content_scan"] = scan
        if refusal:
            item.update(status="refused", reason=refusal)
            continue
        name = f"{ITEM_PREFIX}{len(blobs) + 1:02d}-{real.name}"
        item.update(status="bundled", attachment=name)
        blobs.append((name, data))
        total += size
    manifest = {
        "v": 1, "generated_at": int(time.time()), "cap_bytes": cap_bytes, "bundled_bytes": total,
        "source": ref, "items": items,
        "counts": {s: sum(1 for i in items if i.get("status") == s) for s in ("bundled", "listed", "refused")},
        "read_with": "kanban_attachments, then kanban_attachment_read(attachment_id=...) (64 KiB per read; "
                     "offset/length for larger items)",
    }
    return manifest, blobs


def has_bundle(conn: sqlite3.Connection, task_id: str) -> bool:
    return conn.execute("SELECT 1 FROM task_attachments WHERE task_id = ? AND filename = ? LIMIT 1",
                        (task_id, MANIFEST_NAME)).fetchone() is not None


def attach_bundle(conn: sqlite3.Connection, task, *, host: Optional[str] = None, board: Optional[str] = None,
                  home: Optional[Path] = None, hermes_root: Optional[Path] = None,
                  message_lookup: Callable[[str, str], Optional[str]] = state_db_lookup) -> Optional[dict]:
    """Build and store the bundle for ``task`` (a ``Task``). Returns the manifest, or None when the card already
    has one. Raises on a store failure; :func:`maybe_attach` is the guarded entry point."""
    from hermes_cli import kanban_db as kb
    if has_bundle(conn, task.id):
        return None
    manifest, blobs = build_bundle(task.body or "", session_id=getattr(task, "session_id", None),
                                   home=home or Path.home(), message_lookup=message_lookup,
                                   hermes_root=hermes_root if hermes_root is not None else _hermes_root())
    manifest.update(task_id=task.id, host=host)
    ids: dict[str, int] = {}
    for name, data in blobs:
        ids[name] = kb.store_attachment_bytes(conn, task.id, name, data, uploaded_by="dispatcher", board=board)
    for item in manifest["items"]:
        if item.get("attachment") in ids:
            item["attachment_id"] = ids[item["attachment"]]
    payload = json.dumps(manifest, indent=1, sort_keys=True).encode()
    kb.store_attachment_bytes(conn, task.id, MANIFEST_NAME, payload, content_type="application/json",
                              uploaded_by="dispatcher", board=board)
    with kb.write_txn(conn):
        kb._append_event(conn, task.id, EVENT_KIND, {"host": host, "bytes": manifest["bundled_bytes"],
                                                    **manifest["counts"]})
    return manifest


def maybe_attach(conn: sqlite3.Connection, task, *, host: Optional[str], board: Optional[str] = None) -> Optional[dict]:
    """Dispatch hook: bundle when the toggle is on and the worker is going to a pool host. Never raises."""
    try:
        if host is None or not enabled():
            return None
        return attach_bundle(conn, task, host=host, board=board)
    except Exception:  # noqa: BLE001 -- best effort; the dispatch goes on
        _log.warning("evidence bundle for %s failed", getattr(task, "id", "?"), exc_info=True)
        return None
