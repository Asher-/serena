"""
What the next instance needs to take over a kept process: a supervised language server or structural bridge outlives
serena, so the next instance adopts it and takes over its state rather than starting a second process
(pg://brain/daemon/restart/readoption). Each kept process has a state file of its own, which an instance claims by
rename, so of the instances reaching one file one takes it. Serena takes over every abandoned one when it starts and holds
it for the first use of its command, which attaches to it; one that ends first has its outcome consumed.
"""

import hashlib
import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Generic, TypeVar

from solidlsp.util import supervise

log = logging.getLogger(__name__)

LANGUAGE_SERVER_KIND = "solidlsp.language_server"
"""The waiting kind of a language server's record."""


@dataclass
class KeptState:
    """What an instance holds of a kept process, and what every state a next instance takes over begins with."""

    record: str
    """the path of the process's record."""
    instance_pid: int
    """the pid of the serena instance that last held the process."""
    instance_start: str = ""
    """the kernel start time of that instance, which with the pid names it."""

    @classmethod
    def from_fields(cls, fields: dict[str, Any]) -> "KeptState":
        """
        :param fields: the fields of a state file.
        :return: the state they hold.
        """
        return cls(**fields)

    def held(self) -> bool:
        """
        :return: whether the instance that last held the process is alive, this one included, so the process is that
            instance's.
        """
        return supervise.alive(self.instance_pid, self.instance_start)

    def hold(self) -> None:
        """Names this instance as the one holding the process."""
        self.instance_pid = os.getpid()
        self.instance_start = supervise.start_time(self.instance_pid)

    def adoptable(self) -> bool:
        """
        :return: whether a next instance can take over the live process this state describes.
        """
        return True


S = TypeVar("S", bound=KeptState)


class KeptStates(Generic[S]):
    """
    The state files of the kept processes one command runs, one file per process named by its record, under a directory
    of the command's own. One command runs in as many processes as serena has uses for it.
    """

    KIND: ClassVar[str]
    """the name of the directory holding the command directories of one kind of kept process."""
    STATE_TYPE: ClassVar[type[KeptState]]
    """the type of the states."""

    def __init__(self, key: Any) -> None:
        """
        :param key: what names the command, as JSON.
        """
        digest = hashlib.sha256(json.dumps(key, default=str).encode()).hexdigest()[:32]
        self._directory = self.kind_directory() / digest

    @classmethod
    def kind_directory(cls) -> Path:
        """
        :return: the directory holding the command directories of this kind.
        """
        return supervise.restart_root() / supervise.OWNER / cls.KIND

    @classmethod
    def everywhere(cls) -> list["KeptStates[S]"]:
        """
        :return: the state files of every command of this kind that has any.
        """
        found = []
        directory = cls.kind_directory()
        for command_directory in sorted(directory.iterdir()) if directory.is_dir() else ():
            if command_directory.is_dir():
                states = cls.__new__(cls)
                states._directory = command_directory
                found.append(states)
        return found

    @classmethod
    def named_records(cls) -> set[Path]:
        """
        :return: the records some state of this kind names, whichever instance holds it.
        """
        return {Path(state.record) for states in cls.everywhere() for state in states._states()}

    @classmethod
    def hold_everywhere(cls) -> None:
        """
        Takes over every live process of this kind an ended instance held, each held for the first use of its command, and
        consumes the outcome of each that has ended. Serena does this when it starts, so a process it never uses is owned.
        """
        for states in cls.everywhere():
            while (state := states.take_over()) is not None:
                HOLDINGS.hold(states, state)

    @property
    def key(self) -> str:
        """The name of the command's state files, the same in every instance."""
        return str(self._directory)

    def _states(self) -> list[S]:
        return [state for path in sorted(self._directory.glob("*.json")) if (state := self._load(path)) is not None]

    def take_over(self) -> S | None:
        """
        :return: the state of a live process an ended instance held, now this instance's, or None when there is none. On
            the way, each such process that has ended has its outcome consumed, and each that cannot be taken over is
            stopped.
        """
        while (state := self.claim()) is not None:
            record = supervise.Record.load(Path(state.record)) if Path(state.record).exists() else None
            if record is None:
                self.remove(state)
                continue
            if not record.outcome().ended and not state.adoptable():
                log.info("Stopping %s, which no instance can take over", state.record)
                record.stop()
            if record.outcome().ended:
                record.discard()
                self.remove(state)
                continue
            return state
        return None

    def claim(self) -> S | None:
        """
        :return: the state of a process whose last instance has ended, now held by this instance, or None when there is
            none. A process another live instance holds is that instance's.
        """
        for path in sorted(self._directory.glob("*.json")):
            state = self._claimed(path)
            if state is not None:
                return state
        return None

    def _claimed(self, path: Path) -> S | None:
        state = self._load(path)
        if state is None or state.held():
            return None

        # the rename takes the file from every other instance while this one looks again: an instance that finished
        # claiming before the rename has written the file back under its own identity, so it is read once more here
        claim = path.with_name(f"{path.name}.{os.getpid()}.claim")
        try:
            path.rename(claim)
        except FileNotFoundError:
            return None
        current = self._load(claim)
        if current is None or current.held():
            claim.rename(path)
            return None

        current.hold()
        self.save(current)
        claim.unlink()
        return current

    def _load(self, path: Path) -> S | None:
        try:
            return self.STATE_TYPE.from_fields(json.loads(path.read_text()))  # type: ignore[return-value]
        except (FileNotFoundError, ValueError, TypeError):
            return None

    def save(self, state: S) -> None:
        """
        :param state: the state to hold, written by rename so a reader sees one whole state.
        """
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self._path(state)
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(asdict(state)))
        temporary.replace(path)

    def remove(self, state: S) -> None:
        """
        :param state: the state of a process that is gone.
        """
        self._path(state).unlink(missing_ok=True)

    def _path(self, state: S) -> Path:
        return self._directory / f"{Path(state.record).stem}.json"


class Holdings:
    """
    The kept processes this instance took over when it started that no first use of their command has attached yet. Each
    is owned from the start: its state names this instance, and a watcher consumes the outcome of one that ends unattached,
    as the instance that started it would have (pg://brain/daemon/restart/readoption).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._held: dict[str, list[KeptState]] = {}

    def hold(self, states: KeptStates[S], state: S) -> None:
        """
        :param states: the state files of the process's command.
        :param state: the process's state, which names this instance.
        """
        with self._lock:
            self._held.setdefault(states.key, []).append(state)
        threading.Thread(target=self._watch, args=(states, state), name=f"held-{Path(state.record).stem}", daemon=True).start()

    def take(self, states: KeptStates[S]) -> S | None:
        """
        :param states: the state files of a command.
        :return: the state of a live process of that command this instance holds, now the caller's, or None. A held
            process found ended has its outcome consumed.
        """
        while True:
            with self._lock:
                held = self._held.get(states.key)
                state = held.pop(0) if held else None
            if state is None:
                return None
            record = Path(state.record)
            if record.exists() and not supervise.Record.load(record).outcome().ended:
                return state  # type: ignore[return-value]
            if record.exists():
                supervise.Record.load(record).discard()
            states.remove(state)  # type: ignore[arg-type]

    def _watch(self, states: KeptStates[S], state: S) -> None:
        # the process's end, unless a first use takes the process first
        try:
            record: supervise.Record | None = supervise.Record.load(Path(state.record))
        except FileNotFoundError:
            record = None
        while record is not None and not record.outcome().ended:
            if not self._holds(states, state):
                return
            time.sleep(supervise.WATCH_SECONDS)

        # its outcome consumed, as the instance that started it would have
        with self._lock:
            held = self._held.get(states.key, [])
            if not any(entry is state for entry in held):
                return
            held.remove(state)
        if record is not None:
            record.discard()
        states.remove(state)

    def _holds(self, states: KeptStates[S], state: S) -> bool:
        with self._lock:
            return any(entry is state for entry in self._held.get(states.key, []))


HOLDINGS = Holdings()
"""The kept processes this instance holds for the first use of their command."""


@dataclass
class OpenDocument:
    """A document the language server holds open."""

    version: int
    """the version the server last received."""
    digest: str | None
    """the sha256 of the text the server holds, or None when an incremental change left it unknown."""


@dataclass
class LanguageServerState(KeptState):
    """What a client knows of the language server it talks to."""

    initialize_result: Any = None
    """the server's answer to initialize."""
    request_id: int = 1
    """the next JSON-RPC id."""
    stdout_offset: int = 0
    """the first offset of the server's stdout not yet consumed."""
    open_documents: dict[str, OpenDocument] = field(default_factory=dict)
    """the documents the server holds open, by URI."""

    @classmethod
    def from_fields(cls, fields: dict[str, Any]) -> "LanguageServerState":
        documents = {uri: OpenDocument(**document) for uri, document in fields.pop("open_documents", {}).items()}
        return cls(open_documents=documents, **fields)

    def adoptable(self) -> bool:
        """
        :return: whether the server answered initialize: a server that never did has no capabilities to hand a next client.
        """
        return self.initialize_result is not None


class LanguageServerStates(KeptStates[LanguageServerState]):
    """The state files of the language servers one command runs in one directory, which is the project they serve."""

    KIND = "language_server"
    STATE_TYPE = LanguageServerState

    def __init__(self, command: Any, directory: str | None) -> None:
        """
        :param command: the command that starts the servers.
        :param directory: the directory they run in.
        """
        super().__init__([command, directory])


def text_digest(text: str) -> str:
    """
    :param text: the text of a document.
    :return: its sha256.
    """
    return hashlib.sha256(text.encode()).hexdigest()
