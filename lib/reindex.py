"""Sibling reindex-regenerator resolution and invocation.

Every mutating CLI regenerates INDEX.md by shelling out to the sibling
`reindex-issues` script that lives next to it in the same bin directory.
The dev tree keeps the suffixed name (`reindex-issues.sh`); the Homebrew
formula installs it renamed without the `.sh` suffix (`bin.install
"reindex-issues"`). Resolving only the suffixed name made auto-reindex a
silent no-op for every installed binary, violating `cli.auto-reindex`.
Resolve both names, and if neither is found emit a visible
stderr warning -- a missing sibling is a broken install, not a transient
reindex hiccup, so the next packaging regression is not silent.

Kept as a shared helper so the three near-identical `run_reindex` copies
(transition-issue, edit-issue, and issue-note via the edit path) cannot
drift.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

# Preferred first: the dev tree ships the suffixed name; the installed
# layout drops the suffix. Both name the same regenerator.
_REINDEX_NAMES = ("reindex-issues.sh", "reindex-issues")


# --- In-process reindex seam --------------------------------
#
# The consolidated `issue` surface regenerates the index by an in-process
# call into `reindex_core`, NOT by resolving and subprocessing a sibling
# `reindex-issues[.sh]` binary. Because there is no sibling name to resolve,
# the failure class (installed binaries silently skipping reindex on a
# name mismatch) cannot occur on the consolidated surface. The consolidated
# mutating verbs (WP3/WP4) call `reindex_after_mutation`; the `issue admin
# reindex` verb (WP5) calls `reindex_core` directly and surfaces errors.
#
# The subprocess path below (`resolve_reindex` / `run_reindex`) is retained
# for the flat binaries until WP7 tombstones them.


def reindex_after_mutation(store_root, anchor, *,
                           suppress_commit: bool = False,
                           to_stderr: bool = False) -> None:
    """Regenerate INDEX.md in-process after a successful mutation.

    store_root None selects the legacy single-dir reindex on `anchor`; a
    store root reindexes the whole store. Best-effort: a reindex hiccup must
    not mask a mutation that already succeeded, matching the prior
    subprocess contract (`check=False`). The `admin reindex` verb does NOT
    use this best-effort wrapper -- it calls `reindex_core` directly so it
    can surface commit errors.
    """
    import reindex_core

    try:
        if store_root is None:
            msg = reindex_core.reindex_legacy_dir(anchor)
        else:
            msg = reindex_core.reindex_store(
                store_root, suppress_commit=suppress_commit
            )
    except Exception:
        return
    if msg:
        # --json stdout carries exactly one JSON document; the
        # reindex notice is informational and moves to stderr on request.
        (sys.stderr if to_stderr else sys.stdout).write(msg + "\n")


def resolve_reindex(bin_dir: Path) -> Optional[Path]:
    """Return the sibling reindex regenerator in bin_dir, or None.

    Tries the suffixed dev-tree name, then the installed no-suffix name.
    """
    for name in _REINDEX_NAMES:
        cand = bin_dir / name
        if cand.is_file():
            return cand
    return None


def run_reindex(bin_dir: Path, store_root, anchor, *,
                suppress_commit: bool = False,
                to_stderr: bool = False) -> None:
    """Invoke the sibling reindex regenerator found next to bin_dir.

    In store mode (store_root not None) run with no arg (full store
    reindex); in legacy mode pass the anchor as the single-dir target.
    Silent on runtime errors so a reindex hiccup does not mask a
    successful mutation -- but a missing sibling (broken install) warns
    on stderr rather than skipping silently.
    """
    reindex = resolve_reindex(bin_dir)
    if reindex is None:
        sys.stderr.write(
            "warning: reindex-issues regenerator not found next to "
            f"{bin_dir}; INDEX.md not refreshed (broken install?)\n"
        )
        return
    cmd = [str(reindex)]
    if store_root is None:
        cmd.append(str(anchor))
    env = os.environ.copy()
    if suppress_commit:
        env["ISSUE_MGMT_NO_AUTOCOMMIT"] = "1"
    try:
        if to_stderr:
            # --json stdout carries exactly one JSON document;
            # the reindex notice is informational and moves to stderr.
            r = subprocess.run(cmd, check=False, env=env,
                               capture_output=True, text=True)
            if r.stdout:
                sys.stderr.write(r.stdout)
            if r.stderr:
                sys.stderr.write(r.stderr)
        else:
            subprocess.run(cmd, check=False, env=env)
    except OSError:
        pass
