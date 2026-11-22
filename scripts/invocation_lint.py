#!/usr/bin/env python3
"""Operational-doc invocation linter -- the IM-283 check.

`docs/plan.md` and `CLAUDE.md` carry operational instructions that name
specific `issue <verb> --flag` and `make <target>` invocations. Sessions
act on those instructions directly, and nothing else checks them against
the CLI or Makefile they target -- so a retired flag or target survives in
the docs and a session following it runs a command that exits 2 (the
IM-283 `--force` instance), or worse, follows a retired ceremony (the
v3.5.0 `make release-gitea` mis-publish; the `make publish` /
`make distribute` step found in this file's own adoption sweep).

This module resolves every PRESCRIBED invocation in the operational docs
against the committed ground truth:

  `issue ...` invocations  ->  docs/cli-surface.json (IM-245), the
                               deterministic record of every verb's flag
                               registrations -- never a restated flag list
  `make ...` invocations   ->  the Makefile's own target list

Scope, declared per the IM-283 acceptance criteria:

  IN   docs/plan.md, CLAUDE.md -- the files a session acts on
  OUT  docs/releases.md, CHANGELOG.md -- history by construction; checking
       them would fail every past release entry

Prescription vs narration: only inline code spans (`...`) and fenced
code-block lines are read -- prose that happens to start with "make" or
"issue" is never a candidate -- and a candidate on a line carrying an
explicit historical qualifier (`was`, `through vX`, `retired`, `removed`,
`no longer`, `superseded`, `formerly`, `deprecated`, `deleted`, `renamed`)
passes as a deliberate historical mention, the same carve-out
`surface.self-naming` makes for the CLI's own text.

What the check deliberately does NOT catch, recorded on IM-283: a correct
command named in the wrong ceremony step (the v3.5.0 class). That is a
claim about a sequence, not about a flag, and is not mechanically
checkable from prose; the tooling refusing to be bypassed is the fix for
that half, tracked tool-side.

Resolution rules (each shape was measured in the adoption sweep):

  - compound spans split on `&&` / `||` / `;` / `|`; each piece is a
    candidate of its own
  - a token containing `<` or `*` is a placeholder or glob (`issue
    <verb>`, `make coverage-*`) -- a placeholder verb skips the span, a
    placeholder operand is ignored
  - slash-joined alternatives expand (`issue start/complete/cancel`,
    `--add-dep/--remove-dep`): every alternative must resolve
  - `--flag=value` checks the flag spelling only; non-`--` tokens are
    operands and are never checked
  - a flag after a slash-joined verb list resolves against the union of
    the listed verbs' flag sets
  - `issue --help` / `issue --version` are dispatcher-level and pass
  - `make` tokens with `=` (VERSION=...) or a leading `-` are ignored;
    the remaining words must each be Makefile targets
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "docs" / "cli-surface.json"
MAKEFILE_PATH = REPO_ROOT / "Makefile"

# The operational docs a session acts on. docs/releases.md and CHANGELOG.md
# are excluded as history by construction (IM-283 scope decision).
DOC_PATHS = ("docs/plan.md", "CLAUDE.md")

DISPATCHER_FLAGS = {"--help", "-h", "--version"}

HISTORICAL_QUALIFIER = re.compile(
    r"\b(was|were|through v\d|retired|removed|former(ly)?|no longer|"
    r"superseded|deprecated|deleted|renamed)\b",
    re.IGNORECASE,
)

_INLINE_SPAN = re.compile(r"`([^`]+)`")
_COMMAND = re.compile(r"^(issue|make)(?:\s+(.*))?$")
_FENCE = re.compile(r"^\s*(```|~~~)")


class LintError(Exception):
    """The lint could not run (missing/corrupt manifest or Makefile)."""


class Finding:
    def __init__(self, doc, line_no, span, detail):
        self.doc = doc
        self.line_no = line_no
        self.span = span
        self.detail = detail

    def as_dict(self):
        return {
            "doc": self.doc,
            "line": self.line_no,
            "span": self.span,
            "detail": self.detail,
        }

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"Finding({self.doc}:{self.line_no}: {self.detail})"


def load_verb_table(manifest_path=MANIFEST_PATH):
    """(tier, verb) -> set of accepted flag spellings, from the manifest."""
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise LintError(f"cannot read surface manifest {manifest_path}: {e}")
    table = {}
    for entry in manifest.get("verbs", []):
        names = set()
        for flag in entry.get("flags", []):
            names.update(flag.get("names", []))
        table[(entry["tier"], entry["verb"])] = names
    if not table:
        raise LintError(f"surface manifest {manifest_path} lists no verbs")
    return table


def load_make_targets(makefile_path=MAKEFILE_PATH):
    """The set of target names the Makefile defines."""
    try:
        text = makefile_path.read_text(encoding="utf-8")
    except OSError as e:
        raise LintError(f"cannot read {makefile_path}: {e}")
    targets = set()
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z0-9_.%-]+(?:\s+[A-Za-z0-9_.%-]+)*)\s*:(?!=)",
                     line)
        if m:
            targets.update(m.group(1).split())
        if line.startswith(".PHONY:"):
            targets.update(line.split(":", 1)[1].split())
    targets.discard(".PHONY")
    if not targets:
        raise LintError(f"{makefile_path} defines no targets")
    return targets


def _resolve_verb_flags(parts, verb_table):
    """Flag-name union for one slash-joined verb token, or None if any
    alternative is not a verb on the surface."""
    union = set()
    for part in parts:
        matches = [flags for (tier, verb), flags in verb_table.items()
                   if verb == part and tier not in ("admin", "history")]
        if not matches:
            return None
        for flags in matches:
            union.update(flags)
    return union


def _clean_token(token):
    return token.strip("[]()").rstrip(".,;:)")


def _is_placeholder(token):
    return "<" in token or "*" in token


def _check_issue_span(span, verb_table):
    """Yield detail strings for one `issue ...` candidate."""
    tokens = span.split()[1:]  # drop the leading `issue`
    if not tokens:
        return
    head = _clean_token(tokens[0])
    if _is_placeholder(head):
        return
    if head.startswith("-"):
        # dispatcher-level flag invocation, e.g. `issue --help`
        for token in tokens:
            token = _clean_token(token).split("=")[0]
            if token.startswith("-") and token not in DISPATCHER_FLAGS:
                yield f"issue (dispatcher): unknown flag {token}"
        return

    rest = tokens[1:]
    if head in ("admin", "history"):
        if not rest:
            return
        sub = _clean_token(rest[0])
        rest = rest[1:]
        if _is_placeholder(sub):
            return
        flag_union = set()
        for part in sub.split("/"):
            flags = verb_table.get((head, part))
            if flags is None:
                yield f"issue {head} {part}: unknown {head} subverb"
                return
            flag_union.update(flags)
        label = f"issue {head} {sub}"
    else:
        flag_union = _resolve_verb_flags(head.split("/"), verb_table)
        if flag_union is None:
            yield f"issue {head}: unknown verb"
            return
        label = f"issue {head}"

    for token in rest:
        token = _clean_token(token)
        if _is_placeholder(token) or not token.startswith("--"):
            continue
        for part in token.split("/"):
            part = _clean_token(part).split("=")[0]
            if not part.startswith("--"):
                continue
            if part not in flag_union:
                yield f"{label}: unknown flag {part}"


def _check_make_span(span, make_targets):
    """Yield detail strings for one `make ...` candidate."""
    for token in span.split()[1:]:
        token = _clean_token(token)
        if (not token or token.startswith("-") or "=" in token
                or _is_placeholder(token) or "$" in token):
            continue
        if token not in make_targets:
            yield f"make {token}: no such Makefile target"


def _candidates(text):
    """Yield (line_no, line, candidate_span) for every prescribed-command
    candidate: inline code spans and fenced code-block lines, split on
    shell connectors, filtered to `issue ...` / `make ...` heads."""
    in_fence = False
    for line_no, line in enumerate(text.splitlines(), 1):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            spans = [line.split("#", 1)[0].strip()]
        else:
            spans = [s.strip() for s in _INLINE_SPAN.findall(line)]
        for span in spans:
            for piece in re.split(r"\s*(?:&&|\|\||;|\|)\s*", span):
                piece = piece.strip()
                if _COMMAND.match(piece):
                    yield line_no, line, piece


def lint_text(doc_name, text, verb_table, make_targets):
    """Lint one document's text; returns a list of Findings."""
    findings = []
    for line_no, line, span in _candidates(text):
        if HISTORICAL_QUALIFIER.search(line):
            continue
        tool = span.split()[0]
        if tool == "issue":
            details = _check_issue_span(span, verb_table)
        else:
            details = _check_make_span(span, make_targets)
        for detail in details:
            findings.append(Finding(doc_name, line_no, span, detail))
    return findings


def lint_docs(repo_root=REPO_ROOT):
    """Lint the in-scope operational docs; returns a list of Findings."""
    verb_table = load_verb_table(repo_root / "docs" / "cli-surface.json")
    make_targets = load_make_targets(repo_root / "Makefile")
    findings = []
    for rel in DOC_PATHS:
        path = repo_root / rel
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            raise LintError(f"cannot read {path}: {e}")
        findings.extend(lint_text(rel, text, verb_table, make_targets))
    return findings
