"""FleetReview #987 (t_e038f946): the kanban notifier's home-line lookup is a pure
read, run once per subscription per tick after the event cursor advanced. It must
not open a writable SessionDB (schema init + up to 20 s write patience on a lock)."""
import hermes_state as _state


def test_resolve_home_line_opens_state_db_read_only(monkeypatch):
    from gateway import kanban_watchers as kw

    seen = []

    class Spy:
        def __init__(self, *a, **k):
            seen.append(k.get("read_only", False))

        def get_session(self, sid):
            return None

        def close(self):
            pass

    monkeypatch.setattr(_state, "SessionDB", Spy)
    kw._resolve_home_line("sid_x")
    assert seen == [True]
