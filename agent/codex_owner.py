"""Codex pool ownership and single-use refresh transactions.

Ownership comes from the selected auth store, never from an account or token
match. Existing provider-wide shadowing is intentional: local rows and root
rows cannot be mixed implicitly. All operations acquire the pool mutex before
one owner auth lock; they never acquire a profile lock while holding root.
"""

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import uuid

import hermes_cli.auth as auth

PROVIDER = "openai-codex"
TOKEN_FIELDS = ("access_token", "refresh_token", "last_refresh")


def _error(message, code="codex_refresh_uncertain"):
    return auth.AuthError(message, provider=PROVIDER, code=code, relogin_required=True)


def _rows(store):
    return store.get("credential_pool", {}).get(PROVIDER, [])


def _tokens(store):
    return store.get("providers", {}).get(PROVIDER, {}).get("tokens", {})


def resolve_owner():
    local = auth._auth_file_path()
    store = auth._load_auth_store(local)
    # A declared local singleton is also a local grant, not permission to
    # overwrite or seed from the root singleton for the same account.
    if _rows(store) or _tokens(store):
        return local
    root = auth._global_auth_file_path()
    if root is not None:
        global_store = auth._load_auth_store(root)
        if _rows(global_store) or _tokens(global_store):
            return root
    return local


def require_local_admin(pool):
    if not auth._same_path(pool._auth_owner, auth._auth_file_path()):
        raise _error(
            "Inherited Codex pool: add/remove at its root owner. Mixing local and shared rows requires an explicit migration.",
            "codex_owner_migration_required",
        )


def _require_usable(entry):
    from agent.credential_pool import _exhausted_until
    import time

    if entry.last_status == "dead":
        raise _error("Codex credential is quarantined; authenticate at its owner.")
    until = _exhausted_until(entry)
    if until is not None and until > time.time():
        raise auth.AuthError(
            "Codex credential is in cooldown.",
            provider=PROVIDER,
            code=auth.CODEX_RATE_LIMITED_CODE,
            relogin_required=False,
        )


def resolve_runtime(*, force_refresh, refresh_if_expiring, refresh_skew_seconds):
    pool = load()
    if not pool._entries:
        store = auth._load_auth_store(pool._auth_owner)
        if "device_code" in store.get("suppressed_sources", {}).get(PROVIDER, []):
            raise _error(
                "Codex singleton is suppressed; explicitly authenticate or repair its owner before use.",
                "codex_source_suppressed",
            )
        return None
    # Preserve the singleton runtime selection where it is explicitly declared;
    # never match a manual grant by account ID or by its token bytes.
    has_singleton = bool(
        _tokens(auth._load_auth_store(pool._auth_owner)).get("access_token")
    )
    singletons = (
        [e for e in pool._entries if e.source == "device_code"] if has_singleton else []
    )
    candidates = singletons or pool._entries
    from agent.credential_pool import STATUS_DEAD, STATUS_EXHAUSTED, _exhausted_until
    import time

    blocked = None
    for entry in candidates:
        if entry.last_status == STATUS_DEAD:
            blocked = _error(
                "Codex credential is quarantined; authenticate at its owner."
            )
            continue
        if entry.last_status == STATUS_EXHAUSTED:
            until = _exhausted_until(entry)
            if until is not None and until > time.time():
                if not pool._codex_quota_restored_upstream(entry):
                    blocked = auth.AuthError(
                        "Codex credential is in cooldown.",
                        provider=PROVIDER,
                        code=auth.CODEX_RATE_LIMITED_CODE,
                        relogin_required=False,
                    )
                    continue
                cleared = replace(
                    entry,
                    last_status=None,
                    last_status_at=None,
                    last_error_code=None,
                    last_error_reason=None,
                    last_error_message=None,
                    last_error_reset_at=None,
                )
                pool._replace_entry(entry, cleared)
                pool._persist()
                entry = cleared
        if force_refresh or (
            refresh_if_expiring
            and auth._codex_access_token_is_expiring(
                entry.access_token, refresh_skew_seconds
            )
        ):
            entry = refresh(pool, entry, force_refresh)
        else:
            entry = sync(pool, entry)
        _require_usable(entry)
        return {
            "provider": PROVIDER,
            "base_url": os.getenv("HERMES_CODEX_BASE_URL", "").strip().rstrip("/")
            or auth.DEFAULT_CODEX_BASE_URL,
            "api_key": entry.access_token,
            "source": "hermes-auth-store" if singletons else "credential_pool",
            "last_refresh": entry.last_refresh,
            "auth_mode": "chatgpt",
        }
    if blocked is not None:
        raise blocked
    return None


def _receipt(owner, entry):
    # A receipt is scoped to the declared owner and row. Fingerprints detect
    # replay of a generation; they do NOT infer ownership across stores.
    key = hashlib.sha256(
        (entry.id + "\0" + (entry.refresh_token or "")).encode()
    ).hexdigest()
    return owner.with_name(owner.name + ".codex-refresh") / (key + ".json")


def _sync_dir(path):
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _reserve(owner, entry):
    path = _receipt(owner, entry)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _sync_dir(path.parent.parent)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        # No tokens, account identifiers, or exception text in receipts.
        json.dump({"version": 1, "outcome": "uncertain"}, handle)
        handle.flush()
        os.fsync(handle.fileno())
    _sync_dir(path.parent)
    return path


def load():
    from agent.credential_pool import CredentialPool, PooledCredential, label_from_token

    owner = resolve_owner()
    with auth._auth_store_lock(target_path=owner):
        store = auth._load_auth_store(owner)
        raw = deepcopy(_rows(store))
        tokens = _tokens(store)
        suppressed = store.get("suppressed_sources", {}).get(PROVIDER, [])
        changed = False
        # Only the declared singleton source is bound to this store's singleton.
        # Manual grants are NEVER joined by account ID or token equality.
        if tokens.get("access_token") and "device_code" not in suppressed:
            singles = [r for r in raw if r.get("source") == "device_code"]
            if len(singles) > 1:
                raise _error(
                    "Ambiguous Codex singleton rows; repair ownership before refreshing."
                )
            if not singles:
                raw.append(
                    dict(
                        id=uuid.uuid4().hex,
                        source="device_code",
                        auth_type="oauth",
                        priority=len(raw),
                        label=label_from_token(tokens["access_token"], "device_code"),
                        base_url=auth.DEFAULT_CODEX_BASE_URL,
                        **{k: tokens.get(k) for k in TOKEN_FIELDS},
                    )
                )
                changed = True
        ids = set()
        for r in raw:
            if not r.get("id"):
                r["id"] = uuid.uuid4().hex
                changed = True
            if r["id"] in ids:
                raise _error(
                    "Duplicate Codex pool row identities; repair the owner store."
                )
            ids.add(r["id"])
        if changed:
            store.setdefault("credential_pool", {})[PROVIDER] = raw
        # Keep the established singleton freshness gate, but only against the
        # explicitly owned singleton, never a fallback from another store.
        if tokens.get("access_token") and "device_code" not in suppressed:
            from agent.credential_pool import _upsert_entry

            entries = [PooledCredential.from_dict(PROVIDER, r) for r in raw]
            state = store["providers"][PROVIDER]
            seeded = _upsert_entry(
                entries,
                PROVIDER,
                "device_code",
                {
                    "source": "device_code",
                    "auth_type": "oauth",
                    "access_token": tokens.get("access_token"),
                    "refresh_token": tokens.get("refresh_token"),
                    "last_refresh": state.get("last_refresh"),
                    "base_url": auth.DEFAULT_CODEX_BASE_URL,
                    "label": state.get("label")
                    or label_from_token(tokens["access_token"], "device_code"),
                },
            )
            if seeded:
                by_id = {r["id"]: r for r in raw}
                raw = [dict(by_id.get(e.id, {}), **e.to_dict()) for e in entries]
                changed = True
        if changed:
            store.setdefault("credential_pool", {})[PROVIDER] = raw
            auth._save_auth_store(store, target_path=owner)
        pool = CredentialPool(
            PROVIDER, [PooledCredential.from_dict(PROVIDER, r) for r in raw]
        )
        pool._auth_owner = owner
        pool._owner_baseline = {e.id: e.to_dict() for e in pool._entries}
        # A killed writer leaves the original row but its receipt fences it.
        pool._entries = [
            replace(e, last_status="dead", last_error_reason="codex_refresh_uncertain")
            if _receipt(owner, e).exists()
            else e
            for e in pool._entries
        ]
        return pool


def persist(pool, removed_ids=None):
    """Apply deltas to current rows; never resurrect removed snapshots."""
    owner = pool._auth_owner
    with auth._auth_store_lock(target_path=owner):
        store = auth._load_auth_store(owner)
        current = _rows(store)
        baseline = pool._owner_baseline
        incoming = {e.id: e.to_dict() for e in pool._entries}
        removed = set(removed_ids or ())
        merged = []
        for disk in current:
            ident = disk.get("id")
            before = baseline.get(ident)
            same_generation = (
                before is not None
                and disk.get("source") == before.get("source")
                and all(disk.get(k) == before.get(k) for k in TOKEN_FIELDS)
            )
            if ident in removed and same_generation:
                if disk.get("source") == "device_code":
                    store.get("providers", {}).get(PROVIDER, {}).pop("tokens", None)
                continue
            item = dict(disk)
            after = incoming.get(ident)
            if before is not None and after is not None and same_generation:
                # Preserve unknown fields and concurrent unchanged metadata.
                delta = {k: v for k, v in after.items() if v != before.get(k)}
                item.update(delta)
                if any(disk.get(k) != before.get(k) for k in auth._POOL_STATUS_FIELDS):
                    item = auth._merge_disk_cooldown_state(item, disk, PROVIDER)
            merged.append(item)
        known = {r.get("id") for r in current}
        for ident, after in incoming.items():
            if ident not in baseline and ident not in known and ident not in removed:
                merged.append(after)
        store.setdefault("credential_pool", {})[PROVIDER] = merged
        auth._save_auth_store(store, target_path=owner)
        # Keep the baseline paired with the actual in-memory snapshot. Do not
        # advance it to an unseen peer generation (that would allow clobbering).
        pool._owner_baseline = deepcopy(incoming)


def _current(pool, entry):
    from agent.credential_pool import PooledCredential

    store = auth._load_auth_store(pool._auth_owner)
    matches = [r for r in _rows(store) if r.get("id") == entry.id]
    if len(matches) != 1 or matches[0].get("source") != entry.source:
        raise _error(
            "Codex credential was removed or replaced; reload the pool.",
            "codex_row_removed",
        )
    current = PooledCredential.from_dict(PROVIDER, matches[0])
    if _receipt(pool._auth_owner, current).exists():
        raise _error(
            "Codex refresh outcome is uncertain; authenticate a new grant at its owner."
        )
    return store, current


def sync(pool, entry):
    with pool._lock, auth._auth_store_lock(target_path=pool._auth_owner):
        _, current = _current(pool, entry)
        if current.to_dict() == entry.to_dict():
            return entry
        pool._replace_entry(entry, current)
        pool._owner_baseline[current.id] = current.to_dict()
        return current


def refresh(pool, entry, force):
    # Match persistence's mutex -> owner order even on deferred refresh paths.
    with (
        pool._lock,
        auth._auth_store_lock(
            target_path=pool._auth_owner,
            timeout_seconds=pool._single_use_refresh_lock_timeout(),
        ),
    ):
        store, current = _current(pool, entry)
        if any(getattr(current, k) != getattr(entry, k) for k in TOKEN_FIELDS):
            _require_usable(current)
            pool._replace_entry(entry, current)
            pool._owner_baseline[current.id] = current.to_dict()
            return current  # peer committed; even forced waiters must not POST again
        if current.last_status == "dead":
            raise _error("Codex credential is quarantined; authenticate a new grant.")
        receipt = _reserve(pool._auth_owner, current)  # durable BEFORE any POST
        try:
            refreshed = auth.refresh_codex_oauth_pure(
                current.access_token, current.refresh_token
            )
        except auth.AuthError as exc:
            # A definite quota rejection did not consume the grant. All other
            # errors stay fenced: transport/5xx/malformed success are uncertain,
            # invalid-grant/reused are terminal. Never claim refresh succeeded.
            if exc.code == auth.CODEX_RATE_LIMITED_CODE:
                receipt.unlink()
                _sync_dir(receipt.parent)
            raise
        updated = replace(
            current,
            **{k: refreshed.get(k) for k in TOKEN_FIELDS},
            last_status="ok",
            last_status_at=None,
            last_error_code=None,
            last_error_reason=None,
            last_error_message=None,
            last_error_reset_at=None,
        )
        for r in _rows(store):
            if r.get("id") == current.id:
                r.update({
                    k: updated.to_dict().get(k)
                    for k in (*TOKEN_FIELDS, *auth._POOL_STATUS_FIELDS)
                })
        if current.source == "device_code":
            state = store.setdefault("providers", {}).setdefault(PROVIDER, {})
            state.setdefault("tokens", {}).update({
                k: refreshed.get(k) for k in TOKEN_FIELDS
            })
            state["last_refresh"] = refreshed.get("last_refresh")
        # Receipt survives failure, and also fences accidental restoration of
        # the consumed generation after a successful commit. Fresh grants have
        # a different generation and recover without deleting the receipt.
        auth._save_auth_store(store, target_path=pool._auth_owner)
        pool._replace_entry(entry, updated)
        pool._owner_baseline[updated.id] = updated.to_dict()
        return updated
