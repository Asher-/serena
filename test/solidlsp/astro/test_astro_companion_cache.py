"""
The stop of a real Astro language server writes the symbols its companion TypeScript server served, which the companion
holds in memory until then, to the companion's cache.
"""

import os

import pytest

from serena.constants import SERENA_MANAGED_DIR_NAME
from solidlsp.ls import SolidLanguageServer
from solidlsp.ls_config import LanguageServerId
from test.conftest import get_repo_path, start_ls_context

pytestmark = pytest.mark.astro

# a plain .ts module, whose symbols the companion TypeScript server serves
FORMAT_TS = os.path.join("src", "utils", "format.ts")


def test_a_stop_writes_the_symbols_the_companion_served_to_the_companion_cache() -> None:
    cache_file = (
        get_repo_path(LanguageServerId.ASTRO)
        / SERENA_MANAGED_DIR_NAME
        / SolidLanguageServer.CACHE_FOLDER_NAME
        / LanguageServerId.TYPESCRIPT.get_key()
        / SolidLanguageServer.DOCUMENT_SYMBOL_CACHE_FILENAME
    )
    cache_file.unlink(missing_ok=True)

    with start_ls_context(LanguageServerId.ASTRO) as language_server:
        assert language_server.request_document_symbols(FORMAT_TS).root_symbols

    assert cache_file.is_file()
