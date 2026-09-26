"""Shared validation for immutable configuration and observation values."""

from datetime import datetime
from math import isfinite

from .errors import ValidationError


def identifier(value: str, code: str = "invalid_identifier") -> None:
    """Require a bounded, nonempty reference without surrounding whitespace."""
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 255
    ):
        raise ValidationError(code)


def instant(value: datetime) -> None:
    """Require an aware timestamp for persisted temporal comparisons."""
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValidationError("timezone_required")


def seconds(value: float, *, positive: bool = False) -> None:
    """Require finite nonnegative duration, optionally excluding zero."""
    if (
        isinstance(value, bool)
        or not isfinite(value)
        or value < 0
        or (positive and value == 0)
    ):
        raise ValidationError("invalid_duration")
