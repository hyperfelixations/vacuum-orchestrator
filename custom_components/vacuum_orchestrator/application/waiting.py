"""Explain a waiting job with the same checks dispatch uses, without I/O."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from ..domain.errors import PlanningError
from ..domain.holds import HoldPurpose
from ..domain.planning import WorkUnit
from ..domain.queue import Job, OrchestratorState
from ..domain.readiness import ReadinessReport
from ..domain.requirements import RequirementState
from ..domain.types import JobState, QueueMode
from ..domain.waiting import Blocker, Waiting, rank, robot_blocker_code

if TYPE_CHECKING:
    from .orchestrator import VacuumOrchestrator


def explain_waiting(
    core: VacuumOrchestrator, state: OrchestratorState, job: Job, now: datetime
) -> Waiting | None:
    """Rank every blocker of a waiting job; see dev doc "Wartegrund".

    Robot checks use the last observations, so a read never calls a device.
    """
    if job.state is JobState.QUEUED:
        unit = core._planner.create_plan(job.job_id, job.intent).work_units[0]
    elif job.state is JobState.DISPATCHING and job.active_attempt_id is None:
        unit = state.next_pending_unit(job)
    else:
        return None
    queued = job.state is JobState.QUEUED
    blockers: list[Blocker] = []
    if queued and (hold := state.active_hold(job.job_id, now)) is not None:
        blockers.append(
            Blocker(
                "being_edited"
                if hold.purpose is HoldPurpose.EDIT
                else "pending_confirmation",
                until=hold.expires_at,
            )
        )
    report = core.job_readiness(state, job, operation=unit.operation)
    if report.blocked_room_ids:
        blockers.append(Blocker("room_not_released", room_ids=report.blocked_room_ids))
    blockers.extend(_condition_blockers(job, report))
    blockers.extend(_robot_blockers(core, state, job, unit, report))
    if queued:
        run = state.queue_run
        if run is not None and run.ending:
            blockers.append(Blocker("queue_ending"))
        elif state.mode is QueueMode.IDLE:
            blockers.append(Blocker("queue_idle"))
        elif state.mode is QueueMode.PAUSED:
            blockers.append(Blocker("queue_paused"))
        if job.start_after is not None and now < job.start_after:
            blockers.append(Blocker("start_delayed", until=job.start_after))
    return rank(blockers)


def _condition_blockers(job: Job, report: ReadinessReport) -> list[Blocker]:
    conditions = (*job.intent.required_on, *job.intent.required_off)
    blockers = [
        Blocker(
            "requirement_not_satisfied",
            entity_ids=(*report.failed_on, *report.failed_off),
        ),
        Blocker(
            "requirement_unknown",
            entity_ids=tuple(item for item in report.unknown if item in conditions),
        ),
    ]
    blockers.extend(
        Blocker(
            item.reason,
            room_ids=(item.room_id,) if item.room_id else (),
            entity_ids=(item.entity_id,),
        )
        for item in report.requirements
        if item.state is not RequirementState.READY and item.reason
    )
    return [item for item in blockers if item.entity_ids]


def _robot_blockers(
    core: VacuumOrchestrator,
    state: OrchestratorState,
    job: Job,
    unit: WorkUnit,
    neutral: ReadinessReport,
) -> list[Blocker]:
    """Return nothing as soon as one robot could take the unit now.

    A robot's own conditions are those that fail only when checked for it.
    """
    profiles = tuple(adapter.profile for adapter in core.adapters.values())
    if not profiles:
        return [Blocker("no_robot_configured")]
    capable = [item for item in profiles if unit.operation in item.effective_operations]
    if not capable:
        return [Blocker("operation_unsupported")]
    targets = unit.canonical_targets
    unreachable = tuple(
        target
        for target in targets
        if not any(target in item.capabilities.target_map for item in capable)
    )
    if unreachable:
        return [Blocker("room_unreachable", room_ids=unreachable)]
    reaching = [
        item
        for item in capable
        if all(target in item.capabilities.target_map for target in targets)
    ]
    if not reaching:
        return [Blocker("rooms_not_reachable_together", room_ids=targets)]
    active = state.active_target_sets(excluding_job_id=job.job_id)
    in_use = tuple(target for target in targets if any(target in s for s in active))
    if in_use:
        return [Blocker("rooms_in_use", room_ids=in_use)]
    failing_anyway = {
        (item.entity_id, item.room_id)
        for item in neutral.requirements
        if item.state is not RequirementState.READY
    }
    blockers: list[Blocker] = []
    for profile in reaching:
        report = core.job_readiness(
            state, job, robot_id=profile.robot_id, operation=unit.operation
        )
        failing = [
            item
            for item in report.requirements
            if item.state is not RequirementState.READY
            and (item.entity_id, item.room_id) not in failing_anyway
        ]
        if failing:
            blockers.extend(
                Blocker(
                    item.reason,
                    room_ids=(item.room_id,) if item.room_id else (),
                    entity_ids=(item.entity_id,),
                    robot_ids=(profile.robot_id,),
                )
                for item in failing
                if item.reason
            )
            continue
        try:
            core._selector.assign(
                unit,
                (profile,),
                core.latest_observations,
                state.robot_leases,
                frozenset(state.blocked_robots),
                active,
                profile.robot_id,
                defaults=state.job_defaults,
            )
        except PlanningError as err:
            blockers.append(
                Blocker(robot_blocker_code(err.code), robot_ids=(profile.robot_id,))
            )
            continue
        return []
    return blockers
