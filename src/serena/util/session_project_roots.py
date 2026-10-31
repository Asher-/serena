"""
Durable record of the project root each MCP session explicitly activated.

:class:`SerenaAgent._explicit_project_roots_by_session` holds this mapping in
process memory, which is enough to survive an evicted per-session slot but not
a daemon restart. Because that map is the ONLY evidence that a session ever
chose a project, losing it makes a session that explicitly activated a worktree
indistinguishable from one that never activated anything -- and the self-heal in
:meth:`Tool.apply_ex` then has nothing to restore. This store backs the map with
a small JSON file so the evidence outlives the process and the session's own
root is restored after a restart instead of the session being stranded.

Only ``str`` session keys are persisted. Tier-3 keys are ``id(mcp_ctx.session)``
ints whose meaning ends with the process, so writing them would let an unrelated
future session inherit a root at a recycled address.

A record is keyed so that every call of one conversation reads it. A client's
``X-Forwarded-Mcp-Session-Id`` changes when its connection ends and a new one
initializes, and a tier-2 session key begins with that id, so the record of a
call carrying a session identifier is keyed on the session key from ``session/``
on. The record of any other call is keyed on its session key.

Every operation is best-effort: a store that cannot be read or written degrades
the cross-restart restore, and must never prevent an activation from succeeding
or a tool call from running. Failures are logged at warning level rather than
raised.
"""

import json
import os
import re
import threading
import time
from typing import Any, Final

from sensai.util import logging

log = logging.getLogger(__name__)

DEFAULT_MAX_AGE_SECONDS: Final[float] = 30 * 24 * 60 * 60
"""
age after which an entry is dropped on the next read. Tier-2 entries are never
evicted in-process (no finalizer is registered against a multiplexer-forwarded
CC session), so without an age bound the file would grow for the life of the
installation.
"""

_SESSION_IDENTIFIER_KEY: Final[re.Pattern[str]] = re.compile(r"[^/]+/(session/.+)", re.DOTALL)
"""
the session key of a call carrying a session identifier, which is the forwarded
session id followed directly by ``session/``. The segment must follow that id: a
pipe session's key is a project root, and a root or an agent identifier may hold
``/session/`` without the key being a session identifier's.
"""


class ExplicitProjectRootStore:
    """
    A crash-safe, process-shared record of each session's explicitly activated project root, by record key (:meth:`_record_key`).

    Reads re-load the file each time rather than caching: several daemons may
    share one ``SERENA_HOME``, and a stale in-process cache would resurrect a
    root the user has since re-pointed elsewhere -- the exact class of error
    this store exists to prevent.
    """

    def __init__(self, path: str, max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS) -> None:
        """
        :param path: the JSON file backing the store; created on first write.
        :param max_age_seconds: entries last written longer ago than this are
            dropped when the file is read.
        """
        self._path = path
        self._max_age_seconds = max_age_seconds
        self._lock = threading.Lock()

    def get(self, session_key: str | int) -> str | None:
        """
        :param session_key: the per-session key :meth:`Tool.apply_ex` derived.
        :return: the project root this session explicitly activated, or None if
            the key is non-durable, unknown, or expired. The record under the
            session's record key is read first, and one under the session key
            itself second.
        """
        if not isinstance(session_key, str):
            return None
        with self._lock:
            entries = self._read()
        entry = entries.get(self._record_key(session_key), entries.get(session_key))
        return entry["root"] if entry is not None else None

    def set(self, session_key: str | int, project_root: str) -> None:
        """
        Record ``project_root`` as the root ``session_key`` explicitly activated,
        under the session's record key.

        :param session_key: the per-session key; non-``str`` keys are ignored.
        :param project_root: the absolute project root that was activated.
        """
        if not isinstance(session_key, str):
            return
        with self._lock:
            entries = self._read()
            entries[self._record_key(session_key)] = {"root": project_root, "updated_at": time.time()}
            self._write(entries)

    def discard(self, session_key: str | int) -> None:
        """
        Drop ``session_key``'s record under the session's record key and under
        the session key itself. Idempotent; unknown keys are a no-op.

        :param session_key: the per-session key being evicted.
        """
        if not isinstance(session_key, str):
            return
        with self._lock:
            entries = self._read()
            removed = [entries.pop(key, None) for key in (self._record_key(session_key), session_key)]
            if any(removed):
                self._write(entries)

    @staticmethod
    def _record_key(session_key: str) -> str:
        """
        :param session_key: the per-session key :meth:`Tool.apply_ex` derived.
        :return: the key of the session's record: the text of ``session_key``
            from ``session/`` on when that segment directly follows the forwarded
            session id, and ``session_key`` itself otherwise.
        """
        match = _SESSION_IDENTIFIER_KEY.fullmatch(session_key)
        return match.group(1) if match is not None else session_key

    def _read(self) -> dict[str, dict[str, Any]]:
        """
        :return: the well-formed, unexpired entries in the file; an empty mapping
            if the file is absent, unreadable, or corrupt. A corrupt file is not
            an error the caller can act on -- the worst case is the same stranded
            session we had before the store existed -- so it degrades rather than
            propagating.
        """
        try:
            with open(self._path, encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            log.warning(f"Could not read the explicit-activation store at {self._path!r}: {e}. Treating it as empty.")
            return {}
        if not isinstance(raw, dict):
            log.warning(f"Explicit-activation store at {self._path!r} is not a JSON object. Treating it as empty.")
            return {}

        cutoff = time.time() - self._max_age_seconds
        entries: dict[str, dict[str, Any]] = {}
        for key, entry in raw.items():
            if not isinstance(key, str) or not isinstance(entry, dict):
                continue
            root = entry.get("root")
            updated_at = entry.get("updated_at")
            if not isinstance(root, str) or not isinstance(updated_at, int | float) or isinstance(updated_at, bool):
                continue
            if updated_at < cutoff:
                continue
            entries[key] = {"root": root, "updated_at": float(updated_at)}
        return entries

    def _write(self, entries: dict[str, dict[str, Any]]) -> None:
        """
        Replace the file with ``entries``.

        Written to a sibling temporary file and renamed, so a crash mid-write
        leaves the previous record intact rather than a truncated one that would
        read as "this session never activated anything".

        :param entries: the full contents to persist.
        """
        tmp_path = f"{self._path}.{os.getpid()}.tmp"
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(entries, f, indent=2, sort_keys=True)
            os.replace(tmp_path, self._path)
        except OSError as e:
            log.warning(
                f"Could not write the explicit-activation store at {self._path!r}: {e}. "
                f"This session's project binding will not survive a daemon restart."
            )
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
