"""QMD document-recall fold-in for the mem0 plugin (Unified Recall, spec v0.3, 2026-06-28).

Folds the LOCAL QMD hybrid document index into mem0's two recall paths — prefetch (every
turn, intent-gated, mem0-first) and the explicit mem0_search tool (additive `docs` key) —
WITHOUT merging the stores. QMD stays read-only document SEARCH; nothing here ever writes
to mem0 or QMD (INV-1).

Lives in the mem0 plugin package (a sibling submodule, imported by __init__.py) so the pure
functions are unit-testable without the network-heavy mem0 client. No new pip dependency, no
`mcp` SDK — a tiny stdlib http.client MCP client with a hard wall-clock deadline enforced by
a watchdog that shuts the socket (interrupts an SSE keepalive trickle hang — INV-4).
"""

from __future__ import annotations

import fnmatch
import http.client
import json
import logging
import socket
import threading
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ---- config defaults (D-5) -------------------------------------------------
QMD_DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "url": "http://[::1]:8181/mcp",
    "qmd_total_deadline_s": 6.0,   # whole-operation wall-clock deadline (INV-4); 6s catches
                                   # real warm hybrid+rerank latency (measured 2.3-5.3s live,
                                   # 2026-06-30) with margin, still bounded under the 10s join
    "mem0_budget_s": 6.0,          # the mem0 leg's own budget (INV-4a); +deadline <= join 10s
    "min_score": 0.45,            # RRF/rerank score is POSITIONAL not calibrated-relevance
                                  # (rank1~0.9, rank2~0.5, tail 0.34-0.47 — identical for a real
                                  # and a nonsense query; measured 2026-06-30). So this floor TRIMS
                                  # the low-rank tail, it does NOT gate relevance. Relevance is
                                  # protected by prefetch_limit + the intent gate. 0.45 keeps the
                                  # legit rank-2 hit that 0.5 flakily clipped.
    "prefetch_limit": 3,
    "search_limit": 5,
    # allowlist — sessions & memories EXCLUDED by default (egress-aware, INV-5)
    "collections": ["obsidian", "skills", "plans", "projects"],
    "exclude_path_globs": [],      # client-side post-filter on `file` (INV-5/N3)
    "intent_min_tokens": 4,
    "prefetch_rerank": True,
}

# leading tokens that mark a NON-lookup turn (affirmation / imperative-action) — D-9/INV-7
_NON_LOOKUP_LEADERS = {
    "yes", "yep", "yeah", "ok", "okay", "sure", "thanks", "thank", "thx", "ty",
    "ship", "do", "go", "fix", "run", "add", "delete", "remove", "make", "build",
    "create", "commit", "push", "merge", "send", "post", "stop", "cancel", "no",
    "nope", "yup", "sounds", "great", "perfect", "good", "nice", "cool",
}


def load_qmd_config(raw: Optional[dict]) -> Dict[str, Any]:
    """Merge a `qmd` sub-block over the defaults. Missing block -> defaults (enabled False)."""
    cfg = dict(QMD_DEFAULTS)
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k in cfg and v is not None:
                cfg[k] = v
    return cfg


def is_lookup_intent(query: str, min_tokens: int) -> bool:
    """Pure intent gate (D-9). False for short or imperative/affirmation turns → skip QMD."""
    if not query or not query.strip():
        return False
    toks = query.strip().lower().split()
    if len(toks) < max(1, int(min_tokens)):
        return False
    first = "".join(ch for ch in toks[0] if ch.isalpha())
    if first in _NON_LOOKUP_LEADERS:
        return False
    return True


def parse_qmd_results(payload: Any, min_score: float,
                      exclude_globs: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Pure: extract the pointer list from a tools/call `query` response.

    GROUND-TRUTHED shape: result["structuredContent"]["results"] (top-level result keys are
    `content` + `structuredContent`; there is NO bare result["results"]). Pointers only — the
    `snippet`/`context`/`content` body fields are dropped (INV-5).
    """
    try:
        results = (payload or {}).get("result", {}).get("structuredContent", {}).get("results", [])
    except AttributeError:
        return []
    if not isinstance(results, list):
        return []
    globs = exclude_globs or []
    out: List[Dict[str, Any]] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        try:
            score = float(r.get("score", 0) or 0)
        except (TypeError, ValueError):
            score = 0.0
        if score < min_score:
            continue
        f = str(r.get("file", "") or "")
        if any(fnmatch.fnmatch(f, g) for g in globs):
            continue
        out.append({
            "file": f,
            "title": str(r.get("title", "") or ""),
            "score": round(score, 4),
            "line": int(r.get("line", 0) or 0),
            "docid": str(r.get("docid", "") or ""),
        })
    return out


def render_qmd_block(hits: List[Dict[str, Any]]) -> str:
    """Pure renderer. Empty -> "" (no header, INV-7/m2). Pointers only (INV-5)."""
    if not hits:
        return ""
    lines = ["## Local Docs (QMD)"]
    for h in hits:
        pct = f"{round(float(h.get('score', 0)) * 100)}%"
        title = h.get("title") or h.get("file", "")
        line = h.get("line", 0)
        loc = f" :{line}" if line else ""
        lines.append(f"- {h.get('file','')} — {title} ({pct}){loc}")
    return "\n".join(lines)


def join_blocks(mem0_block: str, qmd_block: str) -> str:
    """Join the two recall blocks. Skip the separator when a side is empty (INV-6/m2 byte-guard)."""
    a = mem0_block or ""
    b = qmd_block or ""
    if a and b:
        return a + "\n\n" + b
    return a or b


def _extract_json(raw: str) -> Optional[Any]:
    """Parse a possibly-SSE-framed JSON body (collect `data:` lines, else the raw body)."""
    data_lines = [ln[5:].lstrip() for ln in raw.splitlines() if ln.startswith("data:")]
    body = "".join(data_lines) if data_lines else raw
    try:
        return json.loads(body)
    except Exception:
        return None


def qmd_query(query: str, *, limit: int, min_score: float,
              collections: Optional[List[str]], rerank: bool, deadline_s: float,
              url: str = "http://[::1]:8181/mcp",
              exclude_globs: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Hard-cancellable MCP `query` against the local QMD daemon. ANY failure -> [].

    A watchdog timer trips at `deadline_s` (wall-clock, whole operation incl. the 3-POST
    handshake) and `sock.shutdown(SHUT_RDWR)+close()`s the live connection, so a blocked
    read — including an SSE keepalive trickle — raises immediately instead of hanging the
    turn (INV-4). Degraded-safe: never raises, the watchdog is always cancelled (INV-3).
    """
    parsed = urlparse(url)
    host = parsed.hostname or "::1"
    port = parsed.port or 8181
    path = parsed.path or "/mcp"

    state: Dict[str, Any] = {"conn": None, "sock": None, "fired": False}
    lock = threading.Lock()

    def _trip() -> None:
        with lock:
            state["fired"] = True
            conn = state["conn"]
            sock = state.get("sock")
        # http.client can hand the raw socket to HTTPResponse.read(); closing only
        # HTTPConnection is not enough on that path. Shut down/close both handles.
        for s in (sock, getattr(conn, "sock", None) if conn is not None else None):
            if s is None:
                continue
            try:
                s.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                s.close()
            except Exception:
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    timer = threading.Timer(max(0.05, float(deadline_s)), _trip)
    timer.daemon = True
    timer.start()

    common = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Connection": "close",
    }

    def _post(body: dict, headers: dict):
        with lock:
            if state["fired"]:
                raise TimeoutError("qmd deadline tripped")
            conn = http.client.HTTPConnection(host, port, timeout=float(deadline_s))
            state["conn"] = conn
        conn.request("POST", path, body=json.dumps(body), headers=headers)
        with lock:
            state["sock"] = getattr(conn, "sock", None)
        resp = conn.getresponse()
        with lock:
            # Keep the raw socket handle alive for the watchdog while HTTPResponse.read()
            # blocks; HTTPConnection may no longer expose it once the response owns it.
            state["sock"] = getattr(resp, "fp", None) and getattr(resp.fp, "raw", None) and getattr(resp.fp.raw, "_sock", None) or state.get("sock")
        data = resp.read().decode("utf-8", "replace")
        sid = resp.getheader("mcp-session-id")
        try:
            conn.close()
        except Exception:
            pass
        return resp.status, data, sid

    try:
        _st, _d, sid = _post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "mem0-recall", "version": "1"}},
        }, common)
        if not sid:
            return []
        h2 = dict(common)
        h2["mcp-session-id"] = sid
        _post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}, h2)

        args: Dict[str, Any] = {
            "searches": [{"type": "vec", "query": query}, {"type": "lex", "query": query}],
            "limit": int(limit),
            "minScore": float(min_score),
            "rerank": bool(rerank),
        }
        if collections:
            args["collections"] = list(collections)
        st, data, _sid = _post({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "query", "arguments": args},
        }, h2)
        if st != 200:
            return []
        payload = _extract_json(data)
        if payload is None:
            return []
        return parse_qmd_results(payload, min_score, exclude_globs)
    except Exception as e:  # degraded-safe: ANY failure -> [] (INV-3)
        logger.debug("qmd_query degraded: %s", e)
        return []
    finally:
        timer.cancel()
