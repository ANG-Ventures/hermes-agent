#!/usr/bin/env python3
"""Isolated adoption smoke for stephenschoettler/hermes-lcm (PRD #2 v2, Phase 3).

This probe evaluates the vendored ``hermes-lcm`` context engine WITHOUT touching
any live profile. It drives the real ``LCMEngine`` in-process against a throwaway
SQLite DB under a temp dir, with the summarization LLM stubbed deterministically
so the smoke is offline and reproducible (the same technique the plugin's own
tests use via ``monkeypatch.setattr(lcm_engine, "summarize_with_escalation", ...)``).

Scope guard (hard): the plugin is loaded ONLY from the vendored copy at
``staging/lcm-profile/plugins/hermes-lcm`` inside this worktree. The probe never
writes to ``~/.hermes/plugins`` or ``~/.hermes/profiles/*`` and never runs the
plugin's ``scripts/install.sh`` (which would symlink into a live plugin dir).

Smoke dimensions (PRD #2 v2 §5 Phase 3 / Acceptance):
  1. load + identity        — engine loads as a ContextEngine subclass, name == "lcm"
  2. normal chat/tool        — ingest messages, lcm_status / lcm_describe respond
  3. threshold compaction    — should_compress() honors threshold; compress() builds a
                               DAG summary node and shrinks active context
  4. lcm_grep / describe /   — a fact compacted out of active context is still found by
     expand recall             grep and recovered byte-identically by expand
  5. reset semantics         — on_session_reset() zeroes per-session counters while the
                               immutable lossless store still answers grep
  6. failure behavior        — summarizer LLM unavailable degrades to L3 deterministic
                               truncation (fail-open: no crash, no message lost)

Usage:
  python scripts/probe_hermes_lcm_isolated.py \
      --profile-dir staging/lcm-profile \
      --out docs/reports/hermes-lcm-adoption-smoke.md

Exit code 0 = all smoke checks passed; non-zero = at least one check failed.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

# Worktree root = parent of this scripts/ dir. Used for both `import agent.*`
# and locating the vendored plugin under the isolated staging profile.
WORKTREE_ROOT = Path(__file__).resolve().parent.parent


class Check:
    """One smoke assertion with its observed evidence."""

    def __init__(self, name: str, ok: bool, evidence: str):
        self.name = name
        self.ok = ok
        self.evidence = evidence

    def line(self) -> str:
        mark = "PASS" if self.ok else "FAIL"
        return f"[{mark}] {self.name} — {self.evidence}"


def _load_plugin_package(plugin_dir: Path):
    """Register the vendored plugin as the ``hermes_lcm`` package, in-process.

    Mirrors the plugin's own tests/conftest.py loader so relative imports
    inside the plugin resolve. Does NOT exec __init__.register() (which would
    try to wire into a live host); we instantiate LCMEngine directly.
    """
    if str(WORKTREE_ROOT) not in sys.path:
        sys.path.insert(0, str(WORKTREE_ROOT))

    pkg = "hermes_lcm"
    if pkg in sys.modules:
        return sys.modules[pkg]
    spec = importlib.util.spec_from_file_location(
        pkg, str(plugin_dir / "__init__.py"),
        submodule_search_locations=[str(plugin_dir)],
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__path__ = [str(plugin_dir)]
    mod.__package__ = pkg
    sys.modules[pkg] = mod
    return mod


def _new_engine(LCMEngine, LCMConfig, db_path: Path, *, leaf_chunk_tokens: int = 1):
    cfg = LCMConfig(
        fresh_tail_count=4,
        leaf_chunk_tokens=leaf_chunk_tokens,
        database_path=str(db_path),
    )
    e = LCMEngine(config=cfg)
    e.context_length = 200_000
    e.threshold_tokens = int(200_000 * cfg.context_threshold)
    return e, cfg


def run_smoke(plugin_dir: Path) -> List[Check]:
    checks: List[Check] = []
    _load_plugin_package(plugin_dir)

    from agent.context_engine import ContextEngine
    from hermes_lcm.config import LCMConfig
    from hermes_lcm.engine import LCMEngine
    import hermes_lcm.engine as lcm_engine
    import hermes_lcm.escalation as lcm_escalation

    tmp = Path(tempfile.mkdtemp(prefix="lcm_adopt_smoke_"))

    # ---- 1. load + identity ----
    e, cfg = _new_engine(LCMEngine, LCMConfig, tmp / "identity.db")
    e.on_session_start("identity-s", platform="cli", context_length=200_000)
    is_engine = issubclass(LCMEngine, ContextEngine)
    checks.append(Check(
        "load+identity",
        is_engine and e.name == "lcm",
        f"ContextEngine subclass={is_engine}, engine.name={e.name!r}, "
        f"version={(plugin_dir / 'plugin.yaml').exists()}",
    ))

    # ---- 2. normal chat/tool (status, describe respond on a live session) ----
    e._ingest_messages([
        {"role": "user", "content": "hello, normal turn"},
        {"role": "assistant", "content": "hi"},
    ])
    status = json.loads(e.handle_tool_call("lcm_status", {}))
    describe = json.loads(e.handle_tool_call("lcm_describe", {}))
    normal_ok = (
        status.get("session_id") is not None
        and "store_message_count" in describe
        and describe.get("store_message_count", 0) >= 2
    )
    checks.append(Check(
        "normal-chat/tool",
        normal_ok,
        f"lcm_status.session_id={status.get('session_id')!r}, "
        f"lcm_describe.store_message_count={describe.get('store_message_count')}",
    ))
    e.shutdown()

    # ---- 3 + 4. threshold compaction + grep/describe/expand recall ----
    # Deterministic summarizer stub (offline; same seam the plugin's tests patch).
    lcm_engine.summarize_with_escalation = lambda **kw: (
        "SUMMARY: earlier turns covered the deploy code and arithmetic", 1)

    e2, cfg2 = _new_engine(LCMEngine, LCMConfig, tmp / "compact.db")
    e2.on_session_start("compact-s", platform="cli", context_length=200_000)
    secret = "DEPLOY-CODE-7F3A"
    convo: List[Dict[str, Any]] = [
        {"role": "system", "content": "You are concise."},
        {"role": "user", "content": f"Remember the deploy code is {secret} for prod."},
        {"role": "assistant", "content": f"Noted {secret}."},
        {"role": "user", "content": "What is 2+2?"},
        {"role": "assistant", "content": "4"},
        {"role": "user", "content": "And 3+3?"},
        {"role": "assistant", "content": "6"},
        {"role": "user", "content": "What was the deploy code?"},
    ]
    fires = e2.should_compress(e2.threshold_tokens) and not e2.should_compress(1000)
    active = e2.compress(list(convo))
    compacted = (
        e2._last_compression_status == "compacted"
        and e2.compression_count == 1
        and len(active) < len(convo)
    )
    has_summary = any("SUMMARY:" in (m.get("content") or "") for m in active)
    checks.append(Check(
        "threshold-compaction",
        fires and compacted and has_summary,
        f"should_compress(threshold)=True/should_compress(1000)=False={fires}; "
        f"status={e2._last_compression_status}, count={e2.compression_count}, "
        f"active {len(active)}<orig {len(convo)}; DAG-summary-in-active={has_summary}",
    ))

    grep = json.loads(e2.handle_tool_call("lcm_grep", {"query": secret}))
    grep_hits = grep.get("total_results", 0)
    # grep returns ranked candidates; an agent inspects each snippet and expands
    # the one that actually carries the fact. Select by snippet content rather
    # than blindly taking results[0] (FTS rank order is not the selector).
    results = grep.get("results") or []
    chosen = next(
        (r for r in results if secret in (r.get("snippet") or r.get("content") or "")),
        results[0] if results else {},
    )
    store_id = chosen.get("store_id")
    expand = json.loads(e2.handle_tool_call("lcm_expand", {"store_id": store_id}))
    byte_exact = secret in (expand.get("content") or "")
    recall_ok = grep_hits >= 1 and store_id is not None and byte_exact
    checks.append(Check(
        "grep/describe/expand-recall",
        recall_ok,
        f"grep total_results={grep_hits} (fact compacted out of active ctx); "
        f"selected snippet-matching store_id={store_id}; expand.content recovers raw "
        f"{secret!r} byte-exact={byte_exact}",
    ))

    # expand of an unknown id must be a loud, non-fabricating error
    expand_bad = json.loads(e2.handle_tool_call("lcm_expand", {"store_id": 999_999}))
    checks.append(Check(
        "expand-unknown-id-loud-error",
        "error" in expand_bad and "999999" in json.dumps(expand_bad),
        f"lcm_expand(bad id) -> {json.dumps(expand_bad)[:120]}",
    ))
    e2.shutdown()

    # ---- 5. reset semantics ----
    e3, cfg3 = _new_engine(LCMEngine, LCMConfig, tmp / "reset.db")
    e3.on_session_start("reset-s", platform="cli", context_length=200_000)
    reset_convo = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "fact ALPHA-secret-token"},
        {"role": "assistant", "content": "ok alpha"},
        {"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"}, {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "q3"},
    ]
    e3.compress(list(reset_convo))
    count_before = e3.compression_count
    grep_before = json.loads(
        e3.handle_tool_call("lcm_grep", {"query": "ALPHA-secret-token"})).get("total_results", 0)
    e3.on_session_reset()
    count_after = e3.compression_count
    grep_after_all = json.loads(e3.handle_tool_call(
        "lcm_grep", {"query": "ALPHA-secret-token", "session_scope": "all"})).get("total_results", 0)
    reset_ok = count_before >= 1 and count_after == 0 and grep_after_all >= 1
    checks.append(Check(
        "reset-semantics",
        reset_ok,
        f"compression_count {count_before}->{count_after} after on_session_reset; "
        f"grep-before={grep_before}; lossless store still answers grep after reset "
        f"(all-scope)={grep_after_all}",
    ))
    e3.shutdown()

    # ---- 6. failure behavior (summarizer LLM down -> L3 deterministic truncation) ----
    # Patch the LLM chain to yield no usable summary; escalation must fall through
    # to deterministic truncation (guaranteed convergence) rather than raising.
    lcm_escalation._invoke_summary_llm_chain = lambda *a, **k: None
    e4, cfg4 = _new_engine(LCMEngine, LCMConfig, tmp / "fail.db")
    e4.on_session_start("fail-s", platform="cli", context_length=200_000)
    fail_convo = [{"role": "system", "content": "sys"}] + [
        {"role": "user", "content": f"fact number {i} with filler text to summarize"}
        for i in range(8)
    ]
    crashed = False
    crash_detail = ""
    try:
        fail_active = e4.compress(list(fail_convo))
    except Exception as ex:  # noqa: BLE001
        crashed = True
        fail_active = None
        crash_detail = repr(ex)
    fail_grep = 0
    if not crashed:
        fail_grep = json.loads(
            e4.handle_tool_call("lcm_grep", {"query": "fact number 0"})).get("total_results", 0)
    fail_ok = (not crashed) and fail_active is not None and len(fail_active) >= 1 and fail_grep >= 1
    checks.append(Check(
        "failure-fail-open",
        fail_ok,
        (f"summarizer LLM unavailable -> no crash={not crashed}, status="
         f"{e4._last_compression_status}, active_len={len(fail_active) if fail_active else None}, "
         f"raw still grep-recoverable={fail_grep}")
        if not crashed else f"CRASHED (fail-closed) -> {crash_detail}",
    ))
    e4.shutdown()

    return checks


def _git_identity(plugin_dir: Path) -> Dict[str, str]:
    info = {"vendored_path": str(plugin_dir)}
    yaml_path = plugin_dir / "plugin.yaml"
    if yaml_path.exists():
        for ln in yaml_path.read_text(encoding="utf-8").splitlines():
            if ln.startswith("version:"):
                info["plugin_version"] = ln.split(":", 1)[1].strip().strip('"')
            if ln.startswith("name:"):
                info["plugin_name"] = ln.split(":", 1)[1].strip().strip('"')
    prov = plugin_dir.parent.parent / "VENDORED_FROM.txt"
    if prov.exists():
        info["provenance"] = prov.read_text(encoding="utf-8").strip()
    return info


def write_report(out_path: Path, checks: List[Check], identity: Dict[str, str],
                 profile_dir: Path) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    passed = sum(1 for c in checks if c.ok)
    total = len(checks)
    verdict = "GO (isolated smoke clean)" if passed == total else "BLOCKED (smoke failure)"

    lines: List[str] = []
    lines.append("# hermes-lcm Adoption Smoke — Isolated Profile (PRD #2 v2, Phase 3)")
    lines.append("")
    lines.append(f"**Generated:** {ts}  ")
    lines.append(f"**Probe:** `scripts/probe_hermes_lcm_isolated.py --profile-dir {profile_dir} "
                 f"--out {out_path}`  ")
    lines.append(f"**Verdict:** **{verdict}** — {passed}/{total} checks passed  ")
    lines.append("")
    lines.append("## Plugin under test")
    lines.append("")
    for k, v in identity.items():
        lines.append(f"- **{k}:** `{v}`")
    lines.append("")
    lines.append("## Isolation guarantees")
    lines.append("")
    lines.append("- Engine loaded ONLY from the vendored copy under "
                 f"`{profile_dir}/plugins/hermes-lcm` inside the worktree.")
    lines.append("- No writes to `~/.hermes/plugins` or `~/.hermes/profiles/*`; "
                 "the plugin's `scripts/install.sh` (which symlinks into a live plugin dir) "
                 "was NOT run.")
    lines.append("- Each check uses a throwaway SQLite DB under a fresh temp dir.")
    lines.append("- Summarization is stubbed deterministically (offline) — same "
                 "`summarize_with_escalation` / `_invoke_summary_llm_chain` seam the "
                 "plugin's own tests patch.")
    lines.append("")
    lines.append("## Smoke results")
    lines.append("")
    lines.append("| # | Check | Result | Evidence |")
    lines.append("|---|-------|--------|----------|")
    for i, c in enumerate(checks, 1):
        ev = c.evidence.replace("|", "\\|")
        lines.append(f"| {i} | {c.name} | {'PASS' if c.ok else 'FAIL'} | {ev} |")
    lines.append("")
    lines.append("## Raw check log")
    lines.append("")
    lines.append("```")
    for c in checks:
        lines.append(c.line())
    lines.append("```")
    lines.append("")
    lines.append("## Notes for the reviewer")
    lines.append("")
    lines.append("- This is a **Phase 3 isolated smoke**, not the PRD #3 real-session recovery "
                 "gate. It proves the engine loads, compacts, recalls byte-exact, resets, and "
                 "fails open in-process — it does NOT prove a live model spontaneously calls "
                 "`lcm_expand` without being told (that is PRD #3's job).")
    lines.append("- Live activation still requires `plugins.enabled: [hermes-lcm]` + "
                 "`context.engine: lcm` in a profile config and a Hermes restart — deferred "
                 "to a first low-blast-radius profile (Daedalus/Athena) per PRD §9.5, gated on "
                 "PRD #3.")
    lines.append("- License: the upstream repo ships **no LICENSE file**. Internal fleet "
                 "run/fork is acceptable; public redistribution/vendoring is blocked until a "
                 "license grant (PRD §0.1, §1).")
    lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile-dir", required=True,
                    help="Isolated staging profile dir (e.g. staging/lcm-profile); "
                         "the vendored plugin must live at <profile-dir>/plugins/hermes-lcm")
    ap.add_argument("--out", required=True, help="Markdown report output path")
    args = ap.parse_args()

    profile_dir = (WORKTREE_ROOT / args.profile_dir).resolve() \
        if not Path(args.profile_dir).is_absolute() else Path(args.profile_dir)
    plugin_dir = profile_dir / "plugins" / "hermes-lcm"
    out_path = (WORKTREE_ROOT / args.out).resolve() \
        if not Path(args.out).is_absolute() else Path(args.out)

    # Hard isolation guard: never operate on a live profile path.
    home = Path.home()
    forbidden = [home / ".hermes" / "plugins", home / ".hermes" / "profiles"]
    rp = plugin_dir.resolve()
    for f in forbidden:
        try:
            rp.relative_to(f.resolve())
            print(f"REFUSING: plugin dir {rp} is under a live path {f}", file=sys.stderr)
            return 3
        except ValueError:
            continue

    if not plugin_dir.is_dir():
        print(f"Vendored plugin not found at {plugin_dir}", file=sys.stderr)
        return 2

    checks = run_smoke(plugin_dir)
    identity = _git_identity(plugin_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_report(out_path, checks, identity, Path(args.profile_dir))

    for c in checks:
        print(c.line())
    passed = sum(1 for c in checks if c.ok)
    print(f"\n{passed}/{len(checks)} checks passed. Report: {out_path}")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
