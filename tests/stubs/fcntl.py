"""Minimal Windows-only test stub for Home Assistant's process lock import."""

LOCK_EX = 2
LOCK_NB = 4


def flock(_file_descriptor: int, _operation: int) -> None:
    """No-op because the pytest fixture never starts a second HA process."""
