"""Coalesced event-driven scheduling with a single cancellable deadline timer."""

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from datetime import datetime
from typing import Any


class WakeupScheduler:
    """Serialize wakeups without a permanent polling task or lost notifications."""

    def __init__(
        self,
        work: Callable[[], Awaitable[None]],
        deadline: Callable[[], datetime | None],
        clock: Callable[[], datetime],
        create_task: Callable[[Coroutine[Any, Any, None]], asyncio.Task[None]],
        on_error: Callable[[Exception], None],
    ) -> None:
        self._work = work
        self._deadline = deadline
        self._clock = clock
        self._create_task = create_task
        self._on_error = on_error
        self._task: asyncio.Task[None] | None = None
        self._timer: asyncio.TimerHandle | None = None
        self._pending = False
        self._closed = False

    def notify(self) -> None:
        """Request another pass, also when a pass is currently awaiting I/O."""
        if self._closed:
            return
        self._pending = True
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._task is None:
            self._task = self._create_task(self._run())

    async def _run(self) -> None:
        failed = False
        try:
            while self._pending and not self._closed:
                self._pending = False
                await self._work()
        except Exception as err:
            failed = True
            self._on_error(err)
        finally:
            self._task = None
            if not failed and not self._closed:
                try:
                    due = self._deadline()
                    if due is not None:
                        self._timer = asyncio.get_running_loop().call_later(
                            max(0.01, (due - self._clock()).total_seconds()),
                            self.notify,
                        )
                except Exception as err:
                    self._on_error(err)

    async def async_close(self) -> None:
        """Stop timers and drain the one worker after its owner fences commands."""
        self._closed = True
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
