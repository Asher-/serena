"""
Stopping a stdio language server whose process does not read its stdin: the stop ends the process within its timeout
rather than waiting on a write the process never consumes.
"""

import logging
import subprocess
import sys
import threading
from unittest.mock import MagicMock

from solidlsp.ls_config import LanguageServerId
from solidlsp.ls_process import StdioLanguageServer
from solidlsp.util import supervise


def test_stop_ends_a_server_that_never_reads_its_stdin_while_a_write_to_it_is_pending() -> None:
    server = StdioLanguageServer(
        process_launch_info=MagicMock(), ls_id=LanguageServerId.PYTHON, determine_log_level=lambda _line: logging.INFO
    )
    process = supervise.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    server._process = process

    # a message far larger than the stdin pipe holds, written on another thread as a request would be
    message = {"jsonrpc": "2.0", "method": "unread", "params": {"text": "x" * 1_000_000}}
    threading.Thread(target=server._send_payload, args=(message,), daemon=True).start()
    stopper = threading.Thread(target=server.stop, kwargs={"timeout": 2.0}, daemon=True)
    stopper.start()
    stopper.join(30)

    assert not stopper.is_alive(), "stop did not return within 30 s"
    assert process.poll() is not None
