"""Conservative adapter using only public Home Assistant vacuum actions."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256

from homeassistant.components.vacuum import SERVICE_CLEAN_AREA
from homeassistant.components.vacuum.const import (
    DOMAIN as VACUUM_DOMAIN,
)
from homeassistant.components.vacuum.const import (
    VacuumEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ..domain.capabilities import (
    AreaAddressing,
    CancelSemantics,
    CompletionEvidence,
    PassCapability,
    RobotCapabilities,
    RobotProfile,
    StartEvidence,
)
from ..domain.dispatching import RobotObservation
from ..domain.errors import ConflictError
from ..domain.planning import DispatchAssignment, WorkUnit
from ..domain.types import OperationKind, PassScope, RobotAvailabilityState

SERVICE_STOP = "stop"
ATTR_CLEANING_AREA_ID = "cleaning_area_id"


class HomeAssistantVacuumAdapter:
    """Dispatch semantics advertised by an existing HA vacuum entity only."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        robot_id: str,
        source_robot_id: str,
        entity_id: str,
        adapter_name: str,
        target_areas: tuple[str, ...],
        last_clean_start_entity_id: str | None,
        last_clean_end_entity_id: str | None,
    ) -> None:
        self._hass = hass
        self._entity_id = entity_id
        self._last_clean_start_entity_id = last_clean_start_entity_id
        self._last_clean_end_entity_id = last_clean_end_entity_id
        state = hass.states.get(entity_id)
        supported = (
            0 if state is None else int(state.attributes.get("supported_features", 0))
        )
        clean_area = bool(supported & VacuumEntityFeature.CLEAN_AREA)
        operations = (
            frozenset({OperationKind.VACUUM_AND_MOP}) if clean_area else frozenset()
        )
        completion_evidence = {CompletionEvidence.ACTIVITY_TERMINAL_TRANSITION}
        has_history_evidence = bool(
            last_clean_start_entity_id and last_clean_end_entity_id
        )
        if has_history_evidence:
            completion_evidence.add(CompletionEvidence.CLEANING_HISTORY_TIMESTAMPS)
        revision_source = "|".join(
            (
                entity_id,
                str(supported),
                ",".join(sorted(target_areas)),
                str(has_history_evidence),
            )
        )
        capabilities = RobotCapabilities(
            revision=sha256(revision_source.encode()).hexdigest(),
            operations=operations,
            area_addressing=AreaAddressing.HOME_ASSISTANT_AREA,
            target_map={area_id: area_id for area_id in target_areas},
            map_context=None,
            passes=PassCapability(1, PassScope.TARGET_SET),
            vacuum_levels=frozenset(),
            water_levels=frozenset(),
            cancel=CancelSemantics.STOP,
            start_evidence=frozenset({StartEvidence.ACTIVITY_START_TRANSITION}),
            completion_evidence=frozenset(completion_evidence),
        )
        self._profile = RobotProfile(
            robot_id=robot_id,
            source_robot_id=source_robot_id,
            adapter=adapter_name,
            capabilities=capabilities,
        )

    @property
    def profile(self) -> RobotProfile:
        """Return the capability snapshot captured for exact planning."""
        return self._profile

    @property
    def watched_entity_ids(self) -> tuple[str, ...]:
        """Return entities that can change availability or run evidence."""
        return tuple(
            item
            for item in (
                self._entity_id,
                self._last_clean_start_entity_id,
                self._last_clean_end_entity_id,
            )
            if item is not None
        )

    async def async_observe(self) -> RobotObservation:
        """Normalize current HA state through the shared availability contract."""
        state = self._hass.states.get(self._entity_id)
        if state is None or state.state in {"unknown", "unavailable"}:
            availability = RobotAvailabilityState.UNKNOWN
            battery = None
        else:
            availability = (
                RobotAvailabilityState.AVAILABLE
                if state.state in {"idle", "docked"}
                else RobotAvailabilityState.BUSY
            )
            raw_battery = state.attributes.get("battery_level")
            battery = raw_battery if isinstance(raw_battery, int) else None
        return RobotObservation(
            self._profile.robot_id,
            self._profile.source_robot_id,
            availability,
            battery,
            state.state if state is not None else "entity_missing",
            datetime.now(UTC),
            self._history_value(self._last_clean_start_entity_id),
            self._history_value(self._last_clean_end_entity_id),
        )

    def _history_value(self, entity_id: str | None) -> datetime | None:
        if entity_id is None or (state := self._hass.states.get(entity_id)) is None:
            return None
        parsed = dt_util.parse_datetime(state.state)
        if parsed is None:
            return None
        return parsed.astimezone(UTC)

    async def async_dispatch(
        self, unit: WorkUnit, assignment: DispatchAssignment
    ) -> None:
        """Dispatch one exact HA `vacuum.clean_area` work unit."""
        if assignment.work_unit_id != unit.work_unit_id:
            raise ConflictError("assignment_work_unit_mismatch")
        if not assignment.adapter_targets:
            raise ConflictError("empty_adapter_targets")
        await self._hass.services.async_call(
            VACUUM_DOMAIN,
            SERVICE_CLEAN_AREA,
            {
                "entity_id": self._entity_id,
                ATTR_CLEANING_AREA_ID: list(assignment.adapter_targets),
            },
            blocking=True,
        )

    async def async_cancel(self) -> None:
        """Stop the current physical activity without assuming completion."""
        await self._hass.services.async_call(
            VACUUM_DOMAIN,
            SERVICE_STOP,
            {"entity_id": self._entity_id},
            blocking=True,
        )
