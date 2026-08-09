"""depcore -- issue-dep's lock-free `list` reader.

Relocated verbatim from the flat ``scripts/issue-dep`` binary when that
binary became a v3.0.0 tombstone stub. The consolidated ``issue dep``
verb (``lib/cli/dep.py``) reuses this reader for the ``list`` subcommand and
routes add/remove through ``editcore.apply_edits`` (commit message
``dep <id>``), exactly as the flat binary did. ``list`` acquires no lock and
may return slightly stale results if a write commits during the read.
"""

from __future__ import annotations

import json
import sys

import frontmatter  # noqa: E402  (lib/ is on sys.path via the entrypoint)

import editcore


def cmd_list(args):
    """Print the edges on args.issue_id as `TYPE:TARGET` lines (or JSON).

    Returns the CLI exit code. Acquires no lock.
    """
    try:
        issue_file, _lock, _store, _anchor = editcore.resolve_issue_paths(
            args.issue_id
        )
    except ValueError as e:
        print(f"issue dep: {e}", file=sys.stderr)
        return 2
    if not issue_file.exists():
        print(f"issue dep: issue file not found: {issue_file}",
              file=sys.stderr)
        return 1
    fm_lines, _body = frontmatter.read_issue(issue_file)
    edges = frontmatter.parse_frontmatter_lines(fm_lines).get("depends_on")
    if not isinstance(edges, list):
        edges = []
    if args.json_mode:
        json.dump({"id": args.issue_id, "depends_on": edges},
                  sys.stdout, indent=2, sort_keys=False)
        sys.stdout.write("\n")
    else:
        for edge in edges:
            if isinstance(edge, dict):
                print(f"{edge.get('type', '?')}:{edge.get('to', '?')}")
            else:
                print(str(edge))
    return 0
