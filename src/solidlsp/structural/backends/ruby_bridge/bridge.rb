# frozen_string_literal: true

# serena-ruby-bridge
#
# Length-prefixed JSON request/response loop over stdio. Each request is a
# 4-byte little-endian length followed by UTF-8 JSON; each response has the
# same framing. Stateless: every request carries the source text it operates
# on, so there are no cross-call handles to invalidate.
#
# Operations mirror the Swift / TypeScript / Go / Rust / Java bridges:
#   - walk_symbols       : (kind, name_path, extent_offset, extent_length, body_range)
#   - insert_child       : new source with a rendered child inserted
#   - remove_child       : new source with a named child excised
#   - find_matches       : match extents + captured bindings
#   - apply_replacement  : new source with a byte range replaced
#   - probe_parse        : whether the source has parse errors
#   - shutdown           : exit cleanly
#
# All offsets on the wire are UTF-8 byte offsets, which is what Prism's
# Location#start_offset and #end_offset already emit (verified against a
# multi-byte fixture during development).

require 'json'
require 'prism'

# ---------------------------------------------------------------------------
# I/O framing
# ---------------------------------------------------------------------------

STDIN.binmode
STDOUT.binmode

def read_exact(n)
  return ''.b if n.zero?
  buf = String.new(capacity: n, encoding: Encoding::BINARY)
  while buf.bytesize < n
    chunk = STDIN.read(n - buf.bytesize)
    return nil if chunk.nil? || chunk.empty?
    buf << chunk.b
  end
  buf
end

def read_frame
  prefix = read_exact(4)
  return nil if prefix.nil?
  length = prefix.unpack1('V') # little-endian uint32
  return ''.b if length.zero?
  read_exact(length)
end

def write_frame(payload)
  bytes = payload.to_s.b
  STDOUT.write([bytes.bytesize].pack('V'))
  STDOUT.write(bytes)
  STDOUT.flush
end

def json_encode(obj)
  JSON.generate(obj)
end

def ok_response(fields = {})
  json_encode({ ok: true }.merge(fields))
end

def error_response(kind, message)
  json_encode({ ok: false, error_kind: kind, message: message })
end

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_source(source)
  # Prism.parse returns a ParseResult whose .value is the ProgramNode
  Prism.parse(source)
end

def has_parse_errors?(result)
  !result.errors.empty?
end

# ---------------------------------------------------------------------------
# Kind classification
# ---------------------------------------------------------------------------

def kind_for(node, parent_kind)
  case node
  when Prism::ClassNode       then 'class'
  when Prism::ModuleNode      then 'module'
  when Prism::DefNode
    node.receiver.nil? ? 'method' : 'singleton_method'
  when Prism::ConstantWriteNode, Prism::ConstantPathWriteNode,
       Prism::ConstantOrWriteNode, Prism::ConstantAndWriteNode,
       Prism::ConstantOperatorWriteNode
    'constant'
  when Prism::AliasMethodNode then 'alias'
  when Prism::CallNode
    # only recognise `require` / `require_relative` as `require` kind;
    # every other call is ignored as a named symbol
    if (node.name == :require || node.name == :require_relative) && require_arg_string(node)
      'require'
    else
      nil
    end
  else
    nil
  end
end

# ---------------------------------------------------------------------------
# Name spelling
# ---------------------------------------------------------------------------

def name_for(node, source)
  case node
  when Prism::ClassNode, Prism::ModuleNode
    constant_path_name(node.constant_path, source)
  when Prism::DefNode
    if node.receiver.nil?
      node.name.to_s
    else
      "self.#{node.name}"
    end
  when Prism::ConstantWriteNode
    node.name.to_s
  when Prism::ConstantPathWriteNode
    constant_path_name(node.target, source)
  when Prism::ConstantOrWriteNode, Prism::ConstantAndWriteNode,
       Prism::ConstantOperatorWriteNode
    node.name.to_s
  when Prism::AliasMethodNode
    alias_new_name(node.new_name, source)
  when Prism::CallNode
    arg = require_arg_string(node)
    arg
  else
    nil
  end
end

def constant_path_name(node, source)
  case node
  when Prism::ConstantReadNode
    node.name.to_s
  when Prism::ConstantPathNode
    # left may be a ConstantReadNode, ConstantPathNode, or nil (for `::Foo`)
    left = node.parent
    right = node.name.to_s
    left.nil? ? "::#{right}" : "#{constant_path_name(left, source)}::#{right}"
  else
    # fallback: slice the source text
    node.slice rescue nil
  end
end

def alias_new_name(name_node, source)
  case name_node
  when Prism::SymbolNode
    name_node.value.to_s
  else
    # interpolated or dynamic — fall back to the slice text
    name_node.slice
  end
end

# The single string argument to require/require_relative, or nil when the
# argument is not a plain string literal. `require File.expand_path(...)`
# would return nil — we skip such calls so walk output stays deterministic.
def require_arg_string(call_node)
  args = call_node.arguments
  return nil if args.nil?
  list = args.arguments
  return nil if list.size != 1
  a = list.first
  case a
  when Prism::StringNode
    a.unescaped
  else
    nil
  end
end

# ---------------------------------------------------------------------------
# Extent + trailing newline
# ---------------------------------------------------------------------------

# Prism Location#start_offset and #end_offset are UTF-8 byte offsets into the
# original source string. `end_offset` is exclusive. For declarations that
# end with an `end` keyword, end_offset points just past the keyword.

def extent_bytes(node, source)
  start_off = node.location.start_offset
  end_off = node.location.end_offset
  [start_off, end_off]
end

# Walk past trailing whitespace and one line-ending so "after anchor"
# insertion lands past the anchor's trailing newline. Mirrors the Swift
# bridge's use of endPosition-including-trivia.
def extend_through_trailing_newline(source, end_off)
  bytes = source.bytes
  e = end_off
  while e < bytes.length
    c = bytes[e]
    if c == 0x20 || c == 0x09 # space or tab
      e += 1
      next
    end
    if c == 0x0d # CR
      return bytes[e + 1] == 0x0a ? e + 2 : e + 1
    end
    if c == 0x0a # LF
      return e + 1
    end
    break
  end
  e
end

# ---------------------------------------------------------------------------
# Symbol walking
# ---------------------------------------------------------------------------

def walk_symbols(result, source)
  out = []
  program = result.value
  return out if program.nil?
  walk_nodes(program.statements&.body || [], [], nil, source, out)
  out
end

def walk_nodes(nodes, parent_path, parent_kind, source, out)
  nodes.each do |node|
    kind = kind_for(node, parent_kind)
    next if kind.nil?
    name = name_for(node, source)
    next if name.nil? || name.empty?
    segments = parent_path + [name]
    name_path = segments.join('/')
    start_off, end_off = extent_bytes(node, source)
    end_off = extend_through_trailing_newline(source, end_off)
    out << {
      kind: kind,
      name_path: name_path,
      extent_offset: start_off,
      extent_length: end_off - start_off,
      body_range: nil
    }
    # recurse into class/module bodies
    children = member_list(node)
    if children
      walk_nodes(children, segments, kind, source, out)
    end
  end
end

def member_list(node)
  case node
  when Prism::ClassNode, Prism::ModuleNode
    body = node.body
    return nil if body.nil?
    case body
    when Prism::StatementsNode then body.body
    else []
    end
  else
    nil
  end
end

def lookup_symbol(result, source, name_path)
  walk_symbols(result, source).find { |s| s[:name_path] == name_path }
end

# ---------------------------------------------------------------------------
# ops: walk_symbols / probe_parse
# ---------------------------------------------------------------------------

def op_walk_symbols(request)
  source = request['source']
  return error_response('bad_request', "walk_symbols requires 'source'") unless source.is_a?(String)
  result = parse_source(source)
  ok_response(symbols: walk_symbols(result, source).map do |e|
    {
      kind: e[:kind],
      name_path: e[:name_path],
      extent_offset: e[:extent_offset],
      extent_length: e[:extent_length],
      body_range: e[:body_range]
    }
  end)
end

def op_probe_parse(request)
  source = request['source']
  return error_response('bad_request', "probe_parse requires 'source'") unless source.is_a?(String)
  result = parse_source(source)
  ok_response(has_errors: has_parse_errors?(result))
end

# ---------------------------------------------------------------------------
# op: insert_child
# ---------------------------------------------------------------------------

def op_insert_child(request)
  source = request['source']
  child_source = request['child_source']
  position = request['position']
  parent_name_path = request['parent_name_path']
  anchor_name_path = request['anchor_name_path']
  unless source.is_a?(String) && child_source.is_a?(String) && position.is_a?(String)
    return error_response('bad_request', 'insert_child requires source/child_source/position')
  end
  result = parse_source(source)
  if parent_name_path
    parent = lookup_symbol(result, source, parent_name_path)
    return error_response('symbol_missing', "parent #{parent_name_path} not found") unless parent
    return error_response('no_body', "parent #{parent_name_path} has no body range") if parent[:body_range].nil?
    window_start, window_end = parent[:body_range]
  else
    window_start = 0
    window_end = source.bytesize
  end
  offset =
    case position
    when 'start' then window_start
    when 'end' then window_end
    when 'before', 'after'
      return error_response('bad_request', "position #{position} requires anchor") if anchor_name_path.nil?
      anchor = lookup_symbol(result, source, anchor_name_path)
      return error_response('symbol_missing', "anchor #{anchor_name_path} not found") unless anchor
      position == 'before' ? anchor[:extent_offset] : anchor[:extent_offset] + anchor[:extent_length]
    else
      return error_response('bad_request', "invalid position: #{position}")
    end
  normalized = child_source.end_with?("\n") ? child_source : child_source + "\n"
  edited = replace_byte_range(source, offset, 0, normalized)
  ok_response(source: edited)
end

# ---------------------------------------------------------------------------
# op: remove_child
# ---------------------------------------------------------------------------

def op_remove_child(request)
  source = request['source']
  child_name_path = request['child_name_path']
  unless source.is_a?(String) && child_name_path.is_a?(String)
    return error_response('bad_request', 'remove_child requires source/child_name_path')
  end
  result = parse_source(source)
  sym = lookup_symbol(result, source, child_name_path)
  return error_response('symbol_missing', "child #{child_name_path} not found") unless sym
  edited = replace_byte_range(source, sym[:extent_offset], sym[:extent_length], '')
  ok_response(source: edited)
end

# ---------------------------------------------------------------------------
# op: apply_replacement
# ---------------------------------------------------------------------------

def op_apply_replacement(request)
  source = request['source']
  match_offset = request['match_offset']
  match_length = request['match_length']
  replacement_source = request['replacement_source']
  unless source.is_a?(String) && match_offset.is_a?(Integer) &&
         match_length.is_a?(Integer) && replacement_source.is_a?(String)
    return error_response('bad_request',
                          'apply_replacement requires source/match_offset/match_length/replacement_source')
  end
  edited = replace_byte_range(source, match_offset, match_length, replacement_source)
  ok_response(source: edited)
end

# ---------------------------------------------------------------------------
# op: find_matches
# ---------------------------------------------------------------------------
#
# Pattern grammar: target language's own surface syntax, extended with
# capture sigils. Because `$name` is a Ruby global-variable identifier,
# Prism parses it as a GlobalVariableReadNode with name `:$name`. We
# detect those nodes during structural comparison and treat them as
# wildcard captures.
#
# `$_` is the anonymous wildcard (no binding recorded). Any other `$name`
# captures the candidate subtree under `name` (sans the leading `$`).
#
# Matching strategy mirrors the Java and Swift bridges: walk the pattern
# down to a single candidate root (the only statement, unwrapped if it
# is a StatementsNode with one child), then walk the input tree and
# structurally compare each node's class + children.

def op_find_matches(request)
  source = request['source']
  pattern_source = request['pattern_source']
  scope_name_path = request['scope_name_path']
  unless source.is_a?(String) && pattern_source.is_a?(String)
    return error_response('bad_request', 'find_matches requires source/pattern_source')
  end
  pattern_result = parse_source(pattern_source)
  return error_response('pattern_parse', pattern_result.errors.first.message) unless pattern_result.errors.empty?
  pattern_root = extract_pattern_root(pattern_result)
  return error_response('pattern_parse', 'pattern did not produce a matchable node') if pattern_root.nil?

  source_result = parse_source(source)
  search_root = source_result.value
  if scope_name_path
    scope_sym = lookup_symbol(source_result, source, scope_name_path)
    return error_response('symbol_missing', "scope #{scope_name_path} not found") unless scope_sym
    scope_node = node_with_start_offset(source_result.value, scope_sym[:extent_offset])
    search_root = scope_node if scope_node
  end

  matches = collect_matches(search_root, pattern_root, source, pattern_source)
  ok_response(matches: matches.map do |m|
    start_off, end_off = extent_bytes(m[:node], source)
    {
      extent_offset: start_off,
      extent_length: end_off - start_off,
      bindings: m[:bindings].transform_values do |b_node|
        b_start, b_end = extent_bytes(b_node, source)
        {
          source: source.byteslice(b_start, b_end - b_start),
          extent_offset: b_start,
          extent_length: b_end - b_start
        }
      end
    }
  end)
end

def extract_pattern_root(pattern_result)
  program = pattern_result.value
  return nil if program.nil?
  stmts = program.statements&.body || []
  return nil if stmts.size != 1
  only = stmts.first
  # When the statement is an ExpressionStatement-like wrapper, unwrap. In
  # Prism there is no such wrapper; every statement is a full node. Pattern
  # for `foo($x)` is a CallNode directly.
  only
end

def node_with_start_offset(root, byte_offset)
  # Locate the first AST node whose location.start_offset equals byte_offset.
  # Used for scope narrowing: walk_symbols extents include trailing-newline
  # trivia that Prism's own locations do not, so a containment-by-range check
  # against the extended extent stops above the scope node. Matching on
  # start_offset is exact because every named symbol's declaration begins at
  # a unique byte.
  found = nil
  walk = lambda do |node|
    return if found
    if node.location.start_offset == byte_offset
      found = node
      return
    end
    node.child_nodes.each { |c| walk.call(c) if c }
  end
  walk.call(root)
  found
end

def enclosing_node(root, byte_offset, byte_length)
  target_start = byte_offset
  target_end = byte_offset + byte_length
  best = root
  walk = lambda do |node|
    start_off = node.location.start_offset
    end_off = node.location.end_offset
    if start_off <= target_start && end_off >= target_end
      best = node
      node.child_nodes.each { |c| walk.call(c) if c }
    end
  end
  walk.call(root)
  best
end

def collect_matches(root, pattern, source, pattern_source)
  out = []
  walk = lambda do |node|
    bindings = {}
    if match_node(pattern, node, bindings)
      out << { node: node, bindings: bindings }
    end
    node.child_nodes.each { |c| walk.call(c) if c }
  end
  walk.call(root)
  out
end

# Structural comparison. A GlobalVariableReadNode in the pattern matches
# any candidate node. `$_` is anonymous; other `$name` records a binding.
# Repeated `$name` must match equal candidate source text.
def match_node(pattern, candidate, bindings)
  if pattern.is_a?(Prism::GlobalVariableReadNode)
    raw = pattern.name.to_s
    # `:$name` - strip the leading `$`
    name = raw.start_with?('$') ? raw[1..] : raw
    return true if name == '_'
    existing = bindings[name]
    if existing
      return existing.slice == candidate.slice
    end
    bindings[name] = candidate
    return true
  end
  return false unless pattern.class == candidate.class
  # Identity check: for nodes whose meaning depends on a name attribute
  # beyond their AST children (CallNode method name, DefNode method name,
  # ConstantReadNode constant name, local/instance/class-variable reads,
  # etc.), require name equality. Without this, foo($x) would match
  # bar($x) because their AST child shapes are identical.
  if pattern.respond_to?(:name) && candidate.respond_to?(:name)
    return false unless pattern.name == candidate.name
  end
  p_children = pattern.child_nodes.compact
  c_children = candidate.child_nodes.compact
  if p_children.empty? && c_children.empty?
    # Leaf comparison via slice text catches literals (IntegerNode,
    # StringNode, SymbolNode) whose value lives outside child_nodes.
    return pattern.slice == candidate.slice
  end
  return false if p_children.length != c_children.length
  p_children.each_with_index do |pc, i|
    return false unless match_node(pc, c_children[i], bindings)
  end
  true
end

# ---------------------------------------------------------------------------
# Byte-range rewriter
# ---------------------------------------------------------------------------

def replace_byte_range(source, byte_offset, byte_length, replacement)
  before = source.byteslice(0, byte_offset)
  after = source.byteslice(byte_offset + byte_length, source.bytesize - (byte_offset + byte_length))
  (before || ''.b) + replacement + (after || ''.b)
end

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def handle(request)
  op = request['op']
  case op
  when 'walk_symbols'      then op_walk_symbols(request)
  when 'insert_child'      then op_insert_child(request)
  when 'remove_child'      then op_remove_child(request)
  when 'find_matches'      then op_find_matches(request)
  when 'apply_replacement' then op_apply_replacement(request)
  when 'probe_parse'       then op_probe_parse(request)
  when 'shutdown'
    write_frame(ok_response)
    exit 0
  else
    error_response('unknown_op', "unknown op: #{op}")
  end
rescue => e
  error_response('bridge', e.message || e.to_s)
end

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

loop do
  frame = read_frame
  break if frame.nil?
  next if frame.bytesize.zero?
  begin
    request = JSON.parse(frame.force_encoding('UTF-8'))
  rescue JSON::ParserError
    write_frame(error_response('bad_request', 'malformed JSON'))
    next
  end
  unless request.is_a?(Hash) && request['op'].is_a?(String)
    write_frame(error_response('bad_request', 'malformed request'))
    next
  end
  write_frame(handle(request))
end
