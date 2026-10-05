"""Tests for serial physical command execution and fencing."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.vacuum_orchestrator.application.robot_session import (
    RobotOwnershipRegistry,
    RobotSession,
)
from custom_components.vacuum_orchestrator.domain.errors import (
    ConflictError,
    StaleCommandError,
)
from custom_components.vacuum_orchestrator.ports.command_scope import (
    check_command_authorization,
    command_guard,
)


async def _noop() -> None:
    await asyncio.sleep(0)


async def test_cancel_fence_rejects_delayed_dispatch() -> None:
    session = RobotSession("roborock:registry-1")
    ticket = session.reserve("attempt-1")
    session.fence(ticket.generation + 1, needs_attention=False)

    with pytest.raises(StaleCommandError, match="stale_robot_generation"):
        await session.dispatch(ticket, _noop, _noop, _noop)


async def test_cancel_is_serialized_after_in_flight_start() -> None:
    session = RobotSession("roborock:registry-1")
    ticket = session.reserve("attempt-1")
    entered = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def start() -> None:
        calls.append("start_enter")
        entered.set()
        await release.wait()
        calls.append("start_exit")

    dispatch_task = asyncio.create_task(session.dispatch(ticket, _noop, _noop, start))
    await entered.wait()
    cancel_ticket = session.fence(ticket.generation + 1, needs_attention=False)

    async def stop() -> None:
        calls.append("stop")

    cancel_task = asyncio.create_task(session.cancel(cancel_ticket, stop))
    await asyncio.sleep(0)
    assert calls == ["start_enter"]
    release.set()
    await dispatch_task
    await cancel_task

    assert calls == ["start_enter", "start_exit", "stop"]


def test_source_robot_has_one_fleet_owner() -> None:
    registry = RobotOwnershipRegistry()
    registry.claim("roborock:registry-1", "fleet-a")

    with pytest.raises(ConflictError, match="source_robot_already_owned"):
        registry.claim("roborock:registry-1", "fleet-b")

    registry.release("roborock:registry-1", "fleet-a")
    registry.claim("roborock:registry-1", "fleet-b")


async def test_session_rejects_wrong_source_owner_and_nonmonotonic_fence() -> None:
    session = RobotSession("source-1")
    ticket = session.reserve("attempt-1")

    with pytest.raises(ConflictError, match="already_owned"):
        session.reserve("attempt-2")
    with pytest.raises(ConflictError, match="not_monotonic"):
        session.fence(0, needs_attention=False)
    wrong_ticket = type(ticket)("other-source", ticket.generation, "attempt-1")
    with pytest.raises(StaleCommandError, match="source_robot_mismatch"):
        await session.dispatch(wrong_ticket, _noop, _noop, _noop)


async def test_attention_fence_blocks_commands_and_release_checks_owner() -> None:
    session = RobotSession("source-1")
    session.reserve("attempt-1")

    assert session.generation == 0
    assert session.needs_attention is False
    with pytest.raises(ConflictError, match="owner_mismatch"):
        session.release("attempt-2")
    session.release("attempt-1")
    blocked = session.fence(1, needs_attention=True)
    assert session.needs_attention is True
    with pytest.raises(StaleCommandError, match="robot_needs_attention"):
        await session.cancel(blocked, lambda: asyncio.sleep(0))


def test_ownership_release_requires_matching_fleet() -> None:
    registry = RobotOwnershipRegistry()
    registry.claim("source-1", "fleet-1")

    with pytest.raises(ConflictError, match="source_robot_owner_mismatch"):
        registry.release("source-1", "fleet-2")
    registry.release("missing", "fleet-1")


async def test_command_scope_rechecks_preconditions_and_clears_after_failure() -> None:
    session = RobotSession("source")
    ticket = session.reserve("attempt")
    allowed = True
    calls = []

    def guard() -> None:
        if not allowed:
            raise ConflictError("readiness_changed")

    async def prepare() -> None:
        nonlocal allowed
        check_command_authorization()
        calls.append("settings")
        allowed = False
        check_command_authorization()
        calls.append("unreachable")

    async def start() -> None:
        calls.append("start")

    with pytest.raises(ConflictError, match="readiness_changed"):
        await session.dispatch(ticket, prepare, _noop, start, guard)
    assert calls == ["settings"]
    assert command_guard.get() is None
    check_command_authorization()


async def test_boundary_runs_after_prepare_and_rechecks_before_start() -> None:
    session = RobotSession("source")
    ticket = session.reserve("attempt")
    calls: list[str] = []

    async def prepare() -> None:
        calls.append("prepare")

    async def boundary() -> None:
        calls.append("boundary")
        check_command_authorization()

    async def start() -> None:
        calls.append("start")

    await session.dispatch(ticket, prepare, boundary, start)
    assert calls == ["prepare", "boundary", "start"]


async def test_fence_during_boundary_prevents_start() -> None:
    session = RobotSession("source")
    ticket = session.reserve("attempt")
    calls: list[str] = []

    async def boundary() -> None:
        calls.append("boundary")
        session.fence(ticket.generation + 1, needs_attention=False)

    async def start() -> None:
        calls.append("start")

    with pytest.raises(StaleCommandError, match="stale_robot_generation"):
        await session.dispatch(ticket, _noop, boundary, start)
    assert calls == ["boundary"]
