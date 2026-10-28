"""Write/delete guards for ``skill_manage``. Every guard returns ``None`` when the
operation may proceed, else a refusal (error dict or message). Origin-owned state
(``_find_skill``, ``_skills_dir``) is reached lazily via ``tools.skill_manager_tool``
so test patches keep working."""

import contextvars as _ctxvars
import logging
import os
import re
import threading
from contextlib import suppress
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("tools.skill_manager_tool")


def _refusal(message: str, **extra: Any) -> Dict[str, Any]:
    return {"success": False, "error": message, **extra}


def _is_background_review() -> bool:
    """True inside the autonomous curator review fork; False on any lookup failure."""
    try:
        from tools.skill_provenance import is_background_review
        return bool(is_background_review())
    except Exception:
        return False


def _resolved_str(path: Path) -> str:
    with suppress(Exception):
        return str(path.resolve())
    return str(path)


class _BackgroundReviewReadMarks:
    """Read marks shared by copied tool contexts within one review run."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._paths: set[str] = set()

    def add(self, path: str) -> None:
        with self._lock:
            self._paths.add(path)

    def contains(self, path: str) -> bool:
        with self._lock:
            return path in self._paths


_background_review_read_paths: "_ctxvars.ContextVar[Optional[_BackgroundReviewReadMarks]]" = (
    _ctxvars.ContextVar("background_review_read_paths", default=None))


def mark_background_review_skill_read(path: Path) -> None:
    """Record that the active background-review fork has read a skill file. The fork must not
    patch content it only inferred from the transcript: skill_view/read_file call this, and
    the write guards require the mark."""
    if not _is_background_review():
        return
    if (marks := _background_review_read_paths.get()) is None:
        _background_review_read_paths.set(marks := _BackgroundReviewReadMarks())
    marks.add(_resolved_str(path))


def _background_review_has_read(path: Path) -> bool:
    marks = _background_review_read_paths.get()
    return marks is not None and marks.contains(_resolved_str(path))


def _reset_background_review_read_marks() -> None:
    """Start a fresh, isolated read set for the current review context."""
    _background_review_read_paths.set(_BackgroundReviewReadMarks())


def _resolved_roots(skill_path: Path):
    """``(resolved skill_path, [(root, resolved_root), ...])`` over every resolvable skills root."""
    from agent.skill_utils import get_all_skills_dirs
    try:
        resolved = skill_path.resolve()
    except OSError:
        resolved = skill_path
    roots = []
    for root in get_all_skills_dirs():
        with suppress(OSError):
            roots.append((root, root.resolve()))
    return resolved, roots


def _containing_skills_root(skill_path: Path) -> Path:
    """Skills root (local or external_dirs) containing ``skill_path``; local dir if none match."""
    from tools import skill_manager_tool as _smt
    resolved, roots = _resolved_roots(skill_path)
    return next((root for root, r in roots if resolved.is_relative_to(r)), _smt._skills_dir())


def _is_path_redirect(path: Path) -> bool:
    """Symlink or (Windows 3.12+) junction — either lets a poisoned tree redirect rmtree outside."""
    try:
        return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())
    except OSError:
        return False


def _validate_delete_target(skill_dir: Path) -> Optional[str]:
    """Last-line guard before rmtree: even a poisoned tree must never delete (1) a path outside
    every known skills root, (2) a skills root itself, (3) a symlink/junction (rmtree follows it).

    ``_find_skill`` already restricts ``skill_dir`` to a real ``SKILL.md`` parent discovered by walking the
    skills roots, so the agent cannot inject an arbitrary path the way Kilo Code's HTTP endpoint could
    (their issue 11227: a built-in-skill sentinel resolved to the server cwd and a recursive delete wiped
    the user's entire working directory). This is the matching defense-in-depth for our agent-facing
    ``skill_manage`` delete path: even if discovery or a poisoned tree hands us a bad directory, never
    recursively delete See #11227.
    """
    if _is_path_redirect(skill_dir):
        return (f"Refusing to delete '{skill_dir}': the skill directory is a "
                f"symlink/junction. Remove the link target manually if intended.")
    try:
        skill_dir.resolve()
    except OSError as exc:
        return f"Refusing to delete '{skill_dir}': could not resolve path ({exc})."
    resolved, roots = _resolved_roots(skill_dir)
    for _root, root in roots:
        if resolved == root:
            return (f"Refusing to delete '{skill_dir}': resolves to the skills root "
                    f"itself, which would remove every installed skill.")
        if resolved.is_relative_to(root):
            return None
    return f"Refusing to delete '{skill_dir}': path does not resolve inside any known skills root."


def _is_pinned(name: str, what: str) -> Optional[bool]:
    """skill_usage pinned flag; None (logged at debug) when the record is unreadable."""
    try:
        from tools import skill_usage
        return bool(skill_usage.get_record(name).get("pinned"))
    except Exception:
        logger.debug("%s lookup failed for %s", what, name, exc_info=True)
        return None


def _pinned_guard(name: str) -> Optional[str]:
    """Refusal message if *name* is pinned or essential, else None. Pin only guards DELETION;
    patches/edits stay allowed. ESSENTIAL_SKILLS are permanently pinned (the system prompt
    references them). Best-effort: an unreadable sidecar lets the delete through."""
    try:
        from agent.skill_utils import ESSENTIAL_SKILLS
        if name in ESSENTIAL_SKILLS:
            return (
                f"Skill '{name}' is essential to Hermes (the agent's own "
                f"operating manual referenced by the system prompt) and "
                f"cannot be deleted. Patches and edits are still allowed.")
    except Exception:
        logger.debug("essential-guard lookup failed for %s", name, exc_info=True)
    if _is_pinned(name, "pinned-guard"):
        return (
            f"Skill '{name}' is pinned and cannot be deleted by skill_manage. Ask the user to "
            f"run `hermes curator unpin {name}` if they want to delete it. Patches and edits "
            f"are allowed on pinned skills; only deletion is blocked.")
    return None


def _harness_manifest_block(name: str) -> Optional[str]:
    """Why *name* must be treated as pinned by the review fork, else None: it is listed in
    skills-harness-manifests, or a manifest is unreadable (fail closed). A lookup error also fails closed."""
    try:
        from agent.curator import _read_harness_manifests
        listed, unreadable = _read_harness_manifests()
    except Exception:
        logger.warning("harness-manifest guard lookup failed for %s", name, exc_info=True)
        return "skills-harness-manifests could not be read"
    if name in listed:
        return ("it is listed in skills-harness-manifests, and skills-harness-sync fails on a listed "
                "name that no longer resolves. Treat it as pinned")
    if unreadable:
        return "skills-harness-manifests has unreadable file(s) (%s)" % ", ".join(unreadable)
    return None


def _background_review_write_guard(
    name: str, skill_dir: Path, action: str) -> Optional[Dict[str, Any]]:
    """Refuse autonomous curator writes to anything but curator-owned sediment. The review fork
    has no user in the loop, so it is also blocked on pinned/manifest-listed/external/bundled/hub skills."""
    if not _is_background_review():
        return None
    refuse = f"Refusing background curator {action} for"
    if _is_pinned(name, "pinned skill guard"):
        return _refusal(
            f"{refuse} pinned skill '{name}': pinned skills "
            f"are off-limits to autonomous maintenance. Ask the user to run `hermes curator "
            f"unpin {name}` if they want it changed.")
    if (why := _harness_manifest_block(name)) is not None:
        return _refusal(
            f"{refuse} skill '{name}': {why}. Manifest-listed skills are pinned and off-limits to "
            f"autonomous maintenance.", _fail_closed=True)
    try:
        from agent.skill_utils import is_external_skill_path, is_shared_curatable_path
        # Fork: a shared skills tree the operator opted INTO curation (skills.shared scope) is
        # curator-owned sediment even though it is listed under external_dirs.
        if is_external_skill_path(skill_dir) and not is_shared_curatable_path(skill_dir):
            return _refusal(
                f"{refuse} skill '{name}': the skill lives in skills.external_dirs, which are "
                f"externally owned and read-only to autonomous curation.")
    except Exception:
        logger.debug("external skill guard lookup failed for %s", name, exc_info=True)
    try:
        from tools import skill_usage
        for predicate, label in (
            (skill_usage.is_protected_builtin, "protected built-in"),
            (skill_usage.is_hub_installed, "hub-installed"),
            (skill_usage.is_bundled, "bundled")):
            if predicate(name):
                return _refusal(f"{refuse} {label} skill '{name}'.")
        # Fork: an in-scope shared skill is curatable by configuration (skills.shared), not by a
        # per-skill `created_by: "agent"` record — the ownership check below does not apply to it.
        try:
            from agent.skill_utils import is_shared_curatable_path
            if is_shared_curatable_path(skill_dir):
                return None
        except Exception:
            logger.debug("shared-curatable check failed for %s", name, exc_info=True)
        # Not curator-managed (no `created_by: "agent"`) => user-owned. A MISSING
        # record and an explicit `created_by: null` must resolve IDENTICALLY (keying
        # on presence made the policy depend on the guard's own side effect: the
        # first write created a null record, the next identical write was refused).
        usage_rec = skill_usage.load_usage().get(name)
        # Skills that are not curator-managed are off-limits to autonomous curation. This prevents the LLM
        # consolidation pass from mutating skills the user owns (manually authored, URL-installed, or
        # created by a foreground `skill_manage(create)` at the user's request), which lack the `created_by:
        # "agent"` marker. Keying on `isinstance(usage_rec, dict)` made the policy depend on the guard's own
        # side effect: a local skill with no telemetry record passed, the successful write called
        # bump_patch() which created a `created_by: null` record, and the very same write was refused from
        # then on. "Allowed exactly once" is not a policy — it is a race with our own bookkeeping. Fail
        # closed for both shapes; `hermes curator adopt <name>` is the supported way in. See #67140.
        if not skill_usage._is_curator_managed_record(usage_rec):
            _detail = (f"created_by={usage_rec.get('created_by')!r}" if isinstance(usage_rec, dict)
                       else "no usage record")
            return _refusal(
                f"{refuse} skill '{name}': the skill is not "
                f"curator-managed ({_detail}). User-owned skills are off-limits to autonomous "
                f"curation. Run `hermes curator adopt {name}` to opt it in.")
    except Exception:
        logger.warning("owned skill guard lookup failed for %s", name, exc_info=True)
        return _refusal(
            f"{refuse} skill '{name}': agent ownership could not "
            f"be verified because the provenance record is unavailable or unreadable.")
    return None


def _background_review_read_before_write_guard(
    name: str, target: Path, action: str, file_label: str) -> Optional[Dict[str, Any]]:
    """Require review forks to load the exact target before mutating it."""
    if not _is_background_review() or _background_review_has_read(target):
        return None
    return _refusal(
        f"Refusing background curator {action} for skill '{name}': the current {file_label} "
        f"content has not been loaded in this review turn. Call skill_view(name) for SKILL.md, or "
        f"skill_view(name, file_path=...) for a supporting file, then retry the write using the "
        f"content just returned.",
        _read_before_write_required=True)


_CARRY_MIN_LINE = 16  # shorter normalized lines ("## Usage", rules, fences) are not distinctive
_CARRY_MAX_BYTES = 2_000_000
_CARRY_SHOWN = 5
_MD_HEADING = re.compile(r"#{1,6} +")


def _carry_lines(data: bytes) -> set:
    # Stray non-UTF-8 bytes (one cp1252 quote) must not exempt prose from the check: decode with
    # replacement, identically on both sides. A heading is a line like any other (a constraint
    # stated only as a heading is still content); its level is dropped so a fold that demotes it
    # still carries it.
    text = data.decode("utf-8", errors="replace").lstrip("\ufeff")
    lines = (_MD_HEADING.sub("", " ".join(raw.split()), count=1) for raw in text.splitlines())
    return {ln for ln in lines if len(ln) >= _CARRY_MIN_LINE and sum(c.isalnum() for c in ln) >= 8}


def _carry_candidates(target: Path):
    """Every other file under the skills roots, same-basename files first (the usual move)."""
    from agent.skill_utils import get_all_skills_dirs
    seen, same, rest = {_resolved_str(target)}, [], []
    for root in get_all_skills_dirs():
        # Prune while walking: .archive/.curator_backups hold most of the bytes and are not live.
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames
                                 if not d.startswith(".") and d not in {"__pycache__", "node_modules"})
            for fname in sorted(filenames):
                path = Path(dirpath) / fname
                if fname.startswith(".") or (key := _resolved_str(path)) in seen:
                    continue
                seen.add(key)
                (same if fname == target.name else rest).append(path)
    return same + rest


def _read_carrier(path: Path) -> Optional[bytes]:
    """A carrier's bytes, or None when it cannot count (unreadable or too big): fails closed."""
    with suppress(OSError):
        if path.stat().st_size <= _CARRY_MAX_BYTES:
            return path.read_bytes()
    return None


def _uncarried_lines(target: Path) -> list:
    """Why removing *target* would lose content: its distinctive lines found in no other file under
    the skills roots, or one named ``<...>`` reason when that cannot be verified. Empty = carried.
    Every failure to inspect the source refuses; nothing here may read as "nothing uncarried"."""
    try:
        if target.stat().st_size > _CARRY_MAX_BYTES:
            return [f"<file exceeds the {_CARRY_MAX_BYTES}-byte carry check; move it whole>"]
        data = target.read_bytes()
    except OSError as e:
        return [f"<source could not be read for the carry check ({type(e).__name__}: {e}); refusing>"]
    if b"\0" in data:  # provably binary: no prose lines, so only a byte-identical copy carries it
        for other in _carry_candidates(target):
            if _read_carrier(other) == data:
                return []
        return ["<binary file with no byte-identical copy under the skills roots; move it whole>"]
    missing = _carry_lines(data)
    for other in _carry_candidates(target):
        if not missing:
            break
        if (other_data := _read_carrier(other)) is not None:
            missing -= _carry_lines(other_data)
    return sorted(missing)


def _curator_remove_file_carry_guard(
    name: str, skill_dir: Path, target: Path, file_path: str) -> Optional[Dict[str, Any]]:
    """Background curator remove_file only after the file's content is carried elsewhere.

    A fold that condenses a reference and then removes the original deletes every line the
    condensation dropped (10 files on one weekly run). Refuse unless every distinctive line exists
    in another file under the skills roots (a byte-identical move passes trivially), and refuse
    while the skill's own SKILL.md still links the path (the stub would dangle). Foreground,
    user-directed removes are not gated."""
    if not _is_background_review():
        return None
    skill_md = skill_dir / "SKILL.md"
    # Own-relative link only (`references/x.md`, `./references/x.md`); a repointed
    # `../umbrella/references/x.md` names the new home and must not match.
    own_link = re.compile(r"(?<![\w/.-])(?:\./)?" + re.escape(file_path) + r"(?![\w/.-])")
    with suppress(OSError):
        if own_link.search(skill_md.read_bytes().decode("utf-8", errors="replace")):
            return _refusal(
                f"Refusing background curator remove_file of '{file_path}' in skill '{name}': "
                f"SKILL.md still links it. Repoint the link to the file's new home first, then retry.",
                _content_not_carried=True)
    if not (missing := _uncarried_lines(target)):
        return None
    shown = "\n".join(f"  - {ln[:160]}" for ln in missing[:_CARRY_SHOWN])
    return _refusal(
        f"Refusing background curator remove_file of '{file_path}' in skill '{name}': "
        f"{len(missing)} distinctive line(s) exist in no other skill file, so removing it loses "
        f"content. Move the file WHOLE instead: write_file it byte-identical into the umbrella's "
        f"references/ (same content, any topical name), repoint every link to the new path, then "
        f"retry remove_file. Uncarried lines include:\n{shown}",
        _content_not_carried=True, uncarried_line_count=len(missing))


def _background_review_preflight(action: str, name: str) -> Optional[Dict[str, Any]]:
    if action not in {"edit", "patch", "delete", "write_file", "remove_file"}:
        return None
    from tools import skill_manager_tool as _smt
    existing = _smt._find_skill(name)
    return _background_review_write_guard(name, existing["path"], action) if existing else None


def _curator_consolidation_delete_guard(
    name: str, absorbed_into: Optional[str]) -> Optional[Dict[str, Any]]:
    """Fail closed on unverified deletes during the curator consolidation pass. The fork's only
    legitimate delete is a consolidation declared via ``absorbed_into=<umbrella>`` (existence
    validated in ``_delete_skill``); the deterministic inactivity prune never calls skill_manage,
    so a bare delete here can only be the LLM pass pruning without evidence.

    A delete with no forwarding target — ``absorbed_into`` omitted (``None``) or empty (``""``) — is the
    fail-open behavior reported in #29912: the consolidation pass archived whole clusters of active skills
    with zero verified consolidations (``consolidated_this_run == 0``), leaving active automations pointing
    at names that no longer resolve. Refuse it; keep the skill active.
    """
    if not _is_background_review():
        return None
    if (why := _harness_manifest_block(name)) is not None:
        return _refusal(
            f"Refusing background curator delete of skill '{name}': {why}. Keeping '{name}' active.",
            _fail_closed=True)
    if isinstance(absorbed_into, str) and absorbed_into.strip():
        return None
    return _refusal(
        f"Refusing background curator delete of skill '{name}': the consolidation pass may only "
        f"archive a skill it has absorbed into an umbrella. Pass absorbed_into=<umbrella> (the "
        f"umbrella must already exist) to record a verified consolidation. Pruning a skill with no "
        f"forwarding target is not permitted here — the deterministic inactivity prune handles "
        f"staleness archival separately. Keeping '{name}' active.",
        _fail_closed=True)


def _is_org_mirror(skill_path: Path) -> bool:
    from agent.skill_utils import is_org_mirror_path
    from tools import skill_manager_tool as _smt
    return is_org_mirror_path(skill_path, _smt._skills_dir())


def _maybe_auto_propose_org_edit(name: str, skill_path: Path) -> Optional[str]:
    """Submit an org-skill edit upstream when `sync.org_auto_propose` is on. Returns a note for
    the tool result or None; never raises (the edit is saved locally and can be proposed later)."""
    try:
        from tools import skills_sync_client as ssc
        if not _is_org_mirror(skill_path):
            return None
        if not ssc.sync_org_auto_propose():
            return (
                f"This skill is shared by your organisation. Your edit is "
                f"saved locally and will not be overwritten by org updates. "
                f"Run `hermes sync propose {name}` to share it back.")
        from tools.skills_sync_client_org import propose_skill
        result = propose_skill(name)
        if result.get("proposal_pending"):
            return (
                f"Auto-proposed to your organisation as proposal "
                f"#{result.get('proposal_id')} (pending admin review).")
        return "Auto-proposed to your organisation (merged into the shared set)."
    except Exception as e:
        logger.debug("auto-propose skipped for %s: %s", name, e)
        return (
            f"Edit saved locally. Could not submit it to your organisation "
            f"right now — run `hermes sync propose {name}` to retry.")


def _org_mirror_write_guard(name: str, skill_path: Path, action: str) -> Optional[Dict[str, Any]]:
    """Org-shared skills are EDITABLE IN PLACE — this only blocks deletion. Edits land in the
    mirror, survive the next org pull (baseline sidecar in skills_sync_client) and reach the org
    via `hermes sync propose`. Deletion stays refused: the mirror is a view of org HEAD, so a
    local delete just comes back, and removing for everyone is an admin action."""
    if action not in {"delete", "remove_file"}:
        return None
    try:
        if _is_org_mirror(skill_path):
            return _refusal(
                f"Cannot {action} '{name}' locally: it is shared by your organisation, so a local "
                f"delete would just come back on the next sync. Ask an org admin to remove it for "
                f"everyone. (Editing it IS allowed — your changes are kept and can be proposed "
                f"back with `hermes sync propose {name}`.)")
    except Exception:
        logger.debug("org mirror guard lookup failed for %s", name, exc_info=True)
    return None
