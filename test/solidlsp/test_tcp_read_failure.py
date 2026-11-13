"""
A TCP language server whose read loop ends, whether the peer closes the connection or a BaseException that is not an
Exception aborts it, fails every pending request with LanguageServerTerminatedException.
"""

import logging
import socket
import threading
from collections.abc import Iterator

import pytest

from solidlsp.ls_config import LanguageServerId
from solidlsp.ls_exceptions import SolidLSPException
from solidlsp.ls_process import LanguageServerTerminatedException, TCPConnectionInfo, TCPLanguageServer


class _ReadAborted(BaseException):
    """A BaseException that is not an Exception, raised while the read loop dispatches a message."""


class _Peer:
    """
    A TCP peer that accepts one connection and, once the client has sent anything, either answers with a notification
    instead of a response or closes the connection.
    """

    NOTIFICATION = b'{"jsonrpc": "2.0", "method": "test/abort", "params": {}}'

    def __init__(self, close_on_request: bool) -> None:
        self._close_on_request = close_on_request
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port: int = self._listener.getsockname()[1]
        self._connection: socket.socket | None = None
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        connection, _ = self._listener.accept()
        self._connection = connection
        connection.recv(65536)
        if self._close_on_request:
            connection.shutdown(socket.SHUT_RDWR)
        else:
            connection.sendall(b"Content-Length: %d\r\n\r\n" % len(self.NOTIFICATION) + self.NOTIFICATION)

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
        self._listener.close()


@pytest.fixture
def notifying_peer() -> Iterator[_Peer]:
    peer = _Peer(close_on_request=False)
    try:
        yield peer
    finally:
        peer.close()


@pytest.fixture
def closing_peer() -> Iterator[_Peer]:
    peer = _Peer(close_on_request=True)
    try:
        yield peer
    finally:
        peer.close()


def _language_server(peer: _Peer) -> TCPLanguageServer:
    return TCPLanguageServer(
        TCPConnectionInfo(port=peer.port, connection_timeout=10.0), LanguageServerId.PYTHON, lambda _: logging.DEBUG, request_timeout=10.0
    )


def _send_request_expecting_failure(language_server: TCPLanguageServer) -> SolidLSPException:
    language_server.start()
    try:
        with pytest.raises(SolidLSPException) as raised:
            language_server.send_request("textDocument/hover")
    finally:
        language_server.stop()
    return raised.value


def test_a_read_loop_aborted_by_a_base_exception_fails_the_pending_request_with_terminated(notifying_peer: _Peer) -> None:
    language_server = _language_server(notifying_peer)

    def abort(method: str, params: object) -> None:
        raise _ReadAborted(method)

    language_server.on_any_notification(abort)
    failure = _send_request_expecting_failure(language_server)

    assert isinstance(failure.cause, LanguageServerTerminatedException)
    assert isinstance(failure.cause.cause, _ReadAborted)


def test_a_connection_the_peer_closes_fails_the_pending_request_with_terminated(closing_peer: _Peer) -> None:
    failure = _send_request_expecting_failure(_language_server(closing_peer))

    assert isinstance(failure.cause, LanguageServerTerminatedException)
