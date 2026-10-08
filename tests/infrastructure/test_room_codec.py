"""Current room snapshots and non-destructive V2 migration."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest

from custom_components.vacuum_orchestrator.domain.completion import CompletionQuality
from custom_components.vacuum_orchestrator.domain.due import (
    DueBasis,
    DuePolicy,
    OccupancyCounter,
)
from custom_components.vacuum_orchestrator.domain.errors import StorageIntegrityError
from custom_components.vacuum_orchestrator.domain.queue import OrchestratorState
from custom_components.vacuum_orchestrator.domain.releases import (
    ReleaseKind,
    RoomRelease,
)
from custom_components.vacuum_orchestrator.domain.requirements import StateRequirement
from custom_components.vacuum_orchestrator.domain.room_registry import RoomRegistry
from custom_components.vacuum_orchestrator.domain.rooms import Room, RoomBinding
from custom_components.vacuum_orchestrator.domain.types import OperationKind
from custom_components.vacuum_orchestrator.infrastructure.codec import (
    decode_orchestrator_state,
    encode_orchestrator_state,
    migrate_schema_four,
    migrate_schema_three,
    migrate_schema_two,
)
from custom_components.vacuum_orchestrator.infrastructure.critical_repository import (
    CriticalOrchestratorRepository,
    MigratingOrchestratorRepository,
)
from custom_components.vacuum_orchestrator.infrastructure.integrity import seal_snapshot
from custom_components.vacuum_orchestrator.infrastructure.room_codec import (
    decode_room_registry,
    encode_room_registry,
)
from tests.domain.test_queue import INTENT
from tests.domain.test_rooms import NOW, receipt
from tests.infrastructure.test_critical_repository import Backend


def rich_registry(kind: ReleaseKind = ReleaseKind.ONCE) -> RoomRegistry:
    grant = RoomRelease(
        "grant",
        kind,
        NOW,
        NOW + timedelta(hours=2) if kind is ReleaseKind.TIMED else None,
    )
    room = Room(
        "room",
        "Kitchen",
        area_id="kitchen",
        floor_id="ground",
        bindings=(RoomBinding("robot", ("1", "2"), "map"),),
        requirements=(
            StateRequirement(
                "binary_sensor.door",
                ("on",),
                60,
                "robot",
                OperationKind.MOP,
                "registry-id",
            ),
        ),
        release=grant,
        due_policy=DuePolicy(DueBasis.OCCUPIED, 3600, 7200, "binary_sensor.presence"),
        occupancy=OccupancyCounter().advance(NOW, True),
    )
    return RoomRegistry({"room": room}).record(
        receipt(operation=OperationKind.VACUUM_AND_MOP)
    )


@pytest.mark.parametrize("kind", list(ReleaseKind))
def test_complete_room_registry_roundtrip_preserves_grants_and_evidence(
    kind: ReleaseKind,
) -> None:
    registry = rich_registry(kind)
    assert decode_room_registry(encode_room_registry(registry)) == registry
    admitted = registry.admit("job", ("room",), NOW).mark_started("job")
    assert decode_room_registry(encode_room_registry(admitted)) == admitted
    state = replace(OrchestratorState.empty("installation"), room_registry=registry)
    assert decode_orchestrator_state(encode_orchestrator_state(state)) == state


@pytest.mark.parametrize(
    "field,value",
    [
        ("enabled", "false"),
        ("last_cleaning", []),
        ("release", {}),
        (
            "occupancy",
            {
                "epoch": 0,
                "occupied_seconds": float("nan"),
                "unknown_seconds": 0,
                "observed_at": None,
                "occupied": None,
            },
        ),
    ],
)
def test_corrupt_room_storage_is_rejected(field: str, value: object) -> None:
    data = encode_orchestrator_state(
        replace(OrchestratorState.empty("installation"), room_registry=rich_registry())
    )
    data["room_registry"]["rooms"]["room"][field] = value
    with pytest.raises(StorageIntegrityError):
        decode_orchestrator_state(data)


def test_forged_cleaning_projection_is_rejected() -> None:
    data = encode_orchestrator_state(
        replace(OrchestratorState.empty("installation"), room_registry=rich_registry())
    )
    data["room_registry"]["receipts"] = {}
    with pytest.raises(StorageIntegrityError, match="invalid_storage_payload"):
        decode_orchestrator_state(data)


def test_evidence_upgrade_preserves_occupied_time_baseline() -> None:
    initial = replace(receipt(), quality=CompletionQuality.DERIVED)
    room = Room("room", "Kitchen", occupancy=OccupancyCounter().advance(NOW, True))
    registry = RoomRegistry({"room": room}).record(initial)
    later = replace(
        registry.rooms["room"],
        occupancy=room.occupancy.advance(NOW + timedelta(hours=1), True),
    )
    upgraded = registry.put_room(later).record(
        replace(initial, quality=CompletionQuality.CONFIRMED)
    )
    stamp = upgraded.rooms["room"].last_confirmed[OperationKind.VACUUM]
    assert stamp.occupancy_baseline_known
    assert stamp.occupied_seconds == 0
    assert decode_room_registry(encode_room_registry(upgraded)) == upgraded


async def test_v2_import_preserves_source_and_does_not_grant_rooms() -> None:
    old_state = replace(
        OrchestratorState.empty("installation"), start_delay_seconds=0
    ).add_job("job", INTENT, NOW)
    payload = encode_orchestrator_state(old_state)
    del payload["room_registry"]
    del payload["start_delay_seconds"]
    del payload["jobs"]["job"]["start_after"]
    payload["schema_version"] = 2
    old = Backend()
    old.data = seal_snapshot(payload)
    original = deepcopy(old.data)
    current = CriticalOrchestratorRepository(Backend())
    repository = MigratingOrchestratorRepository(
        current, Backend(), "installation", previous_stores=((old, migrate_schema_two),)
    )

    imported = await repository.async_load()

    assert imported.jobs == old_state.jobs
    assert imported.queue == old_state.queue
    assert imported.room_registry.rooms["kitchen"].release is None
    assert imported.room_registry.rooms["kitchen"].area_missing
    assert old.data == original
    old.data = {"invalid": True}
    assert await repository.async_load() == imported


async def test_v2_wrong_installation_and_unknown_schema_are_not_imported() -> None:
    payload = encode_orchestrator_state(OrchestratorState.empty("other"))
    payload["schema_version"] = 2
    old = Backend()
    old.data = seal_snapshot(payload)
    current = CriticalOrchestratorRepository(Backend())
    repository = MigratingOrchestratorRepository(
        current, Backend(), "installation", previous_stores=((old, migrate_schema_two),)
    )
    with pytest.raises(StorageIntegrityError, match="ownership_mismatch"):
        await repository.async_load()
    assert await current.async_load() is None
    with pytest.raises(StorageIntegrityError, match="unsupported_previous"):
        migrate_schema_two({"schema_version": 99})


async def test_newest_previous_store_is_imported_first() -> None:
    def sealed(job_id: str, schema: int) -> Backend:
        payload = encode_orchestrator_state(
            OrchestratorState.empty("installation").add_job(job_id, INTENT, NOW)
        )
        payload["schema_version"] = schema
        del payload["start_delay_seconds"]
        for job in payload["jobs"].values():
            del job["start_after"]
        if schema < 4:
            del payload["job_defaults"]
        if schema == 2:
            del payload["room_registry"]
        backend = Backend()
        backend.data = seal_snapshot(payload)
        return backend

    current = CriticalOrchestratorRepository(Backend())
    repository = MigratingOrchestratorRepository(
        current,
        Backend(),
        "installation",
        previous_stores=(
            (sealed("four", 4), migrate_schema_four),
            (sealed("three", 3), migrate_schema_three),
            (sealed("two", 2), migrate_schema_two),
        ),
    )

    imported = await repository.async_load()

    assert set(imported.jobs) == {"four"}
    assert await current.async_load() == imported
