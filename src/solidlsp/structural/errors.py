"""
Exceptions raised by the structural-language layer.
"""


class StructuralError(Exception):
    """Base class for all structural-layer errors."""


class ParseError(StructuralError):
    """Source text could not be parsed by the language's backend.

    :ivar language_key: the :class:`solidlsp.ls_config.Language` value of the
        backend that failed to parse.
    :ivar source_preview: a short excerpt from the offending source, suitable
        for diagnostics. Never the full source.
    :ivar detail: parser-supplied message (e.g. libcst ParseError reason).
    """

    def __init__(self, language_key: str, source_preview: str, detail: str) -> None:
        super().__init__(f"{language_key} parse failed: {detail}")
        self.language_key = language_key
        self.source_preview = source_preview
        self.detail = detail


class RoundTripViolation(StructuralError):
    """serialize(parse(src)) did not equal src byte-for-byte.

    Raised by the round-trip test harness. A violation means the backend is
    not admissible to the structural layer: any mutation on such a language
    could silently corrupt untouched code.

    :ivar language_key: the language whose backend violated the contract.
    :ivar diff_summary: a bounded diff summary for diagnostics.
    """

    def __init__(self, language_key: str, diff_summary: str) -> None:
        super().__init__(f"{language_key} round-trip violated: {diff_summary}")
        self.language_key = language_key
        self.diff_summary = diff_summary


class PatternError(StructuralError):
    """A rewrite pattern could not be parsed or does not match any node.

    :ivar reason: which phase failed \u2014 ``"parse"`` for a malformed pattern
        string, ``"no-match"`` when the pattern compiled but matched nothing,
        ``"ambiguous"`` when the pattern matched more than allowed.
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"pattern {reason}: {detail}")
        self.reason = reason
        self.detail = detail


class DeclarationError(StructuralError):
    """A declaration was rejected by the language's kind schema.

    :ivar kind: the attempted symbol kind.
    :ivar detail: why the declaration is not valid \u2014 e.g. disallowed parent
        kind, missing required attribute, attribute type mismatch.
    """

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(f"declaration of kind {kind!r} rejected: {detail}")
        self.kind = kind
        self.detail = detail


class NameResolutionError(StructuralError):
    """A logical name could not be resolved to a source location.

    :ivar logical_name: the name that could not be resolved.
    :ivar detail: why resolution failed \u2014 unknown language convention,
        collision with existing source, invalid name grammar.
    """

    def __init__(self, logical_name: str, detail: str) -> None:
        super().__init__(f"cannot resolve {logical_name!r}: {detail}")
        self.logical_name = logical_name
        self.detail = detail


class NodeRenderError(StructuralError):
    """A walked node could not be rendered to standalone source text.

    Raised by :meth:`solidlsp.structural.base.StructuralLanguage.render_node_source`
    when a backend has no faithful way to emit a node's value as source. The
    cursor surface catches this and omits the body rather than leaking an
    internal repr across the agent boundary.
    """
