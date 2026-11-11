import os
import threading
from collections.abc import Callable
from typing import Any

import pytest

from solidlsp import SolidLanguageServer
from solidlsp.ls import LSPFileBuffer
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


@pytest.mark.parametrize("language_server", [LanguageServerId.PYTHON], indirect=True)
def test_a_file_taken_again_while_its_last_holder_closes_it_is_reopened_after_the_close(
    language_server: SolidLanguageServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    notify = language_server.server.notify
    notifications: list[str] = []
    open_document = notify.did_open_text_document
    close_document = notify.did_close_text_document
    ensure_open_in_ls = LSPFileBuffer.ensure_open_in_ls
    close_started = threading.Event()
    second_holder_entered = threading.Event()
    close_held_until_second_holder_entered: list[bool] = []

    def recorded_open_document(params: Any) -> None:
        if params["textDocument"]["uri"].endswith("variables.py"):
            notifications.append("didOpen")
        open_document(params)

    def held_back_close_document(params: Any) -> None:
        if params["textDocument"]["uri"].endswith("variables.py"):
            if not close_started.is_set():
                close_started.set()
                close_held_until_second_holder_entered.append(second_holder_entered.wait(timeout=30))
            notifications.append("didClose")
        close_document(params)

    def signalling_ensure_open_in_ls(file_buffer: LSPFileBuffer) -> None:
        if close_started.is_set():
            second_holder_entered.set()
        ensure_open_in_ls(file_buffer)

    monkeypatch.setattr(notify, "did_open_text_document", recorded_open_document)
    monkeypatch.setattr(notify, "did_close_text_document", held_back_close_document)
    monkeypatch.setattr(LSPFileBuffer, "ensure_open_in_ls", signalling_ensure_open_in_ls)
    errors: list[BaseException] = []
    second_holder_is_registered: list[bool] = []
    notifications_seen_by_second_holder: list[str] = []

    def last_holder() -> None:
        with language_server.open_file(_FILE):
            pass

    def second_holder() -> None:
        assert close_started.wait(timeout=30)
        with language_server.open_file(_FILE) as file_buffer:
            second_holder_is_registered.append(language_server.open_file_buffers.get(file_buffer.uri) is file_buffer)
            notifications_seen_by_second_holder.extend(notifications)

    def recording_errors(call: Callable[[], None]) -> Callable[[], None]:
        def run() -> None:
            try:
                call()
            except BaseException as e:
                errors.append(e)

        return run

    threads = [threading.Thread(target=recording_errors(last_holder)), threading.Thread(target=recording_errors(second_holder))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads)

    assert errors == []
    assert close_held_until_second_holder_entered == [True]
    assert second_holder_is_registered == [True]
    assert notifications_seen_by_second_holder == ["didOpen", "didClose", "didOpen"]
