"""Tests may not open sockets, on Windows as on CI's Linux.

See dev doc "E2E-Host": a local run must fail where CI fails.
"""

import asyncio
import socket

import pytest
from pytest_homeassistant_custom_component.plugins import HASocketBlockedError

pytestmark = pytest.mark.filterwarnings("ignore:A test tried to use socket.socket")


def test_a_test_cannot_create_a_socket() -> None:
    with pytest.raises(HASocketBlockedError) as blocked:
        socket.socket()
    HASocketBlockedError.instances.remove(blocked.value)


def test_an_event_loop_still_creates_its_wakeup_pair() -> None:
    loop = asyncio.new_event_loop()
    loop.close()
    with pytest.raises(HASocketBlockedError) as blocked:
        socket.socket()
    HASocketBlockedError.instances.remove(blocked.value)
