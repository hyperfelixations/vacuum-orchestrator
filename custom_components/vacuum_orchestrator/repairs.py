"""Repair notifications for persistent configuration and recovery problems."""

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .adapters.home_assistant_vacuum import HomeAssistantVacuumAdapter
from .application.orchestrator import VacuumOrchestrator
from .const import DOMAIN


class RepairReporter:
    """Keep persistent defects separate from ordinary room readiness blockers."""

    def __init__(self, hass: HomeAssistant, core: VacuumOrchestrator) -> None:
        self.hass, self.core = hass, core
        self._issues: set[str] = set()

    @callback
    def update(self) -> None:
        """Reconcile actionable issues after a completed runtime pass."""
        issues: dict[str, str] = {}
        for robot_id, adapter in self.core.adapters.items():
            if (
                isinstance(adapter, HomeAssistantVacuumAdapter)
                and adapter.entity_id is None
            ):
                issues[f"robot_{robot_id}"] = "robot_entity_missing"
            elif (
                not adapter.profile.effective_operations
                or not adapter.profile.capabilities.target_map
            ):
                issues[f"robot_{robot_id}"] = "robot_configuration_incomplete"
        for room in self.core.rooms.registry.rooms.values():
            if room.enabled and room.area_missing:
                issues[f"room_{room.room_id}"] = "room_area_missing"
        if self.core.state.needs_attention:
            issues["recovery_required"] = "recovery_required"
        for issue_id, reason in issues.items():
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=reason,
            )
        for issue_id in self._issues - issues.keys():
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        self._issues = set(issues)

    @callback
    def close(self) -> None:
        """Remove runtime-owned notifications when this integration unloads."""
        for issue_id in self._issues:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
        self._issues.clear()
