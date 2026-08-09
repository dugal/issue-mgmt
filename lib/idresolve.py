"""idresolve -- the one place issue IDs and prefixes are canonicalized.

Previously the `^([A-Z][A-Z0-9]{1,7})-\\d+$` id shape and the
`^[A-Z][A-Z0-9]{1,7}$` prefix shape were copy-pasted into ~7 modules, each
matched strictly: `IM-7` did not resolve `IM-007.md`, and `im-42` did not
resolve the `IM` prefix directory. This module converges those regexes into
one shared resolver and adds a *fallback* canonicalization step -- applied
strictly after an exact match misses -- so a human- or agent-typed
`IM-7` / `im-7` / `IM-0007` all resolve the same on-disk file.

Design decisions (approved):

- D1 -- a dependency-edge target that may not exist yet is canonicalized
  syntactically: case-fold the prefix (always) and integer-normalize the
  number to a `:03d` minimum width. No I/O, no existence check.
- D2 -- if a width-agnostic lookup finds two on-disk files that zero-pad the
  same integer differently (`IM-007.md` AND `IM-0007.md`), fail LOUD with a
  store-integrity error naming both -- never silently pick one.
- Feedback -- resolvers emit a one-line stderr note ONLY when the input was
  actually normalized (`resolved IM-7 -> IM-007`); already-canonical input is
  silent so `--json` stdout stays a single clean document.
- Boundary -- only provably-unique mappings: the case-fold of a
  unique-uppercase prefix and the integer value of a zero-padded number. No
  heuristic matching (substring, Levenshtein, abbreviation).

The already-canonical hot path adds zero glob: it does at most the single
`is_file()` stat the caller already needed. The width-agnostic glob fires
ONLY on an exact-filename miss.

Stdlib only. Imports nothing from ``lib/cli/`` (to avoid an import cycle); it
imports ``errors`` (for the D2 typed exception) and lazily imports
``migrate`` (for the registry read) inside the one function that needs it.
"""

from __future__ import annotations

import re
from pathlib import Path

import errors  # noqa: E402  (lib/ is on sys.path via the entrypoint)

# The two canonical shapes. Every other module aliases these names so the
# id/prefix grammar lives in exactly one place.
ID_RE = re.compile(r"^([A-Z][A-Z0-9]{1,7})-\d+$")
PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9]{1,7}$")

# Non-issue files that live alongside `<PREFIX>-NNN.md` and must never be
# treated as issue files during the width-agnostic glob.
_GLOB_SKIP = {"INDEX.md"}

_MIN_WIDTH = 3


class ResolveResult:
    """Outcome of :func:`resolve_issue_paths`.

    ``canonical_id`` is the on-disk stem the caller must use for EVERYTHING
    downstream -- the per-issue lock path, every commit message, the
    ``--json`` ``id`` field, audit note lines, dep ``to:`` edges, and stderr
    text. ``issue_file`` / ``lock_path`` are built from it. ``normalized`` is
    True when ``canonical_id`` differs from the raw input.
    """

    __slots__ = (
        "canonical_id", "prefix", "issue_file", "lock_path",
        "store_root", "anchor", "normalized",
    )

    def __init__(self, canonical_id, prefix, issue_file, lock_path,
                 store_root, anchor, normalized):
        self.canonical_id = canonical_id
        self.prefix = prefix
        self.issue_file = issue_file
        self.lock_path = lock_path
        self.store_root = store_root
        self.anchor = anchor
        self.normalized = normalized

    def as_tuple(self):
        """(issue_file, lock_path, store_root, anchor) -- the legacy 4-tuple."""
        return (self.issue_file, self.lock_path, self.store_root, self.anchor)


def split_id(raw):
    """Case-insensitively parse ``PREFIX-NNN`` into ``(PREFIX, int)``.

    Returns None when ``raw`` is not a syntactically valid id. Pure: no I/O.
    The returned prefix is always upper-cased (the canonical prefix casing).
    """
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    m = ID_RE.match(s.upper())
    if not m:
        return None
    prefix = m.group(1)
    number_text = s.split("-", 1)[1]
    return prefix, int(number_text)


def resolve_prefix(raw_prefix, known_prefixes):
    """Resolve a raw prefix to a known canonical prefix, or None.

    Exact match wins. Otherwise a *sole* case-insensitive match wins. Zero or
    more-than-one case-insensitive match -> None (ambiguous or unknown). Pure:
    no I/O; ``known_prefixes`` is the caller-supplied set of real prefixes.
    """
    if not isinstance(raw_prefix, str):
        return None
    p = raw_prefix.strip()
    if not p:
        return None
    known = set(known_prefixes or ())
    if p in known:
        return p
    up = p.upper()
    matches = [k for k in known if k.upper() == up]
    if len(matches) == 1:
        return matches[0]
    return None


def canonicalize_target(raw, known_prefixes):
    """Syntactic canonical id for a cross-ref target that need not exist.

    Case-folds the prefix (always -- D1) and integer-normalizes the number to
    a ``:03d`` minimum width. When ``known_prefixes`` resolves the prefix, its
    canonical casing is used; otherwise the plain upper-case is kept
    (write-now / resolve-later for an unknown cross-prefix target). Returns
    None when ``raw`` is not a valid id shape. Pure: no I/O.
    """
    parsed = split_id(raw)
    if parsed is None:
        return None
    prefix_upper, n = parsed
    prefix = resolve_prefix(prefix_upper, known_prefixes) or prefix_upper
    return f"{prefix}-{n:0{_MIN_WIDTH}d}"


def _read_prefix_sentinel(anchor):
    """Legacy-mode single prefix from ``<anchor>/.prefix``, or None."""
    if anchor is None:
        return None
    pfile = Path(anchor) / ".prefix"
    if not pfile.is_file():
        return None
    val = pfile.read_text(encoding="utf-8").strip()
    return val or None


def _known_prefixes(store_root, anchor):
    """The set of real prefixes for the active store/anchor.

    Store mode: every ``<store>/<PREFIX>`` directory whose name matches the
    prefix grammar, unioned with the registry's (`<store>/.repos`) values.
    Legacy mode: the single ``<anchor>/.prefix`` value (or empty).
    """
    if store_root is not None:
        store_root = Path(store_root)
        prefixes = set()
        if store_root.is_dir():
            for d in store_root.iterdir():
                if d.is_dir() and PREFIX_RE.match(d.name):
                    prefixes.add(d.name)
        # Lazy import keeps idresolve free of a hard migrate dependency and
        # sidesteps any import-order concern at module load.
        try:
            import migrate  # noqa: E402
            prefixes.update(migrate._read_registry(store_root).values())
        except Exception:
            pass
        return prefixes
    sentinel = _read_prefix_sentinel(anchor)
    return {sentinel} if sentinel else set()


def _find_by_int(dir_, prefix, n):
    """Width-agnostic on-disk lookup: the sole ``prefix-*.md`` whose stem
    parses to integer ``n``.

    Returns the matching stem, or None when no file matches. Raises
    ``errors.StoreIntegrityError`` (D2) when more than one file matches --
    two paddings of the same integer are a store-integrity fault, not a
    resolvable ambiguity. Globs only; called ONLY after an exact miss.
    """
    matches = []
    if not dir_.is_dir():
        return None
    for path in dir_.glob(f"{prefix}-*.md"):
        if path.name in _GLOB_SKIP:
            continue
        parsed = split_id(path.stem)
        if parsed is None:
            continue
        pfx2, m = parsed
        if pfx2 == prefix and m == n:
            matches.append(path)
    if not matches:
        return None
    if len(matches) > 1:
        names = sorted(str(p) for p in matches)
        raise errors.StoreIntegrityError(
            f"issue number {n} is stored under {len(names)} different "
            f"zero-paddings: {', '.join(names)} -- rename or remove all but "
            "one so the id resolves unambiguously",
            files=names,
            issue_id=f"{prefix}-{n}",
        )
    return matches[0].stem


def resolve_issue_paths(issue_id, store_root, anchor, *, emit_note=None):
    """Resolve ``issue_id`` to its canonical on-disk paths.

    ``store_root`` / ``anchor`` are resolved by the caller (via the shared
    ``common`` / ``editcore`` helpers, which own the ``--store`` /
    ``ISSUE_STORE_ROOT`` / sentinel precedence). This function owns only the
    id/prefix canonicalization and the path build.

    The fallback ladder:

    1. ``split_id`` -> raise ValueError('invalid issue id') on a bad shape.
    2. Resolve the prefix. LAZY: when ``<store>/<PFX-upper>`` already exists
       as a directory the known-prefix set is never computed.
    3. FAST PATH: if ``<dir>/<PFX-upper>-<number-as-typed>.md`` is a file,
       the canonical id is that file's stem (one stat, no glob).
    4. FALLBACK (exact miss only): width-agnostic int match over
       ``<PFX>-*.md``; the sole match's stem is canonical; zero matches ->
       the syntactic ``:03d`` canonical (a not-yet-existent file the caller
       will report as not-found); more than one -> D2 fail-loud.
    5. The per-issue lock path is ALWAYS built from ``canonical_id`` (P2).

    Returns a :class:`ResolveResult`. Raises ``ValueError`` on a malformed id
    and ``errors.StoreIntegrityError`` (exit 1) on the D2 collision.
    """
    parsed = split_id(issue_id)
    if parsed is None:
        raise ValueError(f"invalid issue id: {issue_id!r}")
    prefix_upper, n = parsed
    number_text = str(issue_id).strip().split("-", 1)[1]

    if store_root is not None:
        store_root = Path(store_root).expanduser()
        # LAZY prefix resolution: an existing <PFX-upper> dir is the common
        # already-canonical case and needs no registry / iterdir scan.
        if (store_root / prefix_upper).is_dir():
            prefix = prefix_upper
        else:
            prefix = resolve_prefix(
                prefix_upper, _known_prefixes(store_root, anchor)
            ) or prefix_upper
        dir_ = store_root / prefix
        lock_dir = dir_ / ".locks"
    else:
        prefix = resolve_prefix(
            prefix_upper, _known_prefixes(store_root, anchor)
        ) or prefix_upper
        dir_ = Path(anchor)
        lock_dir = dir_ / ".locks"

    exact = dir_ / f"{prefix}-{number_text}.md"
    if exact.is_file():
        canonical_id = exact.stem
    else:
        found = _find_by_int(dir_, prefix, n)
        canonical_id = found if found is not None else f"{prefix}-{n:0{_MIN_WIDTH}d}"

    issue_file = dir_ / f"{canonical_id}.md"
    lock_path = lock_dir / f"{canonical_id}.lock"

    normalized = canonical_id != str(issue_id).strip()
    if normalized and emit_note is not None:
        emit_note(str(issue_id).strip(), canonical_id)

    return ResolveResult(
        canonical_id, prefix, issue_file, lock_path,
        store_root, Path(anchor) if anchor is not None else None, normalized,
    )
