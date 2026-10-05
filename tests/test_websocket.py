"""Tests for the bounded WebSocket read model and subscriptions."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

import pytest
import voluptuous as vol
from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from custom_components.vacuum_orchestrator.api import websocket as websocket_api
from custom_components.vacuum_orchestrator.application.orchestrator import (
    VacuumOrchestrator,
)
from custom_components.vacuum_orchestrator.const import (
    API_VERSION,
    SIGNAL_VIEW_CHANGED,
)
from custom_components.vacuum_orchestrator.domain.intents import JobIntent, TargetRef
from custom_components.vacuum_orchestrator.domain.queue import OrchestratorState
from custom_components.vacuum_orchestrator.domain.types import CleaningMode, JobState
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
        self.runtime_id = "runtime"
        self.runtime_sequence = 1
        self.changed_scopes = frozenset({"queue", "jobs"})

    def readiness_for_job(self, job_id: str) -> object:
        assert job_id == "job"
        return None

    def readiness_before_start(self, job_id: str) -> object:
        assert job_id == "job"
        return None

    def subscribe_view(self, listener: Callable[[], None]) -> Callable[[], None]:
        self.listener = listener

        def unsubscribe() -> None:
            self.unsubscribed = True

        return unsubscribe


class Connection:
    """Record outgoing WebSocket protocol messages."""

    def __init__(self) -> None:
        self.results: list[tuple[int, Any]] = []
        self.errors: list[tuple[int, str, str]] = []
        self.translations: list[tuple[int, str, str | None, Any]] = []
        self.events: list[tuple[int, Any]] = []
        self.subscriptions: dict[int, Callable[[], None]] = {}

    def send_result(self, message_id: int, result: Any = None) -> None:
        self.results.append((message_id, result))

    def context(self, msg: dict[str, Any]) -> Context:
        return Context()

    def send_error(
        self,
        message_id: int,
        code: str,
        message: str,
        translation_key: str | None = None,
        translation_domain: str | None = None,
        translation_placeholders: dict[str, Any] | None = None,
    ) -> None:
        self.errors.append((message_id, code, message))
        if translation_key is not None:
            self.translations.append(
                (
                    message_id,
                    translation_key,
                    translation_domain,
                    translation_placeholders,
                )
            )

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
        "commit_id": 1,
        "runtime_id": "runtime",
        "runtime_sequence": 1,
    }
    for _message_id, result in connection.results:
        assert (
            result["commit_id"],
            result["runtime_id"],
            result["runtime_sequence"],
        ) == (1, "runtime", 1)
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
    assert connection.events == []

    async_dispatcher_send(hass, SIGNAL_VIEW_CHANGED)

    assert connection.events == [
        (
            5,
            {
                "api_version": API_VERSION,
                "loaded": True,
                "commit_id": 1,
                "runtime_id": "runtime",
                "runtime_sequence": 1,
                "queue_revision": 1,
                "mode": "idle",
                "pending_jobs": 1,
                "needs_attention": False,
                "active_count": 0,
                "attention_count": 0,
                "changed": ["jobs", "queue"],
            },
        )
    ]
    connection.subscriptions[5]()
    async_dispatcher_send(hass, SIGNAL_VIEW_CHANGED)
    assert len(connection.events) == 1


def test_subscription_follows_the_currently_loaded_runtime(
    hass: HomeAssistant,
) -> None:
    hass.data[RUNTIME_KEY] = {}
    connection = Connection()
    websocket_api.websocket_subscribe(
        hass,
        cast(ActiveConnection, connection),
        {"id": 7, "type": websocket_api.TYPE_SUBSCRIBE},
    )
    assert connection.results == [(7, None)]
    assert connection.events == [(7, {"api_version": API_VERSION, "loaded": False})]

    reloaded = StubOrchestrator()
    reloaded.runtime_id = "reloaded"
    _install_runtime(hass, reloaded)
    async_dispatcher_send(hass, SIGNAL_VIEW_CHANGED)
    assert connection.events[-1][1]["runtime_id"] == "reloaded"
    assert connection.events[-1][1]["loaded"] is True


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
    await hass.async_block_till_done()

    assert [item[1] for item in connection.errors] == [
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
        websocket_api.websocket_configuration_get,
        websocket_api.websocket_configuration_command,
    ]


async def test_jobs_list_filters_by_state_and_validates_states(
    hass: HomeAssistant,
) -> None:
    _install_runtime(hass, StubOrchestrator())
    connection = Connection()
    active = cast(ActiveConnection, connection)
    for message_id, states in ((1, ["queued"]), (2, ["running", "canceling"])):
        websocket_api.websocket_jobs_list(
            hass,
            active,
            {
                "id": message_id,
                "type": websocket_api.TYPE_JOBS_LIST,
                "offset": 0,
                "limit": 10,
                "states": [JobState(item) for item in states],
            },
        )
    await hass.async_block_till_done()

    assert connection.results[0][1]["total"] == 1
    assert connection.results[1][1]["total"] == 0
    schema = websocket_api.websocket_jobs_list._ws_schema
    assert schema({"id": 3, "type": websocket_api.TYPE_JOBS_LIST, "states": "queued"})[
        "states"
    ] == [JobState.QUEUED]
    for invalid in (["polished"], []):
        with pytest.raises(vol.Invalid):
            schema({"id": 4, "type": websocket_api.TYPE_JOBS_LIST, "states": invalid})


async def test_queue_page_reports_active_and_attention_counts(
    hass: HomeAssistant,
) -> None:
    _install_runtime(hass, StubOrchestrator())
    connection = Connection()
    websocket_api.websocket_queue_get(
        hass,
        cast(ActiveConnection, connection),
        {"id": 1, "type": websocket_api.TYPE_QUEUE_GET, "offset": 0, "limit": 10},
    )
    await hass.async_block_till_done()
    result = connection.results[0][1]
    assert (result["active_count"], result["attention_count"]) == (0, 0)
