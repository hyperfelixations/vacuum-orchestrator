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
    unavailable,
)
from custom_components.vacuum_orchestrator.domain.queue import OrchestratorState
from custom_components.vacuum_orchestrator.domain.queue_runs import RunPhase
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
        "move_up": "job_not_movable",
        "move_down": "job_not_movable",
    }
    done = replace(started.jobs["a"], state=JobState.COMPLETED)
    actions = job_actions(started, done, NOW, None)
    assert (actions["delete"], actions["retry"]) == (AVAILABLE, AVAILABLE)
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
