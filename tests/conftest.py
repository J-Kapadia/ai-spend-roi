"""Socket guard: no test may open a network connection (plan D-12)."""

import socket

import pytest


@pytest.fixture(autouse=True)
def _socket_guard(monkeypatch):
    def refuse(self, *args, **kwargs):
        raise RuntimeError("network access is forbidden in tests (socket guard)")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
