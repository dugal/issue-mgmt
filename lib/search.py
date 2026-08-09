"""Search and filter primitives for issue files.

Used by scripts/search-issues. Pure read-only: no locks, no writes.
The CLI builds a Matcher from --syntax + <query>, enumerates the
issue corpus under store or legacy mode, applies frontmatter filters,
and asks the matcher for body matches.

Public surface:

    class BodyMatch           -- {line, text}
    class IssueMatch          -- {id, file, title, status, priority, type, body_matches}
    class FrontmatterFilter   -- prefix / status / type allowlists; AND across fields
    class SubstringMatcher    -- case-insensitive (default) literal substring
    class RegexMatcher        -- Python re; --syntax regex
    class NoBodyFilter        -- empty query: pass-through, populates no body_matches

    matcher = build_matcher(syntax, query, case_sensitive=False)
    files   = enumerate_issue_files(store_root, anchor, prefixes=None)
    match   = match_issue(path, matcher, ff)   # IssueMatch | None

Matchers expose an `is_filter` boolean so callers can short-circuit
the body scan when no body filter is active (empty query).

Body match line numbers are file-absolute (counting from line 1 of
the issue file) so callers can navigate directly with editors. Each
matcher reports body-text-relative line numbers in isolation;
match_issue translates by adding `len(fm_lines) + 2` (opening fence +
fm content + closing fence).

A future Lucene matcher plugs into the same Matcher contract:
it adds `--syntax lucene` to build_matcher and produces BodyMatch
records consistent with the existing shape. Field-scoped Lucene
clauses against frontmatter ANDs with the explicit FrontmatterFilter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Set

import frontmatter
import idresolve

# Aliased to the single shared grammar in lib/idresolve.py.
PREFIX_RE = idresolve.PREFIX_RE
_ID_RE = idresolve.ID_RE
_LEGACY_SKIP = frozenset({"INDEX.md", "ISSUE-TEMPLATE.md"})


@dataclass
class BodyMatch:
    line: int
    text: str

    def to_dict(self) -> dict:
        return {"line": self.line, "text": self.text}


@dataclass
class IssueMatch:
    id: str
    file: Path
    title: str
    status: str
    priority: str
    type: str
    body_matches: List[BodyMatch] = field(default_factory=list)
    title_match: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "file": str(self.file),
            "title": self.title,
            "status": self.status,
            "priority": self.priority,
            "type": self.type,
            "title_match": self.title_match,
            "body_matches": [bm.to_dict() for bm in self.body_matches],
        }


class NoBodyFilter:
    """Matcher that accepts every body and reports no line matches.

    Selected when the positional query is empty -- callers want
    frontmatter-only filtering. `is_filter` is False so match_issue
    skips the body scan entirely.
    """

    is_filter = False
    query = ""

    def search(self, body_text: str) -> List[BodyMatch]:  # pragma: no cover
        return []


class SubstringMatcher:
    """Literal substring match, line-scoped. Case-insensitive by default."""

    is_filter = True

    def __init__(self, query: str, *, case_sensitive: bool = False) -> None:
        self.query = query
        self.case_sensitive = case_sensitive
        self._needle = query if case_sensitive else query.lower()

    def search(self, body_text: str) -> List[BodyMatch]:
        out: List[BodyMatch] = []
        for lineno, line in enumerate(body_text.splitlines(), start=1):
            hay = line if self.case_sensitive else line.lower()
            if self._needle in hay:
                out.append(BodyMatch(line=lineno, text=line))
        return out


class RegexMatcher:
    """Python re.search, line-scoped. Case-insensitive by default."""

    is_filter = True

    def __init__(self, query: str, *, case_sensitive: bool = False) -> None:
        self.query = query
        self.case_sensitive = case_sensitive
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            self._re = re.compile(query, flags)
        except re.error as e:
            raise ValueError(f"invalid regex: {e}") from e

    def search(self, body_text: str) -> List[BodyMatch]:
        out: List[BodyMatch] = []
        for lineno, line in enumerate(body_text.splitlines(), start=1):
            if self._re.search(line):
                out.append(BodyMatch(line=lineno, text=line))
        return out


SUPPORTED_SYNTAXES = ("substring", "regex")


def build_matcher(
    syntax: str,
    query: str,
    *,
    case_sensitive: bool = False,
):
    """Construct a matcher for (syntax, query).

    Empty query short-circuits to NoBodyFilter regardless of syntax.
    Unknown syntax raises ValueError; the CLI maps that to exit 2.
    """
    if query == "":
        return NoBodyFilter()
    if syntax == "substring":
        return SubstringMatcher(query, case_sensitive=case_sensitive)
    if syntax == "regex":
        return RegexMatcher(query, case_sensitive=case_sensitive)
    raise ValueError(
        f"unknown --syntax {syntax!r}; supported: {{{', '.join(SUPPORTED_SYNTAXES)}}}"
    )


@dataclass
class FrontmatterFilter:
    """AND-of-allowlists for prefix / status / type. None means accept-all."""

    prefixes: Optional[Set[str]] = None
    statuses: Optional[Set[str]] = None
    types: Optional[Set[str]] = None

    def accepts(self, fm_lines, id_value: str) -> bool:
        if self.prefixes is not None:
            prefix = _extract_prefix(id_value)
            if prefix not in self.prefixes:
                return False
        if self.statuses is not None:
            status = frontmatter.get_field(fm_lines, "status")
            if status not in self.statuses:
                return False
        if self.types is not None:
            ftype = frontmatter.get_field(fm_lines, "type")
            if ftype not in self.types:
                return False
        return True


def parse_csv_set(value: Optional[str]) -> Optional[Set[str]]:
    """Parse a comma-separated CLI value into a set, or None when unset.

    Empty / whitespace-only input becomes None (accept-all), matching
    the behavior of an absent flag. Whitespace around individual items
    is trimmed.
    """
    if value is None:
        return None
    parts = {p.strip() for p in value.split(",") if p.strip()}
    return parts or None


def enumerate_issue_files(
    store_root: Optional[Path],
    anchor: Optional[Path],
    prefixes: Optional[Set[str]] = None,
) -> Iterator[Path]:
    """Yield issue file Paths.

    Store mode (store_root is not None): scan <store>/<PREFIX>/*.md
      across every PREFIX dir whose name matches `^[A-Z][A-Z0-9]{1,7}$`.
      Filter to `prefixes` when given.

    Legacy mode (store_root is None): scan <anchor>/*.md.
      `prefixes` filters on each file's ID prefix (split on `-`).
    """
    if store_root is not None:
        if not store_root.is_dir():
            return
        for sub in sorted(store_root.iterdir()):
            if not sub.is_dir():
                continue
            if not PREFIX_RE.match(sub.name):
                continue
            if prefixes is not None and sub.name not in prefixes:
                continue
            for f in sorted(sub.glob("*.md")):
                if f.name in _LEGACY_SKIP:
                    continue
                yield f
        return

    if anchor is None or not anchor.is_dir():
        return
    for f in sorted(anchor.glob("*.md")):
        if f.name in _LEGACY_SKIP:
            continue
        if prefixes is not None:
            prefix = _extract_prefix(f.stem)
            if prefix not in prefixes:
                continue
        yield f


def match_issue(
    path: Path,
    matcher,
    ff: FrontmatterFilter,
) -> Optional[IssueMatch]:
    """Read path and return an IssueMatch when filters + matcher accept it.

    Files with malformed frontmatter are skipped silently (return None);
    structural validation is `validate-issue`'s job.
    """
    try:
        fm_lines, body_text = frontmatter.read_issue(path)
    except (OSError, ValueError):
        return None

    id_value = frontmatter.get_field(fm_lines, "id")
    if not id_value:
        return None

    if not ff.accepts(fm_lines, id_value):
        return None

    title = frontmatter.get_field(fm_lines, "title") or ""
    title_match = False
    if matcher.is_filter:
        # The free-text query matches the title as well as the body, so an
        # issue is found by what it is about even when the body is empty
        # (the common case right after creation). The title lives in
        # frontmatter, which is otherwise reached only by the --status/
        # --type/--prefix filters; matching it here is a text hit, not a
        # filter. Reuse the same matcher on the one-line title string.
        body_matches = matcher.search(body_text)
        title_match = bool(matcher.search(title))
        if not body_matches and not title_match:
            return None
        # Translate body-text-relative line numbers to file-absolute so
        # consumers can navigate directly (e.g. vim +<N> <file>).
        offset = len(fm_lines) + 2
        for bm in body_matches:
            bm.line += offset
    else:
        body_matches = []

    return IssueMatch(
        id=id_value,
        file=path,
        title=title,
        status=frontmatter.get_field(fm_lines, "status") or "",
        priority=frontmatter.get_field(fm_lines, "priority") or "",
        type=frontmatter.get_field(fm_lines, "type") or "",
        body_matches=body_matches,
        title_match=title_match,
    )


def search(
    store_root: Optional[Path],
    anchor: Optional[Path],
    matcher,
    ff: FrontmatterFilter,
) -> List[IssueMatch]:
    """High-level: enumerate + match. Returns the list of matches in scan order."""
    out: List[IssueMatch] = []
    for path in enumerate_issue_files(store_root, anchor, ff.prefixes):
        m = match_issue(path, matcher, ff)
        if m is not None:
            out.append(m)
    return out


def _extract_prefix(id_or_stem: str) -> str:
    m = _ID_RE.match(id_or_stem)
    if m:
        return m.group(1)
    # Stem fallback: strip the trailing -NNN if present
    if "-" in id_or_stem:
        return id_or_stem.rsplit("-", 1)[0]
    return id_or_stem
