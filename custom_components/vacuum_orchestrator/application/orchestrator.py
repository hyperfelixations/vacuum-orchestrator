"""Single-writer application service for integration-wide orchestration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from ..domain.correlation import correlate_run
from ..domain.dispatching import (
    RobotObservation,
    RobotSelector,
    assignment_supports_current_capabilities,
)
from ..domain.errors import (
    ConflictError,
    DispatchNotStartedError,
    PlanningError,
    StaleCommandError,
    StorageIntegrityError,
)
from ..domain.execution import ExecutionAttempt, RobotLease, RobotRun
from ..domain.intents import JobIntent, JobIntentPatch, TargetRef
from ..domain.planning import DispatchAssignment, ExecutionPlan, Planner
from ..domain.queue import Job, OrchestratorState
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
from ..ports.command_scope import command_origin
from ..ports.repository import OrchestratorRepository
from ..ports.robot import RobotAdapter
from .external_observations import apply_external_observation
from .observation_handler import apply_observation
from .queue_run_service import QueueRunService
from .robot_session import RobotSession
from .room_readiness import RequirementReader, evaluate_room_readiness
from .room_service import RoomService
from .template_service import TemplateService
from .tracing import TraceEvent, TraceRecorder

_LOGGER = logging.getLogger(__name__)

Clock = Callable[[], datetime]
IdFactory = Callable[[], str]
StateReader = Callable[[tuple[str, ...]], Mapping[str, str | None]]
StateListener = Callable[[], None]


def _utcnow() -> datetime:
    return datetime.now(UTC)


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
        clock: Clock = _utcnow,
        id_factory: IdFactory = _uuid,
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
        self._clock = clock
        self._id_factory = id_factory
        self._state: OrchestratorState | None = None
        self._storage_uncertain = False
        self._closing = False
        self._lock = asyncio.Lock()
        self._sessions: dict[str, RobotSession] = {}
        self._listeners: set[StateListener] = set()
        self._view_listeners: set[StateListener] = set()
        self.runtime_id = _uuid()
        self.runtime_sequence = 0
        self.trace = TraceRecorder()
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

    async def async_shutdown(self) -> None:
        """Fence future commands and persist unresolved ownership before unloading."""
        async with self._lock:
            self._closing = True
            state = self.state
            candidate = state.require_attention_for_active_leases(self._clock())
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
            loaded = await self._repository.async_load()
            if loaded is None:
                loaded = OrchestratorState.empty(self._installation_id)
                await self._repository.async_commit(
                    loaded, expected_previous_commit_id=-1
                )
            if loaded.installation_id != self._installation_id:
                raise StorageIntegrityError("installation_storage_ownership_mismatch")
            if loaded.robot_leases:
                candidate = loaded.require_attention_for_active_leases(self._clock())
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

    def notify_runtime_change(self) -> None:
        """Invalidate read models without scheduling or persisting a clock tick."""
        self.runtime_sequence += 1
        for listener in tuple(self._view_listeners):
            try:
                listener()
            except Exception:
                _LOGGER.error("A runtime view listener failed")

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
        references = (*job.intent.required_on, *job.intent.required_off)
        base = self._readiness.evaluate(job.intent, self._state_reader(references))
        return evaluate_room_readiness(
            job,
            self.state.room_registry,
            base,
            self._requirement_reader,
            self._clock(),
            robot_id=robot_id,
            operation=operation,
            robot_requirements=self._adapters[robot_id].profile.requirements
            if robot_id in self._adapters
            else (),
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

    async def async_update_job(self, job_id: str, patch: JobIntentPatch) -> None:
        """Apply a partial update without exposing internal revisions."""

        def update(state: OrchestratorState) -> OrchestratorState:
            if job_id not in state.jobs:
                raise ConflictError("unknown_job")
            intent = self._canonical_intent(
                state, patch.apply(state.jobs[job_id].intent)
            )
            return state.update_job(
                job_id, replace(patch, areas=intent.areas), self._clock()
            )

        await self._mutate(update)

    async def async_delete_job(self, job_id: str) -> None:
        """Delete queued or terminal work."""
        await self._mutate(lambda state: state.delete_job(job_id))

    async def async_move_job(self, job_id: str, direction: MoveDirection) -> None:
        """Move a pending job using one intuitive direction."""
        await self._mutate(lambda state: state.move_job(job_id, direction))

    async def async_set_queue_mode(self, mode: QueueMode) -> None:
        """Set automatic queue dispatch mode."""
        await self.runs.async_set_mode(mode)

    async def async_reconcile_queue_run(self) -> None:
        """Track queue quiescence without mistaking blocked pending work for a run."""
        await self.runs.async_set_mode(self.state.mode)
        if self.state.mode is not QueueMode.RUNNING:
            return
        observations = await self._observe_robots()

        def ready(state: OrchestratorState) -> bool:
            for job_id in state.queue:
                if self.readiness_for_job(job_id).state.value != "ready":
                    continue
                job = state.jobs[job_id]
                unit = self._planner.create_plan(job_id, job.intent).work_units[0]
                profiles = tuple(
                    adapter.profile
                    for adapter in self._adapters.values()
                    if self.readiness_for_job(
                        job_id,
                        robot_id=adapter.profile.robot_id,
                        operation=unit.operation,
                    ).state.value
                    == "ready"
                )
                try:
                    self._selector.assign(
                        unit,
                        profiles,
                        observations,
                        state.robot_leases,
                        frozenset(state.blocked_robots),
                        state.active_target_sets(),
                    )
                except PlanningError:
                    continue
                return True
            return False

        await self.runs.async_reconcile(ready)

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

    async def async_run_queue(self) -> tuple[DispatchAssignment, ...]:
        """Enable queue processing and dispatch every currently safe candidate."""
        await self.async_set_queue_mode(QueueMode.RUNNING)
        return await self.async_dispatch_available()

    async def async_dispatch_available(self) -> tuple[DispatchAssignment, ...]:
        """Dispatch continuations first, then scan queued jobs without reordering."""
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
                assignment = await self._async_dispatch_job(job_id, None)
            except (PlanningError, ConflictError):
                continue
            except StorageIntegrityError:
                raise
            except Exception:
                self._verified_state()
                _LOGGER.error("Robot dispatch failed; continuing eligible queue work")
                continue
            if assignment is not None:
                dispatched.append(assignment)
        return tuple(dispatched)

    async def async_cancel_job(self, job_id: str) -> None:
        """Cancel queued work or fence an active robot before physical stop."""
        adapter: RobotAdapter | None = None
        session: RobotSession | None = None
        ticket = None
        previous: OrchestratorState
        candidate: OrchestratorState
        async with self._lock:
            previous = self._verified_state()
            candidate, generation = previous.request_cancel(job_id, self._clock())
            await self._commit_locked(previous, candidate)
            if generation is None:
                return
            attempt_id = candidate.jobs[job_id].active_attempt_id
            if attempt_id is None:
                raise ConflictError("active_attempt_missing")
            attempt = candidate.attempts[attempt_id]
            session = self._sessions[attempt.source_robot_id]
            ticket = session.fence(generation, needs_attention=False)
            adapter = self._adapters.get(attempt.robot_id)
            if adapter is None:
                raise ConflictError("assigned_robot_missing")
        assert adapter is not None and session is not None and ticket is not None
        try:
            await session.cancel(ticket, adapter.async_cancel)
        except Exception as err:
            await self._mark_robot_uncertain(attempt_id, err)
            raise

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
            self.trace.record(
                TraceEvent.RECOVERY,
                self._clock(),
                robot_id=robot_id,
                reason="operator_assumed_stopped"
                if not stopped
                else "verified_stopped",
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
        except Exception:
            observation = RobotObservation(
                robot_id,
                adapter.profile.source_robot_id,
                RobotAvailabilityState.UNKNOWN,
                observed_at=self._clock(),
                reason="observation_failed",
            )
        async with self._lock:
            state = self._verified_state()
            lease = state.robot_leases.get(observation.source_robot_id)
            self.trace.record(
                TraceEvent.OBSERVATION,
                self._clock(),
                robot_id=robot_id,
                attempt_id=lease.attempt_id if lease else None,
                job_id=state.attempts[lease.attempt_id].job_id if lease else None,
                state=observation.state.value,
            )
            candidate = (
                apply_observation(state, observation, self._clock(), self._id_factory)
                if lease is not None
                else apply_external_observation(
                    state, observation, adapter.profile, self._clock(), self._id_factory
                )
            )
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
    ) -> DispatchAssignment | None:
        observations = await self._observe_robots()
        async with self._lock:
            previous = self._verified_state()
            job = previous.jobs.get(job_id)
            if job is None:
                raise ConflictError("unknown_job")
            if job.state not in {JobState.QUEUED, JobState.DISPATCHING}:
                raise ConflictError("job_not_dispatchable")
            if job.state is JobState.QUEUED:
                report = self.readiness_for_job(job_id)
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
            profiles = tuple(adapter.profile for adapter in self._adapters.values())
            ready_profiles = tuple(
                profile
                for profile in profiles
                if self.readiness_for_job(
                    job_id, robot_id=profile.robot_id, operation=unit.operation
                ).state.value
                == "ready"
            )
            if profiles and not ready_profiles:
                raise PlanningError("job_conditions_not_satisfied")
            assignment = self._selector.assign(
                unit,
                ready_profiles,
                observations,
                previous.robot_leases,
                frozenset(previous.blocked_robots),
                previous.active_target_sets(excluding_job_id=job_id),
                requested_robot_id,
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
            await self._commit_locked(previous, prepared)
            session = self._sessions.setdefault(
                assignment.source_robot_id,
                RobotSession(assignment.source_robot_id, generation - 1),
            )
            session.fence(generation, needs_attention=False)
            ticket = session.reserve(attempt_id)
            sent = prepared.mark_command_sent(attempt_id, self._clock())
            await self._commit_locked(prepared, sent)

        origin_token = command_origin.set(origin or job.origin)
        try:
            await session.dispatch(
                ticket,
                lambda: adapter.async_dispatch(unit, assignment),
                lambda: self._guard_dispatch(job_id, assignment, unit.operation),
            )
        except StaleCommandError:
            return None
        except DispatchNotStartedError as err:
            await self._finish_unstarted(attempt_id, err.code)
            raise
        except Exception as err:
            await self._mark_robot_uncertain(attempt_id, err)
            raise
        finally:
            command_origin.reset(origin_token)
        async with self._lock:
            current = self._verified_state()
            accepted = current.mark_dispatch_accepted(attempt_id, self._clock())
            if accepted is not current:
                await self._commit_locked(current, accepted)
        return assignment

    async def _observe_robots(self) -> dict[str, RobotObservation]:
        adapters = dict(self._adapters)
        observations = await asyncio.gather(
            *(adapter.async_observe() for adapter in adapters.values()),
            return_exceptions=True,
        )
        return {
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
                    attempt_id, None, None, self._clock()
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
        return replace(
            intent,
            areas=tuple(
                TargetRef(
                    state.room_registry.resolve(target.area_id).room_id,
                    target.map_context,
                )
                for target in intent.areas
            ),
        )

    @staticmethod
    def _existing_plan(state: OrchestratorState, job: Job) -> ExecutionPlan:
        if job.plan_id is None or job.plan_id not in state.plans:
            raise ConflictError("job_plan_missing")
        return state.plans[job.plan_id]

    async def _mutate(
        self, mutation: Callable[[OrchestratorState], OrchestratorState]
    ) -> None:
        async with self._lock:
            previous = self._verified_state()
            candidate = mutation(previous)
            if candidate is previous:
                return
            await self._commit_locked(previous, candidate)

    async def _commit_locked(
        self, previous: OrchestratorState, candidate: OrchestratorState
    ) -> None:
        try:
            await self._repository.async_commit(
                candidate, expected_previous_commit_id=previous.commit_id
            )
        except Exception:
            self._storage_uncertain = True
            raise
        self._state = candidate
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
        self.notify_runtime_change()
        for listener in tuple(self._listeners):
            try:
                listener()
            except Exception:
                _LOGGER.error("A state listener failed after a verified commit")

    async def _mark_robot_uncertain(self, attempt_id: str, _error: Exception) -> None:
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
                attempt_id, None, None, self._clock()
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
