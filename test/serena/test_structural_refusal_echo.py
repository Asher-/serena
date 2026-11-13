"""
A refusal of a structural backend names the caller's container path, member path, key, segment or anchor exactly as sent,
and an edit the same backend accepts still lands.
"""

import dataclasses
from collections.abc import Callable
from pathlib import Path

import pytest
import tomlkit

from serena.cursor import CursorManager
from solidlsp.structural.backends.cpp import CppStructuralLanguage
from solidlsp.structural.backends.csharp import CSharpStructuralLanguage
from solidlsp.structural.backends.go import GoStructuralLanguage
from solidlsp.structural.backends.java import JavaStructuralLanguage
from solidlsp.structural.backends.json import JsonStructuralLanguage
from solidlsp.structural.backends.markdown import MarkdownStructuralLanguage
from solidlsp.structural.backends.python import PythonStructuralLanguage
from solidlsp.structural.backends.ruby import RubyStructuralLanguage
from solidlsp.structural.backends.rust import RustStructuralLanguage
from solidlsp.structural.backends.toml import TomlStructuralLanguage, _resolve_node_by_path_toml, _TomlTree
from solidlsp.structural.backends.typescript import TypeScriptStructuralLanguage
from solidlsp.structural.backends.yaml import YamlStructuralLanguage, _resolve_node_by_path_yaml
from solidlsp.structural.errors import StructuralError
from test.serena.test_cursor_container_edit import _make_manager_via_subclass
from test.serena.test_cursor_plaintext import _force_lsp_miss, _manager

_PYTHON = PythonStructuralLanguage()
_PYTHON_PREAMBLE = "import os\nclass C:\n    pass\nX = 1\n"
_PYTHON_TREE = _PYTHON.parse(_PYTHON_PREAMBLE + 'D = {"it\'s": 1, "n": {}}\nL = [*y, 1]\n')

_JSON = JsonStructuralLanguage()
_JSON_TREE = _JSON.parse('{"it\'s": 1, "a": [[1]], "o": {}}')

_YAML = YamlStructuralLanguage()
_YAML_SOURCE = "it's: 1\nlist:\n  - 1\nm:\n  a: 1\nseqs:\n  - k: 1\n"
_YAML_TREE = _YAML.parse(_YAML_SOURCE)
_YAML_SCALAR_TREE = _YAML.parse("5\n...\n")

_TOML = TomlStructuralLanguage()
_TOML_SOURCE = '"it\'s" = 1\nlist = [1]\n\n[m]\na = 1\n\n[[seqs]]\nk = 1\n'
_TOML_TREE = _TOML.parse(_TOML_SOURCE)
_TOML_SCALAR_TREE = _TomlTree(source="", data=tomlkit.integer(5))

_MARKDOWN = MarkdownStructuralLanguage()
_MARKDOWN_BODILESS_TREE = _MARKDOWN.parse("# It's\n")
_MARKDOWN_BODILESS_ANCHOR = next(ref for _, _, ref in _MARKDOWN.walk_symbols(_MARKDOWN_BODILESS_TREE))
_MARKDOWN_TREE = _MARKDOWN.parse("# It's\n\ntext\n")
_MARKDOWN_ANCHOR = next(ref for _, _, ref in _MARKDOWN.walk_symbols(_MARKDOWN_TREE))
_MARKDOWN_CHILD = _MARKDOWN.build_declaration("paragraph", {"content": "x"}, ())

_CPP = CppStructuralLanguage()
_CPP_BODILESS_TREE = _CPP.parse("int x = 1;\n")
_CPP_BODILESS_ANCHOR = next(ref for _, _, ref in _CPP.walk_symbols(_CPP_BODILESS_TREE))
_CPP_TREE = _CPP.parse("class A { int y; };\n")
_CPP_ANCHOR = next(ref for name, _, ref in _CPP.walk_symbols(_CPP_TREE) if name == "A")
_CPP_CHILD = _CPP.build_declaration("include", {"statement": "#include <map>"}, ())

_YAML_PAIR = _YAML.build_declaration("pair", {"key": "it's"}, (_YAML.build_declaration("scalar", {"value": 2}, ()),))
_TOML_PAIR = _TOML.build_declaration("pair", {"key": "it's"}, (_TOML.build_declaration("scalar", {"value": 2}, ()),))

_REFUSALS: list[tuple[str, Callable[[], object], str]] = [
    # keys of a container member
    ("yaml remove a missing key", lambda: _YAML.container_remove_member(_YAML_TREE, "m/it's"), "key 'it's' not found in mapping"),
    ("yaml replace a missing key", lambda: _YAML.container_replace_member(_YAML_TREE, "m/it's", "1"), "key 'it's' not found in mapping"),
    (
        "yaml insert beside a missing key",
        lambda: _YAML.container_insert_member(_YAML_TREE, "m/it's", "b: 1", position="before"),
        "anchor key 'it's' not found in mapping",
    ),
    ("toml remove a missing key", lambda: _TOML.container_remove_member(_TOML_TREE, "m/it's"), "key 'it's' not found in mapping"),
    ("toml replace a missing key", lambda: _TOML.container_replace_member(_TOML_TREE, "m/it's", "1"), "key 'it's' not found in mapping"),
    (
        "toml insert beside a missing key",
        lambda: _TOML.container_insert_member(_TOML_TREE, "m/it's", "b = 1", position="before"),
        "anchor key 'it's' not found in mapping",
    ),
    # duplicate keys of a declaration
    (
        "yaml mapping declaration with a duplicate key",
        lambda: _YAML.build_declaration("mapping", {}, (_YAML_PAIR, _YAML_PAIR)),
        "duplicate key 'it's' in mapping",
    ),
    ("yaml insert a declared duplicate key", lambda: _YAML.insert_child(_YAML_TREE, _YAML_PAIR), "duplicate key 'it's' in mapping"),
    (
        "toml table declaration with a duplicate key",
        lambda: _TOML.build_declaration("table", {}, (_TOML_PAIR, _TOML_PAIR)),
        "duplicate key 'it's' in table",
    ),
    ("toml insert a declared duplicate key", lambda: _TOML.insert_child(_TOML_TREE, _TOML_PAIR), "duplicate key 'it's' in mapping"),
    # logical names
    ("python logical name", lambda: _PYTHON.name_resolver.parse("a.it's"), "invalid Python identifier part: 'it's'"),
    ("json logical name", lambda: _JSON.name_resolver.parse("a.it's"), "invalid json name part: 'it's'"),
    ("yaml logical name", lambda: _YAML.name_resolver.parse("a.it's"), "invalid yaml name part: 'it's'"),
    ("toml logical name", lambda: _TOML.name_resolver.parse("a.it's"), "invalid toml name part: 'it's'"),
    ("markdown logical name", lambda: _MARKDOWN.name_resolver.parse("a.it's"), "invalid markdown name part: 'it's'"),
    ("cpp logical name", lambda: _CPP.name_resolver.parse("a::it's"), "invalid C++ identifier part: 'it's'"),
    # python
    (
        "python unbracketed member path",
        lambda: _PYTHON.container_remove_member(_PYTHON_TREE, "it's"),
        "member path 'it's' does not end in a bracketed segment",
    ),
    (
        "python member path without container",
        lambda: _PYTHON.container_remove_member(_PYTHON_TREE, '["it\'s"]'),
        "member path '[\"it's\"]' has no container-path prefix",
    ),
    (
        "python missing dict member",
        lambda: _PYTHON.container_remove_member(_PYTHON_TREE, 'D/["it\'s!"]'),
        "no dict member with segment '[\"it's!\"]'",
    ),
    (
        "python segment that is not a list index",
        lambda: _PYTHON.container_remove_member(_PYTHON_TREE, 'L/["it\'s"]'),
        "segment '[\"it's\"]' is not a list index",
    ),
    ("python list index out of range", lambda: _PYTHON.container_remove_member(_PYTHON_TREE, "L/[9]"), "list index 9 is out of range"),
    (
        "python unaddressable container path",
        lambda: _PYTHON.container_insert_member(_PYTHON_TREE, "it's", "1"),
        "no addressable node at path 'it's'",
    ),
    (
        "python class as container",
        lambda: _PYTHON.container_insert_member(_PYTHON_TREE, "C", "1"),
        "path 'C' resolves to a non-assignment 'class' node",
    ),
    (
        "python import as container",
        lambda: _PYTHON.container_insert_member(_PYTHON_TREE, "os", "1"),
        "path 'os' is not an assignment",
    ),
    (
        "python scalar binding as container",
        lambda: _PYTHON.container_insert_member(_PYTHON_TREE, "X", "1"),
        "path 'X' does not bind a dict or list literal",
    ),
    (
        "python scalar member as container",
        lambda: _PYTHON.container_insert_member(_PYTHON_TREE, 'D/["it\'s"]', '"k": 1'),
        "path 'D/[\"it's\"]' addresses a container member whose value is not itself a dict or list",
    ),
    # json
    (
        "json scalar member as container",
        lambda: _JSON.container_insert_member(_JSON_TREE, "it's", "2"),
        "path 'it's' does not resolve to a JSON object or array",
    ),
    ("json empty member path", lambda: _JSON.container_remove_member(_JSON_TREE, ""), "empty path does not resolve to a node"),
    (
        "json index segment on an object",
        lambda: _JSON.container_insert_member(_JSON_TREE, "[it's]", "2"),
        "segment '[it's]' expects an array, got 'object'",
    ),
    (
        "json malformed index segment",
        lambda: _JSON.container_insert_member(_JSON_TREE, "a/[it's]", "2"),
        "segment '[it's]' is not a valid array index",
    ),
    (
        "json index out of range",
        lambda: _JSON.container_insert_member(_JSON_TREE, "a/[9]", "2"),
        "array index 9 out of range for segment '[9]'",
    ),
    (
        "json key segment on an array",
        lambda: _JSON.container_insert_member(_JSON_TREE, "a/it's", "2"),
        "segment 'it's' expects an object, got 'array'",
    ),
    (
        "json missing key",
        lambda: _JSON.container_insert_member(_JSON_TREE, "it's!", "2"),
        "object has no member with key 'it's!'",
    ),
    # yaml
    ("yaml insert into a foreign tree", lambda: _YAML.container_insert_member(_TOML_TREE, "", "a: 1"), "tree must be _YamlTree"),
    ("yaml remove from a foreign tree", lambda: _YAML.container_remove_member(_TOML_TREE, "a"), "tree must be _YamlTree"),
    ("yaml replace in a foreign tree", lambda: _YAML.container_replace_member(_TOML_TREE, "a", "1"), "tree must be _YamlTree"),
    (
        "yaml unknown insert position",
        lambda: _YAML.container_insert_member(_YAML_TREE, "", "a: 1", position="middle"),
        "invalid position: 'middle'",
    ),
    (
        "yaml duplicate key",
        lambda: _YAML.container_insert_member(_YAML_TREE, "", '"it\'s": 2'),
        "duplicate key 'it's' in mapping",
    ),
    (
        "yaml insert into a scalar root",
        lambda: _YAML.container_insert_member(_YAML_SCALAR_TREE, "", "1"),
        "container at '' is not a mapping or sequence",
    ),
    (
        "yaml remove from a scalar root",
        lambda: _YAML.container_remove_member(_YAML_SCALAR_TREE, "it's"),
        "container at '' is not a mapping or sequence",
    ),
    (
        "yaml replace in a scalar root",
        lambda: _YAML.container_replace_member(_YAML_SCALAR_TREE, "it's", "1"),
        "container at '' is not a mapping or sequence",
    ),
    (
        "yaml unbracketed sequence anchor",
        lambda: _YAML.container_insert_member(_YAML_TREE, "list/it's", "0", position="before"),
        "sequence segment must be [N], got 'it's'",
    ),
    (
        "yaml unbracketed sequence member",
        lambda: _YAML.container_remove_member(_YAML_TREE, "list/it's"),
        "sequence segment must be [N], got 'it's'",
    ),
    (
        "yaml malformed sequence index",
        lambda: _YAML.container_replace_member(_YAML_TREE, "list/[it's]", "2"),
        "invalid sequence index in segment '[it's]'",
    ),
    ("yaml empty path", lambda: _resolve_node_by_path_yaml(_YAML_TREE.data, ""), "empty path does not resolve to a node"),
    (
        "yaml index segment on a mapping",
        lambda: _YAML.container_insert_member(_YAML_TREE, "[it's]", "2"),
        "segment '[it's]' expects a sequence, got CommentedMap",
    ),
    (
        "yaml index out of range",
        lambda: _YAML.container_insert_member(_YAML_TREE, "list/[9]", "2"),
        "sequence index 9 out of range for segment '[9]'",
    ),
    (
        "yaml key segment on a sequence",
        lambda: _YAML.container_insert_member(_YAML_TREE, "list/it's", "2"),
        "segment 'it's' expects a mapping, got CommentedSeq",
    ),
    ("yaml missing key", lambda: _YAML.container_insert_member(_YAML_TREE, "it's!", "2"), "mapping has no key 'it's!'"),
    (
        "yaml scalar as container",
        lambda: _YAML.container_insert_member(_YAML_TREE, "it's", "2"),
        "path 'it's' does not resolve to a YAML container",
    ),
    # toml
    ("toml insert into a foreign tree", lambda: _TOML.container_insert_member(_YAML_TREE, "", "a = 1"), "tree must be _TomlTree"),
    ("toml remove from a foreign tree", lambda: _TOML.container_remove_member(_YAML_TREE, "a"), "tree must be _TomlTree"),
    ("toml replace in a foreign tree", lambda: _TOML.container_replace_member(_YAML_TREE, "a", "1"), "tree must be _TomlTree"),
    (
        "toml unknown insert position",
        lambda: _TOML.container_insert_member(_TOML_TREE, "", "a = 1", position="middle"),
        "invalid position: 'middle'",
    ),
    (
        "toml duplicate key",
        lambda: _TOML.container_insert_member(_TOML_TREE, "", '"it\'s" = 2'),
        "duplicate key 'it's' in mapping",
    ),
    (
        "toml insert into a non-container root",
        lambda: _TOML.container_insert_member(_TOML_SCALAR_TREE, "", "1"),
        "container at '' is not a mapping or sequence",
    ),
    (
        "toml remove from a non-container root",
        lambda: _TOML.container_remove_member(_TOML_SCALAR_TREE, "it's"),
        "container at '' is not a mapping or sequence",
    ),
    (
        "toml replace in a non-container root",
        lambda: _TOML.container_replace_member(_TOML_SCALAR_TREE, "it's", "1"),
        "container at '' is not a mapping or sequence",
    ),
    (
        "toml unbracketed sequence anchor",
        lambda: _TOML.container_insert_member(_TOML_TREE, "list/it's", "0", position="before"),
        "sequence segment must be [N], got 'it's'",
    ),
    (
        "toml unbracketed sequence member",
        lambda: _TOML.container_remove_member(_TOML_TREE, "list/it's"),
        "sequence segment must be [N], got 'it's'",
    ),
    (
        "toml malformed sequence index",
        lambda: _TOML.container_replace_member(_TOML_TREE, "list/[it's]", "2"),
        "invalid sequence index in segment '[it's]'",
    ),
    ("toml empty path", lambda: _resolve_node_by_path_toml(_TOML_TREE.data, ""), "empty path does not resolve to a node"),
    (
        "toml index segment on a table",
        lambda: _TOML.container_insert_member(_TOML_TREE, "[it's]", "2"),
        "segment '[it's]' expects AoT/Array, got TOMLDocument",
    ),
    (
        "toml index out of range",
        lambda: _TOML.container_insert_member(_TOML_TREE, "list/[9]", "2"),
        "sequence index 9 out of range for segment '[9]'",
    ),
    (
        "toml key segment on an array",
        lambda: _TOML.container_insert_member(_TOML_TREE, "list/it's", "2"),
        "segment 'it's' expects a mapping, got Array",
    ),
    ("toml missing key", lambda: _TOML.container_insert_member(_TOML_TREE, "it's!", "2"), "mapping has no key 'it's!'"),
    (
        "toml scalar as container",
        lambda: _TOML.container_insert_member(_TOML_TREE, "it's", "2"),
        "path 'it's' does not resolve to a TOML container",
    ),
    # markdown
    (
        "markdown anchor of a foreign kind",
        lambda: _MARKDOWN.insert_child(_MARKDOWN_TREE, _MARKDOWN_CHILD, anchor=_CPP_ANCHOR, position="start"),
        "anchor must be _MdSymbolRef or None",
    ),
    (
        "markdown sibling position without an anchor",
        lambda: _MARKDOWN.insert_child(_MARKDOWN_TREE, _MARKDOWN_CHILD, anchor=None, position="before"),
        "position 'before' requires an anchor",
    ),
    (
        "markdown start inside a bodiless heading",
        lambda: _MARKDOWN.insert_child(_MARKDOWN_BODILESS_TREE, _MARKDOWN_CHILD, anchor=_MARKDOWN_BODILESS_ANCHOR, position="start"),
        "anchor 'It's' has no body scope to insert into",
    ),
    (
        "markdown end inside a bodiless heading",
        lambda: _MARKDOWN.insert_child(_MARKDOWN_BODILESS_TREE, _MARKDOWN_CHILD, anchor=_MARKDOWN_BODILESS_ANCHOR, position="end"),
        "anchor 'It's' has no body scope to insert into",
    ),
    # cpp
    (
        "cpp anchor of a foreign kind",
        lambda: _CPP.insert_child(_CPP_TREE, _CPP_CHILD, anchor=_MARKDOWN_ANCHOR, position="start"),
        "anchor must be _CppSymbolRef or None",
    ),
    (
        "cpp sibling position without an anchor",
        lambda: _CPP.insert_child(_CPP_TREE, _CPP_CHILD, anchor=None, position="before"),
        "position 'before' requires an anchor",
    ),
    (
        "cpp start inside a bodiless variable",
        lambda: _CPP.insert_child(_CPP_BODILESS_TREE, _CPP_CHILD, anchor=_CPP_BODILESS_ANCHOR, position="start"),
        "anchor 'x' has no body to insert into",
    ),
    (
        "cpp end inside a bodiless variable",
        lambda: _CPP.insert_child(_CPP_BODILESS_TREE, _CPP_CHILD, anchor=_CPP_BODILESS_ANCHOR, position="end"),
        "anchor 'x' has no body to insert into",
    ),
]

_ACCEPTED: list[tuple[str, Callable[[], str], str]] = [
    # python
    (
        "python remove a dict member",
        lambda: _PYTHON.serialize(_PYTHON.container_remove_member(_PYTHON_TREE, 'D/["it\'s"]')),
        _PYTHON_PREAMBLE + 'D = {"n": {}}\nL = [*y, 1]\n',
    ),
    (
        "python remove a list item past a starred element",
        lambda: _PYTHON.serialize(_PYTHON.container_remove_member(_PYTHON_TREE, "L/[0]")),
        _PYTHON_PREAMBLE + 'D = {"it\'s": 1, "n": {}}\nL = [*y]\n',
    ),
    (
        "python insert into a nested dict member",
        lambda: _PYTHON.serialize(_PYTHON.container_insert_member(_PYTHON_TREE, 'D/["n"]', '"k": 2')),
        _PYTHON_PREAMBLE + 'D = {"it\'s": 1, "n": {"k": 2}}\nL = [*y, 1]\n',
    ),
    (
        "python insert into an assigned list",
        lambda: _PYTHON.serialize(_PYTHON.container_insert_member(_PYTHON_TREE, "L", "2")),
        _PYTHON_PREAMBLE + 'D = {"it\'s": 1, "n": {}}\nL = [*y, 1, 2]\n',
    ),
    # json
    (
        "json insert into the root",
        lambda: _JSON.serialize(_JSON.container_insert_member(_JSON_TREE, "", '"k": 2')),
        '{"it\'s": 1, "a": [[1]], "o": {}, "k": 2}',
    ),
    (
        "json insert into an object member",
        lambda: _JSON.serialize(_JSON.container_insert_member(_JSON_TREE, "o", '"k": 2')),
        '{"it\'s": 1, "a": [[1]], "o": {"k": 2}}',
    ),
    (
        "json insert into an array item",
        lambda: _JSON.serialize(_JSON.container_insert_member(_JSON_TREE, "a/[0]", "2")),
        '{"it\'s": 1, "a": [[1, 2]], "o": {}}',
    ),
    # yaml
    (
        "yaml insert into a mapping",
        lambda: _YAML.serialize(_YAML.container_insert_member(_YAML_TREE, "m", "b: 2")),
        "it's: 1\nlist:\n  - 1\nm:\n  a: 1\n  b: 2\nseqs:\n  - k: 1\n",
    ),
    (
        "yaml insert before a sequence item",
        lambda: _YAML.serialize(_YAML.container_insert_member(_YAML_TREE, "list/[0]", "0", position="before")),
        "it's: 1\nlist:\n  - 0\n  - 1\nm:\n  a: 1\nseqs:\n  - k: 1\n",
    ),
    (
        "yaml insert into a mapping inside a sequence",
        lambda: _YAML.serialize(_YAML.container_insert_member(_YAML_TREE, "seqs/[0]", "c: 3")),
        "it's: 1\nlist:\n  - 1\nm:\n  a: 1\nseqs:\n  - k: 1\n    c: 3\n",
    ),
    (
        "yaml remove a mapping member",
        lambda: _YAML.serialize(_YAML.container_remove_member(_YAML_TREE, "m/a")),
        "it's: 1\nlist:\n  - 1\nm: {}\nseqs:\n  - k: 1\n",
    ),
    (
        "yaml remove a sequence item",
        lambda: _YAML.serialize(_YAML.container_remove_member(_YAML_TREE, "list/[0]")),
        "it's: 1\nlist: []\nm:\n  a: 1\nseqs:\n  - k: 1\n",
    ),
    (
        "yaml replace a mapping value",
        lambda: _YAML.serialize(_YAML.container_replace_member(_YAML_TREE, "m/a", "2")),
        "it's: 1\nlist:\n  - 1\nm:\n  a: 2\nseqs:\n  - k: 1\n",
    ),
    (
        "yaml replace a sequence item",
        lambda: _YAML.serialize(_YAML.container_replace_member(_YAML_TREE, "list/[0]", "2")),
        "it's: 1\nlist:\n  - 2\nm:\n  a: 1\nseqs:\n  - k: 1\n",
    ),
    # toml
    (
        "toml insert into a table",
        lambda: _TOML.serialize(_TOML.container_insert_member(_TOML_TREE, "m", "b = 2")),
        '"it\'s" = 1\nlist = [1]\n\n[m]\na = 1\n\nb = 2\n[[seqs]]\nk = 1\n',
    ),
    (
        "toml insert before an array item",
        lambda: _TOML.serialize(_TOML.container_insert_member(_TOML_TREE, "list/[0]", "0", position="before")),
        '"it\'s" = 1\nlist = [0,1]\n\n[m]\na = 1\n\n[[seqs]]\nk = 1\n',
    ),
    (
        "toml insert into a table of an array of tables",
        lambda: _TOML.serialize(_TOML.container_insert_member(_TOML_TREE, "seqs/[0]", "c = 3")),
        '"it\'s" = 1\nlist = [1]\n\n[m]\na = 1\n\n[[seqs]]\nk = 1\nc = 3\n',
    ),
    (
        "toml remove a table member",
        lambda: _TOML.serialize(_TOML.container_remove_member(_TOML_TREE, "m/a")),
        '"it\'s" = 1\nlist = [1]\n\n[m]\n\n[[seqs]]\nk = 1\n',
    ),
    (
        "toml remove an array item",
        lambda: _TOML.serialize(_TOML.container_remove_member(_TOML_TREE, "list/[0]")),
        '"it\'s" = 1\nlist = []\n\n[m]\na = 1\n\n[[seqs]]\nk = 1\n',
    ),
    (
        "toml replace a table value",
        lambda: _TOML.serialize(_TOML.container_replace_member(_TOML_TREE, "m/a", "2")),
        '"it\'s" = 1\nlist = [1]\n\n[m]\na = 2\n\n[[seqs]]\nk = 1\n',
    ),
    (
        "toml replace an array item",
        lambda: _TOML.serialize(_TOML.container_replace_member(_TOML_TREE, "list/[0]", "2")),
        '"it\'s" = 1\nlist = [2]\n\n[m]\na = 1\n\n[[seqs]]\nk = 1\n',
    ),
    # markdown
    (
        "markdown start of the document",
        lambda: _MARKDOWN.serialize(_MARKDOWN.insert_child(_MARKDOWN_TREE, _MARKDOWN_CHILD, anchor=None, position="start")),
        "x\n\n# It's\n\ntext\n",
    ),
    (
        "markdown end of the document",
        lambda: _MARKDOWN.serialize(_MARKDOWN.insert_child(_MARKDOWN_TREE, _MARKDOWN_CHILD, anchor=None, position="end")),
        "# It's\n\ntext\n\nx\n",
    ),
    (
        "markdown start of a heading's body",
        lambda: _MARKDOWN.serialize(_MARKDOWN.insert_child(_MARKDOWN_TREE, _MARKDOWN_CHILD, anchor=_MARKDOWN_ANCHOR, position="start")),
        "# It's\n\nx\n\ntext\n",
    ),
    (
        "markdown end of a heading's body",
        lambda: _MARKDOWN.serialize(_MARKDOWN.insert_child(_MARKDOWN_TREE, _MARKDOWN_CHILD, anchor=_MARKDOWN_ANCHOR, position="end")),
        "# It's\n\ntext\n\nx\n",
    ),
    (
        "markdown before a heading",
        lambda: _MARKDOWN.serialize(_MARKDOWN.insert_child(_MARKDOWN_TREE, _MARKDOWN_CHILD, anchor=_MARKDOWN_ANCHOR, position="before")),
        "x\n\n# It's\n\ntext\n",
    ),
    (
        "markdown after a heading",
        lambda: _MARKDOWN.serialize(_MARKDOWN.insert_child(_MARKDOWN_TREE, _MARKDOWN_CHILD, anchor=_MARKDOWN_ANCHOR, position="after")),
        "# It's\n\nx\n\ntext\n",
    ),
    # cpp
    (
        "cpp start of the translation unit",
        lambda: _CPP.serialize(_CPP.insert_child(_CPP_TREE, _CPP_CHILD, anchor=None, position="start")),
        "#include <map>\nclass A { int y; };\n",
    ),
    (
        "cpp end of the translation unit",
        lambda: _CPP.serialize(_CPP.insert_child(_CPP_TREE, _CPP_CHILD, anchor=None, position="end")),
        "class A { int y; };\n#include <map>\n",
    ),
    (
        "cpp start of a class body",
        lambda: _CPP.serialize(_CPP.insert_child(_CPP_TREE, _CPP_CHILD, anchor=_CPP_ANCHOR, position="start")),
        "class A {#include <map>\n int y; };\n",
    ),
    (
        "cpp end of a class body",
        lambda: _CPP.serialize(_CPP.insert_child(_CPP_TREE, _CPP_CHILD, anchor=_CPP_ANCHOR, position="end")),
        "class A { int y; #include <map>\n};\n",
    ),
    (
        "cpp before a class",
        lambda: _CPP.serialize(_CPP.insert_child(_CPP_TREE, _CPP_CHILD, anchor=_CPP_ANCHOR, position="before")),
        "#include <map>\nclass A { int y; };\n",
    ),
    (
        "cpp after a class",
        lambda: _CPP.serialize(_CPP.insert_child(_CPP_TREE, _CPP_CHILD, anchor=_CPP_ANCHOR, position="after")),
        "class A { int y; }#include <map>\n;\n",
    ),
]

_IDENTIFIER_BACKENDS = [
    JavaStructuralLanguage,
    TypeScriptStructuralLanguage,
    RubyStructuralLanguage,
    CSharpStructuralLanguage,
    GoStructuralLanguage,
    RustStructuralLanguage,
]


@pytest.mark.parametrize(("refuse", "expected"), [(refuse, expected) for _, refuse, expected in _REFUSALS], ids=[c[0] for c in _REFUSALS])
def test_a_backend_refusal_names_the_value_as_sent(refuse: Callable[[], object], expected: str) -> None:
    with pytest.raises((ValueError, TypeError, StructuralError)) as refusal:
        refuse()

    assert expected in str(refusal.value)


@pytest.mark.parametrize(("edit", "expected"), [(edit, expected) for _, edit, expected in _ACCEPTED], ids=[c[0] for c in _ACCEPTED])
def test_an_accepted_edit_lands(edit: Callable[[], str], expected: str) -> None:
    assert edit() == expected


@pytest.mark.parametrize("backend_class", _IDENTIFIER_BACKENDS, ids=lambda cls: cls.__name__)
def test_an_invalid_identifier_segment_is_named_as_sent(backend_class: type) -> None:
    with pytest.raises(StructuralError) as refusal:
        backend_class().name_resolver.parse("a.it's")

    assert "invalid identifier segment: 'it's'" in str(refusal.value)


@pytest.mark.parametrize("backend_class", _IDENTIFIER_BACKENDS, ids=lambda cls: cls.__name__)
def test_an_empty_logical_name_is_refused(backend_class: type) -> None:
    with pytest.raises(StructuralError) as refusal:
        backend_class().name_resolver.parse("")

    assert "logical name must be non-empty" in str(refusal.value)


@pytest.mark.parametrize("backend_class", _IDENTIFIER_BACKENDS, ids=lambda cls: cls.__name__)
def test_a_dotted_logical_name_splits_into_its_segments(backend_class: type) -> None:
    assert backend_class().name_resolver.parse("a.b").parts == ("a", "b")


def _structural_cursor(tmp_path: Path, relative_path: str, contents: str, name_path: str) -> tuple[CursorManager, str]:
    """
    :return: a cursor manager over ``tmp_path`` and the id of a structural cursor on ``name_path`` of ``relative_path``,
        which holds ``contents``
    """
    target = tmp_path / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(contents, encoding="utf-8")
    manager = _manager(tmp_path)
    with _force_lsp_miss():
        cursor_id, _ = manager.start_cursor(name_path, relative_path=relative_path)
    return manager, cursor_id


def test_a_duplicate_yaml_key_inserted_through_a_cursor_is_named_as_sent(tmp_path: Path) -> None:
    manager, cursor_id = _structural_cursor(tmp_path, "it's/config.yaml", "m:\n  it's: 1\n", "m")

    with pytest.raises(StructuralError) as refusal:
        manager.apply_container_edit(cursor_id, "insert_end", '"it\'s": 2')

    assert "duplicate key 'it's' in mapping" in str(refusal.value)


def test_a_duplicate_toml_key_inserted_through_a_cursor_is_named_as_sent(tmp_path: Path) -> None:
    manager, cursor_id = _structural_cursor(tmp_path, "it's/config.toml", '[m]\n"it\'s" = 1\n', "m")

    with pytest.raises(StructuralError) as refusal:
        manager.apply_container_edit(cursor_id, "insert_end", '"it\'s" = 2')

    assert "duplicate key 'it's' in mapping" in str(refusal.value)


def test_a_json_container_that_became_a_scalar_is_named_as_sent(tmp_path: Path) -> None:
    manager, cursor_id = _structural_cursor(tmp_path, "it's/config.json", '{"it\'s": {}}\n', "it's")
    (tmp_path / "it's" / "config.json").write_text('{"it\'s": 1}\n', encoding="utf-8")

    with pytest.raises(ValueError) as refusal:
        manager.apply_container_edit(cursor_id, "insert_end", '"k": 2')

    assert "path 'it's' does not resolve to a JSON object or array" in str(refusal.value)


def test_a_python_container_that_became_a_scalar_is_named_as_sent(tmp_path: Path) -> None:
    manager, cursor_id = _structural_cursor(tmp_path, "its/config.py", 'D = {"it\'s": {}}\n', 'D/["it\'s"]')
    (tmp_path / "its" / "config.py").write_text('D = {"it\'s": 1}\n', encoding="utf-8")

    with pytest.raises(ValueError) as refusal:
        manager.apply_container_edit(cursor_id, "insert_end", '"k": 2')

    assert "path 'D/[\"it's\"]' addresses a container member whose value is not itself a dict or list" in str(refusal.value)


@pytest.mark.parametrize(
    ("relative_path", "contents", "name_path", "expected"),
    [
        ("it's/config.yaml", "list:\n  - 1\n  - 2\n", "list/[0]", "list:\n  - 2\n"),
        ("it's/config.toml", "list = [1, 2]\n", "list/[0]", "list = [2]\n"),
        ("its/config.py", "L = [1, 2]\n", "L/[0]", "L = [2]\n"),
    ],
    ids=["yaml", "toml", "python"],
)
def test_a_sequence_item_removed_through_a_cursor_leaves_its_siblings(
    tmp_path: Path, relative_path: str, contents: str, name_path: str, expected: str
) -> None:
    target = tmp_path / relative_path
    target.parent.mkdir(parents=True)
    target.write_text(contents, encoding="utf-8")
    manager = _make_manager_via_subclass(tmp_path)
    cursor_id, _ = manager.start_cursor(name_path, relative_path=relative_path)

    manager.apply_container_edit(cursor_id, "remove", "")

    assert target.read_text(encoding="utf-8") == expected


_YAML_SCALAR = _YAML.build_declaration("scalar", {"value": 2}, ())
_YAML_KEYLESS_PAIR = dataclasses.replace(_YAML_PAIR, key=None)
_TOML_KEYLESS_PAIR = dataclasses.replace(_TOML_PAIR, key=None)
_YAML_SEQUENCE_TREE = _YAML.parse("- 1\n")

_MALFORMED: list[tuple[str, Callable[[], object], str]] = [
    # yaml declarations
    ("yaml unknown kind", lambda: _YAML.build_declaration("nope", {}, ()), "yaml has no kind 'nope'"),
    ("yaml scalar without a value", lambda: _YAML.build_declaration("scalar", {}, ()), "attribute 'value' is required"),
    ("yaml pair of a foreign child", lambda: _YAML.build_declaration("pair", {"key": "k"}, (object(),)), "pair child must be"),
    ("yaml mapping of a keyless pair", lambda: _YAML.build_declaration("mapping", {}, (_YAML_KEYLESS_PAIR,)), "is missing its key"),
    ("yaml sequence of a foreign child", lambda: _YAML.build_declaration("sequence", {}, (object(),)), "sequence child 0 must be"),
    # yaml insertions
    ("yaml insert into a foreign parent", lambda: _YAML.insert_child(_TOML_TREE, _YAML_PAIR), "parent must be _YamlTree"),
    ("yaml insert a foreign child", lambda: _YAML.insert_child(_YAML_TREE, _TOML_PAIR), "child must be a _YamlDeclaration"),
    ("yaml insert at an unknown position", lambda: _YAML.insert_child(_YAML_TREE, _YAML_PAIR, position="middle"), "invalid position"),
    ("yaml insert a keyless pair", lambda: _YAML.insert_child(_YAML_TREE, _YAML_KEYLESS_PAIR), "is missing its key"),
    ("yaml insert a pair into a sequence", lambda: _YAML.insert_child(_YAML_SEQUENCE_TREE, _YAML_PAIR), "requires a value declaration"),
    ("yaml insert into a scalar root", lambda: _YAML.insert_child(_YAML_SCALAR_TREE, _YAML_SCALAR), "cannot insert into scalar root"),
    # toml declarations
    ("toml unknown kind", lambda: _TOML.build_declaration("nope", {}, ()), "toml has no kind 'nope'"),
    ("toml scalar without a value", lambda: _TOML.build_declaration("scalar", {}, ()), "attribute 'value' is required"),
    ("toml pair of a foreign child", lambda: _TOML.build_declaration("pair", {"key": "k"}, (object(),)), "pair child must be"),
    ("toml table of a keyless pair", lambda: _TOML.build_declaration("table", {}, (_TOML_KEYLESS_PAIR,)), "is missing its key"),
    ("toml array of a foreign child", lambda: _TOML.build_declaration("array", {}, (object(),)), "array child 0 must be"),
    # toml insertions
    ("toml insert into a foreign parent", lambda: _TOML.insert_child(_YAML_TREE, _TOML_PAIR), "parent must be _TomlTree"),
    ("toml insert a foreign child", lambda: _TOML.insert_child(_TOML_TREE, _YAML_PAIR), "child must be a _TomlDeclaration"),
    ("toml insert at an unknown position", lambda: _TOML.insert_child(_TOML_TREE, _TOML_PAIR, position="middle"), "invalid position"),
    ("toml insert a keyless pair", lambda: _TOML.insert_child(_TOML_TREE, _TOML_KEYLESS_PAIR), "is missing its key"),
]


@pytest.mark.parametrize(("refuse", "expected"), [(refuse, expected) for _, refuse, expected in _MALFORMED], ids=[c[0] for c in _MALFORMED])
def test_a_malformed_declaration_or_insertion_is_refused(refuse: Callable[[], object], expected: str) -> None:
    with pytest.raises((ValueError, TypeError, KeyError, StructuralError)) as refusal:
        refuse()

    assert expected in str(refusal.value)


def test_a_value_declared_for_a_sequence_root_is_appended() -> None:
    assert _YAML.serialize(_YAML.insert_child(_YAML_SEQUENCE_TREE, _YAML_SCALAR)) == "- 1\n- 2\n"
