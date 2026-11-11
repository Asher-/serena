import os
import threading
from collections.abc import Callable
from typing import Any

import pytest

from solidlsp import SolidLanguageServer
from solidlsp.ls_config import LanguageServerId

pytestmark = pytest.mark.python

_FILE = os.path.join("test_repo", "variables.py")
_THREAD_COUNT = 8


def _run_in_threads(work: Callable[[], None]) -> list[BaseException]:
    errors: list[BaseException] = []

    def run() -> None:
        try:
            work()
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=run) for _ in range(_THREAD_COUNT)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads)
    return errors


@pytest.mark.parametrize("language_server", [LanguageServerId.PYTHON], indirect=True)
def test_threads_holding_one_file_at_once_open_it_on_the_language_server_once(
    language_server: SolidLanguageServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    notify = language_server.server.notify
    opened: list[str] = []
    open_document = notify.did_open_text_document

    def counted_open_document(params: Any) -> None:
        opened.append(params["textDocument"]["uri"])
        open_document(params)

    monkeypatch.setattr(notify, "did_open_text_document", counted_open_document)
    all_inside = threading.Barrier(_THREAD_COUNT, timeout=30)

    def open_and_wait_for_the_others() -> None:
        with language_server.open_file(_FILE):
            all_inside.wait()

    errors = _run_in_threads(open_and_wait_for_the_others)

    assert errors == []
    assert len([uri for uri in opened if uri.endswith("variables.py")]) == 1


@pytest.mark.parametrize("language_server", [LanguageServerId.PYTHON], indirect=True)
def test_threads_repeatedly_opening_and_closing_one_file_leave_it_closed(language_server: SolidLanguageServer) -> None:
    def open_and_close_repeatedly() -> None:
        for _ in range(50):
            with language_server.open_file(_FILE) as file_buffer:
                assert file_buffer.contents

    errors = _run_in_threads(open_and_close_repeatedly)

    assert errors == []
    assert not any(uri.endswith("variables.py") for uri in language_server.open_file_buffers)
