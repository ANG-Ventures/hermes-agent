import asyncio

from hermes_cli import web_server


class _FakeSessionDB:
    """Fake backing the /api/sessions/search endpoint.

    The endpoint surfaces direct session-id matches first, then FTS message
    matches, deduping both by compression lineage root. This fake has no
    compression chains (get_session returns no parent), so each session is its
    own lineage root.
    """

    closed = False
    opened_read_only = None
    requested_fields = None

    def __init__(self, *args, **kwargs):
        type(self).opened_read_only = kwargs.get("read_only")

    @staticmethod
    def _source_allowed(row, source=None, sources=None, exclude_sources=None):
        row_source = row.get("source")
        if source and row_source != source:
            return False
        if sources and row_source not in sources:
            return False
        if exclude_sources and row_source in exclude_sources:
            return False
        return True

    def search_sessions_by_id(
        self,
        query,
        limit=20,
        include_archived=True,
        source=None,
        sources=None,
        exclude_sources=None,
    ):
        assert query == "20260603"
        assert include_archived is True
        rows = [
            {
                "id": "20260603_090200_exact",
                "preview": "ID match preview",
                "source": "cli",
                "model": "claude",
                "started_at": 100,
            }
        ]
        return [
            row
            for row in rows
            if self._source_allowed(
                row, source=source, sources=sources, exclude_sources=exclude_sources
            )
        ][:limit]

    def search_sessions_by_title(self, query, limit=20, include_archived=True):
        assert query == "20260603"
        assert include_archived is True
        return []

    def search_messages(self, query, limit=20):
        assert query == "20260603*"
        return [
            row
            for row in rows
            if self._source_allowed(
                row, source=source, sources=sources, exclude_sources=exclude_sources
            )
        ][:limit]

    def search_messages(
        self,
        query,
        source_filter=None,
        exclude_sources=None,
        limit=20,
        fields=None,
    ):
        assert query == "20260603*"
        type(self).requested_fields = fields
        rows = [
            {
                "session_id": "20260603_090200_exact",
                "snippet": "duplicate content hit should not replace ID hit",
                "role": "user",
                "source": "cli",
                "model": "claude",
                "session_started": 100,
            },
            {
                "session_id": "content_session",
                "snippet": "content hit",
                "role": "assistant",
                "source": "desktop",
                "model": "gpt",
                "session_started": 200,
            },
        ]
        return [
            row
            for row in rows
            if self._source_allowed(
                row, sources=source_filter, exclude_sources=exclude_sources
            )
        ][:limit]

    def get_session(self, session_id):
        # No compression chains in this fixture — every session is its own root.
        return {"id": session_id, "parent_session_id": None}

    def get_compression_tip(self, session_id):
        return session_id

    def close(self):
        self.closed = True


def test_desktop_session_search_merges_id_matches_before_content_matches(monkeypatch):
    _FakeSessionDB.opened_read_only = None
    _FakeSessionDB.requested_fields = None
    monkeypatch.setattr("hermes_state.SessionDB", _FakeSessionDB)

    response = asyncio.run(web_server.search_sessions(q="20260603", limit=2))

    assert _FakeSessionDB.requested_fields is not None
    assert "context" not in _FakeSessionDB.requested_fields
    # ID match surfaces first; the content hit on the SAME session is deduped
    # by lineage root (not double-listed); the unrelated content hit follows.
    assert response == {
        "results": [
            {
                "id": "20260603_090200_exact",
                "session_id": "20260603_090200_exact",
                "lineage_root": "20260603_090200_exact",
                "snippet": "ID match preview",
                "role": None,
                "source": "cli",
                "model": "claude",
                "session_started": 100,
            },
            {
                "id": "content_session",
                "session_id": "content_session",
                "lineage_root": "content_session",
                "snippet": "content hit",
                "role": "assistant",
                "source": "desktop",
                "model": "gpt",
                "session_started": 200,
            },
        ]
    }


class _FakeTitleSessionDB:
    """Fake proving title matches rank above content matches (and dedupe)."""

    # fork-parity: upstream's _open_session_db_at_path bootstraps a missing
    # store via SessionDB(db_path=..., read_only=False), and its search handler
    # passes source/sources/exclude_sources (+ fields on search_messages). This
    # fork-authored fake predates both, so it needs the permissive ctor its
    # sibling _FakeSessionDB already has and the widened kwargs.
    def __init__(self, *args, **kwargs):
        pass

    def search_sessions_by_id(
        self,
        query,
        limit=20,
        include_archived=True,
        source=None,
        sources=None,
        exclude_sources=None,
    ):
        return []

    def search_sessions_by_title(self, query, limit=20, include_archived=True):
        assert query == "portal"
        return [
            {
                "id": "titled_session",
                "title": "DNS Portal Investigation",
                "preview": "first user message",
                "source": "discord",
                "model": "claude",
                "started_at": 300,
            }
        ]

    def search_messages(
        self,
        query,
        limit=20,
        source_filter=None,
        exclude_sources=None,
        fields=None,
    ):
        assert query == "portal*"
        return [
            # Content hit on the SAME session the title lane already surfaced —
            # must dedupe by lineage, not double-list.
            {
                "session_id": "titled_session",
                "snippet": "portal mentioned in content",
                "role": "user",
                "source": "discord",
                "model": "claude",
                "session_started": 300,
            },
            {
                "session_id": "content_only",
                "snippet": "another portal content hit",
                "role": "assistant",
                "source": "cli",
                "model": "gpt",
                "session_started": 400,
            },
        ]

    def get_session(self, session_id):
        return {"id": session_id, "parent_session_id": None}

    def get_compression_tip(self, session_id):
        return session_id

    def close(self):
        pass


def test_desktop_session_search_ranks_title_matches_before_content_matches(monkeypatch):
    monkeypatch.setattr("hermes_state.SessionDB", _FakeTitleSessionDB)

    response = asyncio.run(web_server.search_sessions(q="portal", limit=5))

    results = response["results"]
    assert [r["session_id"] for r in results] == ["titled_session", "content_only"]
    # The title lane carries the title through to the client.
    assert results[0]["title"] == "DNS Portal Investigation"
    # Title-lane snippet prefers the session preview.
    assert results[0]["snippet"] == "first user message"
    # The duplicate content hit on titled_session did not double-list it.
    assert len(results) == 2
    assert _FakeSessionDB.opened_read_only is True
