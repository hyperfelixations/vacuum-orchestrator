"""One translated error shape for actions and WebSocket handlers."""

from __future__ import annotations

from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.exceptions import ServiceValidationError

from ..const import DOMAIN
from ..domain.errors import OrchestratorError


def service_error(err: OrchestratorError) -> ServiceValidationError:
    """Translate by code and keep the raw detail for clients."""
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=err.code,
        translation_placeholders={
            "code": err.code,
            "detail": err.detail or "",
            "field": ".".join(map(str, err.path)),
        },
    )


def send_websocket_error(
    connection: ActiveConnection, message_id: int, err: OrchestratorError
) -> None:
    """Send the same code, message and translation fields as an action error."""
    error = service_error(err)
    connection.send_error(
        message_id,
        err.code,
        str(error),
        translation_key=error.translation_key,
        translation_domain=error.translation_domain,
        translation_placeholders=error.translation_placeholders,
    )
