"""`issue admin install-skills` -- install the shipped Claude Code skills.

Dispatcher port of the flat ``issue-mgmt-install-skills`` bash binary.
Copies each ``<brew-prefix>/share/issue-mgmt/skills/<name>.md`` into
``$HOME/.claude/skills/<name>/SKILL.md``. Behavior -- stdout/stderr lines,
exit codes (0 / 1 / 2), the ``HOME`` override, and the usage/version text --
is byte-identical to the bash binary it replaces.

The data path (which skills exist, where they are staged) is unchanged; only
the arg surface moved onto the ``issue`` dispatcher.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

VERSION = "v3.4.1"

# The one place this verb names itself (IM-239); `_USAGE` interpolates it.
PROG = "issue admin install-skills"

_USAGE = f"Usage: {PROG} [--json] [--help|--version]\n" + """\

Installs Claude Code skills from the issue-mgmt Homebrew formula into
~/.claude/skills/.

All .md files in <brew-prefix>/share/issue-mgmt/skills/ are installed as
skills. Each <name>.md is installed as ~/.claude/skills/<name>/SKILL.md.
Restart Claude Code for newly installed skills to take effect.

The bundled CLI/skills cheat sheet (<brew-prefix>/share/issue-mgmt/
CHEATSHEET.md) is copied to ~/.claude/skills/issue-mgmt-CHEATSHEET.md
alongside the skills so a session can find it on disk; its install path is
printed. Idempotent -- a re-run overwrites the copy.

HOME may be overridden in the environment to install into a different
target tree -- useful for verification scripts that exercise this tool
end-to-end without touching the real ~/.claude/.

JSON mode (--json): exactly one object on stdout, shape

  {
    "source": PATH,              # <brew-prefix>/share/issue-mgmt/skills
    "dest": PATH,                # <HOME>/.claude/skills
    "installed": [{"name": NAME, "path": PATH}, ...],
    "count": N,                  # len(installed)
    "cheatsheet": PATH|null      # null when the formula predates it
  }

Every key is present on every successful run, so output shape is a
function of argv alone. The per-skill progress lines are replaced by the
JSON document, not duplicated, and the "older formula?" notice stays on
stderr. A failure (no brew, no skills dir, zero skill files) exits 1 with
a diagnostic on stderr and no JSON document.
"""


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv

    # Hand-rolled argv walk (the dispatcher hands this verb its argv
    # verbatim through a prefix_chars=chr(0) catch-all), so --json is wired
    # in here rather than through common.add_json_flag.
    json_mode = False
    for arg in argv:
        if arg in ("-h", "--help"):
            sys.stdout.write(_USAGE)
            return 0
        if arg == "--version":
            print(f"{PROG} {VERSION}")
            return 0
        if arg == "--json":
            json_mode = True
            continue
        print(f"Unknown argument: {arg}", file=sys.stderr)
        print("Run with --help for usage.", file=sys.stderr)
        return 2

    if shutil.which("brew") is None:
        print(
            f"Error: brew not found on PATH; {PROG} relies on Homebrew.",
            file=sys.stderr,
        )
        return 1

    brew_prefix = subprocess.run(
        ["brew", "--prefix"], capture_output=True, text=True, check=True
    ).stdout.strip()
    pkgshare = Path(brew_prefix) / "share" / "issue-mgmt" / "skills"

    if not pkgshare.is_dir():
        print(f"Error: skills directory not found: {pkgshare}", file=sys.stderr)
        print(
            "Is the issue-mgmt formula installed? Try: brew install "
            "dugal/tap/issue-mgmt",
            file=sys.stderr,
        )
        return 1

    skills_dir = Path(os.environ["HOME"]) / ".claude" / "skills"
    installed = []

    for src in sorted(pkgshare.glob("*.md")):
        if not src.is_file():
            continue
        name = src.name[: -len(".md")]
        dest_dir = skills_dir / name
        dest = dest_dir / "SKILL.md"
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        if not json_mode:
            print(f"Installed: {name} -> {dest}")
        installed.append({"name": name, "path": str(dest)})

    count = len(installed)
    if count == 0:
        print(f"Warning: no skill files found in {pkgshare}", file=sys.stderr)
        return 1

    # Surface the bundled cheat sheet next to the installed skills so a
    # session can discover it on disk. The cheat sheet ships one level up
    # from the skills dir (share/issue-mgmt/CHEATSHEET.md). Copying is
    # idempotent (overwrite); a missing source is a non-fatal skip so an
    # older formula that predates the cheat sheet still installs skills.
    cheatsheet_src = pkgshare.parent / "CHEATSHEET.md"
    cheatsheet_dest = None
    if cheatsheet_src.is_file():
        cheatsheet_dest = skills_dir / "issue-mgmt-CHEATSHEET.md"
        skills_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(cheatsheet_src, cheatsheet_dest)
        if not json_mode:
            print(f"Installed cheat sheet: {cheatsheet_dest}")
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
            },
            sys.stdout,
            indent=2,
            sort_keys=False,
        )
        sys.stdout.write("\n")
        return 0

    print("")
    print(
        f"Done. {count} skill(s) installed. Restart Claude Code for new "
        "skills to take effect."
    )
    return 0


# --- consolidated `issue admin install-skills` dispatcher glue -

VERB = "install-skills"
TIER = "admin"
SUMMARY = "install the shipped Claude Code skills into ~/.claude/skills/"
ORDER = 50

# Surface-manifest declarations (IM-245); protocol table in lib/cli/__init__.py.
SURFACE_CLASS = "scaffolding"
# IM-251: writes ~/.claude/skills/, no store at all.
SURFACE_STORE = "none"
# IM-254: `unconverted` until every failure path on this verb carries the
# key=value payload; the set is enumerated, never waived, and only shrinks
# (conversion work is IM-260).
SURFACE_STRUCTURED_ERRORS = "unconverted"
# IM-245: this verb has NO parser -- `register()` installs a
# prefix_chars="\x00" passthrough subparser with one nargs="*" catch-all and
# `main()` walks argv by hand, so an argparse walk sees zero flags while the
# verb really accepts these. Declared literally so the manifest records the
# true surface instead of a false-CLEAN empty flag set. Keep in sync with the
# argv loop in `main()`.
SURFACE_MANUAL_FLAGS = (
    {"names": ["-h", "--help"], "action": "help"},
    {"names": ["--version"], "action": "version"},
    {"names": ["--json"], "action": "store_true",
     "dest": "json_mode", "default": False},
)


def register(subparsers):
    p = subparsers.add_parser(
        VERB, help=SUMMARY, add_help=False, prefix_chars="\x00"
    )
    p.add_argument("rest", nargs="*")
    p.set_defaults(_run=run)
    return p


def run(args):
    try:
        rc = main(args.rest)
    except SystemExit as e:
        rc = e.code
    if rc is None:
        return 0
    return rc if isinstance(rc, int) else 1
