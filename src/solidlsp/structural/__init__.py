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
    DeclarationError,
    NameResolutionError,
    ParseError,
    PatternError,
    RoundTripViolation,
)
from solidlsp.structural.kinds import KindName, KindSchema, KindSpec
from solidlsp.structural.names import LogicalName, LogicalNameResolver, NameResolution
from solidlsp.structural.patterns import AstPattern, PatternMatch

__all__ = [
    "AstPattern",
    "DeclarationError",
    "KindName",
    "KindSchema",
    "KindSpec",
    "LogicalName",
    "LogicalNameResolver",
    "NameResolution",
    "NameResolutionError",
    "ParseError",
    "PatternError",
    "PatternMatch",
    "RoundTripViolation",
    "StructuralLanguage",
]
