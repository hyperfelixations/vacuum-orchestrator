"""User-facing cleaning intent value objects."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias, TypeVar

from .errors import ValidationError
from .types import CleaningMode, MopRoute, SemanticLevel, SettingsPolicy

JsonScalar: TypeAlias = str | int | float | bool | None


@dataclass(frozen=True, slots=True)
class TargetRef:
    """Canonical cleaning target with optional map context."""

    area_id: str
    map_context: str | None = None

    def __post_init__(self) -> None:
        if not self.area_id.strip():
            raise ValidationError("empty_target")
        if self.map_context is not None and not self.map_context.strip():
            raise ValidationError("empty_map_context")


@dataclass(frozen=True, slots=True)
class CleaningPreferences:
    """Desired tuning; core cleaning semantics do not depend on support."""

    vacuum_power: SemanticLevel | None = None
    mop_intensity: SemanticLevel | None = None
    mop_route: MopRoute | None = None


@dataclass(frozen=True, slots=True)
class VendorExtension:
    """Namespaced request interpreted only by the owning adapter."""

    namespace: str
    parameters: tuple[tuple[str, JsonScalar], ...] = ()

    def __post_init__(self) -> None:
        if "." not in self.namespace or not self.namespace.strip():
            raise ValidationError("invalid_vendor_extension_namespace")
        keys = [key for key, _value in self.parameters]
        if any(not key.strip() for key in keys) or len(keys) != len(set(keys)):
            raise ValidationError("invalid_vendor_extension_parameters")


def _normalized_references(values: tuple[str, ...], code: str) -> tuple[str, ...]:
    normalized = tuple(value.strip() for value in values)
    if any(not value for value in normalized) or len(normalized) != len(
        set(normalized)
    ):
        raise ValidationError(code)
    return normalized


@dataclass(frozen=True, slots=True)
class JobIntent:
    """Manufacturer-neutral desired cleaning outcome."""

    areas: tuple[TargetRef, ...]
    mode: CleaningMode
    name: str | None = None
    preferences: CleaningPreferences = CleaningPreferences()
    passes: int = 1
    source: str | None = None
    reason: str | None = None
    note: str | None = None
    dedupe_key: str | None = None
    required_on: tuple[str, ...] = ()
    required_off: tuple[str, ...] = ()
    settings_policy: SettingsPolicy = SettingsPolicy.BEST_EFFORT
    vendor_extension: VendorExtension | None = None

    def __post_init__(self) -> None:
        if not self.areas:
            raise ValidationError("job_requires_area")
        if (
            self.mode is not CleaningMode.MOP
            and self.preferences.vacuum_power is SemanticLevel.OFF
        ) or (
            self.mode is not CleaningMode.VACUUM
            and self.preferences.mop_intensity is SemanticLevel.OFF
        ):
            raise ValidationError("preference_conflicts_with_cleaning_mode")
        area_ids = [target.area_id for target in self.areas]
        if len(area_ids) != len(set(area_ids)):
            raise ValidationError("duplicate_area")
        if len({target.map_context for target in self.areas}) > 1:
            raise ValidationError("mixed_map_contexts")
        if not 1 <= self.passes <= 10:
            raise ValidationError("invalid_pass_count", str(self.passes))
        for field_name in ("name", "source", "reason", "note", "dedupe_key"):
            value = getattr(self, field_name)
            if value is not None and not value.strip():
                raise ValidationError(f"empty_{field_name}")
        required_on = _normalized_references(self.required_on, "invalid_required_on")
        required_off = _normalized_references(self.required_off, "invalid_required_off")
        if set(required_on) & set(required_off):
            raise ValidationError("contradictory_state_requirement")
        object.__setattr__(self, "required_on", required_on)
        object.__setattr__(self, "required_off", required_off)


class _Unset(Enum):
    VALUE = 0


UNSET = _Unset.VALUE
PatchValue: TypeAlias = _Unset
_T = TypeVar("_T")


def _patched(value: _T | PatchValue, current: _T) -> _T:
    return current if value is UNSET else value


@dataclass(frozen=True, slots=True)
class JobIntentPatch:
    """Typed partial update for one editable job."""

    areas: tuple[TargetRef, ...] | PatchValue = UNSET
    mode: CleaningMode | PatchValue = UNSET
    name: str | PatchValue | None = UNSET
    vacuum_power: SemanticLevel | PatchValue | None = UNSET
    mop_intensity: SemanticLevel | PatchValue | None = UNSET
    mop_route: MopRoute | PatchValue | None = UNSET
    passes: int | PatchValue = UNSET
    source: str | PatchValue | None = UNSET
    reason: str | PatchValue | None = UNSET
    note: str | PatchValue | None = UNSET
    dedupe_key: str | PatchValue | None = UNSET
    required_on: tuple[str, ...] | PatchValue = UNSET
    required_off: tuple[str, ...] | PatchValue = UNSET
    settings_policy: SettingsPolicy | PatchValue = UNSET
    vendor_extension: VendorExtension | PatchValue | None = UNSET

    def apply(self, intent: JobIntent) -> JobIntent:
        """Apply only explicitly supplied fields and re-run all invariants."""
        if all(
            value is UNSET
            for value in (
                self.areas,
                self.mode,
                self.name,
                self.vacuum_power,
                self.mop_intensity,
                self.mop_route,
                self.passes,
                self.source,
                self.reason,
                self.note,
                self.dedupe_key,
                self.required_on,
                self.required_off,
                self.settings_policy,
                self.vendor_extension,
            )
        ):
            raise ValidationError("empty_job_update")
        return JobIntent(
            areas=_patched(self.areas, intent.areas),
            mode=_patched(self.mode, intent.mode),
            name=_patched(self.name, intent.name),
            preferences=CleaningPreferences(
                _patched(self.vacuum_power, intent.preferences.vacuum_power),
                _patched(self.mop_intensity, intent.preferences.mop_intensity),
                _patched(self.mop_route, intent.preferences.mop_route),
            ),
            passes=_patched(self.passes, intent.passes),
            source=_patched(self.source, intent.source),
            reason=_patched(self.reason, intent.reason),
            note=_patched(self.note, intent.note),
            dedupe_key=_patched(self.dedupe_key, intent.dedupe_key),
            required_on=_patched(self.required_on, intent.required_on),
            required_off=_patched(self.required_off, intent.required_off),
            settings_policy=_patched(self.settings_policy, intent.settings_policy),
            vendor_extension=_patched(self.vendor_extension, intent.vendor_extension),
        )
