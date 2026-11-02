# SPDX-License-Identifier: GPL-3.0-or-later
import logging
from typing import TYPE_CHECKING

from overrides import override
from sensai.util.logging import LogTime

from serena.language_backend import BuiltinLanguageBackend, LanguageBackend
from serena.util.file_proxy import FileProxy, LocalProjectFileProxy

if TYPE_CHECKING:
    from serena.agent import SerenaAgent
    from serena.code_editor import LanguageServerCodeEditor
    from serena.project import Project
    from serena.repl.facade import ApiScope, Facade
    from serena.tools import Tool
    from serena.util.file_lifecycle import WritePrecondition

log = logging.getLogger(__name__)


class LSPLanguageBackend(LanguageBackend):
    def __init__(self):
        super().__init__(BuiltinLanguageBackend.LSP.value)

    @override
    def get_lsp_tool_class_replacements(self) -> "dict[type[Tool], type[Tool]]":
        return {}

    @override
    def create_facades(self, agent: "SerenaAgent", api_scope: "ApiScope") -> list["Facade"]:
        from ..repl.api.lsp_api import LspApi
        from ..repl.facade import Facade

        return [Facade.from_api(LspApi(agent), api_scope)]

    @override
    def init_active_project(self, agent: "SerenaAgent") -> None:
        with LogTime("Language server initialization", logger=log):
            agent.reset_language_server_manager()

    @override
    def shutdown_active_project(self, project: "Project", timeout: float) -> None:
        # nothing to do; the language server manager is already shut down by the project itself
        pass

    @override
    def get_project_activation_statement(self, project: "Project") -> str:
        language_servers_str = ", ".join(ls.get_key() for ls in project.project_config.language_servers)
        statement = f"Programming languages: {language_servers_str}.\n"

        # activation is a blocking call until the language server manager is up: wait on the project's readiness event
        # with the same bound the read path uses (:meth:`Project.get_language_server_manager_or_raise`). The event lives on
        # the project, so multi-session daemons get a stable per-project signal regardless of activation interleaving.
        ls_manager = project.language_server_manager
        wait_timeout: float | None = None
        if ls_manager is None:
            tool_timeout = project.serena_config.tool_timeout
            wait_timeout = None if (tool_timeout is None or tool_timeout < 0) else tool_timeout
            project._lsm_ready_event.wait(timeout=wait_timeout)
            ls_manager = project.language_server_manager
        if ls_manager is None:
            waited = f"{wait_timeout:g}s" if wait_timeout is not None else "the unbounded activation budget"
            return statement + (
                f"Language servers ({language_servers_str}) had not finished starting within {waited} "
                "(the activation wait budget, not the observed duration; cold starts normally complete "
                "in seconds). The next tool call will block on the readiness event and proceed once a "
                "server is up; query get_current_config or check logs for the latest status.\n"
            )

        # the servers that are up and those that failed
        active = ls_manager.get_active_language_server_ids()
        if active:
            statement += f"Active language servers: {', '.join(ls_id.get_key() for ls_id in active)}.\n"
        unavailable = ls_manager.get_unavailable_languages()
        if unavailable:
            failures = "; ".join(f"{ls_id.get_key()}: {exc}" for ls_id, exc in unavailable.items())
            statement += (
                f"Language servers that failed to start: {failures}. Tools that target these languages will raise a "
                "LanguageUnavailableError until the servers are restarted (use restart_language_server).\n"
            )
        return statement

    @override
    def get_config_overview_statement(self, project: "Project") -> str:
        return f"Language server status: {project.get_language_server_manager_status()}\n"

    @override
    def create_code_editor(self, project: "Project", write_precondition: "WritePrecondition | None" = None) -> "LanguageServerCodeEditor":
        from serena.code_editor import LanguageServerCodeEditor
        from serena.symbol import LanguageServerSymbolRetriever

        symbol_retriever = LanguageServerSymbolRetriever(project)
        return LanguageServerCodeEditor(symbol_retriever, write_precondition=write_precondition)

    @override
    def is_source_file(self, abs_path: str, project: "Project") -> bool:
        is_file_in_supported_languages = False
        for language in project.project_config.language_servers:
            fn_matcher = language.get_source_fn_matcher()
            if fn_matcher.is_relevant_filename(abs_path):
                is_file_in_supported_languages = True
                break
        return is_file_in_supported_languages

    @override
    def is_external_path(self, relative_path: str) -> bool:
        # LSP backend currently uses only true project-relative paths
        return False

    @override
    def create_file_proxy(self, relative_path: str, project: "Project") -> FileProxy:
        return LocalProjectFileProxy(relative_path, project)
