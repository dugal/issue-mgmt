#!/usr/bin/env python3
"""Unit tests for lib/migrate.py.

Covers:
  - is_migrated / read_sentinel
  - plan_migration: prefix resolution (override > .prefix > registry),
    cross-validation mismatch detection, no-prefix-resolvable error,
    invalid prefix shape, no .issues/ dir, missing store root,
    already-migrated short-circuit (AlreadyMigrated raised), source
    enumeration (issues vs companions vs extraneous), validation
    integration (errors surface in plan.validation_results).
  - execute_migration: dry-run does not write, byte-identical sha
    on every copy, sentinel written last, cross-ref scan counts
    resolved/unresolved tokens correctly, validation errors block
    execution, idempotent re-run via sentinel.
  - scan_cross_references: own-prefix tokens resolve against the
    in-flight corpus, cross-project tokens warn but don't error,
    dedup per file.
  - Lock side-effect: each issue copy creates a .locks/<ID>.lock
    file under the destination prefix dir.

End-to-end CLI scenarios (exit codes, --dry-run output, --json
shape, concurrent-edit exit 17, already-migrated message,
validation-abort) live in tests/verification/migrate-to-store.sh.

Run:
    python3 -m unittest tests.test_migrate -v
"""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))

import concurrency  # noqa: E402
import migrate  # noqa: E402


def write_issue(path: Path, *, id_: str, title: str = "Title",
                type_: str = "feature", status: str = "open",
                priority: str = "high", body: str = "Body.") -> None:
    path.write_text(
        "---\n"
        f"id: {id_}\n"
        f'title: "{title}"\n'
        f"type: {type_}\n"
        f"status: {status}\n"
        f"priority: {priority}\n"
        "created: 2026-05-15\n"
        "updated: 2026-05-15\n"
        'branch: ""\n'
        "tags: []\n"
        "---\n"
        "\n"
        "## Summary\n"
        "\n"
        f"{body}\n",
        encoding="utf-8",
    )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_init_with_commit(repo: Path, *, file_relpath: str = "README.md",
                          contents: str = "init\n") -> str:
    """Init repo, write one file, commit it. Returns the commit sha.

    Tests need real git repos because execute_migration captures the
    source HEAD sha and drives store-side commits.
    """
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)],
                   check=True, env=env)
    # Set a repo-local identity so *in-process* commits (execute_migration
    # drives store-side commits via lib/concurrency, which does not inherit
    # the GIT_AUTHOR_*/GIT_COMMITTER_* env below) succeed on a clean box
    # with no global git identity configured.
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"],
                   check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"],
                   check=True, env=env)
    (repo / file_relpath).parent.mkdir(parents=True, exist_ok=True)
    (repo / file_relpath).write_text(contents, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "--", file_relpath],
                   check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"],
                   check=True, env=env)
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True, env=env,
    ).stdout.strip()
    return sha


class MigrateTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="im-027-")).resolve()
        self.project = self.tmp / "myproj"
        self.anchor = self.project / ".issues"
        self.anchor.mkdir(parents=True)
        self.store = self.tmp / "store"
        self.store.mkdir()
        # Apply-path tests need real git repos in both source and store
        # Plan-only tests don't trip these paths, so the cost
        # is just per-test git init -- about 10ms each.
        self._git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@t",
        }
        self.source_pre_sha = _git_init_with_commit(self.project)
        _git_init_with_commit(self.store, contents="store init\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _store_log_oneline(self):
        r = subprocess.run(
            ["git", "-C", str(self.store), "log", "--format=%H %s"],
            check=True, capture_output=True, text=True, env=self._git_env,
        )
        return r.stdout.strip().splitlines()


# ---------------------------------------------------------------- sentinel ---

class SentinelTests(MigrateTestBase):
    def test_is_migrated_absent_returns_false(self):
        assert migrate.is_migrated(self.anchor) is False

    def test_is_migrated_present_returns_true(self):
        (self.anchor / migrate.SENTINEL_NAME).write_text("anywhere", encoding="utf-8")
        assert migrate.is_migrated(self.anchor) is True

    def test_read_sentinel_trims_whitespace(self):
        (self.anchor / migrate.SENTINEL_NAME).write_text("  /a/b  \n", encoding="utf-8")
        assert migrate.read_sentinel(self.anchor) == "/a/b"


# --------------------------------------------------------------- planning ---

class PlanPrefixResolutionTests(MigrateTestBase):
    def test_prefix_from_prefix_file(self):
        (self.anchor / ".prefix").write_text("MP\n", encoding="utf-8")
        write_issue(self.anchor / "MP-001.md", id_="MP-001")
        plan = migrate.plan_migration(self.project, self.store)
        assert plan.prefix == "MP"
        assert plan.target_dir == self.store / "MP"
        assert len(plan.issue_copies) == 1

    def test_prefix_override_wins(self):
        (self.anchor / ".prefix").write_text("MP\n", encoding="utf-8")
        write_issue(self.anchor / "MP-001.md", id_="MP-001")
        with self.assertRaises(migrate.MigrationError):
            # Override disagrees with .prefix -- mismatch error.
            migrate.plan_migration(
                self.project, self.store, prefix_override="ZZ"
            )

    def test_prefix_from_registry_when_no_prefix_file(self):
        (self.store / ".repos").write_text("myproj:MP\n", encoding="utf-8")
        write_issue(self.anchor / "MP-001.md", id_="MP-001")
        plan = migrate.plan_migration(self.project, self.store)
        assert plan.prefix == "MP"
        assert plan.registry_present is True
        assert plan.registry_prefix == "MP"

    def test_prefix_mismatch_registry_vs_prefix_file(self):
        (self.anchor / ".prefix").write_text("MP\n", encoding="utf-8")
        (self.store / ".repos").write_text("myproj:ZZ\n", encoding="utf-8")
        write_issue(self.anchor / "MP-001.md", id_="MP-001")
        with self.assertRaises(migrate.MigrationError) as cm:
            migrate.plan_migration(self.project, self.store)
        assert "mismatch" in str(cm.exception).lower()

    def test_invalid_prefix_shape_rejected(self):
        (self.anchor / ".prefix").write_text("not-a-prefix\n", encoding="utf-8")
        with self.assertRaises(migrate.MigrationError) as cm:
            migrate.plan_migration(self.project, self.store)
        assert "prefix" in str(cm.exception).lower()

    def test_no_prefix_resolvable_errors(self):
        # Empty .issues, no .prefix, no registry entry.
        with self.assertRaises(migrate.MigrationError) as cm:
            migrate.plan_migration(self.project, self.store)
        assert "prefix" in str(cm.exception).lower()

    def test_repo_name_override_changes_registry_lookup(self):
        (self.store / ".repos").write_text("other-name:MP\n", encoding="utf-8")
        write_issue(self.anchor / "MP-001.md", id_="MP-001")
        plan = migrate.plan_migration(
            self.project, self.store, repo_name_override="other-name"
        )
        assert plan.prefix == "MP"
        assert plan.registry_present is True


class PlanEnumerationTests(MigrateTestBase):
    def setUp(self):
        super().setUp()
        (self.anchor / ".prefix").write_text("MP\n", encoding="utf-8")

    def test_enumerates_issue_files(self):
        write_issue(self.anchor / "MP-001.md", id_="MP-001")
        write_issue(self.anchor / "MP-002.md", id_="MP-002")
        plan = migrate.plan_migration(self.project, self.store)
        names = sorted(op.source.name for op in plan.issue_copies)
        assert names == ["MP-001.md", "MP-002.md"]

    def test_issue_copies_carry_lock_paths(self):
        write_issue(self.anchor / "MP-001.md", id_="MP-001")
        plan = migrate.plan_migration(self.project, self.store)
        op = plan.issue_copies[0]
        assert op.lock_path == self.store / "MP" / ".locks" / "MP-001.lock"
        assert op.issue_id == "MP-001"
        assert op.kind == "issue"

    def test_companion_files_collected(self):
        write_issue(self.anchor / "MP-001.md", id_="MP-001")
        (self.anchor / "ISSUE-TEMPLATE.md").write_text("template body", encoding="utf-8")
        plan = migrate.plan_migration(self.project, self.store)
        companion_names = sorted(op.source.name for op in plan.companion_copies)
        assert ".prefix" in companion_names
        assert "ISSUE-TEMPLATE.md" in companion_names

    def test_index_md_skipped(self):
        write_issue(self.anchor / "MP-001.md", id_="MP-001")
        (self.anchor / "INDEX.md").write_text("# index", encoding="utf-8")
        plan = migrate.plan_migration(self.project, self.store)
        all_names = [op.source.name for op in plan.copies]
        assert "INDEX.md" not in all_names

    def test_unexpected_md_is_extraneous(self):
        write_issue(self.anchor / "MP-001.md", id_="MP-001")
        (self.anchor / "NOTES.md").write_text("# notes", encoding="utf-8")
        plan = migrate.plan_migration(self.project, self.store)
        assert [p.name for p in plan.extraneous_files] == ["NOTES.md"]

    def test_wrong_prefix_md_is_extraneous(self):
        write_issue(self.anchor / "MP-001.md", id_="MP-001")
        # ZZ-001.md doesn't match the project's prefix MP, so it's not
        # an issue copy. It also isn't a known companion, so it's extraneous.
        write_issue(self.anchor / "ZZ-001.md", id_="ZZ-001")
        plan = migrate.plan_migration(self.project, self.store)
        issue_names = [op.source.name for op in plan.issue_copies]
        assert issue_names == ["MP-001.md"]
        assert [p.name for p in plan.extraneous_files] == ["ZZ-001.md"]


class PlanPreflightErrorTests(MigrateTestBase):
    def test_already_migrated_raises(self):
        (self.anchor / ".prefix").write_text("MP\n", encoding="utf-8")
        (self.anchor / migrate.SENTINEL_NAME).write_text(str(self.store), encoding="utf-8")
        with self.assertRaises(migrate.AlreadyMigrated) as cm:
            migrate.plan_migration(self.project, self.store)
        assert cm.exception.sentinel_path == self.anchor / migrate.SENTINEL_NAME
        assert cm.exception.content == str(self.store)

    def test_no_issues_dir_errors(self):
        shutil.rmtree(self.anchor)
        with self.assertRaises(migrate.MigrationError) as cm:
            migrate.plan_migration(self.project, self.store)
        assert ".issues" in str(cm.exception)

    def test_missing_store_root_errors(self):
        (self.anchor / ".prefix").write_text("MP\n", encoding="utf-8")
        with self.assertRaises(migrate.MigrationError) as cm:
            migrate.plan_migration(self.project, self.store / "does-not-exist")
        assert "store" in str(cm.exception).lower()


class PlanValidationTests(MigrateTestBase):
    def setUp(self):
        super().setUp()
        (self.anchor / ".prefix").write_text("MP\n", encoding="utf-8")

    def test_valid_issue_yields_clean_validation(self):
        write_issue(self.anchor / "MP-001.md", id_="MP-001")
        plan = migrate.plan_migration(self.project, self.store)
        assert plan.has_validation_errors is False
        assert all(r.ok for r in plan.validation_results)

    def test_invalid_status_surfaces_violation(self):
        write_issue(self.anchor / "MP-001.md", id_="MP-001", status="bogus")
        plan = migrate.plan_migration(self.project, self.store)
        assert plan.has_validation_errors is True
        bad = [r for r in plan.validation_results if not r.ok]
        assert len(bad) == 1
        codes = {v.code for v in bad[0].violations}
        assert "invalid-status" in codes

    def test_id_filename_mismatch_blocks_migration(self):
        # Filename MP-001.md but frontmatter id is MP-999 -> mismatch.
        write_issue(self.anchor / "MP-001.md", id_="MP-999")
        plan = migrate.plan_migration(self.project, self.store)
        assert plan.has_validation_errors is True

    def test_one_bad_file_among_many_is_caught(self):
        write_issue(self.anchor / "MP-001.md", id_="MP-001")
        write_issue(self.anchor / "MP-002.md", id_="MP-002", priority="bogus")
        write_issue(self.anchor / "MP-003.md", id_="MP-003")
        plan = migrate.plan_migration(self.project, self.store)
        assert plan.has_validation_errors is True
        ok_paths = [r.path.name for r in plan.validation_results if r.ok]
        assert ok_paths == ["MP-001.md", "MP-003.md"]


# ---------------------------------------------------------------- execute ---

class ExecuteTests(MigrateTestBase):
    def setUp(self):
        super().setUp()
        (self.anchor / ".prefix").write_text("MP\n", encoding="utf-8")
        write_issue(self.anchor / "MP-001.md", id_="MP-001")
        write_issue(self.anchor / "MP-002.md", id_="MP-002")

    def test_dry_run_writes_nothing(self):
        plan = migrate.plan_migration(self.project, self.store)
        result = migrate.execute_migration(plan, dry_run=True)
        assert result.dry_run is True
        assert result.executed is False
        assert result.sentinel_written is False
        assert not (self.store / "MP").exists()
        assert not (self.anchor / migrate.SENTINEL_NAME).exists()

    def test_apply_copies_all_files(self):
        plan = migrate.plan_migration(self.project, self.store)
        result = migrate.execute_migration(plan, dry_run=False)
        assert result.executed is True
        # Two issues plus the .prefix companion.
        assert len(result.copies_succeeded) == 3
        assert (self.store / "MP" / "MP-001.md").is_file()
        assert (self.store / "MP" / "MP-002.md").is_file()
        assert (self.store / "MP" / ".prefix").is_file()

    def test_destinations_are_byte_identical(self):
        plan = migrate.plan_migration(self.project, self.store)
        migrate.execute_migration(plan, dry_run=False)
        for op in plan.issue_copies:
            assert sha256(op.source) == sha256(op.dest), (
                f"sha256 mismatch for {op.source.name}"
            )

    def test_sentinel_written_last(self):
        plan = migrate.plan_migration(self.project, self.store)
        result = migrate.execute_migration(plan, dry_run=False)
        assert result.sentinel_written is True
        sentinel = self.anchor / migrate.SENTINEL_NAME
        assert sentinel.is_file()
        assert sentinel.read_text(encoding="utf-8").strip() == str(self.store.resolve())

    def test_sentinel_preserves_symlink_form_of_store(self):
        # When --store is a symlink, the sentinel payload must
        # carry the symlink path, NOT the realpath. Routing primitive
        # must stay self-sufficient when the underlying mount disappears
        # or maps differently on a different machine.
        link = self.tmp / "store-link"
        link.symlink_to(self.store)
        plan = migrate.plan_migration(self.project, link)
        migrate.execute_migration(plan, dry_run=False)
        sentinel = self.anchor / migrate.SENTINEL_NAME
        payload = sentinel.read_text(encoding="utf-8").strip()
        assert payload == str(link), (
            f"expected symlink form {link!s}, got {payload!s}"
        )
        # And the realpath form (what the old behavior wrote) must NOT
        # be what landed -- guards against accidental regression to
        # Path.resolve().
        assert payload != os.path.realpath(str(link)), (
            "sentinel regressed to realpath form"
        )

    def test_validation_errors_block_execution(self):
        # Introduce an invalid status; planner picks it up but doesn't raise.
        write_issue(self.anchor / "MP-003.md", id_="MP-003", status="bogus")
        plan = migrate.plan_migration(self.project, self.store)
        with self.assertRaises(migrate.MigrationError):
            migrate.execute_migration(plan, dry_run=False)
        assert not (self.store / "MP").exists()
        assert not (self.anchor / migrate.SENTINEL_NAME).exists()

    def test_re_run_after_apply_raises_already_migrated(self):
        plan = migrate.plan_migration(self.project, self.store)
        migrate.execute_migration(plan, dry_run=False)
        with self.assertRaises(migrate.AlreadyMigrated):
            migrate.plan_migration(self.project, self.store)

    def test_per_issue_lock_files_created(self):
        plan = migrate.plan_migration(self.project, self.store)
        migrate.execute_migration(plan, dry_run=False)
        for op in plan.issue_copies:
            assert op.lock_path.is_file(), f"missing lock at {op.lock_path}"


# ---------------------------------------------------------- cross-references --

class CrossRefTests(MigrateTestBase):
    def setUp(self):
        super().setUp()
        (self.anchor / ".prefix").write_text("MP\n", encoding="utf-8")

    def test_own_prefix_resolves_against_in_flight_corpus(self):
        write_issue(
            self.anchor / "MP-001.md", id_="MP-001",
            body="Refers to MP-002 inline."
        )
        write_issue(self.anchor / "MP-002.md", id_="MP-002")
        plan = migrate.plan_migration(self.project, self.store)
        _, resolved, unresolved = migrate.scan_cross_references(plan, use_sources=True)
        # MP-001 appears in its own file's frontmatter; MP-002 too.
        # Per-file dedup: MP-001 file -> {MP-001, MP-002}; MP-002 file -> {MP-002}.
        # All resolve (own_ids contains both).
        assert unresolved == 0
        assert resolved == 3

    def test_cross_project_unresolved_token_warns(self):
        write_issue(
            self.anchor / "MP-001.md", id_="MP-001",
            body="See DOUG-099 for context."
        )
        plan = migrate.plan_migration(self.project, self.store)
        warnings, resolved, unresolved = migrate.scan_cross_references(
            plan, use_sources=True
        )
        assert unresolved == 1
        assert any("DOUG-099" in w for w in warnings)

    def test_cross_project_resolved_against_existing_store_dir(self):
        # Pre-populate store with a DOUG-099 file from another project.
        (self.store / "DOUG").mkdir()
        (self.store / "DOUG" / "DOUG-099.md").write_text("---\nid: DOUG-099\n---\n", encoding="utf-8")
        write_issue(
            self.anchor / "MP-001.md", id_="MP-001",
            body="See DOUG-099 for context."
        )
        plan = migrate.plan_migration(self.project, self.store)
        _, resolved, unresolved = migrate.scan_cross_references(plan, use_sources=True)
        assert unresolved == 0

    def test_token_deduplicated_within_file(self):
        write_issue(
            self.anchor / "MP-001.md", id_="MP-001",
            body="MP-002 MP-002 MP-002 MP-002.",
        )
        write_issue(self.anchor / "MP-002.md", id_="MP-002")
        plan = migrate.plan_migration(self.project, self.store)
        _, resolved, _ = migrate.scan_cross_references(plan, use_sources=True)
        # MP-001 file: {MP-001, MP-002} = 2 unique. MP-002 file: {MP-002} = 1.
        # Without dedup we'd see ~5 tokens; with dedup we see exactly 3.
        assert resolved == 3


# ------------------------------------------------------------ template --

CANONICAL_TEMPLATE_BODY = (
    "---\n"
    "id: PFX-NNN\n"
    'title: "Title here"\n'
    "type: bug | feature | chore | spike | decision\n"
    "status: open\n"
    "priority: high | medium | low\n"
    "created: YYYY-MM-DD\n"
    "updated: YYYY-MM-DD\n"
    'branch: ""\n'
    "tags: []\n"
    "---\n"
    "\n"
    "## Summary\n"
    "\n"
    "(short paragraph)\n"
)

OVERRIDE_TEMPLATE_BODY = CANONICAL_TEMPLATE_BODY + "\n## Project-specific section\n"


class TemplateActionTests(MigrateTestBase):
    """IM-044 -- migrate-to-store de-dup of ISSUE-TEMPLATE.md."""

    def setUp(self):
        super().setUp()
        (self.anchor / ".prefix").write_text("MP\n", encoding="utf-8")
        write_issue(self.anchor / "MP-001.md", id_="MP-001")
        # Canonical generic lives outside the project + store so it
        # stands in for a Homebrew-style install path.
        self.canonical = self.tmp / "homebrew_template.md"
        self.canonical.write_text(CANONICAL_TEMPLATE_BODY, encoding="utf-8")

    def test_template_absent_in_source(self):
        plan = migrate.plan_migration(
            self.project, self.store,
            canonical_template_path=self.canonical,
        )
        assert plan.template_action == migrate.TEMPLATE_ACTION_ABSENT
        assert plan.template_local_path is None
        assert plan.template_local_sha is None
        # ISSUE-TEMPLATE.md must not be queued for copy.
        names = [op.source.name for op in plan.companion_copies]
        assert "ISSUE-TEMPLATE.md" not in names

    def test_template_skipped_when_matches_canonical(self):
        # Local template is byte-identical to the Homebrew canonical.
        (self.anchor / "ISSUE-TEMPLATE.md").write_text(
            CANONICAL_TEMPLATE_BODY, encoding="utf-8"
        )
        plan = migrate.plan_migration(
            self.project, self.store,
            canonical_template_path=self.canonical,
        )
        assert plan.template_action == migrate.TEMPLATE_ACTION_SKIP_CANONICAL
        assert plan.template_local_path == self.anchor / "ISSUE-TEMPLATE.md"
        assert plan.template_canonical_path == self.canonical
        assert plan.template_local_sha == plan.template_canonical_sha
        # And the copy is suppressed.
        names = [op.source.name for op in plan.companion_copies]
        assert "ISSUE-TEMPLATE.md" not in names

    def test_template_copied_when_diverges_from_canonical(self):
        (self.anchor / "ISSUE-TEMPLATE.md").write_text(
            OVERRIDE_TEMPLATE_BODY, encoding="utf-8"
        )
        plan = migrate.plan_migration(
            self.project, self.store,
            canonical_template_path=self.canonical,
        )
        assert plan.template_action == migrate.TEMPLATE_ACTION_COPY
        assert plan.template_local_sha != plan.template_canonical_sha
        # The override IS queued.
        names = [op.source.name for op in plan.companion_copies]
        assert "ISSUE-TEMPLATE.md" in names

    def test_store_root_template_overrides_homebrew_canonical(self):
        # When <store>/ISSUE-TEMPLATE.md exists, it is the canonical for
        # comparison -- the Homebrew generic is not consulted.
        (self.store / "ISSUE-TEMPLATE.md").write_text(
            OVERRIDE_TEMPLATE_BODY, encoding="utf-8"
        )
        # Local matches the store-root canonical (not the Homebrew one).
        (self.anchor / "ISSUE-TEMPLATE.md").write_text(
            OVERRIDE_TEMPLATE_BODY, encoding="utf-8"
        )
        plan = migrate.plan_migration(
            self.project, self.store,
            canonical_template_path=self.canonical,
        )
        assert plan.template_action == migrate.TEMPLATE_ACTION_SKIP_CANONICAL
        assert plan.template_canonical_path == self.store / "ISSUE-TEMPLATE.md"

    def test_no_canonical_anywhere_falls_through_to_copy(self):
        # No store-root template, no Homebrew canonical provided -- local
        # is treated as an override (cannot prove it is a duplicate).
        (self.anchor / "ISSUE-TEMPLATE.md").write_text(
            CANONICAL_TEMPLATE_BODY, encoding="utf-8"
        )
        plan = migrate.plan_migration(
            self.project, self.store,
            canonical_template_path=None,
        )
        assert plan.template_action == migrate.TEMPLATE_ACTION_COPY
        assert plan.template_canonical_path is None
        assert plan.template_canonical_sha is None
        names = [op.source.name for op in plan.companion_copies]
        assert "ISSUE-TEMPLATE.md" in names

    def test_apply_skips_writing_template_when_skip_canonical(self):
        (self.anchor / "ISSUE-TEMPLATE.md").write_text(
            CANONICAL_TEMPLATE_BODY, encoding="utf-8"
        )
        plan = migrate.plan_migration(
            self.project, self.store,
            canonical_template_path=self.canonical,
        )
        migrate.execute_migration(plan, dry_run=False)
        assert not (self.store / "MP" / "ISSUE-TEMPLATE.md").exists()
        # Issue copy still landed.
        assert (self.store / "MP" / "MP-001.md").is_file()
        # And the sentinel still wrote.
        assert (self.anchor / migrate.SENTINEL_NAME).is_file()

    def test_apply_writes_template_when_action_is_copy(self):
        (self.anchor / "ISSUE-TEMPLATE.md").write_text(
            OVERRIDE_TEMPLATE_BODY, encoding="utf-8"
        )
        plan = migrate.plan_migration(
            self.project, self.store,
            canonical_template_path=self.canonical,
        )
        migrate.execute_migration(plan, dry_run=False)
        dest = self.store / "MP" / "ISSUE-TEMPLATE.md"
        assert dest.is_file()
        assert dest.read_text(encoding="utf-8") == OVERRIDE_TEMPLATE_BODY

    def test_to_dict_template_section_shape(self):
        (self.anchor / "ISSUE-TEMPLATE.md").write_text(
            CANONICAL_TEMPLATE_BODY, encoding="utf-8"
        )
        plan = migrate.plan_migration(
            self.project, self.store,
            canonical_template_path=self.canonical,
        )
        d = plan.to_dict()
        assert "template" in d
        t = d["template"]
        assert t["action"] == migrate.TEMPLATE_ACTION_SKIP_CANONICAL
        assert t["local_path"] == str(self.anchor / "ISSUE-TEMPLATE.md")
        assert t["canonical_path"] == str(self.canonical)
        assert t["local_sha"] == t["canonical_sha"]

    def test_to_dict_template_absent_serializes_nulls(self):
        plan = migrate.plan_migration(
            self.project, self.store,
            canonical_template_path=self.canonical,
        )
        t = plan.to_dict()["template"]
        assert t["action"] == migrate.TEMPLATE_ACTION_ABSENT
        assert t["local_path"] is None
        assert t["local_sha"] is None


# ---------------------------------------------------------------- IM-054 ---
#
# .migrations record + two-commit flow.

class MigrationsRecordTests(MigrateTestBase):
    def setUp(self):
        super().setUp()
        (self.anchor / ".prefix").write_text("MP\n", encoding="utf-8")
        write_issue(self.anchor / "MP-001.md", id_="MP-001")
        write_issue(self.anchor / "MP-002.md", id_="MP-002")

    def test_migrations_file_appended_with_one_line(self):
        plan = migrate.plan_migration(self.project, self.store)
        migrate.execute_migration(plan, dry_run=False)
        migrations = self.store / migrate.MIGRATIONS_NAME
        assert migrations.is_file()
        lines = migrations.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1

    def test_migrations_line_has_five_tsv_fields(self):
        plan = migrate.plan_migration(self.project, self.store)
        migrate.execute_migration(plan, dry_run=False)
        line = (self.store / migrate.MIGRATIONS_NAME).read_text().strip()
        fields = line.split("\t")
        assert len(fields) == 5
        prefix, source_path, source_sha, store_sha, when = fields
        assert prefix == "MP"
        assert source_path == str(self.project.resolve())
        assert source_sha == self.source_pre_sha
        # store_sha is a real sha shape
        assert len(store_sha) == 40 and all(c in "0123456789abcdef" for c in store_sha)
        # ISO 8601 with timezone: contains a T and either +/- or Z near the end
        assert "T" in when

    def test_two_store_commits_after_migration(self):
        log_before = self._store_log_oneline()
        plan = migrate.plan_migration(self.project, self.store)
        migrate.execute_migration(plan, dry_run=False)
        log_after = self._store_log_oneline()
        assert len(log_after) == len(log_before) + 2
        # Newest first.
        assert log_after[0].split(" ", 1)[1].startswith(
            "chore: record .migrations entry for MP"
        )
        assert log_after[1].split(" ", 1)[1].startswith(
            "feat: migrate myproj to store ("
        )

    def test_migration_commit_sha_matches_recorded_sha(self):
        plan = migrate.plan_migration(self.project, self.store)
        result = migrate.execute_migration(plan, dry_run=False)
        line = (self.store / migrate.MIGRATIONS_NAME).read_text().strip()
        store_sha_in_line = line.split("\t")[3]
        assert result.migration_commit_sha == store_sha_in_line

    def test_canonical_source_path_resolves_symlinks(self):
        # Migrate via a symlinked path. The .migrations line should
        # record the realpath, not the symlink form.
        link = self.tmp / "via-link"
        link.symlink_to(self.project)
        plan = migrate.plan_migration(link, self.store)
        migrate.execute_migration(plan, dry_run=False)
        line = (self.store / migrate.MIGRATIONS_NAME).read_text().strip()
        recorded_path = line.split("\t")[1]
        assert recorded_path == os.path.realpath(str(link))
        assert recorded_path == str(self.project.resolve())

    def test_custom_message_used_for_migration_commit(self):
        plan = migrate.plan_migration(self.project, self.store)
        migrate.execute_migration(
            plan, dry_run=False,
            commit_message="feat: migrate myproj -- DOUG-039 Stage X",
        )
        log = self._store_log_oneline()
        # log[0] is the .migrations commit, log[1] is the migration commit.
        assert "DOUG-039 Stage X" in log[1]

    def test_partial_prior_migration_detected(self):
        # Run once successfully, then delete the sentinel and try to
        # re-run -- the partial-prior-migration check should refuse.
        plan = migrate.plan_migration(self.project, self.store)
        migrate.execute_migration(plan, dry_run=False)
        (self.anchor / migrate.SENTINEL_NAME).unlink()
        plan2 = migrate.plan_migration(self.project, self.store)
        with self.assertRaises(migrate.MigrationError) as cm:
            migrate.execute_migration(plan2, dry_run=False)
        assert "partial prior migration" in str(cm.exception).lower()

    def test_dry_run_writes_no_migrations_line_and_no_commits(self):
        log_before = self._store_log_oneline()
        plan = migrate.plan_migration(self.project, self.store)
        migrate.execute_migration(plan, dry_run=True)
        assert not (self.store / migrate.MIGRATIONS_NAME).exists()
        assert self._store_log_oneline() == log_before

    def test_unrelated_dirty_state_in_store_is_preserved(self):
        # Path-restricted commits should not sweep in unrelated changes.
        stray = self.store / "stray.txt"
        stray.write_text("unrelated\n", encoding="utf-8")
        plan = migrate.plan_migration(self.project, self.store)
        migrate.execute_migration(plan, dry_run=False)
        # File still on disk, still untracked.
        assert stray.is_file()
        r = subprocess.run(
            ["git", "-C", str(self.store), "ls-files", "--", "stray.txt"],
            check=True, capture_output=True, text=True, env=self._git_env,
        )
        assert r.stdout.strip() == ""  # not tracked


class CaptureSourceShaTests(MigrateTestBase):
    def test_no_git_repo_raises(self):
        # Override the source repo to a non-git dir.
        not_a_repo = self.tmp / "bare-dir"
        not_a_repo.mkdir()
        with self.assertRaises(migrate.MigrationError) as cm:
            migrate.capture_source_pre_migration_sha(not_a_repo)
        assert "not a git repository" in str(cm.exception).lower()

    def test_returns_current_head_sha(self):
        sha = migrate.capture_source_pre_migration_sha(self.project)
        assert sha == self.source_pre_sha


if __name__ == "__main__":
    unittest.main()
