"""`issue show` -- print an issue's current working-tree contents.

Reference verb module for the consolidated CLI. Read-only
counterpart to `issue edit`; reads issue files from disk so uncommitted
edits are visible, and acquires no locks. Ported from the flat `issue-show`
binary (--section, multi-id batch); the rendering data path
is unchanged -- only the arg-surface glue moved onto the dispatcher.

Multi-id batch mode, --section atomicity, --frontmatter/--no-body, and the
JSON shapes are byte-identical to the flat binary so skills and the future
MCP server need no retest.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

import errors  # noqa: E402
import frontmatter  # noqa: E402  (lib/ is on sys.path via the entrypoint)
import jsonfields  # noqa: E402
import idresolve  # noqa: E402

from . import common

VERB = "show"
TIER = "tier1"
SUMMARY = "print an issue's current contents (whole file, section, or frontmatter)"
ORDER = 60

# Surface-manifest declarations (IM-245); protocol table in lib/cli/__init__.py.
SURFACE_CLASS = "point-read"
SURFACE_STORE = "flag"
# IM-260 C0: the exemplar conversion. Every failure path -- the pre-read
# usage refusals, argparse's own refusals (via
# `common.structured_parser_errors`), the store gate, and each per-id
# miss -- exits through the branchable payload. Ratchets: may never
# revert to `unconverted`.
SURFACE_STRUCTURED_ERRORS = "converted"

# Top-level keys of each per-id --json object; the field-selection set for
# `--json <fields>`. The shape is conditional: `sections_requested`
# / `sections` appear only under --section, `error` only on a failed id, and
# `full_text` is null under --section/--no-body -- a requested-but-absent
# field is simply omitted from that object's projection.
VALID_JSON_FIELDS = (
    "id", "file", "store", "frontmatter",
    "sections_requested", "sections", "full_text", "error",
)

_DESCRIPTION = """\
Print one or more issue files as they are on disk right now (uncommitted
edits visible). Read-only: no locks acquired.

Default (no flags) prints the file byte-for-byte. --section NAME (repeatable)
prints only that level-2 section; a missing section is exit 1 and suppresses
all output for that id. --frontmatter prepends the raw frontmatter block;
--no-body prints frontmatter only (rejects --section).

Multi-id batch mode: two or more ids render each under a `=== <id> ===`
header in text mode; --json emits a top-level array in positional order.
Exit 1 if any id failed.

--json accepts an optional comma-separated field list (e.g. --json id,file)
that projects each per-id object to those top-level fields, in the order
given (applied inside each element of the batch array); bare --json emits the
full shape and an unknown field name is a usage error (exit 2)."""

_EPILOG = """\
Examples:
  issue show ABC-123
  issue show ABC-124 --section Design
  issue show ABC-124 --section "Implementation Notes" --section Summary
  issue show ABC-123 --no-body
  issue show ABC-123 --frontmatter --section "Acceptance Criteria"
  issue show ABC-123 ABC-125 ABC-126 --json
  issue show ABC-123 ABC-125 --json id,file
"""


def register(subparsers):
    p = subparsers.add_parser(
        VERB,
        help=SUMMARY,
        description=_DESCRIPTION,
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("ids", nargs="*", metavar="ID", help="One or more issue IDs.")
    p.add_argument(
        "--section",
        action="append",
        default=[],
        metavar="NAME",
        dest="sections",
        help="Repeatable. Print only the named level-2 section.",
    )
    p.add_argument(
        "--frontmatter",
        action="store_true",
        dest="want_frontmatter",
        help="Also/only print the raw frontmatter block.",
    )
    p.add_argument(
        "--no-body",
        action="store_true",
        dest="no_body",
        help="Print frontmatter only (mutually exclusive with --section).",
    )
    common.add_json_field_flag(p)
    common.add_store_flag(p)
    common.add_version_flag(p)
    common.structured_parser_errors(p)
    p.set_defaults(_run=run)
    return p


# --- rendering (ported verbatim from the flat issue-show binary) -----------


class IdError(Exception):
    def __init__(self, message, file_label=None, store_label=None,
                 section=None):
        super().__init__(message)
        self.file_label = file_label
        self.store_label = store_label
        self.section = section


def resolve_issue_file(issue_id, anchor, store_root, *, emit_note=None):
    """Return (issue_file_path, file_label, store_label, canonical_id).

    Routes through ``idresolve`` so ``IM-7`` / ``im-7`` resolve the
    on-disk file; the labels and the batch header use the canonical id.
    Raises ValueError on a bad shape and ``errors.StoreIntegrityError`` on a
    D2 dup-padding collision.
    """
    if not common.ID_RE.match(str(issue_id).strip().upper()):
        raise ValueError(f"invalid ID '{issue_id}' (expected <PREFIX>-NNN)")
    result = idresolve.resolve_issue_paths(
        issue_id, store_root, anchor, emit_note=emit_note
    )
    canonical_id = result.canonical_id
    prefix = result.prefix
    if store_root is not None:
        file_label = f"{prefix}/{canonical_id}.md"
        store_label = str(result.store_root)
    else:
        file_label = str(result.issue_file)
        store_label = None
    return result.issue_file, file_label, store_label, canonical_id


def frontmatter_block(fm_lines):
    return "---\n" + "".join(fm_lines) + "---\n"


def frontmatter_dict(fm_lines):
    out = {}
    for line in fm_lines:
        stripped = line.rstrip("\r\n")
        m = re.match(r"^(\s*)([A-Za-z_][A-Za-z0-9_.-]*):[ \t]?(.*)$", stripped)
        if not m:
            continue
        indent, key, rest = m.groups()
        if indent:
            continue
        value = rest
        hash_pos = -1
        in_single = False
        in_double = False
        for i, ch in enumerate(value):
            if ch == "'" and not in_double:
                in_single = not in_single
            elif ch == '"' and not in_single:
                in_double = not in_double
            elif ch == "#" and not in_single and not in_double:
                if i == 0 or value[i - 1].isspace():
                    hash_pos = i
                    break
        if hash_pos >= 0:
            value = value[:hash_pos]
        value = value.strip()
        if len(value) >= 2 and (
            (value[0] == value[-1] == '"') or (value[0] == value[-1] == "'")
        ):
            value = value[1:-1]
        out[key] = value
    return out


def extract_section_text(body_text, name):
    bounds = frontmatter.find_section(body_text, name)
    if bounds is None:
        return None
    heading_idx, body_start, body_end = bounds
    lines = body_text.splitlines(keepends=True)
    chunk = "".join(lines[heading_idx:body_end])
    return chunk.rstrip("\n") + "\n"


def render_one_text(
    issue_id, file_label, store_label, fm_lines, body_text,
    sections, want_frontmatter, no_body,
):
    parts = []
    if want_frontmatter or no_body:
        parts.append(frontmatter_block(fm_lines))
    if no_body:
        return "".join(parts)
    if sections:
        chunks = []
        for name in sections:
            chunk = extract_section_text(body_text, name)
            if chunk is None:
                raise IdError(
                    f"section '{name}' not found in {issue_id}",
                    file_label, store_label, section=name,
                )
            chunks.append(chunk.rstrip("\n"))
        section_text = "\n\n".join(chunks) + "\n"
        if parts:
            parts.append("\n")
        parts.append(section_text)
        return "".join(parts)
    if parts:
        parts.append(body_text if body_text.startswith("\n") else "\n" + body_text)
    else:
        parts.append("---\n" + "".join(fm_lines) + "---\n" + body_text)
    return "".join(parts)


def render_one_json(
    issue_id, file_label, store_label, fm_lines, body_text,
    sections, want_frontmatter, no_body,
):
    obj = {
        "id": issue_id,
        "file": file_label,
        "store": store_label,
        "frontmatter": frontmatter_dict(fm_lines),
    }
    if sections:
        rendered = {}
        for name in sections:
            chunk = extract_section_text(body_text, name)
            if chunk is None:
                raise IdError(
                    f"section '{name}' not found in {issue_id}",
                    file_label, store_label, section=name,
                )
            rendered[name] = chunk
        obj["sections_requested"] = list(sections)
        obj["sections"] = rendered
        obj["full_text"] = None
    elif no_body:
        obj["full_text"] = None
    else:
        obj["full_text"] = body_text
    return obj


def process_one(
    issue_id, anchor, store_root,
    sections, want_frontmatter, no_body, json_mode,
):
    """Render one id. Returns (True, payload) or (False, ContractError).

    IM-260: a per-id failure is returned as the typed error that names its
    condition (`not-found` for a missing file or section,
    `precondition-failed` for an unreadable or shape-invalid record,
    `store-integrity` for a dup-padding collision), so the caller renders
    one branchable payload per failed id instead of prose. Every variant
    maps to exit 1, matching the documented batch rule (exit 1 if any id
    failed) and the point-read not-found rule in `output.exit-codes`.
    """
    def _note(before, after):
        print(f"issue show: resolved {before} -> {after}", file=sys.stderr)

    try:
        issue_file, file_label, store_label, canonical_id = resolve_issue_file(
            issue_id, anchor, store_root, emit_note=_note
        )
    except ValueError as e:
        return False, errors.PreconditionError(
            str(e), reason="invalid-id", id=str(issue_id))
    except errors.StoreIntegrityError as e:
        # The typed error already names every colliding file in its fields.
        return False, e
    issue_id = canonical_id

    if not issue_file.is_file():
        return False, errors.NotFoundError(
            f"issue file not found: {issue_file}",
            id=issue_id, file=file_label, store=store_label,
        )

    try:
        fm_lines, body_text = frontmatter.read_issue(issue_file)
    except ValueError as e:
        return False, errors.PreconditionError(
            str(e), reason="unreadable-issue",
            id=issue_id, file=file_label, store=store_label,
        )

    try:
        if json_mode:
            payload = render_one_json(
                issue_id, file_label, store_label, fm_lines, body_text,
                sections, want_frontmatter, no_body,
            )
        else:
            payload = render_one_text(
                issue_id, file_label, store_label, fm_lines, body_text,
                sections, want_frontmatter, no_body,
            )
    except IdError as e:
        return False, errors.NotFoundError(
            str(e), id=issue_id, section=e.section,
            file=e.file_label, store=e.store_label,
        )

    return True, payload


def run(args) -> int:
    json_mode = jsonfields.json_enabled(args.json_mode)
    prog = "issue show"

    if not args.ids:
        common.emit_error(
            errors.UsageError("missing ID (use -h for usage)",
                              reason="missing-id"),
            prog=prog, json_mode=json_mode)
        return errors.EXIT_USAGE

    if args.no_body and args.sections:
        common.emit_error(
            errors.UsageError("--no-body is mutually exclusive with --section",
                              reason="conflicting-flags",
                              flags="--no-body,--section"),
            prog=prog, json_mode=json_mode)
        return errors.EXIT_USAGE

    for issue_id in args.ids:
        # Case-insensitive shape check; canonicalization happens in
        # process_one -> resolve_issue_file.
        if idresolve.split_id(issue_id) is None:
            common.emit_error(
                errors.UsageError(
                    f"invalid ID '{issue_id}' "
                    f"(expected <PREFIX>-NNN, e.g. ABC-123 or XYZ-9)",
                    reason="invalid-id", id=str(issue_id)),
                prog=prog, json_mode=json_mode)
            return errors.EXIT_USAGE

    anchor = common.find_project_anchor()
    store_root = common.resolve_store_root(anchor, args.store)
    rc = common.require_store(store_root, prog=prog, json_mode=json_mode,
                              legacy_anchor=anchor)
    if rc is not None:
        return rc

    multi = len(args.ids) > 1

    if json_mode:
        try:
            fields = jsonfields.parse_fields(
                args.json_mode, VALID_JSON_FIELDS, prog=prog
            )
        except jsonfields.FieldSelectionError as e:
            common.emit_error(
                errors.UsageError(e.message, flag="--json",
                                  reason="bad-json-field"),
                prog=prog, json_mode=True)
            return errors.EXIT_USAGE
        results = []
        any_failed = False
        for issue_id in args.ids:
            ok, payload = process_one(
                issue_id, anchor, store_root,
                args.sections, args.want_frontmatter, args.no_body,
                json_mode=True,
            )
            if ok:
                results.append(jsonfields.project_object(payload, fields))
            else:
                any_failed = True
                common.emit_error(payload, prog=prog, json_mode=True)
                results.append(jsonfields.project_object({
                    "id": issue_id,
                    "file": payload.fields.get("file"),
                    "store": payload.fields.get("store"),
                    "error": payload.message,
                }, fields))
        if multi:
            json.dump(results, sys.stdout, indent=2, sort_keys=False)
            sys.stdout.write("\n")
        else:
            json.dump(results[0], sys.stdout, indent=2, sort_keys=False)
            sys.stdout.write("\n")
        return 1 if any_failed else 0

    any_failed = False
    text_chunks = []
    for issue_id in args.ids:
        ok, payload = process_one(
            issue_id, anchor, store_root,
            args.sections, args.want_frontmatter, args.no_body,
            json_mode=False,
        )
        if ok:
            if multi:
                text_chunks.append(f"=== {issue_id} ===\n" + payload)
            else:
                text_chunks.append(payload)
        else:
            # One branchable payload line per failed id; the `id=` field
            # replaces the former `=== <id> ===` stderr header.
            any_failed = True
            common.emit_error(payload, prog=prog, json_mode=False)

    if multi:
        sys.stdout.write("\n".join(text_chunks))
        if text_chunks and not text_chunks[-1].endswith("\n"):
            sys.stdout.write("\n")
    else:
        for chunk in text_chunks:
            sys.stdout.write(chunk)

    return 1 if any_failed else 0
