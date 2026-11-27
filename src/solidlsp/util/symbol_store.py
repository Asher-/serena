# SPDX-License-Identifier: MIT

import contextlib
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

    A read takes a connection from a bounded pool of reading connections that belong to no thread, reads one row, gives
    the connection back and unpickles one entry; a read that meets a database another connection holds, or any other
    database error, is a miss. A save writes only the rows put
    since the last save. The writes of the store in a process go through one writer thread the store owns, started by
    the first save that has rows to write, until the store is closed, and through the calling thread afterwards; a write
    that meets a database another connection holds keeps its rows for the next save and does not wait. Closing the
    store writes what is left in the calling thread and
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
        self._readers = _ReadPool(self._read_uri)
        self._version = pickle.dumps(version)
        self._loader = loader
        self._pending: dict[str, _Row] = {}
        """the rows put and not yet handed to a write"""
        self._unwritten: dict[str, _Row] = {}
        """the newest row of each file whose write has not yet ended, which reads are served from"""
        self._writer = _Writer(self)
        """the current writer, which reopen replaces once it has ended"""

    def get(self, key: str) -> tuple[str, Any] | None:
        """
        :param key: the relative path of the file
        :return: the hash of the content the file's entry was cached for and the entry, or None if no entry is held, the
            key is one no UTF-8 text holds, or the store cannot be read now
        """
        if not self._is_storable(key):
            return None
        row = self._unwritten.get(key) or self._read_row(key)
        if row is None:
            return None
        try:
            return row.content_hash, self._loader.loads(row.data)
        except Exception as error:
            log.warning("Ignoring the unreadable entry of %s in %s: %s", key, self._path, error)
            return None

    def put(self, key: str, content_hash: str, entry: Any) -> None:
        """
        Holds the file's entry, to be written by the next save. The entry is pickled at once, so that a later change of
        the object does not change what is written. A key that no UTF-8 text holds, which the database cannot store, is
        refused with a log line, and nothing is held for it.

        :param key: the relative path of the file
        :param content_hash: the hash of the file content the entry was cached for
        :param entry: the entry
        """
        if not self._is_storable(key):
            log.warning("Not caching the symbols of %r in %s, as no UTF-8 text holds its name", key, self._path)
            return
        row = _Row(content_hash, pickle.dumps(entry))
        self._unwritten[key] = row
        self._pending[key] = row

    @staticmethod
    def _is_storable(key: str) -> bool:
        """
        :param key: the relative path of a file
        :return: whether UTF-8 text holds the key, as the database's keys must
        """
        try:
            key.encode("utf-8")
        except UnicodeEncodeError:
            return False
        return True

    def submit(self) -> None:
        """
        Hands the rows put since the last save to the writer, which writes them after this returns. Once the store is
        closed, they are written in the calling thread before this returns.
        """
        batch = self._take_pending()
        if batch and not self._current_writer().send(batch):
            self._write(batch)

    def close(self) -> None:
        """
        Ends the writer once it has written every batch it accepted, then writes the rows put since the last save in
        the calling thread; with no such rows, it writes nothing. This covers the rows that were dirty or accepted when
        the close began. Rows a write could not write, as another connection held the database, are kept for the next
        save. Then it closes the reading connections, those of reads still in flight as they end. The store can be read
        and saved to afterwards; it has no writer until it is reopened.
        """
        try:
            self._current_writer().stop()
            batch = self._take_pending()
            if batch:
                self._write(batch)
        finally:
            readers = self._readers
            readers.closed = True
            self._readers = _ReadPool(self._read_uri)
            readers.drain()

    def reopen(self) -> None:
        """
        Gives a closed store a writer again; a store that has one keeps it.
        """
        writer = self._current_writer()
        if writer.is_ended():
            self._writer = writer.replace(_Writer(self))

    def _current_writer(self) -> "_Writer":
        """
        :return: the last writer the store was given
        """
        return self._writer

    def _take_pending(self) -> dict[str, _Row]:
        """
        :return: the pending rows, which are pending no longer; a row put meanwhile stays pending
        """
        batch: dict[str, _Row] = {}
        for key in list(self._pending):
            row = self._pending.pop(key, None)
            if row is not None:
                batch[key] = row
        return batch

    def _read_row(self, key: str) -> _Row | None:
        """
        :param key: the relative path of the file
        :return: the row the database holds for the file, or None if it holds none or cannot be read now
        """
        if not self._path.is_file():
            return None
        pool = self._readers
        try:
            connection = pool.take()
        except sqlite3.Error as error:
            log.debug("Opening %s for reading missed: %s", self._path, error)
            return None
        try:
            query = "SELECT e.content_hash, e.entry FROM entries e, meta m WHERE e.key = ? AND m.key = 'version' AND m.value = ?"
            found = connection.execute(query, (key, self._version)).fetchone()
        except sqlite3.Error as error:
            log.debug("Reading %s from %s missed: %s", key, self._path, error)
            return None
        finally:
            pool.give_back(connection)
        return None if found is None else _Row(*found)

    def _open_for_writing(self) -> sqlite3.Connection:
        """
        :return: a new connection writing the database, which it creates if it does not exist, in WAL mode
        """
        connection = sqlite3.connect(str(self._path), isolation_level=None, check_same_thread=False)
        try:
            connection.execute("PRAGMA busy_timeout = 0")
            connection.execute("PRAGMA journal_mode = WAL")
        except BaseException:
            connection.close()
            raise
        return connection

    def _write(self, batch: dict[str, _Row], connection: sqlite3.Connection | None = None) -> None:
        """
        Writes the rows in one transaction, without waiting for a database another connection holds. Rows not written
        as the database was held, or as the write raised, go back to the pending rows, unless a newer row of the file
        was put.

        :param batch: the rows to write
        :param connection: the connection to write through, which stays open; None to open one for this write only
        """
        dirty = True
        try:
            if connection is None:
                with contextlib.closing(self._open_for_writing()) as own_connection:
                    self._write_rows(batch, own_connection)
            else:
                self._write_rows(batch, connection)
            dirty = False
        except (sqlite3.Error, OSError) as error:
            dirty = _is_busy(error)
            self._log_unwritten(batch, error, dirty)
        finally:
            self._settle(batch, dirty)

    def _write_rows(self, batch: dict[str, _Row], connection: sqlite3.Connection) -> None:
        """
        :param batch: the rows to write in one transaction, after removing those of another version
        :param connection: the connection to write through
        """
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value BLOB NOT NULL)")
            connection.execute("CREATE TABLE IF NOT EXISTS entries (key TEXT PRIMARY KEY, content_hash TEXT NOT NULL, entry BLOB NOT NULL)")
            recorded = connection.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
            if recorded is None or recorded[0] != self._version:
                connection.execute("DELETE FROM entries")
                connection.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('version', ?)", (self._version,))
            connection.executemany(
                "INSERT OR REPLACE INTO entries (key, content_hash, entry) VALUES (?, ?, ?)",
                [(key, row.content_hash, row.data) for key, row in batch.items()],
            )
            connection.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                connection.execute("ROLLBACK")
            raise

    def _log_unwritten(self, batch: dict[str, _Row], error: Exception, deferred: bool) -> None:
        if deferred:
            log.info("Deferring %d rows to the next save of %s, which another connection holds", len(batch), self._path)
        else:
            log.error("Failed to save %d document symbol entries to %s: %s", len(batch), self._path, error)

    def _settle(self, batch: dict[str, _Row], dirty: bool) -> None:
        """
        Ends the write of the rows, leaving alone each file a newer row was put for.

        :param batch: the rows
        :param dirty: whether the rows go back to the pending rows
        """
        for key, row in batch.items():
            if self._unwritten.get(key) is not row:
                continue
            if dirty:
                self._pending.setdefault(key, row)
            else:
                removed = self._unwritten.pop(key, None)
                # a newer row put since the check above is held again, unless a newer one still was put meanwhile
                if removed is not None and removed is not row:
                    self._unwritten.setdefault(key, removed)


def _is_busy(error: Exception) -> bool:
    """
    :return: whether the error reports a database another connection holds
    """
    if not isinstance(error, sqlite3.OperationalError):
        return False
    code = getattr(error, "sqlite_errorcode", None)
    if code is not None:
        return code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    message = str(error)
    return "locked" in message or "busy" in message


class _ReadPool:
    """
    The idle reading connections of a store, at most :data:`SIZE` of them, which belong to no thread: a read takes one,
    or opens one when none is idle, and gives it back afterwards, closing it when the pool is full. A pool marked closed
    is drained and its connections closed, by its closer and by every reader giving a connection back to it, so that a
    connection given back while the pool closes is closed as well.
    """

    SIZE = 4

    def __init__(self, uri: str) -> None:
        """
        :param uri: the URI the connections open the database with
        """
        self._uri = uri
        self._idle: queue.SimpleQueue[sqlite3.Connection] = queue.SimpleQueue()
        self.closed = False
        """whether the pool is closed, after which every connection given back to it is closed"""

    def take(self) -> sqlite3.Connection:
        """
        :return: an idle connection, or a new one when none is idle
        """
        try:
            return self._idle.get_nowait()
        except queue.Empty:
            connection = sqlite3.connect(self._uri, uri=True, isolation_level=None, check_same_thread=False)
            try:
                connection.execute("PRAGMA busy_timeout = 0")
            except BaseException:
                connection.close()
                raise
            return connection

    def give_back(self, connection: sqlite3.Connection) -> None:
        """
        :param connection: a connection taken from the pool, which is closed when the pool is full or closed
        """
        self._idle.put(connection)
        if self.closed:
            self.drain()
            return
        # each giver trims the pool back to its size after its own put, so that no put racing another leaves it larger
        while self._idle.qsize() > self.SIZE:
            try:
                surplus = self._idle.get_nowait()
            except queue.Empty:
                return
            surplus.close()

    def drain(self) -> None:
        """
        Closes every idle connection.
        """
        while True:
            try:
                connection = self._idle.get_nowait()
            except queue.Empty:
                return
            connection.close()


class _Link:
    """
    A place in a writer's chain, with the batch accepted there. What follows it is decided once, by the first link
    offered: deciding it is one atomic step.
    """

    def __init__(self, batch: dict[str, _Row] | None = None, is_first: bool = False, is_end: bool = False) -> None:
        """
        :param batch: the accepted rows; None for the first link and the end
        :param is_first: whether this is the link the chain begins with, which accepts nothing
        :param is_end: whether this is the end of the chain, which nothing follows
        """
        self.batch = batch
        self.is_first = is_first
        self.is_end = is_end
        self._next: dict[str, _Link] = {}

    def next(self) -> "_Link | None":
        """
        :return: the link that follows, None if it is not decided
        """
        return self._next.get("link")

    def decide_next(self, link: "_Link") -> "_Link":
        """
        :param link: the link offered
        :return: the link that follows, which is the one offered if none was decided before
        """
        return self._next.setdefault("link", link)


class _Writer:
    """
    The thread writing a store's rows, fed by a chain of accepted batches, started by the first of them and ended by a
    stop, which ends the chain: a batch either precedes the end and is written, or is refused to its sender.
    """

    def __init__(self, store: SymbolStore) -> None:
        self._store = store
        self._head = _Link(is_first=True)
        """the last link the loop has taken; advanced by the loop alone, so that no written link is kept"""
        self._accepted: queue.SimpleQueue[None] = queue.SimpleQueue()
        """a token for each link appended, which the loop waits for"""
        self._ended: queue.SimpleQueue[None] = queue.SimpleQueue()
        """holds a token once the loop has ended, or will never run"""
        self._thread = threading.Thread(target=self._run, name=f"{SymbolStore.WRITER_THREAD_NAME_PREFIX}:{store._path.name}", daemon=True)
        self._successor: dict[str, _Writer] = {}
        self._is_ended = False
        """whether the end of the chain was appended"""

    def send(self, batch: dict[str, _Row]) -> bool:
        """
        Hands the batch to the thread, starting it on the first batch.

        :param batch: the rows to write
        :return: whether the batch was accepted; False if the writer was stopped
        """
        preceding = self._append(_Link(batch))
        if preceding is None:
            return False
        if preceding.is_first:
            self._start()
        return True

    def stop(self) -> None:
        """
        Ends the writer, which accepts no batch from then on, and waits until its thread has written every batch
        accepted before and has ended. A writer whose thread was never started never starts it.
        """
        preceding = self._append(_Link(is_end=True))
        if preceding is not None and preceding.is_first:
            self._ended.put(None)
        # the token is put back for every other stop
        self._ended.get()
        self._ended.put(None)
        if self._thread.ident is not None:
            self._thread.join()

    def is_ended(self) -> bool:
        """
        :return: whether the writer was stopped, and so accepts no batch
        """
        return self._is_ended

    def replace(self, successor: "_Writer") -> "_Writer":
        """
        :param successor: the writer that replaces this one, unless another has replaced it before
        :return: the writer that replaced this one
        """
        return self._successor.setdefault("writer", successor)

    def _append(self, link: _Link) -> _Link | None:
        """
        :param link: the link to append to the chain
        :return: the link it follows, None if the chain has ended
        """
        preceding = self._head
        while not preceding.is_end:
            following = preceding.decide_next(link)
            if following is link:
                if link.is_end:
                    self._is_ended = True
                self._accepted.put(None)
                return preceding
            preceding = following
        return None

    def _start(self) -> None:
        """
        Starts the thread. If it cannot be started, the writer is ended and its batches are written in the caller.
        """
        try:
            self._thread.start()
        except RuntimeError as error:
            log.warning("Cannot start the writer of %s, whose rows are written by their senders: %s", self._store._path, error)
            self._append(_Link(is_end=True))
            self._run()

    def _run(self) -> None:
        try:
            self._write_accepted()
        finally:
            self._abandon()
            self._ended.put(None)

    def _write_accepted(self) -> None:
        # one connection for the loop, so that no batch's end is the last close of the database, which would hold it
        # while checkpointing
        connection: sqlite3.Connection | None = None
        try:
            while (batch := self._take()) is not None:
                if connection is None:
                    try:
                        connection = self._open()
                    except Exception as error:
                        # the taken batch is no link's any more, so no end of the loop gives it back
                        self._store._settle(batch, dirty=True)
                        log.error("Cannot open %s for writing, whose writer ends: %s", self._store._path, error, exc_info=error)
                        return
                self._write(batch, connection)
        finally:
            if connection is not None:
                connection.close()

    def _take(self) -> dict[str, _Row] | None:
        """
        :return: the next batch, which its link lets go of, once one is accepted; None at the end of the chain
        """
        self._accepted.get()
        link = self._head.next()
        assert link is not None, "a token is put for an appended link only"
        self._head = link
        batch, link.batch = link.batch, None
        return batch

    def _open(self) -> sqlite3.Connection | None:
        """
        :return: a connection to write through; None if none can be opened, which leaves each batch to open its own
        """
        try:
            return self._store._open_for_writing()
        except sqlite3.Error as error:
            log.debug("Cannot keep %s open for writing: %s", self._store._path, error)
            return None

    def _write(self, batch: dict[str, _Row], connection: sqlite3.Connection | None) -> None:
        try:
            self._store._write(batch, connection)
        except Exception as error:
            log.error("Failed to save %d document symbol entries to %s: %s", len(batch), self._store._path, error, exc_info=error)

    def _abandon(self) -> None:
        """
        Ends the chain and gives every batch the loop has not taken back to the store.
        """
        self._append(_Link(is_end=True))
        while (link := self._head.next()) is not None:
            self._head = link
            if link.batch is not None:
                self._store._settle(link.batch, dirty=True)
                link.batch = None
