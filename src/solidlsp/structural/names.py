"""
Logical-name resolution for the structural-language layer.

The agent never sees file paths. It refers to sources by logical names that
each language interprets in its own terms:

    Python     ``myproject.auth.tokens``                 (dotted package path)
    C++        ``myproject::auth::tokens``               (namespace path)
    Swift      ``MyProject/Auth/Tokens``                 (module/file path)

The backend's :class:`LogicalNameResolver` maps logical names to
:class:`NameResolution` results that Serena uses to read or create the
underlying source file. The agent never touches these paths directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from solidlsp.structural.kinds import KindName


@dataclass(frozen=True)
class LogicalName:
    """A language-interpreted, filesystem-neutral identifier for a source.

    :ivar parts: the name's components in their native order (``("myproject",
        "auth", "tokens")``). Languages define how parts compose into a
        physical path.
    :ivar raw: the original string form the agent supplied (``"myproject.auth.
        tokens"``), preserved for diagnostics.
    """

    parts: tuple[str, ...]
    raw: str


@dataclass(frozen=True)
class NameResolution:
    """The outcome of resolving a :class:`LogicalName` to a physical location.

    :ivar relative_path: path, relative to the project root, where the source
        lives (or should live, for a not-yet-created source).
    :ivar source_kind: which :class:`~solidlsp.structural.kinds.KindName` the
        source root carries in the language's schema. Used by
        ``create_source`` to stamp the root of a new source with the correct
        kind.
    :ivar exists: ``True`` if a source already exists at ``relative_path``.
        ``create_source`` refuses to overwrite when ``True``; ``cursor_start``
        with a logical name requires ``True``.
    """

    relative_path: str
    source_kind: KindName
    exists: bool


class LogicalNameResolver(Protocol):
    """Resolves :class:`LogicalName` values to :class:`NameResolution` outcomes.

    Implementations hold language-specific conventions (where Python packages
    live, how Swift module paths map to files, etc.) and project-specific
    configuration (source roots, vendored directories). The resolver is
    pure: it never reads or writes source content \u2014 only paths.
    """

    def parse(self, raw: str) -> LogicalName:
        """Parse the agent-supplied string form into a :class:`LogicalName`.

        Raises :class:`~solidlsp.structural.errors.NameResolutionError` when
        the grammar does not match the language's convention.
        """
        ...

    def resolve(self, name: LogicalName) -> NameResolution:
        """Map a parsed :class:`LogicalName` to its :class:`NameResolution`.

        Raises :class:`~solidlsp.structural.errors.NameResolutionError` when
        the language has no convention that can place ``name``, or when the
        project's layout cannot accommodate it (e.g. no source root exists
        for the requested namespace).
        """
        ...
