"""`issue admin validate` -- structural validator for issue files.

Dispatcher port of the flat `validate-issue` binary. The
data path (``lib/validate.py``) is untouched; only the arg-surface glue
moved onto the dispatcher. Diagnostics, the ``--json`` per-file array shape,
and the 0/1/2 exit codes are byte-identical to the flat binary so callers
and the future MCP server need no retest.

Pure structural validation: required frontmatter fields present, allowed-value
fields (type, status, priority) legal, id/filename consistency, prefix/dir
consistency, and the ``depends_on`` block shape. No business logic.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import validate  # noqa: E402  (lib/ is on sys.path via the entrypoint)

from . import common

VERB = "validate"
TIER = "admin"
SUMMARY = "structural validator for issue files (schema + id/filename/prefix + deps shape)"
ORDER = 20

_DESCRIPTION = """\
Validate the YAML frontmatter and ID/filename consistency of one or more
issue markdown files. Required fields present, allowed-value fields (type,
status, priority) legal, the id field matches the filename stem, and (under
a prefix directory) the id prefix matches the directory name. Also checks the
depends_on block shape. Pure structural validation -- no business logic
(branch existence, dependency resolution, section completeness)."""

_EPILOG = """\
Output:
  Human mode (default): one diagnostic per violation, <file>:<line>: <code>: <msg>
  JSON mode (--json): a top-level array of per-file objects.

Exit codes:
  0  all files validate
  1  one or more files have violations
  2  tool error (file not found, unreadable, or invalid arguments)

Examples:
  issue admin validate .issues/IM-026.md
  issue admin validate --json .issues/*.md
  issue admin validate ~/issues-store/IM/*.md ~/issues-store/RM/*.md
"""


def register(subparsers):
    p = subparsers.add_parser(
        VERB,
        help=SUMMARY,
        description=_DESCRIPTION,
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("files", nargs="*", metavar="FILE", help="One or more issue files.")
    common.add_json_flag(p)
    common.add_version_flag(p)
    p.set_defaults(_run=run)
    return p


def run(args) -> int:
    if not args.files:
        print(
            "issue admin validate: no files given (use -h for usage)",
            file=sys.stderr,
        )
        return 2

    results = []
    had_tool_error = False
    had_violations = False

    for raw in args.files:
        path = Path(raw)
        try:
            result = validate.validate_file(path)
        except FileNotFoundError:
            had_tool_error = True
            print(f"issue admin validate: file not found: {path}", file=sys.stderr)
            continue
        except IsADirectoryError:
            had_tool_error = True
            print(f"issue admin validate: is a directory: {path}", file=sys.stderr)
            continue
        except OSError as e:
            had_tool_error = True
            print(f"issue admin validate: cannot read {path}: {e}", file=sys.stderr)
            continue
        except UnicodeDecodeError as e:
            had_tool_error = True
            print(
                f"issue admin validate: cannot decode {path} as utf-8: {e}",
                file=sys.stderr,
            )
            continue

        results.append(result)
        if not result.ok:
            had_violations = True

    if args.json_mode:
        json.dump(
            [r.to_dict() for r in results],
            sys.stdout,
            indent=2,
            sort_keys=False,
        )
        sys.stdout.write("\n")
    else:
        for r in results:
            for v in r.violations:
                print(v.format(r.path))

    if had_tool_error:
        return 2
    if had_violations:
        return 1
    return 0
