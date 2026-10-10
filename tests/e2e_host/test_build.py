"""The host's config starts onboarded, on loopback, with dashboard and card.

No HA process runs here; `test_host.py` starts one. See dev doc "E2E-Host".
"""

import json
from pathlib import Path

import pytest
import yaml
from homeassistant.components import onboarding
from homeassistant.core import HomeAssistant

import tests.realistic as realistic
from tests.e2e_host.host import CARD, CARD_TYPE, HOUSEHOLD, HOUSEHOLDS, build
from tests.realistic.roborock import ROLES, RoborockV1


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_build_writes_an_onboarded_loopback_config_with_the_card(
    tmp_path: Path,
) -> None:
    card = tmp_path / "card.js"
    card.write_text("customElements;", encoding="utf-8")
    config = tmp_path / "config"
    build(config, port=8125, household="two_robots", install_voi=True, card=card)

    yaml_config = yaml.safe_load((config / "configuration.yaml").read_text("utf-8"))
    assert "http" not in yaml_config
    assert yaml_config["voi_e2e_support"] == {"install_voi": True}
    storage = config / ".storage"
    # Confirmed, so HA never reverts to all interfaces.
    http = _json(storage / "http")["data"]
    assert (http["pending"], http["yaml_migration_done"]) == (None, True)
    assert (http["stable"]["server_host"], http["stable"]["server_port"]) == (
        ["127.0.0.1"],
        8125,
    )
    assert _json(storage / "onboarding")["data"]["done"] == onboarding.STEPS
    (resource,) = _json(storage / "lovelace_resources")["data"]["items"]
    assert resource["type"] == "module"
    assert resource["url"].startswith(f"/local/{CARD}?v=")
    (view,) = _json(storage / "lovelace")["data"]["config"]["views"]
    assert view["type"] == "sections"
    assert view["sections"][0]["cards"][0]["type"] == CARD_TYPE
    assert (config / "www" / CARD).read_text(encoding="utf-8") == "customElements;"
    components = config / "custom_components"
    assert sorted(path.name for path in components.iterdir()) == [
        "roborock",
        "vacuum_orchestrator",
        "voi_e2e_support",
        "voi_sim",
    ]
    assert not list(components.rglob("__pycache__"))
    names = _json(components / "roborock" / "translations" / "en.json")["entity"]
    for role in ROLES:
        if role.translation_key is not None:
            assert names[role.domain][role.translation_key]["name"] == role.name
    household = _json(config / HOUSEHOLD)
    assert household["areas"] == list(realistic.AREAS)
    assert [robot["name"] for robot in household["robots"]] == ["Saugi", "Flitzi"]


def test_without_a_card_the_default_dashboard_stays_untouched(tmp_path: Path) -> None:
    build(tmp_path, port=8125)

    assert not (tmp_path / ".storage" / "lovelace").exists()
    assert not (tmp_path / "www").exists()


@pytest.mark.parametrize("name", sorted(HOUSEHOLDS))
async def test_the_households_are_those_of_the_realistic_fixtures(
    hass: HomeAssistant, name: str
) -> None:
    home = getattr(realistic, name)(hass)

    assert {
        robot["name"]: (robot["kind"], robot.get("cleaning", False))
        for robot in HOUSEHOLDS[name]
    } == {
        robot_name: (
            "roborock" if isinstance(robot, RoborockV1) else "area",
            hass.states.get(robot.entity_id).state == "cleaning",
        )
        for robot_name, robot in home.robots.items()
    }
