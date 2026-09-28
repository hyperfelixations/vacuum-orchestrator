"""Explicit versioned JSON codec for critical orchestration state."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

from ..domain.completion import CleaningSource, CompletionQuality
from ..domain.errors import StorageIntegrityError, ValidationError
from ..domain.execution import (
    ExecutionAttempt,
    ExecutionPolicy,
    RobotLease,
    RobotRun,
    RunCorrelation,
)
from ..domain.intents import (
    CleaningPreferences,
    JobIntent,
    TargetRef,
    VendorExtension,
)
from ..domain.planning import (
    DispatchAssignment,
    ExecutionPlan,
    PreferenceResolution,
    WorkUnit,
    plan_matches_intent,
)
from ..domain.queue import Job, OrchestratorState
from ..domain.queue_runs import QueueRun
from ..domain.requests import CommandOrigin
from ..domain.room_registry import RoomRegistry
from ..domain.rooms import Room
from ..domain.templates import JobTemplate
from ..domain.types import (
    AttemptState,
    CleaningMode,
    CorrelationConfidence,
    CorrelationState,
    JobState,
    MopRoute,
    OperationKind,
    PassScope,
    QueueMode,
    SemanticLevel,
    SettingsPolicy,
    WorkUnitState,
)
from .codec_values import (
    _bool,
    _decode_datetime,
    _decode_optional_datetime,
    _encode_datetime,
    _encode_optional_datetime,
    _enum,
    _enum_value,
    _EnumT,
    _int,
    _object,
    _object_list,
    _optional_enum,
    _optional_str,
    _pair_list,
    _str,
    _string_list,
    _string_mapping,
)
from .integrity import JsonObject
from .room_codec import _number, decode_room_registry, encode_room_registry

SCHEMA_VERSION = 3


def encode_orchestrator_state(state: OrchestratorState) -> JsonObject:
    """Encode state without relying on dataclass implementation details."""
    return {
        "schema_version": SCHEMA_VERSION,
        "installation_id": state.installation_id,
        "commit_id": state.commit_id,
        "queue_revision": state.queue_revision,
        "mode": state.mode.value,
        "queue": list(state.queue),
        "jobs": {key: _encode_job(value) for key, value in state.jobs.items()},
        "robot_generations": dict(state.robot_generations),
        "plans": {key: _encode_plan(value) for key, value in state.plans.items()},
        "work_unit_states": {
            key: value.value for key, value in state.work_unit_states.items()
        },
        "assignments": {
            key: _encode_assignment(value) for key, value in state.assignments.items()
        },
        "attempts": {
            key: _encode_attempt(value) for key, value in state.attempts.items()
        },
        "robot_runs": {
            key: _encode_robot_run(value) for key, value in state.robot_runs.items()
        },
        "correlations": {
            key: _encode_correlation(value) for key, value in state.correlations.items()
        },
        "robot_leases": {
            key: _encode_lease(value) for key, value in state.robot_leases.items()
        },
        "blocked_robots": dict(state.blocked_robots),
        "room_registry": encode_room_registry(state.room_registry),
        "queue_grace_seconds": state.queue_grace_seconds,
        "queue_run": None
        if state.queue_run is None
        else {
            "run_id": state.queue_run.run_id,
            "started_at": _encode_datetime(state.queue_run.started_at),
            "grace_seconds": state.queue_run.grace_seconds,
            "idle_since": _encode_optional_datetime(state.queue_run.idle_since),
            "completed_at": _encode_optional_datetime(state.queue_run.completed_at),
        },
        "templates": {
            key: {
                "template_id": value.template_id,
                "name": value.name,
                "intent": _encode_intent(value.intent),
                "updated_at": _encode_datetime(value.updated_at),
                "enabled": value.enabled,
                "automatic": value.automatic,
                "demand_tokens": dict(value.demand_tokens),
            }
            for key, value in state.templates.items()
        },
    }


def decode_orchestrator_state(data: JsonObject) -> OrchestratorState:
    """Decode and relationally validate a complete current snapshot."""
    try:
        if data["schema_version"] != SCHEMA_VERSION:
            raise StorageIntegrityError("unsupported_storage_schema")
        state = OrchestratorState(
            installation_id=_str(data["installation_id"]),
            commit_id=_int(data["commit_id"]),
            queue_revision=_int(data["queue_revision"]),
            mode=_enum(QueueMode, data["mode"]),
            queue=tuple(_string_list(data["queue"])),
            jobs={
                key: _decode_job(_object(value))
                for key, value in _string_mapping(data["jobs"]).items()
            },
            robot_generations={
                key: _int(value)
                for key, value in _string_mapping(data["robot_generations"]).items()
            },
            plans={
                key: _decode_plan(_object(value))
                for key, value in _string_mapping(data["plans"]).items()
            },
            work_unit_states={
                key: _enum(WorkUnitState, value)
                for key, value in _string_mapping(data["work_unit_states"]).items()
            },
            assignments={
                key: _decode_assignment(_object(value))
                for key, value in _string_mapping(data["assignments"]).items()
            },
            attempts={
                key: _decode_attempt(_object(value))
                for key, value in _string_mapping(data["attempts"]).items()
            },
            robot_runs={
                key: _decode_robot_run(_object(value))
                for key, value in _string_mapping(data["robot_runs"]).items()
            },
            correlations={
                key: _decode_correlation(_object(value))
                for key, value in _string_mapping(data["correlations"]).items()
            },
            robot_leases={
                key: _decode_lease(_object(value))
                for key, value in _string_mapping(data["robot_leases"]).items()
            },
            blocked_robots={
                key: _str(value)
                for key, value in _string_mapping(data["blocked_robots"]).items()
            },
            room_registry=decode_room_registry(_object(data["room_registry"])),
            queue_grace_seconds=_number(data.get("queue_grace_seconds", 900)),
            queue_run=_decode_queue_run(data.get("queue_run")),
            templates={
                key: _decode_template(_object(value))
                for key, value in _string_mapping(data.get("templates", {})).items()
            },
        )
    except StorageIntegrityError:
        raise
    except (KeyError, TypeError, ValueError, ValidationError) as err:
        raise StorageIntegrityError("invalid_storage_payload") from err
    _validate_relational_integrity(state)
    return state


def _decode_template(data: JsonObject) -> JobTemplate:
    return JobTemplate(
        _str(data["template_id"]),
        _str(data["name"]),
        _decode_intent(_object(data["intent"])),
        _decode_datetime(data["updated_at"]),
        _bool(data["enabled"]),
        _bool(data["automatic"]),
        {
            key: _str(value)
            for key, value in _string_mapping(data["demand_tokens"]).items()
        },
    )


def _decode_queue_run(value: object) -> QueueRun | None:
    if value is None:
        return None
    data = _object(value)
    return QueueRun(
        _str(data["run_id"]),
        _decode_datetime(data["started_at"]),
        _number(data["grace_seconds"]),
        _decode_optional_datetime(data["idle_since"]),
        _decode_optional_datetime(data["completed_at"]),
    )


def migrate_schema_two(data: JsonObject) -> OrchestratorState:
    """Import legacy area identities without inventing grants or device mappings."""
    if data.get("schema_version") != 2:
        raise StorageIntegrityError("unsupported_previous_storage_schema")
    candidate = {
        **data,
        "schema_version": SCHEMA_VERSION,
        "room_registry": encode_room_registry(RoomRegistry()),
    }
    state = decode_orchestrator_state(candidate)
    rooms = {
        target.area_id: Room(
            target.area_id, target.area_id, area_id=target.area_id, area_missing=True
        )
        for job in state.jobs.values()
        for target in job.intent.areas
    }
    return replace(state, room_registry=RoomRegistry(rooms))


def migrate_schema_one(data: JsonObject, installation_id: str) -> OrchestratorState:
    """Convert the pre-release fleet snapshot without changing legacy storage."""
    try:
        if data["schema_version"] != 1:
            raise StorageIntegrityError("unsupported_legacy_storage_schema")
        legacy_plans = _string_mapping(data["plans"])
        plans = {
            key: _migrate_plan(_object(value)) for key, value in legacy_plans.items()
        }
        plan_by_job = {plan.job_id: plan.plan_id for plan in plans.values()}
        attempts = {
            key: _decode_attempt({**_object(value), "observed_start_at": None})
            for key, value in _string_mapping(data["attempts"]).items()
        }
        jobs = {
            key: _migrate_job(_object(value), plan_by_job.get(key))
            for key, value in _string_mapping(data["jobs"]).items()
        }
        work_unit_states = {
            unit.work_unit_id: WorkUnitState.PENDING
            for plan in plans.values()
            for unit in plan.work_units
        }
        old_units = {
            _str(item["work_unit_id"]): item
            for value in legacy_plans.values()
            for item in _object_list(_object(value)["work_units"])
        }
        assignments: dict[str, DispatchAssignment] = {}
        for attempt_id, attempt in attempts.items():
            old_unit = old_units[attempt.work_unit_id]
            assignments[attempt_id] = DispatchAssignment(
                attempt.work_unit_id,
                attempt.robot_id,
                attempt.source_robot_id,
                _str(old_unit["adapter"]),
                tuple(_string_list(old_unit["adapter_targets"])),
                _str(old_unit["capability_revision"]),
                PreferenceResolution((), ()),
            )
            work_unit_states[attempt.work_unit_id] = _legacy_work_unit_state(
                attempt.state
            )
        runs = {
            key: _decode_robot_run(_object(value))
            for key, value in _string_mapping(data["robot_runs"]).items()
        }
        correlations = {
            key: _decode_correlation(_object(value))
            for key, value in _string_mapping(data["correlations"]).items()
        }
        leases = {
            key: _decode_lease(_object(value))
            for key, value in _string_mapping(data["robot_leases"]).items()
        }
        old_attention = _bool(data["needs_attention"])
        blocked = (
            {source_id: "legacy_recovery_required" for source_id in leases}
            if old_attention
            else {}
        )
        if old_attention and not blocked:
            blocked["legacy:unscoped"] = "legacy_recovery_required"
        state = OrchestratorState(
            installation_id=installation_id,
            commit_id=_int(data["commit_id"]),
            queue_revision=_int(data["queue_revision"]),
            mode=_legacy_queue_mode(_str(data["mode"])),
            queue=tuple(
                job_id
                for job_id in _string_list(data["queue"])
                if jobs[job_id].state is JobState.QUEUED
            ),
            jobs=jobs,
            robot_generations={
                key: _int(value)
                for key, value in _string_mapping(data["robot_generations"]).items()
            },
            plans=plans,
            work_unit_states=work_unit_states,
            assignments=assignments,
            attempts=attempts,
            robot_runs=runs,
            correlations=correlations,
            robot_leases=leases,
            blocked_robots=blocked,
        )
    except StorageIntegrityError:
        raise
    except (KeyError, TypeError, ValueError) as err:
        raise StorageIntegrityError("invalid_legacy_storage_payload") from err
    _validate_relational_integrity(state)
    return state


def _migrate_job(data: JsonObject, plan_id: str | None) -> Job:
    old_state = _str(data["state"])
    state_map = {
        "queued": JobState.QUEUED,
        "executing": JobState.NEEDS_ATTENTION,
        "cancel_requested": JobState.NEEDS_ATTENTION,
        "succeeded": JobState.COMPLETED,
        "failed": JobState.FAILED,
        "cancelled": JobState.CANCELLED,
        "needs_attention": JobState.NEEDS_ATTENTION,
    }
    try:
        state = state_map[old_state]
    except KeyError as err:
        raise StorageIntegrityError("unsupported_legacy_job_state") from err
    active_attempt_id = _optional_str(data["active_attempt_id"])
    return Job(
        job_id=_str(data["job_id"]),
        revision=_int(data["revision"]),
        intent=_migrate_intent(_object(data["intent"])),
        state=state,
        created_at=_decode_datetime(data["created_at"]),
        updated_at=_decode_datetime(data["updated_at"]),
        plan_id=plan_id,
        active_attempt_id=(
            active_attempt_id if state is JobState.NEEDS_ATTENTION else None
        ),
        retries_job_id=_optional_str(data["retries_job_id"]),
        failure_code=_optional_str(data["failure_code"]),
    )


def _migrate_intent(data: JsonObject) -> JobIntent:
    operations = _object_list(data["operations"])
    kinds = tuple(_str(item["kind"]) for item in operations)
    mode_map = {
        ("vacuum",): CleaningMode.VACUUM,
        ("mop",): CleaningMode.MOP,
        ("combined",): CleaningMode.VACUUM_AND_MOP,
        ("vacuum", "mop"): CleaningMode.VACUUM_THEN_MOP,
    }
    try:
        mode = mode_map[kinds]
    except KeyError as err:
        raise StorageIntegrityError("ambiguous_legacy_operation_sequence") from err
    pass_counts = {_int(_object(item["passes"])["count"]) for item in operations}
    if len(pass_counts) != 1:
        raise StorageIntegrityError("ambiguous_legacy_pass_count")
    pass_scopes = {
        _enum(PassScope, _object(item["passes"])["scope"]) for item in operations
    }
    if len(pass_scopes) != 1:
        raise StorageIntegrityError("ambiguous_legacy_pass_scope")
    if next(iter(pass_scopes)) is not PassScope.TARGET_SET:
        raise StorageIntegrityError("unsupported_legacy_pass_scope")
    raw_extensions = [item["vendor_extension"] for item in operations]
    if any(value != raw_extensions[0] for value in raw_extensions[1:]):
        raise StorageIntegrityError("ambiguous_legacy_vendor_extension")

    def one_setting(name: str, enum_type: type[_EnumT]) -> _EnumT | None:
        values = {
            _enum(enum_type, item[name])
            for item in operations
            if item[name] is not None
        }
        if len(values) > 1:
            raise StorageIntegrityError(f"ambiguous_legacy_{name}")
        return next(iter(values), None)

    requirements = _object_list(data["requirements"])
    required_on = tuple(
        _str(item["reference"])
        for item in requirements
        if _str(item["expected_state"]) == "on"
    )
    required_off = tuple(
        _str(item["reference"])
        for item in requirements
        if _str(item["expected_state"]) == "off"
    )
    if len(required_on) + len(required_off) != len(requirements):
        raise StorageIntegrityError("unsupported_legacy_requirement_state")
    return JobIntent(
        areas=tuple(
            TargetRef(_str(item["area_id"]), _optional_str(item["map_context"]))
            for item in _object_list(data["targets"])
        ),
        mode=mode,
        preferences=CleaningPreferences(
            cast(SemanticLevel | None, one_setting("vacuum_level", SemanticLevel)),
            cast(SemanticLevel | None, one_setting("water_level", SemanticLevel)),
            cast(MopRoute | None, one_setting("mop_route", MopRoute)),
        ),
        passes=next(iter(pass_counts)),
        dedupe_key=_optional_str(data["idempotency_key"]),
        required_on=required_on,
        required_off=required_off,
        vendor_extension=_migrate_vendor_extension(raw_extensions[0]),
    )


def _migrate_plan(data: JsonObject) -> ExecutionPlan:
    return ExecutionPlan(
        _str(data["plan_id"]),
        _str(data["job_id"]),
        tuple(_migrate_work_unit(item) for item in _object_list(data["work_units"])),
    )


def _migrate_work_unit(data: JsonObject) -> WorkUnit:
    operation = _object(data["operation"])
    kind = _str(operation["kind"])
    return WorkUnit(
        work_unit_id=_str(data["work_unit_id"]),
        operation=(
            OperationKind.VACUUM_AND_MOP if kind == "combined" else OperationKind(kind)
        ),
        canonical_targets=tuple(_string_list(data["canonical_targets"])),
        map_context=_optional_str(data["map_context"]),
        passes=_int(_object(operation["passes"])["count"]),
        pass_scope=_enum(PassScope, _object(operation["passes"])["scope"]),
        preferences=CleaningPreferences(
            _optional_enum(SemanticLevel, operation["vacuum_level"]),
            _optional_enum(SemanticLevel, operation["water_level"]),
            _optional_enum(MopRoute, operation["mop_route"]),
        ),
        settings_policy=SettingsPolicy.BEST_EFFORT,
        vendor_extension=_migrate_vendor_extension(operation["vendor_extension"]),
        depends_on=tuple(_string_list(data["depends_on"])),
    )


def _migrate_vendor_extension(value: Any) -> VendorExtension | None:
    if value is None:
        return None
    extension = _object(value)
    return VendorExtension(
        _str(extension["namespace"]),
        tuple(
            (_str(item[0]), cast(Any, item[1]))
            for item in _pair_list(extension["parameters"])
        ),
    )


def _legacy_work_unit_state(state: AttemptState) -> WorkUnitState:
    if state is AttemptState.SUCCEEDED:
        return WorkUnitState.COMPLETED
    if state is AttemptState.FAILED:
        return WorkUnitState.FAILED
    if state is AttemptState.CANCELLED:
        return WorkUnitState.CANCELLED
    if state is AttemptState.RECOVERY_REQUIRED:
        return WorkUnitState.NEEDS_ATTENTION
    return WorkUnitState.ACTIVE


def _legacy_queue_mode(value: str) -> QueueMode:
    if value == "running":
        return QueueMode.RUNNING
    if value in {"paused", "needs_attention"}:
        return QueueMode.PAUSED
    if value == "idle":
        return QueueMode.IDLE
    raise StorageIntegrityError("unsupported_legacy_queue_mode")


def _validate_relational_integrity(state: OrchestratorState) -> None:
    for room in state.room_registry.rooms.values():
        if (
            room.release
            and room.release.queue_run_id is not None
            and (
                state.queue_run is None
                or not state.queue_run.active
                or room.release.queue_run_id != state.queue_run.run_id
            )
        ):
            raise StorageIntegrityError("release_queue_run_mismatch")
    if state.commit_id < 0 or state.queue_revision < 0:
        raise StorageIntegrityError("negative_storage_revision")
    if any(value < 0 for value in state.robot_generations.values()):
        raise StorageIntegrityError("negative_robot_generation")
    if any(key != job.job_id for key, job in state.jobs.items()):
        raise StorageIntegrityError("job_storage_key_mismatch")
    if any(key != plan.plan_id for key, plan in state.plans.items()):
        raise StorageIntegrityError("plan_storage_key_mismatch")
    work_units = {
        unit.work_unit_id: (plan.job_id, unit)
        for plan in state.plans.values()
        for unit in plan.work_units
    }
    if sum(len(plan.work_units) for plan in state.plans.values()) != len(work_units):
        raise StorageIntegrityError("duplicate_work_unit_identity")
    if set(state.work_unit_states) != set(work_units):
        raise StorageIntegrityError("work_unit_state_ledger_mismatch")
    for attempt_id, ledger_attempt in state.attempts.items():
        planned = work_units.get(ledger_attempt.work_unit_id)
        assignment = state.assignments.get(attempt_id)
        if (
            attempt_id != ledger_attempt.attempt_id
            or ledger_attempt.job_id not in state.jobs
            or planned is None
            or planned[0] != ledger_attempt.job_id
            or assignment is None
            or assignment.work_unit_id != ledger_attempt.work_unit_id
            or assignment.robot_id != ledger_attempt.robot_id
            or assignment.source_robot_id != ledger_attempt.source_robot_id
            or (
                assignment.room_targets
                and set(assignment.room_targets) != set(planned[1].canonical_targets)
            )
        ):
            raise StorageIntegrityError("attempt_ledger_reference_mismatch")
    for source_robot_id, lease in state.robot_leases.items():
        lease_attempt = state.attempts.get(lease.attempt_id)
        generation = state.robot_generations.get(source_robot_id)
        if (
            source_robot_id != lease.source_robot_id
            or lease_attempt is None
            or lease_attempt.source_robot_id != source_robot_id
            or lease_attempt.work_unit_id != lease.work_unit_id
            or lease_attempt.robot_id != lease.robot_id
            or lease_attempt.robot_generation != lease.generation
            or generation is None
            or generation < lease.generation
        ):
            raise StorageIntegrityError("robot_lease_reference_mismatch")
    active_states = {
        JobState.RUNNING,
        JobState.CANCELING,
        JobState.NEEDS_ATTENTION,
    }
    for job in state.jobs.values():
        if job.plan_id is not None:
            plan = state.plans.get(job.plan_id)
            if plan is None or plan.job_id != job.job_id:
                raise StorageIntegrityError("job_plan_reference_mismatch")
            if not plan_matches_intent(plan, job.intent):
                raise StorageIntegrityError("job_plan_semantics_mismatch")
        if job.state in active_states or (
            job.state is JobState.DISPATCHING and job.active_attempt_id is not None
        ):
            active_attempt = (
                None
                if job.active_attempt_id is None
                else state.attempts.get(job.active_attempt_id)
            )
            if active_attempt is None or active_attempt.job_id != job.job_id:
                raise StorageIntegrityError("active_job_attempt_mismatch")
        elif (
            job.state is not JobState.DISPATCHING and job.active_attempt_id is not None
        ):
            raise StorageIntegrityError("inactive_job_has_active_attempt")
    for attempt_id, correlation in state.correlations.items():
        if (
            attempt_id != correlation.attempt_id
            or attempt_id not in state.attempts
            or correlation.robot_run_id not in state.robot_runs
        ):
            raise StorageIntegrityError("correlation_ledger_reference_mismatch")
    for job_id, admissions in state.room_registry.admissions.items():
        admitted_job = state.jobs.get(job_id)
        if (
            admitted_job is None
            or admitted_job.state
            not in {
                JobState.DISPATCHING,
                JobState.RUNNING,
                JobState.CANCELING,
                JobState.NEEDS_ATTENTION,
            }
            or {item.room_id for item in admissions}
            != {target.area_id for target in admitted_job.intent.areas}
        ):
            raise StorageIntegrityError("room_admission_job_mismatch")


def _encode_job(job: Job) -> JsonObject:
    return {
        "job_id": job.job_id,
        "revision": job.revision,
        "intent": _encode_intent(job.intent),
        "state": job.state.value,
        "created_at": _encode_datetime(job.created_at),
        "updated_at": _encode_datetime(job.updated_at),
        "plan_id": job.plan_id,
        "active_attempt_id": job.active_attempt_id,
        "completed_work_unit_ids": list(job.completed_work_unit_ids),
        "retries_job_id": job.retries_job_id,
        "failure_code": job.failure_code,
        "origin": None
        if job.origin is None
        else {
            "context_id": job.origin.context_id,
            "user_id": job.origin.user_id,
            "parent_id": job.origin.parent_id,
        },
    }


def _decode_job(data: JsonObject) -> Job:
    return Job(
        job_id=_str(data["job_id"]),
        revision=_int(data["revision"]),
        intent=_decode_intent(_object(data["intent"])),
        state=_enum(JobState, data["state"]),
        created_at=_decode_datetime(data["created_at"]),
        updated_at=_decode_datetime(data["updated_at"]),
        plan_id=_optional_str(data["plan_id"]),
        active_attempt_id=_optional_str(data["active_attempt_id"]),
        completed_work_unit_ids=tuple(_string_list(data["completed_work_unit_ids"])),
        retries_job_id=_optional_str(data["retries_job_id"]),
        failure_code=_optional_str(data["failure_code"]),
        origin=_decode_origin(data.get("origin")),
    )


def _decode_origin(value: object) -> CommandOrigin | None:
    if value is None:
        return None
    data = _object(value)
    return CommandOrigin(
        _str(data["context_id"]),
        _optional_str(data["user_id"]),
        _optional_str(data["parent_id"]),
    )


def _encode_intent(intent: JobIntent) -> JsonObject:
    extension = intent.vendor_extension
    return {
        "areas": [
            {"area_id": item.area_id, "map_context": item.map_context}
            for item in intent.areas
        ],
        "mode": intent.mode.value,
        "name": intent.name,
        "preferences": {
            "vacuum_power": _enum_value(intent.preferences.vacuum_power),
            "mop_intensity": _enum_value(intent.preferences.mop_intensity),
            "mop_route": _enum_value(intent.preferences.mop_route),
        },
        "passes": intent.passes,
        "source": intent.source,
        "reason": intent.reason,
        "note": intent.note,
        "dedupe_key": intent.dedupe_key,
        "required_on": list(intent.required_on),
        "required_off": list(intent.required_off),
        "settings_policy": intent.settings_policy.value,
        "vendor_extension": None
        if extension is None
        else {
            "namespace": extension.namespace,
            "parameters": [list(item) for item in extension.parameters],
        },
    }


def _decode_intent(data: JsonObject) -> JobIntent:
    preferences = _object(data["preferences"])
    raw_extension = data["vendor_extension"]
    extension = None
    if raw_extension is not None:
        extension_data = _object(raw_extension)
        extension = VendorExtension(
            _str(extension_data["namespace"]),
            tuple(
                (_str(item[0]), cast(Any, item[1]))
                for item in _pair_list(extension_data["parameters"])
            ),
        )
    return JobIntent(
        areas=tuple(
            TargetRef(_str(item["area_id"]), _optional_str(item["map_context"]))
            for item in _object_list(data["areas"])
        ),
        mode=_enum(CleaningMode, data["mode"]),
        name=_optional_str(data["name"]),
        preferences=CleaningPreferences(
            _optional_enum(SemanticLevel, preferences["vacuum_power"]),
            _optional_enum(SemanticLevel, preferences["mop_intensity"]),
            _optional_enum(MopRoute, preferences["mop_route"]),
        ),
        passes=_int(data["passes"]),
        source=_optional_str(data["source"]),
        reason=_optional_str(data["reason"]),
        note=_optional_str(data["note"]),
        dedupe_key=_optional_str(data["dedupe_key"]),
        required_on=tuple(_string_list(data["required_on"])),
        required_off=tuple(_string_list(data["required_off"])),
        settings_policy=_enum(SettingsPolicy, data["settings_policy"]),
        vendor_extension=extension,
    )


def _encode_plan(plan: ExecutionPlan) -> JsonObject:
    return {
        "plan_id": plan.plan_id,
        "job_id": plan.job_id,
        "work_units": [_encode_work_unit(unit) for unit in plan.work_units],
    }


def _decode_plan(data: JsonObject) -> ExecutionPlan:
    return ExecutionPlan(
        _str(data["plan_id"]),
        _str(data["job_id"]),
        tuple(_decode_work_unit(item) for item in _object_list(data["work_units"])),
    )


def _encode_work_unit(unit: WorkUnit) -> JsonObject:
    extension = unit.vendor_extension
    return {
        "work_unit_id": unit.work_unit_id,
        "operation": unit.operation.value,
        "canonical_targets": list(unit.canonical_targets),
        "map_context": unit.map_context,
        "passes": unit.passes,
        "pass_scope": unit.pass_scope.value,
        "preferences": {
            "vacuum_power": _enum_value(unit.preferences.vacuum_power),
            "mop_intensity": _enum_value(unit.preferences.mop_intensity),
            "mop_route": _enum_value(unit.preferences.mop_route),
        },
        "settings_policy": unit.settings_policy.value,
        "vendor_extension": None
        if extension is None
        else {
            "namespace": extension.namespace,
            "parameters": [list(item) for item in extension.parameters],
        },
        "depends_on": list(unit.depends_on),
    }


def _decode_work_unit(data: JsonObject) -> WorkUnit:
    preferences = _object(data["preferences"])
    raw_extension = data["vendor_extension"]
    extension = None
    if raw_extension is not None:
        extension_data = _object(raw_extension)
        extension = VendorExtension(
            _str(extension_data["namespace"]),
            tuple(
                (_str(item[0]), cast(Any, item[1]))
                for item in _pair_list(extension_data["parameters"])
            ),
        )
    return WorkUnit(
        work_unit_id=_str(data["work_unit_id"]),
        operation=_enum(OperationKind, data["operation"]),
        canonical_targets=tuple(_string_list(data["canonical_targets"])),
        map_context=_optional_str(data["map_context"]),
        passes=_int(data["passes"]),
        pass_scope=_enum(PassScope, data["pass_scope"]),
        preferences=CleaningPreferences(
            _optional_enum(SemanticLevel, preferences["vacuum_power"]),
            _optional_enum(SemanticLevel, preferences["mop_intensity"]),
            _optional_enum(MopRoute, preferences["mop_route"]),
        ),
        settings_policy=_enum(SettingsPolicy, data["settings_policy"]),
        vendor_extension=extension,
        depends_on=tuple(_string_list(data["depends_on"])),
    )


def _encode_assignment(assignment: DispatchAssignment) -> JsonObject:
    return {
        "room_targets": {
            key: list(value) for key, value in assignment.room_targets.items()
        },
        "work_unit_id": assignment.work_unit_id,
        "robot_id": assignment.robot_id,
        "source_robot_id": assignment.source_robot_id,
        "adapter": assignment.adapter,
        "adapter_targets": list(assignment.adapter_targets),
        "capability_revision": assignment.capability_revision,
        "preference_resolution": {
            "applied": list(assignment.preference_resolution.applied),
            "omitted": list(assignment.preference_resolution.omitted),
        },
    }


def _decode_assignment(data: JsonObject) -> DispatchAssignment:
    resolution = _object(data["preference_resolution"])
    return DispatchAssignment(
        _str(data["work_unit_id"]),
        _str(data["robot_id"]),
        _str(data["source_robot_id"]),
        _str(data["adapter"]),
        tuple(_string_list(data["adapter_targets"])),
        _str(data["capability_revision"]),
        PreferenceResolution(
            tuple(_string_list(resolution["applied"])),
            tuple(_string_list(resolution["omitted"])),
        ),
        {
            key: tuple(_string_list(value))
            for key, value in _string_mapping(data.get("room_targets", {})).items()
        },
    )


def _encode_attempt(attempt: ExecutionAttempt) -> JsonObject:
    return {
        "attempt_id": attempt.attempt_id,
        "job_id": attempt.job_id,
        "work_unit_id": attempt.work_unit_id,
        "robot_id": attempt.robot_id,
        "source_robot_id": attempt.source_robot_id,
        "robot_generation": attempt.robot_generation,
        "state": attempt.state.value,
        "prepared_at": _encode_datetime(attempt.prepared_at),
        "command_boundary_at": _encode_optional_datetime(attempt.command_boundary_at),
        "observed_start_at": _encode_optional_datetime(attempt.observed_start_at),
        "prior_history_start": _encode_optional_datetime(attempt.prior_history_start),
        "prior_history_end": _encode_optional_datetime(attempt.prior_history_end),
        "failure_code": attempt.failure_code,
        "policy": {
            "start_seconds": attempt.policy.start_seconds,
            "run_seconds": attempt.policy.run_seconds,
            "cancel_seconds": attempt.policy.cancel_seconds,
            "settle_seconds": attempt.policy.settle_seconds,
        },
        "terminal_observed_at": _encode_optional_datetime(attempt.terminal_observed_at),
        "last_observation_at": _encode_optional_datetime(attempt.last_observation_at),
        "completion_quality": _enum_value(attempt.completion_quality),
        "cancel_requested_at": _encode_optional_datetime(attempt.cancel_requested_at),
    }


def _decode_attempt(data: JsonObject) -> ExecutionAttempt:
    policy = _object(data.get("policy", {}))
    return ExecutionAttempt(
        _str(data["attempt_id"]),
        _str(data["job_id"]),
        _str(data["work_unit_id"]),
        _str(data["robot_id"]),
        _str(data["source_robot_id"]),
        _int(data["robot_generation"]),
        _enum(AttemptState, data["state"]),
        _decode_datetime(data["prepared_at"]),
        _decode_optional_datetime(data["command_boundary_at"]),
        _decode_optional_datetime(data["observed_start_at"]),
        _decode_optional_datetime(data["prior_history_start"]),
        _decode_optional_datetime(data["prior_history_end"]),
        _optional_str(data["failure_code"]),
        ExecutionPolicy(
            _number(policy.get("start_seconds", 180)),
            _number(policy.get("run_seconds", 14400)),
            _number(policy.get("cancel_seconds", 120)),
            _number(policy.get("settle_seconds", 30)),
        ),
        _decode_optional_datetime(data.get("terminal_observed_at")),
        _decode_optional_datetime(data.get("last_observation_at")),
        _optional_enum(CompletionQuality, data.get("completion_quality")),
        _decode_optional_datetime(data.get("cancel_requested_at")),
    )


def _encode_robot_run(run: RobotRun) -> JsonObject:
    return {
        "robot_run_id": run.robot_run_id,
        "source_robot_id": run.source_robot_id,
        "observed_start": _encode_optional_datetime(run.observed_start),
        "observed_end": _encode_optional_datetime(run.observed_end),
        "history_start": _encode_optional_datetime(run.history_start),
        "history_end": _encode_optional_datetime(run.history_end),
        "cleaning_activity_seen": run.cleaning_activity_seen,
        "causal_token": run.causal_token,
        "completion_quality": _enum_value(run.completion_quality),
        "operation": _enum_value(run.operation),
        "canonical_targets": list(run.canonical_targets),
        "source": run.source.value,
        "failure_code": run.failure_code,
        "capability_revision": run.capability_revision,
    }


def _decode_robot_run(data: JsonObject) -> RobotRun:
    return RobotRun(
        _str(data["robot_run_id"]),
        _str(data["source_robot_id"]),
        _decode_optional_datetime(data["observed_start"]),
        _decode_optional_datetime(data["observed_end"]),
        _decode_optional_datetime(data["history_start"]),
        _decode_optional_datetime(data["history_end"]),
        _bool(data["cleaning_activity_seen"]),
        _optional_str(data["causal_token"]),
        _optional_enum(CompletionQuality, data.get("completion_quality")),
        _optional_enum(OperationKind, data.get("operation")),
        tuple(_string_list(data.get("canonical_targets", []))),
        _enum(CleaningSource, data.get("source", "voi")),
        _optional_str(data.get("failure_code")),
        _optional_str(data.get("capability_revision")),
    )


def _encode_correlation(value: RunCorrelation) -> JsonObject:
    return {
        "attempt_id": value.attempt_id,
        "robot_run_id": value.robot_run_id,
        "state": value.state.value,
        "confidence": value.confidence.value,
        "reason_codes": list(value.reason_codes),
        "causal_token_available": value.causal_token_available,
    }


def _decode_correlation(data: JsonObject) -> RunCorrelation:
    return RunCorrelation(
        _str(data["attempt_id"]),
        _str(data["robot_run_id"]),
        _enum(CorrelationState, data["state"]),
        _enum(CorrelationConfidence, data["confidence"]),
        tuple(_string_list(data["reason_codes"])),
        _bool(data["causal_token_available"]),
    )


def _encode_lease(value: RobotLease) -> JsonObject:
    return {
        "source_robot_id": value.source_robot_id,
        "robot_id": value.robot_id,
        "attempt_id": value.attempt_id,
        "work_unit_id": value.work_unit_id,
        "generation": value.generation,
    }


def _decode_lease(data: JsonObject) -> RobotLease:
    return RobotLease(
        _str(data["source_robot_id"]),
        _str(data["robot_id"]),
        _str(data["attempt_id"]),
        _str(data["work_unit_id"]),
        _int(data["generation"]),
    )
