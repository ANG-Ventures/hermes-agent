"""One ``(provider, model)`` resolver for every writer that persists a model id.

Incident t_d564a8dd (2026-10-11): a card carried ``model_override=claude-sonnet-4-8``,
an id no fleet lane serves. Nothing checked it at write time, so for 8 hours every
dispatch got ``HTTP 404: model: claude-sonnet-4-8`` and the card looked healthy. The
same morning a cron job was minted with the same id as its primary AND in its derived
fallback chain. Kanban cards, cron jobs, ``delegate_task`` and the dispatcher's
pre-spawn check all resolve through :func:`resolve` so an unknown id is refused once,
at the writer, with the lane's real list — or deliberately forced with a reason.

The catalog is the same source ``/model`` lists: :func:`hermes_cli.models.
cached_provider_model_ids` (the lane's live ``/v1/models`` behind the 10-minute
in-process memo below and the shared disk cache). A lane whose list cannot be read
(relay down, provider unknown here, no credentials) is **unverifiable** and the write
is admitted — refusing would strand every card behind a transient outage — but the
verdict says so, and the dispatcher re-checks on every tick.
"""
from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

#: In-process memo of a lane's catalog; the disk cache under it has the same TTL.
CATALOG_TTL_SECONDS = 600

_memo: dict[tuple[str, str], tuple[float, tuple[str, ...]]] = {}
_memo_lock = threading.Lock()


class UnknownModel(ValueError):
    """``model`` is not on ``provider``'s live model list."""

    def __init__(self, provider: str, model: str, valid_ids: tuple[str, ...], *, flag: str = "--force-model"):
        self.provider = provider
        self.model = model
        self.valid_ids = valid_ids
        self.flag = flag
        valid = ", ".join(valid_ids) if valid_ids else "(empty)"
        super().__init__(
            f"unknown model {model!r} on {provider}: not on the lane's model list "
            f"[{valid}]; pass {flag} <reason> to pin it anyway"
        )


@dataclass(frozen=True)
class ModelResolution:
    ok: bool
    provider: Optional[str]
    model: Optional[str]
    #: ``known`` (on the list) · ``unknown`` (refused) · ``unverifiable`` (no list for
    #: this lane) · ``no_provider`` (nothing to check against) · ``forced`` (bypassed)
    #: · ``empty`` (no model given)
    reason: str
    valid_ids: tuple[str, ...] = field(default_factory=tuple)
    force_reason: Optional[str] = None

    @property
    def verified(self) -> bool:
        return self.reason == "known"


def _memo_key(provider: str) -> tuple[str, str]:
    try:
        from hermes_constants import hermes_home_key

        home = str(hermes_home_key())
    except Exception:
        home = ""
    return home, provider


def _split_prefixed(model_id: str) -> tuple[str, str]:
    prefix, sep, rest = model_id.partition("/")
    return (prefix, rest) if sep and rest else ("", model_id)


def _bare_id(provider: str, model_id: str) -> str:
    """``claude-alr/claude-sonnet-5-5`` → ``claude-sonnet-5-5`` when the prefix is the lane."""
    prefix, rest = _split_prefixed(model_id)
    return rest if prefix and prefix.lower() == provider.lower() else model_id


def clear_catalog_memo() -> None:
    with _memo_lock:
        _memo.clear()


def _fetch_catalog(provider: str, *, force_refresh: bool = False) -> tuple[str, ...]:
    """Live/disk-cached catalog read. Tests of the writers patch THIS seam.

    Inside pytest the lookup is skipped (every writer test would otherwise probe real
    relays and depend on their state); ``HERMES_MODEL_REGISTRY_LIVE=1`` re-enables it.
    """
    if os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get("HERMES_MODEL_REGISTRY_LIVE"):
        return ()
    try:
        from hermes_cli.models import cached_provider_model_ids

        return tuple(
            str(m) for m in cached_provider_model_ids(
                provider, force_refresh=force_refresh, ttl_seconds=CATALOG_TTL_SECONDS,
            ) if m
        )
    except Exception:
        return ()


def catalog_for(provider: str, *, force_refresh: bool = False) -> tuple[str, ...]:
    """The lane's model ids (bare and ``provider/``-prefixed as served). Empty when unknown."""
    provider = (provider or "").strip().lower()
    if not provider:
        return ()
    key = _memo_key(provider)
    now = time.monotonic()
    if not force_refresh:
        with _memo_lock:
            hit = _memo.get(key)
        if hit is not None and now - hit[0] < CATALOG_TTL_SECONDS:
            return hit[1]
    ids = _fetch_catalog(provider, force_refresh=force_refresh)
    if ids:
        with _memo_lock:
            _memo[key] = (now, ids)
    return ids


def _matches(provider: str, model: str, catalog: tuple[str, ...]) -> bool:
    wanted = {model.lower(), _bare_id(provider, model).lower()}
    try:
        from agent.model_metadata import strip_codex_context_variant_suffix

        stripped = strip_codex_context_variant_suffix(model)
        if stripped:
            wanted.add(stripped.lower())
            wanted.add(_bare_id(provider, stripped).lower())
    except Exception:
        pass
    for cid in catalog:
        low = cid.lower()
        if low in wanted or _bare_id(provider, cid).lower() in wanted:
            return True
    return False


def resolve(
    provider: Optional[str],
    model: Optional[str],
    *,
    force_reason: Optional[str] = None,
    resolve_aliases: bool = True,
) -> ModelResolution:
    """Resolve ``(provider, model)`` against the lane's model list.

    Config ``model_aliases`` / ``provider/model`` forms are expanded first (the same
    write-time resolution kanban and cron already apply), so the pair checked is the
    pair that will be persisted. ``force_reason`` admits an unknown id and is recorded
    on the verdict so the writer can leave the audit comment.
    """
    model = (model or "").strip() or None
    provider = (provider or "").strip().lower() or None
    if not model:
        return ModelResolution(True, provider, None, "empty")
    if resolve_aliases:
        try:
            from hermes_cli.model_switch import resolve_model_pair_for_storage

            model, provider = resolve_model_pair_for_storage(model, provider)
            model = (model or "").strip() or None
            provider = (provider or "").strip().lower() or None
        except Exception:
            pass
    force = (force_reason or "").strip() or None
    if model and not provider:
        # ``lane/model`` with the lane unknown to the alias resolver (a plugin provider
        # not loaded here): split it the way the dispatcher does (kanban_provider_health).
        prefix, rest = _split_prefixed(model)
        if prefix and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", prefix) and "/" not in rest:
            provider, model = prefix.lower(), rest
    if not provider or not model:
        return ModelResolution(True, provider, model, "no_provider", force_reason=force)
    catalog = catalog_for(provider)
    if not catalog:
        return ModelResolution(True, provider, model, "unverifiable", force_reason=force)
    if _matches(provider, model, catalog):
        return ModelResolution(True, provider, model, "known", catalog, force)
    if force:
        return ModelResolution(True, provider, model, "forced", catalog, force)
    return ModelResolution(False, provider, model, "unknown", catalog, None)


def require(
    provider: Optional[str],
    model: Optional[str],
    *,
    force_reason: Optional[str] = None,
    flag: str = "--force-model",
) -> ModelResolution:
    """:func:`resolve`, raising :class:`UnknownModel` on a refused id."""
    verdict = resolve(provider, model, force_reason=force_reason)
    if not verdict.ok:
        raise UnknownModel(verdict.provider or "", verdict.model or "", verdict.valid_ids, flag=flag)
    return verdict


def invalid_pin_reason(provider: Optional[str], model: Optional[str]) -> Optional[str]:
    """``model_override_invalid: <id> not on <lane>`` for a stored pin, else None.

    The dispatcher's pre-spawn form: a row written by hand or before the writers
    validated is caught here, every tick, without ever raising.
    """
    try:
        verdict = resolve(provider, model, resolve_aliases=False)
    except Exception:
        return None
    if verdict.ok:
        return None
    return f"model_override_invalid: {verdict.model} not on {verdict.provider}"


__all__ = [
    "CATALOG_TTL_SECONDS",
    "ModelResolution",
    "UnknownModel",
    "catalog_for",
    "clear_catalog_memo",
    "invalid_pin_reason",
    "require",
    "resolve",
]
