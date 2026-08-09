"""`issue` consolidated CLI -- verb registry and discovery.

This package hosts one module per `issue` verb. The dispatcher
(``scripts/issue``) discovers them automatically at startup, so adding a
verb is a single new file with **no edit to any shared registry or switch
statement**. That is the property that lets WP1-WP6 land verb modules in
parallel worktrees without merge contention (design
``docs/design/cli-consolidation.md`` sections 2a, 3).

Verb-module protocol
--------------------
Any public module under ``lib/cli/`` (filename not starting with ``_``)
that defines a ``VERB`` attribute is registered. Contract:

======== ==== ==============================================================
attr     kind meaning
======== ==== ==============================================================
VERB     str  verb name within its tier, e.g. ``"show"``.
TIER     str  one of ``"tier1"`` | ``"history"`` | ``"admin"``.
SUMMARY  str  one-line description shown in the tiered ``issue --help``.
ORDER    int  optional sort key within a tier (default 100; ties -> VERB).
register func ``register(subparsers) -> ArgumentParser``: add this verb's
              subparser (its own flags and ``-h``) to the argparse
              subparsers action; MUST call ``parser.set_defaults(_run=run)``.
run      func ``run(args) -> int``: execute with the parsed namespace;
              return an exit code (0/1/2 or the 3-7 loser-side contract).
======== ==== ==============================================================

``show.py`` is the reference implementation. Discovery is import-time and
deterministic (sorted by ``(ORDER, VERB)``).
"""

from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType
from typing import Dict, List

TIERS = ("tier1", "history", "admin")


def discover() -> Dict[str, List[ModuleType]]:
    """Import every verb module and group them by tier.

    Returns a dict keyed by tier name; each value is the tier's verb
    modules sorted by ``(ORDER, VERB)``. A module under ``lib/cli/`` that
    does not declare ``VERB`` (e.g. ``common``) is skipped, not an error.
    """
    groups: Dict[str, List[ModuleType]] = {t: [] for t in TIERS}
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda i: i.name):
        if info.name.startswith("_"):
            continue
        mod = importlib.import_module(f"{__name__}.{info.name}")
        verb = getattr(mod, "VERB", None)
        tier = getattr(mod, "TIER", None)
        if verb is None:
            continue
        if tier not in groups:
            raise ValueError(
                f"lib/cli/{info.name}.py declares VERB={verb!r} with "
                f"invalid TIER={tier!r} (expected one of {TIERS})"
            )
        groups[tier].append(mod)
    for tier in TIERS:
        groups[tier].sort(key=lambda m: (getattr(m, "ORDER", 100), m.VERB))
    return groups
