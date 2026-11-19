"""The Studio runs a kanban card only for a NAMED Studio-only capability (t_9965b8d5).

Ace 2026-11-10 (Discord 1569804355438379159), on top of his 2026-11-08 ruling (the Studio is the last
resort for fleet work): workers must not spawn on the Mac Studio unless they absolutely have to.
The 24 h audit before this module (posted on t_9965b8d5): 242 of 639 placements (37.9 %) ran on the
Studio; 66 needed a Studio-only capability, 65 could have run on a pool host as they were, 111 after
a small change (carry the scratch dir, port the worktree).

When the rule is active (:func:`active`):

1. A ``local`` route stands only when :func:`justify` names what established it: a capability that
   no pool host's row in ``fleet/host-capabilities.json`` lists (or ``control-plane-read``), or a
   structural fact only the Studio serves (an assignee no pool host runs, a native-command lane, a
   link or shared directory on the Studio, a host-bound card no pool host proves it can reach). The
   files in its scratch dir (``local_files``) and a worktree (``workspace_kind``) are NOT
   capabilities: the dispatcher carries / ports them; a card it cannot move WAITS, named.
2. A portable card is never spawned on the Studio: remote first always, and a pool refusal is a wait
   (``studio_capability_only:<refusal>``), never the old local last resort.
3. ``host: studio`` holds only with a ``requires:`` word for a Studio-ONLY capability (fleet-ssh,
   docker, screen and browser-ephemeral are on pool hosts too: they no longer hold it).
4. The body sniff for ``studio-state`` (``kanban.db``, ``~/.hermes/state/``, ``state/x.jsonl`` ...)
   no longer establishes it: only an explicit ``requires: studio-state`` line (or a skill) does.
5. Every Studio placement record carries ``studio_capability`` = ``{capability, source, evidence}``.

Kill switch: ``kanban.placement.studio_capability_only: false`` in the ROOT config.yaml restores the
behaviour before this card. The rule is also off whenever the host_preference switch (t_c2fce87e)
does not make the Studio a last-resort host, and while the capability map is unreadable (the
vocabulary is then unknown: nothing moves on a broken file, the same rule as studio_pin_reasons).
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Mapping, Optional

KNOB = "studio_capability_only"
WAIT_PREFIX = "studio_capability_only"
# Body-sniffed capabilities that no longer establish a Studio placement on their own (rule 4).
BODY_TIGHTENED = frozenset({"studio-state"})
# CARD_NEEDS name -> the registry capability it stands for (recorded, not enforced, here).
NEED_CAPS = {
    "vault": "obsidian-vault", "skills_tree": "control-plane", "studio_relay": "studio-state",
    "sub_ssh": "fleet-ssh", "studio_launchd": "control-plane", "studio_tree": "studio-state",
}
# portable() rules that are facts only the Studio serves, not files or a checkout.
_STRUCTURAL = ("profile", "native_command", "host_reach", "linked", "workspace_of", "workspace_shared")


def knob_on(kcfg: Optional[Mapping]) -> bool:
    """``kanban.placement.studio_capability_only`` (default on). False/"off"/0 is the kill switch."""
    placement = (kcfg or {}).get("placement") if isinstance(kcfg, Mapping) else None
    val = placement.get(KNOB, True) if isinstance(placement, Mapping) else True
    if isinstance(val, str):
        return val.strip().lower() not in ("off", "false", "no", "0", "")
    return bool(val)


def active(kcfg: Optional[Mapping], last_resort: bool) -> bool:
    return bool(last_resort) and knob_on(kcfg)


def studio_only(cmap) -> Optional[frozenset]:
    """The Studio row minus every other host's row, plus ``control-plane-read``; None = map unreadable."""
    if cmap is None or not getattr(cmap, "valid", False):
        return None
    from hermes_cli import kanban_host_capabilities as _khc
    from hermes_cli import kanban_placement_reasons as _pr
    hosts = getattr(cmap, "hosts", None) or {}
    studio = hosts.get(_khc.STUDIO_ROW)
    if not studio:
        return None
    pool = frozenset().union(*(frozenset(v) for h, v in hosts.items() if h != _khc.STUDIO_ROW))
    return (frozenset(studio) - pool) | {_pr.ACCESS_CONTROL_PLANE_READ}


def reasons_for(cmap, on: bool) -> Optional[frozenset]:
    """The ``requires:`` words that justify ``host: studio``: Studio-only ones when the rule is on."""
    from hermes_cli import kanban_worker_pool as _kwp
    return studio_only(cmap) if on else _kwp.studio_map_reasons(cmap)


def create_reasons(cmap) -> Optional[frozenset]:
    """:func:`reasons_for` for ``kanban create`` (no tick): reads the root config and the policy once."""
    try:
        from hermes_cli import kanban_db as _kb
        from hermes_cli import kanban_worker_pool as _kwp
        from hermes_cli import allocation_policy as _ppol
        last = _ppol.load(_kb.kanban_home() / "fleet").is_last_resort(_kwp.STUDIO_HOST)
        on = active(_kb._board_home_kanban_cfg(), last)
    except Exception:
        on = False
    return reasons_for(cmap, on)


def tighten(verdict):
    """``verdict`` without the body-sniffed requirements of :data:`BODY_TIGHTENED` (rule 4); they move to
    ``body_ignored`` so the tick still records them."""
    from hermes_cli import kanban_host_capabilities as _khc
    drop = tuple(r for r in verdict.enforced if r.matched_by == _khc.BODY and r.cap in BODY_TIGHTENED)
    if not drop:
        return verdict
    import dataclasses
    keep = tuple(r for r in verdict.enforced if r not in drop)
    hard = _khc._first([r for r in keep if r.strength == _khc.HARD])
    soft = _khc._first([r for r in keep if r.strength == _khc.SOFT])
    return dataclasses.replace(verdict, enforced=keep, hard_rule=hard.rule if hard else None,
                               soft_rule=soft.rule if soft else None, pin=hard or soft,
                               body_ignored=tuple(verdict.body_ignored) + drop)


def _rec(capability, source, evidence, kind="capability") -> dict:
    return {"capability": capability, "source": source, "evidence": evidence, "kind": kind}


def studio_capability(requires, skills, cmap) -> Optional[dict]:
    """The consumer-neutral decision (operator 2026-11-10 16:5x): given a unit of work's DECLARED requirements
    (``requires:`` words) and skills, the Studio-only capability that justifies the Studio, or None. No kanban
    inputs: the allocator, CI and Prism can call this as is. Returns ``{capability, source, evidence, kind}``."""
    from hermes_cli import kanban_host_capabilities as _khc
    only = studio_only(cmap) or frozenset()
    words = [w for w in (requires or ()) if w in only]
    if words:
        return _rec(words[0], "declared", "requires: " + ", ".join(words))
    index = getattr(cmap, "index", None) or {}
    for s in skills or ():
        hit = [c for c in index.get(_khc.skill_key(s), ()) if c in only]
        if hit:
            return _rec(hit[0], "skill", s)
    return None


def justify(*, rule: Optional[str], affinity: Optional[Mapping], body: Optional[str], skills,
            assignee: Optional[str], cmap) -> Optional[dict]:
    """What established a ``local`` route, or None when nothing a pool host lacks did."""
    from hermes_cli import kanban_host_capabilities as _khc
    from hermes_cli import kanban_worker_pool as _kwp
    rule = rule or ""
    aff = affinity or {}
    declared = _khc.declared_words(body)
    if rule.startswith("access_bound:"):
        cap = rule.split(":", 1)[1]
        return _rec(cap, "declared" if cap in declared else "body", cap)
    if rule.startswith("requires:"):
        cap = rule[len("requires:"):]
        if cap.startswith(_khc.UNKNOWN_PREFIX):
            return None
        return _rec(cap, aff.get("matched_by") or "declared", aff.get("term") or cap)
    if rule == _kwp.STUDIO_NEEDS_RULE:
        needs = sorted(_kwp.card_needs(body))
        caps = sorted({NEED_CAPS.get(n, n) for n in needs})
        return _rec(",".join(caps) or None, "body", ",".join(needs)) if needs else None
    if rule in ("studio_bound_skill", "studio_manifest"):
        return _rec("control-plane", "skill", aff.get("matched_by") or rule)
    if rule == "studio_body":
        return _rec("browser-session", "body", aff.get("matched_by") or rule)
    if rule == "host_studio":
        return studio_capability(declared, skills, cmap)
    if rule.startswith(_STRUCTURAL):
        return _rec(rule, "assignee" if rule in ("profile", "native_command") else "card",
                    assignee if rule in ("profile", "native_command") else rule, kind="structural")
    return None   # local_files, workspace_kind, or a rule this map cannot name


def dirty_paths(task_id: str, workspace_path: Optional[str], board_default: Optional[str]) -> Optional[int]:
    """Uncommitted + untracked paths in the card's own Studio worktree, or None when not readable."""
    raw = (workspace_path or "").strip() or (board_default or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    for tree in (p, p / ".worktrees" / task_id):
        if tree.name == task_id and tree.parent.name == ".worktrees" and tree.is_dir():
            try:
                proc = subprocess.run(["git", "-C", str(tree), "status", "--porcelain", "--untracked-files=all"],
                                      capture_output=True, text=True, encoding="utf-8", timeout=10,
                                      stdin=subprocess.DEVNULL)
            except (OSError, subprocess.SubprocessError):
                return None
            return len([ln for ln in proc.stdout.splitlines() if ln.strip()]) if proc.returncode == 0 else None
    return None


def wait_reason(rule: Optional[str], local_reason: Optional[str], *, task_id: str,
                workspace_path: Optional[str] = None, board_default: Optional[str] = None) -> str:
    """``studio_capability_only:<rule> (<what the local route said>[, N dirty paths])``."""
    detail = [local_reason] if local_reason else []
    if local_reason and "unpushed_dirty_tree" in local_reason:
        n = dirty_paths(task_id, workspace_path, board_default)
        if n is not None:
            detail.append(f"{n} dirty paths")
    return f"{WAIT_PREFIX}:{rule or 'local'}" + (f" ({', '.join(detail)})" if detail else "")
