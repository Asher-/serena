# SPDX-License-Identifier: GPL-3.0-or-later

import logging
import os.path
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from sensai.util.logging import LogTime

from serena.config.serena_config import ProjectConfig, SerenaPaths
from solidlsp import SolidLanguageServer
from solidlsp.ls_config import LanguageServerConfig, LanguageServerIdLike
from solidlsp.lsp_protocol_handler.lsp_types import DidChangeWatchedFilesParams, FileChangeType, FileEvent
from solidlsp.settings import SolidLSPSettings

if TYPE_CHECKING:
    from .project import Project

log = logging.getLogger(__name__)


class LanguageUnavailableError(Exception):
    """
    raised when a language server is requested for a language that is not currently running.

    :ivar language: the language whose server is unavailable (``None`` if the caller could not identify
        a specific language, e.g. when the manager has no running servers at all)
    :ivar cause: the original exception captured during the failed startup (if any)
    :ivar unavailable_languages: all languages whose servers are currently unavailable on the manager,
        mapped to the respective startup exception
    """

    def __init__(
        self,
        message: str,
        language: "LanguageServerIdLike | None" = None,
        cause: Exception | None = None,
        unavailable_languages: "dict[LanguageServerIdLike, Exception] | None" = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.language = language
        self.cause = cause
        self.unavailable_languages = unavailable_languages or {}


class LanguageServerFactory:
    def __init__(
        self,
        project_root: str,
        project_config: ProjectConfig,
        project_data_path: str,
        encoding: str,
        ignored_patterns: list[str],
        ls_timeout: float | None = None,
        ls_specific_settings: dict | None = None,
        trace_lsp_communication: bool = False,
        is_project_trusted: bool = True,
    ):
        self.project_root = project_root
        self.project_config = project_config
        self.project_data_path = project_data_path
        self.encoding = encoding
        self.ignored_patterns = ignored_patterns
        self.ls_timeout = ls_timeout
        self.ls_specific_settings = ls_specific_settings
        self.trace_lsp_communication = trace_lsp_communication
        self.is_project_trusted = is_project_trusted

    def create_language_server(self, ls_id: LanguageServerIdLike) -> SolidLanguageServer:
        ls_config = LanguageServerConfig(
            workspace_folders=self.project_config.ls_workspace_folders,
            additional_workspace_folders=self.project_config.ls_additional_workspace_folders,
            ls_id=ls_id,
            ignored_paths=self.ignored_patterns,
            trace_lsp_communication=self.trace_lsp_communication,
            encoding=self.encoding,
            is_project_trusted=self.is_project_trusted,
        )

        log.info(f"Creating language server instance for {self.project_root}, language={ls_id}.")
        return SolidLanguageServer.create(
            ls_config,
            self.project_root,
            timeout=self.ls_timeout,
            solidlsp_settings=SolidLSPSettings(
                solidlsp_dir=SerenaPaths().serena_user_home_dir,
                project_data_path=self.project_data_path,
                ls_specific_settings=self.ls_specific_settings or {},
            ),
        )


class LanguageServerManager:
    """
    Manages one or more language servers for a project.
    """

    def __init__(
        self,
        language_servers: dict[LanguageServerIdLike, SolidLanguageServer],
        language_server_factory: LanguageServerFactory | None = None,
        project: "Project | None" = None,
        unavailable_languages: dict[LanguageServerIdLike, Exception] | None = None,
    ) -> None:
        """
        :param language_servers: a mapping from language to language server; the servers are assumed to be already started.
            The first server in the iteration order is used as the default server.
            All servers are assumed to serve the same project root.
        :param language_server_factory: factory for language server creation; if None, dynamic (re)creation of language servers
            is not supported
        :param unavailable_languages: a mapping from language to the exception captured during a failed startup attempt;
            these languages are known to the manager but not currently served. Callers that request them receive a
            typed :class:`LanguageUnavailableError` so the failure surface is loud rather than silent.
        """
        self._language_servers = language_servers
        self._language_server_factory = language_server_factory
        self._file_change_notifier = LanguageServerFileChangeNotifier(project, self) if project is not None else None
        self._unavailable_languages: dict[LanguageServerIdLike, Exception] = dict(unavailable_languages or {})
        self._restart_locks: dict[LanguageServerIdLike, threading.RLock] = {}
        self._restart_locks_guard = threading.Lock()

    @property
    def _default_language_server(self) -> SolidLanguageServer:
        """
        :return: the first running language server in iteration order
        :raises LanguageUnavailableError: if the manager has no running servers at all (every requested
            language is in ``_unavailable_languages`` or none were requested)
        """
        if len(self._language_servers) == 0:
            unavailable = dict(self._unavailable_languages)
            if unavailable:
                summary = ", ".join(f"{lang.value}: {exc}" for lang, exc in unavailable.items())
                raise LanguageUnavailableError(
                    f"No language server is running. All requested languages failed to start ({summary}).",
                    unavailable_languages=unavailable,
                )
            raise LanguageUnavailableError(
                "No language servers available in the manager.",
                unavailable_languages=unavailable,
            )
        return next(iter(self._language_servers.values()))

    @staticmethod
    def from_languages(
        languages: list[LanguageServerIdLike], factory: LanguageServerFactory, project: "Project"
    ) -> "LanguageServerManager":
        """
        Creates a manager with language servers for the given languages using the given factory.
        The language servers are started in parallel threads. Languages that fail to start are
        tracked on the returned manager as unavailable rather than preventing construction; callers
        that attempt to use them receive a typed :class:`LanguageUnavailableError` at the point of
        use. This policy keeps partial-success working — a failing Scala server no longer tears
        down a functioning Python server — while making the failure loud at the point of use rather
        than silently degrading.

        :param languages: the languages for which to spawn language servers
        :param factory: the factory for language server creation
        :param project: the project for which the language servers are created
                :return: the instance, potentially with a subset of the requested languages served
        """

        class StartLSThread(threading.Thread):
            def __init__(self, ls_id: LanguageServerIdLike):
                super().__init__(target=self._start_language_server, name="StartLS:" + ls_id.get_key())
                self.ls_id = ls_id
                self.language_server: SolidLanguageServer | None = None
                self.exception: Exception | None = None

            def _start_language_server(self) -> None:
                try:
                    with LogTime(f"Language server startup (ls_id={self.ls_id.get_key()})"):
                        self.language_server = factory.create_language_server(self.ls_id)
                        self.language_server.start()
                        if not self.language_server.is_running():
                            raise RuntimeError(f"Failed to start the language server {self.ls_id.get_key()}")
                except Exception as e:
                    log.error(f"Error starting language server {self.ls_id.get_key()}: {e}", exc_info=e)
                    self.exception = e

        # start language servers in parallel threads
        threads = []
        for language in languages:
            thread = StartLSThread(language)
            thread.start()
            threads.append(thread)

        # collect successfully-started servers and per-language startup exceptions
        language_servers: dict[LanguageServerIdLike, SolidLanguageServer] = {}
        unavailable: dict[LanguageServerIdLike, Exception] = {}
        for thread in threads:
            thread.join()
            if thread.exception is not None:
                unavailable[thread.ls_id] = thread.exception
                # if a server was partially created before start() failed, stop it so we don't leak the process
                if thread.ls_id_server is not None:
                    try:
                        thread.ls_id_server.stop()
                    except Exception as cleanup_error:
                        log.debug(
                            f"Ignoring cleanup error while stopping partially-started {thread.ls_id.value} server: {cleanup_error}"
                        )
            elif thread.language_server is not None:
                language_servers[thread.ls_id] = thread.language_server

        # surface a summary of what started and what did not; the manager is returned even when all languages fail so the
        # agent-facing APIs have a stable object and the escalation contract (typed error at the point of use) holds
        if unavailable:
            failure_summary = "; ".join(f"{lang.get_key()}: {e}" for lang, e in unavailable.items())
            log.warning(
                f"Language server manager constructed with degraded coverage "
                f"(running: {[lang.get_key() for lang in language_servers]}, "
                f"unavailable: {[lang.get_key() for lang in unavailable]}). Failures: {failure_summary}"
            )
        else:
            log.info(f"Language server manager started for languages: {[lang.get_key() for lang in language_servers]}")

        return LanguageServerManager(language_servers, factory, project, unavailable)

    def _restart_lock(self, ls_id: LanguageServerIdLike) -> threading.RLock:
        """
        :param ls_id: the language server id
        :return: the lock that serializes the replacement of the given language's server
        """
        with self._restart_locks_guard:
            return self._restart_locks.setdefault(ls_id, threading.RLock())

    def _ensure_functional_ls(self, ls: SolidLanguageServer) -> SolidLanguageServer:
        if ls.is_running():
            return ls

        # replace the server under its language's restart lock, unless a caller that held the lock first already did
        with self._restart_lock(ls.ls_id):
            current = self._language_servers.get(ls.ls_id)
            if current is not None and current is not ls and current.is_running():
                return current
            log.warning(f"Language server for language {ls.ls_id} is not running; restarting ...")
            return self.restart_language_server(ls.ls_id)

    def _get_suitable_language_server(self, relative_path: str) -> SolidLanguageServer | None:
        """:param relative_path: relative path to a file"""
        for candidate in self._language_servers.values():
            if not candidate.is_ignored_path(relative_path, ignore_unsupported_files=True):
                return candidate
        return None

    def get_language_server(self, relative_path: str) -> SolidLanguageServer:
        """:param relative_path: relative path to a file"""
        ls: SolidLanguageServer | None = None
        if len(self._language_servers) > 1:
            if os.path.isdir(relative_path):
                raise ValueError(f"Expected a file path, but got a directory: {relative_path}")
            ls = self._get_suitable_language_server(relative_path)
        if ls is None:
            ls = self._default_language_server
        return self._ensure_functional_ls(ls)

    def _create_and_start_language_server(self, ls_id: LanguageServerIdLike) -> SolidLanguageServer:
        if self._language_server_factory is None:
            raise ValueError(f"No language server factory available to create language server for {ls_id}")
        language_server = self._language_server_factory.create_language_server(ls_id)
        try:
            language_server.start()
        except Exception:
            # stop a server whose start failed, so a partly started process is not left running
            language_server.stop()
            raise
        self._language_servers[ls_id] = language_server
        return language_server

    def restart_language_server(self, ls_id: LanguageServerIdLike) -> SolidLanguageServer:
        """
        Forces recreation and restart of the language server for the given language. Works for
        languages that are currently running (their LS is assumed to be no longer functional) as
        well as for languages that were recorded as unavailable after a failed startup. The server
        the restart replaces is stopped once its replacement is in place.

        :param ls_id: the language server identifier
        :return: the newly created language server
        :raises LanguageUnavailableError: if creation/startup fails; the language remains recorded
            as unavailable with the new failure cause
        :raises ValueError: if the language was never requested for this manager
        """
        with self._restart_lock(ls_id):
            if ls_id not in self._language_servers and ls_id not in self._unavailable_languages:
                raise ValueError(f"No language server for language {ls_id.get_key()} is known to this manager; cannot restart")
            replaced = self._language_servers.get(ls_id)
            try:
                ls = self._create_and_start_language_server(ls_id)
            except Exception as e:
                self._unavailable_languages[ls_id] = e
                raise LanguageUnavailableError(
                    f"Failed to (re)start language server for {ls_id.get_key()}: {e}",
                    language=ls_id,
                    cause=e,
                    unavailable_languages=dict(self._unavailable_languages),
                ) from e
            # successful startup clears any prior unavailability for the language
            self._unavailable_languages.pop(ls_id, None)

            # stop the replaced server now that its replacement holds the slot
            if replaced is not None:
                replaced.stop()
            return ls

    def add_language_server(self, ls_id: LanguageServerIdLike) -> SolidLanguageServer:
        """
        Dynamically adds a new language server for the given language.

        :param ls_id: the language server to add
        :return: the newly created language server
        """
        if ls_id in self._language_servers:
            raise ValueError(f"Language server {ls_id.get_key()} already present")
        return self._create_and_start_language_server(ls_id)

    def remove_language_server(self, ls_id: LanguageServerIdLike, save_cache: bool = False) -> None:
        """
        Removes the language server for the given language, stopping it if it is running.

        :param ls_id: the language
        """
        if ls_id not in self._language_servers:
            raise ValueError(f"No language server for language {ls_id.get_key()} present; cannot remove")
        ls = self._language_servers.pop(ls_id)
        self._stop_language_server(ls, save_cache=save_cache)

    def get_active_language_server_ids(self) -> list[LanguageServerIdLike]:
        """
        Returns the list of languages for which language servers are currently managed.

        :return: list of languages
        """
        return list(self._language_servers.keys())

    def get_unavailable_languages(self) -> dict[LanguageServerIdLike, Exception]:
        """
        :return: a copy of the languages whose servers are currently not running, mapped to the
            exception captured when their startup (or last restart attempt) failed
        """
        return dict(self._unavailable_languages)

    def is_language_available(self, language: LanguageServerIdLike) -> bool:
        """
        :param language: the language
        :return: ``True`` if the manager has a running server for this language; ``False`` if the
            language was requested but failed to start or was never requested at all
        """
        return language in self._language_servers

    @staticmethod
    def _stop_language_server(ls: SolidLanguageServer, save_cache: bool = False, timeout: float = 2.0) -> None:
        if ls.is_running():
            if save_cache:
                ls.save_cache()
            log.info(f"Stopping language server for language {ls.ls_id} ...")
            ls.stop(shutdown_timeout=timeout)

    def iter_language_servers(self) -> Iterator[SolidLanguageServer]:
        for ls in self._language_servers.values():
            yield self._ensure_functional_ls(ls)

    def stop_all(self, save_cache: bool = False, timeout: float = 2.0) -> None:
        """
        Stops all managed language servers.

        :param save_cache: whether to save the cache before stopping
        :param timeout: timeout for shutdown of each language server
        """
        for ls in self.iter_language_servers():
            self._stop_language_server(ls, save_cache=save_cache, timeout=timeout)

    def save_all_caches(self) -> None:
        """
        Saves the caches of all managed language servers.
        """
        for ls in self.iter_language_servers():
            if ls.is_running():
                ls.save_cache()

    def has_suitable_ls_for_file(self, relative_file_path: str) -> bool:
        return self._get_suitable_language_server(relative_file_path) is not None

    def sync_file_system_changes(self) -> int:
        """
        Polls the file system for changes to source files and notifies the language servers of any changes
        (particularly changes that happened outside of Serena's own file tools, which are not covered by
        the notifications sent by those tools/CodeEditor).

        :return: the number of individual file change events detected (0 if nothing changed).
        """
        log.info("Polling file system for changes to source files ...")
        if self._file_change_notifier is None:
            return 0
        num_changes = self._file_change_notifier.poll_and_notify()
        log.info(f"File system polling complete; {num_changes} change events sent to language servers.")
        return num_changes


class LanguageServerFileChangeNotifier:
    """
    Detects changes to source files on disk and notifies language servers of those changes.
    """

    def __init__(self, project: "Project", language_server_manager: LanguageServerManager, initial_poll: bool = True) -> None:
        self._project = project
        self._language_server_manager = language_server_manager
        self._freshness_last_seen_mtimes: dict[str, float] | None = None
        self._freshness_lock = threading.Lock()

        if initial_poll:
            # Establish the baseline for the first poll; no notifications are sent on the first call.
            with LogTime("Initialising file change notifier (polling for baseline)"):
                self.poll_and_notify()

    def poll_and_notify(self) -> int:
        """
        Detects source files that were changed, created or deleted on disk since the last call
        and notifies every language server managed for this project via the LSP
        ``workspace/didChangeWatchedFiles`` notification.

        This exists because Serena's own file and symbol tools notify the language server inline
        (via didOpen/didChange/didClose) when they edit a file, but edits made through any other
        channel (another editor, a second agent, a git checkout, a build step) are otherwise
        invisible to a warm language server, causing symbolic queries to answer from a stale index.

        The set of files considered is exactly the set Serena itself tracks (see
        :meth:`gather_source_files`), so no separate file-discovery logic has to be kept in sync.
        The dominant cost is the directory walk plus one ``os.stat`` per tracked file; this is
        intended to be called before symbolic tool invocations rather than on a timer.

        :return: the number of change events sent (0 if nothing changed, if no language server is
            running yet, or on the first call, which only establishes the baseline).
        """
        current: dict[str, float] = {}
        for source_file in self._project.gather_source_files():
            try:
                stat = source_file.dir_entry.stat()
                current[source_file.rel_path] = stat.st_mtime
            except OSError:
                continue

        # Read-diff-swap under the lock only; the filesystem walk above and the LSP notifications
        # below stay outside it so concurrent callers do not serialize on I/O.
        with self._freshness_lock:
            previous = self._freshness_last_seen_mtimes
            self._freshness_last_seen_mtimes = current

            if previous is None:
                return 0

            # compute the set of individual events (created, changed, deleted)
            events: list[tuple[str, FileChangeType]] = []
            for rel_path, mtime in current.items():
                prev_mtime = previous.get(rel_path)
                if prev_mtime is None:
                    events.append((rel_path, FileChangeType.Created))
                elif mtime > prev_mtime:
                    events.append((rel_path, FileChangeType.Changed))
            events.extend((rel_path, FileChangeType.Deleted) for rel_path in previous if rel_path not in current)

        if not events:
            return 0

        # create the change didChangeWatchedFiles notification
        changes: list[FileEvent] = [
            {"uri": Path(self._project.project_root, rel_path).resolve().as_uri(), "type": change_type} for rel_path, change_type in events
        ]
        params: DidChangeWatchedFilesParams = {"changes": changes}
        created_paths = [rel_path for rel_path, change_type in events if change_type == FileChangeType.Created]

        for ls in self._language_server_manager.iter_language_servers():
            # send the didChangeWatchedFiles notification to the language server
            try:
                ls.server.notify.did_change_watched_files(params)
            except Exception as e:
                log.error("Failed to notify language server of watched file changes", exc_info=e)

            # A didChangeWatchedFiles(Created) notification alone is not enough for every backend
            # (observed with pyright) to fold a brand-new file into its cross-file reference graph;
            # an open/close cycle forces the parse+bind that Serena's own file tools trigger via
            # SolidLanguageServer.open_file().
            relevant_created_paths = [
                rel_path for rel_path in created_paths if not ls.is_ignored_path(rel_path, ignore_unsupported_files=True)
            ]
            if relevant_created_paths:
                try:
                    ls.notify_files_created(relevant_created_paths)
                except Exception as e:
                    log.error("Failed to notify language server of newly created files", exc_info=e)
            for rel_path in relevant_created_paths:
                try:
                    with ls.open_file(rel_path):
                        pass
                except Exception as e:
                    log.error(f"Failed to refresh newly created file {rel_path!r} in language server", exc_info=e)

        return len(events)
