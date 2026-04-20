// serena-rust-bridge
//
// Length-prefixed JSON request/response loop over stdio. Each request is a
// 4-byte little-endian length followed by UTF-8 JSON; each response has the
// same framing. Stateless: every request carries the source text it
// operates on, so there are no cross-call handles to invalidate.
//
// Operations mirror the Swift / TypeScript / Go bridges:
//   - walk_symbols       : (kind, name_path, extent_offset, extent_length, body_range)
//   - insert_child       : new source with a rendered child inserted
//   - remove_child       : new source with a named child excised
//   - find_matches       : match extents + captured bindings
//   - apply_replacement  : new source with a byte range replaced
//   - probe_parse        : whether the source has parse errors
//   - shutdown           : exit cleanly
//
// All offsets on the wire are UTF-8 byte offsets. proc-macro2's
// `Span::byte_range` (stable in the fallback compiler context used here)
// already yields byte offsets, so no conversion is needed.

use proc_macro2::Span;
use quote::ToTokens;
use serde_json::{json, Value};
use std::io::{self, Read, Write};
use syn::spanned::Spanned;

// ---------------------------------------------------------------------------
// I/O framing
// ---------------------------------------------------------------------------

fn read_frame<R: Read>(r: &mut R) -> io::Result<Option<Vec<u8>>> {
    let mut len_buf = [0u8; 4];
    match r.read_exact(&mut len_buf) {
        Ok(()) => {}
        Err(e) if e.kind() == io::ErrorKind::UnexpectedEof => return Ok(None),
        Err(e) => return Err(e),
    }
    let len = u32::from_le_bytes(len_buf) as usize;
    if len == 0 {
        return Ok(Some(Vec::new()));
    }
    let mut buf = vec![0u8; len];
    r.read_exact(&mut buf)?;
    Ok(Some(buf))
}

fn write_frame<W: Write>(w: &mut W, payload: &[u8]) -> io::Result<()> {
    let len = (payload.len() as u32).to_le_bytes();
    w.write_all(&len)?;
    w.write_all(payload)?;
    w.flush()?;
    Ok(())
}

fn write_response<W: Write>(w: &mut W, v: &Value) {
    let body = serde_json::to_vec(v).unwrap_or_else(|_| {
        br#"{"ok":false,"error_kind":"bridge","message":"json marshal failed"}"#.to_vec()
    });
    let _ = write_frame(w, &body);
}

fn write_error<W: Write>(w: &mut W, kind: &str, message: &str) {
    write_response(
        w,
        &json!({
            "ok": false,
            "error_kind": kind,
            "message": message,
        }),
    );
}

// ---------------------------------------------------------------------------
// Span -> byte range
// ---------------------------------------------------------------------------

/// Return the UTF-8 byte range `[start, end)` of `span` in the original
/// source text. Uses `proc_macro2::Span::byte_range`, which is stable in
/// fallback (non-proc-macro) contexts.
fn span_byte_range(span: Span) -> (usize, usize) {
    let r = span.byte_range();
    (r.start, r.end)
}

/// Extend `end` past whitespace up to and including one line break so
/// "after anchor" inserts land on the next line, matching the Swift /
/// TypeScript / Go convention.
fn extend_through_trailing_newline(source: &str, mut end: usize) -> usize {
    let bytes = source.as_bytes();
    while end < bytes.len() {
        let c = bytes[end];
        if c == b' ' || c == b'\t' {
            end += 1;
            continue;
        }
        if c == b'\r' {
            if end + 1 < bytes.len() && bytes[end + 1] == b'\n' {
                return end + 2;
            }
            return end + 1;
        }
        if c == b'\n' {
            return end + 1;
        }
        break;
    }
    end
}

// ---------------------------------------------------------------------------
// Symbol walking
// ---------------------------------------------------------------------------

#[derive(Clone, Debug)]
struct SymbolEntry {
    kind: String,
    name_path: String,
    extent_offset: usize,
    extent_length: usize,
}

/// Parse `source` into a `syn::File`. Returns `None` for empty source or
/// unparseable input. syn bails on the first parse error, so unlike
/// go/parser this is not error-tolerant — but that matches proc-macro2's
/// behaviour when Rust itself refuses to tokenise.
fn parse_source(source: &str) -> Option<syn::File> {
    if source.is_empty() {
        return None;
    }
    syn::parse_file(source).ok()
}

/// Render a `syn::Type` back to its source spelling. Used for impl `self_ty`
/// and impl `trait_` paths in name-path construction.
fn render_type(ty: &syn::Type) -> String {
    ty.to_token_stream().to_string().split_whitespace().collect::<Vec<_>>().join(" ")
}

/// Render a `syn::Path` back to its source spelling. `quote!` emits
/// whitespace around `::`, so we collapse whitespace to produce a
/// canonical dotted form that matches user intuition.
fn render_path(path: &syn::Path) -> String {
    let raw = path.to_token_stream().to_string();
    raw.replace(' ', "")
}

/// Expand a `UseTree` into a list of leaf paths, joined by `+` to mirror
/// Go's grouped-import convention. Aliases are dropped — the canonical
/// path is what matters for the name.
fn expand_use_tree(tree: &syn::UseTree, prefix: &str) -> Vec<String> {
    use syn::UseTree;
    match tree {
        UseTree::Path(p) => {
            let seg = p.ident.to_string();
            let new_prefix = if prefix.is_empty() {
                seg
            } else {
                format!("{}::{}", prefix, seg)
            };
            expand_use_tree(&p.tree, &new_prefix)
        }
        UseTree::Name(n) => {
            let seg = n.ident.to_string();
            let joined = if prefix.is_empty() {
                seg
            } else {
                format!("{}::{}", prefix, seg)
            };
            vec![joined]
        }
        UseTree::Rename(r) => {
            let seg = r.ident.to_string();
            let joined = if prefix.is_empty() {
                seg
            } else {
                format!("{}::{}", prefix, seg)
            };
            vec![joined]
        }
        UseTree::Glob(_) => {
            let joined = if prefix.is_empty() {
                "*".to_string()
            } else {
                format!("{}::*", prefix)
            };
            vec![joined]
        }
        UseTree::Group(g) => g
            .items
            .iter()
            .flat_map(|t| expand_use_tree(t, prefix))
            .collect(),
    }
}

/// Build the name_path for an `impl` block. Conventions:
///   - inherent `impl Type`              → "impl Type"
///   - trait impl `impl Trait for Type`  → "impl Trait for Type"
///
/// The leading "impl " avoids collisions with a struct / enum / trait
/// named `Type` (which walks under name_path "Type"). Methods inside an
/// impl compose as "impl Type/method_name" or
/// "impl Trait for Type/method_name".
fn impl_name_path(item_impl: &syn::ItemImpl) -> String {
    let self_ty = render_type(&item_impl.self_ty);
    if let Some((_, trait_path, _)) = &item_impl.trait_ {
        format!("impl {} for {}", render_path(trait_path), self_ty)
    } else {
        format!("impl {}", self_ty)
    }
}

/// Walk `file`'s top-level items plus methods inside `impl` blocks. Extents
/// are byte ranges in `source`, extended through one trailing newline so
/// whole-line removals do not leave orphan blank lines.
fn walk_symbols(file: &syn::File, source: &str) -> Vec<SymbolEntry> {
    let mut out: Vec<SymbolEntry> = Vec::new();
    for item in &file.items {
        if let Some((kind, name)) = top_level_kind_and_name(item) {
            let (start, end_raw) = span_byte_range(item.span());
            let end = extend_through_trailing_newline(source, end_raw);
            out.push(SymbolEntry {
                kind: kind.to_string(),
                name_path: name,
                extent_offset: start,
                extent_length: end - start,
            });
            // Recurse into impl blocks to enumerate methods.
            if let syn::Item::Impl(item_impl) = item {
                let parent = impl_name_path(item_impl);
                for impl_item in &item_impl.items {
                    if let syn::ImplItem::Fn(method) = impl_item {
                        let method_name = method.sig.ident.to_string();
                        let (m_start, m_end_raw) = span_byte_range(method.span());
                        let m_end = extend_through_trailing_newline(source, m_end_raw);
                        out.push(SymbolEntry {
                            kind: "method".to_string(),
                            name_path: format!("{}/{}", parent, method_name),
                            extent_offset: m_start,
                            extent_length: m_end - m_start,
                        });
                    }
                }
            }
        }
    }
    out
}

fn top_level_kind_and_name(item: &syn::Item) -> Option<(&'static str, String)> {
    match item {
        syn::Item::Use(u) => {
            let paths = expand_use_tree(&u.tree, "");
            if paths.is_empty() {
                return None;
            }
            Some(("use", paths.join("+")))
        }
        syn::Item::Mod(m) => Some(("mod", m.ident.to_string())),
        syn::Item::Const(c) => Some(("const", c.ident.to_string())),
        syn::Item::Static(s) => Some(("static", s.ident.to_string())),
        syn::Item::Type(t) => Some(("type", t.ident.to_string())),
        syn::Item::Fn(f) => Some(("fn", f.sig.ident.to_string())),
        syn::Item::Struct(s) => Some(("struct", s.ident.to_string())),
        syn::Item::Enum(e) => Some(("enum", e.ident.to_string())),
        syn::Item::Trait(t) => Some(("trait", t.ident.to_string())),
        syn::Item::Impl(i) => Some(("impl", impl_name_path(i))),
        // Items we do not expose in v1:
        //   ExternCrate, ForeignMod, Macro, TraitAlias, Union, Verbatim, non-exhaustive
        _ => None,
    }
}

fn lookup_symbol(file: &syn::File, source: &str, name_path: &str) -> Option<SymbolEntry> {
    walk_symbols(file, source)
        .into_iter()
        .find(|e| e.name_path == name_path)
}

// ---------------------------------------------------------------------------
// ops: walk_symbols / probe_parse
// ---------------------------------------------------------------------------

fn op_walk_symbols<W: Write>(w: &mut W, req: &Value) {
    let source = match req.get("source").and_then(|v| v.as_str()) {
        Some(s) => s,
        None => {
            write_error(w, "bad_request", "walk_symbols requires 'source'");
            return;
        }
    };
    let mut payload: Vec<Value> = Vec::new();
    if let Some(file) = parse_source(source) {
        for e in walk_symbols(&file, source) {
            payload.push(json!({
                "kind": e.kind,
                "name_path": e.name_path,
                "extent_offset": e.extent_offset,
                "extent_length": e.extent_length,
                "body_range": Value::Null,
            }));
        }
    }
    write_response(w, &json!({"ok": true, "symbols": payload}));
}

fn op_probe_parse<W: Write>(w: &mut W, req: &Value) {
    let source = match req.get("source").and_then(|v| v.as_str()) {
        Some(s) => s,
        None => {
            write_error(w, "bad_request", "probe_parse requires 'source'");
            return;
        }
    };
    let has_errors = if source.is_empty() {
        false
    } else {
        syn::parse_file(source).is_err()
    };
    write_response(w, &json!({"ok": true, "has_errors": has_errors}));
}

// ---------------------------------------------------------------------------
// op: insert_child
// ---------------------------------------------------------------------------

fn op_insert_child<W: Write>(w: &mut W, req: &Value) {
    let source = match req.get("source").and_then(|v| v.as_str()) {
        Some(s) => s,
        None => {
            write_error(w, "bad_request", "insert_child requires 'source'");
            return;
        }
    };
    let child_source = match req.get("child_source").and_then(|v| v.as_str()) {
        Some(s) => s,
        None => {
            write_error(w, "bad_request", "insert_child requires 'child_source'");
            return;
        }
    };
    let position = match req.get("position").and_then(|v| v.as_str()) {
        Some(s) => s,
        None => {
            write_error(w, "bad_request", "insert_child requires 'position'");
            return;
        }
    };
    let has_parent = !matches!(req.get("parent_name_path"), None | Some(Value::Null));
    let parent_path_raw = req
        .get("parent_name_path")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let has_anchor = !matches!(req.get("anchor_name_path"), None | Some(Value::Null));
    let anchor_path_raw = req
        .get("anchor_name_path")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();

    if has_parent {
        // v1: we do not expose Rust body ranges, so nested insertion is
        // not supported. Agents insert at tree level and use an anchor
        // for nested-style placement.
        write_error(
            w,
            "no_body",
            &format!(
                "parent {} has no body (rust bridge does not expose body ranges in v1)",
                parent_path_raw
            ),
        );
        return;
    }

    let window_end = source.len();
    let offset = match position {
        "start" => 0usize,
        "end" => window_end,
        "before" | "after" => {
            if !has_anchor {
                write_error(
                    w,
                    "bad_request",
                    &format!("position {} requires anchor", position),
                );
                return;
            }
            let file = match parse_source(source) {
                Some(f) => f,
                None => {
                    write_error(
                        w,
                        "symbol_missing",
                        &format!(
                            "anchor {} not found (source did not parse)",
                            anchor_path_raw
                        ),
                    );
                    return;
                }
            };
            let anchor = match lookup_symbol(&file, source, &anchor_path_raw) {
                Some(a) => a,
                None => {
                    write_error(
                        w,
                        "symbol_missing",
                        &format!("anchor {} not found", anchor_path_raw),
                    );
                    return;
                }
            };
            if position == "before" {
                anchor.extent_offset
            } else {
                anchor.extent_offset + anchor.extent_length
            }
        }
        _ => {
            write_error(w, "bad_request", &format!("invalid position: {}", position));
            return;
        }
    };

    let normalized = if child_source.ends_with('\n') {
        child_source.to_string()
    } else {
        format!("{}\n", child_source)
    };
    let edited = format!(
        "{}{}{}",
        &source[..offset],
        normalized,
        &source[offset..]
    );
    write_response(w, &json!({"ok": true, "source": edited}));
}

// ---------------------------------------------------------------------------
// op: remove_child
// ---------------------------------------------------------------------------

fn op_remove_child<W: Write>(w: &mut W, req: &Value) {
    let source = match req.get("source").and_then(|v| v.as_str()) {
        Some(s) => s,
        None => {
            write_error(w, "bad_request", "remove_child requires 'source'");
            return;
        }
    };
    let child_path = match req.get("child_name_path").and_then(|v| v.as_str()) {
        Some(s) => s,
        None => {
            write_error(w, "bad_request", "remove_child requires 'child_name_path'");
            return;
        }
    };
    let file = match parse_source(source) {
        Some(f) => f,
        None => {
            write_error(
                w,
                "symbol_missing",
                &format!("child {} not found (source did not parse)", child_path),
            );
            return;
        }
    };
    let sym = match lookup_symbol(&file, source, child_path) {
        Some(s) => s,
        None => {
            write_error(
                w,
                "symbol_missing",
                &format!("child {} not found", child_path),
            );
            return;
        }
    };
    let edited = format!(
        "{}{}",
        &source[..sym.extent_offset],
        &source[sym.extent_offset + sym.extent_length..]
    );
    write_response(w, &json!({"ok": true, "source": edited}));
}

// ---------------------------------------------------------------------------
// op: apply_replacement
// ---------------------------------------------------------------------------

fn op_apply_replacement<W: Write>(w: &mut W, req: &Value) {
    let source = match req.get("source").and_then(|v| v.as_str()) {
        Some(s) => s,
        None => {
            write_error(w, "bad_request", "apply_replacement requires 'source'");
            return;
        }
    };
    let replacement = match req.get("replacement_source").and_then(|v| v.as_str()) {
        Some(s) => s,
        None => {
            write_error(
                w,
                "bad_request",
                "apply_replacement requires 'replacement_source'",
            );
            return;
        }
    };
    let match_offset = match req.get("match_offset").and_then(|v| v.as_u64()) {
        Some(n) => n as usize,
        None => {
            write_error(w, "bad_request", "apply_replacement requires 'match_offset'");
            return;
        }
    };
    let match_length = match req.get("match_length").and_then(|v| v.as_u64()) {
        Some(n) => n as usize,
        None => {
            write_error(w, "bad_request", "apply_replacement requires 'match_length'");
            return;
        }
    };
    if match_offset + match_length > source.len() {
        write_error(w, "bad_request", "match range out of source bounds");
        return;
    }
    let edited = format!(
        "{}{}{}",
        &source[..match_offset],
        replacement,
        &source[match_offset + match_length..]
    );
    write_response(w, &json!({"ok": true, "source": edited}));
}

// ---------------------------------------------------------------------------
// op: find_matches
// ---------------------------------------------------------------------------
//
// Pattern grammar: the Rust surface syntax plus `$name` capture sigils and
// `$_` wildcard. Sigils are preprocessed to `__capture_<name>` before
// parsing (Rust identifiers cannot contain `$`), then stripped back during
// structural comparison.

const CAPTURE_PREFIX: &str = "__capture_";

/// Rewrite every `$name` / `$_` sigil in `src` to an identifier of the
/// form `__capture_<name>`. The rewrite is token-dumb — it operates on
/// the raw text — because the parser has not seen the source yet.
fn preprocess_pattern(src: &str) -> String {
    let bytes = src.as_bytes();
    let mut out = String::with_capacity(src.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'$' && i + 1 < bytes.len() {
            let next = bytes[i + 1];
            let is_ident_start = next == b'_' || next.is_ascii_alphabetic();
            if is_ident_start {
                // consume the identifier
                let mut j = i + 1;
                while j < bytes.len() {
                    let c = bytes[j];
                    if c == b'_' || c.is_ascii_alphanumeric() {
                        j += 1;
                    } else {
                        break;
                    }
                }
                // reject bare `$` alone
                if j > i + 1 {
                    out.push_str(CAPTURE_PREFIX);
                    out.push_str(&src[i + 1..j]);
                    i = j;
                    continue;
                }
            }
        }
        // SAFETY: indexing into a UTF-8 string by byte — we resync at each
        // `$`, so ASCII handling above is correct, and we copy multi-byte
        // chars through one char at a time below.
        let ch = src[i..].chars().next().unwrap();
        out.push(ch);
        i += ch.len_utf8();
    }
    out
}

/// Pattern root kind after parsing. syn parses at one of three grammar
/// points: a top-level `Item`, a block `Stmt`, or a bare `Expr`. We try
/// each in order of decreasing breadth; v1 only uses the expression form
/// when actually matching, but accepting item/stmt patterns at compile
/// time lets agents iterate on pattern shape without surprise errors.
enum PatternRoot {
    Item,
    Stmt,
    Expr(Box<syn::Expr>),
}

fn parse_pattern(src: &str) -> Result<PatternRoot, String> {
    // try 1: top-level item
    if syn::parse_str::<syn::Item>(src).is_ok() {
        return Ok(PatternRoot::Item);
    }
    // try 2: single statement
    if syn::parse_str::<syn::Stmt>(src).is_ok() {
        return Ok(PatternRoot::Stmt);
    }
    // try 3: bare expression
    if let Ok(expr) = syn::parse_str::<syn::Expr>(src) {
        return Ok(PatternRoot::Expr(Box::new(expr)));
    }
    Err(format!("could not parse pattern: {}", src))
}

/// Return the raw source text slice covered by `span`, falling back to the
/// span's rendered token stream when bounds are invalid.
fn span_text(span: Span, source: &str) -> String {
    let (start, end) = span_byte_range(span);
    if start <= end && end <= source.len() {
        source[start..end].to_string()
    } else {
        String::new()
    }
}

/// Return the raw source text for an `syn::Expr`, as a Rust source slice.
fn expr_text(expr: &syn::Expr, source: &str) -> String {
    let (start, end) = span_byte_range(expr.span());
    if start <= end && end <= source.len() {
        source[start..end].to_string()
    } else {
        expr.to_token_stream().to_string()
    }
}

/// Capture name if `expr` is a bare path of the form `__capture_<name>`.
fn capture_name_from_expr(expr: &syn::Expr) -> Option<String> {
    if let syn::Expr::Path(p) = expr {
        if p.qself.is_none() && p.path.segments.len() == 1 {
            let seg = &p.path.segments[0];
            if seg.arguments.is_none() {
                let name = seg.ident.to_string();
                if let Some(stripped) = name.strip_prefix(CAPTURE_PREFIX) {
                    return Some(stripped.to_string());
                }
            }
        }
    }
    None
}

fn match_expr(
    pat: &syn::Expr,
    cand: &syn::Expr,
    source: &str,
    bindings: &mut std::collections::HashMap<String, String>,
) -> bool {
    // Capture sigil: a bare path `__capture_<name>` matches any expression.
    if let Some(name) = capture_name_from_expr(pat) {
        if name == "_" {
            return true;
        }
        let text = expr_text(cand, source);
        if let Some(existing) = bindings.get(&name) {
            return existing == &text;
        }
        bindings.insert(name, text);
        return true;
    }
    match (pat, cand) {
        (syn::Expr::Call(pc), syn::Expr::Call(cc)) => {
            if !match_expr(&pc.func, &cc.func, source, bindings) {
                return false;
            }
            if pc.args.len() != cc.args.len() {
                return false;
            }
            for (pa, ca) in pc.args.iter().zip(cc.args.iter()) {
                if !match_expr(pa, ca, source, bindings) {
                    return false;
                }
            }
            true
        }
        (syn::Expr::MethodCall(pm), syn::Expr::MethodCall(cm)) => {
            if !match_expr(&pm.receiver, &cm.receiver, source, bindings) {
                return false;
            }
            if pm.method != cm.method {
                return false;
            }
            if pm.args.len() != cm.args.len() {
                return false;
            }
            for (pa, ca) in pm.args.iter().zip(cm.args.iter()) {
                if !match_expr(pa, ca, source, bindings) {
                    return false;
                }
            }
            true
        }
        (syn::Expr::Path(pp), syn::Expr::Path(cp)) => {
            // fall back to textual equality on the rendered path
            render_path(&pp.path) == render_path(&cp.path) && pp.qself.is_none() && cp.qself.is_none()
        }
        (syn::Expr::Lit(pl), syn::Expr::Lit(cl)) => pl.lit.to_token_stream().to_string() == cl.lit.to_token_stream().to_string(),
        (syn::Expr::Binary(pb), syn::Expr::Binary(cb)) => {
            std::mem::discriminant(&pb.op) == std::mem::discriminant(&cb.op)
                && match_expr(&pb.left, &cb.left, source, bindings)
                && match_expr(&pb.right, &cb.right, source, bindings)
        }
        (syn::Expr::Unary(pu), syn::Expr::Unary(cu)) => {
            std::mem::discriminant(&pu.op) == std::mem::discriminant(&cu.op)
                && match_expr(&pu.expr, &cu.expr, source, bindings)
        }
        (syn::Expr::Paren(pp), _) => match_expr(&pp.expr, cand, source, bindings),
        (_, syn::Expr::Paren(cp)) => match_expr(pat, &cp.expr, source, bindings),
        (syn::Expr::Field(pf), syn::Expr::Field(cf)) => {
            match_expr(&pf.base, &cf.base, source, bindings)
                && pf.member.to_token_stream().to_string() == cf.member.to_token_stream().to_string()
        }
        // Fallback: textual equality on the rendered token stream. This
        // catches leaf kinds we have not special-cased above.
        _ => pat.to_token_stream().to_string() == cand.to_token_stream().to_string(),
    }
}

fn collect_exprs<'a>(source: &'a str, file: &'a syn::File) -> Vec<&'a syn::Expr> {
    struct ExprVisitor<'a> {
        out: Vec<&'a syn::Expr>,
    }
    impl<'a> syn::visit::Visit<'a> for ExprVisitor<'a> {
        fn visit_expr(&mut self, node: &'a syn::Expr) {
            self.out.push(node);
            syn::visit::visit_expr(self, node);
        }
    }
    let mut visitor = ExprVisitor { out: Vec::new() };
    syn::visit::Visit::visit_file(&mut visitor, file);
    let _ = source;
    visitor.out
}

fn op_find_matches<W: Write>(w: &mut W, req: &Value) {
    let source = match req.get("source").and_then(|v| v.as_str()) {
        Some(s) => s,
        None => {
            write_error(w, "bad_request", "find_matches requires 'source'");
            return;
        }
    };
    let pattern_source = match req.get("pattern_source").and_then(|v| v.as_str()) {
        Some(s) => s,
        None => {
            write_error(w, "bad_request", "find_matches requires 'pattern_source'");
            return;
        }
    };
    // scope is accepted but ignored in v1.
    let _ = req.get("scope_name_path");

    let processed = preprocess_pattern(pattern_source);
    let pat_root = match parse_pattern(&processed) {
        Ok(p) => p,
        Err(e) => {
            write_error(w, "pattern_parse", &e);
            return;
        }
    };

    let file = match parse_source(source) {
        Some(f) => f,
        None => {
            write_response(w, &json!({"ok": true, "matches": []}));
            return;
        }
    };

    let mut matches: Vec<Value> = Vec::new();
    let exprs = collect_exprs(source, &file);

    // v1: only expression-level patterns participate in find_matches.
    // Item/stmt patterns accept the compile but return no matches. This
    // mirrors the Swift/TypeScript smoke-level pattern contract.
    if let PatternRoot::Expr(pat_expr) = &pat_root {
        for cand in exprs {
            let mut bindings: std::collections::HashMap<String, String> =
                std::collections::HashMap::new();
            if match_expr(pat_expr, cand, source, &mut bindings) {
                let (start, end) = span_byte_range(cand.span());
                if end < start {
                    continue;
                }
                let mut bm = serde_json::Map::new();
                for (k, v) in &bindings {
                    bm.insert(
                        k.clone(),
                        json!({
                            "source": v,
                            "extent_offset": 0,
                            "extent_length": v.len(),
                        }),
                    );
                }
                matches.push(json!({
                    "extent_offset": start,
                    "extent_length": end - start,
                    "bindings": Value::Object(bm),
                }));
            }
        }
    }
    // Item/Stmt patterns compile successfully but return no matches in v1.

    // De-duplicate matches that share the same (offset, length) — nested
    // expressions can match themselves and their parents for trivial
    // patterns.
    matches.sort_by(|a, b| {
        let ao = a.get("extent_offset").and_then(|v| v.as_u64()).unwrap_or(0);
        let bo = b.get("extent_offset").and_then(|v| v.as_u64()).unwrap_or(0);
        ao.cmp(&bo)
    });
    let _ = span_text; // suppress unused-warning in some build configs

    write_response(w, &json!({"ok": true, "matches": matches}));
}

// ---------------------------------------------------------------------------
// Dispatch
// ---------------------------------------------------------------------------

fn handle<W: Write>(w: &mut W, req: &Value) {
    let op = req.get("op").and_then(|v| v.as_str()).unwrap_or("");
    match op {
        "walk_symbols" => op_walk_symbols(w, req),
        "insert_child" => op_insert_child(w, req),
        "remove_child" => op_remove_child(w, req),
        "find_matches" => op_find_matches(w, req),
        "apply_replacement" => op_apply_replacement(w, req),
        "probe_parse" => op_probe_parse(w, req),
        "shutdown" => {
            write_response(w, &json!({"ok": true}));
            std::process::exit(0);
        }
        _ => write_error(w, "unknown_op", &format!("unknown op: {}", op)),
    }
}

fn main() {
    let stdin = io::stdin();
    let stdout = io::stdout();
    let mut stdin_lock = stdin.lock();
    let mut stdout_lock = stdout.lock();

    loop {
        let frame = match read_frame(&mut stdin_lock) {
            Ok(Some(f)) => f,
            Ok(None) => return, // EOF
            Err(e) => {
                write_error(
                    &mut stdout_lock,
                    "bad_request",
                    &format!("frame read: {}", e),
                );
                continue;
            }
        };
        if frame.is_empty() {
            continue;
        }
        let req: Value = match serde_json::from_slice(&frame) {
            Ok(v) => v,
            Err(e) => {
                write_error(
                    &mut stdout_lock,
                    "bad_request",
                    &format!("malformed JSON: {}", e),
                );
                continue;
            }
        };
        // guard against panics mid-op
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            handle(&mut stdout_lock, &req);
        }));
        if result.is_err() {
            write_error(&mut stdout_lock, "bridge", "panic");
        }
    }
}
