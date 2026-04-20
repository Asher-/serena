// serena-go-bridge
//
// Length-prefixed JSON request/response loop over stdio. Each request is a
// 4-byte little-endian length followed by UTF-8 JSON; each response has the
// same framing. Stateless: every request carries the source text it
// operates on, so there are no cross-call handles to invalidate.
//
// Operations mirror the Swift / TypeScript bridges:
//   - walk_symbols       : (kind, name_path, extent_offset, extent_length, body_range)
//   - insert_child       : new source with a rendered child inserted
//   - remove_child       : new source with a named child excised
//   - find_matches       : match extents + captured bindings
//   - apply_replacement  : new source with a byte range replaced
//   - probe_parse        : whether the source has parse errors
//   - shutdown           : exit cleanly
//
// All offsets on the wire are UTF-8 byte offsets. Go's token.FileSet
// already tracks byte offsets, so no conversion is needed.

package main

import (
	"bufio"
	"bytes"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"go/ast"
	"go/parser"
	"go/printer"
	"go/token"
	"io"
	"os"
	"reflect"
	"regexp"
	"strings"
)

// ---------------------------------------------------------------------------
// I/O framing
// ---------------------------------------------------------------------------

var stdinReader = bufio.NewReader(os.Stdin)
var stdoutWriter io.Writer = os.Stdout

func readFrame() ([]byte, error) {
	var prefix [4]byte
	if _, err := io.ReadFull(stdinReader, prefix[:]); err != nil {
		return nil, err
	}
	length := binary.LittleEndian.Uint32(prefix[:])
	if length == 0 {
		return []byte{}, nil
	}
	buf := make([]byte, length)
	if _, err := io.ReadFull(stdinReader, buf); err != nil {
		return nil, err
	}
	return buf, nil
}

func writeFrame(payload []byte) {
	var prefix [4]byte
	binary.LittleEndian.PutUint32(prefix[:], uint32(len(payload)))
	stdoutWriter.Write(prefix[:])
	stdoutWriter.Write(payload)
}

func writeResponse(v interface{}) {
	buf, err := json.Marshal(v)
	if err != nil {
		writeFrame([]byte(`{"ok":false,"error_kind":"bridge","message":"json marshal failed"}`))
		return
	}
	writeFrame(buf)
}

func writeError(kind, message string) {
	writeResponse(map[string]interface{}{
		"ok":         false,
		"error_kind": kind,
		"message":    message,
	})
}

// ---------------------------------------------------------------------------
// Symbol walking
// ---------------------------------------------------------------------------

type symbolEntry struct {
	kind         string
	namePath     string
	extentOffset int
	extentLength int
	bodyRange    []int
}

// parseSource parses the source into an *ast.File, tolerating errors.
// Returns nil if the source is empty or unparseable to any degree.
func parseSource(source string) (*token.FileSet, *ast.File) {
	if source == "" {
		return nil, nil
	}
	fset := token.NewFileSet()
	file, _ := parser.ParseFile(fset, "input.go", source, parser.ParseComments|parser.AllErrors)
	return fset, file
}

func walkSymbols(file *ast.File, fset *token.FileSet, source string) []symbolEntry {
	out := make([]symbolEntry, 0, len(file.Decls)+1)
	// package clause
	if file.Package.IsValid() && file.Name != nil {
		start := fset.Position(file.Package).Offset
		end := fset.Position(file.Name.End()).Offset
		end = extendThroughTrailingNewline(source, end)
		out = append(out, symbolEntry{
			kind:         "package",
			namePath:     file.Name.Name,
			extentOffset: start,
			extentLength: end - start,
		})
	}
	// top-level decls
	for _, decl := range file.Decls {
		kind, name := topLevelKindAndName(decl)
		if kind == "" || name == "" {
			continue
		}
		start := fset.Position(decl.Pos()).Offset
		end := fset.Position(decl.End()).Offset
		end = extendThroughTrailingNewline(source, end)
		out = append(out, symbolEntry{
			kind:         kind,
			namePath:     name,
			extentOffset: start,
			extentLength: end - start,
		})
	}
	return out
}

func topLevelKindAndName(decl ast.Decl) (string, string) {
	switch d := decl.(type) {
	case *ast.GenDecl:
		switch d.Tok {
		case token.IMPORT:
			return "import", joinImportNames(d)
		case token.CONST:
			return "const", joinValueNames(d)
		case token.VAR:
			return "variable", joinValueNames(d)
		case token.TYPE:
			return "type", joinTypeNames(d)
		}
	case *ast.FuncDecl:
		if d.Recv == nil {
			if d.Name == nil {
				return "", ""
			}
			return "function", d.Name.Name
		}
		recvType := receiverTypeName(d.Recv)
		if recvType == "" || d.Name == nil {
			return "", ""
		}
		return "method", recvType + "/" + d.Name.Name
	}
	return "", ""
}

func joinImportNames(d *ast.GenDecl) string {
	var names []string
	for _, s := range d.Specs {
		spec, ok := s.(*ast.ImportSpec)
		if !ok || spec.Path == nil {
			continue
		}
		path := spec.Path.Value
		if len(path) >= 2 && (path[0] == '"' || path[0] == '`') {
			path = path[1 : len(path)-1]
		}
		names = append(names, path)
	}
	return strings.Join(names, "+")
}

func joinValueNames(d *ast.GenDecl) string {
	var names []string
	for _, s := range d.Specs {
		spec, ok := s.(*ast.ValueSpec)
		if !ok {
			continue
		}
		for _, n := range spec.Names {
			if n != nil {
				names = append(names, n.Name)
			}
		}
	}
	return strings.Join(names, "+")
}

func joinTypeNames(d *ast.GenDecl) string {
	var names []string
	for _, s := range d.Specs {
		spec, ok := s.(*ast.TypeSpec)
		if !ok || spec.Name == nil {
			continue
		}
		names = append(names, spec.Name.Name)
	}
	return strings.Join(names, "+")
}

func receiverTypeName(recv *ast.FieldList) string {
	if recv == nil || len(recv.List) == 0 {
		return ""
	}
	return unwrapReceiverType(recv.List[0].Type)
}

func unwrapReceiverType(t ast.Expr) string {
	switch e := t.(type) {
	case *ast.Ident:
		return e.Name
	case *ast.StarExpr:
		return unwrapReceiverType(e.X)
	case *ast.IndexExpr:
		return unwrapReceiverType(e.X)
	case *ast.IndexListExpr:
		return unwrapReceiverType(e.X)
	}
	return ""
}

// extendThroughTrailingNewline advances end past whitespace up to one line
// break so "after anchor" inserts land on the next line, matching the
// Swift / TypeScript convention.
func extendThroughTrailingNewline(source string, end int) int {
	for end < len(source) {
		c := source[end]
		if c == ' ' || c == '\t' {
			end++
			continue
		}
		if c == '\r' {
			if end+1 < len(source) && source[end+1] == '\n' {
				return end + 2
			}
			return end + 1
		}
		if c == '\n' {
			return end + 1
		}
		break
	}
	return end
}

func lookupSymbol(file *ast.File, fset *token.FileSet, source, namePath string) *symbolEntry {
	entries := walkSymbols(file, fset, source)
	for i := range entries {
		if entries[i].namePath == namePath {
			return &entries[i]
		}
	}
	return nil
}

// ---------------------------------------------------------------------------
// ops: walk_symbols / probe_parse
// ---------------------------------------------------------------------------

func opWalkSymbols(req map[string]interface{}) {
	source, ok := req["source"].(string)
	if !ok {
		writeError("bad_request", "walk_symbols requires 'source'")
		return
	}
	fset, file := parseSource(source)
	payload := make([]map[string]interface{}, 0)
	if file != nil {
		for _, e := range walkSymbols(file, fset, source) {
			var bodyRange interface{}
			if e.bodyRange != nil {
				bodyRange = e.bodyRange
			}
			payload = append(payload, map[string]interface{}{
				"kind":          e.kind,
				"name_path":     e.namePath,
				"extent_offset": e.extentOffset,
				"extent_length": e.extentLength,
				"body_range":    bodyRange,
			})
		}
	}
	writeResponse(map[string]interface{}{"ok": true, "symbols": payload})
}

func opProbeParse(req map[string]interface{}) {
	source, ok := req["source"].(string)
	if !ok {
		writeError("bad_request", "probe_parse requires 'source'")
		return
	}
	fset := token.NewFileSet()
	_, err := parser.ParseFile(fset, "input.go", source, parser.ParseComments|parser.AllErrors)
	writeResponse(map[string]interface{}{"ok": true, "has_errors": err != nil})
}

// ---------------------------------------------------------------------------
// op: insert_child
// ---------------------------------------------------------------------------

func opInsertChild(req map[string]interface{}) {
	source, ok1 := req["source"].(string)
	childSource, ok2 := req["child_source"].(string)
	position, ok3 := req["position"].(string)
	if !ok1 || !ok2 || !ok3 {
		writeError("bad_request", "insert_child requires source/child_source/position")
		return
	}
	parentPathRaw, hasParent := req["parent_name_path"].(string)
	if req["parent_name_path"] == nil {
		hasParent = false
	}
	anchorPathRaw, hasAnchor := req["anchor_name_path"].(string)
	if req["anchor_name_path"] == nil {
		hasAnchor = false
	}

	if hasParent {
		// v1: we do not expose Go body ranges, so nested insertion is
		// not supported. Agents insert at tree level and use an anchor
		// for nested-style placement.
		writeError("no_body", "parent "+parentPathRaw+" has no body (go bridge does not expose body ranges in v1)")
		return
	}

	fset, file := parseSource(source)
	var windowEnd int
	if source == "" {
		windowEnd = 0
	} else {
		windowEnd = len(source)
	}

	var offset int
	switch position {
	case "start":
		offset = 0
	case "end":
		offset = windowEnd
	case "before", "after":
		if !hasAnchor {
			writeError("bad_request", "position "+position+" requires anchor")
			return
		}
		if file == nil {
			writeError("symbol_missing", "anchor "+anchorPathRaw+" not found (source did not parse)")
			return
		}
		anchor := lookupSymbol(file, fset, source, anchorPathRaw)
		if anchor == nil {
			writeError("symbol_missing", "anchor "+anchorPathRaw+" not found")
			return
		}
		if position == "before" {
			offset = anchor.extentOffset
		} else {
			offset = anchor.extentOffset + anchor.extentLength
		}
	default:
		writeError("bad_request", "invalid position: "+position)
		return
	}

	normalized := childSource
	if !strings.HasSuffix(normalized, "\n") {
		normalized += "\n"
	}
	edited := source[:offset] + normalized + source[offset:]
	writeResponse(map[string]interface{}{"ok": true, "source": edited})
}

// ---------------------------------------------------------------------------
// op: remove_child
// ---------------------------------------------------------------------------

func opRemoveChild(req map[string]interface{}) {
	source, ok1 := req["source"].(string)
	childPath, ok2 := req["child_name_path"].(string)
	if !ok1 || !ok2 {
		writeError("bad_request", "remove_child requires source/child_name_path")
		return
	}
	fset, file := parseSource(source)
	if file == nil {
		writeError("symbol_missing", "child "+childPath+" not found (source did not parse)")
		return
	}
	sym := lookupSymbol(file, fset, source, childPath)
	if sym == nil {
		writeError("symbol_missing", "child "+childPath+" not found")
		return
	}
	edited := source[:sym.extentOffset] + source[sym.extentOffset+sym.extentLength:]
	writeResponse(map[string]interface{}{"ok": true, "source": edited})
}

// ---------------------------------------------------------------------------
// op: apply_replacement
// ---------------------------------------------------------------------------

func opApplyReplacement(req map[string]interface{}) {
	source, ok1 := req["source"].(string)
	replacement, ok2 := req["replacement_source"].(string)
	matchOffsetF, ok3 := req["match_offset"].(float64)
	matchLengthF, ok4 := req["match_length"].(float64)
	if !ok1 || !ok2 || !ok3 || !ok4 {
		writeError("bad_request", "apply_replacement requires source/match_offset/match_length/replacement_source")
		return
	}
	matchOffset := int(matchOffsetF)
	matchLength := int(matchLengthF)
	if matchOffset < 0 || matchLength < 0 || matchOffset+matchLength > len(source) {
		writeError("bad_request", "match range out of source bounds")
		return
	}
	edited := source[:matchOffset] + replacement + source[matchOffset+matchLength:]
	writeResponse(map[string]interface{}{"ok": true, "source": edited})
}

// ---------------------------------------------------------------------------
// op: find_matches
// ---------------------------------------------------------------------------

const capturePrefix = "__capture_"

var captureSigilRE = regexp.MustCompile(`\$([A-Za-z_][A-Za-z_0-9]*|_)`)

func preprocessPattern(src string) string {
	return captureSigilRE.ReplaceAllStringFunc(src, func(m string) string {
		// m starts with '$'
		return capturePrefix + m[1:]
	})
}

// parsePattern tries several wrappings, in order of decreasing breadth:
//  1. file-level:     "package __p\n<src>"   → use first Decl
//  2. statement-level: "package __p\nfunc __f() {\n<src>\n}" → use first Stmt
//  3. expression-level: parser.ParseExprFrom(<src>)
//
// Returns the root pattern node (ast.Node interface) or an error.
func parsePattern(src string) (ast.Node, error) {
	// try 1: top-level decl
	wrapped1 := "package __p\n" + src
	if !strings.HasSuffix(wrapped1, "\n") {
		wrapped1 += "\n"
	}
	fset1 := token.NewFileSet()
	file1, err1 := parser.ParseFile(fset1, "pattern.go", wrapped1, parser.ParseComments)
	if err1 == nil && file1 != nil && len(file1.Decls) == 1 {
		return file1.Decls[0], nil
	}
	// try 2: single statement inside a function
	wrapped2 := "package __p\nfunc __f() {\n" + src + "\n}\n"
	fset2 := token.NewFileSet()
	file2, err2 := parser.ParseFile(fset2, "pattern.go", wrapped2, parser.ParseComments)
	if err2 == nil && file2 != nil && len(file2.Decls) == 1 {
		if fn, ok := file2.Decls[0].(*ast.FuncDecl); ok && fn.Body != nil && len(fn.Body.List) == 1 {
			stmt := fn.Body.List[0]
			if exprStmt, ok := stmt.(*ast.ExprStmt); ok {
				return exprStmt.X, nil
			}
			return stmt, nil
		}
	}
	// try 3: bare expression
	fset3 := token.NewFileSet()
	expr, err3 := parser.ParseExprFrom(fset3, "pattern.go", src, parser.ParseComments)
	if err3 == nil && expr != nil {
		return expr, nil
	}
	return nil, fmt.Errorf("could not parse pattern: decl err %v; stmt err %v; expr err %v", err1, err2, err3)
}

func opFindMatches(req map[string]interface{}) {
	source, ok1 := req["source"].(string)
	patternSource, ok2 := req["pattern_source"].(string)
	if !ok1 || !ok2 {
		writeError("bad_request", "find_matches requires source/pattern_source")
		return
	}
	scopePath, hasScope := req["scope_name_path"].(string)
	if req["scope_name_path"] == nil {
		hasScope = false
	}
	_ = scopePath

	processed := preprocessPattern(patternSource)
	patRoot, err := parsePattern(processed)
	if err != nil {
		writeError("pattern_parse", err.Error())
		return
	}

	candFset, candFile := parseSource(source)
	matches := make([]map[string]interface{}, 0)
	if candFile == nil {
		writeResponse(map[string]interface{}{"ok": true, "matches": matches})
		return
	}

	var scopeNode ast.Node = candFile
	if hasScope {
		// scope support is not wired in v1 — always search whole tree
		scopeNode = candFile
	}

	ast.Inspect(scopeNode, func(n ast.Node) bool {
		if n == nil {
			return false
		}
		bindings := map[string]string{}
		if matchNode(patRoot, n, candFset, source, bindings) {
			start := candFset.Position(n.Pos()).Offset
			end := candFset.Position(n.End()).Offset
			// skip zero-length pseudo-matches
			if end < start {
				return true
			}
			bm := make(map[string]interface{}, len(bindings))
			for k, v := range bindings {
				bm[k] = map[string]interface{}{
					"source":        v,
					"extent_offset": 0,
					"extent_length": len(v),
				}
			}
			matches = append(matches, map[string]interface{}{
				"extent_offset": start,
				"extent_length": end - start,
				"bindings":      bm,
			})
		}
		return true
	})
	writeResponse(map[string]interface{}{"ok": true, "matches": matches})
}

// ---------------------------------------------------------------------------
// Structural matching
// ---------------------------------------------------------------------------

var tokenPosType = reflect.TypeOf(token.NoPos)

// skipFieldName lists struct-field names that are comments / semantic
// metadata we ignore when structurally comparing nodes. Position fields
// are already dropped by type check (token.Pos).
var skipFieldNames = map[string]bool{
	"Obj":        true, // *ast.Object on Ident (deprecated)
	"Doc":        true, // *CommentGroup leading comments
	"Comment":    true, // *CommentGroup trailing comments
	"Comments":   true, // []*CommentGroup on File
	"Scope":      true, // *Scope on File
	"Imports":    true, // []*ImportSpec on File (duplicates Decls)
	"Unresolved": true, // []*Ident on File
	"GoVersion":  true, // File go-version annotation
}

func matchNode(pat, cand ast.Node, candFset *token.FileSet, candSource string, bindings map[string]string) bool {
	if pat == nil && cand == nil {
		return true
	}
	if pat == nil || cand == nil {
		return false
	}
	// capture sigil: a pattern Ident named __capture_<name>
	if id, ok := pat.(*ast.Ident); ok && strings.HasPrefix(id.Name, capturePrefix) {
		name := strings.TrimPrefix(id.Name, capturePrefix)
		if name == "_" {
			return true
		}
		ct := nodeText(cand, candFset, candSource)
		if existing, has := bindings[name]; has {
			return existing == ct
		}
		bindings[name] = ct
		return true
	}
	// type check
	if reflect.TypeOf(pat) != reflect.TypeOf(cand) {
		return false
	}
	return matchStructFields(
		reflect.ValueOf(pat).Elem(),
		reflect.ValueOf(cand).Elem(),
		candFset, candSource, bindings,
	)
}

func matchStructFields(pv, cv reflect.Value, candFset *token.FileSet, candSource string, bindings map[string]string) bool {
	for i := 0; i < pv.NumField(); i++ {
		fieldType := pv.Type().Field(i)
		name := fieldType.Name
		if skipFieldNames[name] {
			continue
		}
		if !matchValue(pv.Field(i), cv.Field(i), candFset, candSource, bindings) {
			return false
		}
	}
	return true
}

func matchValue(pv, cv reflect.Value, candFset *token.FileSet, candSource string, bindings map[string]string) bool {
	// drop any field whose type is token.Pos (position metadata)
	if pv.Type() == tokenPosType {
		return true
	}
	switch pv.Kind() {
	case reflect.Ptr:
		if pv.IsNil() && cv.IsNil() {
			return true
		}
		if pv.IsNil() || cv.IsNil() {
			return false
		}
		pn, ok1 := pv.Interface().(ast.Node)
		cn, ok2 := cv.Interface().(ast.Node)
		if ok1 && ok2 {
			return matchNode(pn, cn, candFset, candSource, bindings)
		}
		return reflect.DeepEqual(pv.Interface(), cv.Interface())
	case reflect.Interface:
		if pv.IsNil() && cv.IsNil() {
			return true
		}
		if pv.IsNil() || cv.IsNil() {
			return false
		}
		pn, ok1 := pv.Interface().(ast.Node)
		cn, ok2 := cv.Interface().(ast.Node)
		if ok1 && ok2 {
			return matchNode(pn, cn, candFset, candSource, bindings)
		}
		return reflect.DeepEqual(pv.Interface(), cv.Interface())
	case reflect.Slice:
		if pv.Len() != cv.Len() {
			return false
		}
		for i := 0; i < pv.Len(); i++ {
			if !matchValue(pv.Index(i), cv.Index(i), candFset, candSource, bindings) {
				return false
			}
		}
		return true
	case reflect.String, reflect.Bool,
		reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64,
		reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64:
		return pv.Interface() == cv.Interface()
	case reflect.Struct:
		return reflect.DeepEqual(pv.Interface(), cv.Interface())
	default:
		return reflect.DeepEqual(pv.Interface(), cv.Interface())
	}
}

func nodeText(n ast.Node, fset *token.FileSet, source string) string {
	if n == nil {
		return ""
	}
	start := fset.Position(n.Pos()).Offset
	end := fset.Position(n.End()).Offset
	if start >= 0 && end >= start && end <= len(source) {
		return source[start:end]
	}
	// fallback: pretty-print
	var buf bytes.Buffer
	printer.Fprint(&buf, fset, n)
	return buf.String()
}

// ---------------------------------------------------------------------------
// Dispatch
// ---------------------------------------------------------------------------

func handle(req map[string]interface{}) {
	op, _ := req["op"].(string)
	defer func() {
		if r := recover(); r != nil {
			writeError("bridge", fmt.Sprintf("panic: %v", r))
		}
	}()
	switch op {
	case "walk_symbols":
		opWalkSymbols(req)
	case "insert_child":
		opInsertChild(req)
	case "remove_child":
		opRemoveChild(req)
	case "find_matches":
		opFindMatches(req)
	case "apply_replacement":
		opApplyReplacement(req)
	case "probe_parse":
		opProbeParse(req)
	case "shutdown":
		writeResponse(map[string]interface{}{"ok": true})
		os.Exit(0)
	default:
		writeError("unknown_op", "unknown op: "+op)
	}
}

func main() {
	for {
		frame, err := readFrame()
		if err == io.EOF {
			return
		}
		if err != nil {
			writeError("bad_request", "frame read: "+err.Error())
			continue
		}
		if len(frame) == 0 {
			continue
		}
		var req map[string]interface{}
		if err := json.Unmarshal(frame, &req); err != nil {
			writeError("bad_request", "malformed JSON: "+err.Error())
			continue
		}
		handle(req)
	}
}
