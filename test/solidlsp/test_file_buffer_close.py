import threading
from pathlib import Path
from unittest.mock import MagicMock

from solidlsp.ls import LSPFileBuffer


def _open_buffer(tmp_path: Path, notify: MagicMock, ref_count: int) -> LSPFileBuffer:
    path = tmp_path / "main.py"
    path.write_text("x = 1\n", encoding="utf-8")
    language_server = MagicMock()
    language_server.server.notify = notify
    return LSPFileBuffer(
        abs_path=path,
        uri=path.as_uri(),
        encoding="utf-8",
        version=0,
        language_id="python",
        ref_count=ref_count,
        language_server=language_server,
    )


def test_close_sends_one_did_close_and_a_later_open_reopens_the_file(tmp_path: Path) -> None:
    notify = MagicMock()
    buffer = _open_buffer(tmp_path, notify, ref_count=1)

    buffer.close()
    buffer.close()

    notify.did_close_text_document.assert_called_once()
    assert notify.did_close_text_document.call_args.args[0]["textDocument"]["uri"] == buffer.uri

    buffer.ensure_open_in_ls()

    assert notify.did_open_text_document.call_count == 2


def test_a_referenced_buffer_is_left_open_and_an_unreferenced_one_is_closed(tmp_path: Path) -> None:
    notify = MagicMock()
    ref_count_lock = threading.Lock()
    buffer = _open_buffer(tmp_path, notify, ref_count=1)

    assert buffer.close_if_unreferenced(ref_count_lock) is False
    notify.did_close_text_document.assert_not_called()

    buffer.ref_count = 0

    assert buffer.close_if_unreferenced(ref_count_lock) is True
    notify.did_close_text_document.assert_called_once()
