"""Actions follow the same predicates as the commands they offer."""

from dataclasses import replace
from datetime import timedelta

import pytest

from custom_components.vacuum_orchestrator.domain.errors import ConflictError
from custom_components.vacuum_orchestrator.domain.holds import (
    HOLD_LEASE_SECONDS,
    HoldPurpose,
)
from custom_components.vacuum_orchestrator.domain.permissions import (
    AVAILABLE,
    Availability,
    job_actions,
    queue_actions,
    require,
    robot_actions,
    room_actions,
    template_actions,
    unavailable,
)
from custom_components.vacuum_orchestrator.domain.queue import OrchestratorState
from custom_components.vacuum_orchestrator.domain.queue_runs import RunPhase
from custom_components.vacuum_orchestrator.domain.rooms import Room
from custom_components.vacuum_orchestrator.domain.templates import JobTemplate
from custom_components.vacuum_orchestrator.domain.types import JobState
from custom_components.vacuum_orchestrator.domain.waiting import (
    Blocker,
    Robots,
    RobotsState,
    Waiting,
)
from tests.domain.test_holds import _hold
from tests.domain.test_queue import INTENT, NOW, _prepared

READY = Robots(RobotsState.READY)


def _queued(*job_ids: str) -> OrchestratorState:
    state = OrchestratorState.empty("installation")
    for job_id in job_ids:
        state = state.add_job(job_id, INTENT, NOW)
    return state


def _reasons(actions: dict[str, Availability]) -> dict[str, str | None]:
    return {name: item.reason for name, item in actions.items()}


def test_a_waiting_job_is_edited_deleted_or_started_but_not_cancelled() -> None:
    state = _queued("a", "b", "c")

    assert _reasons(job_actions(state, state.jobs["b"], NOW, None)) == {
        "edit": None,
        "delete": None,
        "cancel": "job_not_started",
        "start": None,
        "retry": "job_not_retryable",
        "correct": "job_not_correctable",
        "move_up": None,
        "move_down": None,
    }
    first = job_actions(state, state.jobs["a"], NOW, None)
    last = job_actions(state, state.jobs["c"], NOW, None)
    assert first["move_up"] == unavailable("job_at_top")
    assert last["move_down"] == unavailable("job_at_bottom")


def test_a_foreign_hold_blocks_what_its_holder_decides() -> None:
    state = _queued("a").hold_job(_hold(purpose=HoldPurpose.CONFIRM), NOW)

    actions = job_actions(state, state.jobs["a"], NOW, None)
    held = unavailable("job_held", "confirm")
    assert (actions["edit"], actions["delete"], actions["start"]) == (held,) * 3
    assert actions["move_up"].reason == "job_at_top"

    # A lapsed hold no longer protects against a manual start or edit.
    later = NOW + timedelta(seconds=HOLD_LEASE_SECONDS)
    lapsed = Waiting((Blocker("pending_confirmation"),), READY, ())
    actions = job_actions(state, state.jobs["a"], later, lapsed)
    assert actions["edit"] == actions["start"] == AVAILABLE


@pytest.mark.parametrize(
    "waiting",
    [
        Waiting((Blocker("room_not_released", ("kitchen",)),), READY, ()),
        Waiting((), Robots(RobotsState.NO_ROBOT_READY), ()),
    ],
)
def test_start_needs_the_job_and_a_robot_but_not_the_queue(waiting: Waiting) -> None:
    state = _queued("a")

    assert job_actions(state, state.jobs["a"], NOW, waiting)["start"] == (
        unavailable("job_not_startable")
    )
    queue_only = Waiting((), READY, (Blocker("queue_idle"), Blocker("start_delayed")))
    assert job_actions(state, state.jobs["a"], NOW, queue_only)["start"] == AVAILABLE


def test_started_work_is_cancelled_and_finished_work_retried() -> None:
    started, _unit = _prepared()

    assert _reasons(job_actions(started, started.jobs["a"], NOW, None)) == {
        "edit": "job_not_editable",
        "delete": "job_not_deletable",
        "cancel": None,
        "start": "job_not_waiting",
        "retry": "job_not_retryable",
        "correct": "job_not_correctable",
        "move_up": "job_not_movable",
        "move_down": "job_not_movable",
    }
    done = replace(started.jobs["a"], state=JobState.COMPLETED)
    actions = job_actions(started, done, NOW, None)
    assert (actions["delete"], actions["retry"], actions["correct"]) == (
        AVAILABLE,
        AVAILABLE,
        AVAILABLE,
    )
    assert actions["cancel"] == unavailable("job_not_cancellable")


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        (
            RunPhase.OFF,
            {
                "run": None,
                "pause": "queue_not_running",
                "resume": "queue_not_running",
                "end": "queue_not_running",
            },
        ),
        (
            RunPhase.ACTIVE,
            {
                "run": "queue_running",
                "pause": None,
                "resume": "queue_running",
                "end": None,
            },
        ),
        (
            RunPhase.STANDBY,
            {
                "run": "queue_running",
                "pause": None,
                "resume": "queue_running",
                "end": None,
            },
        ),
        (
            RunPhase.PAUSED,
            {
                "run": "queue_running",
                "pause": "queue_paused",
                "resume": None,
                "end": None,
            },
        ),
        (
            RunPhase.ENDING,
            {
                "run": "queue_running",
                "pause": "queue_ending",
                "resume": None,
                "end": "queue_ending",
            },
        ),
    ],
)
def test_queue_actions_follow_the_run_phase(
    phase: RunPhase, expected: dict[str, str | None]
) -> None:
    assert _reasons(queue_actions(phase)) == expected


def test_require_raises_the_reason() -> None:
    require(AVAILABLE)
    with pytest.raises(ConflictError, match="job_held") as raised:
        require(unavailable("job_held", "edit"))
    assert raised.value.detail == "edit"


def test_room_actions_follow_activity_release_and_running_work() -> None:
    state = _queued()
    room = Room("kitchen", "Kitchen", "kitchen")

    assert _reasons(room_actions(state, room)) == {
        "release": None,
        "revoke": "room_not_released",
        "edit": None,
        "disable": None,
        "enable": "room_enabled",
        "create_job": None,
    }
    excluded = replace(room, enabled=False)
    assert _reasons(room_actions(state, excluded)) == {
        "release": "room_unavailable",
        "revoke": "room_not_released",
        "edit": None,
        "disable": "room_disabled",
        "enable": None,
        "create_job": "room_unavailable",
    }
    started, _unit = _prepared()
    busy = room_actions(started, room)
    assert (busy["edit"], busy["disable"]) == (unavailable("room_has_active_job"),) * 2
    assert busy["release"] == AVAILABLE


def test_robot_actions_follow_lease_attention_and_dock() -> None:
    idle = robot_actions(leased=False, blocked=False, returns=True, at_dock=False)
    assert _reasons(idle) == {
        "configure": None,
        "rename": None,
        "remove": None,
        "return_to_dock": None,
    }
    leased = robot_actions(leased=True, blocked=True, returns=True, at_dock=False)
    assert _reasons(leased) == {
        "configure": "robot_busy",
        "rename": None,
        "remove": "robot_busy",
        "return_to_dock": "robot_already_executing",
    }
    for blocked, returns, at_dock, reason in (
        (True, True, False, "robot_needs_attention"),
        (False, False, False, "return_to_dock_unsupported"),
        (False, True, True, "robot_at_dock"),
        (False, None, None, "robot_unavailable"),
    ):
        actions = robot_actions(
            leased=False, blocked=blocked, returns=returns, at_dock=at_dock
        )
        assert actions["return_to_dock"] == unavailable(reason)


def test_template_actions_follow_enabled_and_demand() -> None:
    template = JobTemplate("t", "Daily", INTENT, NOW)

    assert _reasons(template_actions(template)) == {
        "create_job": None,
        "edit": None,
        "remove": None,
        "reset_demand": "no_suppressed_demand",
    }
    paused = replace(template, enabled=False, demand_tokens={"kitchen": "token"})
    actions = template_actions(paused)
    assert actions["create_job"] == unavailable("template_disabled")
    assert actions["reset_demand"] == AVAILABLE
