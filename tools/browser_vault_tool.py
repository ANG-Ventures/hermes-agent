#!/usr/bin/env python3
"""Vault-backed model-blind browser autofill tools.

Two model-facing tools, gated on the local vault having at least one item
(zero schema cost otherwise, same ``check_fn`` pattern as the Home Assistant
tools):

- ``browser_vault_list``  → handles + metadata (for logins this includes the
  identifier — it is NOT a secret; the agent types it itself). Passwords are
  never returned.
- ``browser_vault_fill``  → server-side fill of the CURRENT page from a vault
  handle: the password field for logins, card fields for payment items (after
  the user confirms), address fields for address items. The secret is
  resolved locally, the page origin must EXACTLY match the item's bound
  origin (pre-checked AND re-asserted synchronously inside the fill script),
  the field is chosen by the ported login-control classifier, injection runs
  exclusively over the supervisor CDP WebSocket (never argv), and the tool
  result reports only ``{filled_fields, kind, origin, success}`` — the
  password never appears in tool results, logs, or the session DB, and its
  exact bytes are registered with the browser-result redaction boundary so
  no later browser tool call can echo them back to the model.

Ported design from Merit-Systems/OpenInstinct (MIT): opaque-handle vault
autofill (kernel-login-autofill.ts / fill_from_vault.ts).
"""

from __future__ import annotations

import json
import secrets
import logging
from contextvars import ContextVar
from functools import wraps
from inspect import signature
from typing import Any, Dict, NamedTuple, Optional

logger = logging.getLogger(__name__)


# An operation-local document witness, never an origin-based tab selector.
_PAGE_BINDING: ContextVar[Any] = ContextVar("vault_page_binding", default=None)


class _Binding(NamedTuple):
    task_id: str
    key: Any        # agent-browser session key, the Browser Use route dict, or the leased VaultTransaction
    session: Any    # agent-browser session record (None on Browser Use)
    witness: str
    driver: str = "agent-browser"
    target: Optional[tuple] = None  # Browser Use: (target_id, cdp session_id, daemon pid) at pin time


def _pin_page(task_id):
    from tools.browser_tool import (
        _active_sessions, _last_session_key, _session_info_owned_by_task,
        _is_camofox_mode, _is_browser_use_cli_mode,
    )
    from tools.browser_tool_session import _run_browser_command

    # Camofox has no CDP target API: fail closed. Never reuse a stale built-in session
    # while another backend owns navigation.
    if _is_camofox_mode():
        return None
    from tools.browser_use_target import _pin, browser_use_owns
    # The tool handlers (_fenced_page_op) always hold a daemon selection lease here; a direct
    # call without one binds through navigation's driver route instead.
    if browser_use_owns(task_id) and _pin.get() is not None:
        return _pin_selected_target_page(task_id)
    if _is_browser_use_cli_mode():
        return _pin_browser_use_page(task_id)
    key = _last_session_key(task_id)
    session = _active_sessions.get(key)
    if session is None or not _session_info_owned_by_task(session, task_id, key):
        return None
    witness = secrets.token_hex(24)
    result = _run_browser_command(key, "eval", [
        f"window[{json.dumps(witness)}] = true; window.location.href"
    ])
    if not result.get("success"):
        return None
    return _Binding(task_id, key, session, witness)


def _pin_browser_use_page(task_id):
    """Pin the page browser_exec's harness daemon is attached to right now (its current target),
    on the route navigation last drove. Requires that route's vault supervisor."""
    from tools import browser_use_cli as bu
    from tools.browser_supervisor import SUPERVISOR_REGISTRY

    route = bu.last_route(task_id)
    if not route or route.get("supervisor") is None or SUPERVISOR_REGISTRY.get(task_id) is not route["supervisor"]:
        return None
    witness = secrets.token_hex(24)
    seen = bu.driver_eval(task_id, route, f"(window[{json.dumps(witness)}] = true, window.location.href)")
    if not seen:
        return None
    return _Binding(task_id, route, None, witness, "browser-use",
                    (seen["target_id"], seen["session_id"], seen.get("pid")))


def _selected_target_eval(task_id, expression: str) -> Dict[str, Any]:
    """Evaluate on the Browser Use target the daemon selection lease pins (validated per call)."""
    try:
        supervisor = _ensure_supervisor(task_id)
        sup = supervisor.evaluate_runtime(expression)
        return {"success": bool(sup.get("ok")), "result": sup.get("result"),
                "error": sup.get("error", "")}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def _pin_selected_target_page(task_id):
    """Browser Use with a daemon selection lease (_fenced_page_op began it): mark the document on
    the leased target with the operation witness. No lease for this task: refuse."""
    from tools import browser_use_target as target

    tx = target._pin.get()
    if tx is None or tx.task_id != task_id:
        return None
    witness = secrets.token_hex(24)
    seen = _selected_target_eval(task_id, f"(window[{json.dumps(witness)}] = true, window.location.href)")
    if not seen.get("success"):
        return None
    return _Binding(task_id, tx, None, witness, "browser-use-lease", (tx.target,))


def _bound_page(fn):
    call_signature = signature(fn)

    @wraps(fn)
    def bound(*args, **kwargs):
        task_id = call_signature.bind(*args, **kwargs).arguments.get("task_id") or "default"
        # Nested save-login -> fill keeps the document the user approved.
        existing = _PAGE_BINDING.get()
        if existing is not None:
            if existing[0] != task_id or not _binding_is_current(existing):
                return json.dumps({"success": False, "error_type": "page_changed",
                                   "error": "Browser session changed"})
            return fn(*args, **kwargs)
        binding = _pin_page(task_id)
        if binding is None:
            return json.dumps({"success": False, "error_type": "page_binding_required",
                               "error": "No current browser page owned by this task. Navigate first."})
        token = _PAGE_BINDING.set(binding)
        try:
            return fn(*args, **kwargs)
        finally:
            _PAGE_BINDING.reset(token)
    return bound


def _binding_is_current(binding):
    from tools.browser_tool import (
        _active_sessions, _last_session_key, _is_camofox_mode, _is_browser_use_cli_mode,
    )
    if _is_camofox_mode():
        return False
    if binding.driver == "browser-use-lease":
        from tools import browser_use_target as target
        return (target.browser_use_owns(binding.task_id) and target._pin.get() is binding.key
                and target.route_for_task(binding.task_id) is binding.key.route)
    if binding.driver == "browser-use":
        from tools import browser_use_cli as bu
        from tools.browser_supervisor import SUPERVISOR_REGISTRY
        return (_is_browser_use_cli_mode() and bu.last_route(binding.task_id) == binding.key
                and SUPERVISOR_REGISTRY.get(binding.task_id) is binding.key["supervisor"])
    return (not _is_browser_use_cli_mode()
            and _last_session_key(binding.task_id) == binding.key
            and _active_sessions.get(binding.key) is binding.session)


def _driver_eval_bound(binding, expression: str) -> Dict[str, Any]:
    """Evaluate in the navigation driver's CURRENT page, only while it is still the pinned document."""
    witness = json.dumps(binding.witness)
    guarded = f"window[{witness}] === true ? ({expression}) : null"
    if binding.driver == "browser-use-lease":
        return _selected_target_eval(binding.task_id, guarded)
    if binding.driver == "browser-use":
        from tools import browser_use_cli as bu
        seen = bu.driver_eval(binding.task_id, binding.key, guarded)
        if not seen or (seen["target_id"], seen["session_id"], seen.get("pid")) != binding.target:
            return {"success": False, "error": "Current browser page changed"}
        return {"success": True, "result": seen.get("value")}
    from tools.browser_tool_session import _run_browser_command
    result = _run_browser_command(binding.key, "eval", [guarded])
    return {"success": bool(result.get("success")),
            "result": (result.get("data") or {}).get("result")}


# ---------------------------------------------------------------------------
# Availability check
# ---------------------------------------------------------------------------

def _check_vault_available() -> bool:
    """Schema-gate: the vault tools ride with the browser. An empty vault still needs
    browser_vault_save_login so the agent can offer to remember a login the first time it meets a
    form; hiding the tools until an item exists meant nobody ever discovered the feature."""
    from tools.browser_tool_install import check_browser_requirements
    from tools.browser_use_cli import is_browser_use_cli_mode
    # check_browser_requirements() is False by design in Browser Use mode (browser_exec replaces the
    # built-in surface); the vault serves both stacks.
    return bool(is_browser_use_cli_mode() or check_browser_requirements())


# ---------------------------------------------------------------------------
# JS evaluation plumbing (server-side; results never carry secret values)
# ---------------------------------------------------------------------------

def _eval_js(task_id: str, expression: str) -> Dict[str, Any]:
    """Evaluate NON-SECRET JS on the current page (inspection, origin reads).

    During a vault operation, use the navigation driver's current document,
    guarded by the operation witness. Outside one, prefer the supervisor and
    fall back to the agent-browser CLI. Never use this for expressions that
    embed secret values — the fallback places the expression in subprocess
    argv. Use :func:`_eval_js_secret` for secret-bearing expressions.
    """
    binding = _PAGE_BINDING.get()
    if binding is not None:
        if not _binding_is_current(binding):
            return {"success": False, "error": "Browser session changed"}
        return _driver_eval_bound(binding, expression)
    from tools.browser_use_target import browser_use_owns
    if browser_use_owns(task_id):
        return _selected_target_eval(task_id, expression)
    try:
        from tools.browser_supervisor import SUPERVISOR_REGISTRY

        supervisor = SUPERVISOR_REGISTRY.get(task_id)
        if supervisor is not None:
            sup = supervisor.evaluate_runtime(expression)
            if sup.get("ok"):
                return {"success": True, "result": sup.get("result")}
            err = str(sup.get("error") or "")
            if "supervisor" not in err.lower():
                return {"success": False, "error": err}
    except ImportError:
        pass
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("vault fill: supervisor eval unavailable (%s)", exc)

    from tools.browser_tool import _last_session_key
    from tools.browser_tool_session import _run_browser_command

    effective = _last_session_key(task_id)
    result = _run_browser_command(effective, "eval", [expression])
    if not result.get("success"):
        return {"success": False, "error": result.get("error", "eval failed")}
    return {"success": True, "result": result.get("data", {}).get("result")}


def _ensure_supervisor(task_id: str):
    """The supervisor for ``task_id``, attaching one on demand for a LOCAL built-in browser session.

    Cloud/CDP-override sessions and browser_exec attach their supervisor when the session is created;
    a local agent-browser ``--session`` has no ``cdp_url`` of its own, so nothing did. Ask the daemon
    for the packaged Chromium's endpoint (``get cdp-url``: same daemon, same reaper) and attach.
    Returns None when no endpoint is reachable; the fill then refuses rather than touching argv."""
    from tools.browser_supervisor import SUPERVISOR_REGISTRY
    from tools.browser_use_target import browser_use_owns, supervisor_for_task

    if browser_use_owns(task_id):
        return supervisor_for_task(task_id)
    from tools.browser_tool import _last_session_key
    task_id = _last_session_key(task_id)
    supervisor = SUPERVISOR_REGISTRY.get(task_id)
    if supervisor is not None:
        return supervisor
    from tools.browser_tool_cdp import _get_dialog_policy_config, _resolve_cdp_override
    from tools.browser_tool_session import _run_browser_command

    res = _run_browser_command(task_id, "get", ["cdp-url"])
    cdp_url = str(((res or {}).get("data") or {}).get("cdpUrl") or "") if (res or {}).get("success") else ""
    if not cdp_url:
        return None
    policy, timeout_s = _get_dialog_policy_config()
    try:
        return SUPERVISOR_REGISTRY.get_or_start(task_id=task_id, cdp_url=_resolve_cdp_override(cdp_url),
                                                dialog_policy=policy, dialog_timeout_s=timeout_s)
    except Exception as exc:
        logger.debug("vault fill: supervisor attach to local session failed (%s)", exc)
        return None


def _eval_js_secret(task_id: str, expression: str) -> Dict[str, Any]:
    """Evaluate a SECRET-BEARING JS expression. Supervisor CDP-WS only.

    Fails closed: there is deliberately NO fallback to the agent-browser CLI
    ``eval`` path, because that places the expression — and therefore the
    credential bytes — in subprocess argv, visible to any process listing.
    When no supervisor session is available the caller gets a typed refusal
    (``error_type='supervisor_required'``) and nothing is written.
    """
    binding = _PAGE_BINDING.get()
    try:
        # Browser Use: only the supervisor browser_exec attached for this route; never start a
        # built-in browser to find one.
        supervisor = (binding.key["supervisor"] if binding is not None and binding.driver == "browser-use"
                      else _ensure_supervisor(task_id))
    except Exception as exc:
        logger.debug("vault fill: supervisor unavailable (%s)", exc)
        supervisor = None

    if supervisor is None:
        return {
            "success": False,
            "error_type": "supervisor_required",
            "error": (
                "Vault fill requires the supervised browser session (direct "
                "CDP WebSocket). The fallback eval path would place the "
                "credential in subprocess argv, so it is never used for "
                "secrets. Start the browser through the Hermes-managed "
                "session and retry."
            ),
        }

    # Re-admit at the WRITE. The handler-level fence (_fenced_page_op) admitted before a possibly
    # human-length prompt (enter_code waits for the user's code); a takeover during that wait must
    # refuse here, before the credential lands in a page the human is now typing into. The outer
    # epoch check only discards the result, and a fill is a side effect, not a result.
    if _bot_desktop_browser_session(task_id):
        from tools.bot_desktop import lease as _bd_lease
        try:
            _bd_lease.assert_agent_may_act()
        except _bd_lease.HumanHasControl as exc:
            return {"success": False, "error_type": "human_has_control", "error": str(exc)}

    if binding is not None:
        if binding.task_id != task_id or not _binding_is_current(binding):
            return {"success": False, "error_type": "page_changed", "error": "Browser session changed"}
        # Verify the driver's current document again after any human prompt or
        # backend resolution. Never send secret-bearing JS through the driver.
        if not _eval_js(task_id, "true").get("result"):
            return {"success": False, "error_type": "page_changed", "error": "Current browser page changed"}
        witness = json.dumps(binding.witness)
        # Under a daemon selection lease the write is already pinned to the leased target
        # (supervisor_for_task); otherwise the supervisor selects the bound document here.
        if binding.driver != "browser-use-lease":
            focused = supervisor.focus_page("", accept=f"window[{witness}] === true")
            if not focused.get("ok"):
                return {"success": False, "error_type": "page_changed", "error": "Bound document no longer exists"}
            # Browser Use names its current target: the supervisor must have selected exactly that one.
            if binding.target is not None and focused.get("target_id") != binding.target[0]:
                return {"success": False, "error_type": "page_changed", "error": "Supervisor selected another tab"}
            if not _binding_is_current(binding) or not _eval_js(task_id, "true").get("result"):
                return {"success": False, "error_type": "page_changed", "error": "Browser page changed during target selection"}
        expression = (f"window[{witness}] === true ? ({expression}) : "
                      "JSON.stringify({refused: 'origin_changed'})")
    sup = supervisor.evaluate_runtime(expression)
    if sup.get("ok"):
        return {"success": True, "result": sup.get("result")}
    return {
        "success": False,
        "outcome": sup.get("outcome", "unknown"),
        "error_type": "supervisor_required"
        if "supervisor" in str(sup.get("error") or "").lower()
        else "eval_failed",
        "error": str(sup.get("error") or "eval failed"),
    }


def _parse_json_result(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return raw
    return raw


def _current_page_origin(task_id: str) -> Optional[str]:
    res = _eval_js(task_id, "window.location.href")
    if not res.get("success"):
        return None
    href = str(res.get("result") or "").strip().strip('"').strip("'")
    if not href or href == "about:blank":
        return None
    try:
        from agent.vault_store import normalize_origin

        return normalize_origin(href)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def browser_vault_list() -> str:
    """List login handles + metadata across every enabled backend. Passwords are never included.

    A locked external manager contributes no items; instead it is reported under ``locked`` so the
    agent knows to call browser_vault_fill (which prompts the user to unlock) or tell the user.
    """
    from agent.vault_backends import enabled_backends
    from agent.vault_backends.unlock import can_prompt_here

    items, locked, errors = [], [], []
    for backend in enabled_backends():
        if backend.needs_unlock and not backend.is_unlocked():
            locked.append({"backend": backend.name, "display_name": backend.display_name,
                           "unlock": "browser_vault_unlock" if can_prompt_here() else "unavailable_in_this_session"})
            continue
        try:
            metas = backend.list_items()
        except Exception as exc:
            errors.append({"backend": backend.name, "error": str(exc)[:200]})
            continue
        for meta in metas:
            entry = {"handle": meta.id, "backend": backend.name, "label": meta.label, "kind": meta.kind,
                     "origin": meta.origin, "available": _fill_path(meta) is not None}
            if len(meta.allowed_origins) > 1:
                entry["allowed_origins"] = list(meta.allowed_origins)
            for key in ("category", "vault", "last4"):
                if getattr(meta, key, None):
                    entry[key] = getattr(meta, key)
            if not meta.fill_supported:
                entry["fill_supported"] = False
            elif meta.kind == "payment" and not meta.origin:
                entry["origin_binding"] = "the user approves the exact https page for each fill"
            if meta.has_otp or backend.needs_unlock:
                entry["two_factor"] = "automatic" if meta.has_otp else "automatic if the manager stores a TOTP seed, else the user is asked"
            if meta.identifier:
                entry["identifier"] = meta.identifier
                entry["identifier_type"] = meta.identifier_type
            items.append(entry)
    out: Dict[str, Any] = {"success": True, "items": items}
    if not items:
        out["hint"] = ("No saved logins. On a login page, call browser_vault_save_login to ask the user to save one. "
                       "Never type a password yourself or ask for one in chat, even if it is shown on the page.")
    if locked:
        out["locked"] = locked
    if errors:
        out["errors"] = errors
    return json.dumps(out, ensure_ascii=False)


def browser_vault_unlock(backend_name: str) -> str:
    """Ask the user (via the surface's masked prompt) to unlock an external manager for this session."""
    from agent.vault_backends import enabled_backends
    from agent.vault_backends.unlock import can_prompt_here, get_unlock_prompt_callback

    backend = next((b for b in enabled_backends() if b.name == backend_name and b.needs_unlock), None)
    if backend is None:
        return json.dumps({"success": False, "error": f"No unlockable vault backend named {backend_name!r}."})
    if backend.is_unlocked():
        return json.dumps({"success": True, "backend": backend.name, "already_unlocked": True})
    if not can_prompt_here():
        return json.dumps({"success": False, "error_type": "unlock_unavailable",
                           "error": (f"{backend.display_name} is locked and this session cannot prompt for the "
                                     "master password (headless/cron/API). Unlock it from an interactive Hermes "
                                     "session or the Desktop app first.")})
    prompt = get_unlock_prompt_callback()
    master = prompt(backend.name, backend.display_name) if prompt else ""
    if not master:
        return json.dumps({"success": False, "error_type": "unlock_cancelled",
                           "error": f"The user declined to unlock {backend.display_name}."})
    try:
        backend.unlock(master)  # type: ignore[attr-defined]
    except Exception as exc:
        return json.dumps({"success": False, "error_type": "unlock_failed", "error": str(exc)[:300]})
    finally:
        del master
    return json.dumps({"success": True, "backend": backend.name})


@_bound_page
def browser_vault_save_login(label: str = "", task_id: Optional[str] = None) -> str:
    """Ask the user (masked prompt on their surface) for the login of the CURRENT page, store it in the local
    vault bound to that origin, and fill the password at once. The values never enter the conversation."""
    from agent.vault_backends.unlock import can_prompt_here, get_save_login_prompt_callback
    from agent.vault_store import get_vault_store

    effective_task_id = task_id or "default"
    origin = _current_page_origin(effective_task_id)
    if not origin:
        return json.dumps({"success": False, "error": "Open the site's login page first; the login is saved for that page's origin."})
    prompt = get_save_login_prompt_callback()
    if prompt is None or not can_prompt_here():
        return json.dumps({"success": False, "error_type": "prompt_unavailable",
                           "error": (f"This session cannot ask the user for a login (headless/cron/API). Tell them to run "
                                     f"`hermes vault add` or use Desktop → Settings → Passwords & Logins for {origin}.")})
    host = origin.split("://", 1)[-1]
    site = label.strip() or host
    answer = prompt(origin, host)  # the prompt names the site by host: the user recognises URLs, not agent labels
    if not answer or not answer.get("password") or not answer.get("identifier"):
        return json.dumps({"success": False, "error_type": "save_declined",
                           "error": "The user chose not to save a login for this site. Do not ask again this turn."})
    identifier = str(answer["identifier"]).strip()
    id_type = "email" if "@" in identifier else ("phone" if identifier.lstrip("+").isdigit() else "username")
    try:
        meta = get_vault_store().add_item("login", site, {"identifier_type": id_type, "identifier": identifier,
                                                        "password": str(answer["password"])}, origin=origin)
    except Exception as exc:
        return json.dumps({"success": False, "error_type": "save_failed", "error": str(exc)[:200]})
    finally:
        answer.clear()
    filled = json.loads(browser_vault_fill(meta.id, task_id=effective_task_id))
    return json.dumps({"success": True, "handle": meta.id, "origin": origin, "identifier": identifier,
                       "identifier_type": id_type, "fill": filled,
                       "next": "Type the identifier into the username field if the form has one, then submit."},
                      ensure_ascii=False)


@_bound_page
def browser_vault_enter_code(handle: str = "", task_id: Optional[str] = None) -> str:
    """Second factor: fill the one-time code the CURRENT page asks for. If the saved login (``handle``) has an
    authenticator seed, the code is minted server-side and nobody is asked; otherwise the user is prompted on
    their surface for the code their phone/email/app shows. The code goes into the page over the supervisor
    socket and never enters the conversation."""
    from agent.redact import register_vault_redaction_value
    from agent.vault_backends import backend_for_handle
    from agent.vault_backends.unlock import can_prompt_here, get_code_prompt_callback
    from agent.vault_login_classifier import LoginControl, build_fill_js, build_inspection_js, build_otp_fills, classify_otp_controls

    effective_task_id = task_id or "default"
    origin = _current_page_origin(effective_task_id)
    if not origin:
        return json.dumps({"success": False, "error": "No page with a code field is open."})
    site = origin.split("://", 1)[-1]

    nonce = secrets.token_hex(8)
    inspect = _eval_js(effective_task_id, build_inspection_js(nonce))
    raw_controls = _parse_json_result(inspect.get("result")) if inspect.get("success") else None
    if isinstance(raw_controls, str):
        raw_controls = _parse_json_result(raw_controls)
    otp_controls = classify_otp_controls([LoginControl.from_dict(r) for r in (raw_controls or []) if isinstance(r, dict)])
    if not otp_controls:
        return json.dumps({"success": False, "error_type": "no_code_field",
                           "error": ("No one-time-code field on the current page. If the site wants a passkey, hardware key or "
                                     "an approval tap in an app, tell the user to complete it on their device and wait for the page to move on.")})

    code: Optional[str] = None
    source = "user"
    backend = backend_for_handle(handle) if handle else None
    if backend is not None:
        try:
            code = backend.resolve_otp(handle)
        except Exception:
            code = None
        if code:
            source = backend.name
    if not code:
        prompt = get_code_prompt_callback()
        if prompt is None or not can_prompt_here():
            return json.dumps({"success": False, "error_type": "prompt_unavailable",
                               "error": (f"{site} asks for a one-time code and this session cannot ask the user (headless/cron/API). "
                                         "Save an authenticator key for this login so codes can be generated automatically.")})
        code = (prompt(site, "") or "").strip().replace(" ", "").replace("-", "")
        if not code:
            return json.dumps({"success": False, "error_type": "code_declined",
                               "error": "The user did not enter a code. Do not ask again this turn."})

    register_vault_redaction_value(code)
    fills = build_otp_fills(otp_controls, code)
    result = _eval_js_secret(effective_task_id, build_fill_js(fills, expected_origin=origin, nonce=nonce))
    del code
    if not result.get("success"):
        return json.dumps({"success": False, "outcome": result.get("outcome", "unknown"),
                           "error": str(result.get("error") or "fill failed")[:200]})
    parsed = _parse_json_result(result.get("result"))
    if isinstance(parsed, str):
        parsed = _parse_json_result(parsed)
    if isinstance(parsed, dict) and parsed.get("refused") == "origin_changed":
        return json.dumps({"success": False, "error_type": "origin_changed", "error": "The page navigated before the code could be entered. Nothing was written."})
    filled = int(parsed.get("filled", 0)) if isinstance(parsed, dict) else 0
    return json.dumps({"success": bool(filled), "filled_fields": filled, "origin": origin, "source": source,
                       "next": "Submit the form (many sites auto-submit when the last digit lands)."})


@_bound_page
def browser_vault_fill(handle: str, task_id: Optional[str] = None) -> str:
    """Fill the current page from a vault handle: the password of a login, card fields of a payment
    item (one human approval per fill, naming the page), address fields of a local address item.

    For a login the identifier is agent-visible metadata (see
    browser_vault_list) and is typed by the agent via normal input tools.
    The password is resolved server-side and injected via in-page JS over
    the supervisor CDP WebSocket; the result reports only counts/metadata.
    """
    from agent.redact import register_vault_redaction_value
    from agent.vault_login_classifier import (
        ClassifiedLoginControl,
        LoginControl,
        build_fill_js,
        build_inspection_js,
        classify_checkout_control,
        classify_login_control,
        select_checkout_fills,
        select_password_fill,
    )
    from agent.vault_backends import UnlockRequired, backend_for_handle
    from agent.vault_backends.base import VaultAccessDenied
    from agent.vault_store import ADDRESS_FIELDS, PAYMENT_FIELDS, scrub_secret_from_text

    effective_task_id = task_id or "default"
    backend = backend_for_handle(handle)
    if backend is not None and backend.needs_unlock and not backend.is_unlocked():
        unlocked = json.loads(browser_vault_unlock(backend.name))
        if not unlocked.get("success"):
            return json.dumps(unlocked)

    try:
        meta = backend.get_meta(handle) if backend is not None else None
    except UnlockRequired:
        return json.dumps({"success": False, "error_type": "unlock_required",
                           "error": f"{backend.display_name} locked again; call browser_vault_unlock."})
    if meta is None:
        return json.dumps(
            {
                "success": False,
                "error": (
                    f"No vault item with handle {handle!r}. Use browser_vault_list. "
                    "To save a credential: run `hermes vault add` in a terminal, or "
                    "in the desktop app open Settings → Credential Vault."
                ),
            }
        )
    path = _fill_path(meta)
    if path is None and meta.kind in _FILLABLE_KINDS and meta.fill_supported:
        return json.dumps({"success": False, "error_type": "no_origin",
                           "error": f"Vault item {handle!r} has no bound origin; {meta.kind} items are filled only on the site they were saved for."})
    if path is None:
        # Before any secret read: only the three fill paths below exist. A metadata-only manager item
        # (kind ``reference``: password, document, SSH key...) or any unknown kind stops here.
        return json.dumps({"success": False, "error_type": "not_fillable",
                           "error": (f"Vault item {handle!r} ({meta.category or meta.kind}) is listed for reference "
                                     "only; only logins and payment cards (and local address items) can be filled.")})

    # ── Origin binding pre-check (cheap early exit; the authoritative check
    # runs synchronously inside the fill script itself) ──────────────────────
    # Manager items can bind several websites (e.g. amazon.co.uk + www.amazon.co.uk);
    # every saved origin is a valid fill target. Matching stays exact-origin —
    # nothing wildcard/parent-domain is ever inferred. A payment card with no saved
    # website has no allowed list: the user approves the one https page it may fill.
    allowed = list(meta.allowed_origins) or ([str(meta.origin)] if meta.origin else [])
    page_origin = _current_page_origin(effective_task_id)
    if not page_origin:
        return json.dumps(
            {"success": False, "error": "Could not determine the current page origin. Navigate to the login page first."}
        )
    if path == "approved_page":
        if not page_origin.startswith("https://"):
            return json.dumps({"success": False, "error_type": "origin_mismatch",
                               "error": f"Refused: {page_origin} is not an https page. A card is filled only on https checkouts."})
        allowed = [page_origin]
    if page_origin not in allowed:
        return json.dumps(
            {
                "success": False,
                "error_type": "origin_mismatch",
                "error": (
                    f"Refused: current page origin ({page_origin}) does not match "
                    f"the vault item's bound origin(s) ({', '.join(allowed)}). Vault fills "
                    "only run on the exact origin(s) the credential was saved for."
                ),
            }
        )
    if meta.kind == "payment":
        # One approval per fill, naming the page that will receive the card. Nothing is resolved before it,
        # and the page is re-read afterwards: a navigation while the prompt was open refuses here, and one
        # after this read is refused by the fill script's own origin assert.
        card = meta.label + (f" ending {meta.last4}" if meta.last4 else "")
        if not _confirm_payment_fill(card, page_origin):
            return json.dumps({"success": False, "error_type": "payment_declined",
                               "error": "The user did not confirm filling this payment card. Do not retry; ask them instead."})
        if _current_page_origin(effective_task_id) != page_origin:
            return json.dumps({"success": False, "error_type": "origin_changed",
                               "error": f"Refused: the page left {page_origin} while the approval was open. Nothing was written."})

    # ── Inspect + classify page controls ────────────────────────────────────
    nonce = secrets.token_hex(8)  # binds this fill to THIS inspection's stamps
    inspect = _eval_js(effective_task_id, build_inspection_js(nonce))
    if not inspect.get("success"):
        return json.dumps(
            {"success": False, "error": f"Could not inspect page inputs: {inspect.get('error', 'eval failed')}"}
        )
    raw_controls = _parse_json_result(inspect.get("result"))
    if isinstance(raw_controls, str):
        raw_controls = _parse_json_result(raw_controls)
    if not isinstance(raw_controls, list):
        return json.dumps({"success": False, "error": "Page input inspection returned no usable controls."})

    classify = classify_login_control if meta.kind == "login" else classify_checkout_control
    classified: list[ClassifiedLoginControl] = []
    for raw in raw_controls:
        if not isinstance(raw, dict):
            continue
        result = classify(LoginControl.from_dict(raw))
        if result is not None:
            classified.append(result)
    if not classified:
        return json.dumps({"success": False, "error": f"No {meta.kind} form fields were found on the current page."})

    # ── Resolve secret and fill (secret never enters any logged string) ─────
    try:
        if meta.kind == "login":
            secret = {"password": backend.resolve_password(handle)}
            fills = select_password_fill(classified, secret["password"])
        else:
            secret = backend.resolve_secret(handle)
            fills = select_checkout_fills(classified, secret, PAYMENT_FIELDS if meta.kind == "payment" else ADDRESS_FIELDS)
    except UnlockRequired:
        return json.dumps({"success": False, "error_type": "unlock_required",
                           "error": f"{backend.display_name} locked again; call browser_vault_unlock."})
    except VaultAccessDenied:
        return json.dumps({"success": False, "error_type": "vault_access_denied",
                           "error": f"{backend.display_name} does not let this agent read that item's vault. Nothing was filled."})
    except Exception as exc:  # backend messages name fields, never values; nothing was written
        logger.info("vault fill: resolving %s failed (%s)", handle, type(exc).__name__)
        return json.dumps({"success": False, "error_type": "resolve_failed",
                           "error": f"Could not read the saved {meta.kind} item: {str(exc)[:200]}. Nothing was filled."})
    if not fills:
        return json.dumps(
            {"success": False, "error": f"No fillable {meta.kind} field matched the saved item on this page."}
        )

    # Register the secret bytes with the model-egress redaction boundary
    # BEFORE they touch the page: any later browser_* result (including
    # browser_cdp Runtime.evaluate reads) that echoes them is scrubbed.
    # Address values are not secrets but the card fields are: register every payment value.
    for value in (secret.values() if meta.kind == "payment" else [secret.get("password", "")]):
        register_vault_redaction_value(value)
    if meta.kind == "payment":
        # Checkout inputs commonly re-insert separators as the number is typed; a later read of the field
        # returns the formatted form, which an exact-value registration would miss.
        for formatted in _formatted_card_numbers(str(secret.get("card_number") or "")):
            register_vault_redaction_value(formatted)

    try:
        fill_result = _eval_js_secret(
            effective_task_id, build_fill_js(fills, expected_origin=page_origin, nonce=nonce)
        )
    except Exception as exc:
        # Strip any secret material from exception text before surfacing.
        return json.dumps(
            {"success": False, "error": scrub_secret_from_text(str(exc), secret)}
        )
    if not fill_result.get("success"):
        err = scrub_secret_from_text(str(fill_result.get("error") or "fill failed"), secret)
        out = {"success": False, "error": err, "outcome": fill_result.get("outcome", "unknown")}
        if fill_result.get("error_type"):
            out["error_type"] = fill_result["error_type"]
        return json.dumps(out)

    parsed = _parse_json_result(fill_result.get("result"))
    if isinstance(parsed, str):
        parsed = _parse_json_result(parsed)
    if isinstance(parsed, dict) and parsed.get("refused") == "origin_changed":
        return json.dumps(
            {
                "success": False,
                "error_type": "origin_changed",
                "error": (
                    "Refused: the page navigated away from the bound origin "
                    f"({page_origin}) before the fill could run "
                    f"(now on {parsed.get('found') or 'unknown'}). "
                    "Nothing was written."
                ),
            }
        )
    filled = parsed.get("filled", 0) if isinstance(parsed, dict) else 0

    out = {"success": bool(filled), "filled_fields": int(filled), "backend": backend.name,
           "kind": meta.kind, "origin": page_origin}
    if meta.kind == "login":
        out["next"] = ("Submit. If the site then asks for a verification code, call browser_vault_enter_code with this handle"
                       + (" (a code will be generated automatically)." if meta.has_otp else "."))
    if meta.kind != "login":
        out["fields"] = sorted(f["token"] for f in fills)  # which controls were targeted, never the values
    return json.dumps(out)


_FILLABLE_KINDS = ("login", "payment", "address")


def _formatted_card_numbers(number: str) -> list:
    """The display forms of a card number a checkout input may produce: 4-4-4-4(-3) groups and the
    Amex 4-6-5 grouping, each joined by a space or a hyphen."""
    digits = "".join(ch for ch in number if ch.isdigit())
    if len(digits) < 12:
        return []
    groupings = [[digits[i:i + 4] for i in range(0, len(digits), 4)]]
    if len(digits) == 15:
        groupings.append([digits[:4], digits[4:10], digits[10:]])
    return [sep.join(g) for g in groupings for sep in (" ", "-")]


def _fill_path(meta) -> Optional[str]:
    """How an item may be filled, or None. ``bound``: exact match against its saved origin(s).
    ``approved_page``: a password-manager card (1Password ``CREDIT_CARD``; cards there carry no website)
    with no saved website, filled only on the https page the user approves for that single fill.
    Everything else (``reference`` items, unknown kinds, a local item with no origin) has no fill path."""
    if meta.kind not in _FILLABLE_KINDS or not getattr(meta, "fill_supported", True):
        return None
    if meta.kind == "login" or meta.origin or meta.allowed_origins:
        return "bound"
    return "approved_page" if meta.kind == "payment" and meta.category == "CREDIT_CARD" else None


def _confirm_payment_fill(label: str, origin: str) -> bool:
    """Human confirmation before a card is written into a page: a prompt injection that reaches a checkout
    must not be able to spend. Routes through the approval surface of the active session (gateway button
    round-trip or CLI panel); headless sessions cannot confirm and the fill is refused.

    ``vault.payment_fill_confirm: false`` skips the prompt: the operator's own instruction to pay is the
    consent, and the mechanical guards (https-only, exact page-origin binding re-asserted at fill time,
    card bytes registered for redaction) still apply. Anything but an explicit ``false`` keeps the prompt."""
    if not _payment_fill_confirm_enabled():
        logger.info("vault fill: payment prompt skipped by config (vault.payment_fill_confirm=false) for %s", origin)
        return True
    from tools.approval_prompt import request_elicitation_consent

    return request_elicitation_consent(
        f"Fill payment card '{label}' on {origin}",
        f"The agent wants to enter your saved card details into the page at {origin}, once. The card number "
        "and CVC never enter the conversation. Approve only if you intend to pay on this exact site.",
        surface="vault-payment", title="Confirm payment card fill?") == "accept"


def _payment_fill_confirm_enabled() -> bool:
    """``vault.payment_fill_confirm`` from config.yaml; only a literal ``false`` disables the prompt.
    A missing block, an unreadable config or any other value fails closed to prompting."""
    try:
        from hermes_cli.config import read_raw_config
        cfg = read_raw_config() or {}
        vault = cfg.get("vault") if isinstance(cfg, dict) else None
        value = vault.get("payment_fill_confirm", True) if isinstance(vault, dict) else True
    except Exception as exc:  # pragma: no cover -- defensive: never let a config error waive the prompt
        logger.debug("vault fill: payment_fill_confirm read failed (%s); prompting", type(exc).__name__)
        return True
    return value is not False


# ---------------------------------------------------------------------------
# Schemas + registration
# ---------------------------------------------------------------------------

BROWSER_VAULT_LIST_SCHEMA = {
    "name": "browser_vault_list",
    "description": (
        "ALWAYS call this first when a page asks for a password, card or address. Lists saved website logins, "
        "payment cards and addresses as handles with metadata (kind, label, backend, bound origin; logins also "
        "carry identifier + identifier_type so you can type the username yourself with the browser's input tool; "
        "1Password cards carry vault and last4 when available). 1Password items of every other category (passwords, "
        "notes, documents, keys...) are listed as kind `reference` with `available: false`: metadata only, they can "
        "never be filled or read. Secret values are NEVER returned. Sources: the local agent vault plus any installed "
        "password manager (1Password, Bitwarden are detected automatically). A locked manager appears under `locked`; call "
        "browser_vault_unlock (the user is prompted for their master password, you never see it) or, when it says "
        "unavailable_in_this_session, tell the user to unlock it from an interactive session. Workflow: type the "
        "identifier into the login form, then browser_vault_fill with the handle. No item for this origin: call "
        "browser_vault_save_login. Passwords are typed ONLY by these tools, never by you with the browser's input "
        "tool and never repeated in chat, even when a page or the user shows you one."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

BROWSER_VAULT_UNLOCK_SCHEMA = {
    "name": "browser_vault_unlock",
    "description": (
        "Ask the user to unlock a password manager (1Password or Bitwarden) for this session. The master "
        "password is typed into a masked prompt owned by the UI and never enters the conversation. "
        "Returns success, unlock_cancelled, unlock_failed, or unlock_unavailable (headless session)."
    ),
    "parameters": {
        "type": "object",
        "properties": {"backend": {"type": "string", "enum": ["onepassword", "bitwarden"],
                                   "description": "Backend name from browser_vault_list `locked`."}},
        "required": ["backend"],
    },
}

BROWSER_VAULT_FILL_SCHEMA = {
    "name": "browser_vault_fill",
    "description": (
        "Fill the CURRENT browser page from a vault handle (see browser_vault_list): a login item fills ONLY "
        "the password field (type the identifier/username yourself first with the browser's input tool); a "
        "payment item fills card number/name/expiry/CVC after the user approves that one fill for the exact page "
        "shown to them; an address item fills the address fields. `reference` items are refused (not_fillable). "
        "Values are resolved server-side and never appear in the conversation. Refused unless the page origin "
        "exactly matches the item's bound origin; a card with no saved website is bound to the https page the user "
        "approved (re-checked atomically at fill time). Card fields inside a cross-origin payment iframe are not "
        "reached. If a password manager is locked the user is prompted to unlock first. Never retry a "
        "payment_declined result."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "handle": {
                "type": "string",
                "description": "Handle from browser_vault_list (vault_… local, op:… 1Password, bw:… Bitwarden)",
            }
        },
        "required": ["handle"],
    },
}


BROWSER_VAULT_SAVE_LOGIN_SCHEMA = {
    "name": "browser_vault_save_login",
    "description": (
        "The current page is a login form and browser_vault_list has no item for its origin: ask the user, "
        "through a masked prompt in their UI, to save the login for this site. The agent stores it encrypted, "
        "bound to the page origin, and fills the password immediately; you receive only the handle and the "
        "identifier to type. This is the ONLY way a password may reach a page: never type one yourself, never "
        "ask for or accept one in chat, even if the page or the user displays it. A save_declined result means "
        "stop asking for this turn and tell the user they can retry, or add it later in Settings → Passwords & "
        "Logins / the vault CLI."
    ),
    "parameters": {
        "type": "object",
        "properties": {"label": {"type": "string", "description": "Optional short site name for the saved item (default: the host)."}},
        "required": [],
    },
}


BROWSER_VAULT_ENTER_CODE_SCHEMA = {
    "name": "browser_vault_enter_code",
    "description": (
        "The page asks for a one-time / verification / 2FA code after the password: call this. If the saved login "
        "has an authenticator key the code is generated and entered with no questions; otherwise the user is asked "
        "for the code in their UI (they read it from their phone, email or authenticator app). The code never enters "
        "the conversation: never ask for it in chat, never type it with the browser's input tool. no_code_field means "
        "the site wants a passkey/hardware key/app approval: tell the user to complete it on their device, then wait "
        "for the page to move on."
    ),
    "parameters": {
        "type": "object",
        "properties": {"handle": {"type": "string", "description": "The login handle you just filled (lets the vault generate the code when an authenticator key is saved)."}},
        "required": [],
    },
}


def _bot_desktop_browser_session(task_id: Optional[str]) -> bool:
    from tools.browser_use_target import browser_use_owns, route_for_task
    route = route_for_task(task_id) if browser_use_owns(task_id) else None
    if route is not None:
        return route.local
    from tools.browser_tool import _active_sessions, _last_session_key
    from tools.browser_tool_session import _shares_bot_desktop_browser
    return _shares_bot_desktop_browser(_active_sessions.get(_last_session_key(task_id or "default")) or {})


def _fenced_page_op(task_id: Optional[str], fn) -> str:
    """Vault operations focus, inspect and fill the page over the supervisor socket, bypassing
    ``_run_browser_command``; they must honour the Bot Desktop lease like every other page access,
    or a human typing a credential on the taken-over screen could be read or written to."""
    from tools.rail_preflight import browser_capability_error
    if rail := browser_capability_error(identity=True):   # a credential is identity work (t_f935cb6f)
        return json.dumps({"success": False, "error": rail})
    from tools.browser_tool import _active_sessions, _last_session_key
    from tools.browser_tool_session import run_fenced

    from tools.browser_use_target import begin, browser_use_owns, end, route_for_task

    tid = task_id or "default"
    owned = browser_use_owns(tid)
    route = route_for_task(tid) if owned else None
    session = ({"features": {"local": route.local}} if route is not None else
               _active_sessions.get(_last_session_key(tid)) or {})

    def pinned_op():
        token = None
        try:
            if route_for_task(tid) is not route:
                raise RuntimeError("Browser Use route changed after takeover fence selection")
            if owned:
                token = begin(tid, expected_route=route)
        except Exception:
            return {"raw": json.dumps({"success": False, "error_type": "selected_target_unavailable",
                    "outcome": "not_dispatched",
                    "error": "Browser Use selected target changed, closed, or is unavailable; select a live tab with browser_exec and retry. Nothing was written."})}
        try:
            return {"raw": fn()}
        except Exception:
            # The callback may already have dispatched a fill (or saved an item).
            # Never misreport an arbitrary callback failure as a pre-write refusal.
            return {"raw": json.dumps({"success": False, "error_type": "operation_outcome_unknown",
                    "outcome": "unknown", "error": "Vault operation outcome unknown; inspect the page and saved item before retrying."})}
        finally:
            if token is not None:
                end(token)

    res = run_fenced(session, pinned_op)
    return res["raw"] if "raw" in res else json.dumps(res)


def _handle_vault_enter_code(args: Dict[str, Any], **kwargs) -> str:
    tid = kwargs.get("task_id")
    return _fenced_page_op(tid, lambda: browser_vault_enter_code(handle=str(args.get("handle") or ""), task_id=tid))


def _handle_vault_save_login(args: Dict[str, Any], **kwargs) -> str:
    tid = kwargs.get("task_id")
    return _fenced_page_op(tid, lambda: browser_vault_save_login(label=str(args.get("label") or ""), task_id=tid))


def _handle_vault_list(args: Dict[str, Any], **kwargs) -> str:
    return browser_vault_list()


def _handle_vault_unlock(args: Dict[str, Any], **kwargs) -> str:
    return browser_vault_unlock(str(args.get("backend") or ""))


def _handle_vault_fill(args: Dict[str, Any], **kwargs) -> str:
    tid = kwargs.get("task_id")
    return _fenced_page_op(tid, lambda: browser_vault_fill(handle=str(args.get("handle") or ""), task_id=tid))


from tools.registry import no_cache_check_fn, registry  # noqa: E402

_check_vault_available = no_cache_check_fn(_check_vault_available)

registry.register(
    name="browser_vault_list",
    toolset="browser",
    schema=BROWSER_VAULT_LIST_SCHEMA,
    handler=_handle_vault_list,
    check_fn=_check_vault_available,
    emoji="🔐",
)

registry.register(
    name="browser_vault_unlock",
    toolset="browser",
    schema=BROWSER_VAULT_UNLOCK_SCHEMA,
    handler=_handle_vault_unlock,
    check_fn=_check_vault_available,
    emoji="🔐",
)

registry.register(
    name="browser_vault_save_login",
    toolset="browser",
    schema=BROWSER_VAULT_SAVE_LOGIN_SCHEMA,
    handler=_handle_vault_save_login,
    check_fn=_check_vault_available,
    emoji="🔐",
)

registry.register(
    name="browser_vault_enter_code",
    toolset="browser",
    schema=BROWSER_VAULT_ENTER_CODE_SCHEMA,
    handler=_handle_vault_enter_code,
    check_fn=_check_vault_available,
    emoji="🔐",
)

registry.register(
    name="browser_vault_fill",
    toolset="browser",
    schema=BROWSER_VAULT_FILL_SCHEMA,
    handler=_handle_vault_fill,
    check_fn=_check_vault_available,
    emoji="🔐",
)
