"""Room commands commit through the global application transaction."""

from dataclasses import replace
from datetime import timedelta

import pytest

from custom_components.vacuum_orchestrator.application.room_service import AreaSnapshot
from custom_components.vacuum_orchestrator.domain.due import DueBasis, DuePolicy
from custom_components.vacuum_orchestrator.domain.errors import (
    ConflictError,
    ValidationError,
)
from custom_components.vacuum_orchestrator.domain.releases import ReleaseKind
from custom_components.vacuum_orchestrator.domain.types import OperationKind
from tests.application.test_orchestrator import (
    NOW,
    RecordingAdapter,
    RecordingBackend,
    _intent,
    _orchestrator,
)
from tests.domain.test_rooms import receipt


async def test_area_import_rename_exclusion_deletion_and_reappearance() -> None:
    orchestrator = await _orchestrator(RecordingBackend(), seed_rooms=False)
    service = orchestrator.rooms
    await service.async_import_areas({"kitchen": AreaSnapshot("Kitchen", "floor")})
    room = service.registry.resolve("kitchen")
    assert room.release is None
    assert room.floor_id == "floor"
    commit_id = orchestrator.state.commit_id
    await service.async_import_areas({"kitchen": AreaSnapshot("Kitchen", "floor")})
    assert orchestrator.state.commit_id == commit_id
    await service.async_disable(room.room_id)
    await service.async_import_areas({"kitchen": AreaSnapshot("Renamed", "floor")})
    renamed = service.registry.resolve("kitchen")
    assert renamed.room_id == room.room_id
    assert renamed.name == "Renamed"
    assert not renamed.enabled
    await service.async_import_areas({})
    assert service.registry.resolve("kitchen").area_missing
    with pytest.raises(ConflictError, match="room_unavailable"):
        await service.async_grant(room.room_id, ReleaseKind.PERMANENT)
    await service.async_update(
        room.room_id, lambda room: replace(room, name="Custom", follow_area_name=False)
    )
    await service.async_import_areas({"kitchen": AreaSnapshot("Remote name")})
    assert service.registry.resolve("kitchen").name == "Custom"
    await service.async_enable(room.room_id)
    assert service.registry.resolve("kitchen").enabled


async def test_grant_revoke_and_runtime_facts_cannot_be_patched() -> None:
    orchestrator = await _orchestrator(RecordingBackend(), seed_rooms=False)
    service = orchestrator.rooms
    room_id = await service.async_create("Kitchen")
    await service.async_grant(room_id, ReleaseKind.TIMED, 7200)
    room = service.registry.resolve(room_id)
    assert room.release.expires_at == NOW + timedelta(hours=2)
    with pytest.raises(ValidationError, match="runtime_state"):
        await service.async_update(room_id, lambda room: replace(room, release=None))
    with pytest.raises(ValidationError, match="duration_mismatch"):
        await service.async_grant(room_id, ReleaseKind.ONCE, 60)
    await service.async_revoke(room_id)
    assert service.registry.resolve(room_id).release is None
    commit_id = orchestrator.state.commit_id
    await service.async_revoke(room_id)
    await service.async_update(room_id, lambda room: room)
    assert orchestrator.state.commit_id == commit_id


async def test_room_configuration_is_protected_during_execution() -> None:
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    orchestrator = await _orchestrator(backend, adapter, seed_rooms=False)
    room_id = await orchestrator.rooms.async_create("Kitchen", area_id="kitchen")
    await orchestrator.rooms.async_grant(room_id, ReleaseKind.PERMANENT)
    adapter._profile = replace(
        adapter.profile,
        capabilities=replace(
            adapter.profile.capabilities, target_map={room_id: "kitchen"}
        ),
    )
    job_id = await orchestrator.async_create_job(_intent())
    await orchestrator.async_start_job(job_id)
    with pytest.raises(ConflictError, match="room_has_active_job"):
        await orchestrator.rooms.async_disable(room_id)


async def test_policy_change_resets_counter_epoch_and_records_restart_gap() -> None:
    orchestrator = await _orchestrator(RecordingBackend(), seed_rooms=False)
    service = orchestrator.rooms
    room_id = await service.async_create("Kitchen")
    await service.async_update(
        room_id,
        lambda room: replace(
            room,
            due_policy=DuePolicy(
                DueBasis.OCCUPIED, 3600, None, "binary_sensor.presence"
            ),
        ),
    )
    assert service.registry.resolve(room_id).occupancy.epoch == 1
    await service.async_observe_occupancy({"binary_sensor.presence": "on"}, NOW)
    fact = replace(receipt(at=NOW), room_ids=(room_id,))
    await service.async_record_receipt(fact)
    commit_id = orchestrator.state.commit_id
    await service.async_record_receipt(fact)
    assert orchestrator.state.commit_id == commit_id
    await service.async_observe_occupancy(
        {"binary_sensor.presence": "off"}, NOW + timedelta(hours=1)
    )
    assert service.registry.resolve(room_id).occupancy.occupied_seconds == 3600
    await service.async_observe_occupancy(
        {"binary_sensor.presence": None}, NOW + timedelta(hours=2), restart=True
    )
    room = service.registry.resolve(room_id)
    assert room.occupancy.unknown_seconds == 3600
    assert room.last_confirmed[OperationKind.VACUUM].receipt_id == fact.receipt_id
    with pytest.raises(ValidationError, match="completion_in_future"):
        await service.async_record_receipt(
            replace(fact, receipt_id="future", completed_at=NOW + timedelta(days=1))
        )
