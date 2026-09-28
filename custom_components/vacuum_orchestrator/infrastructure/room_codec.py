"""Explicit storage contract for rooms, grants, counters and receipts."""

from math import isfinite

from ..domain.completion import CleaningReceipt, CleaningSource, CompletionQuality
from ..domain.due import CleaningStamp, DueBasis, DuePolicy, OccupancyCounter
from ..domain.errors import StorageIntegrityError
from ..domain.releases import ReleaseKind, RoomRelease
from ..domain.requirements import StateRequirement
from ..domain.room_registry import RoomAdmission, RoomRegistry
from ..domain.rooms import Room, RoomBinding
from ..domain.types import OperationKind
from . import codec_values as cv
from .integrity import JsonObject


def _number(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(value)
    ):
        raise StorageIntegrityError("expected_finite_number")
    return float(value)


def _optional_number(value: object) -> float | None:
    return None if value is None else _number(value)


def encode_requirement(value: StateRequirement) -> JsonObject:
    """Serialize a scoped entity requirement."""
    return {
        "entity_id": value.entity_id,
        "accepted_states": list(value.accepted_states),
        "max_age_seconds": value.max_age_seconds,
        "robot_id": value.robot_id,
        "operation": cv._enum_value(value.operation),
        "entity_registry_id": value.entity_registry_id,
    }


def decode_requirement(data: JsonObject) -> StateRequirement:
    """Validate a scoped entity requirement without coercing truth values."""
    return StateRequirement(
        cv._str(data["entity_id"]),
        tuple(cv._string_list(data["accepted_states"])),
        _optional_number(data["max_age_seconds"]),
        cv._optional_str(data["robot_id"]),
        cv._optional_enum(OperationKind, data["operation"]),
        cv._optional_str(data["entity_registry_id"]),
    )


def encode_receipt(value: CleaningReceipt) -> JsonObject:
    """Serialize a successful run's independent provenance."""
    return {
        "receipt_id": value.receipt_id,
        "source": value.source.value,
        "source_id": value.source_id,
        "room_ids": list(value.room_ids),
        "operation": value.operation.value,
        "completed_at": cv._encode_datetime(value.completed_at),
        "quality": value.quality.value,
        "evidence": list(value.evidence),
    }


def decode_receipt(data: JsonObject) -> CleaningReceipt:
    """Decode scope, operation and completion quality separately."""
    return CleaningReceipt(
        cv._str(data["receipt_id"]),
        cv._enum(CleaningSource, data["source"]),
        cv._str(data["source_id"]),
        tuple(cv._string_list(data["room_ids"])),
        cv._enum(OperationKind, data["operation"]),
        cv._decode_datetime(data["completed_at"]),
        cv._enum(CompletionQuality, data["quality"]),
        tuple(cv._string_list(data["evidence"])),
    )


def _encode_stamp(value: CleaningStamp) -> JsonObject:
    return {
        "receipt_id": value.receipt_id,
        "completed_at": cv._encode_datetime(value.completed_at),
        "quality": value.quality.value,
        "occupancy_epoch": value.occupancy_epoch,
        "occupied_seconds": value.occupied_seconds,
        "unknown_seconds": value.unknown_seconds,
        "occupancy_baseline_known": value.occupancy_baseline_known,
    }


def _decode_stamp(data: JsonObject) -> CleaningStamp:
    return CleaningStamp(
        cv._str(data["receipt_id"]),
        cv._decode_datetime(data["completed_at"]),
        cv._enum(CompletionQuality, data["quality"]),
        cv._int(data["occupancy_epoch"]),
        _number(data["occupied_seconds"]),
        _number(data["unknown_seconds"]),
        cv._bool(data["occupancy_baseline_known"]),
    )


def encode_room(value: Room) -> JsonObject:
    """Serialize canonical configuration and cleaning projections."""
    grant = value.release
    policy = value.due_policy
    counter = value.occupancy
    return {
        "room_id": value.room_id,
        "name": value.name,
        "area_id": value.area_id,
        "floor_id": value.floor_id,
        "enabled": value.enabled,
        "area_missing": value.area_missing,
        "follow_area_name": value.follow_area_name,
        "bindings": [
            {
                "robot_id": binding.robot_id,
                "target_ids": list(binding.target_ids),
                "map_id": binding.map_id,
            }
            for binding in value.bindings
        ],
        "requirements": [
            encode_requirement(requirement) for requirement in value.requirements
        ],
        "release": None
        if grant is None
        else {
            "grant_id": grant.grant_id,
            "kind": grant.kind.value,
            "granted_at": cv._encode_datetime(grant.granted_at),
            "expires_at": cv._encode_optional_datetime(grant.expires_at),
            "reserved_job_id": grant.reserved_job_id,
            "consumed": grant.consumed,
            "queue_run_id": grant.queue_run_id,
        },
        "due_policy": {
            "basis": policy.basis.value,
            "vacuum_seconds": policy.vacuum_seconds,
            "mop_seconds": policy.mop_seconds,
            "occupancy_entity_id": policy.occupancy_entity_id,
            "occupied_state": policy.occupied_state,
            "unoccupied_state": policy.unoccupied_state,
            "occupancy_entity_registry_id": policy.occupancy_entity_registry_id,
        },
        "occupancy": {
            "epoch": counter.epoch,
            "occupied_seconds": counter.occupied_seconds,
            "unknown_seconds": counter.unknown_seconds,
            "observed_at": cv._encode_optional_datetime(counter.observed_at),
            "occupied": counter.occupied,
        },
        "last_cleaning": {
            key.value: _encode_stamp(stamp)
            for key, stamp in value.last_cleaning.items()
        },
        "last_confirmed": {
            key.value: _encode_stamp(stamp)
            for key, stamp in value.last_confirmed.items()
        },
    }


def decode_room(data: JsonObject) -> Room:
    """Decode the complete room schema; absence is never treated as a grant."""
    policy = cv._object(data["due_policy"])
    counter = cv._object(data["occupancy"])
    grant = None if data["release"] is None else cv._object(data["release"])
    return Room(
        room_id=cv._str(data["room_id"]),
        name=cv._str(data["name"]),
        area_id=cv._optional_str(data["area_id"]),
        floor_id=cv._optional_str(data["floor_id"]),
        enabled=cv._bool(data["enabled"]),
        area_missing=cv._bool(data["area_missing"]),
        follow_area_name=cv._bool(data["follow_area_name"]),
        bindings=tuple(
            RoomBinding(
                cv._str(item["robot_id"]),
                tuple(cv._string_list(item["target_ids"])),
                cv._optional_str(item["map_id"]),
            )
            for item in cv._object_list(data["bindings"])
        ),
        requirements=tuple(
            decode_requirement(item) for item in cv._object_list(data["requirements"])
        ),
        release=None
        if grant is None
        else RoomRelease(
            cv._str(grant["grant_id"]),
            cv._enum(ReleaseKind, grant["kind"]),
            cv._decode_datetime(grant["granted_at"]),
            cv._decode_optional_datetime(grant["expires_at"]),
            cv._optional_str(grant["reserved_job_id"]),
            cv._bool(grant["consumed"]),
            cv._optional_str(grant.get("queue_run_id")),
        ),
        due_policy=DuePolicy(
            cv._enum(DueBasis, policy["basis"]),
            _optional_number(policy["vacuum_seconds"]),
            _optional_number(policy["mop_seconds"]),
            cv._optional_str(policy["occupancy_entity_id"]),
            cv._str(policy["occupied_state"]),
            cv._str(policy["unoccupied_state"]),
            cv._optional_str(policy.get("occupancy_entity_registry_id")),
        ),
        occupancy=OccupancyCounter(
            cv._int(counter["epoch"]),
            _number(counter["occupied_seconds"]),
            _number(counter["unknown_seconds"]),
            cv._decode_optional_datetime(counter["observed_at"]),
            None if counter["occupied"] is None else cv._bool(counter["occupied"]),
        ),
        last_cleaning={
            cv._enum(OperationKind, key): _decode_stamp(cv._object(value))
            for key, value in cv._object(data["last_cleaning"]).items()
        },
        last_confirmed={
            cv._enum(OperationKind, key): _decode_stamp(cv._object(value))
            for key, value in cv._object(data["last_confirmed"]).items()
        },
    )


def encode_room_registry(value: RoomRegistry) -> JsonObject:
    """Serialize the room aggregate within the critical global snapshot."""
    return {
        "rooms": {key: encode_room(room) for key, room in value.rooms.items()},
        "receipts": {
            key: encode_receipt(receipt) for key, receipt in value.receipts.items()
        },
        "admissions": {
            key: [
                {
                    "room_id": admission.room_id,
                    "grant_id": admission.grant_id,
                    "started": admission.started,
                }
                for admission in admissions
            ]
            for key, admissions in value.admissions.items()
        },
    }


def decode_room_registry(data: JsonObject) -> RoomRegistry:
    """Validate room, receipt and projection relations during every commit."""
    return RoomRegistry(
        rooms={
            key: decode_room(cv._object(value))
            for key, value in cv._object(data["rooms"]).items()
        },
        receipts={
            key: decode_receipt(cv._object(value))
            for key, value in cv._object(data["receipts"]).items()
        },
        admissions={
            key: tuple(
                RoomAdmission(
                    cv._str(item["room_id"]),
                    cv._str(item["grant_id"]),
                    cv._bool(item["started"]),
                )
                for item in cv._object_list(value)
            )
            for key, value in cv._object(data["admissions"]).items()
        },
    )
