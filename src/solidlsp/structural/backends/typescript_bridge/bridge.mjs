// serena-typescript-bridge
//
// Length-prefixed JSON request/response loop over stdio. Each request is a
// 4-byte little-endian length followed by UTF-8 JSON; each response has the
// same framing. Stateless: every request carries the source text it
// operates on, so there are no cross-call handles to invalidate.
//
// Operations mirror the Swift bridge:
//   - walk_symbols       : (kind, name_path, extent_offset, extent_length, body_range)
//   - insert_child       : new source with a rendered child inserted
//   - remove_child       : new source with a named child excised
//   - find_matches       : match extents + captured bindings
//   - apply_replacement  : new source with a byte range replaced
//   - probe_parse        : whether the source has parse errors
//   - shutdown           : exit cleanly
//
// All offsets on the wire are UTF-8 byte offsets (matching the Swift
// backend and SwiftSyntax's utf8Offset convention). TypeScript itself
// uses UTF-16 code unit offsets; the bridge converts at the wire boundary.

import * as fs from "node:fs";
import * as ts from "typescript";

// ---------------------------------------------------------------------------
// I/O framing
// ---------------------------------------------------------------------------

/** Read exactly `n` bytes from stdin. Returns null at clean EOF. */
function readExact(n) {
    const buf = Buffer.alloc(n);
    let read = 0;
    while (read < n) {
        let chunk;
        try {
            chunk = fs.readSync(0, buf, read, n - read, null);
        } catch (err) {
            if (err.code === "EAGAIN") continue;
            return null;
        }
        if (chunk === 0) return null;
        read += chunk;
    }
    return buf;
}

function readFrame() {
    const prefix = readExact(4);
    if (prefix === null) return null;
    const length = prefix.readUInt32LE(0);
    if (length === 0) return Buffer.alloc(0);
    return readExact(length);
}

function writeFrame(payloadBuf) {
    const prefix = Buffer.alloc(4);
    prefix.writeUInt32LE(payloadBuf.length, 0);
    fs.writeSync(1, prefix);
    fs.writeSync(1, payloadBuf);
}

function jsonEncode(obj) {
    return Buffer.from(JSON.stringify(obj), "utf8");
}

function okResponse(fields = {}) {
    return jsonEncode({ ok: true, ...fields });
}

function errorResponse(kind, message) {
    return jsonEncode({ ok: false, error_kind: kind, message });
}

// ---------------------------------------------------------------------------
// UTF-16 <-> UTF-8 offset conversion
// ---------------------------------------------------------------------------
//
// TypeScript's AST positions are UTF-16 code unit offsets into the source
// string. The wire protocol uses UTF-8 byte offsets. For typical ASCII
// sources the two are identical; for non-ASCII we convert via Buffer.

function charToByte(source, charOffset) {
    if (charOffset <= 0) return 0;
    if (charOffset >= source.length) return Buffer.byteLength(source, "utf8");
    return Buffer.byteLength(source.substring(0, charOffset), "utf8");
}

function byteToChar(source, byteOffset) {
    if (byteOffset <= 0) return 0;
    const buf = Buffer.from(source, "utf8");
    if (byteOffset >= buf.length) return source.length;
    return buf.slice(0, byteOffset).toString("utf8").length;
}

// ---------------------------------------------------------------------------
// Parsing
// ---------------------------------------------------------------------------

const DEFAULT_FILENAME = "input.ts";

function parseSource(source) {
    return ts.createSourceFile(
        DEFAULT_FILENAME,
        source,
        ts.ScriptTarget.Latest,
        /* setParentNodes */ true,
        ts.ScriptKind.TS,
    );
}

function hasParseErrors(sourceFile) {
    const diagnostics = sourceFile.parseDiagnostics || [];
    return diagnostics.length > 0;
}

// ---------------------------------------------------------------------------
// Kind classification
// ---------------------------------------------------------------------------
//
// Returns the structural kind name for a top-level or nested declaration.
// Context matters: a FunctionDeclaration inside a namespace is still a
// `function`, but inside a class body we never see FunctionDeclaration —
// that role is played by MethodDeclaration.

function kindFor(node, parentKind) {
    switch (node.kind) {
        case ts.SyntaxKind.ImportDeclaration:
        case ts.SyntaxKind.ImportEqualsDeclaration:
            return "import";
        case ts.SyntaxKind.ExportDeclaration:
        case ts.SyntaxKind.ExportAssignment:
            return "export";
        case ts.SyntaxKind.ClassDeclaration:
            return "class";
        case ts.SyntaxKind.InterfaceDeclaration:
            return "interface";
        case ts.SyntaxKind.TypeAliasDeclaration:
            return "type_alias";
        case ts.SyntaxKind.EnumDeclaration:
            return "enum";
        case ts.SyntaxKind.ModuleDeclaration:
            return "namespace";
        case ts.SyntaxKind.FunctionDeclaration:
            return "function";
        case ts.SyntaxKind.VariableStatement:
            return "variable";
        case ts.SyntaxKind.MethodDeclaration:
        case ts.SyntaxKind.MethodSignature:
            return "method";
        case ts.SyntaxKind.Constructor:
            return "constructor";
        case ts.SyntaxKind.PropertyDeclaration:
        case ts.SyntaxKind.PropertySignature:
            return "property";
        case ts.SyntaxKind.GetAccessor:
        case ts.SyntaxKind.SetAccessor:
            return "method";
        case ts.SyntaxKind.EnumMember:
            return "enum_member";
        default:
            return null;
    }
}

// ---------------------------------------------------------------------------
// Name spelling
// ---------------------------------------------------------------------------

function getName(node, sourceFile) {
    switch (node.kind) {
        case ts.SyntaxKind.ImportDeclaration: {
            // `import ... from 'x'` — use the module specifier string literal
            const spec = node.moduleSpecifier;
            if (spec && ts.isStringLiteral(spec)) return spec.text;
            return null;
        }
        case ts.SyntaxKind.ImportEqualsDeclaration: {
            // `import X = ns` — use the LHS name
            return node.name?.text ?? null;
        }
        case ts.SyntaxKind.ExportDeclaration: {
            // `export * from 'x'` / `export { a } from 'x'` — module specifier,
            // or the joined element list for a bare `export { a, b }`.
            const spec = node.moduleSpecifier;
            if (spec && ts.isStringLiteral(spec)) return spec.text;
            const clause = node.exportClause;
            if (clause && ts.isNamedExports(clause)) {
                const names = clause.elements.map((e) => e.name.text);
                if (names.length > 0) return names.join("+");
            }
            return "*";
        }
        case ts.SyntaxKind.ExportAssignment: {
            // `export default X` / `export = X`
            return "default";
        }
        case ts.SyntaxKind.ClassDeclaration:
        case ts.SyntaxKind.InterfaceDeclaration:
        case ts.SyntaxKind.TypeAliasDeclaration:
        case ts.SyntaxKind.EnumDeclaration:
        case ts.SyntaxKind.FunctionDeclaration: {
            // may be anonymous default exports; fall back to "default" so we
            // never emit a null name path segment
            return node.name?.text ?? "default";
        }
        case ts.SyntaxKind.ModuleDeclaration: {
            // `namespace foo.bar { }` — the AST nests ModuleDeclarations, so
            // the name here is only the leftmost segment.
            const n = node.name;
            if (!n) return null;
            if (ts.isIdentifier(n)) return n.text;
            if (ts.isStringLiteral(n)) return n.text;
            return n.getText(sourceFile);
        }
        case ts.SyntaxKind.VariableStatement: {
            // `const a = 1, b = 2;` — spell as joined binding names so the
            // declaration is addressable even with multi-binding forms
            const decls = node.declarationList.declarations;
            const names = decls
                .map((d) => bindingName(d.name))
                .filter((x) => x !== null);
            if (names.length === 0) return null;
            return names.join("+");
        }
        case ts.SyntaxKind.MethodDeclaration:
        case ts.SyntaxKind.MethodSignature:
        case ts.SyntaxKind.PropertyDeclaration:
        case ts.SyntaxKind.PropertySignature: {
            return propertyLikeName(node.name);
        }
        case ts.SyntaxKind.Constructor:
            return "constructor";
        case ts.SyntaxKind.GetAccessor:
            return "get " + (propertyLikeName(node.name) ?? "?");
        case ts.SyntaxKind.SetAccessor:
            return "set " + (propertyLikeName(node.name) ?? "?");
        case ts.SyntaxKind.EnumMember: {
            return propertyLikeName(node.name);
        }
        default:
            return null;
    }
}

/** Best-effort name spelling for a BindingName (identifier or binding pattern). */
function bindingName(binding) {
    if (!binding) return null;
    if (ts.isIdentifier(binding)) return binding.text;
    // destructuring patterns are not addressable; return null
    return null;
}

/** Best-effort spelling for a class/interface member name. */
function propertyLikeName(name) {
    if (!name) return null;
    if (ts.isIdentifier(name) || ts.isPrivateIdentifier(name)) return name.text;
    if (ts.isStringLiteral(name) || ts.isNumericLiteral(name)) return name.text;
    if (ts.isComputedPropertyName(name)) return `[${name.expression.getText()}]`;
    return null;
}

// ---------------------------------------------------------------------------
// Body range extraction
// ---------------------------------------------------------------------------
//
// For compound declarations (class / interface / namespace / enum) the
// body range is the strict interior of the `{ ... }` braces, matching the
// Swift bridge's convention. A child inserted at `body_range[1]` lands
// between the last member and the closing brace.

function bodyRangeFor(node, sourceFile) {
    switch (node.kind) {
        case ts.SyntaxKind.ClassDeclaration:
        case ts.SyntaxKind.InterfaceDeclaration:
        case ts.SyntaxKind.EnumDeclaration:
            return bracedInterior(node, sourceFile);
        case ts.SyntaxKind.ModuleDeclaration: {
            // `namespace foo { ... }` — the body is a ModuleBlock child
            if (node.body && node.body.kind === ts.SyntaxKind.ModuleBlock) {
                return bracedInterior(node.body, sourceFile);
            }
            return null;
        }
        default:
            return null;
    }
}

/**
 * Given a node that carries surface `{ ... }` delimiters in its text span,
 * locate the OpenBraceToken and CloseBraceToken child tokens and return the
 * strict interior (end-of-`{` through start-of-`}`).
 */
function bracedInterior(node, sourceFile) {
    const children = node.getChildren(sourceFile);
    let openBrace = null;
    let closeBrace = null;
    for (const c of children) {
        if (c.kind === ts.SyntaxKind.OpenBraceToken) openBrace = c;
        else if (c.kind === ts.SyntaxKind.CloseBraceToken) closeBrace = c;
    }
    if (!openBrace || !closeBrace) return null;
    return [openBrace.end, closeBrace.getStart(sourceFile)];
}

/**
 * Extend an offset forward through whitespace, consuming at most one line
 * break. Mirrors the Swift bridge's use of `endPosition` which includes
 * trailing trivia: "after anchor" lands past the anchor's trailing newline.
 */
function extendThroughTrailingNewline(source, end) {
    let e = end;
    while (e < source.length) {
        const c = source.charCodeAt(e);
        if (c === 0x20 || c === 0x09) { // space or tab
            e++;
            continue;
        }
        if (c === 0x0d) { // CR
            return source.charCodeAt(e + 1) === 0x0a ? e + 2 : e + 1;
        }
        if (c === 0x0a) { // LF
            return e + 1;
        }
        break;
    }
    return e;
}

// ---------------------------------------------------------------------------
// Symbol walking
// ---------------------------------------------------------------------------
//
// Walks top-level and nested named declarations of a source file. Parent
// context is tracked so MethodDeclaration inside a class emits `method`
// under the class's path.

function walkSymbols(sourceFile) {
    const out = [];
    walkNodes(sourceFile.statements, [], null, sourceFile, out);
    return out;
}

function walkNodes(nodes, parentPath, parentKind, sourceFile, out) {
    for (const node of nodes) {
        const kind = kindFor(node, parentKind);
        if (kind === null) continue;
        const name = getName(node, sourceFile);
        if (name === null) continue;
        const segments = parentPath.concat([name]);
        const namePath = segments.join("/");
        const extentOffset = node.getStart(sourceFile);
        const extentEnd = extendThroughTrailingNewline(sourceFile.text, node.end);
        const body = bodyRangeFor(node, sourceFile);
        out.push({
            kind,
            namePath,
            extentOffset: charToByte(sourceFile.text, extentOffset),
            extentLength: charToByte(sourceFile.text, extentEnd) - charToByte(sourceFile.text, extentOffset),
            bodyRange: body
                ? [charToByte(sourceFile.text, body[0]), charToByte(sourceFile.text, body[1])]
                : null,
        });
        // recurse into members for type-like containers
        const members = memberList(node);
        if (members !== null) {
            walkNodes(members, segments, kind, sourceFile, out);
        }
    }
}

/** If `node` carries a `{ members }` block, return the member array; else null. */
function memberList(node) {
    switch (node.kind) {
        case ts.SyntaxKind.ClassDeclaration:
        case ts.SyntaxKind.InterfaceDeclaration:
            return node.members;
        case ts.SyntaxKind.EnumDeclaration:
            return node.members;
        case ts.SyntaxKind.ModuleDeclaration:
            if (node.body && node.body.kind === ts.SyntaxKind.ModuleBlock) {
                return node.body.statements;
            }
            return null;
        default:
            return null;
    }
}

// ---------------------------------------------------------------------------
// Symbol lookup by name path
// ---------------------------------------------------------------------------

function lookupSymbol(sourceFile, namePath) {
    return walkSymbols(sourceFile).find((s) => s.namePath === namePath) ?? null;
}

// ---------------------------------------------------------------------------
// ops: walk_symbols / probe_parse
// ---------------------------------------------------------------------------

function opWalkSymbols(request) {
    const source = request.source;
    if (typeof source !== "string") {
        return errorResponse("bad_request", "walk_symbols requires 'source'");
    }
    const sourceFile = parseSource(source);
    const entries = walkSymbols(sourceFile);
    const payload = entries.map((e) => ({
        kind: e.kind,
        name_path: e.namePath,
        extent_offset: e.extentOffset,
        extent_length: e.extentLength,
        body_range: e.bodyRange,
    }));
    return okResponse({ symbols: payload });
}

function opProbeParse(request) {
    const source = request.source;
    if (typeof source !== "string") {
        return errorResponse("bad_request", "probe_parse requires 'source'");
    }
    const sourceFile = parseSource(source);
    return okResponse({ has_errors: hasParseErrors(sourceFile) });
}

// ---------------------------------------------------------------------------
// op: insert_child
// ---------------------------------------------------------------------------

function opInsertChild(request) {
    const { source, child_source, position, parent_name_path, anchor_name_path } = request;
    if (typeof source !== "string" || typeof child_source !== "string" || typeof position !== "string") {
        return errorResponse("bad_request", "insert_child requires source/child_source/position");
    }
    const sourceFile = parseSource(source);
    let windowStart;
    let windowEnd;
    if (parent_name_path != null) {
        const parent = lookupSymbol(sourceFile, parent_name_path);
        if (!parent) {
            return errorResponse("symbol_missing", `parent ${parent_name_path} not found`);
        }
        if (!parent.bodyRange) {
            return errorResponse("no_body", `parent ${parent_name_path} has no body`);
        }
        windowStart = parent.bodyRange[0];
        windowEnd = parent.bodyRange[1];
    } else {
        windowStart = 0;
        windowEnd = Buffer.byteLength(source, "utf8");
    }
    let offset;
    switch (position) {
        case "start":
            offset = windowStart;
            break;
        case "end":
            offset = windowEnd;
            break;
        case "before":
        case "after": {
            if (anchor_name_path == null) {
                return errorResponse("bad_request", `position ${position} requires anchor`);
            }
            const anchor = lookupSymbol(sourceFile, anchor_name_path);
            if (!anchor) {
                return errorResponse("symbol_missing", `anchor ${anchor_name_path} not found`);
            }
            offset = position === "before" ? anchor.extentOffset : anchor.extentOffset + anchor.extentLength;
            break;
        }
        default:
            return errorResponse("bad_request", `invalid position: ${position}`);
    }
    // ensure trailing newline between adjacent decls (matches Swift discipline)
    const normalized = /\n$/.test(child_source) ? child_source : child_source + "\n";
    const edited = replaceByteRange(source, offset, 0, normalized);
    return okResponse({ source: edited });
}

// ---------------------------------------------------------------------------
// op: remove_child
// ---------------------------------------------------------------------------

function opRemoveChild(request) {
    const { source, child_name_path } = request;
    if (typeof source !== "string" || typeof child_name_path !== "string") {
        return errorResponse("bad_request", "remove_child requires source/child_name_path");
    }
    const sourceFile = parseSource(source);
    const sym = lookupSymbol(sourceFile, child_name_path);
    if (!sym) {
        return errorResponse("symbol_missing", `child ${child_name_path} not found`);
    }
    const edited = replaceByteRange(source, sym.extentOffset, sym.extentLength, "");
    return okResponse({ source: edited });
}

// ---------------------------------------------------------------------------
// op: apply_replacement
// ---------------------------------------------------------------------------

function opApplyReplacement(request) {
    const { source, match_offset, match_length, replacement_source } = request;
    if (
        typeof source !== "string" ||
        typeof match_offset !== "number" ||
        typeof match_length !== "number" ||
        typeof replacement_source !== "string"
    ) {
        return errorResponse(
            "bad_request",
            "apply_replacement requires source/match_offset/match_length/replacement_source",
        );
    }
    const edited = replaceByteRange(source, match_offset, match_length, replacement_source);
    return okResponse({ source: edited });
}

// ---------------------------------------------------------------------------
// op: find_matches
// ---------------------------------------------------------------------------
//
// Matching strategy mirrors the Swift bridge: parse the pattern as a source
// file, extract its single most-specific node (first Declaration / Statement /
// Expression), walk the input tree, and compare each node's structure to
// the pattern's root. `$name` tokens in the pattern bind any subtree;
// repeated names must match by printed text.

function opFindMatches(request) {
    const { source, pattern_source, scope_name_path } = request;
    if (typeof source !== "string" || typeof pattern_source !== "string") {
        return errorResponse("bad_request", "find_matches requires source/pattern_source");
    }
    const sourceFile = parseSource(source);
    const patternRoot = extractPatternRoot(pattern_source);
    if (!patternRoot) {
        return errorResponse("pattern_parse", "pattern did not produce a matchable node");
    }
    let searchRoot = sourceFile;
    if (scope_name_path != null) {
        const scopeSym = lookupSymbol(sourceFile, scope_name_path);
        if (!scopeSym) {
            return errorResponse("symbol_missing", `scope ${scope_name_path} not found`);
        }
        const scopeNode = enclosingNode(sourceFile, scopeSym.extentOffset, scopeSym.extentLength);
        if (scopeNode) searchRoot = scopeNode;
    }
    const matches = collectMatches(searchRoot, patternRoot.node, patternRoot.sourceFile, sourceFile);
    const payload = matches.map((m) => ({
        extent_offset: charToByte(sourceFile.text, m.node.getStart(sourceFile)),
        extent_length:
            charToByte(sourceFile.text, m.node.end) - charToByte(sourceFile.text, m.node.getStart(sourceFile)),
        bindings: Object.fromEntries(
            Object.entries(m.bindings).map(([name, bNode]) => [
                name,
                {
                    source: bNode.getText(sourceFile),
                    extent_offset: charToByte(sourceFile.text, bNode.getStart(sourceFile)),
                    extent_length:
                        charToByte(sourceFile.text, bNode.end) -
                        charToByte(sourceFile.text, bNode.getStart(sourceFile)),
                },
            ]),
        ),
    }));
    return okResponse({ matches: payload });
}

function extractPatternRoot(patternSource) {
    const sourceFile = parseSource(patternSource);
    const stmts = sourceFile.statements;
    if (stmts.length !== 1) return null;
    const only = stmts[0];
    // Prefer the inner expression of an ExpressionStatement so `$x` used as
    // a pattern matches any Expression rather than only an ExpressionStatement
    if (ts.isExpressionStatement(only)) {
        return { node: only.expression, sourceFile };
    }
    return { node: only, sourceFile };
}

function enclosingNode(root, byteOffset, byteLength) {
    const sourceFile = root.kind === ts.SyntaxKind.SourceFile ? root : root.getSourceFile();
    const charStart = byteToChar(sourceFile.text, byteOffset);
    const charEnd = byteToChar(sourceFile.text, byteOffset + byteLength);
    let best = null;
    const walk = (node) => {
        const s = node.getStart(sourceFile);
        const e = node.end;
        if (s <= charStart && e >= charEnd) {
            best = node;
            ts.forEachChild(node, walk);
        }
    };
    walk(root);
    return best;
}

function collectMatches(root, pattern, patternSourceFile, candidateSourceFile) {
    const out = [];
    const walk = (node) => {
        const bindings = {};
        if (matchNode(pattern, node, patternSourceFile, candidateSourceFile, bindings)) {
            out.push({ node, bindings });
        }
        ts.forEachChild(node, walk);
    };
    walk(root);
    return out;
}

/**
 * Structural comparison. If the pattern is a bare identifier starting with
 * `$`, it matches any candidate and records a binding. Otherwise compare
 * syntax kinds and recurse into matching-arity children.
 */
function matchNode(pattern, candidate, patternSourceFile, candidateSourceFile, bindings) {
    if (ts.isIdentifier(pattern) && pattern.text.startsWith("$") && pattern.text.length > 1) {
        const name = pattern.text.substring(1);
        if (name === "_") return true;
        const existing = bindings[name];
        if (existing) {
            return existing.getText(candidateSourceFile) === candidate.getText(candidateSourceFile);
        }
        bindings[name] = candidate;
        return true;
    }
    if (pattern.kind !== candidate.kind) return false;
    // leaf token comparison: compare source text for simple tokens
    const pChildren = pattern.getChildren(patternSourceFile);
    const cChildren = candidate.getChildren(candidateSourceFile);
    if (pChildren.length === 0 && cChildren.length === 0) {
        return pattern.getText(patternSourceFile) === candidate.getText(candidateSourceFile);
    }
    if (pChildren.length !== cChildren.length) return false;
    for (let i = 0; i < pChildren.length; i++) {
        if (!matchNode(pChildren[i], cChildren[i], patternSourceFile, candidateSourceFile, bindings)) {
            return false;
        }
    }
    return true;
}

// ---------------------------------------------------------------------------
// Byte-range rewriter
// ---------------------------------------------------------------------------

function replaceByteRange(source, byteOffset, byteLength, replacement) {
    const buf = Buffer.from(source, "utf8");
    const before = buf.slice(0, byteOffset);
    const after = buf.slice(byteOffset + byteLength);
    const replBuf = Buffer.from(replacement, "utf8");
    return Buffer.concat([before, replBuf, after]).toString("utf8");
}

// ---------------------------------------------------------------------------
// Dispatch
// ---------------------------------------------------------------------------

function handle(request) {
    const op = request.op;
    try {
        switch (op) {
            case "walk_symbols":
                return opWalkSymbols(request);
            case "insert_child":
                return opInsertChild(request);
            case "remove_child":
                return opRemoveChild(request);
            case "find_matches":
                return opFindMatches(request);
            case "apply_replacement":
                return opApplyReplacement(request);
            case "probe_parse":
                return opProbeParse(request);
            case "shutdown":
                writeFrame(okResponse());
                process.exit(0);
            default:
                return errorResponse("unknown_op", `unknown op: ${op}`);
        }
    } catch (err) {
        return errorResponse("bridge", err && err.message ? err.message : String(err));
    }
}

// ---------------------------------------------------------------------------
// Main loop
// ---------------------------------------------------------------------------

while (true) {
    const frame = readFrame();
    if (frame === null) break;
    if (frame.length === 0) continue;
    let request;
    try {
        request = JSON.parse(frame.toString("utf8"));
    } catch {
        writeFrame(errorResponse("bad_request", "malformed JSON"));
        continue;
    }
    if (!request || typeof request.op !== "string") {
        writeFrame(errorResponse("bad_request", "malformed request"));
        continue;
    }
    writeFrame(handle(request));
}
