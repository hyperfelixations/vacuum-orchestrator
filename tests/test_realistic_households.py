"""VOI in realistic homes: discovery, rooms, reach, maps and a full run.

The homes come from `tests/realistic`; see dev doc "Testarchitektur".
"""

from collections.abc import Awaitable, Callable
from datetime import timedelta

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.vacuum_orchestrator.const import CONF_INSTALLATION_ID, DOMAIN
from custom_components.vacuum_orchestrator.domain.types import OperationKind
from custom_components.vacuum_orchestrator.runtime import async_get_runtime
from tests.realistic import (
    Household,
    busy,
    generic_area,
    no_robot,
    single_roborock,
    two_robots,
)
from tests.realistic.roborock import ROLES, Step, mop_run, vacuum_run
from tests.test_configuration_api import call
from tests.test_runtime import MemoryBackend


@pytest.fixture
def install(
    hass: HomeAssistant, enable_custom_integrations: None, monkeypatch
) -> Callable[[Callable[[HomeAssistant], Household]], object]:
    async def setup(build: Callable[[HomeAssistant], Household]) -> Household:
        MemoryBackend.data = None
        monkeypatch.setattr(
            "custom_components.vacuum_orchestrator.runtime.HomeAssistantSnapshotBackend",
            MemoryBackend,
        )
        home = build(hass)
        assert await async_setup_component(hass, DOMAIN, {})
        entry = MockConfigEntry(domain=DOMAIN, data={CONF_INSTALLATION_ID: DOMAIN})
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        return home

    return setup


async def _rooms(hass: HomeAssistant, home: Household) -> dict[str, dict]:
    """VOI rooms by area name."""
    names = {area_id: name for name, area_id in home.areas.items()}
    return {
        names[room["area_id"]]: room
        for room in (await call(hass, "get_rooms"))["rooms"]
    }


async def _robots(hass: HomeAssistant) -> dict[str, dict]:
    return {
        robot["name"]: robot for robot in (await call(hass, "get_robots"))["robots"]
    }


def _reach(robot: dict, rooms: dict[str, dict]) -> dict[str, tuple[str, list, list]]:
    names = {room["room_id"]: name for name, room in rooms.items()}
    return {
        names[item["room_id"]]: (item["status"], item["targets"], item["ignored"])
        for item in robot["reach"]
    }


async def test_entities_carry_the_names_home_assistant_gives_them(
    hass: HomeAssistant, install
) -> None:
    """Device name and entity name, as HA forms `friendly_name` for entities
    that have an entity name; a vacuum without one carries the device name."""
    home = await install(two_robots)
    rocky, dusty = home.roborock, home.generic
    dock_role = next(role for role in ROLES if role.dock)

    def name(entity_id: str) -> str:
        return hass.states.get(entity_id).attributes["friendly_name"]

    assert name(rocky.entity_id) == "Rocky"
    assert name(rocky.entities["battery"].entity_id) == "Rocky Battery"
    assert (
        name(rocky.entities[dock_role.key].entity_id) == f"Rocky Dock {dock_role.name}"
    )
    assert name(dusty.entity_id) == "Dusty"
    rocky.set("battery", "50")
    rocky.set_vacuum("cleaning")
    dusty.set_vacuum("cleaning")
    assert [
        name(rocky.entities["battery"].entity_id),
        name(rocky.entity_id),
        name(dusty.entity_id),
    ] == ["Rocky Battery", "Rocky", "Dusty"]


async def test_a_home_without_robots_imports_its_areas_as_locked_rooms(
    hass: HomeAssistant, install
) -> None:
    home = await install(no_robot)

    rooms = await _rooms(hass, home)
    assert sorted(rooms) == sorted(home.areas)
    assert not any(room["released"] for room in rooms.values())
    assert await _robots(hass) == {}
    assert (await call(hass, "get_setup"))["assistant_pending"]


async def test_a_roborock_is_found_with_its_dock_roles_maps_and_reach(
    hass: HomeAssistant, install
) -> None:
    home = await install(single_roborock)

    (rocky,) = (await _robots(hass)).values()
    assert (rocky["name"], rocky["name_source"]) == ("Rocky", "home_assistant")
    configuration = rocky["configuration"]
    assert (configuration["adapter"], configuration["protocol"]) == (
        "roborock",
        "roborock_v1",
    )
    assert configuration["source_robot_id"].startswith("physical:")
    (candidate,) = (await call(hass, "get_robot_candidates"))["candidates"]
    # Every companion entity, the dock's included, is bound to its own role.
    registry = er.async_get(hass)
    assert {
        registry.async_get(value).entity_id for value in candidate["roles"].values()
    } == {item.entity_id for item in home.roborock.entities.values()}
    assert len(candidate["roles"]) == len(ROLES)
    assert not candidate["ambiguous_roles"]
    assert _reach(rocky, await _rooms(hass, home)) == {
        "Dining Room": ("reachable", ["0_16"], []),
        "Studio": ("reachable", ["0_17"], []),
        "Library": ("reachable", ["0_18"], ["0_99"]),
        "Conservatory": ("not_on_current_map", [], ["1_16"]),
    }
    assert [
        (item["name"], item["current"], item["image_entity_id"])
        for item in rocky["maps"]
    ] == [
        ("Main Floor", True, "image.rocky_main_floor"),
        ("Lower Floor", False, "image.rocky_lower_floor"),
    ]
    assert rocky["activity"] == {"phase": "docked", "external": False}


async def test_a_generic_area_vacuum_cleans_the_areas_mapped_to_it(
    hass: HomeAssistant, install
) -> None:
    home = await install(generic_area)

    (flitzi,) = (await _robots(hass)).values()
    assert (flitzi["name"], flitzi["configuration"]["adapter"]) == (
        "Dusty",
        "home_assistant",
    )
    reach = _reach(flitzi, await _rooms(hass, home))
    assert {name: status for name, (status, _, _) in reach.items()} == {
        "Dining Room": "reachable",
        "Studio": "reachable",
        "Library": "reachable",
        "Conservatory": "area_not_mapped",
    }


async def test_two_robots_are_two_profiles_with_their_own_reach(
    hass: HomeAssistant, install
) -> None:
    home = await install(two_robots)

    robots = await _robots(hass)
    assert sorted(robots) == ["Dusty", "Rocky"]
    rooms = await _rooms(hass, home)
    assert _reach(robots["Dusty"], rooms)["Conservatory"][0] == "area_not_mapped"
    assert _reach(robots["Rocky"], rooms)["Conservatory"][0] == "not_on_current_map"


async def test_a_run_from_the_vendor_app_is_shown_as_external(
    hass: HomeAssistant, install
) -> None:
    await install(busy)

    (rocky,) = (await _robots(hass)).values()
    assert rocky["activity"] == {"phase": "cleaning", "external": True}


async def test_a_job_cleans_the_segments_of_its_areas_and_completes(
    hass: HomeAssistant, install, freezer: FrozenDateTimeFactory
) -> None:
    home = await install(single_roborock)
    robot = home.roborock
    areas = [home.areas["Dining Room"], home.areas["Studio"]]

    await call(hass, "release_room", areas=areas, kind="permanent")
    job = (await call(hass, "create_job", areas=areas, mode="vacuum", start=True))[
        "job_id"
    ]
    await hass.async_block_till_done()
    assert robot.segment_commands() == [{"segments": [16, 17], "repeat": 1}]

    advance = _advancer(hass, freezer)
    robot.clean()
    await advance(5)
    assert (await call(hass, "get_job", job_id=job))["state"] == "running"
    robot.return_home()
    await advance(600)
    robot.dock_after_run()
    await advance(60)
    detail = await call(hass, "get_job", job_id=job)
    assert detail["state"] == "completed"
    quality = detail["completion"]["quality"]
    assert quality in {"derived", "confirmed"}
    registry = async_get_runtime(hass).orchestrator.rooms.registry

    def cleaned(name: str) -> str | None:
        stamp = registry.resolve(home.areas[name]).last_cleaning.get(
            OperationKind.VACUUM
        )
        return stamp.quality.value if stamp else None

    assert {name: cleaned(name) for name in ("Dining Room", "Studio", "Library")} == {
        "Dining Room": quality,
        "Studio": quality,
        "Library": None,
    }


async def test_a_cleaning_roborock_reports_the_room_it_was_sent_to(
    hass: HomeAssistant, install
) -> None:
    home = await install(single_roborock)
    robot = home.roborock
    areas = [home.areas["Studio"]]

    await call(hass, "release_room", areas=areas, kind="permanent")
    await call(hass, "create_job", areas=areas, mode="vacuum", start=True)
    await hass.async_block_till_done()
    robot.clean()

    current = hass.states.get(robot.entities["current_room"].entity_id)
    assert current is not None
    assert current.state == "Studio"


def _advancer(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> Callable[[float], Awaitable[None]]:
    async def advance(seconds: float) -> None:
        # VOI observes the new state first, then time passes.
        await hass.async_block_till_done()
        freezer.tick(timedelta(seconds=seconds))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    return advance


@pytest.mark.parametrize(
    ("mode", "script", "attention"),
    [
        pytest.param("vacuum", vacuum_run(2), [], id="vacuum"),
        pytest.param("mop", mop_run(3), [], id="mop with washes between sections"),
        pytest.param("mop", mop_run(1, prewash=240), [], id="mop after a long wash"),
        # The robot cleans on while the dock lacks water; the dock's error names
        # the clean water tank fact (EXE-17). See dev doc "Gerätefehler".
        pytest.param(
            "mop",
            mop_run(3, empty_tank="during"),
            ["clean_water_empty"],
            id="mop while the dock runs out of water",
        ),
        pytest.param(
            "mop",
            mop_run(1, empty_tank="after"),
            ["clean_water_empty"],
            id="mop until the last wash empties the dock",
        ),
    ],
)
async def test_a_typical_run_keeps_its_job_running_and_completes_once(
    hass: HomeAssistant,
    install,
    freezer: FrozenDateTimeFactory,
    mode: str,
    script: tuple[Step, ...],
    attention: list[str],
) -> None:
    """Washes, dock faults, the run flag and the dock's last report are no end."""
    home = await install(single_roborock)
    robot = home.roborock
    areas = [home.areas["Dining Room"], home.areas["Studio"]]
    await call(hass, "release_room", areas=areas, kind="permanent")
    job = (await call(hass, "create_job", areas=areas, mode=mode, start=True))["job_id"]
    await hass.async_block_till_done()
    assert robot.segment_commands() == [{"segments": [16, 17], "repeat": 1}]
    advance = _advancer(hass, freezer)
    states: list[str] = []

    async def watch(seconds: float) -> None:
        await advance(seconds)
        states.append((await call(hass, "get_job", job_id=job))["state"])

    await robot.play(script, watch)
    await advance(60)

    assert set(states) == {"running"}
    detail = await call(hass, "get_job", job_id=job)
    assert (detail["state"], detail["completion"]["notes"]) == ("completed", [])
    (rocky,) = (await _robots(hass)).values()
    assert rocky["activity"] == {"phase": "docked", "external": False}
    # The fault stays visible until someone refills the dock.
    assert [
        (entry["kind"], entry["codes"])
        for entry in (await call(hass, "get_queue"))["attention"]
    ] == [("device_fault", attention)] * bool(attention)
