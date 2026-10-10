"""A robot is named as Home Assistant names its vacuum; VOI may override it."""

from __future__ import annotations

from typing import Any, cast

import pytest
from homeassistant.components.vacuum.const import VacuumEntityFeature
from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vacuum_orchestrator.adapters.discovery import (
    discover_robots,
    ha_robot_name,
)
from custom_components.vacuum_orchestrator.api.websocket import (
    TYPE_SUBSCRIBE,
    websocket_subscribe,
)
from custom_components.vacuum_orchestrator.runtime import async_get_runtime
from tests.errors import raises_code
from tests.test_configuration_api import add_robot, call, configured  # noqa: F401
from tests.test_websocket import Connection


def _vacuum(hass: HomeAssistant, device_name: str = "Saugi") -> er.RegistryEntry:
    """Register a vacuum named like Roborock's: by its device only."""
    owner = MockConfigEntry(domain="demo")
    owner.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=owner.entry_id,
        identifiers={("demo", device_name)},
        name=device_name,
    )
    vacuum = er.async_get(hass).async_get_or_create(
        "vacuum",
        "demo",
        device_name,
        config_entry=owner,
        device_id=device.id,
        has_entity_name=True,
        original_name=None,
    )
    hass.states.async_set(
        vacuum.entity_id,
        "docked",
        {"supported_features": int(VacuumEntityFeature.CLEAN_AREA)},
    )
    return vacuum


async def _robot(hass: HomeAssistant, vacuum: er.RegistryEntry) -> dict[str, Any]:
    await add_robot(hass, vacuum.entity_id, fixed_mode="vacuum")
    await hass.async_block_till_done()
    (robot,) = (await call(hass, "get_robots"))["robots"]
    return cast(dict[str, Any], robot)


def _entry(hass: HomeAssistant) -> ConfigEntry:
    controller = async_get_runtime(hass).controller
    assert controller is not None
    return controller.entry


def _title(hass: HomeAssistant, robot_id: str) -> str:
    return _entry(hass).subentries[robot_id].title


@pytest.mark.usefixtures("configured")
async def test_the_name_is_the_vacuum_name_home_assistant_shows(
    hass: HomeAssistant,
) -> None:
    vacuum = _vacuum(hass)
    area = ar.async_get(hass).async_create("Kitchen")
    assert vacuum.device_id is not None
    dr.async_get(hass).async_update_device(vacuum.device_id, area_id=area.id)
    entry = er.async_get(hass).async_get(vacuum.entity_id)
    assert entry is not None

    assert ha_robot_name(hass, entry) == "Saugi"
    assert [item.name for item in discover_robots(hass)] == ["Saugi"]
    robot = await _robot(hass, vacuum)
    assert (robot["name"], robot["name_source"], robot["ha_name"]) == (
        "Saugi",
        "home_assistant",
        "Saugi",
    )
    assert _title(hass, robot["robot_id"]) == "Saugi"

    er.async_get(hass).async_update_entity(vacuum.entity_id, name="Wischi")
    entry = er.async_get(hass).async_get(vacuum.entity_id)
    assert entry is not None and ha_robot_name(hass, entry) == "Wischi"


@pytest.mark.usefixtures("configured")
async def test_renaming_in_home_assistant_renames_without_rebuilding(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    vacuum = _vacuum(hass)
    robot_id = (await _robot(hass, vacuum))["robot_id"]
    controller = async_get_runtime(hass).controller
    assert controller is not None
    rebuilds: list[object] = []
    rebuild = controller._async_rebuild_adapters

    async def counted(active: frozenset[str]) -> None:
        rebuilds.append(active)
        await rebuild(active)

    monkeypatch.setattr(controller, "_async_rebuild_adapters", counted)
    subscriber = Connection()
    websocket_subscribe(
        hass, cast(ActiveConnection, subscriber), {"id": 1, "type": TYPE_SUBSCRIBE}
    )
    await hass.async_block_till_done()
    before = len(subscriber.events)

    assert vacuum.device_id is not None
    dr.async_get(hass).async_update_device(vacuum.device_id, name_by_user="Flitzer")
    await hass.async_block_till_done()

    (robot,) = (await call(hass, "get_robots"))["robots"]
    assert (robot["name"], robot["ha_name"]) == ("Flitzer", "Flitzer")
    assert _title(hass, robot_id) == "Flitzer"
    assert rebuilds == []
    changed = set().union(
        *(event["changed"] for _id, event in subscriber.events[before:])
    )
    assert "robots" in changed

    settled = len(subscriber.events)
    await hass.async_block_till_done()
    assert len(subscriber.events) == settled


@pytest.mark.usefixtures("configured")
async def test_a_custom_name_overrides_home_assistant_until_reset(
    hass: HomeAssistant,
) -> None:
    vacuum = _vacuum(hass)
    robot_id = (await _robot(hass, vacuum))["robot_id"]

    renamed = await call(hass, "rename_robot", robot_id=robot_id, name=" Küchenheld ")
    await hass.async_block_till_done()
    assert renamed["robot_id"] == robot_id
    (robot,) = (await call(hass, "get_robots"))["robots"]
    assert (robot["name"], robot["name_source"], robot["ha_name"]) == (
        "Küchenheld",
        "custom",
        "Saugi",
    )
    assert _title(hass, robot_id) == "Küchenheld"

    assert vacuum.device_id is not None
    dr.async_get(hass).async_update_device(vacuum.device_id, name_by_user="Flitzer")
    await hass.async_block_till_done()
    (robot,) = (await call(hass, "get_robots"))["robots"]
    assert (robot["name"], robot["ha_name"]) == ("Küchenheld", "Flitzer")

    await call(hass, "rename_robot", robot_id=robot_id, name=None)
    await hass.async_block_till_done()
    (robot,) = (await call(hass, "get_robots"))["robots"]
    assert (robot["name"], robot["name_source"]) == ("Flitzer", "home_assistant")
    assert _title(hass, robot_id) == "Flitzer"

    with raises_code("invalid_robot_name"):
        await call(hass, "rename_robot", robot_id=robot_id, name="   ")
    with raises_code("unknown_robot"):
        await call(hass, "rename_robot", robot_id="missing", name="Robo")


@pytest.mark.usefixtures("configured")
async def test_renaming_the_robot_entry_in_home_assistant_sets_the_override(
    hass: HomeAssistant,
) -> None:
    vacuum = _vacuum(hass)
    robot_id = (await _robot(hass, vacuum))["robot_id"]
    entry = _entry(hass)

    hass.config_entries.async_update_subentry(
        entry, entry.subentries[robot_id], title="Flurputzer"
    )
    await hass.async_block_till_done()
    (robot,) = (await call(hass, "get_robots"))["robots"]
    assert (robot["name"], robot["name_source"]) == ("Flurputzer", "custom")
    assert entry.subentries[robot_id].data["name"] == "Flurputzer"

    hass.config_entries.async_update_subentry(
        entry, entry.subentries[robot_id], title="Saugi"
    )
    await hass.async_block_till_done()
    (robot,) = (await call(hass, "get_robots"))["robots"]
    assert (robot["name"], robot["name_source"]) == ("Saugi", "home_assistant")
    assert entry.subentries[robot_id].data.get("name") is None


@pytest.mark.usefixtures("configured")
async def test_renaming_an_area_still_renames_its_room(hass: HomeAssistant) -> None:
    area = ar.async_get(hass).async_create("Kitchen")
    await hass.async_block_till_done()
    rooms = (await call(hass, "get_rooms"))["rooms"]
    assert [item["name"] for item in rooms] == ["Kitchen"]

    ar.async_get(hass).async_update(area.id, name="Küche")
    await hass.async_block_till_done()

    rooms = (await call(hass, "get_rooms"))["rooms"]
    assert [item["name"] for item in rooms] == ["Küche"]


@pytest.mark.usefixtures("configured")
async def test_a_robot_added_with_a_name_keeps_it_and_its_last_name_after_removal(
    hass: HomeAssistant,
) -> None:
    vacuum = _vacuum(hass)
    await call(hass, "add_robot", entity_id=vacuum.entity_id, name=" Robi ")
    await hass.async_block_till_done()
    (robot,) = (await call(hass, "get_robots"))["robots"]
    assert (robot["name"], robot["name_source"]) == ("Robi", "custom")
    robot_id = robot["robot_id"]
    assert _title(hass, robot_id) == "Robi"

    updates: list[object] = []
    update = hass.config_entries.async_update_subentry

    def counted(*args: Any, **kwargs: Any) -> bool:
        updates.append(kwargs)
        return update(*args, **kwargs)

    hass.config_entries.async_update_subentry = counted  # type: ignore[method-assign]
    try:
        await call(hass, "rename_robot", robot_id=robot_id, name="Robi")
        await hass.async_block_till_done()
    finally:
        hass.config_entries.async_update_subentry = update  # type: ignore[method-assign]
    assert updates == []

    await call(hass, "rename_robot", robot_id=robot_id)
    await hass.async_block_till_done()
    er.async_get(hass).async_remove(vacuum.entity_id)
    await hass.async_block_till_done()
    (robot,) = (await call(hass, "get_robots"))["robots"]
    assert (robot["name"], robot["name_source"], robot["ha_name"]) == (
        "Saugi",
        "home_assistant",
        None,
    )
