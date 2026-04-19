"""
Pattern matching and AST-level rewriting protocol.

The agent rewrites code by describing a *pattern* (a code fragment with
placeholders) and a *replacement* (a code fragment that may reference the
placeholders' captures). The backend parses both as native AST fragments in
the target language, matches the pattern against the subtree under a cursor,
and substitutes.

Placeholder syntax is language-specific \u2014 backends choose a sigil that is
unambiguous in their grammar. The common conventions are:

    ``$name``       a single-node capture bound to ``name``.
    ``$*name``      a sequence capture (zero or more adjacent nodes).
    ``$_``          anonymous single-node wildcard.

No regex. No line numbers. No string-level matching.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol


class AstPattern(Protocol):
    """An opaque, backend-owned compiled pattern.

    Obtained from :meth:`~solidlsp.structural.base.StructuralLanguage.compile_pattern`.
    The agent never constructs one directly; it hands Serena a source string,
    and Serena hands it back to the backend for compilation.

    Implementations may be libcst matchers, libclang matcher predicates,
    SwiftSyntax visitor closures, etc. \u2014 whatever the language's own AST
    toolkit uses. The shared contract is only that the backend can feed one
    to :meth:`~solidlsp.structural.base.StructuralLanguage.find_matches`.
    """


@dataclass(frozen=True)
class PatternMatch:
    """One hit produced by matching a pattern against an AST.

    :ivar node: the backend's opaque AST node at the matched location. The
        same opaque type the backend uses everywhere else; Serena never
        inspects its internals. Used as the substitution target when a
        rewrite is applied.
    :ivar bindings: placeholder name \u2192 opaque captured node (or tuple of
        nodes for sequence captures). The backend consults these when
        rendering the replacement.
    :ivar symbol_path: the name-path of the smallest enclosing named symbol,
        for agent-facing diagnostics (``"MyClass/my_method"``). ``None`` if
        the match sits outside any named symbol (e.g. at module top level).
    """

    node: Any
    bindings: Mapping[str, Any]
    symbol_path: str | None
