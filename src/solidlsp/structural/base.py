"""
The :class:`StructuralLanguage` ABC \u2014 the operation surface every
structural-language backend must implement.

The ABC defines what Serena's symbolic tools can ask of a language. Each
backend (``PythonStructuralLanguage``, ``CppStructuralLanguage``,
``SwiftStructuralLanguage``) maps these operations onto its native toolkit:
libcst, libclang, SwiftSyntax, and so on.

Design premises encoded in the ABC
----------------------------------

* **Per-language opaque AST.** ``parse`` returns an opaque handle the backend
  owns; every downstream operation takes the handle back. Serena never walks
  the AST \u2014 only the backend does.

* **Byte-identical round-trip.** ``serialize(parse(src)) == src`` for every
  source the backend accepts. This is the single contract enforced by the
  test harness; a backend that fails it is not admissible.

* **Validation at the schema, not at the parser.** ``declare`` calls go
  through :meth:`~solidlsp.structural.kinds.KindSchema.validate_composition`
  *before* the backend is asked to build a node, so bad compositions are
  rejected with a typed :class:`~solidlsp.structural.errors.DeclarationError`
  rather than a parser error.

* **No fallback to text.** There is no "raw edit" method on this ABC. The
  agent can only mutate through ``declare`` and ``rewrite``. If a file cannot
  be parsed, the agent cannot mutate it structurally; its only recourse is
  the inspect-only fallback cursor defined in :mod:`serena` (outside this
  layer).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from typing import Any

from solidlsp.structural.kinds import KindName, KindSchema
from solidlsp.structural.names import LogicalNameResolver
from solidlsp.structural.patterns import AstPattern, PatternMatch


class StructuralLanguage(ABC):
    """Operation surface for one language's structural backend.

    All methods operate on opaque AST handles owned by the concrete backend.
    Serena treats these handles as values: it does not inspect their
    internals, copy them, or rely on any attribute beyond what this ABC
    exposes.
    """

    # ---- identity ----------------------------------------------------------

    @property
    @abstractmethod
    def language_key(self) -> str:
        """The :class:`solidlsp.ls_config.Language` value identifying this
        backend. Used for error messages, the kind-schema's ``language_key``,
        and routing within Serena.
        """

    @property
    @abstractmethod
    def kind_schema(self) -> KindSchema:
        """The symbol-kind vocabulary this backend exposes."""

    @property
    @abstractmethod
    def name_resolver(self) -> LogicalNameResolver:
        """The backend's logical-name resolver."""

    # ---- parse / serialize -------------------------------------------------

    @abstractmethod
    def parse(self, source: str) -> Any:
        """Parse ``source`` into a backend-owned opaque AST handle.

        Raises :class:`~solidlsp.structural.errors.ParseError` on failure. The
        error's ``source_preview`` should excerpt near the offending region,
        never the full source.
        """

    @abstractmethod
    def serialize(self, tree: Any) -> str:
        """Render an AST handle back to source text.

        Invariant: ``serialize(parse(s)) == s`` for any ``s`` this backend
        accepts. The round-trip harness verifies this on a language corpus;
        any deviation fails the harness with
        :class:`~solidlsp.structural.errors.RoundTripViolation`.
        """

    # ---- symbol-tree introspection ----------------------------------------

    @abstractmethod
    def root_kind(self, tree: Any) -> KindName:
        """Return the kind of the root of ``tree`` (e.g. ``"module"`` for
        Python sources).
        """

    @abstractmethod
    def walk_symbols(self, tree: Any) -> Iterable[tuple[str, KindName, Any]]:
        """Yield ``(name_path, kind, node)`` for every named symbol in ``tree``.

        Name paths use the ``Parent/Child`` convention Serena already uses in
        its cursor tools. Backends decide which AST nodes qualify as named
        symbols; the kind must exist in :attr:`kind_schema`.
        """

    # ---- declaration -------------------------------------------------------

    @abstractmethod
    def build_declaration(self, kind: KindName, attributes: Mapping[str, Any], children: Iterable[Any]) -> Any:
        """Construct an opaque AST node for a symbol of ``kind``.

        ``attributes`` must satisfy the :class:`~solidlsp.structural.kinds.KindSpec`
        for ``kind``; backends are trusted to re-validate but the schema's
        ``validate_composition`` has already confirmed placement.

        ``children`` are opaque nodes produced by earlier ``build_declaration``
        calls, letting the agent assemble a tree bottom-up.
        """

    @abstractmethod
    def insert_child(self, parent: Any, child: Any, anchor: Any | None = None, position: str = "end") -> Any:
        """Return a copy of the ``parent`` subtree with ``child`` inserted.

        :param anchor: an existing child of ``parent`` to position relative
            to; ``None`` means position at the extreme indicated by
            ``position``.
        :param position: one of ``"before"``, ``"after"``, ``"start"``,
            ``"end"``. The first two require ``anchor``.

        AST handles are treated as values: the returned parent is a new handle
        and the caller's old handle remains unchanged.
        """

    @abstractmethod
    def remove_child(self, parent: Any, child: Any) -> Any:
        """Return a copy of ``parent`` with ``child`` removed.

        Undoes what ``insert_child`` produced. Raises ``ValueError`` if
        ``child`` is not found under ``parent``.
        """

    # ---- pattern matching & rewriting --------------------------------------

    @abstractmethod
    def compile_pattern(self, pattern_source: str) -> AstPattern:
        """Compile agent-supplied pattern source into a backend pattern.

        The pattern grammar is the target language's own surface syntax,
        extended with capture sigils documented in
        :mod:`~solidlsp.structural.patterns`.

        Raises :class:`~solidlsp.structural.errors.PatternError` with
        ``reason="parse"`` on malformed patterns.
        """

    @abstractmethod
    def find_matches(self, tree: Any, pattern: AstPattern, scope: Any | None = None) -> Iterable[PatternMatch]:
        """Yield matches of ``pattern`` within ``tree``.

        ``scope`` is an opaque node inside ``tree`` limiting the search to
        that subtree; ``None`` means search the whole tree.
        """

    @abstractmethod
    def render_replacement(self, replacement_source: str, bindings: Mapping[str, Any]) -> Any:
        """Parse a replacement fragment using captured bindings.

        Capture references in the replacement use the same sigils as patterns.
        Returns an opaque node suitable for use with
        :meth:`apply_replacement`.

        Raises :class:`~solidlsp.structural.errors.PatternError` with
        ``reason="parse"`` on malformed replacements, or an appropriate error
        when a referenced capture is absent from ``bindings``.
        """

    @abstractmethod
    def apply_replacement(self, tree: Any, match: PatternMatch, replacement: Any) -> Any:
        """Return a copy of ``tree`` with ``match.node`` replaced by
        ``replacement``.

        The returned tree must round-trip identically to ``serialize(tree)``
        except for the replaced region.
        """

    # ---- new-source construction ------------------------------------------

    @abstractmethod
    def empty_source(self, source_kind: KindName) -> Any:
        """Return an opaque AST handle representing an empty source of
        ``source_kind``. Used by ``create_source`` as the seed that subsequent
        ``insert_child`` calls populate.
        """
