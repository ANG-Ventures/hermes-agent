"""Event-loop turn backpressure and bounded, individually cancellable resumes."""

import asyncio
import logging
from collections import deque
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)


class TurnAdmission:
    """Reserve user capacity without letting internal waiters occupy total slots.

    Admission is reentrant only within the SAME asyncio task: the handler
    acquires before its transcript lease, and the executor reuses that permit.
    Child tasks never inherit it. Config is fixed for the runner's lifetime.
    """

    warning_after = 5.0
    ack_after = 15.0

    def __init__(self, cap):
        self.cap = cap
        self.total = asyncio.Semaphore(cap) if cap else None
        self.internal = asyncio.Semaphore(max(1, cap - 2)) if cap else None
        self.in_flight = 0
        self.waiting = 0
        self._owners = {}

    def retain_worker(self, future):
        """A cancelled/timed-out caller must not free a still-running executor."""
        workers = self._owners.get(asyncio.current_task())
        if workers is not None:
            workers.append(future)

    async def _wait_notice(self, key, internal, ack, started):
        await asyncio.sleep(self.warning_after)
        logger.warning(
            "PHASE=turn_slot_wait key=%s waited=%.1f in_flight=%s kind=%s",
            key, asyncio.get_running_loop().time() - started,
            self.in_flight, "internal" if internal else "user",
        )
        if not internal and ack is not None:
            await asyncio.sleep(max(0, self.ack_after - self.warning_after))
            await ack()

    @asynccontextmanager
    async def slot(self, key, *, internal=False, ack=None):
        owner = asyncio.current_task()
        if self.total is None or owner in self._owners:
            yield
            return
        internal_acquired = total_acquired = False
        self.waiting += 1
        notice = asyncio.create_task(self._wait_notice(
            key, internal, ack, asyncio.get_running_loop().time(),
        ))
        try:
            if internal:
                await self.internal.acquire()
                internal_acquired = True
            await self.total.acquire()
            total_acquired = True
        finally:
            self.waiting -= 1
            notice.cancel()
            await asyncio.gather(notice, return_exceptions=True)
            if internal_acquired and not total_acquired:
                self.internal.release()
        self.in_flight += 1
        workers = self._owners[owner] = []
        logger.info("PHASE=turn_slot_acquire in_flight=%s/%s", self.in_flight, self.cap)
        try:
            yield
        finally:
            del self._owners[owner]
            pending = {worker for worker in workers if not worker.done()}

            def release(worker=None):
                if worker is not None:
                    pending.discard(worker)
                if not pending:
                    self.in_flight -= 1
                    self.total.release()
                    if internal_acquired:
                        self.internal.release()

            if pending:
                for worker in pending:
                    worker.add_done_callback(release)
            else:
                release()


class StartupResumePool:
    """FIFO dispatcher with at most N resume bodies, not N tasks per entry.

    Queued entries have a Future, preserving the shutdown path's per-session
    cancel/done contract. Only admitted entries get an execution task.
    """

    def __init__(self, concurrency):
        self.concurrency = concurrency
        self.pending = deque()
        self.running = set()

    def submit(self, callback, *args):
        future = asyncio.get_running_loop().create_future()
        self.pending.append((future, callback, args))
        # Create admitted tasks now, matching create_task's scheduling timing.
        # Their bodies cannot run until the synchronous caller yields, so the
        # startup scheduler still claims every session sentinel first.
        self._pump()
        return future

    def _pump(self):
        while self.pending and len(self.running) < self.concurrency:
            future, callback, args = self.pending.popleft()
            if future.cancelled():
                continue
            task = asyncio.create_task(callback(*args))
            self.running.add(task)

            def cancel(future, task=task):
                if future.cancelled():
                    task.cancel()

            def finished(task, future=future, cancel=cancel):
                self.running.discard(task)
                future.remove_done_callback(cancel)
                if task.cancelled():
                    future.cancel()
                else:
                    error = task.exception()
                    if not future.done():
                        if error is not None:
                            future.set_exception(error)
                        else:
                            future.set_result(task.result())
                self._pump()

            future.add_done_callback(cancel)
            task.add_done_callback(finished)
