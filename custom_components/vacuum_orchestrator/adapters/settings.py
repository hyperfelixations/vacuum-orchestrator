"""Public HA setting actions with observed acknowledgement and command fencing."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event

from ..domain.errors import ConflictError
from ..ha_context import physical_context
from ..ports.command_scope import check_command_authorization
from ..ports.telemetry import TelemetryEvent, report_adapter


def available_options(hass: HomeAssistant, entity_id: str | None) -> tuple[str, ...]:
    """Read selectable options without treating unavailable entities as usable."""
    if entity_id is None or (state := hass.states.get(entity_id)) is None:
        return ()
    if state.state in {"unavailable", "unknown"}:
        return ()
    attribute = "fan_speed_list" if entity_id.startswith("vacuum.") else "options"
    options = state.attributes.get(attribute, ())
    if not isinstance(options, (list, tuple)):
        return ()
    return tuple(value for value in options if isinstance(value, str))


async def async_set_option(
    hass: HomeAssistant,
    entity_id: str,
    option: str,
    *,
    confirmation_seconds: float,
    setting: str | None = None,
) -> None:
    """Require actual state acknowledgement before the next physical command."""
    vacuum = entity_id.startswith("vacuum.")

    def acknowledged() -> bool:
        state = hass.states.get(entity_id)
        return state is not None and (
            state.attributes.get("fan_speed") == option
            if vacuum
            else state.state == option
        )

    if option not in available_options(hass, entity_id):
        raise ConflictError("setting_option_unavailable")
    if acknowledged():
        report_adapter(TelemetryEvent.SETTING, "unchanged", setting)
        return
    changed = asyncio.Event()

    @callback
    def state_changed(_event: Any) -> None:
        changed.set()

    unsubscribe = async_track_state_change_event(hass, [entity_id], state_changed)
    try:
        check_command_authorization()
        report_adapter(TelemetryEvent.SETTING, "requested", setting)
        async with asyncio.timeout(confirmation_seconds):
            await hass.services.async_call(
                "vacuum" if vacuum else "select",
                "set_fan_speed" if vacuum else "select_option",
                {"entity_id": entity_id, "fan_speed" if vacuum else "option": option},
                blocking=True,
                context=physical_context(),
            )
            while not acknowledged():
                changed.clear()
                if option not in available_options(hass, entity_id):
                    raise ConflictError("setting_entity_unavailable")
                await changed.wait()
            check_command_authorization()
            report_adapter(TelemetryEvent.SETTING, "confirmed", setting)
    except TimeoutError as err:
        report_adapter(TelemetryEvent.SETTING, "timeout", setting)
        raise ConflictError("setting_confirmation_timeout") from err
    finally:
        unsubscribe()


def supported_mapping(
    configured: Mapping[str, str], options: tuple[str, ...]
) -> dict[str, str]:
    """Advertise only configured values currently supported by the HA entity."""
    return {key: value for key, value in configured.items() if value in options}
