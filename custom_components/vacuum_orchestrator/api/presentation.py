"""Versioned public serialization shared by actions and WebSocket APIs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from ..const import API_VERSION, INTEGRATION_VERSION
from ..domain.execution import ExecutionAttempt
from ..domain.job_defaults import JobDefaults
from ..domain.planning import SettingsResolution
from ..domain.queue import Job, OrchestratorState
from ..domain.readiness import ReadinessReport
from ..domain.rooms import Room
from ..domain.types import JobState
from ..ports.entities import EntityReferences

if TYPE_CHECKING:
    from ..application.orchestrator import VacuumOrchestrator


def view_metadata(core: VacuumOrchestrator) -> dict[str, object]:
    """Identify the committed and transient view a read response belongs to."""
    return {
        "commit_id": core.state.commit_id,
        "runtime_id": core.runtime_id,
        "runtime_sequence": core.runtime_sequence,
    }


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


def present_job(
    job: Job,
    entities: EntityReferences,
    readiness: ReadinessReport | None = None,
    rooms: Mapping[str, Room] | None = None,
    attempts: Mapping[str, ExecutionAttempt] | None = None,
) -> dict[str, object]:
    """Serialize one bounded job record without leaking mutable internals."""
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


def present_queue(
    state: OrchestratorState,
    jobs: list[dict[str, object]],
    *,
    offset: int,
    limit: int,
) -> dict[str, object]:
    """Serialize one stable page of the pending queue."""
    return {
        "api_version": API_VERSION,
        "integration_version": INTEGRATION_VERSION,
        "commit_id": state.commit_id,
        "queue_revision": state.queue_revision,
        "mode": state.mode.value,
        "needs_attention": state.needs_attention,
        "active_count": state.active_job_count,
        "attention_count": state.attention_job_count,
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
            "completed_at": state.queue_run.completed_at.isoformat()
            if state.queue_run.completed_at
            else None,
        },
        "total": len(state.queue),
        "offset": offset,
        "limit": limit,
        "jobs": jobs,
    }
