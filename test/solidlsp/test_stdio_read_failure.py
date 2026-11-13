"""
A read of the language server's stdout that raises a BaseException that is not an Exception fails every pending request
with LanguageServerTerminatedException, as a read that raises an Exception does.
"""

from unittest.mock import MagicMock

from solidlsp.ls_process import LanguageServerTerminatedException, Request
from test.solidlsp.test_stdio_send_failure import _server


class _ReadAborted(BaseException):
    """A BaseException that is not an Exception, raised from a read of stdout."""


def test_a_read_raising_a_base_exception_fails_the_pending_request_with_terminated() -> None:
    server = _server()
    process = MagicMock()
    process.poll.return_value = None
    process.stdout.readline.side_effect = _ReadAborted("read aborted")
    server._process = process
    request = Request(request_id=1, method="textDocument/hover")
    server._pending_requests[1] = request

    server._read_ls_process_stdout()

    result = request.get_result(timeout=10)
    assert result.is_error()
    assert isinstance(result.error, LanguageServerTerminatedException)
    assert isinstance(result.error.cause, _ReadAborted)
