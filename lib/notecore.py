"""notecore -- issue-note's date/text helpers.

Relocated verbatim from the flat ``scripts/issue-note`` binary when that
binary became a v3.0.0 tombstone stub. The consolidated ``issue note``
verb (``lib/cli/note.py``) reuses these two helpers and delegates the
mutation itself to ``editcore.apply_edits`` (commit message ``note <id>``),
exactly as the flat binary did.
"""

from __future__ import annotations

import datetime
import re
import sys

DEFAULT_SECTION = "Implementation Notes"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def resolve_date(raw):
    """Return a YYYY-MM-DD string. Raises ValueError on invalid input."""
    if raw is None:
        return datetime.date.today().isoformat()
    if not _DATE_RE.match(raw):
        raise ValueError(
            f"--date must be YYYY-MM-DD (got {raw!r})"
        )
    # Strict calendar validation (e.g. rejects 2026-02-30, 2026-13-01).
    try:
        datetime.date.fromisoformat(raw)
    except ValueError as e:
        raise ValueError(f"--date is not a valid calendar date: {raw!r}: {e}")
    return raw


def read_text(positional):
    """Return the note text. `-` reads stdin (trailing newline stripped)."""
    if positional == "-":
        data = sys.stdin.read()
        # Strip a single trailing newline so `echo "x" | issue note ID -`
        # yields the same payload as `issue note ID "x"`. Internal
        # newlines are preserved.
        if data.endswith("\n"):
            data = data[:-1]
        return data
    return positional
