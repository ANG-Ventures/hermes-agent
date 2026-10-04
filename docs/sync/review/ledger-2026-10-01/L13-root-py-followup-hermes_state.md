# FOLLOWUP deltas for `hermes_state.py` (lane L13-root-py, parity 2026-10-01)

Upstream decomposed this file into a facade + `agent/*`/sibling mixins. The fork's changes to the
methods below were made on the pre-decomposition monolith; their owning method now lives in the
module named in each heading (outside lane L13), so the delta was NOT applied. Each block is
`diff -u` of the method body, merge-base -> fork (`-` = base, `+` = fork). Re-thread the `+` lines
into the named module's copy (adapt names: upstream may have refactored the body).

Applied in the facade (not listed): _sane_skew_ratio, _model_skew_key, _effective_last_active_visible_clause, _default_db_path, _os_account_home, _deployed_hermes_home_root, RewindWouldOrphanError, TranscriptInvariantError, SessionDB.__init__, SessionDB, SessionDB._sqlite_supports_trigram, SessionDB._drop_trigram_triggers, SessionDB._execute_write, SessionDB._row_exists, SessionDB._is_effective_last_active_visible, SessionDB._resolve_effective_last_active_root, SessionDB._expected_effective_last_active, SessionDB._recompute_effective_last_active, SessionDB._recompute_effective_last_active_for_session, SessionDB._bump_effective_last_active_for_message, SessionDB._collect_orphan_effective_last_active_targets, SessionDB._recompute_effective_last_active_many, SessionDB._backfill_effective_last_active, SessionDB._record_effective_last_active_backfill, SessionDB._needs_effective_last_active_backfill, SessionDB.expected_effective_last_active, SessionDB.recompute_effective_last_active, SessionDB.backfill_effective_last_active, SessionDB.audit_effective_last_active, SessionDB._assert_unique_gateway_routes, SessionDB.record_compression_skew_history, SessionDB.get_compression_skew_history, SessionDB.clear_compression_skew_history, SessionDB.record_model_skew_history, SessionDB.get_model_skew_history, SessionDB.clear_model_skew_history, SessionDB.update_message_finish_reason, SessionDB.most_recent_interrupt_close_session, SessionDB.get_last_turn_usage, SessionDB.upsert_desktop_resume_marker, SessionDB.get_desktop_resume_marker, SessionDB.clear_desktop_resume_marker, SessionDB.claim_desktop_auto_resume, SessionDB.clear_desktop_auto_resume_breaker, SessionDB.sweep_desktop_auto_resume_state, SessionDB._finish_session_list_rows, SessionDB._list_sessions_rich_denorm, SessionDB._active_duplicate_tool_result_ids, SessionDB._clone_message_tail_rows, SessionDB._raise_if_rewind_would_orphan_tool, SessionDB.restore_ids, SessionDB.bump_redo_count, SessionDB.search_sessions_by_title, SessionDB.session_counts_by_source; module-level helpers/imports.


## target: `hermes_state_sessions.py`

### `_delete_delegate_children`
```diff
--- base::_delete_delegate_children
+++ fork::_delete_delegate_children
@@ -1,3 +1,7 @@
-def _delete_delegate_children(conn, parent_ids: List[str]) -> List[str]:
+def _delete_delegate_children(
+    conn,
+    parent_ids: List[str],
+    orphaned_child_ids: Optional[List[str]] = None,
+) -> List[str]:
     ids = _collect_delegate_child_ids(conn, parent_ids)
     if ids:
@@ -5,4 +9,11 @@
         conn.execute(f"DELETE FROM messages WHERE session_id IN ({ph})", ids)
         # FK safety: orphan any untagged stragglers pointing at a doomed row.
+        if orphaned_child_ids is not None:
+            orphaned_child_ids.extend(
+                row["id"] for row in conn.execute(
+                    f"SELECT id FROM sessions WHERE parent_session_id IN ({ph})",
+                    ids,
+                ).fetchall()
+            )
         conn.execute(
             f"UPDATE sessions SET parent_session_id = NULL "
```

### `SessionDB._insert_session_row`
```diff
--- base::SessionDB._insert_session_row
+++ fork::SessionDB._insert_session_row
@@ -54,4 +54,5 @@
         """
         def _do(conn):
+            previous_root_id = self._resolve_effective_last_active_root(conn, session_id)
             system_prompt_hash = self._store_system_prompt(conn, system_prompt)
             conn.execute(
@@ -60,7 +61,8 @@
                    model, model_config, system_prompt, system_prompt_hash,
                    parent_session_id, cwd, profile_name, git_repo_root,
-                   origin_json, display_name, started_at
+                   origin_json, display_name, started_at,
+                   effective_last_active
                 )
-                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
+                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    ON CONFLICT(id) DO UPDATE SET
                        model = COALESCE(sessions.model, excluded.model),
@@ -186,4 +188,6 @@
                     (session_id,),
                 )
+            self._recompute_effective_last_active(conn, previous_root_id)
+            self._recompute_effective_last_active_for_session(conn, session_id)
         # Session-row creation is transcript-critical: if it fails, the
         # first flush of a new session fails and the turn is aborted as
```

### `SessionDB.end_session`
```diff
--- base::SessionDB.end_session
+++ fork::SessionDB.end_session
@@ -10,4 +10,5 @@
         """
         def _do(conn):
+            root_id = self._resolve_effective_last_active_root(conn, session_id)
             conn.execute(
                 "UPDATE sessions SET ended_at = ?, end_reason = ? "
@@ -15,3 +16,5 @@
                 (time.time(), end_reason, session_id),
             )
+            self._recompute_effective_last_active(conn, root_id)
+            self._recompute_effective_last_active_for_session(conn, session_id)
         self._execute_write(_do)
```

### `SessionDB.reopen_session`
```diff
--- base::SessionDB.reopen_session
+++ fork::SessionDB.reopen_session
@@ -6,4 +6,7 @@
         """
         def _do(conn):
+            # Fork denorm gate: capture the recency root BEFORE any mutation
+            # below, so the walk sees the pre-reopen lineage edges.
+            root_id = self._resolve_effective_last_active_root(conn, session_id)
             placeholders = ",".join("?" for _ in _RESET_END_REASONS)
             # WHERE shape shared with _RESET_CHILD_SQL's fallback arm via
@@ -24,3 +27,5 @@
                 (session_id,),
             )
+            self._recompute_effective_last_active(conn, root_id)
+            self._recompute_effective_last_active_for_session(conn, session_id)
         self._execute_write(_do)
```

### `SessionDB.clear_session_activity_labels`
```diff
--- base::SessionDB.clear_session_activity_labels
+++ fork::SessionDB.clear_session_activity_labels
@@ -21,9 +21,10 @@
         # clear. Read-only, no write lock.
         try:
-            row = self._conn.execute(
-                "SELECT last_activity_description, last_activity_provenance "
-                "FROM sessions WHERE id = ?",
-                (session_id,),
-            ).fetchone()
+            with self._read_ctx() as conn:
+                row = conn.execute(
+                    "SELECT last_activity_description, last_activity_provenance "
+                    "FROM sessions WHERE id = ?",
+                    (session_id,),
+                ).fetchone()
         except sqlite3.Error:
             row = None
```

### `SessionDB.update_session_meta`
```diff
--- base::SessionDB.update_session_meta
+++ fork::SessionDB.update_session_meta
@@ -10,4 +10,20 @@
         column unchanged.  Routes through _execute_write for the standard
         BEGIN IMMEDIATE + jitter-retry + lock guarantee.
+
+        Rewriting ``model_config`` can flip a row's session.list *visibility*
+        (it carries the ``_delegate_from`` / ``_branched_from`` markers the
+        visible-clause keys on), so the denormalized ``effective_last_active``
+        must be recomputed here — otherwise a row made delegate-only keeps a
+        stale non-NULL recency and stays visible in the flag-on denorm path
+        (or vice-versa), diverging from the CTE oracle. Mirrors the recompute
+        every other model_config-mutating path already performs.
+
+        The marker flip can also MOVE the row between compression roots (a
+        continuation child that branches away from its root, or a row that
+        joins/leaves a chain), so the row's *previous* recency root must be
+        recomputed too — otherwise the old root keeps an effective_last_active
+        that still folds in the departed child's messages and sorts ahead of
+        the CTE path. Capture the previous root BEFORE the write, like the
+        other linkage-changing paths (see create_session/upsert).
         """
         # Barrier against queued token deltas — see update_session_model.
@@ -15,7 +31,10 @@
 
         def _do(conn):
+            previous_root_id = self._resolve_effective_last_active_root(conn, session_id)
             conn.execute(
                 "UPDATE sessions SET model_config = ?, model = COALESCE(?, model) WHERE id = ?",
                 (model_config_json, model, session_id),
             )
+            self._recompute_effective_last_active(conn, previous_root_id)
+            self._recompute_effective_last_active_for_session(conn, session_id)
         self._execute_write(_do)
```

### `SessionDB.update_system_prompt`
```diff
--- base::SessionDB.update_system_prompt
+++ fork::SessionDB.update_system_prompt
@@ -10,4 +10,9 @@
                 (system_prompt_hash, session_id),
             )
+            if system_prompt is None:
+                logger.warning(
+                    "Explicit system_prompt=NULL write for session %s via "
+                    "update_system_prompt", session_id, stack_info=True,
+                )
             self._delete_unreferenced_system_prompts(conn)
         self._execute_write(_do)
```

### `SessionDB.update_session_model`
```diff
--- base::SessionDB.update_session_model
+++ fork::SessionDB.update_session_model
@@ -7,7 +7,7 @@
         (only filling in NULL), this unconditionally sets the model column
         so that the dashboard reflects the user's latest /model choice.
-        Also nulls ``system_prompt`` so stale ``Model:`` / ``Provider:``
-        footer metadata is rebuilt on the next turn. A successful /model
-        switch explicitly replaces any confirmed Browser runtime lock while
+        Retains the prior prompt until the next turn replaces it: the restore
+        path checks its runtime identity and rebuilds on a real change. A
+        successful /model switch replaces any confirmed Browser runtime lock while
         preserving unrelated lineage markers in ``model_config``.
 
@@ -42,9 +42,7 @@
             conn.execute(
                 "UPDATE sessions SET "
-                "model = ?, model_config = ?, "
-                "system_prompt = NULL, system_prompt_hash = NULL "
+                "model = ?, model_config = ? "
                 "WHERE id = ?",
                 (model, merged, session_id),
             )
-            self._delete_unreferenced_system_prompts(conn)
         self._execute_write(_do)
```

### `SessionDB.update_session_runtime_lock`
```diff
--- base::SessionDB.update_session_runtime_lock
+++ fork::SessionDB.update_session_runtime_lock
@@ -12,6 +12,6 @@
 
         Merges ``browser_model_lock`` into the existing ``model_config`` JSON so
-        ``_branched_from`` / ``_delegate_from`` survive. Nulls ``system_prompt``
-        so cached ``Model:`` / ``Provider:`` footers cannot lie after a switch.
+        ``_branched_from`` / ``_delegate_from`` survive. The prior prompt is
+        retained until restore checks the runtime identity and replaces it.
         """
         lock = {
@@ -33,10 +33,7 @@
                 """UPDATE sessions SET
                    model_config = ?,
-                   model = COALESCE(?, model),
-                   system_prompt = NULL,
-                   system_prompt_hash = NULL
+                   model = COALESCE(?, model)
                    WHERE id = ?""",
                 (merged, model, session_id),
             )
-            self._delete_unreferenced_system_prompts(conn)
         self._execute_write(_do)
```

### `SessionDB.set_session_archived`
```diff
--- base::SessionDB.set_session_archived
+++ fork::SessionDB.set_session_archived
@@ -8,41 +8,82 @@
         displayed tip lets the still-unarchived root resurrect it on refresh.
         Returns True when at least one row was updated.
+
+        Archiving is also a **routing-affecting** operation. Flipping the flag
+        alone left the gateway's ``gateway_routing`` key for that session
+        orphaned forever: every in-memory eviction path in
+        ``gateway/session.py`` gates on the row being *ended*, so a row with
+        ``end_reason IS NULL`` was unreachable by all of them and the key was
+        rewritten from the live index on every full persist. So archiving:
+
+        * retires still-live rows with ``end_reason = 'archived'`` (COALESCE,
+          so ``'compression'`` and every other explicit reason survive — the
+          lineage edges above depend on them), which is what re-enables the
+          existing startup prune and routing-time self-heal; and
+        * drops the durable routing rows that map to the archived lineage, so
+          an install with no live gateway is clean immediately rather than at
+          the next restart.
+
+        Unarchiving reverses exactly what archiving wrote (rows still carrying
+        ``end_reason = 'archived'``); a session ended for a real reason stays
+        ended, and no routing entry is resurrected — the next message rebuilds
+        it through the normal create path.
+
+        Consequence worth naming: a session archived while still live is now an
+        *ended* row, so the retention sweep (``sessions.auto_prune``, off by
+        default) can eventually reap it. Rows archived by
+        :meth:`archive_sessions` were already ended and already eligible.
         """
         def _do(conn):
-            cursor = conn.execute(
-                """
-                WITH RECURSIVE
-                  ancestors(id) AS (
-                    SELECT ?
-                    UNION
-                    SELECT parent.id
-                    FROM ancestors a
-                    JOIN sessions child ON child.id = a.id
-                    JOIN sessions parent ON parent.id = child.parent_session_id
-                    WHERE parent.end_reason = 'compression'
-                  ),
-                  descendants(id) AS (
-                    SELECT ?
-                    UNION
-                    SELECT child.id
-                    FROM descendants d
-                    JOIN sessions parent ON parent.id = d.id
-                    JOIN sessions child ON child.parent_session_id = parent.id
-                    WHERE parent.end_reason = 'compression'
-                  ),
-                  lineage(id) AS (
-                    SELECT id FROM ancestors
-                    UNION
-                    SELECT id FROM descendants
-                  )
-                UPDATE sessions
-                SET archived = ?
-                WHERE id IN (SELECT id FROM lineage)
-                """,
-                (session_id, session_id, 1 if archived else 0),
-            )
+            now = time.time()
+            if archived:
+                cursor = conn.execute(
+                    self._SESSION_LINEAGE_CTE
+                    + """
+                    UPDATE sessions
+                    SET archived = 1,
+                        ended_at = COALESCE(ended_at, ?),
+                        end_reason = COALESCE(end_reason, ?)
+                    WHERE id IN (SELECT id FROM lineage)
+                    """,
+                    (session_id, session_id, now, self.ARCHIVE_END_REASON),
+                )
+            else:
+                cursor = conn.execute(
+                    self._SESSION_LINEAGE_CTE
+                    + """
+                    UPDATE sessions
+                    SET archived = 0,
+                        ended_at = CASE WHEN end_reason = ? THEN NULL
+                                        ELSE ended_at END,
+                        end_reason = CASE WHEN end_reason = ? THEN NULL
+                                          ELSE end_reason END
+                    WHERE id IN (SELECT id FROM lineage)
+                    """,
+                    (
+                        session_id,
+                        session_id,
+                        self.ARCHIVE_END_REASON,
+                        self.ARCHIVE_END_REASON,
+                    ),
+                )
             rowcount = cursor.rowcount
             if rowcount is None or rowcount < 0:
                 rowcount = conn.execute("SELECT changes()").fetchone()[0]
+            if archived:
+                # Match on the mapped session_id, not on (scope, session_key):
+                # two profiles sharing one state.db produce the same key in
+                # different scopes for *different* sessions, and only this
+                # lineage's mapping is orphaned. json_valid guards the delete
+                # so one corrupt routing row cannot fail the archive.
+                conn.execute(
+                    self._SESSION_LINEAGE_CTE
+                    + """
+                    DELETE FROM gateway_routing
+                    WHERE json_valid(entry_json)
+                      AND json_extract(entry_json, '$.session_id')
+                          IN (SELECT id FROM lineage)
+                    """,
+                    (session_id, session_id),
+                )
             return rowcount
         rowcount = self._execute_write(_do)
```

### `SessionDB.list_sessions_rich`
```diff
--- base::SessionDB.list_sessions_rich
+++ fork::SessionDB.list_sessions_rich
@@ -14,4 +14,5 @@
         archived_only: bool = False,
         id_query: str = None,
+        _force_cte_oracle: bool = False,
         search_query: str = None,
         compact_rows: bool = False,
@@ -46,7 +47,8 @@
         the "most-recent activity" is taken from the live tip (not the root),
         so an old conversation that was compressed and continued recently
-        surfaces in the correct slot. Ordering is computed at SQL level via
-        a recursive CTE that walks compression-continuation edges, so LIMIT
-        and OFFSET still apply efficiently.
+        surfaces in the correct slot. Ordering is computed by the legacy CTE
+        unless the dormant ``dashboard.session_list_denorm`` flag is true, in
+        which case the indexed denormalized path applies LIMIT/OFFSET before
+        enrichment.
 
         ``search_query`` matches case-insensitive substrings against each
@@ -147,4 +149,24 @@
         id_needle = (id_query or "").strip().lower()
         search_needle = (search_query or "").strip().lower()
+        if (
+            order_by_last_active
+            and not _force_cte_oracle
+            and not include_children
+            and _session_list_denorm_enabled()
+            and not search_needle
+            and not compact_rows
+        ):
+            return self._list_sessions_rich_denorm(
+                source=source,
+                exclude_sources=exclude_sources,
+                cwd_prefix=cwd_prefix,
+                limit=limit,
+                offset=offset,
+                min_message_count=min_message_count,
+                project_compression_tips=project_compression_tips,
+                include_archived=include_archived,
+                archived_only=archived_only,
+                id_query=id_query,
+            )
         if order_by_last_active:
             # Compute effective_last_active by walking each surfaced session's
@@ -220,5 +242,4 @@
                       AND json_extract(COALESCE(child.model_config, '{{}}'), '$._branched_from') IS NULL
                       AND json_extract(COALESCE(child.model_config, '{{}}'), '$._delegate_from') IS NULL
-                      AND COALESCE(child.source, '') != 'tool'
                 ),
                 chain_max AS (
@@ -277,6 +298,8 @@
             s = self._session_row_dict(row)
             s["preview"] = _shape_preview(s.pop("_preview_raw", ""))
-            # Drop the internal ordering column so callers see a clean dict.
+            # Drop the internal ordering column so callers see a clean dict
+            # (both the query alias and the fork's denormalized real column).
             s.pop("_effective_last_active", None)
+            s.pop("effective_last_active", None)
             sessions.append(s)
 
```

### `SessionDB.session_count_by_source`
```diff
--- base::SessionDB.session_count_by_source
+++ fork::SessionDB.session_count_by_source
@@ -33,5 +33,5 @@
 
         with self._read_ctx() as conn:
-            if self._conn is None:
+            if conn is None:
                 raise RuntimeError("SessionDB connection is closed")
             rows = conn.execute(
```

### `SessionDB.get_session_delete_targets`
```diff
--- base::SessionDB.get_session_delete_targets
+++ fork::SessionDB.get_session_delete_targets
@@ -13,4 +13,4 @@
             if not exists:
                 return []
-            delegate_ids = _collect_delegate_child_ids(self._conn, [session_id])
+            delegate_ids = _collect_delegate_child_ids(conn, [session_id])
         return [session_id, *sorted(delegate_ids)]
```

### `SessionDB.delete_session`
```diff
--- base::SessionDB.delete_session
+++ fork::SessionDB.delete_session
@@ -39,5 +39,14 @@
                 if actual_ids != expected_ids:
                     return False
-            removed_delegate_ids.extend(_delete_delegate_children(conn, [session_id]))
+            orphaned_child_ids: List[str] = []
+            affected_root_ids: List[str] = []
+            removed_delegate_ids.extend(
+                _delete_delegate_children(conn, [session_id], orphaned_child_ids)
+            )
+            direct_orphans, direct_roots = self._collect_orphan_effective_last_active_targets(
+                conn, [session_id]
+            )
+            orphaned_child_ids.extend(direct_orphans)
+            affected_root_ids.extend(direct_roots)
             # Orphan remaining child sessions (branches, etc.) so FK is satisfied.
             conn.execute(
@@ -48,4 +57,7 @@
             conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
             conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
+            self._recompute_effective_last_active_many(
+                conn, affected_root_ids + orphaned_child_ids
+            )
             self._delete_unreferenced_system_prompts(conn)
             return True
```

### `SessionDB.delete_sessions`
```diff
--- base::SessionDB.delete_sessions
+++ fork::SessionDB.delete_sessions
@@ -53,5 +53,14 @@
 
             existing_placeholders = ",".join("?" * len(existing))
-            removed_delegate_ids.extend(_delete_delegate_children(conn, existing))
+            orphaned_child_ids: List[str] = []
+            affected_root_ids: List[str] = []
+            removed_delegate_ids.extend(
+                _delete_delegate_children(conn, existing, orphaned_child_ids)
+            )
+            direct_orphans, direct_roots = self._collect_orphan_effective_last_active_targets(
+                conn, existing
+            )
+            orphaned_child_ids.extend(direct_orphans)
+            affected_root_ids.extend(direct_roots)
             # Orphan remaining children whose parent is in the kill list so the
             # FK constraint stays satisfied. Pin children whose parent
@@ -74,4 +83,7 @@
             self._delete_unreferenced_system_prompts(conn)
             removed_ids.extend(existing)
+            self._recompute_effective_last_active_many(
+                conn, affected_root_ids + orphaned_child_ids
+            )
             return len(existing)
 
```

### `SessionDB.delete_empty_sessions`
```diff
--- base::SessionDB.delete_empty_sessions
+++ fork::SessionDB.delete_empty_sessions
@@ -40,4 +40,9 @@
 
             placeholders = ",".join("?" * len(session_ids))
+            orphaned_child_ids, affected_root_ids = (
+                self._collect_orphan_effective_last_active_targets(
+                    conn, list(session_ids)
+                )
+            )
             conn.execute(
                 f"UPDATE sessions SET parent_session_id = NULL "
@@ -57,4 +62,7 @@
                 conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
                 removed_ids.append(sid)
+            self._recompute_effective_last_active_many(
+                conn, affected_root_ids + orphaned_child_ids
+            )
             self._delete_unreferenced_system_prompts(conn)
             return len(session_ids)
```

## target: `NOWHERE (upstream deleted/renamed — find replacement)`

### `__getattr__`  (fork-only symbol)
```diff
--- base::__getattr__
+++ fork::__getattr__
@@ -0,0 +1,8 @@
+def __getattr__(name: str):
+    """Lazy module attributes. DEFAULT_DB_PATH must resolve get_hermes_home()
+    at ACCESS time, not import time — a frozen import-time constant ignores
+    HERMES_HOME redirects (test hermeticity: prod state.db was opened by
+    hermetic suites; 2026-07-24 incident)."""
+    if name == "DEFAULT_DB_PATH":
+        return get_hermes_home() / "state.db"
+    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
```

### `_production_state_roots`
```diff
--- base::_production_state_roots
+++ fork::_production_state_roots
@@ -4,8 +4,13 @@
     if real_root is not None:
         roots.append(real_root)
+    deployed_root = _deployed_hermes_home_root()
+    if deployed_root is not None and deployed_root not in roots:
+        roots.append(deployed_root)
     for extra in _STATE_DB_GUARD_EXTRA_DENY_ROOTS:
         try:
-            roots.append(Path(extra).expanduser().resolve())
+            resolved_extra = Path(extra).expanduser().resolve()
         except Exception:
             continue
+        if resolved_extra not in roots:
+            roots.append(resolved_extra)
     return roots
```

### `SessionDB.restore_rewound`
```diff
--- base::SessionDB.restore_rewound
+++ fork::SessionDB.restore_rewound
@@ -1,4 +1,7 @@
     def restore_rewound(self, session_id: str, since_message_id: int) -> int:
-        """Mark inactive messages with id >= *since_message_id* active again.
+        """DEPRECATED for stacked undo/redo — use ``restore_ids``.
+
+        Mark inactive messages with id >= *since_message_id* active again.
+        ``id >=`` range restore clobbers stacked ops of differing N.
 
         Returns the number of rows flipped back to ``active=1``.
```

## target: `hermes_state_guard.py`

### `_real_platform_state_root`
```diff
--- base::_real_platform_state_root
+++ fork::_real_platform_state_root
@@ -7,8 +7,23 @@
     through the patched callable would misidentify the test's own hermetic
     home as "production" (false positive) or, worse, miss the real one
-    (false negative).  ``os.path.expanduser`` reads the HOME environment
-    variable / passwd entry, which the hermetic conftest never rewrites.
+    (false negative).
+
+    Anchored on the OS ACCOUNT home (:func:`_os_account_home`) rather than
+    ``os.path.expanduser("~")``, which on POSIX is just ``$HOME``.  Reading
+    ``$HOME`` made this function answer "production" for the tmpdir of any
+    test using the hermetic ``monkeypatch.setenv("HOME", tmp_path)`` idiom —
+    so a hermetic board at ``<tmp>/.hermes/kanban.db`` WAS, to the guard, the
+    live board, and both guards refused it (2026-09-21: 2 files / 24 tests
+    red).  A guard that fires on the standard isolation idiom teaches people
+    to disarm it globally, which is how the previous two opt-in mitigations
+    died; keeping it precise is what keeps it armed.
+
+    The account home is still not monkeypatchable from inside a test, so the
+    property the old comment was protecting is preserved — it is simply read
+    from the passwd entry, which ``$HOME`` only aliases when nothing has
+    redirected it.
     """
     try:
+        account_home = _os_account_home()
         if sys.platform == "win32":
             base = os.environ.get("LOCALAPPDATA", "").strip()
@@ -16,8 +31,11 @@
                 Path(base) / "hermes"
                 if base
-                else Path(os.path.expanduser("~")) / "AppData" / "Local" / "hermes"
+                else (account_home or Path(os.path.expanduser("~")))
+                / "AppData"
+                / "Local"
+                / "hermes"
             )
         else:
-            root = Path(os.path.expanduser("~")) / ".hermes"
+            root = (account_home or Path(os.path.expanduser("~"))) / ".hermes"
         return root.resolve()
     except Exception:
```

## target: `hermes_state_common.py`

### `_trigram_fts_config_enabled`  (fork-only symbol)
```diff
--- base::_trigram_fts_config_enabled
+++ fork::_trigram_fts_config_enabled
@@ -0,0 +1,33 @@
+def _trigram_fts_config_enabled() -> bool:
+    """Whether the trigram FTS index is enabled by config (default True).
+
+    Reads ``session_store.trigram_fts`` from config.yaml. The trigram index
+    roughly doubles the on-disk footprint of every message (it stores a full
+    second copy of message content plus a trigram token index); on large,
+    busy installs it can dominate state.db size (observed: ~2GB of a 4.5GB
+    DB) and slow synchronous queries enough to stall the dashboard event
+    loop. Setting ``session_store.trigram_fts: false`` disables it: CJK /
+    substring message search degrades to the existing LIKE fallback (same
+    degradation as a SQLite build without the trigram tokenizer); base FTS,
+    title/session search, and message writes are unaffected.
+
+    Lazy import + fail-open so hermes_state keeps zero hard deps on the CLI
+    config layer (tests and standalone scripts open SessionDB directly).
+    """
+    try:
+        from hermes_cli.config import load_config_readonly
+
+        cfg = load_config_readonly()
+        section = cfg.get("session_store")
+        if not isinstance(section, dict):
+            return True
+        val = section.get("trigram_fts", True)
+        if isinstance(val, bool):
+            return val
+        # Tolerate integer 0/1 written in YAML or produced by env-var
+        # substitution; anything else fails open (trigram stays enabled).
+        if isinstance(val, int):
+            return bool(val)
+        return True
+    except Exception:
+        return True
```

## target: `hermes_state_repair.py`

### `_persistent_repair_exhausted_error`
```diff
--- base::_persistent_repair_exhausted_error
+++ fork::_persistent_repair_exhausted_error
@@ -6,5 +6,6 @@
         "the corruption is beyond the schema/FTS repair strategies "
         "(likely b-tree page damage). Manual recovery required: restore "
-        f"a backup, or salvage with `sqlite3 {db_path} \".recover\"`. "
+        "a backup, or salvage with "
+        f'`sqlite3 {hint_value(str(db_path))} ".recover"`. '
         f"Delete {_repair_ledger_path(db_path).name} to force another "
         "automatic attempt."
```

### `_backup_db_file`
```diff
--- base::_backup_db_file
+++ fork::_backup_db_file
@@ -114,6 +114,6 @@
                     f"copying the damaged DB needs {need / 1e9:.2f}GB and must "
                     f"leave {headroom / 1e9:.2f}GB headroom. Free disk space, "
-                    f"then retry (or recover manually with `sqlite3 {db_path} "
-                    '".recover"`).'
+                    "then retry (or recover manually with "
+                    f'`sqlite3 {hint_value(str(db_path))} ".recover"`).'
                 )
                 logger.error("Refusing forensic backup of %s: %s", db_path, reason)
@@ -129,5 +129,5 @@
                 "refusing the forensic copy rather than risk filling the "
                 f"volume. Free disk space, then retry (or recover manually "
-                f'with `sqlite3 {db_path} ".recover"`).'
+                f'with `sqlite3 {hint_value(str(db_path))} ".recover"`).'
             )
             logger.error("Refusing forensic backup of %s: %s", db_path, reason)
```

### `_db_opens_cleanly`
```diff
--- base::_db_opens_cleanly
+++ fork::_db_opens_cleanly
@@ -64,5 +64,5 @@
                 # (hermes_state.py:645-723). The supported degraded-runtime
                 # path (SessionDB._is_fts5_unavailable_error + the
-                # regression suite in tests/test_hermes_state.py:600-632)
+                # regression suite in tests/test_hermes_state_*.py)
                 # treats both "no such module: fts5" and
                 # "no such tokenizer: trigram" as the capability error.
```

## target: `hermes_state_fts.py`

### `SessionDB._is_trigram_unavailable_error`
```diff
--- base::SessionDB._is_trigram_unavailable_error
+++ fork::SessionDB._is_trigram_unavailable_error
@@ -1,13 +1,4 @@
     @staticmethod
     def _is_trigram_unavailable_error(exc: sqlite3.OperationalError) -> bool:
-        """True when only an optional tokenizer is missing (FTS5 itself works).
-
-        Covers the built-in trigram tokenizer (needs SQLite >= 3.34) and the
-        loadable cjk_unicode61 tokenizer — both mean "this one index can't be
-        served here", never "disable FTS".
-        """
         err = str(exc).lower()
-        return (
-            "no such tokenizer: trigram" in err
-            or "no such tokenizer: cjk_unicode61" in err
-        )
+        return "no such tokenizer" in err and "trigram" in err
```

### `SessionDB._drop_fts_triggers`
```diff

```

## target: `hermes_state_gateway.py`

### `SessionDB.record_gateway_session_peer`
```diff
--- base::SessionDB.record_gateway_session_peer
+++ fork::SessionDB.record_gateway_session_peer
@@ -39,4 +39,5 @@
 
         def _do(conn):
+            root_id = self._resolve_effective_last_active_root(conn, session_id)
             lineage_cte = ""
             target_clause = "WHERE id = ?"
@@ -126,4 +127,8 @@
                         ),
                     )
+            # Fork denorm gate: recompute AFTER the self-heal insert above, so
+            # a row this call just created is included in the denormalization.
+            self._recompute_effective_last_active(conn, root_id)
+            self._recompute_effective_last_active_for_session(conn, session_id)
 
         self._execute_write(_do)
```

### `SessionDB.save_gateway_routing_entry`
```diff
--- base::SessionDB.save_gateway_routing_entry
+++ fork::SessionDB.save_gateway_routing_entry
@@ -16,4 +16,5 @@
 
         def _do(conn):
+            self._assert_unique_gateway_routes(conn, {session_key: entry_json}, scope)
             conn.execute(
                 """INSERT INTO gateway_routing (scope, session_key, entry_json, updated_at)
```

### `SessionDB.replace_gateway_routing_entries`
```diff
--- base::SessionDB.replace_gateway_routing_entries
+++ fork::SessionDB.replace_gateway_routing_entries
@@ -1,4 +1,4 @@
     def replace_gateway_routing_entries(
-        self, entries: Dict[str, str], *, scope: str = ""
+        self, entries: Dict[str, str], *, scope: str = "", retired_keys=()
     ) -> None:
         """Atomically replace the routing index for *scope* with *entries*.
@@ -12,4 +12,5 @@
 
         def _do(conn):
+            self._assert_unique_gateway_routes(conn, entries, scope, retired_keys=retired_keys)
             conn.execute("DELETE FROM gateway_routing WHERE scope = ?", (scope,))
             if entries:
```

### `SessionDB.adopt_orphaned_gateway_session`
```diff
--- base::SessionDB.adopt_orphaned_gateway_session
+++ fork::SessionDB.adopt_orphaned_gateway_session
@@ -61,4 +61,8 @@
                 (time.time(), donor_id),
             )
+            # The orphan may have just become the donor's child: keep the
+            # denormalized session-list rollup coherent for both rows.
+            self._recompute_effective_last_active_for_session(conn, orphan_id)
+            self._recompute_effective_last_active_for_session(conn, donor_id)
             return True
 
```

### `SessionDB.get_handoff_state`
```diff
--- base::SessionDB.get_handoff_state
+++ fork::SessionDB.get_handoff_state
@@ -6,10 +6,11 @@
         """
         try:
-            cur = self._conn.execute(
-                "SELECT handoff_state, handoff_platform, handoff_error "
-                "FROM sessions WHERE id = ?",
-                (session_id,),
-            )
-            row = cur.fetchone()
+            with self._read_ctx() as conn:
+                cur = conn.execute(
+                    "SELECT handoff_state, handoff_platform, handoff_error "
+                    "FROM sessions WHERE id = ?",
+                    (session_id,),
+                )
+                row = cur.fetchone()
             if not row:
                 return None
```

### `SessionDB.list_pending_handoffs`
```diff
--- base::SessionDB.list_pending_handoffs
+++ fork::SessionDB.list_pending_handoffs
@@ -5,13 +5,14 @@
         """
         try:
-            cur = self._conn.execute(
-                "SELECT s.*, "
-                "COALESCE(sp.prompt, s.system_prompt) AS _system_prompt_resolved "
-                "FROM sessions s "
-                "LEFT JOIN system_prompts sp ON sp.hash = s.system_prompt_hash "
-                "WHERE s.handoff_state = 'pending' "
-                "ORDER BY s.started_at ASC"
-            )
-            return [self._session_row_dict(r) for r in cur.fetchall()]
+            with self._read_ctx() as conn:
+                cur = conn.execute(
+                    "SELECT s.*, "
+                    "COALESCE(sp.prompt, s.system_prompt) AS _system_prompt_resolved "
+                    "FROM sessions s "
+                    "LEFT JOIN system_prompts sp ON sp.hash = s.system_prompt_hash "
+                    "WHERE s.handoff_state = 'pending' "
+                    "ORDER BY s.started_at ASC"
+                )
+                return [self._session_row_dict(r) for r in cur.fetchall()]
         except Exception:
             return []
```

## target: `hermes_state_compression.py`

### `SessionDB.publish_compression_child`
```diff
--- base::SessionDB.publish_compression_child
+++ fork::SessionDB.publish_compression_child
@@ -122,15 +122,6 @@
                 if tail_rows:
                     tail_ids = [int(r["id"]) for r in tail_rows]
-                    placeholders = ",".join("?" for _ in tail_ids)
-                    clone_cols = [
-                        c for c in self._message_column_names(conn)
-                        if c not in ("id", "session_id", "active", "compacted")
-                    ]
-                    col_list = ", ".join(clone_cols)
-                    conn.execute(
-                        f"INSERT INTO messages ({col_list}, session_id, active, compacted) "
-                        f"SELECT {col_list}, ?, 1, 0 FROM messages "
-                        f"WHERE id IN ({placeholders}) ORDER BY id",
-                        [child_session_id, *tail_ids],
+                    self._clone_message_tail_rows(
+                        conn, tail_ids, child_session_id, retarget=True
                     )
                     total_messages += len(tail_ids)
```

### `SessionDB.acquire_session_turn_lease`
```diff
--- base::SessionDB.acquire_session_turn_lease
+++ fork::SessionDB.acquire_session_turn_lease
@@ -9,4 +9,6 @@
         on_wait=None,
         wait_notice_interval_seconds: float = 15.0,
+        wait_notice_backoff: float = 2.0,
+        wait_notice_max_interval_seconds: float = 300.0,
         should_abort=None,
         acquire_patience_s: float = 0.5,
@@ -15,7 +17,13 @@
 
         ``on_wait(elapsed_seconds)`` is best-effort: invoked when the first
-        attempt fails (elapsed ~0) and again about every
-        ``wait_notice_interval_seconds`` while still waiting, so UIs can show
-        that another process holds the conversation.
+        attempt fails (elapsed ~0), again after ``wait_notice_interval_seconds``,
+        and then at geometrically growing gaps (``wait_notice_backoff`` x,
+        capped at ``wait_notice_max_interval_seconds``) while still waiting, so
+        UIs can show that another process holds the conversation WITHOUT
+        flooding the chat. Messaging surfaces post every notice as a fresh
+        message; a fixed 15s cadence produced 24 "Still waiting" posts in one
+        6-minute wait (2026-09-21, #apollo). With the defaults a 10-minute
+        wait now emits ~6 notices (0s, 15s, 45s, 105s, 225s, 465s).
+        ``wait_notice_backoff <= 1`` restores the fixed cadence.
 
         When ``should_abort()`` returns True (for example the agent received
@@ -27,4 +35,10 @@
         last_notice_at = None
         notice_every = max(0.0, float(wait_notice_interval_seconds))
+        notice_backoff = max(1.0, float(wait_notice_backoff or 1.0))
+        # The cap is a real ceiling: a caller passing a cap below the base
+        # interval gets the cap, not a silently widened interval.
+        notice_cap = max(0.0, float(wait_notice_max_interval_seconds))
+        if notice_cap > 0.0:
+            notice_every = min(notice_every, notice_cap)
         while True:
             if should_abort is not None:
@@ -69,4 +83,9 @@
                         exc_info=True,
                     )
+                # Grow the gap only after a notice that followed a full
+                # interval (not the immediate first-failure notice), so the
+                # cadence is 0, +I, +I*b, +I*b^2 ... capped.
+                if last_notice_at is not None and notice_every > 0.0:
+                    notice_every = min(notice_cap, notice_every * notice_backoff)
                 last_notice_at = now
             time.sleep(min(max(0.01, float(poll_interval_seconds)), remaining))
```

### `SessionDB.get_compression_lock_holder`
```diff
--- base::SessionDB.get_compression_lock_holder
+++ fork::SessionDB.get_compression_lock_holder
@@ -7,9 +7,10 @@
             return None
         now = time.time()
-        row = self._conn.execute(
-            "SELECT holder FROM compression_locks "
-            "WHERE session_id = ? AND expires_at >= ?",
-            (session_id, now),
-        ).fetchone()
+        with self._read_ctx() as conn:
+            row = conn.execute(
+                "SELECT holder FROM compression_locks "
+                "WHERE session_id = ? AND expires_at >= ?",
+                (session_id, now),
+            ).fetchone()
         if row is None:
             return None
```

## target: `hermes_state_messages.py`

### `SessionDB.set_message_api_content`  (fork-only symbol)
```diff
--- base::SessionDB.set_message_api_content
+++ fork::SessionDB.set_message_api_content
@@ -0,0 +1,22 @@
+    def set_message_api_content(
+        self, session_id: str, row_id: int, api_content: str
+    ) -> int:
+        """Stamp the ``api_content`` sidecar onto an already-persisted row.
+
+        The incremental flush writes each live message once. A mid-turn /steer
+        (or the run-budget notice) is appended to the current turn's tool
+        result AFTER the sequential executor flushed it, so the row held only
+        the bare tool output: the steer vanished from reloaded history and the
+        next turn's prompt-cache prefix broke (t_a17e2305). ``content`` stays
+        the clean tool output (rendered content is append-only); the sidecar
+        carries the bytes that were sent, and replay substitutes it. Returns
+        the number of rows updated (0 or 1).
+        """
+        def _do(conn):
+            cursor = conn.execute(
+                "UPDATE messages SET api_content = ? WHERE id = ? AND session_id = ?",
+                (_scrub_surrogates(api_content), row_id, session_id),
+            )
+            return cursor.rowcount
+
+        return self._execute_write(_do)
```

### `SessionDB.append_message`
```diff
--- base::SessionDB.append_message
+++ fork::SessionDB.append_message
@@ -134,4 +134,7 @@
                     (session_id,),
                 )
+            self._bump_effective_last_active_for_message(
+                conn, session_id, message_timestamp
+            )
             return msg_id
 
```

### `SessionDB.append_messages_batch`
```diff
--- base::SessionDB.append_messages_batch
+++ fork::SessionDB.append_messages_batch
@@ -7,4 +7,5 @@
         chunk_rows: Optional[int] = None,
         turn_lease_ttl_seconds: float = 300.0,
+        row_ids_out: Optional[List[int]] = None,
     ) -> int:
         """Append multiple messages atomically in ONE write transaction.
@@ -20,4 +21,8 @@
         rows, typically 3-8 messages) as one BEGIN IMMEDIATE / commit pair
         instead of one transaction (and, off WAL, one fsync) per row.
+
+        ``row_ids_out``: optional list that receives the committed row id of
+        each inserted message, in ``messages`` order. Only populated on a
+        successful commit.
 
         Atomicity contract: all rows land or none do (the caller re-flushes
@@ -47,4 +52,5 @@
                     turn_lease_holder=turn_lease_holder,
                     turn_lease_ttl_seconds=turn_lease_ttl_seconds,
+                    row_ids_out=row_ids_out,
                 )
             return inserted_total
@@ -69,7 +75,12 @@
             inserted = 0
             tool_calls_total = 0
+            # Collect locally and extend the caller's list only after the
+            # transaction commits — a retried/rolled-back attempt must not
+            # leave phantom ids behind.
+            _local_ids: List[int] = []
             if inserted_rows:
                 inserted, tool_calls_total = self._insert_message_rows(
-                    conn, session_id, inserted_rows
+                    conn, session_id, inserted_rows,
+                    row_ids_out=_local_ids if row_ids_out is not None else None,
                 )
 
@@ -86,4 +97,25 @@
                     (inserted, session_id),
                 )
+            if row_ids_out is not None:
+                # Positional contract (fork): row_ids_out must align 1:1 with
+                # ``messages``, because run_agent's flush indexes it by the
+                # message's own position (_batch_row_ids[_idx]). Upstream's
+                # resolve_and_repair_transcript_batch PARTITIONS the batch —
+                # a message repaired in place is not re-inserted and gets no
+                # id from _insert_message_rows — so a bare extend(_local_ids)
+                # would shift every id after the first repaired message onto
+                # the wrong message. Rebuild in ``messages`` order: repaired
+                # rows contribute their (stamped) ``_row_id``; inserted rows
+                # consume _local_ids in order.
+                _inserted_ids = iter(_local_ids)
+                _aligned: List[Any] = []
+                _inserted_set = {id(m) for m in inserted_rows}
+                for _msg in messages:
+                    if id(_msg) in _inserted_set:
+                        _aligned.append(next(_inserted_ids, None))
+                    else:
+                        _rid = _msg.get("_row_id") if isinstance(_msg, dict) else None
+                        _aligned.append(_rid if isinstance(_rid, int) else None)
+                row_ids_out.extend(_aligned)
             return inserted
 
```

### `SessionDB._insert_message_rows`
```diff
--- base::SessionDB._insert_message_rows
+++ fork::SessionDB._insert_message_rows
@@ -1,3 +1,9 @@
-    def _insert_message_rows(self, conn, session_id: str, messages: List[Dict[str, Any]]) -> tuple[int, int]:
+    def _insert_message_rows(
+        self,
+        conn,
+        session_id: str,
+        messages: List[Dict[str, Any]],
+        row_ids_out: Optional[List[int]] = None,
+    ) -> tuple[int, int]:
         """Insert *messages* as fresh active rows for *session_id*.
 
@@ -7,4 +13,11 @@
         ``(inserted_count, tool_call_count)``. Does NOT touch sessions.* counters
         — the caller owns that, since the two flows reconcile counts differently.
+
+        ``row_ids_out``: when supplied, each inserted row's ``lastrowid`` is
+        appended in message order. The turn-flush caller needs those ids to
+        (a) re-persist an in-place ``interrupt_close`` finish_reason and (b)
+        stamp the desktop's optimistic rows with their committed ids — both
+        fork features that the per-row ``append_message`` return value used to
+        supply before this batch path existed.
         """
         now_ts = time.time()
@@ -86,4 +99,9 @@
                 msg["_row_id"] = cur.lastrowid
             inserted += 1
+            if row_ids_out is not None:
+                row_ids_out.append(cur.lastrowid)
+            self._bump_effective_last_active_for_message(
+                conn, session_id, message_timestamp
+            )
             if tool_calls is not None:
                 tool_calls_total += (
```

### `SessionDB.replace_messages`
```diff
--- base::SessionDB.replace_messages
+++ fork::SessionDB.replace_messages
@@ -96,4 +96,5 @@
                 (total_messages, total_tool_calls, session_id),
             )
+            self._recompute_effective_last_active_for_session(conn, session_id)
 
         self._execute_write(_do)
```

### `SessionDB.archive_and_compact`
```diff
--- base::SessionDB.archive_and_compact
+++ fork::SessionDB.archive_and_compact
@@ -106,4 +106,10 @@
             # loads (active=1 only) still exclude them. Tail originals are
             # archived too — their clones (below) carry the live copy.
+            # One-result-per-tool-call invariant (t_aace5343): remember which
+            # keys were already duplicated in the live set so only a duplicate
+            # this compaction INTRODUCES fails the commit.
+            preexisting_dup_tool_results = self._active_duplicate_tool_result_ids(
+                conn, session_id
+            )
             conn.execute(
                 "UPDATE messages SET active = 0, compacted = 1 "
@@ -119,18 +125,19 @@
                 # a pure-SQL column clone: no decode/re-encode round trip, no
                 # field drift — new id, active=1, compacted=0, all else exact.
-                placeholders = ",".join("?" for _ in tail_ids)
-                clone_cols = [
-                    c for c in self._message_column_names(conn)
-                    if c not in ("id", "active", "compacted")
-                ]
-                col_list = ", ".join(clone_cols)
-                conn.execute(
-                    f"INSERT INTO messages ({col_list}, active, compacted) "
-                    f"SELECT {col_list}, 1, 0 FROM messages "
-                    f"WHERE id IN ({placeholders}) ORDER BY id",
-                    tail_ids,
-                )
+                self._clone_message_tail_rows(conn, tail_ids, session_id)
                 inserted += len(tail_ids)
                 tool_calls_total += tail_tool_calls
+
+            introduced = {
+                tc_id
+                for tc_id, n in self._active_duplicate_tool_result_ids(conn, session_id).items()
+                if n > preexisting_dup_tool_results.get(tc_id, 1)
+            }
+            if introduced:
+                raise TranscriptInvariantError(
+                    f"archive_and_compact({session_id!r}) would publish "
+                    f"{len(introduced)} tool_call_id(s) with more than one active "
+                    f"result row (e.g. {sorted(introduced)[:3]}); rolled back"
+                )
 
             # message_count / tool_call_count reflect the LIVE (active) set —
@@ -147,4 +154,5 @@
                     (inserted, tool_calls_total, patched_model_config, session_id),
                 )
+            self._recompute_effective_last_active_for_session(conn, session_id)
             return inserted
 
```

### `SessionDB.get_messages`
```diff
--- base::SessionDB.get_messages
+++ fork::SessionDB.get_messages
@@ -6,4 +6,5 @@
         limit: Optional[int] = None,
         offset: int = 0,
+        preserve_unparseable_tool_calls: bool = False,
         latest: bool = False,
         after_id: Optional[int] = None,
@@ -142,5 +143,14 @@
                 except (json.JSONDecodeError, TypeError):
                     logger.warning("Failed to deserialize tool_calls in get_messages, falling back to []")
-                    msg["tool_calls"] = []
+                    # The auto-resume mutation gate must distinguish "no tool
+                    # calls" from corrupt/ambiguous tool-call state. Preserve a
+                    # non-list sentinel for that safety-sensitive caller so its
+                    # conservative classifier fails closed; retain the historic
+                    # [] fallback for every existing caller.
+                    msg["tool_calls"] = (
+                        {"unparseable": True}
+                        if preserve_unparseable_tool_calls
+                        else []
+                    )
             if msg.get("display_metadata") is not None:
                 msg["display_metadata"] = self._decode_display_metadata(msg["display_metadata"])
```

### `SessionDB.get_messages_as_conversation`
```diff
--- base::SessionDB.get_messages_as_conversation
+++ fork::SessionDB.get_messages_as_conversation
@@ -4,4 +4,5 @@
         include_ancestors: bool = False,
         include_inactive: bool = False,
+        include_timestamp: bool = False,
         repair_alternation: bool = False,
         include_row_ids: bool = False,
@@ -52,4 +53,5 @@
             include_ancestors=include_ancestors,
             repair_alternation=repair_alternation,
+            include_timestamp=include_timestamp,
             include_row_ids=include_row_ids,
         )
```

### `SessionDB._rows_to_conversation`
```diff
--- base::SessionDB._rows_to_conversation
+++ fork::SessionDB._rows_to_conversation
@@ -6,4 +6,5 @@
         include_ancestors: bool,
         repair_alternation: bool,
+        include_timestamp: bool = False,
         include_row_ids: bool = False,
         include_summary_markers: bool = False,
@@ -61,12 +62,22 @@
                 if decoded is not None:
                     msg["display_metadata"] = decoded
+            # Upstream's opt-in compressed-summary marker. NOTE: upstream's
+            # side of this hunk also carried the unconditional
+            # ``msg["timestamp"] = row["timestamp"]`` pair from the merge
+            # base; that is deliberately NOT restored — the fork moved the
+            # timestamp surface behind the ``include_timestamp`` gate below,
+            # so re-adding it here would leak a timestamp into every
+            # consumer's byte-stable legacy message shape.
             if include_summary_markers and row["_compressed_summary"]:
                 msg["_compressed_summary"] = True
-            if row["timestamp"]:
-                msg["timestamp"] = row["timestamp"]
             if row["tool_call_id"]:
                 msg["tool_call_id"] = row["tool_call_id"]
             if row["tool_name"]:
                 msg["tool_name"] = row["tool_name"]
+                # The live tool message carries ``name`` too (Gemini requires
+                # it; chat transports send it). Restore it so a reloaded
+                # history replays the bytes the previous turn sent (t_a17e2305).
+                if row["role"] == "tool":
+                    msg["name"] = row["tool_name"]
             if row["effect_disposition"]:
                 msg["effect_disposition"] = row["effect_disposition"]
@@ -86,4 +97,11 @@
             if row["observed"]:
                 msg["observed"] = True
+            # Surface the durable per-message arrival timestamp ONLY when the
+            # caller opts in (LCM ingest/replay path). Off by default so every
+            # other consumer (CLI/TUI/ACP/gateway display) gets the byte-stable
+            # legacy shape — and the key can never reach a model payload or a
+            # prompt-cache key. Metadata only; never sent to the model.
+            if include_timestamp and row["timestamp"] is not None:
+                msg["timestamp"] = row["timestamp"]
             # Restore reasoning fields on assistant messages so providers
             # that replay reasoning (OpenRouter, OpenAI, Nous) receive
@@ -118,6 +136,11 @@
                     self._canonical_replayed_user_content(msg)
                 )
+                # Key off the ROW's timestamp, not msg.get("timestamp"): the
+                # fork gates the message-shape timestamp behind
+                # ``include_timestamp`` (off for display/resume), so reading
+                # it from msg would null the key and silently disable the
+                # exact-clone dedupe for rotated tail rows.
                 exact_clone_key = self._exact_replayed_user_clone_key(
-                    msg.get("timestamp"), canonical_content
+                    row["timestamp"], canonical_content
                 )
                 previous_exact = (
```

### `SessionDB.rewind_to_message`
```diff
--- base::SessionDB.rewind_to_message
+++ fork::SessionDB.rewind_to_message
@@ -3,4 +3,5 @@
         session_id: str,
         target_message_id: int,
+        require_user_role: bool = True,
         *,
         preserve_compaction_handoff: bool = False,
@@ -21,9 +22,11 @@
                 "rewound_count": int,    # number of rows newly flipped to active=0
                 "target_message": dict,  # full row dict of the target
-                "new_head_id":   int|None  # id of the last still-active row, or None
+                "new_head_id":   int|None, # id of the last still-active row, or None
+                "rewound_ids":   list[int] # rows newly flipped active=1 -> active=0
             }
 
         Raises ``ValueError`` if the target message does not exist in
-        *session_id* or if its role is not ``"user"``.  With
+        *session_id* or, when ``require_user_role`` is true, if its role is
+        not ``"user"``.  With
         ``preserve_compaction_handoff=True``, a composite summary carrier is
         split inside the same write transaction: its original row is archived
@@ -79,5 +82,5 @@
                 )
             target_row = dict(row)
-            if target_row.get("role") != "user":
+            if require_user_role and target_row.get("role") != "user":
                 raise ValueError(
                     f"rewind target must be a 'user' message (got role="
@@ -118,4 +121,14 @@
                 replacement = handoff if preserve_compaction_handoff else None
 
+            # Fork guard (orphaned-tool-row protection). Upstream moved the
+            # whole rewind inside this write transaction, so the guard has to
+            # move with it — and it must reuse ``conn``: ``_execute_write``
+            # already holds ``self._lock`` (a plain, non-reentrant Lock), so
+            # the guard's own ``with self._lock`` would self-deadlock here.
+            # Runs before any mutation, same as the fork's pre-txn placement.
+            self._raise_if_rewind_would_orphan_tool(
+                session_id, target_message_id, conn=conn
+            )
+
             cursor = conn.execute(
                 "SELECT id FROM messages "
@@ -139,4 +152,11 @@
                 (session_id,),
             )
+            # Head id + counters are recomputed INSIDE the transaction
+            # (upstream). This also retires the fork's post-commit fail-soft
+            # head read: that guard existed because the old read ran AFTER the
+            # write committed and a failure there would misreport a durable
+            # rewind as "nothing changed". In-transaction there is no such
+            # split — a failure rolls the whole rewind back, so the caller's
+            # error is truthful.
             message_count, tool_call_count = self._active_transcript_counts(
                 conn, session_id
@@ -168,4 +188,5 @@
             "target_message": target_row,
             "new_head_id": new_head_id,
+            "rewound_ids": rewound,
         }
         if preserve_compaction_handoff:
```

### `SessionDB.clear_messages`
```diff
--- base::SessionDB.clear_messages
+++ fork::SessionDB.clear_messages
@@ -9,3 +9,4 @@
                 (session_id,),
             )
+            self._recompute_effective_last_active_for_session(conn, session_id)
         self._execute_write(_do)
```

## target: `hermes_state_usage.py`

### `SessionDB.update_session_billing_route`
```diff
--- base::SessionDB.update_session_billing_route
+++ fork::SessionDB.update_session_billing_route
@@ -13,7 +13,6 @@
         that the dashboard reflects the user's latest /model switch.
 
-        Also nulls ``system_prompt`` so the cached snapshot (which embeds a
-        stale ``Model:`` / ``Provider:`` header) is rebuilt — matching the
-        behavior of ``update_session_model`` (see #48173, #48248).
+        Retains the cached snapshot until restore compares its runtime identity;
+        a billing-only route update does not change the prompt bytes.
         """
         # Barrier against queued token deltas — see update_session_model.
@@ -25,10 +24,7 @@
                    billing_provider = ?,
                    billing_base_url = ?,
-                   billing_mode = COALESCE(?, billing_mode),
-                   system_prompt = NULL,
-                   system_prompt_hash = NULL
+                   billing_mode = COALESCE(?, billing_mode)
                    WHERE id = ?""",
                 (provider, base_url, billing_mode, session_id),
             )
-            self._delete_unreferenced_system_prompts(conn)
         self._execute_write(_do)
```

### `SessionDB._coalesce_token_deltas`
```diff
--- base::SessionDB._coalesce_token_deltas
+++ fork::SessionDB._coalesce_token_deltas
@@ -25,4 +25,12 @@
                         # None so COALESCE keeps the stored value untouched.
                         merged[f] = (merged.get(f) or 0.0) + value
+                for f in self._TOKEN_DELTA_SNAPSHOT_FIELDS:
+                    value = kwargs.get(f)
+                    if value is not None:
+                        # Last-non-None-wins (NOT a sum): these mirror the
+                        # COALESCE(?, existing) write, so a merged run must
+                        # end up with the newest turn's snapshot exactly as
+                        # applying the deltas one-by-one would.
+                        merged[f] = value
             else:
                 groups.append((key, session_id, dict(kwargs)))
```

### `SessionDB.update_token_counts`
```diff
--- base::SessionDB.update_token_counts
+++ fork::SessionDB.update_token_counts
@@ -18,4 +18,19 @@
         api_call_count: int = 0,
         absolute: bool = False,
+        last_turn_input_tokens: Optional[int] = None,
+        last_turn_output_tokens: Optional[int] = None,
+        last_turn_cache_read_tokens: Optional[int] = None,
+        last_turn_cache_write_tokens: Optional[int] = None,
+        last_turn_reasoning_tokens: Optional[int] = None,
+        input_tokens_unknown: bool = False,
+        output_tokens_unknown: bool = False,
+        cache_read_tokens_unknown: bool = False,
+        cache_write_tokens_unknown: bool = False,
+        usage_unknown: bool = False,
+        last_turn_input_tokens_unknown: Optional[bool] = None,
+        last_turn_output_tokens_unknown: Optional[bool] = None,
+        last_turn_cache_read_tokens_unknown: Optional[bool] = None,
+        last_turn_cache_write_tokens_unknown: Optional[bool] = None,
+        last_turn_usage_unknown: Optional[bool] = None,
     ) -> None:
         """Update token counters and backfill model if not already set.
@@ -40,4 +55,19 @@
                    cache_write_tokens = ?,
                    reasoning_tokens = ?,
+                   input_tokens_unknown = MAX(COALESCE(input_tokens_unknown, 0), ?),
+                   output_tokens_unknown = MAX(COALESCE(output_tokens_unknown, 0), ?),
+                   cache_read_tokens_unknown = MAX(COALESCE(cache_read_tokens_unknown, 0), ?),
+                   cache_write_tokens_unknown = MAX(COALESCE(cache_write_tokens_unknown, 0), ?),
+                   usage_unknown = MAX(COALESCE(usage_unknown, 0), ?),
+                   last_turn_input_tokens_unknown = COALESCE(?, last_turn_input_tokens_unknown),
+                   last_turn_output_tokens_unknown = COALESCE(?, last_turn_output_tokens_unknown),
+                   last_turn_cache_read_tokens_unknown = COALESCE(?, last_turn_cache_read_tokens_unknown),
+                   last_turn_cache_write_tokens_unknown = COALESCE(?, last_turn_cache_write_tokens_unknown),
+                   last_turn_usage_unknown = COALESCE(?, last_turn_usage_unknown),
+                   last_turn_input_tokens = COALESCE(?, last_turn_input_tokens),
+                   last_turn_output_tokens = COALESCE(?, last_turn_output_tokens),
+                   last_turn_cache_read_tokens = COALESCE(?, last_turn_cache_read_tokens),
+                   last_turn_cache_write_tokens = COALESCE(?, last_turn_cache_write_tokens),
+                   last_turn_reasoning_tokens = COALESCE(?, last_turn_reasoning_tokens),
                    estimated_cost_usd = COALESCE(?, 0),
                    actual_cost_usd = CASE
@@ -45,5 +75,15 @@
                        ELSE ?
                    END,
-                   cost_status = COALESCE(?, cost_status),
+                   cost_status = CASE
+                       WHEN ? IS NULL THEN cost_status
+                       WHEN ? IN ('unknown', 'partial') THEN (
+                           CASE WHEN COALESCE(?, 0) > 0
+                                     OR CASE WHEN ? IS NULL
+                                             THEN COALESCE(actual_cost_usd, 0)
+                                             ELSE ? END > 0
+                                THEN 'partial' ELSE 'unknown' END
+                       )
+                       ELSE ?
+                   END,
                    cost_source = COALESCE(?, cost_source),
                    pricing_version = COALESCE(?, pricing_version),
@@ -61,4 +101,19 @@
                    cache_write_tokens = cache_write_tokens + ?,
                    reasoning_tokens = reasoning_tokens + ?,
+                   input_tokens_unknown = MAX(COALESCE(input_tokens_unknown, 0), ?),
+                   output_tokens_unknown = MAX(COALESCE(output_tokens_unknown, 0), ?),
+                   cache_read_tokens_unknown = MAX(COALESCE(cache_read_tokens_unknown, 0), ?),
+                   cache_write_tokens_unknown = MAX(COALESCE(cache_write_tokens_unknown, 0), ?),
+                   usage_unknown = MAX(COALESCE(usage_unknown, 0), ?),
+                   last_turn_input_tokens_unknown = COALESCE(?, last_turn_input_tokens_unknown),
+                   last_turn_output_tokens_unknown = COALESCE(?, last_turn_output_tokens_unknown),
+                   last_turn_cache_read_tokens_unknown = COALESCE(?, last_turn_cache_read_tokens_unknown),
+                   last_turn_cache_write_tokens_unknown = COALESCE(?, last_turn_cache_write_tokens_unknown),
+                   last_turn_usage_unknown = COALESCE(?, last_turn_usage_unknown),
+                   last_turn_input_tokens = COALESCE(?, last_turn_input_tokens),
+                   last_turn_output_tokens = COALESCE(?, last_turn_output_tokens),
+                   last_turn_cache_read_tokens = COALESCE(?, last_turn_cache_read_tokens),
+                   last_turn_cache_write_tokens = COALESCE(?, last_turn_cache_write_tokens),
+                   last_turn_reasoning_tokens = COALESCE(?, last_turn_reasoning_tokens),
                    estimated_cost_usd = COALESCE(estimated_cost_usd, 0) + COALESCE(?, 0),
                    actual_cost_usd = CASE
@@ -66,5 +121,15 @@
                        ELSE COALESCE(actual_cost_usd, 0) + ?
                    END,
-                   cost_status = COALESCE(?, cost_status),
+                   cost_status = CASE
+                       WHEN ? IS NULL THEN cost_status
+                       WHEN ? IN ('unknown', 'partial') THEN (
+                           CASE WHEN COALESCE(estimated_cost_usd, 0) + COALESCE(?, 0) > 0
+                                     OR CASE WHEN ? IS NULL
+                                             THEN COALESCE(actual_cost_usd, 0)
+                                             ELSE COALESCE(actual_cost_usd, 0) + ? END > 0
+                                THEN 'partial' ELSE 'unknown' END
+                       )
+                       ELSE ?
+                   END,
                    cost_source = COALESCE(?, cost_source),
                    pricing_version = COALESCE(?, pricing_version),
@@ -80,4 +145,8 @@
             or estimated_cost_usd or actual_cost_usd
         )
+        def _flag(value) -> Optional[int]:
+            """Bool → the 0/1 the INTEGER columns store; None stays None."""
+            return None if value is None else (1 if value else 0)
+
         params = (
             input_tokens,
@@ -86,4 +155,40 @@
             cache_write_tokens,
             reasoning_tokens,
+            _flag(bool(input_tokens_unknown)),
+            _flag(bool(output_tokens_unknown)),
+            _flag(bool(cache_read_tokens_unknown)),
+            _flag(bool(cache_write_tokens_unknown)),
+            _flag(bool(usage_unknown)),
+            _flag(last_turn_input_tokens_unknown),
+            _flag(last_turn_output_tokens_unknown),
+            _flag(last_turn_cache_read_tokens_unknown),
+            _flag(last_turn_cache_write_tokens_unknown),
+            _flag(last_turn_usage_unknown),
+            last_turn_input_tokens,
+            last_turn_output_tokens,
+            last_turn_cache_read_tokens,
+            last_turn_cache_write_tokens,
+            last_turn_reasoning_tokens,
+            estimated_cost_usd,
+            actual_cost_usd,
+            actual_cost_usd,
+            # cost_status CASE: (is it NULL?), (is it incomplete?), the
+            # estimated dollars this write contributes, (is actual NULL?), the
+            # actual dollars this write contributes, and the value to store
+            # otherwise.
+            #
+            # Both incomplete labels are judged, and both are judged against the
+            # POST-update estimated AND actual (r6 round-4 finding 10). The old
+            # shape asked only `= 'unknown'` against incoming estimated + OLD
+            # actual, so: a NULL-keep write relabelled a row holding retained
+            # catalog spend as 'unknown' (stranding it — 'unknown' is outside
+            # the reprice allowlist); a first actual of $5 arriving with
+            # 'unknown' stayed 'unknown' because this statement's own actual was
+            # invisible to the test; and an incoming 'partial' fell to the ELSE
+            # arm and was stored verbatim even after a $0 replace left the row
+            # with no dollars to be partial about. Same rule the model-usage
+            # upsert below already applies.
+            cost_status,
+            cost_status,
             estimated_cost_usd,
             actual_cost_usd,
@@ -165,4 +270,9 @@
                     cost_source=cost_source,
                     api_call_count=api_call_count,
+                    input_tokens_unknown=bool(input_tokens_unknown),
+                    output_tokens_unknown=bool(output_tokens_unknown),
+                    cache_read_tokens_unknown=bool(cache_read_tokens_unknown),
+                    cache_write_tokens_unknown=bool(cache_write_tokens_unknown),
+                    usage_unknown=bool(usage_unknown),
                 )
         self._execute_write(_do)
```

### `SessionDB._record_model_usage`
```diff
--- base::SessionDB._record_model_usage
+++ fork::SessionDB._record_model_usage
@@ -19,4 +19,9 @@
         api_call_count: int,
         task: str = "",
+        input_tokens_unknown: bool = False,
+        output_tokens_unknown: bool = False,
+        cache_read_tokens_unknown: bool = False,
+        cache_write_tokens_unknown: bool = False,
+        usage_unknown: bool = False,
     ) -> None:
         """Accumulate a per-API-call usage delta into session_model_usage.
@@ -63,7 +68,10 @@
                    task, api_call_count, input_tokens, output_tokens,
                    cache_read_tokens, cache_write_tokens, reasoning_tokens,
+                   input_tokens_unknown, output_tokens_unknown,
+                   cache_read_tokens_unknown, cache_write_tokens_unknown,
+                   usage_unknown,
                    estimated_cost_usd, actual_cost_usd, cost_status, cost_source,
                    first_seen, last_seen
-               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
+               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, model, billing_provider, billing_base_url, billing_mode, task)
                DO UPDATE SET
@@ -74,7 +82,28 @@
                    cache_write_tokens = cache_write_tokens + excluded.cache_write_tokens,
                    reasoning_tokens = reasoning_tokens + excluded.reasoning_tokens,
+                   input_tokens_unknown = MAX(input_tokens_unknown, excluded.input_tokens_unknown),
+                   output_tokens_unknown = MAX(output_tokens_unknown, excluded.output_tokens_unknown),
+                   cache_read_tokens_unknown = MAX(cache_read_tokens_unknown, excluded.cache_read_tokens_unknown),
+                   cache_write_tokens_unknown = MAX(cache_write_tokens_unknown, excluded.cache_write_tokens_unknown),
+                   usage_unknown = MAX(usage_unknown, excluded.usage_unknown),
                    estimated_cost_usd = estimated_cost_usd + excluded.estimated_cost_usd,
                    actual_cost_usd = actual_cost_usd + excluded.actual_cost_usd,
-                   cost_status = COALESCE(excluded.cost_status, cost_status),
+                   -- Same rule as the sessions row, but judged against THIS
+                   -- (model, provider, mode, task) row's own dollars. The
+                   -- incoming status is derived session-wide, so a wholly
+                   -- unpriced model must not inherit another model's spend and
+                   -- render "partial" on the Spend-by-model breakdown
+                   -- (r6 finding 1); conversely a row that does hold priced
+                   -- dollars must not be relabelled 'unknown' and stranded
+                   -- outside the reprice allowlist.
+                   cost_status = CASE
+                       WHEN excluded.cost_status IS NULL THEN cost_status
+                       WHEN excluded.cost_status IN ('unknown', 'partial') THEN (
+                           CASE WHEN estimated_cost_usd + excluded.estimated_cost_usd > 0
+                                     OR actual_cost_usd + excluded.actual_cost_usd > 0
+                                THEN 'partial' ELSE 'unknown' END
+                       )
+                       ELSE excluded.cost_status
+                   END,
                    cost_source = COALESCE(excluded.cost_source, cost_source),
                    last_seen = excluded.last_seen""",
@@ -92,7 +121,28 @@
                 cache_write_tokens or 0,
                 reasoning_tokens or 0,
+                # Absorbing per bucket: MAX over the accumulated flag and this
+                # delta's, so one unmeasured call latches the bucket. UNKNOWN
+                # != 0 — the int columns above keep summing regardless.
+                1 if input_tokens_unknown else 0,
+                1 if output_tokens_unknown else 0,
+                1 if cache_read_tokens_unknown else 0,
+                1 if cache_write_tokens_unknown else 0,
+                1 if usage_unknown else 0,
                 float(estimated_cost_usd or 0.0),
                 float(actual_cost_usd or 0.0),
-                cost_status,
+                # A row's FIRST write cannot be "partial": there is no prior
+                # spend on this (model, provider, mode, task) to be partial
+                # about. The incoming status is session-scoped, so scope it to
+                # this row's own dollars (r6 finding 1).
+                (
+                    (
+                        "partial"
+                        if (float(estimated_cost_usd or 0.0) > 0
+                            or float(actual_cost_usd or 0.0) > 0)
+                        else "unknown"
+                    )
+                    if cost_status in ("unknown", "partial")
+                    else cost_status
+                ),
                 cost_source,
                 now,
```

### `SessionDB.record_auxiliary_usage`
```diff
--- base::SessionDB.record_auxiliary_usage
+++ fork::SessionDB.record_auxiliary_usage
@@ -14,4 +14,9 @@
         estimated_cost_usd: Optional[float] = None,
         api_call_count: int = 1,
+        input_tokens_unknown: bool = False,
+        output_tokens_unknown: bool = False,
+        cache_read_tokens_unknown: bool = False,
+        cache_write_tokens_unknown: bool = False,
+        usage_unknown: bool = False,
     ) -> None:
         """Record an auxiliary LLM call's usage against *session_id* (issue #23270).
@@ -62,4 +67,9 @@
                 ),
                 task=task,
+                input_tokens_unknown=bool(input_tokens_unknown),
+                output_tokens_unknown=bool(output_tokens_unknown),
+                cache_read_tokens_unknown=bool(cache_read_tokens_unknown),
+                cache_write_tokens_unknown=bool(cache_write_tokens_unknown),
+                usage_unknown=bool(usage_unknown),
             )
         self._execute_write(_do)
```

## target: `hermes_state_maintenance.py`

### `SessionDB.prune_sessions`
```diff
--- base::SessionDB.prune_sessions
+++ fork::SessionDB.prune_sessions
@@ -57,4 +57,9 @@
             # Orphan any sessions whose parent is about to be deleted
             placeholders = ",".join("?" * len(session_ids))
+            orphaned_child_ids, affected_root_ids = (
+                self._collect_orphan_effective_last_active_targets(
+                    conn, list(session_ids)
+                )
+            )
             conn.execute(
                 f"UPDATE sessions SET parent_session_id = NULL "
@@ -67,4 +72,7 @@
                 conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
                 removed_ids.append(sid)
+            self._recompute_effective_last_active_many(
+                conn, affected_root_ids + orphaned_child_ids
+            )
             self._delete_unreferenced_system_prompts(conn)
             return len(session_ids)
```

### `SessionDB.logical_size_bytes`
```diff
--- base::SessionDB.logical_size_bytes
+++ fork::SessionDB.logical_size_bytes
@@ -18,5 +18,5 @@
         try:
             with self._read_ctx() as conn:
-                if self._conn is None:
+                if conn is None:
                     return None
                 page_count = conn.execute("PRAGMA page_count").fetchone()[0]
```
