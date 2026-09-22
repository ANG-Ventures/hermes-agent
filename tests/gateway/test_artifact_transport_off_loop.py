"""The one-shot artifact routes must not block the aiohttp event loop.

``POST /v1/artifacts/upload`` handed its validated bytes to
``ArtifactStore.store``: mkstemp, write, ``os.fsync``, ``os.replace``.
``GET /v1/artifacts/download/{id}`` ends in ``ArtifactStore.load``: a
whole-file ``read_bytes``, a SHA-256 re-hash, and an ``unlink``.  Both ran
inline on the single aiohttp event-loop thread, so a slow filesystem stalled
every other in-flight API request behind one artifact.

These are BEHAVIOURAL tests: they make the filesystem genuinely slow and
assert the loop kept serving.  The store-resolution tests cover the third
blocking site on the same routes — ``ArtifactStore.__init__``'s mkdir +
full-directory orphan sweep plus ``prune_expired`` — which a cold profile
pays on its first artifact request.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway import browser_control_artifacts as artifacts_mod
from gateway.browser_control_artifacts import ArtifactStore
from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter

API_KEY = "-".join(("fixture", "neutral", "api", "key", "123"))
TEXT_BYTES = b"fixture artifact payload\n"
HOLD_SECONDS = 0.3


def _adapter(monkeypatch):
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": API_KEY}))
    monkeypatch.setattr(adapter, "_browser_control_enabled", lambda: True)
    return adapter


def _app(adapter):
    app = web.Application()
    app.router.add_post("/v1/artifacts/upload", adapter._handle_artifact_upload)
    app.router.add_get(
        "/v1/artifacts/download/{artifact_id}", adapter._handle_artifact_download
    )
    return app


def _auth():
    return {"Authorization": f"Bearer {API_KEY}"}


def _upload_headers(name="note.txt"):
    return {"Content-Type": "text/plain", "X-Artifact-Filename": name, **_auth()}


class _Ticker:
    """Counts how many times a sibling coroutine advanced."""

    def __init__(self):
        self.ticks = 0
        self._stop = asyncio.Event()
        self._task = None

    def start(self):
        self._task = asyncio.create_task(self._run())
        return self

    async def _run(self):
        while not self._stop.is_set():
            self.ticks += 1
            await asyncio.sleep(0.005)

    async def stop(self):
        self._stop.set()
        if self._task is not None:
            await self._task


# ----------------------------------------------------------------------
# Upload: the frozen ratchet entry (os.fsync via ArtifactStore.store)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_keeps_the_loop_alive_while_the_rename_is_held(
    monkeypatch, tmp_path
):
    """A slow artifact write must not stall every other API request.

    Inline, the loop ticked ~0 times for the whole hold.  Off-loop it keeps
    serving, so the tick count is bounded well below zero-progress.
    """
    adapter = _adapter(monkeypatch)
    adapter._inject_browser_control_artifacts(ArtifactStore(tmp_path / "root"))
    loop_thread = threading.current_thread().name
    ran_on: list[str] = []
    real_fsync = artifacts_mod.os.fsync

    def slow_fsync(fd):
        ran_on.append(threading.current_thread().name)
        time.sleep(HOLD_SECONDS)
        return real_fsync(fd)

    monkeypatch.setattr(artifacts_mod.os, "fsync", slow_fsync)

    async with TestClient(TestServer(_app(adapter))) as client:
        ticker = _Ticker().start()
        await asyncio.sleep(0.05)
        before = ticker.ticks
        response = await client.post(
            "/v1/artifacts/upload", data=TEXT_BYTES, headers=_upload_headers()
        )
        during = ticker.ticks - before
        await ticker.stop()

    assert response.status == 201
    assert ran_on, "the artifact write never reached os.fsync"
    assert ran_on[0] != loop_thread, (
        f"ArtifactStore.store ran on the event-loop thread ({loop_thread})"
    )
    # 0.3s of hold at a 5ms tick is ~60 ticks of headroom; inline this is 0-1.
    assert during >= 10, (
        f"the event loop advanced only {during} times while one artifact "
        f"write held os.fsync for {HOLD_SECONDS}s"
    )


@pytest.mark.asyncio
async def test_upload_still_round_trips_through_the_offload(monkeypatch, tmp_path):
    """Moving the write off-loop must not change the route's contract."""
    adapter = _adapter(monkeypatch)
    adapter._inject_browser_control_artifacts(ArtifactStore(tmp_path / "root"))

    async with TestClient(TestServer(_app(adapter))) as client:
        upload = await client.post(
            "/v1/artifacts/upload", data=TEXT_BYTES, headers=_upload_headers()
        )
        assert upload.status == 201
        receipt = await upload.json()
        assert receipt["size_bytes"] == len(TEXT_BYTES)

        download = await client.get(receipt["download_path"], headers=_auth())
        assert download.status == 200
        assert await download.read() == TEXT_BYTES


@pytest.mark.asyncio
async def test_upload_surfaces_a_failed_write_instead_of_a_success_receipt(
    monkeypatch, tmp_path
):
    """A rejected offload must not become a 201 with an unwritten artifact.

    The offload is awaited precisely so the caller still learns the write
    failed.  A fire-and-forget or exception-swallowing form would hand the
    client a receipt for bytes that are not on disk.
    """
    adapter = _adapter(monkeypatch)
    adapter._inject_browser_control_artifacts(ArtifactStore(tmp_path / "root"))

    def exploding_replace(src, dst):
        raise OSError("no space left on device")

    monkeypatch.setattr(artifacts_mod.os, "replace", exploding_replace)

    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.post(
            "/v1/artifacts/upload", data=TEXT_BYTES, headers=_upload_headers()
        )

    assert response.status != 201, (
        "the route reported success for an artifact that was never written"
    )


@pytest.mark.asyncio
async def test_concurrent_uploads_do_not_lose_receipts(monkeypatch, tmp_path):
    """Off-loop writes are genuinely concurrent; every id must stay live.

    Inline, the read-modify-write of the index was implicitly serialized by
    the loop.  In worker threads it is not, and every minted id must still
    be downloadable.
    """
    adapter = _adapter(monkeypatch)
    adapter._inject_browser_control_artifacts(ArtifactStore(tmp_path / "root"))

    async with TestClient(TestServer(_app(adapter))) as client:
        uploads = await asyncio.gather(*[
            client.post(
                "/v1/artifacts/upload",
                data=f"payload {i}\n".encode(),
                headers=_upload_headers(f"note-{i}.txt"),
            )
            for i in range(12)
        ])
        assert [u.status for u in uploads] == [201] * 12
        receipts = [await u.json() for u in uploads]
        assert len({r["artifact_id"] for r in receipts}) == 12
        for index, receipt in enumerate(receipts):
            download = await client.get(receipt["download_path"], headers=_auth())
            assert download.status == 200
            assert await download.read() == f"payload {index}\n".encode()


# ----------------------------------------------------------------------
# Download: the same class, one coroutine over from the frozen entry
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_keeps_the_loop_alive_while_the_read_is_held(
    monkeypatch, tmp_path
):
    """``ArtifactStore.load`` reads, re-hashes and unlinks — all blocking."""
    adapter = _adapter(monkeypatch)
    store = ArtifactStore(tmp_path / "root")
    adapter._inject_browser_control_artifacts(store)
    loop_thread = threading.current_thread().name

    async with TestClient(TestServer(_app(adapter))) as client:
        upload = await client.post(
            "/v1/artifacts/upload", data=TEXT_BYTES, headers=_upload_headers()
        )
        receipt = await upload.json()

        ran_on: list[str] = []
        real_unlink = os.unlink

        def slow_unlink(path, **kwargs):
            ran_on.append(threading.current_thread().name)
            time.sleep(HOLD_SECONDS)
            return real_unlink(path, **kwargs)

        monkeypatch.setattr(os, "unlink", slow_unlink)

        ticker = _Ticker().start()
        await asyncio.sleep(0.05)
        before = ticker.ticks
        download = await client.get(receipt["download_path"], headers=_auth())
        during = ticker.ticks - before
        await ticker.stop()

    assert download.status == 200
    assert ran_on, "the download never reached the artifact unlink"
    assert ran_on[0] != loop_thread, (
        f"ArtifactStore.load ran on the event-loop thread ({loop_thread})"
    )
    assert during >= 10, (
        f"the event loop advanced only {during} times while one artifact "
        f"download held unlink for {HOLD_SECONDS}s"
    )


# ----------------------------------------------------------------------
# Store resolution: the cold-profile construction on the same routes
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_request_for_a_profile_resolves_the_store_off_loop(
    monkeypatch, tmp_path
):
    """The cold-profile orphan sweep must not run on the loop thread.

    ``ArtifactStore.__init__`` mkdirs the root and iterates the whole
    directory to delete orphans; a profile with a large artifact root pays
    that on its first request.  Nothing is injected here, so the route takes
    the real lazy-construction path.
    """
    adapter = _adapter(monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    loop_thread = threading.current_thread().name
    ran_on: list[str] = []
    real_init = ArtifactStore.__init__

    def slow_init(self, root, **kwargs):
        ran_on.append(threading.current_thread().name)
        time.sleep(HOLD_SECONDS)
        return real_init(self, root, **kwargs)

    monkeypatch.setattr(ArtifactStore, "__init__", slow_init)

    async with TestClient(TestServer(_app(adapter))) as client:
        ticker = _Ticker().start()
        await asyncio.sleep(0.05)
        before = ticker.ticks
        response = await client.post(
            "/v1/artifacts/upload", data=TEXT_BYTES, headers=_upload_headers()
        )
        during = ticker.ticks - before
        await ticker.stop()

    assert response.status == 201
    assert ran_on, "the store was never constructed"
    assert ran_on[0] != loop_thread, (
        f"ArtifactStore construction ran on the event-loop thread ({loop_thread})"
    )
    assert during >= 10, (
        f"the event loop advanced only {during} times while one cold-profile "
        f"store construction held for {HOLD_SECONDS}s"
    )


@pytest.mark.asyncio
async def test_concurrent_cold_requests_share_one_store_instance(
    monkeypatch, tmp_path
):
    """The offload adds an await between cache miss and cache fill.

    Without single-flight both racers construct a store and the loser's
    instance is evicted from the cache — and since receipts live only in
    that instance's memory, an artifact uploaded through it becomes
    permanently undownloadable.  Assert BOTH halves: one construction, and
    every uploaded id still retrievable.
    """
    adapter = _adapter(monkeypatch)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    constructions: list[str] = []
    real_init = ArtifactStore.__init__

    def counting_init(self, root, **kwargs):
        constructions.append(str(root))
        time.sleep(0.05)  # widen the race window
        return real_init(self, root, **kwargs)

    monkeypatch.setattr(ArtifactStore, "__init__", counting_init)

    async with TestClient(TestServer(_app(adapter))) as client:
        uploads = await asyncio.gather(*[
            client.post(
                "/v1/artifacts/upload",
                data=f"racer {i}\n".encode(),
                headers=_upload_headers(f"racer-{i}.txt"),
            )
            for i in range(4)
        ])
        assert [u.status for u in uploads] == [201] * 4
        receipts = [await u.json() for u in uploads]

        assert len(constructions) == 1, (
            f"built {len(constructions)} stores for one profile; a racing "
            "first request evicted another's in-memory receipt index"
        )

        for receipt in receipts:
            download = await client.get(receipt["download_path"], headers=_auth())
            assert download.status == 200, (
                f"artifact {receipt['artifact_id']} was uploaded through a "
                "store instance that is no longer reachable"
            )


@pytest.mark.asyncio
async def test_store_resolution_stays_on_the_loop_when_already_cached(
    monkeypatch, tmp_path
):
    """A warm cache hit must not pay a thread hop per request.

    The offload is for construction only; making every artifact request
    round-trip through a worker thread would be a latency regression on the
    common path.
    """
    adapter = _adapter(monkeypatch)
    store = ArtifactStore(tmp_path / "root")
    adapter._inject_browser_control_artifacts(store)
    hops: list[str] = []
    real_to_thread = asyncio.to_thread
    # Compare the UNDERLYING function: ``adapter._artifact_store_for`` builds a
    # fresh bound-method object on every attribute access, so an ``is`` test
    # against it never matches and this assertion would be vacuous.
    resolver = APIServerAdapter._artifact_store_for

    async def counting_to_thread(func, /, *args, **kwargs):
        if getattr(func, "__func__", func) is resolver:
            hops.append(getattr(func, "__name__", repr(func)))
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", counting_to_thread)

    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.post(
            "/v1/artifacts/upload", data=TEXT_BYTES, headers=_upload_headers()
        )

    assert response.status == 201
    assert hops == [], (
        "a cached store was still resolved through a worker thread"
    )
