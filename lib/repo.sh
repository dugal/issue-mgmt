#!/usr/bin/env bash
# Repo-identity helper: resolve the basename of the main checkout from
# any worktree.
#
# Usage:
#   source "${LIB_DIR}/repo.sh"
#   if name=$(repo::main_basename); then
#       echo "main repo: $name"
#   fi
#
# Returns:
#   0  -- success; basename printed to stdout
#   1  -- not inside a git repository, OR the main checkout has been
#         deleted while a linked worktree still survives. In the latter
#         case a diagnostic is written to stderr.
#
# Why not `basename $(git rev-parse --show-toplevel)`?
#   `--show-toplevel` returns the *invoking* worktree's root, which for
#   any linked worktree is the worktree's own basename (e.g.
#   `issue-094-foo`), not the main repo (`issue-mgmt`). `--git-common-dir`
#   returns the shared `.git` directory regardless of which worktree the
#   call originates from; its parent is the main repo's root.

repo::main_basename() {
    local common_dir common_dir_abs main_toplevel
    if ! common_dir=$(git rev-parse --git-common-dir 2>/dev/null); then
        return 1
    fi
    # `cd && pwd` resolves a relative common-dir (e.g. `.git` when run
    # from the primary checkout) against cwd.
    common_dir_abs=$(cd "$common_dir" 2>/dev/null && pwd) || return 1
    main_toplevel=$(dirname "$common_dir_abs")
    if [[ ! -d "$main_toplevel" ]]; then
        echo "Error: main checkout has been deleted: $main_toplevel" >&2
        echo "       This linked worktree cannot resolve the parent repo's identity." >&2
        echo "       Pass --repo NAME explicitly, or restore the main checkout." >&2
        return 1
    fi
    basename "$main_toplevel"
}
