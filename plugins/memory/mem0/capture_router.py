"""Arm-B two-pass capture router for the Track A-lite drain worker (Phase 2.5).

FLAG-GATED, default OFF (`mem0_capture_router.enabled` in mem0.json). When OFF the drain worker's
behavior is byte-identical to today — this module is never invoked.

When ON, the router runs ADDITIVELY on top of the unchanged mem0 write path:

  drain_once():
    self._add(messages, kwargs)          <- UNCHANGED: mem0 server-side extraction + gate writes the
                                            preference/ops_state facts to the store, exactly as today
                                            (certified gate, exactly-once reconcile, post-write scrub
                                            all preserved). This IS "the existing mem0 write path".
    router.route_turn(...)               <- NEW, best-effort, never breaks the turn: two DEDICATED
                                            extraction passes run CONCURRENTLY (armB-prefs + armB-world),
                                            codex-bridge PRIMARY, gemini-bridge FALLBACK on error/timeout;
                                            then the deterministic class router:
                                              preference / ops_state -> "mem0" destination (already
                                                  written by _add above; the router does NOT re-write them)
                                              world_entity / event   -> DEDUPED against the prefs-pass
                                                  output (the benchmark's leak fix), then written as
                                                  STAGED markdown to the staging dir with frontmatter.

STAGING (the Phase 2.5 gate): while `staging_mode` is true (default), world/event facts are written to
~/.hermes/state/capture-router-staged/<date>/<turn_id>.md — NOT to mem0, NOT to the brain repo/inbox.
Flipping `staging_mode` false (a config flip, not a code change) redirects those same writes to the
gbrain capture inbox (~/gbrain/brain/inbox) which the nightly sync ingests. Go-live is a knob, not a diff.

The benchmark (benchmark-report.md) chose Arm B: two dedicated passes beat one merged prompt on the
world domain (+16 F1). Its wiring notes: run the two passes CONCURRENTLY, and dedup the world pass
against the prefs pass (B's world pass leaks user-ops junk as low-value world candidates).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# The four capture classes plus the "nothing worth capturing" sentinel.
PREFS_CLASSES = ("preference", "ops_state")
WORLD_CLASSES = ("world_entity", "event")
ALL_CLASSES = PREFS_CLASSES + WORLD_CLASSES + ("none",)

_DEFAULT_STAGING_DIR = "~/.hermes/state/capture-router-staged"
_DEFAULT_BRAIN_INBOX = "~/gbrain/brain/inbox"
# Transient-narration filter (gbrain residue PRD §5.3 / RC1). Single source of truth lives in
# ~/gbrain/scripts/transient_fact_filter.py (RC8-pinned to its eval). Loaded GUARDED + FAIL-OPEN:
# if it can't load, we drop nothing and capture continues exactly as before (never break the turn).
# Greptile #407 P2: load by FILE PATH via importlib — do NOT mutate the process-global sys.path
# (an append there permanently makes every later import in this interpreter search ~/gbrain/scripts,
# risking module shadowing in co-loaded plugins/tests).
try:
    import importlib.util as _ilu
    _TFF = os.path.expanduser("~/gbrain/scripts/transient_fact_filter.py")
    _spec = _ilu.spec_from_file_location("gbrain_transient_fact_filter", _TFF)
    if _spec and _spec.loader:
        _mod = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        _is_transient = _mod.is_transient
    else:
        _is_transient = None
except Exception as _e:  # pragma: no cover - degraded-safe
    _is_transient = None
    logging.getLogger(__name__).debug("capture-router: transient filter unavailable, fail-open: %s", _e)

# Prompt assets live alongside the plugin (copied from the benchmark harness so the live wiring does
# not depend on a path under ~/.hermes/plans, which is not shipped with the plugin).
_PROMPT_DIR = os.path.join(os.path.dirname(__file__), "assets")
_PREFS_PROMPT_FILE = "capture_router_armB_prefs.md"
_WORLD_PROMPT_FILE = "capture_router_armB_world.md"


# ---------------------------------------------------------------------------
# Candidate parsing (shared shape with bench/run_arms.py)
# ---------------------------------------------------------------------------

def parse_candidates(text: str) -> Optional[List[Dict[str, Any]]]:
    """Best-effort parse of a model extraction response into a candidate list, or None if unparseable.
    Mirrors bench/run_arms.py so the live path and the benchmark agree on output shape."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("```")[1]
        if t.startswith("json"):
            t = t[4:]
    try:
        d = json.loads(t)
    except Exception:
        try:
            d = json.loads(t[t.index("{"):t.rindex("}") + 1])
        except Exception:
            return None
    c = d.get("candidates")
    if not isinstance(c, list):
        return None
    out: List[Dict[str, Any]] = []
    for x in c:
        if isinstance(x, dict) and x.get("content"):
            out.append({
                "content": str(x["content"]),
                "class": str(x.get("class", "")).strip(),
                "confidence": x.get("confidence"),
            })
    return out


# ---------------------------------------------------------------------------
# Dedup (the benchmark's leak fix)
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set:
    return set(_WORD_RE.findall((text or "").lower()))


def dedup_world_against_prefs(
    world_cands: List[Dict[str, Any]],
    prefs_cands: List[Dict[str, Any]],
    *,
    overlap_threshold: float = 0.6,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Drop world-pass candidates that are really the SAME fact as a prefs-pass candidate — B's world
    pass leaks the user's own config/ops as low-value world_entity/event candidates (benchmark
    secondary finding). Deterministic Jaccard-style token overlap: a world candidate whose token set
    overlaps a prefs candidate at >= threshold (relative to the smaller set) is a duplicate.

    Returns (kept, dropped). Order-preserving and side-effect free (pure) so it is trivially testable.
    """
    prefs_token_sets = [_tokens(c.get("content", "")) for c in prefs_cands]
    kept: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []
    for wc in world_cands:
        wt = _tokens(wc.get("content", ""))
        is_dup = False
        if wt:
            for pt in prefs_token_sets:
                if not pt:
                    continue
                inter = len(wt & pt)
                denom = min(len(wt), len(pt))
                if denom and (inter / denom) >= overlap_threshold:
                    is_dup = True
                    break
        (dropped if is_dup else kept).append(wc)
    return kept, dropped


# ---------------------------------------------------------------------------
# Two-pass extraction with primary/fallback provider
# ---------------------------------------------------------------------------

# Primary-lane cooldown gate. When codex-bridge (CLIProxyAPI) answers 429 it names how long its
# credentials are cooling down (`{"error": {"code": "model_cooldown", "reset_seconds": N}}`). Every
# capture fired two passes straight into that 429 (94 wasted calls / 50 min on 2026-09-25, codex
# capped for days) before falling back. While the reset window is open, route straight to the
# fallback. Process-wide and keyed by primary URL so every extractor instance (one per mem0 provider
# instance) shares one view of the lane. Capped so an early recovery (a new credential) is re-probed.
_COOLDOWN_DEFAULT_S = 60.0
_COOLDOWN_MAX_S = 1800.0
_primary_cooldown_until: Dict[str, float] = {}
_primary_cooldown_lock = threading.Lock()


def _cooldown_seconds_from_429(err: urllib.error.HTTPError) -> Tuple[float, str]:
    """Seconds to skip the primary after a 429, and where that number came from.

    Order: the bridge's `error.reset_seconds` (CLIProxyAPI model_cooldown body), then a numeric
    Retry-After header, then a short default. Always clamped to (0, _COOLDOWN_MAX_S]."""
    secs: Optional[float] = None
    source = "default"
    try:
        body = err.read()
        payload = json.loads(body.decode("utf-8", "replace")) if body else {}
        inner = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(inner, dict) and inner.get("reset_seconds") is not None:
            secs = float(inner["reset_seconds"])
            source = str(inner.get("code") or "reset_seconds")
    except Exception:
        secs = None
    if secs is None:
        try:
            ra = (err.headers or {}).get("Retry-After")
            if ra is not None:
                secs = float(ra)
                source = "retry-after"
        except Exception:
            secs = None
    if secs is None or secs <= 0:
        secs, source = _COOLDOWN_DEFAULT_S, "default"
    return min(secs, _COOLDOWN_MAX_S), source


def primary_cooldown_remaining(url: str, now: Optional[float] = None) -> float:
    """Seconds left on the primary's cooldown gate (0.0 when the primary may be tried)."""
    with _primary_cooldown_lock:
        until = _primary_cooldown_until.get(url, 0.0)
    return max(0.0, until - (time.time() if now is None else now))


def reset_primary_cooldowns() -> None:
    """Clear every cooldown gate (tests / operator)."""
    with _primary_cooldown_lock:
        _primary_cooldown_until.clear()


class BridgeExtractor:
    """Runs one extraction pass against codex-bridge (PRIMARY); on ANY error/timeout falls back to
    gemini-bridge. Both are OpenAI-compatible /v1/chat/completions endpoints behind a bearer secret.

    The secret is resolved the same way the bridges' own launchers do — `op read` from 1Password
    (fleet service-account token), no new env var, no secret on disk. A caller may inject an
    `auth_fn`/`http_fn` for tests so no network or 1Password access is needed.
    """

    def __init__(
        self,
        *,
        primary_url: str = "http://127.0.0.1:18812/v1/chat/completions",
        fallback_url: str = "http://192.168.1.216:18813/v1/chat/completions",
        model: str = "gpt-6-astra",
        fallback_model: Optional[str] = None,
        primary_secret_ref: str = "op://Engineering/codex-bridge/secret",
        fallback_secret_ref: str = "op://Engineering/gemini-bridge/secret",
        timeout_s: float = 180.0,
        http_fn: Optional[Callable[[str, bytes, Dict[str, str], float], str]] = None,
        auth_fn: Optional[Callable[[str], str]] = None,
    ):
        self._primary_url = primary_url
        self._fallback_url = fallback_url
        self._model = model
        # gemini-bridge advertises different model ids; a caller can pin one. Default: let the bridge
        # pick its default model by omitting an id it doesn't know when the passthrough model is unknown.
        # The fallback bridge (gemini) does NOT share the primary's (codex) model namespace, so
        # passing the primary id through 400s every fallback ("unknown model 'gpt-…'") — the
        # fallback leg was structurally dead until 2026-09-07. Default to the bridge's own
        # family alias, which gemini-bridge re-resolves to the newest live Flash tier.
        self._fallback_model = fallback_model or "gemini-flash"
        self._primary_ref = primary_secret_ref
        self._fallback_ref = fallback_secret_ref
        self._timeout_s = timeout_s
        self._http = http_fn or self._default_http
        self._auth = auth_fn or self._op_read
        # ref -> (secret, fetched_at). TTL'd so a rotated 1Password token recovers without a
        # process restart (Greptile #250 P2); per-ref locks so concurrent first-turn passes
        # don't spawn duplicate `op read` subprocesses (Greptile #250 P2).
        self._secret_cache: Dict[str, Tuple[str, float]] = {}
        self._secret_ttl_s = 3600.0
        self._secret_locks: Dict[str, threading.Lock] = {}
        self._secret_locks_guard = threading.Lock()

    # -- provider plumbing --------------------------------------------------
    @staticmethod
    def _op_read(ref: str) -> str:
        import subprocess
        try:
            out = subprocess.run(
                ["op", "read", ref],
                capture_output=True,
                text=True,
                timeout=20,
                stdin=subprocess.DEVNULL,
            )
            if out.returncode == 0:
                return out.stdout.strip()
            logger.warning("capture-router: op read %s failed rc=%s", ref, out.returncode)
        except Exception as e:
            logger.warning("capture-router: op read %s error: %s", ref, e)
        return ""

    def _secret(self, ref: str) -> str:
        now = time.time()
        hit = self._secret_cache.get(ref)
        if hit is not None and (now - hit[1]) < self._secret_ttl_s:
            return hit[0]
        with self._secret_locks_guard:
            lock = self._secret_locks.setdefault(ref, threading.Lock())
        with lock:
            # double-check under the lock — the other pass may have minted while we waited
            hit = self._secret_cache.get(ref)
            if hit is not None and (time.time() - hit[1]) < self._secret_ttl_s:
                return hit[0]
            secret = self._auth(ref) or ""
            if secret:
                self._secret_cache[ref] = (secret, time.time())
            else:
                # failed fetch: cache briefly (60s) so a down `op` doesn't stampede,
                # but recover quickly once it's back
                self._secret_cache[ref] = ("", time.time() - self._secret_ttl_s + 60.0)
            return secret

    def invalidate_secret(self, ref: str) -> None:
        """Drop a cached secret (called on auth-shaped errors so rotation heals mid-process)."""
        self._secret_cache.pop(ref, None)

    @staticmethod
    def _default_http(url: str, body: bytes, headers: Dict[str, str], timeout: float) -> str:
        req = urllib.request.Request(url, data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8")

    def _call(self, url: str, secret_ref: str, model: str, system_prompt: str,
              user: str, assistant: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any], float]:
        user_content = f"USER MESSAGE:\n{user}\n\nASSISTANT REPLY:\n{assistant}"
        body = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        }).encode("utf-8")
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self._secret(secret_ref)}"}
        t0 = time.time()
        raw = self._http(url, body, headers, self._timeout_s)
        latency = time.time() - t0
        resp = json.loads(raw)
        text = resp["choices"][0]["message"]["content"]
        usage = resp.get("usage", {}) or {}
        cands = parse_candidates(text)
        if cands is None:
            raise ValueError(f"unparseable extraction output: {text[:200]}")
        return cands, usage, latency

    def _call_with_auth_retry(self, url: str, secret_ref: str, model: str, system_prompt: str,
                              user: str, assistant: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any], float]:
        """_call, but on an auth-shaped failure (401/403) drop the cached secret and retry once —
        so a rotated 1Password token heals mid-process instead of failing until restart."""
        try:
            return self._call(url, secret_ref, model, system_prompt, user, assistant)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                logger.warning("capture-router: auth-shaped %s from %s — refreshing secret and retrying",
                               e.code, url)
                self.invalidate_secret(secret_ref)
                return self._call(url, secret_ref, model, system_prompt, user, assistant)
            raise

    def extract(self, system_prompt: str, user: str, assistant: str) -> Dict[str, Any]:
        """One pass. Returns {candidates, usage, latency, provider} or {error, ...}. codex PRIMARY,
        gemini FALLBACK on any exception/timeout. Never raises (fail-soft — a pass failure yields no
        candidates rather than breaking the turn)."""
        cooldown_left = primary_cooldown_remaining(self._primary_url)
        try:
            if cooldown_left > 0:
                raise _PrimaryCoolingDown(cooldown_left)
            cands, usage, latency = self._call_with_auth_retry(
                self._primary_url, self._primary_ref, self._model, system_prompt, user, assistant)
            return {"candidates": cands, "usage": usage, "latency": latency, "provider": "codex-bridge"}
        except Exception as primary_err:
            if isinstance(primary_err, _PrimaryCoolingDown):
                # Known state, not a failure: no request was sent to the primary.
                logger.debug("capture-router: primary (codex-bridge) skipped, %s", primary_err)
            elif isinstance(primary_err, urllib.error.HTTPError) and primary_err.code == 429:
                secs, source = _cooldown_seconds_from_429(primary_err)
                with _primary_cooldown_lock:
                    _primary_cooldown_until[self._primary_url] = max(
                        _primary_cooldown_until.get(self._primary_url, 0.0), time.time() + secs)
                logger.info("capture-router: primary (codex-bridge) 429 (%s); routing captures "
                            "straight to fallback for %.0fs", source, secs)
            else:
                logger.warning("capture-router: primary (codex-bridge) pass failed, trying fallback: %s",
                               primary_err)
            try:
                cands, usage, latency = self._call_with_auth_retry(
                    self._fallback_url, self._fallback_ref, self._fallback_model,
                    system_prompt, user, assistant)
                return {"candidates": cands, "usage": usage, "latency": latency,
                        "provider": "gemini-bridge", "primary_error": str(primary_err)[:200]}
            except Exception as fallback_err:
                logger.warning("capture-router: fallback (gemini-bridge) pass ALSO failed: %s",
                               fallback_err)
                return {"error": f"primary={primary_err}; fallback={fallback_err}",
                        "candidates": [], "usage": {}, "latency": 0.0, "provider": "none"}


class _PrimaryCoolingDown(Exception):
    """Raised (and caught) inside extract() when the primary's cooldown gate is open."""

    def __init__(self, remaining_s: float):
        super().__init__(f"cooldown gate open for {remaining_s:.0f}s more")


# ---------------------------------------------------------------------------
# The router
# ---------------------------------------------------------------------------

def _load_prompt(name: str) -> str:
    path = os.path.join(_PROMPT_DIR, name)
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class CaptureRouter:
    """Deterministic class router around the two-pass extractor. Pure routing logic + staged writes;
    the extractor is injected so tests never touch the network."""

    def __init__(
        self,
        *,
        extractor: Optional[BridgeExtractor] = None,
        prefs_prompt: Optional[str] = None,
        world_prompt: Optional[str] = None,
        staging_dir: str = _DEFAULT_STAGING_DIR,
        brain_inbox_dir: str = _DEFAULT_BRAIN_INBOX,
        staging_mode: bool = True,
        confidence_floor: float = 0.0,
        transient_filter_enabled: bool = True,
        now_fn: Optional[Callable[[], datetime]] = None,
        write_fn: Optional[Callable[[str, str], None]] = None,
    ):
        self._extractor = extractor or BridgeExtractor()
        self._prefs_prompt = prefs_prompt if prefs_prompt is not None else _load_prompt(_PREFS_PROMPT_FILE)
        self._world_prompt = world_prompt if world_prompt is not None else _load_prompt(_WORLD_PROMPT_FILE)
        self._staging_dir = os.path.expanduser(staging_dir)
        self._brain_inbox = os.path.expanduser(brain_inbox_dir)
        self._staging_mode = bool(staging_mode)
        self._confidence_floor = float(confidence_floor)
        self._transient_filter_enabled = bool(transient_filter_enabled)
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._write = write_fn or self._default_write
        self.stats = {"turns_routed": 0, "world_staged": 0, "world_deduped": 0,
                      "prefs_seen": 0, "extract_errors": 0, "fallback_passes": 0,
                      "transient_dropped": 0}

    # -- extraction ---------------------------------------------------------
    def two_pass_extract(self, user: str, assistant: str) -> Dict[str, Any]:
        """Run the prefs pass and the world pass CONCURRENTLY (benchmark wiring note: collapse the
        2x sequential latency). Returns a dict with both pass results."""
        with ThreadPoolExecutor(max_workers=2) as ex:
            f_prefs = ex.submit(self._extractor.extract, self._prefs_prompt, user, assistant)
            f_world = ex.submit(self._extractor.extract, self._world_prompt, user, assistant)
            prefs = f_prefs.result()
            world = f_world.result()
        return {"prefs": prefs, "world": world}

    # -- routing ------------------------------------------------------------
    def _classify(self, cands: List[Dict[str, Any]], allowed: tuple) -> List[Dict[str, Any]]:
        """Deterministic class filter: keep only candidates whose class label is in `allowed` and
        which clear the confidence floor. A candidate with an out-of-domain label is dropped (the
        prefs pass should never emit world classes and vice versa; if it does, it is misrouted noise)."""
        out = []
        for c in cands:
            cls = (c.get("class") or "").strip()
            if cls not in allowed:
                continue
            conf = c.get("confidence")
            if isinstance(conf, (int, float)) and conf < self._confidence_floor:
                continue
            out.append(c)
        return out

    def route_turn(self, user: str, assistant: str, *, turn_id: str, session: str,
                   ts: Optional[str] = None) -> Dict[str, Any]:
        """Full router pass for ONE turn. Runs the two concurrent extractions, applies the
        deterministic class router + dedup, and STAGES world/event facts. Returns a structured
        result for observability/replay. Never raises (fail-soft)."""
        result: Dict[str, Any] = {
            "turn_id": turn_id, "session": session, "ts": ts,
            "prefs_facts": [], "world_facts": [], "world_dropped": [],
            "destination": None, "usage": {}, "latency": 0.0,
            "providers": {}, "error": None,
        }
        try:
            passes = self.two_pass_extract(user, assistant)
        except Exception as e:  # ThreadPool/executor level failure — should be rare (extract is soft)
            self.stats["extract_errors"] += 1
            result["error"] = f"two_pass_extract failed: {e}"
            return result

        prefs_res, world_res = passes["prefs"], passes["world"]
        result["providers"] = {"prefs": prefs_res.get("provider"), "world": world_res.get("provider")}
        for r in (prefs_res, world_res):
            if r.get("provider") == "gemini-bridge":
                self.stats["fallback_passes"] += 1
            if r.get("error"):
                self.stats["extract_errors"] += 1
        # combined tokens + latency (concurrent passes: latency is the MAX, tokens SUM)
        usage: Dict[str, Any] = {}
        for r in (prefs_res, world_res):
            for k, v in (r.get("usage") or {}).items():
                if isinstance(v, (int, float)):
                    usage[k] = usage.get(k, 0) + v
        result["usage"] = usage
        result["latency"] = max(prefs_res.get("latency") or 0.0, world_res.get("latency") or 0.0)

        prefs_cands = self._classify(prefs_res.get("candidates") or [], PREFS_CLASSES)
        world_raw = self._classify(world_res.get("candidates") or [], WORLD_CLASSES)
        # Transient-narration gate (§5.3 / RC1): drop internal work-narration BEFORE dedup/stage,
        # logging each drop to _dropped-log.jsonl (RC2). Fail-open: if the filter is unavailable the
        # comprehension keeps everything. Toggle off restores pre-filter behavior for A/B.
        # Greptile #407 P1: the WHOLE gate is wrapped — route_turn is contract-bound to "never raises"
        # (fail-soft), so a filter exception (bad input, regex issue) must degrade to keep-all, never
        # propagate into the drain worker.
        if self._transient_filter_enabled and _is_transient is not None:
            try:
                kept = []
                for c in world_raw:
                    if _is_transient(str(c.get("content") or "")):
                        self.stats["transient_dropped"] += 1
                        self._log_dropped(c, turn_id=turn_id, session=session, ts=ts)
                    else:
                        kept.append(c)
                world_raw = kept
            except Exception as e:  # fail-open: keep everything, never break the turn
                logger.debug("capture-router: transient gate errored, fail-open keep-all: %s", e)
        # DEDUP world against prefs (the leak fix).
        world_kept, world_dropped = dedup_world_against_prefs(world_raw, prefs_cands)

        self.stats["prefs_seen"] += len(prefs_cands)
        self.stats["world_deduped"] += len(world_dropped)

        result["prefs_facts"] = prefs_cands       # destination: mem0 (written by the unchanged add path)
        result["world_facts"] = world_kept
        result["world_dropped"] = world_dropped

        # Deterministic destination for world/event facts.
        dest_dir = self._staging_dir if self._staging_mode else self._brain_inbox
        result["destination"] = "staging" if self._staging_mode else "brain-inbox"
        if world_kept:
            path = self._stage_world_facts(world_kept, dest_dir, turn_id=turn_id,
                                           session=session, ts=ts)
            result["staged_path"] = path
            self.stats["world_staged"] += len(world_kept)

        self.stats["turns_routed"] += 1
        return result

    # -- transient drop log (RC2) ------------------------------------------
    def _dropped_log_path(self) -> str:
        """Where the drop-log lives, HONORING the staging contract (Greptile #407 P1). In staging
        mode world facts go to the staging dir — so the drop-log for those same facts goes there too
        (_dropped-log.jsonl under the staging dir), NOT the brain repo/inbox. Only when staging_mode
        is OFF (go-live) does it write beside the brain inbox, matching where facts then land."""
        if self._staging_mode:
            return os.path.join(self._staging_dir, "_dropped-log.jsonl")
        return os.path.join(self._brain_inbox, "_dropped-log.jsonl")

    def _log_dropped(self, cand: Dict[str, Any], *, turn_id: str, session: str,
                     ts: Optional[str]) -> None:
        """Append one dropped-fact audit row. Fail-soft: a logging error never breaks capture."""
        try:
            path = self._dropped_log_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            row = {
                "ts": ts or self._now().isoformat(),
                "turn_id": turn_id,
                "session": session,
                "class": (cand.get("class") or "").strip(),
                "confidence": cand.get("confidence"),
                "reason": "transient_narration",
                "text_prefix": str(cand.get("content") or "")[:200],
            }
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception as e:  # pragma: no cover - degraded-safe
            logger.debug("capture-router: dropped-log write failed (non-fatal): %s", e)

    # -- staged write -------------------------------------------------------
    @staticmethod
    def _default_write(path: str, content: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)

    def _stage_world_facts(self, facts: List[Dict[str, Any]], dest_dir: str, *,
                           turn_id: str, session: str, ts: Optional[str]) -> str:
        """Write world/event facts for one turn as a single markdown file with YAML frontmatter.
        Path: <dest_dir>/<date>/<turn_id>.md. The frontmatter carries class/session/ts/source_turn
        so the nightly gbrain sync (once staging_mode is flipped off) can ingest with provenance."""
        now = self._now()
        date = now.strftime("%Y-%m-%d")
        # the dominant class in the file (for the frontmatter `class`); per-fact class is inline.
        classes = [f.get("class", "") for f in facts]
        top_class = max(set(classes), key=classes.count) if classes else "world_entity"
        fm_ts = ts or now.isoformat()
        lines = [
            "---",
            f"class: {top_class}",
            f"session: {session}",
            f"ts: {fm_ts}",
            f"source_turn: {turn_id}",
            f"routed_by: capture-router-armb",
            f"staged_at: {now.isoformat()}",
            "---",
            "",
            f"# Captured world knowledge — turn {turn_id}",
            "",
        ]
        for f in facts:
            conf = f.get("confidence")
            conf_str = f" _(confidence: {conf})_" if conf is not None else ""
            lines.append(f"- **[{f.get('class','')}]** {f.get('content','')}{conf_str}")
        content = "\n".join(lines) + "\n"
        path = os.path.join(dest_dir, date, f"{turn_id}.md")
        self._write(path, content)
        return path


def build_router_from_config(cfg: Dict[str, Any]) -> Optional[CaptureRouter]:
    """Construct a CaptureRouter from the `mem0_capture_router` sub-block of mem0.json, or None if
    the flag is absent/off. Degrade-safe: any construction error -> None (router disabled, drain
    worker keeps its unchanged behavior)."""
    router_cfg = cfg.get("mem0_capture_router") or {}
    if not isinstance(router_cfg, dict) or not router_cfg.get("enabled"):
        return None
    try:
        extractor = BridgeExtractor(
            primary_url=str(router_cfg.get(
                "primary_url", "http://127.0.0.1:18812/v1/chat/completions")),
            fallback_url=str(router_cfg.get(
                "fallback_url", "http://192.168.1.216:18813/v1/chat/completions")),
            primary_secret_ref=str(router_cfg.get(
                "primary_secret_ref", "op://Engineering/codex-bridge/secret")),
            fallback_secret_ref=str(router_cfg.get(
                "fallback_secret_ref", "op://Engineering/gemini-bridge/secret")),
            model=str(router_cfg.get("model", "gpt-6-astra")),
            fallback_model=router_cfg.get("fallback_model") or None,
            timeout_s=float(router_cfg.get("timeout_s", 180.0)),
        )
        return CaptureRouter(
            extractor=extractor,
            staging_dir=str(router_cfg.get("staging_dir", _DEFAULT_STAGING_DIR)),
            brain_inbox_dir=str(router_cfg.get("brain_inbox_dir", _DEFAULT_BRAIN_INBOX)),
            staging_mode=bool(router_cfg.get("staging_mode", True)),
            confidence_floor=float(router_cfg.get("confidence_floor", 0.0)),
        )
    except Exception as e:
        logger.warning("capture-router: build failed (router disabled): %s", e)
        return None
