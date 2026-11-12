"""`issue create` -- create a new issue file in the resolved store/dir.

Port of the flat bash ``new-issue.sh`` onto the consolidated dispatcher.
The data path is unchanged: same store/legacy resolution,
same ``<store>/.repos`` registry lookup + ``.prefix`` cross-validation, same
template-resolution order, same ID assignment, same
store-mutation commit (``create <id>``), and the same ``--section
NAME=TEXT`` / ``--from-file`` body seeding and ``--json`` shape.
Only two things move: bash -> Python, and the post-write reindex switches
from subprocessing the sibling ``reindex-issues`` to the in-process seam
(``reindex.reindex_after_mutation`` -> ``reindex_core``), killing the
sibling-subprocess failure class on this surface.

Exit codes match the flat binary: 0 success; 1 precondition (unregistered
repo, prefix mismatch, non-git store mode without ``--repo``, unreadable
``--from-file``, missing template, invalid type/priority, wrong positional
count); 2 argument/usage (``--section``/``--from-file`` mutual exclusion,
malformed ``--section``, and -- since IM-248 -- the zero-positional form
with a non-TTY stdin, which used to exit 1 through an unhandled
``EOFError`` traceback out of the prompt). The store-commit-lock path
surfaces the 3-7 loser-side contract unchanged.
"""

from __future__ import annotations

import argparse
import json as _json
import os
import re
import sys
from pathlib import Path

import concurrency  # noqa: E402  (lib/ is on sys.path via the entrypoint)
import frontmatter as _fm  # noqa: E402
import reindex as _reindex  # noqa: E402
import repo as _repo  # noqa: E402

from . import common

# The one place this verb names itself; the hand-written usage line
# below formats through it (IM-239).
PROG = "issue create"

VERB = "create"
TIER = "tier1"
SUMMARY = "create a new issue in the resolved store (or legacy .issues/)"
ORDER = 10

# Surface-manifest declarations (IM-245); protocol table in lib/cli/__init__.py.
SURFACE_CLASS = "mutating"
SURFACE_STORE = "flag"
# IM-254: `unconverted` until every failure path on this verb carries the
# key=value payload; the set is enumerated, never waived, and only shrinks
# (conversion work is IM-260).
SURFACE_STRUCTURED_ERRORS = "unconverted"
# IM-250 / IM-262: `created` is the new issue's own creation date, read from
# the clock and written into the record -- record content, not a stamp
# describing this render. Declared so an ambient value cannot reach `--json`
# undeclared; `tests/test_surface_ambient_fields.py` measures it.
SURFACE_RECORD_FIELDS = ("created",)

VALID_TYPES = ("bug", "feature", "chore", "spike", "decision")
VALID_PRIORITIES = ("high", "medium", "low")

_DESCRIPTION = """\
Create a new PREFIX-NNN issue file. Non-interactive with three positionals
(title type priority); with none, prompts interactively -- which needs a TTY
on stdin, and is otherwise refused with an argument error rather than a
prompt nobody can answer.

Store mode is selected (highest precedence) by --store ROOT, the
ISSUE_STORE_ROOT environment variable, or a nearest
.issues/.migrated-to-store sentinel; the repo identity is the main
checkout's basename (override with --repo NAME) and its prefix is looked up
in <store>/.repos (run `issue admin init-project` to register). A local
.issues/.prefix, when present, is cross-validated against the registry.
Legacy mode (none of the above) writes to the nearest .issues/ walking up
from cwd."""

_EPILOG = """\
Body seeding:
  --section NAME=TEXT   (repeatable) replace ## NAME's body with TEXT, or
                        append a new ## NAME section if absent.
  --from-file PATH      use PATH verbatim as the issue body ('-' = stdin);
                        mutually exclusive with --section.

Examples:
  issue create "Fix embedding bug" bug high
  issue create --repo issue-mgmt "Add CLI" feature medium
  issue create "Spike: design X" spike medium --section "Summary=Pitch."
  cat draft.md | issue create "Decision: ADR-001" decision low --from-file -
  issue create "Json out" feature low --json
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
        "words",
        nargs="*",
        metavar="TITLE TYPE PRIORITY",
        help="Provide all three for non-interactive create, or none to prompt.",
    )
    p.add_argument(
        "--repo",
        default="",
        metavar="NAME",
        help="Override the repo identity used for the registry lookup.",
    )
    p.add_argument(
        "--section",
        action="append",
        default=[],
        metavar="NAME=TEXT",
        dest="sections",
        help="Repeatable. Seed/replace a body section at creation time.",
    )
    p.add_argument(
        "--from-file",
        default="",
        metavar="PATH",
        dest="from_file",
        help="Use PATH ('-' = stdin) verbatim as the body (excludes --section).",
    )
    common.add_json_flag(p)
    common.add_store_flag(p)
    common.add_lock_timeout_flag(p)
    common.add_no_reindex_flag(p)
    common.add_version_flag(p)
    p.set_defaults(_run=run)
    return p


# --- store / legacy resolution (ported from new-issue.sh) ------------------


def _workspace_root() -> Path:
    env = os.environ.get("ISSUE_MGMT_ROOT")
    if env:
        return Path(env)
    parent = Path.cwd().parent
    if (parent / ".issues").is_dir():
        return parent
    return Path.cwd()


def _find_project_anchor() -> Path:
    cwd = Path.cwd()
    for d in [cwd, *cwd.parents]:
        if (d / ".issues").is_dir():
            return d / ".issues"
    return _workspace_root() / ".issues"


def _resolve_store_root(anchor: Path, cli_store):
    """--store -> ISSUE_STORE_ROOT -> sentinel -> None (legacy)."""
    if cli_store:
        return Path(cli_store)
    env = os.environ.get("ISSUE_STORE_ROOT")
    if env:
        return Path(env)
    sentinel = anchor / ".migrated-to-store"
    if sentinel.is_file():
        return Path(sentinel.read_text(encoding="utf-8").strip())
    return None


def _lookup_repo_prefix(store_root: Path, repo_name: str):
    """Return the prefix for repo_name from <store>/.repos, or None."""
    registry = store_root / ".repos"
    if not registry.is_file():
        return None
    for raw in registry.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        name, _, prefix = line.partition(":")
        if name.strip() == repo_name:
            return prefix.strip()
    return None


def _find_template(store_root, prefix: str) -> str:
    """Template order: store/<PREFIX> -> store -> Homebrew share -> dev."""
    root = Path(__file__).resolve().parents[2]
    if store_root is not None:
        cand = store_root / prefix / "ISSUE-TEMPLATE.md"
        if cand.is_file():
            return str(cand)
        cand = store_root / "ISSUE-TEMPLATE.md"
        if cand.is_file():
            return str(cand)
    cand = root / "share" / "issue-mgmt" / "ISSUE-TEMPLATE.md"
    if cand.is_file():
        return str(cand)
    cand = root / "ISSUE-TEMPLATE.md"
    if cand.is_file():
        return str(cand)
    return ""


def _next_id(issues_dir: Path, prefix: str) -> str:
    max_n = 0
    num_re = re.compile(rf"^{re.escape(prefix)}-(\d+)$")
    for f in issues_dir.glob(f"{prefix}-*.md"):
        stem = f.stem
        if stem == f"{prefix}-TEMPLATE":
            continue
        m = num_re.match(stem)
        if not m:
            continue
        n = int(m.group(1))  # strips leading zeros
        if n > max_n:
            max_n = n
    return f"{prefix}-{max_n + 1:03d}"


def _build_issue_text(template_path, id_, title, type_, priority, today,
                      sections, from_file_path) -> str:
    """Port of new-issue.sh's PYEOF body builder (byte-for-byte)."""
    with open(template_path, encoding="utf-8") as f:
        content = f.read()

    parts = content.split("---", 2)
    fm_raw = parts[1].strip() if len(parts) >= 3 else ""
    body_raw = parts[2] if len(parts) >= 3 else ""

    fm_subs = {
        "id": id_,
        "title": f'"{title}"',
        "type": type_,
        "status": "open",
        "priority": priority,
        "created": today,
        "updated": today,
        "branch": '""',
        "tags": "[]",
    }

    fm_lines = []
    for line in fm_raw.split("\n"):
        if ":" in line:
            key = line.split(":", 1)[0].strip()
            if key in fm_subs:
                fm_lines.append(f"{key}: {fm_subs[key]}")
                continue
        fm_lines.append(line)

    if from_file_path:
        with open(from_file_path, encoding="utf-8") as f:
            body_text = f.read()
        if not body_text.endswith("\n"):
            body_text += "\n"
    else:
        section_headers = [ln for ln in body_raw.split("\n") if ln.startswith("## ")]
        body_chunks = [f"\n{h}\n" for h in section_headers] + ["\n"]
        body_text = "".join(body_chunks)

    for name, text in sections:
        try:
            body_text = _fm.edit_section(body_text, name, "replace", text)
        except ValueError:
            trimmed = body_text.rstrip("\n")
            text_norm = text.rstrip("\n")
            body_text = trimmed + "\n\n## " + name + "\n\n" + text_norm + "\n"

    return "---\n" + "\n".join(fm_lines) + "\n" + "---\n" + body_text


def _validate(value, options, label):
    if value not in options:
        print(
            f"Error: invalid {label} '{value}'. Must be one of: "
            f"{' '.join(options)}",
            file=sys.stderr,
        )
        return False
    return True


def _stdin_is_tty() -> bool:
    """Whether the zero-positional prompt has anyone to ask.

    Defensive about the probe itself: a detached or already-closed stdin can
    raise rather than answer, and a traceback out of the no-traceback guard
    would be the funniest possible bug. An unanswerable probe reads as "not
    a TTY", which is the refusing branch.
    """
    try:
        return bool(sys.stdin is not None and sys.stdin.isatty())
    except (OSError, ValueError):
        return False


def run(args) -> int:
    # --- argument shape / body-input validation (new-issue.sh order) ---
    section_pairs = []
    for spec in args.sections:
        if "=" not in spec:
            print(
                f"Error: --section requires NAME=TEXT format (got '{spec}')",
                file=sys.stderr,
            )
            return 2
        name, _, text = spec.partition("=")
        if not name:
            print("Error: --section NAME must be non-empty", file=sys.stderr)
            return 2
        section_pairs.append((name, text))

    if section_pairs and args.from_file:
        print(
            "Error: --section and --from-file are mutually exclusive",
            file=sys.stderr,
        )
        return 2

    # IM-248: the zero-positional form prompts, and an unanswerable prompt is
    # an argv-shape problem -- knowable here, before any store is resolved or
    # any directory created. `surface.non-interactive` requires the refusal to
    # name what supplies the value instead; `surface.no-traceback` requires it
    # to be a diagnostic rather than the EOFError `input()` raises on a closed
    # stdin (and rather than the indefinite block a live pipe produced).
    # Checked ahead of the stdin staging below so `--from-file -` cannot
    # consume a stream the verb is about to refuse.
    if not args.words and not _stdin_is_tty():
        import errors as _errors
        common.emit_error(
            _errors.UsageError(
                "stdin is not a TTY, so the interactive prompt for title, "
                "type and priority cannot be answered -- pass them "
                f"positionally instead ({PROG} TITLE TYPE PRIORITY), "
                "seeding the body with --section NAME=TEXT or "
                "--from-file PATH if wanted",
                reason="non-interactive-stdin",
            ),
            prog=PROG,
            json_mode=args.json_mode,
        )
        return _errors.EXIT_USAGE

    # Stage stdin / from-file content up front so I/O errors fail fast.
    from_file_staged = ""
    if args.from_file == "-":
        import tempfile
        fd, from_file_staged = tempfile.mkstemp(prefix="issue-create-stdin.")
        with os.fdopen(fd, "wb") as f:
            f.write(sys.stdin.buffer.read())
    elif args.from_file:
        if not os.access(args.from_file, os.R_OK):
            print(
                f"Error: --from-file path not readable: {args.from_file}",
                file=sys.stderr,
            )
            return 1
        from_file_staged = args.from_file

    try:
        return _run_inner(args, section_pairs, from_file_staged)
    finally:
        if args.from_file == "-" and from_file_staged:
            try:
                os.unlink(from_file_staged)
            except OSError:
                pass


def _run_inner(args, section_pairs, from_file_staged) -> int:
    anchor = _find_project_anchor()
    store_root = _resolve_store_root(anchor, args.store)

    if store_root is not None:
        # --- Store mode ---
        if args.repo:
            repo_name = args.repo
        else:
            repo_name = _repo.resolve_main_basename()
            if not repo_name:
                print(
                    "Error: store mode requires --repo NAME or a git repository\n"
                    f"       (cwd: {os.getcwd()})",
                    file=sys.stderr,
                )
                return 1

        registry_prefix = _lookup_repo_prefix(store_root, repo_name)
        if not registry_prefix:
            print(
                f"Error: repo '{repo_name}' is not registered in "
                f"{store_root}/.repos\n"
                "       Run 'issue admin init-project' to register this project.",
                file=sys.stderr,
            )
            return 1

        local_prefix_file = anchor / ".prefix"
        if local_prefix_file.is_file():
            local_prefix = local_prefix_file.read_text(encoding="utf-8").strip()
            if local_prefix and local_prefix != registry_prefix:
                print(
                    f"Error: prefix mismatch for repo '{repo_name}':\n"
                    f"       registry ({store_root}/.repos): {registry_prefix}\n"
                    f"       local ({local_prefix_file}): {local_prefix}",
                    file=sys.stderr,
                )
                return 1

        prefix = registry_prefix
        issues_dir = store_root / prefix
        issues_dir.mkdir(parents=True, exist_ok=True)
    else:
        # --- Legacy mode ---
        issues_dir = anchor
        if not issues_dir.is_dir():
            print(
                f"Error: issues directory not found at {issues_dir}",
                file=sys.stderr,
            )
            return 1
        prefix_file = anchor / ".prefix"
        if prefix_file.is_file():
            prefix = prefix_file.read_text(encoding="utf-8").strip()
        else:
            prefix = "ISSUE"

    template = _find_template(store_root, prefix)
    if not template:
        print(
            "Error: ISSUE-TEMPLATE.md not found. Install issue-mgmt via "
            "Homebrew or place a template under <store>/ or "
            f"<store>/{prefix}/.",
            file=sys.stderr,
        )
        return 1

    # --- Collect title/type/priority ---
    words = args.words
    if len(words) >= 3:
        title, type_, priority = words[0], words[1], words[2]
        if not _validate(type_, VALID_TYPES, "type"):
            return 1
        if not _validate(priority, VALID_PRIORITIES, "priority"):
            return 1
    elif len(words) == 0:
        title = input("Title: ")
        type_ = input("Type [bug/feature/chore/spike/decision]: ")
        if not _validate(type_, VALID_TYPES, "type"):
            return 1
        priority = input("Priority [high/medium/low]: ")
        if not _validate(priority, VALID_PRIORITIES, "priority"):
            return 1
    else:
        print(
            "Error: provide 0 or 3 positional arguments (title type "
            "priority)\n"
            f'Usage: {PROG} [--repo NAME] ["title" type priority]',
            file=sys.stderr,
        )
        return 1

    issue_id = _next_id(issues_dir, prefix)
    filename = issues_dir / f"{issue_id}.md"
    today = __import__("datetime").date.today().isoformat()

    content = _build_issue_text(
        template, issue_id, title, type_, priority, today,
        section_pairs, from_file_staged,
    )

    no_autocommit_env = bool(os.environ.get("ISSUE_MGMT_NO_AUTOCOMMIT"))
    lock_timeout = (
        args.lock_timeout
        if args.lock_timeout is not None
        else concurrency.DEFAULT_LOCK_TIMEOUT
    )

    def _write_and_reindex(*, suppress_commit):
        filename.write_text(content, encoding="utf-8")
        if not args.json_mode:
            print(f"Created: {filename}")
        if not args.no_reindex:
            _reindex.reindex_after_mutation(
                store_root, issues_dir,
                suppress_commit=suppress_commit,
                to_stderr=args.json_mode,
            )

    if store_root is not None and not no_autocommit_env:
        import errors as _errors
        rel_file = f"{prefix}/{filename.name}"
        commit_paths = [rel_file, f"{prefix}/INDEX.md", "INDEX.md"]
        if args.json_mode:
            os.environ["ISSUE_MGMT_STDERR_JSON"] = "1"
        try:
            with concurrency.store_mutation(
                store_root, commit_paths, f"create {issue_id}",
                lock_timeout=lock_timeout,
            ):
                _write_and_reindex(suppress_commit=True)
        except concurrency.StoreCommitError as e:
            print(e.format_stderr("issue create"), file=sys.stderr)
            return _errors.EXIT_PRECONDITION
        except _errors.ContractError as e:
            # store-commit-lock timeout -> ConcurrentEdit (exit 3), etc.
            print(
                e.format_stderr("issue create", json_mode=args.json_mode),
                file=sys.stderr,
            )
            return e.exit_code
    else:
        _write_and_reindex(suppress_commit=bool(no_autocommit_env))

    if args.json_mode:
        obj = {
            "id": issue_id,
            "file": os.path.abspath(str(filename)),
            "store": str(store_root) if store_root is not None else None,
            "prefix": prefix,
            "title": title,
            "type": type_,
            "priority": priority,
            "created": today,
            "sections_applied": [name for name, _ in section_pairs],
            "from_file": (
                None
                if (not args.from_file or args.from_file == "-")
                else os.path.abspath(args.from_file)
            ),
        }
        _json.dump(obj, sys.stdout)
        sys.stdout.write("\n")

    return 0
