"""validate -- structural validator for issue-mgmt markdown files.

Pure structural validation: frontmatter schema, allowed values, ID format,
and filename / parent-directory consistency. Does not check business logic
(branch existence, dependency resolution, content correctness, completeness
of Implementation Notes, etc.).

Public surface:

    from validate import validate_file, ValidationResult, Violation

    result = validate_file(path)
    if result.ok:
        ...
    for v in result.violations:
        print(v.format(result.path))

The CLI entry point lives in scripts/validate-issue.

Schema (canonical for v2.x; matches docs/spec.md and ISSUE-TEMPLATE.md):

Required frontmatter:
    id        ^[A-Z][A-Z0-9]{1,7}-\\d+$
    title     non-empty string
    type      bug | feature | chore | spike | decision
    status    open | in-progress | blocked | done | cancelled
    priority  high | medium | low
    created   YYYY-MM-DD
    updated   YYYY-MM-DD

Optional frontmatter (validator does not error on absence; values are
unconstrained when present unless noted):
    branch    free-form string
    tags      free-form (typically YAML list)
    changelog free-form (empty, "skip", "unreleased", or a version tag)
    claim     object (assignee, claimed_at); present only while claimed
    lease_until       ISO 8601 timestamp; sibling of claim, not nested
    claim_generation  non-negative integer when present (W0a F5); missing
                      reads as 0

Structural checks:
    Filename stem matches id.
    If parent directory name matches ^[A-Z][A-Z0-9]{1,7}$, it must equal
    the id's prefix.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import idresolve  # noqa: E402  (lib/ is on sys.path via the entrypoint)

# IM_DEP_KINDS lives in lib.deps; the block-aware frontmatter parser the
# dep-shape lint needs lives in lib.frontmatter (both relocated out of the
# retired bd bridge). The imports are guarded so a stripped-down
# deployment that lacks them still loads validate; the dep-shape lint then
# falls back to a static kind tuple / no-op.
try:  # pragma: no cover - exercised indirectly via the dep-shape lint
    from deps import IM_DEP_KINDS
    from frontmatter import parse_frontmatter_lines as _parse_fm_dict
except Exception:  # pragma: no cover
    try:
        from lib.deps import IM_DEP_KINDS  # type: ignore
        from lib.frontmatter import (  # type: ignore
            parse_frontmatter_lines as _parse_fm_dict,
        )
    except Exception:
        IM_DEP_KINDS = ("blocks", "parent-child", "related", "discovered-from")
        _parse_fm_dict = None


ALLOWED_TYPES = ("bug", "feature", "chore", "spike", "decision")
ALLOWED_STATUSES = ("open", "in-progress", "blocked", "done", "cancelled")
ALLOWED_PRIORITIES = ("high", "medium", "low")
ALLOWED_DEP_KINDS = IM_DEP_KINDS

REQUIRED_FIELDS = (
    "id", "title", "type", "status", "priority", "created", "updated",
)

# ID_RE / PREFIX_RE alias the single shared grammar in lib/idresolve.py.
# validate consumes the SHAPE only -- it must NOT canonicalize or
# call resolve_issue_paths: `issue admin validate` structurally lints
# hand-written / migrated frontmatter and must keep accepting the loose
# `PREFIX-<int>` form (e.g. `IM-7`) without rewriting it.
ID_RE = idresolve.ID_RE
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PREFIX_RE = idresolve.PREFIX_RE


@dataclass
class Violation:
    code: str
    message: str
    field: Optional[str] = None
    line: Optional[int] = None
    value: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"code": self.code, "message": self.message}
        if self.field is not None:
            d["field"] = self.field
        if self.line is not None:
            d["line"] = self.line
        if self.value is not None:
            d["value"] = self.value
        return d

    def format(self, path: Path) -> str:
        loc = f"{path}:{self.line}" if self.line is not None else str(path)
        return f"{loc}: {self.code}: {self.message}"


@dataclass
class ValidationResult:
    path: Path
    ok: bool
    violations: List[Violation] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file": str(self.path),
            "valid": self.ok,
            "violations": [v.to_dict() for v in self.violations],
        }


def validate_file(path) -> ValidationResult:
    """Validate the issue file at `path`. Raises OSError / FileNotFoundError
    if the file cannot be read; CLI translates those to exit-2 tool errors.
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    return validate_text(p, text)


def validate_text(path: Path, text: str) -> ValidationResult:
    fields, line_numbers, parse_err = _parse_frontmatter(text)
    if parse_err is not None:
        return ValidationResult(path=path, ok=False, violations=[parse_err])

    violations: List[Violation] = []

    for key in REQUIRED_FIELDS:
        if key not in fields:
            violations.append(Violation(
                code="missing-field",
                field=key,
                message=f"required field {key!r} is missing",
            ))

    if "type" in fields and fields["type"] not in ALLOWED_TYPES:
        violations.append(Violation(
            code="invalid-type",
            field="type",
            line=line_numbers.get("type"),
            value=fields["type"],
            message=(
                f"type {fields['type']!r} not in "
                f"{{{', '.join(ALLOWED_TYPES)}}}"
            ),
        ))

    if "status" in fields and fields["status"] not in ALLOWED_STATUSES:
        violations.append(Violation(
            code="invalid-status",
            field="status",
            line=line_numbers.get("status"),
            value=fields["status"],
            message=(
                f"status {fields['status']!r} not in "
                f"{{{', '.join(ALLOWED_STATUSES)}}}"
            ),
        ))

    if "priority" in fields and fields["priority"] not in ALLOWED_PRIORITIES:
        violations.append(Violation(
            code="invalid-priority",
            field="priority",
            line=line_numbers.get("priority"),
            value=fields["priority"],
            message=(
                f"priority {fields['priority']!r} not in "
                f"{{{', '.join(ALLOWED_PRIORITIES)}}}"
            ),
        ))

    if "title" in fields and not fields["title"]:
        violations.append(Violation(
            code="empty-title",
            field="title",
            line=line_numbers.get("title"),
            message="title must be non-empty",
        ))

    if "claim_generation" in fields:
        raw = fields["claim_generation"]
        ok_int = False
        try:
            ok_int = int(raw) >= 0
        except (TypeError, ValueError):
            ok_int = False
        if not ok_int:
            violations.append(Violation(
                code="invalid-claim-generation",
                field="claim_generation",
                line=line_numbers.get("claim_generation"),
                value=raw,
                message=(
                    f"claim_generation {raw!r} is not a non-negative "
                    "integer"
                ),
            ))

    violations.extend(_check_depends_on(text, line_numbers))

    for key in ("created", "updated"):
        if key in fields and not DATE_RE.match(fields[key]):
            violations.append(Violation(
                code="invalid-date-format",
                field=key,
                line=line_numbers.get(key),
                value=fields[key],
                message=f"{key} {fields[key]!r} is not YYYY-MM-DD",
            ))

    if "id" in fields:
        m = ID_RE.match(fields["id"])
        if not m:
            violations.append(Violation(
                code="invalid-id-format",
                field="id",
                line=line_numbers.get("id"),
                value=fields["id"],
                message=(
                    f"id {fields['id']!r} does not match "
                    f"{ID_RE.pattern}"
                ),
            ))
        else:
            id_prefix = m.group(1)
            stem = path.stem
            if stem != fields["id"]:
                violations.append(Violation(
                    code="id-filename-mismatch",
                    field="id",
                    line=line_numbers.get("id"),
                    value=fields["id"],
                    message=(
                        f"filename stem {stem!r} does not match id "
                        f"{fields['id']!r}"
                    ),
                ))
            parent_name = path.parent.name
            if PREFIX_RE.match(parent_name) and parent_name != id_prefix:
                violations.append(Violation(
                    code="prefix-directory-mismatch",
                    field="id",
                    line=line_numbers.get("id"),
                    value=fields["id"],
                    message=(
                        f"id prefix {id_prefix!r} does not match parent "
                        f"directory {parent_name!r}"
                    ),
                ))

    return ValidationResult(
        path=path, ok=not violations, violations=violations,
    )


def _check_depends_on(text: str, line_numbers) -> List[Violation]:
    """Lint the shape of the `depends_on` frontmatter field.

    The `depends_on` field is a YAML block sequence of dependency edges.
    A malformed value does not fail loudly at authoring time -- the
    authoring path treats any non-list `depends_on` as "unmanaged" and
    leaves it untouched -- so this lint is the out-of-band backstop for any
    authoring path (programmatic or hand-edit) that produces a malformed
    edge.

    The scalar-only `_parse_frontmatter` used for the rest of the schema
    mangles a block sequence, so the dep read runs off
    `frontmatter.parse_frontmatter_lines`, which is block-list-aware. If it
    is unavailable (stripped deployment) the lint is skipped rather than
    failing.

    Rules (report-all, one Violation per bad entry):
      - present value that is not a list -> invalid-depends-on-shape
      - entry missing `to` (mapping form) or a string with no colon / empty
        target -> dep-missing-to
      - `type` present and not one of the four IM kinds -> invalid-dep-kind
      - `to` not a syntactically valid issue id -> malformed-dep-target
    An empty list `[]` and an absent key both pass (both are legitimate).
    Cross-prefix targets pass (shape only; no store resolution).
    """
    if _parse_fm_dict is None:
        return []
    try:
        fm_lines, _ = _frontmatter_lines(text)
    except ValueError:
        return []
    if fm_lines is None:
        return []
    parsed = _parse_fm_dict(fm_lines)
    if "depends_on" not in parsed:
        return []
    raw = parsed["depends_on"]
    line = line_numbers.get("depends_on")

    if raw is None:
        # Empty block (bare `depends_on:` with nothing under it) parses to
        # None. Treat as an empty list -- no managed edges, valid shape.
        return []
    if not isinstance(raw, list):
        return [Violation(
            code="invalid-depends-on-shape",
            field="depends_on",
            line=line,
            value=str(raw),
            message=(
                "depends_on must be a block sequence (list); "
                f"got a scalar {raw!r}"
            ),
        )]

    violations: List[Violation] = []
    for entry in raw:
        to_val: Optional[str] = None
        kind_val: Optional[str] = None
        if isinstance(entry, dict):
            to_val = entry.get("to")
            kind_val = entry.get("type")
        elif isinstance(entry, str):
            head, sep, tail = entry.partition(":")
            if not sep or not tail.strip():
                violations.append(Violation(
                    code="dep-missing-to",
                    field="depends_on",
                    line=line,
                    value=entry,
                    message=(
                        "depends_on string entry must be 'KIND:TARGET'; "
                        f"got {entry!r}"
                    ),
                ))
                continue
            kind_val = head.strip()
            to_val = tail.strip()
        else:
            violations.append(Violation(
                code="dep-missing-to",
                field="depends_on",
                line=line,
                value=str(entry),
                message=(
                    "depends_on entry must be a mapping with 'to' or a "
                    f"'KIND:TARGET' string; got {entry!r}"
                ),
            ))
            continue

        if not to_val:
            violations.append(Violation(
                code="dep-missing-to",
                field="depends_on",
                line=line,
                value=str(entry),
                message="depends_on entry is missing required 'to' target",
            ))
            # Cannot check the target shape without a value; skip further
            # checks on this entry.
            if kind_val is not None and kind_val not in ALLOWED_DEP_KINDS:
                violations.append(Violation(
                    code="invalid-dep-kind",
                    field="depends_on",
                    line=line,
                    value=str(kind_val),
                    message=(
                        f"dep type {kind_val!r} not in "
                        f"{{{', '.join(ALLOWED_DEP_KINDS)}}}"
                    ),
                ))
            continue

        if kind_val is not None and kind_val not in ALLOWED_DEP_KINDS:
            violations.append(Violation(
                code="invalid-dep-kind",
                field="depends_on",
                line=line,
                value=str(kind_val),
                message=(
                    f"depends_on entry type {kind_val!r} not in "
                    f"{{{', '.join(ALLOWED_DEP_KINDS)}}}"
                ),
            ))

        if not ID_RE.match(to_val):
            violations.append(Violation(
                code="malformed-dep-target",
                field="depends_on",
                line=line,
                value=to_val,
                message=(
                    f"depends_on entry target {to_val!r} does not match "
                    f"{ID_RE.pattern}"
                ),
            ))

    return violations


_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(.*?\r?\n)---[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)


def _frontmatter_lines(text: str):
    """Return (fm_lines, body_text) using the shared frontmatter splitter.

    fm_lines is the raw list of lines between the '---' fences (line endings
    preserved), suitable for frontmatter.parse_frontmatter_lines. Raises ValueError
    if the fences are absent (the caller already surfaced malformed-frontmatter
    via _parse_frontmatter, so this path is defensive).
    """
    import frontmatter
    return frontmatter.parse_issue(text)


def _parse_frontmatter(text: str):
    """Return (fields, line_numbers, err).

    On success: fields is a dict[str, str], line_numbers is dict[str, int]
    (1-based), err is None.
    On failure: fields and line_numbers are None, err is a Violation.

    Frontmatter syntax accepted:
        key: value             -- scalar, optional surrounding whitespace
        key: "value"           -- scalar in double quotes
        key: 'value'           -- scalar in single quotes
        key:                   -- empty value
        key: [a, b]            -- inline list (kept as raw string)
        # comment line         -- skipped
        (blank line)           -- skipped
        key: value # comment   -- trailing comment stripped

    Multi-line scalars, block lists, and anchors are not supported; they
    surface as malformed-frontmatter.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return None, None, Violation(
            code="malformed-frontmatter",
            line=1,
            message=(
                "file does not begin with a '---' delimited frontmatter "
                "block (opening and closing '---' lines required)"
            ),
        )

    body = m.group(1)
    fields: Dict[str, str] = {}
    line_numbers: Dict[str, int] = {}
    base = 2  # the line after the opening `---`

    for offset, raw in enumerate(body.splitlines()):
        line_num = base + offset
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        # Indented lines are continuation lines of a block value (nested map
        # entries under `claim:`, or block-sequence items under
        # `depends_on:`). This scalar parser only tracks top-level fields, so
        # skip them -- otherwise a nested key such as the `type:` under a
        # depends_on entry would clobber the real top-level `type` field.
        # Block-aware reads use frontmatter.parse_frontmatter_lines.
        if raw[:1] in (" ", "\t"):
            continue
        if ":" not in raw:
            return None, None, Violation(
                code="malformed-frontmatter",
                line=line_num,
                message=(
                    f"unparseable frontmatter line (no ':'): {raw!r}"
                ),
            )
        key, _, value = raw.partition(":")
        key = key.strip()
        if not key:
            return None, None, Violation(
                code="malformed-frontmatter",
                line=line_num,
                message="empty key in frontmatter line",
            )
        value = _strip_inline_comment(value)
        value = value.strip()
        value = _strip_matched_quotes(value)
        fields[key] = value
        line_numbers[key] = line_num

    return fields, line_numbers, None


def _strip_inline_comment(s: str) -> str:
    """Remove a `# ...` trailing comment from a YAML scalar, respecting
    matched single/double quotes."""
    in_single = False
    in_double = False
    for i, ch in enumerate(s):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if i == 0 or s[i - 1].isspace():
                return s[:i]
    return s


def _strip_matched_quotes(s: str) -> str:
    if len(s) >= 2 and ((s[0] == s[-1] == '"') or (s[0] == s[-1] == "'")):
        return s[1:-1]
    return s
