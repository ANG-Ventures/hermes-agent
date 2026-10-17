"""Strict append-only oracle for consecutive provider requests (t_91b6a4c4).

Ported from the Phase-1 measurement's ``strict_pairs.py`` (t_38dd407a): request N is STABLE iff its
system, tools and every message are an exact prefix of request N+1. That script canonicalised the
Anthropic wire (cache_control dropped, whitespace edges trimmed); this oracle applies the same rule to
the chat-completions request the test agent sends, dropping only ``cache_control``, so it is at least
as strict. It does not grant prefix_diff's "no replayed thinking after the edit" leniency.
"""

import json


def _canon(obj):
    if isinstance(obj, dict):
        return {k: _canon(v) for k, v in obj.items() if k != "cache_control" and v is not None}
    if isinstance(obj, list):
        return [_canon(v) for v in obj]
    return obj


def _fp(obj) -> str:
    return json.dumps(_canon(obj), sort_keys=True, ensure_ascii=False)


def append_only_breaks(req_n: dict, req_n1: dict) -> list:
    """``[]`` when ``req_n`` is a strict prefix of ``req_n1``; else one ``(section, detail)`` per break."""
    out = []
    a, b = req_n.get("messages") or [], req_n1.get("messages") or []
    sys_a = [m for m in a if m.get("role") == "system"]
    sys_b = [m for m in b if m.get("role") == "system"]
    if [_fp(m) for m in sys_a] != [_fp(m) for m in sys_b]:
        out.append(("system", "system prompt changed"))
    if _fp(req_n.get("tools")) != _fp(req_n1.get("tools")):
        out.append(("tools", "tools changed"))
    fa, fb = [_fp(m) for m in a], [_fp(m) for m in b]
    if not (len(fb) >= len(fa) and fb[: len(fa)] == fa):
        k = 0
        while k < min(len(fa), len(fb)) and fa[k] == fb[k]:
            k += 1
        out.append(("messages", f"diverge@{k} of {len(fa)}->{len(fb)}"))
    return out
