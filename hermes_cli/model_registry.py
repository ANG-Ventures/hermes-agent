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

import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional

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
        from hermes_cli.models import CuratedFallbackModels, cached_provider_model_ids

        ids = cached_provider_model_ids(provider, force_refresh=force_refresh, ttl_seconds=CATALOG_TTL_SECONDS)
        # A curated placeholder (the live probe failed; the plugin's offline snapshot) is not the
        # lane's list: refusing against it refused every id the snapshot omits (t_cf4c6032).
        if isinstance(ids, CuratedFallbackModels):
            return ()
        return tuple(str(m) for m in ids if m)
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
    """:func:`resolve`, raising :class:`UnknownModel` on a refused id.

    A slot (``sol``) is checked as the id it resolves to; writers still store the slot."""
    verdict = resolve(provider, slot_resolve(model)[0], force_reason=force_reason)
    if not verdict.ok:
        raise UnknownModel(verdict.provider or "", verdict.model or "", verdict.valid_ids, flag=flag)
    return verdict


def invalid_pin_reason(provider: Optional[str], model: Optional[str]) -> Optional[str]:
    """``model_override_invalid: <id> not on <lane>`` for a stored pin, else None.

    The dispatcher's pre-spawn form: a row written by hand or before the writers
    validated is caught here, every tick, without ever raising.
    """
    # A slot pin (``sol``) is valid when the id it resolves to is; the row keeps the slot.
    model = slot_resolve(model)[0]
    try:
        verdict = resolve(provider, model, resolve_aliases=False)
    except Exception:
        return None
    if verdict.ok:
        return None
    return f"model_override_invalid: {verdict.model} not on {verdict.provider}"


def retired_rewrite(model: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """``(successor, note)`` when ``model`` is RETIRED on this home, else ``(model, None)``.

    G7 (t_1e1450d1): long-lived sessions copied a retired id (``gpt-6.1-sol``) out of their
    own old context into ``delegate_task``; the lane still served it, so nothing refused.
    Retired ids are ``<root>/config/model-retired-ids.txt`` (``<id> cutover=...``) and the
    successor is ``<root>/config/model-successors.json`` ``successors``; a chain of retired
    ids is followed to the first live one. A ``lane/`` prefix is kept. Any missing or
    unreadable file, or a retired id with no successor, means no rewrite (fail open).
    """
    if not isinstance(model, str) or not model.strip():
        return model, None
    try:
        import json
        from hermes_constants import get_default_hermes_root

        cfg = get_default_hermes_root() / "config"
        retired: dict[str, str] = {}
        for line in (cfg / "model-retired-ids.txt").read_text(encoding="utf-8-sig").splitlines():
            parts = line.split("#", 1)[0].split()
            if parts:
                retired[parts[0]] = next((p[8:] for p in parts[1:] if p.startswith("cutover=")), "")
        successors = json.loads((cfg / "model-successors.json").read_text(encoding="utf-8-sig"))["successors"]
        prefix, bare = _split_prefixed(model.strip())
        current, hops = bare, 0
        while current in retired and isinstance(successors.get(current), str) and hops < 8:
            current, hops = successors[current].strip(), hops + 1
        if not hops or not current or current in retired:
            return model, None
    except Exception:
        return model, None
    new = f"{prefix}/{current}" if prefix else current
    cutover = retired.get(bare) or "?"
    return new, (f"model rewritten: {model} -> {new} ({bare} is retired, cutover {cutover}, "
                 f"per config/model-retired-ids.txt + model-successors.json)")


#: ``path -> (mtime_ns, size, parsed view)``; one entry per home and view file, re-read when it changes.
_view_memo: dict[str, tuple[int, int, Any]] = {}
_view_lock = threading.Lock()


def _drop_view(key: str) -> None:
    """A missing file evicts its cached view, so a torn re-create cannot fall back to it."""
    with _view_lock:
        _view_memo.pop(key, None)


def _cached_view(filename: str, parse: Callable[[Any], Any], empty: Any) -> Any:
    """``parse(json)`` of ``<root>/config/<filename>``, mtime-cached.

    A missing file means ``empty`` (fail open); a file that cannot be read or parsed (a
    checkout rewriting it mid-read) keeps the last good parse.
    """
    hit = None
    try:
        import json
        from hermes_constants import get_default_hermes_root

        path = get_default_hermes_root() / "config" / filename
        key = str(path)
        with _view_lock:
            hit = _view_memo.get(key)
        try:
            st = path.stat()
        except FileNotFoundError:
            _drop_view(key)
            return empty
        if hit is not None and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
            return hit[2]
        try:
            text = path.read_text(encoding="utf-8-sig")
        except FileNotFoundError:  # deleted between stat and read: missing, not torn
            _drop_view(key)
            return empty
        view = parse(json.loads(text))
        with _view_lock:
            _view_memo[key] = (st.st_mtime_ns, st.st_size, view)
        return view
    except Exception:
        return hit[2] if hit is not None else empty


def _parse_slot_view(data: Any) -> dict[str, str]:
    table = data.get("slots") if isinstance(data.get("slots"), dict) else data
    return {
        str(k).strip(): v.strip()
        for k, v in table.items()
        if isinstance(k, str) and isinstance(v, str) and v.strip()
        and not k.startswith("_") and k != "rendered_from"
    }


def slot_view() -> dict[str, str]:
    """``{slot: id}`` from ``<root>/config/model-slots.json`` (rendered by ``model-registry.py``).

    Accepts ``{"slots": {...}, ...}`` or a flat ``{slot: id, ...}`` (keys starting with ``_``
    and ``rendered_from`` are metadata). mtime-cached, so a re-render is seen on the next call
    with no restart. A missing file means ``{}`` (fail open: names pass through); a file that
    cannot be read or parsed (a checkout rewriting it mid-read) keeps the last good parse.
    """
    return _cached_view("model-slots.json", _parse_slot_view, {})


def slot_resolve(model: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """``(id, slot)`` when ``model`` names a slot in :func:`slot_view`, else ``(model, None)``.

    A ``lane/`` prefix is kept on both (``cpa/sol`` -> ``cpa/gpt-6.2-sol``, slot ``cpa/sol``), so
    re-resolving the slot later lands on the same lane. Literal ids, unknown names and a missing
    view pass through unchanged.
    """
    if not isinstance(model, str) or not model.strip():
        return model, None
    spec = model.strip()
    prefix, bare = _split_prefixed(spec)
    target = slot_view().get(bare)
    if not target or target == bare:
        return model, None
    # ``google/gemini`` with target ``google/gemini-2.5-pro``: the target already carries it.
    if prefix and not target.startswith(f"{prefix}/"):
        target = f"{prefix}/{target}"
    return target, spec


def slot_canonicalise(
    model: Optional[str], provider: Optional[str] = None, *, exact: bool = False,
) -> tuple[Optional[str], Optional[str]]:
    """``(slot, note)`` when ``model`` is exactly a slot's CURRENT id, else ``(model, None)``.

    Slot aliases P6 (t_efde9d1a): a writer that stores a model (``/model``, a cron job, a card
    pin) stores the slot name instead of the id the slot points at today, so the next rollout
    moves it with no sweep. The note is the writer's one confirmation line. A ``lane/`` prefix is
    kept (``cpa/gpt-6.2-sol`` -> ``cpa/sol``). Untouched: ``exact`` (the writer's opt-out), an id
    no slot points at (a retired id keeps its successor rewrite), an id two slots share, a value
    that already is a slot, and a lane whose readable model list lacks the id (the lane does not
    serve it) or a forced pin. A missing view stores the literal (fail open).
    """
    if exact or not isinstance(model, str) or not model.strip():
        return model, None
    spec = model.strip()
    prefix, bare = _split_prefixed(spec)
    view = slot_view()
    if not view or bare in view:
        return model, None
    # A bare id passed with its lane separately matches a lane-qualified target
    # (``sol: cpa/gpt-6.2-sol`` with provider ``cpa``; Prism 7c7a0c08ca9d).
    laned = f"{provider.strip()}/{spec}" if isinstance(provider, str) and provider.strip() and not prefix else None
    names = sorted(name for name, target in view.items() if target in (bare, spec, laned))
    if len(names) != 1:
        return model, None
    # An explicit lane prefix stays on the slot (``cpa/gpt-6.2-sol`` on cpa -> ``cpa/sol``), so a
    # later rollout to an unqualified target keeps the caller's lane (Prism 7c7a0c08ca9d).
    on_lane = bool(prefix) and isinstance(provider, str) and prefix.lower() == provider.strip().lower()
    keep = prefix and (on_lane or view[names[0]] not in (spec, laned))
    slot = f"{prefix}/{names[0]}" if keep else names[0]
    if slot_resolve(slot)[0] not in (spec, laned):
        return model, None
    try:
        verdict = resolve(provider, spec, resolve_aliases=False)
    except Exception:
        return model, None
    if verdict.reason in ("unknown", "forced"):
        return model, None
    return slot, f"stored as slot {slot}, currently id {spec}"


def slot_store_note(requested: Optional[str], stored: Optional[str]) -> Optional[str]:
    """The confirmation line for a writer whose store rewrote ``requested`` to the slot
    ``stored`` (:func:`slot_canonicalise` ran below the caller), else None."""
    if not isinstance(requested, str) or not isinstance(stored, str) or stored == requested.strip():
        return None
    resolved, slot = slot_resolve(stored)
    return f"stored as slot {slot}, currently id {resolved}" if slot else None


def _parse_meta_view(data: Any) -> dict[str, dict]:
    table = data.get("models") if isinstance(data, dict) else None
    if not isinstance(table, dict):
        return {}
    return {k.strip().lower(): v for k, v in table.items() if isinstance(k, str) and isinstance(v, dict)}


def model_meta_view() -> dict[str, dict]:
    """``{id: {"context": int, "pricing": {...}}}`` from ``<root>/config/model-meta.json``.

    Slot aliases P7 (t_efde9d1a): the registry row carries the context window and price of an
    id; ``model-registry.py render`` writes this view and the runtime reads it before its
    built-in tables, so a new id needs no runtime release. Keys are lower-cased ids. A missing
    view is ``{}``: every lookup falls back to the built-in tables.
    """
    return _cached_view("model-meta.json", _parse_meta_view, {})


def _meta_row(model: Optional[str]) -> Optional[dict]:
    if not isinstance(model, str) or not model.strip():
        return None
    resolved = slot_resolve(model.strip())[0] or ""
    view = model_meta_view()
    return view.get(resolved.lower()) or view.get(_split_prefixed(resolved)[1].lower())


def registry_context_length(model: Optional[str]) -> Optional[int]:
    """The registry's context window for ``model`` (a slot resolves first), else None."""
    row = _meta_row(model)
    ctx = row.get("context") if row else None
    return ctx if isinstance(ctx, int) and not isinstance(ctx, bool) and ctx > 0 else None


#: registry pricing keys (USD per million tokens); ``input`` and ``output`` are required.
PRICING_KEYS = ("input", "output", "cache_read", "cache_write")


def registry_pricing(model: Optional[str]) -> Optional[dict[str, str]]:
    """``{input, output[, cache_read, cache_write]}`` USD per million tokens as decimal strings,
    from the registry view, else None (also None when ``input`` or ``output`` is missing or any
    rate is not a non-negative number)."""
    row = _meta_row(model)
    pricing = row.get("pricing") if row else None
    if not isinstance(pricing, dict):
        return None
    out: dict[str, str] = {}
    for key in PRICING_KEYS:
        value = pricing.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return None
        # Decimal, not float: a huge JSON integer must not raise OverflowError, and a
        # non-finite rate (Infinity, NaN, or past float range) is no row (Prism c-61c5c01d036c6b97).
        try:
            number = Decimal(str(value).strip())
        except (InvalidOperation, ValueError):
            return None
        if not number.is_finite() or number < 0 or not math.isfinite(float(number)):
            return None
        out[key] = str(value)
    return out if "input" in out and "output" in out else None


__all__ = [
    "CATALOG_TTL_SECONDS",
    "PRICING_KEYS",
    "model_meta_view",
    "registry_context_length",
    "registry_pricing",
    "slot_canonicalise",
    "slot_resolve",
    "slot_store_note",
    "slot_view",
    "ModelResolution",
    "UnknownModel",
    "catalog_for",
    "clear_catalog_memo",
    "invalid_pin_reason",
    "require",
    "resolve",
    "retired_rewrite",
]
