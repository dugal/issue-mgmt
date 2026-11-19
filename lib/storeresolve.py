"""storeresolve -- the one store-root resolution chain (IM-280).

Every verb resolves the store root through the single 4-step chain here:

    --store flag -> ISSUE_STORE_ROOT -> <anchor>/.migrated-to-store sentinel
    -> None (legacy .issues/ mode)

``anchor`` is the project's ``.issues`` directory (``common.find_project_anchor``
/ ``editcore.find_project_anchor``). There is deliberately no config-file leg
and no prompt in this chain: an ambient per-machine config redirecting every
verb's writes is the failure class the store-safety convention guards against.

Exactly three admin verbs deviate, each because it cannot rely on this chain
and each documenting the reason at its own resolver:

- ``admin init-project`` and ``admin migrate`` run BEFORE the sentinel exists
  (the sentinel is their output), so they fall through to the per-machine
  config file ``~/.config/issue-mgmt/config`` (key ``store_root``) --
  ``init-project`` additionally to an interactive prompt.
- ``admin snapshot`` is designed to run from cron, where no env var is set and
  no sentinel walk-up from ``$HOME`` succeeds, so it too falls through to the
  config file.

Those verbs share ``read_config_store_root`` below rather than carrying their
own copies of the reader.
"""

from __future__ import annotations

import os
from pathlib import Path

import errors

CONFIG_PATH = Path.home() / ".config" / "issue-mgmt" / "config"


def read_sentinel_payload(sentinel: Path) -> str:
    """Return the trimmed payload of a ``.migrated-to-store`` sentinel.

    A present-but-empty (or whitespace-only) sentinel is corrupt routing
    state, never a store root: ``Path('')`` is ``Path('.')``, so silently
    accepting it would reroute the verb to whatever directory it runs from
    -- which exists, passes the store-unresolvable guard, and misreports
    every downstream failure (IM-294). One rule for every sentinel reader:
    fail loud naming the sentinel path, so the caller fixes the file
    instead of chasing a phantom store.
    """
    payload = sentinel.read_text(encoding="utf-8").strip()
    if not payload:
        raise errors.PreconditionError(
            "corrupt .migrated-to-store sentinel: file exists but carries "
            "no store path (re-run `issue admin init-project`, or remove "
            "the sentinel to restore legacy .issues/ mode)",
            reason="corrupt-sentinel",
            sentinel=str(sentinel),
        )
    return payload


def resolve_store_root(anchor: Path, cli_store=None):
    """Resolve the store root, or None for legacy ``.issues/`` mode.

    Precedence: ``--store`` -> ``ISSUE_STORE_ROOT`` ->
    ``<anchor>/.migrated-to-store`` sentinel -> None. Each leg is
    ``expanduser``'d so a literal ``~/...`` in a flag, env var, or sentinel
    payload resolves the same way everywhere. A present-but-empty sentinel
    raises ``PreconditionError`` (``reason=corrupt-sentinel``) rather than
    resolving -- see ``read_sentinel_payload``. An earlier leg still
    short-circuits: a corrupt sentinel is only reached, and only reported,
    when the chain actually falls through to it.
    """
    if cli_store:
        return Path(cli_store).expanduser()
    env = os.environ.get("ISSUE_STORE_ROOT")
    if env:
        return Path(env).expanduser()
    sentinel = anchor / ".migrated-to-store"
    if sentinel.is_file():
        return Path(read_sentinel_payload(sentinel)).expanduser()
    return None


def read_config_store_root():
    """Return ``store_root`` from ``~/.config/issue-mgmt/config``, or None.

    Consumed ONLY by the three admin verbs whose documented deviation adds a
    config-file leg (see module docstring); no Tier-1 verb reads this file.
    """
    if not CONFIG_PATH.is_file():
        return None
    try:
        for raw in CONFIG_PATH.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == "store_root":
                return value.strip() or None
    except OSError:
        return None
    return None
