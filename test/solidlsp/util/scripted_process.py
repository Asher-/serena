from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

from solidlsp.ls import SolidLanguageServer
from solidlsp.ls_utils import PathUtils


def function_symbol(name: str) -> dict[str, Any]:
    """
    :param name: the name of the function
    :return: the function, defined in the first line of its file, as a language server answers it
    """
    whole = {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}
    return {"name": name, "kind": 12, "range": whole, "selectionRange": whole, "children": []}


@contextmanager
def scripted_process(server_class: type[SolidLanguageServer], answer: Callable[[Path], list[dict[str, Any]]]) -> Iterator[mock.Mock]:
    """
    Replaces the process of every server of the class made within the context by a double, which answers a
    documentSymbol request, and needs no runtime dependency of the class installed.

    :param server_class: the language server implementation
    :param answer: a function giving the symbols the double answers for the file
    :return: the double of the documentSymbol request, which counts its calls
    """
    document_symbol = mock.Mock(side_effect=lambda params: answer(Path(PathUtils.uri_to_path(params["textDocument"]["uri"]))))

    def create_process(self: SolidLanguageServer, logging_fn: object) -> mock.MagicMock:
        process = mock.MagicMock()
        process.send.document_symbol = document_symbol
        process.is_running.return_value = False
        process.process_id.return_value = None
        process.start.side_effect = lambda: process.is_running.configure_mock(return_value=True)
        process.stop.side_effect = lambda **_kwargs: process.is_running.configure_mock(return_value=False)
        return process

    replaced: dict[str, object] = {
        "_create_language_server_interface": create_process,
        "_start_server": lambda self: self.server.start(),
        "_setup_runtime_dependency": staticmethod(lambda *_args, **_kwargs: True),
    }
    with mock.patch.multiple(server_class, **{name: replacement for name, replacement in replaced.items() if hasattr(server_class, name)}):
        yield document_symbol
