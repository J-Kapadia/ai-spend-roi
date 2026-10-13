import socket

import pytest


def test_connect_raises():
    with socket.socket() as s, pytest.raises(RuntimeError, match="socket guard"):
        s.connect(("127.0.0.1", 9))
