"""SessionStore storage plumbing: per-profile SessionDB handle resolution and the routing-index
load/save paths (state.db gateway_routing primary, sessions.json legacy mirror). Mixin split out of
``gateway/session.py``; bound onto ``SessionStore`` via the MRO."""

from __future__ import annotations

import contextlib
import logging
import json
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from gateway.session import SessionEntry

# Log-record parity with the origin module.
logger = logging.getLogger("gateway.session")

# "No SessionDB pinned" sentinel: lets ``_db`` distinguish "resolve from the active scope" from a
# deliberate ``store._db = None`` (JSONL fallback).
_DB_UNPINNED = object()

# Self-documenting sentinel written first into sessions.json; "_" keys are skipped on load.
_SESSIONS_JSON_README = (
    "LEGACY MIRROR of the gateway routing index (the primary copy lives in the gateway_routing "
    "table in ~/.hermes/state.db). Maps messaging session keys (agent:main:<platform>:...) to "
    "active session IDs. This is NOT the session list. ALL sessions (CLI, TUI, and gateway) live "
    "in ~/.hermes/state.db and are shown by `hermes sessions list` and `/sessions`. Disable this "
    "file with `gateway.write_sessions_json: false` in config.yaml."
)


def _is_live_system_guard(exc: BaseException) -> bool:
    """Test-isolation guard: must stay a loud failure and is never cached."""
    return isinstance(exc, RuntimeError) and "live-system guard" in str(exc)


class SessionPersistenceMixin:
    """SessionStore storage plumbing: SessionDB handle resolution and routing-index load/save."""

    def _open_session_db_for_active_scope(self, db_path: Optional[Path] = None):
        """SessionDB for the active profile scope. ``db_path`` pins the store; otherwise
        ``_default_db_path()`` follows the context-local HERMES_HOME (resolved per call so
        multiplexed profiles reach their own store). Handles are cached per path; failed opens enter
        a bounded backoff during which callers keep using the JSONL fallback.

        Resolving here rather than once in ``__init__`` is the whole fix for #88532: it lets the scoping
        that the multiplexed inbound path already performs actually reach session storage.
        """
        from hermes_state import _default_db_path
        from hermes_state_registry import acquire

        path = Path(db_path) if db_path is not None else Path(_default_db_path())

        def _open():
            try:
                return acquire(path)  # process-wide registry: one writer per path
            except Exception as e:
                if not _is_live_system_guard(e):
                    print(f"[gateway] Warning: SQLite session store unavailable, falling back to JSONL: {e}")
                raise

        return self._db_handle_cache.get(path, _open, non_cacheable=_is_live_system_guard)

    def _pinned_db(self):
        """Return the explicitly pinned DB (``store._db = x``), else ``_DB_UNPINNED``."""
        return getattr(self, "_db_pinned", _DB_UNPINNED)

    @property
    def _db(self):
        """The SessionDB for the active profile scope, or a pinned override. Assigning ``store._db``
        pins that value for every subsequent read (tests install a fake or disable the DB with
        ``store._db = None``); unpinned, each read resolves the scope so a multiplexed profile's
        writes reach its own store."""
        pinned = self._pinned_db()
        return self._open_session_db_for_active_scope() if pinned is _DB_UNPINNED else pinned

    @_db.setter
    def _db(self, value) -> None:
        self._db_pinned = value

    @property
    def _routing_db(self):
        """The one store that owns the routing index, whatever scope is active. ``_entries`` is one
        flat dict holding every profile's keys, so it must persist to ONE file (``_routing_home``),
        not whichever profile is scoped — otherwise a mid-turn rewrite and the unscoped startup
        load see different copies and crash markers under a secondary profile go unrecovered. A
        pinned handle still wins; bare test instances lacking the handle cache report no DB.

        Reading it through ``_db`` made that file whichever profile happened to be scoped at the time: a
        whole-index rewrite during one profile's turn copied every other profile's routing rows into that
        profile's store, and startup — which runs unscoped — then loaded a different copy than the one the
        last writer produced. See #66887.
        """
        pinned = self._pinned_db()
        if pinned is not _DB_UNPINNED:
            return pinned
        home = getattr(self, "_routing_home", None)
        try:
            if home is None:
                return self._db
            # A re-pointed ``hermes_state.DEFAULT_DB_PATH`` is already one process-wide store and
            # is what ``_db`` resolves to (``_default_db_path``); honour the same pin here or the
            # routing rewrite and the row reads/prunes land in two different files.
            import hermes_state as _hs
            override = getattr(_hs, "DEFAULT_DB_PATH", None)
            if override is not None and override != getattr(_hs, "_IMPORT_DEFAULT_DB_PATH", None):
                return self._open_session_db_for_active_scope(db_path=Path(override))
            return self._open_session_db_for_active_scope(db_path=home / "state.db")
        except Exception:
            return None

    def _named_profile_for_key(self, session_key: Optional[str]) -> Optional[str]:
        """The non-default profile that owns *session_key*, or None (ambient store is authoritative:
        multiplexing off, or legacy ``agent:main``). Deliberately does NOT cover "that profile has
        no directory" — ownership and resolvability are separate questions for ``_db_for_key``."""
        if not getattr(self.config, "multiplex_profiles", False):
            return None
        profile = self._profile_from_session_key(session_key)
        return None if not profile or profile == "default" else profile

    def _profile_home_for_key(self, session_key: Optional[str]) -> Optional[Path]:
        """HERMES_HOME of the profile owning *session_key*, or None (no named owner or
        unresolvable)."""
        profile = self._named_profile_for_key(session_key)
        if profile is None:
            return None
        cache = self._profile_home_cache
        if profile in cache:
            return cache[profile]
        home: Optional[Path] = None
        try:
            from hermes_cli.profiles import get_profile_dir, profile_exists
            if profile_exists(profile):
                home = Path(get_profile_dir(profile))
        except Exception as exc:
            logger.debug("Could not resolve profile home for %r: %s", session_key, exc)
            home = None
        # Only hits are memoized: a profile directory can be provisioned *after* startup (enrollment
        # bridge), and a cached miss would pin that profile's rows to the ambient store for life.
        if home is not None:
            cache[profile] = home
        return home

    def _db_for_key(self, session_key: Optional[str]):
        """The SessionDB holding *session_key*'s rows, whatever scope is active (the owning profile
        is encoded in the key). ``_db`` follows the ambient HERMES_HOME that only the inbound message
        path installs; unscoped background work (expiry watcher) would otherwise write profile rows
        into the ROOT store until the stale-route self-heal drops a live conversation.

        Background work runs unscoped while operating on every profile's keys out of the single process-wide
        ``_entries`` dict — ``_session_housekeeping_watcher`` is the clearest case — so it reads and writes the
        ROOT store for rows that actually live under ``profiles/<name>/state.db``. The two writers then
        drift apart on the same logical session until the routing index disagrees with the row and the
        #54878 self-heal drops a live conversation (#66887).
        """
        pinned = self._pinned_db()
        if pinned is not _DB_UNPINNED:
            return pinned
        profile = self._named_profile_for_key(session_key)
        if profile is None:
            # Default-profile (``agent:main``) rows belong to the launch home, not to whichever
            # profile's scope happens to be active: a scoped drain tick or cron mirror touching a
            # default chat used to write its rows into the secondary's store (#102157's picture).
            routing_home = getattr(self, "_routing_home", None)
            if routing_home is None or not getattr(self.config, "multiplex_profiles", False):
                return self._db
            try:
                return self._open_session_db_for_active_scope(db_path=routing_home / "state.db")
            except Exception:
                return None
        home = self._profile_home_for_key(session_key)
        if home is None:
            # Falling back to the ambient store would split ONE session identity across two
            # physical stores — fail closed; callers already handle a missing DB.
            logger.warning(
                "gateway.session: profile %r has no resolvable home (key %r); refusing to fall "
                "back to the ambient store", profile, session_key)
            return None
        try:
            return self._open_session_db_for_active_scope(db_path=home / "state.db")
        except Exception:
            return None  # same contract as ``_db``: a failed open degrades to JSONL fallback

    def _owner_key_for_session_id(self, session_id: Optional[str]) -> Optional[str]:
        """The routing key that owns *session_id*, or None. The published index is authoritative;
        ``_session_owner_hints`` covers the window where ownership is proven but routing not yet
        published. Deliberately lock-free: several callers already hold ``_lock``."""
        if not session_id:
            return None
        try:
            for entry in list(self._entries.values()):
                if entry.session_id == session_id:
                    return entry.session_key
        except Exception:
            pass  # bare stores / foreign entry objects in suites
        return (getattr(self, "_session_owner_hints", None) or {}).get(session_id)

    def _db_for_session_id(self, session_id: Optional[str]):
        """The SessionDB holding *session_id*'s row (owner from the index or a pre-published hint;
        unknown ids fall back to the ambient store)."""
        if not session_id:
            return self._db
        return self._db_for_key(self._owner_key_for_session_id(session_id))

    def close_all_db_handles(self) -> None:
        """Close every SessionDB handle this store opened (one per path). Closing only ``store._db``
        would strand secondary profiles' handles with their WAL lock held ('database is locked' on
        restart). Drained under the lock, closed outside it; a pinned handle is the pinner's."""
        def _close(db) -> None:
            from hermes_state_registry import release_or_close  # shared instances no-op on close()
            try:
                release_or_close(db)
            except Exception as exc:
                logger.debug("SessionDB close error during handle sweep: %s", exc)

        # Retire the deferred sessions.json mirror writer first: a queued snapshot must land (or
        # time out loudly) before the process tears down, or a turn's routing update is lost from
        # the legacy mirror.
        try:
            if not self.stop_sessions_json_writer(timeout=10.0):
                logger.warning(
                    "gateway.session: sessions.json mirror writer did not drain within 10s at shutdown")
        except Exception as exc:  # noqa: BLE001 - shutdown must continue
            logger.debug("sessions.json writer shutdown error: %s", exc)

        self._db_handle_cache.close_all(_close)

    def _ensure_loaded(self) -> None:
        """Load sessions index from disk if not already loaded."""
        with self._lock:
            self._ensure_loaded_locked()

    def _entry_locked(self, session_key: str) -> Optional[SessionEntry]:
        """Load the index and return the entry for *session_key*. Lock held."""
        self._ensure_loaded_locked()
        return self._entries.get(session_key)

    def _routing_scope(self) -> str:
        """Namespace for this store's gateway_routing rows: the resolved sessions_dir, so stores
        with different dirs never share entries."""
        try:
            return str(Path(self.sessions_dir).resolve())
        except Exception:
            return str(self.sessions_dir)

    def _routing_db_method(self, name: str):
        """Bound ``_routing_db.<name>`` if the handle exists and has it, else None."""
        method = getattr(self._routing_db or None, name, None)
        return method if callable(method) else None

    @staticmethod
    def _routing_entry_from_json(key: str, entry_json: str) -> Optional[SessionEntry]:
        """Parse one gateway_routing row; None (with a warning) when invalid."""
        from gateway.session import SessionEntry
        try:
            entry_data = json.loads(entry_json)
            if isinstance(entry_data, dict):
                return SessionEntry.from_dict(entry_data)
        except (ValueError, KeyError, TypeError) as e:
            logger.warning("Skipping invalid routing entry %r: %s", key, e)
        return None

    def _ensure_loaded_locked(self) -> None:
        """Load the routing index (lock held). state.db ``gateway_routing`` is primary;
        sessions.json is the legacy import for keys the DB lacks (persisted on the next _save).

        Read order (#9006 follow-up): the ``gateway_routing`` table in state.db is the primary source;
        sessions.json is the legacy import path for pre-migration installs (its entries are folded in for
        keys the DB doesn't have, then persisted to the DB on the next _save).

        The reads run with ``_lock`` RELEASED when the caller holds a ``_StoreLock`` (t_cc8533d1):
        loading is the first step of every locked block, so dropping the lock exposes no
        half-applied state; if another thread finished the load meanwhile, its result wins.
        """
        if self._loaded:
            self._reconcile_recovered_routing_locked(allow_release=True)
            return
        sources = self._with_lock_released(self._read_routing_sources)
        if self._loaded:
            self._reconcile_recovered_routing_locked(allow_release=False)
            return
        db_rows, db_load_succeeded, legacy_data, legacy_error = sources
        if db_load_succeeded:
            for key, entry_json in db_rows.items():
                entry = self._routing_entry_from_json(key, entry_json)
                if entry is not None:
                    self._entries[key] = entry
        db_had_entries = db_load_succeeded and bool(self._entries)
        self._import_legacy_sessions_json(db_had_entries, legacy_data, legacy_error)
        self._loaded = True
        self._valid_routing_keys = set(self._entries)
        self._seed_chat_model_pins_locked()
        self._routing_db_loaded = db_load_succeeded
        self._routing_fallback_baseline = None if db_load_succeeded else self._entries_as_dicts()
        # Legacy spellings the resolver already retires (Discord ``channel`` -> ``group``) are
        # folded into the canonical key HERE, before any producer can write: the adapter-driven
        # migration runs only after every platform connects (76s live on 2026-09-09) while the
        # startup-restore gate releases inbound replay at 30s, so an alias still loaded then made
        # the write guard refuse the user's own chat (SessionKeyConflict bubbled into #Home).
        try:
            self._redirect_legacy_alias_routes_locked()
        except Exception:
            logger.warning("Legacy session alias redirect failed", exc_info=True)
        # A hard crash skips graceful shutdown and leaves sessions.json pointing at ended sessions.
        if self._find_session_key_conflict() is None:
            self._prune_stale_sessions_locked()
        else:
            logger.warning("Legacy duplicate session routes await canonical migration")

    def _import_legacy_sessions_json(self, db_had_entries: bool, legacy_data=None, legacy_error=None) -> None:
        """Legacy import: sessions.json fills only keys the DB lacks. Lock held. ``legacy_data`` /
        ``legacy_error`` come from ``_read_routing_sources`` (read with the lock released); without
        them the file is read here."""
        from gateway.session import SessionEntry
        if legacy_error is not None:
            print(f"[gateway] Warning: Failed to load sessions: {legacy_error}")
            return
        if legacy_data is None:
            sessions_file = self.sessions_dir / "sessions.json"
            if not sessions_file.exists():
                return
            try:
                with open(sessions_file, "r", encoding="utf-8-sig") as f:
                    legacy_data = json.load(f)
            except Exception as e:
                print(f"[gateway] Warning: Failed to load sessions: {e}")
                return
        try:
            data = legacy_data
            imported = 0
            for key, entry_data in data.items():
                # "_"-prefixed keys are sentinels (e.g. "_README"), not entries.
                if key.startswith("_") or key in self._entries:
                    continue
                if not isinstance(entry_data, dict):  # corrupt file must not abort the whole load
                    logger.warning(
                        "Skipping invalid session entry %r: expected dict, got %s", key,
                        type(entry_data).__name__)
                    continue
                try:
                    self._entries[key] = SessionEntry.from_dict(entry_data)
                    imported += 1
                except (ValueError, KeyError, TypeError) as e:
                    logger.warning("Skipping invalid session entry %r: %s", key, e)
            if imported and db_had_entries:
                logger.info(
                    "gateway.session: imported %d legacy sessions.json entr%s missing from "
                    "state.db routing table", imported, "y" if imported == 1 else "ies")
        except Exception as e:
            print(f"[gateway] Warning: Failed to load sessions: {e}")

    def _prune_stale_sessions_locked(self) -> None:
        """Remove routing entries whose session has ended in state.db, and old inert routes that
        never reached state.db (startup, lock held). Stale == ``end_reason IS NOT NULL``; rows
        absent from the DB are kept unless the routing metadata proves no activity before the
        grace window elapsed; a ``None`` DB handle is a no-op; DB errors are non-fatal.

        When the caller holds a ``_StoreLock`` the per-route state.db lookups run with the lock
        released (``_prune_stale_sessions_off_lock``, t_cc8533d1); lock-free callers (tests) get
        the synchronous pass.
        """
        if not self._entries:
            return
        from gateway.session import _StoreLock
        lock = getattr(self, "_lock", None)
        if isinstance(lock, _StoreLock) and lock.held_by_current_thread():
            # Synchronous but with ``_lock`` released: the caller's critical section (and its
            # return value, e.g. ``snapshot_entries``) must already see pruned routes.
            self._with_lock_released(self._prune_stale_sessions_off_lock)
            return
        plan = self._plan_stale_prune(list(self._entries.items()))
        if plan is not None:
            self._apply_stale_prune_locked(plan, expected=None)

    def _plan_stale_prune(self, items):
        """Decide stale / repointed routes; performs the state.db I/O. Returns ``(stale_keys,
        repointed)`` or ``None`` when a DB error made the pass unsafe (pruning skipped)."""
        stale_keys: list = []
        repointed: Dict[str, "SessionEntry"] = {}
        try:
            for key, entry in items:
                # Ask the store that owns the key, not the ambient handle, or a live
                # secondary-profile session gets pruned on the root copy.
                db = self._db_for_key(key)
                if db is None:
                    continue
                row = db.get_session(entry.session_id)
                if row is None:
                    if self._is_never_persisted_stub(entry):
                        logger.warning(
                            "gateway.session: pruning old inert routing entry %r -> %s; session "
                            "row was never persisted", key, entry.session_id)
                        stale_keys.append(key)
                    continue
                if row.get("end_reason") is None:
                    continue
                verdict = self._stale_entry_verdict(key, entry, row)
                if verdict == "prune":
                    stale_keys.append(key)
                elif verdict is not None:
                    repointed[key] = verdict
        except Exception as exc:
            logger.warning("gateway.session: stale-entry pruning skipped due to DB error: %s", exc)
            return None
        return stale_keys, repointed

    def _stale_entry_verdict(self, key: str, entry, row):
        """For a routing entry whose row has ended: ``"prune"``, a replacement entry (repoint), or
        None (keep as-is)."""
        from gateway.session_lifecycle import _now
        recovered_entry = None
        if entry.origin is not None:
            try:
                recovered_entry = self._recover_session_from_db(
                    session_key=key, source=entry.origin, now=_now(), raise_on_lookup_error=True)
            except Exception as exc:
                # Indeterminate: keep the only routing handle.
                logger.debug(
                    "gateway.session: recovery lookup failed for stale sessions.json entry %r -> "
                    "%s: %s", key, entry.session_id, exc)
                return None
        # Compression-ended parent with a newer live child for the same peer: repoint instead of
        # dropping, or queued/resume-pending work vanishes until the next message.
        if recovered_entry is not None and recovered_entry.session_id != entry.session_id:
            logger.warning(
                "gateway.session: repointing stale sessions.json entry %r from ended %s "
                "(end_reason=%r) to recovered %s", key, entry.session_id, row["end_reason"],
                recovered_entry.session_id)
            if entry.prompt_pin and (
                self._compression_tip_for_session_id(entry.session_id) == recovered_entry.session_id
            ):
                # The compression child continues this conversation: its internal turns keep the
                # pinned system bytes (the rebuilt entry is minimal and would drop them).
                recovered_entry.prompt_pin = entry.prompt_pin
            return recovered_entry
        # Same-id recovery == successful resume: keep the ORIGINAL entry object (the recovered one
        # is rebuilt minimal and would drop counters, model_override, resume markers, metadata).
        # A non-None recovery with the SAME session id is a successful resume (all recovery gates passed,
        # row reopened): keep the routing entry — it is proven valid, not a dead route (#95957). Nothing in
        # sessions.json changes, so no save is needed for this branch.
        if recovered_entry is not None:
            logger.info(
                "gateway.session: reopened ended session %s for sessions.json entry %r "
                "(end_reason=%r); keeping route", entry.session_id, key, row["end_reason"])
            return None
        logger.warning(
            "gateway.session: pruning stale sessions.json entry %r -> %s (end_reason=%r); left by "
            "a crashed gateway", key, entry.session_id, row["end_reason"])
        return "prune"

    def _entries_as_dicts(self) -> Dict[str, Any]:
        """Serializable snapshot of ``_entries``. Lock held."""
        return {key: entry.to_dict() for key, entry in self._entries.items()}

    def _save(self, *, require_primary: bool = False, retired_keys=()) -> None:
        """Persist the routing index while the caller holds ``_lock``."""
        data, generation = self._snapshot_routing_locked()
        self._persist_routing_data(
            data, generation, require_primary=require_primary, retired_keys=retired_keys)

    def _next_routing_generation_locked(self) -> int:
        """Bump and return the shared routing counter (lock held). Full snapshots AND single-entry
        fast saves MUST allocate from this one counter: the stale-write protection is a total order
        over serialization times and silently breaks otherwise."""
        self._routing_generation = getattr(self, "_routing_generation", 0) + 1
        return self._routing_generation

    def _reconcile_recovered_routing_locked(self, *, allow_release: bool = False) -> None:
        """Merge authoritative rows after a fallback-only startup load.

        ``allow_release`` (the load step only) reads state.db with ``_lock`` released; elsewhere
        the read stays in place (t_cc8533d1).
        """
        baseline = getattr(self, "_routing_fallback_baseline", None)
        if getattr(self, "_routing_db_loaded", False) or baseline is None:
            return
        loader = self._routing_db_method("load_gateway_routing_entries")
        if loader is None:
            return
        try:
            if allow_release:
                durable = self._with_lock_released(lambda: loader(scope=self._routing_scope()))
            else:
                durable = loader(scope=self._routing_scope())
        except Exception as exc:
            logger.warning("gateway.session: recovered state.db routing load failed: %s", exc)
            return
        # Another thread may have reconciled while the lock was released.
        baseline = getattr(self, "_routing_fallback_baseline", None)
        if getattr(self, "_routing_db_loaded", False) or baseline is None:
            return
        current = self._entries_as_dicts()
        for key, entry_json in durable.items():
            durable_entry = self._routing_entry_from_json(key, entry_json)
            if durable_entry is None:
                continue
            if key not in baseline:
                # A key created while on fallback wins over a DB-only key; otherwise restore the
                # authoritative row that fallback never saw.
                self._entries.setdefault(key, durable_entry)
            elif key not in current:
                continue  # loaded from fallback and deliberately removed
            elif current[key] == baseline[key]:
                self._entries[key] = durable_entry  # unchanged fallback data yields to the DB copy
        self._routing_db_loaded = True
        self._routing_fallback_baseline = None

    def _snapshot_routing_locked(self) -> tuple[Dict[str, Any], int]:
        """Capture immutable routing data and a monotonic generation."""
        self._reconcile_recovered_routing_locked()
        self._assert_unique_session_routes()
        generation = self._next_routing_generation_locked()
        # Read by ``_publish_persisted_entry``: a full snapshot numbered above a single-entry
        # write supersedes it on disk.
        self._last_full_snapshot_generation = generation
        return self._entries_as_dicts(), generation

    def _persist_routing_data(
        self, data: Dict[str, Any], generation: int, *, require_primary: bool = False,
        retired_keys=(),
    ) -> None:
        """Serialize all whole-index writers through one durable write lock. Never runs under
        ``_lock``: a caller holding it gets this write deferred until release (``_StoreLock``).
        ``require_primary``: a caller that demands a durable state.db commit (clearing a /model
        override) sees the failure instead of a silent JSON-only fallback that reverses after
        restart. ``retired_keys``: alias keys the routing-identity guard must accept as retired."""
        if self._defer_while_locked(
            lambda: self._persist_routing_data(
                data, generation, require_primary=require_primary, retired_keys=retired_keys)
        ):
            return
        from gateway.routing_identity import SessionKeyConflict
        with self._lazy("_save_lock", threading.Lock):
            if generation <= getattr(self, "_persisted_routing_generation", 0):
                return
            # Fold in fast upserts numbered above this snapshot: they were serialized after us and
            # a delayed full rewrite must not regress them.
            fast_persisted = getattr(self, "_fast_persisted_entries", None)
            if fast_persisted:
                for key, (revision, entry_json) in fast_persisted.items():
                    if revision > generation:
                        data[key] = json.loads(entry_json)
            db_saved = False
            replacer = self._routing_db_method("replace_gateway_routing_entries")
            if replacer is not None:
                try:
                    replacer(
                        {k: json.dumps(v) for k, v in data.items()}, scope=self._routing_scope(),
                        **({"retired_keys": retired_keys} if retired_keys else {}))
                    db_saved = True
                except SessionKeyConflict as exc:
                    self._reject_session_key_conflict(exc)
                except Exception as exc:
                    logger.warning("gateway.session: state.db routing save failed: %s", exc)
                    if require_primary:
                        raise
            if getattr(self, "_write_sessions_json", True) or not db_saved:
                try:
                    self._dispatch_sessions_json_save(
                        data, generation, retired_keys=retired_keys,
                        must_be_synchronous=not db_saved)
                except SessionKeyConflict as exc:
                    self._reject_session_key_conflict(exc)
                except Exception as exc:
                    if not db_saved:
                        raise
                    # state.db is authoritative: a failed legacy mirror must not report the
                    # already-committed primary write as failed.
                    logger.warning(
                        "gateway.session: sessions.json mirror save failed after state.db commit: "
                        "%s", exc)
            self._persisted_routing_generation = generation
            self._valid_routing_keys = set(data)
            # This rewrite supersedes fast records at or below its generation; newer ones stay for
            # the next delayed full writer.
            if fast_persisted:
                for key in [k for k, (rev, _) in fast_persisted.items() if rev <= generation]:
                    del fast_persisted[key]

    def _save_sessions_json(self, data: Dict[str, Any], *, retired_keys=()) -> None:
        """Write the legacy sessions.json mirror of the routing index (atomic + fsync), guarded
        by a cross-process lock and the routing-identity uniqueness check before replace."""
        from gateway.status import _try_acquire_file_lock, _release_file_lock
        from gateway.routing_identity import assert_unique_routing_entries

        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        with (self.sessions_dir / ".sessions.lock").open("a+b") as lock:
            if not _try_acquire_file_lock(lock):
                raise OSError("Session routing file is locked by another writer")
            try:
                path = self.sessions_dir / "sessions.json"
                existing = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
                assert_unique_routing_entries(data, existing, retired_keys=retired_keys)
                self._write_sessions_json_unlocked(data)
            finally:
                _release_file_lock(lock)

    def _save_entries(self) -> None:
        """Snapshot latest state under ``_lock`` and persist after releasing it."""
        with self._lock:
            data, generation = self._snapshot_routing_locked()
        self._persist_routing_data(data, generation)

    def _save_entry(
        self, session_key: str, *, entry_data: Optional[Dict[str, Any]] = None,
        lock_held: bool = False) -> Optional[int]:
        """Persist ONE routing entry via UPSERT — the per-turn fast path (a full rewrite fsyncs a
        multi-MB sessions.json). Returns the routing revision allocated to this write (``None``
        when the key vanished before capture). The key -> session_id mapping never changes here:
        structural transitions use the full rewrite (which also refreshes the sessions.json
        mirror; it may lag in metadata only). The revision comes from the shared routing
        generation counter; under ``_save_lock`` the upsert is skipped if a full snapshot or a
        newer fast save of this key already persisted (the reverse case lives in
        ``_persist_routing_data``). No DB or a failed upsert falls back to the full rewrite.
        ``entry_data`` persists a candidate BEFORE it is published to the live entry
        (failure-atomic transitions); the fallback carries it too.

        The candidate is captured under ``_lock``; the SQLite upsert (and any full-rewrite
        fallback) runs after ``_lock`` is released (t_cc8533d1), via ``_persist_captured_entry``.
        """
        from gateway.session import SessionEntry

        def _capture() -> Optional[tuple[str, int, Optional[Dict[str, Any]]]]:
            candidate = SessionEntry.from_dict(entry_data) if entry_data is not None else None
            self._assert_unique_session_routes(candidate)
            entry = self._entries.get(session_key)
            if entry is None:
                return None
            serialized = dict(entry_data) if entry_data is not None else entry.to_dict()
            # The O(n) full snapshot is deferred to the fallback branch.
            entry_json, revision = json.dumps(serialized), self._next_routing_generation_locked()
            return entry_json, revision, serialized if entry_data is not None else None

        guard = contextlib.nullcontext() if lock_held else self._lock
        with guard:
            captured = _capture()
        if captured is None:
            return None
        entry_json, revision, candidate_entry = captured
        persist = lambda inline_lock_held=False: self._persist_captured_entry(  # noqa: E731
            session_key, entry_json, revision, candidate_entry, lock_held=inline_lock_held)
        if not self._defer_while_locked(persist):
            # Reached with the lock still held only for a non-``_StoreLock`` test double; keep
            # the old in-place fallback snapshot there.
            persist(lock_held)
        return revision
