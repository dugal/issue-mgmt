"""editcore -- the reusable edit-issue data path.

Relocated verbatim from the flat ``scripts/edit-issue`` binary when that
binary became a v3.0.0 tombstone stub (WP7). WP4's consolidated ``issue
edit`` / ``issue note`` / ``issue dep`` verbs previously reached this logic
by importing the flat script at runtime via ``lib/cli/_flatload.py``; once
the flat script is a two-line redirect stub that import would load a body
with no logic. So the whole reusable data path -- argv interpretation
(``interpret_section_actions``), the lock+snapshot+atomic-write+commit
lifecycle (``apply_edits``), the ``--if-unchanged-since`` guard, the
``--claim-generation`` fencing, the dep-edge engine, the
authoring cycle guard, and the escaping-header refusal -- lives here
in ``lib/`` where the verb modules import it directly.

This is a pure relocation: the functions are byte-for-byte the flat binary's,
minus the CLI shell (``parse_args`` / ``show_help`` / ``main`` / the
sibling-subprocess ``run_reindex``, which the consolidated verbs replace with
the in-process ``reindex.reindex_after_mutation`` seam). Exit codes and the
``--json`` shape the verb modules produce are unchanged.
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
from pathlib import Path

import concurrency  # noqa: E402  (lib/ is on sys.path via the entrypoint)
import deps  # noqa: E402
import errors  # noqa: E402
import frontmatter  # noqa: E402
import graph  # noqa: E402
import idresolve  # noqa: E402
import lease  # noqa: E402

GUARDED_FIELDS = {
    "status": "use `issue transition` to change status",
    "id": "immutable (rename via migration tool)",
    "created": "immutable",
}

# Aliased to the single shared grammar in lib/idresolve.py.
_ID_RE = idresolve.ID_RE


class GuardedFieldError(Exception):
    def __init__(self, field, reason):
        super().__init__(f"field '{field}' is guarded: {reason}")
        self.field = field
        self.reason = reason


class OperandInputError(Exception):
    """Raised when reading body input from a file or stdin fails.

    Maps to CLI exit 1 (precondition failed). Used for --append-file
    / --prepend-file / --replace-file (missing path, unreadable file,
    not valid utf-8) including the PATH=='-' stdin variant.
    """


class _OrderedFlagAction(argparse.Action):
    """Record every occurrence of section-related flags in argv order.

    All flag occurrences land in a single shared list
    `ns.ordered_section_flags` of `(option_string, values)` tuples. The
    post-argparse interpreter (`interpret_section_actions`) walks the
    list to fold qualifier flags into the most recent anchor (e.g.
    `--append TEXT` qualifies the preceding `--section NAME`).

    `nargs=0` flags (currently only `--at-end`) record values=[].
    """

    def __call__(self, parser, ns, values, option_string=None):
        lst = getattr(ns, "ordered_section_flags", None)
        if lst is None:
            lst = []
            setattr(ns, "ordered_section_flags", lst)
        lst.append((option_string, values))


# Action tuples (small, immutable, easy to assert on in tests):
#   ("edit", section_name, op, text)
#   ("add", section_name, body, position_kwargs)   # position_kwargs may be {}
#   ("rename", old_name, new_name)
#   ("remove", section_name)
#   ("move", section_name, position_kwargs)


_ANCHOR_FLAGS = {
    "--section",
    "--add-section",
    "--rename-section",
    "--remove-section",
    "--move-section",
    "--add-dep",
    "--remove-dep",
}
_BODY_OP_FLAGS = {"--append", "--prepend", "--replace"}
_BODY_FILE_FLAGS = {"--append-file", "--prepend-file", "--replace-file"}
_POSITION_FLAGS = {"--after", "--before", "--to-position", "--at-end"}
_DEP_FLAGS = {"--add-dep", "--remove-dep"}
_DEP_QUALIFIER_FLAGS = {"--kind", "--dep-note"}

# IM-241: retired spelling -> canonical, normalized at the top of the walk
# so every downstream check AND every error message names the current form
# (surface.self-naming). `--dep-type` was `edit`'s spelling for the
# dependency kind through v3.3.x; `--kind` is now canonical on both `edit`
# and `dep`, which spelled the same operand `--type`.
_RETIRED_FLAG_SPELLINGS = {"--dep-type": "--kind"}


def _check_body_nests(text, op_label, anchor_label):
    """Refuse body text whose headers would escape the target section.

    Sections are ##-delimited, so a `#` or `## ` line in an incoming
    body reparses as a NEW top-level section and silently fragments the
    file. Raises ValueError (CLI exit 2, the argv/usage
    contract) naming the offending line; runs at parse time, before any
    lock is acquired.
    """
    offending = frontmatter.find_escaping_header(text)
    if offending is not None:
        raise ValueError(
            f"{anchor_label} {op_label}: body contains a markdown "
            f"header that would escape the level-2 section and fragment "
            f"the file: {offending!r} -- demote it to nest under the "
            "section (e.g. '## heading' -> '### heading') and retry"
        )


def interpret_section_actions(ordered_flags):
    """Fold a list of `(flag, value)` pairs into typed section actions.

    Returns `(section_actions, dep_actions)`. Section actions mutate the
    body; dep actions mutate the `depends_on:` frontmatter block. Both are
    accumulated in argv order.

    Dep action tuples:
      ("add-dep", target, kind, note_or_None)
      ("remove-dep", target, kind_or_None)

    Raises ValueError with a stderr-ready message for any structural
    error (qualifier without anchor, duplicate qualifier on an anchor,
    missing required qualifier, multiple positions on one anchor, a bad
    dependency kind or malformed target).
    Raises OperandInputError if an `--*-file` PATH cannot be read
    (forwarded directly so the caller maps it to exit 1).
    """
    actions = []
    dep_actions = []
    anchor = None  # dict describing the in-flight anchor
    stdin_consumed_by = None  # flag that already read PATH=='-'

    def _close_anchor():
        nonlocal anchor
        if anchor is None:
            return
        kind = anchor["kind"]
        if kind == "add-dep":
            deps.validate_kind(anchor["dep_kind"])
            deps.validate_target(anchor["name"])
            dep_actions.append(
                ("add-dep", anchor["name"], anchor["dep_kind"],
                 anchor.get("note"))
            )
            anchor = None
            return
        if kind == "remove-dep":
            deps.validate_target(anchor["name"])
            if anchor.get("dep_kind") is not None:
                deps.validate_kind(anchor["dep_kind"])
            dep_actions.append(
                ("remove-dep", anchor["name"], anchor.get("dep_kind"))
            )
            anchor = None
            return
        if kind == "section":
            op = anchor.get("op")
            if op is None:
                raise ValueError(
                    f"--section {anchor['name']!r} requires one of "
                    "--append/--prepend/--replace TEXT (or "
                    "--append-file/--prepend-file/--replace-file PATH)"
                )
            _check_body_nests(anchor["text"], f"--{op}",
                              f"--section {anchor['name']!r}")
            actions.append(("edit", anchor["name"], op, anchor["text"]))
        elif kind == "add":
            position = _collect_position(anchor, require=False)
            _check_body_nests(anchor.get("body", ""), "--body",
                              f"--add-section {anchor['name']!r}")
            actions.append(
                ("add", anchor["name"], anchor.get("body", ""), position)
            )
        elif kind == "move":
            position = _collect_position(anchor, require=True)
            actions.append(("move", anchor["name"], position))
        anchor = None

    def _require_anchor_for(flag, *allowed_kinds):
        if anchor is None or anchor["kind"] not in allowed_kinds:
            def _label(k):
                if k == "section":
                    return "--section"
                if k in ("add-dep", "remove-dep"):
                    return f"--{k}"
                return f"--{k}-section"
            allowed = " or ".join(_label(k) for k in allowed_kinds)
            raise ValueError(
                f"{flag} requires a preceding {allowed}"
            )

    for flag, value in ordered_flags:
        flag = _RETIRED_FLAG_SPELLINGS.get(flag, flag)
        if flag in _ANCHOR_FLAGS:
            _close_anchor()
            if flag == "--section":
                anchor = {"kind": "section", "name": value}
            elif flag == "--add-section":
                anchor = {"kind": "add", "name": value}
            elif flag == "--rename-section":
                old, new = value
                actions.append(("rename", old, new))
            elif flag == "--remove-section":
                actions.append(("remove", value))
            elif flag == "--move-section":
                anchor = {"kind": "move", "name": value}
            elif flag == "--add-dep":
                kind_str, sep, target = value.partition(":")
                if not sep or not kind_str or not target:
                    raise ValueError(
                        "--add-dep expects KIND:TARGET (e.g. "
                        "blocks:RM-107)"
                    )
                anchor = {
                    "kind": "add-dep",
                    "name": target,
                    "dep_kind": kind_str,
                }
            elif flag == "--remove-dep":
                anchor = {
                    "kind": "remove-dep",
                    "name": value,
                    "dep_kind": None,
                }
            continue

        if flag in _DEP_QUALIFIER_FLAGS:
            if flag == "--kind":
                _require_anchor_for(flag, "remove-dep")
                if anchor.get("dep_kind") is not None:
                    raise ValueError(
                        "--kind already set for this --remove-dep"
                    )
                anchor["dep_kind"] = value
            elif flag == "--dep-note":
                _require_anchor_for(flag, "add-dep")
                if "note" in anchor:
                    raise ValueError(
                        "--dep-note already set for this --add-dep"
                    )
                anchor["note"] = value
            continue

        if flag in _BODY_OP_FLAGS:
            _require_anchor_for(flag, "section")
            if anchor.get("op") is not None:
                raise ValueError(
                    f"--section {anchor['name']!r} already has a body op; "
                    "specify exactly one of --append/--prepend/--replace "
                    "(or --append-file/--prepend-file/--replace-file)"
                )
            anchor["op"] = flag[2:]
            anchor["text"] = value
            continue

        if flag in _BODY_FILE_FLAGS:
            _require_anchor_for(flag, "section")
            if anchor.get("op") is not None:
                raise ValueError(
                    f"--section {anchor['name']!r} already has a body op; "
                    "specify exactly one of --append/--prepend/--replace "
                    "(or --append-file/--prepend-file/--replace-file)"
                )
            if value == "-":
                if stdin_consumed_by is not None:
                    raise ValueError(
                        f"{flag} -: stdin already consumed by "
                        f"{stdin_consumed_by} (PATH=='-' may appear "
                        "in at most one --*-file flag per invocation)"
                    )
                stdin_consumed_by = flag
            # Read PATH (or stdin) NOW, before any lock is acquired.
            # OperandInputError -> CLI exit 1.
            anchor["op"] = flag[2:].replace("-file", "")
            anchor["text"] = _read_op_file(value)
            continue

        if flag == "--body":
            _require_anchor_for(flag, "add")
            if "body" in anchor:
                raise ValueError(
                    f"--add-section {anchor['name']!r} already has --body"
                )
            anchor["body"] = value
            continue

        if flag in _POSITION_FLAGS:
            _require_anchor_for(flag, "add", "move")
            _check_no_existing_position(anchor, flag)
            if flag == "--at-end":
                anchor["at_end"] = True
            elif flag == "--after":
                anchor["after"] = value
            elif flag == "--before":
                anchor["before"] = value
            elif flag == "--to-position":
                anchor["to_position"] = value
            continue

        raise ValueError(f"unknown ordered flag: {flag}")

    _close_anchor()
    return actions, dep_actions


def _check_no_existing_position(anchor, new_flag):
    existing = [
        k for k in ("after", "before", "to_position", "at_end")
        if k in anchor
    ]
    if existing:
        present = ", ".join(f"--{e.replace('_', '-')}" for e in existing)
        raise ValueError(
            f"{new_flag} on --{anchor['kind']}-section {anchor['name']!r}: "
            f"position already set by {present}"
        )


def _collect_position(anchor, *, require):
    position = {}
    if "after" in anchor:
        position["after"] = anchor["after"]
    if "before" in anchor:
        position["before"] = anchor["before"]
    if "to_position" in anchor:
        position["to_position"] = anchor["to_position"]
    # --at-end is the default, represented as an empty dict
    if not position and require:
        raise ValueError(
            f"--{anchor['kind']}-section {anchor['name']!r} requires "
            "one of --after/--before/--to-position"
        )
    return position


def find_project_anchor():
    """Walk up from cwd looking for .issues/. Falls back to workspace root.

    Mirrors the bash scripts' find_project_anchor + workspace_root logic.
    """
    cwd = Path.cwd().resolve()
    for d in [cwd, *cwd.parents]:
        if (d / ".issues").is_dir():
            return d / ".issues"
    workspace = os.environ.get("ISSUE_MGMT_ROOT")
    if workspace:
        return Path(workspace) / ".issues"
    parent = cwd.parent
    if (parent / ".issues").is_dir():
        return parent / ".issues"
    return cwd / ".issues"


def resolve_store_root(anchor):
    """Return Path of the store root, or None when legacy mode applies."""
    env = os.environ.get("ISSUE_STORE_ROOT")
    if env:
        return Path(env)
    sentinel = anchor / ".migrated-to-store"
    if sentinel.is_file():
        return Path(sentinel.read_text(encoding="utf-8").strip())
    return None


def resolve_issue_paths(issue_id, *, emit_note=None):
    """Return (issue_file, lock_path, store_root_or_None, anchor).

    Thin wrapper over ``idresolve.resolve_issue_paths``: the
    canonicalization and path build live there, so ``IM-7`` / ``im-7`` /
    ``IM-0007`` all resolve the on-disk file and the returned lock path is
    always built from the canonical stem (the P2 fix). The 4-tuple return
    shape is preserved for the earlier callers (``depcore``, tests); use
    ``idresolve.resolve_issue_paths`` directly when the canonical id is
    needed. Raises ValueError on a malformed id (unchanged).
    """
    anchor = find_project_anchor()
    store_root = resolve_store_root(anchor)
    result = idresolve.resolve_issue_paths(
        issue_id, store_root, anchor, emit_note=emit_note
    )
    return result.as_tuple()


def parse_set_args(sets):
    """Parse a list of '--set FIELD=VALUE' specs into [(field, value), ...]."""
    out = []
    for spec in sets:
        if "=" not in spec:
            raise ValueError(f"--set expects FIELD=VALUE (got {spec!r})")
        field, _, value = spec.partition("=")
        field = field.strip()
        if not field:
            raise ValueError(f"--set: empty field name (got {spec!r})")
        out.append((field, value))
    return out


def _read_op_file(path_str):
    """Read body operand from PATH; return text.

    PATH=='-' reads stdin to EOF, matching `new-issue --from-file -`
    and the broader POSIX convention used by cat, kubectl,
    git apply, gpg, openssl, etc. Otherwise the file at PATH is read
    verbatim relative to cwd: no trim, no normalization, empty file
    -> empty text. Raises OperandInputError on missing file,
    unreadable file, or not-valid-utf-8 content.

    Called by interpret_section_actions at parse time, before any
    per-issue lock is acquired, so I/O errors (missing file, slow
    stdin) fail fast and never block a lock-holder.
    """
    if not path_str:
        raise OperandInputError(
            "empty path for body-input flag (expected a path or '-')"
        )
    if path_str == "-":
        try:
            data = sys.stdin.buffer.read()
        except OSError as e:
            raise OperandInputError(f"cannot read stdin: {e}")
        source = "<stdin>"
    else:
        path = Path(path_str)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            raise OperandInputError(f"file not found: {path_str}")
        except IsADirectoryError:
            raise OperandInputError(
                f"path is a directory, not a file: {path_str}"
            )
        except PermissionError:
            raise OperandInputError(f"file not readable: {path_str}")
        except OSError as e:
            raise OperandInputError(f"cannot read {path_str}: {e}")
        source = path_str
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise OperandInputError(f"input not valid utf-8: {source}: {e}")


def _section_body(body_text, section_name):
    """Return the body text of section_name from body_text.

    Returns None if the section is absent. Returns the substring
    between the heading line and the next level-2 heading (or EOF),
    with the leading newline preserved so byte-equality with
    `find_section`'s output is straightforward.
    """
    bounds = frontmatter.find_section(body_text, section_name)
    if bounds is None:
        return None
    _heading_idx, body_start, body_end = bounds
    lines = body_text.splitlines(keepends=True)
    return "".join(lines[body_start:body_end])


def _check_section_unchanged_since(
    store_root, issue_file, current_body_text, section_name, ref
):
    """Raise ConcurrentEdit if the named section's body at <ref> differs
    from its body in the current file (already read under the lock).

    Used by --if-unchanged-since for caller-side stale-body
    detection on --replace operations.

    Error classification:
      - ValueError ("--if-unchanged-since: cannot resolve <ref>...")
        propagates to the CLI as exit 2.
      - FileNotFoundError ("--if-unchanged-since: file did not exist
        at <ref>...") propagates to the CLI as exit 1.
      - ConcurrentEdit ("section <name> changed since <ref>")
        propagates to the CLI as exit 3.
    """
    rel_path = issue_file.resolve().relative_to(store_root.resolve())
    try:
        raw = concurrency.git_show(store_root, ref, rel_path)
    except ValueError as e:
        raise ValueError(f"--if-unchanged-since: {e}")
    except FileNotFoundError as e:
        raise FileNotFoundError(f"--if-unchanged-since: {e}")
    try:
        text_at_ref = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(
            f"--if-unchanged-since: file at {ref} is not valid utf-8: {e}"
        )
    try:
        _fm_at_ref, body_at_ref = frontmatter.parse_issue(
            text_at_ref, path=f"{issue_file}@{ref}"
        )
    except ValueError as e:
        raise ValueError(f"--if-unchanged-since: {e}")
    section_at_ref = _section_body(body_at_ref, section_name)
    section_now = _section_body(current_body_text, section_name)
    if section_at_ref is None and section_now is not None:
        raise errors.ConcurrentEdit(
            f"section {section_name!r} did not exist at {ref} but "
            "exists now -- section was added concurrently; re-read and "
            "rebuild the replacement body",
            resource=str(issue_file),
            section=section_name,
            caller_sha=ref,
        )
    if section_at_ref is None and section_now is None:
        # Both absent -- edit_section will fail downstream with "section
        # not found" (exit 1). Don't preempt that error here.
        return
    if section_at_ref != section_now:
        raise errors.ConcurrentEdit(
            f"section {section_name!r} changed since {ref} -- "
            "re-read and rebuild the replacement body",
            resource=str(issue_file),
            section=section_name,
            caller_sha=ref,
        )


def _check_body_unchanged_since(store_root, issue_file, current_body_text, ref):
    """Raise ConcurrentEdit if the whole BODY at <ref> differs from the
    body in the current file (already read under the lock).

    The whole-body sibling of _check_section_unchanged_since, used by
    --if-unchanged-since composed with --replace-body-file.
    Only the body is compared: frontmatter drift (lease renews,
    `updated:` stamps, dep edits) must not fail a body replace.

    Error classification matches the section guard: ValueError -> exit 2,
    FileNotFoundError -> exit 1, ConcurrentEdit -> exit 3.
    """
    rel_path = issue_file.resolve().relative_to(store_root.resolve())
    try:
        raw = concurrency.git_show(store_root, ref, rel_path)
    except ValueError as e:
        raise ValueError(f"--if-unchanged-since: {e}")
    except FileNotFoundError as e:
        raise FileNotFoundError(f"--if-unchanged-since: {e}")
    try:
        text_at_ref = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(
            f"--if-unchanged-since: file at {ref} is not valid utf-8: {e}"
        )
    try:
        _fm_at_ref, body_at_ref = frontmatter.parse_issue(
            text_at_ref, path=f"{issue_file}@{ref}"
        )
    except ValueError as e:
        raise ValueError(f"--if-unchanged-since: {e}")
    if body_at_ref != current_body_text:
        raise errors.ConcurrentEdit(
            f"body changed since {ref} -- re-read and rebuild the "
            "replacement body",
            resource=str(issue_file),
            caller_sha=ref,
        )


def _apply_one_section_action(body_text, action):
    """Dispatch one section action against body_text and return new body.

    Action shapes:
      ("edit", name, op, text)
      ("add", name, body, position_kwargs)   # position_kwargs may be {}
      ("rename", old, new)
      ("remove", name)
      ("move", name, position_kwargs)
    """
    kind = action[0]
    if kind == "edit":
        _, name, op, text = action
        return frontmatter.edit_section(body_text, name, op, text)
    if kind == "add":
        _, name, body, position = action
        return frontmatter.add_section(body_text, name, body, **position)
    if kind == "rename":
        _, old, new = action
        return frontmatter.rename_section(body_text, old, new)
    if kind == "remove":
        _, name = action
        return frontmatter.remove_section(body_text, name)
    if kind == "move":
        _, name, position = action
        return frontmatter.move_section(body_text, name, **position)
    raise ValueError(f"unknown section action: {action!r}")


def _summarize_section_action(action):
    """Render a JSON-friendly summary of one section action."""
    kind = action[0]
    if kind == "edit":
        _, name, op, _text = action
        return {"kind": "edit", "section": name, "op": op}
    if kind == "add":
        _, name, _body, position = action
        return {"kind": "add", "section": name, "position": position or {"at_end": True}}
    if kind == "rename":
        _, old, new = action
        return {"kind": "rename", "old": old, "new": new}
    if kind == "remove":
        _, name = action
        return {"kind": "remove", "section": name}
    if kind == "move":
        _, name, position = action
        return {"kind": "move", "section": name, "position": position}
    raise ValueError(f"unknown section action: {action!r}")


def _summarize_dep_action(action):
    """Render a JSON-friendly summary of one dependency-edge action."""
    kind = action[0]
    if kind == "add-dep":
        _, target, dep_kind, _note = action
        return {"kind": "add-dep", "to": target, "type": dep_kind}
    if kind == "remove-dep":
        _, target, dep_kind = action
        return {"kind": "remove-dep", "to": target, "type": dep_kind}
    raise ValueError(f"unknown dep action: {action!r}")


def _apply_dep_actions(fm_lines, dep_actions, known_prefixes=None):
    """Apply the ordered dep actions to the `depends_on:` block in place.

    Reads the current edge list once, folds each add/remove over it, and
    writes the reduced authoritative list back. Removing the last edge
    yields `depends_on: []` (never a deleted key), so the list stays
    authoritative rather than flipping to unmanaged. Returns the list of
    JSON summaries.

    Each edge target is canonicalized through
    ``idresolve.canonicalize_target``: case-folded prefix +
    integer-normalized number (`blocks:im-84` -> `to: IM-084`). The target
    need not exist yet (write-now / resolve-later), so canonicalization is
    purely syntactic; a target that is not a valid id shape is written
    verbatim (deps.validate_target already rejected malformed shapes at
    parse time).
    """
    def _canon(target):
        return idresolve.canonicalize_target(target, known_prefixes) or target

    edges = deps.get_deps(fm_lines)
    summaries = []
    for action in dep_actions:
        kind = action[0]
        if kind == "add-dep":
            _, target, dep_kind, note = action
            target = _canon(target)
            edge = {"to": target, "type": dep_kind}
            if note:
                edge["note"] = note
            # add_dep dedups on (to, type); re-add is a no-op. Handle the
            # note qualifier here so a re-add with a note still no-ops.
            existing = [
                e for e in (edges or [])
                if e.get("to") == target and e.get("type") == dep_kind
            ]
            if not existing:
                edges = list(edges or [])
                edges.append(edge)
            action = ("add-dep", target, dep_kind, note)
        elif kind == "remove-dep":
            _, target, dep_kind = action
            target = _canon(target)
            edges = deps.remove_dep(edges, target, dep_kind)
            action = ("remove-dep", target, dep_kind)
        summaries.append(_summarize_dep_action(action))
    deps.set_deps(fm_lines, edges or [])
    return summaries


def _check_generation_guard(fm_lines, caller_generation):
    """Opt-in caller-side fencing guard.

    No-op when caller_generation is None. Mismatch raises
    StaleViewError (exit 5). A matching generation > 0 whose lease is
    expired or released raises LeaseExpiredError (exit 7) -- the
    caller's own era ended, so the guarded edit must not proceed
    unguarded.
    """
    if caller_generation is None:
        return
    current = lease.get_claim_generation(fm_lines)
    if current != caller_generation:
        raise errors.StaleViewError(
            "claim generation drifted since caller's read",
            current_generation=current,
            caller_generation=caller_generation,
        )
    if current > 0 and not lease.lease_is_active(fm_lines):
        claim = lease.get_claim(fm_lines) or {}
        raise errors.LeaseExpiredError(
            "caller's generation matches but the lease is expired "
            "or released",
            actor=claim.get("assignee"),
            lease_until=lease.get_lease_until(fm_lines),
        )


def apply_edits(
    issue_file,
    lock_path,
    sets,
    section_actions,
    lock_timeout,
    *,
    store_root=None,
    commit_paths=None,
    commit_message=None,
    reindex_fn=None,
    if_unchanged_since=None,
    if_unchanged_since_section=None,
    caller_generation=None,
    dep_actions=None,
    replace_body=None,
):
    """Lock + read + mutate + verify + write. Returns a summary dict.

    `section_actions` is an ordered list of action tuples (see
    `_apply_one_section_action`). All actions land under a single
    lock + snapshot + atomic write + (optionally) store_mutation
    commit.

    When `store_root`, `commit_paths`, and `commit_message` are all
    provided, the verify+atomic-write (and the optional reindex_fn,
    invoked with suppress_commit=True so it does not open its own
    commit) are wrapped in `concurrency.store_mutation`. The edit's
    file change and the reindex's INDEX deltas land in one commit
    named by `commit_message`.

    When `if_unchanged_since` is set, after the per-issue
    lock is held and the file has been read, fetch the
    `if_unchanged_since_section` body at that SHA in the store repo
    and compare to the current section body. Mismatch raises
    ConcurrentEdit so caller-side staleness surfaces as exit 3. With
    `if_unchanged_since_section=None` the whole body is compared
    instead (the --replace-body-file composition).

    When `replace_body` is set, the entire body below the
    frontmatter fence is replaced with that text; `fm_lines` pass
    through untouched (bar the `updated:` stamp). The caller enforces
    exclusivity with section_actions; the body is opaque -- no
    escaping-header check applies.

    When `caller_generation` is set, the file's top-level
    `claim_generation` is checked under the lock: mismatch raises
    StaleViewError (exit 5); a matching generation > 0 whose lease has
    expired or been released raises LeaseExpiredError (exit 7).
    """
    today = datetime.date.today().isoformat()
    with concurrency.flock_acquire(
        lock_path, timeout=lock_timeout, resource=str(issue_file)
    ):
        snapshot = concurrency.capture_state(issue_file)
        if snapshot.is_absent():
            raise FileNotFoundError(str(issue_file))

        fm_lines, body_text = frontmatter.read_issue(issue_file)

        _check_generation_guard(fm_lines, caller_generation)

        if if_unchanged_since is not None:
            if if_unchanged_since_section is not None:
                _check_section_unchanged_since(
                    store_root,
                    issue_file,
                    body_text,
                    if_unchanged_since_section,
                    if_unchanged_since,
                )
            else:
                _check_body_unchanged_since(
                    store_root, issue_file, body_text, if_unchanged_since
                )

        summary = {"field_changes": [], "section_actions": [],
                   "dep_actions": [], "body_actions": []}

        if dep_actions:
            # Canonicalize dep targets against the store's real prefixes.
            # An empty set still case-folds + int-normalizes,
            # so legacy mode (store_root is None) is fine.
            known_prefixes = (
                idresolve._known_prefixes(store_root, None)
                if store_root is not None else set()
            )
            summary["dep_actions"] = _apply_dep_actions(
                fm_lines, dep_actions, known_prefixes
            )
            # Authoring-time blocks-cycle guard. A `blocks` cycle is
            # a real contradiction in the ordering graph; refuse it before
            # the markdown write commits so the source of truth can never
            # hold one. Pre-validate the final blocks-successor set here --
            # before any write or commit -- and refuse with a structured
            # precondition-failed/dependency-cycle error. Store-mode only
            # (needs sibling issues to resolve). Only a `blocks` add can
            # close a cycle, so the whole-store resolution is skipped
            # otherwise.
            if store_root is not None and any(
                a[0] == "add-dep" and a[2] == "blocks" for a in dep_actions
            ):
                issue_id = frontmatter.get_field(fm_lines, "id") \
                    or Path(issue_file).stem
                blocks_targets = [
                    e["to"] for e in (deps.get_deps(fm_lines) or [])
                    if e.get("type") == "blocks" and e.get("to")
                ]
                graph.detect_blocks_cycle(
                    store_root, issue_id, blocks_targets
                )

        for field, value in sets:
            if field in GUARDED_FIELDS:
                raise GuardedFieldError(field, GUARDED_FIELDS[field])
            old_value = frontmatter.get_field(fm_lines, field)
            frontmatter.set_field(fm_lines, field, value)
            summary["field_changes"].append(
                {"field": field, "old": old_value, "new": value}
            )

        if replace_body is not None:
            body_text = replace_body
            summary["body_actions"].append(
                {"kind": "replace-body",
                 "bytes": len(replace_body.encode("utf-8"))}
            )

        for action in section_actions:
            body_text = _apply_one_section_action(body_text, action)
            summary["section_actions"].append(
                _summarize_section_action(action)
            )

        if (summary["field_changes"] or summary["section_actions"]
                or summary["dep_actions"] or summary["body_actions"]):
            frontmatter.set_field(fm_lines, "updated", today)

        new_text = frontmatter.serialize_issue(fm_lines, body_text)

        do_autocommit = (
            store_root is not None
            and commit_paths is not None
            and commit_message is not None
        )
        if do_autocommit:
            with concurrency.store_mutation(
                store_root, commit_paths, commit_message
            ):
                # Hand-edit detection: someone (or some non-locking tool)
                # touched the file between our snapshot and now.
                concurrency.verify_unchanged(
                    issue_file, snapshot, resource=str(issue_file)
                )
                concurrency.atomic_write(issue_file, new_text)
                if reindex_fn is not None:
                    reindex_fn(suppress_commit=True)
        else:
            concurrency.verify_unchanged(
                issue_file, snapshot, resource=str(issue_file)
            )
            concurrency.atomic_write(issue_file, new_text)
            if reindex_fn is not None:
                reindex_fn(suppress_commit=False)

    return summary
