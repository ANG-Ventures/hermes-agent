#!/usr/bin/env python3
"""Boomerang v2 INV-4 gate (spec AC-5): does a forked child own its inherited turns?

Dev probe, not a tool (no registry.register; tools/dev_probes/ has no __init__.py).
Stdlib + urllib; reuses the P0 replay helpers (normalise / post / _prompt_tokens).

Shape of one prefix (mirrors spec 5.1/5.2 and D-8):
  * A real saved parent request (request_dump_*.json, anthropic_messages body) is cut so the
    transcript ENDS at a real, complete, text-only assistant turn (the parent's "last complete
    assistant turn"). One sentinel sentence is planted in an EARLIER assistant turn.
  * warm   POSTs the parent request that produced that final assistant turn (transcript minus
           the final assistant turn, one 1h cache_control marker on its last block), under a
           scratch session id. This is the parent's own cold write; its served seat is recorded.
  * fork   (x N) re-sends the warm body bytes UNCHANGED + the real final assistant turn + ONE
           framing turn, pinned to the warm call's seat (x-hermes-pin-sub, first request only).
           `--framing user` appends the spec 5.2 user turn (D-10). `--framing ack` (the D-10
           fallback) appends an assistant acknowledgement + a user turn carrying the task.

Per fork it records: cache_read ratio (cache_read / input+cache_read+cache_creation), recall
(exact sentinel token present in the reply text), disavowal (fixed INV-4 regex), raw reply.

Subcommands:
  plan    list text-only assistant cut points of a dump with cumulative body chars.
  run     warm + N forks for one prefix; appends one JSON row per fork to --out.
  report  print the per-prefix table and the gate verdict for a results.jsonl.

Never prints the API key. Exit codes: 0 ok, 1 gate FAIL (report), 2 usage/HTTP.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from boomerang_v2_probe_replay import (  # noqa: E402  (sibling dev probe, not a package)
    DEFAULT_URL,
    PIN_HEADER,
    _load_body,
    _prompt_tokens,
    normalise,
    post,
)

SENTINEL_TOKEN = "7F3A"
SENTINEL_TEXT = "Noting for later in this session: the deploy token for project Kestrel is 7F3A."
TASK = "State the deploy token for project Kestrel and which turn you set it in."
DISAVOWAL_RE = re.compile(r"never did|didn't do that|wasn't me|not my (work|turn)|I have no record", re.I)
CONTRACT = (
    "Execute autonomously: make reasonable assumptions, do NOT stop to ask questions. If you hit a "
    "genuine blocker, complete what you can and report the blocker in your summary."
)
HANDOFF = "End with the structured handoff: Outcome / Changed / Commands-validation / Blockers."


def frame_user_turn(parent_sid: str, delegation_id: str, goal: str = TASK) -> str:
    """Spec 5.2 frame, verbatim except the placeholders (<sid>, <del-...>, <goal>, contract)."""
    return (
        "[BOOMERANG FORK — you are a forked continuation of this same conversation, running in the\n"
        f"background. The turns above are yours. Parent session: {parent_sid}. Delegation ID: {delegation_id}.\n"
        f"Your task now: {goal} {CONTRACT}\n"
        f"{HANDOFF}]"
    )


# D-10 fallback: trailing assistant ack (the fork speaking as itself) + a user turn with the task.
ACK_TEXT = (
    "Understood. I am a forked continuation of this same conversation, running in the background; "
    "the turns above are mine. Ready for the task."
)


def frame_ack_turns(parent_sid: str, delegation_id: str, goal: str = TASK) -> list:
    return [
        {"role": "assistant", "content": [{"type": "text", "text": ACK_TEXT}]},
        {"role": "user", "content": frame_user_turn(parent_sid, delegation_id, goal)},
    ]


# ── prefix construction ──────────────────────────────────────────────────────────────────────
def _blocks(content) -> list:
    return content if isinstance(content, list) else [{"type": "text", "text": str(content or "")}]


def _is_text_only_assistant(m) -> bool:
    if m.get("role") != "assistant":
        return False
    bl = _blocks(m.get("content"))
    has_text = any(isinstance(b, dict) and b.get("type") == "text" and str(b.get("text", "")).strip() for b in bl)
    has_tool = any(isinstance(b, dict) and b.get("type") == "tool_use" for b in bl)
    return has_text and not has_tool


def _strip_message_cache_control(msgs) -> int:
    n = 0
    for m in msgs:
        if isinstance(m.get("content"), list):
            for b in m["content"]:
                if isinstance(b, dict) and b.pop("cache_control", None) is not None:
                    n += 1
    return n


def cut_points(body: dict) -> list:
    """Indexes i of text-only assistant turns preceded by a user turn, with cumulative chars."""
    msgs = body["messages"]
    base = len(json.dumps({k: v for k, v in body.items() if k != "messages"}, ensure_ascii=False))
    out, run = [], base
    for i, m in enumerate(msgs):
        run += len(json.dumps(m, ensure_ascii=False)) + 1
        if i >= 2 and _is_text_only_assistant(m) and msgs[i - 1].get("role") == "user":
            out.append((i, run))
    return out


def plant(msgs: list, idx: int) -> None:
    """Plant SENTINEL_TEXT in assistant turn msgs[idx] (text appended; tool_use order kept)."""
    m = msgs[idx]
    assert m.get("role") == "assistant", idx
    c = m.get("content")
    if isinstance(c, str):
        m["content"] = c.rstrip() + "\n\n" + SENTINEL_TEXT
        return
    blocks = list(c)
    first_tool = next((j for j, b in enumerate(blocks) if isinstance(b, dict) and b.get("type") == "tool_use"), None)
    new = {"type": "text", "text": SENTINEL_TEXT}
    if first_tool is None:
        blocks.append(new)
    else:
        blocks.insert(first_tool, new)
    m["content"] = blocks


def pick_plant_index(msgs: list, cut: int, frac: float) -> int:
    """The assistant turn nearest to frac*cut (prefer text-bearing), strictly before the cut."""
    target = int(cut * frac)
    cands = [i for i in range(1, cut) if msgs[i].get("role") == "assistant"]
    if not cands:
        raise SystemExit("no assistant turn before the cut to plant into")
    texty = [i for i in cands if any(isinstance(b, dict) and b.get("type") == "text" for b in _blocks(msgs[i].get("content")))]
    pool = texty or cands
    return min(pool, key=lambda i: abs(i - target))


def build_prefix(request_path: str, cut: int, plant_frac: float, model: str, nonce: str):
    """Return (warm_body, final_assistant_msg, plant_idx, headers). warm_body ends with a user turn."""
    raw = _load_body(request_path)
    body, headers = normalise(raw, model=model, max_tokens=1024, ttl="1h", nonce=nonce, drop_thinking=True)
    msgs = body["messages"]
    if not (0 < cut < len(msgs)) or not _is_text_only_assistant(msgs[cut]):
        raise SystemExit(f"cut {cut} is not a text-only assistant turn (see `plan`)")
    msgs = msgs[: cut + 1]
    plant_idx = pick_plant_index(msgs, cut, plant_frac)
    plant(msgs, plant_idx)
    _strip_message_cache_control(msgs)
    final = msgs.pop()  # the parent's last complete assistant turn (real, unmodified)
    last = msgs[-1]
    blocks = _blocks(last.get("content"))
    blocks[-1] = dict(blocks[-1], cache_control={"type": "ephemeral", "ttl": "1h"})
    last["content"] = blocks
    body["messages"] = msgs
    return body, final, plant_idx, headers


def _reply_text(payload) -> str:
    content = payload.get("content") if isinstance(payload, dict) else None
    return "\n".join(b.get("text", "") for b in (content or []) if isinstance(b, dict) and b.get("type") == "text")


def _reply_tools(payload) -> list:
    content = payload.get("content") if isinstance(payload, dict) else None
    return [b.get("name") for b in (content or []) if isinstance(b, dict) and b.get("type") == "tool_use"]


def _dumps(body) -> bytes:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# ── subcommands ──────────────────────────────────────────────────────────────────────────────
def cmd_plan(a) -> int:
    body = _load_body(a.request)
    msgs = body["messages"]
    print(json.dumps({"request": os.path.basename(a.request), "messages": len(msgs), "model": body.get("model")}))
    for i, chars in cut_points(body):
        print(json.dumps({"cut": i, "chars": chars, "est_tokens": int(chars / a.chars_per_token)}))
    return 0


def cmd_run(a) -> int:
    nonce = secrets.token_hex(6)
    warm_body, final, plant_idx, extra = build_prefix(a.request, a.cut, a.plant_frac, a.model, nonce)
    session = f"boomerang-inv4-{secrets.token_hex(6)}"
    headers = dict(extra, **{"x-hermes-session": session, "x-hermes-lane": "background"})
    warm_bytes = _dumps(warm_body)
    print(json.dumps({"step": "warm", "prefix": a.label, "session": session, "messages": len(warm_body["messages"]),
                      "plant_idx": plant_idx, "cut": a.cut, "body_bytes": len(warm_bytes)}), flush=True)
    status, rh, payload, el = post(a.url, warm_bytes, headers, a.timeout)
    usage = (payload.get("usage") if isinstance(payload, dict) else None) or {}
    seat = rh.get("x-pool-served-by")
    print(json.dumps({"step": "warm", "http": status, "elapsed_s": el, "served_by": seat, "usage": usage}), flush=True)
    if status != 200:
        print(json.dumps({"error": payload}, default=str)[:2000])
        return 2
    warm_ts = time.time()
    parent_prompt = _prompt_tokens(usage)
    rows = 0
    for k in range(a.n):
        delegation_id = f"del-inv4-{secrets.token_hex(4)}"
        body = json.loads(warm_bytes)  # warm bytes re-parsed, no transform
        if a.framing == "user":
            tail = [final, {"role": "user", "content": frame_user_turn(session, delegation_id)}]
        else:
            tail = [final] + frame_ack_turns(session, delegation_id)
        body["messages"] = body["messages"] + tail
        fb = _dumps(body)
        # The warm request's bytes are exactly reproduced by the fork body cut back to warm length.
        prefix_same = _dumps(dict(body, messages=body["messages"][: -len(tail)])) == warm_bytes
        fh = dict(headers)
        fh[PIN_HEADER] = seat
        st, rh2, pl, el2 = post(a.url, fb, fh, a.timeout)
        u = (pl.get("usage") if isinstance(pl, dict) else None) or {}
        prompt = _prompt_tokens(u)
        read = int(u.get("cache_read_input_tokens") or 0)
        text = _reply_text(pl)
        row = {
            "prefix": a.label, "framing": a.framing, "probe": k + 1, "delegation_id": delegation_id,
            "source_dump": os.path.basename(a.request), "cut": a.cut, "plant_idx": plant_idx,
            "session": session, "model": warm_body.get("model"), "http": st, "elapsed_s": el2,
            "warm_served_by": seat, "served_by": rh2.get("x-pool-served-by"), "pin": seat,
            "age_s": round(time.time() - warm_ts, 1), "parent_prompt_tokens": parent_prompt,
            "prompt_tokens": prompt, "cache_read": read,
            "cache_creation": int(u.get("cache_creation_input_tokens") or 0),
            "input_tokens": int(u.get("input_tokens") or 0), "output_tokens": int(u.get("output_tokens") or 0),
            "cache_read_ratio": round(read / prompt, 4) if prompt else 0.0,
            "warm_body_prefix_identical": prefix_same,
            "recall": SENTINEL_TOKEN in text,
            "disavowal": bool(DISAVOWAL_RE.search(text)),
            "disavowal_match": (DISAVOWAL_RE.search(text).group(0) if DISAVOWAL_RE.search(text) else None),
            "tool_calls": _reply_tools(pl), "stop_reason": pl.get("stop_reason") if isinstance(pl, dict) else None,
            "response_text": text,
        }
        if st != 200:
            row["error"] = pl
        with open(a.out, "a", encoding="utf-8") as fh_out:
            fh_out.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        rows += 1
        print(json.dumps({k2: row[k2] for k2 in ("prefix", "framing", "probe", "http", "served_by", "prompt_tokens",
                                                   "cache_read_ratio", "recall", "disavowal", "tool_calls")}), flush=True)
    return 0


def cmd_report(a) -> int:
    rows = [json.loads(line) for line in open(a.results, encoding="utf-8-sig") if line.strip()]
    if a.framing:
        rows = [r for r in rows if r.get("framing") == a.framing]
    by = {}
    for r in rows:
        by.setdefault(r["prefix"], []).append(r)
    print("| prefix | source | prompt tokens | probes | cache_read ratio (min) | recall | disavowal |")
    print("|---|---|---|---|---|---|---|")
    for p, rs in by.items():
        src = rs[0]["source_dump"].removeprefix("request_dump_").removesuffix(".json")
        print(f"| {p} | {src} | {max(r['prompt_tokens'] for r in rs):,} | {len(rs)} | "
              f"{min(r['cache_read_ratio'] for r in rs):.4f} | {sum(r['recall'] for r in rs)}/{len(rs)} | "
              f"{sum(r['disavowal'] for r in rs)}/{len(rs)} |")
    n = len(rows)
    recall = sum(r["recall"] for r in rows)
    dis = sum(r["disavowal"] for r in rows)
    ok = n >= 20 and recall >= n - 1 and dis == 0
    print(json.dumps({"framing": a.framing, "n": n, "recall": recall, "disavowal": dis,
                      "bar": ">= 19/20 recall AND 0/20 disavowal", "verdict": "PASS" if ok else "FAIL"}))
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    pp = sub.add_parser("plan", help="list text-only assistant cut points")
    pp.add_argument("--request", required=True)
    pp.add_argument("--chars-per-token", type=float, default=2.42, help="P0.2: 1,208,629 B / 502,708 tok")
    pp.set_defaults(func=cmd_plan)
    pr = sub.add_parser("run", help="warm the prefix once, then N pinned fork probes")
    pr.add_argument("--request", required=True)
    pr.add_argument("--cut", type=int, required=True, help="index of the final text-only assistant turn")
    pr.add_argument("--label", required=True, help="prefix label, e.g. 200k")
    pr.add_argument("--out", required=True, help="results.jsonl (appended)")
    pr.add_argument("--framing", choices=("user", "ack"), default="user")
    pr.add_argument("--n", type=int, default=4)
    pr.add_argument("--plant-frac", type=float, default=0.4, help="plant depth as a fraction of the cut")
    pr.add_argument("--model", default="claude-opus-5-5")
    pr.add_argument("--url", default=DEFAULT_URL)
    pr.add_argument("--timeout", type=float, default=600)
    pr.set_defaults(func=cmd_run)
    rp = sub.add_parser("report", help="table + gate verdict")
    rp.add_argument("--results", required=True)
    rp.add_argument("--framing", choices=("user", "ack"), default=None)
    rp.set_defaults(func=cmd_report)
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
