"""Diagnostic exports omit names, request users, device identifiers and payloads."""

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from homeassistant.components.vacuum.const import VacuumEntityFeature
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from custom_components.vacuum_orchestrator.application.tracing import (
    TraceEvent,
    TraceRecorder,
)
from custom_components.vacuum_orchestrator.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.vacuum_orchestrator.domain.incidents import (
    Incident,
    ObservationTrace,
)
from custom_components.vacuum_orchestrator.domain.intents import JobIntent, TargetRef
from custom_components.vacuum_orchestrator.domain.rooms import RoomBinding
from custom_components.vacuum_orchestrator.domain.types import CleaningMode
from custom_components.vacuum_orchestrator.runtime import async_setup_orchestrator
from tests.test_configuration_api import add_robot, call, configured  # noqa: F401
from tests.test_runtime import _entry


@pytest.mark.usefixtures("configured")
async def test_diagnostics_and_trace_queries_are_bounded_and_sanitized(hass, request):
    entry = request.getfixturevalue("configured")
    core = entry.runtime_data.orchestrator
    room_id = await core.rooms.async_create("Private room name")
    job = await core.async_create_job(
        JobIntent(
            (TargetRef(room_id),),
            CleaningMode.VACUUM,
            name="Private job",
            note="Never export this note",
        )
    )
    export = await async_get_config_entry_diagnostics(hass, entry)
    rendered = json.dumps(export)
    for secret in (room_id, job, "Private room", "Private job", "Never export"):
        assert secret not in rendered
    transitions = [
        record for record in export["traces"] if record["event"] == "job_transition"
    ]
    assert export["jobs"][0]["job_id"] == transitions[0]["job_id"]
    trace = await call(hass, "get_trace", job_id=job, limit=1)
    assert trace["records"][0]["job_id"] == job
    assert trace["incidents"] == []
    for attempt_id, job_id in (("attempt", job), ("other-attempt", "other-job")):
        incident = Incident(
            attempt_id,
            job_id,
            "robot",
            datetime(2026, 9, 1, tzinfo=UTC),
            "busy",
            None,
            "vacuum",
            ObservationTrace(phase="cleaning", monitor_reason="run_timeout"),
        )
        # Incidents ride along with a transition; this one stands alone.
        await core._mutate(
            lambda state, item=incident: replace(
                state.record_incident(item), commit_id=state.commit_id + 1
            )
        )
    (kept,) = (await call(hass, "get_trace", job_id=job))["incidents"]
    assert (kept["event"], kept["attempt_id"], kept["monitor_reason"]) == (
        "robot_observation",
        "attempt",
        "run_timeout",
    )
    assert len((await call(hass, "get_trace"))["incidents"]) == 2
    assert (await call(hass, "get_diagnostics"))["jobs"][0]["state"] == "queued"
    assert not (await call(hass, "get_history"))["runs"]


@pytest.mark.usefixtures("configured")
async def test_diagnostics_show_ignored_segments_without_area_names(hass, request):
    entry = request.getfixturevalue("configured")
    core = entry.runtime_data.orchestrator
    registry = er.async_get(hass)
    vacuum = registry.async_get_or_create("vacuum", "demo", "diagnosed")
    hass.states.async_set(
        vacuum.entity_id,
        "docked",
        {"supported_features": int(VacuumEntityFeature.CLEAN_AREA)},
    )
    registry.async_update_entity_options(
        vacuum.entity_id, "vacuum", {"area_mapping": {"private_kitchen": ["7"]}}
    )
    room_id = await core.rooms.async_create("Kitchen", area_id="private_kitchen")
    robot = await add_robot(hass, vacuum.entity_id, fixed_mode="vacuum")
    await core.rooms.async_update(
        room_id,
        lambda room: replace(
            room, bindings=(RoomBinding(robot, ("private_kitchen", "private_den")),)
        ),
    )
    await hass.async_block_till_done()

    export = await async_get_config_entry_diagnostics(hass, entry)
    rendered = json.dumps(export)
    assert "private_" not in rendered
    assert room_id not in rendered
    reach = export["robots"][0]["reach"]
    assert len(reach) == 1
    assert reach[0]["status"] == "binding_invalid"
    assert len(reach[0]["ignored"]) == 1


def test_trace_ring_drops_oldest_and_keeps_sequence_for_gap_detection():
    trace = TraceRecorder(capacity=2)
    for index in range(3):
        trace.record(
            TraceEvent.JOB, datetime.now(UTC), job_id=str(index), state="queued"
        )
    assert [row["sequence"] for row in trace.snapshot()] == [2, 3]
    trace.snapshot()[0]["state"] = "mutated"
    assert trace.snapshot()[0]["state"] == "queued"
    assert len(trace.snapshot("2")) == 1


async def test_setup_failure_keeps_sanitized_diagnostics(hass, monkeypatch, caplog):
    def fail(*args, **kwargs):
        raise RuntimeError("private setup details")

    monkeypatch.setattr(
        "custom_components.vacuum_orchestrator.runtime.build_adapters", fail
    )
    entry = _entry()
    with pytest.raises(RuntimeError):
        await async_setup_orchestrator(hass, entry)
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["runtime_loaded"] is False
    assert [row["stage"] for row in result["traces"]] == ["starting", "failed"]
    assert "private setup details" not in json.dumps(result)
    assert "private setup details" not in caplog.text


async def test_diagnostics_before_any_setup_are_empty(hass):
    assert (await async_get_config_entry_diagnostics(hass, _entry()))["traces"] == []


@pytest.mark.usefixtures("configured")
async def test_configuration_repairs_clear_when_configuration_is_resolved(
    hass, request
):
    entry = request.getfixturevalue("configured")
    core = entry.runtime_data.orchestrator

    room_id = await core.rooms.async_create("Room", area_id="deleted")
    await core.rooms.async_import_areas({})
    entry.runtime_data.controller.repairs.update()
    assert ir.async_get(hass).async_get_issue("vacuum_orchestrator", f"room_{room_id}")
    await core.rooms.async_update(room_id, lambda room: replace(room, enabled=False))
    entry.runtime_data.controller.repairs.update()
    assert (
        ir.async_get(hass).async_get_issue("vacuum_orchestrator", f"room_{room_id}")
        is None
    )
