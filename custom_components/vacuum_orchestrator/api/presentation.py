"""Versioned public serialization shared by actions and WebSocket APIs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from ..const import API_VERSION, INTEGRATION_VERSION
from ..domain.attention import Attention
from ..domain.completion import JobCompletion
from ..domain.execution import ExecutionAttempt
from ..domain.holds import JobHold
from ..domain.job_defaults import JobDefaults
from ..domain.permissions import Availability, job_actions, queue_actions
from ..domain.planning import SettingsResolution
from ..domain.progress import Progress
from ..domain.queue import Job, OrchestratorState
from ..domain.queue_runs import RunPhase
from ..domain.readiness import ReadinessReport
from ..domain.rooms import Room
from ..domain.setup import SetupStatus
from ..domain.types import JobState
from ..domain.waiting import Blocker, Waiting
from ..ports.entities import EntityReferences

if TYPE_CHECKING:
    from ..application.orchestrator import VacuumOrchestrator


def view_metadata(core: VacuumOrchestrator) -> dict[str, object]:
    """Identify the committed and transient view a read response belongs to."""
    return {
        "commit_id": core.state.commit_id,
        "runtime_id": core.runtime_id,
        "runtime_sequence": core.runtime_sequence,
        "server_time": server_time(core),
    }


def server_time(core: VacuumOrchestrator) -> str:
    """Anchor client countdowns; see dev doc "Serverzeit"."""
    return core.now().isoformat(timespec="microseconds")


def present_settings(resolution: SettingsResolution) -> list[dict[str, object]]:
    """List requested and applied values; `applied` null: no such setting."""
    return [
        {"name": item.name, "requested": item.requested, "applied": item.applied}
        for item in resolution.settings
    ]


def present_job_defaults(defaults: JobDefaults) -> dict[str, object]:
    """Serialize the values new jobs receive when they name none."""
    return {
        "mode": defaults.mode.value,
        "vacuum_power": defaults.vacuum_power.value,
        "mop_intensity": defaults.mop_intensity.value,
        "mop_route": defaults.mop_route.value,
        "passes": defaults.passes,
        "settings_policy": defaults.settings_policy.value,
        "configured": defaults.configured,
    }


def present_references(
    references: tuple[str, ...], entities: EntityReferences
) -> list[str]:
    """Name stored references by their current entity ID where one exists."""
    return [entities.entity_id(reference) or reference for reference in references]


def present_blocker(blocker: Blocker, entities: EntityReferences) -> dict[str, object]:
    """Name one waiting reason with current entity IDs."""
    return {
        "code": blocker.code,
        "until": blocker.until.isoformat() if blocker.until else None,
        "room_ids": list(blocker.room_ids),
        "entity_ids": present_references(blocker.entity_ids, entities),
        "robot_ids": list(blocker.robot_ids),
        "detail": blocker.detail,
    }


def present_waiting(waiting: Waiting, entities: EntityReferences) -> dict[str, object]:
    """Serialize the main reason and the job, execution and queue axes."""

    def blockers(items: tuple[Blocker, ...]) -> list[dict[str, object]]:
        return [present_blocker(item, entities) for item in items]

    robots = waiting.robots
    return {
        **present_blocker(waiting.primary, entities),
        "job": {"ready": not waiting.job, "blockers": blockers(waiting.job)},
        "robots": {
            "state": robots.state.value,
            "candidates": [
                {"robot_id": item.robot_id, "blockers": blockers(item.blockers)}
                for item in robots.candidates
            ],
            "unsuitable": [
                {"robot_id": item.robot_id, "reasons": blockers(item.blockers)}
                for item in robots.unsuitable
            ],
        },
        "queue": None
        if waiting.queue is None
        else {"ready": not waiting.queue, "blockers": blockers(waiting.queue)},
    }


def present_actions(actions: Mapping[str, Availability]) -> dict[str, object]:
    """Serialize which actions are offered now and why not."""
    return {
        name: {
            "available": item.available,
            "reason": item.reason,
            "detail": item.detail,
        }
        for name, item in actions.items()
    }


def present_job_view(
    core: VacuumOrchestrator, job: Job, readiness: ReadinessReport | None = None
) -> dict[str, object]:
    """Serialize a job with its hold, waiting reason, progress and actions."""
    state = core.state
    waiting = core.waiting(job.job_id)
    return present_job(
        job,
        core.entity_references,
        readiness,
        state.room_registry.rooms,
        state.attempts,
        hold=core.job_hold(job.job_id),
        waiting=waiting,
        progress=core.progress(job.job_id),
        completion=state.completion(job.job_id),
        actions=job_actions(state, job, core.now(), waiting),
    )


def present_job(
    job: Job,
    entities: EntityReferences,
    readiness: ReadinessReport | None = None,
    rooms: Mapping[str, Room] | None = None,
    attempts: Mapping[str, ExecutionAttempt] | None = None,
    *,
    hold: JobHold | None = None,
    waiting: Waiting | None = None,
    progress: Progress | None = None,
    completion: JobCompletion | None = None,
    actions: Mapping[str, Availability] | None = None,
) -> dict[str, object]:
    """Serialize one bounded job record; a hold never reveals its token."""
    intent = job.intent
    canceling = (
        attempts.get(job.active_attempt_id)
        if attempts is not None
        and job.state is JobState.CANCELING
        and job.active_attempt_id is not None
        else None
    )
    result: dict[str, object] = {
        "api_version": API_VERSION,
        "job_id": job.job_id,
        "revision": job.revision,
        "state": job.state.value,
        "name": intent.name,
        "areas": [
            room.area_id or room.room_id
            if rooms is not None and (room := rooms.get(target.area_id)) is not None
            else target.area_id
            for target in intent.areas
        ],
        "room_ids": [target.area_id for target in intent.areas],
        "mode": intent.mode.value,
        "vacuum_power": (
            None
            if intent.preferences.vacuum_power is None
            else intent.preferences.vacuum_power.value
        ),
        "mop_intensity": (
            None
            if intent.preferences.mop_intensity is None
            else intent.preferences.mop_intensity.value
        ),
        "mop_route": (
            None
            if intent.preferences.mop_route is None
            else intent.preferences.mop_route.value
        ),
        "passes": intent.passes,
        "all_rooms": intent.all_rooms,
        "reason": intent.reason,
        "note": intent.note,
        "dedupe_key": intent.dedupe_key,
        "required_on": present_references(intent.required_on, entities),
        "required_off": present_references(intent.required_off, entities),
        "settings_policy": intent.settings_policy.value,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
        "start_after": job.start_after.isoformat() if job.start_after else None,
        "hold": None
        if hold is None
        else {"purpose": hold.purpose.value, "expires_at": hold.expires_at.isoformat()},
        "waiting": None if waiting is None else present_waiting(waiting, entities),
        "progress": None
        if progress is None
        else {
            "operation": progress.operation.value,
            "phase": progress.phase,
            "phases": progress.phases,
            "started_at": progress.started_at.isoformat()
            if progress.started_at
            else None,
            "robot_id": progress.robot_id,
            "phase_percent": progress.phase_percent,
            "percent": progress.percent,
            "fault": None
            if progress.fault is None
            else {
                "codes": list(progress.fault.codes),
                "since": progress.fault.since.isoformat(),
                "fails_at": progress.fault.fails_at.isoformat(),
            },
        },
        "completion": None
        if completion is None
        else {
            "quality": completion.quality.value,
            "deviations": list(completion.deviations),
        },
        "actions": None if actions is None else present_actions(actions),
        "active_attempt_id": job.active_attempt_id,
        "origin": {
            "kind": job.provenance.kind.value,
            "template_id": job.provenance.template_id,
        },
        "retries_job_id": job.retries_job_id,
        "failure_code": job.failure_code,
        "after_cancel": None
        if canceling is None
        else "return_to_dock"
        if canceling.return_to_dock
        else "stay",
    }
    if readiness is not None:
        result["readiness"] = {
            "state": readiness.state.value,
            "failed_on": present_references(readiness.failed_on, entities),
            "failed_off": present_references(readiness.failed_off, entities),
            "unknown": present_references(readiness.unknown, entities),
            "reason_codes": list(readiness.reason_codes),
            "blocked_room_ids": list(readiness.blocked_room_ids),
            "requirements": [
                {
                    "entity_id": item.entity_id,
                    "state": item.state.value,
                    "reason": item.reason,
                    "room_id": item.room_id,
                    "robot_id": item.robot_id,
                    "operation": item.operation.value if item.operation else None,
                }
                for item in readiness.requirements
            ],
        }
    return result


def present_attention(entries: tuple[Attention, ...]) -> list[dict[str, object]]:
    """Serialize what a person has to act on now."""
    return [
        {
            "kind": entry.kind.value,
            "robot_id": entry.robot_id,
            "job_id": entry.job_id,
            "codes": list(entry.codes),
            "operations": sorted(item.value for item in entry.operations),
            "since": entry.since.isoformat() if entry.since else None,
            "fails_at": entry.fails_at.isoformat() if entry.fails_at else None,
        }
        for entry in entries
    ]


def present_queue(
    state: OrchestratorState,
    jobs: list[dict[str, object]],
    *,
    offset: int,
    limit: int,
    phase: RunPhase,
    attention: tuple[Attention, ...],
) -> dict[str, object]:
    """Serialize one stable page of the pending queue."""
    return {
        "api_version": API_VERSION,
        "integration_version": INTEGRATION_VERSION,
        "commit_id": state.commit_id,
        "queue_revision": state.queue_revision,
        "mode": state.mode.value,
        "needs_attention": bool(attention),
        "active_count": state.active_job_count,
        "attention_count": len(attention),
        "attention": present_attention(attention),
        "recovery_targets": [
            {
                "robot_id": state.robot_leases[source_id].robot_id
                if source_id in state.robot_leases
                else source_id,
                "reason": reason,
            }
            for source_id, reason in state.blocked_robots.items()
        ],
        "queue_grace_seconds": state.queue_grace_seconds,
        "start_delay_seconds": state.start_delay_seconds,
        "job_defaults": present_job_defaults(state.job_defaults),
        "queue_run": None
        if state.queue_run is None
        else {
            "run_id": state.queue_run.run_id,
            "started_at": state.queue_run.started_at.isoformat(),
            "active": state.queue_run.active,
            "idle_since": state.queue_run.idle_since.isoformat()
            if state.queue_run.idle_since
            else None,
            "deadline": state.queue_run.deadline.isoformat()
            if state.queue_run.deadline
            else None,
            "ending": state.queue_run.ending,
            "phase": phase.value,
            "ends_at": state.queue_run.deadline.isoformat()
            if phase is RunPhase.STANDBY and state.queue_run.deadline
            else None,
            "completed_at": state.queue_run.completed_at.isoformat()
            if state.queue_run.completed_at
            else None,
        },
        "actions": present_actions(queue_actions(phase)),
        "total": len(state.queue),
        "offset": offset,
        "limit": limit,
        "jobs": jobs,
    }


def present_setup(status: SetupStatus) -> dict[str, object]:
    """Serialize the setup facts the assistant shows."""
    return {
        "api_version": API_VERSION,
        "assistant_pending": status.assistant_pending,
        "completed_at": status.completed_at.isoformat()
        if status.completed_at
        else None,
        "steps": {
            "robots": {"robot_ids": list(status.robot_ids)},
            "rooms": {
                "room_ids": list(status.room_ids),
                "unreachable_room_ids": list(status.unreachable_room_ids),
            },
            "defaults": {"configured": status.defaults_configured},
            "queue": {
                "grace_seconds": status.grace_seconds,
                "start_delay_seconds": status.start_delay_seconds,
            },
        },
    }
