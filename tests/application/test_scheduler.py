"""Coalesced scheduler wakeups, deadline wiring and controlled shutdown."""

import asyncio
from datetime import UTC, datetime, timedelta

from custom_components.vacuum_orchestrator.application.scheduler import WakeupScheduler


async def test_burst_coalesces_and_notification_during_await_is_not_lost() -> None:
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []
    errors = []

    async def work():
        calls.append("work")
        if len(calls) == 1:
            entered.set()
            await release.wait()
        else:
            finished.set()

    scheduler = WakeupScheduler(
        work,
        lambda: None,
        lambda: datetime.now(UTC),
        asyncio.create_task,
        errors.append,
    )
    for _ in range(20):
        scheduler.notify()
    await entered.wait()
    for _ in range(20):
        scheduler.notify()
    release.set()
    await finished.wait()
    await asyncio.sleep(0)
    assert calls == ["work", "work"]
    assert not errors
    await scheduler.async_close()
    scheduler.notify()
    assert calls == ["work", "work"]


async def test_close_cancels_running_work_and_clears_deadline() -> None:
    entered, stopped = asyncio.Event(), asyncio.Event()

    async def work():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    scheduler = WakeupScheduler(
        work,
        lambda: datetime.now(UTC) + timedelta(hours=1),
        lambda: datetime.now(UTC),
        asyncio.create_task,
        lambda _: None,
    )
    scheduler.notify()
    await entered.wait()
    await scheduler.async_close()
    assert stopped.is_set()
    assert scheduler._task is None
    assert scheduler._timer is None


async def test_worker_error_is_reported_once_without_deadline_spin() -> None:
    errors, tasks = [], []

    async def fail():
        raise RuntimeError("test")

    def create(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    scheduler = WakeupScheduler(
        fail,
        lambda: datetime.now(UTC),
        lambda: datetime.now(UTC),
        create,
        errors.append,
    )
    scheduler.notify()
    await tasks[0]
    assert len(errors) == 1
    assert scheduler._timer is None
    await scheduler.async_close()


async def test_deadline_timer_is_replaced_on_an_earlier_wakeup() -> None:
    tasks = []

    async def work():
        pass

    def create(coro):
        task = asyncio.create_task(coro)
        tasks.append(task)
        return task

    scheduler = WakeupScheduler(
        work,
        lambda: datetime.now(UTC) + timedelta(hours=1),
        lambda: datetime.now(UTC),
        create,
        lambda _: None,
    )
    scheduler.notify()
    await tasks[-1]
    timer = scheduler._timer
    assert timer is not None
    scheduler.notify()
    assert timer.cancelled()
    await tasks[-1]
    assert scheduler._timer is not timer
    await scheduler.async_close()
    assert scheduler._timer is None
