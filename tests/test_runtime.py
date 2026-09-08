"""Tests for single-runtime composition and physical ownership."""

from copy import deepcopy

import pytest
from homeassistant.components.vacuum.const import VacuumEntityFeature
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuum_orchestrator.const import (
    ADAPTER_ROBOROCK,
    CONF_ADAPTER,
    CONF_INSTALLATION_ID,
    CONF_ROBOT_ENTITY_ID,
    CONF_ROBOT_REGISTRY_ID,
    CONF_TARGET_AREAS,
    DOMAIN,
    SUBENTRY_TYPE_ROBOT,
)
from custom_components.vacuum_orchestrator.domain.errors import ConflictError
from custom_components.vacuum_orchestrator.infrastructure.integrity import JsonObject
from custom_components.vacuum_orchestrator.runtime import (
    async_get_registry,
    async_get_runtime,
    async_setup_orchestrator,
    async_unload_orchestrator,
)


class MemoryBackend:
    atomic_writes = True
    data: JsonObject | None = None

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    async def async_load_raw(self) -> JsonObject | None:
        return deepcopy(self.data)

    async def async_save_raw(self, data: JsonObject) -> None:
        type(self).data = deepcopy(data)


def _entry(installation_id: str = DOMAIN) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={CONF_INSTALLATION_ID: installation_id},
        subentries_data=[
            {
                "subentry_type": SUBENTRY_TYPE_ROBOT,
                "title": "Roborock",
                "unique_id": "registry",
                "data": {
                    CONF_ROBOT_ENTITY_ID: "vacuum.roborock",
                    CONF_ROBOT_REGISTRY_ID: "registry",
                    CONF_ADAPTER: ADAPTER_ROBOROCK,
                    CONF_TARGET_AREAS: ["kitchen"],
                },
            }
        ],
    )


async def test_runtime_composes_one_global_owner_and_releases_it(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    MemoryBackend.data = None
    monkeypatch.setattr(
        "custom_components.vacuum_orchestrator.runtime.HomeAssistantSnapshotBackend",
        MemoryBackend,
    )
    hass.states.async_set(
        "vacuum.roborock",
        "docked",
        {"supported_features": int(VacuumEntityFeature.CLEAN_AREA)},
    )
    first = _entry()
    second = _entry("other")

    assert await async_setup_orchestrator(hass, first)
    assert async_get_runtime(hass) is async_get_registry(hass)[first.entry_id]
    assert async_get_runtime(hass).orchestrator.state.installation_id == DOMAIN
    with pytest.raises(ConflictError, match="source_robot_already_owned"):
        await async_setup_orchestrator(hass, second)

    assert await async_unload_orchestrator(hass, first)
    MemoryBackend.data = None
    assert await async_setup_orchestrator(hass, second)
    assert await async_unload_orchestrator(hass, second)
    assert await async_unload_orchestrator(hass, second)


def test_runtime_resolution_rejects_zero_or_multiple_entries(
    hass: HomeAssistant,
) -> None:
    with pytest.raises(ConflictError, match="not_loaded"):
        async_get_runtime(hass)
    registry = async_get_registry(hass)
    registry["one"] = object()  # type: ignore[assignment]
    registry["two"] = object()  # type: ignore[assignment]
    with pytest.raises(ConflictError, match="multiple"):
        async_get_runtime(hass)
