"""Minimal Windows-only test stub for POSIX file-descriptor limits."""

RLIMIT_NOFILE = 7
_LIMIT = (2048, 2048)


def getrlimit(_resource: int) -> tuple[int, int]:
    """Return a stable limit sufficient for the in-process test instance."""
    return _LIMIT


def setrlimit(_resource: int, _limits: tuple[int, int]) -> None:
    """No-op because Windows does not expose POSIX resource limits."""
