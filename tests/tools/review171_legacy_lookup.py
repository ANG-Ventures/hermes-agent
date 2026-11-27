from pathlib import Path
from typing import Optional, Dict, Any
def _find_skill(name: str) -> Optional[Dict[str, Any]]:
    """Find a skill (local skills dir, then skills.external_dirs) -> ``{"path": Path}`` | None.

    Accepts the bare dir name (``axolotl``; matches category-nested skills too) and the
    categorized relative path (``mlops/axolotl``) — the two forms skill_view resolves. The
    categorized form matches RELATIVE to the local root only (relative_to raises for external dirs)."""
    from agent.skill_utils import get_all_skills_dirs
    local_root = None
    if "/" in name or "\\" in name:
        try:
            local_root = _skills_dir().resolve()
        except OSError:
            logger.debug(
                "skills dir resolve failed; categorized lookups fall back to the unresolved path",
                exc_info=True)
            local_root = _skills_dir()
    for skills_dir in get_all_skills_dirs():
        if not skills_dir.exists():
            continue
        for skill_dir in _iter_skill_dirs(skills_dir):
            if skill_dir.name == name:
                return {"path": skill_dir}
            if local_root is not None:
                resolved = skill_dir.resolve()
                if (resolved.is_relative_to(local_root)
                        and resolved.relative_to(local_root).as_posix() == name):  # POSIX form
                    return {"path": skill_dir}
    return None