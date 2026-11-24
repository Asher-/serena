"""
The stop of a language server that runs companion servers writes each companion's unsaved symbols to that companion's
cache before the companion's stop drops them.

Deliberately not marked with a language: no language server is started. Each server is built without its ``__init__``,
which would install and launch it, and given a companion that keeps its symbols in memory until a stop drops them.
"""

import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from solidlsp.language_servers.angular_language_server import AngularLanguageServer
from solidlsp.language_servers.astro_language_server import AstroLanguageServer
from solidlsp.language_servers.svelte_language_server import SvelteLanguageServer
from solidlsp.language_servers.vue_language_server import VueLanguageServer
from solidlsp.ls import SolidLanguageServer


class _Companion:
    """
    A companion server whose symbols live in memory until its stop drops them, and which a save writes to its cache file.
    """

    def __init__(self, cache_file: Path, save_failure: Exception | None = None) -> None:
        """
        :param cache_file: the file a save writes the companion's symbols to
        :param save_failure: the exception a save raises instead of writing, if any
        """
        self._cache_file = cache_file
        self._save_failure = save_failure
        self._symbols: list[str] | None = ["Component", "render"]
        self.stopped = False

    def save_cache(self) -> None:
        if self._save_failure is not None:
            raise self._save_failure
        if self._symbols is not None:
            self._cache_file.write_text("\n".join(self._symbols), encoding="utf-8")

    def stop(self, shutdown_timeout: float = 2.0) -> None:
        self._symbols = None
        self.stopped = True

    def release_retained_file(self, uri: str) -> None:
        pass


class _Process:
    """The stopped process of a server built without its ``__init__``."""

    def stop(self, timeout: float = 2.0) -> None:
        pass


def _bare_server(cls: type[SolidLanguageServer], companions: dict[str, _Companion], **fields: object) -> SolidLanguageServer:
    """
    :param cls: the language server class
    :param companions: the companions, by the attribute that holds each
    :param fields: the further state the class's stop reads
    :return: a server of the class, built without its ``__init__``, holding the companions and no symbols of its own
    """
    server = object.__new__(cls)
    server.server = _Process()
    server.server_started = True
    server._raw_document_symbols_cache = {}
    server._raw_document_symbols_cache_is_modified = False
    server._document_symbols_cache = {}
    server._document_symbols_cache_is_modified = False
    for name, value in {**companions, **fields}.items():
        setattr(server, name, value)
    return server


def _astro(companions: dict[str, _Companion]) -> SolidLanguageServer:
    return _bare_server(AstroLanguageServer, companions, _ts_server_started=True, _indexed_astro_file_uris=["file:///page.astro"])


def _svelte(companions: dict[str, _Companion]) -> SolidLanguageServer:
    return _bare_server(SvelteLanguageServer, companions, _ts_server_started=True, _indexed_svelte_file_uris=["file:///App.svelte"])


def _vue(companions: dict[str, _Companion]) -> SolidLanguageServer:
    return _bare_server(
        VueLanguageServer,
        companions,
        _ts_server_started=True,
        _indexed_vue_file_uris=["file:///App.vue"],
        _ls_operational_lock=threading.Lock(),
        _ls_operational_ready_event=threading.Event(),
        _ls_operational_thread=None,
    )


def _angular(companions: dict[str, _Companion]) -> SolidLanguageServer:
    return _bare_server(AngularLanguageServer, companions, _ts_server_started=True, _html_server_started=True)


_FAMILIES = pytest.mark.parametrize(
    ("build", "companion_attributes"),
    [
        (_astro, ["_ts_server"]),
        (_svelte, ["_ts_server"]),
        (_vue, ["_ts_server"]),
        (_angular, ["_ts_server", "_html_server"]),
    ],
    ids=["astro", "svelte", "vue", "angular"],
)


@_FAMILIES
def test_a_stop_stops_each_companion_whose_save_fails(
    tmp_path: Path, build: Callable[[dict[str, _Companion]], SolidLanguageServer], companion_attributes: list[str]
) -> None:
    companions = {
        attribute: _Companion(tmp_path / f"{attribute}.cache", save_failure=OSError("disk full")) for attribute in companion_attributes
    }
    server = build(companions)

    server.stop()

    assert all(companion.stopped for companion in companions.values())


@_FAMILIES
def test_a_stop_writes_the_unsaved_symbols_of_each_companion_to_its_cache(
    tmp_path: Path, build: Callable[[dict[str, _Companion]], SolidLanguageServer], companion_attributes: list[str]
) -> None:
    cache_files = {attribute: tmp_path / f"{attribute}.cache" for attribute in companion_attributes}
    server = build({attribute: _Companion(cache_file) for attribute, cache_file in cache_files.items()})

    server.stop()

    assert {attribute: cache_file.read_text(encoding="utf-8") for attribute, cache_file in cache_files.items()} == dict.fromkeys(
        companion_attributes, "Component\nrender"
    )
