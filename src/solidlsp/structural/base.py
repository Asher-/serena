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

from solidlsp.structural.errors import NodeRenderError
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

    def render_node_source(self, node: Any) -> str:
        """Render a single walked ``node`` back to standalone source text.

        Unlike :meth:`serialize` (which renders a whole tree handle), this
        renders one node yielded by :meth:`walk_nodes` / :meth:`walk_symbols`
        so the cursor surface can show a node's body. Rendering is the
        backend's responsibility: a leaf emits its VALUE, never an internal
        repr.

        The default handles code-language backends whose nodes expose a
        faithful text hook (libcst ``.code``, tomlkit ``.as_string()``);
        structural backends (yaml / json / toml) override it. A node with no
        faithful source form raises
        :class:`~solidlsp.structural.errors.NodeRenderError` rather than
        falling back to ``str(node)``.
        """
        code_attr = getattr(node, "code", None)
        if isinstance(code_attr, str):
            return code_attr
        as_string = getattr(node, "as_string", None)
        if callable(as_string):
            try:
                result = as_string()
            except TypeError:
                result = None
            if isinstance(result, str):
                return result
        raise NodeRenderError(
            f"{type(self).__name__} cannot render {type(node).__name__} as source",
        )

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

    def walk_nodes(self, tree: Any) -> Iterable[tuple[str, KindName, Any]]:
        """Yield ``(name_path, kind, node)`` for every addressable AST node in ``tree``.

        Superset of :meth:`walk_symbols`. Backends may override this method to
        expose unnamed syntactic constructs — decorators, match / with / try /
        for / while / if statements, top-level expressions — using synthetic
        name paths of the form ``parent/<kind>#<index>`` for unnamed nodes.

        The default implementation delegates to :meth:`walk_symbols`, so
        backends without AST-level addressability yield only named symbols
        and keep their pre-``walk_nodes`` behavior.
        """
        return self.walk_symbols(tree)

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

    # ---- container-member editing -----------------------------------------
    #
    # These methods operate on *collection-literal members* — dict entries,
    # list items, object members, array items, mapping pairs, sequence items.
    # They complement ``insert_child`` / ``remove_child``, which only act on
    # the body of named block-scoped constructs (modules, classes, functions).
    #
    # All three take the full ``tree`` plus a ``name_path`` as emitted by
    # :meth:`walk_nodes`, and return a fresh tree of the same type as
    # ``tree``. Implementations should re-parse after mutation so handles to
    # the old tree stay valid but stale — the caller is expected to discard
    # them.
    #
    # A backend that cannot represent container members as addressable nodes
    # (e.g. ``markdown``) raises :class:`NotImplementedError` from all three.

    def container_insert_member(
        self,
        tree: Any,
        anchor_or_container_path: str,
        source: str,
        position: str = "end",
    ) -> Any:
        """Insert a new member into a container inside ``tree``.

        :param anchor_or_container_path: when ``position`` is ``"start"`` or
            ``"end"``, this is the name-path of the *container* (dict/list/
            object/array/mapping/sequence). When ``position`` is ``"before"``
            or ``"after"``, this is the name-path of the *anchor member*
            within its parent container; the new member becomes the anchor's
            sibling.
        :param source: the new member's source text in the target language.
            For mapping-like containers (dict, object, mapping) this is a
            full key-and-value fragment, e.g. ``'"foo": 42'``. For
            sequence-like containers (list, array, sequence) it is just the
            value expression.
        :param position: one of ``"before"``, ``"after"``, ``"start"``,
            ``"end"``.
        :return: a new ``tree`` of the same type as the input.

        Default implementation raises :class:`NotImplementedError`; backends
        that support container-member editing override this.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support container-member insertion",
        )

    def container_remove_member(self, tree: Any, member_path: str) -> Any:
        """Remove the member at ``member_path`` from its container.

        :param member_path: name-path of the member (not the container).
        :return: a new ``tree`` of the same type as the input.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support container-member removal",
        )

    def container_replace_member(self, tree: Any, member_path: str, source: str) -> Any:
        """Replace the *value* of the member at ``member_path``.

        For mapping-like containers this replaces only the value half of the
        key/value pair — the key is preserved. For sequence-like containers
        the entire item is replaced (there is no separate key).

        :param member_path: name-path of the member.
        :param source: the new value's source text (not a full key/value
            pair, even for mapping-like containers).
        :return: a new ``tree`` of the same type as the input.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support container-member replacement",
        )

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
