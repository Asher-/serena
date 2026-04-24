import logging
import os.path
import threading
from collections.abc import Iterator

from sensai.util.logging import LogTime

from serena.config.serena_config import SerenaPaths
from solidlsp import SolidLanguageServer
from solidlsp.ls_config import Language, LanguageServerConfig
from solidlsp.settings import SolidLSPSettings

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
        language: "Language | None" = None,
        cause: Exception | None = None,
        unavailable_languages: "dict[Language, Exception] | None" = None,
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
        project_data_path: str,
        encoding: str,
        ignored_patterns: list[str],
        ls_timeout: float | None = None,
        ls_specific_settings: dict | None = None,
        trace_lsp_communication: bool = False,
    ):
        self.project_root = project_root
        self.project_data_path = project_data_path
        self.encoding = encoding
        self.ignored_patterns = ignored_patterns
        self.ls_timeout = ls_timeout
        self.ls_specific_settings = ls_specific_settings
        self.trace_lsp_communication = trace_lsp_communication

    def create_language_server(self, language: Language) -> SolidLanguageServer:
        ls_config = LanguageServerConfig(
            code_language=language,
            ignored_paths=self.ignored_patterns,
            trace_lsp_communication=self.trace_lsp_communication,
            encoding=self.encoding,
        )

        log.info(f"Creating language server instance for {self.project_root}, language={language}.")
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
        language_servers: dict[Language, SolidLanguageServer],
        language_server_factory: LanguageServerFactory | None = None,
        unavailable_languages: dict[Language, Exception] | None = None,
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
        self._unavailable_languages: dict[Language, Exception] = dict(unavailable_languages or {})

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
    def from_languages(languages: list[Language], factory: LanguageServerFactory) -> "LanguageServerManager":
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
        :return: the instance, potentially with a subset of the requested languages served
        """

        class StartLSThread(threading.Thread):
            def __init__(self, language: Language):
                super().__init__(target=self._start_language_server, name="StartLS:" + language.value)
                self.language = language
                self.language_server: SolidLanguageServer | None = None
                self.exception: Exception | None = None

            def _start_language_server(self) -> None:
                try:
                    with LogTime(f"Language server startup (language={self.language.value})"):
                        self.language_server = factory.create_language_server(self.language)
                        self.language_server.start()
                        if not self.language_server.is_running():
                            raise RuntimeError(f"Failed to start the language server for language {self.language.value}")
                except Exception as e:
                    log.error(f"Error starting language server for language {self.language.value}: {e}", exc_info=e)
                    self.exception = e

        # start language servers in parallel threads
        threads = []
        for language in languages:
            thread = StartLSThread(language)
            thread.start()
            threads.append(thread)

        # collect successfully-started servers and per-language startup exceptions
        language_servers: dict[Language, SolidLanguageServer] = {}
        unavailable: dict[Language, Exception] = {}
        for thread in threads:
            thread.join()
            if thread.exception is not None:
                unavailable[thread.language] = thread.exception
                # if a server was partially created before start() failed, stop it so we don't leak the process
                if thread.language_server is not None:
                    try:
                        thread.language_server.stop()
                    except Exception as cleanup_error:
                        log.debug(
                            f"Ignoring cleanup error while stopping partially-started {thread.language.value} server: {cleanup_error}"
                        )
            elif thread.language_server is not None:
                language_servers[thread.language] = thread.language_server

        # surface a summary of what started and what did not; the manager is returned even when all languages fail so the
        # agent-facing APIs have a stable object and the escalation contract (typed error at the point of use) holds
        if unavailable:
            failure_summary = "; ".join(f"{lang.value}: {e}" for lang, e in unavailable.items())
            log.warning(
                f"Language server manager constructed with degraded coverage "
                f"(running: {[lang.value for lang in language_servers]}, "
                f"unavailable: {[lang.value for lang in unavailable]}). Failures: {failure_summary}"
            )
        else:
            log.info(f"Language server manager started for languages: {[lang.value for lang in language_servers]}")

        return LanguageServerManager(language_servers, factory, unavailable)

    def _ensure_functional_ls(self, ls: SolidLanguageServer) -> SolidLanguageServer:
        if not ls.is_running():
            log.warning(f"Language server for language {ls.language} is not running; restarting ...")
            ls = self.restart_language_server(ls.language)
        return ls

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

    def _create_and_start_language_server(self, language: Language) -> SolidLanguageServer:
        if self._language_server_factory is None:
            raise ValueError(f"No language server factory available to create language server for {language}")
        language_server = self._language_server_factory.create_language_server(language)
        language_server.start()
        self._language_servers[language] = language_server
        return language_server

    def restart_language_server(self, language: Language) -> SolidLanguageServer:
        """
        Forces recreation and restart of the language server for the given language. Works for
        languages that are currently running (their LS is assumed to be no longer functional) as
        well as for languages that were recorded as unavailable after a failed startup.

        :param language: the language
        :return: the newly created language server
        :raises LanguageUnavailableError: if creation/startup fails; the language remains recorded
            as unavailable with the new failure cause
        :raises ValueError: if the language was never requested for this manager
        """
        if language not in self._language_servers and language not in self._unavailable_languages:
            raise ValueError(f"No language server for language {language.value} is known to this manager; cannot restart")
        try:
            ls = self._create_and_start_language_server(language)
        except Exception as e:
            self._unavailable_languages[language] = e
            raise LanguageUnavailableError(
                f"Failed to (re)start language server for {language.value}: {e}",
                language=language,
                cause=e,
                unavailable_languages=dict(self._unavailable_languages),
            ) from e
        # successful startup clears any prior unavailability for the language
        self._unavailable_languages.pop(language, None)
        return ls

    def add_language_server(self, language: Language) -> SolidLanguageServer:
        """
        Dynamically adds a new language server for the given language.

        :param language: the language
        :param factory: the factory to create the language server
        :return: the newly created language server
        """
        if language in self._language_servers:
            raise ValueError(f"Language server for language {language.value} already present")
        return self._create_and_start_language_server(language)

    def remove_language_server(self, language: Language, save_cache: bool = False) -> None:
        """
        Removes the language server for the given language, stopping it if it is running.

        :param language: the language
        """
        if language not in self._language_servers:
            raise ValueError(f"No language server for language {language.value} present; cannot remove")
        ls = self._language_servers.pop(language)
        self._stop_language_server(ls, save_cache=save_cache)

    def get_active_languages(self) -> list[Language]:
        """
        Returns the list of languages for which language servers are currently managed.

        :return: list of languages
        """
        return list(self._language_servers.keys())

    def get_unavailable_languages(self) -> dict[Language, Exception]:
        """
        :return: a copy of the languages whose servers are currently not running, mapped to the
            exception captured when their startup (or last restart attempt) failed
        """
        return dict(self._unavailable_languages)

    def is_language_available(self, language: Language) -> bool:
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
            log.info(f"Stopping language server for language {ls.language} ...")
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
