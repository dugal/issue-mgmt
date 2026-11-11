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

Surface-manifest declarations (IM-245)
--------------------------------------
Three further constants are REQUIRED on every verb module. They carry the
facts the CLI surface manifest (``docs/cli-surface.json``) cannot read off an
argparse parser, so the manifest records intent rather than inferring it.
``scripts/cli-surface`` fails loud on a module missing or mis-spelling any of
them -- a silently incomplete manifest is worse than none, because it reads
as CLEAN.

============================= ==========================================
attr                          meaning
============================= ==========================================
SURFACE_CLASS                 ``surface.verb-class-contract``: one of
                              ``enumeration-read`` | ``point-read`` |
                              ``mutating`` | ``scaffolding``. The class
                              determines the verb's obligations. The
                              extractor checks only that the value is
                              legal; asserting the class-IMPLIED behavior
                              per verb (an enumeration read exits 1 on
                              zero rows, a mutating verb offers
                              ``--lock-timeout``) is IM-246's job, so do
                              not read this field as behaviorally
                              validated.
SURFACE_STORE                 IM-251: how the verb gets a store --
                              ``flag`` (resolves via the precedence chain
                              and must therefore offer ``--store``),
                              ``positional`` (an operand names it),
                              ``pinned`` (pins its own deliberately), or
                              ``none`` (touches no store).
SURFACE_STRUCTURED_ERRORS     IM-254: ``converted`` | ``unconverted``.
                              Ratchets: a converted verb never reverts, a
                              new verb is born converted, and the
                              unconverted set is enumerated rather than
                              waived. Conversion work is IM-260.
============================= ==========================================

Five more are optional, declared only by the verbs that need them:

============================= ==========================================
SURFACE_GENERATION_FIELDS     IM-250: ambient-derived output fields of the
                              first kind -- GENERATION METADATA, a value
                              describing this invocation's own rendering.
                              ``index.generated_at`` and
                              ``history activity.since``.
SURFACE_RECORD_FIELDS         IM-250 / IM-262: ambient-derived output
                              fields of the second kind -- RECORD CONTENT,
                              a clock-read value persisted into the issue
                              being written (``create.created``,
                              ``note.date``, the lease stamps). Dotted
                              paths address nested fields
                              (``claim.lease_until``).
                              Both kinds are measured against the real
                              ``--json`` output by
                              ``tests/test_surface_ambient_fields.py``, so
                              an undeclared ambient field fails the suite
                              rather than riding along unrecorded.
SURFACE_SYNONYMS              ``{canonical: (retired, ...)}`` for synonyms
                              declared inline rather than through a
                              ``common.add_*`` helper. Needed where no
                              heuristic can find the pair -- ``edit``
                              spells ``--kind`` and ``--dep-type`` with
                              two DIFFERENT dests.
SURFACE_PARSER                A zero-arg builder returning the verb's real
                              ``ArgumentParser``, for a verb whose
                              dispatcher subparser is a
                              ``prefix_chars="\x00"`` passthrough
                              (``admin init-store`` / ``init-project`` /
                              ``migrate``).
SURFACE_MANUAL_FLAGS          Literal flag specs for a passthrough verb
                              with no parser at all, which walks argv by
                              hand (``admin selftest`` /
                              ``install-skills``).
============================= ==========================================
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
