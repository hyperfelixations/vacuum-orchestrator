"""Versioned public serialization shared by actions and WebSocket APIs."""

from __future__ import annotations

from ..const import API_VERSION
from ..domain.queue import Job, OrchestratorState
from ..domain.readiness import ReadinessReport


def present_job(
    job: Job, readiness: ReadinessReport | None = None
) -> dict[str, object]:
    """Serialize one bounded job record without leaking mutable internals."""
    intent = job.intent
    result: dict[str, object] = {
        "api_version": API_VERSION,
        "job_id": job.job_id,
        "revision": job.revision,
        "state": job.state.value,
        "name": intent.name,
        "areas": [target.area_id for target in intent.areas],
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
        "source": intent.source,
        "reason": intent.reason,
        "note": intent.note,
        "dedupe_key": intent.dedupe_key,
        "required_on": list(intent.required_on),
        "required_off": list(intent.required_off),
        "settings_policy": intent.settings_policy.value,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
        "active_attempt_id": job.active_attempt_id,
        "retries_job_id": job.retries_job_id,
        "failure_code": job.failure_code,
    }
    if readiness is not None:
        result["readiness"] = {
            "state": readiness.state.value,
            "failed_on": list(readiness.failed_on),
            "failed_off": list(readiness.failed_off),
            "unknown": list(readiness.unknown),
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
        "commit_id": state.commit_id,
        "queue_revision": state.queue_revision,
        "mode": state.mode.value,
        "needs_attention": state.needs_attention,
        "total": len(state.queue),
        "offset": offset,
        "limit": limit,
        "jobs": jobs,
    }
