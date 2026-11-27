"""The card's Intent block at the top of the worker's own PR body (t_deee5435, parent t_897eeabb).

Ace 2026-11-04 07:24: the original intent of a change must reach the Prism reviewers. Prism builds intentContext
from the PR title and body only, and 0 of the last 50 worker PRs carried the card's ask. hermes-home
``scripts/lib/pr_intent.py`` (hermes-home#9032) renders a marked ``## Intent`` section; this module calls it from the
handback (``kanban_pr_freshness.check``) for each of the card's OWN PRs, before the draft -> ready flip, so the PR
Prism starts on already carries the ask, acceptance, rulings and the handback's verification.

Worker PRs are opened by the hermes-home gh shim (as drafts), not by hermes-agent, so the first agent-side moment
a PR is known is the handback that names it; that write lands before the ready flip, i.e. before Prism ever reads
the body. Every later handback re-applies the block with its own verification, replacing it in place.

* Renderer: ``<hermes root>/scripts/lib/pr_intent.py render`` when the fleet root has it, else :func:`build_section`
  here, a port with the SAME markers, cap and cut order, so the two can never disagree on what to replace.
* Rulings: card comments whose first line starts ``ruling:`` are added to the card body the renderer reads.
* Write: GET, merge, re-GET, PATCH ``body`` only when unchanged since the snapshot (one retry), as the library does.
* Kill switch: ``KANBAN_PR_INTENT=0`` or ``KANBAN_HANDOFF_PR_INTENT=0`` (env) or ``kanban.pr_intent: false``
  (config). Any failure is logged in one line and reported in the handoff metadata; it never blocks a handback.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Optional

_log = logging.getLogger(__name__)

INTENT_MAX = 2000
START = "<!-- pr-intent:start (t_897eeabb) -->"
END = "<!-- pr-intent:end -->"
CARD_URL = "https://kanban.ace/t/%s"
CARD_RE = re.compile(r"^t_[0-9a-f]{8}$")
META_RE = re.compile(r"^\s*(origin|host|scope|requires|workspace|assignee|priority)\s*:", re.I)
HEADING_RE = re.compile(r"^\s*#{1,6}\s+(.*?)\s*#*\s*$")
ACCEPT_RE = re.compile(r"^\s*(?:[*_]{0,2})acceptance\b[^\n:]{0,80}:", re.I)
RULING_RE = re.compile(r"^\s*[*_>-]*\s*(?:ace\s+)?ruling\b[^\n:]{0,80}:", re.I)
SECTION_NAMES = {"intent", "ask", "acceptance", "acceptance criteria", "verification", "rulings", "ruling",
                 "declared file scope", "scope"}
COMMENT_RULING_RE = re.compile(r"^\s*ruling\s*:", re.I)
MAX_VERIFICATION_LINES = 40
_TIMEOUT_S = 60

GhFn = Callable[..., "subprocess.CompletedProcess"]
# (repo, number) -> status string for the handoff metadata
IntentFn = Callable[[str, int], str]


# ---- kill switch -------------------------------------------------------------------------------------------------

def enabled() -> bool:
    for var in ("KANBAN_PR_INTENT", "KANBAN_HANDOFF_PR_INTENT"):
        if os.environ.get(var, "1").strip() == "0":
            return False
    try:
        from hermes_cli.config import load_config
        value = ((load_config() or {}).get("kanban") or {}).get("pr_intent", True)
    except Exception:  # noqa: BLE001 -- an unreadable config never turns the feature into a failure
        return True
    return value is not False and str(value).strip().lower() not in ("0", "false", "off", "no")


# ---- in-repo renderer: a port of hermes-home scripts/lib/pr_intent.py build_section ------------------------------

def _escape(text):
    return (text or "").replace("<!--", "&lt;!--")


def _blocks(body):
    out, head, cur = [], None, []
    for line in (body or "").replace("\r\n", "\n").split("\n"):
        m = HEADING_RE.match(line)
        if m:
            if cur:
                out.append((head, cur))
            head = m.group(1).strip()
            cur = [] if head.lower().rstrip(":").strip() in SECTION_NAMES else [head]
            continue
        if not line.strip():
            if cur:
                out.append((head, cur))
                cur = []
            continue
        cur.append(line.rstrip())
    if cur:
        out.append((head, cur))
    return out


def parse_card(body):
    ask, acc, rulings, plan = [], [], [], []
    for head, lines in _blocks(body):
        h = (head or "").lower()
        lines = [x for x in lines if not META_RE.match(x)]
        if not lines:
            continue
        if h.startswith("acceptance") or ACCEPT_RE.match(lines[0]):
            acc.extend(lines)
        elif h.startswith("verification"):
            plan.extend(lines)
        elif h.startswith("declared file scope") or h.startswith("scope"):
            continue
        elif h.rstrip(":").strip() in ("ruling", "rulings") or any(RULING_RE.match(x) for x in lines):
            rulings.extend(lines)
        else:
            if ask:
                ask.append("")
            ask.extend(lines)
    return {"ask": ask, "acceptance": acc, "rulings": rulings, "planned_verification": plan}


def _clip(lines, limit):
    text = "\n".join(lines).strip()
    if len(text) <= limit:
        return text
    if limit < 40:
        return ""
    cut = text[: limit - 30]
    sp = max(cut.rfind("\n"), cut.rfind(" "))
    if sp > limit // 2:
        cut = cut[:sp]
    return cut.rstrip() + " [... cut; full card linked]"


def build_section(card_id, title, body, verification=None, limit=INTENT_MAX):
    """The marked ``## Intent`` section; byte-for-byte the library's output for the same input."""
    title, body = _escape(title), _escape(body)
    p = parse_card(body)
    ver = [_escape(x).rstrip() for x in (verification or []) if x.strip()]
    ver_label = "Verification (worker evidence)"
    if not ver and p["planned_verification"]:
        ver, ver_label = p["planned_verification"], "Verification (planned on the card)"
    link = CARD_URL % card_id if CARD_RE.match(card_id or "") else ""
    head = [START, "## Intent", "", "Card: `%s`%s" % (_escape(card_id) or "?", " (%s)" % link if link else ""),
            "Title: %s" % (title or "").strip()]
    parts = [("Ask", p["ask"]), ("Acceptance (verbatim from the card)", p["acceptance"]),
             ("Rulings (verbatim)", p["rulings"]), (ver_label, ver)]
    fixed = len("\n".join(head)) + len(END) + 2 + sum(len(n) + 8 for n, v in parts if v)
    room = max(0, limit - fixed)
    want = {i: len("\n".join(parts[i][1]).strip()) for i in range(4)}
    give = {i: min(want[i], cap) for i, cap in ((1, 700), (2, 400), (3, 500))}
    give[0] = max(0, room - sum(give.values()))
    spare = max(0, give[0] - want[0])
    for i in (1, 2, 3):
        extra = min(spare, want[i] - give[i])
        give[i] += extra
        spare -= extra
    out = list(head)
    for i, (name, lines) in enumerate(parts):
        text = _clip(lines, give[i]) if lines else ""
        if text:
            out += ["", "**%s**" % name, "", text]
    out += ["", END]
    s = "\n".join(out)
    if len(s) > limit:
        s = s[: limit - len(END) - 1].rstrip() + "\n" + END
    return s


def merge_body(body, section):
    """``section`` at the top of ``body``, replacing an earlier marked section (idempotent)."""
    body = (body or "").replace("\r\n", "\n")
    i = body.find(START)
    j = body.find(END, i) if i != -1 else -1
    if i != -1 and j != -1:
        rest = (body[:i] + body[j + len(END):]).strip("\n")
    else:
        rest = body.strip("\n")
    return section + ("\n\n" + rest if rest else "") + "\n"


# ---- card + verification inputs ----------------------------------------------------------------------------------

def card_from_board(conn, task_id: str) -> Optional[dict]:
    """``{id, title, body}`` with each ``ruling:`` comment appended to the body as its own paragraph."""
    row = conn.execute("SELECT id, title, body FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        return None
    from hermes_cli.kanban_identity import is_operator_label

    body = str(row[2] or "")
    rulings = []
    for text, author in conn.execute(
            "SELECT body, author FROM task_comments WHERE task_id = ? ORDER BY id", (task_id,)).fetchall():
        if not is_operator_label(author):
            continue
        text = str(text or "").strip()
        if text and COMMENT_RULING_RE.match(text.splitlines()[0]):
            # One paragraph per comment: the renderer splits on blank lines and classifies a paragraph by
            # its own lines, so a second paragraph would read as ask.
            rulings.append("\n".join(x for x in text.splitlines() if x.strip()))
    if rulings:
        # Under its own heading: appended after a trailing `## Declared file scope` they would be scope.
        body = body.rstrip("\n") + "\n\n## Rulings\n\n" + "\n\n".join(rulings)
    return {"id": str(row[0]), "title": str(row[1] or ""), "body": body}


def verification_lines(summary: Optional[str], metadata: Optional[dict]) -> list:
    """The handback's evidence: its ``live-evidence:`` line and test counts first, then the summary lines."""
    meta = metadata if isinstance(metadata, dict) else {}
    lines = [x.rstrip() for x in str(summary or "").splitlines() if x.strip()]
    head = []
    if not any(x.strip().lower().startswith("live-evidence:") for x in lines):
        le = meta.get("live_evidence")
        if isinstance(le, str) and le.strip():
            head.append("live-evidence: %s" % le.strip())
    for key in ("tests", "tests_run", "tests_passed", "tests_failed"):
        if key in meta and meta[key] not in (None, "", {}, []):
            val = meta[key]
            head.append("%s: %s" % (key, val if isinstance(val, (str, int, float))
                                    else json.dumps(val, sort_keys=True, default=str)))
    return (head + lines)[:MAX_VERIFICATION_LINES]


# ---- render ------------------------------------------------------------------------------------------------------

def library_path() -> Optional[Path]:
    """hermes-home ``scripts/lib/pr_intent.py`` under the fleet root, or None."""
    try:
        from hermes_constants import get_default_hermes_root
        p = get_default_hermes_root() / "scripts" / "lib" / "pr_intent.py"
    except Exception:  # noqa: BLE001
        return None
    return p if p.is_file() else None


def render(card: dict, verification: list) -> tuple:
    """-> (section, renderer) where renderer is ``library`` or ``fallback``."""
    lib = library_path()
    # Older fleet libraries classify heading-only rulings as ask and clip them.
    # Keep those cards on the corrected renderer until the shared library catches up.
    heading_rulings = any((head or "").lower().rstrip(":").strip() in ("ruling", "rulings")
                         for head, _lines in _blocks(card.get("body", "")))
    if lib is not None and not heading_rulings:
        tmp = []
        try:
            for payload in (json.dumps({"task": card}), "\n".join(verification)):
                fh = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
                with fh:
                    fh.write(payload)
                tmp.append(fh.name)
            args = [sys.executable or "python3", str(lib), "render", "--card-json", tmp[0]]
            if verification:
                args += ["--verification", tmp[1]]
            # Prism b3697fd74088: we decode UTF-8, so make the helper write UTF-8 whatever the locale's code page.
            proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=_TIMEOUT_S, check=False,
                                  env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1"))
            section = (proc.stdout or "").strip("\n")
            if proc.returncode == 0 and section.startswith(START) and section.endswith(END):
                return section, "library"
            _log.warning("pr intent: library render rc=%s, using the in-repo renderer: %s", proc.returncode,
                         (proc.stderr or "").strip()[:200])
        except (OSError, subprocess.SubprocessError) as exc:
            _log.warning("pr intent: library render failed, using the in-repo renderer: %s", exc)
        finally:
            for path in tmp:
                try:
                    os.unlink(path)
                except OSError:
                    pass
    return build_section(card["id"], card.get("title", ""), card.get("body", ""), verification), "fallback"


# ---- apply -------------------------------------------------------------------------------------------------------

def gh_run(*args: str) -> "subprocess.CompletedProcess":
    # GH_SHIM_FRESH=1: the fleet gh shim caches identical GETs for 30 s; merging against a cached body would
    # overwrite an edit made inside that window (same as the library, Prism 13fe648102a4).
    return subprocess.run(["gh", "api", *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=_TIMEOUT_S, check=False,
                          env=dict(os.environ, GH_SHIM_FRESH="1"))


def _read_body(gh: GhFn, repo: str, number: int) -> Optional[str]:
    r = gh(f"repos/{repo}/pulls/{number}")
    if getattr(r, "returncode", 1) != 0:
        return None
    try:
        return json.loads(r.stdout).get("body") or ""
    except (ValueError, AttributeError, TypeError):
        return None


def apply(gh: GhFn, repo: str, number: int, section: str) -> str:
    """Write ``section`` at the top of the PR body. -> ``written`` | ``unchanged`` | ``error: ...``.

    GitHub has no If-Match on a PR update: the guard is a re-read right before the PATCH; a body edited after the
    snapshot restarts the merge once, a second change writes nothing."""
    for _attempt in (1, 2):
        cur = _read_body(gh, repo, number)
        if cur is None:
            return "error: PR read failed"
        new = merge_body(cur, section)
        if new == cur:
            return "unchanged"
        now = _read_body(gh, repo, number)
        if now is None:
            return "error: PR re-read failed"
        if now == cur:
            break
    else:
        return "error: body changed twice under apply; nothing written"
    w = gh("-X", "PATCH", f"repos/{repo}/pulls/{number}", "-f", f"body={new}")
    if getattr(w, "returncode", 1) != 0:
        return "error: PR body write failed: %s" % (getattr(w, "stderr", "") or "").strip()[:160]
    return "written"


def applier(card: Optional[dict], verification: list, *, gh: Optional[GhFn] = None) -> Optional[IntentFn]:
    """``(repo, number) -> status`` for ``kanban_pr_freshness.check``, or None when off / no card / under pytest
    without an injected ``gh`` (never a live GitHub write from the test suite)."""
    if not card or not enabled():
        return None
    if gh is None:
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return None
        gh = gh_run
    rendered: dict = {}

    def _apply(repo: str, number: int) -> str:
        try:
            if "section" not in rendered:
                rendered["section"], rendered["renderer"] = render(card, verification)
            status = apply(gh, repo, number, rendered["section"])
        except Exception as exc:  # noqa: BLE001 -- the Intent block never blocks a handback
            status = "error: %s" % str(exc)[:160]
        if status.startswith("error"):
            _log.warning("pr intent: %s#%s %s (handback continues)", repo, number, status)
        return "%s (%s, %d chars)" % (status, rendered.get("renderer", "?"), len(rendered.get("section", "")))
    return _apply


def applier_for_task(conn, task_id: str, summary: Optional[str], metadata: Optional[dict]) -> Optional[IntentFn]:
    """The handback's applier from the board row; None (logged) on any failure."""
    if not enabled():
        return None
    try:
        return applier(card_from_board(conn, task_id), verification_lines(summary, metadata))
    except Exception as exc:  # noqa: BLE001
        _log.warning("pr intent: %s card read failed (handback continues): %s", task_id, exc)
        return None
