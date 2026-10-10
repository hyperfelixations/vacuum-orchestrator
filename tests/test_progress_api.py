"""Started jobs carry their progress; a new percentage notifies job views."""

from dataclasses import replace
from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant

from custom_components.vacuum_orchestrator.api.websocket import (
    TYPE_JOBS_LIST,
    websocket_jobs_list,
)
from custom_components.vacuum_orchestrator.domain.faults import (
    Fault,
    FaultScope,
    FaultSource,
)
from custom_components.vacuum_orchestrator.domain.releases import ReleaseKind
from custom_components.vacuum_orchestrator.domain.types import RobotAvailabilityState
from custom_components.vacuum_orchestrator.runtime import async_get_runtime
from tests.application.test_orchestrator import RecordingAdapter, RecordingBackend
from tests.test_configuration_api import call, configured  # noqa: F401
from tests.test_websocket import Connection


@pytest.mark.usefixtures("configured")
async def test_a_running_job_reports_progress_and_changes_notify_views(
    hass: HomeAssistant,
) -> None:
    runtime = async_get_runtime(hass)
    await runtime.controller.scheduler.async_close()
    core = runtime.orchestrator
    room = (await call(hass, "create_room", name="Office"))["room_id"]
    adapter = RecordingAdapter(RecordingBackend(), "robot", targets=(room,))
    adapter._persisted_attempt_state = lambda: None
    await core.async_replace_adapters({"robot": adapter})
    await core.rooms.async_grant(room, ReleaseKind.PERMANENT)
    job = (await call(hass, "create_job", areas=[room], mode="vacuum_then_mop"))[
        "job_id"
    ]
    assert (await call(hass, "get_job", job_id=job))["progress"] is None
    await call(hass, "start_job", job_id=job)
    started = core.state.attempts[core.state.jobs[job].active_attempt_id]
    await core.async_confirm_start(started.attempt_id)
    runtime.controller._notify_view_changes(False)
    sequence = core.runtime_sequence

    adapter.observation = replace(
        adapter.observation,
        state=RobotAvailabilityState.BUSY,
        cleaning_active=True,
        clean_percent=50,
        clean_percent_at=core.state.attempts[started.attempt_id].observed_start_at
        + timedelta(seconds=30),
    )
    await core.async_process_robot_observation("robot")
    runtime.controller._notify_view_changes(False)
    assert core.runtime_sequence == sequence + 1
    assert "jobs" in core.changed_scopes

    progress = (await call(hass, "get_job", job_id=job))["progress"]
    assert progress == {
        "operation": "vacuum",
        "phase": 1,
        "phases": 2,
        "started_at": progress["started_at"],
        "robot_id": "robot",
        "phase_percent": 50,
        "percent": 25,
        "fault": None,
    }
    assert progress["started_at"] is not None
    connection = Connection()
    websocket_jobs_list(
        hass,
        connection,
        {"id": 1, "type": TYPE_JOBS_LIST, "offset": 0, "limit": 10},
    )
    await hass.async_block_till_done()
    assert connection.results[0][1]["jobs"][0]["progress"] == progress


@pytest.mark.usefixtures("configured")
async def test_a_job_in_a_fault_window_shows_when_it_fails(
    hass: HomeAssistant,
) -> None:
    runtime = async_get_runtime(hass)
    await runtime.controller.scheduler.async_close()
    core = runtime.orchestrator
    room = (await call(hass, "create_room", name="Office"))["room_id"]
    adapter = RecordingAdapter(RecordingBackend(), "robot", targets=(room,))
    adapter._persisted_attempt_state = lambda: None
    await core.async_replace_adapters({"robot": adapter})
    await core.rooms.async_grant(room, ReleaseKind.PERMANENT)
    job = (await call(hass, "create_job", areas=[room], mode="vacuum"))["job_id"]
    await call(hass, "start_job", job_id=job)
    started = core.state.attempts[core.state.jobs[job].active_attempt_id]
    await core.async_confirm_start(started.attempt_id)

    jammed = Fault("main_brush_jammed", FaultSource.ROBOT, FaultScope.VACUUM)
    adapter.observation = replace(
        adapter.observation,
        state=RobotAvailabilityState.BUSY,
        observed_at=core.now(),
        cleaning_active=False,
        faults=(jammed,),
    )
    await core.async_process_robot_observation("robot")

    since = core.state.attempts[started.attempt_id].fault_since
    assert since is not None
    fails_at = (since + timedelta(seconds=900)).isoformat()
    progress = (await call(hass, "get_job", job_id=job))["progress"]
    assert progress["fault"] == {
        "codes": ["main_brush_jammed"],
        "since": since.isoformat(),
        "fails_at": fails_at,
    }
    (entry,) = (await call(hass, "get_queue"))["attention"]
    assert (entry["kind"], entry["job_id"], entry["fails_at"]) == (
        "device_fault",
        job,
        fails_at,
    )
