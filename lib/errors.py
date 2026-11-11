"""errors -- CLI-visible typed exceptions and exit-code constants.

Single home for the CLI loser-side contract: every typed exception a
mutating subcommand can surface to a caller, plus the contiguous
exit-code constants they map to. Design reference:
docs/design/discourse-workflow-split.md Sections 4.5-4.8.

Exit-code contract (contiguous 3-7 scheme; v2.5.0 renumber -- exit 3
was 17 through v2.4.1, exit 7 was 18 in the pre-revision design):

    | Exit | Class                    | Caller action                    |
    |------|--------------------------|----------------------------------|
    | 0    | success                  | Proceed.                         |
    | 1    | precondition-failed      | Surface; do not blindly retry.   |
    |      | / zero-result (queries)  | Sleep or exit cleanly.           |
    | 2    | usage-error              | Fix arguments; do not retry.     |
    | 3    | concurrent-edit          | Re-read body, re-apply, commit.  |
    | 4    | lost-race                | Try next ready issue / back off. |
    | 5    | stale-local-view         | Refresh local read; retry same.  |
    | 6    | dependency-state-changed | Re-query ready; retry.           |
    | 7    | not-claimable            | Escalate; no unattended retry.   |

Stderr shape: key=value tokens on one line by default (values with
whitespace are double-quoted); `--json` opt-in emits a one-line JSON
object on stderr with the same fields plus `exit` and `class`.

Public import path:

    from errors import (
        EXIT_SUCCESS, EXIT_PRECONDITION, EXIT_USAGE,
        EXIT_CONCURRENT_EDIT, EXIT_LOST_RACE, EXIT_STALE_VIEW,
        EXIT_DEP_CHANGED, EXIT_NOT_CLAIMABLE,
        ContractError, ConcurrentEdit, LostRaceError, StaleViewError,
        DependencyChangedError, NotClaimableError, LeaseExpiredError,
        IssueNotFoundError, DependencyCycleError, PreconditionError,
    )

`ConcurrentEdit` moved here from lib/concurrency.py; its
constructor signature is unchanged so pre-move raise sites work as-is.
"""

import json as _json

EXIT_SUCCESS = 0
EXIT_PRECONDITION = 1
# Legacy alias: earlier name for exit 1 (value unchanged).
EXIT_STORE_COMMIT_FAILED = EXIT_PRECONDITION
EXIT_USAGE = 2
EXIT_CONCURRENT_EDIT = 3   # was 17 through v2.4.1
EXIT_LOST_RACE = 4
EXIT_STALE_VIEW = 5
EXIT_DEP_CHANGED = 6
EXIT_NOT_CLAIMABLE = 7


def _fmt_value(v):
    """Render one key=value token value: bare when shell-safe, double-
    quoted (with inner quotes escaped) when it contains whitespace,
    quotes, or is empty."""
    s = str(v)
    if s == "" or '"' in s or any(ch.isspace() for ch in s):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


class ContractError(Exception):
    """Base for CLI-contract typed exceptions.

    Subclasses set `exit_code` and `error_class`. `fields` is an
    ordered mapping rendered into the structured stderr line; None
    values are dropped. `format_stderr` renders either the key=value
    default or the one-line JSON opt-in shape.
    """

    exit_code = EXIT_PRECONDITION
    error_class = "precondition-failed"

    def __init__(self, message="", **fields):
        super().__init__(message or self.error_class)
        self.message = message
        self.fields = {k: v for k, v in fields.items() if v is not None}

    def stderr_fields(self):
        """Ordered field mapping for the stderr line. Subclasses may
        override to fix ordering; default is insertion order."""
        return dict(self.fields)

    def format_stderr(self, tool_name="issue-mgmt", *, json_mode=False):
        fields = {
            k: v for k, v in self.stderr_fields().items() if v is not None
        }
        if json_mode:
            obj = {"exit": self.exit_code, "class": self.error_class,
                   "error": self.error_class}
            obj.update(fields)
            if self.message:
                obj.setdefault("detail", self.message)
            obj.setdefault("tool", tool_name)
            return _json.dumps(obj, separators=(",", ":"))
        parts = [f"{tool_name}: error={self.error_class}"]
        for k, v in fields.items():
            parts.append(f"{k}={_fmt_value(v)}")
        if self.message:
            parts.append(f"detail={_fmt_value(self.message)}")
        return " ".join(parts)


class PreconditionError(ContractError):
    """A generic precondition failure, carrying a `reason=` discriminator.

    IM-253: the shape every verb uses to report a condition that is not a
    zero-result and not an argument error -- most notably an unusable
    issue store, where an enumeration read's exit 1 would otherwise be
    indistinguishable from "matched nothing". The caller branches on
    `reason`; `detail` names the recovery in prose.
    """

    exit_code = EXIT_PRECONDITION
    error_class = "precondition-failed"


class UnknownPrefixError(ContractError):
    """A `--prefix` scope names a prefix the store does not have.

    IM-238: scoping a read to a prefix that exists nowhere in the store is
    an argument error, not a zero-result -- a typo'd prefix is otherwise
    indistinguishable from a genuinely empty scope. Store mode only; legacy
    mode has no registry to validate against.
    """

    exit_code = EXIT_USAGE
    error_class = "unknown-prefix"


class DependencyCycleError(ContractError):
    """Authoring a `blocks` edge would close a dependency cycle.

    The frontmatter-native authoring-time guard (relocated out of
    the retired bd bridge). `blocks` is the sole ordering
    relation, so only a `blocks` edge can form a cycle that matters. Maps
    to exit 1 (precondition-failed reason=dependency-cycle); the message
    and the `cycle` field name the cycle members. Raised before any
    markdown write/commit, so nothing has diverged and there is nothing to
    reconcile.
    """

    exit_code = EXIT_PRECONDITION
    error_class = "precondition-failed"

    def __init__(self, message="", *, cycle=None):
        super().__init__(message, reason="dependency-cycle", cycle=cycle)
        self.cycle = cycle


class StoreIntegrityError(ContractError):
    """Two on-disk files zero-pad the same issue number differently.

    Raised by `idresolve` when a width-agnostic id lookup finds
    more than one file whose stem parses to the requested integer (e.g.
    `IM-007.md` AND `IM-0007.md`). The store cannot decide which file the
    caller meant, so the resolver fails loud rather than silently picking
    one. Maps to exit 1 (precondition-failed); the message and the `files`
    field name every colliding path.
    """

    exit_code = EXIT_PRECONDITION
    error_class = "store-integrity"

    def __init__(self, message="", *, files=None, issue_id=None):
        files_str = ", ".join(files) if files else None
        super().__init__(message, issue_id=issue_id, files=files_str)
        self.files = list(files) if files else []
        self.issue_id = issue_id


class ConcurrentEdit(ContractError):
    """Concurrent-edit signal (body-edit snapshot mismatch or lock
    timeout). Callers map to exit 3 on the CLI (was 17 through v2.4.1).

    Recovery: re-read state, re-apply the edit, retry once.
    """

    exit_code = EXIT_CONCURRENT_EDIT
    error_class = "concurrent-edit"

    def __init__(self, message, *, resource=None, contender_pid=None,
                 **extra):
        super().__init__(
            message, resource=resource, contender_pid=contender_pid,
            **extra,
        )
        self.resource = resource
        self.contender_pid = contender_pid


class LostRaceError(ContractError):
    """CAS race lost; another actor is the current claim holder.

    Maps to exit 4 (lost-race). Stderr names the contender so the loser
    learns who won. Recovery: try the next ready issue, or back off.
    """

    exit_code = EXIT_LOST_RACE
    error_class = "lost-race"

    def __init__(self, message="", *, contender=None, claimed_at=None,
                 lease_until=None):
        super().__init__(
            message, contender=contender, claimed_at=claimed_at,
            lease_until=lease_until,
        )
        self.contender = contender
        self.claimed_at = claimed_at
        self.lease_until = lease_until


class StaleViewError(ContractError):
    """Caller's snapshot SHA or claim generation is behind the store.

    Maps to exit 5 (stale-local-view). Recovery: refresh the local
    read; decide whether to retry the same target.
    """

    exit_code = EXIT_STALE_VIEW
    error_class = "stale-local-view"

    def __init__(self, message="", *, current_sha=None, caller_sha=None,
                 current_generation=None, caller_generation=None):
        super().__init__(
            message, current_generation=current_generation,
            caller_generation=caller_generation,
            current_sha=current_sha, caller_sha=caller_sha,
        )
        self.current_sha = current_sha
        self.caller_sha = caller_sha
        self.current_generation = current_generation
        self.caller_generation = caller_generation


class DependencyChangedError(ContractError):
    """A blocker of the target opened or closed mid-operation.

    Maps to exit 6 (dependency-state-changed). Recovery: re-query
    `ready`; retry. Reserved for future claim-with-dep-check CLIs; no
    current emitter.
    """

    exit_code = EXIT_DEP_CHANGED
    error_class = "dependency-state-changed"

    def __init__(self, message="", *, blocker=None, from_status=None,
                 to_status=None):
        super().__init__(message)
        self.blocker = blocker
        self.from_status = from_status
        self.to_status = to_status
        # stderr keys are `from` / `to` per the design table; `from` is
        # a Python keyword so the attributes carry the _status suffix.
        self.fields = {
            k: v
            for k, v in (
                ("blocker", blocker),
                ("from", from_status),
                ("to", to_status),
            )
            if v is not None
        }


class NotClaimableError(ContractError):
    """Issue not in a claimable state: status not claimable, ID not
    found, lease expired with no new holder, or the actor's own lease
    died.

    Maps to exit 7 (not-claimable). Recovery: escalate; do not retry
    without operator input.
    """

    exit_code = EXIT_NOT_CLAIMABLE
    error_class = "not-claimable"

    def __init__(self, message="", *, status=None, reason=None, **extra):
        super().__init__(message, status=status, reason=reason, **extra)
        self.status = status
        self.reason = reason


class LeaseExpiredError(NotClaimableError):
    """The actor's own lease died (expired, possibly reaped, with no
    new holder). Subclass of NotClaimableError so cleanup paths can
    catch it separately. Maps to exit 7 with reason=lease-expired.
    """

    def __init__(self, message="", *, status=None, actor=None,
                 lease_until=None):
        super().__init__(
            message, status=status, reason="lease-expired", actor=actor,
            lease_until=lease_until,
        )
        self.actor = actor
        self.lease_until = lease_until


class IssueNotFoundError(NotClaimableError):
    """Issue ID does not resolve to a store file, raised from claim
    paths. Maps to exit 7 with reason=not-found. Non-claim paths keep
    their existing FileNotFoundError -> exit 1 behavior.
    """

    def __init__(self, message="", *, issue_id=None):
        super().__init__(message, reason="not-found", id=issue_id)
        self.issue_id = issue_id
