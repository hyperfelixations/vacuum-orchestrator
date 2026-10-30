"""A waiting job is explained on the job, robot and queue axes."""

from dataclasses import replace
from datetime import timedelta

from custom_components.vacuum_orchestrator.application.explanation import explain_job
from custom_components.vacuum_orchestrator.domain.holds import HoldPurpose
from custom_components.vacuum_orchestrator.domain.intents import JobIntent, TargetRef
from custom_components.vacuum_orchestrator.domain.requirements import StateRequirement
from custom_components.vacuum_orchestrator.domain.types import (
    CleaningMode,
    MoveDirection,
    OperationKind,
    QueueMode,
    RobotAvailabilityState,
)
from custom_components.vacuum_orchestrator.domain.waiting import (
    Blocker,
    RobotBlockers,
    RobotsState,
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
    """Return job, execution and queue codes; the robots axis by its state."""
    waiting = core.waiting(job_id)
    if waiting is None:
        return None
    return [
        *(item.code for item in waiting.job),
        *(() if waiting.robots.state is RobotsState.READY else [waiting.robots.state]),
        *(item.code for item in waiting.queue or ()),
    ]


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
        RecordingAdapter(backend, "a", targets=("library",)),
        RecordingAdapter(backend, "b", targets=("studio",)),
    )
    running = await core.async_create_job(_intent())
    await core.async_run_queue()
    await core.async_start_job(running)
    waiting = await core.async_create_job(_intent(area="studio"))
    await core.async_end_queue()
    assert codes(core, waiting) == ["queue_ending", "start_delayed"]


async def test_a_hold_and_a_missing_release_come_before_everything_else() -> None:
    core, _now = await setup_waiting()
    await core.rooms.async_revoke("studio")
    job = await core.async_create_job(
        JobIntent((TargetRef("library"), TargetRef("studio")), CleaningMode.VACUUM)
    )
    waiting = core.waiting(job)
    assert waiting.primary.code == "room_not_released"
    assert waiting.primary.room_ids == ("studio",)
    assert core.jobs_awaiting_release() == {"studio": (job,)}

    hold = await core.async_hold_job(job, HoldPurpose.CONFIRM)
    waiting = core.waiting(job)
    assert [item.code for item in waiting.job] == [
        "pending_confirmation",
        "room_not_released",
    ]
    assert waiting.primary.until == hold.expires_at


async def test_reach_explains_rooms_and_operations_no_robot_covers() -> None:
    backend = RecordingBackend()
    library = RecordingAdapter(backend, "a", targets=("library",))
    studio = RecordingAdapter(backend, "b", targets=("studio",))
    core, _now = await setup_waiting(library, studio)
    both = await core.async_create_job(
        JobIntent((TargetRef("library"), TargetRef("studio")), CleaningMode.VACUUM)
    )
    waiting = core.waiting(both)
    assert waiting.primary.code == "rooms_not_reachable_together"
    assert waiting.primary.room_ids == ("studio", "library")
    assert waiting.primary.robot_ids == ("a", "b")

    for adapter in (library, studio):
        adapter._profile = replace(
            adapter.profile, allowed_operations=frozenset({OperationKind.VACUUM})
        )
    mop = await core.async_create_job(_intent(mode=CleaningMode.MOP))
    assert core.waiting(mop).primary == Blocker(
        "operation_unsupported", robot_ids=("a", "b"), detail="mop"
    )

    studio._profile = replace(
        studio.profile,
        capabilities=replace(studio.profile.capabilities, target_map={}),
    )
    assert core.waiting(both).primary.code == "room_unreachable"
    assert core.waiting(both).primary.room_ids == ("studio",)

    await core.async_replace_adapters({})
    assert core.waiting(both).primary.code == "no_robot_configured"


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
    assert [(item.code, item.entity_ids) for item in waiting.job] == [
        ("requirement_not_satisfied", ("binary_sensor.window",)),
        ("requirement_unknown", ("binary_sensor.door",)),
    ]

    plain = await core.async_create_job(_intent(area="studio"))
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
    assert waiting.primary.room_ids == ("library",)


async def test_a_condition_failing_only_for_one_robot_names_that_robot() -> None:
    core, _now = await setup_waiting(states={"binary_sensor.dock_door": "off"})
    await core.rooms.async_update(
        "library",
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
    ) == (("library",), ("binary_sensor.dock_door",), ("robot",))


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
    waiting = core.waiting(job)
    assert codes(core, job) == [RobotsState.NO_ROBOT_READY]
    assert waiting.queue is None
    assert waiting.primary == Blocker("robot_busy", robot_ids=("robot",))


async def test_disabled_and_arealess_rooms_are_named_as_such() -> None:
    core, _now = await setup_waiting()
    await core.rooms.async_revoke("studio")
    job = await core.async_create_job(
        JobIntent((TargetRef("library"), TargetRef("studio")), CleaningMode.VACUUM)
    )
    await core.rooms.async_disable("library")
    assert [(item.code, item.room_ids) for item in core.waiting(job).job] == [
        ("room_disabled", ("library",)),
        ("room_not_released", ("studio",)),
    ]
    assert core.jobs_awaiting_release() == {"studio": (job,)}

    await core.rooms.async_enable("library")
    await core.rooms.async_update(
        "studio", lambda room: replace(room, area_missing=True)
    )
    assert [(item.code, item.room_ids) for item in core.waiting(job).job] == [
        ("room_area_missing", ("studio",)),
    ]
    assert core.jobs_awaiting_release() == {}


async def test_a_ready_job_names_every_reason_of_every_robot() -> None:
    backend = RecordingBackend()
    first = RecordingAdapter(backend, "a")
    second = RecordingAdapter(backend, "b")
    second._profile = replace(second.profile, minimum_battery=90)
    core, _now = await setup_waiting(
        first, second, states={"binary_sensor.dock_a": "off"}
    )
    await core.rooms.async_update(
        "library",
        lambda room: replace(
            room,
            requirements=(StateRequirement("binary_sensor.dock_a", robot_id="a"),),
        ),
    )
    first.observation = replace(first.observation, state=RobotAvailabilityState.BUSY)
    await core.async_process_robot_observation("a")
    job = await core.async_create_job(_intent())

    waiting = core.waiting(job)
    assert waiting.job == ()
    assert waiting.robots.state is RobotsState.NO_ROBOT_READY
    assert waiting.robots.candidates == (
        RobotBlockers(
            "a",
            (
                Blocker("robot_busy"),
                Blocker(
                    "requirement_not_satisfied",
                    room_ids=("library",),
                    entity_ids=("binary_sensor.dock_a",),
                ),
            ),
        ),
        RobotBlockers("b", (Blocker("battery_low"),)),
    )
    assert waiting.primary == Blocker("no_robot_ready", robot_ids=("a", "b"))


async def test_unsuitable_robots_list_every_structural_reason() -> None:
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot", targets=("library",))
    adapter._profile = replace(
        adapter.profile, allowed_operations=frozenset({OperationKind.VACUUM})
    )
    adapter.observation = replace(
        adapter.observation, state=RobotAvailabilityState.BUSY
    )
    core, _now = await setup_waiting(adapter)
    job = await core.async_create_job(_intent(area="studio", mode=CleaningMode.MOP))

    robots = core.waiting(job).robots
    assert robots.state is RobotsState.NO_CAPABLE_ROBOT and robots.candidates == ()
    assert robots.unsuitable == (
        RobotBlockers(
            "robot",
            (
                Blocker("operation_unsupported", detail="mop"),
                Blocker("room_unreachable", room_ids=("studio",), detail="studio"),
            ),
        ),
    )


async def test_a_lapsed_hold_blocks_dispatch_and_explanation_until_it_ends() -> None:
    core, now = await setup_waiting()
    job = await core.async_create_job(_intent())
    hold = await core.async_hold_job(job, HoldPurpose.EDIT)
    await core.async_set_queue_mode(QueueMode.RUNNING)
    now[0] = hold.expires_at + DELAY

    assert core.waiting(job).primary == Blocker("being_edited", until=hold.expires_at)
    assert core.has_pending_work(core.state)

    await core.async_expire_job_holds()
    assert core.waiting(job).primary.code == "start_delayed"


async def test_the_execution_explanation_names_a_structural_reason_first() -> None:
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot", targets=("library",))
    core, _now = await setup_waiting(adapter)
    running = await core.async_create_job(_intent())
    await core.async_start_job(running)
    job = await core.async_create_job(_intent(area="studio"))

    # The robot is busy now, but it could never clean the studio as configured.
    (unsuitable,) = core.waiting(job).robots.unsuitable
    assert unsuitable.blockers[0].code == "room_unreachable"
    (explanation,) = await explain_job(core, job)
    assert explanation.eligibility_reason == "unmapped_target"


async def test_the_first_ready_job_is_next_and_each_robot_takes_one() -> None:
    core, _now = await setup_waiting()
    first = await core.async_create_job(_intent())
    second = await core.async_create_job(_intent("studio"))
    assert core.next_jobs() == {first}

    await core.async_move_job(second, MoveDirection.UP)
    assert core.next_jobs() == {second}

    await core.rooms.async_revoke("library")
    await core.async_move_job(second, MoveDirection.DOWN)
    # A job its room keeps waiting is passed over; the next ready one leads.
    assert core.next_jobs() == {second}


async def test_two_free_robots_make_two_jobs_next() -> None:
    backend = RecordingBackend()
    core, _now = await setup_waiting(
        RecordingAdapter(backend, "rocky"), RecordingAdapter(backend, "dusty")
    )
    jobs = [
        await core.async_create_job(_intent(area))
        for area in ("library", "library", "studio", "studio")
    ]
    # The second library job would overlap the first; the studio job is next.
    assert core.next_jobs() == {jobs[0], jobs[2]}
