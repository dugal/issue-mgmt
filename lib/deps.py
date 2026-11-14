"""deps -- author and read the `depends_on:` frontmatter block sequence.

The `depends_on:` field is a YAML block sequence of `{to, type}` mappings
(plus optional `note`/`created` keys), the frontmatter-native dependency
edge model (markdown is the source of truth). Neither
`frontmatter.set_field` (scalar-only; `_needs_quoting` quotes `[`/`]`/`:`)
nor `validate._parse_frontmatter` (scalar-only) can read or write a block
sequence, so this module owns the block-aware read/write primitives.

The canonical serialized shape is:

    depends_on:
      - to: IM-101
        type: blocks

That is: `depends_on:` at column 0, each edge a `  - to: <ID>` line
(2-space list marker) followed by `    type: <KIND>` (4-space
continuation), `to` before `type`. Optional `note:`/`created:` follow as
further 4-space lines. An empty edge set serializes to the single inline
line `depends_on: []` -- NEVER an absent key: an absent `depends_on` reads
as "unmanaged" (leave any existing edges alone), while `[]` is the
authoritative empty list that retracts stale IM-kind edges. The
validate.py dep-shape lint keys off that same list-vs-absent distinction.
"""

import re

import idresolve

# The four dependency kinds issue-mgmt recognizes.
# `blocks` is the sole ordering relation (the only kind that gates
# readiness); the other three are provenance/containment metadata.
IM_DEP_KINDS = ("blocks", "parent-child", "related", "discovered-from")

# Prefix-agnostic issue-id shape (PREFIX-NNN). Aliased to the single shared
# grammar in lib/idresolve.py; its capture group is unused here.
_TARGET_RE = idresolve.ID_RE

# Keys preserved on an edge mapping, in serialization order. `to` and
# `type` are the load-bearing pair; `note`/`created` ride along if present.
_EDGE_KEY_ORDER = ("to", "type", "note", "created")


def _strip_quotes(value):
    v = value.strip()
    if len(v) >= 2 and ((v[0] == v[-1] == '"') or (v[0] == v[-1] == "'")):
        return v[1:-1]
    return v


def _top_level_index(fm_lines, key):
    """Index of the top-level (non-indented) line for `key`, or None."""
    pattern = re.compile(r"^%s:(\s|$)" % re.escape(key))
    for i, line in enumerate(fm_lines):
        content = line.rstrip("\r\n")
        if not content or content[0] in " \t":
            continue
        if pattern.match(content) or content == "%s:" % key:
            return i
    return None


def _block_bounds(fm_lines, key):
    """(start, end) line-index bounds of the `key:` block, or None.

    `end` is exclusive: the index of the first following line that is not
    an indented child. A blank line inside the block terminates it (the
    frontmatter writers never emit blank lines mid-block).
    """
    start = _top_level_index(fm_lines, key)
    if start is None:
        return None
    end = start + 1
    while end < len(fm_lines):
        content = fm_lines[end].rstrip("\r\n")
        if not content or content[0] not in " \t":
            break
        end += 1
    return start, end


def get_deps(fm_lines):
    """Return the parsed `depends_on` edges, or None when the key is absent.

    Returns:
      None            -- key absent (unmanaged; leave existing edges).
      []              -- key present as `depends_on: []` (managed, empty).
      [{"to":..., "type":..., ...}, ...] -- block-sequence edges.

    Each edge is a dict preserving whatever keys were on the mapping
    (`to`, `type`, and any `note`/`created`). A bare string list item
    with no `key: value` payload (`- IM-101`) parses to an empty mapping
    `{}` (the item is preserved as an entry rather than silently dropped,
    but carries no `to`; the validate.py dep-shape lint flags it as
    `dep-missing-to`).
    """
    idx = _top_level_index(fm_lines, "depends_on")
    if idx is None:
        return None
    content = fm_lines[idx].rstrip("\r\n")
    _, _, raw = content.partition(":")
    inline = _strip_quotes(raw)
    if inline:
        # Inline form. Only `[]` is meaningful for an edge set; any other
        # inline scalar is a malformed/unmanaged value -- surface it as an
        # empty managed list is wrong, so return [] only for the literal
        # empty flow list and otherwise treat as no parseable edges.
        if inline == "[]":
            return []
        # A non-empty inline value is not a block sequence we author;
        # report zero edges so callers rewrite it into canonical shape.
        return []
    # Block form: walk indented child lines.
    _start, end = _block_bounds(fm_lines, "depends_on")
    edges = []
    current = None
    for line in fm_lines[idx + 1:end]:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("- "):
            if current is not None:
                edges.append(current)
            current = {}
            payload = s[2:].strip()
            kv = _split_kv(payload)
            if kv is not None:
                current[kv[0]] = kv[1]
        elif current is not None:
            kv = _split_kv(s)
            if kv is not None:
                current[kv[0]] = kv[1]
    if current is not None:
        edges.append(current)
    return edges


def _split_kv(text):
    key, sep, rest = text.partition(":")
    if not sep:
        return None
    return key.strip(), _strip_quotes(rest.strip())


def set_deps(fm_lines, edges):
    """Splice the `depends_on:` block into `fm_lines` in place.

    Empty edges -> the single inline line `depends_on: []` (authoritative
    empty list; never delete the key). Non-empty -> the canonical
    block-sequence shape (2-space list marker, 4-space continuation,
    `to` before `type`). Replaces an existing block by its bounds,
    otherwise appends at the end of the frontmatter.
    """
    if not edges:
        block = ["depends_on: []\n"]
    else:
        block = ["depends_on:\n"]
        for edge in edges:
            keys = [k for k in _EDGE_KEY_ORDER if k in edge]
            # Preserve any unexpected keys after the known ones.
            keys += [k for k in edge if k not in _EDGE_KEY_ORDER]
            if not keys:
                continue
            first = keys[0]
            block.append("  - %s: %s\n" % (first, edge[first]))
            for k in keys[1:]:
                block.append("    %s: %s\n" % (k, edge[k]))
    bounds = _block_bounds(fm_lines, "depends_on")
    if bounds is not None:
        start, end = bounds
        fm_lines[start:end] = block
    else:
        fm_lines.extend(block)


def add_dep(edges, kind, target):
    """Return a new edge list with `{to: target, type: kind}` appended.

    Idempotent: an existing edge with the same `(to, type)` pair is left
    untouched (no duplicate). `edges` may be None (absent key) -- treated
    as an empty starting list.
    """
    result = list(edges or [])
    for edge in result:
        if edge.get("to") == target and edge.get("type") == kind:
            return result
    result.append({"to": target, "type": kind})
    return result


def remove_dep(edges, target, kind=None):
    """Return a new edge list with entries matching `target` dropped.

    When `kind` is given, only edges whose `type` also matches are
    dropped. `edges` may be None -- returns an empty list.
    """
    result = []
    for edge in (edges or []):
        if edge.get("to") == target and (kind is None or edge.get("type") == kind):
            continue
        result.append(edge)
    return result


def validate_kind(kind):
    """Raise ValueError when `kind` is not one of the four IM dep kinds."""
    if kind not in IM_DEP_KINDS:
        raise ValueError(
            "invalid dependency kind %r (expected one of: %s)"
            % (kind, ", ".join(IM_DEP_KINDS))
        )


def validate_target(target):
    """Raise ValueError when `target` is not a syntactically valid id.

    Shape-only, case-insensitive: `im-84` passes here and is
    canonicalized to `IM-084` at write time (D1). The check accepts the
    loose `PREFIX-<int>` form (any zero-padding); it does not resolve or
    rewrite the target.
    """
    if idresolve.split_id(target) is None:
        raise ValueError(
            "invalid dependency target %r (expected a PREFIX-NNN issue id)"
            % (target,)
        )


def split_operand(operand, kind=None, *, fused_required=False):
    """Parse a dependency operand into `(target, kind_or_None)`.

    One grammar for both halves of the authoring surface (IM-243). The
    edge-authoring verbs encoded the (kind, target) pair two ways within
    one verb through v3.4.x -- `dep add ID KIND:TARGET` fused it, while
    `dep remove ID TARGET --kind KIND` split it, so an agent that learned
    the add form and tried `dep remove ID blocks:XYZ-9` got a failed
    lookup for a target literally named `blocks:XYZ-9`. Removal now
    accepts BOTH spellings and this function is the one place either is
    read.

    Rules:
      - A colon-free operand is the target; `kind` (from `--kind`) rides
        through unchanged, so the split form is untouched.
      - A colon-headed operand whose head is one of the four dependency
        kinds is the fused form, and its head becomes the kind.
      - A colon-headed operand whose head is NOT a kind is an error
        naming the four kinds -- never a target silently containing a
        colon, which is not a valid id shape anyway.
      - A fused kind plus a conflicting `--kind` is an error; a fused kind
        plus a MATCHING `--kind` is accepted (the caller said one thing
        twice).

    `fused_required=True` is the add side, where the kind is mandatory and
    the split form was never accepted.

    Raises ValueError with a stderr-ready message (CLI exit 2, the
    argv/usage contract) for every rejection; the caller prefixes its own
    name. Validates both components, so a successful return needs no
    further `validate_kind` / `validate_target` call.
    """
    head, sep, tail = operand.partition(":")
    if sep and head in IM_DEP_KINDS:
        if kind is not None and kind != head:
            raise ValueError(
                "conflicting dependency kind: operand says %r, --kind says "
                "%r -- pass the kind once" % (head, kind)
            )
        target, kind = tail, head
    elif sep:
        raise ValueError(
            "invalid dependency kind %r in %r (expected one of: %s) -- a "
            "colon-free operand is read as a bare target" % (
                head, operand, ", ".join(IM_DEP_KINDS))
        )
    else:
        target = operand

    if fused_required and kind is None:
        raise ValueError(
            "expected KIND:TARGET (e.g. blocks:XYZ-9), where KIND is one "
            "of: %s" % ", ".join(IM_DEP_KINDS)
        )
    if kind is not None:
        validate_kind(kind)
    validate_target(target)
    return target, kind
