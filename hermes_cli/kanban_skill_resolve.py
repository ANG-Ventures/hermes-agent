"""Resolve kanban card skills against the ASSIGNEE profile's skill dirs.

The dispatcher spawns ``hermes -p <assignee> --skills <name> ...``. The worker
resolves each name through ``skill_view`` against its OWN home:
``<home>/skills`` plus ``skills.external_dirs`` from ``<home>/config.yaml``.
When every requested skill is missing the worker exits 1 at startup
(``Error: Unknown skill(s): ...``), the dispatcher counts a crash, retries,
and gives up -- three wasted spawns for a config gap that was knowable before
the first one (2026-10-03, card t_0b786d9b: ``power-outage-recovery`` and
``ups-nut-fleet`` live in an external dir the assignee did not list). When at
least one skill loads the worker only logs ``Unknown skill(s) requested,
skipping`` and runs without the rest (``cli.py`` /
``tui_gateway/server.py``); the dispatcher mirrors that split -- it blocks
only the card that would crash and comments on the card that would run
degraded -- so no card is blocked that the worker would have run.

This module answers, without importing the profile's process-global config:
which card skills does profile X fail to resolve, and where do they live
instead? Matching mirrors ``tools.skills_tool.skill_view``: a direct
``<dir>/<name>/SKILL.md`` (or ``<dir>/<name>.md``), a nested ``SKILL.md`` whose
parent dir is ``<name>``, or a ``SKILL.md`` whose frontmatter ``name`` is
``<name>``, walking with ``iter_skill_index_files`` so ``.archive``/``.hub`` and
support dirs are excluded exactly as the worker excludes them.

Everything here fails OPEN: an identifier we cannot judge (plugin-qualified
``ns:skill``, absolute path) or any error is reported as resolvable, because a
false refusal blocks real work while a missed one only costs the old crash.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Iterable, Optional

_log = logging.getLogger(__name__)


_VAR_RE = re.compile(r"\$(\w+)|\$\{([^}]+)\}")


def _expandvars_as_worker(value: str, home: Path) -> str:
    """``os.path.expandvars`` as the WORKER sees it, without touching os.environ.

    ``_default_spawn`` sets the child's HERMES_HOME to the profile home before the
    worker reads its config, so ``${HERMES_HOME}`` in a profile's external_dirs must
    expand to that profile, not to the dispatcher's own home. Unknown variables
    are left as written, like ``os.path.expandvars``.
    """
    env = dict(os.environ)
    env["HERMES_HOME"] = str(home)

    def _sub(m: "re.Match[str]") -> str:
        key = m.group(1) or m.group(2)
        return env.get(key, m.group(0))

    return _VAR_RE.sub(_sub, value)


def _read_external_dirs(home: Path) -> list[Path]:
    """``skills.external_dirs`` of ``<home>/config.yaml``, expanded like the CLI.

    Same expansion as ``agent.skill_utils.get_external_skills_dirs``: ``~`` and
    ``${VAR}``, relative entries resolved against the home, missing dirs
    dropped. Returns ``[]`` when there is no readable config.
    """
    cfg_path = home / "config.yaml"
    if not cfg_path.is_file():
        return []
    import yaml  # local: the dispatcher hot path never pays for it otherwise

    try:
        parsed = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:
        _log.debug("kanban skill resolve: unreadable %s", cfg_path, exc_info=True)
        return []
    skills_cfg = parsed.get("skills") if isinstance(parsed, dict) else None
    raw = skills_cfg.get("external_dirs") if isinstance(skills_cfg, dict) else None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out: list[Path] = []
    for entry in raw:
        entry = str(entry or "").strip()
        if not entry:
            continue
        p = Path(os.path.expanduser(_expandvars_as_worker(entry, home)))
        if not p.is_absolute():
            p = home / p
        try:
            p = p.resolve()
        except OSError:
            continue
        if p.is_dir() and p not in out:
            out.append(p)
    return out


def profile_skill_dirs(home: Path) -> list[Path]:
    """Skill roots a worker running under ``home`` searches (local first)."""
    dirs: list[Path] = []
    local = home / "skills"
    if local.is_dir():
        dirs.append(local)
    for ext in _read_external_dirs(home):
        if ext not in dirs:
            dirs.append(ext)
    return dirs


def _judgeable(name: str) -> bool:
    """Only bare/relative names are checked; anything else fails open."""
    if not name or ":" in name:
        return False  # plugin-qualified skills resolve through plugins
    return not Path(name).expanduser().is_absolute()


def skill_resolves(name: str, dirs: Iterable[Path]) -> bool:
    """True when ``name`` resolves in any of ``dirs`` the way skill_view does."""
    from agent.skill_utils import (
        is_skill_support_path,
        iter_skill_index_files,
        parse_frontmatter,
    )

    dirs = list(dirs)
    for d in dirs:
        direct = d / name
        if not is_skill_support_path(direct) and (direct / "SKILL.md").is_file():
            return True
        flat = direct.with_suffix(".md")
        if flat.is_file() and not is_skill_support_path(flat):
            return True
    leaf = Path(name).name
    for d in dirs:
        for skill_md in iter_skill_index_files(d, "SKILL.md"):
            if skill_md.parent.name in (name, leaf):
                return True
            try:
                fm, _ = parse_frontmatter(
                    skill_md.read_text(encoding="utf-8-sig", errors="replace")
                )
            except Exception:
                continue
            if isinstance(fm, dict) and fm.get("name") == name:
                return True
    # Legacy flat ``<name>.md`` anywhere below a root (skill_view strategy 3),
    # with the same support-path exclusion.
    for d in dirs:
        try:
            for found in d.rglob(f"{leaf}.md"):
                if found.name != "SKILL.md" and not is_skill_support_path(found):
                    return True
        except OSError:
            continue
    return False


def unresolved_skills(
    names: Iterable[str],
    home: Path,
    *,
    extra_dirs: Iterable[Path] = (),
) -> list[str]:
    """Card skill names the worker under ``home`` will NOT be able to load.

    ``extra_dirs`` are additional roots the worker may also see (e.g. a
    project-local ``.hermes/skills`` in a worktree workspace); including them
    can only shrink the result, never grow it.
    """
    dirs = profile_skill_dirs(home)
    for d in extra_dirs:
        if d.is_dir() and d not in dirs:
            dirs.append(d)
    missing: list[str] = []
    for raw in names:
        name = str(raw or "").strip()
        if not _judgeable(name) or name in missing:
            continue
        try:
            if not skill_resolves(name, dirs):
                missing.append(name)
        except Exception:
            _log.debug("kanban skill resolve: %s failed open", name, exc_info=True)
    return missing


def candidate_roots(home: Path, root_home: Optional[Path]) -> list[Path]:
    """Roots to search for a hint about WHERE a missing skill lives.

    The default (root) home's skill dirs, plus every sibling of the profile's
    and root's external dirs -- so a fleet that keeps one category per
    directory under a shared parent (``skills-shared/<category>``) gets the
    exact directory to add, without this module knowing that layout.
    """
    seen: list[Path] = []

    def _add(p: Path) -> None:
        if p.is_dir() and p not in seen:
            seen.append(p)

    homes = [home] + ([root_home] if root_home is not None else [])
    externals: list[Path] = []
    for h in homes:
        for d in profile_skill_dirs(h):
            _add(d)
        externals.extend(_read_external_dirs(h))
    for ext in externals:
        try:
            for sib in sorted(ext.parent.iterdir()):
                if sib.is_dir() and not sib.name.startswith("."):
                    _add(sib)
        except OSError:
            continue
    return seen


def locate_skill(name: str, roots: Iterable[Path]) -> list[Path]:
    """The roots (not skill dirs) under which ``name`` resolves."""
    hits: list[Path] = []
    for root in roots:
        try:
            if skill_resolves(name, [root]):
                hits.append(root)
        except Exception:
            continue
    return hits


def _unresolved_summary(
    assignee: str,
    home: Path,
    missing: list[str],
    root_home: Optional[Path],
) -> str:
    """``card skill(s) unresolvable for assignee ...: <name> (lives in ...)``."""
    roots = [r for r in candidate_roots(home, root_home)
             if r not in profile_skill_dirs(home)]
    parts: list[str] = []
    for name in missing:
        found = locate_skill(name, roots)
        if found:
            where = ", ".join(str(p) for p in found)
            parts.append(f"{name} (lives in {where}; add it to skills.external_dirs)")
        else:
            parts.append(f"{name} (not found in any known skills dir)")
    return (
        f"card skill(s) unresolvable for assignee {assignee!r} "
        f"({home / 'config.yaml'}): " + "; ".join(parts)
    )


def refusal_reason(
    assignee: str,
    home: Path,
    missing: list[str],
    root_home: Optional[Path],
) -> str:
    """One-line block reason for a card whose worker would crash at startup.

    Only correct when EVERY skill the worker would be given is missing; for
    the partial case use :func:`degraded_reason`.
    """
    return (
        _unresolved_summary(assignee, home, missing, root_home)
        + ". The worker would exit 'Unknown skill(s)' at startup. Fix the "
        "profile's skills.external_dirs (or the card's skills), then unblock."
    )


def degraded_reason(
    assignee: str,
    home: Path,
    missing: list[str],
    root_home: Optional[Path],
    loaded: list[str],
) -> str:
    """One-line card comment for a worker that runs WITHOUT some card skills.

    The CLI skips unknown skills when at least one requested skill loads, so
    the card is dispatched; this names what the worker is running without and
    where it lives, which the worker's own log line never surfaces.
    """
    return (
        _unresolved_summary(assignee, home, missing, root_home)
        + ". The worker runs WITHOUT them (it loads " + ", ".join(loaded)
        + " and skips unknown skills). Fix the profile's skills.external_dirs "
        "(or the card's skills) before the next dispatch to load them."
    )
