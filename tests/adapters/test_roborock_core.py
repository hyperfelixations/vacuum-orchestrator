"""The adapter against core's Roborock integration, running core's own methods.

Core's entities run with a minimal stand-in for their coordinator, so what VOI
reads and sends is checked against what core answers and does, not against a
copy of it. See dev doc "Testarchitektur" (core methods).
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import homeassistant.components
import pytest
from homeassistant.components.roborock.sensor import SENSOR_DESCRIPTIONS
from homeassistant.components.roborock.vacuum import RoborockQ10Vacuum, RoborockVacuum
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceNotSupported
from roborock.data.containers import CombinedMapInfo, NamedRoomMapping

from custom_components.vacuum_orchestrator.adapters.roborock_faults import (
    DOCK_FAULTS,
    DOCK_NEUTRAL,
    ROBOT_FAULTS,
    ROBOT_NEUTRAL,
)
from tests.adapters.dispatching import dispatch
from tests.adapters.test_roborock import assignment, setup_robot, unit

ROOT = Path(__file__).parents[2]
CORE = Path(homeassistant.components.__file__).parent / "roborock"


def _maps() -> dict[int, CombinedMapInfo]:
    """Two maps as core's home trait holds them; one room has no name."""
    return {
        0: CombinedMapInfo(
            map_flag=0,
            name="Ground",
            rooms=[
                NamedRoomMapping(segment_id=16, iot_id="a", raw_name="Library"),
                NamedRoomMapping(segment_id=17, iot_id="b", raw_name=None),
            ],
        ),
        1: CombinedMapInfo(
            map_flag=1,
            name="Upper",
            rooms=[NamedRoomMapping(segment_id=16, iot_id="c", raw_name="Studio")],
        ),
    }


def _vacuum(current_map: int = 0) -> RoborockVacuum:
    """Core's V1 vacuum entity without a cloud connection behind it."""
    entity = object.__new__(RoborockVacuum)
    home = SimpleNamespace(home_map_info=_maps())
    entity.coordinator = SimpleNamespace(properties_api=SimpleNamespace(home=home))
    entity._home_trait = home
    entity._maps_trait = SimpleNamespace(current_map=current_map)
    entity.send = AsyncMock()
    return entity


def test_python_roborock_is_pinned_as_core_pins_it() -> None:
    manifest = json.loads((CORE / "manifest.json").read_text(encoding="utf-8"))
    pins = {
        line.split("#")[0].strip()
        for line in (ROOT / "requirements-test.txt")
        .read_text(encoding="utf-8")
        .splitlines()
    }
    assert set(manifest["requirements"]) <= pins


async def test_the_adapter_reads_the_map_inventory_core_answers(
    hass: HomeAssistant,
) -> None:
    response = await RoborockVacuum.get_maps(_vacuum())
    # Core keys rooms by integer segment, unlike a hand-written inventory.
    assert response["maps"][0]["rooms"] == {16: "Library", 17: "Room 17"}
    adapter, inventory, _ = setup_robot(hass)
    inventory.clear()
    inventory.update(response)
    await adapter.async_refresh_maps()
    maps = adapter.maps().maps
    assert [
        (
            item.map_id,
            item.name,
            item.current,
            [(s.segment_id, s.name) for s in item.segments],
        )
        for item in maps
    ] == [
        ("0", "Ground", True, [("16", "Library"), ("17", "Room 17")]),
        ("1", "Upper", False, [("16", "Studio")]),
    ]


async def test_core_segment_ids_are_the_targets_the_adapter_cleans(
    hass: HomeAssistant,
) -> None:
    """Area mappings store core's segment IDs; both sides send the same segments."""
    core = _vacuum(current_map=0)
    ids = [segment.id for segment in await RoborockVacuum.async_get_segments(core)]
    assert ids == ["0_16", "0_17", "1_16"]
    await RoborockVacuum.async_clean_segments(core, ids)
    (core_call,) = core.send.await_args_list
    adapter, inventory, calls = setup_robot(hass)
    inventory.clear()
    inventory.update(await RoborockVacuum.get_maps(core))
    await adapter.async_refresh_maps()
    work = unit()
    await dispatch(adapter, work, await assignment(adapter, work))
    (voi_call,) = calls
    assert voi_call.data["command"] == core_call.args[0]
    voi_segments = voi_call.data["params"][0]["segments"]
    assert voi_segments == core_call.args[1][0]["segments"] == [16, 17]
    # What VOI sends reaches core's send unchanged.
    sent = _vacuum()
    await RoborockVacuum.async_send_command(
        sent, voi_call.data["command"], voi_call.data["params"]
    )
    sent.send.assert_awaited_once_with("app_segment_clean", voi_call.data["params"])


async def test_newer_robots_answer_no_map_inventory() -> None:
    """Q10 robots have no inventory; VOI asks only V1 profiles for one."""
    entity = object.__new__(RoborockQ10Vacuum)
    entity.entity_id = "vacuum.q10"
    with pytest.raises(ServiceNotSupported):
        await RoborockQ10Vacuum.get_maps(entity)


@pytest.mark.parametrize(
    ("key", "table", "neutral"),
    [
        ("vacuum_error", ROBOT_FAULTS, ROBOT_NEUTRAL),
        ("dock_error", DOCK_FAULTS, DOCK_NEUTRAL),
    ],
)
def test_fault_tables_cover_the_options_core_reports(
    key: str, table: dict[str, object], neutral: frozenset[str]
) -> None:
    (description,) = [item for item in SENSOR_DESCRIPTIONS if item.key == key]
    assert set(table) | neutral == set(description.options or ())
