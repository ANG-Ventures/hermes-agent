"""One schema for every mem0.json the plugin reads, plus a lint that validates the live files.

Why: 13 clients on 2 hosts each carry their own mem0.json, and the plugin reads it with
``dict.get`` everywhere, so a typo'd key, a wrong type, or a ``pin_user_id: true`` with no
resolvable ``user_id`` (the provider then refuses to initialize) only shows up at runtime, on one
host. ``validate()`` is the single definition; ``main()`` scans a fleet home and its profiles.

    python3 -m plugins.memory.mem0.config_schema [--home ~/.hermes ...] [--json]

Exit 0 = no errors (warnings allowed), 1 = at least one error, 2 = usage. The lint prints key
NAMES and types only, never a value (mem0.json holds the admin key).
This file is also run standalone on hosts whose agent tree predates it:
    ssh host 'python3 - --home ~/.hermes' < plugins/memory/mem0/config_schema.py
so it imports nothing outside the standard library.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

_NUM = (int, float)
_STR = (str,)
_BOOL = (bool,)
_DICT = (dict,)

# key -> accepted JSON types. Booleans that the plugin reads through ``_truthy`` also accept the
# string forms ("true"/"false") it understands.
KEYS: Dict[str, Tuple[type, ...]] = {
    # connection
    "host": _STR,
    "admin_api_key": _STR,
    "api_key": _STR,
    "ca_bundle": _STR,
    "mode": _STR,
    # identity
    "user_id": _STR,
    "agent_id": _STR,
    "pin_user_id": _BOOL + _STR,
    # recall
    "rerank": _STR + _BOOL,
    "rerank_deadline_ms": _NUM,
    "keyword_search": _BOOL,
    "prefetch_join_timeout_s": _NUM,
    "temporal_search": _BOOL + _STR,
    "temporal_tz": _STR,
    "temporal_overfetch": _NUM,
    "retrieval_kill": _DICT,
    "prefetch_relevance_floor": _DICT,
    "prefetch_rerank_gate": _DICT,
    "prefetch_rerank_gap": _DICT,
    # capture / writes
    "capture": _STR,
    "capture_model": _STR,
    "dedup_candidate_k": _NUM,
    "dedup_cosine_threshold": _NUM,
    "dedup_embed_model": _STR,
    "openai_api_key": _STR,
    "mem0_capture_router": _DICT,
    # document leg
    "mem0_gbrain": _DICT,
    "gbrain": _DICT,
    # destructive tools
    "destructive_tools_enabled": _BOOL + _STR,
    "max_bulk": _NUM,
    "max_bulk_hard_force": _NUM,
    "max_bulk_forget": _NUM,
    "max_bulk_forget_force": _NUM,
    "max_delete_per_hour": _NUM,
    "max_forget_per_hour": _NUM,
    "unscoped_ratio": _NUM,
    "absolute_mass_floor": _NUM,
    "token_ttl_seconds": _NUM,
    # read by fleet scripts, not the plugin (mem0_retrieval_canary, mem0_store_target)
    "graph": _DICT,
    "store_ssh_user": _STR,
}

# Sub-blocks: key -> accepted types. Unknown sub-keys are warnings.
BLOCKS: Dict[str, Dict[str, Tuple[type, ...]]] = {
    "retrieval_kill": {"rerank": _BOOL + _STR},
    "prefetch_relevance_floor": {"enabled": _BOOL + _STR, "min_content_tokens": _NUM, "min_cosine": _NUM},
    "prefetch_rerank_gate": {"enabled": _BOOL + _STR, "min_rerank": _NUM, "min_rerank_specific": _NUM,
                             "specific_min_content_tokens": _NUM},
    "prefetch_rerank_gap": {"enabled": _BOOL + _STR, "max_gap": _NUM},
    "mem0_capture_router": {"enabled": _BOOL + _STR, "staging_mode": _BOOL, "model": _STR,
                            "fallback_model": _STR, "primary_url": _STR, "fallback_url": _STR,
                            "primary_secret_ref": _STR, "fallback_secret_ref": _STR,
                            "timeout_s": _NUM, "staging_dir": _STR, "brain_inbox_dir": _STR,
                            "confidence_floor": _NUM},
    "mem0_gbrain": {"enabled": _BOOL + _STR, "prefetch_enabled": _BOOL + _STR,
                    "search_enabled": _BOOL + _STR, "url": _STR, "creds_path": _STR,
                    "total_deadline_s": _NUM, "mem0_budget_s": _NUM, "min_score": _NUM,
                    "prefetch_limit": _NUM, "search_limit": _NUM, "intent_min_tokens": _NUM},
    "graph": {"ssh_host": _STR, "container": _STR},
}
BLOCKS["gbrain"] = BLOCKS["mem0_gbrain"]

RETIRED = {"mem0_qmd", "qmd", "qmd_total_deadline_s"}
CAPTURE_VALUES = {"auto", "on", "true", "1", "off", "manual", "false", "0"}
RERANK_VALUES = {"off", "builtin"}


def _truthy(v: Any) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _tname(t: Tuple[type, ...]) -> str:
    return "|".join(sorted({x.__name__ for x in t}))


def _type_ok(v: Any, types: Tuple[type, ...]) -> bool:
    if isinstance(v, bool) and bool not in types:
        return False  # True is an int in Python; a bool where a number belongs is drift
    return isinstance(v, types)


def validate(cfg: Any, *, env_keys: Optional[set] = None) -> List[Tuple[str, str, str]]:
    """Return [(level, key, message)] with level in {"error", "warn"}. Never includes a value.

    env_keys: the MEM0_* names set in the profile's .env (names only). Used to decide whether a
    setting the file omits is supplied by the environment (user_id, admin key)."""
    env_keys = env_keys or set()
    out: List[Tuple[str, str, str]] = []
    if not isinstance(cfg, dict):
        return [("error", "", "mem0.json is not a JSON object")]
    for k, v in cfg.items():
        if k in RETIRED:
            out.append(("warn", k, "retired key (qmd leg removed); delete it"))
            continue
        if k not in KEYS:
            out.append(("warn", k, "unknown key: the plugin never reads it (typo?)"))
            continue
        if v is None or v == "":
            continue  # the loader drops empty values, so they mean "use the env/default"
        if not _type_ok(v, KEYS[k]):
            out.append(("error", k, f"wrong type {type(v).__name__}, want {_tname(KEYS[k])}"))
            continue
        if k in BLOCKS:
            for sk, sv in v.items():
                if sk not in BLOCKS[k]:
                    out.append(("warn", f"{k}.{sk}", "unknown sub-key: never read"))
                elif sv is not None and not _type_ok(sv, BLOCKS[k][sk]):
                    out.append(("error", f"{k}.{sk}",
                                f"wrong type {type(sv).__name__}, want {_tname(BLOCKS[k][sk])}"))
    host = str(cfg.get("host") or "").strip()
    if host:
        if not host.startswith(("http://", "https://")):
            out.append(("error", "host", "must start with http:// or https://"))
        if not (str(cfg.get("admin_api_key") or "").strip() or "MEM0_ADMIN_API_KEY" in env_keys):
            out.append(("error", "admin_api_key",
                        "self-hosted host set but no admin_api_key (file or MEM0_ADMIN_API_KEY): "
                        "the provider reports itself unavailable"))
        if host.startswith("https://") and not str(cfg.get("ca_bundle") or "").strip() \
                and "MEM0_CA_BUNDLE" not in env_keys:
            out.append(("warn", "ca_bundle", "https host with no ca_bundle: a private-CA endpoint "
                                             "fails TLS verification"))
    elif not (str(cfg.get("api_key") or "").strip() or "MEM0_API_KEY" in env_keys
              or "MEM0_HOST" in env_keys):
        out.append(("error", "host", "neither a self-hosted host nor a cloud api_key is configured"))
    if _truthy(cfg.get("pin_user_id", False)) and not str(cfg.get("user_id") or "").strip() \
            and "MEM0_USER_ID" not in env_keys:
        out.append(("error", "user_id", "pin_user_id is true but no user_id (file or MEM0_USER_ID): "
                                        "the provider refuses to initialize"))
    cap = cfg.get("capture")
    if isinstance(cap, str) and cap.strip() and cap.strip().lower() not in CAPTURE_VALUES:
        out.append(("error", "capture", f"unknown capture mode, want one of {sorted(CAPTURE_VALUES)}"))
    rr = cfg.get("rerank")
    if isinstance(rr, str) and rr.strip() and rr.strip().lower() not in RERANK_VALUES:
        out.append(("warn", "rerank", "not 'builtin' or 'off': the plugin treats it as off"))
    return out


def _env_keys(env_path: str) -> set:
    keys = set()
    try:
        with open(env_path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:]
                k, sep, v = line.partition("=")
                if sep and k.strip().startswith("MEM0_") and v.strip():
                    keys.add(k.strip())
    except OSError:
        pass
    return keys


def scan_home(home: str) -> List[Dict[str, Any]]:
    home = os.path.expanduser(home)
    results = []
    for path in [os.path.join(home, "mem0.json")] + sorted(
            glob.glob(os.path.join(home, "profiles", "*", "mem0.json"))):
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8-sig") as f:
                cfg = json.load(f)
            findings = validate(cfg, env_keys=_env_keys(os.path.join(os.path.dirname(path), ".env")))
        except (OSError, ValueError) as e:
            findings = [("error", "", f"unreadable: {type(e).__name__}")]
        results.append({"file": path, "findings": findings})
    return results


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m plugins.memory.mem0.config_schema")
    ap.add_argument("--home", action="append", help="fleet home to scan (repeatable; default ~/.hermes)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    results = []
    for h in args.home or ["~/.hermes"]:
        results += scan_home(h)
    errors = sum(1 for r in results for f in r["findings"] if f[0] == "error")
    if args.json:
        print(json.dumps({"files": len(results), "errors": errors, "results": results}))
    else:
        for r in results:
            for level, key, msg in r["findings"]:
                print(f"{level.upper():5} {r['file']} {key}: {msg}")
        print(f"mem0.json schema: files={len(results)} errors={errors} "
              f"warnings={sum(1 for r in results for f in r['findings'] if f[0] == 'warn')}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
