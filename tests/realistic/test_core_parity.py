"""The realistic Roborock registers what the installed core integration does.

The core modules need `python-roborock`, so this reads their source instead.
"""

import ast
import json
from pathlib import Path

import homeassistant.components

from tests.realistic.roborock import ROLES

CORE = Path(homeassistant.components.__file__).parent / "roborock"
# Description lists of V1 devices per platform.
LISTS = {
    "sensor": "SENSOR_DESCRIPTIONS",
    "binary_sensor": "BINARY_SENSOR_DESCRIPTIONS",
    "select": "SELECT_DESCRIPTIONS",
}
# Roles core creates with their own entity class: unique ID prefix and key.
CLASSES = {
    ("sensor", "current_room"): "current_room",
    ("select", "cleaning_mode"): "cleaning_mode",
    ("select", "selected_map"): "selected_map",
}


def _descriptions(platform: str) -> dict[str, dict[str, object]]:
    """Constant keywords of each V1 entity description, by key."""
    tree = ast.parse((CORE / f"{platform}.py").read_text(encoding="utf-8"))
    for node in tree.body:
        target = (
            node.target
            if isinstance(node, ast.AnnAssign)
            else node.targets[0]
            if isinstance(node, ast.Assign)
            else None
        )
        if isinstance(target, ast.Name) and target.id == LISTS[platform]:
            assert isinstance(node.value, ast.List)
            found: dict[str, dict[str, object]] = {}
            for item in node.value.elts:
                assert isinstance(item, ast.Call)
                values = {
                    keyword.arg: keyword.value.value
                    for keyword in item.keywords
                    if isinstance(keyword.value, ast.Constant)
                }
                # Keys given by a constant name are no role of VOI.
                if isinstance(key := values.get("key"), str):
                    found[key] = values
            return found
    raise AssertionError(LISTS[platform])


def test_roles_match_core_descriptions_names_and_unique_ids() -> None:
    strings = json.loads((CORE / "strings.json").read_text(encoding="utf-8"))
    for role in ROLES:
        if (role.domain, role.translation_key) in CLASSES:
            source = (CORE / f"{role.domain}.py").read_text(encoding="utf-8")
            assert f'_attr_translation_key = "{role.translation_key}"' in source
            assert f'f"{role.key}_{{coordinator.duid_slug}}"' in source
            assert not role.dock
        else:
            description = _descriptions(role.domain)[role.key]
            assert description.get("translation_key") == role.translation_key
            assert bool(description.get("is_dock_entity")) is role.dock
        if role.translation_key is not None:
            entity = strings["entity"][role.domain][role.translation_key]
            assert entity["name"] == role.name, role


def test_devices_maps_and_the_vacuum_are_named_like_core() -> None:
    coordinator = (CORE / "coordinator.py").read_text(encoding="utf-8")
    assert 'identifiers={(DOMAIN, f"{self.duid}_dock")}' in coordinator
    assert 'name=f"{self._device.device_info.name} Dock"' in coordinator
    image = (CORE / "image.py").read_text(encoding="utf-8")
    assert 'unique_id = f"{coordinator.duid_slug}_map_{map_name}"' in image
    assert "self._attr_name = map_name" in image
    vacuum = (CORE / "vacuum.py").read_text(encoding="utf-8")
    assert "_attr_translation_key = DOMAIN\n    _attr_name = None" in vacuum
    assert 'Segment(id=f"{current_map_id}_{room_id}"' in vacuum
