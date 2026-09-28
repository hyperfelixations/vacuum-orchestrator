"""External activity is retained without inventing room-level success."""

from dataclasses import replace
from datetime import timedelta

from custom_components.vacuum_orchestrator.application.external_observations import (
    apply_external_observation,
)
from custom_components.vacuum_orchestrator.domain.completion import (
    CleaningSource,
    CompletionQuality,
)
from custom_components.vacuum_orchestrator.domain.dispatching import RobotObservation
from custom_components.vacuum_orchestrator.domain.queue import OrchestratorState
from custom_components.vacuum_orchestrator.domain.room_registry import RoomRegistry
from custom_components.vacuum_orchestrator.domain.rooms import Room
from custom_components.vacuum_orchestrator.domain.types import (
    OperationKind,
    RobotAvailabilityState,
)
from tests.application.test_orchestrator import NOW, RecordingAdapter, RecordingBackend

PROFILE = RecordingAdapter(RecordingBackend(), "robot").profile
START = RobotObservation(
    "robot",
    "source-robot",
    RobotAvailabilityState.BUSY,
    observed_at=NOW,
    cleaning_active=True,
    observed_operation=OperationKind.VACUUM,
)
END = replace(
    START,
    state=RobotAvailabilityState.AVAILABLE,
    cleaning_active=False,
    normal_end=True,
    observed_at=NOW + timedelta(minutes=10),
)
STATE = replace(
    OrchestratorState.empty("installation"),
    room_registry=RoomRegistry(
        {"kitchen": Room("kitchen", "Kitchen"), "hall": Room("hall", "Hall")}
    ),
)


def apply(state, observation):
    return apply_external_observation(
        state, observation, PROFILE, observation.observed_at, lambda: "external-run"
    )


def test_uncertain_external_run_stays_history_only() -> None:
    state = apply(STATE, START)
    assert apply(state, START) is state
    state = apply(state, END)
    assert state.robot_runs["external-run"].source is CleaningSource.EXTERNAL
    assert state.robot_runs["external-run"].observed_end == END.observed_at
    assert not state.room_registry.receipts
    assert apply(state, END) is state


def test_confirmed_external_scope_mode_and_success_update_only_matching_rooms() -> None:
    state = apply(STATE, START)
    state = apply(
        state, replace(END, completion_confirmed=True, completed_targets=("kitchen",))
    )
    stamp = state.room_registry.rooms["kitchen"].last_cleaning[OperationKind.VACUUM]
    assert stamp.quality is CompletionQuality.CONFIRMED
    assert not state.room_registry.rooms["hall"].last_cleaning
    assert len(state.room_registry.receipts) == 1


def test_incomplete_mapping_unknown_mode_and_disconnect_never_update_rooms() -> None:
    for end in (
        replace(
            END, completion_confirmed=True, completed_targets=("kitchen", "missing")
        ),
        replace(
            END,
            completion_confirmed=True,
            completed_targets=("kitchen",),
            observed_operation=None,
        ),
        replace(
            END,
            completion_confirmed=True,
            completed_targets=("kitchen",),
            error_code="stuck",
        ),
    ):
        assert not apply(apply(STATE, START), end).room_registry.receipts
    started = apply(STATE, START)
    interrupted = apply(
        started,
        replace(START, state=RobotAvailabilityState.UNKNOWN, cleaning_active=None),
    )
    assert (
        interrupted.robot_runs["external-run"].failure_code
        == "external_run_interrupted"
    )
    assert not apply(
        interrupted,
        replace(END, completion_confirmed=True, completed_targets=("kitchen",)),
    ).room_registry.receipts


def test_out_of_order_external_completion_does_not_close_the_run() -> None:
    state = apply(STATE, START)
    assert apply(state, replace(END, observed_at=NOW - timedelta(seconds=1))) is state


def test_changed_map_or_conflicting_mode_cannot_attribute_external_completion():
    started = apply(STATE, START)
    end = replace(END, completion_confirmed=True, completed_targets=("kitchen",))
    changed = replace(
        PROFILE, capabilities=replace(PROFILE.capabilities, revision="new-map")
    )
    result = apply_external_observation(
        started, end, changed, end.observed_at, lambda: "unused"
    )
    assert not result.room_registry.receipts
    assert not apply(
        started, replace(end, observed_operation=OperationKind.MOP)
    ).room_registry.receipts
