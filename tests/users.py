"""Home Assistant users the tests act as."""

from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockUser


async def household_member(hass: HomeAssistant) -> MockUser:
    """Return a regular user in HA's default user group, no administrator."""
    group = await hass.auth.async_get_group(GROUP_ID_USER)
    assert group is not None
    return MockUser(groups=[group]).add_to_hass(hass)
