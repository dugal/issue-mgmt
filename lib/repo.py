"""Repo-identity helpers.

Resolve the basename of the main checkout from any worktree -- the
primary checkout, a linked worktree, or a sub-worktree.

`git rev-parse --show-toplevel` returns the *invoking* worktree's root,
which for any linked worktree is the worktree's own basename (e.g.
`issue-094-foo`), not the main repo (`issue-mgmt`). `--git-common-dir`
returns the shared `.git` directory regardless of which worktree the
call originates from; its parent is the main repo's root.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional


def resolve_main_basename() -> Optional[str]:
    """Return the basename of the main checkout, or None on failure.

    Returns None if:
      - git is not on PATH, or
      - the caller is not inside a git repository, or
      - the main checkout has been deleted while a linked worktree
        survives (a diagnostic is written to stderr in this case).
    """
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            check=True, capture_output=True, text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    common_dir = Path(r.stdout.strip())
    # In the primary checkout `--git-common-dir` returns `.git` (relative
    # to cwd); in a linked worktree it returns an absolute path. `resolve()`
    # handles both.
    common_dir_abs = common_dir.resolve()
    main_toplevel = common_dir_abs.parent
    if not main_toplevel.is_dir():
        print(
            f"Error: main checkout has been deleted: {main_toplevel}\n"
            f"       This linked worktree cannot resolve the parent repo's identity.\n"
            f"       Pass --repo NAME explicitly, or restore the main checkout.",
            file=sys.stderr,
        )
        return None
    return main_toplevel.name
