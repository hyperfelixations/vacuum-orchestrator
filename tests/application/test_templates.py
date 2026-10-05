"""Stored templates snapshot intent and bound automatic demand across restarts."""

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from custom_components.vacuum_orchestrator.domain.completion import (
    CleaningReceipt,
    CleaningSource,
    CompletionQuality,
)
from custom_components.vacuum_orchestrator.domain.due import DuePolicy
from custom_components.vacuum_orchestrator.domain.errors import (
    ConflictError,
    DispatchNotStartedError,
    StorageIntegrityError,
)
from custom_components.vacuum_orchestrator.domain.intents import JobIntent, TargetRef
from custom_components.vacuum_orchestrator.domain.types import (
    CleaningMode,
    OperationKind,
)
from custom_components.vacuum_orchestrator.infrastructure.codec import (
    decode_orchestrator_state,
    encode_orchestrator_state,
)
from tests.application.test_orchestrator import (
    NOW,
    RecordingAdapter,
    RecordingBackend,
    _intent,
    _orchestrator,
)


async def setup_due(*adapters):
    backend = adapters[0].backend if adapters else RecordingBackend()
    core = await _orchestrator(backend, *adapters)
    for room in ("kitchen", "hall"):
        await core.rooms.async_update(
            room,
            lambda room: replace(
                room, due_policy=DuePolicy(vacuum_seconds=3600, mop_seconds=3600)
            ),
        )
    return core


async def test_template_snapshots_are_independent_and_storage_roundtrips():
    core = await setup_due()
    key = await core.templates.async_save("Routine", _intent())
    job = await core.templates.async_create_job(key)
    await core.templates.async_save(
        "Updated", _intent(mode=CleaningMode.MOP), template_id=key
    )
    assert core.state.jobs[job].intent.mode is CleaningMode.VACUUM
    assert (
        decode_orchestrator_state(encode_orchestrator_state(core.state)) == core.state
    )
    await core.templates.async_remove(key)
    assert job in core.state.jobs
    for command in (
        core.templates.async_create_job,
        core.templates.async_remove,
        core.templates.async_reset_demand,
    ):
        with pytest.raises(ConflictError, match="unknown_template"):
            await command(key)
    with pytest.raises(ConflictError, match="unknown_template"):
        await core.templates.async_save("Missing", _intent(), template_id=key)
    disabled = await core.templates.async_save(
        "Disabled", _intent(), enabled=False, automatic=True
    )
    with pytest.raises(ConflictError, match="template_disabled"):
        await core.templates.async_create_job(disabled)
    await core.templates.async_generate_due()
    assert len(core.state.jobs) == 1


async def test_concurrent_wakeups_and_templates_do_not_duplicate_room_demand():
    core = await setup_due()
    key = await core.templates.async_save(
        "Both rooms",
        JobIntent((TargetRef("kitchen"), TargetRef("hall")), CleaningMode.VACUUM),
        automatic=True,
    )
    await core.templates.async_save("Other rule", _intent(), automatic=True)
    await asyncio.gather(*(core.templates.async_generate_due() for _ in range(10)))
    assert len(core.state.queue) == 2
    assert len(core.state.templates[key].demand_tokens) == 2
    before = core.state.commit_id
    await core.templates.async_generate_due()
    assert core.state.commit_id == before
    restored = decode_orchestrator_state(encode_orchestrator_state(core.state))
    assert (
        restored.templates[key].demand_tokens == core.state.templates[key].demand_tokens
    )
    for job in tuple(core.state.queue):
        await core.async_cancel_job(job)
        await core.async_delete_job(job)
    await core.templates.async_generate_due()
    # A separate rule has its own single attempt; it cannot create while work overlaps.
    assert len(core.state.queue) == 1
    await core.async_cancel_job(core.state.queue[0])
    await core.templates.async_generate_due()
    assert not core.state.queue
    await core.templates.async_reset_demand(key)
    await core.templates.async_generate_due()
    assert len(core.state.queue) == 2


async def test_failed_automatic_job_stays_suppressed_until_cleaning_or_explicit_reset(
    monkeypatch,
):
    backend = RecordingBackend()
    adapter = RecordingAdapter(backend, "robot")
    core = await _orchestrator(backend, adapter)
    await core.rooms.async_update(
        "kitchen", lambda room: replace(room, due_policy=DuePolicy(vacuum_seconds=3600))
    )
    key = await core.templates.async_save("Rule", _intent(), automatic=True)
    await core.templates.async_generate_due()
    job = core.state.queue[0]

    async def failed(*_args):
        raise DispatchNotStartedError("settings_failed")

    monkeypatch.setattr(adapter, "async_prepare", failed)
    with pytest.raises(DispatchNotStartedError):
        await core.async_start_job(job)
    await core.async_delete_job(job)
    await core.templates.async_generate_due()
    assert not core.state.queue
    await core.rooms.async_record_receipt(
        CleaningReceipt(
            "fresh",
            CleaningSource.EXTERNAL,
            "external",
            ("kitchen",),
            OperationKind.VACUUM,
            NOW,
            CompletionQuality.CONFIRMED,
            ("confirmed_scope",),
        )
    )
    await core.templates.async_generate_due()
    assert not core.state.templates[key].demand_tokens
    core.templates._clock = lambda: NOW + timedelta(hours=2)
    await core.templates.async_generate_due()
    assert len(core.state.queue) == 1


async def test_excluded_rooms_and_unknown_due_do_not_create_work():
    core = await setup_due()
    await core.rooms.async_disable("kitchen")
    await core.templates.async_save("Rule", _intent(), automatic=True)
    await core.templates.async_generate_due()
    assert not core.state.jobs
    data = encode_orchestrator_state(core.state)
    template = next(iter(data["templates"].values()))
    template["intent"]["areas"][0]["area_id"] = "missing"
    with pytest.raises(StorageIntegrityError):
        decode_orchestrator_state(data)
