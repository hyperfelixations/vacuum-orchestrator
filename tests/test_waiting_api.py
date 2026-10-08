"""Jobs and rooms expose VOI's waiting reasons; clients only format them."""

from dataclasses import replace
from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant

from custom_components.vacuum_orchestrator.domain.execution import RobotRun
from custom_components.vacuum_orchestrator.domain.releases import ReleaseKind
from custom_components.vacuum_orchestrator.domain.types import RobotAvailabilityState
from custom_components.vacuum_orchestrator.runtime import async_get_runtime
from tests.application.test_orchestrator import RecordingAdapter, RecordingBackend
from tests.test_configuration_api import call, configured  # noqa: F401


@pytest.mark.usefixtures("configured")
async def test_jobs_name_their_blockers_and_rooms_their_waiting_jobs(
    hass: HomeAssistant,
) -> None:
    room = (await call(hass, "create_room", name="Office"))["room_id"]
    job = (await call(hass, "create_job", areas=[room]))["job_id"]

    detail = await call(hass, "get_job", job_id=job)
    waiting = detail["waiting"]
    assert (waiting["code"], waiting["room_ids"], waiting["until"]) == (
        "room_not_released",
        [room],
        None,
    )
    assert [item["code"] for item in waiting["blockers"]] == [
        "room_not_released",
        "no_robot_configured",
        "queue_idle",
        "start_delayed",
    ]
    assert waiting["blockers"][-1]["until"] == detail["start_after"]
    assert waiting["blockers"][0] == {
        "code": "room_not_released",
        "until": None,
        "room_ids": [room],
        "entity_ids": [],
        "robot_ids": [],
    }
    assert (await call(hass, "get_queue"))["jobs"][0]["waiting"] == waiting
    assert (await call(hass, "get_room", room_id=room))["waiting_job_ids"] == [job]
    rooms = (await call(hass, "get_rooms"))["rooms"]
    assert [item["waiting_job_ids"] for item in rooms] == [[job]]

    await call(hass, "release_room", room_id=room, kind="permanent")
    assert (await call(hass, "get_room", room_id=room))["waiting_job_ids"] == []
    released = (await call(hass, "get_job", job_id=job))["waiting"]
    assert released["code"] == "no_robot_configured"


@pytest.mark.usefixtures("configured")
async def test_a_job_between_phases_notifies_once_when_its_readiness_changes(
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
    hass.states.async_set("binary_sensor.window", "off")
    job = (
        await call(
            hass,
            "create_job",
            areas=[room],
            mode="vacuum_then_mop",
            required_off=["binary_sensor.window"],
            start=True,
        )
    )["job_id"]
    attempt = core.state.attempts[core.state.jobs[job].active_attempt_id]
    await core.async_confirm_start(attempt.attempt_id)
    adapter.observation = replace(
        adapter.observation, state=RobotAvailabilityState.BUSY
    )
    started = attempt.command_boundary_at
    await core.async_record_robot_run(
        attempt.attempt_id,
        RobotRun(
            "run",
            attempt.source_robot_id,
            observed_start=started,
            observed_end=started + timedelta(minutes=10),
            history_start=started,
            history_end=started + timedelta(minutes=10),
            cleaning_activity_seen=True,
        ),
    )
    assert core.state.jobs[job].active_attempt_id is None
    hass.states.async_set("binary_sensor.window", "on")
    runtime.controller._notify_view_changes(False)
    sequence = core.runtime_sequence

    runtime.controller._notify_view_changes(False)
    assert core.runtime_sequence == sequence
    hass.states.async_set("binary_sensor.window", "off")
    runtime.controller._notify_view_changes(False)
    assert core.runtime_sequence == sequence + 1
    assert core.changed_scopes == {"jobs", "queue"}
    runtime.controller._notify_view_changes(False)
    assert core.runtime_sequence == sequence + 1
