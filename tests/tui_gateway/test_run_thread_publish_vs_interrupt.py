"""A started turn worker is never invisible to the interrupt liveness probe (Prism 22606434c9d8 on #1798).

#1798 moved ``session["_run_thread"] = thread`` after ``thread.start()``. Between the two the worker runs while
the session still holds the previous (dead) or no handle, so ``_interrupt_session_turn`` read "not alive" and
cleared ``running`` + the in-flight turn under a live worker, letting a second prompt in on top of it.
"""
import threading
import time

from tui_gateway import server


def _slow_publish_spawn(monkeypatch, started: threading.Event):
    """Start the real thread, then stall before ``_start_session_work`` can publish it."""
    import agent.memory_provider as memory_provider

    real_spawn = memory_provider.spawn_context_thread

    def spawn(target, *, name, **kwargs):
        thread = real_spawn(target, name=name, **kwargs)
        real_start = thread.start

        def start():
            real_start()
            started.set()
            time.sleep(0.5)  # the start->publish window, held open

        thread.start = start
        return thread

    monkeypatch.setattr(memory_provider, "spawn_context_thread", spawn)


def _session() -> dict:
    return {"running": True, "history_lock": threading.Lock(), "session_key": "k-publish-probe", "agent": None,
            "_run_thread": None}


def test_interrupt_does_not_clear_running_under_a_started_unpublished_worker(monkeypatch):
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *_a, **_k: False)
    started, release = threading.Event(), threading.Event()
    _slow_publish_spawn(monkeypatch, started)
    session = _session()
    spawner = threading.Thread(
        target=server._start_session_work, args=(lambda: release.wait(10),),
        kwargs={"name": "publish-vs-interrupt", "session": session}, daemon=True)
    spawner.start()
    try:
        assert started.wait(5)
        server._interrupt_session_turn("sid-publish-probe", session)
        # The worker is alive: only its own finally may clear the turn.
        assert session["running"] is True
    finally:
        release.set()
        spawner.join(5)
        if (t := session.get("_run_thread")) is not None:
            t.join(5)


def test_teardown_and_reaper_read_the_published_worker(monkeypatch):
    """The other liveness readers go through the same lock, so they see the live worker, not the old handle."""
    started, release = threading.Event(), threading.Event()
    _slow_publish_spawn(monkeypatch, started)
    session = _session()
    spawner = threading.Thread(
        target=server._start_session_work, args=(lambda: release.wait(10),),
        kwargs={"name": "publish-vs-reader", "session": session}, daemon=True)
    spawner.start()
    try:
        assert started.wait(5)
        rt = server._session_run_thread(session)
        assert rt is not None and rt.is_alive()
    finally:
        release.set()
        spawner.join(5)
        if (t := session.get("_run_thread")) is not None:
            t.join(5)
