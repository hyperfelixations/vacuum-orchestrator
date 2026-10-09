"""Global job registry, pending queue, and execution-ledger invariants."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from types import MappingProxyType

from .completion import CleaningReceipt, CleaningSource, CompletionQuality
from .errors import ConflictError, ValidationError
from .execution import ExecutionAttempt, RobotLease, RobotRun, RunCorrelation
from .holds import JobHold
from .intents import JobIntent, JobIntentPatch
from .job_defaults import JobDefaults
from .permissions import (
    deletable,
    editable,
    holdable,
    movable,
    require,
    retryable,
)
from .planning import (
    DispatchAssignment,
    ExecutionPlan,
    WorkUnit,
    plan_matches_intent,
)
from .queue_runs import QueueRun
from .requests import CommandOrigin
from .room_registry import RoomRegistry
from .templates import JobTemplate
from .types import (
    AttemptState,
    JobState,
    MoveDirection,
    ProvenanceKind,
    QueueMode,
    WorkUnitState,
)
from .validation import seconds

START_DELAY_SECONDS = 5.0
MAX_START_DELAY_SECONDS = 600
_ACTIVE = frozenset({JobState.DISPATCHING, JobState.RUNNING, JobState.CANCELING})
_TARGET_RESERVED = _ACTIVE | {JobState.NEEDS_ATTENTION}


@dataclass(frozen=True, slots=True)
class JobProvenance:
    """System origin of a job; template kinds name their template."""

    kind: ProvenanceKind = ProvenanceKind.MANUAL
    template_id: str | None = None

    def __post_init__(self) -> None:
        templated = self.kind in {ProvenanceKind.TEMPLATE, ProvenanceKind.AUTOMATIC}
        if templated != (self.template_id is not None):
            raise ValidationError("invalid_job_provenance")


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
    origin: CommandOrigin | None = None
    provenance: JobProvenance = JobProvenance()
    # Automatic queue starts wait until then; see dev doc "Startverzögerung".
    start_after: datetime | None = None


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
    room_registry: RoomRegistry = field(default_factory=RoomRegistry)
    templates: Mapping[str, JobTemplate] = field(default_factory=dict)
    queue_run: QueueRun | None = None
    queue_grace_seconds: float = 900
    job_defaults: JobDefaults = field(default_factory=JobDefaults)
    start_delay_seconds: float = START_DELAY_SECONDS
    job_holds: Mapping[str, JobHold] = field(default_factory=dict)

    def __post_init__(self) -> None:
        seconds(self.queue_grace_seconds)
        if self.queue_grace_seconds > 86400:
            raise ValidationError("queue_grace_out_of_range")
        validate_start_delay(self.start_delay_seconds)
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
            "templates",
            "job_holds",
        ):
            object.__setattr__(
                self, field_name, MappingProxyType(dict(getattr(self, field_name)))
            )
        if not self.installation_id.strip():
            raise ValidationError("empty_installation_id")
        for key, template in self.templates.items():
            if template.template_id != key:
                raise ValidationError("template_identity_mismatch")
            for target in template.intent.areas:
                if target.area_id not in self.room_registry.rooms:
                    raise ValidationError("template_unknown_room")
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
        for key, hold in self.job_holds.items():
            if key != hold.job_id or key not in queued_ids:
                raise ValidationError("invalid_job_hold")

    @classmethod
    def empty(cls, installation_id: str) -> OrchestratorState:
        """Create an empty integration-wide state."""
        return cls(installation_id, 0, 0, QueueMode.IDLE, (), {}, {})

    @property
    def active_job_count(self) -> int:
        """Return jobs that are starting, running or being cancelled."""
        return sum(job.state in _ACTIVE for job in self.jobs.values())

    @property
    def attention_job_count(self) -> int:
        """Return jobs with unresolved physical ownership."""
        return sum(job.state is JobState.NEEDS_ATTENTION for job in self.jobs.values())

    @property
    def needs_attention(self) -> bool:
        """Return whether any robot or job has unresolved physical ownership."""
        return bool(self.blocked_robots) or any(
            job.state is JobState.NEEDS_ATTENTION for job in self.jobs.values()
        )

    def add_job(
        self,
        job_id: str,
        intent: JobIntent,
        now: datetime,
        *,
        origin: CommandOrigin | None = None,
        provenance: JobProvenance | None = None,
    ) -> OrchestratorState:
        """Append a new job with every setting its mode uses to the queue.

        Without an explicit provenance, a request without a user is an automation.
        """
        if job_id in self.jobs:
            raise ConflictError("job_already_exists")
        intent = self.job_defaults.complete(intent)
        if intent.dedupe_key is not None and any(
            job.state is JobState.QUEUED and job.intent.dedupe_key == intent.dedupe_key
            for job in self.jobs.values()
        ):
            raise ConflictError("dedupe_key_already_queued", path=("dedupe_key",))
        jobs = dict(self.jobs)
        if provenance is None:
            provenance = JobProvenance(
                ProvenanceKind.AUTOMATION
                if origin is not None and origin.user_id is None
                else ProvenanceKind.MANUAL
            )
        jobs[job_id] = Job(
            job_id,
            1,
            intent,
            JobState.QUEUED,
            now,
            now,
            origin=origin,
            provenance=provenance,
            start_after=self.delayed_start(now),
        )
        return self._replace(
            jobs=jobs,
            queue=(*self.queue, job_id),
            queue_revision=self.queue_revision + 1,
        )

    def update_job(
        self,
        job_id: str,
        patch: JobIntentPatch,
        now: datetime,
        *,
        hold_id: str | None = None,
    ) -> OrchestratorState:
        """Apply a partial update to a queued job; saving also ends its hold."""
        job = self._job(job_id)
        require(editable(job))
        held = self._permitted_hold(job_id, hold_id, now)
        if patch.empty and held is not None:
            intent = job.intent
        else:
            intent = self.job_defaults.complete(patch.apply(job.intent))
        changed = intent != job.intent
        if not changed and held is None:
            return self
        if intent.dedupe_key is not None and any(
            other.job_id != job_id
            and other.state is JobState.QUEUED
            and other.intent.dedupe_key == intent.dedupe_key
            for other in self.jobs.values()
        ):
            raise ConflictError("dedupe_key_already_queued", path=("dedupe_key",))
        jobs = dict(self.jobs)
        jobs[job_id] = replace(
            job,
            intent=intent,
            revision=job.revision + changed,
            updated_at=now if changed else job.updated_at,
            start_after=self.delayed_start(now),
        )
        return self._replace(jobs=jobs, job_holds=self._holds_without(job_id))

    def delete_job(
        self, job_id: str, now: datetime, *, hold_id: str | None = None
    ) -> OrchestratorState:
        """Delete queued or terminal history, never active, ambiguous or held work."""
        job = self._job(job_id)
        require(deletable(job))
        self._permitted_hold(job_id, hold_id, now)
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
            job_holds=self._holds_without(job_id),
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
        require(movable(job))
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
            start_after=None,
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
            job_holds=self._holds_without(job_id),
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
            last_observation_at=observed_start_at,
        )
        return self._replace(
            attempts=attempts,
            room_registry=self.room_registry.mark_started(attempt.job_id),
        )

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
            if (
                existing_run is not None
                and existing_correlation is not None
                and attempt.state is AttemptState.SUCCEEDED
                and existing_run.completion_quality is CompletionQuality.DERIVED
                and run.completion_quality is CompletionQuality.CONFIRMED
                and not correlation.requires_attention
                and replace(
                    existing_run,
                    completion_quality=CompletionQuality.CONFIRMED,
                    history_start=run.history_start,
                    history_end=run.history_end,
                    causal_token=run.causal_token,
                )
                == run
            ):
                receipt = self.room_registry.receipts.get(f"attempt:{attempt_id}")
                registry = (
                    self.room_registry
                    if receipt is None
                    else self.room_registry.record(
                        replace(
                            receipt,
                            quality=CompletionQuality.CONFIRMED,
                            evidence=correlation.reason_codes,
                        )
                    )
                )
                return self._replace(
                    attempts={
                        **self.attempts,
                        attempt_id: replace(
                            attempt, completion_quality=CompletionQuality.CONFIRMED
                        ),
                    },
                    robot_runs={**self.robot_runs, run.robot_run_id: run},
                    correlations={**self.correlations, attempt_id: correlation},
                    room_registry=registry,
                )
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
        quality = run.completion_quality or CompletionQuality.DERIVED
        attempts[attempt_id] = replace(
            attempt, state=AttemptState.SUCCEEDED, completion_quality=quality
        )
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
        registry = self.room_registry.mark_started(job.job_id)
        if job.job_id in registry.admissions:
            unit = next(
                item
                for item in plan.work_units
                if item.work_unit_id == attempt.work_unit_id
            )
            registry = registry.record(
                CleaningReceipt(
                    f"attempt:{attempt_id}",
                    CleaningSource.VOI,
                    attempt_id,
                    unit.canonical_targets,
                    unit.operation,
                    run.observed_end or now,
                    quality,
                    correlation.reason_codes,
                )
            )
        if final:
            registry = registry.finish(job.job_id)
        return self._replace(
            jobs=jobs,
            attempts=attempts,
            robot_runs=runs,
            correlations=correlations,
            work_unit_states=work_unit_states,
            robot_leases=leases,
            room_registry=registry,
        )

    def request_cancel(
        self, job_id: str, now: datetime, *, return_to_dock: bool = False
    ) -> tuple[OrchestratorState, int | None]:
        """Cancel pending work or atomically fence active physical ownership."""
        job = self._job(job_id)
        if job.state is JobState.QUEUED or (
            job.state is JobState.DISPATCHING and job.active_attempt_id is None
        ):
            self._permitted_hold(job_id, None, now)
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
                    job_holds=self._holds_without(job_id),
                    queue=tuple(item for item in self.queue if item != job_id),
                    queue_revision=self.queue_revision + (job_id in self.queue),
                    work_unit_states={
                        key: WorkUnitState.CANCELLED
                        if job.plan_id is not None
                        and key
                        in {
                            unit.work_unit_id
                            for unit in self.plans[job.plan_id].work_units
                        }
                        and value is WorkUnitState.PENDING
                        else value
                        for key, value in self.work_unit_states.items()
                    },
                    room_registry=self.room_registry.finish(job_id, never_started=True),
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
        generations = dict(self.robot_generations)
        generations[attempt.source_robot_id] = generation
        attempt = replace(
            attempt, cancel_requested_at=now, return_to_dock=return_to_dock
        )
        if attempt.command_boundary_at is None:
            # No start was sent; the fence alone prevents a later one.
            return (
                self._cancelled(
                    job, attempt, now, never_started=True, robot_generations=generations
                ),
                generation,
            )
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
        return (
            self._replace(jobs=jobs, attempts=attempts, robot_generations=generations),
            generation,
        )

    def mark_stop_sent(self, attempt_id: str, now: datetime) -> OrchestratorState:
        """Persist the stop boundary that later cancel evidence must follow."""
        attempt = self._attempt(attempt_id)
        if (
            attempt.state is not AttemptState.CANCEL_PENDING
            or attempt.stop_sent_at is not None
        ):
            return self
        attempts = dict(self.attempts)
        attempts[attempt_id] = replace(attempt, stop_sent_at=now)
        return self._replace(attempts=attempts)

    def confirm_cancel(
        self, job_id: str, now: datetime, *, never_started: bool = False
    ) -> OrchestratorState:
        """Finish cancellation only after observed physical confirmation."""
        job = self._job(job_id)
        if job.state is not JobState.CANCELING or job.active_attempt_id is None:
            raise ConflictError("cancel_not_requested")
        return self._cancelled(
            job,
            self._attempt(job.active_attempt_id),
            now,
            never_started=never_started,
        )

    def _cancelled(
        self,
        job: Job,
        attempt: ExecutionAttempt,
        now: datetime,
        *,
        never_started: bool,
        **changes: object,
    ) -> OrchestratorState:
        jobs = dict(self.jobs)
        jobs[job.job_id] = replace(
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
            room_registry=self.room_registry.finish(
                job.job_id, never_started=never_started
            ),
            **changes,
        )

    def fail_job(
        self,
        job_id: str,
        attempt_id: str,
        failure_code: str,
        now: datetime,
        *,
        never_started: bool = False,
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
            room_registry=self.room_registry.finish(
                job_id, never_started=never_started
            ),
        )

    def retry_job(
        self,
        job_id: str,
        retry_job_id: str,
        now: datetime,
        *,
        origin: CommandOrigin | None = None,
    ) -> OrchestratorState:
        """Create a new queued job while preserving terminal history."""
        job = self._job(job_id)
        require(retryable(job))
        if retry_job_id in self.jobs:
            raise ConflictError("job_already_exists")
        jobs = dict(self.jobs)
        jobs[retry_job_id] = Job(
            retry_job_id,
            1,
            self.job_defaults.complete(job.intent),
            JobState.QUEUED,
            now,
            now,
            retries_job_id=job_id,
            origin=origin,
            provenance=JobProvenance(ProvenanceKind.RETRY),
            start_after=self.delayed_start(now),
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

    def resolve_interrupted_leases(self, now: datetime) -> OrchestratorState:
        """Finish attempts without a command boundary; isolate all others."""
        state = self
        for lease in tuple(self.robot_leases.values()):
            attempt = state.attempts[lease.attempt_id]
            if attempt.state is AttemptState.RECOVERY_REQUIRED:
                continue
            if attempt.command_boundary_at is not None:
                state = state.require_robot_attention(
                    attempt.attempt_id, None, None, now
                )
            elif state.jobs[attempt.job_id].state is JobState.CANCELING:
                state = state.confirm_cancel(attempt.job_id, now, never_started=True)
            else:
                state = state.fail_job(
                    attempt.job_id,
                    attempt.attempt_id,
                    "interrupted_before_start",
                    now,
                    never_started=True,
                )
        return state

    def resolve_recovery(
        self, source_robot_id: str, now: datetime, *, assumed_stopped: bool = False
    ) -> OrchestratorState:
        """Abandon uncertain work after a verified or operator-confirmed stop."""
        if source_robot_id not in self.blocked_robots:
            raise ConflictError("robot_not_needing_recovery")
        jobs, attempts, units = (
            dict(self.jobs),
            dict(self.attempts),
            dict(self.work_unit_states),
        )
        leases, blocked = dict(self.robot_leases), dict(self.blocked_robots)
        registry = self.room_registry
        lease = leases.pop(source_robot_id, None)
        if lease is not None:
            attempt = attempts[lease.attempt_id]
            job = jobs[attempt.job_id]
            reason = (
                "operator_assumed_stopped" if assumed_stopped else "recovery_abandoned"
            )
            attempts[attempt.attempt_id] = replace(
                attempt, state=AttemptState.FAILED, failure_code=reason
            )
            jobs[job.job_id] = replace(
                job,
                state=JobState.FAILED,
                active_attempt_id=None,
                revision=job.revision + 1,
                failure_code=reason,
                updated_at=now,
            )
            for unit in self._plan_for_job(job).work_units:
                if unit.work_unit_id not in job.completed_work_unit_ids:
                    units[unit.work_unit_id] = (
                        WorkUnitState.FAILED
                        if unit.work_unit_id == attempt.work_unit_id
                        else WorkUnitState.CANCELLED
                    )
            registry = registry.finish(job.job_id)
        blocked.pop(source_robot_id)
        return self._replace(
            jobs=jobs,
            attempts=attempts,
            work_unit_states=units,
            robot_leases=leases,
            blocked_robots=blocked,
            room_registry=registry,
            robot_generations={
                **self.robot_generations,
                source_robot_id: self.robot_generations.get(source_robot_id, 0) + 1,
            },
        )

    def next_pending_unit(self, job: Job) -> WorkUnit:
        """Return the first dependency-satisfied unit of a planned job."""
        plan = self._plan_for_job(job)
        completed = set(job.completed_work_unit_ids)
        for unit in plan.work_units:
            if unit.work_unit_id not in completed and set(unit.depends_on) <= completed:
                return unit
        raise ConflictError("job_has_no_pending_work_unit")

    def active_target_sets(
        self, *, excluding_job_id: str | None = None
    ) -> tuple[frozenset[str], ...]:
        """Return target reservations used by the no-overlap invariant."""
        return tuple(
            frozenset(self._active_targets(job))
            for job in self.jobs.values()
            if job.state in _TARGET_RESERVED and job.job_id != excluding_job_id
        )

    def _active_targets(self, job: Job) -> tuple[str, ...]:
        return tuple(target.area_id for target in job.intent.areas)

    def _plan_for_job(self, job: Job) -> ExecutionPlan:
        if job.plan_id is None or job.plan_id not in self.plans:
            raise ConflictError("job_plan_missing")
        return self.plans[job.plan_id]

    def hold_job(self, hold: JobHold, now: datetime) -> OrchestratorState:
        """Protect a waiting job from every start until the hold ends."""
        job = self._job(hold.job_id)
        require(holdable(job))
        self._permitted_hold(hold.job_id, None, now)
        return self._replace(job_holds={**self.job_holds, hold.job_id: hold})

    def renew_job_hold(self, hold_id: str, now: datetime) -> OrchestratorState:
        """Extend the holder's own active lease."""
        hold = self._hold_by_id(hold_id)
        if hold is None or not hold.active(now):
            raise ConflictError("hold_expired")
        return self._replace(
            job_holds={**self.job_holds, hold.job_id: hold.renewed(now)}
        )

    def release_job_hold(self, hold_id: str, now: datetime) -> OrchestratorState:
        """End a hold and restart the job's start delay; a gone hold is a no-op."""
        hold = self._hold_by_id(hold_id)
        if hold is None:
            return self
        return self._released(hold.job_id, now)

    def expire_job_holds(self, now: datetime) -> OrchestratorState:
        """End every lapsed hold as if its holder had released it now."""
        lapsed = [key for key, hold in self.job_holds.items() if not hold.active(now)]
        if not lapsed:
            return self
        start_after = self.delayed_start(now)
        return self._replace(
            jobs={
                **self.jobs,
                **{
                    key: replace(self.jobs[key], start_after=start_after)
                    for key in lapsed
                },
            },
            job_holds={
                key: hold for key, hold in self.job_holds.items() if key not in lapsed
            },
        )

    def active_hold(self, job_id: str, now: datetime) -> JobHold | None:
        """Return the hold that currently protects a job."""
        hold = self.job_holds.get(job_id)
        return hold if hold is not None and hold.active(now) else None

    def _permitted_hold(
        self, job_id: str, hold_id: str | None, now: datetime
    ) -> JobHold | None:
        """Return the caller's own active hold; reject foreign and lapsed ones.

        A token whose hold is gone never authorizes a later change.
        """
        hold = self.active_hold(job_id, now)
        if hold is not None and hold.hold_id != hold_id:
            raise ConflictError("job_held", hold.purpose.value)
        if hold_id is not None and hold is None:
            raise ConflictError("hold_expired")
        return hold

    def _hold_by_id(self, hold_id: str) -> JobHold | None:
        return next(
            (hold for hold in self.job_holds.values() if hold.hold_id == hold_id),
            None,
        )

    def _released(self, job_id: str, now: datetime) -> OrchestratorState:
        jobs = dict(self.jobs)
        jobs[job_id] = replace(jobs[job_id], start_after=self.delayed_start(now))
        return self._replace(jobs=jobs, job_holds=self._holds_without(job_id))

    def _holds_without(self, job_id: str) -> dict[str, JobHold]:
        return {key: value for key, value in self.job_holds.items() if key != job_id}

    def delayed_start(self, now: datetime) -> datetime | None:
        """Return when a job queued or edited now may start automatically."""
        if not self.start_delay_seconds:
            return None
        return now + timedelta(seconds=self.start_delay_seconds)

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


def validate_start_delay(value: float) -> None:
    """Accept a start delay between zero and ten minutes."""
    try:
        seconds(value)
    except ValidationError as err:
        raise ValidationError("start_delay_out_of_range", str(value)) from err
    if value > MAX_START_DELAY_SECONDS:
        raise ValidationError("start_delay_out_of_range", str(value))
