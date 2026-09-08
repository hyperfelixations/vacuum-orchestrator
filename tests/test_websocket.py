"""Tests for the bounded WebSocket read model and subscriptions."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.core import HomeAssistant

from custom_components.vacuum_orchestrator.api import websocket as websocket_api
from custom_components.vacuum_orchestrator.application.orchestrator import (
    VacuumOrchestrator,
)
from custom_components.vacuum_orchestrator.const import API_VERSION
from custom_components.vacuum_orchestrator.domain.intents import JobIntent, TargetRef
from custom_components.vacuum_orchestrator.domain.queue import OrchestratorState
from custom_components.vacuum_orchestrator.domain.types import CleaningMode
from custom_components.vacuum_orchestrator.runtime import (
    RUNTIME_KEY,
    VacuumOrchestratorRuntime,
)

NOW = datetime(2026, 9, 7, 12, tzinfo=UTC)


class StubOrchestrator:
    """Expose the read-side methods consumed by the WebSocket layer."""

    def __init__(self) -> None:
        intent = JobIntent((TargetRef("kitchen"),), CleaningMode.VACUUM)
        self.state = OrchestratorState.empty("installation").add_job("job", intent, NOW)
        self.listener: Callable[[], None] | None = None
        self.unsubscribed = False

    def readiness_for_job(self, job_id: str) -> object:
        assert job_id == "job"
        return None

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        self.listener = listener

        def unsubscribe() -> None:
            self.unsubscribed = True

        return unsubscribe


class Connection:
    """Record outgoing WebSocket protocol messages."""

    def __init__(self) -> None:
        self.results: list[tuple[int, Any]] = []
        self.errors: list[tuple[int, str, str]] = []
        self.events: list[tuple[int, Any]] = []
        self.subscriptions: dict[int, Callable[[], None]] = {}

    def send_result(self, message_id: int, result: Any = None) -> None:
        self.results.append((message_id, result))

    def send_error(self, message_id: int, code: str, message: str) -> None:
        self.errors.append((message_id, code, message))

    def send_event(self, message_id: int, event: Any) -> None:
        self.events.append((message_id, event))


def _install_runtime(hass: HomeAssistant, orchestrator: StubOrchestrator) -> None:
    hass.data[RUNTIME_KEY] = {
        "entry": VacuumOrchestratorRuntime(cast(VacuumOrchestrator, orchestrator), ())
    }


async def test_websocket_queue_job_and_registry_queries(
    hass: HomeAssistant,
) -> None:
    orchestrator = StubOrchestrator()
    _install_runtime(hass, orchestrator)
    connection = Connection()
    active = cast(ActiveConnection, connection)

    websocket_api.websocket_queue_get(
        hass,
        active,
        {"id": 1, "type": websocket_api.TYPE_QUEUE_GET, "offset": 0, "limit": 10},
    )
    websocket_api.websocket_job_get(
        hass,
        active,
        {"id": 2, "type": websocket_api.TYPE_JOB_GET, "job_id": "job"},
    )
    websocket_api.websocket_job_get(
        hass,
        active,
        {"id": 3, "type": websocket_api.TYPE_JOB_GET, "job_id": "missing"},
    )
    websocket_api.websocket_jobs_list(
        hass,
        active,
        {"id": 4, "type": websocket_api.TYPE_JOBS_LIST, "offset": 0, "limit": 10},
    )
    await hass.async_block_till_done()

    assert connection.results[0][1]["jobs"][0]["job_id"] == "job"
    assert connection.results[1][1]["mode"] == "vacuum"
    assert connection.results[2][1] == {
        "api_version": API_VERSION,
        "total": 1,
        "offset": 0,
        "limit": 10,
        "jobs": [connection.results[2][1]["jobs"][0]],
    }
    assert connection.errors == [(3, "unknown_job", "unknown_job")]


def test_websocket_subscription_emits_lightweight_commit_notification(
    hass: HomeAssistant,
) -> None:
    orchestrator = StubOrchestrator()
    _install_runtime(hass, orchestrator)
    connection = Connection()

    websocket_api.websocket_subscribe(
        hass,
        cast(ActiveConnection, connection),
        {"id": 5, "type": websocket_api.TYPE_SUBSCRIBE},
    )
    assert connection.results == [(5, None)]
    assert orchestrator.listener is not None

    orchestrator.listener()

    assert connection.events == [
        (
            5,
            {
                "api_version": API_VERSION,
                "commit_id": 1,
                "queue_revision": 1,
                "mode": "idle",
                "pending_jobs": 1,
                "needs_attention": False,
            },
        )
    ]
    connection.subscriptions[5]()
    assert orchestrator.unsubscribed


async def test_websocket_handlers_report_unloaded_runtime(
    hass: HomeAssistant,
) -> None:
    hass.data[RUNTIME_KEY] = {}
    connection = Connection()
    active = cast(ActiveConnection, connection)

    websocket_api.websocket_queue_get(
        hass,
        active,
        {"id": 1, "type": websocket_api.TYPE_QUEUE_GET, "offset": 0, "limit": 10},
    )
    websocket_api.websocket_job_get(
        hass,
        active,
        {"id": 2, "type": websocket_api.TYPE_JOB_GET, "job_id": "job"},
    )
    websocket_api.websocket_jobs_list(
        hass,
        active,
        {"id": 3, "type": websocket_api.TYPE_JOBS_LIST, "offset": 0, "limit": 10},
    )
    websocket_api.websocket_subscribe(
        hass,
        active,
        {"id": 4, "type": websocket_api.TYPE_SUBSCRIBE},
    )
    await hass.async_block_till_done()

    assert [item[1] for item in connection.errors] == [
        "orchestrator_not_loaded",
        "orchestrator_not_loaded",
        "orchestrator_not_loaded",
        "orchestrator_not_loaded",
    ]


def test_websocket_setup_registers_all_commands(
    hass: HomeAssistant, monkeypatch: Any
) -> None:
    registered: list[object] = []
    monkeypatch.setattr(
        websocket_api,
        "async_register_command",
        lambda _hass, command: registered.append(command),
    )

    websocket_api.async_setup_websocket(hass)

    assert registered == [
        websocket_api.websocket_queue_get,
        websocket_api.websocket_job_get,
        websocket_api.websocket_jobs_list,
        websocket_api.websocket_subscribe,
    ]
