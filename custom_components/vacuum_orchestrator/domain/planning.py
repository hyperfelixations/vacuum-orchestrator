"""Robot-independent job planning and dispatch assignment models."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from .intents import CleaningPreferences, JobIntent, VendorExtension
from .types import CleaningMode, OperationKind, PassScope, SettingsPolicy


@dataclass(frozen=True, slots=True)
class WorkUnit:
    """One immutable, robot-independent, atomically dispatchable operation."""

    work_unit_id: str
    operation: OperationKind
    canonical_targets: tuple[str, ...]
    map_context: str | None
    passes: int
    pass_scope: PassScope
    preferences: CleaningPreferences
    settings_policy: SettingsPolicy
    vendor_extension: VendorExtension | None
    depends_on: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """Immutable logical plan derived solely from one job intent."""

    plan_id: str
    job_id: str
    work_units: tuple[WorkUnit, ...]


@dataclass(frozen=True, slots=True)
class PreferenceResolution:
    """Explicit result of resolving optional settings for one robot."""

    applied: tuple[str, ...]
    omitted: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DispatchAssignment:
    """Late binding of one logical work unit to one eligible robot."""

    work_unit_id: str
    robot_id: str
    source_robot_id: str
    adapter: str
    adapter_targets: tuple[str, ...]
    capability_revision: str
    preference_resolution: PreferenceResolution


class Planner:
    """Compile user intent without inspecting or selecting a robot."""

    def create_plan(self, job_id: str, intent: JobIntent) -> ExecutionPlan:
        """Build the exact logical work-unit sequence for an intent."""
        operations = operations_for_mode(intent.mode)
        targets = tuple(target.area_id for target in intent.areas)
        map_context = intent.areas[0].map_context
        units: list[WorkUnit] = []
        for operation in operations:
            dependency = (units[-1].work_unit_id,) if units else ()
            units.append(
                WorkUnit(
                    work_unit_id=str(uuid4()),
                    operation=operation,
                    canonical_targets=targets,
                    map_context=map_context,
                    passes=intent.passes,
                    pass_scope=PassScope.TARGET_SET,
                    preferences=intent.preferences,
                    settings_policy=intent.settings_policy,
                    vendor_extension=intent.vendor_extension,
                    depends_on=dependency,
                )
            )
        return ExecutionPlan(str(uuid4()), job_id, tuple(units))


def operations_for_mode(mode: CleaningMode) -> tuple[OperationKind, ...]:
    """Return the exact atomic semantics represented by a public mode."""
    if mode is CleaningMode.VACUUM:
        return (OperationKind.VACUUM,)
    if mode is CleaningMode.MOP:
        return (OperationKind.MOP,)
    if mode is CleaningMode.VACUUM_AND_MOP:
        return (OperationKind.VACUUM_AND_MOP,)
    return (OperationKind.VACUUM, OperationKind.MOP)


def plan_matches_intent(plan: ExecutionPlan, intent: JobIntent) -> bool:
    """Return whether an immutable plan exactly represents its source intent."""
    operations = operations_for_mode(intent.mode)
    targets = tuple(target.area_id for target in intent.areas)
    map_context = intent.areas[0].map_context
    if (
        not plan.plan_id.strip()
        or len(plan.work_units) != len(operations)
        or len({unit.work_unit_id for unit in plan.work_units}) != len(plan.work_units)
    ):
        return False
    for index, (unit, operation) in enumerate(
        zip(plan.work_units, operations, strict=True)
    ):
        expected_dependency = (
            (plan.work_units[index - 1].work_unit_id,) if index else ()
        )
        if (
            not unit.work_unit_id.strip()
            or unit.operation is not operation
            or unit.canonical_targets != targets
            or unit.map_context != map_context
            or unit.passes != intent.passes
            or unit.pass_scope is not PassScope.TARGET_SET
            or unit.preferences != intent.preferences
            or unit.settings_policy is not intent.settings_policy
            or unit.vendor_extension != intent.vendor_extension
            or unit.depends_on != expected_dependency
        ):
            return False
    return True
