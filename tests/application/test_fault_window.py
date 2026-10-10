"""A device fault during a run waits for its fix before the job fails.

See dev doc "Gerätefehler". The robot is the real Roborock adapter.
"""

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant

from custom_components.vacuum_orchestrator.domain.attention import AttentionKind
from custom_components.vacuum_orchestrator.domain.completion import CompletionQuality
from custom_components.vacuum_orchestrator.domain.types import (
    AttemptState,
    JobState,
)
from tests.application.test_roborock_completion import _core, _observer, _started

JAMMED = "main_brush_jammed"


async def test_a_fixed_fault_lets_the_run_go_on_and_complete(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    core, job_id, backend, adapter = await _started(hass, freezer)
    observe = _observer(hass, freezer, core)
    await observe("segment_cleaning", "on")

    hass.states.async_set("sensor.error", JAMMED)
    await observe("paused", "on", seconds=60)
    (attempt,) = core.state.attempts.values()
    assert core.state.jobs[job_id].state is JobState.RUNNING
    assert attempt.fault_since is not None
    fails_at = attempt.fault_since + timedelta(seconds=900)
    progress = core.progress(job_id)
    assert progress is not None and progress.fault is not None
    assert (progress.fault.codes, progress.fault.fails_at) == ((JAMMED,), fails_at)
    (entry,) = core.attention()
    assert (entry.kind, entry.job_id, entry.fails_at) == (
        AttentionKind.DEVICE_FAULT,
        job_id,
        fails_at,
    )
    reloaded = await _core(backend, adapter)
    assert reloaded.state.attempts[attempt.attempt_id].fault_since == (
        attempt.fault_since
    )

    hass.states.async_set("sensor.error", "none")
    await observe("paused", "on", seconds=60)
    assert core.state.attempts[attempt.attempt_id].fault_since is not None
    assert core.attention() == ()
    await observe("segment_cleaning", "on", seconds=60)
    assert core.state.attempts[attempt.attempt_id].fault_since is None
    await observe("returning_home", "off", seconds=300)
    await observe("charging", "off", seconds=60)
    await observe("charging", "off", seconds=30)

    assert core.state.jobs[job_id].state is JobState.COMPLETED
    assert (
        core.state.attempts[attempt.attempt_id].completion_quality
        is CompletionQuality.DERIVED
    )


async def test_a_robot_resting_at_the_deadline_fails_the_job_and_is_free(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    core, job_id, _backend, _adapter = await _started(hass, freezer)
    observe = _observer(hass, freezer, core)
    await observe("segment_cleaning", "on")
    hass.states.async_set("sensor.error", JAMMED)
    await observe("paused", "on", seconds=60)
    hass.states.async_set("sensor.error", "none")
    await observe("returning_home", "off", seconds=60)
    await observe("charging", "off", seconds=300)
    assert core.state.jobs[job_id].state is JobState.RUNNING

    await observe("charging", "off", seconds=540)

    job = core.state.jobs[job_id]
    (attempt,) = core.state.attempts.values()
    assert (job.state, job.failure_code) == (JobState.FAILED, "robot_fault_timeout")
    assert attempt.state is AttemptState.FAILED
    assert core.state.robot_leases == {}
    assert core.state.blocked_robots == {}
    assert core.state.room_registry.rooms["kitchen"].last_cleaning == {}


async def test_a_robot_still_faulted_at_the_deadline_needs_recovery(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    core, job_id, _backend, _adapter = await _started(hass, freezer)
    observe = _observer(hass, freezer, core)
    await observe("segment_cleaning", "on")
    hass.states.async_set("sensor.error", JAMMED)
    await observe("paused", "on", seconds=60)

    await observe("paused", "on", seconds=900)

    job = core.state.jobs[job_id]
    assert (job.state, job.failure_code) == (
        JobState.NEEDS_ATTENTION,
        "robot_fault_timeout",
    )
    assert {entry.kind for entry in core.attention()} == {
        AttentionKind.DEVICE_FAULT,
        AttentionKind.ROBOT_RECOVERY,
        AttentionKind.JOB_RECOVERY,
    }
