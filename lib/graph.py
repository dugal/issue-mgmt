"""graph -- frontmatter-native `blocks` dependency-graph checks.

Home of the authoring-time cycle guard, relocated here from the
retired bd bridge. `blocks` is the sole ordering relation among
the four IM dependency kinds -- the only one that gates readiness and the
only kind that can form a cycle that matters -- so these checks resolve
the `blocks` graph directly from store frontmatter and never touch any
external workflow engine.

`detect_blocks_cycle` refuses a `blocks` edge that would close a cycle
before the markdown write commits, keeping the markdown source of truth
self-consistent. It raises `errors.DependencyCycleError`
(precondition-failed / reason=dependency-cycle, exit 1), naming the cycle
path.
"""

from pathlib import Path

import deps
import frontmatter
from errors import DependencyCycleError


def _blocks_successors(store_root, issue_id):
    """The ids `issue_id` blocks (its `blocks`-edge targets), read from the
    store frontmatter.

    A missing/unparseable file yields no successors -- an unresolved
    (e.g. cross-prefix write-now/resolve-later) target cannot be part of a
    cycle we can see, so it is skipped, not an error.
    """
    prefix = issue_id.rsplit("-", 1)[0] if "-" in issue_id else issue_id
    path = Path(store_root) / prefix / ("%s.md" % issue_id)
    if not path.is_file():
        return []
    try:
        fm_lines, _body = frontmatter.read_issue(path)
    except (ValueError, OSError):
        return []
    return [e["to"] for e in (deps.get_deps(fm_lines) or [])
            if e.get("type") == "blocks" and e.get("to")]


def detect_blocks_cycle(store_root, issue_id, blocks_targets):
    """Raise DependencyCycleError if giving `issue_id` the blocks-successors
    `blocks_targets` would close a cycle in the store's `blocks` graph.

    The authoring-time guard: `blocks` is the sole ordering
    relation and the only kind that can form a cycle that matters, so only
    `blocks` edges are checked. Frontmatter-native -- it reads the store
    and never calls any external engine, keeping the markdown source of
    truth self-consistent.

    Targeted: it walks only the `blocks` component reachable from
    `blocks_targets` (a self-edge, or a target that already reaches
    `issue_id` via `blocks`), not the whole store. Raises
    DependencyCycleError naming the cycle path
    (`issue_id -> ... -> issue_id`). Returns None when no cycle would form.
    """
    seen = set()
    for start in blocks_targets:
        if start == issue_id:
            cycle = "%s -> %s" % (issue_id, issue_id)
            raise DependencyCycleError(
                "dependency cycle detected: %s" % cycle, cycle=cycle
            )
        stack = [(start, [issue_id, start])]
        while stack:
            node, path = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            for nxt in _blocks_successors(store_root, node):
                if nxt == issue_id:
                    members = " -> ".join(path + [issue_id])
                    raise DependencyCycleError(
                        "dependency cycle detected: %s" % members,
                        cycle=members,
                    )
                if nxt not in seen:
                    stack.append((nxt, path + [nxt]))
