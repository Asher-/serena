# SPDX-License-Identifier: MIT

import fnmatch
import logging
import os
import shutil
from typing import Any

from overrides import override
from sensai.util.logging import LogTime

from serena.util.file_system import walk_dir_entries
from solidlsp.ls import LanguageServerDependencyProviderSinglePath
from solidlsp.ls_exceptions import SolidLSPException
from solidlsp.lsp_protocol_handler.lsp_types import InitializeResult, TextDocumentSyncKind

from .common import RuntimeDependency, RuntimeDependencyCollection, build_npm_install_command
from .typescript_language_server import TypeScriptLanguageServer

log = logging.getLogger(__name__)

DEFAULT_TYPESCRIPT_VERSION = "7.0.2"


def find_tsgo_executable(install_dir: str) -> str | None:
    """
    :param install_dir: the directory the typescript npm package was installed into
    :return: the first native TypeScript executable, ``@typescript/typescript-*/lib/tsc`` (``tsc.exe`` on Windows), in a
        walk of the install directory's ``node_modules`` that skips hidden directories and follows links to directories,
        entering each real directory at most once; None if there is none
    """
    executable_name = "tsc.exe" if os.name == "nt" else "tsc"
    node_modules = os.path.join(install_dir, "node_modules")
    for directory, _subdirectories, files in walk_dir_entries(
        node_modules, followlinks=True, is_ignored_dir=lambda entry: entry.name.startswith(".")
    ):
        package_dir, lib = os.path.split(directory)
        scope_dir, package = os.path.split(package_dir)
        if (
            lib == "lib"
            and fnmatch.fnmatchcase(package, "typescript-*")
            and os.path.basename(scope_dir) == "@typescript"
            and any(file.name == executable_name for file in files)
        ):
            return os.path.join(directory, executable_name)
    return None


class TsgoLanguageServer(TypeScriptLanguageServer):
    """
    Provides TypeScript support via the native language server of TypeScript 7 (``tsc --lsp --stdio``).

    You can pass the following entries in ls_specific_settings["typescript_tsgo"]:
        - typescript_version: Version of the typescript npm package to install (default: "7.0.2")
        - npm_registry: npm registry to install the typescript package from
        - indexing_timeout: float, timeout in seconds for project indexing (default: 30.0)
        - indexing_start_grace: float, timeout in seconds to wait for indexing progress to *start*
          before the first cross-file reference query (default: 0.0)
    """

    INDEXING_START_GRACE = 0.0

    class DependencyProvider(LanguageServerDependencyProviderSinglePath):
        def _get_or_install_core_dependency(self) -> str:
            typescript_version = self._custom_settings.get("typescript_version", DEFAULT_TYPESCRIPT_VERSION)
            install_dir = os.path.join(self._ls_resources_dir, f"typescript-{typescript_version}")
            executable_path = self._find_executable(install_dir)
            if executable_path is not None:
                return executable_path

            if shutil.which("npm") is None:
                raise SolidLSPException("npm is not installed or isn't in PATH. Please install NodeJS and try again.")
            deps = RuntimeDependencyCollection(
                [
                    RuntimeDependency(
                        id="typescript",
                        description="typescript package",
                        command=build_npm_install_command("typescript", typescript_version, self._custom_settings.get("npm_registry")),
                        platform_id="any",
                    )
                ]
            )
            with LogTime(f"Installation of typescript@{typescript_version}", logger=log):
                deps.install(install_dir)

            executable_path = self._find_executable(install_dir)
            if executable_path is None:
                raise FileNotFoundError(
                    f"No native TypeScript executable found in {install_dir} after installing typescript@{typescript_version}"
                )
            return executable_path

        @staticmethod
        def _find_executable(install_dir: str) -> str | None:
            return find_tsgo_executable(install_dir)

        def _create_launch_command(self, core_path: str) -> list[str]:
            return [core_path, "--lsp", "--stdio"]

    @override
    def _create_tsserver_initialization_options(self) -> dict[str, Any]:
        return {}

    @override
    def _verify_initialize_response(self, init_response: InitializeResult) -> None:
        text_document_sync = init_response["capabilities"].get("textDocumentSync")
        change_kind = text_document_sync.get("change") if isinstance(text_document_sync, dict) else text_document_sync
        if change_kind != TextDocumentSyncKind.Incremental:
            raise SolidLSPException(f"tsc --lsp does not support incremental document sync: textDocumentSync={text_document_sync!r}")

    @override
    def _wait_for_server_ready(self) -> None:
        self.server_ready.set()

    @override
    def _signal_expect_indexing(self) -> None:
        pass
