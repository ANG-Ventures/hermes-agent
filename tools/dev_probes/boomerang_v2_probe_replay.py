#!/usr/bin/env python3
"""Boomerang v2 Phase-0 probe P0.2 / P0.4: does a byte-identical replay read the cache?

Dev probe, not a tool (no registry.register; tools/dev_probes/ has no __init__.py so tool
discovery never imports it). Stdlib + urllib only.

Two steps against the claude pool relay (anthropic_messages wire):

  parent  Load a saved request (a Hermes request_dump_*.json, or a bare Messages body),
          normalise it ONCE, POST it under a scratch session id, and save the exact body
          bytes sent, the seat that served it (x-pool-served-by), usage and the reply.
  replay  Re-send the saved body bytes UNCHANGED + the parent's reply as an assistant turn
          + ONE appended user turn, pinned with `x-hermes-pin-sub: <served seat>`.
          Prints usage and PASS/FAIL: cache_read_input_tokens >= threshold * parent prompt
          tokens (input + cache_read + cache_creation of the parent call).

Normalisation (applied once, in `parent`; `replay` reuses the saved bytes verbatim):
  * `extra_headers` moves out of the body; x-hermes-* routing headers are dropped and
    replaced by a scratch session id (never a live session's id); anthropic-beta is kept.
  * `--nonce` (default on) prefixes system[0] text with a scratch marker so the parent
    write is cold and the replay's read can only be OURS.
  * `--max-tokens` caps output (max_tokens is not part of the cache key).
  * `--ttl {keep,1h,5m}` rewrites every cache_control ttl (5m = drop the ttl key).
  * `--drop-thinking` removes thinking/output_config (both calls identical either way).

Never prints the API key. Exit codes: 0 PASS (replay) / ok (parent), 1 FAIL, 2 usage/HTTP.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:18810/anthropic/v1/messages"
PIN_HEADER = "x-hermes-pin-sub"
PROBE_USER_TURN = (
    "[boomerang-v2 P0 probe] Reply with exactly the single word: ack. "
    "Do not call any tools."
)


def _load_body(path: str) -> dict:
    with open(path, encoding="utf-8-sig") as fh:
        doc = json.load(fh)
    if isinstance(doc, dict) and isinstance(doc.get("request"), dict) and "body" in doc["request"]:
        return doc["request"]["body"]
    if isinstance(doc, dict) and "messages" in doc:
        return doc
    raise SystemExit(f"{path}: neither a request_dump (request.body) nor a Messages body")


def _set_ttl(node, ttl: str) -> int:
    n = 0
    if isinstance(node, dict):
        cc = node.get("cache_control")
        if isinstance(cc, dict):
            if ttl == "5m":
                cc.pop("ttl", None)
            else:
                cc["ttl"] = ttl
            n += 1
        for v in node.values():
            n += _set_ttl(v, ttl)
    elif isinstance(node, list):
        for v in node:
            n += _set_ttl(v, ttl)
    return n


def normalise(body: dict, *, model, max_tokens, ttl, nonce, drop_thinking):
    body = json.loads(json.dumps(body))
    extra = body.pop("extra_headers", None) or {}
    headers = {k: v for k, v in extra.items() if not k.lower().startswith("x-hermes-")}
    if model:
        body["model"] = model
    if max_tokens:
        body["max_tokens"] = max_tokens
    if drop_thinking:
        body.pop("thinking", None)
        body.pop("output_config", None)
    body.pop("stream", None)
    if ttl != "keep":
        _set_ttl(body, ttl)
    if nonce:
        sysblocks = body.get("system")
        marker = f"[boomerang-p0 scratch {nonce}]\n"
        if isinstance(sysblocks, list) and sysblocks and isinstance(sysblocks[0], dict):
            sysblocks[0]["text"] = marker + sysblocks[0].get("text", "")
        elif isinstance(sysblocks, str):
            body["system"] = marker + sysblocks
    return body, headers


def post(url: str, body_bytes: bytes, headers: dict, timeout: float):
    hdrs = {"content-type": "application/json", "anthropic-version": "2023-06-01"}
    hdrs.update(headers)
    key = os.environ.get("CLAUDE_POOL_KEY") or "placeholder"
    hdrs["x-api-key"] = key
    req = urllib.request.Request(url, data=body_bytes, headers=hdrs, method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, rh, raw = resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        status, rh, raw = exc.code, dict(exc.headers or {}), exc.read()
    rh = {k.lower(): v for k, v in rh.items()}
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = {"_raw": raw[:2000].decode("utf-8", "replace")}
    return status, rh, payload, round(time.time() - t0, 1)


def _usage_line(tag, status, rh, payload, elapsed):
    usage = payload.get("usage") if isinstance(payload, dict) else None
    out = {
        "step": tag, "http": status, "elapsed_s": elapsed,
        "served_by": rh.get("x-pool-served-by"), "route_id": rh.get("x-pool-route-id"),
        "usage": usage,
    }
    if status != 200:
        out["error"] = payload if isinstance(payload, dict) else str(payload)[:500]
    print(json.dumps(out, sort_keys=True))
    return usage or {}


def _prompt_tokens(u: dict) -> int:
    return int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0) + \
        int(u.get("cache_creation_input_tokens") or 0)


def cmd_parent(a) -> int:
    body, extra = normalise(_load_body(a.request), model=a.model, max_tokens=a.max_tokens,
                            ttl=a.ttl, nonce=(None if a.no_nonce else (a.nonce or secrets.token_hex(6))),
                            drop_thinking=a.drop_thinking)
    session = a.session or f"boomerang-p0-{secrets.token_hex(6)}"
    headers = dict(extra, **{"x-hermes-session": session, "x-hermes-lane": "background"})
    if a.pin:
        headers[PIN_HEADER] = a.pin
    body_bytes = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    print(json.dumps({"step": "parent", "session": session, "model": body.get("model"),
                      "messages": len(body.get("messages", [])), "tools": len(body.get("tools", [])),
                      "body_bytes": len(body_bytes), "pin": a.pin}))
    status, rh, payload, el = post(a.url, body_bytes, headers, a.timeout)
    usage = _usage_line("parent", status, rh, payload, el)
    if status != 200:
        return 2
    state = {
        "ts": time.time(), "session": session, "url": a.url, "headers": headers,
        "served_by": rh.get("x-pool-served-by"), "usage": usage,
        "reply": payload.get("content"), "body": body_bytes.decode("utf-8"),
    }
    with open(a.state, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    print(json.dumps({"state": a.state, "prompt_tokens": _prompt_tokens(usage)}))
    return 0


def _ack_from_reply(reply) -> list:
    texts = [b for b in (reply or []) if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
    return [{"type": "text", "text": t["text"]} for t in texts] or [{"type": "text", "text": "ok"}]


def cmd_replay(a) -> int:
    with open(a.state, encoding="utf-8-sig") as fh:
        st = json.load(fh)
    body = json.loads(st["body"])  # the exact bytes the parent sent, re-parsed (no transform)
    body["messages"] = body["messages"] + [
        {"role": "assistant", "content": _ack_from_reply(st.get("reply"))},
        {"role": "user", "content": a.user_turn},
    ]
    headers = dict(st["headers"])
    pin = a.pin or st.get("served_by")
    if pin:
        headers[PIN_HEADER] = pin
    age = round(time.time() - st["ts"], 1)
    parent_prompt = _prompt_tokens(st.get("usage") or {})
    body_bytes = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    prefix_same = body_bytes.startswith(st["body"][: st["body"].index('"messages":[') + 12].encode("utf-8"))
    print(json.dumps({"step": "replay", "session": st["session"], "pin": pin, "age_s": age,
                      "parent_prompt_tokens": parent_prompt, "pre_messages_bytes_identical": prefix_same}))
    status, rh, payload, el = post(st.get("url") or a.url, body_bytes, headers, a.timeout)
    usage = _usage_line("replay", status, rh, payload, el)
    if status != 200:
        return 2
    read = int(usage.get("cache_read_input_tokens") or 0)
    ratio = (read / parent_prompt) if parent_prompt else 0.0
    if a.expect == "miss":
        ok = read < a.threshold * parent_prompt
    else:
        ok = read >= a.threshold * parent_prompt
    print(json.dumps({"verdict": "PASS" if ok else "FAIL", "expect": a.expect,
                      "cache_read": read, "cache_creation": usage.get("cache_creation_input_tokens"),
                      "parent_prompt_tokens": parent_prompt, "read_ratio": round(ratio, 4),
                      "threshold": a.threshold, "served_by": rh.get("x-pool-served-by"), "age_s": age}))
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    pa = sub.add_parser("parent", help="send the saved request once; record seat + usage")
    pa.add_argument("--request", required=True, help="request_dump_*.json or a Messages body JSON")
    pa.add_argument("--state", required=True, help="where to write the parent state JSON")
    pa.add_argument("--url", default=DEFAULT_URL)
    pa.add_argument("--model", default=None, help="override model (e.g. a model with quota)")
    pa.add_argument("--max-tokens", type=int, default=256)
    pa.add_argument("--ttl", choices=("keep", "1h", "5m"), default="keep")
    pa.add_argument("--nonce", default=None, help="scratch marker (default: random)")
    pa.add_argument("--no-nonce", action="store_true", help="send system bytes untouched")
    pa.add_argument("--drop-thinking", action="store_true")
    pa.add_argument("--session", default=None, help="scratch session id (default: random)")
    pa.add_argument("--pin", default=None, help="pin the PARENT to a seat too")
    pa.add_argument("--timeout", type=float, default=600)
    pa.set_defaults(func=cmd_parent)
    pr = sub.add_parser("replay", help="replay parent bytes + one user turn, pinned; PASS/FAIL")
    pr.add_argument("--state", required=True)
    pr.add_argument("--url", default=DEFAULT_URL)
    pr.add_argument("--pin", default=None, help="override pin (default: parent's served seat)")
    pr.add_argument("--user-turn", default=PROBE_USER_TURN)
    pr.add_argument("--threshold", type=float, default=0.85)
    pr.add_argument("--expect", choices=("hit", "miss"), default="hit")
    pr.add_argument("--timeout", type=float, default=600)
    pr.set_defaults(func=cmd_replay)
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
