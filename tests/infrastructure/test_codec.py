"""Round-trip, migration and relational corruption tests for the storage schema."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from custom_components.vacuum_orchestrator.domain.errors import StorageIntegrityError
from custom_components.vacuum_orchestrator.domain.execution import (
    ExecutionAttempt,
    RobotLease,
)
from custom_components.vacuum_orchestrator.domain.intents import (
    CleaningPreferences,
    JobIntent,
    TargetRef,
    VendorExtension,
)
from custom_components.vacuum_orchestrator.domain.job_defaults import JobDefaults
from custom_components.vacuum_orchestrator.domain.planning import (
    DispatchAssignment,
    Planner,
    ResolvedSetting,
    SettingsResolution,
)
from custom_components.vacuum_orchestrator.domain.queue import OrchestratorState
from custom_components.vacuum_orchestrator.domain.rooms import Room
from custom_components.vacuum_orchestrator.domain.templates import JobTemplate
from custom_components.vacuum_orchestrator.domain.types import (
    AttemptState,
    CleaningMode,
    JobState,
    MopRoute,
    OperationKind,
    PassScope,
    QueueMode,
    SettingsPolicy,
    VacuumLevel,
    WaterLevel,
)
from custom_components.vacuum_orchestrator.infrastructure.codec import (
    decode_orchestrator_state,
    encode_orchestrator_state,
    migrate_schema_one,
    migrate_schema_three,
)
from custom_components.vacuum_orchestrator.infrastructure.integrity import JsonObject

NOW = datetime(2026, 9, 7, 12, tzinfo=UTC)


def legacy_payload(operation_kinds: tuple[str, ...] = ("vacuum",)) -> JsonObject:
    """Return the smallest complete version-1 queued-job snapshot."""
    operations = [
        {
            "kind": kind,
            "passes": {"count": 2, "scope": "target_set"},
            "vacuum_level": None,
            "water_level": None,
            "mop_route": None,
            "vendor_extension": None,
        }
        for kind in operation_kinds
    ]
    return {
        "schema_version": 1,
        "fleet_id": "legacy-fleet",
        "commit_id": 4,
        "queue_revision": 2,
        "mode": "idle",
        "queue": ["job"],
        "jobs": {
            "job": {
                "job_id": "job",
                "revision": 3,
                "intent": {
                    "targets": [{"area_id": "kitchen", "map_context": "ground-floor"}],
                    "operations": operations,
                    "requirements": [
                        {"reference": "binary_sensor.door", "expected_state": "on"},
                        {
                            "reference": "binary_sensor.person",
                            "expected_state": "off",
                        },
                    ],
                    "idempotency_key": "automatic|kitchen",
                },
                "state": "queued",
                "created_at": NOW.isoformat(),
                "updated_at": NOW.isoformat(),
                "active_attempt_id": None,
                "retries_job_id": None,
                "failure_code": None,
            }
        },
        "robot_generations": {},
        "plans": {},
        "attempts": {},
        "robot_runs": {},
        "correlations": {},
        "robot_leases": {},
        "needs_attention": False,
    }


def _legacy_active_payload(attempt_state: str = "command_sent") -> JsonObject:
    data = legacy_payload(("combined",))
    operation = data["jobs"]["job"]["intent"]["operations"][0]
    operation.update(
        vacuum_level="high",
        water_level="medium",
        mop_route="deep",
        vendor_extension={
            "namespace": "roborock.v1",
            "parameters": [["mode", "custom"]],
        },
    )
    data["jobs"]["job"].update(state="executing", active_attempt_id="attempt")
    data["plans"] = {
        "plan": {
            "plan_id": "plan",
            "job_id": "job",
            "work_units": [
                {
                    "work_unit_id": "unit",
                    "operation": operation,
                    "canonical_targets": ["kitchen"],
                    "map_context": "ground-floor",
                    "depends_on": [],
                    "robot_id": "robot",
                    "source_robot_id": "source",
                    "adapter": "roborock",
                    "adapter_targets": ["16"],
                    "capability_revision": "caps-1",
                }
            ],
        }
    }
    data["attempts"] = {
        "attempt": {
            "attempt_id": "attempt",
            "job_id": "job",
            "work_unit_id": "unit",
            "robot_id": "robot",
            "source_robot_id": "source",
            "robot_generation": 1,
            "state": attempt_state,
            "prepared_at": NOW.isoformat(),
            "command_boundary_at": NOW.isoformat(),
            "prior_history_start": None,
            "prior_history_end": None,
            "failure_code": None,
        }
    }
    data["robot_generations"] = {"source": 1}
    data["robot_leases"] = {
        "source": {
            "source_robot_id": "source",
            "robot_id": "robot",
            "attempt_id": "attempt",
            "work_unit_id": "unit",
            "generation": 1,
        }
    }
    data["needs_attention"] = True
    return data


def _state() -> OrchestratorState:
    intent = JobIntent(
        (TargetRef("kitchen", "main"),),
        CleaningMode.VACUUM_AND_MOP,
        name="Kitchen",
        preferences=CleaningPreferences(
            VacuumLevel.HIGH, WaterLevel.MEDIUM, MopRoute.DEEP
        ),
        passes=2,
        source="manual",
        reason="dirty",
        note="note",
        dedupe_key="manual|kitchen",
        required_on=("binary_sensor.door",),
        required_off=("binary_sensor.person",),
        settings_policy=SettingsPolicy.STRICT,
        vendor_extension=VendorExtension("roborock.v1", (("mode", "custom"),)),
    )
    state = OrchestratorState.empty("installation").add_job("job", intent, NOW)
    plan = Planner().create_plan("job", intent)
    unit = plan.work_units[0]
    assignment = DispatchAssignment(
        unit.work_unit_id,
        "robot",
        "source",
        "roborock",
        ("16",),
        "caps",
        SettingsResolution(
            (
                ResolvedSetting("vacuum_power", "high", "high"),
                ResolvedSetting("mop_intensity", "medium", "low"),
                ResolvedSetting("mop_route", "deep", None),
            )
        ),
    )
    attempt = ExecutionAttempt(
        "attempt",
        "job",
        unit.work_unit_id,
        "robot",
        "source",
        1,
        AttemptState.PREPARED,
        NOW,
        prior_history_start=NOW,
        prior_history_end=NOW,
    )
    lease = RobotLease("source", "robot", "attempt", unit.work_unit_id, 1)
    return state.prepare_dispatch("job", plan, unit, assignment, attempt, lease, NOW)


def test_schema_two_round_trip_preserves_complete_ledger() -> None:
    state = _state().mark_command_sent("attempt", NOW)

    decoded = decode_orchestrator_state(encode_orchestrator_state(state))

    assert decoded == state
    assert decoded.jobs["job"].intent.mode is CleaningMode.VACUUM_AND_MOP
    assert decoded.assignments["attempt"].adapter_targets == ("16",)
    plan = decoded.plans[decoded.jobs["job"].plan_id or ""]
    assert plan.work_units[0].pass_scope is PassScope.TARGET_SET


def test_stop_boundary_round_trips_and_is_optional_in_older_snapshots() -> None:
    sent = _state().mark_command_sent("attempt", NOW)
    canceling, _ = sent.request_cancel("job", NOW)
    stopped = canceling.mark_stop_sent("attempt", NOW)
    data = encode_orchestrator_state(stopped)

    assert decode_orchestrator_state(data) == stopped
    del data["attempts"]["attempt"]["stop_sent_at"]
    assert decode_orchestrator_state(data).attempts["attempt"].stop_sent_at is None


def _schema_three(state: OrchestratorState) -> dict:
    data = encode_orchestrator_state(state)
    data["schema_version"] = 3
    del data["job_defaults"]
    for assignment in data["assignments"].values():
        settings = assignment.pop("settings")
        assignment["preference_resolution"] = {
            "applied": [item["name"] for item in settings if item["applied"]],
            "omitted": [item["name"] for item in settings if not item["applied"]],
        }
    return data


def test_schema_three_vocabulary_is_mapped_and_unstarted_jobs_get_defaults() -> None:
    queued = JobIntent((TargetRef("hall"),), CleaningMode.VACUUM_AND_MOP)
    state = _state().add_job("queued", queued, NOW)
    state = replace(
        state,
        room_registry=state.room_registry.put_room(Room("hall", "Hall")),
        templates={
            "t": JobTemplate(
                "t", "Routine", JobIntent((TargetRef("hall"),), CleaningMode.MOP), NOW
            )
        },
    )
    data = _schema_three(state)
    old = {"vacuum_power": "medium", "mop_intensity": "maximum", "mop_route": "auto"}
    data["jobs"]["job"]["intent"]["preferences"] = dict(old)
    next(iter(data["plans"].values()))["work_units"][0]["preferences"] = dict(old)
    data["jobs"]["queued"]["intent"]["preferences"] = {
        "vacuum_power": "auto",
        "mop_intensity": "standard",
        "mop_route": None,
    }
    data["templates"]["t"]["intent"]["preferences"] = {
        "vacuum_power": None,
        "mop_intensity": "off",
        "mop_route": "fast",
    }

    migrated = migrate_schema_three(deepcopy(data))

    assert migrated.job_defaults == JobDefaults()
    planned = migrated.jobs["job"].intent.preferences
    assert planned == CleaningPreferences(VacuumLevel.STANDARD, WaterLevel.HIGH, None)
    unit = migrated.plans[migrated.jobs["job"].plan_id or ""].work_units[0]
    assert unit.preferences == planned
    assert migrated.assignments["attempt"].settings == SettingsResolution(
        (
            ResolvedSetting("vacuum_power", "standard", "standard"),
            ResolvedSetting("mop_intensity", "high", "high"),
        )
    )
    assert migrated.jobs["queued"].intent.preferences == CleaningPreferences(
        VacuumLevel.STANDARD, WaterLevel.MEDIUM, MopRoute.STANDARD
    )
    assert migrated.templates["t"].intent.preferences == CleaningPreferences(
        None, WaterLevel.MEDIUM, MopRoute.FAST
    )
    assert decode_orchestrator_state(encode_orchestrator_state(migrated)) == migrated
    with pytest.raises(StorageIntegrityError, match="unsupported_previous"):
        migrate_schema_three({**data, "schema_version": 4})
    del data["jobs"]["job"]["intent"]
    with pytest.raises(StorageIntegrityError, match="invalid_storage_payload"):
        migrate_schema_three(data)


def test_cancel_return_choice_and_window_round_trip() -> None:
    sent = _state().mark_command_sent("attempt", NOW)
    attempt = sent.attempts["attempt"]
    sent = replace(
        sent,
        attempts={
            "attempt": replace(
                attempt, policy=replace(attempt.policy, return_seconds=600)
            )
        },
    )
    canceling, _ = sent.request_cancel("job", NOW, return_to_dock=True)
    data = encode_orchestrator_state(canceling)
    decoded = decode_orchestrator_state(data)
    assert decoded == canceling
    assert decoded.attempts["attempt"].return_to_dock
    assert decoded.attempts["attempt"].policy.return_seconds == 600
    del data["attempts"]["attempt"]["return_to_dock"]
    del data["attempts"]["attempt"]["policy"]["return_seconds"]
    older = decode_orchestrator_state(data).attempts["attempt"]
    assert not older.return_to_dock and older.policy.return_seconds == 900


def test_job_defaults_round_trip() -> None:
    defaults = JobDefaults(
        CleaningMode.VACUUM_THEN_MOP,
        VacuumLevel.MAXIMUM_PLUS,
        WaterLevel.LOW,
        MopRoute.DEEP_PLUS,
        3,
        SettingsPolicy.STRICT,
        True,
    )
    state = replace(OrchestratorState.empty("installation"), job_defaults=defaults)
    data = encode_orchestrator_state(state)
    assert decode_orchestrator_state(data).job_defaults == defaults
    del data["job_defaults"]
    assert decode_orchestrator_state(data).job_defaults == JobDefaults()


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        (lambda data: data.update(schema_version=99), "unsupported_storage_schema"),
        (lambda data: data.update(commit_id=-1), "negative_storage_revision"),
        (lambda data: data["jobs"]["wrong"].update(job_id="job"), "job_storage_key"),
        (
            lambda data: data["attempts"]["attempt"].update(job_id="missing"),
            "attempt_ledger",
        ),
        (
            lambda data: data["robot_leases"]["source"].update(generation=9),
            "robot_lease",
        ),
        (
            lambda data: next(iter(data["plans"].values()))["work_units"][0].update(
                pass_scope="per_target"
            ),
            "job_plan_semantics",
        ),
    ],
)
def test_relational_corruption_is_rejected(mutation: object, error: str) -> None:
    data = encode_orchestrator_state(_state())
    if error == "job_storage_key":
        data["jobs"]["wrong"] = data["jobs"].pop("job")
    else:
        mutation(data)  # type: ignore[operator]
    with pytest.raises(StorageIntegrityError, match=error):
        decode_orchestrator_state(deepcopy(data))


def test_invalid_shapes_and_naive_datetime_are_rejected() -> None:
    data = encode_orchestrator_state(_state())
    data["queue"] = "job"
    with pytest.raises(StorageIntegrityError, match="expected_string_list"):
        decode_orchestrator_state(data)

    state = _state()
    bad = encode_orchestrator_state(state)
    bad["jobs"]["job"]["created_at"] = "2026-09-07T12:00:00"
    with pytest.raises(StorageIntegrityError, match="naive_datetime"):
        decode_orchestrator_state(bad)


@pytest.mark.parametrize(
    ("legacy_kinds", "mode"),
    [
        (("vacuum",), CleaningMode.VACUUM),
        (("mop",), CleaningMode.MOP),
        (("combined",), CleaningMode.VACUUM_AND_MOP),
        (("vacuum", "mop"), CleaningMode.VACUUM_THEN_MOP),
    ],
)
def test_schema_one_modes_are_migrated_without_robot_ownership(
    legacy_kinds: tuple[str, ...], mode: CleaningMode
) -> None:
    state = migrate_schema_one(legacy_payload(legacy_kinds), "installation")

    assert state.installation_id == "installation"
    assert state.commit_id == 4
    assert state.queue == ("job",)
    assert state.jobs["job"].intent.mode is mode
    assert state.jobs["job"].intent.passes == 2
    if state.plans:
        assert next(iter(state.plans.values())).work_units[0].pass_scope is (
            PassScope.TARGET_SET
        )
    assert state.jobs["job"].intent.required_on == ("binary_sensor.door",)
    assert state.jobs["job"].intent.required_off == ("binary_sensor.person",)


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        (
            lambda data: data.update(schema_version=0),
            "unsupported_legacy_storage_schema",
        ),
        (
            lambda data: data["jobs"]["job"]["intent"].update(
                operations=[
                    {
                        "kind": "mop",
                        "passes": {"count": 1},
                        "vacuum_level": None,
                        "water_level": None,
                        "mop_route": None,
                    },
                    {
                        "kind": "vacuum",
                        "passes": {"count": 1},
                        "vacuum_level": None,
                        "water_level": None,
                        "mop_route": None,
                    },
                ]
            ),
            "ambiguous_legacy_operation_sequence",
        ),
        (
            lambda data: data["jobs"]["job"]["intent"]["requirements"][0].update(
                expected_state="open"
            ),
            "unsupported_legacy_requirement_state",
        ),
    ],
)
def test_schema_one_migration_fails_closed(mutation: object, error: str) -> None:
    data = legacy_payload()
    mutation(data)  # type: ignore[operator]

    with pytest.raises(StorageIntegrityError, match=error):
        migrate_schema_one(data, "installation")


@pytest.mark.parametrize(
    ("attempt_state", "unit_state"),
    [
        ("command_sent", "active"),
        ("succeeded", "completed"),
        ("failed", "failed"),
        ("cancelled", "cancelled"),
        ("recovery_required", "needs_attention"),
    ],
)
def test_active_schema_one_ledgers_are_imported_and_robot_isolated(
    attempt_state: str, unit_state: str
) -> None:
    state = migrate_schema_one(_legacy_active_payload(attempt_state), "installation")

    assignment = state.assignments["attempt"]
    unit = state.plans["plan"].work_units[0]
    assert state.jobs["job"].state is JobState.NEEDS_ATTENTION
    assert state.jobs["job"].plan_id == "plan"
    assert state.work_unit_states["unit"].value == unit_state
    assert state.blocked_robots == {"source": "legacy_recovery_required"}
    assert assignment.adapter_targets == ("16",)
    assert unit.operation is OperationKind.VACUUM_AND_MOP
    assert unit.preferences.vacuum_power is VacuumLevel.HIGH
    assert unit.preferences.mop_intensity is WaterLevel.MEDIUM
    assert unit.preferences.mop_route is MopRoute.DEEP
    assert unit.vendor_extension is not None
    assert unit.vendor_extension.namespace == "roborock.v1"


@pytest.mark.parametrize(
    ("legacy_state", "expected"),
    [
        ("succeeded", JobState.COMPLETED),
        ("failed", JobState.FAILED),
        ("cancelled", JobState.CANCELLED),
    ],
)
def test_terminal_legacy_job_states_are_preserved(
    legacy_state: str, expected: JobState
) -> None:
    data = legacy_payload()
    data["jobs"]["job"]["state"] = legacy_state

    state = migrate_schema_one(data, "installation")

    assert state.jobs["job"].state is expected
    assert state.queue == ()


@pytest.mark.parametrize(
    ("legacy_mode", "expected"),
    [
        ("running", QueueMode.RUNNING),
        ("paused", QueueMode.PAUSED),
        ("needs_attention", QueueMode.PAUSED),
        ("idle", QueueMode.IDLE),
    ],
)
def test_legacy_queue_modes_have_deterministic_mapping(
    legacy_mode: str, expected: QueueMode
) -> None:
    data = legacy_payload()
    data["mode"] = legacy_mode
    assert migrate_schema_one(data, "installation").mode is expected


def test_unscoped_legacy_attention_is_preserved_fail_closed() -> None:
    data = legacy_payload()
    data["needs_attention"] = True

    state = migrate_schema_one(data, "installation")

    assert state.blocked_robots == {"legacy:unscoped": "legacy_recovery_required"}


@pytest.mark.parametrize(
    ("change", "error"),
    [
        (lambda data: data.pop("jobs"), "invalid_legacy_storage_payload"),
        (
            lambda data: data["jobs"]["job"].update(state="draft"),
            "unsupported_legacy_job_state",
        ),
        (
            lambda data: data.update(mode="broken"),
            "unsupported_legacy_queue_mode",
        ),
        (
            lambda data: data["jobs"]["job"]["intent"]["operations"].append(
                {
                    "kind": "mop",
                    "passes": {"count": 1},
                    "vacuum_level": None,
                    "water_level": None,
                    "mop_route": None,
                }
            ),
            "ambiguous_legacy_pass_count",
        ),
        (
            lambda data: data["jobs"]["job"]["intent"]["operations"][0][
                "passes"
            ].update(scope="per_target"),
            "unsupported_legacy_pass_scope",
        ),
        (
            lambda data: data["jobs"]["job"]["intent"]["operations"][1].update(
                vendor_extension={
                    "namespace": "roborock.v1",
                    "parameters": [],
                }
            ),
            "ambiguous_legacy_vendor_extension",
        ),
        (
            lambda data: data["jobs"]["job"]["intent"]["operations"][1].update(
                vacuum_level="maximum"
            ),
            "ambiguous_legacy_vacuum_level",
        ),
    ],
)
def test_additional_legacy_ambiguity_is_rejected(change: object, error: str) -> None:
    data = (
        legacy_payload(("vacuum", "mop"))
        if "vacuum_level" in error or "vendor_extension" in error
        else legacy_payload()
    )
    if "vacuum_level" in error:
        data["jobs"]["job"]["intent"]["operations"][0]["vacuum_level"] = "high"
    change(data)  # type: ignore[operator]

    with pytest.raises(StorageIntegrityError, match=error):
        migrate_schema_one(data, "installation")
