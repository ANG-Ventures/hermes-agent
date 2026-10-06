"""session["_run_thread"] is only ever visible once started, so a concurrent join() cannot raise (t_99a9c529)."""
import threading
import time

from tui_gateway import session_lifecycle


def test_published_run_thread_is_always_joinable(monkeypatch):
    import agent.memory_provider as memory_provider

    real_spawn = memory_provider.spawn_context_thread
    in_start = threading.Event()

    def slow_start_spawn(target, *, name, **kwargs):
        thread = real_spawn(target, name=name, **kwargs)
        real_start = thread.start

        def start():
            # Hold the publish/start window open far longer than any scheduler hiccup would.
            in_start.set()
            time.sleep(0.5)
            real_start()

        thread.start = start
        return thread

    monkeypatch.setattr(memory_provider, "spawn_context_thread", slow_start_spawn)
    session: dict = {}
    finished = threading.Event()
    spawner = threading.Thread(
        target=session_lifecycle._start_session_work, args=(finished.set,),
        kwargs={"name": "publish-order-probe", "session": session}, daemon=True)
    spawner.start()
    assert in_start.wait(5)

    seen = []
    deadline = time.monotonic() + 5
    while not finished.is_set() and time.monotonic() < deadline:
        if (thread := session.get("_run_thread")) is not None:
            seen.append(thread)
            thread.join(timeout=5)  # raised "cannot join thread before it is started" before the fix
        time.sleep(0.01)
    spawner.join(timeout=5)

    assert finished.is_set()
    assert session["_run_thread"].ident is not None
    assert all(thread.ident is not None for thread in seen)
