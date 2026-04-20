"""
Per-language symbol-kind schema.

Each structural-language backend publishes a :class:`KindSchema` that enumerates
the symbol kinds it exposes to the agent, and the composition rules governing
which kinds may contain which. The agent's declaration surface is driven by
this schema: ``declare(cursor, kind, attributes)`` is validated against the
schema before any mutation is attempted.

Kinds are deliberately language-specific. Python's ``decorator`` is not
JavaScript's ``decorator``; C++'s ``template`` has no Python analogue. A shared
vocabulary would be lossy, so each language names its own.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

# a string identifying one kind within one language's vocabulary; the grammar
# is ``<lower-snake-case>``. Uniqueness is only required within one language.
KindName = str


@dataclass(frozen=True)
class AttributeSpec:
    """Declares one attribute that a symbol of a given kind carries.

    :ivar name: attribute name as it appears in declaration payloads.
    :ivar type_hint: python-style type expression used for documentation and
        validation (``"str"``, ``"list[str]"``, ``"int | None"``). The backend
        is responsible for validating values against this hint.
    :ivar required: whether the attribute must appear in every declaration of
        the kind. Missing required attributes trigger
        :class:`~solidlsp.structural.errors.DeclarationError`.
    :ivar description: human-facing documentation of the attribute's meaning.
    """

    name: str
    type_hint: str
    required: bool
    description: str


@dataclass(frozen=True)
class KindSpec:
    """Specification of one symbol kind within a language.

    :ivar name: the kind's :class:`KindName`.
    :ivar description: human-facing one-paragraph description of what symbols
        of this kind represent in the source language.
    :ivar attributes: the attribute schema a declaration must satisfy.
    :ivar allowed_parent_kinds: the set of kind names that may contain a
        symbol of this kind. An empty set means the kind is source-root only
        (no parent \u2014 it is itself a source). Used to reject declarations
        placed under the wrong parent.
    :ivar allowed_child_kinds: the set of kind names that may appear as
        children of a symbol of this kind. Empty means a leaf.
    """

    name: KindName
    description: str
    attributes: tuple[AttributeSpec, ...]
    allowed_parent_kinds: frozenset[KindName]
    allowed_child_kinds: frozenset[KindName]


@dataclass(frozen=True)
class KindSchema:
    """The full kind vocabulary exposed by one structural-language backend.

    :ivar language_key: the :class:`solidlsp.ls_config.Language` value whose
        backend publishes this schema.
    :ivar source_kinds: kind names that may be the root of a source (i.e.
        top-level in a new file). For Python there is typically one
        (``"module"``); for some languages several are valid (e.g. TypeScript
        modules, declaration files).
    :ivar kinds: mapping from kind name to :class:`KindSpec`. Every kind that
        the agent may reference in declarations or pattern matches appears
        here.
    """

    language_key: str
    source_kinds: frozenset[KindName]
    kinds: Mapping[KindName, KindSpec] = field(default_factory=dict)

    def get(self, kind: KindName) -> KindSpec:
        """Return the :class:`KindSpec` for ``kind`` or raise ``KeyError``."""
        # direct dictionary lookup with a clearer message on miss
        try:
            return self.kinds[kind]
        except KeyError:
            raise KeyError(f"{self.language_key} has no kind {kind!r}; known kinds: {sorted(self.kinds)}") from None

    def validate_composition(self, parent_kind: KindName | None, child_kind: KindName) -> None:
        """Verify ``child_kind`` may appear under ``parent_kind``.

        Raises :class:`~solidlsp.structural.errors.DeclarationError` if the
        composition is forbidden by this schema. ``parent_kind`` is ``None``
        when the child is being declared at source root \u2014 in that case
        the child must be a source kind.
        """
        # delegate per-kind lookup
        child = self.get(child_kind)

        # source-root placement check
        if parent_kind is None:
            if child_kind not in self.source_kinds:
                from solidlsp.structural.errors import DeclarationError

                raise DeclarationError(
                    child_kind, f"cannot appear at source root in {self.language_key}; source kinds are {sorted(self.source_kinds)}"
                )
            return

        # nested placement check
        parent = self.get(parent_kind)
        if child_kind not in parent.allowed_child_kinds:
            from solidlsp.structural.errors import DeclarationError

            raise DeclarationError(
                child_kind,
                f"not permitted under kind {parent_kind!r} in {self.language_key}; allowed children: {sorted(parent.allowed_child_kinds)}",
            )
        if parent_kind not in child.allowed_parent_kinds:
            from solidlsp.structural.errors import DeclarationError

            raise DeclarationError(
                child_kind,
                f"does not permit parent {parent_kind!r} in {self.language_key}; allowed parents: {sorted(child.allowed_parent_kinds)}",
            )
