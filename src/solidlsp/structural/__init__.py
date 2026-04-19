"""
Structural-language layer — per-language parser / serializer / kind-schema
backends that enable a purely symbolic MCP surface.

The agent never sees files, lines, or directories. Every operation is expressed
in terms of symbols (kinds defined by the language's :class:`KindSchema`), logical
names resolved by the language's :class:`LogicalNameResolver`, and mutations
performed by parse → mutate → serialize on the language's native AST through its
:class:`StructuralLanguage` implementation.

Round-trip faithfulness is the load-bearing contract: for every supported
language, ``serialize(parse(src)) == src`` byte-identically. Backends that cannot
meet that contract are not admitted to this layer.
"""

from solidlsp.structural.base import StructuralLanguage
from solidlsp.structural.errors import (
    ParseError,
    RoundTripViolation,
    PatternError,
    DeclarationError,
    NameResolutionError,
)
from solidlsp.structural.kinds import KindName, KindSpec, KindSchema
from solidlsp.structural.names import LogicalName, NameResolution, LogicalNameResolver
from solidlsp.structural.patterns import AstPattern, PatternMatch

__all__ = [
    "StructuralLanguage",
    "ParseError",
    "RoundTripViolation",
    "PatternError",
    "DeclarationError",
    "NameResolutionError",
    "KindName",
    "KindSpec",
    "KindSchema",
    "LogicalName",
    "NameResolution",
    "LogicalNameResolver",
    "AstPattern",
    "PatternMatch",
]
