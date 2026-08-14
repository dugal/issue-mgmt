"""Structure-preserving frontmatter and section editing for issue files.

Used by mutating subcommands (edit-issue, transition-issue) and
read-only ones (search-issues) that need to inspect or update issue
state without rewriting unrelated content.

Public surface:

    fm_lines, body_text = read_issue(path)
    fm_lines, body_text = parse_issue(text)
    text = serialize_issue(fm_lines, body_text)

    value = get_field(fm_lines, key)
    set_field(fm_lines, key, value)         # in place, returns fm_lines

    bounds = find_section(body_text, name)  # (heading_idx, body_start, body_end) or None
    names = list_section_names(body_text)   # H2 names in document order
    new_body = edit_section(body_text, name, op, text)  # op: append|prepend|replace
    new_body = add_section(body_text, name, body, after=..., before=..., to_position=...)
    new_body = rename_section(body_text, old_name, new_name)
    new_body = remove_section(body_text, name)
    new_body = move_section(body_text, name, after=..., before=..., to_position=...)

Round-trip property: read_issue then serialize_issue on the same (fm_lines,
body_text) without intermediate edits returns the original file byte-for-byte
when the file's frontmatter fences use exactly '---\\n'. set_field preserves
indent, separator whitespace, quoting style, and trailing inline comments on
the edited line. edit_section preserves every byte outside the named section.
The structural helpers (add/rename/remove/move) preserve every byte outside the
section they touch.

## Internal API

The structural helpers below are part of the module's public surface but are
consumed only by `lib/editcore.py`'s structural-op dispatch
(--add-section / --rename-section / --remove-section / --move-section, exposed
via `issue edit`) and by each other within this module. Their CLI coverage in
`make coverage-black-box` is shallow when `tests/verification/im-061-structural.sh`
contributes no instrumented runs (a separate CLI-test instrumentation gap,
not a dead-code finding). Audited (bucket-3 walkthrough):

- list_section_names -- called by add_section, move_section, and tests
- add_section, rename_section, remove_section, move_section -- called by
  lib/editcore.py (structural-op dispatch)
- _build_section_block -- internal helper for add_section
- _normalize_extracted_block -- internal helper for move_section
- _ensure_trailing_blank -- internal helper for add_section and move_section
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional, Tuple


_FIELD_LINE_RE = re.compile(
    r"^(\s*)([A-Za-z_][A-Za-z0-9_.-]*)(:[ \t]?)(.*)$"
)


def read_issue(path) -> Tuple[List[str], str]:
    """Read an issue file and split into raw frontmatter lines + body text."""
    text = Path(path).read_text(encoding="utf-8")
    return parse_issue(text, path=path)


def parse_issue(text: str, *, path=None) -> Tuple[List[str], str]:
    """Parse text into (frontmatter_lines, body_text).

    frontmatter_lines is the raw list of lines between the opening and
    closing '---' fences, with original line endings preserved. body_text
    is everything after the closing fence (including its trailing newline,
    if any).
    """
    pathstr = str(path) if path is not None else "<text>"
    lines = text.splitlines(keepends=True)
    if not lines or _strip_fence(lines[0]) != "---":
        raise ValueError(f"{pathstr}: no opening frontmatter fence")
    close_idx = None
    for i in range(1, len(lines)):
        if _strip_fence(lines[i]) == "---":
            close_idx = i
            break
    if close_idx is None:
        raise ValueError(f"{pathstr}: no closing frontmatter fence")
    fm_lines = lines[1:close_idx]
    body_text = "".join(lines[close_idx + 1:])
    return fm_lines, body_text


def serialize_issue(fm_lines: List[str], body_text: str) -> str:
    """Reassemble (fm_lines, body_text) into a full issue file."""
    return "---\n" + "".join(fm_lines) + "---\n" + body_text


def get_field(fm_lines: List[str], key: str) -> Optional[str]:
    """Return the unquoted value of key, or None if absent."""
    for line in fm_lines:
        parsed = _parse_field_line(line)
        if parsed is None:
            continue
        if parsed.key == key:
            return _strip_matched_quotes(parsed.value)
    return None


def set_field(fm_lines: List[str], key: str, value: str) -> List[str]:
    """Set key to value in fm_lines (in place); return fm_lines.

    If key exists: replace the value portion, preserving indent, separator
    whitespace, quoting style of the previous value, and trailing inline
    comments.
    If key is absent: append 'key: <quoted-value>' as a new line.
    """
    for i, line in enumerate(fm_lines):
        parsed = _parse_field_line(line)
        if parsed is None or parsed.key != key:
            continue
        new_value_str = _quote_value_like(parsed.value, value)
        fm_lines[i] = (
            f"{parsed.indent}{parsed.key}{parsed.sep}{new_value_str}"
            f"{parsed.comment}{parsed.newline}"
        )
        return fm_lines
    fm_lines.append(f"{key}: {_default_quote(value) if _needs_quoting(value) else value}\n")
    return fm_lines


def find_section(body_text: str, name: str) -> Optional[Tuple[int, int, int]]:
    """Locate a level-2 section by exact name.

    Returns (heading_idx, body_start, body_end) in line-index space of
    body_text.splitlines(keepends=True): heading_idx is the line of
    '## <name>', body_start is heading_idx + 1, body_end is the index of
    the next '## ' heading or len(lines) if none follows.

    Returns None if the section is not found.
    """
    lines = body_text.splitlines(keepends=True)
    heading_idx = None
    for i, line in enumerate(lines):
        if _is_h2(line, name):
            heading_idx = i
            break
    if heading_idx is None:
        return None
    end_idx = len(lines)
    for j in range(heading_idx + 1, len(lines)):
        if _is_any_h2(lines[j]):
            end_idx = j
            break
    return heading_idx, heading_idx + 1, end_idx


def edit_section(body_text: str, name: str, op: str, text: str) -> str:
    """Apply op (append|prepend|replace) with text to the named section.

    Returns the updated body_text. Raises ValueError if the section is
    not found or op is invalid.

    The function preserves every byte outside the section (heading line +
    preceding text + following sections). Within the section, blank-line
    discipline is normalized: heading is followed by a blank line, the
    section ends with a blank line before the next '## ' (or single
    trailing newline at EOF).
    """
    if op not in ("append", "prepend", "replace"):
        raise ValueError(f"invalid op: {op!r} (use append, prepend, or replace)")
    bounds = find_section(body_text, name)
    if bounds is None:
        raise ValueError(f"section not found: {name!r}")
    heading_idx, body_start, body_end = bounds

    lines = body_text.splitlines(keepends=True)
    section_body = "".join(lines[body_start:body_end])
    inner = section_body.strip("\n")
    text_norm = text.rstrip("\n")

    if op == "replace":
        new_inner = text_norm
    elif op == "append":
        new_inner = (inner + "\n\n" + text_norm) if inner else text_norm
    else:  # prepend
        new_inner = (text_norm + "\n\n" + inner) if inner else text_norm

    has_next = body_end < len(lines)
    if has_next:
        new_section = "\n" + new_inner + "\n\n"
    else:
        new_section = "\n" + new_inner + "\n"

    new_lines = lines[: heading_idx + 1] + [new_section] + lines[body_end:]
    return "".join(new_lines)


def list_section_names(body_text: str) -> List[str]:
    """Return all level-2 section names in body_text, in document order.

    Duplicate names are returned in their order of appearance; callers
    that need uniqueness should check it themselves (the structural
    helpers below treat duplicates as a precondition violation since
    add_section refuses to create a new section whose name collides
    with an existing one).
    """
    names = []
    for line in body_text.splitlines(keepends=True):
        if _is_any_h2(line):
            content = line.rstrip("\r\n").rstrip()
            names.append(content[3:].strip())
    return names


def add_section(
    body_text: str,
    name: str,
    body: str = "",
    *,
    after: Optional[str] = None,
    before: Optional[str] = None,
    to_position: Optional[int] = None,
) -> str:
    """Insert a new level-2 section into body_text.

    Position selectors (mutually exclusive; pass at most one):
      after=NAME         -- after the existing section NAME
      before=NAME        -- immediately before the section NAME
      to_position=N      -- before the section currently at 0-indexed
                            position N; N == len(sections) appends
      (all None)         -- at end of document (default)

    Raises ValueError if a section with `name` already exists, if the
    anchor for after/before is missing, or if to_position is out of
    range. The new block is `## NAME` + blank + body + trailing
    separator; bytes outside the inserted block are preserved.
    """
    if find_section(body_text, name) is not None:
        raise ValueError(f"section already exists: {name!r}")
    specified = sum(x is not None for x in (after, before, to_position))
    if specified > 1:
        raise ValueError(
            "add_section: at most one of after/before/to_position"
        )

    lines = body_text.splitlines(keepends=True)

    if after is not None:
        bounds = find_section(body_text, after)
        if bounds is None:
            raise ValueError(f"section not found: {after!r}")
        insert_idx = bounds[2]
    elif before is not None:
        bounds = find_section(body_text, before)
        if bounds is None:
            raise ValueError(f"section not found: {before!r}")
        insert_idx = bounds[0]
    elif to_position is not None:
        names = list_section_names(body_text)
        if not (0 <= to_position <= len(names)):
            raise ValueError(
                f"position out of range: {to_position} "
                f"(have {len(names)} sections)"
            )
        if to_position == len(names):
            insert_idx = len(lines)
        else:
            target = names[to_position]
            tbounds = find_section(body_text, target)
            insert_idx = tbounds[0]
    else:
        insert_idx = len(lines)

    at_eof = insert_idx >= len(lines)
    block = _build_section_block(name, body, at_eof=at_eof)

    if at_eof:
        prefix = "".join(lines[:insert_idx])
        prefix = _ensure_trailing_blank(prefix)
        return prefix + block

    new_lines = lines[:insert_idx] + [block] + lines[insert_idx:]
    return "".join(new_lines)


def rename_section(body_text: str, old_name: str, new_name: str) -> str:
    """Rename the level-2 heading `## OLD` to `## NEW`.

    Body and bytes outside the heading line are preserved verbatim.
    Raises ValueError if old_name is not found or new_name already
    exists. Renaming to the same name is a no-op.
    """
    if old_name == new_name:
        bounds = find_section(body_text, old_name)
        if bounds is None:
            raise ValueError(f"section not found: {old_name!r}")
        return body_text
    bounds = find_section(body_text, old_name)
    if bounds is None:
        raise ValueError(f"section not found: {old_name!r}")
    if find_section(body_text, new_name) is not None:
        raise ValueError(f"section already exists: {new_name!r}")

    heading_idx, _body_start, _body_end = bounds
    lines = body_text.splitlines(keepends=True)
    old_line = lines[heading_idx]
    if old_line.endswith("\r\n"):
        nl = "\r\n"
    elif old_line.endswith("\n"):
        nl = "\n"
    else:
        nl = ""
    content = old_line[: -len(nl)] if nl else old_line
    idx = content.find("##")
    indent = content[:idx]
    lines[heading_idx] = f"{indent}## {new_name}{nl}"
    return "".join(lines)


def remove_section(body_text: str, name: str) -> str:
    """Remove a level-2 section (heading line and body up to next H2 or EOF).

    Raises ValueError if the section is not found. Bytes outside the
    removed range are preserved verbatim.
    """
    bounds = find_section(body_text, name)
    if bounds is None:
        raise ValueError(f"section not found: {name!r}")
    heading_idx, _body_start, body_end = bounds
    lines = body_text.splitlines(keepends=True)
    return "".join(lines[:heading_idx] + lines[body_end:])


def move_section(
    body_text: str,
    name: str,
    *,
    after: Optional[str] = None,
    before: Optional[str] = None,
    to_position: Optional[int] = None,
) -> str:
    """Move a level-2 section to a new position.

    Exactly one of after/before/to_position must be specified. The
    section is extracted verbatim and re-inserted at the destination.
    Body and bytes inside the moved section are preserved.

    Raises ValueError if the named section is missing, if the
    destination anchor is missing, if to_position is out of range, or
    if the destination anchor is the moved section itself.
    """
    specified = sum(x is not None for x in (after, before, to_position))
    if specified != 1:
        raise ValueError(
            "move_section: exactly one of after/before/to_position required"
        )
    bounds = find_section(body_text, name)
    if bounds is None:
        raise ValueError(f"section not found: {name!r}")
    if after == name or before == name:
        raise ValueError(
            f"move_section: destination anchor cannot be the moved "
            f"section itself: {name!r}"
        )

    heading_idx, _body_start, body_end = bounds
    lines = body_text.splitlines(keepends=True)
    extracted = lines[heading_idx:body_end]
    remaining_lines = lines[:heading_idx] + lines[body_end:]
    remaining_text = "".join(remaining_lines)

    if to_position is not None:
        names_after = list_section_names(remaining_text)
        if not (0 <= to_position <= len(names_after)):
            raise ValueError(
                f"position out of range: {to_position} "
                f"(have {len(names_after)} sections after extraction)"
            )
        if to_position == len(names_after):
            insert_idx = len(remaining_lines)
        else:
            target = names_after[to_position]
            tbounds = find_section(remaining_text, target)
            insert_idx = tbounds[0]
    elif after is not None:
        tbounds = find_section(remaining_text, after)
        if tbounds is None:
            raise ValueError(f"section not found: {after!r}")
        insert_idx = tbounds[2]
    else:  # before is not None
        tbounds = find_section(remaining_text, before)
        if tbounds is None:
            raise ValueError(f"section not found: {before!r}")
        insert_idx = tbounds[0]

    at_eof = insert_idx >= len(remaining_lines)
    normalized = _normalize_extracted_block(extracted, at_eof=at_eof)

    if at_eof:
        prefix = "".join(remaining_lines[:insert_idx])
        prefix = _ensure_trailing_blank(prefix)
        return prefix + normalized

    return (
        "".join(remaining_lines[:insert_idx])
        + normalized
        + "".join(remaining_lines[insert_idx:])
    )


def _build_section_block(name: str, body: str, *, at_eof: bool) -> str:
    """Render `## NAME\\n\\n<body>\\n` (+ trailing blank if not at EOF).

    Empty body collapses to `## NAME\\n[\\n]` (heading + optional
    trailing blank). The blank between heading and body always renders
    when body is non-empty; this matches edit_section's discipline.
    """
    body_stripped = body.strip("\n")
    if body_stripped:
        block = f"## {name}\n\n{body_stripped}\n"
    else:
        block = f"## {name}\n"
    if not at_eof:
        block += "\n"
    return block


def _normalize_extracted_block(extracted_lines: List[str], *, at_eof: bool) -> str:
    """Rewrite an extracted section block to land cleanly at destination.

    Strips trailing blank lines, then re-adds the right trailing
    separator (one trailing newline at EOF, blank line otherwise).
    The heading and section body are preserved verbatim; only the
    trailing whitespace is normalized.
    """
    text = "".join(extracted_lines)
    text = text.rstrip("\n")
    if at_eof:
        return text + "\n"
    return text + "\n\n"


def _ensure_trailing_blank(text: str) -> str:
    """Ensure text ends with `\\n\\n` (one blank line of separation).

    Empty text is returned unchanged. Text ending in a single `\\n`
    gets one more `\\n` appended; text ending in two or more `\\n`s
    is normalized to exactly `\\n\\n`. Text not ending in `\\n` gets
    `\\n\\n` appended.
    """
    if text == "":
        return text
    stripped = text.rstrip("\n")
    return stripped + "\n\n"


# ----------------------------------------------------------------------
# Internals
# ----------------------------------------------------------------------


class _ParsedLine:
    __slots__ = ("indent", "key", "sep", "value", "comment", "newline")

    def __init__(self, indent, key, sep, value, comment, newline):
        self.indent = indent
        self.key = key
        self.sep = sep
        self.value = value
        self.comment = comment
        self.newline = newline


def _parse_field_line(line: str) -> Optional[_ParsedLine]:
    if line.endswith("\r\n"):
        newline = "\r\n"
        content = line[:-2]
    elif line.endswith("\n"):
        newline = "\n"
        content = line[:-1]
    else:
        newline = ""
        content = line
    m = _FIELD_LINE_RE.match(content)
    if not m:
        return None
    indent, key, sep, rest = m.groups()
    value, comment = _split_value_comment(rest)
    return _ParsedLine(indent, key, sep, value.rstrip(), comment, newline)


def _split_value_comment(rest: str) -> Tuple[str, str]:
    """Return (value, comment) split at the first ' #' outside quotes.

    The comment retains its leading whitespace. The value retains no
    trailing whitespace (caller rstrips).
    """
    in_single = False
    in_double = False
    for i, ch in enumerate(rest):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if i == 0 or rest[i - 1].isspace():
                j = i
                while j > 0 and rest[j - 1].isspace():
                    j -= 1
                return rest[:j], rest[j:]
    return rest, ""


def _strip_matched_quotes(value: str) -> str:
    if len(value) >= 2 and (
        (value[0] == value[-1] == '"') or (value[0] == value[-1] == "'")
    ):
        return value[1:-1]
    return value


def _quote_value_like(old_value: str, new_value: str) -> str:
    """Emit new_value with the same outer quoting as old_value."""
    if len(old_value) >= 2:
        if old_value[0] == '"' and old_value[-1] == '"':
            return _double_quote(new_value)
        if old_value[0] == "'" and old_value[-1] == "'":
            return _single_quote(new_value)
    if _needs_quoting(new_value):
        return _default_quote(new_value)
    return new_value


def _double_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _single_quote(value: str) -> str:
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def _default_quote(value: str) -> str:
    return _double_quote(value)


def _needs_quoting(value: str) -> bool:
    if value == "":
        return False
    if value != value.strip():
        return True
    if any(ch in value for ch in ':#"\'\n\r\t[]{},&*!|>%@`'):
        return True
    if value.lower() in {"null", "true", "false", "yes", "no", "on", "off", "~"}:
        return True
    return False


def _strip_fence(line: str) -> str:
    return line.rstrip("\r\n").rstrip()


def _is_h2(line: str, name: str) -> bool:
    stripped = line.rstrip("\r\n").rstrip()
    if not stripped.startswith("## "):
        return False
    return stripped[3:].strip() == name


def _is_any_h2(line: str) -> bool:
    stripped = line.rstrip("\r\n").rstrip()
    return stripped.startswith("## ")


def find_escaping_header(text: str, level: int = 2) -> Optional[str]:
    """Return the first line of `text` that is an ATX header at or above
    `level` (would escape a level-`level` section when spliced into its
    body), or None if the text nests cleanly.

    The check is deliberately fence-naive, matching `find_section`: the
    section parser treats any `## ` line as a boundary, including inside
    a code fence, so such a line genuinely fragments the file and must
    be caught here too. Deeper headers (`###`+) nest fine and pass.
    """
    prefixes = tuple("#" * n + " " for n in range(1, level + 1))
    for line in text.splitlines():
        stripped = line.rstrip("\r\n").rstrip()
        if stripped.startswith(prefixes):
            return stripped
    return None


# --- Block-aware dict parser -------------------------------------------
#
# `get_field`/`set_field` above are scalar-only line editors. A few
# consumers (the depends_on shape lint in validate.py; issue-dep list)
# need the frontmatter parsed into a dict that understands flow lists
# and one-level block sequences/maps. `parse_frontmatter_lines` is that
# reader; it lived in lib/bridge.py until the bd retirement and
# moved here as the natural home once the bridge was removed.

_DICT_FIELD_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]*):\s?(.*)$")


def parse_frontmatter_lines(fm_lines: List[str]) -> dict:
    """Parse raw frontmatter lines (the lines between the --- fences)
    into a dict.

    Conservative reader for the YAML subset issue-mgmt's own writers
    produce: top-level scalars (with optional quoting and trailing
    comments), flow-style lists (`tags: [a, b]`), one-level nested maps
    (`claim:`), and block lists whose items are scalars or flat maps
    (`depends_on:`). Anything deeper is kept as raw lines under the key
    and ignored by the callers.
    """
    fm: dict = {}
    i, n = 0, len(fm_lines)
    while i < n:
        raw = fm_lines[i].rstrip("\r\n")
        i += 1
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if raw[:1] in (" ", "\t"):
            continue  # indentation without a tracked parent; skip
        m = _DICT_FIELD_RE.match(raw)
        if not m:
            continue
        key, rest = m.group(1), m.group(2)
        value_str = _split_value_comment(rest)[0].strip()
        if value_str == "":
            block = []
            while i < n:
                nxt = fm_lines[i].rstrip("\r\n")
                if nxt.strip() and nxt[:1] not in (" ", "\t"):
                    break
                block.append(nxt)
                i += 1
            fm[key] = _parse_block(block)
        else:
            fm[key] = _parse_scalar_or_flow(value_str)
    return fm


def _parse_scalar_or_flow(value_str: str):
    if value_str.startswith("[") and value_str.endswith("]"):
        inner = value_str[1:-1].strip()
        if not inner:
            return []
        return [
            _strip_matched_quotes(part.strip())
            for part in _split_top_commas(inner)
        ]
    return _strip_matched_quotes(value_str)


def _split_top_commas(s: str) -> List[str]:
    parts, buf = [], []
    in_single = in_double = False
    for ch in s:
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        if ch == "," and not in_single and not in_double:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return parts


def _parse_block(block_lines: List[str]):
    """Parse an indented block: list of scalars/flat-maps, or a flat map.

    Returns None for an empty block.
    """
    items = [ln for ln in block_lines if ln.strip()
             and not ln.strip().startswith("#")]
    if not items:
        return None
    if items[0].lstrip().startswith("- "):
        result = []
        current = None
        for ln in items:
            s = ln.strip()
            if s.startswith("- "):
                if current is not None:
                    result.append(current)
                payload = s[2:].strip()
                kv = _flat_kv(payload)
                if kv is not None:
                    current = dict([kv])
                else:
                    result.append(_strip_matched_quotes(payload))
                    current = None
            else:
                kv = _flat_kv(s)
                if kv is not None and isinstance(current, dict):
                    current[kv[0]] = kv[1]
        if current is not None:
            result.append(current)
        return result
    mapping = {}
    for ln in items:
        kv = _flat_kv(ln.strip())
        if kv is not None:
            mapping[kv[0]] = kv[1]
    return mapping


def _flat_kv(text: str):
    m = _DICT_FIELD_RE.match(text)
    if not m:
        return None
    key, rest = m.group(1), m.group(2)
    value = _split_value_comment(rest)[0].strip()
    return key, _strip_matched_quotes(value)
