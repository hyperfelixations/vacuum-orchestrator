"""Single-writer application service for integration-wide orchestration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
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
    PlanningError,
    StaleCommandError,
    StorageIntegrityError,
)
from ..domain.execution import ExecutionAttempt, RobotLease, RobotRun
from ..domain.intents import JobIntent, JobIntentPatch
from ..domain.planning import DispatchAssignment, ExecutionPlan, Planner
from ..domain.queue import Job, OrchestratorState
from ..domain.readiness import ReadinessEvaluator, ReadinessReport
from ..domain.types import AttemptState, JobState, MoveDirection, QueueMode
from ..ports.repository import OrchestratorRepository
from ..ports.robot import RobotAdapter
from .robot_session import RobotSession

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
        self._clock = clock
        self._id_factory = id_factory
        self._state: OrchestratorState | None = None
        self._storage_uncertain = False
        self._lock = asyncio.Lock()
        self._sessions: dict[str, RobotSession] = {}
        self._listeners: set[StateListener] = set()

    @property
    def state(self) -> OrchestratorState:
        """Return the last verified in-memory snapshot."""
        if self._state is None:
            raise ConflictError("orchestrator_not_initialized")
        return self._state

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

    def readiness_for_job(self, job_id: str) -> ReadinessReport:
        """Evaluate one job without considering robot availability."""
        job = self.state.jobs.get(job_id)
        if job is None:
            raise ConflictError("unknown_job")
        references = (*job.intent.required_on, *job.intent.required_off)
        return self._readiness.evaluate(job.intent, self._state_reader(references))

    async def async_create_job(self, intent: JobIntent) -> str:
        """Create and append a validated robot-independent job."""
        job_id = self._id_factory()
        await self._mutate(lambda state: state.add_job(job_id, intent, self._clock()))
        return job_id

    async def async_update_job(self, job_id: str, patch: JobIntentPatch) -> None:
        """Apply a partial update without exposing internal revisions."""
        await self._mutate(lambda state: state.update_job(job_id, patch, self._clock()))

    async def async_delete_job(self, job_id: str) -> None:
        """Delete queued or terminal work."""
        await self._mutate(lambda state: state.delete_job(job_id))

    async def async_move_job(self, job_id: str, direction: MoveDirection) -> None:
        """Move a pending job using one intuitive direction."""
        await self._mutate(lambda state: state.move_job(job_id, direction))

    async def async_set_queue_mode(self, mode: QueueMode) -> None:
        """Set automatic queue dispatch mode."""
        await self._mutate(lambda state: state.set_queue_mode(mode))

    async def async_retry_job(self, job_id: str) -> str:
        """Create a distinct retry while preserving terminal history."""
        retry_job_id = self._id_factory()
        await self._mutate(
            lambda state: state.retry_job(job_id, retry_job_id, self._clock())
        )
        return retry_job_id

    async def async_start_job(
        self, job_id: str, robot_id: str | None = None
    ) -> DispatchAssignment:
        """Start one job using an optional requested robot and shared policy."""
        assignment = await self._async_dispatch_job(job_id, robot_id)
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
            except PlanningError:
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
            adapter = self._adapters.get(attempt.robot_id)
            if adapter is None:
                raise ConflictError("assigned_robot_missing")
            session = self._sessions[attempt.source_robot_id]
            ticket = session.fence(generation, needs_attention=False)
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
        """Advance an owned attempt from normalized adapter evidence."""
        adapter = self._adapters.get(robot_id)
        if adapter is None:
            raise ConflictError("unknown_robot")
        observation = await adapter.async_observe()
        async with self._lock:
            state = self._verified_state()
            lease = state.robot_leases.get(observation.source_robot_id)
            if lease is None:
                return
            attempt = state.attempts[lease.attempt_id]
            job = state.jobs[attempt.job_id]
            observed_at = observation.observed_at or self._clock()
            if attempt.state is AttemptState.CANCEL_PENDING:
                if observation.state.value == "available":
                    candidate = state.confirm_cancel(job.job_id, observed_at)
                    await self._commit_locked(state, candidate)
                    session = self._sessions.get(attempt.source_robot_id)
                    if session is not None:
                        session.release(attempt.attempt_id)
                else:
                    return
            elif attempt.state is AttemptState.COMMAND_SENT:
                if observation.state.value != "busy":
                    return
                candidate = state.mark_start_confirmed(attempt.attempt_id, observed_at)
                await self._commit_locked(state, candidate)
                return
            elif attempt.state is AttemptState.START_CONFIRMED:
                if observation.state.value != "available":
                    return
                run = RobotRun(
                    robot_run_id=self._id_factory(),
                    source_robot_id=attempt.source_robot_id,
                    observed_start=attempt.observed_start_at,
                    observed_end=observed_at,
                    history_start=observation.history_start,
                    history_end=observation.history_end,
                    cleaning_activity_seen=True,
                )
                correlation = correlate_run(attempt, run)
                candidate = state.complete_attempt(
                    attempt.attempt_id, run, correlation, observed_at
                )
                await self._commit_locked(state, candidate)
                session = self._sessions.get(attempt.source_robot_id)
                if session is not None:
                    if correlation.requires_attention:
                        session.fence(
                            candidate.robot_generations[attempt.source_robot_id],
                            needs_attention=True,
                        )
                    else:
                        session.release(attempt.attempt_id)
            else:
                return
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
        self, job_id: str, requested_robot_id: str | None
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
                    raise PlanningError(f"job_{report.state.value}")
                plan = self._planner.create_plan(job.job_id, job.intent)
                unit = plan.work_units[0]
            else:
                plan = self._existing_plan(previous, job)
                unit = previous.next_pending_unit(job)
            profiles = tuple(adapter.profile for adapter in self._adapters.values())
            assignment = self._selector.assign(
                unit,
                profiles,
                observations,
                previous.robot_leases,
                frozenset(previous.blocked_robots),
                previous.active_target_sets(),
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
            prepared = previous.prepare_dispatch(
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

        try:
            await session.dispatch(
                ticket, lambda: adapter.async_dispatch(unit, assignment)
            )
        except StaleCommandError:
            return None
        except Exception as err:
            await self._mark_robot_uncertain(attempt_id, err)
            raise
        async with self._lock:
            current = self._verified_state()
            accepted = current.mark_dispatch_accepted(attempt_id, self._clock())
            if accepted is not current:
                await self._commit_locked(current, accepted)
        return assignment

    async def _observe_robots(self) -> dict[str, RobotObservation]:
        observations = await asyncio.gather(
            *(adapter.async_observe() for adapter in self._adapters.values())
        )
        return {observation.robot_id: observation for observation in observations}

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
        for listener in tuple(self._listeners):
            listener()

    async def _mark_robot_uncertain(self, attempt_id: str, _error: Exception) -> None:
        async with self._lock:
            previous = self._verified_state()
            attempt = previous.attempts[attempt_id]
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
        if self._storage_uncertain:
            raise StorageIntegrityError("critical_storage_state_uncertain")
        return self.state
