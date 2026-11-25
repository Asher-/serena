# SPDX-License-Identifier: MIT

import contextlib
import itertools
import logging
import pickle
import queue
import sqlite3
import threading
import urllib.parse
from collections.abc import Hashable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from solidlsp.util.pickle_util import SafePickleLoader

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Row:
    """A file's cached entry, pickled, with the hash of the file content it was cached for."""

    content_hash: str
    data: bytes


class SymbolStore:
    """
    A persistent map from the relative path of a file to the hash of the content its entry was cached for and the entry
    itself, held as one row per file in a sqlite database in WAL mode.

    A read opens a connection of its own, reads one row, closes the connection and unpickles one entry; a read that
    meets a database another connection holds, or any other database error, is a miss. A save writes only the rows put
    since the last save. All writes of the store in a process go through one writer thread the store owns, fed by a
    queue and started by the first save that has rows to write; a write that meets a database another connection holds
    keeps its rows for the next save and does not wait. Closing the store writes what is left in the calling thread and
    ends the writer, leaving no thread and no connection open. Rows of another version than the store's are never read,
    and the first write of the store removes them.
    """

    WRITER_THREAD_NAME_PREFIX = "SymbolStoreWriter"
    """the prefix of the name of every store's writer thread"""

    def __init__(self, path: str | Path, version: Hashable, loader: SafePickleLoader) -> None:
        """
        :param path: the database file, which is created by the first write
        :param version: the version of the entries, which rows must have been written under to be read
        :param loader: the loader every entry is unpickled with
        """
        self._path = Path(path)
        self._read_uri = f"file:{urllib.parse.quote(str(self._path))}?mode=rw"
        """the URI a read opens the database with, which never creates it"""
        self._version = pickle.dumps(version)
        self._loader = loader
        self._pending: dict[str, _Row] = {}
        """the rows put and not yet handed to a write"""
        self._in_flight: dict[str, _Row] = {}
        """the rows handed to a write that has not yet ended"""
        self._writer = _Writer(self)

    def get(self, key: str) -> tuple[str, Any] | None:
        """
        :param key: the relative path of the file
        :return: the hash of the content the file's entry was cached for and the entry, or None if no entry is held or
            the store cannot be read now
        """
        row = self._pending.get(key) or self._in_flight.get(key)
        if row is not None:
            return row.content_hash, self._loader.loads(row.data)

        if not self._path.is_file():
            return None
        try:
            connection = sqlite3.connect(self._read_uri, uri=True, isolation_level=None)
            try:
                connection.execute("PRAGMA busy_timeout = 0")
                found = connection.execute(
                    "SELECT e.content_hash, e.entry FROM entries e, meta m WHERE e.key = ? AND m.key = 'version' AND m.value = ?",
                    (key, self._version),
                ).fetchone()
            finally:
                connection.close()
        except sqlite3.Error as error:
            log.debug("Reading %s from %s missed: %s", key, self._path, error)
            return None
        if found is None:
            return None
        try:
            return found[0], self._loader.loads(found[1])
        except Exception as error:
            log.warning("Ignoring the unreadable entry of %s in %s: %s", key, self._path, error)
            return None

    def put(self, key: str, content_hash: str, entry: Any) -> None:
        """
        Holds the file's entry, to be written by the next save. The entry is pickled at once, so that a later change of
        the object does not change what is written.

        :param key: the relative path of the file
        :param content_hash: the hash of the file content the entry was cached for
        :param entry: the entry
        """
        self._pending[key] = _Row(content_hash, pickle.dumps(entry))

    def submit(self) -> None:
        """
        Hands the rows put since the last save to the writer, which writes them after this returns.
        """
        batch = self._take_pending()
        if batch:
            self._writer.send(batch)

    def close(self) -> None:
        """
        Ends the writer once it has written every batch handed to it, then writes the rows put since the last save in
        the calling thread; with no such rows, it writes nothing. Rows a write could not write, as another connection
        held the database, are kept for the next save. The store can be used again afterwards, starting a new writer on
        its next submit.
        """
        writer, self._writer = self._writer, _Writer(self)
        writer.stop()
        batch = self._take_pending()
        if batch:
            self._write(batch)

    def _take_pending(self) -> dict[str, _Row]:
        """
        :return: the pending rows, moved to the rows in flight; a row put meanwhile stays pending
        """
        batch: dict[str, _Row] = {}
        for key in list(self._pending):
            row = self._pending.pop(key, None)
            if row is not None:
                self._in_flight[key] = row
                batch[key] = row
        return batch

    def _write(self, batch: dict[str, _Row]) -> None:
        """
        Writes the rows in one transaction, without waiting for a database another connection holds. Rows not written
        as the database was held go back to the pending rows, unless a newer row of the file is pending.

        :param batch: the rows to write
        """
        written = False
        try:
            connection = sqlite3.connect(str(self._path), isolation_level=None)
            try:
                connection.execute("PRAGMA busy_timeout = 0")
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value BLOB NOT NULL)")
                    connection.execute(
                        "CREATE TABLE IF NOT EXISTS entries (key TEXT PRIMARY KEY, content_hash TEXT NOT NULL, entry BLOB NOT NULL)"
                    )
                    recorded = connection.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
                    if recorded is None or recorded[0] != self._version:
                        connection.execute("DELETE FROM entries")
                        connection.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('version', ?)", (self._version,))
                    connection.executemany(
                        "INSERT OR REPLACE INTO entries (key, content_hash, entry) VALUES (?, ?, ?)",
                        [(key, row.content_hash, row.data) for key, row in batch.items()],
                    )
                    connection.execute("COMMIT")
                    written = True
                except BaseException:
                    with contextlib.suppress(sqlite3.Error):
                        connection.execute("ROLLBACK")
                    raise
            finally:
                connection.close()
        except sqlite3.OperationalError as error:
            if _is_busy(error):
                log.info("Deferring %d rows to the next save of %s, which another connection holds", len(batch), self._path)
                for key, row in batch.items():
                    self._pending.setdefault(key, row)
            else:
                log.error("Failed to save %d document symbol entries to %s: %s", len(batch), self._path, error)
        except (sqlite3.Error, OSError) as error:
            log.error("Failed to save %d document symbol entries to %s: %s", len(batch), self._path, error)
        finally:
            for key, row in batch.items():
                if self._in_flight.get(key) is row:
                    self._in_flight.pop(key, None)
        if written:
            log.debug("Saved %d document symbol entries to %s", len(batch), self._path)


def _is_busy(error: sqlite3.OperationalError) -> bool:
    """
    :return: whether the error reports a database another connection holds
    """
    code = getattr(error, "sqlite_errorcode", None)
    if code is not None:
        return code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    message = str(error)
    return "locked" in message or "busy" in message


class _Writer:
    """
    The thread writing a store's rows, fed by a queue, started by the first batch sent to it and ended by a stop.
    """

    _STOP = None

    def __init__(self, store: SymbolStore) -> None:
        self._store = store
        self._queue: queue.SimpleQueue[dict[str, _Row] | None] = queue.SimpleQueue()
        self._tickets = itertools.count()
        """the first ticket drawn decides whether the thread is started: by a batch, or never, by a stop"""
        self._thread = threading.Thread(target=self._run, name=f"{SymbolStore.WRITER_THREAD_NAME_PREFIX}:{store._path.name}", daemon=True)

    def send(self, batch: dict[str, _Row]) -> None:
        """
        Hands the batch to the thread, starting it on the first batch.

        :param batch: the rows to write
        """
        self._queue.put(batch)
        if next(self._tickets) == 0:
            self._thread.start()

    def stop(self) -> None:
        """
        Ends the thread once it has written every batch sent before, and waits for its end. A writer never started is
        never started afterwards.
        """
        if next(self._tickets) == 0:
            return
        self._queue.put(self._STOP)
        try:
            self._thread.join()
        except RuntimeError:
            # the first batch's sender has drawn its ticket and not yet started the thread, which ends on its own
            # once it has written that batch
            log.debug("The writer of %s is ending on its own", self._store._path)

    def _run(self) -> None:
        while (batch := self._queue.get()) is not self._STOP:
            self._store._write(batch)
