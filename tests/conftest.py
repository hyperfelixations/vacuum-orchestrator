"""Shared Home Assistant test configuration."""

import socket
import sys
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import pytest
import pytest_socket

pytest_plugins = ("pytest_homeassistant_custom_component",)


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """Keep store writes out of the installed shared testing config."""
    return hass_tmp_config_dir


def pytest_addoption(parser: pytest.Parser) -> None:
    """Write the recordings of `tests/contract` for a consumer such as the card."""
    parser.addoption(
        "--record-to",
        type=Path,
        default=None,
        metavar="DIR",
        help="write every scenario's recording of tests/contract into DIR",
    )


def pytest_configure() -> None:
    """Give HA's socket guard the one exception Windows asyncio needs."""
    if sys.platform == "win32":
        pytest_socket.disable_socket = _windows_disable_socket
        socket.socketpair = _socketpair


# An event loop wakes itself through a socket pair: a Unix one on Linux,
# which HA's guard allows, a TCP one on loopback on Windows.
_PAIRING: ContextVar[bool] = ContextVar("pairing", default=False)
_disable_socket = pytest_socket.disable_socket
_true_socketpair = socket.socketpair


class _PairingSocket(socket.socket):
    """A socket only `socket.socketpair` may create."""

    def __new__(cls, *args: Any, **kwargs: Any) -> _PairingSocket:
        if not _PAIRING.get():
            raise pytest_socket.SocketBlockedError
        return super().__new__(cls, *args, **kwargs)


def _socketpair(*args: Any, **kwargs: Any) -> tuple[socket.socket, socket.socket]:
    token = _PAIRING.set(True)
    try:
        return _true_socketpair(*args, **kwargs)
    finally:
        _PAIRING.reset(token)


def _windows_disable_socket(allow_unix_socket: bool = False) -> None:
    """Block sockets as on Linux, except a socket pair."""
    _disable_socket(allow_unix_socket=allow_unix_socket)
    socket.socket = _PairingSocket  # type: ignore[misc]
