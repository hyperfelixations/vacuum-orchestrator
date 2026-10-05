"""Run both adapter dispatch phases the way RobotSession sequences them."""

from custom_components.vacuum_orchestrator.domain.planning import (
    DispatchAssignment,
    WorkUnit,
)
from custom_components.vacuum_orchestrator.ports.robot import RobotAdapter


async def dispatch(
    adapter: RobotAdapter, unit: WorkUnit, assignment: DispatchAssignment
) -> None:
    await adapter.async_prepare(unit, assignment)
    await adapter.async_start(unit, assignment)
