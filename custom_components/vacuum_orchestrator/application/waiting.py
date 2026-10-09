"""Explain a waiting job with the same checks dispatch uses, without I/O."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from ..domain.dispatching import eligibility
from ..domain.holds import HoldPurpose
from ..domain.planning import WorkUnit
from ..domain.queue import Job, OrchestratorState
from ..domain.readiness import ReadinessReport
from ..domain.requirements import RequirementState
from ..domain.types import JobState, QueueMode
from ..domain.waiting import (
    Blocker,
    RobotBlockers,
    Robots,
    RobotsState,
    Waiting,
    robot_blocker,
    robot_blockers,
    room_blocker_code,
    waiting,
)

if TYPE_CHECKING:
    from .orchestrator import VacuumOrchestrator


def explain_waiting(
    core: VacuumOrchestrator, state: OrchestratorState, job: Job, now: datetime
) -> Waiting | None:
    """Explain a waiting job on three axes; see dev doc "Wartegrund".

    Robot checks use the last observations, so a read never calls a device.
    """
    if job.state is JobState.QUEUED:
        unit = core._planner.create_plan(job.job_id, job.intent).work_units[0]
    elif job.state is JobState.DISPATCHING and job.active_attempt_id is None:
        unit = state.next_pending_unit(job)
    else:
        return None
    queued = job.state is JobState.QUEUED
    neutral = core.job_readiness(state, job, operation=unit.operation)
    return waiting(
        _job_blockers(state, job, unit, neutral, now, queued=queued),
        _robots(core, state, job, unit, neutral),
        _queue_blockers(state, job, now) if queued else None,
    )


def awaiting_release(state: OrchestratorState, report: ReadinessReport) -> list[str]:
    """Return the blocked rooms a release alone would admit."""
    return [
        room_id
        for room_id in report.blocked_room_ids
        if room_blocker_code(state.room_registry.resolve(room_id))
        == "room_not_released"
    ]


def _job_blockers(
    state: OrchestratorState,
    job: Job,
    unit: WorkUnit,
    report: ReadinessReport,
    now: datetime,
    *,
    queued: bool,
) -> list[Blocker]:
    blockers: list[Blocker] = []
    # A stored hold blocks until its expiry is committed, as in dispatch.
    if queued and (hold := state.job_holds.get(job.job_id)) is not None:
        blockers.append(
            Blocker(
                "being_edited"
                if hold.purpose is HoldPurpose.EDIT
                else "pending_confirmation",
                until=hold.expires_at,
            )
        )
    rooms: dict[str, list[str]] = {}
    for room_id in report.blocked_room_ids:
        code = room_blocker_code(state.room_registry.resolve(room_id))
        rooms.setdefault(code, []).append(room_id)
    blockers.extend(Blocker(code, room_ids=tuple(ids)) for code, ids in rooms.items())
    blockers.extend(_condition_blockers(job, report))
    active = state.active_target_sets(excluding_job_id=job.job_id)
    targets = unit.canonical_targets
    in_use = tuple(target for target in targets if any(target in s for s in active))
    if in_use:
        blockers.append(Blocker("rooms_in_use", room_ids=in_use))
    return blockers


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


def _robots(
    core: VacuumOrchestrator,
    state: OrchestratorState,
    job: Job,
    unit: WorkUnit,
    neutral: ReadinessReport,
) -> Robots:
    """Check every robot like dispatch; its own conditions count as momentary.

    A robot's own conditions are those that fail only when checked for it.
    """
    profiles = tuple(adapter.profile for adapter in core.adapters.values())
    if not profiles:
        return Robots(RobotsState.NO_ROBOT_CONFIGURED)
    failing_anyway = {
        (item.entity_id, item.room_id)
        for item in neutral.requirements
        if item.state is not RequirementState.READY
    }
    candidates: list[RobotBlockers] = []
    unsuitable: list[RobotBlockers] = []
    for profile in profiles:
        reasons = eligibility(
            unit,
            profile,
            core.latest_observations.get(profile.robot_id),
            state.robot_leases,
            frozenset(state.blocked_robots),
            state.job_defaults,
        )
        structural = [item for item in reasons if item.structural]
        if structural:
            unsuitable.append(
                robot_blockers(profile.robot_id, map(robot_blocker, structural))
            )
            continue
        report = core.job_readiness(
            state, job, robot_id=profile.robot_id, operation=unit.operation
        )
        conditions = [
            Blocker(
                item.reason,
                room_ids=(item.room_id,) if item.room_id else (),
                entity_ids=(item.entity_id,),
            )
            for item in report.requirements
            if item.state is not RequirementState.READY
            and (item.entity_id, item.room_id) not in failing_anyway
            and item.reason
        ]
        candidates.append(
            robot_blockers(
                profile.robot_id, [*map(robot_blocker, reasons), *conditions]
            )
        )
    if not candidates:
        robots_state = RobotsState.NO_CAPABLE_ROBOT
    elif any(not item.blockers for item in candidates):
        robots_state = RobotsState.READY
    else:
        robots_state = RobotsState.NO_ROBOT_READY
    return Robots(robots_state, tuple(candidates), tuple(unsuitable))


def _queue_blockers(state: OrchestratorState, job: Job, now: datetime) -> list[Blocker]:
    blockers: list[Blocker] = []
    run = state.queue_run
    if run is not None and run.ending:
        blockers.append(Blocker("queue_ending"))
    elif state.mode is QueueMode.IDLE:
        blockers.append(Blocker("queue_idle"))
    elif state.mode is QueueMode.PAUSED:
        blockers.append(Blocker("queue_paused"))
    if job.start_after is not None and now < job.start_after:
        blockers.append(Blocker("start_delayed", until=job.start_after))
    return blockers
