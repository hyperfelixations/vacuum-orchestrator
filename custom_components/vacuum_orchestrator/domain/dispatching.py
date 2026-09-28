"""Central robot eligibility, availability, and late-binding policy."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from .capabilities import (
    CancelSemantics,
    CompletionEvidence,
    RobotProfile,
    StartEvidence,
)
from .errors import PlanningError
from .execution import RobotLease
from .planning import DispatchAssignment, PreferenceResolution, WorkUnit
from .types import OperationKind, RobotAvailabilityState, SettingsPolicy

_REQUIRED_START_EVIDENCE = frozenset({StartEvidence.ACTIVITY_START_TRANSITION})
_REQUIRED_COMPLETION_EVIDENCE = frozenset(
    {
        CompletionEvidence.ACTIVITY_TERMINAL_TRANSITION,
    }
)


@dataclass(frozen=True, slots=True)
class RobotObservation:
    """One normalized point-in-time robot availability observation."""

    robot_id: str
    source_robot_id: str
    state: RobotAvailabilityState
    battery_percentage: int | None = None
    reason: str | None = None
    observed_at: datetime | None = None
    history_start: datetime | None = None
    history_end: datetime | None = None
    cleaning_active: bool | None = None
    normal_end: bool = False
    error_code: str | None = None
    observed_operation: OperationKind | None = None
    completed_targets: tuple[str, ...] = ()
    completion_confirmed: bool = False

    def __post_init__(self) -> None:
        if (
            self.battery_percentage is not None
            and not 0 <= self.battery_percentage <= 100
        ):
            raise PlanningError("invalid_battery_percentage")


class RobotSelector:
    """Choose one currently eligible robot using a deterministic policy."""

    def assign(
        self,
        unit: WorkUnit,
        profiles: tuple[RobotProfile, ...],
        observations: Mapping[str, RobotObservation],
        leases: Mapping[str, RobotLease],
        blocked_source_robot_ids: frozenset[str],
        active_target_sets: tuple[frozenset[str], ...],
        requested_robot_id: str | None = None,
    ) -> DispatchAssignment:
        """Late-bind a unit or raise one stable, explainable reason."""
        if requested_robot_id is not None:
            candidates = tuple(
                profile
                for profile in profiles
                if profile.robot_id == requested_robot_id
            )
            if not candidates:
                raise PlanningError("unknown_robot", requested_robot_id)
        else:
            candidates = profiles
        if not candidates:
            raise PlanningError("no_robot_configured")
        if any(set(unit.canonical_targets) & targets for targets in active_target_sets):
            raise PlanningError("target_overlap_active")

        successes: list[tuple[tuple[int, int, int, str], DispatchAssignment]] = []
        failures: list[str] = []
        for profile in candidates:
            try:
                assignment = self._evaluate(
                    unit,
                    profile,
                    observations.get(profile.robot_id),
                    leases,
                    blocked_source_robot_ids,
                )
            except PlanningError as err:
                failures.append(err.code)
                continue
            observation = observations[profile.robot_id]
            score = (
                -len(assignment.preference_resolution.omitted),
                profile.preference,
                observation.battery_percentage
                if observation.battery_percentage is not None
                else -1,
                profile.robot_id,
            )
            successes.append((score, assignment))
        if successes:
            return max(successes, key=lambda item: item[0])[1]
        if requested_robot_id is not None:
            raise PlanningError(failures[0])
        unique = set(failures)
        raise PlanningError(failures[0] if len(unique) == 1 else "no_eligible_robot")

    @staticmethod
    def _evaluate(
        unit: WorkUnit,
        profile: RobotProfile,
        observation: RobotObservation | None,
        leases: Mapping[str, RobotLease],
        blocked_source_robot_ids: frozenset[str],
    ) -> DispatchAssignment:
        capabilities = profile.capabilities
        if (
            observation is None
            or observation.source_robot_id != profile.source_robot_id
        ):
            raise PlanningError("robot_availability_unknown")
        if observation.state is not RobotAvailabilityState.AVAILABLE:
            raise PlanningError(f"robot_{observation.state.value}")
        if profile.minimum_battery is not None and (
            observation.battery_percentage is None
            or observation.battery_percentage < profile.minimum_battery
        ):
            raise PlanningError("battery_below_minimum")
        if profile.source_robot_id in blocked_source_robot_ids:
            raise PlanningError("robot_needs_attention")
        if profile.source_robot_id in leases:
            raise PlanningError("robot_already_executing")
        if unit.operation not in profile.effective_operations:
            raise PlanningError("unsupported_operation", unit.operation)
        if (
            unit.map_context is not None
            and capabilities.map_context != unit.map_context
        ):
            raise PlanningError("unsupported_map_context")
        missing_targets = [
            target
            for target in unit.canonical_targets
            if target not in capabilities.target_map
        ]
        if missing_targets:
            raise PlanningError("unmapped_target", ",".join(missing_targets))
        if unit.passes > capabilities.passes.maximum:
            raise PlanningError("unsupported_pass_count")
        if unit.pass_scope is not capabilities.passes.scope:
            raise PlanningError("unsupported_pass_scope")
        if capabilities.cancel is CancelSemantics.UNSUPPORTED:
            raise PlanningError("unsupported_cancel_semantics")
        if not _REQUIRED_START_EVIDENCE.issubset(capabilities.start_evidence):
            raise PlanningError("insufficient_start_evidence")
        if not _REQUIRED_COMPLETION_EVIDENCE.issubset(capabilities.completion_evidence):
            raise PlanningError("insufficient_completion_evidence")
        extension = unit.vendor_extension
        if (
            extension is not None
            and extension.namespace not in capabilities.vendor_extensions
        ):
            raise PlanningError("unsupported_vendor_extension")

        applied: list[str] = []
        omitted: list[str] = []
        requested = {
            "vacuum_power": (
                unit.preferences.vacuum_power,
                capabilities.vacuum_levels,
            ),
            "mop_intensity": (
                unit.preferences.mop_intensity,
                capabilities.water_levels,
            ),
            "mop_route": (unit.preferences.mop_route, capabilities.mop_routes),
        }
        for name, (value, supported) in requested.items():
            if (name == "vacuum_power" and unit.operation is OperationKind.MOP) or (
                name != "vacuum_power" and unit.operation is OperationKind.VACUUM
            ):
                continue
            if value is None:
                continue
            (applied if value in supported else omitted).append(name)
        if omitted and unit.settings_policy is SettingsPolicy.STRICT:
            raise PlanningError("unsupported_cleaning_preference", omitted[0])
        return DispatchAssignment(
            work_unit_id=unit.work_unit_id,
            robot_id=profile.robot_id,
            source_robot_id=profile.source_robot_id,
            adapter=profile.adapter,
            adapter_targets=tuple(
                segment
                for target in unit.canonical_targets
                for segment in capabilities.targets_for(target)
            ),
            capability_revision=capabilities.revision,
            preference_resolution=PreferenceResolution(tuple(applied), tuple(omitted)),
            room_targets={
                target: capabilities.targets_for(target)
                for target in unit.canonical_targets
            },
        )


def assignment_supports_current_capabilities(
    assignment: DispatchAssignment, current: RobotProfile
) -> bool:
    """Reject physical dispatch when late-bound capabilities changed."""
    return (
        assignment.robot_id == current.robot_id
        and assignment.source_robot_id == current.source_robot_id
        and assignment.capability_revision == current.capabilities.revision
    )
