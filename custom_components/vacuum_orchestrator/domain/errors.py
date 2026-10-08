"""Domain-specific exceptions with stable reason codes."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Self

type FieldPath = tuple[str | int, ...]


class OrchestratorError(Exception):
    """Base error carrying a stable machine-readable code and the input field."""

    def __init__(
        self, code: str, detail: str | None = None, *, path: FieldPath = ()
    ) -> None:
        self.code = code
        self.detail = detail
        self.path = path
        super().__init__(code if detail is None else f"{code}: {detail}")

    def within(self, *prefix: str | int) -> Self:
        """Return the same error located below a field of a larger input."""
        return type(self)(self.code, self.detail, path=(*prefix, *self.path))


@contextmanager
def located(*path: str | int) -> Iterator[None]:
    """Locate every orchestrator error raised inside below one input field."""
    try:
        yield
    except OrchestratorError as err:
        raise err.within(*path) from err


class ValidationError(OrchestratorError):
    """Input violates a domain contract."""


class ConflictError(OrchestratorError):
    """Requested mutation conflicts with current state."""


class PlanningError(OrchestratorError):
    """No exact execution plan can satisfy an intent."""


class StaleCommandError(OrchestratorError):
    """A physical command has been invalidated by generation fencing."""


class DispatchNotStartedError(ConflictError):
    """The adapter proves no cleaning-start command was issued."""


class StorageIntegrityError(OrchestratorError):
    """Critical state cannot be proven to match persisted data."""
