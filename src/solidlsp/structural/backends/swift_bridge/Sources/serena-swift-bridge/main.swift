// serena-swift-bridge
//
// Length-prefixed JSON request/response loop over stdio. Each request is a
// 4-byte little-endian length followed by UTF-8 JSON; each response has the
// same framing. The bridge is stateless: every request carries the source
// text it operates on, so there are no cross-call handles to invalidate.
//
// Operations correspond to the subset of StructuralLanguage that SwiftSyntax
// cannot satisfy from pure Python string manipulation:
//   - walk_symbols       : yields (kind, name_path, extent_offset,
//                          extent_length, body_range)
//   - insert_child       : returns new source with a rendered child inserted
//   - remove_child       : returns new source with a named child excised
//   - find_matches       : returns match extents + captured bindings
//   - apply_replacement  : returns new source with an extent replaced
//   - probe_parse        : returns whether the source has error tokens
//
// Errors use `{"ok": false, "error_kind": "...", "message": "..."}`. The
// Python side maps these into the existing ParseError / PatternError /
// DeclarationError hierarchy.

import Foundation
import SwiftSyntax
import SwiftParser

// MARK: - I/O framing

/// Reads a 4-byte little-endian length prefix, then that many bytes of
/// payload. Returns nil at clean EOF (so the subprocess can shut down when
/// its parent closes stdin).
func readFrame(from handle: FileHandle) -> Data? {
    // prefix
    let prefixData: Data
    do {
        let data = try handle.read(upToCount: 4)
        guard let data = data, data.count == 4 else { return nil }
        prefixData = data
    } catch {
        return nil
    }
    let length = prefixData.withUnsafeBytes { bytes -> UInt32 in
        bytes.load(as: UInt32.self).littleEndian
    }
    if length == 0 { return Data() }

    // payload — may come in chunks; accumulate until we have `length` bytes
    var collected = Data()
    collected.reserveCapacity(Int(length))
    while collected.count < Int(length) {
        let remaining = Int(length) - collected.count
        guard let chunk = try? handle.read(upToCount: remaining), !chunk.isEmpty else {
            return nil
        }
        collected.append(chunk)
    }
    return collected
}

/// Writes a 4-byte little-endian length prefix, then the payload.
func writeFrame(_ payload: Data, to handle: FileHandle) {
    var length = UInt32(payload.count).littleEndian
    let prefix = Data(bytes: &length, count: 4)
    try? handle.write(contentsOf: prefix)
    try? handle.write(contentsOf: payload)
}

// MARK: - response helpers

func jsonEncode(_ obj: [String: Any]) -> Data {
    (try? JSONSerialization.data(withJSONObject: obj, options: [])) ?? Data("{}".utf8)
}

func okResponse(_ fields: [String: Any] = [:]) -> Data {
    var r: [String: Any] = ["ok": true]
    for (k, v) in fields { r[k] = v }
    return jsonEncode(r)
}

func errorResponse(kind: String, message: String) -> Data {
    jsonEncode(["ok": false, "error_kind": kind, "message": message])
}

// MARK: - kind classification

/// Returns the structural kind name for a DeclSyntax, taking enclosing
/// context into account (a FunctionDecl inside a class is a "method", not a
/// "function").
func kindName(for decl: DeclSyntax, parentKind: String?) -> String? {
    if decl.is(ImportDeclSyntax.self) { return "import" }
    if decl.is(ClassDeclSyntax.self) { return "class" }
    if decl.is(StructDeclSyntax.self) { return "struct" }
    if decl.is(EnumDeclSyntax.self) { return "enum" }
    if decl.is(ProtocolDeclSyntax.self) { return "protocol" }
    if decl.is(ExtensionDeclSyntax.self) { return "extension" }
    if decl.is(ActorDeclSyntax.self) { return "actor" }
    if decl.is(TypeAliasDeclSyntax.self) { return "type_alias" }
    if decl.is(EnumCaseDeclSyntax.self) { return "enum_case" }

    let typeLike: Set<String> = ["class", "struct", "enum", "protocol", "extension", "actor"]
    let parentIsTypeLike = parentKind.map(typeLike.contains) ?? false

    if decl.is(FunctionDeclSyntax.self) {
        return parentIsTypeLike ? "method" : "function"
    }
    if decl.is(InitializerDeclSyntax.self) { return "initializer" }
    if decl.is(VariableDeclSyntax.self) {
        return parentIsTypeLike ? "property" : "variable"
    }
    return nil
}

/// Returns the spelling used to identify the decl in its name path.
/// Overload disambiguation is not attempted; repeated names under the same
/// parent produce repeated path segments (Python side tolerates ambiguity
/// via extent_offset in the symbol ref).
func nameSpelling(for decl: DeclSyntax) -> String? {
    if let d = decl.as(ImportDeclSyntax.self) {
        return d.path.trimmedDescription
    }
    if let d = decl.as(ClassDeclSyntax.self) { return d.name.text }
    if let d = decl.as(StructDeclSyntax.self) { return d.name.text }
    if let d = decl.as(EnumDeclSyntax.self) { return d.name.text }
    if let d = decl.as(ProtocolDeclSyntax.self) { return d.name.text }
    if let d = decl.as(ActorDeclSyntax.self) { return d.name.text }
    if let d = decl.as(TypeAliasDeclSyntax.self) { return d.name.text }
    if let d = decl.as(ExtensionDeclSyntax.self) {
        return d.extendedType.trimmedDescription
    }
    if let d = decl.as(FunctionDeclSyntax.self) { return d.name.text }
    if decl.is(InitializerDeclSyntax.self) { return "init" }
    if let d = decl.as(VariableDeclSyntax.self) {
        // first binding's identifier, joined with '+' on multi-binding decls
        let names = d.bindings.compactMap { binding -> String? in
            if let ident = binding.pattern.as(IdentifierPatternSyntax.self) {
                return ident.identifier.text
            }
            return nil
        }
        if names.isEmpty { return nil }
        return names.joined(separator: "+")
    }
    if let d = decl.as(EnumCaseDeclSyntax.self) {
        let names = d.elements.map { $0.name.text }
        if names.isEmpty { return nil }
        return names.joined(separator: "+")
    }
    return nil
}

// MARK: - body range extraction

/// For decls that carry a `{ ... }` body, returns the byte range strictly
/// between the braces (exclusive on both ends), so the Python side can
/// place child sources inside without duplicating braces.
func bodyRange(for decl: DeclSyntax) -> (Int, Int)? {
    // each decl kind exposes its members block via a distinct child name
    // in SwiftSyntax; walk them explicitly rather than by reflection
    if let d = decl.as(ClassDeclSyntax.self) {
        return innerRange(of: d.memberBlock)
    }
    if let d = decl.as(StructDeclSyntax.self) {
        return innerRange(of: d.memberBlock)
    }
    if let d = decl.as(EnumDeclSyntax.self) {
        return innerRange(of: d.memberBlock)
    }
    if let d = decl.as(ProtocolDeclSyntax.self) {
        return innerRange(of: d.memberBlock)
    }
    if let d = decl.as(ExtensionDeclSyntax.self) {
        return innerRange(of: d.memberBlock)
    }
    if let d = decl.as(ActorDeclSyntax.self) {
        return innerRange(of: d.memberBlock)
    }
    return nil
}

func innerRange(of block: MemberBlockSyntax) -> (Int, Int) {
    // Pin to the byte offsets of the actual brace characters so trivia
    // (like the newline between `{` and `}` in an empty body) falls inside
    // the window. This keeps insert_child stable across trivia variation.
    let start = block.leftBrace.endPositionBeforeTrailingTrivia.utf8Offset
    let end = block.rightBrace.positionAfterSkippingLeadingTrivia.utf8Offset
    return (start, end)
}

// MARK: - symbol walking

struct SymbolEntry {
    let kind: String
    let namePath: String
    let extentOffset: Int
    let extentLength: Int
    let bodyRange: (Int, Int)?
}

/// Walks the top-level and nested named symbols of a source file. Parent
/// context is tracked so FunctionDecls inside type bodies become "method"
/// and name paths carry their enclosing types.
func walkSymbols(_ source: SourceFileSyntax) -> [SymbolEntry] {
    var out: [SymbolEntry] = []
    walkDecls(source.statements.compactMap { $0.item.as(DeclSyntax.self) },
              parentPath: [],
              parentKind: nil,
              into: &out)
    return out
}

/// Recursively walks a list of DeclSyntax nodes, pushing each named symbol
/// into `out` and recursing into type-like bodies.
func walkDecls(_ decls: [DeclSyntax],
               parentPath: [String],
               parentKind: String?,
               into out: inout [SymbolEntry]) {
    for decl in decls {
        guard let kind = kindName(for: decl, parentKind: parentKind),
              let name = nameSpelling(for: decl) else { continue }
        let pathSegments = parentPath + [name]
        let namePath = pathSegments.joined(separator: "/")
        // Use content start (skipping leading trivia) so "insert before"
        // places new content between the previous decl's trailing newline
        // and this decl's own first character, preserving blank lines.
        // The extent ends at endPosition (including trailing trivia), so
        // "remove" strips the decl's trailing newline too.
        let position = decl.positionAfterSkippingLeadingTrivia.utf8Offset
        let length = decl.endPosition.utf8Offset - position
        let body = bodyRange(for: decl)
        out.append(SymbolEntry(kind: kind,
                               namePath: namePath,
                               extentOffset: position,
                               extentLength: length,
                               bodyRange: body))

        // recurse into type-like bodies so nested members get emitted
        if let members = memberDecls(of: decl) {
            walkDecls(members,
                      parentPath: pathSegments,
                      parentKind: kind,
                      into: &out)
        }
    }
}

/// If `decl` carries a `{ members }` block, returns the member DeclSyntax
/// list; otherwise nil (so the walker knows to stop recursing).
func memberDecls(of decl: DeclSyntax) -> [DeclSyntax]? {
    func fromBlock(_ block: MemberBlockSyntax) -> [DeclSyntax] {
        block.members.map { $0.decl }
    }
    if let d = decl.as(ClassDeclSyntax.self) { return fromBlock(d.memberBlock) }
    if let d = decl.as(StructDeclSyntax.self) { return fromBlock(d.memberBlock) }
    if let d = decl.as(EnumDeclSyntax.self) { return fromBlock(d.memberBlock) }
    if let d = decl.as(ProtocolDeclSyntax.self) { return fromBlock(d.memberBlock) }
    if let d = decl.as(ExtensionDeclSyntax.self) { return fromBlock(d.memberBlock) }
    if let d = decl.as(ActorDeclSyntax.self) { return fromBlock(d.memberBlock) }
    return nil
}

// MARK: - symbol lookup by name path

/// Locates a symbol by its name path; returns its (extent_offset,
/// extent_length, body_range) or nil if not found.
func lookupSymbol(_ source: SourceFileSyntax, namePath: String) -> SymbolEntry? {
    walkSymbols(source).first { $0.namePath == namePath }
}

// MARK: - op: walk_symbols

func opWalkSymbols(_ request: [String: Any]) -> Data {
    guard let source = request["source"] as? String else {
        return errorResponse(kind: "bad_request", message: "walk_symbols requires 'source'")
    }
    let tree = Parser.parse(source: source)
    let entries = walkSymbols(tree)
    let payload = entries.map { entry -> [String: Any] in
        var d: [String: Any] = [
            "kind": entry.kind,
            "name_path": entry.namePath,
            "extent_offset": entry.extentOffset,
            "extent_length": entry.extentLength,
        ]
        if let br = entry.bodyRange {
            d["body_range"] = [br.0, br.1]
        } else {
            d["body_range"] = NSNull()
        }
        return d
    }
    return okResponse(["symbols": payload])
}

// MARK: - op: probe_parse

func opProbeParse(_ request: [String: Any]) -> Data {
    guard let source = request["source"] as? String else {
        return errorResponse(kind: "bad_request", message: "probe_parse requires 'source'")
    }
    let tree = Parser.parse(source: source)
    let hasErrors = tree.hasError
    return okResponse(["has_errors": hasErrors])
}

// MARK: - op: insert_child

/// Inserts `child_source` into `source` at a position determined by
/// parent_name_path / anchor_name_path / position. Parent semantics:
///   - parent_name_path == nil: root source file.
///   - parent_name_path != nil: into the named symbol's body.
/// Position semantics match the StructuralLanguage contract: "end",
/// "start", "before"/"after" (with anchor).
func opInsertChild(_ request: [String: Any]) -> Data {
    guard let source = request["source"] as? String,
          let childSource = request["child_source"] as? String,
          let position = request["position"] as? String else {
        return errorResponse(kind: "bad_request", message: "insert_child requires source/child_source/position")
    }
    let parentPath = request["parent_name_path"] as? String
    let anchorPath = request["anchor_name_path"] as? String
    let tree = Parser.parse(source: source)

    // resolve the byte window in which the child must go
    let (windowStart, windowEnd): (Int, Int)
    if let parentPath = parentPath {
        guard let parent = lookupSymbol(tree, namePath: parentPath) else {
            return errorResponse(kind: "symbol_missing", message: "parent \(parentPath) not found")
        }
        guard let body = parent.bodyRange else {
            return errorResponse(kind: "no_body", message: "parent \(parentPath) has no body")
        }
        windowStart = body.0
        windowEnd = body.1
    } else {
        windowStart = 0
        windowEnd = source.utf8.count
    }

    // pick the exact insertion offset based on position/anchor
    let offset: Int
    switch position {
    case "start":
        offset = windowStart
    case "end":
        offset = windowEnd
    case "before", "after":
        guard let anchorPath = anchorPath else {
            return errorResponse(kind: "bad_request", message: "position \(position) requires anchor")
        }
        guard let anchor = lookupSymbol(tree, namePath: anchorPath) else {
            return errorResponse(kind: "symbol_missing", message: "anchor \(anchorPath) not found")
        }
        offset = position == "before" ? anchor.extentOffset : anchor.extentOffset + anchor.extentLength
    default:
        return errorResponse(kind: "bad_request", message: "invalid position: \(position)")
    }

    // ensure a trailing newline between adjacent decls (matches C++ discipline)
    let normalized = childSource.hasSuffix("\n") ? childSource : childSource + "\n"
    let edited = replaceByteRange(source, offset: offset, length: 0, replacement: normalized)
    return okResponse(["source": edited])
}

// MARK: - op: remove_child

func opRemoveChild(_ request: [String: Any]) -> Data {
    guard let source = request["source"] as? String,
          let childPath = request["child_name_path"] as? String else {
        return errorResponse(kind: "bad_request", message: "remove_child requires source/child_name_path")
    }
    let tree = Parser.parse(source: source)
    guard let sym = lookupSymbol(tree, namePath: childPath) else {
        return errorResponse(kind: "symbol_missing", message: "child \(childPath) not found")
    }
    let edited = replaceByteRange(source, offset: sym.extentOffset, length: sym.extentLength, replacement: "")
    return okResponse(["source": edited])
}

// MARK: - op: apply_replacement

func opApplyReplacement(_ request: [String: Any]) -> Data {
    guard let source = request["source"] as? String,
          let offset = request["match_offset"] as? Int,
          let length = request["match_length"] as? Int,
          let replacement = request["replacement_source"] as? String else {
        return errorResponse(kind: "bad_request", message: "apply_replacement requires source/match_offset/match_length/replacement_source")
    }
    let edited = replaceByteRange(source, offset: offset, length: length, replacement: replacement)
    return okResponse(["source": edited])
}

// MARK: - op: find_matches

/// Simple pattern matcher: the pattern source is parsed as a SourceFile,
/// and we collect its top-level statement/decl/expression. We then walk the
/// input tree and compare each node's structure to the pattern's first
/// meaningful node, allowing `$name` tokens in the pattern to bind any
/// subtree. Mirrors the scope of the C++ backend's matcher — enough for
/// single-node structural matches with simple captures.
func opFindMatches(_ request: [String: Any]) -> Data {
    guard let source = request["source"] as? String,
          let patternSource = request["pattern_source"] as? String else {
        return errorResponse(kind: "bad_request", message: "find_matches requires source/pattern_source")
    }
    let scopePath = request["scope_name_path"] as? String
    let tree = Parser.parse(source: source)
    guard let patternRoot = extractPatternRoot(from: patternSource) else {
        return errorResponse(kind: "pattern_parse", message: "pattern did not produce a matchable node")
    }

    // scope restriction
    let matches: [(Syntax, [String: Syntax])]
    if let scopePath = scopePath {
        guard let scopeSym = lookupSymbol(tree, namePath: scopePath) else {
            return errorResponse(kind: "symbol_missing", message: "scope \(scopePath) not found")
        }
        let scopeSyntax = enclosingSyntax(in: tree, offset: scopeSym.extentOffset, length: scopeSym.extentLength)
        matches = collectMatches(in: scopeSyntax ?? Syntax(tree), pattern: patternRoot)
    } else {
        matches = collectMatches(in: Syntax(tree), pattern: patternRoot)
    }

    let payload = matches.map { (node, bindings) -> [String: Any] in
        let offset = node.position.utf8Offset
        let length = node.totalLength.utf8Length
        var bindingDicts: [String: Any] = [:]
        for (name, bNode) in bindings {
            bindingDicts[name] = [
                "source": String(bNode.trimmedDescription),
                "extent_offset": bNode.position.utf8Offset,
                "extent_length": bNode.totalLength.utf8Length,
            ]
        }
        return [
            "extent_offset": offset,
            "extent_length": length,
            "bindings": bindingDicts,
        ]
    }
    return okResponse(["matches": payload])
}

/// Parses the pattern source and returns the single most-specific node to
/// match against: the first DeclSyntax if one exists, else the first
/// statement's expression, else nil.
func extractPatternRoot(from patternSource: String) -> Syntax? {
    let parsed = Parser.parse(source: patternSource)
    // prefer a single decl
    let decls = parsed.statements.compactMap { $0.item.as(DeclSyntax.self) }
    if decls.count == 1 { return Syntax(decls[0]) }
    // else a single statement
    if parsed.statements.count == 1, let first = parsed.statements.first {
        return Syntax(first.item)
    }
    return nil
}

/// Returns the smallest Syntax node covering the byte range, used when
/// restricting matches to a given scope.
func enclosingSyntax(in root: SourceFileSyntax, offset: Int, length: Int) -> Syntax? {
    let targetEnd = offset + length
    var best: Syntax? = nil
    var stack: [Syntax] = [Syntax(root)]
    while let top = stack.popLast() {
        let s = top.position.utf8Offset
        let e = s + top.totalLength.utf8Length
        if s <= offset && e >= targetEnd {
            best = top
            for child in top.children(viewMode: .sourceAccurate) {
                stack.append(child)
            }
        }
    }
    return best
}

/// Walks `root`, returning every (node, bindings) pair where `node`
/// matches `pattern`.
func collectMatches(in root: Syntax, pattern: Syntax) -> [(Syntax, [String: Syntax])] {
    var out: [(Syntax, [String: Syntax])] = []
    var stack: [Syntax] = [root]
    while let top = stack.popLast() {
        var bindings: [String: Syntax] = [:]
        if matchSyntax(pattern: pattern, candidate: top, bindings: &bindings) {
            out.append((top, bindings))
        }
        for child in top.children(viewMode: .sourceAccurate) {
            stack.append(child)
        }
    }
    return out
}

/// Structural comparison: if `pattern` is a simple identifier starting with
/// `$`, it matches any `candidate` and binds. Otherwise compare kinds and
/// recurse into matching-arity children.
func matchSyntax(pattern: Syntax, candidate: Syntax, bindings: inout [String: Syntax]) -> Bool {
    if let tok = pattern.as(TokenSyntax.self), tok.text.hasPrefix("$"), tok.text.count > 1 {
        let name = String(tok.text.dropFirst())
        if name == "_" { return true }
        if let existing = bindings[name] {
            return existing.trimmedDescription == candidate.trimmedDescription
        }
        bindings[name] = candidate
        return true
    }

    // same syntax kind?
    if pattern.kind != candidate.kind { return false }

    // token vs token: compare literal text
    if let pt = pattern.as(TokenSyntax.self), let ct = candidate.as(TokenSyntax.self) {
        return pt.text == ct.text
    }

    // compare children pairwise
    let pChildren = Array(pattern.children(viewMode: .sourceAccurate))
    let cChildren = Array(candidate.children(viewMode: .sourceAccurate))
    if pChildren.count != cChildren.count { return false }
    for (p, c) in zip(pChildren, cChildren) {
        if !matchSyntax(pattern: p, candidate: c, bindings: &bindings) {
            return false
        }
    }
    return true
}

// MARK: - byte-offset rewriter

/// Replaces a UTF-8 byte range in `source` with `replacement`. Works at the
/// byte level to stay compatible with SwiftSyntax offsets (which are UTF-8
/// byte offsets, not character offsets).
func replaceByteRange(_ source: String, offset: Int, length: Int, replacement: String) -> String {
    let bytes = Array(source.utf8)
    let before = Array(bytes[0..<offset])
    let after = Array(bytes[(offset + length)..<bytes.count])
    let replacementBytes = Array(replacement.utf8)
    return String(decoding: before + replacementBytes + after, as: UTF8.self)
}

// MARK: - main loop

let stdin = FileHandle.standardInput
let stdout = FileHandle.standardOutput

while let frame = readFrame(from: stdin) {
    if frame.isEmpty { continue }
    guard let raw = try? JSONSerialization.jsonObject(with: frame),
          let request = raw as? [String: Any],
          let op = request["op"] as? String else {
        writeFrame(errorResponse(kind: "bad_request", message: "malformed request"), to: stdout)
        continue
    }

    let response: Data
    switch op {
    case "walk_symbols":
        response = opWalkSymbols(request)
    case "insert_child":
        response = opInsertChild(request)
    case "remove_child":
        response = opRemoveChild(request)
    case "find_matches":
        response = opFindMatches(request)
    case "apply_replacement":
        response = opApplyReplacement(request)
    case "probe_parse":
        response = opProbeParse(request)
    case "shutdown":
        writeFrame(okResponse(), to: stdout)
        exit(0)
    default:
        response = errorResponse(kind: "unknown_op", message: "unknown op: \(op)")
    }
    writeFrame(response, to: stdout)
}
