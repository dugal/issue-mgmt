#!/usr/bin/env bash
# Bash wrapper for lib/concurrency.py. Source from shell scripts that need
# to lock, atomically write, snapshot, or verify file state.
#
# Usage:
#   source "${LIB_DIR}/concurrency.sh"
#   concurrency::with_issue_lock <store> <prefix> <id> -- <cmd> [args...]
#   concurrency::with_index_lock <store> -- <cmd> [args...]
#   concurrency::with_registry_lock <store> -- <cmd> [args...]
#   concurrency::with_store_mutation <store> --message MSG --path P1 [--path P2...] -- <cmd> [args...]
#   concurrency::catchup_sweep <store>
#   concurrency::commit_after_write <store> --message MSG --path P1 [--path P2...]
#   echo "<content>" | concurrency::atomic_write <target>
#   snap=$(concurrency::capture_state <path>)
#   concurrency::verify_unchanged <path> "<snap>" || exit $?
#
# Exit-code constants (contiguous scheme; 3 was 17 through v2.4.1):
#   $CONCURRENCY_EXIT_CONCURRENT_EDIT     = 3
#   $CONCURRENCY_EXIT_STORE_COMMIT_FAILED = 1

CONCURRENCY_EXIT_CONCURRENT_EDIT=3
CONCURRENCY_EXIT_STORE_COMMIT_FAILED=1

# Resolve concurrency.py path relative to this script (handles symlinks).
_concurrency_resolve_py() {
    local src="${BASH_SOURCE[0]}"
    local dir
    dir=$(python3 -c "import os,sys; print(os.path.dirname(os.path.realpath(sys.argv[1])))" "$src")
    echo "${dir}/concurrency.py"
}

CONCURRENCY_PY=$(_concurrency_resolve_py)

_concurrency_strip_dashdash() {
    if [ "${1:-}" = "--" ]; then
        shift
    fi
    printf '%s\n' "$@"
}

concurrency::with_lock() {
    # concurrency::with_lock <lockpath> -- <cmd> [args...]
    local lockpath="$1"
    shift
    if [ "${1:-}" = "--" ]; then
        shift
    fi
    python3 "$CONCURRENCY_PY" flock-run "$lockpath" -- "$@"
}

concurrency::_issue_lock_path() {
    # <store> <prefix> <id>
    printf '%s/%s/.locks/%s.lock\n' "$1" "$2" "$3"
}

concurrency::_index_lock_path() {
    printf '%s/.locks/INDEX.lock\n' "$1"
}

concurrency::_registry_lock_path() {
    printf '%s/.locks/.repos.lock\n' "$1"
}

concurrency::with_issue_lock() {
    # <store> <prefix> <id> -- <cmd> [args...]
    local store="$1" prefix="$2" id="$3"
    shift 3
    if [ "${1:-}" = "--" ]; then
        shift
    fi
    local lockpath
    lockpath=$(concurrency::_issue_lock_path "$store" "$prefix" "$id")
    python3 "$CONCURRENCY_PY" flock-run "$lockpath" -- "$@"
}

concurrency::with_index_lock() {
    # <store> -- <cmd> [args...]
    local store="$1"
    shift
    if [ "${1:-}" = "--" ]; then
        shift
    fi
    local lockpath
    lockpath=$(concurrency::_index_lock_path "$store")
    python3 "$CONCURRENCY_PY" flock-run "$lockpath" -- "$@"
}

concurrency::with_registry_lock() {
    # <store> -- <cmd> [args...]
    local store="$1"
    shift
    if [ "${1:-}" = "--" ]; then
        shift
    fi
    local lockpath
    lockpath=$(concurrency::_registry_lock_path "$store")
    python3 "$CONCURRENCY_PY" flock-run "$lockpath" -- "$@"
}

concurrency::with_store_mutation() {
    # <store> --message MSG --path P1 [--path P2...] -- <cmd> [args...]
    # Forwards directly to the python CLI so flag parsing stays in one place.
    python3 "$CONCURRENCY_PY" store-mutation "$@"
}

concurrency::catchup_sweep() {
    # <store>
    python3 "$CONCURRENCY_PY" catchup-sweep "$1"
}

concurrency::commit_after_write() {
    # <store> --message MSG --path P1 [--path P2...]
    python3 "$CONCURRENCY_PY" commit-after-write "$@"
}

concurrency::atomic_write() {
    # stdin -> <target>
    python3 "$CONCURRENCY_PY" atomic-write "$1"
}

concurrency::capture_state() {
    # <path> -> prints "mtime_ns:size:sha256" or "absent"
    python3 "$CONCURRENCY_PY" snapshot "$1"
}

concurrency::verify_unchanged() {
    # <path> <snapshot-string>; exits 3 (concurrent-edit) on mismatch
    python3 "$CONCURRENCY_PY" verify "$1" "$2"
}
