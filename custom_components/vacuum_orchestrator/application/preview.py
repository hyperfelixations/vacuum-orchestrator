"""Read-only job draft preview using the same rules as dispatch."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..domain.capabilities import RobotCapabilities
from ..domain.dispatching import RobotSelector, resolve_settings
from ..domain.errors import ConflictError, PlanningError
from ..domain.intents import (
    CleaningPreferences,
    JobIntent,
    settings_for_mode,
    settings_for_operation,
)
from ..domain.planning import Planner, SettingsResolution, operations_for_mode
from ..domain.queue import Job
from ..domain.types import (
    ROUTE_LADDER,
    VACUUM_LADDER,
    WATER_LADDER,
    CleaningMode,
    JobState,
    OperationKind,
    SettingValue,
    nearest_supported,
)

if TYPE_CHECKING:
    from .orchestrator import VacuumOrchestrator

PREVIEW_JOB_ID = "preview"
_LADDERS: dict[
    str,
    tuple[
        tuple[SettingValue, ...], Callable[[RobotCapabilities], frozenset[SettingValue]]
    ],
] = {
    "vacuum_power": (VACUUM_LADDER, lambda caps: caps.vacuum_levels),
    "mop_intensity": (WATER_LADDER, lambda caps: caps.water_levels),
    "mop_route": (ROUTE_LADDER, lambda caps: caps.mop_routes),
}


@dataclass(frozen=True, slots=True)
class SettingChoice:
    """Selectable values of one setting and the value to preselect."""

    initial: str
    options: tuple[tuple[str, bool], ...]


@dataclass(frozen=True, slots=True)
class RobotPreview:
    """Why one robot could or could not start one operation of the draft now."""

    robot_id: str
    operation: OperationKind
    reason: str | None
    settings: SettingsResolution | None


@dataclass(frozen=True, slots=True)
class JobPreview:
    """Draft resolved against defaults, robots and current conditions."""

    mode: CleaningMode
    settings: Mapping[str, SettingChoice]
    robots: tuple[RobotPreview, ...]
    reason: str | None


async def preview_job(
    core: VacuumOrchestrator,
    mode: CleaningMode,
    preferences: CleaningPreferences,
    intent: JobIntent | None = None,
    robot_id: str | None = None,
) -> JobPreview:
    """Resolve a draft without committing; `intent` is None until rooms exist.

    Options are the union of the values offered by every robot that can carry
    out an operation using the setting on the selected rooms; see dev doc
    "Vorschau".
    """
    state = core.state
    defaults = state.job_defaults
    profiles = tuple(adapter.profile for adapter in core.adapters.values())
    if intent is not None:
        intent = defaults.complete(core._canonical_intent(state, intent))
        mode, preferences = intent.mode, intent.preferences
    targets = () if intent is None else tuple(t.area_id for t in intent.areas)
    settings: dict[str, SettingChoice] = {}
    for name in sorted(settings_for_mode(mode), key=list(_LADDERS).index):
        ladder, levels = _LADDERS[name]
        operations = tuple(
            operation
            for operation in operations_for_mode(mode)
            if name in settings_for_operation(operation)
        )
        offered = [
            levels(profile.capabilities)
            for profile in profiles
            if profile.effective_operations & set(operations)
            and all(target in profile.capabilities.target_map for target in targets)
        ]
        union = {value for values in offered for value in values}
        values = [value for value in ladder if value in union] or list(ladder)
        requested = getattr(preferences, name) or getattr(defaults, name)
        initial = (
            requested
            if requested in values
            else nearest_supported(requested, ladder, frozenset(values))
        )
        settings[name] = SettingChoice(
            requested.value if initial is None else initial.value,
            tuple(
                (value.value, bool(offered) and all(value in item for item in offered))
                for value in values
            ),
        )
    if intent is None:
        return JobPreview(mode, settings, (), "job_requires_area")

    observations = await core._observe_robots()
    now = core._clock()
    job = Job(PREVIEW_JOB_ID, 1, intent, JobState.QUEUED, now, now)
    plan = Planner().create_plan(PREVIEW_JOB_ID, intent)
    robots = []
    for unit in plan.work_units:
        for profile in profiles:
            try:
                resolved: SettingsResolution | None = resolve_settings(
                    unit, profile, defaults
                )
            except PlanningError:
                resolved = None
            report = core.job_readiness(
                state, job, robot_id=profile.robot_id, operation=unit.operation
            )
            reason = report.reason_codes[0] if report.reason_codes else None
            if reason is None:
                try:
                    RobotSelector().assign(
                        unit,
                        (profile,),
                        observations,
                        state.robot_leases,
                        frozenset(state.blocked_robots),
                        state.active_target_sets(),
                        profile.robot_id,
                        defaults=defaults,
                    )
                except PlanningError as err:
                    reason = err.code
            robots.append(
                RobotPreview(profile.robot_id, unit.operation, reason, resolved)
            )
    try:
        state.add_job(PREVIEW_JOB_ID, intent, now)
        report = core.job_readiness(state, job)
        if report.state.value != "ready":
            raise PlanningError(f"job_{report.state.value}")
        core.select_robot(state, job, plan.work_units[0], observations, robot_id)
        startable: str | None = None
    except (ConflictError, PlanningError) as err:
        startable = err.code
    return JobPreview(mode, settings, tuple(robots), startable)
