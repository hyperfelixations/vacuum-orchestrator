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

    (saugi,) = (await _robots(hass)).values()
    assert (saugi["name"], saugi["name_source"]) == ("Saugi", "home_assistant")
    configuration = saugi["configuration"]
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
    assert _reach(saugi, await _rooms(hass, home)) == {
        "Küche": ("reachable", ["0_16"], []),
        "Flur": ("reachable", ["0_17"], []),
        "Wohnzimmer": ("reachable", ["0_18"], ["0_99"]),
        "Bad": ("not_on_current_map", [], ["1_16"]),
    }
    assert [
        (item["name"], item["current"], item["image_entity_id"])
        for item in saugi["maps"]
    ] == [
        ("Erdgeschoss", True, "image.saugi_erdgeschoss"),
        ("Obergeschoss", False, "image.saugi_obergeschoss"),
    ]
    assert saugi["activity"] == {"phase": "docked", "external": False}


async def test_a_generic_area_vacuum_cleans_the_areas_mapped_to_it(
    hass: HomeAssistant, install
) -> None:
    home = await install(generic_area)

    (flitzi,) = (await _robots(hass)).values()
    assert (flitzi["name"], flitzi["configuration"]["adapter"]) == (
        "Flitzi",
        "home_assistant",
    )
    reach = _reach(flitzi, await _rooms(hass, home))
    assert {name: status for name, (status, _, _) in reach.items()} == {
        "Küche": "reachable",
        "Flur": "reachable",
        "Wohnzimmer": "reachable",
        "Bad": "area_not_mapped",
    }


async def test_two_robots_are_two_profiles_with_their_own_reach(
    hass: HomeAssistant, install
) -> None:
    home = await install(two_robots)

    robots = await _robots(hass)
    assert sorted(robots) == ["Flitzi", "Saugi"]
    rooms = await _rooms(hass, home)
    assert _reach(robots["Flitzi"], rooms)["Bad"][0] == "area_not_mapped"
    assert _reach(robots["Saugi"], rooms)["Bad"][0] == "not_on_current_map"


async def test_a_run_from_the_vendor_app_is_shown_as_external(
    hass: HomeAssistant, install
) -> None:
    await install(busy)

    (saugi,) = (await _robots(hass)).values()
    assert saugi["activity"] == {"phase": "cleaning", "external": True}


async def test_a_job_cleans_the_segments_of_its_areas_and_completes(
    hass: HomeAssistant, install, freezer: FrozenDateTimeFactory
) -> None:
    home = await install(single_roborock)
    robot = home.roborock
    areas = [home.areas["Küche"], home.areas["Flur"]]

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

    assert {name: cleaned(name) for name in ("Küche", "Flur", "Wohnzimmer")} == {
        "Küche": quality,
        "Flur": quality,
        "Wohnzimmer": None,
    }


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
        pytest.param("mop", mop_run(1, prewash=220), [], id="mop after a long wash"),
        # The robot cleans on while the dock lacks water; see dev doc
        # "Gerätefehler".
        pytest.param(
            "mop",
            mop_run(3, empty_tank="during"),
            ["water_empty"],
            id="mop while the dock runs out of water",
        ),
        pytest.param(
            "mop",
            mop_run(1, empty_tank="after"),
            ["water_empty"],
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
    areas = [home.areas["Küche"], home.areas["Flur"]]
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
    (saugi,) = (await _robots(hass)).values()
    assert saugi["activity"] == {"phase": "docked", "external": False}
    # The fault stays visible until someone refills the dock.
    assert [
        (entry["kind"], entry["codes"])
        for entry in (await call(hass, "get_queue"))["attention"]
    ] == [("device_fault", attention)] * bool(attention)
