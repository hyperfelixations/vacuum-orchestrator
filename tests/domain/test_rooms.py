"""Room grants, physical addressing, evidence and aging contracts."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.vacuum_orchestrator.domain.completion import (
    CleaningReceipt,
    CleaningSource,
    CompletionQuality,
)
from custom_components.vacuum_orchestrator.domain.due import (
    DueBasis,
    DuePolicy,
    DueState,
    OccupancyCounter,
)
from custom_components.vacuum_orchestrator.domain.errors import (
    ConflictError,
    ValidationError,
)
from custom_components.vacuum_orchestrator.domain.releases import (
    ReleaseKind,
    RoomRelease,
)
from custom_components.vacuum_orchestrator.domain.requirements import (
    RequirementState,
    StateObservation,
    StateRequirement,
    evaluate_requirements,
)
from custom_components.vacuum_orchestrator.domain.room_registry import RoomRegistry
from custom_components.vacuum_orchestrator.domain.rooms import (
    Room,
    RoomBinding,
    validate_room_registry,
)
from custom_components.vacuum_orchestrator.domain.types import OperationKind

NOW = datetime(2026, 9, 1, tzinfo=UTC)


def receipt(
    receipt_id: str = "receipt",
    *,
    at: datetime = NOW,
    quality: CompletionQuality = CompletionQuality.CONFIRMED,
    operation: OperationKind = OperationKind.VACUUM,
) -> CleaningReceipt:
    return CleaningReceipt(
        receipt_id,
        CleaningSource.VOI,
        "attempt",
        ("room",),
        operation,
        at,
        quality,
        ("observed",),
    )


def test_once_grant_reservation_prevents_competing_jobs() -> None:
    grant = RoomRelease("grant", ReleaseKind.ONCE, NOW)
    reserved = grant.reserve("first", NOW)
    assert not reserved.allows_new_job(NOW)
    assert reserved.reserve("first", NOW) is reserved
    with pytest.raises(ConflictError, match="room_not_released"):
        reserved.reserve("second", NOW)
    with pytest.raises(ConflictError, match="release_reservation_mismatch"):
        reserved.consume("second")
    assert reserved.unreserve("first") == grant
    assert reserved.unreserve("second") is reserved
    consumed = reserved.consume("first")
    assert consumed.consumed
    assert consumed.consume("first") is consumed
    assert consumed.unreserve("first") is consumed
    with pytest.raises(ConflictError):
        consumed.reserve("first", NOW)


def test_permanent_and_timed_admission_boundaries() -> None:
    permanent = RoomRelease("grant", ReleaseKind.PERMANENT, NOW)
    assert permanent.reserve("job", NOW) is permanent
    assert permanent.consume("job") is permanent
    assert not permanent.allows_new_job(NOW - timedelta(seconds=1))
    assert permanent.allows_new_job(NOW + timedelta(days=100))
    timed = RoomRelease("grant", ReleaseKind.TIMED, NOW, NOW + timedelta(hours=2))
    assert timed.allows_new_job(NOW + timedelta(hours=1))
    assert not timed.allows_new_job(NOW + timedelta(hours=2))


@pytest.mark.parametrize(
    "changes",
    [
        {"grant_id": " "},
        {"granted_at": NOW.replace(tzinfo=None)},
        {"kind": ReleaseKind.TIMED},
        {"expires_at": NOW + timedelta(hours=1)},
        {"kind": ReleaseKind.TIMED, "expires_at": NOW},
        {"reserved_job_id": "job"},
        {"kind": ReleaseKind.ONCE, "consumed": True},
    ],
)
def test_invalid_grants_are_rejected(changes: dict) -> None:
    with pytest.raises(ValidationError):
        replace(RoomRelease("grant", ReleaseKind.PERMANENT, NOW), **changes)


def test_derived_completion_keeps_last_confirmed_and_partial_operations() -> None:
    room = Room("room", "Kitchen", due_policy=DuePolicy(vacuum_seconds=3600))
    room = room.apply_receipt(receipt())
    later = receipt(
        "derived", at=NOW + timedelta(minutes=30), quality=CompletionQuality.DERIVED
    )
    room = room.apply_receipt(later)
    assert room.last_cleaning[OperationKind.VACUUM].receipt_id == "derived"
    assert room.last_confirmed[OperationKind.VACUUM].receipt_id == "receipt"
    assert OperationKind.MOP not in room.last_cleaning
    due = room.due(OperationKind.VACUUM, NOW + timedelta(minutes=90))
    assert due.state is DueState.DUE
    assert due.quality is CompletionQuality.DERIVED
    assert due.due_at == NOW + timedelta(minutes=90)
    assert room.apply_receipt(receipt()).last_cleaning == room.last_cleaning
    upgraded = room.apply_receipt(replace(later, quality=CompletionQuality.CONFIRMED))
    assert upgraded.last_confirmed == upgraded.last_cleaning


def test_combined_receipt_advances_both_but_never_moves_time_backwards() -> None:
    room = Room("room", "Kitchen").apply_receipt(
        receipt(operation=OperationKind.VACUUM_AND_MOP)
    )
    assert set(room.last_cleaning) == {OperationKind.VACUUM, OperationKind.MOP}
    assert room.apply_receipt(receipt("old", at=NOW - timedelta(days=1))) == room
    assert room.due(OperationKind.MOP, NOW).state is DueState.DISABLED
    with pytest.raises(ValidationError, match="due_requires_single_operation"):
        room.due(OperationKind.VACUUM_AND_MOP, NOW)
    with pytest.raises(ValidationError, match="receipt_room_mismatch"):
        Room("other", "Other").apply_receipt(receipt())


def test_never_cleaned_and_future_clock_are_explicit() -> None:
    room = Room("room", "Kitchen", due_policy=DuePolicy(vacuum_seconds=3600))
    assert room.due(OperationKind.VACUUM, NOW).reason == "never_cleaned"
    room = room.apply_receipt(receipt())
    assert room.due(OperationKind.VACUUM, NOW).state is DueState.FRESH
    assert (
        room.due(OperationKind.VACUUM, NOW - timedelta(seconds=1)).state
        is DueState.UNKNOWN
    )


def test_occupied_time_ignores_unoccupied_periods_without_inventing_due_date() -> None:
    counter = OccupancyCounter().advance(NOW, False)
    room = Room(
        "room",
        "Kitchen",
        occupancy=counter,
        due_policy=DuePolicy(DueBasis.OCCUPIED, 3600, 7200, "binary_sensor.presence"),
    )
    room = room.apply_receipt(receipt())
    counter = room.occupancy.advance(NOW + timedelta(days=1), True)
    room = replace(room, occupancy=counter)
    fresh = room.due(OperationKind.VACUUM, NOW + timedelta(days=1, minutes=30))
    assert fresh.state is DueState.FRESH
    assert fresh.elapsed_seconds == 1800
    assert fresh.due_at is None
    assert (
        room.due(OperationKind.VACUUM, NOW + timedelta(days=1, hours=1)).state
        is DueState.DUE
    )
    assert room.occupancy == counter


def test_restart_gap_is_unknown_unless_measured_time_already_exceeds_limit() -> None:
    room = Room(
        "room",
        "Kitchen",
        occupancy=OccupancyCounter().advance(NOW, True),
        due_policy=DuePolicy(DueBasis.OCCUPIED, 3600, None, "binary_sensor.presence"),
    )
    room = room.apply_receipt(receipt())
    counter = room.occupancy.advance(NOW + timedelta(hours=3), True, gap=True)
    room = replace(room, occupancy=counter)
    report = room.due(OperationKind.VACUUM, NOW + timedelta(hours=3))
    assert report.state is DueState.UNKNOWN
    assert report.elapsed_seconds == 0
    assert (
        room.due(OperationKind.VACUUM, NOW + timedelta(hours=4)).state is DueState.DUE
    )
    changed_epoch = replace(room, occupancy=replace(counter, epoch=1))
    assert (
        changed_epoch.due(OperationKind.VACUUM, NOW + timedelta(hours=4)).reason
        == "occupancy_baseline_unknown"
    )


def test_late_receipt_does_not_invent_an_occupancy_baseline() -> None:
    room = Room(
        "room",
        "Kitchen",
        occupancy=OccupancyCounter().advance(NOW + timedelta(hours=2), True),
        due_policy=DuePolicy(DueBasis.OCCUPIED, 3600, None, "binary_sensor.presence"),
    )
    room = room.apply_receipt(receipt())
    assert (
        room.due(OperationKind.VACUUM, NOW + timedelta(hours=3)).state
        is DueState.UNKNOWN
    )
    with pytest.raises(ValidationError, match="non_monotonic_observation"):
        room.occupancy.advance(NOW, False)


@pytest.mark.parametrize(
    "changes",
    [
        {"source": CleaningSource.EXTERNAL, "quality": CompletionQuality.DERIVED},
        {"room_ids": ()},
        {"room_ids": ("room", "room")},
        {"evidence": ()},
    ],
)
def test_receipt_requires_supported_scope_and_evidence(changes: dict) -> None:
    with pytest.raises(ValidationError):
        replace(receipt(), **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"basis": DueBasis.OCCUPIED},
        {"vacuum_seconds": -1},
        {"vacuum_seconds": float("inf")},
        {"vacuum_seconds": 0},
        {"mop_seconds": True},
        {"occupied_state": "unknown"},
        {"occupied_state": "off"},
    ],
)
def test_due_policy_rejects_ambiguous_sources_and_invalid_intervals(
    changes: dict,
) -> None:
    with pytest.raises(ValidationError):
        replace(DuePolicy(), **changes)


def test_room_mapping_detects_aliases_and_preserves_map_scope() -> None:
    first = Room(
        "one",
        "One",
        area_id="area",
        bindings=(RoomBinding("robot", ("1", "2"), "floor"),),
    )
    second = Room("two", "Two", bindings=(RoomBinding("robot", ("1",), "other"),))
    validate_room_registry({"one": first, "two": second})
    for invalid in (
        {"wrong": first},
        {"one": first, "two": replace(second, area_id="area")},
        {
            "one": first,
            "two": replace(second, bindings=(RoomBinding("robot", ("2",), "floor"),)),
        },
    ):
        with pytest.raises(ValidationError):
            validate_room_registry(invalid)
    with pytest.raises(ValidationError, match="duplicate_room_binding"):
        replace(first, bindings=(*first.bindings, *first.bindings))
    with pytest.raises(ValidationError, match="invalid_room_targets"):
        RoomBinding("robot", ())


def test_readiness_returns_all_applicable_reasons_and_ignores_unconfigured_age() -> (
    None
):
    requirements = (
        StateRequirement("binary_sensor.door"),
        StateRequirement("sensor.water", ("full",), 60),
        StateRequirement("sensor.missing"),
        StateRequirement("sensor.other", robot_id="other"),
        StateRequirement("sensor.mop", operation=OperationKind.MOP),
    )
    observations = {
        "binary_sensor.door": StateObservation("off", NOW - timedelta(days=10)),
        "sensor.water": StateObservation("full", NOW - timedelta(seconds=61)),
    }
    results = evaluate_requirements(
        requirements,
        observations,
        NOW,
        robot_id="robot",
        operation=OperationKind.VACUUM,
    )
    assert [item.state for item in results] == [
        RequirementState.BLOCKED,
        RequirementState.STALE,
        RequirementState.UNKNOWN,
    ]
    observations["binary_sensor.door"] = StateObservation("on")
    assert (
        evaluate_requirements(requirements[:1], observations, NOW)[0].state
        is RequirementState.READY
    )


@pytest.mark.parametrize("states", [(), ("on", "on"), ("unavailable",)])
def test_readiness_cannot_accept_missing_or_duplicate_states(
    states: tuple[str, ...],
) -> None:
    with pytest.raises(ValidationError):
        StateRequirement("binary_sensor.door", states)


@pytest.mark.parametrize("kind", list(ReleaseKind))
def test_admitted_job_survives_expiry_and_consumes_only_its_grant(
    kind: ReleaseKind,
) -> None:
    release = RoomRelease(
        "first",
        kind,
        NOW,
        NOW + timedelta(hours=2) if kind is ReleaseKind.TIMED else None,
    )
    registry = RoomRegistry(
        {"room": Room("room", "Kitchen", area_id="kitchen", release=release)}
    )
    admitted = registry.admit("job", ("kitchen",), NOW)
    assert admitted.admit("job", ("room",), NOW + timedelta(days=1)) is admitted
    started = admitted.mark_started("job")
    if kind is ReleaseKind.ONCE:
        assert started.rooms["room"].release.consumed
    renewed = started.put_room(
        replace(
            started.rooms["room"], release=RoomRelease("next", ReleaseKind.ONCE, NOW)
        )
    )
    assert not renewed.mark_started("job").rooms["room"].release.consumed
    finished = renewed.finish("job")
    assert not finished.admissions
    assert finished.rooms["room"].release.allows_new_job(NOW)
    assert finished.finish("job") is finished
    assert finished.mark_started("job") is finished


def test_unstarted_reservation_can_be_released_but_uncertain_start_consumes_it() -> (
    None
):
    registry = RoomRegistry(
        {
            "room": Room(
                "room", "Kitchen", release=RoomRelease("grant", ReleaseKind.ONCE, NOW)
            )
        }
    )
    admitted = registry.admit("job", ("room",), NOW)
    assert admitted.finish("job", never_started=True) == registry
    assert admitted.finish("job").rooms["room"].release.consumed
    revoked = admitted.put_room(replace(admitted.rooms["room"], release=None))
    assert revoked.mark_started("job").finish("job").rooms["room"].release is None
    other = replace(registry.rooms["room"], room_id="other")
    expanded = admitted.put_room(other)
    with pytest.raises(ConflictError, match="admission_scope_changed"):
        expanded.admit("job", ("other",), NOW)
    with pytest.raises(ValidationError, match="unknown_room"):
        registry.resolve("missing")
    with pytest.raises(ConflictError, match="room_not_released"):
        revoked.admit("other_job", ("room",), NOW)


def test_receipt_identity_is_idempotent_and_conflicting_replay_is_rejected() -> None:
    registry = RoomRegistry({"room": Room("room", "Kitchen")})
    fact = receipt()
    recorded = registry.record(fact)
    assert recorded.record(fact) is recorded
    with pytest.raises(ConflictError, match="receipt_identity_conflict"):
        recorded.record(replace(fact, source_id="another_run"))
    with pytest.raises(ValidationError, match="receipt_registry_mismatch"):
        replace(recorded, receipts={"wrong": fact})
    with pytest.raises(ValidationError, match="invalid_room_admission"):
        replace(recorded, admissions={"job": ()})


@pytest.mark.parametrize(
    "changes",
    [
        {"epoch": -1},
        {"unknown_seconds": -1},
        {"occupied_seconds": float("nan")},
    ],
)
def test_occupancy_counters_reject_invalid_persisted_values(changes: dict) -> None:
    with pytest.raises(ValidationError):
        replace(OccupancyCounter(), **changes)


def test_projection_validation_rejects_wrong_operation_and_false_confirmation() -> None:
    room = Room("room", "Kitchen").apply_receipt(receipt())
    stamp = room.last_cleaning[OperationKind.VACUUM]
    with pytest.raises(ValidationError, match="invalid_cleaning_projection"):
        replace(room, last_cleaning={OperationKind.VACUUM_AND_MOP: stamp})
    with pytest.raises(ValidationError, match="invalid_confirmed_projection"):
        replace(
            room,
            last_confirmed={
                OperationKind.VACUUM: replace(stamp, quality=CompletionQuality.DERIVED)
            },
        )
    with pytest.raises(ValidationError, match="invalid_occupancy_epoch"):
        replace(stamp, occupancy_epoch=-1)
    with pytest.raises(ValidationError, match="ambiguous_room_alias"):
        validate_room_registry(
            {"room": room, "other": Room("other", "Other", area_id="room")}
        )


@pytest.mark.parametrize("reported_at", [None, NOW + timedelta(seconds=1), NOW])
def test_explicit_freshness_requires_a_valid_source_timestamp(
    reported_at: datetime | None,
) -> None:
    requirement = StateRequirement("sensor.water", ("ready",), max_age_seconds=30)
    result = evaluate_requirements(
        (requirement,), {"sensor.water": StateObservation("ready", reported_at)}, NOW
    )[0]
    assert result.state is (
        RequirementState.READY if reported_at == NOW else RequirementState.STALE
    )


def test_withdrawn_receipts_fall_back_to_the_latest_remaining_ones() -> None:
    confirmed = receipt("confirmed", at=NOW)
    derived = receipt(
        "derived", at=NOW + timedelta(hours=1), quality=CompletionQuality.DERIVED
    )
    older = receipt("older", at=NOW - timedelta(hours=1))
    registry = (
        RoomRegistry({"room": Room("room", "Room")})
        .record(older)
        .record(confirmed)
        .record(derived)
    )
    room = registry.rooms["room"]
    assert room.last_cleaning[OperationKind.VACUUM].receipt_id == "derived"
    held = room.last_confirmed[OperationKind.VACUUM]
    assert held.receipt_id == "confirmed"

    # The confirmed stamp is still held and keeps its occupied-time baseline.
    without_derived = registry.withdraw(frozenset({"derived"}))
    assert without_derived.rooms["room"].last_cleaning[OperationKind.VACUUM] == held
    assert set(without_derived.receipts) == {"older", "confirmed"}

    # Older receipts have no held stamp; their baseline is unknown.
    fallback = registry.withdraw(frozenset({"derived", "confirmed"})).rooms["room"]
    stamp = fallback.last_cleaning[OperationKind.VACUUM]
    assert (stamp.receipt_id, stamp.occupancy_baseline_known) == ("older", False)
    assert fallback.last_confirmed[OperationKind.VACUUM] == stamp

    empty = registry.withdraw(frozenset({"older", "confirmed", "derived"}))
    assert empty.rooms["room"].last_cleaning == {}
    assert empty.rooms["room"].last_confirmed == {}
    assert registry.withdraw(frozenset({"unknown"})) is registry
