"""Repair hint while the companion dashboard card is not installed."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any
from urllib.parse import urlsplit

from homeassistant.components.lovelace.const import LOVELACE_DATA
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

ISSUE_ID = "voc_card_missing"
CARD_FILES = frozenset(
    {"vacuum-orchestrator-card.js", "vacuum-orchestrator-card-dev.js"}
)
CARD_URL = (
    "https://my.home-assistant.io/redirect/hacs_repository/"
    "?owner=hyperfelixations&repository=vacuum-orchestrator-card&category=plugin"
)
_LOGGER = logging.getLogger(__name__)


async def async_track_card(hass: HomeAssistant) -> Callable[[], None]:
    """Check dashboard resources now and after every stored resource change."""
    await _async_check(hass)
    data = hass.data.get(LOVELACE_DATA)
    add_listener = getattr(
        getattr(data, "resources", None), "async_add_change_set_listener", None
    )
    if add_listener is None:
        return lambda: None

    async def changed(_changes: Iterable[Any]) -> None:
        await _async_check(hass)

    return add_listener(changed)  # type: ignore[no-any-return]


def async_remove_issue(hass: HomeAssistant) -> None:
    """Forget the hint, including a user's choice to ignore it."""
    ir.async_delete_issue(hass, DOMAIN, ISSUE_ID)


async def _async_check(hass: HomeAssistant) -> None:
    data = hass.data.get(LOVELACE_DATA)
    if data is None:
        _LOGGER.debug("Dashboard resources unavailable; card check skipped")
        return
    try:
        await data.resources.async_get_info()
        urls = [str(item.get("url", "")) for item in data.resources.async_items()]
    except Exception as err:
        _LOGGER.debug("Dashboard resources unreadable; card check skipped: %s", err)
        return
    if any(urlsplit(url).path.rsplit("/", 1)[-1] in CARD_FILES for url in urls):
        ir.async_delete_issue(hass, DOMAIN, ISSUE_ID)
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_ID,
        is_fixable=False,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        learn_more_url=CARD_URL,
        translation_key=ISSUE_ID,
        translation_placeholders={"url": CARD_URL},
    )
