"""Read-only execution explanations using the same dispatch policies."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..domain.dispatching import RobotSelector
from ..domain.errors import ConflictError, PlanningError
from ..domain.planning import Planner, SettingsResolution
from ..domain.readiness import ReadinessReport
from ..domain.types import OperationKind

if TYPE_CHECKING:
    from .orchestrator import VacuumOrchestrator


@dataclass(frozen=True, slots=True)
class RobotExplanation:
    """Current conditions for one robot and one planned operation."""

    robot_id: str
    operation: OperationKind
    readiness: ReadinessReport
    eligibility_reason: str | None
    settings: SettingsResolution | None


async def explain_job(
    core: VacuumOrchestrator, job_id: str
) -> tuple[RobotExplanation, ...]:
    """Observe without dispatching, writing state or reserving a robot."""
    observations = await core._observe_robots()
    state = core.state
    job = state.jobs.get(job_id)
    if job is None:
        raise ConflictError("unknown_job")
    plan = (
        state.plans[job.plan_id]
        if job.plan_id
        else Planner().create_plan(job_id, job.intent)
    )
    results = []
    for unit in plan.work_units:
        for robot_id, adapter in core.adapters.items():
            report = core.readiness_for_job(
                job_id, robot_id=robot_id, operation=unit.operation
            )
            reason = None
            settings = None
            try:
                assignment = RobotSelector().assign(
                    unit,
                    (adapter.profile,),
                    observations,
                    state.robot_leases,
                    frozenset(state.blocked_robots),
                    state.active_target_sets(excluding_job_id=job_id),
                    robot_id,
                    defaults=state.job_defaults,
                )
                settings = assignment.settings
            except PlanningError as err:
                reason = err.code
            results.append(
                RobotExplanation(robot_id, unit.operation, report, reason, settings)
            )
    return tuple(results)
