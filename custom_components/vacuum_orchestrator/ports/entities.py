"""Rename-safe references to entities the core only names, never reads."""

from typing import Protocol


class EntityReferences(Protocol):
    """Translate between public entity IDs and stored references."""

    def reference(self, entity_id: str) -> str:
        """Return the stored form: the registry ID when one exists."""

    def entity_id(self, reference: str) -> str | None:
        """Return the current entity ID; None when it is removed or disabled."""


class LiteralEntityReferences:
    """References that are entity IDs, for a core without an entity registry."""

    def reference(self, entity_id: str) -> str:
        """Keep the entity ID."""
        return entity_id

    def entity_id(self, reference: str) -> str | None:
        """Return the reference unchanged."""
        return reference
