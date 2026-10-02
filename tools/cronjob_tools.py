"""Cron job management tool: one compressed action-oriented `cronjob_manage` tool
(schema/context bloat avoided); `cronjob()` stays callable for direct Python callers."""

import contextlib
import contextvars
import json
import logging
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import copy

from hermes_constants import display_hermes_home

logger = logging.getLogger(__name__)

# Per-turn identity of the CREATING agent's model, so ``cronjob(action="create")``
# can resolve model="auto" (or an unpinned LLM cron) to the model of whoever is
# making the job — instead of leaving it None to inherit the runtime primary
# (often Opus) at fire time.
#
# The turn publisher is task-local. The tool executor also binds the actual
# executing agent at dispatch: a separate asyncio task cannot see a ContextVar
# set after its context snapshot, and a process-global value can belong to a
# different concurrent session.
_current_agent_model: contextvars.ContextVar[Tuple[Optional[str], Optional[str]]] = contextvars.ContextVar(
    "cron_creating_agent_model", default=(None, None)
)


def set_current_agent_model(provider: Optional[str], model: Optional[str]) -> None:
    """Publish the running agent's (provider, model) for model="auto" resolution.

    Called per turn from ``agent/turn_context.py``. Best-effort and cheap; a bad
    value only means model="auto" falls back to leaving the cron unpinned.
    """
    try:
        _current_agent_model.set((provider or None, model or None))
    except Exception:  # never let context bookkeeping break a turn
        pass


def get_current_agent_model() -> Tuple[Optional[str], Optional[str]]:
    """Return the creating agent's (provider, model), or (None, None) if unset."""
    try:
        return _current_agent_model.get()
    except Exception:
        return (None, None)


# Sentinel a caller (or config) uses to mean "pin this cron to the creating
# agent's own model" rather than a literal model id.
_AUTO_MODEL = "auto"

# Reason recorded on a job whose flagship model was INHERITED through the
# "auto" sentinel rather than chosen by the caller. The flagship ban
# (hermes_cli.model_policy.validate_worker_model) guards caller-chosen worker
# routes; an auto pin copies the creating session's own elected model, so the
# session that is already allowed to run that model may pin its crons to it.
_AUTO_PIN_FLAGSHIP_REASON = "auto-pin: inherited creating agent's own elected model"


def _flagship_reason_for_auto_pin(
    resolved_model: Optional[str],
    auto_pin: bool,
    allow_flagship_reason: Optional[str],
) -> Optional[str]:
    """Return the ``allow_flagship_reason`` to persist alongside an auto pin.

    The ONE place the auto-pin exemption is synthesized: every path that turns
    the ``auto`` sentinel into the creating session's model (create, update,
    script->LLM update) must go through here so ``update model='auto'``
    persists the same row ``create model='auto'`` does. Synthesized only when
    the auto actually resolved to a model; an explicit caller reason wins.
    """
    if resolved_model and auto_pin and not str(allow_flagship_reason or "").strip():
        return _AUTO_PIN_FLAGSHIP_REASON
    return allow_flagship_reason


def _pool_for_single_sub(
    provider: Optional[str], model: Optional[str] = None
) -> Optional[str]:
    """Return a durable pool route only for a transient single-sub seat.

    ``provider`` is classified by IDENTITY (name or registered alias resolved
    through the provider registry), never by spelling, so ``claude-api-proxy``
    maps like ``claude-apx-0``.

    ``model`` is the HALF-PIN guard: when a caller supplies a model of its own
    (model-only spec, no provider), the seat's pool is glued on only if that
    pool provably serves the model's vendor. A non-Claude model (``gpt-5.5``)
    or a model whose vendor cannot be inferred returns None, so the caller
    falls through to its pre-existing resolution exactly as if no seat were
    live. The ``auto`` path omits ``model``: the creating session already ran
    that model on that seat, so the pair is known-good by construction.
    """
    if not isinstance(provider, str):
        return None
    from providers import get_provider_profile
    profile = get_provider_profile(provider)
    canonical = profile.name if profile else provider
    if re.fullmatch(r"claude-bpx-\d+", canonical):
        pool = "claude-bpr"
    elif re.fullmatch(r"claude-apx-\d+", canonical):
        pool = "claude-apr"
    else:
        return None
    if model is not None:
        model_vendor = _vendor_of(model, _MODEL_VENDOR_PREFIXES)
        pool_vendor = _vendor_of(pool, _PROVIDER_VENDOR_PREFIXES)
        if not model_vendor or model_vendor != pool_vendor:
            return None
    return pool


def _resolve_cron_llm_model(
    model: Optional[str], provider: Optional[str]
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve the effective (model, provider) for a newly-created LLM cron.

    Precedence:
      1. ``model == "auto"`` → the CREATING agent's published (provider, model).
         (An explicit provider passed alongside "auto" is ignored — "auto" means
         "match me", provider and all.)
      2. No explicit model + config ``cron.default_model``:
           - ``"auto"`` → same as (1).
           - a literal model id → that model (+ ``cron.default_provider`` if set).
      3. Otherwise unchanged (explicit model kept; unpinned stays unpinned).

    An "auto" that cannot resolve (the agent model was never published — e.g. a
    bare Python caller) degrades to leaving the job as-is: it NEVER fabricates a
    model. Returns ``(model, provider)`` in the tool's own argument order.
    """
    explicit_auto = isinstance(model, str) and model.strip().lower() == _AUTO_MODEL

    default_model = None
    default_provider = None
    if not explicit_auto and not model:
        # Only consult config when the caller gave no model at all.
        try:
            from hermes_cli.config import load_config
            cron_cfg = (load_config() or {}).get("cron", {}) or {}
            if isinstance(cron_cfg, dict):
                default_model = (cron_cfg.get("default_model") or "").strip() or None
                default_provider = (cron_cfg.get("default_provider") or "").strip() or None
        except Exception:
            logger.debug("cron.default_model lookup failed", exc_info=True)

    want_auto = explicit_auto or (
        isinstance(default_model, str) and default_model.strip().lower() == _AUTO_MODEL
    )

    if want_auto:
        a_provider, a_model = get_current_agent_model()
        if a_model:
            return (a_model, _pool_for_single_sub(a_provider) or a_provider or provider)
        # Could not resolve "auto" → leave the job UNPINNED rather than guess.
        # Drop both the "auto" sentinel and any provider that rode along with it
        # (a config-pinned provider glued to an unresolved model is worse than
        # None — it half-pins the job). A non-explicit "auto" (came from config
        # default) keeps the caller's original inputs untouched.
        if explicit_auto:
            return (None, None)
        return (model, provider)

    if not model and default_model:
        return (default_model, default_provider or provider)

    if model and not provider:
        live_provider, _ = get_current_agent_model()
        return (model, _pool_for_single_sub(live_provider, model))

    return (model, provider)

# Heartbeat cadence keeping the caller's inactivity watchdog at bay while a manual
# `cronjob(action="run")` executes in-process (comfortably below HERMES_AGENT_TIMEOUT).
# Mirrors the 10s cadence of tools/environments/base.py::touch_activity_if_due (delegate_task's heartbeat
# uses 30s) — comfortably below the 1800s default HERMES_AGENT_TIMEOUT. See #76502.
_CRON_RUN_HEARTBEAT_INTERVAL = 10.0
# Hard ceiling: with HERMES_CRON_TIMEOUT=0 a truly hung run would otherwise mask the
# gateway watchdog forever; past this the heartbeat stops and the watchdog regains authority.
# The child cron run has its own inactivity watchdog (HERMES_CRON_TIMEOUT, default 600s) that bounds a
# wedged job, but with HERMES_CRON_TIMEOUT=0 (explicit "unlimited") a truly hung run_one_job would otherwise
# mask the gateway watchdog forever — pre-#76502 the parent was at least reaped at ~1800s.
_CRON_RUN_HEARTBEAT_CEILING = 6 * 3600.0

sys.path.insert(0, str(Path(__file__).parent.parent))

from cron.jobs import (
    AmbiguousJobReference,
    claim_job_for_fire,
    get_job,
    is_job_runnable,
    list_jobs,
    mark_job_run,
    parse_schedule,
    pause_job,
    remove_job,
    resolve_job_ref,
    resume_job,
    trigger_job,
    update_job)
from tools.cronjob_prompt_scan import _CRON_THREAT_PATTERNS, _scan_cron_prompt  # noqa: F401 — patterns: fork facade re-export
from tools.cronjob_job_args import (
    _apply_continuity,
    _canonical_skills,
    _clean_str_list,
    _format_job,
    _gateway_liveness_notice,
    _local_delivery_notice,
    _mode_guidance_notes,
    _normalize_deliver_param,
    _normalize_optional_job_value,
    _origin_from_env,
    _repeat_display,
    _resolve_cron_context_deliver,
    _split_monitor_arg,
    _validate_bot_chat_deliver,
    _validate_context_from_refs,
    _validate_cron_base_url,
    _validate_cron_script_path)
from tools.registry import registry, tool_error


def _dumps(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, indent=2)


def _notify_provider_jobs_changed_safe() -> None:
    """Tell the active scheduler provider the job set changed; best-effort, never raises."""
    try:
        from cron.scheduler import _notify_provider_jobs_changed
        _notify_provider_jobs_changed()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Manual run execution (claim -> run_one_job -> report)
# ---------------------------------------------------------------------------

def _relay_fronted_delivery_platforms(job: Dict[str, Any]) -> set:
    """Delivery-platform names for this job that the relay connector fronts."""
    try:
        from gateway.relay import relay_fronted_platforms
    except Exception:
        return set()
    fronted = relay_fronted_platforms()
    if not fronted:
        return set()
    try:
        from cron.scheduler import _resolve_delivery_targets
        targets = _resolve_delivery_targets(job) or []
    except Exception:
        return set()
    return {t.get("platform") for t in targets if t.get("platform")} & fronted


def _api_server_base_url() -> str:
    """``http://host:port`` of the local api_server, mirroring its bind resolution
    (extra.host -> API_SERVER_HOST -> 127.0.0.1); a wildcard bind listens on loopback too."""
    import os
    port_raw = os.getenv("API_SERVER_PORT", "").strip()
    try:
        port = int(port_raw) if port_raw else 8642
    except ValueError:
        port = 8642
    try:
        from hermes_cli.config import cfg_get, load_config_readonly
        host = str(cfg_get(load_config_readonly(), "platforms", "api_server", "extra", "host", default="") or "").strip()
    except Exception:
        host = ""
    host = host or os.getenv("API_SERVER_HOST", "").strip()
    if not host or host in ("0.0.0.0", "::", "*"):
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"  # bare IPv6 literal
    return f"http://{host}:{port}"


def _forward_relay_fronted_run(job: Dict[str, Any], extra_prompt: Optional[str] = None) -> Optional[str]:
    """Forward a manual run to the gateway when it targets a relay-fronted platform: such delivery
    has no standalone sender — the gateway's live relay adapter is the only path, reached via
    ``POST /api/jobs/{id}/run`` (marks the job due; ``extra_prompt`` rides in the body). Returns a
    JSON result string when forwarding engages, else None (normal in-process run)."""
    if not _relay_fronted_delivery_platforms(job):
        return None
    from agent.secret_scope import get_secret
    key = get_secret("API_SERVER_KEY", "") or ""
    try:
        import httpx
        resp = httpx.post(
            f"{_api_server_base_url()}/api/jobs/{job['id']}/run", headers={"Authorization": f"Bearer {key}"},
            json=({"prompt": extra_prompt} if extra_prompt else {}), timeout=10.0)
    except Exception:
        resp = None
    if resp is not None and resp.status_code < 300:
        return _dumps({
            "success": True,
            "forwarded_to_gateway": True,
            "note": (
                "This job targets a relay-fronted platform; it was dispatched "
                "to the running gateway, whose live relay adapter owns that "
                "delivery."),
        })
    return _dumps({
        "success": False,
        "error": (
            "This job targets a relay-fronted platform, which has no "
            "standalone sender. Start the gateway — its ticker will "
            "deliver the job on schedule via the live relay adapter."),
    })


def _primary_routed_delivery_platforms(job: Dict[str, Any]) -> set:
    """Delivery-platform names this satellite profile reaches only through the primary gateway's
    ``profile_routes``: routed here, with no credential of its own to send standalone."""
    try:
        from cron.scheduler import _resolve_delivery_targets
        from cron.scheduler_preflight import _delivery_platform_routed_from_primary_gateway
        routed = {t["platform"] for t in _resolve_delivery_targets(job) or []
                  if t.get("platform") and _delivery_platform_routed_from_primary_gateway(t["platform"])}
        if not routed:
            return set()
        from gateway.config import load_gateway_config
        return routed - {p.value for p in load_gateway_config().get_connected_platforms()}
    except Exception:
        return set()


def _hand_off_primary_routed_run(job: Dict[str, Any], extra_prompt: Optional[str] = None) -> Optional[str]:
    """Queue a manual run for the gateway ticker when the job delivers through the primary gateway's
    profile route: only the gateway process holding the primary's bot can send it, so an in-process
    run would spend the whole turn and then record ``delivery_failed`` (#120330). Returns a JSON
    result string when the hand-off engages, else None (normal in-process run)."""
    runner_ref = getattr(sys.modules.get("gateway.run"), "_gateway_runner_ref", None)
    if callable(runner_ref) and runner_ref() is not None:
        return None  # inside the gateway: its live adapter delivers (#89302)
    if not is_job_runnable(job):
        return None  # keep the normal paused refusal; trigger_job would resume the job
    routed = _primary_routed_delivery_platforms(job)
    if not routed:
        return None
    from hermes_cli.cron import _builtin_gateway_liveness
    alive = _builtin_gateway_liveness()
    if alive is not True:
        # None = the probe could not tell; a run queued for a ticker that may not exist is worse
        # than an honest refusal (nothing would ever pick it up).
        cause = ("Start the gateway — its ticker will deliver the job on schedule." if alive is False
                 else "Could not determine whether a gateway serves this profile; check "
                      "`hermes cron status` and re-run.")
        return _dumps({
            "success": False,
            "error": (
                f"This job delivers to {', '.join(sorted(routed))} through the primary "
                f"gateway's profile route, which has no standalone sender. {cause}"),
        })
    updated = trigger_job(job["id"], extra_prompt=extra_prompt)
    _notify_provider_jobs_changed_safe()
    return _dumps({
        "success": True,
        "job": _format_job(updated),
        "note": (
            "This job delivers through the primary gateway's profile route; it was "
            "queued for that gateway's next scheduler tick, which runs and delivers it."),
    })


def _manual_run_delivery_note(deliver: str, refreshed: Dict[str, Any]) -> str:
    """Parenthetical delivery note for a manual run's summary; follows the refreshed record's
    ``last_delivery_error`` so the summary never claims success over a failed delivery.

    Follows the refreshed job record (#83993): ``run_one_job`` writes ``last_delivery_error`` via
    ``mark_job_run`` when the post-run delivery (telegram/discord/…) failed, and the summary must not claim
    success over that record — the calling agent relays this line to the user. Local jobs never deliver; an
    empty/missing error keeps the legacy wording byte-for-byte.
    """
    # Falsy deliver ("", stored JSON null) is normalized to "local" at fire time -> saved
    # locally. Whitespace-only values fall through so the fire-time "no target" error surfaces.
    if not deliver or deliver == "local":
        return " (output saved locally only)"
    err = str(refreshed.get("last_delivery_error") or "").strip()
    if not err:
        if refreshed.get("last_delivery_queued"):
            return " (output queued for Bot Chat; completion unverified, do not resend)"
        return " (output was delivered there by the job itself)"
    return f" (⚠ delivery FAILED: {err[:200]})"


_ALREADY_RUNNING_ERROR = (
    "Job is already running (a scheduler tick or another "
    "manual run is executing it); not started again.")


def _claim_for_manual_run(job_id: str, log_label: str):
    """At-most-once claim shared by the sync and background run paths: ``(claimed_job, None)`` or
    ``(None, error_dict)`` in the ``_execute_job_now`` shape. A lost claim is labelled precisely —
    claim_job_for_fire also returns False for paused/disabled/missing jobs, not just in-flight ones."""
    try:
        claimed_job = claim_job_for_fire(job_id, manual=True, return_job=True)
        if isinstance(claimed_job, dict):
            return claimed_job, None
        refreshed = get_job(job_id)
        if refreshed is None:
            reason = "Job no longer exists; nothing to run."
        elif not is_job_runnable(refreshed):
            reason = "Job is paused/disabled; resume it before running."
        else:
            reason = "Job is already being fired by the scheduler; not run again."
        return None, {"claimed": False, "success": False, "error": reason}
    except Exception as e:
        logger.error("Failed to claim cron job %s for %s: %s", job_id, log_label, e)
        with contextlib.suppress(Exception):
            mark_job_run(job_id, False, str(e))
        return None, {"claimed": True, "success": False, "error": str(e)}


def _execute_job_now(job: Dict[str, Any], extra_prompt: Optional[str] = None) -> Dict[str, Any]:
    """Run a job now, outside the scheduler tick: claim via ``claim_job_for_fire`` (the ticker's
    CAS, so a concurrent tick cannot double-fire and next_run_at advances), then fire through
    the shared ``run_one_job`` body. Returns {"claimed", "success", "error"}."""
    claimed_job, err = _claim_for_manual_run(job["id"], "immediate run")
    return err if err is not None else _run_claimed_job(claimed_job, extra_prompt=extra_prompt)


@contextlib.contextmanager
def _run_heartbeat(job_name: str):
    """Heartbeat into the caller's activity tracker while a manual run executes (minutes,
    synchronously on the caller's thread — without tool activity the gateway inactivity
    watchdog would kill the parent turn). Best-effort: no callback -> no thread."""
    stop = threading.Event()
    thread = None
    try:
        # run_one_job records last_run_at/last_status via mark_job_run (which also clears the fire claim)
        # and returns True iff it processed the job. ``job`` here is the exact claimed snapshot
        # (owner-bearing), so the shared body fences every terminal write by that owner. A manual `run`
        # executes the job synchronously on the caller's thread, and a cron job is itself a full agent run
        # that routinely takes minutes. The calling turn emits no tool activity for that entire window, so
        # the gateway inactivity watchdog concludes the agent is hung and kills the parent turn (#76502).
        # Fire a heartbeat into the caller's activity tracker (the same signal tool progress uses) while the
        # job runs, so the watchdog sees a working tool instead of a silent one — mirrors the delegate_task
        # heartbeat pattern. Best-effort: if no activity callback is registered (direct Python callers,
        # tests), behavior is unchanged.
        from tools.environments.base import get_activity_callback
        # Capture on THIS thread: the callback is thread-local (installed by the tool
        # executor), so a freshly spawned thread cannot read it.
        activity_cb = get_activity_callback()
    except Exception:
        activity_cb = None

    def _heartbeat_loop() -> None:
        started = time.monotonic()
        while not stop.wait(_CRON_RUN_HEARTBEAT_INTERVAL):
            elapsed = time.monotonic() - started
            if elapsed > _CRON_RUN_HEARTBEAT_CEILING:
                # A run this long with an unlimited child watchdog is likely wedged.
                logger.warning(
                    "cronjob run heartbeat ceiling reached for job "
                    "'%s' (%.0fs) — stopping heartbeat; gateway watchdog regains authority",
                    job_name, elapsed)
                return
            try:
                activity_cb(f"cronjob: running job '{job_name}' ({int(elapsed)}s elapsed)")
            except Exception:
                continue  # one transient callback error must not drop protection

    if activity_cb is not None:
        thread = threading.Thread(target=_heartbeat_loop, daemon=True, name="cronjob-run-heartbeat")
        thread.start()
    try:
        yield
    finally:
        stop.set()
        if thread is not None:
            thread.join(timeout=_CRON_RUN_HEARTBEAT_INTERVAL + 1)


def _run_claimed_job(job: Dict[str, Any], extra_prompt: Optional[str] = None) -> Dict[str, Any]:
    """Fire an already-claimed job through the shared ``run_one_job`` body (split from
    ``_execute_job_now`` so the background path can claim synchronously and hand the run
    to a worker). Returns {"claimed": True, "success": bool, "error": ...}."""
    job_id = job["id"]
    _registered = False
    fire_owner = None
    try:
        from cron.scheduler import release_running_job, run_one_job, try_register_running_job

        # In-flight dedupe: the fire claim's TTL is routinely outlived by real jobs, so
        # register in the scheduler's shared running set (same guard the ticker uses;
        # also visible to the gateway shutdown drain).
        # In-flight dedupe (idea from #53395 by @izumi0uu): the fire claim's TTL (300s) is routinely
        # outlived by real jobs, so it alone cannot stop a manual run from double-firing a job the ticker
        # (or another manual run) is still executing.
        if not try_register_running_job(job_id):
            return {"claimed": True, "success": False, "error": _ALREADY_RUNNING_ERROR}
        _registered = True

        claim = job.get("fire_claim")
        fire_owner = str(claim.get("by") or "") if isinstance(claim, dict) else None

        # Inside the gateway process deliver on the loop that owns clients such as
        # Matrix/aiohttp (a standalone asyncio.run() loop breaks them).
        runner_ref = getattr(sys.modules.get("gateway.run"), "_gateway_runner_ref", None)
        # Manual runs invoked from a gateway agent execute outside the scheduler ticker, but they still
        # share the process with the live platform adapters. Calling those clients from run_one_job's
        # standalone asyncio.run() loop raises errors like "Timeout context manager should be used inside a
        # task" and can break encrypted Matrix delivery (#61495 — salvaged from #63586 by @Fly-onlyone).
        runner = runner_ref() if callable(runner_ref) else None
        adapters = getattr(runner, "adapters", None) if runner is not None else None
        # ``runner.adapters`` is the LAUNCH profile's map; under multiplex the run executes with
        # HERMES_HOME bound to the owning profile, so resolve that profile's adapters the way the
        # ticker (``tick_adapters_for``) does — fail closed, never the default bot (#124248). A
        # resolution error propagates to the ``except`` below and marks the run failed.
        if runner is not None and hasattr(runner, "_adapters_for_profile"):
            from hermes_constants import get_hermes_home, profile_name_for_home

            profile = profile_name_for_home(get_hermes_home())
            adapters = runner._adapters_for_profile(profile)
            # A credentialless shared-bot satellite borrows the primary's bot for ROUTED targets
            # only — the same grant the ticker's ``tick_adapters_for`` makes, never the full map.
            if getattr(runner, "_is_shared_bot_satellite", lambda _p: False)(profile):
                from cron.scheduler_preflight import (
                    SharedRouteAdapters, _primary_profile_routes_for_current_home)

                adapters = SharedRouteAdapters(adapters, _primary_profile_routes_for_current_home())
        gateway_loop = getattr(runner, "_gateway_loop", None) if runner is not None else None
        try:
            # run_one_job records last_run_at/last_status via mark_job_run; `job` is the
            # owner-bearing claimed snapshot, so terminal writes stay fenced by that owner.
            with _run_heartbeat(str(job.get("name") or job_id)):
                processed = run_one_job(job, adapters=adapters, loop=gateway_loop, extra_prompt=extra_prompt)
        finally:
            _registered = False
            release_running_job(job_id)
        refreshed = get_job(job_id) or {}
        execution = None
        execution_id = job.get("execution_id")
        if execution_id:
            from cron.executions import get_execution

            execution = get_execution(str(execution_id))
        last_status = refreshed.get("last_status")
        # "delivery_failed": the run succeeded but output never reached the user — not a
        # success for the caller; surface last_delivery_error.
        run_error = refreshed.get("last_error")
        if last_status == "delivery_failed" and not run_error:
            run_error = refreshed.get("last_delivery_error")
        # That is NOT a success for the caller — the calling agent relays this result — so report it as
        # failed and surface the delivery error, which lives in last_delivery_error (last_error is None for
        # these runs, and a bare success=False with error=None reads as an unexplained failure). See #83993.
        ok = last_status in {"ok", "delivery_queued"}
        if execution is not None and execution.get("status") != "completed":
            ok = False
            run_error = execution.get("error") or f"execution ended in {execution.get('status') or 'unknown'} state"
        return {"claimed": True, "success": bool(processed and ok), "error": run_error}
    except Exception as e:
        logger.error("Failed to execute cron job %s immediately: %s", job_id, e)
        if _registered:
            # Raised before the run's own release (e.g. heartbeat setup): don't leave the
            # job marked in-flight. Only release registrations WE took — a bare discard
            # could erase a ticker-owned entry.
            with contextlib.suppress(Exception):
                release_running_job(job_id)
        with contextlib.suppress(Exception):
            mark_job_run(job_id, False, str(e), expected_fire_owner=fire_owner)
        return {"claimed": True, "success": False, "error": str(e)}


def execute_job_for_event(
    job_ref: str, extra_prompt: Optional[str] = None
) -> Dict[str, Any]:
    """Fire an existing cron job in response to an external event.

    Public entry point for event-driven triggers (the webhook adapter's
    ``cron_job`` routes). Resolves ``job_ref`` (ID or name) and
    fires it through the exact same claimed-run body a manual
    ``cronjob(action='run')`` uses, so at-most-once claiming, in-flight
    dedupe, delivery, and ``[SILENT]`` handling stay identical across the
    scheduler / manual / event paths.

    ``extra_prompt`` is injected as transient per-run context (the job's
    stored prompt is never mutated), exactly like ``action='run'`` with a
    ``prompt`` argument.

    Returns the ``_execute_job_now`` result shape:
    ``{"claimed": bool, "success": bool, "error": str|None}``.
    """
    try:
        job = resolve_job_ref(job_ref)
    except AmbiguousJobReference as e:
        return {"claimed": False, "success": False, "error": str(e)}
    if job is None:
        return {
            "claimed": False,
            "success": False,
            "error": f"Cron job '{job_ref}' not found.",
        }
    return _execute_job_now(job, extra_prompt=extra_prompt)


def _latest_job_output_excerpt(job_id: str, max_chars: int = 2000) -> Optional[str]:
    """Excerpt of the job's most recent saved output file for the background completion
    block (parent sees what the job produced). Never raises."""
    try:
        from cron.jobs import get_cron_output_dir

        out_dir = get_cron_output_dir() / job_id
        files = sorted(out_dir.glob("*.md"))
        if not files:
            return None
        text = files[-1].read_text(encoding="utf-8-sig", errors="replace").strip()
        if not text:
            return None
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n… (truncated; full output: {files[-1]})"
        return text
    except Exception:
        return None


def _reap_stale_executions(job_name: str) -> None:
    """Reap execution rows left 'claimed'/'running' by a provably-dead owner (e.g. a prior
    one-shot `hermes cron run` that died mid-run). The ticker does this at startup; one-shot
    invocations have no such moment, so a stale claim would block every later manual run.
    Best-effort self-heal: must not block dispatch."""
    try:
        # Reap any execution row this job (or any job) left stranded 'claimed'/ 'running' by a dead owner
        # process -- e.g. a PRIOR one-shot `hermes cron run` invocation whose dispatched runner died with
        # the exiting process before writing a terminal status (issue #86721). Safe and cheap: provably-dead
        # owners (PID gone, or PID reused by a different process per its start time) are reaped, as is a
        # live owner whose claim is older than the derived stale bound (the process itself is not killed).
        from cron.executions import recover_interrupted_executions
        _reclaimed = recover_interrupted_executions()
        if _reclaimed:
            logger.warning(
                "Reclaimed %d stale cron execution(s) from dead owner(s) before dispatching job '%s'",
                _reclaimed, job_name)
    except Exception as _reap_exc:
        logger.debug("Stale execution reclaim failed: %s", _reap_exc)


def _background_session_key(session_id: Optional[str]) -> str:
    """Routing key for a detached completion, captured on THIS thread (contextvars don't
    cross the pool). Empty string = no durable consumer."""
    try:
        from tools.approval_context import get_current_session_key
        session_key = get_current_session_key(default="")
    except Exception:
        session_key = ""
    # CLI path: the approval contextvar is only bound during gateway/TUI turns; the CLI
    # drain filters completions by the durable session id, and an empty key would fail
    # closed (completion never claimable).
    return session_key or (str(session_id) if session_id else "")


def _manual_run_completion(
    res: Dict[str, Any], job_id: str, job_name: str, deliver: str, started_at: float) -> Dict[str, Any]:
    """Async-delegation completion block for a finished background manual run."""
    duration = round(time.time() - started_at, 2)
    refreshed = get_job(job_id) or {}
    lines = [
        f"Cron job '{job_name}' ({job_id}) finished its manual run.",
        f"Result: {'ok' if res.get('success') else 'FAILED'}"
        + (f" — {res.get('error')}" if res.get("error") else ""),
        f"Delivery target: {deliver}" + _manual_run_delivery_note(deliver, refreshed),
    ]
    if refreshed.get("next_run_at"):
        lines.append(f"Next scheduled run: {refreshed['next_run_at']}")
    excerpt = _latest_job_output_excerpt(job_id)
    if excerpt:
        lines += ["--- JOB OUTPUT ---", excerpt]
    return {
        "status": "completed" if res.get("success") else "error", "summary": "\n".join(lines),
        "error": res.get("error"), "api_calls": 0, "duration_seconds": duration,
    }


def _try_dispatch_background_run(
    job: Dict[str, Any], session_id: Optional[str] = None, extra_prompt: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Claim ``job`` now (SYNCHRONOUSLY, so unrunnable jobs report immediately), then fire it
    on the async-delegation executor like ``delegate_task``'s background mode: the tool returns
    a handle and a ``type="async_delegation"`` completion re-enters as a fresh turn (role
    alternation legal, prompt cache intact) instead of blocking the parent turn for minutes.
    Returns None when background delivery is unavailable (caller runs sync); ``{"claimed":
    False}`` on a lost claim; ``{"claimed": True, "dispatched": True, "delegation_id"}``; or
    ``{"claimed": True, "dispatched": False, ...}`` when the pool was full and it ran inline."""
    job_id = job["id"]
    job_name = str(job.get("name") or job_id)
    # Reap BEFORE the async/sync branch: the one-shot `hermes cron run` path returns early
    # below, and this is the only moment it heals a stale claim left by a killed prior run (#113923).
    _reap_stale_executions(job_name)

    # Finite sessions cannot route a detached result back after the turn ends (delegate_task's gate).
    try:
        from gateway.session_context import async_delivery_supported
        if not async_delivery_supported():
            return None
    except Exception:
        pass

    # Routing capture BEFORE the claim: no routable session = no durable consumer for a detached
    # completion, so don't claim-and-dispatch (direct callers like `hermes cron run` exit right after).
    session_key = _background_session_key(session_id)
    # CLI path: the approval contextvar is only bound during gateway/TUI turns. The CLI drain filters
    # completions by the durable agent session id (#64240), so stamp it as the key — an empty key would fail
    # closed and the completion could never be claimed.
    if not session_key:
        return None

    # Early dedupe so a mid-run job reports in THIS response, not as a delayed error completion
    # (authoritative check: try_register_running_job). Home-scoped: one process ticks every
    # profile, so the bare-id union would report another profile's same-named job as running.
    try:
        from cron.scheduler import is_job_running
        if is_job_running(job_id):
            return {"claimed": False, "success": False, "error": _ALREADY_RUNNING_ERROR}
    except Exception:
        pass

    claimed_job, err = _claim_for_manual_run(job_id, "background run")
    if err is not None:
        if err["claimed"]:
            err["dispatched"] = False
        return err

    origin_ui_session_id = ""
    try:
        from gateway.session_context import get_session_env
        origin_ui_session_id = get_session_env("HERMES_UI_SESSION_ID", "") or ""
    except Exception:
        pass

    try:
        from tools.async_delegation import _current_origin_session_id, dispatch_async_delegation
        origin_session_id = _current_origin_session_id()
    except Exception as e:
        logger.warning(
            "cronjob run: async delegation registry unavailable (%s); running job '%s' inline.", e, job_name)
        result = _run_claimed_job(claimed_job, extra_prompt=extra_prompt)
        result["dispatched"] = False
        return result

    try:
        from tools.delegate_tool import _get_max_async_children
        max_async = _get_max_async_children()
    except Exception:
        max_async = 3

    started_at = time.time()
    # Scheduler's own normalizer (falsy -> "local", list -> comma string) on the claimed snapshot.
    from cron.scheduler import _normalize_deliver_value
    deliver = _normalize_deliver_value(claimed_job.get("deliver", "local"))

    def _runner() -> Dict[str, Any]:
        res = _run_claimed_job(claimed_job, extra_prompt=extra_prompt)
        return _manual_run_completion(res, job_id, job_name, deliver, started_at)

    dispatch = dispatch_async_delegation(
        goal=f"Manual run of cron job '{job_name}' ({job_id})",
        context=("Triggered via cronjob(action='run'). The job executed in its own "
                 "fresh cron session; this block reports its outcome."),
        toolsets=None, role="cron_run", model=job.get("model"), session_key=session_key,
        parent_session_id=str(session_id) if session_id else None, runner=_runner,
        origin_ui_session_id=origin_ui_session_id, origin_session_id=origin_session_id,
        max_async_children=max_async)
    if dispatch.get("status") == "dispatched":
        return {"claimed": True, "dispatched": True, "delegation_id": dispatch.get("delegation_id")}

    # Pool at capacity (or submit failure): the claim is already taken and must not be stranded.
    logger.info(
        "cronjob run: background pool unavailable (%s); running job '%s' inline.",
        dispatch.get("error", "rejected"), job_name)
    result = _run_claimed_job(job, extra_prompt=extra_prompt)
    result["dispatched"] = False
    return result


# ---------------------------------------------------------------------------
# Tool actions. Each takes the cronjob() argument dict `a` (and the resolved
# job record for job-bound actions) and returns the JSON result string.
# ---------------------------------------------------------------------------

def _creation_admission_warnings(job: Dict[str, Any]) -> List[str]:
    """Return actionable warnings for costly or noisy cron job shapes.

    These are warnings rather than hard failures because both shapes can be
    intentional: an agent job may deliberately follow the configured default
    model, and a standing digest may deliberately deliver to its origin forever.
    Surfacing the risks in the create response lets the caller make that choice
    explicitly instead of discovering it later via fleet lint alerts.
    """
    warnings: List[str] = []
    if not job.get("no_agent") and not (job.get("model") and job.get("provider")):
        warnings.append(
            "Warning: this LLM cron does not explicitly pin both model and provider; "
            "it follows the configured default inference route (with drift snapshots) "
            "and may consume primary-model rates. Set both model and provider when "
            "creating or updating the job."
        )

    repeat_times = (job.get("repeat") or {}).get("times")
    schedule_kind = (job.get("schedule") or {}).get("kind")
    if (
        job.get("deliver") == "origin"
        and schedule_kind != "once"
        and repeat_times is None
    ):
        warnings.append(
            "Warning: this recurring deliver='origin' cron has no finite repeat cap; "
            "it can append to the originating conversation indefinitely. Set "
            "repeat=N, or make the job self-pause/remove and verify that behavior."
        )
    return warnings


# Session-pollution floor for deliver=origin (mirrors cron-config-lint Rule #5
# I1). A recurring job that delivers into the originating conversation MORE than
# once an hour spams Ace's live session on every tick — this is never intentional
# (an ops receipt/heartbeat belongs in #logs, not the session), so unlike the
# no-cap case in _creation_admission_warnings it is a HARD block at create/update
# time, not a warning. Caught the rsd-dropbox-finalize job (deliver=origin +
# every 10m) only AFTER creation via the daily lint, 2026-07-18.
_ORIGIN_SUBHOURLY_FLOOR_SECONDS = 3600

# Bare platform names that resolve to the HOME channel rather than the caller's
# conversation. Refused on recurring jobs (Rule #4a) in favour of an explicit
# "<platform>:<chat_id>", "origin", or "local".
_BARE_PLATFORM_DELIVER_VALUES = frozenset({
    "discord", "telegram", "slack", "signal", "whatsapp", "imessage", "matrix",
})


def _cron_minute_field_min_gap_seconds(minute: str) -> Optional[int]:
    """Smallest gap between consecutive fires (seconds) for a 5-field cron's
    MINUTE field, assuming the hour/day fields permit firing each hour.

    Expands every form the minute field can take — ``*``, ``*/N``, comma lists
    (``0,30``), ranges (``0-29``), stepped ranges (``0-59/10``), and the
    start-with-step form (``N/step`` = start at N, step to 59, e.g. ``0/30`` →
    ``{0,30}``) — into the concrete set of fire-minutes, then returns the minimum
    gap between consecutive fires INCLUDING the wrap across the hour boundary (so
    a job that fires only at :00 has a 3600s gap, but ``0,30`` has 1800s).
    Returns None if the field can't be parsed. This closes the bypass where
    non-``*/N`` minute forms fell through to a flat hourly assumption (Greptile,
    PR #397).
    """
    fires: set[int] = set()
    for token in minute.split(","):
        token = token.strip()
        if not token:
            continue
        step = 1
        has_step = "/" in token
        if has_step:
            base, _, step_s = token.partition("/")
            if not step_s.isdigit() or int(step_s) <= 0:
                return None
            step = int(step_s)
        else:
            base = token
        if base == "*":
            lo, hi = 0, 59
        elif "-" in base:
            lo_s, _, hi_s = base.partition("-")
            if not (lo_s.isdigit() and hi_s.isdigit()):
                return None
            lo, hi = int(lo_s), int(hi_s)
        elif base.isdigit():
            # Cron semantics: a bare number WITH a step (``N/step``) means "start
            # at N, fire every step up to 59" — so the upper bound is 59, not N.
            # A bare number WITHOUT a step is a single fire at N.
            lo = int(base)
            hi = 59 if has_step else lo
        else:
            return None
        if not (0 <= lo <= 59 and 0 <= hi <= 59 and lo <= hi):
            return None
        fires.update(range(lo, hi + 1, step))
    if not fires:
        return None
    mins = sorted(fires)
    if len(mins) == 1:
        return 3600  # fires once per hour
    gaps = [(b - a) for a, b in zip(mins, mins[1:])]
    gaps.append((mins[0] + 60) - mins[-1])  # wrap across the hour boundary
    return min(gaps) * 60


def _schedule_interval_seconds(schedule: Optional[Dict[str, Any]]) -> Optional[int]:
    """Best-effort seconds between fires for a PARSED schedule dict; None if
    unknown / one-shot. Mirrors cron-config-lint.interval_seconds so the
    create-time gate and the after-the-fact lint agree on cadence."""
    if not isinstance(schedule, dict):
        return None
    kind = schedule.get("kind")
    if kind == "interval":
        minutes = schedule.get("minutes")
        if isinstance(minutes, (int, float)) and minutes > 0:
            return int(minutes * 60)
        return None
    if kind == "cron":
        expr = schedule.get("expr") or schedule.get("display") or ""
        parts = expr.split()
        if len(parts) == 5:
            return _cron_minute_field_min_gap_seconds(parts[0])
    return None


# --- Vendor consistency of the (model, provider) pair --------------------
# A job pinned to e.g. model="gpt-5.6-sol" + provider="claude-apr" fails EVERY
# run with `HTTP 400: the model field names a different vendor model than this
# endpoint serves; retrying will not help` -- a mis-pair, not a down model.
# It is a recurring class: seen live 2026-08-19 and again 2026-08-29, each time
# only caught AFTER the job had been persisted and had burned failed fires.
#
# This mirrors the post-hoc `cron-config-lint` Rule #20b, promoted to an
# admission gate so the pair is refused before it is stored. Keep the two in
# sync: the sibling implementation lives in the fleet lint script as
# `model_provider_vendor_mismatch()`.
#
# The check is mechanical: infer each side's vendor from its name prefix and
# refuse a cross-vendor pair. The prefix maps are deliberately conservative --
# an unrecognized model or provider name yields vendor None and is NOT flagged
# (fail-open on unknowns). This rule exists to catch the obvious mis-pairs, not
# to maintain a model catalog.
_MODEL_VENDOR_PREFIXES = (
    ("claude", "anthropic"),
    ("gpt-", "openai"),
    ("o1", "openai"), ("o3", "openai"), ("o4", "openai"),
    ("gemini", "google"),
    ("grok", "xai"),
    ("kimi", "moonshot"),
    ("deepseek", "deepseek"),
    ("llama", "meta"),
    ("qwen", "alibaba"),
)
_PROVIDER_VENDOR_PREFIXES = (
    ("claude", "anthropic"),
    ("anthropic", "anthropic"),
    ("openai", "openai"),
    ("codex", "openai"),
    ("gemini", "google"), ("google", "google"),
    ("grok", "xai"), ("xai", "xai"),
    ("moonshot", "moonshot"), ("kimi", "moonshot"),
    ("deepseek", "deepseek"),
)


def _vendor_of(name: Any, prefixes: Tuple[Tuple[str, str], ...]) -> Optional[str]:
    text = str(name or "").strip().lower()
    for prefix, vendor in prefixes:
        if text.startswith(prefix):
            return vendor
    return None


def model_provider_vendor_mismatch(
    model_name: Any, provider_name: Any
) -> Optional[Tuple[str, str]]:
    """Return ``(model_vendor, provider_vendor)`` when the pair is provably
    cross-vendor, else None. Unknown names on either side -> None (fail-open)."""
    model_vendor = _vendor_of(model_name, _MODEL_VENDOR_PREFIXES)
    provider_vendor = _vendor_of(provider_name, _PROVIDER_VENDOR_PREFIXES)
    if model_vendor and provider_vendor and model_vendor != provider_vendor:
        return (model_vendor, provider_vendor)
    return None


def _model_provider_vendor_error(model: Any, provider: Any) -> Optional[str]:
    """Hard-block message for a provably cross-vendor (model, provider) pair.

    ``model`` may be a plain name or the ``{"model": ..., "provider": ...}``
    object shape -- both occur in stored jobs. For the object shape the inner
    model name is checked against BOTH the inner provider and the effective
    top-level provider, since either can be the one that actually routes the
    request. Returns None when neither side's vendor can be inferred.
    """
    if isinstance(model, dict):
        model_name = model.get("model")
        candidate_providers = (provider, model.get("provider"))
    else:
        model_name = model
        candidate_providers = (provider,)

    for candidate in candidate_providers:
        mismatch = model_provider_vendor_mismatch(model_name, candidate)
        if mismatch:
            model_vendor, provider_vendor = mismatch
            return (
                f"model '{str(model_name).strip()}' ({model_vendor}) cannot run on "
                f"provider '{str(candidate).strip()}' ({provider_vendor}) -- this pair "
                "fails every run with HTTP 400 (the model field names a different "
                "vendor model than this endpoint serves; retrying will not help). "
                "Pick a provider that serves this model's vendor. "
                "(cron Rule #20b)"
            )
    return None


def _creation_admission_error(job: Dict[str, Any]) -> Optional[str]:
    """Return a hard-block error string for a cron shape that must be refused at
    create/update time, or None if the job is admissible.

    Three rules. One is about the job never being able to run at all:

    * a provably cross-vendor (model, provider) pair (Rule #20b) -- every fire
      returns HTTP 400, so the job is dead on arrival. Checked before the
      schedule-shape rules because it holds for any cadence, including
      one-shot jobs.

    The other two are about delivery going somewhere the author did not intend:

    * deliver=origin on a sub-hourly recurring job (Rule #5 I1) --
      session-pollution every <1h is never a deliberate choice.
    * a BARE platform deliver ("discord"/"telegram") on a recurring job
      (Rule #4a) -- it resolves to the HOME channel for any job without a
      captured origin, and origin is only stamped when the job is created
      from inside a live chat session. The spelling reads like "this chat"
      and silently means "the home channel", so it is refused in favour of an
      explicit target.
    """
    # Rule #20b runs first and unconditionally: a cross-vendor pair is broken
    # for every cadence (including 'once') and stays broken while disabled, so
    # it is refused regardless of schedule shape or enabled state.
    vendor_error = _model_provider_vendor_error(job.get("model"), job.get("provider"))
    if vendor_error:
        return vendor_error
    if not job.get("enabled", True):
        return None
    schedule = job.get("schedule") or {}
    if schedule.get("kind") == "once":
        return None

    deliver = (job.get("deliver") or "").strip()

    # --- Rule #4a: bare platform on a recurring job with no origin ---
    # With an origin captured (created from a live chat on the same platform),
    # a bare platform correctly resolves to that conversation -- unambiguous,
    # so it is allowed. Without one there is nothing to resolve to and it
    # silently falls back to the home channel; that is the misroute.
    origin = job.get("origin") or {}
    origin_platform = str((origin or {}).get("platform") or "").strip().lower()
    bare_parts = [
        part.strip()
        for part in deliver.split(",")
        if part.strip()
        and part.strip().lower() in _BARE_PLATFORM_DELIVER_VALUES
        and part.strip().lower() != origin_platform
    ]
    if bare_parts:
        bare = bare_parts[0].lower()
        return (
            f"deliver='{bare}' on a recurring job resolves to the HOME channel, "
            "not the conversation you are in -- a job only remembers an origin "
            "when it is created from inside a live chat session. Name the "
            "target explicitly:\n"
            "  deliver='origin'          -> this conversation (hourly-or-slower)\n"
            f"  deliver='{bare}:<chat_id>'  -> a specific channel (ops/monitor output)\n"
            "  deliver='local'           -> file only, no message\n"
            "(cron Rule #4a)"
        )

    if deliver.lower() != "origin":
        return None
    secs = _schedule_interval_seconds(schedule)
    if secs is not None and secs < _ORIGIN_SUBHOURLY_FLOOR_SECONDS:
        mins = max(1, secs // 60)
        return (
            f"deliver='origin' on a sub-hourly job (every ~{mins}m) would append to "
            "Ace's live session on every tick (session pollution). Use "
            "deliver='discord'/'telegram'/a specific channel for an ops receipt/"
            "heartbeat, or make the cadence hourly-or-slower. (cron Rule #5 I1)"
        )
    return None


def _with_guidance(result: Dict[str, Any], job: Dict[str, Any], deliver: Optional[str]) -> Dict[str, Any]:
    """Attach mode/delivery guidance (create and update echo the same notes)."""
    _notes = _mode_guidance_notes(job, deliver)
    if _notes:
        result["guidance"] = _notes
    return result


def _resolve_create_model(a: Dict[str, Any]) -> None:
    """Resolve model/provider for an LLM cron (``no_agent=False``) IN PLACE on the arg dict.

    An unpinned LLM cron inherits the runtime PRIMARY (often Opus) at fire time — a silent cost
    footgun. Order: explicit ``model="auto"`` (or config ``cron.default_model="auto"``) pins the
    CREATING agent's own (provider, model); config ``cron.default_model``/``default_provider``
    pins those; otherwise the inputs stay as given (unpinned stays unpinned). An "auto" that
    cannot resolve leaves the job unpinned — it never guesses a model. An auto pin inherits the
    creating session's elected primary, not a caller-chosen flagship route, so it carries the
    auto-pin ``allow_flagship_reason``."""
    requested_model = a["model"]
    # Only the "auto" sentinel authorizes inherited-model auto-pin. A literal configured
    # default is a model choice, not inheritance.
    auto_pin = isinstance(requested_model, str) and requested_model.strip().lower() == _AUTO_MODEL
    if not requested_model:
        try:
            from hermes_cli.config import load_config
            configured = ((load_config() or {}).get("cron", {}) or {}).get("default_model")
            auto_pin = isinstance(configured, str) and configured.strip().lower() == _AUTO_MODEL
        except Exception:
            auto_pin = False
    a["model"], a["provider"] = _resolve_cron_llm_model(a["model"], a["provider"])
    a["allow_flagship_reason"] = _flagship_reason_for_auto_pin(a["model"], auto_pin, a["allow_flagship_reason"])


def _admission_warnings_for(job: Dict[str, Any], model_spec_warning: Optional[str]) -> List[str]:
    """Create/update response warnings: an ignored model spec first, then the shape warnings."""
    warnings = _creation_admission_warnings(job)
    return [model_spec_warning, *warnings] if model_spec_warning else warnings


def _action_create(a: Dict[str, Any]) -> str:
    prompt, script = a["prompt"], a["script"]
    deliver = _normalize_deliver_param(a["deliver"])
    if not a["schedule"]:
        return tool_error("schedule is required for create", success=False)
    canonical_skills = _canonical_skills(a["skill"], a["skills"])
    _no_agent = bool(a["no_agent"])
    if not _no_agent:
        _resolve_create_model(a)
    # no_agent=True -> the script IS the job (prompt/skills optional); else prompt or skills.
    if _no_agent:
        if not script:
            return tool_error(
                "create with no_agent=True requires a script — "
                "the script is the job. In no_agent mode the LLM is "
                "skipped entirely: prompt and skills are ignored, "
                "non-empty stdout is delivered verbatim, empty stdout "
                "sends nothing (watchdog pattern), and a non-zero exit or timeout sends an error alert.",
                success=False)
    elif not prompt and not canonical_skills:
        return tool_error("create requires either prompt or at least one skill", success=False)
    error = (
        (prompt and _scan_cron_prompt(prompt))
        or (script and _validate_cron_script_path(script))
        or (a["monitor_script"] and _validate_cron_script_path(a["monitor_script"]))
        # A model-supplied base_url must not route a named provider's stored credential
        # to an attacker endpoint.
        or _validate_cron_base_url(a["provider"], a["base_url"])
        # bot-chat targets are machine-local: fail the CREATE, not the run.
        or _validate_bot_chat_deliver(deliver)
        # failure_deliver shares deliver's grammar and validators.
        or _validate_bot_chat_deliver(_normalize_deliver_param(a["failure_deliver"]))
        or (a["context_from"] and _validate_context_from_refs(
            [a["context_from"]] if isinstance(a["context_from"], str) else a["context_from"])))
    if error:
        return tool_error(error, success=False)
    # Reject a shape that must never be persisted BEFORE persisting it: a cross-vendor
    # model/provider pair (Rule #20b, dead on arrival with HTTP 400), deliver=origin on a
    # sub-hourly recurring job (Rule #5 I1), a bare platform deliver on a recurring job (Rule #4a).
    # Preview from the same inputs create_job() will use so the gate reads the real parsed shape.
    _admission_error = _creation_admission_error({
        "deliver": deliver, "enabled": True, "schedule": parse_schedule(a["schedule"]),
        "origin": _origin_from_env(a["schedule"]),
        "model": _normalize_optional_job_value(a["model"]),
        "provider": _normalize_optional_job_value(a["provider"]),
    })
    if _admission_error:
        return tool_error(_admission_error, success=False)

    context_from = a["context_from"]
    if a["continuity"] is not None:
        context_from = _apply_continuity(context_from, a["continuity"])

    from cron.scheduler import CronSchedulerRegistrationError, create_job_with_scheduler_registration
    try:
        job = create_job_with_scheduler_registration(
            prompt=prompt or "", schedule=a["schedule"], name=a["name"], repeat=a["repeat"],
            deliver=_resolve_cron_context_deliver(deliver),
            origin=_origin_from_env(a["schedule"]),
            skills=canonical_skills,
            model=_normalize_optional_job_value(a["model"]), provider=_normalize_optional_job_value(a["provider"]),
            allow_flagship_reason=a["allow_flagship_reason"],
            base_url=_normalize_optional_job_value(a["base_url"], strip_trailing_slash=True),
            script=_normalize_optional_job_value(script), context_from=context_from,
            enabled_toolsets=a["enabled_toolsets"] or None, workdir=_normalize_optional_job_value(a["workdir"]),
            no_agent=_no_agent, attach_to_session=a["attach_to_session"],
            monitor_script=_normalize_optional_job_value(a["monitor_script"]),
            monitor_url=_normalize_optional_job_value(a["monitor_url"]),
            # CLI-only lane: absent from CRONJOB_SCHEMA and the model dispatch (models don't pick models).
            reasoning_effort=a["reasoning_effort"], interpreter=a["interpreter"],
            pinned=bool(a["pinned"]),
            failure_deliver=_resolve_cron_context_deliver(_normalize_deliver_param(a["failure_deliver"])),
            **({"paused": a["paused"], "paused_reason": a["paused_reason"]}
               if a["paused"] is not False or a["paused_reason"] is not None else {}))
    except CronSchedulerRegistrationError as exc:
        _partial = exc.to_dict()
        return tool_error(_partial.pop("error"), success=False, **_partial)
    _create_message = " ".join(filter(None, (f"Cron job '{job['name']}' created.",
        "Created PAUSED — resume to schedule, or explicitly run now." if not job.get("enabled", True) else None,
        _local_delivery_notice(job, deliver))))
    _admission_warnings = _admission_warnings_for(job, a["model_spec_warning"])
    if _admission_warnings:
        _create_message = f"{_create_message} {' '.join(_admission_warnings)}"
    # The builtin ticker lives in the gateway process: with no gateway running the job is stored
    # but never fires — tell the model (the CLI already warns).
    _result = {
        "success": True, "job_id": job["id"], "name": job["name"], "skill": job.get("skill"),
        "skills": job.get("skills", []), "schedule": job["schedule_display"], "repeat": _repeat_display(job),
        "deliver": job.get("deliver", "local"), "next_run_at": job["next_run_at"], "job": _format_job(job),
        "warnings": _admission_warnings, "message": _create_message, **_gateway_liveness_notice(),
    }
    return _dumps(_with_guidance(_result, job, deliver))


def _action_list(a: Dict[str, Any]) -> str:
    jobs = [_format_job(job) for job in list_jobs(include_disabled=a["include_disabled"])]
    _result = {"success": True, "count": len(jobs), "jobs": jobs}
    # Same inert-job class as create; an empty list has nothing inert.
    if jobs:
        # Same silent-inert-job class as create (#87033): an agent inspecting existing jobs in a
        # gateway-less environment must learn they are not firing, not just see a clean list.
        _result.update(_gateway_liveness_notice(plural=True))
    return _dumps(_result)


def _action_remove(job: Dict[str, Any], a: Dict[str, Any]) -> str:
    job_id = job["id"]
    if not remove_job(job_id):
        return tool_error(f"Failed to remove job '{job_id}'", success=False)
    _notify_provider_jobs_changed_safe()
    return _dumps({
        "success": True,
        "message": f"Cron job '{job['name']}' removed.",
        "removed_job": {"id": job_id, "name": job["name"], "schedule": job.get("schedule_display")},
    })


def _job_state_result(updated: Dict[str, Any]) -> str:
    _notify_provider_jobs_changed_safe()
    return _dumps({"success": True, "job": _format_job(updated)})


def _refreshed_job_view(job_id: str) -> Dict[str, Any]:
    """Re-read so the response reflects the post-run last_run_at/last_status."""
    return _format_job(get_job(job_id) or {"id": job_id})


def _action_run(job: Dict[str, Any], a: Dict[str, Any]) -> str:
    job_id = job["id"]
    # `prompt` on run is transient per-fire context appended to the stored prompt, never
    # persisted; same strict scan as stored prompts.
    extra_prompt = a["prompt"] or None
    # See #57331, #57342, #57360.
    if extra_prompt:
        scan_error = _scan_cron_prompt(extra_prompt)
        if scan_error:
            return tool_error(scan_error, success=False)
    # Primary-routed satellite delivery has no sender outside the gateway: hand the run to its ticker.
    handed_off = _hand_off_primary_routed_run(job, extra_prompt=extra_prompt)
    if handed_off is not None:
        return handed_off
    # A manual run must actually run even with no ticker active. Preferred: background
    # dispatch (handle now, outcome as a completion event); inline fallback otherwise.
    bg = _try_dispatch_background_run(job, session_id=a["session_id"], extra_prompt=extra_prompt)
    if bg is not None and bg.get("dispatched"):
        _notify_provider_jobs_changed_safe()
        result = _refreshed_job_view(job_id)
        result["executed"] = True
        result["execution_mode"] = "background"
        result["delegation_id"] = bg.get("delegation_id")
        return _dumps({
            "success": True,
            "job": result,
            "note": (
                "The job is running in the background. You and the "
                "user can keep working; its outcome re-enters the "
                "conversation as a new message when it finishes. "
                "Do not wait or poll — just continue."),
        })
    if bg is not None:
        exec_result = bg  # terminal result: claim lost or inline fallback
    else:
        # Relay-fronted manual run: no live adapter here — forward to the running gateway.
        forwarded = _forward_relay_fronted_run(job, extra_prompt=extra_prompt)
        if forwarded is not None:
            return forwarded
        exec_result = _execute_job_now(job, extra_prompt=extra_prompt)
    # A claimed direct run advances next_run_at and may race an external provider's
    # one-shot for the same occurrence; a lost consumed fire cannot re-arm itself, so
    # reconcile after the run has persisted its final state.
    claimed = exec_result.get("claimed", False)
    if claimed:
        _notify_provider_jobs_changed_safe()
    result = _refreshed_job_view(job_id)
    result["executed"] = claimed
    result["execution_success"] = exec_result.get("success", False)
    if not claimed:
        result["execution_skipped"] = exec_result.get("error") or (
            "Already being fired by the scheduler; not run again.")
    elif exec_result.get("error"):
        result["execution_error"] = exec_result["error"]
    return _dumps({"success": True, "job": result})


def _pick(updates: Dict[str, Any], job: Dict[str, Any], key: str) -> Any:
    """Effective value of ``key`` after this update: pending update wins over the stored job."""
    return updates[key] if key in updates else job.get(key)


def _update_core_fields(job: Dict[str, Any], a: Dict[str, Any], updates: Dict[str, Any]) -> Optional[str]:
    """prompt / name / deliver / skills / model pins; returns an error string or None."""
    prompt, deliver, skill, skills = a["prompt"], a["deliver"], a["skill"], a["skills"]
    if prompt is not None:
        scan_error = _scan_cron_prompt(prompt)
        if scan_error:
            return scan_error
        updates["prompt"] = prompt
    if a["name"] is not None and a["name"].strip():
        # Blank name is a no-op, not a clear: a model re-sending the whole schema with
        # type-default empties must not wipe untouched fields.
        updates["name"] = a["name"]
    if deliver is not None:
        bot_chat_error = _validate_bot_chat_deliver(_normalize_deliver_param(deliver))
        if bot_chat_error:
            return bot_chat_error
        updates["deliver"] = _resolve_cron_context_deliver(_normalize_deliver_param(deliver))
    if a["failure_deliver"] is not None:
        # '' clears the override (failures fall back to deliver); non-empty values share
        # deliver's validation AND its cron-context origin resolution (a job created from
        # inside a cron run must never store literal 'origin').
        _norm_fd = _normalize_deliver_param(a["failure_deliver"])
        if _norm_fd:
            bot_chat_error = _validate_bot_chat_deliver(_norm_fd)
            if bot_chat_error:
                return bot_chat_error
            _norm_fd = _resolve_cron_context_deliver(_norm_fd)
        updates["failure_deliver"] = _norm_fd
    if skills is not None or skill is not None:
        canonical_skills = _canonical_skills(skill, skills)
        updates["skills"] = canonical_skills
        updates["skill"] = canonical_skills[0] if canonical_skills else None
    if a["model"] is not None:
        # Model resolution must use the mode AFTER this update, not the stored mode: a script job
        # can become an LLM job (or vice versa) in the same request that changes its model.
        effective_no_agent = bool(a["no_agent"]) if a["no_agent"] is not None else bool(job.get("no_agent"))
        model, flagship_reason = a["model"], a["allow_flagship_reason"]
        if isinstance(model, str) and model.strip().lower() == _AUTO_MODEL and not effective_no_agent:
            model, provider = _resolve_cron_llm_model(model, a["provider"])
            updates["provider"] = _normalize_optional_job_value(provider)
            # Same exemption create applies: an inherited flagship is the session's own elected
            # model, not a caller-chosen worker route — without it update_job's
            # validate_worker_model refuses the very row create just persisted.
            flagship_reason = _flagship_reason_for_auto_pin(model, True, flagship_reason)
        elif model and a["provider"] is None and not effective_no_agent:
            live_provider, _ = get_current_agent_model()
            pool_provider = _pool_for_single_sub(live_provider, model)
            if pool_provider:
                updates["provider"] = pool_provider
        updates["model"] = _normalize_optional_job_value(model)
        updates["allow_flagship_reason"] = flagship_reason
    if a["provider"] is not None:
        updates["provider"] = _normalize_optional_job_value(a["provider"])
    if a["pinned"] is not None:
        updates["pinned"] = bool(a["pinned"])
    if a["base_url"] is not None:
        updates["base_url"] = _normalize_optional_job_value(a["base_url"], strip_trailing_slash=True)
    if a["reasoning_effort"] is not None:
        # CLI-only lane; update_job validates, empty string clears the pin.
        updates["reasoning_effort"] = a["reasoning_effort"]
    if a["interpreter"] is not None:
        # CLI-only lane like reasoning_effort; update_job trims, empty string clears.
        updates["interpreter"] = a["interpreter"]
    # Re-validate the EFFECTIVE provider/base_url on EVERY update: a job persisted before
    # this guard may hold an unsafe pair, and editing an unrelated field must not leave it
    # schedulable. Merging this update over the stored job lets an operator remediate.
    return _validate_cron_base_url(_pick(updates, job, "provider"), _pick(updates, job, "base_url"))


def _update_script_fields(job: Dict[str, Any], a: Dict[str, Any], updates: Dict[str, Any]) -> Optional[str]:
    """script / monitor_script / monitor_url (empty string clears); returns an error string or None."""
    monitor_script, monitor_url = a["monitor_script"], a["monitor_url"]
    for field, value in (("script", a["script"]), ("monitor_script", monitor_script)):
        if value is not None:
            if value:
                path_error = _validate_cron_script_path(value)
                if path_error:
                    return path_error
            updates[field] = _normalize_optional_job_value(value) if value else None
    if monitor_url is not None:
        updates["monitor_url"] = _normalize_optional_job_value(monitor_url) if monitor_url else None
    if (monitor_script is not None or monitor_url is not None) and (
        _pick(updates, job, "monitor_script") and _pick(updates, job, "monitor_url")):
        return("monitor_script and monitor_url are mutually exclusive — clear one before setting the other.")
    return None


def _update_context_from(job: Dict[str, Any], a: Dict[str, Any], updates: Dict[str, Any]) -> Optional[str]:
    """context_from / continuity: empty string / list clears; otherwise every ref must
    exist. Stored as a list (or None) to match create_job()."""
    context_from, continuity = a["context_from"], a["continuity"]
    if context_from is None and continuity is None:
        return None
    if context_from is None:
        context_from = list(job.get("context_from") or [])  # continuity-only update
    refs = _clean_str_list(context_from)
    if continuity is not None:
        refs = _apply_continuity(refs, continuity) or []
    if refs:
        ref_error = _validate_context_from_refs(refs)
        if ref_error:
            return ref_error
    updates["context_from"] = refs or None
    return None


def _update_run_fields(job: Dict[str, Any], a: Dict[str, Any], updates: Dict[str, Any]) -> Optional[str]:
    """enabled_toolsets / attach_to_session / workdir / no_agent / repeat / schedule."""
    if a["enabled_toolsets"] is not None:
        updates["enabled_toolsets"] = a["enabled_toolsets"] or None
    if a["attach_to_session"] is not None:
        updates["attach_to_session"] = bool(a["attach_to_session"])
    if a["workdir"] is not None:
        # Empty string clears; otherwise update_job() validates/normalizes.
        updates["workdir"] = _normalize_optional_job_value(a["workdir"]) or None
    if a["no_agent"] is not None:
        # Flipping to True needs a script on the job or in this same update.
        target_no_agent = bool(a["no_agent"])
        if target_no_agent and not _pick(updates, job, "script"):
            return (
                "Cannot set no_agent=True on a job without a script. "
                "Set `script` in the same update, or on the job first.")
        updates["no_agent"] = target_no_agent
    if a["repeat"] is not None:
        # Shared chokepoint coerces string forms ('forever'/'once'/'3') and 0/negative.
        from cron.jobs import normalize_repeat_value
        repeat_state = dict(job.get("repeat") or {})
        repeat_state["times"] = normalize_repeat_value(a["repeat"])
        updates["repeat"] = repeat_state
    if a["schedule"] is not None:
        parsed_schedule = parse_schedule(a["schedule"])
        updates["schedule"] = parsed_schedule
        updates["schedule_display"] = parsed_schedule.get("display", a["schedule"])
        if job.get("state") != "paused":
            updates["state"] = "scheduled"
            updates["enabled"] = True
    return None


# Validation order is behavior (first failing field wins): keep this sequence.
_UPDATE_STEPS = (_update_core_fields, _update_script_fields, _update_context_from, _update_run_fields)


def _action_update(job: Dict[str, Any], a: Dict[str, Any]) -> str:
    updates: Dict[str, Any] = {}
    for step in _UPDATE_STEPS:
        error = step(job, a, updates)
        if error:
            return tool_error(error, success=False)
    if not updates:
        return tool_error("No updates provided.", success=False)
    # Re-validate the admission gates on the EFFECTIVE job (Rules #20b, #5 I1, #4a): an update may
    # supply only `model` or only `provider`, so the cross-vendor check must run against the merged
    # pair, and an update that flips deliver->origin, tightens the cadence or re-enables a paused
    # job can introduce the sub-hourly-origin shape. Refuse here, not after a failed fire.
    _admission_error = _creation_admission_error({**job, **updates})
    if _admission_error:
        return tool_error(_admission_error, success=False)
    updated = update_job(job["id"], updates)
    if not updated:
        return tool_error(f"Failed to update cron job '{job['id']}'.", success=False)
    _notify_provider_jobs_changed_safe()
    _admission_warnings = _admission_warnings_for(updated, a["model_spec_warning"])
    _update_message = f"Cron job '{updated['name']}' updated."
    if _admission_warnings:
        _update_message = f"{_update_message} {' '.join(_admission_warnings)}"
    # An update can switch modes or delivery — echo the same guidance as create.
    return _dumps(_with_guidance(
        {"success": True, "job": _format_job(updated), "warnings": _admission_warnings, "message": _update_message},
        updated, _normalize_deliver_param(a["deliver"])))


_JOBLESS_ACTIONS = {"create": _action_create, "list": _action_list}
_JOB_ACTIONS = {
    "remove": _action_remove, "update": _action_update,
    "run": _action_run, "run_now": _action_run, "trigger": _action_run,
    "pause": lambda job, a: _job_state_result(pause_job(job["id"], reason=a["reason"])),
    "resume": lambda job, a: _job_state_result(resume_job(job["id"])),
}


def _resolve_job_or_error(job_id: str):
    """``(job, None)`` or ``(None, json_error)`` for a job_id/name reference."""
    try:
        job = resolve_job_ref(job_id)
    except AmbiguousJobReference as exc:
        return None, _dumps({
            "success": False,
            "error": str(exc),
            "matches": [
                {"id": m["id"], "name": m.get("name"), "schedule": m.get("schedule_display"), "next_run_at": m.get("next_run_at")}
                for m in exc.matches
            ],
        })
    if not job:
        return None, _dumps(
            {"success": False, "error": f"Job with ID or name '{job_id}' not found. Use cronjob(action='list') to inspect jobs."},
        )
    return job, None


def cronjob(
    action: str,
    job_id: Optional[str] = None,
    prompt: Optional[str] = None,
    schedule: Optional[str] = None,
    name: Optional[str] = None,
    repeat: Optional[int] = None,
    deliver: Optional[str] = None,
    include_disabled: bool = False,
    skill: Optional[str] = None,
    skills: Optional[List[str]] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    allow_flagship_reason: Optional[str] = None,
    base_url: Optional[str] = None,
    reason: Optional[str] = None,
    script: Optional[str] = None,
    context_from: Optional[Union[str, List[str]]] = None,
    continuity: Optional[bool] = None,
    enabled_toolsets: Optional[List[str]] = None,
    workdir: Optional[str] = None,
    no_agent: Optional[bool] = None,
    attach_to_session: Optional[bool] = None,
    monitor_script: Optional[str] = None,
    monitor_url: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    model_spec_warning: Optional[str] = None,
    failure_deliver: Optional[Union[str, List[str]]] = None,
    task_id: str = None,
    session_id: Optional[str] = None,
    paused: bool = False,
    paused_reason: Optional[str] = None,
    pinned: Optional[bool] = None,
    interpreter: Optional[str] = None) -> str:
    """Unified cron job management tool."""
    a = dict(locals())
    del a["task_id"]  # unused but kept for handler signature compatibility
    try:
        normalized = (action or "").strip().lower()
        # Validate an optional per-job reasoning_effort override once, up front, so both create
        # and update reject a bad value with a clear error instead of silently storing garbage.
        if reasoning_effort is not None and str(reasoning_effort).strip():
            from hermes_constants import VALID_REASONING_EFFORTS
            _re = str(reasoning_effort).strip().lower()
            if _re not in VALID_REASONING_EFFORTS and _re != "none":
                return tool_error(
                    f"Invalid reasoning_effort '{reasoning_effort}'. "
                    f"Valid: {', '.join(VALID_REASONING_EFFORTS)}, none.", success=False)
        handler = _JOBLESS_ACTIONS.get(normalized)
        if handler is not None:
            return handler(a)
        if not job_id:
            return tool_error(f"job_id is required for action '{normalized}'", success=False)
        # Job resolution precedes the action check (an unknown action on a missing job
        # reports the missing job) — preserved ordering.
        job, error = _resolve_job_or_error(job_id)
        if error is not None:
            return error
        handler = _JOB_ACTIONS.get(normalized)
        if handler is None:
            return tool_error(f"Unknown cron action '{action}'", success=False)
        return handler(job, a)
    except Exception as e:
        return tool_error(str(e), success=False)


def _script_description(home: str) -> str:
    return (f"Optional script run each tick; stdout is injected into the agent's prompt as context (with no_agent=True "
            f"the script IS the job). Relative paths resolve under {home}/scripts/; .sh/.bash via bash, else Python. "
            "On update, '' clears.")


def _cronjob_schema_overrides() -> dict:
    """Rebuild the ``script`` path hint from the ACTIVE profile at every get_definitions(): the
    static schema is built once per process, but the multiplexed gateway serves every profile from
    that process, so a path baked in at import would name the launch profile's home (#95685)."""
    params = copy.deepcopy(CRONJOB_SCHEMA["parameters"])
    params["properties"]["script"]["description"] = _script_description(display_hermes_home())
    return {"parameters": params}


CRONJOB_SCHEMA = {
    "name": "cronjob_manage",
    "description": """Manage scheduled cron jobs: action='create' schedules a job from a prompt and/or skills; 'list' inspects jobs; 'update'/'pause'/'resume'/'remove' manage one by job_id (always list first — never guess job IDs); 'run' fires a job immediately in the BACKGROUND (returns a handle at once, outcome re-enters the conversation when done — do not wait or poll; optional 'prompt' adds transient context for that fire only).

Jobs run on the main agent model (whatever `hermes model` is set to when they fire) unless pinned.

Jobs run in a fresh session with no current-chat context, so prompts must be self-contained, and the agent's FINAL RESPONSE is what gets delivered — cron runs are autonomous and cannot ask questions. Jobs run on the main agent model (whatever `hermes model` is set to when they fire) unless the user pins one. Prefer updating an existing job over creating near-duplicates.""",
    "parameters": {
        "type": "object",
        "properties": {
            "paused": {"type": "boolean", "description": "Create only: persist disabled atomically. Resume to schedule; explicit run remains available. Default false."},
            "paused_reason": {"type": "string", "description": "Create only: auditable reason; requires paused=true."},
            "action": {
                "type": "string",
                "description": "One of: create, list, update, pause, resume, remove, run. When action=create, the 'schedule' and 'prompt' fields are REQUIRED."
            },
            "job_id": {
                "type": "string",
                "description": "Required for update/pause/resume/remove/run."
            },
            "pinned": {
                "type": "boolean",
                "description": "For create/update. ONLY set when the user explicitly asks to pin (or unpin) a job's model. pinned=true locks the CURRENT main agent model (and its provider) onto the job so later `hermes model` / `/model` changes never touch it; pinned=false releases the lock so the job follows the main agent model again. Never set it on your own initiative: by default jobs follow the main model."
            },
            "prompt": {
                "type": "string",
                "description": "For create: the full self-contained prompt (paired with any skills as the task instruction). For run: optional transient context for that single fire (never persisted)."
            },
            "schedule": {
                "type": "string",
                "type": "string",
                "description": "REQUIRED for create. Schedule forms: (1) recurring interval — '30m', 'every 2h', 'every hour' (EVERY 30 minutes / 2 hours / hour, forever by default); (2) explicit one-shot by duration — 'in 30m', 'in 2h' (fires ONCE that far from now; use this for 'remind me in N minutes' — do NOT hand-compute an absolute timestamp); (3) natural day/time — 'every monday 9am', 'weekdays at 9am', 'every day at 9am' (recurring weekly/daily); (4) cron syntax — '0 9 * * *' (daily 9am); (5) absolute one-shot — ISO timestamp '2026-06-01T09:00:00'."
            },
            "name": {
                "type": "string",
                "description": "Optional human-friendly name"
            },
            "repeat": {
                "type": "integer",
                "description": "Optional repeat count. Omit for defaults (once for one-shot, forever for recurring)."
            },
            "deliver": {
                "type": "string",
                "description": "Where the job's output is POSTED as a one-way message (the job itself always runs in a fresh session with no chat context). Omit to address the chat/topic this job was created from. Otherwise: 'local' (save only, no delivery), 'all' (every connected home channel, resolved at fire time), 'bot-chat' or 'bot-chat:<profile>' (inject into a Bot Chat as a real message), or platform:chat_id:thread_id (e.g. 'telegram:-1001234567890:17585'). Comma-combine like 'origin,all'."
            },
            "model": {
                "type": "object",
                "description": "Optional per-job model override, as an object {\"model\": \"<name>\", \"provider\": \"<provider>\"}. A flat model-name STRING (e.g. model=\"gpt-5.6-sol\" with a sibling provider=\"openai-codex\") is also accepted and coerced to this object. Use model='auto' to pin the job to the CREATING agent's own model (recommended for LLM crons — otherwise an unpinned job inherits the runtime primary, often Opus, at fire time). If provider is omitted (and model is not 'auto'), the current main provider is pinned at creation time so the job stays stable. Explicit flagship models require allow_flagship_reason.",
                "properties": {
                    "provider": {
                        "type": "string",
                        "description": "Provider name (e.g. 'openrouter', 'anthropic', or 'custom:<name>' for a provider defined in custom_providers config — always include the ':<name>' suffix, never pass the bare 'custom'). Omit to use and pin the current provider."
                    },
                    "model": {
                        "type": "string",
                        "description": "Model name (e.g. 'anthropic/claude-sonnet-4', 'claude-sonnet-4')"
                    }
                },
                "required": ["model"]
            },
            "allow_flagship_reason": {
                "type": "string",
                "description": "Nonblank justification for an explicit flagship model override (--allow-flagship); persisted with the job for audit."
            },
            "failure_deliver": {
                "type": "string",
                "description": "Optional override target for FAILURE notices only (same grammar as deliver). When set, engine failure/interruption notices go here instead of the deliver target; 'local' suppresses them entirely (state still recorded in cron list/run history). Use for jobs delivering into shared channels where failure noise is unwanted. Omit = failures follow deliver (default). On update, '' clears."
            },
            "skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional ordered skill names loaded before the cron prompt. On update, [] clears."
            },
            "script": {
                "type": "string",
                "description": _script_description("the profile HERMES_HOME")
            },
            "monitor": {
                "type": "string",
                "description": "Optional change-detector that gates the agent: an http(s) URL (fetched each tick) or a script path (same rules as `script`, run each tick) — cheap, no LLM. Output identical to the previous tick skips the agent run entirely; changed output wakes the agent with a diff injected into the prompt. First tick always runs (baseline). Output must be deterministic (no timestamps) or every tick looks changed. Incompatible with no_agent. On update, '' clears."
            },
            "no_agent": {
                "type": "boolean",
                "default": False,
                "description": "True = no LLM: the scheduler runs `script` (required) on schedule and delivers its stdout verbatim; empty stdout sends nothing (watchdog pattern). Use for script-only pings with fixed output; keep False for anything needing reasoning."
            },
            "context_from": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional job ID(s) whose most recent completed output is injected as context each run — chains jobs (A collects, B processes). For a job's OWN previous output prefer `continuity`. On update, [] clears."
            },
            "continuity": {
                "type": "boolean",
                "description": "True = each run sees the job's own previous output, so it can dedupe and continue where it left off (scouts, monitors, incremental digests). Default false. On update, false turns it off."
            },
            "enabled_toolsets": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional toolset names to restrict the job's agent to (e.g. [\"web\", \"terminal\"]) — cuts token overhead. Infer from the prompt. Omit for all default tools. On update, [] clears."
            },
            "workdir": {
                "type": "string",
                "description": "Optional absolute existing path to run the job from: injects that directory's AGENTS.md/context files and anchors terminal/file tools there. On update, '' clears."
            },
            "attach_to_session": {
                "type": "boolean",
                # parity 2026-08-30 / 2026-10-01: reasoning_effort DELIBERATELY absent from the
                # model-facing schema on BOTH sides (models never choose reasoning config; the CLI is
                # the only mutation surface). Converged — do not re-add.
                "description": "True = the job's delivery is CONTINUABLE — the user can reply and the agent has the brief in context (threads on thread-capable platforms, mirrored into the DM elsewhere). Use for conversational recurring jobs (briefings); leave unset for fire-and-forget alerts. Scope: the job's own conversation only — the origin chat, the home-channel fallback when deliver='origin' captured no origin (script-created jobs), a user-written bare platform target (deliver='slack' — that platform's home channel), or the job's single explicit platform:chat target (this flag is the only way to attach an explicit target). Broadcast targets are never attached; no effect when deliver='local'."
            },
        },
        "required": ["action"]
    }
}


def _resolve_model_override(model_obj: Optional[Dict[str, Any]]) -> tuple:
    """Resolve a model override object into (provider, model) for job storage.

    If provider is omitted, pins the current main provider from config so the
    job doesn't drift when the user later changes their default via hermes model.

    Returns (provider_str_or_none, model_str_or_none).
    """
    if not model_obj or not isinstance(model_obj, dict):
        return (None, None)
    model_name = (model_obj.get("model") or "").strip() or None
    provider_name = (model_obj.get("provider") or "").strip() or None
    # Bare "custom" is usually an incomplete spec — the canonical form is
    # "custom:<name>" matching a custom_providers entry, and LLMs frequently
    # supply the bare type because the schema does not advertise the
    # ":<name>" suffix. It is only a problem when it can't resolve at runtime:
    # a user may literally name a ``providers.custom`` (or custom_providers
    # "custom") entry, in which case the job should keep ``provider="custom"``
    # and run against that endpoint. Only when no such entry exists do we treat
    # the bare value as "no provider supplied" and pin the current main
    # provider below — otherwise pinning to ``model.provider`` (e.g. codex)
    # silently hijacks a job that meant to use the configured custom endpoint.
    if provider_name == "custom":
        try:
            from hermes_cli.runtime_provider import has_named_custom_provider
            if not has_named_custom_provider("custom"):
                provider_name = None
        except Exception:
            provider_name = None
    # "auto" is a sentinel meaning "pin to the creating agent's model" — do NOT
    # pin the config main provider to it here; _resolve_cron_llm_model (called
    # in the create path) resolves it against the live agent. Pinning a provider
    # now would leave a stale provider glued to an unresolved "auto".
    if model_name and model_name.strip().lower() == "auto":
        return (provider_name, model_name)
    if model_name and not provider_name:
        live_provider, _ = get_current_agent_model()
        pool_provider = _pool_for_single_sub(live_provider, model_name)
        if pool_provider:
            return (pool_provider, model_name)
        # Pin to the current main provider so the job is stable
        try:
            from hermes_cli.config import load_config
            cfg = load_config()
            model_cfg = cfg.get("model", {})
            if isinstance(model_cfg, dict):
                provider_name = model_cfg.get("provider") or None
        except Exception:
            pass  # Best-effort; provider stays None
    return (provider_name, model_name)


def _coerce_model_override_arg(
    model_arg: Any, provider_arg: Optional[str]
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Normalize the tool's ``model`` argument into the ``{model, provider}`` object
    ``_resolve_model_override`` expects, tolerating the common flat-string call shape.

    The ``cronjob`` schema documents ``model`` as an OBJECT
    (``{"model": "...", "provider": "..."}``), but callers frequently pass a flat
    string (``model="gpt-5.6-sol"``) plus a sibling ``provider="openai-codex"`` —
    the shape every OTHER model-bearing tool uses. Before this coercion the flat
    string silently failed ``_resolve_model_override``'s ``isinstance(dict)`` guard
    → ``(None, None)`` → the job fell through to ``cron.default_model`` (often
    ``auto``) and pinned to the CREATING agent's model instead of the requested
    one — an explicit input SILENTLY dropped.

    Returns ``(model_obj_or_None, warning_or_None)``:
      * dict passed through untouched (canonical shape) → no warning.
      * bare string → wrapped as ``{"model": <str>, "provider": <provider_arg>}``
        (folding in the sibling flat ``provider`` so it isn't masked by the
        config-main-provider pin) → no warning; the intuitive call now works.
      * a non-string / non-dict truthy value (e.g. a list/number) → returned as
        ``None`` WITH a warning, so the caller learns the spec was ignored rather
        than silently getting auto-pinned.
    """
    if model_arg is None:
        return (None, None)
    if isinstance(model_arg, dict):
        return (model_arg, None)
    if isinstance(model_arg, str):
        text = model_arg.strip()
        if not text:
            return (None, None)
        obj: Dict[str, Any] = {"model": text}
        prov = (provider_arg or "").strip() if isinstance(provider_arg, str) else ""
        if prov:
            obj["provider"] = prov
        return (obj, None)
    # Truthy but neither str nor dict — cannot interpret; do NOT silently drop.
    return (
        None,
        "model spec ignored — expected an object {\"model\": ..., \"provider\": ...} "
        "or a plain model-name string; the job was left to auto-pin. "
        f"(got {type(model_arg).__name__})",
    )


def check_cronjob_requirements() -> bool:
    """Available in interactive CLI mode, gateway/messaging platforms, and cron runs (the
    scheduler is internal; no crontab needed). Flags must be explicitly truthy via
    ``env_var_enabled``. An external cron worker has the presence vars stripped from its env, so
    the cron session marker keeps ``cron.allow_agent_scheduling`` meaningful there."""
    from gateway.session_context import get_session_env
    from utils import env_var_enabled, is_truthy_value
    return (
        env_var_enabled("HERMES_INTERACTIVE")
        or env_var_enabled("HERMES_GATEWAY_SESSION")
        or env_var_enabled("HERMES_EXEC_ASK")
        or is_truthy_value(get_session_env("HERMES_CRON_SESSION", ""))
    )


# Agent-facing arguments forwarded verbatim to cronjob(). base_url is intentionally NOT here: a
# model-supplied endpoint must never route a stored credential elsewhere. ``model``/``provider``
# ARE model-facing on the fork (object or flat string + ``allow_flagship_reason``), fenced by the
# admission gates above: cross-vendor refusal, the flagship ban (``validate_worker_model``) and the
# ``auto`` sentinel that pins the creating agent's OWN model rather than letting the agent pick one.
_HANDLER_FORWARDED_ARGS = (
    "job_id", "prompt", "schedule", "name", "repeat", "deliver", "failure_deliver", "skill", "skills", "reason",
    "script", "context_from", "continuity", "enabled_toolsets", "workdir", "no_agent", "attach_to_session",
    "paused_reason", "pinned", "allow_flagship_reason")


def _cronjob_handler(args, **kw):
    """Model-tool dispatch.

    Coerces the ``model`` argument (the canonical ``{model, provider}`` object OR a flat model-name
    string + sibling ``provider``) into the override object, resolves it to a stored ``(provider,
    model)`` pair, and threads any coercion warning through so an ignored/malformed model spec is
    surfaced instead of silently auto-pinned. Resolves the one model-facing ``monitor`` field into
    the stored ``monitor_script``/``monitor_url`` pair (legacy field names still accepted)."""
    model_obj, spec_warning = _coerce_model_override_arg(args.get("model"), args.get("provider"))
    resolved_provider, resolved_model = _resolve_model_override(model_obj)
    # The object flattener normally pins a model-only spec to the live/config provider. A request
    # whose target is script-only must not inherit that route before cronjob() can apply its
    # effective-mode guard.
    if args.get("no_agent") is True and isinstance(model_obj, dict) and not model_obj.get("provider"):
        resolved_provider = None
    # An uninterpretable model spec (spec_warning set) means "leave the job auto-pinned" — a stray
    # sibling ``provider`` must not leak through as a provider-without-model half-pin.
    _fallback_provider = None if spec_warning else args.get("provider")
    _mon_script, _mon_url = _split_monitor_arg(args.get("monitor"), args.get("monitor_script"), args.get("monitor_url"))
    return cronjob(
        action=args.get("action", ""),
        include_disabled=args.get("include_disabled", True),
        model=resolved_model,
        provider=resolved_provider or _fallback_provider,
        model_spec_warning=spec_warning,
        monitor_script=_mon_script,
        monitor_url=_mon_url,
        task_id=kw.get("task_id"),
        session_id=kw.get("session_id"),
        paused=args.get("paused", False),
        **{key: args.get(key) for key in _HANDLER_FORWARDED_ARGS},
    )


registry.register(
    name="cronjob_manage",
    toolset="cronjob",
    schema=CRONJOB_SCHEMA,
    handler=_cronjob_handler,
    check_fn=check_cronjob_requirements,
    emoji="⏰",
    dynamic_schema_overrides=_cronjob_schema_overrides,
)
