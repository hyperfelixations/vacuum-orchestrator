"""Views are notified only for real changes and name the affected scopes."""

from __future__ import annotations

from dataclasses import fields, replace
from types import MappingProxyType
from typing import Any, cast

import pytest
from homeassistant.components.vacuum.const import VacuumEntityFeature
from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuum_orchestrator.api.websocket import (
    TYPE_SUBSCRIBE,
    websocket_subscribe,
)
from custom_components.vacuum_orchestrator.application.orchestrator import (
    INTERNAL_FIELDS,
    SCOPE_FIELDS,
    VIEW_SCOPES,
)
from custom_components.vacuum_orchestrator.const import (
    CONF_ADAPTER,
    CONF_INSTALLATION_ID,
    CONF_ROBOT_ENTITY_ID,
    CONF_ROBOT_REGISTRY_ID,
    CONF_TARGET_AREAS,
    DOMAIN,
)
from custom_components.vacuum_orchestrator.domain.holds import HoldPurpose
from custom_components.vacuum_orchestrator.domain.intents import JobIntent, TargetRef
from custom_components.vacuum_orchestrator.domain.queue import OrchestratorState
from custom_components.vacuum_orchestrator.domain.releases import ReleaseKind
from custom_components.vacuum_orchestrator.domain.types import CleaningMode, JobState
from custom_components.vacuum_orchestrator.runtime import (
    async_setup_orchestrator,
    async_unload_orchestrator,
)
from tests.application.test_orchestrator import (
    NOW,
    RecordingAdapter,
    RecordingBackend,
    _intent,
    _orchestrator,
)
from tests.test_configuration_api import call, configured  # noqa: F401
from tests.test_job_holds_api import Card
from tests.test_runtime import MemoryBackend
from tests.test_websocket import Connection


def test_job_counters_follow_job_states() -> None:
    state = OrchestratorState.empty("installation").add_job(
        "a", JobIntent((TargetRef("kitchen"),), CleaningMode.VACUUM), NOW
    )
    jobs = dict(state.jobs)
    jobs["b"] = replace(jobs["a"], job_id="b", state=JobState.RUNNING)
    jobs["c"] = replace(jobs["a"], job_id="c", state=JobState.CANCELING)
    jobs["d"] = replace(jobs["a"], job_id="d", state=JobState.NEEDS_ATTENTION)
    counted = replace(state, jobs=jobs, queue=("a",))

    assert counted.active_job_count == 2
    assert counted.attention_job_count == 1


async def test_commits_report_the_changed_scopes() -> None:
    backend = RecordingBackend()
    core = await _orchestrator(backend, RecordingAdapter(backend, "robot"))
    seen: list[frozenset[str]] = []
    core.subscribe_view(lambda: seen.append(core.changed_scopes))

    job = await core.async_create_job(_intent())
    await core.rooms.async_grant("hall", ReleaseKind.PERMANENT)
    await core.async_start_job(job)

    assert seen[0] == {"jobs", "queue"}
    assert seen[1] == {"rooms"}
    assert {"jobs", "queue", "robots", "rooms"} <= set().union(*seen[2:])
    seen.clear()
    await core.async_configure_job_defaults({"passes": 2})
    await core.async_configure_job_defaults({"passes": 2})
    # Configured defaults are a setup fact too.
    assert seen == [frozenset({"queue", "setup"})]


def test_every_state_field_belongs_to_a_scope_or_is_internal() -> None:
    scoped = {name for names in SCOPE_FIELDS.values() for name in names}

    assert set(SCOPE_FIELDS) == VIEW_SCOPES
    assert not scoped & INTERNAL_FIELDS
    assert scoped | INTERNAL_FIELDS == {item.name for item in fields(OrchestratorState)}


async def test_holds_and_the_start_delay_signal_their_read_models() -> None:
    backend = RecordingBackend()
    core = await _orchestrator(backend, RecordingAdapter(backend, "robot"))
    job = await core.async_create_job(_intent())
    seen: list[frozenset[str]] = []
    core.subscribe_view(lambda: seen.append(core.changed_scopes))

    hold = await core.async_hold_job(job, HoldPurpose.EDIT)
    await core.async_release_job_hold(hold.hold_id)
    await core.runs.async_configure(start_delay_seconds=30)

    assert seen == [{"jobs", "queue"}, {"jobs", "queue"}, {"queue", "setup"}]


async def test_a_job_waiting_for_a_release_signals_its_room() -> None:
    backend = RecordingBackend()
    core = await _orchestrator(backend, RecordingAdapter(backend, "robot"))
    await core.rooms.async_revoke("hall")
    seen: list[frozenset[str]] = []
    core.subscribe_view(lambda: seen.append(core.changed_scopes))

    job = await core.async_create_job(_intent("hall"))
    await core.rooms.async_grant("hall", ReleaseKind.PERMANENT)
    await core.rooms.async_revoke("hall")
    await core.async_delete_job(job)

    assert seen == [{"jobs", "queue", "rooms"}] * 4


async def test_a_new_area_of_a_named_room_signals_the_jobs_naming_it() -> None:
    backend = RecordingBackend()
    core = await _orchestrator(backend, RecordingAdapter(backend, "robot"))
    await core.async_create_job(_intent())
    seen: list[frozenset[str]] = []
    core.subscribe_view(lambda: seen.append(core.changed_scopes))

    await core.rooms.async_update("kitchen", lambda room: replace(room, name="Cook"))
    await core.rooms.async_update("hall", lambda room: replace(room, area_id="lobby"))
    await core.rooms.async_update(
        "kitchen", lambda room: replace(room, area_id="galley")
    )

    assert seen == [{"rooms"}, {"rooms"}, {"jobs", "queue", "rooms"}]


async def test_a_failing_projection_signals_every_scope_and_keeps_the_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = RecordingBackend()
    core = await _orchestrator(backend, RecordingAdapter(backend, "robot"))
    seen: list[frozenset[str]] = []
    core.subscribe_view(lambda: seen.append(core.changed_scopes))

    def broken(_state: OrchestratorState) -> dict[str, object]:
        raise RuntimeError("projection")

    monkeypatch.setattr(core, "view_projection", broken)
    job = await core.async_create_job(_intent())

    assert job in core.state.jobs
    assert seen == [VIEW_SCOPES]
    assert any(
        row.get("reason") == "view_projection_failed" for row in core.trace.snapshot()
    )


@pytest.mark.usefixtures("configured")
async def test_a_loaded_subscriber_hears_what_others_change(
    hass: HomeAssistant,
) -> None:
    room = (await call(hass, "create_room", name="Office"))["room_id"]
    subscriber = Connection()
    websocket_subscribe(
        hass, cast(ActiveConnection, subscriber), {"id": 1, "type": TYPE_SUBSCRIBE}
    )
    await hass.async_block_till_done()

    async def changed(action: Any) -> set[str]:
        before = len(subscriber.events)
        await action
        await hass.async_block_till_done()
        return set().union(
            *(event["changed"] for _id, event in subscriber.events[before:])
        )

    job = None

    async def create() -> None:
        nonlocal job
        job = (await call(hass, "create_job", areas=[room]))["job_id"]

    assert {"jobs", "queue", "rooms"} <= await changed(create())
    assert "queue" in await changed(
        call(hass, "configure_queue", start_delay_seconds=30)
    )
    assert {"jobs", "queue"} <= await changed(
        Card(hass).command("hold_job", job_id=job, purpose="edit")
    )
    assert {"jobs", "queue", "rooms"} <= await changed(
        call(hass, "release_room", rooms=[room], kind="permanent")
    )


@pytest.mark.usefixtures("configured")
async def test_a_new_room_signals_the_reach_of_every_robot(hass: HomeAssistant) -> None:
    vacuum = er.async_get(hass).async_get_or_create("vacuum", "demo", "reach")
    hass.states.async_set(
        vacuum.entity_id,
        "docked",
        {"supported_features": int(VacuumEntityFeature.CLEAN_AREA)},
    )
    await call(
        hass,
        "add_robot",
        configuration={"robot_entity_id": vacuum.entity_id, "fixed_mode": "vacuum"},
    )
    await hass.async_block_till_done()
    subscriber = Connection()
    websocket_subscribe(
        hass, cast(ActiveConnection, subscriber), {"id": 1, "type": TYPE_SUBSCRIBE}
    )
    await hass.async_block_till_done()
    before = len(subscriber.events)

    room = (await call(hass, "create_room", name="Hall"))["room_id"]
    await hass.async_block_till_done()

    changed = set().union(
        *(event["changed"] for _id, event in subscriber.events[before:])
    )
    assert changed == {"rooms", "robots", "setup"}
    reach = (await call(hass, "get_robots"))["robots"][0]["reach"]
    assert room in {item["room_id"] for item in reach}


async def test_controller_skips_passes_without_visible_change(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    MemoryBackend.data = None
    monkeypatch.setattr(
        "custom_components.vacuum_orchestrator.runtime.HomeAssistantSnapshotBackend",
        MemoryBackend,
    )
    registry = er.async_get(hass)
    vacuum = registry.async_get_or_create(
        "vacuum", "demo", "unit", suggested_object_id="test"
    )
    area = ar.async_get(hass).async_create("Kitchen")
    registry.async_update_entity_options(
        vacuum.entity_id, "vacuum", {"area_mapping": {area.id: ["16"]}}
    )
    attrs = {"supported_features": int(VacuumEntityFeature.CLEAN_AREA)}
    hass.states.async_set(vacuum.entity_id, "docked", {**attrs, "battery_level": 80})
    hass.states.async_set("binary_sensor.door", "off")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_INSTALLATION_ID: DOMAIN, "auto_discover_robots": False},
    )
    entry.add_to_hass(hass)
    hass.config_entries.async_add_subentry(
        entry,
        ConfigSubentry(
            data=MappingProxyType(
                {
                    CONF_ROBOT_REGISTRY_ID: vacuum.id,
                    CONF_ROBOT_ENTITY_ID: vacuum.entity_id,
                    CONF_ADAPTER: "home_assistant",
                    CONF_TARGET_AREAS: [area.id],
                    "fixed_mode": "vacuum",
                }
            ),
            subentry_type="robot",
            title="Robot",
            unique_id=vacuum.id,
        ),
    )
    await async_setup_orchestrator(hass, entry)
    await hass.async_block_till_done()
    core = entry.runtime_data.orchestrator
    await core.async_create_job(
        JobIntent(
            (TargetRef(area.id),),
            CleaningMode.VACUUM,
            required_on=("binary_sensor.door",),
        )
    )
    await hass.async_block_till_done()
    sequence = core.runtime_sequence

    hass.states.async_set(vacuum.entity_id, "docked", {**attrs, "battery_level": 79})
    await hass.async_block_till_done()
    assert core.runtime_sequence == sequence

    hass.states.async_set("binary_sensor.door", "on")
    await hass.async_block_till_done()
    assert core.runtime_sequence == sequence + 1
    assert core.changed_scopes == {"jobs", "queue"}
    await async_unload_orchestrator(hass, entry)
