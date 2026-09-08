"""Shared Home Assistant test configuration."""

import sys

import pytest_socket

pytest_plugins = ("pytest_homeassistant_custom_component",)


def pytest_configure() -> None:
    """Adapt HA's POSIX socket guard to Windows asyncio without opening egress."""
    if sys.platform == "win32":
        pytest_socket.disable_socket = _windows_disable_socket


def _windows_disable_socket(*, allow_unix_socket: bool = False) -> None:
    """Allow socket creation while restricting connects to loopback addresses."""
    pytest_socket.socket_allow_hosts(
        ["localhost", "127.0.0.1", "::1"],
        allow_unix_socket=allow_unix_socket,
    )
