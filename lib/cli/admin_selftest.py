"""`issue admin selftest` -- post-install end-to-end functional smoke.

Dispatcher port of the flat ``issue-mgmt-selftest`` bash binary.
Proves an install actually WORKS end-to-end --
store scaffold, project registration, issue creation + note, a blocks
dependency edge, the frontmatter-native ready-set transition, and body
search -- against a throwaway ``mktemp`` store with ``HOME`` /
``ISSUE_STORE_ROOT`` pinned inside it, so the operator's real store,
sentinels, and config are never touched. Idempotent, self-cleaning, no
network. Exit 0 pass; 1 a named check failed; 2 usage error.

Self-consistency: the smoke drives the consolidated ``issue
<verb>`` surface -- each step resolves to ``issue <verb>`` when that verb is
present in the dispatcher, so a shipped v3 install exercises the very
binary under test. Verbs not yet registered on the current tree (parallel
WPs land them) fall back to the flat sibling next to the dispatcher, so the
smoke passes at every phase-integration point; the shipped install never
takes the fallback (all verbs exist under ``issue``).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import discover

VERSION = "v3.3.1"

_USAGE = """\
Usage: issue-mgmt-selftest [--help|--version]

Post-install functional smoke for issue-mgmt. Creates a throwaway issue
store in a temp directory and exercises the real workflow end-to-end:

  1. init-issue-store scaffold
  2. init-issue-project prefix registration
  3. two issues via new-issue, a note via issue-note
  4. a blocks dependency edge via issue-dep
  5. issue-ready EXCLUDES the blocked issue
  6. blocker completed open -> in-progress -> done via transition-issue
  7. issue-ready now INCLUDES the unblocked issue
  8. body search via search-issues

Isolation guarantees (the load-bearing safety property):
  - Runs against a store created under mktemp -d, removed on exit.
  - HOME is overridden to a temp dir for the duration of the run, so
    ~/.config/issue-mgmt/config and any tool state under HOME are never
    read or written.
  - ISSUE_STORE_ROOT is pinned to the temp store; any inherited value,
    cwd sentinels, and the operator's configured store are ignored.
  - No network calls.

git is a required prerequisite (the issue store is a git repo; every
mutation commits to it); the smoke fails by name when git is absent.

Exit codes: 0 pass; 1 a named check failed; 2 usage error.
"""


class _Fail(Exception):
    """A named check failed (fail-by-name, exit 1)."""


# flat-CLI-name -> (tier, verb) mapping to the consolidated surface. Mirrors
# tests/verification/lib.sh's _consolidated_verb_for for the verbs this smoke
# exercises.
_INVOKE = {
    "new-issue": ("tier1", "create"),
    "init-issue-store": ("admin", "init-store"),
    "init-issue-project": ("admin", "init-project"),
    "issue-note": ("tier1", "note"),
    "issue-dep": ("tier1", "dep"),
    "issue-ready": ("tier1", "ready"),
    "transition-issue": ("tier1", "transition"),
    "search-issues": ("tier1", "search"),
}


def _make_resolver(issue_bin):
    """Return resolve(flatname) -> argv prefix list.

    Prefers ``issue <verb>`` when the verb is registered on this dispatcher;
    otherwise the flat sibling next to the dispatcher (pre-integration path).
    """
    groups = discover()
    self_dir = os.path.dirname(issue_bin)

    def resolve(flatname):
        tier, verb = _INVOKE[flatname]
        if any(getattr(m, "VERB", None) == verb for m in groups.get(tier, [])):
            words = [verb] if tier == "tier1" else [tier, verb]
            return [issue_bin] + words
        for cand in (
            os.path.join(self_dir, flatname),
            os.path.join(self_dir, flatname + ".sh"),
        ):
            if os.access(cand, os.X_OK):
                return [cand]
        raise _Fail(
            f"cannot resolve CLI '{flatname}' under {self_dir}"
        )

    return resolve


def _word_present(text, token):
    return re.search(r"(?<![\w-])" + re.escape(token) + r"(?![\w-])", text) is not None


def _run_smoke():
    ok_count = 0
    skip_count = 0

    def ok(msg):
        nonlocal ok_count
        print(f"ok: {msg}")
        ok_count += 1

    # Resolve the dispatcher path BEFORE any chdir so relative argv[0] stays
    # valid for the resolver and its flat-sibling fallback.
    issue_bin = os.path.realpath(sys.argv[0])
    resolve = _make_resolver(issue_bin)

    # git is a hard prerequisite -- fail by name before any work.
    if shutil.which("git") is None:
        raise _Fail("git not found on PATH (required for the issue store)")

    tmp_parent = os.environ.get("TMPDIR", "/tmp")
    work = tempfile.mkdtemp(prefix="issue-mgmt-selftest.", dir=tmp_parent)
    try:
        store = os.path.join(work, "store")
        proj = os.path.join(work, "selftest-proj")

        os.environ["HOME"] = os.path.join(work, "home")
        os.makedirs(os.environ["HOME"], exist_ok=True)
        os.environ["ISSUE_STORE_ROOT"] = store

        for cfg in (
            ["user.name", "issue-mgmt-selftest"],
            ["user.email", "selftest@issue-mgmt.invalid"],
            ["init.defaultBranch", "main"],
        ):
            if subprocess.run(
                ["git", "config", "--global", *cfg]
            ).returncode != 0:
                return 1

        def run_cli(flatname, *args, cwd=None):
            return subprocess.run(
                [*resolve(flatname), *args],
                capture_output=True, text=True, cwd=cwd,
            )

        # --- 1. store scaffold ------------------------------------------
        if run_cli("init-issue-store", store, "--non-interactive").returncode != 0:
            raise _Fail("init-issue-store: scaffold exited non-zero")
        if not (
            os.path.isfile(os.path.join(store, "INDEX.md"))
            and os.path.isdir(os.path.join(store, ".git"))
        ):
            raise _Fail(
                "init-issue-store: expected INDEX.md and .git in the new store"
            )
        ok("init-issue-store scaffolded a fresh store")

        # --- 2. project registration ------------------------------------
        os.makedirs(proj, exist_ok=True)
        if subprocess.run(["git", "init", "-q"], cwd=proj).returncode != 0:
            raise _Fail("could not create temp project repo")
        os.chdir(proj)
        if run_cli(
            "init-issue-project", "--repo", "selftest-proj", "--prefix", "SMK",
            "--non-interactive",
        ).returncode != 0:
            raise _Fail("init-issue-project: registration exited non-zero")
        repos = Path(store) / ".repos"
        if not (repos.is_file() and "selftest-proj:SMK" in
                [ln.strip() for ln in repos.read_text().splitlines()]):
            raise _Fail(
                "init-issue-project: selftest-proj:SMK not recorded in "
                "<store>/.repos"
            )
        ok("init-issue-project registered prefix SMK")

        # --- 3. issue creation + note -----------------------------------
        def new_issue(title):
            r = run_cli("new-issue", title, "feature", "high")
            m = re.search(r"SMK-\d+", r.stdout or "")
            return m.group(0) if m else ""

        id1 = new_issue("Selftest blocker issue")
        if not (id1 and os.path.isfile(os.path.join(store, "SMK", f"{id1}.md"))):
            raise _Fail("new-issue: first issue not created under the temp store")
        id2 = new_issue("Selftest dependent issue")
        if not (
            id2 and id2 != id1
            and os.path.isfile(os.path.join(store, "SMK", f"{id2}.md"))
        ):
            raise _Fail("new-issue: second issue not created under the temp store")
        ok(f"new-issue created {id1} and {id2}")

        if run_cli(
            "issue-note", id2, "selftest-marker-a11ce body search token"
        ).returncode != 0:
            raise _Fail("issue-note: append exited non-zero")
        id2_file = Path(store) / "SMK" / f"{id2}.md"
        if "selftest-marker-a11ce" not in id2_file.read_text():
            raise _Fail(f"issue-note: marker text not present in {id2}")
        ok(f"issue-note appended a dated note to {id2}")

        # --- 4. dependency edge -----------------------------------------
        if run_cli("issue-dep", "add", id2, f"blocks:{id1}").returncode != 0:
            raise _Fail("issue-dep add: exited non-zero")
        dep_list = run_cli("issue-dep", "list", id2)
        if not _word_present(dep_list.stdout or "", id1):
            raise _Fail(
                f"issue-dep list: blocks edge {id2} -> {id1} not recorded"
            )
        ok(f"issue-dep recorded blocks edge {id2} -> {id1}")

        # --- 5-7. ready-set transition (frontmatter-native) -------------
        def ready_ids():
            r = run_cli("issue-ready", "--json")
            if r.returncode != 0:
                raise _Fail("issue-ready: exited non-zero")
            data = json.loads(r.stdout)
            return [i["id"] for i in data.get("ready", [])]

        ready_before = ready_ids()
        if id1 not in ready_before:
            raise _Fail(f"issue-ready: {id1} (unblocked) missing from the ready set")
        if id2 in ready_before:
            raise _Fail(
                f"issue-ready: {id2} present in ready set while blocked by {id1}"
            )
        ok(f"issue-ready excludes blocked {id2}, includes {id1}")

        if run_cli("transition-issue", id1, "in-progress").returncode != 0:
            raise _Fail(f"transition-issue: {id1} open -> in-progress exited non-zero")
        if run_cli("transition-issue", id1, "done").returncode != 0:
            raise _Fail(f"transition-issue: {id1} in-progress -> done exited non-zero")
        if "status: done" not in (Path(store) / "SMK" / f"{id1}.md").read_text():
            raise _Fail(f"transition-issue: {id1} frontmatter status is not done")
        ok(f"transition-issue walked {id1} open -> in-progress -> done")

        if id2 not in ready_ids():
            raise _Fail(f"issue-ready: {id2} still not ready after blocker {id1} done")
        ok(f"issue-ready includes {id2} after blocker completed")

        # --- 8. body search ---------------------------------------------
        search = run_cli("search-issues", "selftest-marker-a11ce")
        if not _word_present(search.stdout or "", id2):
            raise _Fail(f"search-issues: body search did not find {id2}")
        ok(f"search-issues found {id2} by body token")

        print(
            f"issue-mgmt-selftest: PASS ({ok_count} ok, {skip_count} skipped)"
        )
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv

    for arg in argv:
        if arg in ("-h", "--help"):
            sys.stdout.write(_USAGE)
            return 0
        if arg == "--version":
            print(f"issue-mgmt-selftest {VERSION}")
            return 0
        print(f"Unknown argument: {arg}", file=sys.stderr)
        print("Run with --help for usage.", file=sys.stderr)
        return 2

    try:
        return _run_smoke()
    except _Fail as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1


# --- consolidated `issue admin selftest` dispatcher glue ----

VERB = "selftest"
TIER = "admin"
SUMMARY = "end-to-end functional smoke against a throwaway store"
ORDER = 40


def register(subparsers):
    p = subparsers.add_parser(
        VERB, help=SUMMARY, add_help=False, prefix_chars="\x00"
    )
    p.add_argument("rest", nargs="*")
    p.set_defaults(_run=run)
    return p


def run(args):
    try:
        rc = main(args.rest)
    except SystemExit as e:
        rc = e.code
    if rc is None:
        return 0
    return rc if isinstance(rc, int) else 1
