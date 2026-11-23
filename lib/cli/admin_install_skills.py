"""`issue admin install-skills` -- install the shipped Claude Code skills.

Copies each ``<brew-prefix>/share/issue-mgmt/skills/<name>.md`` into
``$HOME/.claude/skills/<name>/SKILL.md``. The data path (which skills
exist, where they are staged, the ``HOME`` override, the 0 / 1 / 2 exit
codes) dates to the flat bash binary the v3.0.0 consolidation ported;
IM-272 moved the arg surface onto a normal argparse subparser, so
``--help`` is generated from the registrations and the manifest extractor
walks the real flags instead of a hand-maintained declaration.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import errors  # noqa: E402  (lib/ is on sys.path via the entrypoint)

from . import common

# The one place this verb names itself (IM-239); every diagnostic formats
# through it.
PROG = "issue admin install-skills"

VERB = "install-skills"
TIER = "admin"
SUMMARY = "install the shipped Claude Code skills into ~/.claude/skills/"
ORDER = 50

# Surface-manifest declarations (IM-245); protocol table in lib/cli/__init__.py.
SURFACE_CLASS = "scaffolding"
# IM-251: writes ~/.claude/skills/, no store at all.
SURFACE_STORE = "none"
# IM-260 C3: every failure path carries the branchable payload (missing
# brew, a failing `brew --prefix`, a missing or empty skills directory;
# argparse refusals via `common.structured_parser_errors`). Ratchets:
# may never revert.
SURFACE_STRUCTURED_ERRORS = "converted"

_DESCRIPTION = """\
Install the Claude Code skills shipped with the issue-mgmt Homebrew
formula into ~/.claude/skills/.

All .md files in <brew-prefix>/share/issue-mgmt/skills/ are installed as
skills. Each <name>.md is installed as ~/.claude/skills/<name>/SKILL.md.
Restart Claude Code for newly installed skills to take effect.

The bundled CLI/skills cheat sheet (<brew-prefix>/share/issue-mgmt/
CHEATSHEET.md) is copied to ~/.claude/skills/issue-mgmt-CHEATSHEET.md
alongside the skills so a session can find it on disk; its install path
is printed. Idempotent -- a re-run overwrites the copy.

HOME may be overridden in the environment to install into a different
target tree -- useful for verification scripts that exercise this tool
end-to-end without touching the real ~/.claude/."""

_EPILOG = """\
JSON mode (--json): exactly one object on stdout, shape

  {
    "source": PATH,              # <brew-prefix>/share/issue-mgmt/skills
    "dest": PATH,                # <HOME>/.claude/skills
    "installed": [{"name": NAME, "path": PATH, "existed": BOOL}, ...],
    "count": N,                  # len(installed)
    "cheatsheet": PATH|null,     # null when the formula predates it
    "dry_run": BOOL              # true when --dry-run was passed
  }

Every key is present on every successful run, so output shape is a
function of argv alone. The per-skill progress lines are replaced by the
JSON document, not duplicated, and the "older formula?" notice stays on
stderr. A failure (no brew, no skills dir, zero skill files) exits 1 with
a diagnostic on stderr and no JSON document.

--dry-run resolves the same source and destination set and emits the
same shape -- `installed` lists exactly what the real run would write,
with `existed` separating new installs from overwrites of an existing
SKILL.md -- but writes nothing under ~/.claude/."""


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
        help="Report what would be installed without writing.",
    )
    common.add_json_flag(p)
    common.add_version_flag(p)
    common.structured_parser_errors(p)
    p.set_defaults(_run=run)
    return p


def run(args):
    json_mode = bool(getattr(args, "json_mode", False))
    dry_run = bool(getattr(args, "dry_run", False))

    if shutil.which("brew") is None:
        common.emit_error(
            errors.PreconditionError(
                f"brew not found on PATH; {PROG} relies on Homebrew.",
                reason="missing-dependency", dependency="brew"),
            prog=PROG, json_mode=json_mode)
        return errors.EXIT_PRECONDITION

    try:
        brew_prefix = subprocess.run(
            ["brew", "--prefix"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except subprocess.CalledProcessError as e:
        common.emit_error(
            errors.PreconditionError(
                f"`brew --prefix` failed (exit {e.returncode})",
                reason="missing-dependency", dependency="brew"),
            prog=PROG, json_mode=json_mode)
        return errors.EXIT_PRECONDITION
    pkgshare = Path(brew_prefix) / "share" / "issue-mgmt" / "skills"

    if not pkgshare.is_dir():
        common.emit_error(
            errors.NotFoundError(
                f"skills directory not found: {pkgshare}",
                file=str(pkgshare)),
            prog=PROG, json_mode=json_mode)
        print(
            "Is the issue-mgmt formula installed? Try: brew install "
            "dugal/tap/issue-mgmt",
            file=sys.stderr,
        )
        return errors.EXIT_PRECONDITION

    skills_dir = Path(os.environ["HOME"]) / ".claude" / "skills"
    installed = []

    for src in sorted(pkgshare.glob("*.md")):
        if not src.is_file():
            continue
        name = src.name[: -len(".md")]
        dest_dir = skills_dir / name
        dest = dest_dir / "SKILL.md"
        existed = dest.is_file()
        if not dry_run:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
        if not json_mode:
            if dry_run:
                kind = "overwrite" if existed else "new"
                print(f"Would install: {name} -> {dest} ({kind})")
            else:
                print(f"Installed: {name} -> {dest}")
        installed.append({"name": name, "path": str(dest), "existed": existed})

    count = len(installed)
    if count == 0:
        common.emit_error(
            errors.NotFoundError(
                f"no skill files found in {pkgshare}",
                file=str(pkgshare)),
            prog=PROG, json_mode=json_mode)
        return errors.EXIT_PRECONDITION

    # Surface the bundled cheat sheet next to the installed skills so a
    # session can discover it on disk. The cheat sheet ships one level up
    # from the skills dir (share/issue-mgmt/CHEATSHEET.md). Copying is
    # idempotent (overwrite); a missing source is a non-fatal skip so an
    # older formula that predates the cheat sheet still installs skills.
    cheatsheet_src = pkgshare.parent / "CHEATSHEET.md"
    cheatsheet_dest = None
    if cheatsheet_src.is_file():
        cheatsheet_dest = skills_dir / "issue-mgmt-CHEATSHEET.md"
        if not dry_run:
            skills_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(cheatsheet_src, cheatsheet_dest)
        if not json_mode:
            verb = "Would install" if dry_run else "Installed"
            print(f"{verb} cheat sheet: {cheatsheet_dest}")
    else:
        print(
            f"Note: no cheat sheet found at {cheatsheet_src} (older formula?)",
            file=sys.stderr,
        )

    if json_mode:
        json.dump(
            {
                "source": str(pkgshare),
                "dest": str(skills_dir),
                "installed": installed,
                "count": count,
                "cheatsheet": str(cheatsheet_dest) if cheatsheet_dest
                else None,
                "dry_run": dry_run,
            },
            sys.stdout,
            indent=2,
            sort_keys=False,
        )
        sys.stdout.write("\n")
        return 0

    print("")
    if dry_run:
        print(f"Dry run. {count} skill(s) would be installed; nothing written.")
    else:
        print(
            f"Done. {count} skill(s) installed. Restart Claude Code for new "
            "skills to take effect."
        )
    return 0
