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

VERSION = "v3.4.1"

# The one place this verb names itself, so the manifest sees exactly one
# name for it and `surface.self-naming` is checkable (IM-239). Declared
# above `_USAGE` because the usage block interpolates it.
PROG = "issue admin selftest"

_USAGE = f"Usage: {PROG} [--json] [--help|--version]\n" + """\

Post-install functional smoke for issue-mgmt. Creates a throwaway issue
store in a temp directory and exercises the real workflow end-to-end:

  1. `issue admin init-store` scaffold
  2. `issue admin init-project` prefix registration
  3. two issues via `issue create`, a note via `issue note`
  4. a blocks dependency edge via `issue dep`
  5. `issue ready` EXCLUDES the blocked issue
  6. blocker completed open -> in-progress -> done via `issue transition`
  7. `issue ready` now INCLUDES the unblocked issue
  8. body search via `issue search`

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

JSON mode (--json): exactly one object on stdout, shape

  {
    "result": "pass" | "fail",
    "checks": [{"name": TEXT, "status": "ok"}, ...],
    "ok": N,                      # checks that passed
    "skipped": N,
    "failed": TEXT|null           # the failed check's message, or null
  }

Emitted on both outcomes, so a caller branches on `result` rather than on
exit code alone and gets the failed check's name without scraping stderr.
Every key is present in both, so output shape is a function of argv
alone. The per-check progress lines are replaced by the JSON document,
not duplicated; the `FAIL:` diagnostic still goes to stderr.

Exit codes: 0 pass; 1 a named check failed; 2 usage error.
"""


class _Fail(Exception):
    """A named check failed (fail-by-name, exit 1)."""


def _make_resolver(issue_bin):
    """Return resolve("<verb>" | "<tier> <subverb>") -> argv prefix list.

    IM-239: this used to be keyed on the RETIRED flat binary names
    (`new-issue`, `transition-issue`, ...) through an `_INVOKE` routing
    table, with a fallback to a flat sibling next to the dispatcher. The
    fallback cannot fire -- the tombstone stubs were removed in v3.1.0, and
    before that they exited 2 -- so the table was a retired-name inventory
    with nothing behind it, and every check message built its prefix from
    the key. Keying on the consolidated verb path deletes the table and
    makes each message name the verb it actually ran.
    """
    groups = discover()

    def resolve(verbpath):
        words = verbpath.split()
        tier, verb = ("tier1", words[0]) if len(words) == 1 else (words[0], words[1])
        if not any(getattr(m, "VERB", None) == verb
                   for m in groups.get(tier, [])):
            raise _Fail(
                f"verb 'issue {verbpath}' is not registered on this "
                f"dispatcher ({issue_bin})"
            )
        return [issue_bin] + words

    return resolve


def _word_present(text, token):
    return re.search(r"(?<![\w-])" + re.escape(token) + r"(?![\w-])", text) is not None


def _run_smoke(checks, *, json_mode=False):
    """Drive the smoke, appending one record per passing check to `checks`.

    `checks` is owned by the caller so a mid-run `_Fail` still leaves the
    checks that did pass available for the `--json` document.
    """
    skip_count = 0

    def ok(msg):
        # Progress lines are stdout's only content in human mode; under
        # --json stdout must carry exactly one document, so they are
        # replaced by the checks array rather than duplicated (IM-144).
        if not json_mode:
            print(f"ok: {msg}")
        checks.append({"name": msg, "status": "ok"})

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

        # The one email literal in the public view that is NOT the shared
        # test-fixture identity (IM-268). It is deliberately its own address:
        # this is shipped runtime code authoring real commits in the store it
        # creates, not a test fixture, so it cannot import a test constant and
        # should not be named after one. It has one occurrence and no reason
        # to multiply, which is exactly what the nine fixture addresses did.
        # `.publish.yaml` `leaks.allow` carries it explicitly, and
        # `scripts/check-fixture-emails` is what requires that entry to exist.
        for cfg in (
            ["user.name", "issue-mgmt-selftest"],
            ["user.email", "selftest@issue-mgmt.invalid"],
            ["init.defaultBranch", "main"],
        ):
            if subprocess.run(
                ["git", "config", "--global", *cfg]
            ).returncode != 0:
                # Was a bare `return 1` with nothing on either stream;
                # named so --json always has a `failed` to report and the
                # human run says which step broke.
                raise _Fail(
                    f"git config --global {cfg[0]} failed in the temp HOME"
                )

        def run_cli(flatname, *args, cwd=None):
            return subprocess.run(
                [*resolve(flatname), *args],
                capture_output=True, text=True, cwd=cwd,
            )

        # --- 1. store scaffold ------------------------------------------
        if run_cli("admin init-store", store, "--non-interactive").returncode != 0:
            raise _Fail("issue admin init-store: scaffold exited non-zero")
        if not (
            os.path.isfile(os.path.join(store, "INDEX.md"))
            and os.path.isdir(os.path.join(store, ".git"))
        ):
            raise _Fail(
                "issue admin init-store: expected INDEX.md and .git in the new store"
            )
        ok("issue admin init-store scaffolded a fresh store")

        # --- 2. project registration ------------------------------------
        os.makedirs(proj, exist_ok=True)
        if subprocess.run(["git", "init", "-q"], cwd=proj).returncode != 0:
            raise _Fail("could not create temp project repo")
        os.chdir(proj)
        if run_cli(
            "admin init-project", "--repo", "selftest-proj", "--prefix", "SMK",
            "--non-interactive",
        ).returncode != 0:
            raise _Fail("issue admin init-project: registration exited non-zero")
        repos = Path(store) / ".repos"
        if not (repos.is_file() and "selftest-proj:SMK" in
                [ln.strip() for ln in repos.read_text().splitlines()]):
            raise _Fail(
                "issue admin init-project: selftest-proj:SMK not recorded in "
                "<store>/.repos"
            )
        ok("issue admin init-project registered prefix SMK")

        # --- 3. issue creation + note -----------------------------------
        def new_issue(title):
            r = run_cli("create", title, "feature", "high")
            m = re.search(r"SMK-\d+", r.stdout or "")
            return m.group(0) if m else ""

        id1 = new_issue("Selftest blocker issue")
        if not (id1 and os.path.isfile(os.path.join(store, "SMK", f"{id1}.md"))):
            raise _Fail("issue create: first issue not created under the temp store")
        id2 = new_issue("Selftest dependent issue")
        if not (
            id2 and id2 != id1
            and os.path.isfile(os.path.join(store, "SMK", f"{id2}.md"))
        ):
            raise _Fail("issue create: second issue not created under the temp store")
        ok(f"issue create created {id1} and {id2}")

        if run_cli(
            "note", id2, "selftest-marker-a11ce body search token"
        ).returncode != 0:
            raise _Fail("issue note: append exited non-zero")
        id2_file = Path(store) / "SMK" / f"{id2}.md"
        if "selftest-marker-a11ce" not in id2_file.read_text():
            raise _Fail(f"issue note: marker text not present in {id2}")
        ok(f"issue note appended a dated note to {id2}")

        # --- 4. dependency edge -----------------------------------------
        if run_cli("dep", "add", id2, f"blocks:{id1}").returncode != 0:
            raise _Fail("issue dep add: exited non-zero")
        dep_list = run_cli("dep", "list", id2)
        if not _word_present(dep_list.stdout or "", id1):
            raise _Fail(
                f"issue dep list: blocks edge {id2} -> {id1} not recorded"
            )
        ok(f"issue dep recorded blocks edge {id2} -> {id1}")

        # --- 5-7. ready-set transition (frontmatter-native) -------------
        def ready_ids():
            r = run_cli("ready", "--json")
            if r.returncode != 0:
                raise _Fail("issue ready: exited non-zero")
            data = json.loads(r.stdout)
            return [i["id"] for i in data.get("ready", [])]

        ready_before = ready_ids()
        if id1 not in ready_before:
            raise _Fail(f"issue ready: {id1} (unblocked) missing from the ready set")
        if id2 in ready_before:
            raise _Fail(
                f"issue ready: {id2} present in ready set while blocked by {id1}"
            )
        ok(f"issue ready excludes blocked {id2}, includes {id1}")

        if run_cli("transition", id1, "in-progress").returncode != 0:
            raise _Fail(f"issue transition: {id1} open -> in-progress exited non-zero")
        if run_cli("transition", id1, "done").returncode != 0:
            raise _Fail(f"issue transition: {id1} in-progress -> done exited non-zero")
        if "status: done" not in (Path(store) / "SMK" / f"{id1}.md").read_text():
            raise _Fail(f"issue transition: {id1} frontmatter status is not done")
        ok(f"issue transition walked {id1} open -> in-progress -> done")

        if id2 not in ready_ids():
            raise _Fail(f"issue ready: {id2} still not ready after blocker {id1} done")
        ok(f"issue ready includes {id2} after blocker completed")

        # --- 8. body search ---------------------------------------------
        search = run_cli("search", "selftest-marker-a11ce")
        if not _word_present(search.stdout or "", id2):
            raise _Fail(f"issue search: body search did not find {id2}")
        ok(f"issue search found {id2} by body token")

        if not json_mode:
            print(
                f"{PROG}: PASS ({len(checks)} ok, "
                f"{skip_count} skipped)"
            )
        return 0, skip_count
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _emit_json(checks, skipped, failed):
    json.dump(
        {
            "result": "fail" if failed else "pass",
            "checks": checks,
            "ok": len(checks),
            "skipped": skipped,
            "failed": failed,
        },
        sys.stdout,
        indent=2,
        sort_keys=False,
    )
    sys.stdout.write("\n")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv

    # Hand-rolled argv walk (the dispatcher hands this verb its argv
    # verbatim through a prefix_chars=chr(0) catch-all), so --json is wired
    # in here rather than through common.add_json_flag.
    json_mode = False
    for arg in argv:
        if arg in ("-h", "--help"):
            sys.stdout.write(_USAGE)
            return 0
        if arg == "--version":
            print(f"{PROG} {VERSION}")
            return 0
        if arg == "--json":
            json_mode = True
            continue
        print(f"Unknown argument: {arg}", file=sys.stderr)
        print("Run with --help for usage.", file=sys.stderr)
        return 2

    checks = []
    try:
        rc, skipped = _run_smoke(checks, json_mode=json_mode)
    except _Fail as e:
        print(f"FAIL: {e}", file=sys.stderr)
        if json_mode:
            _emit_json(checks, 0, str(e))
        return 1
    if json_mode:
        _emit_json(checks, skipped, None)
    return rc


# --- consolidated `issue admin selftest` dispatcher glue ----

VERB = "selftest"
TIER = "admin"
SUMMARY = "end-to-end functional smoke against a throwaway store"
ORDER = 40

# Surface-manifest declarations (IM-245); protocol table in lib/cli/__init__.py.
SURFACE_CLASS = "scaffolding"
# IM-251: deliberately pins its own throwaway store and must refuse
# --store -- never touching the operator's store is the whole promise.
SURFACE_STORE = "pinned"
# IM-254: `unconverted` until every failure path on this verb carries the
# key=value payload; the set is enumerated, never waived, and only shrinks
# (conversion work is IM-260).
SURFACE_STRUCTURED_ERRORS = "unconverted"
# IM-245: this verb has NO parser -- `register()` installs a
# prefix_chars="\x00" passthrough subparser with one nargs="*" catch-all and
# `main()` walks argv by hand, so an argparse walk sees zero flags while the
# verb really accepts these. Declared literally so the manifest records the
# true surface instead of a false-CLEAN empty flag set. Keep in sync with the
# argv loop in `main()`.
SURFACE_MANUAL_FLAGS = (
    {"names": ["-h", "--help"], "action": "help"},
    {"names": ["--version"], "action": "version"},
    {"names": ["--json"], "action": "store_true",
     "dest": "json_mode", "default": False},
)


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
