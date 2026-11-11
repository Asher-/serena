"""
Waiting for a language server's initial workspace analysis: a server adopted from an ended instance, which finished
its analysis under that instance and does not announce it again, is not waited on; any other server is waited on until
it announces the analysis or the timeout passes.
"""

import threading
import time
from unittest.mock import MagicMock

from solidlsp.language_servers.pyright_server import PyrightServer
from solidlsp.ls import SolidLanguageServer


def _language_server(adopted: bool) -> SolidLanguageServer:
    language_server = object.__new__(PyrightServer)
    language_server.server = MagicMock(adopted=adopted)
    return language_server


def test_an_adopted_server_is_not_waited_on() -> None:
    started = time.monotonic()

    assert _language_server(adopted=True)._wait_for_initial_analysis(threading.Event(), 60.0)
    assert time.monotonic() - started < 1.0


def test_a_server_that_announced_its_analysis_is_ready() -> None:
    announced = threading.Event()
    announced.set()

    assert _language_server(adopted=False)._wait_for_initial_analysis(announced, 60.0)


def test_a_server_that_has_not_announced_its_analysis_is_waited_on_until_the_timeout() -> None:
    started = time.monotonic()

    assert not _language_server(adopted=False)._wait_for_initial_analysis(threading.Event(), 0.2)
    assert time.monotonic() - started >= 0.2
