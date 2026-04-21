// serena-csharp-bridge
//
// Length-prefixed JSON request/response loop over stdio. Each request is a
// 4-byte little-endian length followed by UTF-8 JSON; each response has the
// same framing. Stateless: every request carries the source text it
// operates on, so there are no cross-call handles to invalidate.
//
// Operations mirror the Swift / TypeScript / Go / Rust / Java / Ruby bridges:
//   - walk_symbols       : (kind, name_path, extent_offset, extent_length, body_range)
//   - insert_child       : new source with a rendered child inserted
//   - remove_child       : new source with a named child excised
//   - find_matches       : match extents + captured bindings
//   - apply_replacement  : new source with a byte range replaced
//   - probe_parse        : whether the source has parse errors
//   - shutdown           : exit cleanly
//
// All offsets on the wire are UTF-8 byte offsets. Roslyn reports positions
// as UTF-16 character offsets (`TextSpan`), so the bridge converts via a
// per-request ByteOffsetMapper (mirrors the TypeScript / Java bridges).

using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.Json;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;
using Microsoft.CodeAnalysis.Text;

namespace Serena;

internal static class Program
{
    private const string CapturePrefix = "__capture_";

    public static int Main(string[] args)
    {
        using var stdin = Console.OpenStandardInput();
        using var stdout = Console.OpenStandardOutput();
        while (true)
        {
            byte[]? frame = ReadFrame(stdin);
            if (frame is null) return 0;
            if (frame.Length == 0) continue;
            JsonDocument req;
            try
            {
                req = JsonDocument.Parse(Encoding.UTF8.GetString(frame));
            }
            catch (Exception ex)
            {
                WriteError(stdout, "bad_request", "malformed JSON: " + ex.Message);
                continue;
            }
            try
            {
                Handle(stdout, req.RootElement);
            }
            catch (Exception ex)
            {
                WriteError(stdout, "bridge", ex.GetType().Name + ": " + ex.Message);
            }
            finally
            {
                req.Dispose();
            }
        }
    }

    private static void Handle(Stream outStream, JsonElement req)
    {
        string op = StringOr(req, "op", "");
        switch (op)
        {
            case "walk_symbols": OpWalkSymbols(outStream, req); return;
            case "insert_child": OpInsertChild(outStream, req); return;
            case "remove_child": OpRemoveChild(outStream, req); return;
            case "find_matches": OpFindMatches(outStream, req); return;
            case "apply_replacement": OpApplyReplacement(outStream, req); return;
            case "probe_parse": OpProbeParse(outStream, req); return;
            case "shutdown":
                WriteResponse(outStream, new Dictionary<string, object?> { ["ok"] = true });
                Environment.Exit(0);
                return;
            default:
                WriteError(outStream, "unknown_op", "unknown op: " + op);
                return;
        }
    }

    // -----------------------------------------------------------------------
    // I/O framing
    // -----------------------------------------------------------------------

    private static byte[]? ReadFrame(Stream input)
    {
        byte[] lenBuf = new byte[4];
        int read = 0;
        while (read < 4)
        {
            int n = input.Read(lenBuf, read, 4 - read);
            if (n == 0)
            {
                if (read == 0) return null;
                throw new IOException("truncated frame length");
            }
            read += n;
        }
        int len = BitConverter.ToInt32(lenBuf, 0);
        if (!BitConverter.IsLittleEndian)
        {
            // Force little-endian interpretation.
            Array.Reverse(lenBuf);
            len = BitConverter.ToInt32(lenBuf, 0);
        }
        if (len == 0) return Array.Empty<byte>();
        if (len < 0) throw new IOException("negative frame length");
        byte[] body = new byte[len];
        int total = 0;
        while (total < len)
        {
            int n = input.Read(body, total, len - total);
            if (n == 0) throw new IOException("truncated frame body");
            total += n;
        }
        return body;
    }

    private static void WriteFrame(Stream output, byte[] payload)
    {
        byte[] prefix = new byte[4];
        int length = payload.Length;
        prefix[0] = (byte)(length & 0xFF);
        prefix[1] = (byte)((length >> 8) & 0xFF);
        prefix[2] = (byte)((length >> 16) & 0xFF);
        prefix[3] = (byte)((length >> 24) & 0xFF);
        output.Write(prefix, 0, 4);
        output.Write(payload, 0, payload.Length);
        output.Flush();
    }

    private static void WriteResponse(Stream output, Dictionary<string, object?> obj)
    {
        byte[] body = Encoding.UTF8.GetBytes(SerializeJson(obj));
        WriteFrame(output, body);
    }

    private static void WriteError(Stream output, string kind, string message)
    {
        WriteResponse(output, new Dictionary<string, object?>
        {
            ["ok"] = false,
            ["error_kind"] = kind,
            ["message"] = message,
        });
    }

    private static string SerializeJson(object? value)
    {
        var options = new JsonSerializerOptions
        {
            Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
        };
        return JsonSerializer.Serialize(value, options);
    }

    // -----------------------------------------------------------------------
    // JSON helpers
    // -----------------------------------------------------------------------

    private static string StringOr(JsonElement obj, string key, string def)
    {
        if (!obj.TryGetProperty(key, out var el) || el.ValueKind != JsonValueKind.String) return def;
        return el.GetString() ?? def;
    }

    private static string RequireString(JsonElement obj, string key)
    {
        if (!obj.TryGetProperty(key, out var el) || el.ValueKind != JsonValueKind.String)
            throw new ArgumentException("missing string: " + key);
        return el.GetString() ?? "";
    }

    private static string? OptionalString(JsonElement obj, string key)
    {
        if (!obj.TryGetProperty(key, out var el) || el.ValueKind == JsonValueKind.Null) return null;
        if (el.ValueKind != JsonValueKind.String) return null;
        return el.GetString();
    }

    private static long RequireLong(JsonElement obj, string key)
    {
        if (!obj.TryGetProperty(key, out var el) || el.ValueKind != JsonValueKind.Number)
            throw new ArgumentException("missing number: " + key);
        return el.GetInt64();
    }

    // -----------------------------------------------------------------------
    // Byte offset mapping
    // -----------------------------------------------------------------------

    /// <summary>
    /// Per-request mapper from UTF-16 char offset to UTF-8 byte offset.
    /// Built in one pass over the source string.
    /// </summary>
    private sealed class ByteOffsetMapper
    {
        public readonly byte[] SourceBytes;
        public readonly int[] CharToByte;

        public ByteOffsetMapper(string source)
        {
            SourceBytes = Encoding.UTF8.GetBytes(source);
            int len = source.Length;
            CharToByte = new int[len + 1];
            int byteIdx = 0;
            int i = 0;
            while (i < len)
            {
                CharToByte[i] = byteIdx;
                char c = source[i];
                int cp;
                int step;
                if (char.IsHighSurrogate(c) && i + 1 < len && char.IsLowSurrogate(source[i + 1]))
                {
                    cp = char.ConvertToUtf32(c, source[i + 1]);
                    step = 2;
                }
                else
                {
                    cp = c;
                    step = 1;
                }
                int u;
                if (cp < 0x80) u = 1;
                else if (cp < 0x800) u = 2;
                else if (cp < 0x10000) u = 3;
                else u = 4;
                byteIdx += u;
                i += step;
            }
            CharToByte[len] = byteIdx;
        }

        public int ByteOffsetOfChar(int charOffset)
        {
            if (charOffset < 0) return 0;
            if (charOffset >= CharToByte.Length) return CharToByte[^1];
            return CharToByte[charOffset];
        }

        public int BeginByte(TextSpan span) => ByteOffsetOfChar(span.Start);
        public int EndByteExclusive(TextSpan span) => ByteOffsetOfChar(span.End);

        /// <summary>
        /// Extend `endByteExclusive` past whitespace up to and including
        /// one line break, so "after anchor" inserts land on the next
        /// line. Mirrors Rust/Go/Swift/TypeScript/Java convention.
        /// </summary>
        public int ExtendThroughTrailingNewline(int endByteExclusive)
        {
            int end = endByteExclusive;
            while (end < SourceBytes.Length)
            {
                byte b = SourceBytes[end];
                if (b == (byte)' ' || b == (byte)'\t')
                {
                    end++;
                    continue;
                }
                if (b == (byte)'\r')
                {
                    if (end + 1 < SourceBytes.Length && SourceBytes[end + 1] == (byte)'\n')
                        return end + 2;
                    return end + 1;
                }
                if (b == (byte)'\n') return end + 1;
                break;
            }
            return end;
        }
    }

    // -----------------------------------------------------------------------
    // Parsing
    // -----------------------------------------------------------------------

    private static readonly CSharpParseOptions ParseOpts =
        new CSharpParseOptions(LanguageVersion.Latest);

    private static SyntaxTree ParseTree(string source)
    {
        return CSharpSyntaxTree.ParseText(source, ParseOpts);
    }

    // -----------------------------------------------------------------------
    // Symbol walking
    // -----------------------------------------------------------------------

    private sealed record SymbolEntry(string Kind, string NamePath, int ExtentOffset, int ExtentLength);

    private static List<SymbolEntry> WalkSymbols(SyntaxTree tree, string source)
    {
        var mapper = new ByteOffsetMapper(source);
        var root = (CompilationUnitSyntax)tree.GetRoot();
        var outList = new List<SymbolEntry>();

        // Top-level usings.
        foreach (var u in root.Usings)
        {
            EmitUsing(u, "", mapper, outList);
        }

        // Top-level members: namespaces, types, delegates.
        foreach (var m in root.Members)
        {
            WalkTopLevelMember(m, "", source, mapper, outList);
        }

        return outList;
    }

    private static void EmitUsing(UsingDirectiveSyntax u, string parentPath,
                                  ByteOffsetMapper mapper, List<SymbolEntry> outList)
    {
        int start = mapper.BeginByte(u.Span);
        int end = mapper.ExtendThroughTrailingNewline(mapper.EndByteExclusive(u.Span));
        string name = UsingName(u);
        string path = parentPath.Length == 0 ? name : parentPath + "/" + name;
        outList.Add(new SymbolEntry("using", path, start, end - start));
    }

    private static string UsingName(UsingDirectiveSyntax u)
    {
        // Handle: `using System;`, `using static System.Math;`, `using X = System.Collections.ArrayList;`.
        if (u.Alias != null)
        {
            // `using Alias = X.Y.Z;` -- emit the alias name.
            return u.Alias.Name.Identifier.Text;
        }
        string prefix = u.StaticKeyword.IsKind(SyntaxKind.StaticKeyword) ? "static " : "";
        string global = u.GlobalKeyword.IsKind(SyntaxKind.GlobalKeyword) ? "global " : "";
        return global + prefix + (u.Name?.ToString() ?? "");
    }

    private static void WalkTopLevelMember(MemberDeclarationSyntax m, string parentPath,
                                           string source, ByteOffsetMapper mapper,
                                           List<SymbolEntry> outList)
    {
        switch (m)
        {
            case BaseNamespaceDeclarationSyntax nsd:
                WalkNamespace(nsd, parentPath, source, mapper, outList);
                return;
            case TypeDeclarationSyntax td:
                WalkType(td, parentPath, source, mapper, outList);
                return;
            case EnumDeclarationSyntax ed:
                EmitLeafType(ed, "enum", ed.Identifier.Text, parentPath, mapper, outList);
                return;
            case DelegateDeclarationSyntax dd:
                EmitDelegate(dd, parentPath, mapper, outList);
                return;
            case GlobalStatementSyntax:
                // Top-level statements -- not modeled as symbols in v1.
                return;
        }
    }

    private static void WalkNamespace(BaseNamespaceDeclarationSyntax nsd, string parentPath,
                                      string source, ByteOffsetMapper mapper,
                                      List<SymbolEntry> outList)
    {
        string nsName = nsd.Name.ToString();
        string fullPath = parentPath.Length == 0 ? nsName : parentPath + "/" + nsName;

        // File-scoped namespace has a semicolon terminator; block namespace has braces.
        // In both cases, the node's Span includes the body.
        int start = mapper.BeginByte(nsd.Span);
        int end = mapper.ExtendThroughTrailingNewline(mapper.EndByteExclusive(nsd.Span));
        outList.Add(new SymbolEntry("namespace", fullPath, start, end - start));

        foreach (var u in nsd.Usings)
        {
            EmitUsing(u, fullPath, mapper, outList);
        }
        foreach (var m in nsd.Members)
        {
            WalkTopLevelMember(m, fullPath, source, mapper, outList);
        }
    }

    private static void WalkType(TypeDeclarationSyntax td, string parentPath,
                                 string source, ByteOffsetMapper mapper,
                                 List<SymbolEntry> outList)
    {
        int start = mapper.BeginByte(td.Span);
        int end = mapper.ExtendThroughTrailingNewline(mapper.EndByteExclusive(td.Span));
        string name = td.Identifier.Text;
        string fullPath = parentPath.Length == 0 ? name : parentPath + "/" + name;
        string kind = td switch
        {
            ClassDeclarationSyntax => "class",
            StructDeclarationSyntax => "struct",
            InterfaceDeclarationSyntax => "interface",
            RecordDeclarationSyntax => "record",
            _ => "class",
        };
        outList.Add(new SymbolEntry(kind, fullPath, start, end - start));

        foreach (var member in td.Members)
        {
            EmitMember(member, fullPath, source, mapper, outList);
        }
    }

    private static void EmitMember(MemberDeclarationSyntax member, string parentPath,
                                   string source, ByteOffsetMapper mapper,
                                   List<SymbolEntry> outList)
    {
        switch (member)
        {
            case MethodDeclarationSyntax md:
            {
                int s = mapper.BeginByte(md.Span);
                int e = mapper.ExtendThroughTrailingNewline(mapper.EndByteExclusive(md.Span));
                string n = parentPath + "/" + md.Identifier.Text + "(" + ParamTypeString(md.ParameterList) + ")";
                outList.Add(new SymbolEntry("method", n, s, e - s));
                return;
            }
            case ConstructorDeclarationSyntax cd:
            {
                int s = mapper.BeginByte(cd.Span);
                int e = mapper.ExtendThroughTrailingNewline(mapper.EndByteExclusive(cd.Span));
                string n = parentPath + "/" + cd.Identifier.Text + "(" + ParamTypeString(cd.ParameterList) + ")";
                outList.Add(new SymbolEntry("constructor", n, s, e - s));
                return;
            }
            case PropertyDeclarationSyntax pd:
            {
                int s = mapper.BeginByte(pd.Span);
                int e = mapper.ExtendThroughTrailingNewline(mapper.EndByteExclusive(pd.Span));
                string n = parentPath + "/" + pd.Identifier.Text;
                outList.Add(new SymbolEntry("property", n, s, e - s));
                return;
            }
            case FieldDeclarationSyntax fd:
            {
                // A FieldDeclaration can declare multiple variables: `int a, b;`.
                // Emit one entry per variable, using the whole field range for
                // the first so removal excises the full statement.
                var vars = fd.Declaration.Variables;
                for (int i = 0; i < vars.Count; i++)
                {
                    var v = vars[i];
                    TextSpan span = i == 0 ? fd.Span : v.Span;
                    int s = mapper.BeginByte(span);
                    int e = mapper.ExtendThroughTrailingNewline(mapper.EndByteExclusive(span));
                    outList.Add(new SymbolEntry("field", parentPath + "/" + v.Identifier.Text, s, e - s));
                }
                return;
            }
            case EventDeclarationSyntax evd:
            {
                int s = mapper.BeginByte(evd.Span);
                int e = mapper.ExtendThroughTrailingNewline(mapper.EndByteExclusive(evd.Span));
                string n = parentPath + "/" + evd.Identifier.Text;
                outList.Add(new SymbolEntry("event", n, s, e - s));
                return;
            }
            case EventFieldDeclarationSyntax efd:
            {
                var vars = efd.Declaration.Variables;
                for (int i = 0; i < vars.Count; i++)
                {
                    var v = vars[i];
                    TextSpan span = i == 0 ? efd.Span : v.Span;
                    int s = mapper.BeginByte(span);
                    int e = mapper.ExtendThroughTrailingNewline(mapper.EndByteExclusive(span));
                    outList.Add(new SymbolEntry("event", parentPath + "/" + v.Identifier.Text, s, e - s));
                }
                return;
            }
            case TypeDeclarationSyntax ntd:
                WalkType(ntd, parentPath, source, mapper, outList);
                return;
            case EnumDeclarationSyntax ned:
                EmitLeafType(ned, "enum", ned.Identifier.Text, parentPath, mapper, outList);
                return;
            case DelegateDeclarationSyntax ndd:
                EmitDelegate(ndd, parentPath, mapper, outList);
                return;
            case BaseNamespaceDeclarationSyntax nsd:
                WalkNamespace(nsd, parentPath, source, mapper, outList);
                return;
        }
    }

    private static void EmitLeafType(SyntaxNode node, string kind, string name,
                                     string parentPath, ByteOffsetMapper mapper,
                                     List<SymbolEntry> outList)
    {
        int start = mapper.BeginByte(node.Span);
        int end = mapper.ExtendThroughTrailingNewline(mapper.EndByteExclusive(node.Span));
        string path = parentPath.Length == 0 ? name : parentPath + "/" + name;
        outList.Add(new SymbolEntry(kind, path, start, end - start));
    }

    private static void EmitDelegate(DelegateDeclarationSyntax dd, string parentPath,
                                     ByteOffsetMapper mapper, List<SymbolEntry> outList)
    {
        int start = mapper.BeginByte(dd.Span);
        int end = mapper.ExtendThroughTrailingNewline(mapper.EndByteExclusive(dd.Span));
        string name = dd.Identifier.Text + "(" + ParamTypeString(dd.ParameterList) + ")";
        string path = parentPath.Length == 0 ? name : parentPath + "/" + name;
        outList.Add(new SymbolEntry("delegate", path, start, end - start));
    }

    private static string ParamTypeString(ParameterListSyntax paramList)
    {
        if (paramList.Parameters.Count == 0) return "";
        var sb = new StringBuilder();
        for (int i = 0; i < paramList.Parameters.Count; i++)
        {
            if (i > 0) sb.Append(',');
            var p = paramList.Parameters[i];
            // Render the type; "params" becomes "..." suffix to mirror Java convention.
            string t = p.Type?.ToString() ?? "";
            bool hasParams = p.Modifiers.Any(x => x.IsKind(SyntaxKind.ParamsKeyword));
            if (hasParams) t += "...";
            sb.Append(t);
        }
        return sb.ToString();
    }

    private static SymbolEntry? LookupSymbol(SyntaxTree tree, string source, string namePath)
    {
        foreach (var e in WalkSymbols(tree, source))
        {
            if (e.NamePath == namePath) return e;
        }
        return null;
    }

    // -----------------------------------------------------------------------
    // op: walk_symbols / probe_parse
    // -----------------------------------------------------------------------

    private static void OpWalkSymbols(Stream outStream, JsonElement req)
    {
        string source = RequireString(req, "source");
        var list = new List<Dictionary<string, object?>>();
        if (!string.IsNullOrEmpty(source))
        {
            var tree = ParseTree(source);
            foreach (var e in WalkSymbols(tree, source))
            {
                list.Add(new Dictionary<string, object?>
                {
                    ["kind"] = e.Kind,
                    ["name_path"] = e.NamePath,
                    ["extent_offset"] = e.ExtentOffset,
                    ["extent_length"] = e.ExtentLength,
                    ["body_range"] = null,
                });
            }
        }
        WriteResponse(outStream, new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["symbols"] = list,
        });
    }

    private static void OpProbeParse(Stream outStream, JsonElement req)
    {
        string source = RequireString(req, "source");
        bool hasErrors;
        if (source.Length == 0) hasErrors = false;
        else
        {
            var tree = ParseTree(source);
            hasErrors = tree.GetDiagnostics().Any(d => d.Severity == DiagnosticSeverity.Error);
        }
        WriteResponse(outStream, new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["has_errors"] = hasErrors,
        });
    }

    // -----------------------------------------------------------------------
    // op: insert_child
    // -----------------------------------------------------------------------

    private static void OpInsertChild(Stream outStream, JsonElement req)
    {
        string source = RequireString(req, "source");
        string childSource = RequireString(req, "child_source");
        string position = RequireString(req, "position");
        string? parentPath = OptionalString(req, "parent_name_path");
        string? anchorPath = OptionalString(req, "anchor_name_path");

        if (parentPath != null)
        {
            WriteError(outStream, "no_body",
                "parent " + parentPath +
                " has no body (csharp bridge does not expose body ranges in v1)");
            return;
        }

        byte[] sourceBytes = Encoding.UTF8.GetBytes(source);
        int offset;
        switch (position)
        {
            case "start":
                offset = 0;
                break;
            case "end":
                offset = sourceBytes.Length;
                break;
            case "before":
            case "after":
                if (anchorPath == null)
                {
                    WriteError(outStream, "bad_request", "position " + position + " requires anchor");
                    return;
                }
                var tree = ParseTree(source);
                var anchor = LookupSymbol(tree, source, anchorPath);
                if (anchor == null)
                {
                    WriteError(outStream, "symbol_missing", "anchor " + anchorPath + " not found");
                    return;
                }
                offset = position == "before" ? anchor.ExtentOffset : anchor.ExtentOffset + anchor.ExtentLength;
                break;
            default:
                WriteError(outStream, "bad_request", "invalid position: " + position);
                return;
        }

        string normalized = childSource.EndsWith("\n") ? childSource : childSource + "\n";
        byte[] insertBytes = Encoding.UTF8.GetBytes(normalized);
        var edited = new byte[sourceBytes.Length + insertBytes.Length];
        Array.Copy(sourceBytes, 0, edited, 0, offset);
        Array.Copy(insertBytes, 0, edited, offset, insertBytes.Length);
        Array.Copy(sourceBytes, offset, edited, offset + insertBytes.Length, sourceBytes.Length - offset);
        WriteResponse(outStream, new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["source"] = Encoding.UTF8.GetString(edited),
        });
    }

    // -----------------------------------------------------------------------
    // op: remove_child
    // -----------------------------------------------------------------------

    private static void OpRemoveChild(Stream outStream, JsonElement req)
    {
        string source = RequireString(req, "source");
        string childPath = RequireString(req, "child_name_path");
        if (source.Length == 0)
        {
            WriteError(outStream, "symbol_missing", "child " + childPath + " not found (empty source)");
            return;
        }
        var tree = ParseTree(source);
        var sym = LookupSymbol(tree, source, childPath);
        if (sym == null)
        {
            WriteError(outStream, "symbol_missing", "child " + childPath + " not found");
            return;
        }
        byte[] sourceBytes = Encoding.UTF8.GetBytes(source);
        var edited = new byte[sourceBytes.Length - sym.ExtentLength];
        Array.Copy(sourceBytes, 0, edited, 0, sym.ExtentOffset);
        Array.Copy(sourceBytes, sym.ExtentOffset + sym.ExtentLength,
                   edited, sym.ExtentOffset,
                   sourceBytes.Length - sym.ExtentOffset - sym.ExtentLength);
        WriteResponse(outStream, new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["source"] = Encoding.UTF8.GetString(edited),
        });
    }

    // -----------------------------------------------------------------------
    // op: apply_replacement
    // -----------------------------------------------------------------------

    private static void OpApplyReplacement(Stream outStream, JsonElement req)
    {
        string source = RequireString(req, "source");
        string replacement = RequireString(req, "replacement_source");
        int matchOffset = (int)RequireLong(req, "match_offset");
        int matchLength = (int)RequireLong(req, "match_length");
        byte[] sourceBytes = Encoding.UTF8.GetBytes(source);
        if (matchOffset < 0 || matchLength < 0 || matchOffset + matchLength > sourceBytes.Length)
        {
            WriteError(outStream, "bad_request", "match range out of source bounds");
            return;
        }
        byte[] repBytes = Encoding.UTF8.GetBytes(replacement);
        var edited = new byte[sourceBytes.Length - matchLength + repBytes.Length];
        Array.Copy(sourceBytes, 0, edited, 0, matchOffset);
        Array.Copy(repBytes, 0, edited, matchOffset, repBytes.Length);
        Array.Copy(sourceBytes, matchOffset + matchLength,
                   edited, matchOffset + repBytes.Length,
                   sourceBytes.Length - matchOffset - matchLength);
        WriteResponse(outStream, new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["source"] = Encoding.UTF8.GetString(edited),
        });
    }

    // -----------------------------------------------------------------------
    // op: find_matches (expression-level structural match + $captures)
    // -----------------------------------------------------------------------

    /// <summary>
    /// Rewrite every `$name` / `$_` sigil in `src` to an identifier of the
    /// form `__capture_&lt;name&gt;`. Token-dumb because the parser has not seen
    /// the source yet.
    /// </summary>
    private static string PreprocessPattern(string src)
    {
        var sb = new StringBuilder(src.Length);
        int i = 0;
        int n = src.Length;
        while (i < n)
        {
            char c = src[i];
            if (c == '$' && i + 1 < n)
            {
                char next = src[i + 1];
                bool identStart = next == '_' || char.IsLetter(next);
                if (identStart)
                {
                    int j = i + 1;
                    while (j < n)
                    {
                        char cc = src[j];
                        if (cc == '_' || char.IsLetterOrDigit(cc)) j++;
                        else break;
                    }
                    if (j > i + 1)
                    {
                        sb.Append(CapturePrefix);
                        sb.Append(src, i + 1, j - (i + 1));
                        i = j;
                        continue;
                    }
                }
            }
            sb.Append(c);
            i++;
        }
        return sb.ToString();
    }

    private static ExpressionSyntax? ParsePatternExpression(string src)
    {
        var expr = SyntaxFactory.ParseExpression(src, options: ParseOpts);
        if (expr.ContainsDiagnostics) return null;
        return expr;
    }

    /// <summary>
    /// If `expr` is a bare identifier `__capture_&lt;name&gt;`, return `&lt;name&gt;`.
    /// </summary>
    private static string? CaptureNameOf(ExpressionSyntax expr)
    {
        if (expr is IdentifierNameSyntax ins)
        {
            string name = ins.Identifier.Text;
            if (name.StartsWith(CapturePrefix, StringComparison.Ordinal))
                return name.Substring(CapturePrefix.Length);
        }
        return null;
    }

    private static string ExprText(SyntaxNode node, string source, ByteOffsetMapper mapper)
    {
        int start = mapper.BeginByte(node.Span);
        int end = mapper.EndByteExclusive(node.Span);
        if (start < 0 || end > mapper.SourceBytes.Length || start >= end) return node.ToString();
        return Encoding.UTF8.GetString(mapper.SourceBytes, start, end - start);
    }

    private static bool MatchExpr(ExpressionSyntax pat, ExpressionSyntax cand,
                                  string source, ByteOffsetMapper mapper,
                                  Dictionary<string, string> bindings)
    {
        string? capName = CaptureNameOf(pat);
        if (capName != null)
        {
            if (capName == "_") return true;
            string text = ExprText(cand, source, mapper);
            if (bindings.TryGetValue(capName, out var existing))
                return existing == text;
            bindings[capName] = text;
            return true;
        }
        // Paren passes through on either side.
        if (pat is ParenthesizedExpressionSyntax pp)
            return MatchExpr(pp.Expression, cand, source, mapper, bindings);
        if (cand is ParenthesizedExpressionSyntax cp)
            return MatchExpr(pat, cp.Expression, source, mapper, bindings);

        if (pat is InvocationExpressionSyntax pi && cand is InvocationExpressionSyntax ci)
        {
            if (!MatchExpr(pi.Expression, ci.Expression, source, mapper, bindings)) return false;
            var pa = pi.ArgumentList.Arguments;
            var ca = ci.ArgumentList.Arguments;
            if (pa.Count != ca.Count) return false;
            for (int i = 0; i < pa.Count; i++)
                if (!MatchExpr(pa[i].Expression, ca[i].Expression, source, mapper, bindings)) return false;
            return true;
        }
        if (pat is ObjectCreationExpressionSyntax po && cand is ObjectCreationExpressionSyntax co)
        {
            if (po.Type.ToString() != co.Type.ToString()) return false;
            var pa = po.ArgumentList?.Arguments ?? default;
            var ca = co.ArgumentList?.Arguments ?? default;
            if (pa.Count != ca.Count) return false;
            for (int i = 0; i < pa.Count; i++)
                if (!MatchExpr(pa[i].Expression, ca[i].Expression, source, mapper, bindings)) return false;
            return true;
        }
        if (pat is MemberAccessExpressionSyntax pm && cand is MemberAccessExpressionSyntax cm)
        {
            if (!pm.Kind().Equals(cm.Kind())) return false;
            if (pm.Name.Identifier.Text != cm.Name.Identifier.Text) return false;
            return MatchExpr(pm.Expression, cm.Expression, source, mapper, bindings);
        }
        if (pat is BinaryExpressionSyntax pb && cand is BinaryExpressionSyntax cb)
        {
            if (pb.Kind() != cb.Kind()) return false;
            return MatchExpr(pb.Left, cb.Left, source, mapper, bindings)
                && MatchExpr(pb.Right, cb.Right, source, mapper, bindings);
        }
        if (pat is PrefixUnaryExpressionSyntax ppu && cand is PrefixUnaryExpressionSyntax cpu)
        {
            if (ppu.Kind() != cpu.Kind()) return false;
            return MatchExpr(ppu.Operand, cpu.Operand, source, mapper, bindings);
        }
        if (pat is PostfixUnaryExpressionSyntax ppo && cand is PostfixUnaryExpressionSyntax cpo)
        {
            if (ppo.Kind() != cpo.Kind()) return false;
            return MatchExpr(ppo.Operand, cpo.Operand, source, mapper, bindings);
        }
        if (pat is LiteralExpressionSyntax pl && cand is LiteralExpressionSyntax cl)
        {
            return pl.Token.Text == cl.Token.Text;
        }
        if (pat is IdentifierNameSyntax pin && cand is IdentifierNameSyntax cin)
        {
            return pin.Identifier.Text == cin.Identifier.Text;
        }
        // Fallback: textual equality on the rendered form.
        return pat.ToString() == cand.ToString();
    }

    private static void CollectExprs(SyntaxNode root, List<ExpressionSyntax> outList)
    {
        foreach (var child in root.ChildNodes())
        {
            if (child is ExpressionSyntax e) outList.Add(e);
            CollectExprs(child, outList);
        }
    }

    private static void OpFindMatches(Stream outStream, JsonElement req)
    {
        string source = RequireString(req, "source");
        string patternSource = RequireString(req, "pattern_source");
        // scope accepted but ignored in v1.

        string processed = PreprocessPattern(patternSource);
        var patExpr = ParsePatternExpression(processed);
        if (patExpr == null)
        {
            // Accept valid non-expression patterns silently (yield no matches)
            // so a statement-shaped pattern does not crash callers.
            var stmt = SyntaxFactory.ParseStatement(processed, options: ParseOpts);
            var mem = SyntaxFactory.ParseMemberDeclaration(processed, options: ParseOpts);
            bool nonExprButValid = (!stmt.ContainsDiagnostics) ||
                (mem != null && !mem.ContainsDiagnostics);
            if (nonExprButValid)
            {
                WriteResponse(outStream, new Dictionary<string, object?>
                {
                    ["ok"] = true,
                    ["matches"] = new List<Dictionary<string, object?>>(),
                });
                return;
            }
            WriteError(outStream, "pattern_parse", "could not parse pattern: " + patternSource);
            return;
        }

        var matches = new List<Dictionary<string, object?>>();
        if (!string.IsNullOrEmpty(source))
        {
            var tree = ParseTree(source);
            var mapper = new ByteOffsetMapper(source);
            var root = tree.GetRoot();
            var exprs = new List<ExpressionSyntax>();
            CollectExprs(root, exprs);
            foreach (var cand in exprs)
            {
                var bindings = new Dictionary<string, string>();
                if (MatchExpr(patExpr, cand, source, mapper, bindings))
                {
                    int start = mapper.BeginByte(cand.Span);
                    int end = mapper.EndByteExclusive(cand.Span);
                    if (end <= start) continue;
                    var bm = new Dictionary<string, object?>();
                    foreach (var kv in bindings)
                    {
                        bm[kv.Key] = new Dictionary<string, object?>
                        {
                            ["source"] = kv.Value,
                            ["extent_offset"] = 0,
                            ["extent_length"] = Encoding.UTF8.GetByteCount(kv.Value),
                        };
                    }
                    matches.Add(new Dictionary<string, object?>
                    {
                        ["extent_offset"] = start,
                        ["extent_length"] = end - start,
                        ["bindings"] = bm,
                    });
                }
            }
        }
        WriteResponse(outStream, new Dictionary<string, object?>
        {
            ["ok"] = true,
            ["matches"] = matches,
        });
    }
}
