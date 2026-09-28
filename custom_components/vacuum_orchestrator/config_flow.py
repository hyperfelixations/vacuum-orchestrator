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
    ObjectSelector,
)

from .adapters.discovery import candidate_for
from .configuration import validate_robot_configuration
from .const import (
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
from .domain.errors import OrchestratorError, ValidationError


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
    """Add or reconfigure a robot using the common configuration validator."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Configure a discovered or manually selected HA vacuum."""
        return await self._async_configure(user_input, reconfigure=False)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Update an idle robot without changing its persistent identity."""
        return await self._async_configure(user_input, reconfigure=True)

    async def _async_configure(
        self, user_input: dict[str, Any] | None, *, reconfigure: bool
    ) -> SubentryFlowResult:
        entry = self._get_entry()
        subentry = self._get_reconfigure_subentry() if reconfigure else None
        suggested = dict(subentry.data) if subentry else {}
        if subentry and (
            entity := er.async_get(self.hass).async_get(
                str(suggested[CONF_ROBOT_REGISTRY_ID])
            )
        ):
            suggested[CONF_ROBOT_ENTITY_ID] = entity.entity_id
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input = dict(user_input)
            advanced = user_input.pop("advanced", {})
            if not isinstance(advanced, dict):
                advanced = {}
                errors[CONF_ROBOT_ENTITY_ID] = "invalid_robot_configuration"
            suggested.update(advanced)
            suggested.update(user_input)
            submitted = dict(suggested)
            submitted.pop(CONF_ROBOT_REGISTRY_ID, None)
            try:
                if errors:
                    raise ValidationError("invalid_robot_configuration")
                data = validate_robot_configuration(
                    self.hass,
                    entry,
                    submitted,
                    robot_id=subentry.subentry_id if subentry else None,
                )
                if subentry:
                    if (
                        data[CONF_ROBOT_REGISTRY_ID]
                        != subentry.data[CONF_ROBOT_REGISTRY_ID]
                    ):
                        raise ValidationError("robot_identity_change")
                    return self.async_update_and_abort(entry, subentry, data=data)
                return self.async_create_entry(
                    title=candidate_for(self.hass, data[CONF_ROBOT_REGISTRY_ID]).name,
                    data=data,
                    unique_id=data[CONF_ROBOT_REGISTRY_ID],
                )
            except OrchestratorError as err:
                errors[CONF_ROBOT_ENTITY_ID] = err.code
        return self.async_show_form(
            step_id="reconfigure" if reconfigure else "user",
            data_schema=_robot_schema(suggested),
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
            vol.Optional(
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
                "advanced",
                description={
                    "suggested_value": {
                        key: value
                        for key, value in values.items()
                        if key
                        not in {
                            CONF_ROBOT_ENTITY_ID,
                            CONF_ROBOT_REGISTRY_ID,
                            CONF_TARGET_AREAS,
                            CONF_LAST_CLEAN_START_ENTITY_ID,
                            CONF_LAST_CLEAN_END_ENTITY_ID,
                            "adapter",
                            "source_robot_id",
                        }
                    }
                },
            ): ObjectSelector(),
            vol.Optional(
                CONF_LAST_CLEAN_END_ENTITY_ID,
                description={
                    "suggested_value": values.get(CONF_LAST_CLEAN_END_ENTITY_ID)
                },
            ): EntitySelector(EntitySelectorConfig(domain="sensor")),
        }
    )
