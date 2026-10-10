"""Connection gaps and restarts do not stop a run.

See dev doc "Unterbrechungen". The robot is the real Roborock adapter.
"""

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant

from custom_components.vacuum_orchestrator.api.presentation import present_job_view
from custom_components.vacuum_orchestrator.domain.types import (
    AttemptState,
    JobState,
    OperationKind,
)
from tests.application.test_roborock_completion import (
    _core,
    _finish,
    _observer,
    _started,
)


def _connect(hass: HomeAssistant, online: bool) -> None:
    vacuum = hass.states.get("vacuum.test")
    assert vacuum is not None
    hass.states.async_set(
        "vacuum.test", "cleaning" if online else "unavailable", vacuum.attributes
    )


async def test_a_short_connection_loss_lets_the_run_go_on(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    core, job_id, _backend, _adapter = await _started(hass, freezer)
    observe = _observer(hass, freezer, core)
    await observe("segment_cleaning", "on")
    _connect(hass, False)
    await observe("segment_cleaning", "on", seconds=60)
    (attempt,) = core.state.attempts.values()
    assert core.state.jobs[job_id].state is JobState.RUNNING
    assert attempt.lost_since is not None and attempt.gap_since is not None

    _connect(hass, True)
    await observe("segment_cleaning", "on", seconds=120)
    attempt = core.state.attempts[attempt.attempt_id]
    assert (attempt.lost_since, attempt.gap_since) == (None, None)
    await _finish(observe)

    assert core.state.jobs[job_id].state is JobState.COMPLETED
    assert core.state.completion(job_id).notes == ()


async def test_a_run_that_ends_in_a_gap_completes_with_a_note(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    core, job_id, _backend, _adapter = await _started(hass, freezer)
    observe = _observer(hass, freezer, core)
    await observe("segment_cleaning", "on")
    _connect(hass, False)
    await observe("segment_cleaning", "on", seconds=60)
    _connect(hass, True)
    await observe("charging", "off", seconds=300)
    await observe("charging", "off", seconds=30)

    assert core.state.jobs[job_id].state is JobState.COMPLETED
    (receipt,) = core.state.room_registry.receipts.values()
    assert (receipt.operation, receipt.evidence) == (
        OperationKind.VACUUM,
        ("derived_completion", "end_not_observed"),
    )
    assert present_job_view(core, core.state.jobs[job_id])["completion"] == {
        "quality": "derived",
        "notes": ["end_not_observed"],
    }


async def test_a_lasting_connection_loss_needs_attention(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    core, job_id, _backend, _adapter = await _started(hass, freezer)
    observe = _observer(hass, freezer, core)
    await observe("segment_cleaning", "on")
    _connect(hass, False)
    await observe("segment_cleaning", "on", seconds=60)
    await observe("segment_cleaning", "on", seconds=599)
    assert core.state.jobs[job_id].state is JobState.RUNNING
    await observe("segment_cleaning", "on", seconds=1)

    job = core.state.jobs[job_id]
    assert (job.state, job.failure_code) == (
        JobState.NEEDS_ATTENTION,
        "robot_connection_lost",
    )


async def test_a_restart_during_cleaning_keeps_watching_the_run(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    core, job_id, backend, adapter = await _started(hass, freezer)
    await _observer(hass, freezer, core)("segment_cleaning", "on")
    await core.async_shutdown()
    freezer.tick(timedelta(minutes=5))
    restarted = await _core(backend, adapter)
    (attempt,) = restarted.state.attempts.values()
    assert attempt.state is AttemptState.START_CONFIRMED
    assert attempt.gap_since is not None
    assert restarted.state.jobs[job_id].state is JobState.RUNNING

    observe = _observer(hass, freezer, restarted)
    await observe("charging", "off", seconds=60)
    await observe("charging", "off", seconds=30)

    assert restarted.state.jobs[job_id].state is JobState.COMPLETED
    assert restarted.state.completion(job_id).notes == ("end_not_observed",)


async def test_a_robot_that_rests_at_the_start_deadline_fails_and_is_free(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    core, job_id, _backend, _adapter = await _started(hass, freezer)
    observe = _observer(hass, freezer, core)
    await observe("charging", "off", seconds=179)
    assert core.state.jobs[job_id].state is JobState.RUNNING
    await observe("charging", "off", seconds=1)

    job = core.state.jobs[job_id]
    assert (job.state, job.failure_code) == (JobState.FAILED, "start_not_observed")
    assert not core.state.robot_leases
    assert not core.state.blocked_robots
    retry = await core.async_retry_job(job_id)
    assert core.state.jobs[retry].state is not JobState.FAILED
