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


#: ``path -> (mtime_ns, size, {slot: id})``; one entry per home, re-read when the file changes.
_slot_view_memo: dict[str, tuple[int, int, dict[str, str]]] = {}
_slot_view_lock = threading.Lock()


def slot_view() -> dict[str, str]:
    """``{slot: id}`` from ``<root>/config/model-slots.json`` (rendered by ``model-registry.py``).

    Accepts ``{"slots": {...}, ...}`` or a flat ``{slot: id, ...}`` (keys starting with ``_``
    and ``rendered_from`` are metadata). mtime-cached, so a re-render is seen on the next call
    with no restart. A missing file means ``{}`` (fail open: names pass through); a file that
    cannot be read or parsed (a checkout rewriting it mid-read) keeps the last good parse.
    """
    hit = None
    try:
        import json
        from hermes_constants import get_default_hermes_root

        path = get_default_hermes_root() / "config" / "model-slots.json"
        key = str(path)
        with _slot_view_lock:
            hit = _slot_view_memo.get(key)
        try:
            st = path.stat()
        except FileNotFoundError:
            return {}
        if hit is not None and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
            return hit[2]
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        table = data.get("slots") if isinstance(data.get("slots"), dict) else data
        view = {
            str(k).strip(): v.strip()
            for k, v in table.items()
            if isinstance(k, str) and isinstance(v, str) and v.strip()
            and not k.startswith("_") and k != "rendered_from"
        }
        with _slot_view_lock:
            _slot_view_memo[key] = (st.st_mtime_ns, st.st_size, view)
        return view
    except Exception:
        return hit[2] if hit is not None else {}


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
    return (f"{prefix}/{target}" if prefix else target), spec


def _lane_vendors(provider: Optional[str]) -> Optional[frozenset[str]]:
    """The vendors (``_infer_vendor_from_model`` keys) ``provider`` serves; ``None`` = unknown.

    Built from the lane metadata pricing already keys on (Claude relay lanes, the multi-vendor
    ``cpa`` proxy, single-vendor OAuth lanes), then the lane's static or live catalog. An
    aggregator, a custom endpoint with no list, or an unknown lane is ``None``: nothing to refuse on.
    """
    p = (provider or "").strip().lower()
    if p.startswith("custom:"):
        p = p.split(":", 1)[1].strip()
    if not p:
        return None
    try:
        from agent import usage_pricing as up
        from agent.relay_lanes import is_relay_backed

        if p == "anthropic" or up.is_notional_anthropic_provider(p) or is_relay_backed(p):
            return frozenset({"anthropic"})
        if up.proxy_lane(p):
            return frozenset(up._PROXY_VENDOR_PRICING_LANE)
        if p == "xai" or up.is_notional_xai_provider(p):
            return frozenset({"xai"})
        if p in up.NOTIONAL_MOONSHOT_PROVIDERS:
            return frozenset({"moonshotai"})
        if p == "openai-codex":
            return frozenset({"openai"})
        from hermes_cli.models import _AGGREGATOR_PROVIDERS, _PROVIDER_MODELS, normalize_provider

        if normalize_provider(p) in _AGGREGATOR_PROVIDERS:
            return None
        ids = tuple(_PROVIDER_MODELS.get(normalize_provider(p), ())) or catalog_for(p)
        vendors = frozenset(v for v in (up._infer_vendor_from_model(str(i).split("/")[-1]) for i in ids) if v)
        return vendors or None
    except Exception:
        return None


def _configured_lanes() -> list[str]:
    """Lane names this home is configured with: ``providers:``, ``model.provider``, alias lanes."""
    lanes: list[str] = []
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        model_cfg = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
        if isinstance(cfg.get("providers"), dict):
            lanes.extend(str(k) for k in cfg["providers"])
        lanes.append(str(model_cfg.get("provider") or ""))
        from hermes_cli.model_switch import DIRECT_ALIASES, _ensure_direct_aliases

        _ensure_direct_aliases()
        lanes.extend(str(a.provider or "") for a in DIRECT_ALIASES.values())
    except Exception:
        pass
    seen: list[str] = []
    for lane in (x.strip().lower() for x in lanes):
        if lane and lane not in seen:
            seen.append(lane)
    return seen


def slot_lane_refusal(model: Optional[str], lane: Optional[str]) -> Optional[str]:
    """One refusal line when the bare slot ``model`` would run on ``lane`` and that lane does not
    serve the vendor of the slot's current id; ``None`` otherwise.

    Callers use it only for the lane a slot would INHERIT (the session's, the profile's, the
    parent's): an explicit ``lane/slot`` or a ``model.aliases`` lane is honoured as given. Lanes
    differ in billing, so a non-serving lane is refused, never swapped for another one.
    Unknown vendor, unknown lane, or a name that is not a bare slot: ``None`` (admit).
    """
    resolved, slot = slot_resolve(model)
    if not slot or "/" in slot or not lane:
        return None
    from agent.usage_pricing import _infer_vendor_from_model

    vendor = _infer_vendor_from_model(str(resolved))
    served = _lane_vendors(lane)
    if not vendor or served is None or vendor in served:
        return None
    serving = [x for x in _configured_lanes() if (_lane_vendors(x) or frozenset()) >= {vendor}]
    hint = (f"lanes that serve it: {', '.join(serving)} (e.g. {serving[0]}/{slot})" if serving
            else f"no configured lane serves {vendor}; name one as <lane>/{slot}")
    return f"slot {slot} ({resolved}) is not served on {lane.strip().lower()}; {hint}"


class SlotLaneRefused(ValueError):
    """A bare slot would run on an inherited lane that does not serve its vendor."""


def require_slot_lane(model: Optional[str], lane: Optional[str]) -> None:
    """:func:`slot_lane_refusal`, raising :class:`SlotLaneRefused`."""
    line = slot_lane_refusal(model, lane)
    if line:
        raise SlotLaneRefused(line)


__all__ = [
    "CATALOG_TTL_SECONDS",
    "slot_resolve",
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
