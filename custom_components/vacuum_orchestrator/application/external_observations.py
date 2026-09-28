"""Record unowned activity; project room facts only from complete external proof."""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime

from ..domain.capabilities import RobotProfile
from ..domain.completion import CleaningReceipt, CleaningSource, CompletionQuality
from ..domain.dispatching import RobotObservation
from ..domain.execution import RobotRun
from ..domain.queue import OrchestratorState
from ..domain.types import RobotAvailabilityState


def apply_external_observation(
    state: OrchestratorState,
    observation: RobotObservation,
    profile: RobotProfile,
    now: datetime,
    id_factory: Callable[[], str],
) -> OrchestratorState:
    """Keep uncertain external runs in history without advancing room timestamps."""
    pending = next(
        (
            run
            for run in reversed(tuple(state.robot_runs.values()))
            if run.source is CleaningSource.EXTERNAL
            and run.source_robot_id == observation.source_robot_id
            and run.observed_end is None
        ),
        None,
    )
    observed_at = observation.observed_at or now
    if observed_at > now or (
        pending is not None
        and pending.observed_start is not None
        and observed_at < pending.observed_start
    ):
        return state
    if (
        pending is not None
        and pending.failure_code is None
        and (
            observation.error_code is not None
            or observation.state
            in {RobotAvailabilityState.UNKNOWN, RobotAvailabilityState.UNAVAILABLE}
        )
    ):
        run = replace(pending, failure_code="external_run_interrupted")
        return replace(
            state,
            commit_id=state.commit_id + 1,
            robot_runs={**state.robot_runs, run.robot_run_id: run},
        )
    if observation.cleaning_active is True:
        if pending is not None:
            return state
        run = RobotRun(
            id_factory(),
            observation.source_robot_id,
            observed_start=observed_at,
            history_start=observation.history_start,
            cleaning_activity_seen=True,
            operation=observation.observed_operation,
            source=CleaningSource.EXTERNAL,
            capability_revision=profile.capabilities.revision,
        )
        return replace(
            state,
            commit_id=state.commit_id + 1,
            robot_runs={**state.robot_runs, run.robot_run_id: run},
        )
    if (
        pending is None
        or not observation.normal_end
        or observation.cleaning_active is not False
    ):
        return state
    room_ids: tuple[str, ...] = ()
    proven = (
        pending.failure_code is None
        and pending.capability_revision == profile.capabilities.revision
        and pending.operation in {None, observation.observed_operation}
        and observation.completion_confirmed
        and observation.observed_operation is not None
        and bool(observation.completed_targets)
        and observation.error_code is None
    )
    if proven:
        targets = set(observation.completed_targets)
        matched = {
            room_id: set(profile.capabilities.targets_for(room_id))
            for room_id in profile.capabilities.target_map
            if set(profile.capabilities.targets_for(room_id)) <= targets
        }
        covered = set().union(*matched.values()) if matched else set()
        unique = sum(len(values) for values in matched.values()) == len(covered)
        if (
            covered == targets
            and unique
            and set(matched) <= state.room_registry.rooms.keys()
        ):
            room_ids = tuple(sorted(matched))
    run = replace(
        pending,
        observed_end=observed_at,
        history_end=observation.history_end,
        operation=observation.observed_operation if proven else pending.operation,
        canonical_targets=room_ids,
        completion_quality=CompletionQuality.CONFIRMED if proven else None,
    )
    registry = state.room_registry
    if room_ids and observation.observed_operation is not None:
        registry = registry.record(
            CleaningReceipt(
                f"external:{run.robot_run_id}",
                CleaningSource.EXTERNAL,
                run.robot_run_id,
                room_ids,
                observation.observed_operation,
                observed_at,
                CompletionQuality.CONFIRMED,
                ("external_scope_mode_and_success_confirmed",),
            )
        )
    return replace(
        state,
        commit_id=state.commit_id + 1,
        robot_runs={**state.robot_runs, run.robot_run_id: run},
        room_registry=registry,
    )
