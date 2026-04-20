package com.serena;

import com.github.javaparser.JavaParser;
import com.github.javaparser.ParseResult;
import com.github.javaparser.ParserConfiguration;
import com.github.javaparser.Position;
import com.github.javaparser.Range;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.ImportDeclaration;
import com.github.javaparser.ast.Node;
import com.github.javaparser.ast.PackageDeclaration;
import com.github.javaparser.ast.body.AnnotationDeclaration;
import com.github.javaparser.ast.body.BodyDeclaration;
import com.github.javaparser.ast.body.ClassOrInterfaceDeclaration;
import com.github.javaparser.ast.body.ConstructorDeclaration;
import com.github.javaparser.ast.body.EnumDeclaration;
import com.github.javaparser.ast.body.FieldDeclaration;
import com.github.javaparser.ast.body.MethodDeclaration;
import com.github.javaparser.ast.body.Parameter;
import com.github.javaparser.ast.body.RecordDeclaration;
import com.github.javaparser.ast.body.TypeDeclaration;
import com.github.javaparser.ast.body.VariableDeclarator;
import com.github.javaparser.ast.expr.Expression;
import com.github.javaparser.ast.expr.MethodCallExpr;
import com.github.javaparser.ast.expr.NameExpr;
import com.github.javaparser.ast.expr.ObjectCreationExpr;
import com.github.javaparser.ast.expr.BinaryExpr;
import com.github.javaparser.ast.expr.UnaryExpr;
import com.github.javaparser.ast.expr.LiteralExpr;
import com.github.javaparser.ast.expr.EnclosedExpr;
import com.github.javaparser.ast.expr.FieldAccessExpr;
import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;

import java.io.DataInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * serena-java-bridge
 *
 * Length-prefixed JSON request/response loop over stdio. Each request is a
 * 4-byte little-endian length followed by UTF-8 JSON; each response has the
 * same framing. Stateless: every request carries the source text it
 * operates on, so there are no cross-call handles to invalidate.
 *
 * Operations mirror the Swift / TypeScript / Go / Rust bridges:
 *   - walk_symbols       : (kind, name_path, extent_offset, extent_length, body_range)
 *   - insert_child       : new source with a rendered child inserted
 *   - remove_child       : new source with a named child excised
 *   - find_matches       : match extents + captured bindings
 *   - apply_replacement  : new source with a byte range replaced
 *   - probe_parse        : whether the source has parse errors
 *   - shutdown           : exit cleanly
 *
 * All offsets on the wire are UTF-8 byte offsets. JavaParser reports
 * positions as (line, column) pairs (1-indexed, char-based), so the
 * bridge converts via a per-request ByteOffsetMapper.
 */
public final class Bridge {

    private static final String CAPTURE_PREFIX = "__capture_";

    // -----------------------------------------------------------------------
    // Entry point
    // -----------------------------------------------------------------------

    public static void main(String[] args) throws IOException {
        InputStream in = System.in;
        OutputStream out = System.out;
        DataInputStream din = new DataInputStream(in);
        while (true) {
            byte[] frame = readFrame(din);
            if (frame == null) {
                return; // EOF
            }
            if (frame.length == 0) {
                continue;
            }
            JsonObject req;
            try {
                req = JsonParser.parseString(new String(frame, StandardCharsets.UTF_8))
                        .getAsJsonObject();
            } catch (Exception e) {
                writeError(out, "bad_request", "malformed JSON: " + e.getMessage());
                continue;
            }
            try {
                handle(out, req);
            } catch (Throwable t) {
                String msg = t.getClass().getSimpleName() + ": " + t.getMessage();
                writeError(out, "bridge", msg);
            }
        }
    }

    private static void handle(OutputStream out, JsonObject req) throws IOException {
        String op = stringOr(req, "op", "");
        switch (op) {
            case "walk_symbols":
                opWalkSymbols(out, req);
                return;
            case "insert_child":
                opInsertChild(out, req);
                return;
            case "remove_child":
                opRemoveChild(out, req);
                return;
            case "find_matches":
                opFindMatches(out, req);
                return;
            case "apply_replacement":
                opApplyReplacement(out, req);
                return;
            case "probe_parse":
                opProbeParse(out, req);
                return;
            case "shutdown":
                JsonObject ok = new JsonObject();
                ok.addProperty("ok", true);
                writeResponse(out, ok);
                System.exit(0);
                return;
            default:
                writeError(out, "unknown_op", "unknown op: " + op);
        }
    }

    // -----------------------------------------------------------------------
    // I/O framing
    // -----------------------------------------------------------------------

    private static byte[] readFrame(DataInputStream in) throws IOException {
        byte[] lenBuf = new byte[4];
        int read = 0;
        while (read < 4) {
            int n = in.read(lenBuf, read, 4 - read);
            if (n == -1) {
                if (read == 0) {
                    return null;
                }
                throw new IOException("truncated frame length");
            }
            read += n;
        }
        int len = ByteBuffer.wrap(lenBuf).order(ByteOrder.LITTLE_ENDIAN).getInt();
        if (len == 0) {
            return new byte[0];
        }
        if (len < 0) {
            throw new IOException("negative frame length");
        }
        byte[] body = new byte[len];
        int total = 0;
        while (total < len) {
            int n = in.read(body, total, len - total);
            if (n == -1) {
                throw new IOException("truncated frame body");
            }
            total += n;
        }
        return body;
    }

    private static void writeFrame(OutputStream out, byte[] payload) throws IOException {
        byte[] prefix = ByteBuffer.allocate(4)
                .order(ByteOrder.LITTLE_ENDIAN)
                .putInt(payload.length)
                .array();
        out.write(prefix);
        out.write(payload);
        out.flush();
    }

    private static void writeResponse(OutputStream out, JsonObject obj) throws IOException {
        byte[] body = obj.toString().getBytes(StandardCharsets.UTF_8);
        writeFrame(out, body);
    }

    private static void writeError(OutputStream out, String kind, String message) throws IOException {
        JsonObject err = new JsonObject();
        err.addProperty("ok", false);
        err.addProperty("error_kind", kind);
        err.addProperty("message", message);
        writeResponse(out, err);
    }

    // -----------------------------------------------------------------------
    // JSON helpers
    // -----------------------------------------------------------------------

    private static String stringOr(JsonObject obj, String key, String def) {
        JsonElement el = obj.get(key);
        if (el == null || el.isJsonNull() || !el.isJsonPrimitive() || !el.getAsJsonPrimitive().isString()) {
            return def;
        }
        return el.getAsString();
    }

    private static String requireString(JsonObject obj, String key) {
        JsonElement el = obj.get(key);
        if (el == null || el.isJsonNull() || !el.isJsonPrimitive() || !el.getAsJsonPrimitive().isString()) {
            throw new IllegalArgumentException("missing string: " + key);
        }
        return el.getAsString();
    }

    private static String optionalString(JsonObject obj, String key) {
        JsonElement el = obj.get(key);
        if (el == null || el.isJsonNull()) {
            return null;
        }
        if (!el.isJsonPrimitive() || !el.getAsJsonPrimitive().isString()) {
            return null;
        }
        return el.getAsString();
    }

    private static long requireLong(JsonObject obj, String key) {
        JsonElement el = obj.get(key);
        if (el == null || el.isJsonNull() || !el.isJsonPrimitive() || !el.getAsJsonPrimitive().isNumber()) {
            throw new IllegalArgumentException("missing number: " + key);
        }
        return el.getAsLong();
    }

    // -----------------------------------------------------------------------
    // Byte offset mapping
    // -----------------------------------------------------------------------

    /**
     * Per-request mapper from (line, column) → UTF-8 byte offset, and from
     * UTF-16 char offset → UTF-8 byte offset. Built in one pass over the
     * source string.
     */
    private static final class ByteOffsetMapper {
        final String source;
        final int[] lineStartCharOffsets; // 1-indexed; [0] unused sentinel
        final int[] charToByte;           // length source.length() + 1

        ByteOffsetMapper(String source) {
            this.source = source;
            List<Integer> lineStarts = new ArrayList<>();
            lineStarts.add(-1);        // [0] unused
            lineStarts.add(0);         // line 1 starts at char 0
            int[] c2b = new int[source.length() + 1];
            int byteIdx = 0;
            int charIdx = 0;
            int len = source.length();
            while (charIdx < len) {
                c2b[charIdx] = byteIdx;
                int cp = source.codePointAt(charIdx);
                int utf8Len;
                if (cp < 0x80) utf8Len = 1;
                else if (cp < 0x800) utf8Len = 2;
                else if (cp < 0x10000) utf8Len = 3;
                else utf8Len = 4;
                byteIdx += utf8Len;
                int charStep = Character.charCount(cp);
                charIdx += charStep;
                if (cp == '\n') {
                    lineStarts.add(charIdx);
                }
            }
            c2b[len] = byteIdx;
            this.charToByte = c2b;
            int[] arr = new int[lineStarts.size()];
            for (int i = 0; i < lineStarts.size(); i++) {
                arr[i] = lineStarts.get(i);
            }
            this.lineStartCharOffsets = arr;
        }

        int charOffset(int line, int column) {
            if (line < 1 || line >= lineStartCharOffsets.length) {
                return source.length();
            }
            int start = lineStartCharOffsets[line];
            int charOffset = start;
            int col = 1;
            int len = source.length();
            while (col < column && charOffset < len) {
                int cp = source.codePointAt(charOffset);
                if (cp == '\n') break;
                charOffset += Character.charCount(cp);
                col++;
            }
            return charOffset;
        }

        int byteOffsetOfChar(int charOffset) {
            if (charOffset < 0) return 0;
            if (charOffset >= charToByte.length) {
                return charToByte[charToByte.length - 1];
            }
            return charToByte[charOffset];
        }

        int beginByteOffset(Range range) {
            return byteOffsetOfChar(charOffset(range.begin.line, range.begin.column));
        }

        /**
         * JavaParser Range end positions are inclusive at both the line
         * and column level. Return the exclusive byte offset one code
         * point past the end.
         */
        int endByteOffsetExclusive(Range range) {
            int lastChar = charOffset(range.end.line, range.end.column);
            int len = source.length();
            if (lastChar >= len) return byteOffsetOfChar(len);
            int cp = source.codePointAt(lastChar);
            int endChar = lastChar + Character.charCount(cp);
            return byteOffsetOfChar(endChar);
        }

        /**
         * Extend `endByteExclusive` past whitespace up to and including
         * one line break, so "after anchor" inserts land on the next
         * line. Mirrors Rust/Go/Swift/TypeScript convention.
         */
        int extendThroughTrailingNewline(int endByteExclusive) {
            byte[] bytes = source.getBytes(StandardCharsets.UTF_8);
            int end = endByteExclusive;
            while (end < bytes.length) {
                byte c = bytes[end];
                if (c == ' ' || c == '\t') {
                    end++;
                    continue;
                }
                if (c == '\r') {
                    if (end + 1 < bytes.length && bytes[end + 1] == '\n') {
                        return end + 2;
                    }
                    return end + 1;
                }
                if (c == '\n') {
                    return end + 1;
                }
                break;
            }
            return end;
        }
    }

    // -----------------------------------------------------------------------
    // Parsing
    // -----------------------------------------------------------------------

    private static final JavaParser PARSER;
    static {
        ParserConfiguration config = new ParserConfiguration()
                .setLanguageLevel(ParserConfiguration.LanguageLevel.JAVA_21);
        PARSER = new JavaParser(config);
    }

    private static CompilationUnit parseSource(String source) {
        if (source.isEmpty()) {
            return null;
        }
        ParseResult<CompilationUnit> result = PARSER.parse(source);
        if (!result.isSuccessful() || result.getResult().isEmpty()) {
            return null;
        }
        return result.getResult().get();
    }

    // -----------------------------------------------------------------------
    // Symbol walking
    // -----------------------------------------------------------------------

    /** One entry in the walk_symbols output. */
    private static final class SymbolEntry {
        final String kind;
        final String namePath;
        final int extentOffset;
        final int extentLength;

        SymbolEntry(String kind, String namePath, int extentOffset, int extentLength) {
            this.kind = kind;
            this.namePath = namePath;
            this.extentOffset = extentOffset;
            this.extentLength = extentLength;
        }
    }

    private static List<SymbolEntry> walkSymbols(CompilationUnit cu, String source) {
        ByteOffsetMapper mapper = new ByteOffsetMapper(source);
        List<SymbolEntry> out = new ArrayList<>();

        // Package declaration (at most one).
        cu.getPackageDeclaration().ifPresent(pkg -> {
            pkg.getRange().ifPresent(r -> {
                int start = mapper.beginByteOffset(r);
                int end = mapper.extendThroughTrailingNewline(mapper.endByteOffsetExclusive(r));
                out.add(new SymbolEntry("package",
                        pkg.getNameAsString(), start, end - start));
            });
        });

        // Imports.
        for (ImportDeclaration imp : cu.getImports()) {
            imp.getRange().ifPresent(r -> {
                int start = mapper.beginByteOffset(r);
                int end = mapper.extendThroughTrailingNewline(mapper.endByteOffsetExclusive(r));
                String name = imp.getNameAsString();
                if (imp.isAsterisk()) {
                    name = name + ".*";
                }
                out.add(new SymbolEntry("import", name, start, end - start));
            });
        }

        // Top-level type declarations and their direct members (one layer).
        for (TypeDeclaration<?> typeDecl : cu.getTypes()) {
            walkTypeLevel(typeDecl, "", source, mapper, out);
        }
        return out;
    }

    /**
     * Add a type declaration (with `parentPath` prefix) and its direct
     * members to `out`. Nested types inside `typeDecl` are walked as
     * types only — their members are enumerated too, so an inner class's
     * methods show up with a `Outer/Inner/method(args)` path.
     */
    private static void walkTypeLevel(TypeDeclaration<?> typeDecl,
                                      String parentPath,
                                      String source,
                                      ByteOffsetMapper mapper,
                                      List<SymbolEntry> out) {
        if (typeDecl.getRange().isEmpty()) return;
        Range range = typeDecl.getRange().get();
        int start = mapper.beginByteOffset(range);
        int end = mapper.extendThroughTrailingNewline(mapper.endByteOffsetExclusive(range));
        String name = typeDecl.getNameAsString();
        String fullPath = parentPath.isEmpty() ? name : parentPath + "/" + name;
        String kind = typeKind(typeDecl);
        out.add(new SymbolEntry(kind, fullPath, start, end - start));

        for (BodyDeclaration<?> member : typeDecl.getMembers()) {
            if (member instanceof MethodDeclaration) {
                MethodDeclaration m = (MethodDeclaration) member;
                m.getRange().ifPresent(mr -> {
                    int ms = mapper.beginByteOffset(mr);
                    int me = mapper.extendThroughTrailingNewline(mapper.endByteOffsetExclusive(mr));
                    String mname = m.getNameAsString() + "(" + paramTypeString(m.getParameters()) + ")";
                    out.add(new SymbolEntry("method",
                            fullPath + "/" + mname, ms, me - ms));
                });
            } else if (member instanceof ConstructorDeclaration) {
                ConstructorDeclaration c = (ConstructorDeclaration) member;
                c.getRange().ifPresent(cr -> {
                    int cs = mapper.beginByteOffset(cr);
                    int ce = mapper.extendThroughTrailingNewline(mapper.endByteOffsetExclusive(cr));
                    String cname = c.getNameAsString() + "(" + paramTypeString(c.getParameters()) + ")";
                    out.add(new SymbolEntry("constructor",
                            fullPath + "/" + cname, cs, ce - cs));
                });
            } else if (member instanceof FieldDeclaration) {
                FieldDeclaration f = (FieldDeclaration) member;
                // A FieldDeclaration can declare multiple variables:
                // `int a, b;`. Emit one entry per variable, but use the
                // whole field range for the first so removal excises the
                // full statement. v1 keeps the per-variable addressing
                // for walk only.
                List<VariableDeclarator> vars = f.getVariables();
                for (int i = 0; i < vars.size(); i++) {
                    VariableDeclarator v = vars.get(i);
                    if (v.getRange().isEmpty()) continue;
                    Range fr = (i == 0 && f.getRange().isPresent()) ? f.getRange().get() : v.getRange().get();
                    int fs = mapper.beginByteOffset(fr);
                    int fe = mapper.extendThroughTrailingNewline(mapper.endByteOffsetExclusive(fr));
                    out.add(new SymbolEntry("field",
                            fullPath + "/" + v.getNameAsString(), fs, fe - fs));
                }
            } else if (member instanceof ClassOrInterfaceDeclaration
                    || member instanceof EnumDeclaration
                    || member instanceof RecordDeclaration
                    || member instanceof AnnotationDeclaration) {
                walkTypeLevel((TypeDeclaration<?>) member, fullPath, source, mapper, out);
            }
        }
    }

    private static String typeKind(TypeDeclaration<?> decl) {
        if (decl instanceof ClassOrInterfaceDeclaration) {
            return ((ClassOrInterfaceDeclaration) decl).isInterface() ? "interface" : "class";
        }
        if (decl instanceof EnumDeclaration) return "enum";
        if (decl instanceof RecordDeclaration) return "record";
        if (decl instanceof AnnotationDeclaration) return "annotation_type";
        return "class";
    }

    private static String paramTypeString(List<Parameter> params) {
        if (params.isEmpty()) return "";
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < params.size(); i++) {
            if (i > 0) sb.append(",");
            Parameter p = params.get(i);
            String t = p.getType().asString();
            if (p.isVarArgs()) t = t + "...";
            sb.append(t);
        }
        return sb.toString();
    }

    private static SymbolEntry lookupSymbol(CompilationUnit cu, String source, String namePath) {
        for (SymbolEntry e : walkSymbols(cu, source)) {
            if (e.namePath.equals(namePath)) {
                return e;
            }
        }
        return null;
    }

    // -----------------------------------------------------------------------
    // op: walk_symbols / probe_parse
    // -----------------------------------------------------------------------

    private static void opWalkSymbols(OutputStream out, JsonObject req) throws IOException {
        String source = requireString(req, "source");
        JsonArray arr = new JsonArray();
        CompilationUnit cu = parseSource(source);
        if (cu != null) {
            for (SymbolEntry e : walkSymbols(cu, source)) {
                JsonObject o = new JsonObject();
                o.addProperty("kind", e.kind);
                o.addProperty("name_path", e.namePath);
                o.addProperty("extent_offset", e.extentOffset);
                o.addProperty("extent_length", e.extentLength);
                o.add("body_range", com.google.gson.JsonNull.INSTANCE);
                arr.add(o);
            }
        }
        JsonObject resp = new JsonObject();
        resp.addProperty("ok", true);
        resp.add("symbols", arr);
        writeResponse(out, resp);
    }

    private static void opProbeParse(OutputStream out, JsonObject req) throws IOException {
        String source = requireString(req, "source");
        boolean hasErrors;
        if (source.isEmpty()) {
            hasErrors = false;
        } else {
            ParseResult<CompilationUnit> result = PARSER.parse(source);
            hasErrors = !result.isSuccessful();
        }
        JsonObject resp = new JsonObject();
        resp.addProperty("ok", true);
        resp.addProperty("has_errors", hasErrors);
        writeResponse(out, resp);
    }

    // -----------------------------------------------------------------------
    // op: insert_child
    // -----------------------------------------------------------------------

    private static void opInsertChild(OutputStream out, JsonObject req) throws IOException {
        String source = requireString(req, "source");
        String childSource = requireString(req, "child_source");
        String position = requireString(req, "position");
        String parentPathRaw = optionalString(req, "parent_name_path");
        String anchorPathRaw = optionalString(req, "anchor_name_path");

        if (parentPathRaw != null) {
            // v1: nested insertion is not supported (body ranges not exposed).
            writeError(out, "no_body",
                    "parent " + parentPathRaw
                            + " has no body (java bridge does not expose body ranges in v1)");
            return;
        }

        int windowEnd = source.getBytes(StandardCharsets.UTF_8).length;
        int offset;
        switch (position) {
            case "start":
                offset = 0;
                break;
            case "end":
                offset = windowEnd;
                break;
            case "before":
            case "after":
                if (anchorPathRaw == null) {
                    writeError(out, "bad_request", "position " + position + " requires anchor");
                    return;
                }
                CompilationUnit cu = parseSource(source);
                if (cu == null) {
                    writeError(out, "symbol_missing",
                            "anchor " + anchorPathRaw
                                    + " not found (source did not parse)");
                    return;
                }
                SymbolEntry anchor = lookupSymbol(cu, source, anchorPathRaw);
                if (anchor == null) {
                    writeError(out, "symbol_missing",
                            "anchor " + anchorPathRaw + " not found");
                    return;
                }
                if (position.equals("before")) {
                    offset = anchor.extentOffset;
                } else {
                    offset = anchor.extentOffset + anchor.extentLength;
                }
                break;
            default:
                writeError(out, "bad_request", "invalid position: " + position);
                return;
        }

        String normalized = childSource.endsWith("\n") ? childSource : childSource + "\n";
        byte[] sourceBytes = source.getBytes(StandardCharsets.UTF_8);
        byte[] insertBytes = normalized.getBytes(StandardCharsets.UTF_8);
        byte[] edited = new byte[sourceBytes.length + insertBytes.length];
        System.arraycopy(sourceBytes, 0, edited, 0, offset);
        System.arraycopy(insertBytes, 0, edited, offset, insertBytes.length);
        System.arraycopy(sourceBytes, offset, edited, offset + insertBytes.length,
                sourceBytes.length - offset);
        JsonObject resp = new JsonObject();
        resp.addProperty("ok", true);
        resp.addProperty("source", new String(edited, StandardCharsets.UTF_8));
        writeResponse(out, resp);
    }

    // -----------------------------------------------------------------------
    // op: remove_child
    // -----------------------------------------------------------------------

    private static void opRemoveChild(OutputStream out, JsonObject req) throws IOException {
        String source = requireString(req, "source");
        String childPath = requireString(req, "child_name_path");
        CompilationUnit cu = parseSource(source);
        if (cu == null) {
            writeError(out, "symbol_missing",
                    "child " + childPath + " not found (source did not parse)");
            return;
        }
        SymbolEntry sym = lookupSymbol(cu, source, childPath);
        if (sym == null) {
            writeError(out, "symbol_missing", "child " + childPath + " not found");
            return;
        }
        byte[] sourceBytes = source.getBytes(StandardCharsets.UTF_8);
        byte[] edited = new byte[sourceBytes.length - sym.extentLength];
        System.arraycopy(sourceBytes, 0, edited, 0, sym.extentOffset);
        System.arraycopy(sourceBytes, sym.extentOffset + sym.extentLength,
                edited, sym.extentOffset, sourceBytes.length - sym.extentOffset - sym.extentLength);
        JsonObject resp = new JsonObject();
        resp.addProperty("ok", true);
        resp.addProperty("source", new String(edited, StandardCharsets.UTF_8));
        writeResponse(out, resp);
    }

    // -----------------------------------------------------------------------
    // op: apply_replacement
    // -----------------------------------------------------------------------

    private static void opApplyReplacement(OutputStream out, JsonObject req) throws IOException {
        String source = requireString(req, "source");
        String replacement = requireString(req, "replacement_source");
        int matchOffset = (int) requireLong(req, "match_offset");
        int matchLength = (int) requireLong(req, "match_length");

        byte[] sourceBytes = source.getBytes(StandardCharsets.UTF_8);
        if (matchOffset < 0 || matchLength < 0 || matchOffset + matchLength > sourceBytes.length) {
            writeError(out, "bad_request", "match range out of source bounds");
            return;
        }
        byte[] repBytes = replacement.getBytes(StandardCharsets.UTF_8);
        byte[] edited = new byte[sourceBytes.length - matchLength + repBytes.length];
        System.arraycopy(sourceBytes, 0, edited, 0, matchOffset);
        System.arraycopy(repBytes, 0, edited, matchOffset, repBytes.length);
        System.arraycopy(sourceBytes, matchOffset + matchLength, edited,
                matchOffset + repBytes.length, sourceBytes.length - matchOffset - matchLength);
        JsonObject resp = new JsonObject();
        resp.addProperty("ok", true);
        resp.addProperty("source", new String(edited, StandardCharsets.UTF_8));
        writeResponse(out, resp);
    }

    // -----------------------------------------------------------------------
    // op: find_matches (expression-level structural match + $captures)
    // -----------------------------------------------------------------------

    /**
     * Rewrite every `$name` / `$_` sigil in `src` to an identifier of the
     * form `__capture_<name>`. Token-dumb because the parser has not seen
     * the source yet.
     */
    static String preprocessPattern(String src) {
        StringBuilder out = new StringBuilder(src.length());
        int i = 0;
        int n = src.length();
        while (i < n) {
            char c = src.charAt(i);
            if (c == '$' && i + 1 < n) {
                char next = src.charAt(i + 1);
                boolean isIdentStart = next == '_' || Character.isLetter(next);
                if (isIdentStart) {
                    int j = i + 1;
                    while (j < n) {
                        char cc = src.charAt(j);
                        if (cc == '_' || Character.isLetterOrDigit(cc)) {
                            j++;
                        } else {
                            break;
                        }
                    }
                    if (j > i + 1) {
                        out.append(CAPTURE_PREFIX);
                        out.append(src, i + 1, j);
                        i = j;
                        continue;
                    }
                }
            }
            out.append(c);
            i++;
        }
        return out.toString();
    }

    private static Expression parsePatternExpression(String src) {
        ParseResult<Expression> res = PARSER.parseExpression(src);
        if (res.isSuccessful() && res.getResult().isPresent()) {
            return res.getResult().get();
        }
        return null;
    }

    /** If `expr` is a bare name `__capture_<name>`, return `<name>`. */
    private static String captureNameOf(Expression expr) {
        if (expr instanceof NameExpr) {
            String name = ((NameExpr) expr).getNameAsString();
            if (name.startsWith(CAPTURE_PREFIX)) {
                return name.substring(CAPTURE_PREFIX.length());
            }
        }
        return null;
    }

    private static String exprText(Node node, String source, ByteOffsetMapper mapper) {
        if (node.getRange().isEmpty()) return node.toString();
        Range r = node.getRange().get();
        int start = mapper.beginByteOffset(r);
        int end = mapper.endByteOffsetExclusive(r);
        byte[] bytes = source.getBytes(StandardCharsets.UTF_8);
        if (start < 0 || end > bytes.length || start >= end) return node.toString();
        return new String(bytes, start, end - start, StandardCharsets.UTF_8);
    }

    private static boolean matchExpr(Expression pat, Expression cand,
                                     String source, ByteOffsetMapper mapper,
                                     Map<String, String> bindings) {
        String capName = captureNameOf(pat);
        if (capName != null) {
            if (capName.equals("_")) return true;
            String text = exprText(cand, source, mapper);
            String existing = bindings.get(capName);
            if (existing != null) {
                return existing.equals(text);
            }
            bindings.put(capName, text);
            return true;
        }
        // Paren passes through on either side.
        if (pat instanceof EnclosedExpr) {
            return matchExpr(((EnclosedExpr) pat).getInner(), cand, source, mapper, bindings);
        }
        if (cand instanceof EnclosedExpr) {
            return matchExpr(pat, ((EnclosedExpr) cand).getInner(), source, mapper, bindings);
        }
        if (pat instanceof MethodCallExpr && cand instanceof MethodCallExpr) {
            MethodCallExpr pm = (MethodCallExpr) pat;
            MethodCallExpr cm = (MethodCallExpr) cand;
            if (!pm.getNameAsString().equals(cm.getNameAsString())) return false;
            boolean psc = pm.getScope().isPresent();
            boolean csc = cm.getScope().isPresent();
            if (psc != csc) return false;
            if (psc && !matchExpr(pm.getScope().get(), cm.getScope().get(), source, mapper, bindings)) {
                return false;
            }
            if (pm.getArguments().size() != cm.getArguments().size()) return false;
            for (int i = 0; i < pm.getArguments().size(); i++) {
                if (!matchExpr(pm.getArguments().get(i), cm.getArguments().get(i),
                        source, mapper, bindings)) {
                    return false;
                }
            }
            return true;
        }
        if (pat instanceof ObjectCreationExpr && cand instanceof ObjectCreationExpr) {
            ObjectCreationExpr pc = (ObjectCreationExpr) pat;
            ObjectCreationExpr cc = (ObjectCreationExpr) cand;
            if (!pc.getType().asString().equals(cc.getType().asString())) return false;
            if (pc.getArguments().size() != cc.getArguments().size()) return false;
            for (int i = 0; i < pc.getArguments().size(); i++) {
                if (!matchExpr(pc.getArguments().get(i), cc.getArguments().get(i),
                        source, mapper, bindings)) {
                    return false;
                }
            }
            return true;
        }
        if (pat instanceof BinaryExpr && cand instanceof BinaryExpr) {
            BinaryExpr pb = (BinaryExpr) pat;
            BinaryExpr cb = (BinaryExpr) cand;
            if (pb.getOperator() != cb.getOperator()) return false;
            return matchExpr(pb.getLeft(), cb.getLeft(), source, mapper, bindings)
                    && matchExpr(pb.getRight(), cb.getRight(), source, mapper, bindings);
        }
        if (pat instanceof UnaryExpr && cand instanceof UnaryExpr) {
            UnaryExpr pu = (UnaryExpr) pat;
            UnaryExpr cu = (UnaryExpr) cand;
            if (pu.getOperator() != cu.getOperator()) return false;
            return matchExpr(pu.getExpression(), cu.getExpression(), source, mapper, bindings);
        }
        if (pat instanceof FieldAccessExpr && cand instanceof FieldAccessExpr) {
            FieldAccessExpr pf = (FieldAccessExpr) pat;
            FieldAccessExpr cf = (FieldAccessExpr) cand;
            if (!pf.getNameAsString().equals(cf.getNameAsString())) return false;
            return matchExpr(pf.getScope(), cf.getScope(), source, mapper, bindings);
        }
        if (pat instanceof LiteralExpr && cand instanceof LiteralExpr) {
            return pat.toString().equals(cand.toString());
        }
        if (pat instanceof NameExpr && cand instanceof NameExpr) {
            return ((NameExpr) pat).getNameAsString().equals(((NameExpr) cand).getNameAsString());
        }
        // Fallback: textual equality on the rendered form.
        return pat.toString().equals(cand.toString());
    }

    private static void collectExprs(Node root, List<Expression> out) {
        for (Node child : root.getChildNodes()) {
            if (child instanceof Expression) {
                out.add((Expression) child);
            }
            collectExprs(child, out);
        }
    }

    private static void opFindMatches(OutputStream out, JsonObject req) throws IOException {
        String source = requireString(req, "source");
        String patternSource = requireString(req, "pattern_source");
        // scope accepted but ignored in v1.

        String processed = preprocessPattern(patternSource);

        // Try to parse the pattern as Expression first; fall back silently on
        // failure for non-expression patterns (we accept the compile but
        // report no matches).
        Expression patExpr = parsePatternExpression(processed);
        if (patExpr == null) {
            // Attempt to detect whether the pattern is any valid Java
            // construct (Statement / BodyDeclaration / etc.) so we
            // distinguish "invalid pattern" from "non-expression pattern".
            ParseResult<com.github.javaparser.ast.stmt.Statement> stmtRes =
                    PARSER.parseStatement(processed);
            ParseResult<BodyDeclaration<?>> bdRes =
                    PARSER.parseBodyDeclaration(processed);
            if ((stmtRes.isSuccessful() && stmtRes.getResult().isPresent())
                    || (bdRes.isSuccessful() && bdRes.getResult().isPresent())) {
                // Accept but yield no matches in v1.
                JsonObject respOk = new JsonObject();
                respOk.addProperty("ok", true);
                respOk.add("matches", new JsonArray());
                writeResponse(out, respOk);
                return;
            }
            writeError(out, "pattern_parse", "could not parse pattern: " + patternSource);
            return;
        }

        CompilationUnit cu = parseSource(source);
        JsonArray matches = new JsonArray();
        if (cu != null) {
            ByteOffsetMapper mapper = new ByteOffsetMapper(source);
            List<Expression> exprs = new ArrayList<>();
            collectExprs(cu, exprs);
            for (Expression cand : exprs) {
                Map<String, String> bindings = new HashMap<>();
                if (matchExpr(patExpr, cand, source, mapper, bindings)
                        && cand.getRange().isPresent()) {
                    Range r = cand.getRange().get();
                    int start = mapper.beginByteOffset(r);
                    int end = mapper.endByteOffsetExclusive(r);
                    if (end <= start) continue;
                    JsonObject m = new JsonObject();
                    m.addProperty("extent_offset", start);
                    m.addProperty("extent_length", end - start);
                    JsonObject bm = new JsonObject();
                    for (Map.Entry<String, String> e : bindings.entrySet()) {
                        JsonObject bv = new JsonObject();
                        bv.addProperty("source", e.getValue());
                        bv.addProperty("extent_offset", 0);
                        bv.addProperty("extent_length",
                                e.getValue().getBytes(StandardCharsets.UTF_8).length);
                        bm.add(e.getKey(), bv);
                    }
                    m.add("bindings", bm);
                    matches.add(m);
                }
            }
        }
        JsonObject resp = new JsonObject();
        resp.addProperty("ok", true);
        resp.add("matches", matches);
        writeResponse(out, resp);
    }
}
