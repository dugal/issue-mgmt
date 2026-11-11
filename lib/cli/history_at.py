"""`issue history at` -- print an issue's contents at a past commit/date.

History sub-namespace verb. Ported from the flat
`show-issue-at` binary; the git data path is unchanged -- only the
arg-surface glue moved onto the dispatcher. Wraps
`git show <resolved-ref>:<PREFIX>/<id>.md` against the issue store.
`--at` accepts a SHA, tag, branch, or date (absolute or relative).
Read-only: no locks acquired.

Store mode only; refuses (exit 1) in legacy mode and prints the exact
`git show` fallback per D9. Exit codes and the raw/`--json`
output shapes are byte-identical to the flat binary.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

import errors  # noqa: E402
import history  # noqa: E402  (lib/ is on sys.path via the entrypoint)
import jsonfields  # noqa: E402
import idresolve  # noqa: E402

from . import common

VERB = "at"
TIER = "history"

# The one place this verb names itself. Every diagnostic, usage line,
# and `prog=` keyword formats through it, so the surface manifest sees
# exactly one name per verb and `surface.self-naming` is checkable
# rather than a ~90-site grep (IM-239).
PROG = "issue history at"
SUMMARY = "print an issue's contents at a past commit or date (git show)"
ORDER = 30

# Surface-manifest declarations (IM-245); protocol table in lib/cli/__init__.py.
SURFACE_CLASS = "point-read"
SURFACE_STORE = "flag"
# IM-254: `unconverted` until every failure path on this verb carries the
# key=value payload; the set is enumerated, never waived, and only shrinks
# (conversion work is IM-260).
SURFACE_STRUCTURED_ERRORS = "unconverted"

# Top-level keys of the single --json object; the field-selection set for
# `--json <fields>`. The whole object is the per-object here.
VALID_JSON_FIELDS = ("id", "store", "file", "ref", "resolved_sha", "content")

_DESCRIPTION = """\
Print an issue file as it existed at a given commit or date, via
`git show <ref>:<PREFIX>/<id>.md` inside the issue store. --at accepts a
SHA (full or short), tag, branch, or a date (absolute like 2026-04-01 or
relative like 2.weeks.ago); date inputs resolve to the most recent commit
at-or-before that point. Read-only: no locks acquired. Ported from the
flat `show-issue-at` binary.

Store mode (required) -- selected when ISSUE_STORE_ROOT is set or the
nearest .issues/.migrated-to-store sentinel exists.

Legacy mode -- this subcommand operates only on the issue store; it
refuses (exit 1) when no store is active and prints the exact fallback
`git show <ref>:.issues/<id>.md` to run in the originating project repo.
Pre-migration history is out of scope for this surface.

Default output is the raw file contents; --json emits a single object
with id/store/file/ref/resolved_sha/content. --json accepts an optional
comma-separated field list (e.g. --json resolved_sha,content) that
projects the object to those top-level fields; bare --json emits the full
shape and an unknown field name is a usage error (exit 2)."""

_EPILOG = """\
Examples:
  issue history at ABC-123 --at HEAD
  issue history at ABC-123 --at v1.5.0
  issue history at ABC-123 --at a1b2c3d
  issue history at ABC-123 --at 2026-04-01
  issue history at ABC-123 --at 2.weeks.ago --json
  issue history at ABC-123 --at v1.5.0 --json resolved_sha,content
"""


def register(subparsers):
    p = subparsers.add_parser(
        VERB,
        help=SUMMARY,
        description=_DESCRIPTION,
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("id", nargs="?", default=None, metavar="ID")
    p.add_argument(
        "--at",
        default=None,
        metavar="REF",
        help="Required. A git ref (SHA/tag/branch/HEAD~N) or a date.",
    )
    common.add_json_field_flag(p)
    common.add_store_flag(p)
    common.add_version_flag(p)
    p.set_defaults(_run=run)
    return p


def legacy_mode_error(issue_id: str, at: str) -> str:
    return (
        f"{PROG}: store mode not active "
        "(ISSUE_STORE_ROOT unset and no .issues/.migrated-to-store sentinel). "
        "This subcommand operates only on the issue store. "
        f"For pre-migration history, run the following inside the "
        f"originating project repo:\n"
        f"    git show {at}:.issues/{issue_id}.md"
    )


def emit_json(payload, out):
    json.dump(payload, out, indent=2, sort_keys=False)
    out.write("\n")


def run(args) -> int:
    if args.id is None:
        print(
            f"{PROG}: missing ID (use --help for usage)",
            file=sys.stderr,
        )
        return 2

    if idresolve.split_id(args.id) is None:
        print(
            f"{PROG}: invalid ID '{args.id}' "
            f"(expected <PREFIX>-NNN, e.g. ABC-123 or XYZ-9)",
            file=sys.stderr,
        )
        return 2

    if args.at is None:
        print(
            f"{PROG}: --at REF is required (use --help for usage)",
            file=sys.stderr,
        )
        return 2

    json_fields = None
    if jsonfields.json_enabled(args.json_mode):
        try:
            json_fields = jsonfields.parse_fields(
                args.json_mode, VALID_JSON_FIELDS, prog=PROG
            )
        except jsonfields.FieldSelectionError as e:
            print(e.message, file=sys.stderr)
            return 2

    anchor = common.find_project_anchor()
    store_root = common.resolve_store_root(anchor, args.store)
    if store_root is None:
        print(legacy_mode_error(args.id, args.at), file=sys.stderr)
        return 1

    store_root = store_root.expanduser().resolve()
    if not store_root.is_dir():
        print(
            f"{PROG}: store root does not exist: {store_root}",
            file=sys.stderr,
        )
        return 1

    def _note(before, after):
        print(f"{PROG}: resolved {before} -> {after}", file=sys.stderr)

    try:
        _rr = idresolve.resolve_issue_paths(
            args.id, store_root, anchor, emit_note=_note
        )
    except errors.StoreIntegrityError as e:
        print(e.format_stderr(PROG, json_mode=args.json_mode),
              file=sys.stderr)
        return e.exit_code
    canonical_id = _rr.canonical_id
    rel_path = history.rel_path_for(canonical_id)

    try:
        sha = history.resolve_ref(store_root, args.at)
    except history.HistoryError as e:
        print(f"{PROG}: {e}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        stderr_tail = (e.stderr or "").strip().splitlines()[-1:] or [""]
        print(
            f"{PROG}: git failed ({e.returncode}): {stderr_tail[0]}",
            file=sys.stderr,
        )
        return 1

    try:
        content = history.git_show(store_root, sha, rel_path)
    except history.HistoryError as e:
        print(f"{PROG}: {e}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        stderr_tail = (e.stderr or "").strip().splitlines()[-1:] or [""]
        print(
            f"{PROG}: git show failed ({e.returncode}): {stderr_tail[0]}",
            file=sys.stderr,
        )
        return 1

    if jsonfields.json_enabled(args.json_mode):
        payload = {
            "id": canonical_id,
            "store": str(store_root),
            "file": rel_path,
            "ref": args.at,
            "resolved_sha": sha,
            "content": content,
        }
        emit_json(jsonfields.project_object(payload, json_fields), sys.stdout)
    else:
        sys.stdout.write(content)

    return 0
