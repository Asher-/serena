"""
A symbol store persists one row per file: a row is read without reading any other, a save writes only the rows that
changed and never waits on a database another connection holds, and closing the store ends its writer.
"""

import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

from solidlsp.util.pickle_util import SafePickleLoader
from solidlsp.util.symbol_store import SymbolStore

_LOADER = SafePickleLoader()

# the most a call that must not wait may take, far below any timeout a waiting call would run into
_PROMPT = 1.0


def _entry(name: str) -> list[dict]:
    """
    :param name: the name of the entry's one symbol
    :return: an entry as a language server reports a file's symbols
    """
    return [{"name": name, "kind": 12, "range": {"start": {"line": 0, "character": 0}, "end": {"line": 1, "character": 0}}}]


def _writer_threads() -> list[threading.Thread]:
    """
    :return: the alive writer threads of all symbol stores
    """
    return [thread for thread in threading.enumerate() if thread.name.startswith("SymbolStoreWriter")]


@contextmanager
def _held(path: Path, exclusive: bool) -> Iterator[None]:
    """
    Holds the store's database from another connection, as another process would.

    :param path: the store's database
    :param exclusive: whether to keep readers out as well as writers
    """
    with closing(sqlite3.connect(str(path), isolation_level=None)) as connection:
        connection.execute("PRAGMA busy_timeout = 0")
        if exclusive:
            connection.execute("PRAGMA locking_mode = EXCLUSIVE")
            connection.execute("BEGIN EXCLUSIVE")
            # the exclusive lock of a database in WAL mode is taken by its first write
            connection.execute("CREATE TABLE IF NOT EXISTS held_by_test (x)")
        else:
            connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        finally:
            connection.execute("ROLLBACK")


def test_a_closed_stores_rows_are_read_by_the_next_store_on_its_file(tmp_path: Path) -> None:
    path = tmp_path / "store.sqlite"
    store = SymbolStore(path, version=(1,), loader=_LOADER)
    store.put("a.py", "hash-a", _entry("a"))
    store.put("b.py", "hash-b", _entry("b"))
    store.close()

    reopened = SymbolStore(path, version=(1,), loader=_LOADER)
    try:
        assert reopened.get("a.py") == ("hash-a", _entry("a"))
        assert reopened.get("b.py") == ("hash-b", _entry("b"))
        assert reopened.get("c.py") is None
    finally:
        reopened.close()


def test_a_row_put_and_not_yet_saved_is_read_back(tmp_path: Path) -> None:
    store = SymbolStore(tmp_path / "store.sqlite", version=(1,), loader=_LOADER)
    try:
        store.put("a.py", "hash-a", _entry("a"))
        assert store.get("a.py") == ("hash-a", _entry("a"))
        store.submit()
        assert store.get("a.py") == ("hash-a", _entry("a"))
    finally:
        store.close()


def test_a_store_of_another_version_reads_none_of_the_rows_and_keeps_only_its_own(tmp_path: Path) -> None:
    path = tmp_path / "store.sqlite"
    first = SymbolStore(path, version=(1,), loader=_LOADER)
    first.put("a.py", "hash-a", _entry("a"))
    first.close()

    second = SymbolStore(path, version=(2,), loader=_LOADER)
    assert second.get("a.py") is None
    second.put("b.py", "hash-b", _entry("b"))
    second.close()

    third = SymbolStore(path, version=(2,), loader=_LOADER)
    try:
        assert third.get("a.py") is None
        assert third.get("b.py") == ("hash-b", _entry("b"))
    finally:
        third.close()


def test_a_save_that_meets_a_held_database_does_not_wait_and_writes_its_rows_at_the_next_save(tmp_path: Path) -> None:
    path = tmp_path / "store.sqlite"
    store = SymbolStore(path, version=(1,), loader=_LOADER)
    store.put("a.py", "hash-a", _entry("a"))
    store.close()

    store.put("b.py", "hash-b", _entry("b"))
    with _held(path, exclusive=False):
        started = time.monotonic()
        store.close()
        assert time.monotonic() - started < _PROMPT
    store.close()

    reopened = SymbolStore(path, version=(1,), loader=_LOADER)
    try:
        assert reopened.get("b.py") == ("hash-b", _entry("b"))
    finally:
        reopened.close()


def test_a_read_that_meets_an_exclusively_held_database_is_a_miss_without_waiting(tmp_path: Path) -> None:
    path = tmp_path / "store.sqlite"
    store = SymbolStore(path, version=(1,), loader=_LOADER)
    store.put("a.py", "hash-a", _entry("a"))
    store.close()

    reader = SymbolStore(path, version=(1,), loader=_LOADER)
    try:
        with _held(path, exclusive=True):
            started = time.monotonic()
            assert reader.get("a.py") is None
            assert time.monotonic() - started < _PROMPT
        assert reader.get("a.py") == ("hash-a", _entry("a"))
    finally:
        reader.close()


def test_a_submit_returns_before_its_rows_are_written_and_a_close_writes_them_and_ends_the_writer(tmp_path: Path) -> None:
    path = tmp_path / "store.sqlite"
    writers_before = set(_writer_threads())
    store = SymbolStore(path, version=(1,), loader=_LOADER)
    for index in range(2000):
        store.put(f"pkg/module_{index}.py", f"hash-{index}", _entry(f"function_{index}"))
    store.submit()
    store.close()

    assert [thread for thread in _writer_threads() if thread not in writers_before] == []
    reopened = SymbolStore(path, version=(1,), loader=_LOADER)
    try:
        assert reopened.get("pkg/module_1999.py") == ("hash-1999", _entry("function_1999"))
    finally:
        reopened.close()


def test_concurrent_saves_of_one_file_by_several_stores_leave_every_row_readable(tmp_path: Path) -> None:
    path = tmp_path / "store.sqlite"
    stores = [SymbolStore(path, version=(1,), loader=_LOADER) for _ in range(8)]
    errors: list[BaseException] = []

    def save_repeatedly(index: int) -> None:
        try:
            for save in range(50):
                stores[index].put(f"store_{index}/module_{save}.py", f"hash-{save}", _entry(f"function_{save}"))
                stores[index].submit()
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=save_repeatedly, args=(index,)) for index in range(len(stores))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    # rows a save deferred, as another store held the file, are written by the next save
    for store in stores:
        store.close()

    assert errors == []
    reopened = SymbolStore(path, version=(1,), loader=_LOADER)
    try:
        for index in range(len(stores)):
            for save in range(50):
                assert reopened.get(f"store_{index}/module_{save}.py") == (f"hash-{save}", _entry(f"function_{save}"))
    finally:
        reopened.close()


def test_an_entry_that_cannot_be_pickled_is_refused_and_leaves_the_saved_rows_as_they_were(tmp_path: Path) -> None:
    path = tmp_path / "store.sqlite"
    store = SymbolStore(path, version=(1,), loader=_LOADER)
    store.put("a.py", "hash-a", _entry("a"))
    store.close()

    with pytest.raises(TypeError, match="pickle"):
        store.put("a.py", "hash-b", [{"lock": threading.Lock()}])
    store.close()

    reopened = SymbolStore(path, version=(1,), loader=_LOADER)
    try:
        assert reopened.get("a.py") == ("hash-a", _entry("a"))
    finally:
        reopened.close()


def test_a_store_that_never_wrote_starts_no_writer_and_creates_no_file(tmp_path: Path) -> None:
    path = tmp_path / "store.sqlite"
    writers_before = set(_writer_threads())
    store = SymbolStore(path, version=(1,), loader=_LOADER)
    assert store.get("a.py") is None
    store.submit()
    store.close()

    assert [thread for thread in _writer_threads() if thread not in writers_before] == []
    assert not path.exists()
