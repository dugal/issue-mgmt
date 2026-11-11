"""`issue admin reap` -- clear expired TTL leases across the store (manual override).

Dispatcher port of the flat `issue-reap-expired-claims` binary.
This is the manual override; `issue start` / `issue ready` fold the reap in
automatically (WP3, per the design's evidence-review amendment). The data
path (``lib/lease.py``, ``lib/concurrency.py``, ``lib/frontmatter.py``) is
untouched.

Walks every ``<PFX>/<PFX>-NNN.md`` under the store, finds issues whose
top-level ``lease_until`` is in the past (or whose ``claim:`` block has a
missing/malformed ``lease_until``), and clears the claim, preserving
``claim_generation``. Valid (unexpired) claims are never touched. The scan,
the per-issue lock + re-verify + ``reap <id>`` commit, the ``--json`` shape,
and the 0/1/2/3 exit codes are byte-identical to the flat binary.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path

import concurrency  # noqa: E402  (lib/ is on sys.path via the entrypoint)
import errors  # noqa: E402
import frontmatter  # noqa: E402
import lease  # noqa: E402

from . import common

VERB = "reap"
TIER = "admin"
SUMMARY = "clear expired TTL leases across the store (manual override)"
ORDER = 40

# Surface-manifest declarations (IM-245); protocol table in lib/cli/__init__.py.
SURFACE_CLASS = "mutating"
# IM-251: resolves via the precedence chain with no operand naming it, so
# the absent `--store` is a real gap -- IM-251 follow-up work.
SURFACE_STORE = "flag"
# IM-254: `unconverted` until every failure path on this verb carries the
# key=value payload; the set is enumerated, never waived, and only shrinks
# (conversion work is IM-260).
SURFACE_STRUCTURED_ERRORS = "unconverted"

NOTES_SECTION = "Implementation Notes"

PREFIX_DIR_RE = re.compile(r"^[A-Z][A-Z0-9]{1,7}$")

_DESCRIPTION = """\
Clear expired TTL leases across the issue store. Finds issues whose
top-level lease_until is in the past (or whose claim: block has a
missing/malformed lease_until) and clears the claim: object and lease_until
field; claim_generation is preserved. A reaper note is appended to
## Implementation Notes. Valid (unexpired) claims are never touched.

The reaper is a privileged caller: it does not take or check
--claim-generation. It reconciles abandoned state rather than racing for
ownership. Store mode only."""

_EPILOG = """\
Exit codes:
  0  success (whether 0 or N claims were reaped)
  1  store unreadable / not resolvable / precondition failed
  2  argument / usage error
  3  concurrent-edit (per-issue lock timeout)

Examples:
  issue admin reap
  issue admin reap --dry-run
  issue admin reap --prefix IM --json
"""


def register(subparsers):
    p = subparsers.add_parser(
        VERB,
        help=SUMMARY,
        description=_DESCRIPTION,
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Print what would be reaped without writing.",
    )
    common.add_prefix_scope_flag(
        p, multi=False,
        help="Restrict the walk to one prefix directory.",
    )
    common.add_json_flag(p)
    common.add_lock_timeout_flag(
        p, default=concurrency.DEFAULT_LOCK_TIMEOUT
    )
    common.add_version_flag(p)
    p.set_defaults(_run=run)
    return p


# also in the flat issue-reap-expired-claims binary -- keep in sync
def _find_project_anchor():
    cwd = Path.cwd().resolve()
    for d in [cwd, *cwd.parents]:
        if (d / ".issues").is_dir():
            return d / ".issues"
    workspace = os.environ.get("ISSUE_MGMT_ROOT")
    if workspace:
        return Path(workspace) / ".issues"
    parent = cwd.parent
    if (parent / ".issues").is_dir():
        return parent / ".issues"
    return cwd / ".issues"


def _resolve_store_root():
    env = os.environ.get("ISSUE_STORE_ROOT")
    if env:
        return Path(env)
    sentinel = _find_project_anchor() / ".migrated-to-store"
    if sentinel.is_file():
        return Path(sentinel.read_text(encoding="utf-8").strip())
    return None


def _append_note_line(body_text, note):
    """Append one line to ## Implementation Notes (create if absent)."""
    if frontmatter.find_section(body_text, NOTES_SECTION) is None:
        if body_text and not body_text.endswith("\n"):
            body_text += "\n"
        if body_text and not body_text.endswith("\n\n"):
            body_text += "\n"
        body_text += f"## {NOTES_SECTION}\n"
    return frontmatter.edit_section(body_text, NOTES_SECTION, "append", note)


def _iter_issue_files(store_root, repo_filter):
    """Yield (prefix, path) for every issue file under the store."""
    for child in sorted(store_root.iterdir()):
        if not child.is_dir():
            continue
        prefix = child.name
        if not PREFIX_DIR_RE.match(prefix):
            continue
        if repo_filter is not None and prefix != repo_filter:
            continue
        for f in sorted(child.glob(f"{prefix}-*.md")):
            yield prefix, f


def _scan_expired(store_root, repo_filter, now):
    """Unlocked scan pass: list candidates whose claim is present and expired.
    The per-issue reap re-verifies under the lock before writing."""
    candidates = []
    for prefix, path in _iter_issue_files(store_root, repo_filter):
        try:
            fm_lines, _body = frontmatter.read_issue(path)
        except (OSError, ValueError) as e:
            print(f"issue admin reap: skipping {path}: {e}", file=sys.stderr)
            continue
        claim = lease.get_claim(fm_lines)
        if claim is None:
            continue
        if lease.lease_is_active(fm_lines, now=now):
            continue
        try:
            generation = lease.get_claim_generation(fm_lines)
        except ValueError:
            generation = None
        candidates.append({
            "prefix": prefix,
            "path": path,
            "id": path.stem,
            "assignee": claim.get("assignee"),
            "lease_until": lease.get_lease_until(fm_lines),
            "claim_generation": generation,
        })
    return candidates


def _reap_one(store_root, prefix, issue_file, issue_id, lock_timeout, now):
    """Lock + re-verify expiry + clear claim + note + commit one issue.

    Returns the reaped-claim record, or None if the claim was renewed,
    released, or re-claimed with a valid lease between the scan and the lock.
    """
    today = datetime.date.today().isoformat()
    lock_path = concurrency.issue_lock_path(store_root, prefix, issue_id)
    autocommit_enabled = not os.environ.get("ISSUE_MGMT_NO_AUTOCOMMIT")
    with concurrency.flock_acquire(
        lock_path, timeout=lock_timeout, resource=str(issue_file)
    ):
        snapshot = concurrency.capture_state(issue_file)
        if snapshot.is_absent():
            return None
        fm_lines, body_text = frontmatter.read_issue(issue_file)
        claim = lease.get_claim(fm_lines)
        if claim is None or lease.lease_is_active(fm_lines, now=now):
            return None  # state moved between scan and lock; leave it
        lease_until = lease.get_lease_until(fm_lines)
        try:
            generation = lease.get_claim_generation(fm_lines)
        except ValueError:
            generation = None
        body_text = _append_note_line(
            body_text,
            f"{today}: claim: expired lease for {claim.get('assignee')} "
            f"reaped (lease_until {lease_until}, generation "
            f"{generation} preserved)",
        )
        lease.clear_claim(fm_lines)
        frontmatter.set_field(fm_lines, "updated", today)
        new_text = frontmatter.serialize_issue(fm_lines, body_text)

        if autocommit_enabled:
            rel = issue_file.resolve().relative_to(store_root.resolve())
            with concurrency.store_mutation(store_root, [rel], f"reap {issue_id}"):
                concurrency.verify_unchanged(
                    issue_file, snapshot, resource=str(issue_file)
                )
                concurrency.atomic_write(issue_file, new_text)
        else:
            concurrency.verify_unchanged(
                issue_file, snapshot, resource=str(issue_file)
            )
            concurrency.atomic_write(issue_file, new_text)

    return {
        "id": issue_id,
        "assignee": claim.get("assignee"),
        "lease_until": lease_until,
        "claim_generation": generation,
    }


def run(args) -> int:
    if args.prefix is not None and not PREFIX_DIR_RE.match(args.prefix):
        print(
            f"issue admin reap: invalid prefix {args.prefix!r} "
            f"(must match {PREFIX_DIR_RE.pattern})",
            file=sys.stderr,
        )
        return 2

    store_root = _resolve_store_root()
    if store_root is None:
        print(
            "issue admin reap: store mode required -- set ISSUE_STORE_ROOT "
            "or run inside a project with a .issues/.migrated-to-store "
            "sentinel (see `issue admin migrate`)",
            file=sys.stderr,
        )
        return 1
    if not store_root.is_dir():
        print(
            f"issue admin reap: store root does not exist or is not a "
            f"directory: {store_root}",
            file=sys.stderr,
        )
        return 1

    # IM-238: an absent --prefix scope is an argument error (exit 2), the
    # same rule every verb taking --prefix holds.
    rc = common.enforce_prefix_scope(
        [args.prefix] if args.prefix else None, store_root, None,
        prog="issue admin reap", json_mode=bool(getattr(args, "json_mode", False)),
    )
    if rc is not None:
        return rc

    now = lease.now_utc()
    candidates = _scan_expired(store_root, args.prefix, now)

    reaped = []
    if args.dry_run:
        reaped = [
            {
                "id": c["id"],
                "assignee": c["assignee"],
                "lease_until": c["lease_until"],
                "claim_generation": c["claim_generation"],
            }
            for c in candidates
        ]
    else:
        for c in candidates:
            try:
                record = _reap_one(
                    store_root, c["prefix"], c["path"], c["id"],
                    args.lock_timeout, now,
                )
            except errors.ContractError as e:
                print(
                    e.format_stderr("issue admin reap", json_mode=args.json_mode),
                    file=sys.stderr,
                )
                return e.exit_code
            except concurrency.StoreCommitError as e:
                print(e.format_stderr("issue admin reap"), file=sys.stderr)
                return errors.EXIT_PRECONDITION
            if record is not None:
                reaped.append(record)

    if args.json_mode:
        json.dump(
            {"reaped": reaped, "dry_run": args.dry_run},
            sys.stdout,
            sort_keys=False,
        )
        sys.stdout.write("\n")
    else:
        verb = "Would reap" if args.dry_run else "Reaped"
        for r in reaped:
            print(
                f"{verb} {r['id']}: claim by {r['assignee']} expired "
                f"(lease_until {r['lease_until']}, generation "
                f"{r['claim_generation']} preserved)"
            )
        if not reaped:
            print("No expired claims.")
        else:
            print(f"{verb} {len(reaped)} expired claim(s).")

    return 0
