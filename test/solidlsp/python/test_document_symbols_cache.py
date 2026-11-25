"""
A language server's document symbols are cached in proportion to the symbols, never to the text of their files: what a
server served survives its stop and is served again without asking the language server, a cache left in the former
whole-file format is removed unread, and the symbols held in memory are bounded by the configured number of files.
"""

import logging
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import pytest

from serena.config.serena_config import SerenaPaths
from serena.constants import SERENA_MANAGED_DIR_NAME
from serena.util.file_system import IgnoreSpecs
from solidlsp.ls import SolidLanguageServer
from solidlsp.ls_config import LanguageServerConfig, LanguageServerId
from solidlsp.settings import SolidLSPSettings

pytestmark = pytest.mark.python

# the files whose former whole-file caches a server removes when it is made
_LEGACY_CACHE_FILES = ("document_symbols.pkl", "raw_document_symbols.pkl")


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


def _create(root: Path, max_cached_files: int | None = None) -> SolidLanguageServer:
    """
    :param root: the project directory
    :param max_cached_files: the number of files whose document symbols the server holds in memory, or None for the
        default
    :return: a python language server for the project, not yet started
    """
    config = LanguageServerConfig(
        ls_id=LanguageServerId.PYTHON, ignore_spec_of=IgnoreSpecs(str(root), [], read_gitignore_files=True).spec_of
    )
    settings_kwargs = {} if max_cached_files is None else {"document_symbols_cache_max_entries": max_cached_files}
    settings = SolidLSPSettings(
        solidlsp_dir=SerenaPaths().serena_user_home_dir,
        project_data_path=os.path.join(root, SERENA_MANAGED_DIR_NAME),
        **settings_kwargs,
    )
    return SolidLanguageServer.create(config, str(root), solidlsp_settings=settings)


@contextmanager
def _running(root: Path, max_cached_files: int | None = None) -> Iterator[SolidLanguageServer]:
    """
    :param root: the project directory
    :param max_cached_files: as for :func:`_create`
    :return: a started python language server for the project, stopped on exit
    """
    server = _create(root, max_cached_files)
    server.start()
    try:
        yield server
    finally:
        server.stop()


@contextmanager
def _counting(server: SolidLanguageServer) -> Iterator[dict[str, int]]:
    """
    Counts how often the server asks its language server for a file's symbols and how often it builds a file's
    high-level symbols from the language server's ones.

    :param server: the server
    :return: the counts, keyed "requests" and "builds"
    """
    counts = {"requests": 0, "builds": 0}
    server_type = type(server)
    request = server_type._request_raw_document_symbols
    build = server_type._build_document_symbols_from_raw_symbols

    def counted_request(self, *args, **kwargs):
        counts["requests"] += 1
        return request(self, *args, **kwargs)

    def counted_build(self, *args, **kwargs):
        counts["builds"] += 1
        return build(self, *args, **kwargs)

    with (
        mock.patch.object(server_type, "_request_raw_document_symbols", counted_request),
        mock.patch.object(server_type, "_build_document_symbols_from_raw_symbols", counted_build),
    ):
        yield counts


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


def test_a_stop_leaves_no_writer_of_the_cache_behind(tmp_path: Path) -> None:
    root = _project(tmp_path / "project", 2)
    writers_before = {thread for thread in threading.enumerate() if thread.name.startswith("SymbolStoreWriter")}
    with _running(root) as server:
        _names(server, "module_0.py")
        server.save_cache()
        _names(server, "module_1.py")
        server.save_cache()

    assert [t for t in threading.enumerate() if t.name.startswith("SymbolStoreWriter") and t not in writers_before] == []
