"""Config and robot-subentry flows for Vacuum Orchestrator."""

from __future__ import annotations

from typing import Any, override

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    AreaSelector,
    AreaSelectorConfig,
    EntitySelector,
    EntitySelectorConfig,
)

from .const import (
    ADAPTER_ROBOROCK,
    CONF_ADAPTER,
    CONF_INSTALLATION_ID,
    CONF_LAST_CLEAN_END_ENTITY_ID,
    CONF_LAST_CLEAN_START_ENTITY_ID,
    CONF_ROBOT_ENTITY_ID,
    CONF_ROBOT_REGISTRY_ID,
    CONF_TARGET_AREAS,
    CONFIG_ENTRY_MINOR_VERSION,
    CONFIG_ENTRY_VERSION,
    DOMAIN,
    SUBENTRY_TYPE_ROBOT,
)


class VacuumOrchestratorConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create the single orchestrator and expose robot subentries."""

    VERSION = CONFIG_ENTRY_VERSION
    MINOR_VERSION = CONFIG_ENTRY_MINOR_VERSION

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Create the integration-wide queue before robots are added."""
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title="Vacuum Orchestrator",
                data={CONF_INSTALLATION_ID: DOMAIN},
            )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({}),
        )

    @classmethod
    @callback
    @override
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return robot-profile subentry support."""
        return {SUBENTRY_TYPE_ROBOT: RobotSubentryFlow}


class RobotSubentryFlow(ConfigSubentryFlow):
    """Add or reconfigure one robot profile."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Validate a Roborock-backed Home Assistant vacuum entity."""
        errors: dict[str, str] = {}
        if user_input is not None:
            entity_id = str(user_input[CONF_ROBOT_ENTITY_ID])
            registry_entry = er.async_get(self.hass).async_get(entity_id)
            if registry_entry is None:
                errors[CONF_ROBOT_ENTITY_ID] = "entity_not_registered"
            elif registry_entry.platform != ADAPTER_ROBOROCK:
                errors[CONF_ROBOT_ENTITY_ID] = "not_roborock_entity"
            elif any(
                subentry.data.get(CONF_ROBOT_REGISTRY_ID) == registry_entry.id
                for subentry in self._get_entry().subentries.values()
            ):
                errors[CONF_ROBOT_ENTITY_ID] = "already_configured"
            else:
                data = dict(user_input)
                data[CONF_ADAPTER] = ADAPTER_ROBOROCK
                data[CONF_ROBOT_REGISTRY_ID] = registry_entry.id
                return self.async_create_entry(
                    title=registry_entry.name
                    or registry_entry.original_name
                    or entity_id,
                    data=data,
                    unique_id=registry_entry.id,
                )
        return self.async_show_form(
            step_id="user",
            data_schema=_robot_schema(user_input),
            errors=errors,
        )


def _robot_schema(suggested: dict[str, Any] | None) -> vol.Schema:
    values = suggested or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_ROBOT_ENTITY_ID,
                description={"suggested_value": values.get(CONF_ROBOT_ENTITY_ID)},
            ): EntitySelector(EntitySelectorConfig(domain="vacuum")),
            vol.Required(
                CONF_TARGET_AREAS,
                description={"suggested_value": values.get(CONF_TARGET_AREAS)},
            ): AreaSelector(AreaSelectorConfig(multiple=True)),
            vol.Optional(
                CONF_LAST_CLEAN_START_ENTITY_ID,
                description={
                    "suggested_value": values.get(CONF_LAST_CLEAN_START_ENTITY_ID)
                },
            ): EntitySelector(EntitySelectorConfig(domain="sensor")),
            vol.Optional(
                CONF_LAST_CLEAN_END_ENTITY_ID,
                description={
                    "suggested_value": values.get(CONF_LAST_CLEAN_END_ENTITY_ID)
                },
            ): EntitySelector(EntitySelectorConfig(domain="sensor")),
        }
    )
