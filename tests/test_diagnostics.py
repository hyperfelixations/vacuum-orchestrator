"""Diagnostic exports omit names, request users, device identifiers and payloads."""

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from homeassistant.helpers import issue_registry as ir

from custom_components.vacuum_orchestrator.application.tracing import (
    TraceEvent,
    TraceRecorder,
)
from custom_components.vacuum_orchestrator.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.vacuum_orchestrator.domain.intents import JobIntent, TargetRef
from custom_components.vacuum_orchestrator.domain.types import CleaningMode
from tests.test_configuration_api import call, configured  # noqa: F401


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
    assert export["jobs"][0]["job_id"] == export["traces"][0]["job_id"]
    trace = await call(hass, "get_trace", job_id=job, limit=1)
    assert trace["records"][0]["job_id"] == job
    assert (await call(hass, "get_diagnostics"))["jobs"][0]["state"] == "queued"
    assert not (await call(hass, "get_history"))["runs"]


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
