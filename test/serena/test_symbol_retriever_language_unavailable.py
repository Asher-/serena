"""Tests how symbol lookup in one file answers when the file's language server is unavailable."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from serena.ls_manager import LanguageUnavailableError
from serena.symbol import LanguageServerSymbolRetriever
from solidlsp.ls_config import LanguageServerId


def _retriever(tmp_path: Path, error: LanguageUnavailableError) -> LanguageServerSymbolRetriever:
    (tmp_path / "m.py").write_text("def f():\n    pass\n", encoding="utf-8")
    manager = MagicMock()
    manager.get_language_server.side_effect = error
    project = MagicMock()
    project.project_root = str(tmp_path)
    project.get_language_server_manager_or_raise.return_value = manager
    return LanguageServerSymbolRetriever(project)


def test_a_file_of_a_project_requesting_no_language_server_has_no_symbols(tmp_path: Path) -> None:
    retriever = _retriever(tmp_path, LanguageUnavailableError("no language server"))

    assert retriever.find("f", within_relative_path="m.py") == []


def test_a_file_whose_requested_language_server_failed_raises(tmp_path: Path) -> None:
    failure = RuntimeError("startup failed")
    retriever = _retriever(tmp_path, LanguageUnavailableError("python failed", unavailable_languages={LanguageServerId.PYTHON: failure}))

    with pytest.raises(LanguageUnavailableError):
        retriever.find("f", within_relative_path="m.py")
