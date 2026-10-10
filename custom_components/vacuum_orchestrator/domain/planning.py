"""Robot-independent job planning and dispatch assignment models."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from uuid import uuid4

from .errors import ValidationError
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
class ResolvedSetting:
    """Requested and applied semantic value of one setting for one robot."""

    name: str
    requested: str
    applied: str | None


@dataclass(frozen=True, slots=True)
class SettingsResolution:
    """Every setting an operation uses, translated for one robot.

    `applied` is `None` only where the robot has no such setting at all.
    """

    settings: tuple[ResolvedSetting, ...] = ()

    @property
    def applied(self) -> tuple[str, ...]:
        """Name settings the adapter sets on the robot."""
        return tuple(item.name for item in self.settings if item.applied is not None)

    @property
    def omitted(self) -> tuple[str, ...]:
        """Name settings the robot does not have."""
        return tuple(item.name for item in self.settings if item.applied is None)

    @property
    def substituted(self) -> tuple[str, ...]:
        """Name settings applied with the nearest supported value."""
        return tuple(
            item.name
            for item in self.settings
            if item.applied is not None and item.applied != item.requested
        )

    def value(self, name: str) -> str | None:
        """Return the applied value of one setting."""
        return next((item.applied for item in self.settings if item.name == name), None)


@dataclass(frozen=True, slots=True)
class DispatchAssignment:
    """Late binding of one logical work unit to one eligible robot."""

    work_unit_id: str
    robot_id: str
    source_robot_id: str
    adapter: str
    adapter_targets: tuple[str, ...]
    capability_revision: str
    settings: SettingsResolution
    room_targets: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        values = tuple(
            target for targets in self.room_targets.values() for target in targets
        )
        if self.room_targets and (
            not all(self.room_targets.values())
            or len(values) != len(set(values))
            or set(values) != set(self.adapter_targets)
        ):
            raise ValidationError("assignment_room_mapping_invalid")
        object.__setattr__(
            self, "room_targets", MappingProxyType(dict(self.room_targets))
        )


class Planner:
    """Compile user intent without inspecting or selecting a robot."""

    def __init__(self, id_factory: Callable[[], str] = lambda: str(uuid4())) -> None:
        self._id_factory = id_factory

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
                    work_unit_id=self._id_factory(),
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
        return ExecutionPlan(self._id_factory(), job_id, tuple(units))


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
