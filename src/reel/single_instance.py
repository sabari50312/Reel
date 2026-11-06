"""One player at a time.

Double-clicking a second episode, or "Open with Reel" while Reel is already
playing, used to start a whole second player: two windows, two audio outputs
fighting over the sound device. This module hands the file to the running window
instead and exits.

Mechanism: a named local socket (`QLocalServer`, i.e. a named pipe on Windows).
The first instance hosts it; every later launch connects, sends one small JSON
message, and quits. A stale endpoint left by a crash is removed before listening.
"""

from __future__ import annotations

import json
import os

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

_CONNECT_TIMEOUT_MS = 400
_SERVER_NAME = "reel-single-" + (os.environ.get("USERNAME") or "user").lower()


class SingleInstance(QObject):
    """Hosts the "only one Reel" socket; emits `message` with a forwarded path."""

    message = Signal(str)  # file path from another launch, "" when none was given

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._server: QLocalServer | None = None
        self._socket: QLocalSocket | None = None

    def acquire(self, payload: str = "") -> bool:
        """True when this process is the primary instance.

        When another instance answers, `payload` is forwarded to it and this
        returns False so the caller can exit immediately.
        """
        probe = QLocalSocket()
        probe.connectToServer(_SERVER_NAME)
        if probe.waitForConnected(_CONNECT_TIMEOUT_MS):
            probe.write(json.dumps({"file": payload}).encode("utf-8"))
            probe.flush()
            probe.waitForBytesWritten(_CONNECT_TIMEOUT_MS)
            # Stay connected until the host closes the other end: dropping the
            # socket immediately can discard the payload before it is read.
            probe.waitForDisconnected(_CONNECT_TIMEOUT_MS)
            return False

        QLocalServer.removeServer(_SERVER_NAME)  # endpoint left behind by a crash
        server = QLocalServer(self)
        server.newConnection.connect(self._on_connection)
        if not server.listen(_SERVER_NAME):
            # Refusing to start would be worse than allowing two windows.
            return True
        self._server = server
        return True

    def _on_connection(self) -> None:
        if self._server is None:
            return
        socket = self._server.nextPendingConnection()
        if socket is None:
            return
        self._socket = socket
        socket.readyRead.connect(self._on_ready)
        socket.disconnected.connect(self._on_done)
        socket.errorOccurred.connect(lambda _error: self._on_done())
        if socket.bytesAvailable():
            self._on_ready()  # data can arrive before the signal is connected

    def _on_ready(self) -> None:
        if self._socket is None:
            return
        raw = bytes(self._socket.readAll().data())
        if not raw:
            return
        try:
            path = str(json.loads(raw.decode("utf-8")).get("file", ""))
        except (ValueError, UnicodeDecodeError, AttributeError):
            path = ""
        self.message.emit(path)

    def _on_done(self) -> None:
        if self._socket is not None:
            self._socket.deleteLater()
            self._socket = None

    def close(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None


__all__ = ["SingleInstance"]
