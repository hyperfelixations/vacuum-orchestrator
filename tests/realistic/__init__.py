"""Homes whose registries match real integrations exactly; names are synthetic.

See dev doc "Testarchitektur". A household installs HA areas and robots the way
their integrations register them, so VOI is tested through its real discovery.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar

from tests.realistic.generic import AreaVacuum
from tests.realistic.roborock import RoborockV1

AREAS = ("Küche", "Flur", "Wohnzimmer", "Bad")


@dataclass
class Household:
    """HA areas by name and the robots installed in them."""

    hass: HomeAssistant
    areas: dict[str, str]
    robots: dict[str, RoborockV1 | AreaVacuum] = field(default_factory=dict)

    @property
    def roborock(self) -> RoborockV1:
        """The household's only Roborock."""
        (robot,) = [
            item for item in self.robots.values() if isinstance(item, RoborockV1)
        ]
        return robot

    @property
    def generic(self) -> AreaVacuum:
        """The household's only generic area vacuum."""
        (robot,) = [
            item for item in self.robots.values() if isinstance(item, AreaVacuum)
        ]
        return robot


def _areas(hass: HomeAssistant) -> dict[str, str]:
    registry = ar.async_get(hass)
    return {name: registry.async_create(name).id for name in AREAS}


def no_robot(hass: HomeAssistant) -> Household:
    """Areas only: VOI imports rooms but finds no robot."""
    return Household(hass, _areas(hass))


def single_roborock(hass: HomeAssistant) -> Household:
    """One Roborock V1 with dock and two maps; one mapped segment is stale."""
    home = no_robot(hass)
    home.robots["Saugi"] = RoborockV1.install(hass, "Saugi", home.areas)
    return home


def generic_area(hass: HomeAssistant) -> Household:
    """One vacuum of another integration that cleans HA areas."""
    home = no_robot(hass)
    home.robots["Flitzi"] = AreaVacuum.install(hass, "Flitzi", home.areas)
    return home


def two_robots(hass: HomeAssistant) -> Household:
    """A Roborock downstairs and a generic area vacuum."""
    home = single_roborock(hass)
    home.robots["Flitzi"] = AreaVacuum.install(hass, "Flitzi", home.areas)
    return home


def busy(hass: HomeAssistant) -> Household:
    """A Roborock that cleans a run started in its own app."""
    home = single_roborock(hass)
    home.roborock.clean()
    return home
