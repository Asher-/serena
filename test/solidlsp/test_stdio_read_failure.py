"""
A stdio language server whose stdout reader ends, whether the process exits or a BaseException that is not an Exception
aborts the reader, fails every pending request with LanguageServerTerminatedException.
"""

import logging
import sys
from collections.abc import Iterator

import pytest

from solidlsp.ls_config import LanguageServerId
from solidlsp.ls_exceptions import SolidLSPException
from solidlsp.ls_process import LanguageServerTerminatedException, StdioLanguageServer
from solidlsp.lsp_protocol_handler.server import ProcessLaunchInfo

_NOTIFY_ON_REQUEST = """
import sys, time
sys.stdin.buffer.readline()
body = b'{"jsonrpc": "2.0", "method": "test/abort", "params": {}}'
sys.stdout.buffer.write(b"Content-Length: %d\\r\\n\\r\\n" % len(body) + body)
sys.stdout.buffer.flush()
time.sleep(60)
"""
"""a language server process that answers the first request with a notification instead of a response"""

_EXIT_ON_REQUEST = """
import sys
sys.stdin.buffer.readline()
sys.exit(1)
"""
"""a language server process that exits when the first request arrives"""


class _ReadAborted(BaseException):
    """A BaseException that is not an Exception, raised while the stdout reader dispatches a message."""


def _language_server(script: str) -> StdioLanguageServer:
    return StdioLanguageServer(
        ProcessLaunchInfo(cmd=[sys.executable, "-c", script]), LanguageServerId.PYTHON, lambda _: logging.DEBUG, request_timeout=10.0
    )


@pytest.fixture
def notifying_server() -> Iterator[StdioLanguageServer]:
    language_server = _language_server(_NOTIFY_ON_REQUEST)
    yield language_server
    language_server.stop()


@pytest.fixture
def exiting_server() -> Iterator[StdioLanguageServer]:
    language_server = _language_server(_EXIT_ON_REQUEST)
    yield language_server
    language_server.stop()


def _send_request_expecting_failure(language_server: StdioLanguageServer) -> SolidLSPException:
    language_server.start()
    with pytest.raises(SolidLSPException) as raised:
        language_server.send_request("textDocument/hover")
    return raised.value


def test_a_read_raising_a_base_exception_fails_the_pending_request_with_terminated(notifying_server: StdioLanguageServer) -> None:
    def abort(method: str, params: object) -> None:
        raise _ReadAborted(method)

    notifying_server.on_any_notification(abort)
    failure = _send_request_expecting_failure(notifying_server)

    assert isinstance(failure.cause, LanguageServerTerminatedException)
    assert isinstance(failure.cause.cause, _ReadAborted)


def test_a_process_that_ended_fails_the_pending_request_with_terminated(exiting_server: StdioLanguageServer) -> None:
    failure = _send_request_expecting_failure(exiting_server)

    assert isinstance(failure.cause, LanguageServerTerminatedException)
