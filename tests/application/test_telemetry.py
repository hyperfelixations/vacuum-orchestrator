"""Execution telemetry is bounded, correlated and independent of control flow."""

import asyncio
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.vacuum_orchestrator.adapters.public_values import (
    ADAPTER_VALUES,
)
from custom_components.vacuum_orchestrator.adapters.settings import async_set_option
from custom_components.vacuum_orchestrator.api.telemetry import command_trace
from custom_components.vacuum_orchestrator.application.tracing import (
    TraceEvent,
    TraceRecorder,
)
from custom_components.vacuum_orchestrator.domain.errors import (
    ConflictError,
    StorageIntegrityError,
)
from custom_components.vacuum_orchestrator.domain.types import (
    QueueMode,
    RobotAvailabilityState,
)
from custom_components.vacuum_orchestrator.infrastructure.telemetry import LoggingSink
from custom_components.vacuum_orchestrator.ports.telemetry import (
    adapter_reporter,
    report_adapter,
)
from tests.application.test_orchestrator import (
    RecordingAdapter,
    RecordingBackend,
    _intent,
    _orchestrator,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_sink_and_export_share_private_identifier_tokens(caplog):
    sink = LoggingSink()
    trace = TraceRecorder(sink=sink)
    with caplog.at_level(logging.DEBUG, logger="custom_components.vacuum_orchestrator"):
        trace.record(
            TraceEvent.JOB,
            NOW,
            job_id="private-job",
            robot_id="private-device",
            state="queued",
        )
    exported = sink.sanitize(trace.snapshot()[0])
    payload = json.loads(caplog.records[-1].getMessage().split(" ", 1)[1])
    assert payload["job_id"] == exported["job_id"]
    assert payload["robot_id"] == sink.pseudonym("private-device")
    assert "private-job" not in caplog.text
    assert "private-device" not in caplog.text


def test_broken_sink_cannot_break_trace_or_mutation():
    class BrokenSink:
        def emit(self, record):
            raise RuntimeError("do not propagate")

    trace = TraceRecorder(capacity=2, sink=BrokenSink())
    for _ in range(3):
        trace.record(TraceEvent.JOB, NOW, state="queued")
    assert trace.sequence == 3
    assert len(trace.snapshot()) == 2
    assert trace.sink_failures == 3


def test_untrusted_text_never_reaches_logs_or_export(caplog):
    sink = LoggingSink()
    trace = TraceRecorder(sink=sink)
    with caplog.at_level(logging.DEBUG, logger="custom_components.vacuum_orchestrator"):
        trace.record(
            TraceEvent.OBSERVATION,
            NOW,
            state="secret-state",
            reason="secret_reason",
            robot_id="secret-device",
        )
    assert "secret" not in caplog.text
    exported = json.dumps(sink.sanitize(trace.snapshot()[0]))
    assert "secret" not in exported


def test_repeated_observations_are_summarized_on_change(caplog):
    sink = LoggingSink()
    trace = TraceRecorder(sink=sink)
    with caplog.at_level(logging.DEBUG, logger="custom_components.vacuum_orchestrator"):
        for _ in range(100):
            trace.record(
                TraceEvent.OBSERVATION, NOW, robot_id="device", state="available"
            )
        trace.record(
            TraceEvent.OBSERVATION, NOW, robot_id="device", state="unavailable"
        )
    assert len(caplog.records) == 2
    last = json.loads(caplog.records[-1].getMessage().split(" ", 1)[1])
    assert last["suppressed"] == 99
    assert trace.sequence == 101


async def test_concurrent_requests_keep_distinct_ids_and_reset_context():
    trace = TraceRecorder()

    async def command(name):
        with command_trace(trace, name):
            await asyncio.sleep(0)
            trace.record(TraceEvent.JOB, NOW, state="queued")

    await asyncio.gather(command("create_job"), command("update_job"))
    rows = trace.snapshot()
    received = [row for row in rows if row["stage"] == "received"]
    assert len({row["request_id"] for row in received}) == 2
    for row in received:
        related = [item for item in rows if item["request_id"] == row["request_id"]]
        assert [item["stage"] for item in related] == ["received", None, "returned"]
    trace.record(TraceEvent.JOB, NOW)
    assert trace.snapshot()[-1]["request_id"] is None


def test_exception_frames_omit_message_absolute_paths_and_locals(caplog):
    trace = TraceRecorder(sink=LoggingSink())
    with caplog.at_level(logging.ERROR):
        try:
            raise RuntimeError("secret-token-and-private-path")
        except RuntimeError as err:
            trace.record(TraceEvent.ERROR, NOW, error=err)
    assert "secret-token" not in caplog.text
    row = json.loads(caplog.records[-1].getMessage().split(" ", 1)[1])
    assert row["exception_type"] == "RuntimeError"
    assert row["frames"].startswith("external:")
    assert "exc_info" not in row


def test_repetition_is_bounded_refreshes_and_keeps_quality_changes(caplog, monkeypatch):
    moment = [0.0]
    monkeypatch.setattr(
        "custom_components.vacuum_orchestrator.infrastructure.telemetry.monotonic",
        lambda: moment[0],
    )
    sink = LoggingSink()
    trace = TraceRecorder(sink=sink)
    with caplog.at_level(logging.DEBUG):
        trace.record(TraceEvent.OBSERVATION, NOW, robot_id="robot", quality="derived")
        trace.record(TraceEvent.OBSERVATION, NOW, robot_id="robot", quality="confirmed")
        assert len(caplog.records) == 2
        moment[0] = 61
        trace.record(TraceEvent.OBSERVATION, NOW, robot_id="robot", quality="confirmed")
        assert len(caplog.records) == 3
        for index in range(600):
            trace.record(TraceEvent.BLOCKED, NOW, job_id=str(index))
        assert len(sink._last) == 512
    assert len(trace.snapshot()) == 512


async def test_failed_storage_cannot_log_a_committed_state_or_issue_device_io(caplog):
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    core = await _orchestrator(backend, adapter)
    core.trace.sink = LoggingSink()
    job = await core.async_create_job(_intent())
    before = core.state.commit_id
    backend.swallow_next_save = True
    caplog.clear()
    with caplog.at_level(logging.DEBUG), pytest.raises(StorageIntegrityError):
        await core.async_start_job(job)
    assert not adapter.dispatches
    rows = [
        json.loads(row.getMessage().split(" ", 1)[1])
        for row in caplog.records
        if row.getMessage().startswith("VOI ")
    ]
    storage = [row for row in rows if row["event"] == "storage"]
    assert [row["stage"] for row in storage] == ["committing", "failed"]
    assert all(row["commit_id"] == before for row in storage)


async def test_adapter_events_share_attempt_and_context_is_restored(monkeypatch):
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    core = await _orchestrator(backend, adapter)
    original = adapter.async_start

    async def dispatch(unit, assignment):
        report_adapter(TraceEvent.SETTING, "confirmed", "cleaning_mode")
        report_adapter(TraceEvent.PHYSICAL, "requested")
        await original(unit, assignment)

    monkeypatch.setattr(adapter, "async_start", dispatch)
    job = await core.async_create_job(_intent())
    with core.trace.request() as request_id:
        await core.async_start_job(job)
    records = [
        row
        for row in core.trace.snapshot()
        if row["event"] in {"setting", "physical_command"}
    ]
    assert {row["request_id"] for row in records} == {request_id}
    assert {row["attempt_id"] for row in records} == {
        core.state.jobs[job].active_attempt_id
    }
    assert {row["job_id"] for row in records} == {job}
    assert adapter_reporter.get() is None


async def test_availability_logs_only_changes_even_with_debug_disabled(caplog):
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    core = await _orchestrator(backend, adapter)
    core.trace.sink = LoggingSink()
    with caplog.at_level(logging.INFO, logger="custom_components.vacuum_orchestrator"):
        for state in (RobotAvailabilityState.UNAVAILABLE,) * 10 + (
            RobotAvailabilityState.AVAILABLE,
        ):
            adapter.observation = replace(adapter.observation, state=state)
            await core.async_process_robot_observation("robot")
    rows = [json.loads(row.getMessage().split(" ", 1)[1]) for row in caplog.records]
    assert [row["state"] for row in rows] == ["offline", "online"]


async def test_sink_failure_does_not_change_physical_execution():
    class BrokenSink:
        def emit(self, record):
            raise RuntimeError("private")

    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    core = await _orchestrator(backend, adapter)
    core.trace.sink = BrokenSink()
    job = await core.async_create_job(_intent())
    await core.async_start_job(job)
    assert len(adapter.dispatches) == 1
    assert core.trace.sink_failures > 0


def test_unknown_exception_and_command_names_are_not_disclosed(caplog):
    sink = LoggingSink()
    assert sink.pseudonym(None) is None
    clean = sink.sanitize(
        {
            "event": "secret",
            "command": "secret",
            "exception_type": "secret",
            "payload": "secret",
        }
    )
    assert "secret" not in json.dumps(clean)


@pytest.mark.parametrize("acknowledge", [True, False])
async def test_settings_log_observed_confirmation_or_timeout(hass, acknowledge):
    trace = TraceRecorder()
    hass.states.async_set(
        "select.cleaning", "old", {"options": ["old", "private-option"]}
    )

    async def select(call):
        if acknowledge:
            hass.states.async_set(
                "select.cleaning",
                "private-option",
                {"options": ["old", "private-option"]},
            )

    hass.services.async_register("select", "select_option", select)
    token = adapter_reporter.set(
        lambda event, stage, reason: trace.record(
            event, NOW, stage=stage, reason=reason
        )
    )
    try:
        if acknowledge:
            await async_set_option(
                hass,
                "select.cleaning",
                "private-option",
                confirmation_seconds=1,
                setting="cleaning_mode",
            )
        else:
            with pytest.raises(ConflictError, match="setting_confirmation_timeout"):
                await async_set_option(
                    hass,
                    "select.cleaning",
                    "private-option",
                    confirmation_seconds=0.01,
                    setting="cleaning_mode",
                )
    finally:
        adapter_reporter.reset(token)
    assert [row["stage"] for row in trace.snapshot()] == [
        "requested",
        "confirmed" if acknowledge else "timeout",
    ]
    assert "private-option" not in json.dumps(trace.snapshot())


async def test_queue_trace_distinguishes_pause_quiet_window_and_completion():
    core = await _orchestrator(RecordingBackend())
    moment = [NOW]
    core._clock = lambda: moment[0]
    core.runs._clock = lambda: moment[0]
    await core.async_run_queue()
    await core.async_reconcile_queue_run()
    await core.async_set_queue_mode(QueueMode.PAUSED)
    await core.async_run_queue()
    await core.async_reconcile_queue_run()
    moment[0] += timedelta(seconds=901)
    await core.async_reconcile_queue_run()
    rows = [row for row in core.trace.snapshot() if row["event"] == "queue_run"]
    assert [row["stage"] for row in rows] == [
        "started",
        "waiting",
        "paused",
        "resumed",
        "waiting",
        "completed",
    ]
    assert len({row["run_id"] for row in rows}) == 1


async def test_observation_exception_and_listener_failure_are_correlated(monkeypatch):
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    core = await _orchestrator(backend, adapter)

    async def unavailable():
        raise RuntimeError("private vendor details")

    def listener():
        raise RuntimeError("private listener details")

    monkeypatch.setattr(adapter, "async_observe", unavailable)
    await core.async_process_robot_observation("robot")
    core.subscribe_view(listener)
    core.notify_runtime_change()
    errors = [row for row in core.trace.snapshot() if row["event"] == "internal_error"]
    assert [row["reason"] for row in errors] == [
        "observation_failed",
        "view_listener_failed",
    ]
    assert errors[0]["robot_id"] == "robot"
    assert "private" not in json.dumps(core.trace.snapshot())


async def test_blocked_queue_does_not_alternate_duplicate_log_reasons(caplog):
    core = await _orchestrator(RecordingBackend())
    core.trace.sink = LoggingSink()
    await core.async_create_job(replace(_intent(), required_on=("binary_sensor.door",)))
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        await core.async_run_queue()
        for _ in range(20):
            await core.async_dispatch_available()
    rows = [
        json.loads(row.getMessage().split(" ", 1)[1])
        for row in caplog.records
        if row.getMessage().startswith("VOI ")
    ]
    blocked = [row for row in rows if row["event"] == "dispatch_blocked"]
    assert len(blocked) == 1
    assert blocked[0]["reason"] == "job_prerequisites"


def test_own_codes_and_known_vendor_values_stay_readable() -> None:
    sink = LoggingSink(ADAPTER_VALUES)
    record = {
        "event": "attempt_transition",
        "command": "end_queue",
        "state": "recovery_required",
        "reason": "observed_mode_mismatch",
    }
    assert sink.sanitize(record) | {"diagnostic_version": 1} == {
        **record,
        "diagnostic_version": 1,
    }
    for reason in ("returning_home", "completion_pending", "recovery_abandoned"):
        assert sink.sanitize({"reason": reason})["reason"] == reason
    assert LoggingSink().sanitize({"reason": "returning_home"})["reason"] == (
        "redacted"
    )
    assert sink.sanitize({"reason": "Kitchen door"})["reason"] == "redacted"


def test_phase_changes_are_never_folded_into_repetitions(caplog) -> None:
    sink = LoggingSink(ADAPTER_VALUES)
    base = {"event": "robot_observation", "robot_id": "robot", "state": "busy"}
    with caplog.at_level(logging.DEBUG, logger="custom_components.vacuum_orchestrator"):
        for phase, reason, timestamp in (
            ("cleaning", "segment_cleaning", "t1"),
            ("cleaning", "segment_cleaning", "t2"),
            ("returning", "returning_home", "t3"),
            ("returning", "vendor text", "t4"),
            ("cleaning", "other vendor text", "t5"),
        ):
            sink.emit(
                {**base, "phase": phase, "reason": reason, "timestamp": timestamp}
            )
    phases = [
        json.loads(record.getMessage().split(" ", 1)[1])["phase"]
        for record in caplog.records
    ]
    assert phases == ["cleaning", "returning", "returning", "cleaning"]


def test_unknown_observation_facts_stay_visible_as_none() -> None:
    clean = LoggingSink().sanitize(
        {
            "event": "robot_observation",
            "cleaning_active": None,
            "normal_end": False,
            "faults": "water_empty:station_mop:dock,Kitchen:general:robot",
        }
    )
    assert clean["cleaning_active"] is None and clean["normal_end"] is False
    assert clean["faults"] == "redacted:station_mop:dock,redacted:general:robot"
    assert "cleaning_active" not in LoggingSink().sanitize({"event": "command"})
