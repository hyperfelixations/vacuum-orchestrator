"""Robots come only from the E2E household; see dev doc "E2E-Host"."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult


class SimulatorFlow(ConfigFlow, domain="voi_sim"):
    """Add one simulated area vacuum per household entry."""

    VERSION = 1

    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        """Create the robot `voi_e2e_support` passes."""
        await self.async_set_unique_id(import_data["name"])
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=import_data["name"], data=import_data)
