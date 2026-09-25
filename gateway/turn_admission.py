"""Event-loop turn backpressure and bounded, individually cancellable resumes."""

import asyncio
import logging
from collections import deque
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)


def _reap_notice(task):
    """Retrieve a cancelled/failed notice task's outcome off the hot path."""
    if task.cancelled():
        return
    if task.exception() is not None:
        logger.debug("turn slot notice failed", exc_info=task.exception())


class TurnAdmission:
    """Reserve user capacity without letting internal waiters occupy total slots.

    Admission is reentrant only within the SAME asyncio task: the handler
    acquires before its transcript lease, and the executor reuses that permit.
    Child tasks never inherit it. Config is fixed for the runner's lifetime.
    """

    warning_after = 5.0
    ack_after = 15.0
    default_reserve = 2

    def __init__(self, cap, reserve=None):
        self.cap = cap
        if reserve is None:
            reserve = self.default_reserve
        # Clamp into [0, cap - 1]: the reserve may never starve internal turns
        # of their last slot, and a negative reserve just disables it.
        reserve = min(max(int(reserve), 0), max(0, (cap or 1) - 1))
        self.reserve = reserve
        self.total = asyncio.Semaphore(cap) if cap else None
        self.internal = asyncio.Semaphore(max(1, cap - reserve)) if cap else None
        self.in_flight = 0
        self.waiting = 0
        self._owners = {}
        logger.info(
            "PHASE=turn_admission_init cap=%s reserve=%s", cap, reserve,
        )

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
        internal_acquired = total_acquired = admitted = False
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
            self.in_flight += 1
            admitted = True
            workers = self._owners[owner] = []
            logger.info(
                "PHASE=turn_slot_acquire in_flight=%s/%s", self.in_flight, self.cap,
            )
        except BaseException:
            # A generator that raises BEFORE its first yield never runs
            # __aexit__, so this is the ONLY release path for anything the
            # pre-yield section acquired. Cancellation is the live case: a
            # CancelledError delivered here used to burn one of ``cap`` slots
            # permanently, and at zero every turn blocked in acquire forever.
            if admitted:
                self.in_flight -= 1
                self._owners.pop(owner, None)
            if total_acquired:
                self.total.release()
            if internal_acquired:
                self.internal.release()
            raise
        finally:
            self.waiting -= 1
            # Never await the notice inside the critical section: that await
            # is the wide cancellation window (it sits behind ack() ->
            # adapter.send after a 15 s wait). Reap it detached instead.
            notice.cancel()
            notice.add_done_callback(_reap_notice)
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


class _AdmittedResumeHandle(asyncio.Future):
    """Handle for an admitted resume whose cancel() owns the running task.

    A bare Future marks itself done the instant it is cancelled, even though
    the admitted resume body keeps running. The shutdown path in
    ``gateway/run.py`` reads exactly that done/cancelled state to tell "never
    started -> cancel + re-mark the session" from "in progress -> leave it
    alone", so a bare Future let a RUNNING resume be re-marked and restored a
    second time on top of the original turn. Cancel therefore delegates to the
    task, and the handle stays pending until the task actually finishes.
    """

    _task = None

    def cancel(self, msg=None):
        task = self._task
        if task is not None and not task.done():
            # ``finished`` resolves this handle from the task's done callback.
            return task.cancel() if msg is None else task.cancel(msg)
        return super().cancel() if msg is None else super().cancel(msg)


class StartupResumePool:
    """FIFO dispatcher with at most N resume bodies, not N tasks per entry.

    Queued entries have a Future, preserving the shutdown path's per-session
    cancel/done contract. Only admitted entries get an execution task, and
    their handle's cancel() delegates to that task.
    """

    def __init__(self, concurrency):
        self.concurrency = concurrency
        self.pending = deque()
        self.running = set()

    def submit(self, callback, *args):
        future = _AdmittedResumeHandle(loop=asyncio.get_running_loop())
        self.pending.append((future, callback, args))
        # Create admitted tasks now, matching create_task's scheduling timing.
        # Their bodies cannot run until the synchronous caller yields, so the
        # startup scheduler still claims every session sentinel first.
        self._pump()
        return future

    def _pump(self):
        while self.pending and (self.concurrency is None or len(self.running) < self.concurrency):
            future, callback, args = self.pending.popleft()
            if future.cancelled():
                continue
            task = asyncio.create_task(callback(*args))
            self.running.add(task)
            # Bind before any await point so a cancel racing admission
            # reaches the task rather than completing the handle early.
            future._task = task

            def finished(task, future=future):
                self.running.discard(task)
                if task.cancelled():
                    if not future.done():
                        asyncio.Future.cancel(future)
                else:
                    error = task.exception()
                    if not future.done():
                        if error is not None:
                            future.set_exception(error)
                        else:
                            future.set_result(task.result())
                self._pump()

            task.add_done_callback(finished)
