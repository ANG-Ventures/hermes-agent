"""Request-local policy for callers owning an explicit, bounded attempt chain."""
from contextlib import contextmanager
from contextvars import ContextVar

_ROUTE: ContextVar[tuple[str, dict] | None] = ContextVar('auxiliary_single_attempt_route', default=None)


def single_attempt_config(task):
    route = _ROUTE.get()
    return route[1] if route is not None and route[0] == task else None


@contextmanager
def single_attempt(task, config):
    token = _ROUTE.set((task, dict(config)))
    try:
        yield
    finally:
        _ROUTE.reset(token)
