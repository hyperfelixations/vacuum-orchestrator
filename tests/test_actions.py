"""Tests for the user-friendly Home Assistant action boundary."""

from copy import deepcopy
from typing import Any

import pytest
import voluptuous as vol
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import area_registry as ar
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuum_orchestrator.api.actions import (
    ATTR_AREAS,
    ATTR_JOB_ID,
    ATTR_MODE,
    ATTR_MOP_INTENSITY,
    ATTR_MOP_ROUTE,
    ATTR_PASSES,
    ATTR_REQUIRED_OFF,
    ATTR_REQUIRED_ON,
    ATTR_SETTINGS_POLICY,
    ATTR_VACUUM_POWER,
)
from custom_components.vacuum_orchestrator.const import (
    CONF_INSTALLATION_ID,
    DOMAIN,
    SERVICE_CANCEL_JOB,
    SERVICE_CREATE_JOB,
    SERVICE_DELETE_JOB,
    SERVICE_GET_JOB,
    SERVICE_GET_QUEUE,
    SERVICE_MOVE_JOB,
    SERVICE_PAUSE_QUEUE,
    SERVICE_RESUME_QUEUE,
    SERVICE_RETRY_JOB,
    SERVICE_RUN_QUEUE,
    SERVICE_START_JOB,
    SERVICE_UPDATE_JOB,
)
from custom_components.vacuum_orchestrator.infrastructure.integrity import JsonObject
from tests.errors import raises_code


@pytest.fixture(autouse=True)
def registered_areas(hass: HomeAssistant) -> None:
    for name in ("Kitchen", "Hall"):
        ar.async_get(hass).async_create(name)


class MemoryBackend:
    atomic_writes = True

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.data: JsonObject | None = None

    async def async_load_raw(self) -> JsonObject | None:
        return deepcopy(self.data)

    async def async_save_raw(self, data: JsonObject) -> None:
        self.data = deepcopy(data)


async def test_actions_need_no_config_entry_or_revision_and_accept_mode_aliases(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "custom_components.vacuum_orchestrator.runtime.HomeAssistantSnapshotBackend",
        MemoryBackend,
    )
    assert await async_setup_component(hass, DOMAIN, {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_INSTALLATION_ID: DOMAIN},
        version=2,
        minor_version=0,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    created = await hass.services.async_call(
        DOMAIN,
        SERVICE_CREATE_JOB,
        {ATTR_AREAS: ["kitchen"], ATTR_MODE: "vac_then_mop", "note": "old"},
        blocking=True,
        return_response=True,
    )
    assert created is not None
    job_id = created[ATTR_JOB_ID]
    await hass.services.async_call(
        DOMAIN,
        SERVICE_UPDATE_JOB,
        {ATTR_JOB_ID: job_id, "note": "edited"},
        blocking=True,
    )
    queried = await hass.services.async_call(
        DOMAIN,
        SERVICE_GET_JOB,
        {ATTR_JOB_ID: job_id},
        blocking=True,
        return_response=True,
    )
    queue = await hass.services.async_call(
        DOMAIN,
        SERVICE_GET_QUEUE,
        {},
        blocking=True,
        return_response=True,
    )

    assert queried is not None and queue is not None
    assert queried[ATTR_MODE] == "vacuum_then_mop"
    assert queried["note"] == "edited"
    assert queue["jobs"][0][ATTR_JOB_ID] == job_id
    assert "config_entry_id" not in queue


async def test_queue_management_actions_are_simple_and_response_is_optional(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "custom_components.vacuum_orchestrator.runtime.HomeAssistantSnapshotBackend",
        MemoryBackend,
    )
    assert await async_setup_component(hass, DOMAIN, {})
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_INSTALLATION_ID: DOMAIN})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    first = await hass.services.async_call(
        DOMAIN,
        SERVICE_CREATE_JOB,
        {ATTR_AREAS: ["kitchen"], ATTR_MODE: "vac"},
        blocking=True,
        return_response=True,
    )
    second = await hass.services.async_call(
        DOMAIN,
        SERVICE_CREATE_JOB,
        {ATTR_AREAS: ["hall"], ATTR_MODE: "mop"},
        blocking=True,
        return_response=True,
    )
    assert first is not None and second is not None
    await hass.services.async_call(
        DOMAIN,
        SERVICE_MOVE_JOB,
        {ATTR_JOB_ID: second[ATTR_JOB_ID], "direction": "top"},
        blocking=True,
    )
    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_RUN_QUEUE,
        {},
        blocking=True,
        return_response=True,
    )
    await hass.services.async_call(DOMAIN, SERVICE_PAUSE_QUEUE, {}, blocking=True)
    await hass.services.async_call(
        DOMAIN,
        SERVICE_CANCEL_JOB,
        {ATTR_JOB_ID: first[ATTR_JOB_ID]},
        blocking=True,
    )
    retry = await hass.services.async_call(
        DOMAIN,
        SERVICE_RETRY_JOB,
        {ATTR_JOB_ID: first[ATTR_JOB_ID]},
        blocking=True,
        return_response=True,
    )
    assert retry is not None
    await hass.services.async_call(
        DOMAIN,
        SERVICE_DELETE_JOB,
        {ATTR_JOB_ID: retry[ATTR_JOB_ID]},
        blocking=True,
    )

    assert response == {"dispatched": 0, "robot_ids": []}


async def test_action_fields_support_complete_create_and_partial_clear(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "custom_components.vacuum_orchestrator.runtime.HomeAssistantSnapshotBackend",
        MemoryBackend,
    )
    assert await async_setup_component(hass, DOMAIN, {})
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_INSTALLATION_ID: DOMAIN})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    created = await hass.services.async_call(
        DOMAIN,
        SERVICE_CREATE_JOB,
        {
            ATTR_AREAS: ["kitchen"],
            ATTR_MODE: "vac_and_mop",
            "name": "Kitchen",
            ATTR_VACUUM_POWER: "high",
            ATTR_MOP_INTENSITY: "medium",
            ATTR_MOP_ROUTE: "deep",
            ATTR_PASSES: 2,
            "source": "automation",
            "reason": "dirty",
            "note": "before",
            "dedupe_key": "automatic|kitchen",
            ATTR_REQUIRED_ON: ["binary_sensor.door"],
            ATTR_REQUIRED_OFF: ["binary_sensor.person"],
            ATTR_SETTINGS_POLICY: "strict",
        },
        blocking=True,
        return_response=True,
    )
    assert created is not None
    job_id = created[ATTR_JOB_ID]
    updated = await hass.services.async_call(
        DOMAIN,
        SERVICE_UPDATE_JOB,
        {
            ATTR_JOB_ID: job_id,
            ATTR_AREAS: ["hall"],
            ATTR_MODE: "vacuum",
            "name": None,
            ATTR_VACUUM_POWER: None,
            ATTR_MOP_INTENSITY: None,
            ATTR_MOP_ROUTE: None,
            ATTR_PASSES: 1,
            "source": None,
            "reason": None,
            "note": None,
            "dedupe_key": None,
            ATTR_REQUIRED_ON: [],
            ATTR_REQUIRED_OFF: [],
            ATTR_SETTINGS_POLICY: "best_effort",
        },
        blocking=True,
        return_response=True,
    )
    await hass.services.async_call(
        DOMAIN, SERVICE_RESUME_QUEUE, {}, blocking=True, return_response=False
    )
    queried = await hass.services.async_call(
        DOMAIN,
        SERVICE_GET_JOB,
        {ATTR_JOB_ID: job_id},
        blocking=True,
        return_response=True,
    )

    assert updated == {ATTR_JOB_ID: job_id}
    assert queried is not None
    assert queried[ATTR_AREAS] == ["hall"]
    assert queried[ATTR_VACUUM_POWER] is None
    assert queried["source"] is None


async def test_action_validation_permissions_and_unloaded_runtime_are_clear(
    hass: HomeAssistant,
    enable_custom_integrations: None,
) -> None:
    assert await async_setup_component(hass, DOMAIN, {})

    with raises_code("orchestrator_not_loaded"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_CREATE_JOB,
            {ATTR_AREAS: ["kitchen"], ATTR_MODE: "vacuum"},
            blocking=True,
            return_response=True,
        )
    with pytest.raises(vol.Invalid, match="invalid_cleaning_mode"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_CREATE_JOB,
            {ATTR_AREAS: ["kitchen"], ATTR_MODE: "polish"},
            blocking=True,
            return_response=True,
        )


async def test_non_admin_commands_and_unknown_queries_are_rejected(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    monkeypatch: pytest.MonkeyPatch,
    hass_read_only_user: Any,
) -> None:
    monkeypatch.setattr(
        "custom_components.vacuum_orchestrator.runtime.HomeAssistantSnapshotBackend",
        MemoryBackend,
    )
    assert await async_setup_component(hass, DOMAIN, {})
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_INSTALLATION_ID: DOMAIN})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    with pytest.raises(Unauthorized):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_CREATE_JOB,
            {ATTR_AREAS: ["kitchen"], ATTR_MODE: "vacuum"},
            blocking=True,
            return_response=True,
            context=Context(user_id=hass_read_only_user.id),
        )
    with raises_code("unknown_job"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_GET_JOB,
            {ATTR_JOB_ID: "missing"},
            blocking=True,
            return_response=True,
            context=Context(user_id=hass_read_only_user.id),
        )
    with raises_code("job_blocked"):
        created = await hass.services.async_call(
            DOMAIN,
            SERVICE_CREATE_JOB,
            {ATTR_AREAS: ["kitchen"], ATTR_MODE: "vacuum"},
            blocking=True,
            return_response=True,
        )
        assert created is not None
        await hass.services.async_call(
            DOMAIN,
            SERVICE_START_JOB,
            {ATTR_JOB_ID: created[ATTR_JOB_ID]},
            blocking=True,
            return_response=True,
        )
