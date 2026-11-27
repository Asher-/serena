"""
The stop of a real Svelte language server writes the symbols its companion TypeScript server served, which the companion
holds in memory until then, to the companion's cache.
"""

import os
import shutil
from pathlib import Path

import pytest

from serena.constants import SERENA_MANAGED_DIR_NAME
from solidlsp.ls_config import LanguageServerId
from test.conftest import get_repo_path, language_server_tests_enabled, saved_root_symbol_names, start_ls_context

pytestmark = [
    pytest.mark.svelte,
    pytest.mark.skipif(
        not language_server_tests_enabled(LanguageServerId.SVELTE), reason="the Svelte language server is not installed in this environment"
    ),
]

# a plain .ts module, whose symbols the companion TypeScript server serves
GAME_TS = os.path.join("src", "lib", "game.ts")


def test_a_stop_writes_the_symbols_the_companion_served_to_the_companion_cache(tmp_path: Path) -> None:
    # a copy of the fixture repository of this run only, whose companion cache no other run writes
    repo = tmp_path / "test_repo"
    shutil.copytree(get_repo_path(LanguageServerId.SVELTE), repo, symlinks=True, ignore=shutil.ignore_patterns(SERENA_MANAGED_DIR_NAME))

    with start_ls_context(LanguageServerId.SVELTE, repo_path=str(repo)) as language_server:
        served = [symbol["name"] for symbol in language_server.request_document_symbols(GAME_TS).root_symbols]
        assert "GAME_VERSION" in served
        companion = language_server._ts_server
        assert companion is not None
        assert saved_root_symbol_names(companion, GAME_TS) is None

    assert "GAME_VERSION" in (saved_root_symbol_names(companion, GAME_TS) or [])
