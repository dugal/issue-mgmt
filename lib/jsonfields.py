"""Shared `--json <fields>` field-selection projection.

gh-style field selection for the read verbs: bare ``--json`` emits the full
documented shape (unchanged), while ``--json id,title,status`` projects each
per-object to just those top-level fields. The projection is a **flat** pick
of top-level keys -- no nested paths (`groups.status`) in this iteration --
and strict: an unknown field name is an argument/usage error (exit 2), never
silently dropped. Field order in the output follows the order the caller
listed them. stdlib only; no `--jq` passthrough (callers pipe to jq).

The ``--json`` flag for a field-selecting verb is registered with
``common.add_json_field_flag`` (``nargs="?"``, ``const=True``), so
``args.json_mode`` is one of:

    None       flag absent
    True       bare ``--json``            -> full documented shape
    "id,title" ``--json <fields>``        -> projection to those fields
    ""         ``--json ''``              -> usage error (empty field list)

Every field-selecting verb declares a ``VALID_JSON_FIELDS`` tuple naming the
top-level keys of its per-object shape; ``parse_fields`` validates the caller
request against it. For a wrapper-object verb (``index`` groups, ``ready``,
the ``history`` subverbs) the projection applies to the per-object list the
wrapper carries; the wrapper metadata is left intact (see each verb module).
"""

from __future__ import annotations

from typing import Iterable, List, Optional, Sequence


class FieldSelectionError(ValueError):
    """A `--json` field request is unknown or empty.

    Handlers map this to exit 2 (argument/usage error) and print ``.message``
    on stderr.
    """

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def json_enabled(json_mode) -> bool:
    """True when ``--json`` was supplied in any form (bare, fields, or empty).

    ``args.json_mode`` is ``None`` only when the flag is absent; any other
    value (``True`` for bare ``--json``, a field string, or even ``""``)
    means JSON output was requested. Callers must use this rather than a bare
    truthiness test so ``--json ''`` is treated as *enabled* (and then fails
    as an empty field list) instead of silently falling back to text output.
    """
    return json_mode is not None


def parse_fields(json_mode, valid_fields: Sequence[str], *, prog: str) -> Optional[List[str]]:
    """Resolve ``args.json_mode`` to an ordered field list, or ``None``.

    ``None`` (flag absent) and ``True`` (bare ``--json``) both mean "emit the
    full documented shape" and return ``None``. A comma-separated string is
    parsed into an ordered, de-duplicated list validated against
    ``valid_fields``; an unknown field or an empty (all-whitespace) list
    raises :class:`FieldSelectionError` (which the caller maps to exit 2).
    """
    if json_mode is None or json_mode is True:
        return None
    fields: List[str] = []
    for chunk in str(json_mode).split(","):
        name = chunk.strip()
        if not name:
            continue
        if name not in valid_fields:
            raise FieldSelectionError(
                "%s: unknown --json field '%s' (valid fields: %s)"
                % (prog, name, ", ".join(valid_fields))
            )
        if name not in fields:
            fields.append(name)
    if not fields:
        raise FieldSelectionError(
            "%s: --json field list is empty (valid fields: %s)"
            % (prog, ", ".join(valid_fields))
        )
    return fields


def project_object(obj: dict, fields: Optional[Sequence[str]]) -> dict:
    """Project ``obj`` to ``fields`` in caller order, or return it unchanged.

    ``fields is None`` (full shape) returns ``obj`` unchanged. Otherwise an
    insertion-ordered dict is built picking each requested field in order. A
    field that is valid for the verb but absent from *this particular* object
    (e.g. ``sections`` on an ``issue show`` without ``--section``) is omitted
    from the projection rather than emitted as null.
    """
    if fields is None:
        return obj
    return {name: obj[name] for name in fields if name in obj}


def project_list(objs: Iterable[dict], fields: Optional[Sequence[str]]) -> List[dict]:
    """Project every object in ``objs`` (see :func:`project_object`)."""
    if fields is None:
        return list(objs)
    return [project_object(obj, fields) for obj in objs]
