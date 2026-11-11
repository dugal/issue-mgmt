#!/usr/bin/env python3
"""The project's one throwaway git identity for test fixtures (IM-268).

Every test that commits needs a git identity, and the address is never
asserted on -- it exists only so `git commit` does not refuse. Before this
module each script invented its own, named after whichever issue happened to
need it: nine distinct addresses across nine files.

That mattered because `.publish.yaml` `leaks.allow` had to enumerate them.
The publish gate's email detector reports every address in the public view,
and `_allow_spans` suppresses a hit only when an allow token's span CONTAINS
it -- so a domain-level token suppresses nothing and only the full address
works. Each new fixture address therefore required a matching hand-written
allowlist entry, and three of four authors forgot, twice inside the phase
whose thesis is that declarations get re-validated automatically (IM-240 and
IM-263 each shipped an unlisted address; the gate went red at publish time).

So the fix is to remove the need rather than to make the list easier to
extend: one address, one `leaks.allow` entry, and a new test with no address
to invent. `scripts/check-fixture-emails` enforces that -- every
email-shaped literal in the public view is either this constant or an
explicit `leaks.allow` entry -- and it reads the value from HERE, so this
assignment is the single source of truth. `tests/verification/lib.sh` parses
the same line for the shell suite.

Shrinking the list is also the point: an `allow` entry suppresses a `deny`
hit inside its span, so every entry is an unreviewed override of the deny
list. Fewer entries, less override surface.

The address is under `.invalid` (RFC 6761), which is guaranteed never to
resolve -- unlike `example.com`, which is a real IANA-held domain.

Consumers import it through the project's usual test idiom:

    REPO_ROOT = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(REPO_ROOT / "tests"))
    from fixture_identity import FIXTURE_EMAIL
"""

FIXTURE_EMAIL = "test@example.invalid"
