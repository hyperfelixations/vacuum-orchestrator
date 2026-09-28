"""Queue-run commands keep grant expiration atomic with queue lifecycle state."""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime

from ..domain.errors import ValidationError
from ..domain.queue import OrchestratorState
from ..domain.queue_runs import QueueRun
from ..domain.releases import ReleaseKind
from ..domain.types import JobState, QueueMode
from ..domain.validation import seconds
from .room_service import Commit


class QueueRunService:
    """A pause preserves the run; a quiet interval closes it and its grants."""

    def __init__(
        self,
        mutate: Commit,
        clock: Callable[[], datetime],
        id_factory: Callable[[], str],
    ) -> None:
        self._mutate, self._clock, self._id_factory = mutate, clock, id_factory

    async def async_configure(self, grace_seconds: float) -> None:
        """Set the default grace window for future runs."""
        seconds(grace_seconds)
        if grace_seconds > 86400:
            raise ValidationError("queue_grace_out_of_range")

        def configure(state: OrchestratorState) -> OrchestratorState:
            return (
                state
                if state.queue_grace_seconds == grace_seconds
                else replace(
                    state,
                    commit_id=state.commit_id + 1,
                    queue_grace_seconds=grace_seconds,
                )
            )

        await self._mutate(configure)

    async def async_set_mode(self, mode: QueueMode) -> None:
        """Start or resume a run without replacing its identity on repeated requests."""

        def change(state: OrchestratorState) -> OrchestratorState:
            run = state.queue_run
            rooms = dict(state.room_registry.rooms)
            if mode is QueueMode.RUNNING and (run is None or not run.active):
                run = QueueRun(
                    self._id_factory(), self._clock(), state.queue_grace_seconds
                )
                for key, room in rooms.items():
                    grant = room.release
                    if (
                        grant
                        and grant.kind is ReleaseKind.QUEUE_RUN
                        and grant.queue_run_id is None
                    ):
                        rooms[key] = replace(
                            room, release=replace(grant, queue_run_id=run.run_id)
                        )
            elif mode is not QueueMode.RUNNING and run is not None and run.active:
                run = replace(run, idle_since=None)
            if state.mode is mode and state.queue_run == run:
                return state
            return replace(
                state,
                commit_id=state.commit_id + 1,
                mode=mode,
                queue_run=run,
                room_registry=replace(state.room_registry, rooms=rooms),
            )

        await self._mutate(change)

    async def async_reconcile(self, ready: Callable[[OrchestratorState], bool]) -> None:
        """Close only after rechecking current dispatchability under the writer lock."""

        def reconcile(state: OrchestratorState) -> OrchestratorState:
            run = state.queue_run
            if state.mode is not QueueMode.RUNNING or run is None or not run.active:
                return state
            unfinished = bool(state.robot_leases) or any(
                job.state
                in {
                    JobState.DISPATCHING,
                    JobState.RUNNING,
                    JobState.CANCELING,
                    JobState.NEEDS_ATTENTION,
                }
                for job in state.jobs.values()
            )
            if unfinished or ready(state):
                return (
                    state
                    if run.idle_since is None
                    else replace(
                        state,
                        commit_id=state.commit_id + 1,
                        queue_run=replace(run, idle_since=None),
                    )
                )
            now = self._clock()
            run = replace(run, idle_since=run.idle_since or now)
            if run.deadline is not None and now >= run.deadline:
                rooms = {
                    key: replace(room, release=None)
                    if room.release
                    and room.release.kind is ReleaseKind.QUEUE_RUN
                    and room.release.queue_run_id == run.run_id
                    else room
                    for key, room in state.room_registry.rooms.items()
                }
                return replace(
                    state,
                    commit_id=state.commit_id + 1,
                    mode=QueueMode.IDLE,
                    queue_run=replace(run, completed_at=now),
                    room_registry=replace(state.room_registry, rooms=rooms),
                )
            return (
                state
                if run == state.queue_run
                else replace(
                    state,
                    commit_id=state.commit_id + 1,
                    queue_run=run,
                )
            )

        await self._mutate(reconcile)
