"""Global job registry, pending queue, and execution-ledger invariants."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from types import MappingProxyType

from .errors import ConflictError, ValidationError
from .execution import ExecutionAttempt, RobotLease, RobotRun, RunCorrelation
from .intents import JobIntent, JobIntentPatch
from .planning import (
    DispatchAssignment,
    ExecutionPlan,
    WorkUnit,
    plan_matches_intent,
)
from .types import AttemptState, JobState, MoveDirection, QueueMode, WorkUnitState

_TERMINAL = frozenset({JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED})
_ACTIVE = frozenset({JobState.DISPATCHING, JobState.RUNNING, JobState.CANCELING})
_TARGET_RESERVED = _ACTIVE | {JobState.NEEDS_ATTENTION}


@dataclass(frozen=True, slots=True)
class Job:
    """Persisted job record independent of every robot profile."""

    job_id: str
    revision: int
    intent: JobIntent
    state: JobState
    created_at: datetime
    updated_at: datetime
    plan_id: str | None = None
    active_attempt_id: str | None = None
    completed_work_unit_ids: tuple[str, ...] = ()
    retries_job_id: str | None = None
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class OrchestratorState:
    """Single global orchestration aggregate for one Home Assistant instance."""

    installation_id: str
    commit_id: int
    queue_revision: int
    mode: QueueMode
    queue: tuple[str, ...]
    jobs: Mapping[str, Job]
    robot_generations: Mapping[str, int]
    plans: Mapping[str, ExecutionPlan] = field(default_factory=dict)
    work_unit_states: Mapping[str, WorkUnitState] = field(default_factory=dict)
    assignments: Mapping[str, DispatchAssignment] = field(default_factory=dict)
    attempts: Mapping[str, ExecutionAttempt] = field(default_factory=dict)
    robot_runs: Mapping[str, RobotRun] = field(default_factory=dict)
    correlations: Mapping[str, RunCorrelation] = field(default_factory=dict)
    robot_leases: Mapping[str, RobotLease] = field(default_factory=dict)
    blocked_robots: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in (
            "jobs",
            "robot_generations",
            "plans",
            "work_unit_states",
            "assignments",
            "attempts",
            "robot_runs",
            "correlations",
            "robot_leases",
            "blocked_robots",
        ):
            object.__setattr__(
                self, field_name, MappingProxyType(dict(getattr(self, field_name)))
            )
        if not self.installation_id.strip():
            raise ValidationError("empty_installation_id")
        if len(self.queue) != len(set(self.queue)):
            raise ValidationError("duplicate_queue_job")
        if any(job_id not in self.jobs for job_id in self.queue):
            raise ValidationError("queue_references_unknown_job")
        if any(self.jobs[job_id].state is not JobState.QUEUED for job_id in self.queue):
            raise ValidationError("queue_contains_non_queued_job")
        queued_ids = {
            job_id for job_id, job in self.jobs.items() if job.state is JobState.QUEUED
        }
        if queued_ids != set(self.queue):
            raise ValidationError("queued_job_missing_from_queue")

    @classmethod
    def empty(cls, installation_id: str) -> OrchestratorState:
        """Create an empty integration-wide state."""
        return cls(installation_id, 0, 0, QueueMode.IDLE, (), {}, {})

    @property
    def needs_attention(self) -> bool:
        """Return whether any robot or job has unresolved physical ownership."""
        return bool(self.blocked_robots) or any(
            job.state is JobState.NEEDS_ATTENTION for job in self.jobs.values()
        )

    def add_job(
        self, job_id: str, intent: JobIntent, now: datetime
    ) -> OrchestratorState:
        """Append a new job to the single pending queue."""
        if job_id in self.jobs:
            raise ConflictError("job_already_exists")
        if intent.dedupe_key is not None and any(
            job.state is JobState.QUEUED and job.intent.dedupe_key == intent.dedupe_key
            for job in self.jobs.values()
        ):
            raise ConflictError("dedupe_key_already_queued")
        jobs = dict(self.jobs)
        jobs[job_id] = Job(job_id, 1, intent, JobState.QUEUED, now, now)
        return self._replace(
            jobs=jobs,
            queue=(*self.queue, job_id),
            queue_revision=self.queue_revision + 1,
        )

    def update_job(
        self, job_id: str, patch: JobIntentPatch, now: datetime
    ) -> OrchestratorState:
        """Apply a user-friendly partial update to a queued job."""
        job = self._job(job_id)
        if job.state is not JobState.QUEUED:
            raise ConflictError("job_not_editable")
        intent = patch.apply(job.intent)
        if intent == job.intent:
            return self
        if intent.dedupe_key is not None and any(
            other.job_id != job_id
            and other.state is JobState.QUEUED
            and other.intent.dedupe_key == intent.dedupe_key
            for other in self.jobs.values()
        ):
            raise ConflictError("dedupe_key_already_queued")
        jobs = dict(self.jobs)
        jobs[job_id] = replace(
            job, intent=intent, revision=job.revision + 1, updated_at=now
        )
        return self._replace(jobs=jobs)

    def delete_job(self, job_id: str) -> OrchestratorState:
        """Delete queued or terminal history, never active or ambiguous work."""
        job = self._job(job_id)
        if job.state not in {JobState.QUEUED, *_TERMINAL}:
            raise ConflictError("job_not_deletable")
        jobs = dict(self.jobs)
        del jobs[job_id]
        queue = tuple(item for item in self.queue if item != job_id)
        plan_ids = {key for key, plan in self.plans.items() if plan.job_id == job_id}
        unit_ids = {
            unit.work_unit_id for key in plan_ids for unit in self.plans[key].work_units
        }
        attempt_ids = {
            key for key, attempt in self.attempts.items() if attempt.job_id == job_id
        }
        removed_run_ids = {
            correlation.robot_run_id
            for key, correlation in self.correlations.items()
            if key in attempt_ids
        }
        correlations = {
            key: value
            for key, value in self.correlations.items()
            if key not in attempt_ids
        }
        retained_run_ids = {value.robot_run_id for value in correlations.values()}
        return self._replace(
            jobs=jobs,
            queue=queue,
            queue_revision=self.queue_revision + (queue != self.queue),
            plans={
                key: value for key, value in self.plans.items() if key not in plan_ids
            },
            work_unit_states={
                key: value
                for key, value in self.work_unit_states.items()
                if key not in unit_ids
            },
            attempts={
                key: value
                for key, value in self.attempts.items()
                if key not in attempt_ids
            },
            assignments={
                key: value
                for key, value in self.assignments.items()
                if key not in attempt_ids
            },
            correlations=correlations,
            robot_runs={
                key: value
                for key, value in self.robot_runs.items()
                if key not in removed_run_ids - retained_run_ids
            },
        )

    def move_job(self, job_id: str, direction: MoveDirection) -> OrchestratorState:
        """Move one queued job by a simple relative or absolute direction."""
        job = self._job(job_id)
        if job.state is not JobState.QUEUED:
            raise ConflictError("job_not_movable")
        queue = list(self.queue)
        index = queue.index(job_id)
        if direction is MoveDirection.UP:
            target = max(0, index - 1)
        elif direction is MoveDirection.DOWN:
            target = min(len(queue) - 1, index + 1)
        elif direction is MoveDirection.TOP:
            target = 0
        else:
            target = len(queue) - 1
        if target == index:
            return self
        queue.pop(index)
        queue.insert(target, job_id)
        return self._replace(queue=tuple(queue), queue_revision=self.queue_revision + 1)

    def set_queue_mode(self, mode: QueueMode) -> OrchestratorState:
        """Set whether automatic dispatch may begin new work."""
        return self if mode is self.mode else self._replace(mode=mode)

    def prepare_dispatch(
        self,
        job_id: str,
        plan: ExecutionPlan,
        unit: WorkUnit,
        assignment: DispatchAssignment,
        attempt: ExecutionAttempt,
        lease: RobotLease,
        now: datetime,
    ) -> OrchestratorState:
        """Persist plan, late binding, attempt, and lease before physical I/O."""
        job = self._job(job_id)
        if job.state not in {JobState.QUEUED, JobState.DISPATCHING}:
            raise ConflictError("job_not_dispatchable")
        if assignment.source_robot_id in self.robot_leases:
            raise ConflictError("source_robot_already_leased")
        if assignment.source_robot_id in self.blocked_robots:
            raise ConflictError("robot_needs_attention")
        if (
            plan.job_id != job_id
            or not plan_matches_intent(plan, job.intent)
            or unit not in plan.work_units
        ):
            raise ValidationError("invalid_execution_plan")
        if job.plan_id is not None and job.plan_id != plan.plan_id:
            raise ConflictError("job_plan_identity_conflict")
        if assignment.work_unit_id != unit.work_unit_id:
            raise ValidationError("assignment_work_unit_mismatch")
        if (
            attempt.job_id != job_id
            or attempt.work_unit_id != unit.work_unit_id
            or attempt.robot_id != assignment.robot_id
            or attempt.source_robot_id != assignment.source_robot_id
            or attempt.state is not AttemptState.PREPARED
            or attempt.command_boundary_at is not None
        ):
            raise ValidationError("invalid_execution_attempt")
        if (
            lease.attempt_id != attempt.attempt_id
            or lease.work_unit_id != unit.work_unit_id
            or lease.robot_id != assignment.robot_id
            or lease.source_robot_id != assignment.source_robot_id
            or lease.generation != attempt.robot_generation
        ):
            raise ValidationError("invalid_robot_lease")
        if self.robot_generations.get(lease.source_robot_id, 0) + 1 != lease.generation:
            raise ConflictError("robot_generation_not_reserved")
        if attempt.attempt_id in self.attempts:
            raise ConflictError("execution_identity_conflict")
        if any(
            set(unit.canonical_targets) & set(self._active_targets(active_job))
            for active_job in self.jobs.values()
            if active_job.job_id != job_id and active_job.state in _TARGET_RESERVED
        ):
            raise ConflictError("target_overlap_active")

        jobs = dict(self.jobs)
        jobs[job_id] = replace(
            job,
            state=JobState.DISPATCHING,
            revision=job.revision + 1,
            plan_id=plan.plan_id,
            active_attempt_id=attempt.attempt_id,
            updated_at=now,
        )
        plans = dict(self.plans)
        plans.setdefault(plan.plan_id, plan)
        work_unit_states = dict(self.work_unit_states)
        for planned_unit in plan.work_units:
            work_unit_states.setdefault(
                planned_unit.work_unit_id, WorkUnitState.PENDING
            )
        work_unit_states[unit.work_unit_id] = WorkUnitState.ACTIVE
        assignments = dict(self.assignments)
        assignments[attempt.attempt_id] = assignment
        attempts = dict(self.attempts)
        attempts[attempt.attempt_id] = attempt
        leases = dict(self.robot_leases)
        leases[lease.source_robot_id] = lease
        generations = dict(self.robot_generations)
        generations[lease.source_robot_id] = lease.generation
        queue = tuple(item for item in self.queue if item != job_id)
        return self._replace(
            jobs=jobs,
            plans=plans,
            work_unit_states=work_unit_states,
            assignments=assignments,
            attempts=attempts,
            robot_leases=leases,
            robot_generations=generations,
            queue=queue,
            queue_revision=self.queue_revision + (queue != self.queue),
        )

    def mark_command_sent(
        self, attempt_id: str, command_boundary_at: datetime
    ) -> OrchestratorState:
        """Persist the command boundary before entering physical I/O."""
        attempt = self._attempt(attempt_id)
        if attempt.state is not AttemptState.PREPARED:
            raise ConflictError("attempt_not_prepared")
        attempts = dict(self.attempts)
        attempts[attempt_id] = replace(
            attempt,
            state=AttemptState.COMMAND_SENT,
            command_boundary_at=command_boundary_at,
        )
        return self._replace(attempts=attempts)

    def mark_dispatch_accepted(
        self, attempt_id: str, now: datetime
    ) -> OrchestratorState:
        """Mark accepted physical command while awaiting causal evidence."""
        attempt = self._attempt(attempt_id)
        job = self._job(attempt.job_id)
        if job.state is JobState.CANCELING:
            return self
        if attempt.state is AttemptState.PREPARED:
            raise ConflictError("attempt_command_not_sent")
        if attempt.state not in {
            AttemptState.COMMAND_SENT,
            AttemptState.START_CONFIRMED,
            AttemptState.COMPLETION_PENDING,
        }:
            return self
        if job.state is JobState.RUNNING:
            return self
        if job.state is not JobState.DISPATCHING:
            raise ConflictError("job_not_dispatching")
        jobs = dict(self.jobs)
        jobs[job.job_id] = replace(job, state=JobState.RUNNING, updated_at=now)
        return self._replace(jobs=jobs)

    def mark_start_confirmed(
        self, attempt_id: str, observed_start_at: datetime
    ) -> OrchestratorState:
        """Persist strong observed evidence that physical cleaning started."""
        attempt = self._attempt(attempt_id)
        if attempt.state is not AttemptState.COMMAND_SENT:
            raise ConflictError("attempt_not_awaiting_start")
        attempts = dict(self.attempts)
        attempts[attempt_id] = replace(
            attempt,
            state=AttemptState.START_CONFIRMED,
            observed_start_at=observed_start_at,
        )
        return self._replace(attempts=attempts)

    def complete_attempt(
        self,
        attempt_id: str,
        run: RobotRun,
        correlation: RunCorrelation,
        now: datetime,
    ) -> OrchestratorState:
        """Complete one unit only with a strong matched physical run."""
        attempt = self._attempt(attempt_id)
        job = self._job(attempt.job_id)
        if (
            correlation.attempt_id != attempt_id
            or correlation.robot_run_id != run.robot_run_id
        ):
            raise ValidationError("correlation_identity_mismatch")
        existing_run = self.robot_runs.get(run.robot_run_id)
        existing_correlation = self.correlations.get(attempt_id)
        if existing_run is not None or existing_correlation is not None:
            if existing_run == run and existing_correlation == correlation:
                return self
            raise ConflictError("run_correlation_identity_conflict")
        if correlation.requires_attention:
            return self.require_robot_attention(attempt_id, run, correlation, now)
        if attempt.state not in {
            AttemptState.COMMAND_SENT,
            AttemptState.START_CONFIRMED,
            AttemptState.COMPLETION_PENDING,
        }:
            raise ConflictError("attempt_not_completable")
        attempts = dict(self.attempts)
        attempts[attempt_id] = replace(attempt, state=AttemptState.SUCCEEDED)
        runs = dict(self.robot_runs)
        runs[run.robot_run_id] = run
        correlations = dict(self.correlations)
        correlations[attempt_id] = correlation
        work_unit_states = dict(self.work_unit_states)
        work_unit_states[attempt.work_unit_id] = WorkUnitState.COMPLETED
        completed = (*job.completed_work_unit_ids, attempt.work_unit_id)
        plan = self._plan_for_job(job)
        final = len(completed) == len(plan.work_units)
        jobs = dict(self.jobs)
        jobs[job.job_id] = replace(
            job,
            state=JobState.COMPLETED if final else JobState.DISPATCHING,
            revision=job.revision + 1,
            active_attempt_id=None,
            completed_work_unit_ids=completed,
            updated_at=now,
        )
        leases = dict(self.robot_leases)
        leases.pop(attempt.source_robot_id, None)
        return self._replace(
            jobs=jobs,
            attempts=attempts,
            robot_runs=runs,
            correlations=correlations,
            work_unit_states=work_unit_states,
            robot_leases=leases,
        )

    def request_cancel(
        self, job_id: str, now: datetime
    ) -> tuple[OrchestratorState, int | None]:
        """Cancel pending work or atomically fence active physical ownership."""
        job = self._job(job_id)
        if job.state is JobState.QUEUED:
            jobs = dict(self.jobs)
            jobs[job_id] = replace(
                job,
                state=JobState.CANCELLED,
                revision=job.revision + 1,
                updated_at=now,
            )
            return (
                self._replace(
                    jobs=jobs,
                    queue=tuple(item for item in self.queue if item != job_id),
                    queue_revision=self.queue_revision + 1,
                ),
                None,
            )
        if job.state not in {JobState.DISPATCHING, JobState.RUNNING}:
            raise ConflictError("job_not_cancellable")
        if job.active_attempt_id is None:
            raise ConflictError("active_attempt_missing")
        attempt = self._attempt(job.active_attempt_id)
        lease = self.robot_leases.get(attempt.source_robot_id)
        if lease is None or lease.attempt_id != attempt.attempt_id:
            raise ConflictError("attempt_lease_missing")
        generation = self.robot_generations.get(attempt.source_robot_id, 0) + 1
        jobs = dict(self.jobs)
        jobs[job_id] = replace(
            job,
            state=JobState.CANCELING,
            revision=job.revision + 1,
            updated_at=now,
        )
        attempts = dict(self.attempts)
        attempts[attempt.attempt_id] = replace(
            attempt, state=AttemptState.CANCEL_PENDING
        )
        generations = dict(self.robot_generations)
        generations[attempt.source_robot_id] = generation
        return (
            self._replace(jobs=jobs, attempts=attempts, robot_generations=generations),
            generation,
        )

    def confirm_cancel(self, job_id: str, now: datetime) -> OrchestratorState:
        """Finish cancellation only after observed physical confirmation."""
        job = self._job(job_id)
        if job.state is not JobState.CANCELING or job.active_attempt_id is None:
            raise ConflictError("cancel_not_requested")
        attempt = self._attempt(job.active_attempt_id)
        jobs = dict(self.jobs)
        jobs[job_id] = replace(
            job,
            state=JobState.CANCELLED,
            revision=job.revision + 1,
            active_attempt_id=None,
            updated_at=now,
        )
        attempts = dict(self.attempts)
        attempts[attempt.attempt_id] = replace(attempt, state=AttemptState.CANCELLED)
        units = dict(self.work_unit_states)
        units[attempt.work_unit_id] = WorkUnitState.CANCELLED
        leases = dict(self.robot_leases)
        leases.pop(attempt.source_robot_id, None)
        return self._replace(
            jobs=jobs,
            attempts=attempts,
            work_unit_states=units,
            robot_leases=leases,
        )

    def fail_job(
        self, job_id: str, attempt_id: str, failure_code: str, now: datetime
    ) -> OrchestratorState:
        """Finalize an active attempt without implicit retry."""
        job = self._job(job_id)
        if job.active_attempt_id != attempt_id or job.state not in _ACTIVE:
            raise ConflictError("attempt_ownership_conflict")
        attempt = self._attempt(attempt_id)
        jobs = dict(self.jobs)
        jobs[job_id] = replace(
            job,
            state=JobState.FAILED,
            revision=job.revision + 1,
            active_attempt_id=None,
            failure_code=failure_code,
            updated_at=now,
        )
        attempts = dict(self.attempts)
        attempts[attempt_id] = replace(
            attempt, state=AttemptState.FAILED, failure_code=failure_code
        )
        units = dict(self.work_unit_states)
        units[attempt.work_unit_id] = WorkUnitState.FAILED
        leases = dict(self.robot_leases)
        leases.pop(attempt.source_robot_id, None)
        return self._replace(
            jobs=jobs,
            attempts=attempts,
            work_unit_states=units,
            robot_leases=leases,
        )

    def retry_job(
        self, job_id: str, retry_job_id: str, now: datetime
    ) -> OrchestratorState:
        """Create a new queued job while preserving terminal history."""
        job = self._job(job_id)
        if job.state not in _TERMINAL:
            raise ConflictError("job_not_retryable")
        if retry_job_id in self.jobs:
            raise ConflictError("job_already_exists")
        jobs = dict(self.jobs)
        jobs[retry_job_id] = Job(
            retry_job_id,
            1,
            job.intent,
            JobState.QUEUED,
            now,
            now,
            retries_job_id=job_id,
        )
        return self._replace(
            jobs=jobs,
            queue=(*self.queue, retry_job_id),
            queue_revision=self.queue_revision + 1,
        )

    def require_robot_attention(
        self,
        attempt_id: str,
        run: RobotRun | None,
        correlation: RunCorrelation | None,
        now: datetime,
    ) -> OrchestratorState:
        """Fence and isolate one robot after ambiguous physical ownership."""
        attempt = self._attempt(attempt_id)
        job = self._job(attempt.job_id)
        attempts = dict(self.attempts)
        attempts[attempt_id] = replace(attempt, state=AttemptState.RECOVERY_REQUIRED)
        jobs = dict(self.jobs)
        jobs[job.job_id] = replace(
            job,
            state=JobState.NEEDS_ATTENTION,
            revision=job.revision + 1,
            updated_at=now,
        )
        units = dict(self.work_unit_states)
        units[attempt.work_unit_id] = WorkUnitState.NEEDS_ATTENTION
        generations = dict(self.robot_generations)
        generations[attempt.source_robot_id] = (
            generations.get(attempt.source_robot_id, 0) + 1
        )
        blocked = dict(self.blocked_robots)
        blocked[attempt.source_robot_id] = "physical_run_ownership_uncertain"
        runs = dict(self.robot_runs)
        correlations = dict(self.correlations)
        if run is not None:
            runs[run.robot_run_id] = run
        if correlation is not None:
            correlations[attempt_id] = correlation
        return self._replace(
            jobs=jobs,
            attempts=attempts,
            work_unit_states=units,
            robot_generations=generations,
            blocked_robots=blocked,
            robot_runs=runs,
            correlations=correlations,
        )

    def require_attention_for_active_leases(self, now: datetime) -> OrchestratorState:
        """Conservatively isolate every unfinished source robot after restart."""
        state = self
        for lease in tuple(self.robot_leases.values()):
            attempt = state.attempts[lease.attempt_id]
            if attempt.state is not AttemptState.RECOVERY_REQUIRED:
                state = state.require_robot_attention(
                    attempt.attempt_id, None, None, now
                )
        return state

    def next_pending_unit(self, job: Job) -> WorkUnit:
        """Return the first dependency-satisfied unit of a planned job."""
        plan = self._plan_for_job(job)
        completed = set(job.completed_work_unit_ids)
        for unit in plan.work_units:
            if unit.work_unit_id not in completed and set(unit.depends_on) <= completed:
                return unit
        raise ConflictError("job_has_no_pending_work_unit")

    def active_target_sets(self) -> tuple[frozenset[str], ...]:
        """Return target reservations used by the no-overlap invariant."""
        return tuple(
            frozenset(self._active_targets(job))
            for job in self.jobs.values()
            if job.state in _TARGET_RESERVED
        )

    def _active_targets(self, job: Job) -> tuple[str, ...]:
        if job.active_attempt_id is None:
            return ()
        attempt = self.attempts[job.active_attempt_id]
        plan = self._plan_for_job(job)
        return next(
            unit.canonical_targets
            for unit in plan.work_units
            if unit.work_unit_id == attempt.work_unit_id
        )

    def _plan_for_job(self, job: Job) -> ExecutionPlan:
        if job.plan_id is None or job.plan_id not in self.plans:
            raise ConflictError("job_plan_missing")
        return self.plans[job.plan_id]

    def _replace(self, **changes: object) -> OrchestratorState:
        return replace(
            self,
            commit_id=self.commit_id + 1,
            **changes,  # type: ignore[arg-type]
        )

    def _job(self, job_id: str) -> Job:
        try:
            return self.jobs[job_id]
        except KeyError as err:
            raise ValidationError("unknown_job", job_id) from err

    def _attempt(self, attempt_id: str) -> ExecutionAttempt:
        try:
            return self.attempts[attempt_id]
        except KeyError as err:
            raise ValidationError("unknown_attempt", attempt_id) from err
