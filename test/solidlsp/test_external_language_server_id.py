"""tests for the identity of a language server registered from outside SolidLSP."""

from pathlib import Path
from unittest.mock import Mock, patch

from solidlsp.language_servers.pyright_server import PyrightServer
from solidlsp.ls_config import ExternalLanguageServerId, FilenameMatcher, LanguageServerConfig
from solidlsp.settings import SolidLSPSettings


def test_an_external_language_server_id_keeps_its_key_matcher_and_implementation() -> None:
    matcher = FilenameMatcher(".ext")

    ls_id = ExternalLanguageServerId("external", matcher, PyrightServer)

    assert ls_id.get_key() == "external"
    assert ls_id.get_source_fn_matcher() is matcher
    assert ls_id.get_ls_class() is PyrightServer


def test_a_server_is_built_for_an_external_language_server_id(tmp_path: Path) -> None:
    """The real constructor builds a server for an external id, whose process is never started."""
    ls_id = ExternalLanguageServerId("external", FilenameMatcher(".ext"), PyrightServer)
    settings = SolidLSPSettings(solidlsp_dir=str(tmp_path / "global"), project_data_path=str(tmp_path / "project"))

    with patch.object(PyrightServer, "_create_language_server_interface", Mock(return_value=Mock())):
        server = ls_id.get_ls_class()(LanguageServerConfig(ls_id=ls_id), str(tmp_path), settings)

    assert server.ls_id is ls_id
    assert server.is_ignored_path("module.py")  # not a .ext file
