"""lease -- TTL lease primitive helpers.

Frontmatter-level helpers for the three lease fields, all top-level
siblings (docs/design/discourse-workflow-split.md Section 4.1):

    claim:                      # object; present only while claimed
      assignee: alice
      claimed_at: 2026-06-20T13:46:00Z
    lease_until: 2026-06-20T14:46:00Z   # sibling of claim, not nested
    claim_generation: 8                 # persists across release/reap

Semantics (W0a Section F in docs/design/work-planning-primitives.md):

  - `claim_generation` counts ownership eras. It bumps whenever `claim:`
    transitions from absent-or-expired to present-and-valid; renew
    preserves it; release/reap leave it unchanged. Missing field reads
    as 0; the first-ever claim sets 1 (F5 lenient back-compat).
  - `lease_until` is an absolute ISO 8601 UTC timestamp. A claim whose
    `lease_until` is in the past (or missing/malformed) is inactive.
  - Claimants match ^[A-Za-z0-9._@-]{1,64}$.

Operates on the raw `fm_lines` list produced by lib/frontmatter.py so
byte-preservation of untouched frontmatter lines holds. Only top-level
(non-indented) lines are matched; the nested `assignee` / `claimed_at`
children are managed as part of the `claim:` block.
"""

import datetime
import re
from typing import List, Optional, Tuple

DEFAULT_LEASE_TTL = 3600  # seconds; hard-coded binary default (design 9.5)

CLAIMANT_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")

_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


# ----------------------------------------------------------------------
# Timestamps
# ----------------------------------------------------------------------


def now_utc() -> datetime.datetime:
    """Current UTC time, second precision (lease timestamps do not need
    sub-second resolution and second precision keeps files readable)."""
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def format_ts(dt: datetime.datetime) -> str:
    """Render an aware datetime as `YYYY-MM-DDTHH:MM:SSZ` (UTC)."""
    return dt.astimezone(datetime.timezone.utc).strftime(_TS_FORMAT)


def parse_ts(s: str) -> datetime.datetime:
    """Parse an ISO 8601 timestamp; `Z` suffix and offset forms accepted.

    Naive timestamps are interpreted as UTC. Raises ValueError on
    malformed input.
    """
    s2 = s.strip()
    if s2.endswith("Z"):
        s2 = s2[:-1] + "+00:00"
    dt = datetime.datetime.fromisoformat(s2)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


# ----------------------------------------------------------------------
# Top-level frontmatter line helpers
# ----------------------------------------------------------------------


def _strip_quotes(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and ((v[0] == v[-1] == '"') or (v[0] == v[-1] == "'")):
        return v[1:-1]
    return v


def _top_level_index(fm_lines: List[str], key: str) -> Optional[int]:
    """Index of the top-level (non-indented) line for `key`, or None."""
    pattern = re.compile(rf"^{re.escape(key)}:(\s|$)")
    for i, line in enumerate(fm_lines):
        content = line.rstrip("\r\n")
        if not content or content[0] in " \t":
            continue
        if pattern.match(content) or content == f"{key}:":
            return i
    return None


def _get_top_level(fm_lines: List[str], key: str) -> Optional[str]:
    idx = _top_level_index(fm_lines, key)
    if idx is None:
        return None
    content = fm_lines[idx].rstrip("\r\n")
    _, _, raw = content.partition(":")
    value = _strip_quotes(raw)
    return value if value else None


def _set_top_level(fm_lines: List[str], key: str, value) -> None:
    """Set a top-level scalar `key: value` line (replace or append)."""
    idx = _top_level_index(fm_lines, key)
    new_line = f"{key}: {value}\n"
    if idx is not None:
        fm_lines[idx] = new_line
    else:
        fm_lines.append(new_line)


def _remove_top_level(fm_lines: List[str], key: str) -> None:
    """Remove a top-level scalar line for `key` if present (no children)."""
    idx = _top_level_index(fm_lines, key)
    if idx is not None:
        del fm_lines[idx]


def _claim_block_bounds(fm_lines: List[str]) -> Optional[Tuple[int, int]]:
    """(start, end) line-index bounds of the `claim:` block, or None.

    `end` is exclusive: the index of the first following line that is
    not an indented child (blank lines terminate the block too).
    """
    start = _top_level_index(fm_lines, "claim")
    if start is None:
        return None
    end = start + 1
    while end < len(fm_lines):
        content = fm_lines[end].rstrip("\r\n")
        if not content or content[0] not in " \t":
            break
        end += 1
    return start, end


# ----------------------------------------------------------------------
# Lease field accessors
# ----------------------------------------------------------------------


def get_claim(fm_lines: List[str]) -> Optional[dict]:
    """Return {"assignee": ..., "claimed_at": ...} or None when unclaimed.

    Missing children come back as None values; a bare `claim:` line with
    no children (malformed remnant) reads as an empty claim dict.
    """
    bounds = _claim_block_bounds(fm_lines)
    if bounds is None:
        return None
    start, end = bounds
    claim = {"assignee": None, "claimed_at": None}
    for line in fm_lines[start + 1:end]:
        content = line.strip()
        key, _, raw = content.partition(":")
        key = key.strip()
        if key in claim:
            value = _strip_quotes(raw)
            claim[key] = value if value else None
    return claim


def get_lease_until(fm_lines: List[str]) -> Optional[str]:
    """The top-level `lease_until` value, or None when absent/empty."""
    return _get_top_level(fm_lines, "lease_until")


def get_claim_generation(fm_lines: List[str]) -> int:
    """The `claim_generation` counter; missing reads as 0 (W0a F5).

    Raises ValueError when present but not a non-negative integer.
    """
    raw = _get_top_level(fm_lines, "claim_generation")
    if raw is None:
        return 0
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(
            f"claim_generation is not an integer: {raw!r}"
        )
    if value < 0:
        raise ValueError(f"claim_generation is negative: {value}")
    return value


def lease_is_active(fm_lines: List[str], *, now=None) -> bool:
    """True when a claim is present AND its lease has not expired.

    A claim with a missing or malformed `lease_until` is treated as
    expired (reap-able), never as an infinite lease.
    """
    if get_claim(fm_lines) is None:
        return False
    lease_until = get_lease_until(fm_lines)
    if not lease_until:
        return False
    try:
        expiry = parse_ts(lease_until)
    except ValueError:
        return False
    return expiry > (now or now_utc())


# ----------------------------------------------------------------------
# Lease field mutators (operate in place on fm_lines)
# ----------------------------------------------------------------------


def set_claim(
    fm_lines: List[str],
    assignee: str,
    claimed_at: str,
    lease_until: str,
    generation: int,
) -> None:
    """Write the full lease state: `claim:` block + top-level
    `lease_until` + `claim_generation`.

    Replaces an existing `claim:` block in place; otherwise appends the
    block at the end of the frontmatter. `lease_until` and
    `claim_generation` are written as top-level scalars (design 9.4:
    single source of truth, not mirrored inside `claim:`).
    """
    block = [
        "claim:\n",
        f"  assignee: {assignee}\n",
        f"  claimed_at: {claimed_at}\n",
    ]
    bounds = _claim_block_bounds(fm_lines)
    if bounds is not None:
        start, end = bounds
        fm_lines[start:end] = block
    else:
        fm_lines.extend(block)
    _set_top_level(fm_lines, "lease_until", lease_until)
    _set_top_level(fm_lines, "claim_generation", generation)


def set_lease_until(fm_lines: List[str], lease_until: str) -> None:
    """Extend/replace the top-level `lease_until` value (renew path)."""
    _set_top_level(fm_lines, "lease_until", lease_until)


def clear_claim(fm_lines: List[str]) -> None:
    """Remove the `claim:` block and `lease_until`; preserve
    `claim_generation` (W0a F1/F2 -- the counter marks the last era and
    the next claim bumps from it)."""
    bounds = _claim_block_bounds(fm_lines)
    if bounds is not None:
        start, end = bounds
        del fm_lines[start:end]
    _remove_top_level(fm_lines, "lease_until")
