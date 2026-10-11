"""Self-hosted mem0: re-read the admin key from mem0.json on HTTP 401 when the file changed.

A prod key rotation rewrites every mem0.json, the server accepts the file key, and every live
gateway keeps 401-ing with the init-time key until restarted (t_fdff9596). On a 401 the client
re-stats mem0.json; if the mtime moved since the key was loaded it re-reads the key ONCE and
retries the same call. A second 401 surfaces as before (no loop); an unchanged file retries never.
"""

import io
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request
from email.message import Message

from plugins.memory.mem0 import Mem0MemoryProvider

HOST = "http://mem0.test"


class _HTTPResponse:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


def _header(request, name):
    name = name.lower()
    for key, value in request.header_items():
        if key.lower() == name:
            return value
    return None


def _provider_with_file_key(monkeypatch, tmp_path, key):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for var in ("MEM0_HOST", "MEM0_ADMIN_API_KEY", "MEM0_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MEM0_USER_ID", "ace")
    monkeypatch.setenv("MEM0_DESTRUCTIVE_TOOLS", "false")
    (tmp_path / "mem0.json").write_text(json.dumps({"host": HOST, "admin_api_key": key}))
    provider = Mem0MemoryProvider()
    provider.initialize("test-session")
    assert provider._admin_api_key == key
    return provider


def _install_key_gated_server(monkeypatch, accepted_key):
    """Fake mem0 server: 401 unless X-API-Key == accepted_key. Records the key of every call."""
    sent = []

    def fake_urlopen(request, timeout=0, context=None):
        sent.append(_header(request, "X-API-Key"))
        if sent[-1] != accepted_key:
            raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", hdrs=Message(),
                                         fp=io.BytesIO(b'{"detail":"bad key"}'))
        return _HTTPResponse({"results": [{"id": "m1", "memory": "matched fact", "score": 0.9}]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return sent


def _count_key_rereads(monkeypatch):
    """Count mem0.json reads made by the 401 key re-read ONLY. The recall hot path reads other
    mem0.json blocks fresh on every call by design (rerank kill-switch, prefetch floor), so a
    bare read_text count would not isolate the rotation path."""
    reads = []
    real_read_text = pathlib.Path.read_text

    def counting_read_text(self, *args, **kwargs):
        if self.name == "mem0.json" and sys._getframe(1).f_code.co_name == "_reread_admin_key_if_rotated":
            reads.append(str(self))
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", counting_read_text)
    return reads


def _rotate_file(tmp_path, key):
    """Rewrite mem0.json with a new key, mtime strictly later than the init-time read
    (a real rotation lands seconds later; coarse-mtime filesystems need the nudge)."""
    cfg_path = tmp_path / "mem0.json"
    before = cfg_path.stat().st_mtime
    cfg_path.write_text(json.dumps({"host": HOST, "admin_api_key": key}))
    os.utime(cfg_path, (before + 5, before + 5))


def _search(provider, query="needle"):
    return json.loads(provider.handle_tool_call("mem0_search", {"query": query}))


def test_401_with_rotated_file_rereads_key_once_and_retries(monkeypatch, tmp_path):
    provider = _provider_with_file_key(monkeypatch, tmp_path, "key-A")
    sent = _install_key_gated_server(monkeypatch, accepted_key="key-B")
    _rotate_file(tmp_path, "key-B")
    reads = _count_key_rereads(monkeypatch)

    result = _search(provider)

    assert result == {"results": [{"memory": "matched fact", "score": 0.9}], "count": 1}
    assert sent == ["key-A", "key-B"]          # one 401, one retry with the re-read key
    assert len(reads) == 1                     # exactly one re-read of mem0.json
    assert provider._admin_api_key == "key-B"
    assert provider._consecutive_failures == 0

    # Steady state after the rotation: the new key goes out directly, no further re-read.
    _search(provider, "again")
    assert sent == ["key-A", "key-B", "key-B"]
    assert len(reads) == 1


def test_401_with_unchanged_file_retries_zero_times(monkeypatch, tmp_path):
    provider = _provider_with_file_key(monkeypatch, tmp_path, "key-A")
    sent = _install_key_gated_server(monkeypatch, accepted_key="key-B")
    reads = _count_key_rereads(monkeypatch)

    result = _search(provider)

    assert "error" in result and "401" in result["error"]
    assert sent == ["key-A"]                   # no retry
    assert reads == []                         # no re-read
    assert provider._consecutive_failures == 1
    assert provider._admin_api_key == "key-A"


def test_401_after_reread_surfaces_outage_without_loop(monkeypatch, tmp_path):
    provider = _provider_with_file_key(monkeypatch, tmp_path, "key-A")
    sent = _install_key_gated_server(monkeypatch, accepted_key="key-C")
    _rotate_file(tmp_path, "key-B")            # rotated, but to a key the server still rejects
    reads = _count_key_rereads(monkeypatch)

    result = _search(provider)

    assert "error" in result and "401" in result["error"]
    assert sent == ["key-A", "key-B"]          # exactly one retry, then surfaced
    assert len(reads) == 1
    assert provider._consecutive_failures == 1

    # The file has not moved again: the next call retries zero times and re-reads nothing.
    _search(provider, "again")
    assert sent == ["key-A", "key-B", "key-B"]
    assert len(reads) == 1


def test_non_401_error_never_touches_the_file(monkeypatch, tmp_path):
    provider = _provider_with_file_key(monkeypatch, tmp_path, "key-A")
    _rotate_file(tmp_path, "key-B")
    reads = _count_key_rereads(monkeypatch)
    sent = []

    def fake_urlopen(request, timeout=0, context=None):
        sent.append(_header(request, "X-API-Key"))
        raise urllib.error.HTTPError(request.full_url, 500, "Server Error", hdrs=Message(),
                                     fp=io.BytesIO(b"boom"))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    result = _search(provider)

    assert "error" in result and "500" in result["error"]
    assert sent == ["key-A"]
    assert reads == []


def test_401_with_changed_host_never_sends_the_other_servers_key(monkeypatch, tmp_path):
    provider = _provider_with_file_key(monkeypatch, tmp_path, "key-A")
    sent = _install_key_gated_server(monkeypatch, accepted_key="key-B")
    cfg_path = tmp_path / "mem0.json"
    before = cfg_path.stat().st_mtime
    cfg_path.write_text(json.dumps({"host": "http://other.test", "admin_api_key": "key-B"}))
    os.utime(cfg_path, (before + 5, before + 5))

    result = _search(provider)

    assert "error" in result and "401" in result["error"]
    assert sent == ["key-A"]                   # key-B belongs to other.test: never sent to mem0.test
    assert provider._admin_api_key == "key-A"


def test_rotation_between_init_stat_and_read_is_still_picked_up(monkeypatch, tmp_path):
    """The mtime snapshot is taken BEFORE the config read, so a rotation racing initialize()
    is never marked as already-loaded while the old key was the one read."""
    import plugins.memory.mem0 as mem0_mod
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for var in ("MEM0_HOST", "MEM0_ADMIN_API_KEY", "MEM0_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MEM0_USER_ID", "ace")
    monkeypatch.setenv("MEM0_DESTRUCTIVE_TOOLS", "false")
    cfg_path = tmp_path / "mem0.json"
    cfg_path.write_text(json.dumps({"host": HOST, "admin_api_key": "key-A"}))
    real_load = mem0_mod._load_config

    def load_then_rotate():
        cfg = real_load()                      # returns key-A ...
        _rotate_file(tmp_path, "key-B")        # ... and the rotation lands right after the read
        return cfg

    monkeypatch.setattr(mem0_mod, "_load_config", load_then_rotate)
    provider = Mem0MemoryProvider()
    provider.initialize("test-session")
    assert provider._admin_api_key == "key-A"
    sent = _install_key_gated_server(monkeypatch, accepted_key="key-B")

    result = _search(provider)

    assert result["count"] == 1
    assert sent == ["key-A", "key-B"]


def test_refresh_reads_the_profile_file_pinned_at_init_not_the_current_home(monkeypatch, tmp_path):
    """A background thread (capture drain) may run with a different HERMES_HOME resolved; the
    401 re-read must come from the owning profile's mem0.json, never another profile's."""
    provider = _provider_with_file_key(monkeypatch, tmp_path, "key-A")
    other_home = tmp_path / "other-profile"
    other_home.mkdir()
    (other_home / "mem0.json").write_text(json.dumps({"host": HOST, "admin_api_key": "key-OTHER"}))
    monkeypatch.setenv("HERMES_HOME", str(other_home))
    sent = _install_key_gated_server(monkeypatch, accepted_key="key-B")
    _rotate_file(tmp_path, "key-B")

    result = _search(provider)

    assert result["count"] == 1
    assert sent == ["key-A", "key-B"]          # the owning profile's rotated key, not key-OTHER
    assert "key-OTHER" not in sent
