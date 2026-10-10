"""Route HA actions to the simulated robot that owns the targeted entity."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse

_ROBOTS = "realistic_robots"


class Robot(Protocol):
    """A simulated robot answering the actions sent to its entities."""

    def owns(self, entity_id: str) -> bool:
        """Whether the entity belongs to this robot."""

    async def handle(self, call: ServiceCall) -> dict[str, Any] | None:
        """Record the call and change the robot's state."""


def targets(call: ServiceCall) -> list[str]:
    """The entity IDs of a call, given as one ID or a list."""
    value = call.data.get("entity_id", [])
    return [value] if isinstance(value, str) else list(value)


def attach(
    hass: HomeAssistant,
    robot: Robot,
    actions: Iterable[tuple[str, str]],
    responding: Iterable[tuple[str, str]] = (),
) -> None:
    """Answer the actions for this robot's entities from now on."""
    robots: list[Robot] = hass.data.setdefault(_ROBOTS, [])
    robots.append(robot)
    answering = set(responding)
    for domain, action in (*actions, *answering):
        if hass.services.has_service(domain, action):
            continue

        async def route(call: ServiceCall) -> dict[str, Any] | None:
            owners = [
                item
                for item in hass.data[_ROBOTS]
                if any(item.owns(entity_id) for entity_id in targets(call))
            ]
            assert len(owners) == 1, (call.domain, call.service, targets(call))
            return await owners[0].handle(call)

        hass.services.async_register(
            domain,
            action,
            route,
            supports_response=SupportsResponse.ONLY
            if (domain, action) in answering
            else SupportsResponse.NONE,
        )
