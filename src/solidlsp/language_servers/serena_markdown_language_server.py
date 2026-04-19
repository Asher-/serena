"""
Markdown Language Server backed by the M4 :class:`MarkdownStructuralLanguage`.

Unlike the Marksman-based implementation in :mod:`solidlsp.language_servers.marksman`,
this server uses Serena's in-house markdown-it-py tokenizer for heading discovery,
so the LSP symbol surface and the structural-edit surface share exactly one parse
model. The custom LSP script is shipped as ``markdown_lsp_server.py`` alongside
this module and launched as a subprocess using the current Python interpreter.
"""

import logging
import os
import pathlib
import sys
import threading

from solidlsp.ls import (
    SolidLanguageServer,
)
from solidlsp.ls_config import LanguageServerConfig
from solidlsp.lsp_protocol_handler.lsp_types import InitializeParams
from solidlsp.lsp_protocol_handler.server import ProcessLaunchInfo
from solidlsp.settings import SolidLSPSettings

log = logging.getLogger(__name__)

_MARKDOWN_LSP_SCRIPT = os.path.join(os.path.dirname(__file__), "markdown_lsp_server.py")


class SerenaMarkdownLanguageServer(SolidLanguageServer):
    """
    Markdown-specific instantiation of :class:`SolidLanguageServer` using the
    M4-backed Serena LSP subprocess.

    The language identifier sent in ``initialize`` is ``markdown`` — the same
    identifier Marksman uses — so client-side heuristics that key off the
    language id continue to work.
    """

    def __init__(self, config: LanguageServerConfig, repository_root_path: str, solidlsp_settings: SolidLSPSettings):
        """
        Constructs a :class:`SerenaMarkdownLanguageServer`.

        Not intended for direct instantiation; use :meth:`SolidLanguageServer.create`.

        :param config: language-server configuration.
        :param repository_root_path: absolute path to the workspace root.
        :param solidlsp_settings: Serena-side solidlsp settings.
        """
        process_launch_info = ProcessLaunchInfo(
            cmd=[sys.executable, _MARKDOWN_LSP_SCRIPT],
            cwd=repository_root_path,
        )
        super().__init__(
            config,
            repository_root_path,
            process_launch_info=process_launch_info,
            language_id="markdown",
            solidlsp_settings=solidlsp_settings,
        )
        self.server_ready = threading.Event()

    @staticmethod
    def _get_initialize_params(repository_absolute_path: str) -> InitializeParams:
        """
        Returns the LSP ``initialize`` params for the Serena markdown server.

        :param repository_absolute_path: absolute workspace root path.
        :return: the ``initialize`` request params dict (typed as
            :class:`InitializeParams`).
        """
        root_uri = pathlib.Path(repository_absolute_path).as_uri()
        initialize_params = {
            "locale": "en",
            "capabilities": {
                "textDocument": {
                    "synchronization": {"didSave": True, "dynamicRegistration": True},
                    "documentSymbol": {
                        "dynamicRegistration": True,
                        "hierarchicalDocumentSymbolSupport": True,
                        "symbolKind": {"valueSet": list(range(1, 27))},
                    },
                },
                "workspace": {
                    "workspaceFolders": True,
                    "symbol": {"dynamicRegistration": True},
                },
            },
            "processId": os.getpid(),
            "rootPath": repository_absolute_path,
            "rootUri": root_uri,
            "workspaceFolders": [{"uri": root_uri, "name": os.path.basename(repository_absolute_path)}],
        }
        return initialize_params  # type: ignore[return-value]

    def _start_server(self) -> None:
        """Starts the Serena markdown LSP subprocess and completes the initialize handshake."""

        def window_log_message(msg: dict) -> None:
            log.info(f"LSP: window/logMessage: {msg}")
            self.server_ready.set()

        def do_nothing(_params: dict) -> None:
            pass

        # register notification handlers so unexpected traffic doesn't stall the handshake
        self.server.on_notification("window/logMessage", window_log_message)
        self.server.on_notification("textDocument/publishDiagnostics", do_nothing)

        # boot the subprocess and perform the initialize/initialized exchange
        log.info("Starting Serena markdown LSP server process")
        self.server.start()
        initialize_params = self._get_initialize_params(self.repository_root_path)

        log.info("Sending initialize request to Serena markdown LSP server")
        init_response = self.server.send.initialize(initialize_params)
        log.debug(f"Received initialize response: {init_response}")

        self.server.notify.initialized({})

        # pygls-backed servers come up effectively instantly; a short wait guards against CI flake
        if not self.server_ready.wait(timeout=2.0):
            log.info("Timeout waiting for Serena markdown server ready signal, proceeding anyway")
            self.server_ready.set()

        log.info("Serena markdown LSP server initialization complete")
