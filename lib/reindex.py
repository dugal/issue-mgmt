"""In-process reindex seam for the consolidated `issue` surface.

The consolidated mutating verbs (`create`, `edit`, `note`, `dep`,
`transition`) regenerate INDEX.md by an in-process call into
``reindex_core`` via ``reindex_after_mutation``. There is no sibling
`reindex-issues` binary to resolve and subprocess, so the class of bug
where an installed binary silently skipped auto-reindex on a name
mismatch cannot occur. The `issue admin reindex` verb calls
``reindex_core`` directly (not this best-effort wrapper) so it can
surface commit errors.
"""

from __future__ import annotations

import sys


def reindex_after_mutation(store_root, anchor, *,
                           suppress_commit: bool = False,
                           to_stderr: bool = False) -> None:
    """Regenerate INDEX.md in-process after a successful mutation.

    store_root None selects the legacy single-dir reindex on `anchor`; a
    store root reindexes the whole store. Best-effort: a reindex hiccup must
    not mask a mutation that already succeeded (`check=False` semantics).
    The `admin reindex` verb does NOT use this best-effort wrapper -- it
    calls `reindex_core` directly so it can surface commit errors.
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
