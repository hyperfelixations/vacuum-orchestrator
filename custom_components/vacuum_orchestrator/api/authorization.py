"""Who may command VOI: every user HA lets control the robots (ACT-07)."""

from __future__ import annotations

from homeassistant.auth.models import User
from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import entity_registry as er

from ..const import CONF_ROBOT_REGISTRY_ID, DOMAIN, SUBENTRY_TYPE_ROBOT


def may_command(hass: HomeAssistant, user: User | None) -> bool:
    """Allow a user to command when HA lets them control every configured vacuum.

    See dev doc "Berechtigung".
    """
    if user is None:
        return True
    vacuums = _robot_vacuums(hass)
    if not vacuums:
        return user.permissions.access_all_entities(POLICY_CONTROL)
    return all(
        user.permissions.check_entity(entity_id, POLICY_CONTROL)
        for entity_id in vacuums
    )


async def async_may_command(hass: HomeAssistant, context: Context) -> bool:
    """Resolve the context's user; calls without one (automations) may command."""
    if context.user_id is None:
        return True
    user = await hass.auth.async_get_user(context.user_id)
    return user is not None and may_command(hass, user)


async def async_require_command(hass: HomeAssistant, context: Context) -> None:
    """Refuse a command from a user HA does not let control the robots."""
    if not await async_may_command(hass, context):
        raise Unauthorized(context=context)


def permissions(allowed: bool) -> dict[str, object]:
    """Serialize the command permission a client follows."""
    return {"permissions": {"command": allowed}}


def _robot_vacuums(hass: HomeAssistant) -> tuple[str, ...]:
    registry = er.async_get(hass)
    entity_ids: list[str] = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        for subentry in entry.subentries.values():
            if subentry.subentry_type != SUBENTRY_TYPE_ROBOT:
                continue
            registered = registry.async_get(
                str(subentry.data.get(CONF_ROBOT_REGISTRY_ID, ""))
            )
            if registered is not None:
                entity_ids.append(registered.entity_id)
    return tuple(entity_ids)
