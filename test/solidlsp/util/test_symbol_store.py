"""
A symbol store persists one row per file: a row is read without reading any other, a save writes only the rows that
changed and never waits on a database another connection holds, and closing the store ends its writer.
"""

import gc
import logging
import os
import queue
import sqlite3
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from fractions import Fraction
from pathlib import Path
from typing import Any
from unittest import mock

import psutil
import pytest

from solidlsp.util.pickle_util import SafePickleLoader
from solidlsp.util.symbol_store import SymbolStore, _ReadPool

_LOADER = SafePickleLoader()

# the most a wait may take for an event another thread sets without itself waiting, far below any timeout of a waiting call
_PROMPT = 1.0

# the name of the thread that closes a store while another step of the store is under way
_CLOSER = "closer"


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

    # a close returning while the database is held, a hold that ends only after the close returns, having written nothing
    reader = SymbolStore(path, version=(1,), loader=_LOADER)
    store.put("b.py", "hash-b", _entry("b"))
    with _held(path, exclusive=False):
        store.close()
        assert reader.get("b.py") is None
    store.close()

    try:
        assert reader.get("b.py") == ("hash-b", _entry("b"))
    finally:
        reader.close()


def test_a_read_that_meets_an_exclusively_held_database_is_a_miss_without_waiting(tmp_path: Path) -> None:
    path = tmp_path / "store.sqlite"
    store = SymbolStore(path, version=(1,), loader=_LOADER)
    store.put("a.py", "hash-a", _entry("a"))
    store.close()

    # a read returning a miss while the database is held, a hold that ends only after the read returns
    reader = SymbolStore(path, version=(1,), loader=_LOADER)
    try:
        with _held(path, exclusive=True):
            assert reader.get("a.py") is None
        assert reader.get("a.py") == ("hash-a", _entry("a"))
    finally:
        reader.close()


def test_a_submit_returns_before_its_rows_are_written_and_a_close_writes_them_and_ends_the_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "store.sqlite"
    writers_before = set(_writer_threads())
    store = SymbolStore(path, version=(1,), loader=_LOADER)
    released = threading.Event()
    write_rows = store._write_rows

    def write_rows_once_released(batch: dict, connection: sqlite3.Connection) -> None:
        if threading.current_thread().name.startswith(SymbolStore.WRITER_THREAD_NAME_PREFIX):
            released.wait()
        write_rows(batch, connection)

    monkeypatch.setattr(store, "_write_rows", write_rows_once_released)
    store.put("a.py", "hash-a", _entry("a"))
    store.submit()
    # the writer writes only once released, which happens only after the submit has returned
    assert _read_back(path, "a.py") is None
    released.set()
    store.close()

    assert [thread for thread in _writer_threads() if thread not in writers_before] == []
    assert _read_back(path, "a.py") == ("hash-a", _entry("a"))


def test_concurrent_saves_of_one_file_by_several_stores_leave_every_row_readable(tmp_path: Path) -> None:
    path = tmp_path / "store.sqlite"
    stores = [SymbolStore(path, version=(1,), loader=_LOADER) for _ in range(8)]
    errors = _save_concurrently(stores, saves=50)
    # every writer ended, after which no store holds the file: the rows a save deferred, as another store held it, are
    # written by the next save
    for store in stores:
        store.close()
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


def _save_concurrently(stores: list[SymbolStore], saves: int) -> list[BaseException]:
    """
    Puts and submits rows of files of its own on a thread per store, one row per save.

    :return: the errors the saves raised
    """
    errors: list[BaseException] = []

    def save_repeatedly(index: int) -> None:
        try:
            for save in range(saves):
                stores[index].put(f"store_{index}/module_{save}.py", f"hash-{save}", _entry(f"function_{save}"))
                stores[index].submit()
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=save_repeatedly, args=(index,)) for index in range(len(stores))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return errors


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


def test_a_read_racing_writes_of_its_row_returns_the_old_or_the_new_row(tmp_path: Path) -> None:
    path = tmp_path / "store.sqlite"
    writer = SymbolStore(path, version=(1,), loader=_LOADER)
    writer.put("a.py", "hash-old", _entry("old"))
    writer.close()
    # a store of another process, which reads only what the writer's store has written, and has read before
    reader = SymbolStore(path, version=(1,), loader=_LOADER)
    assert reader.get("a.py") == ("hash-old", _entry("old"))
    read = []
    for index in range(200):
        writer.put("a.py", "hash-new", _entry("new"))
        writer.put(f"other_{index}.py", "hash-other", _entry("other"))
        # the writer's thread writes the rows while the reads that follow are made
        writer.submit()
        read.extend(reader.get("a.py") for _ in range(5))
    writer.close()
    read.append(reader.get("a.py"))

    assert set(map(repr, read)) <= {repr(("hash-old", _entry("old"))), repr(("hash-new", _entry("new")))}
    assert read[-1] == ("hash-new", _entry("new"))


class _SteppedKey(str):
    """
    A key whose every lookup runs a step first and goes on only once the step has ended.
    """

    step: Callable[[], None] | None = None

    __eq__ = str.__eq__

    def __hash__(self) -> int:
        if self.step is not None:
            self.step()
        return super().__hash__()


def test_a_read_at_any_step_of_the_first_save_of_its_row_finds_it(tmp_path: Path) -> None:
    store = SymbolStore(tmp_path / "store.sqlite", version=(1,), loader=_LOADER)
    key = _SteppedKey("a.py")
    store.put(key, "hash-a", _entry("a"))
    reads: list[object] = []

    with ThreadPoolExecutor(1) as reader:
        # each lookup of the row by the save waits for another thread's read of the row to end
        key.step = lambda: reads.append(reader.submit(store.get, "a.py").result())
        store.submit()
        store.close()
        key.step = None

    assert reads
    assert [read for read in reads if read != ("hash-a", _entry("a"))] == []


def _open_connections(path: Path) -> int:
    """
    :return: the number of this process's open descriptors of the database file, one per open connection
    """
    resolved = path.resolve()
    return sum(1 for opened in psutil.Process().open_files() if Path(opened.path).resolve() == resolved)


def _open_sqlite_connections() -> int:
    """
    :return: the number of sqlite connections of this process that are open. A file descriptor does not count them:
        sqlite keeps the descriptor of a closed connection open while another connection of the process holds a lock
        on the file.
    """
    gc.collect()
    count = 0
    for candidate in gc.get_objects():
        if isinstance(candidate, sqlite3.Connection):
            try:
                _ = candidate.total_changes
            except sqlite3.ProgrammingError:
                continue
            count += 1
    return count


def _read_concurrently(store: SymbolStore, key: str, readers: int, reads: int) -> list[object]:
    """
    Reads the key from the store in several threads at once, each starting its reads together with the others.

    :return: every read's result, and every exception a read raised
    """
    start = threading.Barrier(readers)
    results: list[object] = []

    def read() -> None:
        start.wait()
        for _ in range(reads):
            try:
                results.append(store.get(key))
            except BaseException as error:
                results.append(error)

    threads = [threading.Thread(target=read) for _ in range(readers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def test_a_closed_store_leaves_no_connection_to_its_file_open_and_reads_again_afterwards(tmp_path: Path) -> None:
    path = tmp_path / "store.sqlite"
    store = SymbolStore(path, version=(1,), loader=_LOADER)
    store.put("a.py", "hash-a", _entry("a"))
    store.close()
    assert store.get("a.py") == ("hash-a", _entry("a"))
    assert _open_connections(path) > 0
    store.close()
    assert _open_connections(path) == 0

    assert store.get("a.py") == ("hash-a", _entry("a"))
    store.close()
    assert _open_connections(path) == 0


def test_more_concurrent_readers_than_the_pool_holds_leave_no_connection_beyond_it_open(tmp_path: Path) -> None:
    path = tmp_path / "store.sqlite"
    store = SymbolStore(path, version=(1,), loader=_LOADER)
    store.put("a.py", "hash-a", _entry("a"))
    store.close()

    connections_before = _open_sqlite_connections()
    # one connection open throughout, as the first to open a database in WAL mode holds it, which makes other reads misses
    assert store.get("a.py") == ("hash-a", _entry("a"))
    results = _read_concurrently(store, "a.py", readers=12, reads=50)
    connections_after_reads = _open_sqlite_connections()
    store.close()

    assert results == [("hash-a", _entry("a"))] * 600
    assert 0 < connections_after_reads - connections_before <= 4
    assert _open_sqlite_connections() == connections_before
    assert _open_connections(path) == 0


def test_a_close_while_reads_are_in_flight_on_several_threads_leaves_none_of_their_connections_open(tmp_path: Path) -> None:
    path = tmp_path / "store.sqlite"
    store = SymbolStore(path, version=(1,), loader=_LOADER)
    store.put("a.py", "hash-a", _entry("a"))
    store.close()
    # one connection open before the threads read, as for the concurrent readers above
    assert store.get("a.py") == ("hash-a", _entry("a"))

    read_before, read_through = _read_through_a_close(store, "a.py", readers=8)
    # the reads that began after the close opened the store's next reading connections, which its next close closes
    store.close()

    assert read_before == [("hash-a", _entry("a"))] * 8
    # a read meeting the last connection's close, which holds the database, is a miss
    assert [read for read in read_through if read not in (("hash-a", _entry("a")), None)] == []
    assert _open_connections(path) == 0


def _read_through_a_close(store: SymbolStore, key: str, readers: int) -> tuple[list[object], list[object]]:
    """
    :return: the results of reads of the key on several threads made once each before a close of the store, and through it
    """
    reading = threading.Barrier(readers + 1)
    closed = threading.Event()
    read_before: list[object] = []
    read_through: list[object] = []

    def read_until_closed() -> None:
        read_before.append(store.get(key))
        reading.wait()
        while not closed.is_set():
            read_through.append(store.get(key))

    threads = [threading.Thread(target=read_until_closed) for _ in range(readers)]
    for thread in threads:
        thread.start()
    reading.wait()
    store.close()
    closed.set()
    for thread in threads:
        thread.join()
    return read_before, read_through


def test_a_save_after_a_close_writes_in_the_caller_and_starts_no_writer(tmp_path: Path) -> None:
    path = tmp_path / "store.sqlite"
    writers_before = set(_writer_threads())
    store = SymbolStore(path, version=(1,), loader=_LOADER)
    store.put("a.py", "hash-a", _entry("a"))
    store.close()

    store.put("b.py", "hash-b", _entry("b"))
    store.close()
    store.close()

    assert [thread for thread in _writer_threads() if thread not in writers_before] == []
    reopened = SymbolStore(path, version=(1,), loader=_LOADER)
    try:
        assert reopened.get("b.py") == ("hash-b", _entry("b"))
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


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="the file mode keeps no such user out")
def test_a_read_of_a_database_that_cannot_be_opened_is_a_miss(tmp_path: Path) -> None:
    path = tmp_path / "store.sqlite"
    store = SymbolStore(path, version=(1,), loader=_LOADER)
    store.put("a.py", "hash-a", _entry("a"))
    store.close()

    path.chmod(0)
    try:
        assert store.get("a.py") is None
    finally:
        path.chmod(0o600)
    assert store.get("a.py") == ("hash-a", _entry("a"))
    store.close()


def test_a_store_over_a_file_that_is_no_database_misses_every_read_and_logs_its_failed_save(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "store.sqlite"
    path.write_bytes(b"not a database" * 100)
    store = SymbolStore(path, version=(1,), loader=_LOADER)
    assert store.get("a.py") is None

    store.put("a.py", "hash-a", _entry("a"))
    with caplog.at_level(logging.ERROR):
        store.close()

    assert "Failed to save" in caplog.text
    assert store.get("a.py") is None
    store.close()


def test_a_save_the_database_refuses_midway_is_logged_and_leaves_the_saved_rows_as_they_were(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "store.sqlite"
    with closing(sqlite3.connect(str(path), isolation_level=None)) as connection:
        # a table of entries that refuses every row the store writes, as each lacks the value of its last column
        connection.execute("CREATE TABLE entries (key TEXT PRIMARY KEY, content_hash TEXT NOT NULL, entry BLOB NOT NULL, x NOT NULL)")
        connection.execute("INSERT INTO entries VALUES ('z.py', 'hash-z', x'00', 'kept')")
    store = SymbolStore(path, version=(1,), loader=_LOADER)
    store.put("a.py", "hash-a", _entry("a"))
    with caplog.at_level(logging.ERROR):
        store.close()

    assert "Failed to save" in caplog.text
    with closing(sqlite3.connect(str(path))) as connection:
        assert connection.execute("SELECT key FROM entries").fetchall() == [("z.py",)]


def test_a_row_put_while_a_save_writes_an_older_row_of_its_file_is_read_and_written_by_the_next_save(tmp_path: Path) -> None:
    path = tmp_path / "store.sqlite"
    store = SymbolStore(path, version=(1,), loader=_LOADER)
    key = _SteppedKey("a.py")
    store.put(key, "hash-old", _entry("old"))
    rows: list[tuple[str, object]] = []

    def put_newer() -> None:
        rows.append((f"hash-{len(rows)}", _entry(f"new_{len(rows)}")))
        store.put("a.py", *rows[-1])

    # each lookup of the row by the save puts a newer row of the file first, the last one after the write
    key.step = put_newer
    store.close()
    key.step = None

    assert store.get("a.py") == rows[-1]
    store.close()
    reopened = SymbolStore(path, version=(1,), loader=_LOADER)
    try:
        assert reopened.get("a.py") == rows[-1]
    finally:
        reopened.close()


class _PutMeanwhile(dict):
    """
    A map of the rows whose write has not ended, in which another thread puts a newer row of a file and finishes that
    put just after a row is first looked up.
    """

    def __init__(self, rows: dict, put_newer: Callable[[], None]) -> None:
        """
        :param rows: the rows held
        :param put_newer: the put the other thread finishes
        """
        super().__init__(rows)
        self._put_newer: Callable[[], None] | None = put_newer

    def get(self, key: Any, default: Any = None) -> Any:
        row = super().get(key, default)
        put_newer, self._put_newer = self._put_newer, None
        if put_newer is not None:
            other = threading.Thread(target=put_newer)
            other.start()
            other.join()
        return row


def test_a_row_put_while_the_write_of_an_older_row_of_its_file_is_settled_is_read_and_written_by_the_next_save(
    tmp_path: Path,
) -> None:
    path = tmp_path / "store.sqlite"
    store = SymbolStore(path, version=(1,), loader=_LOADER)
    store.put("a.py", "hash-old", _entry("old"))
    store._unwritten = _PutMeanwhile(store._unwritten, lambda: store.put("a.py", "hash-new", _entry("new")))

    store.close()

    assert store.get("a.py") == ("hash-new", _entry("new"))
    store.close()
    assert _read_back(path, "a.py") == ("hash-new", _entry("new"))


def _read_back(path: Path, key: str) -> object:
    """
    :return: what a new store on the file reads for the key
    """
    reader = SymbolStore(path, version=(1,), loader=_LOADER)
    try:
        return reader.get(key)
    finally:
        reader.close()


@contextmanager
def _before_writer_starts(action: Callable[[], None]) -> Iterator[None]:
    """
    Runs the action when a store's writer thread is about to be started, which is started once the action has returned.
    """
    start = threading.Thread.start

    def act_and_start(thread: threading.Thread) -> None:
        if thread.name.startswith(SymbolStore.WRITER_THREAD_NAME_PREFIX):
            action()
        start(thread)

    with mock.patch.object(threading.Thread, "start", act_and_start):
        yield


def _signalling_queue(reached: threading.Event) -> type[queue.SimpleQueue]:  # type: ignore[type-arg]
    """
    :param reached: the event set when the thread named as the closer waits on a queue of the class
    :return: a queue class that sets the event when the closer waits on one of its queues
    """

    class _SignallingQueue(queue.SimpleQueue):  # type: ignore[type-arg]
        def get(self, *args: object, **kwargs: object) -> object:
            if threading.current_thread().name == _CLOSER:
                reached.set()
            return super().get(*args, **kwargs)  # type: ignore[arg-type]

    return _SignallingQueue


def _closer(store: SymbolStore, reached: threading.Event, then: Callable[[], None] = lambda: None) -> threading.Thread:
    """
    :param store: the store the thread closes
    :param reached: the event set once the thread has ended
    :param then: what the thread does once the close has returned
    :return: a thread named as the closer, not started
    """

    def close() -> None:
        try:
            store.close()
            then()
        finally:
            reached.set()

    return threading.Thread(target=close, name=_CLOSER)


def test_a_close_meeting_a_writer_not_yet_started_returns_once_its_rows_are_written(tmp_path: Path) -> None:
    path = tmp_path / "store.sqlite"
    reached = threading.Event()
    with mock.patch.object(queue, "SimpleQueue", _signalling_queue(reached)):
        store = SymbolStore(path, version=(1,), loader=_LOADER)
    store.put("a.py", "hash-a", _entry("a"))
    read_after_close: list[object] = []
    closer = _closer(store, reached, lambda: read_after_close.append(_read_back(path, "a.py")))

    def close_meanwhile() -> None:
        closer.start()
        # a close that does not wait for the writer has ended here; one that waits for it is waiting for it
        reached.wait()

    with _before_writer_starts(close_meanwhile):
        store.submit()
    closer.join()
    assert read_after_close == [("hash-a", _entry("a"))]


def _overtaking_queue(reached: threading.Event, armed: list[Callable[[], None]]) -> type[queue.SimpleQueue]:  # type: ignore[type-arg]
    """
    :param reached: the event set when the thread named as the closer waits on a queue of the class
    :param armed: the steps to run, one before each of the next puts on a queue of the class
    :return: a queue class that runs an armed step before a put and sets the event when the closer waits on it
    """

    class _OvertakenQueue(_signalling_queue(reached)):  # type: ignore[misc]
        def put(self, *args: object, **kwargs: object) -> None:
            if armed:
                armed.pop()()
            super().put(*args, **kwargs)

    return _OvertakenQueue


def test_a_submit_overtaken_by_a_close_while_it_hands_its_rows_over_loses_none_of_them(tmp_path: Path) -> None:
    path = tmp_path / "store.sqlite"
    armed: list[Callable[[], None]] = []
    reached = threading.Event()
    with mock.patch.object(queue, "SimpleQueue", _overtaking_queue(reached, armed)):
        store = SymbolStore(path, version=(1,), loader=_LOADER)
        store.put("a.py", "hash-a", _entry("a"))
        closer = _closer(store, reached)

        def close_meanwhile() -> None:
            closer.start()
            # a close that does not wait for the rows handed over has ended here; one that waits for them is waiting
            reached.wait()

        armed.append(close_meanwhile)
        store.submit()
        closer.join()
    assert _read_back(path, "a.py") == ("hash-a", _entry("a"))


def test_a_submit_after_a_close_writes_in_the_caller_and_starts_no_writer(tmp_path: Path) -> None:
    path = tmp_path / "store.sqlite"
    writers_before = set(_writer_threads())
    store = SymbolStore(path, version=(1,), loader=_LOADER)
    store.close()
    store.put("a.py", "hash-a", _entry("a"))
    store.submit()

    assert [thread for thread in _writer_threads() if thread not in writers_before] == []
    assert _read_back(path, "a.py") == ("hash-a", _entry("a"))


def test_a_store_whose_writer_thread_cannot_start_writes_its_rows_and_is_closed(tmp_path: Path) -> None:
    path = tmp_path / "store.sqlite"
    store = SymbolStore(path, version=(1,), loader=_LOADER)

    def refuse() -> None:
        raise RuntimeError("can't start new thread")

    with _before_writer_starts(refuse):
        for name in ("a", "b"):
            store.put(f"{name}.py", f"hash-{name}", _entry(name))
            store.submit()
            assert _read_back(path, f"{name}.py") == (f"hash-{name}", _entry(name))
    store.close()


def test_a_store_reopened_twice_after_its_close_has_one_writer(tmp_path: Path) -> None:
    writers_before = set(_writer_threads())
    store = SymbolStore(tmp_path / "store.sqlite", version=(1,), loader=_LOADER)
    store.close()
    for name in ("a", "b"):
        store.reopen()
        store.put(f"{name}.py", f"hash-{name}", _entry(name))
        store.submit()
    assert len(set(_writer_threads()) - writers_before) == 1
    store.close()
    assert set(_writer_threads()) <= writers_before


def test_a_reopen_before_a_close_keeps_the_writer_the_close_ends(tmp_path: Path) -> None:
    writers_before = set(_writer_threads())
    path = tmp_path / "store.sqlite"
    store = SymbolStore(path, version=(1,), loader=_LOADER)
    store.put("a.py", "hash-a", _entry("a"))
    store.submit()
    store.reopen()
    store.close()
    assert set(_writer_threads()) <= writers_before
    assert _read_back(path, "a.py") == ("hash-a", _entry("a"))


def test_two_reopens_racing_install_one_writer(tmp_path: Path) -> None:
    writers_before = set(_writer_threads())
    store = SymbolStore(tmp_path / "store.sqlite", version=(1,), loader=_LOADER)
    store.close()
    nested: list[Callable[[], None]] = [store.reopen]
    create = threading.Thread.__init__

    def reopen_within(thread: threading.Thread, *args: object, **kwargs: object) -> None:
        create(thread, *args, **kwargs)  # type: ignore[arg-type]
        # the other start reopens the store, and saves, after the first has found its writer ended
        if nested:
            nested.pop()()
            store.put("a.py", "hash-a", _entry("a"))
            store.submit()

    with mock.patch.object(threading.Thread, "__init__", reopen_within):
        store.reopen()
    store.put("b.py", "hash-b", _entry("b"))
    store.submit()
    assert len(set(_writer_threads()) - writers_before) == 1
    store.close()
    assert set(_writer_threads()) <= writers_before


def test_a_key_no_utf8_text_holds_is_refused_at_put_and_every_other_row_is_written(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "store.sqlite"
    writers_before = set(_writer_threads())
    store = SymbolStore(path, version=(1,), loader=_LOADER)
    # a file name holding a lone surrogate, which no UTF-8 text holds
    unencodable = "\udc80.py"
    with caplog.at_level(logging.WARNING, logger="solidlsp.util.symbol_store"):
        store.put(unencodable, "hash-u", _entry("u"))
    assert any(repr(unencodable) in record.getMessage() for record in caplog.records)
    store.put("a.py", "hash-a", _entry("a"))
    store.submit()
    store.close()

    assert set(_writer_threads()) <= writers_before
    assert store.get(unencodable) is None
    reopened = SymbolStore(path, version=(1,), loader=_LOADER)
    try:
        assert reopened.get("a.py") == ("hash-a", _entry("a"))
        assert reopened.get(unencodable) is None
    finally:
        reopened.close()
    assert _open_connections(path) == 0


def test_an_unwritten_entry_its_loader_refuses_is_a_miss(tmp_path: Path) -> None:
    store = SymbolStore(tmp_path / "store.sqlite", version=(1,), loader=_LOADER)
    store.put("a.py", "hash-a", [{"detail": Fraction(1, 2)}])
    try:
        found: object
        try:
            found = store.get("a.py")
        except Exception as error:
            found = error
        assert found is None
    finally:
        store.close()


class _UnconfigurableConnection(sqlite3.Connection):
    """
    A connection on which setting the busy timeout fails.
    """

    def execute(self, sql: str, parameters: object = (), /) -> sqlite3.Cursor:
        if sql.startswith("PRAGMA busy_timeout"):
            raise sqlite3.OperationalError("disk I/O error")
        return super().execute(sql, parameters)


def test_a_read_whose_new_connection_cannot_be_configured_is_a_miss_and_closes_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "store.sqlite"
    store = SymbolStore(path, version=(1,), loader=_LOADER)
    store.put("a.py", "hash-a", _entry("a"))
    store.close()
    reader = SymbolStore(path, version=(1,), loader=_LOADER)
    opened: list[sqlite3.Connection] = []
    connect = sqlite3.connect

    def connect_unconfigurable(*args: object, **kwargs: object) -> sqlite3.Connection:
        connection = connect(*args, factory=_UnconfigurableConnection, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect_unconfigurable)
    try:
        assert reader.get("a.py") is None
    finally:
        monkeypatch.undo()
        reader.close()

    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        opened[0].execute("SELECT 1")


class _IdleTakenByAnotherGiver:
    """
    The idle connections of a pool, whose surplus another giver takes each time between this giver's look at their
    number and its take.
    """

    def __init__(self) -> None:
        self.held: list[sqlite3.Connection] = []

    def put(self, connection: sqlite3.Connection) -> None:
        self.held.append(connection)

    def qsize(self) -> int:
        return _ReadPool.SIZE + 1

    def get_nowait(self) -> sqlite3.Connection:
        raise queue.Empty


def test_a_connection_given_back_while_another_giver_takes_the_surplus_is_kept(tmp_path: Path) -> None:
    pool = _ReadPool(f"file:{tmp_path / 'store.sqlite'}?mode=rwc")
    idle = _IdleTakenByAnotherGiver()
    pool._idle = idle  # type: ignore[assignment]
    with closing(sqlite3.connect(":memory:")) as connection:
        raised: Exception | None = None
        try:
            pool.give_back(connection)
        except Exception as error:
            raised = error

        assert (raised, idle.held) == (None, [connection])


def test_a_write_that_raises_in_the_writer_is_logged_and_its_rows_are_written_by_the_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "store.sqlite"
    store = SymbolStore(path, version=(1,), loader=_LOADER)
    write_rows = store._write_rows

    def write_rows_raising_in_the_writer(batch: dict, connection: sqlite3.Connection) -> None:
        if threading.current_thread().name.startswith(SymbolStore.WRITER_THREAD_NAME_PREFIX):
            raise RuntimeError("the write failed")
        write_rows(batch, connection)

    monkeypatch.setattr(store, "_write_rows", write_rows_raising_in_the_writer)
    store.put("a.py", "hash-a", _entry("a"))
    with caplog.at_level(logging.ERROR):
        store.submit()
        store.close()

    assert any("the write failed" in record.getMessage() for record in caplog.records)
    assert _read_back(path, "a.py") == ("hash-a", _entry("a"))


def test_a_writer_whose_connection_cannot_be_opened_ends_and_the_close_writes_every_batch_it_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "store.sqlite"
    store = SymbolStore(path, version=(1,), loader=_LOADER)
    opening = threading.Event()
    second_submitted = threading.Event()
    unhandled: list[threading.ExceptHookArgs] = []
    monkeypatch.setattr(store, "_open_for_writing", _open_failing_in_the_writer(store, opening, second_submitted))
    monkeypatch.setattr(threading, "excepthook", unhandled.append)
    store.put("a.py", "hash-a", _entry("a"))
    with caplog.at_level(logging.ERROR):
        store.submit()
        assert opening.wait(_PROMPT)
        store.put("b.py", "hash-b", _entry("b"))
        store.submit()
        second_submitted.set()
        store.close()

    assert [args.exc_value for args in unhandled] == []
    assert any("the database cannot be opened" in record.getMessage() for record in caplog.records)
    assert _read_back(path, "a.py") == ("hash-a", _entry("a"))
    assert _read_back(path, "b.py") == ("hash-b", _entry("b"))


def _open_failing_in_the_writer(
    store: SymbolStore, opening: threading.Event, second_submitted: threading.Event
) -> Callable[[], sqlite3.Connection]:
    """
    :return: the store's open for writing, made to fail in the writer thread once the second batch is accepted behind
        the one the writer took: it sets ``opening``, then waits for ``second_submitted``
    """
    open_for_writing = store._open_for_writing

    def open_failing() -> sqlite3.Connection:
        if threading.current_thread().name.startswith(SymbolStore.WRITER_THREAD_NAME_PREFIX):
            opening.set()
            second_submitted.wait(_PROMPT)
            raise RuntimeError("the database cannot be opened")
        return open_for_writing()

    return open_failing


class _HeldAtWalSwitch(sqlite3.Connection):
    """
    A connection on which switching to WAL mode meets a database another connection holds.
    """

    def execute(self, sql: str, parameters: object = (), /) -> sqlite3.Cursor:
        if sql.startswith("PRAGMA journal_mode"):
            raise sqlite3.OperationalError("database is locked")
        return super().execute(sql, parameters)


def test_a_writer_whose_connections_cannot_be_configured_closes_them_and_the_close_writes_its_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "store.sqlite"
    store = SymbolStore(path, version=(1,), loader=_LOADER)
    opened: list[sqlite3.Connection] = []
    connect = sqlite3.connect

    def connect_held_in_the_writer(*args: object, **kwargs: object) -> sqlite3.Connection:
        if not threading.current_thread().name.startswith(SymbolStore.WRITER_THREAD_NAME_PREFIX):
            return connect(*args, **kwargs)  # type: ignore[call-overload]
        opened.append(connect(*args, factory=_HeldAtWalSwitch, **kwargs))  # type: ignore[call-overload]
        return opened[-1]

    monkeypatch.setattr(sqlite3, "connect", connect_held_in_the_writer)
    store.put("a.py", "hash-a", _entry("a"))
    store.submit()
    store.close()

    assert _read_back(path, "a.py") == ("hash-a", _entry("a"))
    assert opened != []
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


def test_a_save_meeting_a_held_database_reported_only_by_its_message_writes_its_rows_at_the_next_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "store.sqlite"
    store = SymbolStore(path, version=(1,), loader=_LOADER)
    store.close()
    write_rows = store._write_rows
    errors = [sqlite3.OperationalError("database is locked")]

    def write_rows_held_once(batch: dict, connection: sqlite3.Connection) -> None:
        if errors:
            raise errors.pop()
        write_rows(batch, connection)

    monkeypatch.setattr(store, "_write_rows", write_rows_held_once)
    store.put("a.py", "hash-a", _entry("a"))
    store.submit()
    store.submit()

    assert errors == []
    assert _read_back(path, "a.py") == ("hash-a", _entry("a"))
