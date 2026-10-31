"""
What a language server's next client needs from the client that ended: a supervised language server
outlives serena, so the next instance adopts it and takes over this state rather than starting a second
server and initializing it (pg://brain/daemon/restart/readoption).
"""

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from solidlsp.util import supervise


@dataclass
class OpenDocument:
    """A document the language server holds open."""

    version: int
    """the version the server last received."""
    digest: str | None
    """the sha256 of the text the server holds, or None when an incremental change left it unknown."""


@dataclass
class LanguageServerState:
    """What a client knows of the language server it talks to."""

    record: str
    """the path of the server's process record."""
    instance_pid: int
    """the pid of the serena instance that last held the server."""
    initialize_result: Any = None
    """the server's answer to initialize."""
    request_id: int = 1
    """the next JSON-RPC id."""
    stdout_offset: int = 0
    """the first offset of the server's stdout not yet consumed."""
    open_documents: dict[str, OpenDocument] = field(default_factory=dict)
    """the documents the server holds open, by URI."""


class LanguageServerStateFile:
    """The file holding one language server's state, named by the command and directory it serves."""

    def __init__(self, command: Any, directory: str | None) -> None:
        """
        :param command: the command that starts the server.
        :param directory: the directory the server runs in, which is the project it serves.
        """
        key = hashlib.sha256(json.dumps([command, directory], default=str).encode()).hexdigest()[:32]
        self.path = supervise.restart_root() / supervise.OWNER / "language_server" / f"{key}.json"

    def load(self) -> LanguageServerState | None:
        """
        :return: the state held in the file, or None when it holds none that can be read.
        """
        try:
            fields = json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            return None
        documents = {uri: OpenDocument(**document) for uri, document in fields.pop("open_documents", {}).items()}
        return LanguageServerState(open_documents=documents, **fields)

    def adoptable(self) -> LanguageServerState | None:
        """
        :return: the state of a live server whose last instance has ended, which this instance can adopt.
            A server another live instance holds is that instance's, and one that has ended is nobody's.
        """
        state = self.load()
        if state is None or state.instance_pid == os.getpid() or supervise.process_exists(state.instance_pid):
            return None
        try:
            ended = supervise.read_outcome(Path(state.record)).ended
        except FileNotFoundError:
            return None
        return None if ended else state

    def save(self, state: LanguageServerState) -> None:
        """
        :param state: the state to hold, written by rename so a reader sees one whole state.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(asdict(state)))
        temporary.replace(self.path)

    def remove(self) -> None:
        """Removes the file: the server it describes is gone."""
        self.path.unlink(missing_ok=True)


def text_digest(text: str) -> str:
    """
    :param text: the text of a document.
    :return: its sha256.
    """
    return hashlib.sha256(text.encode()).hexdigest()
