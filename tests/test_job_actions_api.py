"""Jobs and the queue name the actions VOI allows now and why others are not."""

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from custom_components.vacuum_orchestrator.runtime import async_get_runtime
from tests.application.test_orchestrator import RecordingAdapter, RecordingBackend
from tests.test_configuration_api import call, configured  # noqa: F401
from tests.test_job_holds_api import Card


def offered(actions: dict[str, Any]) -> dict[str, str | None]:
    """Return each action's reason; `None` means offered."""
    return {
        name: None if item["available"] else item["reason"]
        for name, item in actions.items()
    }


@pytest.mark.usefixtures("configured")
async def test_waiting_jobs_offer_what_their_commands_accept(
    hass: HomeAssistant,
) -> None:
    room = (await call(hass, "create_room", name="Office"))["room_id"]
    first = (await call(hass, "create_job", areas=[room]))["job_id"]
    second = (await call(hass, "create_job", areas=[room]))["job_id"]

    queue = await call(hass, "get_queue")
    assert offered(queue["actions"]) == {
        "run": None,
        "pause": "queue_not_running",
        "resume": "queue_not_running",
        "end": "queue_not_running",
    }
    assert offered(queue["jobs"][0]["actions"]) == {
        "edit": None,
        "delete": None,
        "cancel": "job_not_started",
        "start": "job_not_startable",
        "retry": "job_not_retryable",
        "move_up": "job_at_top",
        "move_down": None,
    }
    assert queue["jobs"][1]["actions"]["move_down"]["reason"] == "job_at_bottom"

    # Released and reachable: start bypasses the idle queue and the delay.
    await call(hass, "release_room", room_id=room, kind="permanent")
    core = async_get_runtime(hass).orchestrator
    adapter = RecordingAdapter(RecordingBackend(), "robot", targets=(room,))
    await core.async_replace_adapters({"robot": adapter})
    await core.async_observed_snapshot()
    detail = await call(hass, "get_job", job_id=first)
    assert detail["waiting"]["code"] == "queue_idle"
    assert detail["actions"]["start"] == {
        "available": True,
        "reason": None,
        "detail": None,
    }

    held = await Card(hass).command("hold_job", job_id=second, purpose="edit")
    assert held["job"]["actions"]["edit"] == {
        "available": False,
        "reason": "job_held",
        "detail": "edit",
    }
    await call(hass, "run_queue")
    queue = await call(hass, "get_queue")
    assert offered(queue["actions"])["pause"] is None
    assert offered(queue["actions"])["run"] == "queue_running"


@pytest.mark.usefixtures("configured")
async def test_commands_refuse_with_the_reason_their_action_names(
    hass: HomeAssistant,
) -> None:
    room = (await call(hass, "create_room", name="Office"))["room_id"]
    job = (await call(hass, "create_job", areas=[room]))["job_id"]
    await Card(hass).command("hold_job", job_id=job, purpose="confirm")
    actions = (await call(hass, "get_job", job_id=job))["actions"]

    for action, service in (("retry", "retry_job"), ("delete", "delete_job")):
        with pytest.raises(ServiceValidationError) as refused:
            await call(hass, service, job_id=job)
        assert refused.value.translation_key == actions[action]["reason"]
