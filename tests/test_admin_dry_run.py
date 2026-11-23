#!/usr/bin/env python3
"""Unit tests for the IM-259 --dry-run previews on the three multi-file
admin verbs (surface.preview-not-confirmation).

The contract under test: each preview emits the SHAPE the real run would
emit -- the same structured result, counts and paths naming exactly what
the real run would write -- and writes nothing: no file write, no commit,
no index regeneration. The preview is distinguishable from a real run
under --json via the unconditional `dry_run` boolean (the same marker
`admin init-store` and `admin reap` already carry).

End-to-end byte-identity over the real CLI is asserted in
tests/verification/im-259-admin-dry-run.sh; these tests pin the library
seams directly.

Run:
    python3 -m unittest tests.test_admin_dry_run -v
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))

import reindex_core  # noqa: E402
from cli import admin_install_skills, admin_snapshot  # noqa: E402

from tests.fixture_identity import FIXTURE_EMAIL  # noqa: E402

ISSUE_MD = """\
---
id: IM-001
title: "dry-run fixture"
type: chore
status: open
priority: low
created: 2026-01-01
updated: 2026-01-01
---
## Summary

fixture body
"""


def _tree_snapshot(root: Path):
    """{relative path: bytes} for every file under root."""
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _git(store, *argv):
    return subprocess.run(
        ["git", "-C", str(store), *argv],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _mk_git_store(root: Path):
    (root / "IM").mkdir(parents=True)
    (root / "IM" / "IM-001.md").write_text(ISSUE_MD)
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    _git(root, "config", "user.email", FIXTURE_EMAIL)
    _git(root, "config", "user.name", "test")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init store")


class TestReindexStoreDryRun(unittest.TestCase):
    def test_dry_run_computes_without_writing(self):
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / "store"
            (store / "IM").mkdir(parents=True)
            (store / "IM" / "IM-001.md").write_text(ISSUE_MD)
            before = _tree_snapshot(store)

            result = reindex_core.reindex_store_detail(store, dry_run=True)

            self.assertEqual(_tree_snapshot(store), before)
            self.assertFalse((store / "INDEX.md").exists())
            self.assertFalse((store / "IM" / "INDEX.md").exists())
            self.assertEqual(result["issues"], 1)
            self.assertEqual(result["indexes_written"], 2)
            self.assertTrue(result["summary"].startswith("Would reindex "))

    def test_dry_run_counts_match_the_real_run(self):
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / "store"
            (store / "IM").mkdir(parents=True)
            (store / "IM" / "IM-001.md").write_text(ISSUE_MD)

            preview = reindex_core.reindex_store_detail(store, dry_run=True)
            real = reindex_core.reindex_store_detail(
                store, suppress_commit=True
            )

            for key in ("mode", "store", "index", "prefixes",
                        "indexes_written", "issues"):
                self.assertEqual(preview[key], real[key], key)
            self.assertTrue((store / "INDEX.md").exists())
            self.assertTrue((store / "IM" / "INDEX.md").exists())

    def test_dry_run_takes_no_lock_and_makes_no_commit(self):
        # A preview must not serialize against writers or touch git;
        # pin that by asserting neither primitive is entered at all.
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / "store"
            (store / "IM").mkdir(parents=True)
            (store / "IM" / "IM-001.md").write_text(ISSUE_MD)
            with mock.patch.object(
                reindex_core.concurrency, "index_lock"
            ) as lock, mock.patch.object(
                reindex_core.concurrency, "store_mutation"
            ) as mut:
                reindex_core.reindex_store_detail(store, dry_run=True)
            lock.assert_not_called()
            mut.assert_not_called()


class TestReindexLegacyDryRun(unittest.TestCase):
    def test_dry_run_computes_without_writing(self):
        with tempfile.TemporaryDirectory() as td:
            issues = Path(td) / ".issues"
            issues.mkdir()
            (issues / "IM-001.md").write_text(ISSUE_MD)
            before = _tree_snapshot(issues)

            result = reindex_core.reindex_legacy_dir_detail(
                issues, dry_run=True
            )

            self.assertEqual(_tree_snapshot(issues), before)
            self.assertFalse((issues / "INDEX.md").exists())
            self.assertEqual(result["issues"], 1)
            self.assertTrue(result["summary"].startswith("Would reindex "))

            real = reindex_core.reindex_legacy_dir_detail(issues)
            self.assertTrue((issues / "INDEX.md").exists())
            for key in ("mode", "issues_dir", "index",
                        "indexes_written", "issues"):
                self.assertEqual(result[key], real[key], key)


class TestSnapshotDryRun(unittest.TestCase):
    def _run(self, store, dry_run):
        args = Namespace(
            store=str(store), dry_run=dry_run, json_mode=True,
            commit_timeout=5.0,
        )
        out = io.StringIO()
        with redirect_stdout(out):
            rc = admin_snapshot.run(args)
        return rc, json.loads(out.getvalue())

    def test_dry_run_names_the_pending_set_and_commits_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / "store"
            _mk_git_store(store)
            with open(store / "IM" / "IM-001.md", "a") as f:
                f.write("pending edit\n")
            head0 = _git(store, "rev-parse", "HEAD")
            before = _tree_snapshot(store / "IM")

            rc, payload = self._run(store, dry_run=True)

            self.assertEqual(rc, 0)
            self.assertEqual(payload["dry_run"], True)
            self.assertEqual(payload["committed"], False)
            self.assertIsNone(payload["commit"])
            self.assertEqual(payload["paths"], ["IM/IM-001.md"])
            self.assertEqual(_git(store, "rev-parse", "HEAD"), head0)
            self.assertEqual(_tree_snapshot(store / "IM"), before)

            # The real run commits exactly the previewed set.
            rc, real = self._run(store, dry_run=False)
            self.assertEqual(rc, 0)
            self.assertEqual(real["dry_run"], False)
            self.assertEqual(real["committed"], True)
            self.assertEqual(real["paths"], payload["paths"])
            self.assertNotEqual(_git(store, "rev-parse", "HEAD"), head0)

    def test_dry_run_on_a_clean_store_matches_the_real_shape(self):
        with tempfile.TemporaryDirectory() as td:
            store = Path(td) / "store"
            _mk_git_store(store)
            rc, payload = self._run(store, dry_run=True)
            self.assertEqual(rc, 0)
            self.assertEqual(
                payload,
                {
                    "store": str(store), "committed": False,
                    "commit": None, "paths": [], "dry_run": True,
                },
            )


class TestInstallSkillsDryRun(unittest.TestCase):
    def _fixture(self, td):
        prefix = Path(td) / "brew-prefix"
        skills = prefix / "share" / "issue-mgmt" / "skills"
        skills.mkdir(parents=True)
        (skills / "im259-a.md").write_text("skill a\n")
        (prefix / "share" / "issue-mgmt" / "CHEATSHEET.md").write_text("cs\n")
        home = Path(td) / "home"
        home.mkdir()
        return prefix, home

    def _run(self, prefix, home, dry_run):
        args = Namespace(json_mode=True, dry_run=dry_run)
        fake_brew = mock.Mock(stdout=f"{prefix}\n")
        out = io.StringIO()
        with mock.patch.object(
            admin_install_skills.shutil, "which", return_value="/usr/bin/brew"
        ), mock.patch.object(
            admin_install_skills.subprocess, "run", return_value=fake_brew
        ), mock.patch.dict(os.environ, {"HOME": str(home)}):
            with redirect_stdout(out):
                rc = admin_install_skills.run(args)
        return rc, json.loads(out.getvalue())

    def test_dry_run_writes_nothing_and_flags_new_installs(self):
        with tempfile.TemporaryDirectory() as td:
            prefix, home = self._fixture(td)

            rc, payload = self._run(prefix, home, dry_run=True)

            self.assertEqual(rc, 0)
            self.assertEqual(payload["dry_run"], True)
            self.assertEqual(payload["count"], 1)
            self.assertEqual(
                payload["installed"],
                [{
                    "name": "im259-a",
                    "path": str(home / ".claude" / "skills" / "im259-a"
                                / "SKILL.md"),
                    "existed": False,
                }],
            )
            self.assertFalse((home / ".claude").exists())

    def test_dry_run_flags_overwrites_after_a_real_install(self):
        with tempfile.TemporaryDirectory() as td:
            prefix, home = self._fixture(td)

            rc, real = self._run(prefix, home, dry_run=False)
            self.assertEqual(rc, 0)
            self.assertEqual(real["dry_run"], False)
            installed = home / ".claude" / "skills" / "im259-a" / "SKILL.md"
            self.assertTrue(installed.is_file())
            before = _tree_snapshot(home)

            rc, payload = self._run(prefix, home, dry_run=True)
            self.assertEqual(rc, 0)
            self.assertEqual(payload["installed"][0]["existed"], True)
            self.assertEqual(_tree_snapshot(home), before)


if __name__ == "__main__":
    unittest.main()
