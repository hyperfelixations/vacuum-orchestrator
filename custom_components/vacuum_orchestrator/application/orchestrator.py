"""Single-writer application service for integration-wide orchestration."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, cast
from uuid import uuid4

from ..domain.attention import Attention, AttentionKind
from ..domain.correlation import correlate_run
from ..domain.dispatching import (
    RobotObservation,
    RobotSelector,
    assignment_supports_current_capabilities,
)
from ..domain.errors import (
    ConflictError,
    DispatchNotStartedError,
    OrchestratorError,
    PlanningError,
    StaleCommandError,
    StorageIntegrityError,
    located,
)
from ..domain.execution import ExecutionAttempt, RobotLease, RobotRun
from ..domain.faults import blocking
from ..domain.holds import HoldPurpose, JobHold, lease_end
from ..domain.intents import UNSET, JobIntent, JobIntentPatch, TargetRef
from ..domain.monitoring import MonitorAction, next_deadline
from ..domain.permissions import require, returnable
from ..domain.planning import DispatchAssignment, ExecutionPlan, Planner, WorkUnit
from ..domain.progress import Progress, job_progress
from ..domain.queue import Job, OrchestratorState
from ..domain.queue_runs import RunPhase, run_phase
from ..domain.readiness import ReadinessEvaluator, ReadinessReport
from ..domain.requests import CommandOrigin
from ..domain.requirements import StateObservation
from ..domain.types import (
    AttemptState,
    JobState,
    MoveDirection,
    OperationKind,
    QueueMode,
    RobotAvailabilityState,
)
from ..domain.waiting import Waiting, pending
from ..ports.command_scope import check_command_authorization, command_origin
from ..ports.entities import EntityReferences, LiteralEntityReferences
from ..ports.repository import OrchestratorRepository
from ..ports.robot import RobotAdapter
from ..ports.telemetry import adapter_reporter
from .external_observations import apply_external_observation
from .observation_handler import ObservationResult, apply_observation
from .queue_run_service import QueueRunService, closed_now, has_unfinished_work
from .robot_session import RobotCommandTicket, RobotSession
from .room_readiness import RequirementReader, evaluate_room_readiness
from .room_service import RoomService
from .template_service import TemplateService
from .tracing import ObservationTrace, TraceEvent, TraceRecord, TraceRecorder
from .waiting import awaiting_release, explain_waiting

Clock = Callable[[], datetime]
_RECOVERY_TRIGGERS = 20
IdFactory = Callable[[], str]
StateReader = Callable[[tuple[str, ...]], Mapping[str, str | None]]
StateListener = Callable[[], None]


def _utcnow() -> datetime:
    return datetime.now(UTC)


VIEW_SCOPES = frozenset({"queue", "jobs", "rooms", "robots", "templates"})

# Set while a command is validated: its first state change ends it unapplied.
_DRY_RUN: ContextVar[bool] = ContextVar("vacuum_orchestrator_dry_run", default=False)


class _DryRunComplete(Exception):
    """A validated command reached its state change."""


# Committed fields each read model shows; see dev doc "Änderungssignale".
SCOPE_FIELDS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "jobs": (
            "jobs",
            "plans",
            "assignments",
            "attempts",
            "work_unit_states",
            "robot_runs",
            "correlations",
            "job_holds",
        ),
        "queue": (
            "queue",
            "queue_revision",
            "mode",
            "queue_run",
            "queue_grace_seconds",
            "start_delay_seconds",
            "job_defaults",
            "blocked_robots",
        ),
        "rooms": ("room_registry",),
        "robots": ("robot_leases", "blocked_robots"),
        "templates": ("templates",),
    }
)
INTERNAL_FIELDS = frozenset({"installation_id", "commit_id", "robot_generations"})


def changed_scopes(
    previous: OrchestratorState, candidate: OrchestratorState
) -> frozenset[str]:
    """Name the public read models whose committed fields differ."""
    return with_queue(
        scope
        for scope, fields in SCOPE_FIELDS.items()
        if any(getattr(previous, name) != getattr(candidate, name) for name in fields)
    )


def with_queue(scopes: Iterable[str]) -> frozenset[str]:
    """Add the queue to job changes; its page embeds job records."""
    result = set(scopes)
    if "jobs" in result:
        result.add("queue")
    return frozenset(result)


@dataclass(frozen=True, slots=True)
class ObservedSnapshot:
    """Robot observations and the state taken right after them."""

    state: OrchestratorState
    observations: Mapping[str, RobotObservation]


@dataclass(frozen=True, slots=True)
class _Stop:
    """A committed, fenced cancel whose physical stop is still to be sent."""

    job_id: str
    attempt: ExecutionAttempt
    adapter: RobotAdapter
    session: RobotSession
    ticket: RobotCommandTicket
    return_to_dock: bool


def _uuid() -> str:
    return str(uuid4())


def _empty_state_reader(references: tuple[str, ...]) -> Mapping[str, str | None]:
    return {reference: None for reference in references}


class VacuumOrchestrator:
    """Own global jobs, queue state, scheduling, and safe physical dispatch."""

    def __init__(
        self,
        installation_id: str,
        repository: OrchestratorRepository,
        adapters: Mapping[str, RobotAdapter],
        *,
        planner: Planner | None = None,
        selector: RobotSelector | None = None,
        readiness: ReadinessEvaluator | None = None,
        state_reader: StateReader = _empty_state_reader,
        requirement_reader: RequirementReader | None = None,
        entity_references: EntityReferences | None = None,
        clock: Clock = _utcnow,
        id_factory: IdFactory = _uuid,
        trace: TraceRecorder | None = None,
    ) -> None:
        self._installation_id = installation_id
        self._repository = repository
        self._adapters = dict(adapters)
        self._planner = planner or Planner()
        self._selector = selector or RobotSelector()
        self._readiness = readiness or ReadinessEvaluator()
        self._state_reader = state_reader
        self._requirement_reader = requirement_reader or (
            lambda requirements: {
                key: StateObservation(value)
                for key, value in state_reader(
                    tuple(item.entity_id for item in requirements)
                ).items()
            }
        )
        self.entity_references = entity_references or LiteralEntityReferences()
        self._clock = clock
        self._id_factory = id_factory
        self._state: OrchestratorState | None = None
        self._storage_uncertain = False
        self._closing = False
        self._lock = asyncio.Lock()
        self._sessions: dict[str, RobotSession] = {}
        self._listeners: set[StateListener] = set()
        self._view_listeners: set[StateListener] = set()
        self.trace = trace or TraceRecorder()
        self.runtime_id = self.trace.runtime_id
        self.runtime_sequence = 0
        self.changed_scopes: frozenset[str] = VIEW_SCOPES
        self._availability: dict[str, bool] = {}
        self._observations: dict[str, RobotObservation] = {}
        # When each robot's current device faults were first observed.
        self._fault_since: dict[str, datetime] = {}
        # The observation that sent an attempt into recovery, newest last.
        self._recovery_triggers: OrderedDict[str, TraceRecord] = OrderedDict()
        self.rooms = RoomService(self._mutate, lambda: self.state, clock, id_factory)
        self.templates = TemplateService(self._mutate, clock, id_factory)
        self.runs = QueueRunService(self._mutate, clock, id_factory)

    @property
    def state(self) -> OrchestratorState:
        """Return the last verified in-memory snapshot."""
        if self._state is None:
            raise ConflictError("orchestrator_not_initialized")
        return self._state

    @property
    def latest_observations(self) -> Mapping[str, RobotObservation]:
        """Return the last observation of each configured robot, read without I/O."""
        return {
            key: value
            for key, value in self._observations.items()
            if key in self._adapters
        }

    @property
    def adapters(self) -> Mapping[str, RobotAdapter]:
        """Return an independent view of configured execution resources."""
        return dict(self._adapters)

    async def async_replace_adapters(
        self, adapters: Mapping[str, RobotAdapter]
    ) -> None:
        """Refresh idle profiles while retaining the exact adapter of active leases."""
        async with self._lock:
            state = self._verified_state()
            updated = dict(adapters)
            for lease in state.robot_leases.values():
                current = self._adapters.get(lease.robot_id)
                if current is not None:
                    updated[lease.robot_id] = current
            self._adapters = updated
            for robot_id in self._observations.keys() - updated.keys():
                del self._observations[robot_id]
                self._fault_since.pop(robot_id, None)

    async def async_shutdown(self) -> None:
        """Fence future commands and persist unresolved ownership before unloading."""
        async with self._lock:
            self._closing = True
            state = self.state
            candidate = state.resolve_interrupted_leases(self._clock())
            try:
                if candidate is not state:
                    await self._commit_locked(state, candidate)
            finally:
                for source_id, session in self._sessions.items():
                    session.fence(
                        max(
                            session.generation + 1,
                            candidate.robot_generations.get(source_id, 0),
                        ),
                        needs_attention=True,
                    )

    async def async_initialize(self) -> OrchestratorState:
        """Load global state and fence unfinished physical ownership."""
        async with self._lock:
            self.trace.record(TraceEvent.STORAGE, self._clock(), stage="loading")
            loaded = await self._repository.async_load()
            if loaded is None:
                loaded = OrchestratorState.empty(self._installation_id)
                await self._repository.async_commit(
                    loaded, expected_previous_commit_id=-1
                )
            if loaded.installation_id != self._installation_id:
                raise StorageIntegrityError("installation_storage_ownership_mismatch")
            if loaded.robot_leases:
                candidate = loaded.resolve_interrupted_leases(self._clock())
                if candidate is not loaded:
                    await self._repository.async_commit(
                        candidate, expected_previous_commit_id=loaded.commit_id
                    )
                    loaded = candidate
            for source_robot_id, generation in loaded.robot_generations.items():
                self._sessions[source_robot_id] = RobotSession(
                    source_robot_id,
                    generation,
                    needs_attention=source_robot_id in loaded.blocked_robots,
                )
            self._state = loaded
            self.trace.commit_id = loaded.commit_id
            self.trace.run_id = loaded.queue_run.run_id if loaded.queue_run else None
            self.trace.record(TraceEvent.STORAGE, self._clock(), stage="loaded")
            return loaded

    def subscribe(self, listener: StateListener) -> Callable[[], None]:
        """Subscribe presentation adapters to committed state changes."""
        self._listeners.add(listener)

        def unsubscribe() -> None:
            self._listeners.discard(listener)

        return unsubscribe

    def subscribe_view(self, listener: StateListener) -> Callable[[], None]:
        """Observe both durable changes and transient readiness updates."""
        self._view_listeners.add(listener)
        return lambda: self._view_listeners.discard(listener)

    def notify_runtime_change(self, scopes: frozenset[str] = VIEW_SCOPES) -> None:
        """Invalidate read models without scheduling or persisting a clock tick."""
        self.changed_scopes = scopes
        self.runtime_sequence += 1
        self.trace.runtime_sequence = self.runtime_sequence
        for listener in tuple(self._view_listeners):
            try:
                listener()
            except Exception as err:
                self.trace.record(
                    TraceEvent.ERROR,
                    self._clock(),
                    reason="view_listener_failed",
                    error=err,
                )

    def attention(self) -> tuple[Attention, ...]:
        """List what a person has to act on now; see dev doc "Aufmerksamkeit"."""
        state = self.state
        entries: list[Attention] = []
        for robot_id, adapter in self._adapters.items():
            observation = self._observations.get(robot_id)
            operations = adapter.profile.effective_operations
            faults = [
                fault
                for fault in (observation.faults if observation else ())
                if fault.operations & operations
            ]
            if faults:
                lease = state.robot_leases.get(adapter.profile.source_robot_id)
                waiting = state.attempts[lease.attempt_id] if lease else None
                if waiting is not None and waiting.fault_since is None:
                    waiting = None
                entries.append(
                    Attention(
                        AttentionKind.DEVICE_FAULT,
                        robot_id,
                        waiting.job_id if waiting else None,
                        tuple(fault.code for fault in faults),
                        frozenset().union(*(fault.operations for fault in faults))
                        & operations,
                        self._fault_since.get(robot_id),
                        waiting.fault_deadline if waiting else None,
                    )
                )
        robots = {
            adapter.profile.source_robot_id: robot_id
            for robot_id, adapter in self._adapters.items()
        }
        entries.extend(
            Attention(
                AttentionKind.ROBOT_RECOVERY,
                robots.get(source_id, source_id),
                None,
                (reason,),
            )
            for source_id, reason in state.blocked_robots.items()
        )
        for job in state.jobs.values():
            if job.state is not JobState.NEEDS_ATTENTION:
                continue
            attempt = (
                state.attempts.get(job.active_attempt_id)
                if job.active_attempt_id
                else None
            )
            entries.append(
                Attention(
                    AttentionKind.JOB_RECOVERY,
                    attempt.robot_id if attempt else None,
                    job.job_id,
                    (job.failure_code,) if job.failure_code else (),
                )
            )
        return tuple(entries)

    def now(self) -> datetime:
        """Return the clock every deadline and read model is based on."""
        return self._clock()

    def active_room_ids(self) -> tuple[str, ...]:
        """Resolve an all-rooms selection; see dev doc "Alle Räume"."""
        rooms = self.state.room_registry.active_room_ids()
        if not rooms:
            raise ConflictError("no_active_rooms")
        return rooms

    def readiness_for_job(
        self,
        job_id: str,
        *,
        robot_id: str | None = None,
        operation: OperationKind | None = None,
    ) -> ReadinessReport:
        """Evaluate one job without considering robot availability."""
        job = self.state.jobs.get(job_id)
        if job is None:
            raise ConflictError("unknown_job")
        return self.job_readiness(
            self.state, job, robot_id=robot_id, operation=operation
        )

    def job_readiness(
        self,
        state: OrchestratorState,
        job: Job,
        *,
        robot_id: str | None = None,
        operation: OperationKind | None = None,
    ) -> ReadinessReport:
        """Evaluate a stored or hypothetical job against one state snapshot."""
        references = (*job.intent.required_on, *job.intent.required_off)
        base = self._readiness.evaluate(job.intent, self._state_reader(references))
        return evaluate_room_readiness(
            job,
            state.room_registry,
            base,
            self._requirement_reader,
            self._clock(),
            robot_id=robot_id,
            operation=operation,
            robot_requirements=self._adapters[robot_id].profile.requirements
            if robot_id in self._adapters
            else (),
        )

    def waiting(self, job_id: str) -> Waiting | None:
        """Explain why a waiting job has not started; None once nothing blocks."""
        job = self.state.jobs.get(job_id)
        if job is None:
            raise ConflictError("unknown_job", job_id)
        return explain_waiting(self, self.state, job, self._clock())

    def progress(self, job_id: str) -> Progress | None:
        """Describe a started job from its attempt and the latest observation."""
        state = self.state
        job = state.jobs.get(job_id)
        if job is None:
            raise ConflictError("unknown_job", job_id)
        attempt = (
            state.attempts.get(job.active_attempt_id)
            if job.active_attempt_id is not None
            else None
        )
        return job_progress(
            job,
            state.plans.get(job.plan_id) if job.plan_id is not None else None,
            attempt,
            self._observations.get(attempt.robot_id) if attempt is not None else None,
        )

    def jobs_awaiting_release(
        self, state: OrchestratorState | None = None
    ) -> dict[str, tuple[str, ...]]:
        """Map each room to the waiting jobs that need its release, queue first."""
        state = self.state if state is None else state
        waiting: dict[str, list[str]] = {}
        for job_id in state.queue:
            report = self.job_readiness(state, state.jobs[job_id])
            for room_id in awaiting_release(state, report):
                waiting.setdefault(room_id, []).append(job_id)
        return {key: tuple(value) for key, value in waiting.items()}

    def readiness_before_start(self, job_id: str) -> ReadinessReport | None:
        """Explain the next start; running attempts are never re-evaluated."""
        job = self.state.jobs.get(job_id)
        if job is None:
            raise ConflictError("unknown_job", job_id)
        return self._readiness_before_start(self.state, job)

    def _readiness_before_start(
        self, state: OrchestratorState, job: Job
    ) -> ReadinessReport | None:
        if job.state is JobState.QUEUED:
            return self.job_readiness(state, job)
        if job.state is JobState.DISPATCHING and job.active_attempt_id is None:
            unit = state.next_pending_unit(job)
            return self.job_readiness(state, job, operation=unit.operation)
        return None

    def view_projection(self, state: OrchestratorState) -> dict[str, object]:
        """Derive the read-model parts that depend on more than their own fields.

        Commit signals and the controller fingerprint compare exactly these
        values; see dev doc "Änderungssignale". No I/O.
        """
        now = self._clock()
        named = {
            target.area_id for job in state.jobs.values() for target in job.intent.areas
        }
        return {
            "jobs": (
                tuple(
                    (
                        job_id,
                        self._readiness_before_start(state, job),
                        explain_waiting(self, state, job, now),
                    )
                    for job_id, job in state.jobs.items()
                    if job.state in {JobState.QUEUED, JobState.DISPATCHING}
                ),
                # Job records name their rooms by area.
                tuple(
                    (room_id, room.area_id)
                    for room_id, room in state.room_registry.rooms.items()
                    if room_id in named
                ),
            ),
            "rooms": self.jobs_awaiting_release(state),
        }

    def _commit_scopes(
        self, previous: OrchestratorState, candidate: OrchestratorState
    ) -> frozenset[str]:
        """Add derived read models a commit changed; signal all if unsure."""
        try:
            before = self.view_projection(previous)
            after = self.view_projection(candidate)
        except Exception as err:
            self.trace.record(
                TraceEvent.ERROR,
                self._clock(),
                reason="view_projection_failed",
                error=err,
            )
            return VIEW_SCOPES
        return with_queue(
            {
                *changed_scopes(previous, candidate),
                *(scope for scope, value in after.items() if before[scope] != value),
            }
        )

    async def async_create_job(self, intent: JobIntent) -> str:
        """Create and append a validated robot-independent job."""
        job_id = self._id_factory()
        await self._mutate(
            lambda state: state.add_job(
                job_id,
                self._canonical_intent(state, intent),
                self._clock(),
                origin=command_origin.get(),
            )
        )
        return job_id

    async def async_update_job(
        self, job_id: str, patch: JobIntentPatch, *, hold_id: str | None = None
    ) -> None:
        """Apply a partial update; with the holder's `hold_id` it ends the hold."""

        def update(state: OrchestratorState) -> OrchestratorState:
            if job_id not in state.jobs:
                raise ConflictError("unknown_job")
            canonical = patch
            if patch.areas is not UNSET:
                intent = self._canonical_intent(
                    state, replace(state.jobs[job_id].intent, areas=patch.areas)
                )
                canonical = replace(patch, areas=intent.areas)
            return state.update_job(job_id, canonical, self._clock(), hold_id=hold_id)

        await self._mutate(update)

    async def async_hold_job(self, job_id: str, purpose: HoldPurpose) -> JobHold:
        """Keep a waiting job from starting; see dev doc "Bearbeitungsschutz"."""
        now = self._clock()
        hold = JobHold(self._id_factory(), job_id, purpose, now, lease_end(now))
        await self._mutate(lambda state: state.hold_job(hold, now))
        return hold

    async def async_renew_job_hold(self, hold_id: str) -> JobHold:
        """Extend the caller's hold by one lease period."""
        await self._mutate(lambda state: state.renew_job_hold(hold_id, self._clock()))
        return next(
            hold for hold in self.state.job_holds.values() if hold.hold_id == hold_id
        )

    async def async_release_job_hold(self, hold_id: str) -> None:
        """End the caller's hold; the job's start delay begins again."""
        await self._mutate(lambda state: state.release_job_hold(hold_id, self._clock()))

    async def async_expire_job_holds(self) -> None:
        """End lapsed holds; their jobs get a new start delay."""
        await self._mutate(lambda state: state.expire_job_holds(self._clock()))

    def job_hold(self, job_id: str) -> JobHold | None:
        """Return the hold that protects a job right now."""
        return self.state.active_hold(job_id, self._clock())

    async def async_configure_job_defaults(self, changes: Mapping[str, object]) -> None:
        """Replace named defaults for future jobs; existing jobs keep their values."""

        def configure(state: OrchestratorState) -> OrchestratorState:
            defaults = replace(
                state.job_defaults, **cast(dict[str, Any], changes), configured=True
            )
            if defaults == state.job_defaults:
                return state
            return replace(state, commit_id=state.commit_id + 1, job_defaults=defaults)

        await self._mutate(configure)

    async def async_delete_job(
        self, job_id: str, *, hold_id: str | None = None
    ) -> None:
        """Delete queued or terminal work that no one else holds."""
        await self._mutate(
            lambda state: state.delete_job(job_id, self._clock(), hold_id=hold_id)
        )

    async def async_move_job(self, job_id: str, direction: MoveDirection) -> None:
        """Move a pending job using one intuitive direction."""
        await self._mutate(lambda state: state.move_job(job_id, direction))

    async def async_set_queue_mode(self, mode: QueueMode) -> None:
        """Set automatic queue dispatch mode."""
        await self.runs.async_set_mode(mode)

    async def async_reconcile_queue_run(self) -> None:
        """Track queue quiescence without mistaking blocked pending work for a run."""
        await self.runs.async_set_mode(self.state.mode)
        run = self.state.queue_run
        if self.state.mode is not QueueMode.RUNNING and not (run and run.ending):
            return
        await self._observe_robots()
        await self.runs.async_reconcile(self.has_pending_work)

    def has_pending_work(self, state: OrchestratorState) -> bool:
        """Return whether a waiting job starts once only its hold or delay pass."""
        now = self._clock()
        return any(
            pending(explain_waiting(self, state, job, now))
            for job in (state.jobs[job_id] for job_id in state.queue)
            if job.state is JobState.QUEUED
        )

    def run_phase(self) -> RunPhase:
        """Classify the queue from committed state and the latest observations."""
        state = self.state
        return run_phase(
            state.mode,
            state.queue_run,
            has_work=has_unfinished_work(state) or self.has_pending_work(state),
        )

    async def async_end_queue(
        self, *, cancel_running: bool = False, return_to_dock: bool = False
    ) -> None:
        """End the queue run: let started jobs finish, or cancel them first.

        Cancelling closes the run and fences every started job in one commit
        before the first stop is sent; see dev doc "Queue-Ende".
        """
        if not cancel_running:
            await self.runs.async_end(close=False)
            return
        failure: Exception | None = None
        stops: list[_Stop] = []
        async with self._lock:
            previous = self._verified_state()
            started = [
                job
                for job in previous.jobs.values()
                if job.state in {JobState.DISPATCHING, JobState.RUNNING}
            ]
            if return_to_dock:
                for job in started:
                    self._require_return(previous, job.active_attempt_id)
            now = self._clock()
            candidate = previous
            requested: list[tuple[str, str | None, int | None]] = []
            for job in started:
                try:
                    candidate, generation = candidate.request_cancel(
                        job.job_id, now, return_to_dock=return_to_dock
                    )
                except OrchestratorError as err:
                    failure = failure or err
                    continue
                requested.append((job.job_id, job.active_attempt_id, generation))
            candidate = closed_now(candidate, now)
            if candidate is not previous:
                # Every change above is one decision and one commit.
                candidate = replace(candidate, commit_id=previous.commit_id + 1)
                await self._commit_locked(previous, candidate)
            for job_id, attempt_id, generation in requested:
                try:
                    stop = self._fence_stop(
                        candidate, job_id, attempt_id, generation, return_to_dock
                    )
                except OrchestratorError as err:
                    failure = failure or err
                    continue
                if stop is not None:
                    stops.append(stop)
        for stop in stops:
            try:
                await self._async_send_stop(stop)
            except Exception as err:
                failure = failure or err
        if failure is not None:
            raise failure

    async def async_retry_job(self, job_id: str) -> str:
        """Create a distinct retry while preserving terminal history."""
        retry_job_id = self._id_factory()
        await self._mutate(
            lambda state: state.retry_job(
                job_id, retry_job_id, self._clock(), origin=command_origin.get()
            )
        )
        return retry_job_id

    async def async_start_job(
        self, job_id: str, robot_id: str | None = None
    ) -> DispatchAssignment:
        """Start one job using an optional requested robot and shared policy."""
        assignment = await self._async_dispatch_job(
            job_id, robot_id, origin=command_origin.get()
        )
        if assignment is None:
            raise PlanningError("job_not_startable")
        return assignment

    async def async_create_and_start_job(
        self, intent: JobIntent, robot_id: str | None = None
    ) -> tuple[str, DispatchAssignment]:
        """Create and reserve atomically; nothing is created if it cannot start.

        Failures after the reservation behave like `start_job`; see dev doc
        "Sofortstart".
        """
        job_id = self._id_factory()
        assignment = await self._async_dispatch_job(
            job_id, robot_id, origin=command_origin.get(), intent=intent
        )
        if assignment is None:
            raise PlanningError("job_not_startable")
        return job_id, assignment

    async def async_run_queue(self) -> tuple[DispatchAssignment, ...]:
        """Enable queue processing and dispatch every currently safe candidate."""
        await self.async_set_queue_mode(QueueMode.RUNNING)
        return await self.async_dispatch_available()

    async def async_dispatch_available(self) -> tuple[DispatchAssignment, ...]:
        """Dispatch continuations first, then scan queued jobs without reordering.

        Lapsed holds end first, so their jobs wait for a new start delay.
        """
        await self.async_expire_job_holds()
        state = self._verified_state()
        candidates = [
            job.job_id
            for job in state.jobs.values()
            if job.state is JobState.DISPATCHING and job.active_attempt_id is None
        ]
        if state.mode is QueueMode.RUNNING:
            candidates.extend(state.queue)
        dispatched: list[DispatchAssignment] = []
        for job_id in candidates:
            try:
                assignment = await self._async_dispatch_job(
                    job_id, None, automatic=True
                )
            except (PlanningError, ConflictError) as err:
                if err.code not in {
                    "job_blocked",
                    "job_unknown",
                    "start_delayed",
                    "job_held",
                    "queue_not_running",
                }:
                    self.trace.record(
                        TraceEvent.BLOCKED,
                        self._clock(),
                        job_id=job_id,
                        reason=err.code,
                    )
                continue
            except StorageIntegrityError:
                raise
            except Exception as err:
                self._verified_state()
                self.trace.record(
                    TraceEvent.ERROR,
                    self._clock(),
                    job_id=job_id,
                    reason="dispatch_failed",
                    error=err,
                )
                continue
            if assignment is not None:
                dispatched.append(assignment)
        return tuple(dispatched)

    async def async_cancel_job(
        self, job_id: str, *, return_to_dock: bool = False
    ) -> None:
        """Cancel queued work or fence an active robot before physical stop.

        With `return_to_dock` the stop is followed by a return under the same
        lease; the cancel completes only once the robot rests at its dock.
        """
        async with self._lock:
            previous = self._verified_state()
            job = previous.jobs.get(job_id)
            attempt_id = job.active_attempt_id if job is not None else None
            if return_to_dock:
                self._require_return(previous, attempt_id)
            candidate, generation = previous.request_cancel(
                job_id, self._clock(), return_to_dock=return_to_dock
            )
            await self._commit_locked(previous, candidate)
            stop = self._fence_stop(
                candidate, job_id, attempt_id, generation, return_to_dock
            )
        if stop is not None:
            await self._async_send_stop(stop)

    def _require_return(self, state: OrchestratorState, attempt_id: str | None) -> None:
        """Refuse a return before any stop when the assigned robot cannot."""
        attempt = state.attempts.get(attempt_id or "")
        robot = attempt and self._adapters.get(attempt.robot_id)
        if robot and not robot.profile.capabilities.returns_to_dock:
            raise ConflictError("return_to_dock_unsupported")

    def _fence_stop(
        self,
        state: OrchestratorState,
        job_id: str,
        attempt_id: str | None,
        generation: int | None,
        return_to_dock: bool,
    ) -> _Stop | None:
        """Fence a committed cancel; return the physical stop still to send."""
        if generation is None:
            return None
        if attempt_id is None:
            raise ConflictError("active_attempt_missing")
        attempt = state.attempts[attempt_id]
        session = self._sessions[attempt.source_robot_id]
        ticket = session.fence(generation, needs_attention=False)
        if attempt.state is AttemptState.CANCELLED:
            return None
        adapter = self._adapters.get(attempt.robot_id)
        if adapter is None:
            raise ConflictError("assigned_robot_missing")
        return _Stop(job_id, attempt, adapter, session, ticket, return_to_dock)

    async def _async_send_stop(self, stop: _Stop) -> None:
        """Send one fenced stop outside the writer lock and record it."""
        attempt = stop.attempt
        telemetry_token = adapter_reporter.set(
            lambda event, stage, reason: self.trace.record(
                event,
                self._clock(),
                stage=stage,
                reason=reason,
                job_id=stop.job_id,
                attempt_id=attempt.attempt_id,
                robot_id=attempt.robot_id,
            )
        )
        try:
            await stop.session.cancel(
                stop.ticket,
                lambda: stop.adapter.async_cancel(return_to_dock=stop.return_to_dock),
            )
        except Exception as err:
            await self._mark_robot_uncertain(attempt.attempt_id, err)
            raise
        finally:
            adapter_reporter.reset(telemetry_token)
        await self._mutate(
            lambda state: state.mark_stop_sent(attempt.attempt_id, self._clock())
        )

    async def async_return_robot(self, robot_id: str) -> None:
        """Send an idle robot home; never while an attempt owns it."""
        async with self._lock:
            state = self._verified_state()
            adapter = self._adapters.get(robot_id)
            if adapter is None:
                raise ConflictError("unknown_robot", robot_id)
            source = adapter.profile.source_robot_id
            require(
                returnable(
                    source in state.robot_leases,
                    source in state.blocked_robots,
                    adapter.profile.capabilities.returns_to_dock,
                )
            )
            session = self._sessions.setdefault(
                source, RobotSession(source, state.robot_generations.get(source, 0))
            )
            ticket = session.idle_ticket()
        self.trace.record(
            TraceEvent.PHYSICAL, self._clock(), stage="return", robot_id=robot_id
        )
        await session.run_idle(ticket, adapter.async_return_to_dock)

    async def async_confirm_cancel(self, job_id: str) -> None:
        """Persist observed cancellation and continue eligible queue work."""
        attempt_id = self.state.jobs[job_id].active_attempt_id
        source_robot_id = (
            None
            if attempt_id is None
            else self.state.attempts[attempt_id].source_robot_id
        )
        await self._mutate(lambda state: state.confirm_cancel(job_id, self._clock()))
        if attempt_id is not None and source_robot_id is not None:
            self._sessions[source_robot_id].release(attempt_id)
        await self.async_dispatch_available()

    async def async_resolve_recovery(
        self, robot_id: str, *, confirm_stopped: bool = False
    ) -> None:
        """Resolve ownership without claiming cleaning success or retrying work."""
        adapter = self._adapters.get(robot_id)
        source_id = (
            adapter.profile.source_robot_id
            if adapter is not None
            else next(
                (
                    source
                    for source, lease in self.state.robot_leases.items()
                    if lease.robot_id == robot_id
                ),
                robot_id if robot_id in self.state.blocked_robots else None,
            )
        )
        if source_id is None:
            raise ConflictError("unknown_robot")
        try:
            observation = await adapter.async_observe() if adapter else None
            stopped = (
                observation is not None
                and observation.state is RobotAvailabilityState.AVAILABLE
                and observation.normal_end
                and observation.cleaning_active is False
            )
        except Exception:
            stopped = False
        if not stopped and not confirm_stopped:
            raise ConflictError("robot_stopped_confirmation_required")
        async with self._lock:
            state = self._verified_state()
            candidate = state.resolve_recovery(
                source_id, self._clock(), assumed_stopped=not stopped
            )
            await self._commit_locked(state, candidate)
            session = self._sessions.setdefault(
                source_id,
                RobotSession(
                    source_id,
                    state.robot_generations.get(source_id, 0),
                    needs_attention=True,
                ),
            )
            session.fence(candidate.robot_generations[source_id], needs_attention=False)
            lease = state.robot_leases.get(source_id)
            attempt = state.attempts[lease.attempt_id] if lease else None
            self.trace.record(
                TraceEvent.RECOVERY,
                self._clock(),
                robot_id=robot_id,
                job_id=attempt.job_id if attempt else None,
                attempt_id=attempt.attempt_id if attempt else None,
                reason="operator_assumed_stopped"
                if not stopped
                else "verified_stopped",
                stage="abandoned" if attempt else None,
            )
        await self.async_dispatch_available()

    async def async_confirm_start(
        self, attempt_id: str, observed_at: datetime | None = None
    ) -> None:
        """Persist observed physical activity start evidence."""
        await self._mutate(
            lambda state: state.mark_start_confirmed(
                attempt_id, observed_at or self._clock()
            )
        )

    async def async_process_robot_observation(self, robot_id: str) -> None:
        """Apply normalized evidence and isolate faults to the affected robot."""
        adapter = self._adapters.get(robot_id)
        if adapter is None:
            raise ConflictError("unknown_robot")
        try:
            observation = await adapter.async_observe()
        except Exception as err:
            self.trace.record(
                TraceEvent.ERROR,
                self._clock(),
                robot_id=robot_id,
                reason="observation_failed",
                error=err,
            )
            observation = RobotObservation(
                robot_id,
                adapter.profile.source_robot_id,
                RobotAvailabilityState.UNKNOWN,
                observed_at=self._clock(),
                reason="observation_failed",
            )
        async with self._lock:
            state = self._verified_state()
            self._record_availability(observation)
            lease = state.robot_leases.get(observation.source_robot_id)
            result = (
                apply_observation(state, observation, self._clock(), self._id_factory)
                if lease is not None
                else ObservationResult(
                    apply_external_observation(
                        state,
                        observation,
                        adapter.profile,
                        self._clock(),
                        self._id_factory,
                    )
                )
            )
            self._trace_observation(robot_id, observation, state, result)
            candidate = result.state
            if candidate is state:
                return
            await self._commit_locked(state, candidate)
            if lease is not None:
                session = self._sessions.get(lease.source_robot_id)
                if session is not None:
                    if (
                        candidate.attempts[lease.attempt_id].state
                        is AttemptState.RECOVERY_REQUIRED
                    ):
                        session.fence(
                            candidate.robot_generations[lease.source_robot_id],
                            needs_attention=True,
                        )
                    elif lease.source_robot_id not in candidate.robot_leases:
                        session.release(lease.attempt_id)
        await self.async_dispatch_available()

    def _trace_observation(
        self,
        robot_id: str,
        observation: RobotObservation,
        state: OrchestratorState,
        result: ObservationResult,
    ) -> None:
        """Record the monitor inputs and the decision actually applied."""
        lease = state.robot_leases.get(observation.source_robot_id)
        previous = state.attempts[lease.attempt_id] if lease else None
        current = result.state.attempts.get(lease.attempt_id) if lease else None
        deadline = next_deadline(current) if current else None
        record = self.trace.record(
            TraceEvent.OBSERVATION,
            self._clock(),
            robot_id=robot_id,
            attempt_id=previous.attempt_id if previous else None,
            job_id=previous.job_id if previous else None,
            state=observation.state.value,
            reason=observation.reason,
            operation=result.operation.value if result.operation else None,
            observation=ObservationTrace(
                observed_at=observation.observed_at.isoformat()
                if observation.observed_at
                else None,
                phase=observation.phase.value,
                cleaning_active=observation.cleaning_active,
                normal_end=observation.normal_end,
                at_dock=observation.at_dock,
                completion_confirmed=observation.completion_confirmed,
                observed_operation=observation.observed_operation.value
                if observation.observed_operation
                else None,
                completed_operation=observation.completed_operation.value
                if observation.completed_operation
                else None,
                faults=",".join(
                    f"{fault.code}:{fault.scope.value}:{fault.source.value}"
                    for fault in observation.faults
                )
                or None,
                targets=result.targets,
                previous_state=previous.state.value if previous else None,
                monitor_action=result.decision.action.value
                if result.decision
                else None,
                monitor_reason=result.decision.reason if result.decision else None,
                deadline_at=deadline.isoformat() if deadline else None,
                terminal_observed_at=current.terminal_observed_at.isoformat()
                if current and current.terminal_observed_at
                else None,
            ),
        )
        if (
            previous is not None
            and result.decision is not None
            and result.decision.action is MonitorAction.ATTENTION
        ):
            self._recovery_triggers[previous.attempt_id] = record
            while len(self._recovery_triggers) > _RECOVERY_TRIGGERS:
                self._recovery_triggers.popitem(last=False)

    def recovery_trigger(self, attempt_id: str) -> Mapping[str, Any] | None:
        """Return the observation that sent an attempt into recovery, if kept."""
        record = self._recovery_triggers.get(attempt_id)
        return asdict(record) if record is not None else None

    async def async_record_robot_run(self, attempt_id: str, run: RobotRun) -> None:
        """Correlate physical evidence, complete the unit, and schedule follow-up."""
        async with self._lock:
            previous = self._verified_state()
            attempt = previous.attempts.get(attempt_id)
            if attempt is None:
                raise ConflictError("unknown_attempt")
            correlation = correlate_run(attempt, run)
            candidate = previous.complete_attempt(
                attempt_id, run, correlation, self._clock()
            )
            await self._commit_locked(previous, candidate)
            session = self._sessions.get(attempt.source_robot_id)
            if session is not None:
                if correlation.requires_attention:
                    session.fence(
                        candidate.robot_generations[attempt.source_robot_id],
                        needs_attention=True,
                    )
                else:
                    session.release(attempt_id)
        await self.async_dispatch_available()

    async def _async_dispatch_job(
        self,
        job_id: str,
        requested_robot_id: str | None,
        *,
        origin: CommandOrigin | None = None,
        intent: JobIntent | None = None,
        automatic: bool = False,
    ) -> DispatchAssignment | None:
        """Select, reserve and start; with `intent`, create the job in that commit.

        Only `automatic` queue starts honor a job's start delay.
        """
        observations = await self._observe_robots()
        async with self._lock:
            committed = self._verified_state()
            previous = (
                committed
                if intent is None
                else committed.add_job(
                    job_id,
                    self._canonical_intent(committed, intent),
                    self._clock(),
                    origin=origin,
                )
            )
            job = previous.jobs.get(job_id)
            if job is None:
                raise ConflictError("unknown_job")
            if job.state not in {JobState.QUEUED, JobState.DISPATCHING}:
                raise ConflictError("job_not_dispatchable")
            if job.state is JobState.QUEUED:
                # The candidate list predates the observation await; a pause or
                # end saved meanwhile wins. Next phases continue while paused.
                if automatic and previous.mode is not QueueMode.RUNNING:
                    raise PlanningError("queue_not_running")
                hold = previous.job_holds.get(job_id)
                if hold is not None and (automatic or hold.active(self._clock())):
                    raise ConflictError("job_held", hold.purpose.value)
                if (
                    automatic
                    and job.start_after is not None
                    and self._clock() < job.start_after
                ):
                    raise PlanningError("start_delayed")
                report = self.job_readiness(previous, job)
                if report.state.value != "ready":
                    self.trace.record(
                        TraceEvent.BLOCKED,
                        self._clock(),
                        job_id=job_id,
                        state=report.state.value,
                        reason="job_prerequisites",
                    )
                    raise PlanningError(f"job_{report.state.value}")
                plan = self._planner.create_plan(job.job_id, job.intent)
                unit = plan.work_units[0]
            else:
                plan = self._existing_plan(previous, job)
                unit = previous.next_pending_unit(job)
            assignment = self.select_robot(
                previous, job, unit, observations, requested_robot_id
            )
            adapter = self._adapters.get(assignment.robot_id)
            if adapter is None or not assignment_supports_current_capabilities(
                assignment, adapter.profile
            ):
                raise ConflictError("capabilities_changed_before_dispatch")
            generation = (
                previous.robot_generations.get(assignment.source_robot_id, 0) + 1
            )
            attempt_id = self._id_factory()
            now = self._clock()
            attempt = ExecutionAttempt(
                attempt_id,
                job.job_id,
                unit.work_unit_id,
                assignment.robot_id,
                assignment.source_robot_id,
                generation,
                AttemptState.PREPARED,
                now,
                policy=adapter.profile.execution_policy,
                prior_history_start=observations[assignment.robot_id].history_start,
                prior_history_end=observations[assignment.robot_id].history_end,
            )
            lease = RobotLease(
                assignment.source_robot_id,
                assignment.robot_id,
                attempt_id,
                unit.work_unit_id,
                generation,
            )
            admitted = replace(
                previous,
                jobs={
                    **previous.jobs,
                    job_id: replace(job, origin=origin or job.origin),
                },
                room_registry=previous.room_registry.admit(
                    job_id, unit.canonical_targets, now
                ),
            )
            prepared = admitted.prepare_dispatch(
                job.job_id, plan, unit, assignment, attempt, lease, now
            )
            await self._commit_locked(
                committed, replace(prepared, commit_id=committed.commit_id + 1)
            )
            session = self._sessions.setdefault(
                assignment.source_robot_id,
                RobotSession(assignment.source_robot_id, generation - 1),
            )
            session.fence(generation, needs_attention=False)
            ticket = session.reserve(attempt_id)

        async def prepare() -> None:
            try:
                async with asyncio.timeout(attempt.policy.start_seconds):
                    await adapter.async_prepare(unit, assignment)
            except TimeoutError as err:
                raise DispatchNotStartedError("preparation_timeout") from err

        async def boundary() -> None:
            async with self._lock:
                check_command_authorization()
                current = self._verified_state()
                sent = current.mark_command_sent(attempt_id, self._clock())
                await self._commit_locked(current, sent)

        origin_token = command_origin.set(origin or job.origin)
        telemetry_token = adapter_reporter.set(
            lambda event, stage, reason: self.trace.record(
                event,
                self._clock(),
                stage=stage,
                reason=reason,
                job_id=job_id,
                attempt_id=attempt_id,
                robot_id=assignment.robot_id,
                work_unit_id=unit.work_unit_id,
                operation=unit.operation.value,
            )
        )
        self.trace.record(
            TraceEvent.PHYSICAL,
            self._clock(),
            stage="selected",
            job_id=job_id,
            attempt_id=attempt_id,
            robot_id=assignment.robot_id,
            work_unit_id=unit.work_unit_id,
            operation=unit.operation.value,
        )
        try:
            await session.dispatch(
                ticket,
                prepare,
                boundary,
                lambda: adapter.async_start(unit, assignment),
                lambda: self._guard_dispatch(job_id, assignment, unit.operation),
            )
        except StaleCommandError:
            self.trace.record(
                TraceEvent.PHYSICAL,
                self._clock(),
                stage="stale",
                job_id=job_id,
                attempt_id=attempt_id,
            )
            return None
        except DispatchNotStartedError as err:
            self.trace.record(
                TraceEvent.PHYSICAL,
                self._clock(),
                stage="rejected",
                reason=err.code,
                job_id=job_id,
                attempt_id=attempt_id,
            )
            await self._finish_unstarted(attempt_id, err.code)
            raise
        except Exception as err:
            await self._mark_robot_uncertain(attempt_id, err)
            raise
        finally:
            adapter_reporter.reset(telemetry_token)
            command_origin.reset(origin_token)
        async with self._lock:
            current = self._verified_state()
            accepted = current.mark_dispatch_accepted(attempt_id, self._clock())
            if accepted is not current:
                await self._commit_locked(current, accepted)
        return assignment

    def select_robot(
        self,
        state: OrchestratorState,
        job: Job,
        unit: WorkUnit,
        observations: Mapping[str, RobotObservation],
        requested_robot_id: str | None = None,
    ) -> DispatchAssignment:
        """Apply per-robot job conditions, then the selector, as dispatch does."""
        profiles = tuple(adapter.profile for adapter in self._adapters.values())
        ready_profiles = tuple(
            profile
            for profile in profiles
            if self.job_readiness(
                state, job, robot_id=profile.robot_id, operation=unit.operation
            ).state.value
            == "ready"
        )
        if profiles and not ready_profiles:
            raise PlanningError("job_conditions_not_satisfied")
        return self._selector.assign(
            unit,
            ready_profiles,
            observations,
            state.robot_leases,
            frozenset(state.blocked_robots),
            state.active_target_sets(excluding_job_id=job.job_id),
            requested_robot_id,
            defaults=state.job_defaults,
        )

    async def async_observed_snapshot(self) -> ObservedSnapshot:
        """Observe robots, then take the state that belongs to the observations.

        Callers derive everything else synchronously, without a further await;
        see dev doc "Vorschau".
        """
        adapters = self._adapters
        observations = await self._observe_robots()
        if self._adapters is not adapters:
            # Robots replaced while observing are observed once more.
            observations = await self._observe_robots()
        return ObservedSnapshot(self.state, MappingProxyType(observations))

    async def _observe_robots(self) -> dict[str, RobotObservation]:
        adapters = dict(self._adapters)
        observations = await asyncio.gather(
            *(adapter.async_observe() for adapter in adapters.values()),
            return_exceptions=True,
        )
        result = {
            robot_id: (
                observation
                if isinstance(observation, RobotObservation)
                else RobotObservation(
                    robot_id,
                    adapters[robot_id].profile.source_robot_id,
                    RobotAvailabilityState.UNKNOWN,
                    reason="observation_failed",
                )
            )
            for robot_id, observation in zip(adapters, observations, strict=True)
        }
        for observation in result.values():
            self._record_availability(observation)
        return result

    def _record_availability(self, observation: RobotObservation) -> None:
        self._observations[observation.robot_id] = observation
        if blocking(observation.faults, None):
            self._fault_since.setdefault(
                observation.robot_id, observation.observed_at or self._clock()
            )
        else:
            self._fault_since.pop(observation.robot_id, None)
        online = observation.state not in {
            RobotAvailabilityState.UNKNOWN,
            RobotAvailabilityState.UNAVAILABLE,
        }
        previous = self._availability.get(observation.robot_id)
        self._availability = {
            key: value
            for key, value in self._availability.items()
            if key in self._adapters
        }
        self._availability[observation.robot_id] = online
        if previous is not online:
            self.trace.record(
                TraceEvent.AVAILABILITY,
                self._clock(),
                robot_id=observation.robot_id,
                state="online" if online else "offline",
                reason=observation.reason,
            )

    def _guard_dispatch(
        self, job_id: str, assignment: DispatchAssignment, operation: OperationKind
    ) -> None:
        self._verified_state()
        report = self.readiness_for_job(
            job_id, robot_id=assignment.robot_id, operation=operation
        )
        if report.state.value != "ready":
            raise DispatchNotStartedError("readiness_changed_before_start")

    async def _finish_unstarted(self, attempt_id: str, reason: str) -> None:
        async with self._lock:
            state = self._verified_state()
            attempt = state.attempts[attempt_id]
            if attempt.state in {
                AttemptState.SUCCEEDED,
                AttemptState.FAILED,
                AttemptState.CANCELLED,
                AttemptState.RECOVERY_REQUIRED,
            }:
                return
            if attempt.observed_start_at is not None:
                candidate = state.require_robot_attention(
                    attempt_id, reason, None, None, self._clock()
                )
            elif attempt.state is AttemptState.CANCEL_PENDING:
                candidate = state.confirm_cancel(
                    attempt.job_id, self._clock(), never_started=True
                )
            else:
                candidate = state.fail_job(
                    attempt.job_id,
                    attempt_id,
                    reason,
                    self._clock(),
                    never_started=True,
                )
            await self._commit_locked(state, candidate)
            session = self._sessions[attempt.source_robot_id]
            if attempt.observed_start_at is not None:
                session.fence(
                    candidate.robot_generations[attempt.source_robot_id],
                    needs_attention=True,
                )
            else:
                session.release(attempt_id)

    @staticmethod
    def _canonical_intent(state: OrchestratorState, intent: JobIntent) -> JobIntent:
        """Name every target by its room; only active rooms take new work."""
        areas = []
        for index, target in enumerate(intent.areas):
            with located("areas", index):
                room = state.room_registry.resolve(target.area_id)
                if not room.enabled or room.area_missing:
                    raise ConflictError("room_unavailable", target.area_id)
            areas.append(TargetRef(room.room_id, target.map_context))
        return replace(intent, areas=tuple(areas))

    @staticmethod
    def _existing_plan(state: OrchestratorState, job: Job) -> ExecutionPlan:
        if job.plan_id is None or job.plan_id not in state.plans:
            raise ConflictError("job_plan_missing")
        return state.plans[job.plan_id]

    async def async_dry_run(self, command: Awaitable[object]) -> None:
        """Run a command up to its state change and discard it.

        The command's own checks run unchanged; see dev doc "Validierung".
        """
        token = _DRY_RUN.set(True)
        try:
            await command
        except _DryRunComplete:
            pass
        finally:
            _DRY_RUN.reset(token)

    async def _mutate(
        self, mutation: Callable[[OrchestratorState], OrchestratorState]
    ) -> None:
        if _DRY_RUN.get():
            mutation(self._verified_state())
            raise _DryRunComplete
        async with self._lock:
            previous = self._verified_state()
            candidate = mutation(previous)
            if candidate is previous:
                return
            await self._commit_locked(previous, candidate)

    async def _commit_locked(
        self, previous: OrchestratorState, candidate: OrchestratorState
    ) -> None:
        if _DRY_RUN.get():
            raise RuntimeError("commit_during_validation")
        self.trace.record(TraceEvent.STORAGE, self._clock(), stage="committing")
        try:
            await self._repository.async_commit(
                candidate, expected_previous_commit_id=previous.commit_id
            )
        except Exception as err:
            self._storage_uncertain = True
            self.trace.record(
                TraceEvent.STORAGE, self._clock(), stage="failed", error=err
            )
            raise
        self._state = candidate
        self.trace.commit_id = candidate.commit_id
        self.trace.run_id = candidate.queue_run.run_id if candidate.queue_run else None
        self.trace.record(TraceEvent.STORAGE, self._clock(), stage="committed")
        if candidate.queue_run != previous.queue_run or candidate.mode != previous.mode:
            run = candidate.queue_run
            if run and not run.active:
                stage = "completed"
            elif candidate.mode is QueueMode.PAUSED:
                stage = "paused"
            elif run is None:
                stage = "idle"
            elif previous.queue_run is None or previous.queue_run.run_id != run.run_id:
                stage = "started"
            elif run.idle_since:
                stage = "waiting"
            elif previous.mode is QueueMode.PAUSED:
                stage = "resumed"
            else:
                stage = "idle_reset"
            self.trace.record(
                TraceEvent.QUEUE, self._clock(), state=candidate.mode.value, stage=stage
            )
        for key, job in candidate.jobs.items():
            if key not in previous.jobs or previous.jobs[key].state != job.state:
                self.trace.record(
                    TraceEvent.JOB,
                    self._clock(),
                    job_id=key,
                    state=job.state.value,
                    reason=job.failure_code,
                )
        for key, attempt in candidate.attempts.items():
            if (
                key not in previous.attempts
                or previous.attempts[key].state != attempt.state
                or previous.attempts[key].completion_quality
                != attempt.completion_quality
            ):
                self.trace.record(
                    TraceEvent.ATTEMPT,
                    self._clock(),
                    job_id=attempt.job_id,
                    attempt_id=key,
                    robot_id=attempt.robot_id,
                    state=attempt.state.value,
                    reason=attempt.failure_code,
                    quality=attempt.completion_quality.value
                    if attempt.completion_quality
                    else None,
                )
        self.notify_runtime_change(self._commit_scopes(previous, candidate))
        for listener in tuple(self._listeners):
            try:
                listener()
            except Exception as err:
                self.trace.record(
                    TraceEvent.ERROR,
                    self._clock(),
                    reason="commit_listener_failed",
                    error=err,
                )

    async def _mark_robot_uncertain(self, attempt_id: str, _error: Exception) -> None:
        self.trace.record(
            TraceEvent.ERROR,
            self._clock(),
            attempt_id=attempt_id,
            reason="dispatch_failed",
            error=_error,
        )
        async with self._lock:
            previous = self._verified_state()
            attempt = previous.attempts[attempt_id]
            if attempt.state in {
                AttemptState.SUCCEEDED,
                AttemptState.FAILED,
                AttemptState.CANCELLED,
                AttemptState.RECOVERY_REQUIRED,
            }:
                return
            candidate = previous.require_robot_attention(
                attempt_id, "dispatch_failed", None, None, self._clock()
            )
            session = self._sessions[attempt.source_robot_id]
            try:
                await self._commit_locked(previous, candidate)
            except Exception:
                session.fence(session.generation + 1, needs_attention=True)
                return
            session.fence(
                candidate.robot_generations[attempt.source_robot_id],
                needs_attention=True,
            )

    def _verified_state(self) -> OrchestratorState:
        if self._closing:
            raise ConflictError("orchestrator_shutting_down")
        if self._storage_uncertain:
            raise StorageIntegrityError("critical_storage_state_uncertain")
        return self.state
