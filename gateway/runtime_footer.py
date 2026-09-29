"""Gateway runtime-metadata footer (model · context % · cwd), off by default to keep replies
minimal. Config: ``display.runtime_footer: {enabled: bool, fields: [model, context_pct, cwd]}``
(order shown; drop any to hide), per-platform override ``display.platforms.<p>.runtime_footer``,
toggled by ``/footer on|off``. Fields: ``model`` (vendor prefix dropped), ``context_pct`` (last-call
occupancy), ``latency`` (turn wall-clock, opt-in — NOT in the default set so an unset ``fields``
renders exactly as before), ``served_model`` (opt-in, ``alias → served``: the deployment a routing
proxy reported via ``x-litellm-model-id`` / ``x-litellm-model-api-base``, or Hermes' own fallback
route; skipped when the served model is the requested one), ``provider_model`` (opt-in,
``provider/model``, e.g. ``claude-bridge-f3/claude-opus-4-8``), ``context_full`` (opt-in,
``used/window (pct)`` humanized, e.g. ``50.2k/1M (5%)``), ``reasoning`` (opt-in, the completed
agent's effort as ``r:<level>``), ``cwd`` (home-relative). ``gateway/run.py`` appends the footer to the
final response only (never to tool-progress or streaming partials); when streaming already
delivered the text, it goes out as a trailing message via ``send_trailing_footer()``."""

from __future__ import annotations

import os
from typing import Any, Iterable, Optional

_DEFAULT_FIELDS: tuple[str, ...] = ("model", "context_pct", "cwd")
_SEP = " · "
_UNSET_REASONING_CONFIG = object()


def _home_relative_cwd(cwd: str) -> str:
    """Return *cwd* with ``$HOME`` collapsed to ``~``.  Empty string if unset."""
    if not cwd:
        return ""
    try:
        home = os.path.expanduser("~")
        p = os.path.abspath(cwd)
        if home and (p == home or p.startswith(home + os.sep)):
            return "~" + p[len(home):]
        return p
    except Exception:
        return cwd


def _model_short(model: Optional[str]) -> str:
    """Drop ``vendor/`` prefix (``openai/gpt-5.4`` → ``gpt-5.4``)."""
    return model.rsplit("/", 1)[-1] if model else ""


def _split_provider_model(provider: Optional[str], model: Optional[str]) -> tuple[str, str]:
    """Clean ``(provider, model)``. With no provider, a ``provider/model`` id is split. An explicit
    runtime provider is authoritative: an aggregator keeps its ``vendor/model`` id (the vendor is NOT
    the serving route), only a prefix identical to the route is stripped."""
    prov, mdl = (provider or "").strip(), (model or "").strip()
    if "/" in mdl:
        prefix, _, remainder = mdl.partition("/")
        if not prov:
            prov, mdl = prefix, remainder
        elif prefix == prov:
            mdl = remainder
    return prov, mdl


def _humanize_tok(n: Any) -> str:
    """Token count -> compact string (``50k``, ``1.5k``, ``1M``, ``1.0M``)."""
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        n = 0
    if abs(n) >= 1_000_000:
        return f"{n // 1_000_000}M" if n % 1_000_000 == 0 else f"{n / 1_000_000:.1f}M"
    if abs(n) >= 1000:
        return f"{n // 1000}k" if n % 1000 == 0 else f"{n / 1000:.1f}k"
    return str(n)


def _env_cwd() -> str:
    try:
        from tools.terminal_scope import terminal_env
    except ImportError:
        return os.environ.get("TERMINAL_CWD", "")
    return terminal_env("TERMINAL_CWD", "")


def resolve_footer_config(user_config: dict[str, Any] | None, platform_key: str | None = None) -> dict[str, Any]:
    """Resolve effective footer config: defaults (enabled=False) <
    ``display.runtime_footer`` < ``display.platforms.<platform_key>.runtime_footer``."""
    resolved = {"enabled": False, "fields": list(_DEFAULT_FIELDS)}
    cfg = (user_config or {}).get("display") or {}
    plat_cfg = (cfg.get("platforms") or {}).get(platform_key) if platform_key else None
    sections = [cfg.get("runtime_footer"), plat_cfg.get("runtime_footer") if isinstance(plat_cfg, dict) else None]
    for section in sections:
        if not isinstance(section, dict):
            continue
        if "enabled" in section:
            resolved["enabled"] = bool(section.get("enabled"))
        if isinstance(section.get("fields"), list) and section["fields"]:
            resolved["fields"] = [str(f) for f in section["fields"]]
    return resolved


def _format_latency(seconds: float) -> str:
    """Humanize a turn duration: ``<1s``, ``22s``, ``1m05s``."""
    if seconds < 1:
        return "<1s"
    total = int(round(seconds))
    if total < 60:
        return f"{total}s"
    m, sec = divmod(total, 60)
    return f"{m}m{sec:02d}s"


def format_runtime_footer(*, model: Optional[str], context_tokens: int,
                          context_length: Optional[int], cwd: Optional[str] = None,
                          turn_seconds: Optional[float] = None,
                          requested_model: Optional[str] = None, served_model: Optional[str] = None,
                          provider: Optional[str] = None, reasoning: Optional[str] = None,
                          fields: Iterable[str] = _DEFAULT_FIELDS) -> str:
    """Render the footer line, or "" if no fields have data. Fields whose data is missing (and
    unknown field names) are skipped silently — a partial footer beats ``?%`` or empty slots."""
    def context_pct() -> str:
        if context_length and context_length > 0 and context_tokens >= 0:
            return f"{max(0, min(100, round((context_tokens / context_length) * 100)))}%"
        return ""

    def served() -> str:
        requested = requested_model or model
        alias = _model_short(requested)
        if served_model and served_model not in (alias, requested):
            return f"{alias} → {served_model}"
        return ""

    def context_full() -> str:
        # Both used and window humanized (50.2k/1M); pct from the raw values.
        if context_length and context_length > 0 and context_tokens >= 0:
            return f"{_humanize_tok(context_tokens)}/{_humanize_tok(context_length)} ({context_pct()})"
        return _humanize_tok(context_tokens) if context_tokens and context_tokens > 0 else ""

    def provider_model() -> str:
        prov, mdl = _split_provider_model(provider, model)
        return f"{prov}/{mdl}" if prov and mdl else mdl

    renderers = {
        "model": lambda: _model_short(model),
        "provider_model": provider_model,
        "context_full": context_full,
        "reasoning": lambda: f"r:{r}" if (r := (reasoning or "").strip()) else "",
        "served_model": served,
        "context_pct": context_pct,
        # Skipped when the caller did not measure (None) or the value is negative.
        "latency": lambda: _format_latency(turn_seconds) if turn_seconds is not None and turn_seconds >= 0 else "",
        "cwd": lambda: _home_relative_cwd(cwd or _env_cwd()),
    }
    return _SEP.join(v for field in fields if (render := renderers.get(field)) and (v := render()))


def _reasoning_label(reasoning_config: Any) -> str:
    """Footer label for a parsed reasoning config (``hermes_constants.parse_reasoning_effort``
    shape): the bare level, ``none`` when thinking is disabled, "" when unset (field dropped)."""
    if not isinstance(reasoning_config, dict):
        return ""
    if not reasoning_config.get("enabled", True):
        return "none"
    return str(reasoning_config.get("effort", "") or "").strip()


def _reasoning_from_config(user_config: dict[str, Any] | None, model: Optional[str] = None) -> str:
    """Effective reasoning level for *model* from *user_config*, through the shared
    ``hermes_constants.resolve_reasoning_config`` chokepoint (per-model overrides, YAML-boolean
    "disabled"). Callers with a live agent pass its ``reasoning_config`` to build_footer_line."""
    try:
        from hermes_constants import resolve_reasoning_config
        return _reasoning_label(resolve_reasoning_config(user_config or {}, model or ""))
    except Exception:
        return ""


def build_footer_line(*, user_config: dict[str, Any] | None, platform_key: str | None,
                      model: Optional[str], context_tokens: int, context_length: Optional[int],
                      cwd: Optional[str] = None, turn_seconds: Optional[float] = None,
                      requested_model: Optional[str] = None, served_model: Optional[str] = None,
                      provider: Optional[str] = None, reasoning: Optional[str] = None,
                      reasoning_config: Any = _UNSET_REASONING_CONFIG) -> str:
    """Entry point for the gateway: footer text, or "" when disabled / no data. Callers append it
    to the final response themselves, preserving a single blank line of separation.
    ``turn_seconds`` is the caller-measured (``time.monotonic()``) run duration; ``None`` skips the
    ``latency`` field. ``reasoning_config`` is the caller's snapshot of the completed agent (fallback
    changes included); an explicit ``None`` means no known effort and drops the field. Only callers
    that OMIT it fall back to config-level resolution."""
    cfg = resolve_footer_config(user_config, platform_key)
    if not cfg.get("enabled"):
        return ""
    if reasoning is None:
        reasoning = (_reasoning_label(reasoning_config) if reasoning_config is not _UNSET_REASONING_CONFIG
                     else _reasoning_from_config(user_config, model))
    return format_runtime_footer(model=model, context_tokens=context_tokens,
                                 context_length=context_length, cwd=cwd, turn_seconds=turn_seconds,
                                 requested_model=requested_model, served_model=served_model,
                                 provider=provider, reasoning=reasoning,
                                 fields=cfg.get("fields") or _DEFAULT_FIELDS)
