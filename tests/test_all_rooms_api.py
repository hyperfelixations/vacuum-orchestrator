"""Actions select every active room with `all_rooms` for jobs and templates."""

from __future__ import annotations

from types import MappingProxyType

import probatio
import pytest
from homeassistant.components.vacuum.const import VacuumEntityFeature
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuum_orchestrator.api.actions import async_setup_actions
from custom_components.vacuum_orchestrator.api.job_input import CREATE_SCHEMA
from custom_components.vacuum_orchestrator.const import (
    CONF_ADAPTER,
    CONF_INSTALLATION_ID,
    CONF_ROBOT_ENTITY_ID,
    CONF_ROBOT_REGISTRY_ID,
    CONF_TARGET_AREAS,
    DOMAIN,
)
from custom_components.vacuum_orchestrator.runtime import (
    async_setup_orchestrator,
    async_unload_orchestrator,
)
from tests.errors import raises_code
from tests.test_runtime import MemoryBackend


def test_schema_takes_areas_and_rooms_as_lists_and_all_rooms_as_flag() -> None:
    selection = CREATE_SCHEMA({"areas": "kitchen", "rooms": "attic"})
    assert (selection["areas"], selection["rooms"]) == (["kitchen"], ["attic"])
    assert CREATE_SCHEMA({"all_rooms": "yes"})["all_rooms"] is True
    with pytest.raises(probatio.Invalid):
        CREATE_SCHEMA({"areas": [{"id": "kitchen"}]})


async def call(hass: HomeAssistant, action: str, **data: object) -> dict:
    response = await hass.services.async_call(
        DOMAIN, action, data, blocking=True, return_response=True
    )
    assert response is not None
    return dict(response)


async def test_jobs_and_templates_can_target_all_rooms(
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
    kitchen = ar.async_get(hass).async_create("Kitchen")
    cellar = ar.async_get(hass).async_create("Cellar")
    registry.async_update_entity_options(
        vacuum.entity_id, "vacuum", {"area_mapping": {kitchen.id: ["16"]}}
    )
    hass.states.async_set(
        vacuum.entity_id,
        "docked",
        {"supported_features": int(VacuumEntityFeature.CLEAN_AREA)},
    )
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
                    CONF_TARGET_AREAS: [kitchen.id],
                    "fixed_mode": "vacuum",
                }
            ),
            subentry_type="robot",
            title="Robot",
            unique_id=vacuum.id,
        ),
    )
    await async_setup_actions(hass)
    await async_setup_orchestrator(hass, entry)
    await hass.async_block_till_done()

    job = await call(hass, "create_job", all_rooms=True, mode="vacuum")
    detail = await call(hass, "get_job", job_id=job["job_id"])
    assert (detail["areas"], detail["all_rooms"]) == ([kitchen.id, cellar.id], True)
    await call(hass, "update_job", job_id=job["job_id"], areas=[kitchen.id])
    assert not (await call(hass, "get_job", job_id=job["job_id"]))["all_rooms"]
    await call(hass, "update_job", job_id=job["job_id"], all_rooms=True)
    assert (await call(hass, "get_job", job_id=job["job_id"]))["all_rooms"]
    from_job = await call(
        hass, "save_job_as_template", job_id=job["job_id"], name="From job"
    )
    template = await call(
        hass,
        "save_template",
        name="All",
        intent={"all_rooms": True, "mode": "vacuum"},
    )
    templates = {
        item["template_id"]: item
        for item in (await call(hass, "get_templates"))["templates"]
    }
    for saved in (template, from_job):
        intent = templates[saved["template_id"]]["intent"]
        assert (intent["areas"], intent["all_rooms"]) == ([], True)

    rooms = (await call(hass, "get_job", job_id=job["job_id"]))["room_ids"]
    await call(hass, "delete_job", job_id=job["job_id"])
    for room in rooms:
        await call(hass, "disable_room", room_id=room)
    with raises_code("no_active_rooms"):
        await call(hass, "create_job", all_rooms=True, mode="vacuum")
    await async_unload_orchestrator(hass, entry)
