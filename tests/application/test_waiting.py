"""A waiting job names its most important blocker and every other one."""

from dataclasses import replace
from datetime import timedelta

from custom_components.vacuum_orchestrator.domain.holds import HoldPurpose
from custom_components.vacuum_orchestrator.domain.intents import JobIntent, TargetRef
from custom_components.vacuum_orchestrator.domain.requirements import StateRequirement
from custom_components.vacuum_orchestrator.domain.types import (
    CleaningMode,
    OperationKind,
    QueueMode,
    RobotAvailabilityState,
)
from tests.application.test_orchestrator import (
    NOW,
    RecordingAdapter,
    RecordingBackend,
    _intent,
    _orchestrator,
)
from tests.application.test_room_execution import finished_run

DELAY = timedelta(seconds=5)


async def setup_waiting(*adapters: RecordingAdapter, states=None):
    backend = RecordingBackend()
    adapters = adapters or (RecordingAdapter(backend, "robot"),)
    for adapter in adapters:
        adapter._backend = backend
    core = await _orchestrator(backend, *adapters, states=states, start_delay=5)
    now = [NOW]
    for owner in (core, core.runs, core.rooms, core.templates):
        owner._clock = lambda: now[0]
    for adapter in adapters:
        await core.async_process_robot_observation(adapter.profile.robot_id)
    return core, now


def codes(core, job_id):
    waiting = core.waiting(job_id)
    return None if waiting is None else [item.code for item in waiting.blockers]


async def test_the_queue_and_the_delay_explain_a_ready_job_until_it_starts() -> None:
    core, now = await setup_waiting()
    job = await core.async_create_job(_intent())
    assert codes(core, job) == ["queue_idle", "start_delayed"]

    await core.async_set_queue_mode(QueueMode.RUNNING)
    waiting = core.waiting(job)
    assert waiting.primary.code == "start_delayed"
    assert waiting.primary.until == NOW + DELAY

    await core.async_set_queue_mode(QueueMode.PAUSED)
    assert codes(core, job) == ["queue_paused", "start_delayed"]

    await core.async_set_queue_mode(QueueMode.RUNNING)
    now[0] += DELAY
    assert core.waiting(job) is None
    await core.async_dispatch_available()
    assert core.waiting(job) is None


async def test_a_job_waits_for_the_end_of_a_finishing_run() -> None:
    backend = RecordingBackend()
    core, _now = await setup_waiting(
        RecordingAdapter(backend, "a", targets=("kitchen",)),
        RecordingAdapter(backend, "b", targets=("hall",)),
    )
    running = await core.async_create_job(_intent())
    await core.async_run_queue()
    await core.async_start_job(running)
    waiting = await core.async_create_job(_intent(area="hall"))
    await core.async_end_queue()
    assert codes(core, waiting) == ["queue_ending", "start_delayed"]


async def test_a_hold_and_a_missing_release_come_before_everything_else() -> None:
    core, _now = await setup_waiting()
    await core.rooms.async_revoke("hall")
    job = await core.async_create_job(
        JobIntent((TargetRef("kitchen"), TargetRef("hall")), CleaningMode.VACUUM)
    )
    waiting = core.waiting(job)
    assert waiting.primary.code == "room_not_released"
    assert waiting.primary.room_ids == ("hall",)
    assert core.jobs_awaiting_release() == {"hall": (job,)}

    hold = await core.async_hold_job(job, HoldPurpose.CONFIRM)
    waiting = core.waiting(job)
    assert [item.code for item in waiting.blockers[:2]] == [
        "pending_confirmation",
        "room_not_released",
    ]
    assert waiting.primary.until == hold.expires_at


async def test_reach_explains_rooms_and_operations_no_robot_covers() -> None:
    backend = RecordingBackend()
    kitchen = RecordingAdapter(backend, "a", targets=("kitchen",))
    hall = RecordingAdapter(backend, "b", targets=("hall",))
    core, _now = await setup_waiting(kitchen, hall)
    both = await core.async_create_job(
        JobIntent((TargetRef("kitchen"), TargetRef("hall")), CleaningMode.VACUUM)
    )
    waiting = core.waiting(both)
    assert waiting.primary.code == "rooms_not_reachable_together"
    assert waiting.primary.room_ids == ("kitchen", "hall")

    for adapter in (kitchen, hall):
        adapter._profile = replace(
            adapter.profile, allowed_operations=frozenset({OperationKind.VACUUM})
        )
    mop = await core.async_create_job(_intent(mode=CleaningMode.MOP))
    assert codes(core, mop)[0] == "operation_unsupported"

    hall._profile = replace(
        hall.profile,
        capabilities=replace(hall.profile.capabilities, target_map={}),
    )
    assert core.waiting(both).primary.code == "room_unreachable"
    assert core.waiting(both).primary.room_ids == ("hall",)

    await core.async_replace_adapters({})
    assert codes(core, both)[0] == "no_robot_configured"


async def test_conditions_and_robot_states_name_entities_and_robots() -> None:
    core, _now = await setup_waiting(
        states={"binary_sensor.window": "on", "binary_sensor.door": None}
    )
    job = await core.async_create_job(
        replace(
            _intent(),
            required_off=("binary_sensor.window",),
            required_on=("binary_sensor.door",),
        )
    )
    waiting = core.waiting(job)
    assert [(item.code, item.entity_ids) for item in waiting.blockers[:2]] == [
        ("requirement_not_satisfied", ("binary_sensor.window",)),
        ("requirement_unknown", ("binary_sensor.door",)),
    ]

    plain = await core.async_create_job(_intent(area="hall"))
    (adapter,) = core.adapters.values()
    adapter.observation = replace(
        adapter.observation, state=RobotAvailabilityState.BUSY
    )
    await core.async_process_robot_observation("robot")
    busy = core.waiting(plain)
    assert busy.primary.code == "robot_busy" and busy.primary.robot_ids == ("robot",)

    adapter.observation = replace(
        adapter.observation, state=RobotAvailabilityState.AVAILABLE
    )
    await core.async_process_robot_observation("robot")
    core._state = replace(
        core.state, blocked_robots={"source-robot": "physical_run_ownership_uncertain"}
    )
    assert core.waiting(plain).primary.code == "robot_needs_attention"


async def test_started_jobs_and_overlapping_rooms() -> None:
    core, now = await setup_waiting()
    first = await core.async_create_job(_intent())
    await core.async_start_job(first)
    assert core.waiting(first) is None

    second = await core.async_create_job(_intent())
    await core.async_set_queue_mode(QueueMode.RUNNING)
    now[0] += DELAY
    waiting = core.waiting(second)
    assert waiting.primary.code == "rooms_in_use"
    assert waiting.primary.room_ids == ("kitchen",)


async def test_a_condition_failing_only_for_one_robot_names_that_robot() -> None:
    core, _now = await setup_waiting(states={"binary_sensor.dock_door": "off"})
    await core.rooms.async_update(
        "kitchen",
        lambda room: replace(
            room,
            requirements=(
                StateRequirement("binary_sensor.dock_door", robot_id="robot"),
            ),
        ),
    )
    job = await core.async_create_job(_intent())
    waiting = core.waiting(job)
    assert waiting.primary.code == "requirement_not_satisfied"
    assert (
        waiting.primary.room_ids,
        waiting.primary.entity_ids,
        waiting.primary.robot_ids,
    ) == (("kitchen",), ("binary_sensor.dock_door",), ("robot",))


async def test_a_next_phase_waits_for_a_robot_but_not_for_the_queue() -> None:
    core, _now = await setup_waiting()
    job = await core.async_create_job(_intent(mode=CleaningMode.VACUUM_THEN_MOP))
    await core.async_start_job(job)
    (adapter,) = core.adapters.values()
    adapter.observation = replace(
        adapter.observation, state=RobotAvailabilityState.BUSY
    )
    attempt = core.state.jobs[job].active_attempt_id
    await core.async_confirm_start(attempt)
    await core.async_record_robot_run(
        attempt, finished_run(core.state.attempts[attempt], name=attempt)
    )
    assert core.state.jobs[job].active_attempt_id is None
    assert codes(core, job) == ["robot_busy"]
