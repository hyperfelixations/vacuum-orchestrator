"""All-rooms selections resolve to rooms that can currently be cleaned."""

from dataclasses import replace

import pytest

from custom_components.vacuum_orchestrator.domain.errors import ConflictError
from custom_components.vacuum_orchestrator.infrastructure.codec import (
    decode_orchestrator_state,
    encode_orchestrator_state,
)
from tests.application.test_orchestrator import (
    RecordingAdapter,
    RecordingBackend,
    _intent,
)
from tests.application.test_templates import setup_due


async def test_eligible_rooms_are_enabled_present_and_mapped() -> None:
    core = await setup_due(RecordingAdapter(RecordingBackend(), "robot"))
    await core.rooms.async_create("Study")
    assert core.eligible_room_ids() == ("kitchen", "hall")

    await core.rooms.async_disable("hall")
    assert core.eligible_room_ids() == ("kitchen",)

    await core.rooms.async_import_areas({})
    with pytest.raises(ConflictError, match="no_eligible_rooms"):
        core.eligible_room_ids()


async def test_all_rooms_templates_resolve_on_every_generation() -> None:
    core = await setup_due(RecordingAdapter(RecordingBackend(), "robot"))
    await core.rooms.async_disable("hall")
    key = await core.templates.async_save(
        "Everything", replace(_intent(), all_rooms=True), automatic=True
    )
    await core.rooms.async_enable("hall")

    job = await core.templates.async_create_job(key)
    assert [target.area_id for target in core.state.jobs[job].intent.areas] == [
        "kitchen",
        "hall",
    ]
    assert core.state.jobs[job].intent.all_rooms
    await core.async_delete_job(job)
    await core.templates.async_generate_due()
    generated = {
        target.area_id
        for item in core.state.jobs.values()
        for target in item.intent.areas
    }
    assert generated == {"kitchen", "hall"}
    assert not any(item.intent.all_rooms for item in core.state.jobs.values())
    assert core.state.templates[key].intent.all_rooms
    assert (
        decode_orchestrator_state(encode_orchestrator_state(core.state)) == core.state
    )

    await core.rooms.async_import_areas({})
    with pytest.raises(ConflictError, match="no_eligible_rooms"):
        await core.templates.async_create_job(key)
