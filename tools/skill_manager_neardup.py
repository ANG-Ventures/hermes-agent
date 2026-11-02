"""Near-duplicate gate for skill_manage create (fork t_f78aa714).

A create whose SKILL.md scores at or above ``skills.neardup_gate.threshold`` against an existing skill
is refused with the top matches, unless the call carries ``justification``; a justified create records
``metadata.hermes.distinct_from`` in the new skill's frontmatter. Refusals and justified creates each
append one row to ``<root>/state/skill-neardup-ledger.jsonl``.

The scorer is the Hermes root's ``scripts/lib/skill_similarity.py`` (ONE scorer, shared with the
root's promotion sweep), loaded by path like ``_load_tier_module``. A root without it (a vanilla
install) has no gate: creation behaves as before.
"""

import importlib.util
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import hermes_yaml as yaml

from utils import is_truthy_value

logger = logging.getLogger(__name__)

_SCORER_REL = ("scripts", "lib", "skill_similarity.py")
_scorer_cache: Dict[Tuple[str, int], Any] = {}
# Same closing fence as skill_manager_tool._validate_frontmatter: a whole ``---`` line, not a ``---key``.
_FRONTMATTER_END_RE = re.compile(r"\n---\s*\n")


class DistinctFromError(ValueError):
    """The existing ``metadata.hermes.distinct_from`` is not a list, so it cannot be extended."""


def _load_scorer(root: Path):
    """The root's scorer module, or None when the file is absent. Reloaded when the file changes."""
    path = root.joinpath(*_SCORER_REL)
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        return None
    mod = _scorer_cache.get(key)
    if mod is None:
        spec = importlib.util.spec_from_file_location("_hermes_skill_similarity", path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _scorer_cache.clear()
        _scorer_cache[key] = mod
    return mod


def _gate_settings(default_threshold: float) -> Tuple[bool, float]:
    """``skills.neardup_gate.{enabled,threshold}``, read at call time from the served profile's config."""
    from hermes_cli.config import cfg_get, load_config
    cfg = load_config()
    enabled = is_truthy_value(cfg_get(cfg, "skills", "neardup_gate", "enabled"), default=True)
    try:
        threshold = float(cfg_get(cfg, "skills", "neardup_gate", "threshold", default=default_threshold))
    except (TypeError, ValueError):
        threshold = default_threshold
    return enabled, threshold


def _skill_roots(shared_root: Path) -> List[Path]:
    """Every loaded skill root plus the whole shared tree (unsubscribed groups and ``*-aux`` folders)."""
    from agent.skill_utils import get_all_skills_dirs
    roots = [Path(p) for p in get_all_skills_dirs()]
    return roots + [shared_root]


def add_distinct_from(content: str, entries: List[Dict[str, Any]]) -> str:
    """Return ``content`` with ``entries`` appended to ``metadata.hermes.distinct_from``."""
    content = content.lstrip("\ufeff")
    end = _FRONTMATTER_END_RE.search(content[3:])
    body_start = 3 + end.start()  # validation already required the fence
    fm = yaml.safe_load(content[3:body_start]) or {}
    meta = fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {}
    fm["metadata"] = meta
    hermes = meta.get("hermes") if isinstance(meta.get("hermes"), dict) else {}
    meta["hermes"] = hermes
    prior = hermes.get("distinct_from")
    if prior is not None and not isinstance(prior, list):
        raise DistinctFromError(
            f"metadata.hermes.distinct_from must be a list, got {type(prior).__name__}; "
            "fix it in the content and retry.")
    hermes["distinct_from"] = list(prior or []) + entries
    dumped = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, width=1_000_000)
    return "---\n" + dumped + content[body_start + 1:]


def check_create(name: str, content: str, justification: Optional[str], *, shared_root: Path,
                 profile: str) -> Tuple[Optional[str], Optional[Dict[str, Any]], str, Optional[Dict[str, Any]]]:
    """-> (refusal message or None, refusal payload, content to write, ledger row to append on success).

    Fails open (no gate) when the scorer is absent or raises: a create is reversible, a crashed
    create path is not a safety property."""
    root = shared_root.parent
    try:
        scorer = _load_scorer(root)
        if scorer is None:
            return None, None, content, None
        enabled, threshold = _gate_settings(scorer.THRESHOLD)
        if not enabled:
            return None, None, content, None
        verdict = scorer.check(name, content, roots=_skill_roots(shared_root), threshold=threshold)
    except Exception:  # noqa: BLE001
        logger.warning("near-duplicate gate unavailable for create of %s; allowing", name, exc_info=True)
        return None, None, content, None
    if not verdict["near_duplicate"]:
        return None, None, content, None
    row = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "path": "skill_manage", "name": name,
           "profile": profile, "threshold": verdict["threshold"], "matches": verdict["matches"]}
    why = (justification or "").strip()
    if not why:
        _append_ledger(scorer, root, {**row, "event": "refuse"})
        return scorer.refusal_message(name, verdict), {"near_duplicates": verdict["matches"]}, content, None
    entries = scorer.distinct_from_entries(verdict, why)
    try:
        content = add_distinct_from(content, entries)
    except DistinctFromError as e:
        return str(e), {}, content, None
    return None, None, content, {**row, "event": "justified_create", "why": why}


def record_justified(row: Optional[Dict[str, Any]], shared_root: Path) -> None:
    if row is None:
        return
    root = shared_root.parent
    try:  # audit only: the skill is already on disk, so nothing here may fail the create
        scorer = _load_scorer(root)
    except Exception:  # noqa: BLE001
        logger.warning("near-duplicate ledger skipped for %s: scorer reload failed", row.get("name"),
                       exc_info=True)
        return
    if scorer is not None:
        _append_ledger(scorer, root, row)


def _append_ledger(scorer, root: Path, row: Dict[str, Any]) -> None:
    try:
        scorer.append_ledger(row, scorer.ledger_path(root))
    except Exception:  # noqa: BLE001  the ledger is audit, never a reason to fail the create
        logger.warning("near-duplicate ledger write failed for %s", row.get("name"), exc_info=True)
