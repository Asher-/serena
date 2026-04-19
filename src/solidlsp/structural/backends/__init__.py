"""Concrete :class:`~solidlsp.structural.base.StructuralLanguage` backends.

Each submodule implements the structural surface for one target language using
that language's own native AST toolkit (libcst for Python, libclang for C++,
SwiftSyntax for Swift). Tier-1 backends must round-trip byte-identically; the
harness at :mod:`test.solidlsp.structural.harness` verifies this over each
backend's corpus.
"""
