"""Domain-specific exceptions with stable reason codes."""


class OrchestratorError(Exception):
    """Base error carrying a stable machine-readable code."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if detail is None else f"{code}: {detail}")


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
