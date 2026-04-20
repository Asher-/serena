"""Structural backend registry: maps languages and file extensions to :class:`StructuralLanguage` instances.

Callers that need a structural backend for a given file (Serena's cursor layer,
refactor tooling, upcoming LSP wrappers) go through :class:`StructuralBackendRegistry`
rather than importing each backend directly. The registry is the single place where
language -> backend bindings are declared, which keeps consumers from knowing about
every concrete backend and lets optional backends (libclang / swift bridge) stay
lazy until something asks for them.

Registry semantics:

* Each language is registered with a factory callable and a set of file extensions.
* The factory is invoked at most once per language; the returned instance is
  memoized and reused. Backends are trusted to be stateless over their inputs,
  so reuse is safe.
* :meth:`StructuralBackendRegistry.for_relative_path` looks up by the file's
  lowercase extension; :meth:`StructuralBackendRegistry.for_language` looks up
  by the normalized language key (``"python"``, ``"cpp"``, ``"markdown"``,
  ``"swift"``). Both return ``None`` when the language is unsupported so
  callers can fall back cleanly.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from solidlsp.structural.base import StructuralLanguage


class StructuralBackendRegistry:
    """Registry mapping language keys and file extensions to structural backends.

    Factories are stored lazily so that importing the registry does not force a
    concrete backend import (libclang / swift bridge) until the corresponding
    language is first requested.

    :ivar _factories: map from normalized language key to a zero-argument factory
        that returns a :class:`StructuralLanguage` instance.
    :ivar _instances: memoized backend instances keyed by language; populated on
        the first :meth:`for_language` call for that key.
    :ivar _language_by_extension: map from lowercase file extension (including
        the leading ``.``) to normalized language key.
    """

    def __init__(self) -> None:
        self._factories: dict[str, Callable[[], StructuralLanguage]] = {}
        self._instances: dict[str, StructuralLanguage] = {}
        self._language_by_extension: dict[str, str] = {}

    def register(
        self,
        language: str,
        extensions: Iterable[str],
        factory: Callable[[], StructuralLanguage],
    ) -> None:
        """Register ``factory`` as the structural backend constructor for ``language``.

        :param language: normalized language key (lowercase, e.g. ``"python"``).
        :param extensions: file extensions this backend should match. Each must
            start with ``.`` and is matched case-insensitively.
        :param factory: zero-argument callable returning a
            :class:`StructuralLanguage` instance. Invoked at most once per
            language; the result is memoized.
        """
        # validate inputs so misregistrations fail loudly at registration time
        if not language:
            raise ValueError("language key must be a non-empty string")
        language_key = language.lower()

        # record extension -> language mapping, normalized to lowercase with leading dot
        for ext in extensions:
            if not ext.startswith("."):
                raise ValueError(f"extension must start with '.': {ext!r}")
            self._language_by_extension[ext.lower()] = language_key

        # store the factory; any previously cached instance for this language is dropped
        self._factories[language_key] = factory
        self._instances.pop(language_key, None)

    def for_language(self, language: str) -> "StructuralLanguage | None":
        """Return the backend registered for ``language``, or ``None`` if unsupported.

        The backend is instantiated on first access and memoized for subsequent
        calls. Lookups are case-insensitive.

        :param language: language key to resolve.
        :return: the memoized backend or ``None`` when no factory is registered.
        """
        # normalize the key before lookup; unregistered languages return None
        key = language.lower()
        factory = self._factories.get(key)
        if factory is None:
            return None

        # instantiate on first access, then reuse
        instance = self._instances.get(key)
        if instance is None:
            instance = factory()
            self._instances[key] = instance
        return instance

    def for_relative_path(self, relative_path: str) -> "StructuralLanguage | None":
        """Return the backend matching the extension of ``relative_path``.

        :param relative_path: POSIX-style path relative to a project root. Only
            the suffix is used; the rest of the path is ignored.
        :return: the backend registered for the file's extension, or ``None``
            when the extension is not mapped to any language.
        """
        # extract the lowercase suffix and route through the language map
        suffix = PurePosixPath(relative_path).suffix.lower()
        if not suffix:
            return None
        language_key = self._language_by_extension.get(suffix)
        if language_key is None:
            return None
        return self.for_language(language_key)

    def registered_languages(self) -> frozenset[str]:
        """Return the set of normalized language keys currently registered.

        :return: immutable snapshot of registered language keys.
        """
        return frozenset(self._factories)


def _python_backend_factory() -> "StructuralLanguage":
    # lazy import so registry consumers do not pay Python-backend import cost until needed
    from solidlsp.structural.backends.python import PythonStructuralLanguage

    return PythonStructuralLanguage()


def _cpp_backend_factory() -> "StructuralLanguage":
    # lazy import: the libclang-backed C++ backend only imports when requested
    from solidlsp.structural.backends.cpp import CppStructuralLanguage

    return CppStructuralLanguage()


def _markdown_backend_factory() -> "StructuralLanguage":
    # lazy import: markdown-it-py is only loaded when the markdown backend is requested
    from solidlsp.structural.backends.markdown import MarkdownStructuralLanguage

    return MarkdownStructuralLanguage()


def _json_backend_factory() -> "StructuralLanguage":
    # lazy import: the hand-rolled JSON CST parser only loads when the backend is requested
    from solidlsp.structural.backends.json import JsonStructuralLanguage

    return JsonStructuralLanguage()


def _swift_backend_factory() -> "StructuralLanguage":
    # lazy import: the swift bridge subprocess is only spun up when requested
    from solidlsp.structural.backends.swift import SwiftStructuralLanguage

    return SwiftStructuralLanguage()


def default_structural_backend_registry() -> StructuralBackendRegistry:
    """Build the default registry pre-populated with Serena's five structural backends.

    The default registry covers the languages that currently have a
    :class:`StructuralLanguage` implementation: Python, C++, Markdown, Swift
    and JSON. Backends are registered with lazy factories so simply
    constructing the registry imposes no import cost beyond this module
    itself.

    :return: a fresh :class:`StructuralBackendRegistry` with defaults registered.
    """
    registry = StructuralBackendRegistry()

    # Python: libcst-backed backend; the only one with a widened walk_nodes today.
    registry.register("python", [".py", ".pyi"], _python_backend_factory)

    # C++: libclang range-anchored backend; walk_nodes still defaults to walk_symbols.
    registry.register(
        "cpp",
        [".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".hxx"],
        _cpp_backend_factory,
    )

    # Markdown: markdown-it-py range-anchored backend.
    registry.register("markdown", [".md", ".markdown"], _markdown_backend_factory)

    # Swift: SwiftSyntax via subprocess bridge; kept lazy because it spawns a process on first use.
    registry.register("swift", [".swift"], _swift_backend_factory)

    # JSON: in-house round-trip CST backend; no LSP component, structural-only.
    registry.register("json", [".json"], _json_backend_factory)

    return registry


__all__ = [
    "StructuralBackendRegistry",
    "default_structural_backend_registry",
]
