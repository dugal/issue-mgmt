"""Shared helpers for `issue` verb modules.

Every verb module under ``lib/cli/`` imports from here for the standard
global flags, the version string, and store-root resolution so the flag
names and resolution precedence stay identical across the whole
consolidated surface. Keeping them in one module is what lets the global
contract in ``docs/design/cli-consolidation.md`` section 4.Q4 be honored
without a fan-out WP re-deriving it.

The heavy data-path modules (``lib/frontmatter.py``, ``lib/concurrency.py``,
...) are untouched; this module carries only argument-surface glue.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import idresolve  # noqa: E402  (lib/ is on sys.path via the entrypoint)

# Single source of the consolidated CLI version. `stamp-version` rewrites
# this line at release time (directory-wide scan; see docs/releases.md).
VERSION = "v3.3.1"

# <PREFIX>-NNN, prefix ^[A-Z][A-Z0-9]{1,7}$. Aliased to the single shared
# grammar in lib/idresolve.py -- do not re-derive it here.
ID_RE = idresolve.ID_RE


# --- Global flags (design 4.Q4) --------------------------------------------
#
# Defined once here and attached by each verb's register() so a verb owns
# exactly the globals that apply to it: read-only verbs take --store/--json;
# mutating verbs additionally take --lock-timeout/--no-reindex. A verb may
# still override a global default locally.


def add_store_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--store",
        default=None,
        metavar="ROOT",
        help="Store root override (precedence: --store -> ISSUE_STORE_ROOT "
        "-> .migrated-to-store sentinel -> legacy .issues/).",
    )


def add_version_flag(p: argparse.ArgumentParser) -> None:
    # Every verb accepts --version so a per-verb invocation
    # (`issue show --version`) prints the version and exits 0, matching the
    # flat binaries the consolidated surface replaces. Top-level
    # `issue --version` is handled by the entrypoint.
    p.add_argument("--version", action="version", version=VERSION)


def add_json_flag(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--json",
        action="store_true",
        dest="json_mode",
        help="Emit a machine-readable JSON document on stdout "
        "(informational notices go to stderr; do not merge the streams).",
    )


def add_json_field_flag(p: argparse.ArgumentParser) -> None:
    # Read-verb variant of --json with gh-style field selection.
    # nargs="?"/const=True makes bare `--json` behave exactly as before
    # (json_mode=True -> full documented shape), while `--json id,title`
    # captures the comma list for lib/jsonfields to project. Absent -> None.
    p.add_argument(
        "--json",
        nargs="?",
        const=True,
        default=None,
        dest="json_mode",
        metavar="FIELDS",
        help="Emit a machine-readable JSON document on stdout (informational "
        "notices go to stderr; do not merge the streams). Optionally "
        "pass a comma-separated field list (e.g. --json id,title,status) to "
        "project each object to those top-level fields; bare --json emits the "
        "full documented shape. Unknown field name -> exit 2.",
    )


def add_lock_timeout_flag(p: argparse.ArgumentParser) -> None:
    # Mutating verbs only. Kept here so every mutating verb spells the flag
    # identically; the value is consumed by the shared concurrency path.
    p.add_argument(
        "--lock-timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Per-record advisory-lock acquisition timeout in seconds.",
    )


def add_no_reindex_flag(p: argparse.ArgumentParser) -> None:
    # Mutating verbs only. Suppresses the post-mutation INDEX regeneration.
    p.add_argument(
        "--no-reindex",
        action="store_true",
        help="Skip the automatic INDEX.md regeneration after the mutation.",
    )


# --- Store-root resolution (parity with the flat binaries) -----------------


def find_project_anchor() -> Path:
    """Nearest ``.issues/`` walking up from cwd, else an env/cwd fallback.

    Byte-identical to the resolution the flat binaries used; carried here
    so every verb resolves the anchor the same way.
    """
    cwd = Path.cwd().resolve()
    for d in [cwd, *cwd.parents]:
        if (d / ".issues").is_dir():
            return d / ".issues"
    workspace = os.environ.get("ISSUE_MGMT_ROOT")
    if workspace:
        return Path(workspace) / ".issues"
    return cwd / ".issues"


def resolve_store_root(anchor: Path, cli_store):
    """Resolve the store root, or None for legacy mode.

    Precedence: --store -> ISSUE_STORE_ROOT -> .migrated-to-store sentinel
    -> None (legacy .issues/ mode).
    """
    if cli_store:
        return Path(cli_store)
    env = os.environ.get("ISSUE_STORE_ROOT")
    if env:
        return Path(env)
    sentinel = anchor / ".migrated-to-store"
    if sentinel.is_file():
        return Path(sentinel.read_text(encoding="utf-8").strip())
    return None


def canonicalize_prefix_set(prefixes, store_root, anchor, *, emit_note=None):
    """Case-fold each scoping prefix to its known canonical form.

    ``--prefix``/``--repo`` on the read verbs scope by prefix directory;
    previously ``--prefix im`` scoped to a non-existent ``im`` dir and
    returned nothing. This maps each raw prefix through
    ``idresolve.resolve_prefix`` against the store's real prefixes. An
    unknown or ambiguous prefix passes through unchanged (so scoping to a
    genuinely absent prefix keeps its existing empty-result behavior).
    ``prefixes`` is the parsed set (or None); returns the mapped set.
    """
    if not prefixes:
        return prefixes
    known = idresolve._known_prefixes(store_root, anchor)
    out = set()
    for p in prefixes:
        resolved = idresolve.resolve_prefix(p, known)
        if resolved is not None and resolved != p:
            if emit_note is not None:
                emit_note(p, resolved)
            out.add(resolved)
        else:
            out.add(p)
    return out
