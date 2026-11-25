"""
A language server's document symbols are cached in proportion to the symbols, never to the text of their files: what a
server served survives its stop and is served again without asking the language server, a cache left in the former
whole-file format is removed unread, serving or saving one file's symbols touches that file's entry only, and the
symbols held in memory are bounded by the configured number of files.

No language marker: the language server process is replaced by a double answering at the protocol seam.
"""

import logging
import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from fractions import Fraction
from pathlib import Path
from typing import Any
from unittest import mock
from unittest.mock import MagicMock

import psutil
import pytest

from serena.config.serena_config import SerenaPaths
from serena.constants import SERENA_MANAGED_DIR_NAME
from serena.util.file_system import IgnoreSpecs
from solidlsp import ls_types
from solidlsp.ls import SolidLanguageServer
from solidlsp.ls_config import LanguageServerConfig, LanguageServerId
from solidlsp.ls_process import LanguageServerInterface
from solidlsp.ls_utils import PathUtils
from solidlsp.lsp_protocol_handler.server import StringDict
from solidlsp.settings import SolidLSPSettings

# the files whose former whole-file caches a server removes when it is made
_LEGACY_CACHE_FILES = ("document_symbols.pkl", "raw_document_symbols.pkl")


class _ScriptedLanguageServer(SolidLanguageServer):
    """
    A language server whose process is replaced by a double, which answers a documentSymbol request with the function a
    file defines in its first two lines, and with nothing for a file of fewer lines.
    """

    def __init__(self, root: Path, settings: SolidLSPSettings, details: dict[str, object]) -> None:
        """
        :param root: the project directory
        :param settings: the settings of the server
        :param details: the detail of the symbols the double answers, by the relative path of their file
        """
        self._details = details
        self._running = False
        self._requests = 0
        config = LanguageServerConfig(
            ls_id=LanguageServerId.PYTHON, ignore_spec_of=IgnoreSpecs(str(root), [], read_gitignore_files=True).spec_of
        )
        super().__init__(config, str(root), None, "python", settings)

    def _create_language_server_interface(self, logging_fn: Callable[[str, str, StringDict | str], None] | None) -> LanguageServerInterface:
        server = MagicMock()
        server.send.document_symbol.side_effect = lambda params: self._answer(Path(PathUtils.uri_to_path(params["textDocument"]["uri"])))
        server.is_running.side_effect = lambda: self._running
        server.process_id.return_value = None
        server.stop.side_effect = lambda **_kwargs: self._set_running(False)
        return server

    def _answer(self, path: Path) -> list[dict[str, Any]]:
        self._requests += 1
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) < 2:
            return []
        definition, body = lines[:2]
        name = definition.removeprefix("def ").removesuffix("():")
        symbol: dict[str, Any] = {
            "name": name,
            "kind": ls_types.SymbolKind.Function.value,
            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 1, "character": len(body)}},
            "selectionRange": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 4 + len(name)}},
            "children": [],
        }
        if path.name in self._details:
            symbol["detail"] = self._details[path.name]
        return [symbol]

    def _set_running(self, running: bool) -> None:
        self._running = running

    def _start_server(self) -> None:
        self._set_running(True)

    def _create_base_initialize_params(self) -> dict:
        return {}

    def requests(self) -> int:
        """
        :return: the number of documentSymbol requests the double was asked
        """
        return self._requests


def _project(root: Path, file_count: int, padding: str = "") -> Path:
    """
    Writes a project of python modules, each defining one function.

    :param root: the project directory to create
    :param file_count: the number of modules
    :param padding: text appended to every module, which defines no symbol
    :return: the project directory
    """
    root.mkdir(parents=True)
    for index in range(file_count):
        (root / f"module_{index}.py").write_text(f"def function_{index}():\n    return {index}\n{padding}", encoding="utf-8")
    return root


def _create(root: Path, max_cached_files: int | None = None, details: dict[str, object] | None = None) -> _ScriptedLanguageServer:
    """
    :param root: the project directory
    :param max_cached_files: the number of files whose document symbols the server holds in memory, or None for the
        default
    :param details: the detail of the symbols the language server answers, by the relative path of their file
    :return: a language server for the project, not yet started
    """
    settings_kwargs = {} if max_cached_files is None else {"document_symbols_cache_max_entries": max_cached_files}
    settings = SolidLSPSettings(
        solidlsp_dir=SerenaPaths().serena_user_home_dir,
        project_data_path=os.path.join(root, SERENA_MANAGED_DIR_NAME),
        **settings_kwargs,
    )
    return _ScriptedLanguageServer(root, settings, details or {})


@contextmanager
def _running(
    root: Path, max_cached_files: int | None = None, details: dict[str, object] | None = None
) -> Iterator[_ScriptedLanguageServer]:
    """
    :param root: the project directory
    :param max_cached_files: as for :func:`_create`
    :param details: as for :func:`_create`
    :return: a started language server for the project, whose symbols are saved and which is stopped on exit
    """
    server = _create(root, max_cached_files, details)
    server.start()
    try:
        yield server
    finally:
        server.save_cache()
        server.stop()


@contextmanager
def _counting(server: _ScriptedLanguageServer) -> Iterator[dict[str, int]]:
    """
    Counts how often the server asks its language server for a file's symbols and how often it builds a file's
    high-level symbols from the language server's ones.

    :param server: the server
    :return: the counts, keyed "requests" and "builds", which are set when the context ends
    """
    counts: dict[str, int] = {}
    requests_before = server.requests()
    build = type(server)._build_document_symbols_from_raw_symbols
    with mock.patch.object(type(server), "_build_document_symbols_from_raw_symbols", autospec=True, side_effect=build) as built:
        yield counts
    counts["requests"] = server.requests() - requests_before
    counts["builds"] = built.call_count


def _names(server: SolidLanguageServer, relative_path: str) -> list[str]:
    """
    :return: the names of the root symbols the server serves for the file
    """
    return [symbol["name"] for symbol in server.request_document_symbols(relative_path).root_symbols]


def _bytes_in(directory: Path) -> int:
    """
    :return: the total size of the files under the directory
    """
    return sum(path.stat().st_size for path in directory.rglob("*") if path.is_file())


def test_the_cache_of_a_file_holds_its_symbols_and_not_its_text(tmp_path: Path) -> None:
    # one function and 400 kB of comment, which defines no symbol
    root = _project(tmp_path / "project", 1, padding="# padding text that defines no symbol\n" * 10_000)

    with _running(root) as server:
        assert _names(server, "module_0.py") == ["function_0"]
        server.save_cache()
        cache_dir = server.cache_dir

    assert _bytes_in(cache_dir) < 64 * 1024 < (root / "module_0.py").stat().st_size


def test_symbols_served_before_a_stop_are_served_by_the_next_server_without_asking_the_language_server(tmp_path: Path) -> None:
    root = _project(tmp_path / "project", 3)
    with _running(root) as server:
        for index in range(3):
            _names(server, f"module_{index}.py")

    with _running(root) as server, _counting(server) as counts:
        assert [_names(server, f"module_{index}.py") for index in range(3)] == [["function_0"], ["function_1"], ["function_2"]]
    assert counts["requests"] == 0


def test_a_changed_file_is_asked_of_the_language_server_again(tmp_path: Path) -> None:
    root = _project(tmp_path / "project", 1)
    with _running(root) as server:
        _names(server, "module_0.py")
    (root / "module_0.py").write_text("def renamed():\n    return 0\n", encoding="utf-8")

    with _running(root) as server, _counting(server) as counts:
        assert _names(server, "module_0.py") == ["renamed"]
    assert counts["requests"] == 1


def test_a_file_the_language_server_answers_nothing_for_is_asked_again(tmp_path: Path) -> None:
    root = _project(tmp_path / "project", 1)
    (root / "module_0.py").write_text("# no symbol\n", encoding="utf-8")

    with _running(root) as server, _counting(server) as counts:
        assert _names(server, "module_0.py") == []
        assert _names(server, "module_0.py") == []
    assert counts == {"requests": 2, "builds": 2}


def test_a_files_persisted_symbols_are_served_while_another_files_persisted_entry_cannot_be_loaded(tmp_path: Path) -> None:
    root = _project(tmp_path / "project", 2)
    # a value of a class the cache's loader refuses, so that the entry holding it is saved and cannot be loaded
    with _running(root, details={"module_0.py": Fraction(1, 2)}) as server:
        _names(server, "module_0.py")
        _names(server, "module_1.py")

    with _running(root) as server:
        with _counting(server) as of_the_other_file:
            assert _names(server, "module_1.py") == ["function_1"]
        with _counting(server) as of_the_unloadable_entry:
            assert _names(server, "module_0.py") == ["function_0"]

    assert of_the_other_file["requests"] == 0
    assert of_the_unloadable_entry["requests"] == 1


def test_a_servers_save_leaves_the_entries_another_server_saved_in_the_same_cache(tmp_path: Path) -> None:
    root = _project(tmp_path / "project", 2)
    with _running(root) as first:
        with _running(root) as second:
            _names(second, "module_1.py")
        _names(first, "module_0.py")

    with _running(root) as server, _counting(server) as counts:
        assert _names(server, "module_0.py") == ["function_0"]
        assert _names(server, "module_1.py") == ["function_1"]
    assert counts["requests"] == 0


def test_a_cache_in_the_former_whole_file_format_is_removed_unread_and_its_removal_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = _project(tmp_path / "project", 1)
    cache_dir = Path(_create(root).cache_dir)
    # bytes no reader of the former format can load: a read of them would be logged as a failure
    for index, name in enumerate(_LEGACY_CACHE_FILES):
        (cache_dir / name).write_bytes(b"not a pickle" * (100 + index))

    with caplog.at_level(logging.INFO), _running(root) as server:
        assert _names(server, "module_0.py") == ["function_0"]

    for index, name in enumerate(_LEGACY_CACHE_FILES):
        assert not (cache_dir / name).exists()
        assert any(
            name in record.getMessage() and str(len(b"not a pickle") * (100 + index)) in record.getMessage() for record in caplog.records
        )
    assert "Failed to load" not in caplog.text


def test_a_cache_in_the_former_whole_file_format_that_cannot_be_removed_is_logged_and_left(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = _project(tmp_path / "project", 1)
    cache_dir = Path(_create(root).cache_dir)
    # a directory, which no removal of a file removes, where the first of the former files would be
    unremovable, removable = (cache_dir / name for name in _LEGACY_CACHE_FILES)
    unremovable.mkdir()
    removable.write_bytes(b"not a pickle")

    with caplog.at_level(logging.WARNING), _running(root) as server:
        assert _names(server, "module_0.py") == ["function_0"]

    assert unremovable.is_dir()
    assert not removable.exists()
    assert any("Failed to remove" in record.getMessage() and unremovable.name in record.getMessage() for record in caplog.records)


def test_a_file_whose_symbols_are_held_is_served_without_building_them_again(tmp_path: Path) -> None:
    root = _project(tmp_path / "project", 2)
    with _running(root, max_cached_files=2) as server:
        _names(server, "module_0.py")
        _names(server, "module_1.py")
        with _counting(server) as counts:
            assert _names(server, "module_0.py") == ["function_0"]
            assert _names(server, "module_1.py") == ["function_1"]
    assert counts == {"requests": 0, "builds": 0}


def test_a_file_whose_symbols_were_let_go_is_built_again_without_asking_the_language_server(tmp_path: Path) -> None:
    root = _project(tmp_path / "project", 3)
    with _running(root, max_cached_files=2) as server:
        for index in range(3):
            _names(server, f"module_{index}.py")
        with _counting(server) as counts:
            assert _names(server, "module_0.py") == ["function_0"]
    assert counts == {"requests": 0, "builds": 1}


def test_the_symbols_held_in_memory_never_exceed_the_configured_number_of_files(tmp_path: Path) -> None:
    root = _project(tmp_path / "project", 6)
    with _running(root, max_cached_files=2) as server:
        for index in range(6):
            _names(server, f"module_{index}.py")

        # each file requested again, most recent first: a file served without a build was held
        held = 0
        for index in reversed(range(6)):
            with _counting(server) as counts:
                _names(server, f"module_{index}.py")
            held += counts["builds"] == 0

    assert held == 2


def test_the_symbols_served_carry_the_text_and_paths_of_their_file(tmp_path: Path) -> None:
    root = _project(tmp_path / "project", 1)
    with _running(root) as server:
        _names(server, "module_0.py")
        [symbol] = server.request_document_symbols("module_0.py").root_symbols

    assert symbol["body"].get_text() == "def function_0():\n    return 0"
    assert symbol["location"]["relativePath"] == "module_0.py"
    assert Path(symbol["location"]["absolutePath"]).resolve() == (root / "module_0.py").resolve()
    assert Path(symbol["location"]["uri"].removeprefix("file://")).resolve() == (root / "module_0.py").resolve()


def test_a_stop_leaves_no_writer_and_no_connection_of_the_cache_behind(tmp_path: Path) -> None:
    root = _project(tmp_path / "project", 2)
    writers_before = {thread for thread in threading.enumerate() if thread.name.startswith("SymbolStoreWriter")}
    with _running(root) as server:
        _names(server, "module_0.py")
        server.save_cache()
        _names(server, "module_1.py")
        server.save_cache()
        cache_dir = Path(server.cache_dir).resolve()

    assert [t for t in threading.enumerate() if t.name.startswith("SymbolStoreWriter") and t not in writers_before] == []
    assert [opened.path for opened in psutil.Process().open_files() if Path(opened.path).resolve().is_relative_to(cache_dir)] == []


def test_a_change_one_caller_makes_to_the_symbols_it_was_served_is_not_served_to_the_next(tmp_path: Path) -> None:
    root = _project(tmp_path / "project", 1)
    with _running(root) as server:
        for _ in range(2):
            [served] = server.request_document_symbols("module_0.py").root_symbols
            served["name"] = "changed"
            served["location"]["relativePath"] = "changed.py"
            served.pop("body")
            contained = server.request_containing_symbol("module_0.py", 1, 4)
            assert contained is not None
            contained["name"] = "changed"
            contained["location"]["relativePath"] = "changed.py"

        [symbol] = server.request_document_symbols("module_0.py").root_symbols
        contained = server.request_containing_symbol("module_0.py", 1, 4)

    assert symbol["name"] == "function_0"
    assert symbol["location"]["relativePath"] == "module_0.py"
    assert symbol["body"].get_text() == "def function_0():\n    return 0"
    assert contained is not None
    assert contained["name"] == "function_0"
    assert contained["location"]["relativePath"] == "module_0.py"
