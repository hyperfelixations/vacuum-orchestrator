"""Defaults copied into every new job; see dev doc "Auftragsvorgaben"."""

from __future__ import annotations

from dataclasses import dataclass, replace

from .errors import ValidationError
from .intents import (
    SETTING_NAMES,
    CleaningPreferences,
    JobIntent,
    settings_for_mode,
)
from .types import CleaningMode, MopRoute, SettingsPolicy, VacuumLevel, WaterLevel


@dataclass(frozen=True, slots=True)
class JobDefaults:
    """Semantic, robot-neutral values a job receives when it names none."""

    mode: CleaningMode = CleaningMode.VACUUM
    vacuum_power: VacuumLevel = VacuumLevel.STANDARD
    mop_intensity: WaterLevel = WaterLevel.MEDIUM
    mop_route: MopRoute = MopRoute.STANDARD
    passes: int = 1
    settings_policy: SettingsPolicy = SettingsPolicy.BEST_EFFORT
    configured: bool = False

    def __post_init__(self) -> None:
        if self.vacuum_power is VacuumLevel.OFF or self.mop_intensity is WaterLevel.OFF:
            raise ValidationError("unsupported_cleaning_preference", "off")
        if not 1 <= self.passes <= 10:
            raise ValidationError("invalid_pass_count", str(self.passes))

    def complete(self, intent: JobIntent) -> JobIntent:
        """Fill each setting the job's mode uses but does not name."""
        current = intent.preferences
        filled = CleaningPreferences(
            *(
                getattr(current, name)
                if getattr(current, name) is not None
                else getattr(self, name)
                for name in SETTING_NAMES
            )
        ).only(settings_for_mode(intent.mode))
        return intent if filled == current else replace(intent, preferences=filled)
