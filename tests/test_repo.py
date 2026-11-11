#!/usr/bin/env python3
"""Unit tests for lib/repo.py (helper; coverage gap closed).

resolve_main_basename() shells out to `git rev-parse --git-common-dir`
in the *current working directory*, so the real-git tests here build a
temp repo (with a repo-local identity, per the hermeticity
lesson) and chdir into it. The two failure paths that cannot be staged
on disk cheaply (git missing from PATH; main checkout deleted while its
linked worktree survives) are exercised by mocking subprocess.run.

End-to-end CLI behavior (new-issue / init-issue-project resolving repo
identity from linked worktrees) lives in
tests/verification/im-090-repo-name-worktree.sh.

Run:
    python3 -m unittest tests.test_repo -v
"""

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

from fixture_identity import FIXTURE_EMAIL  # noqa: E402

import repo  # noqa: E402


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_repo_with_commit(path):
    _git("init", "-b", "main", cwd=path)
    _git("config", "user.email", FIXTURE_EMAIL, cwd=path)
    _git("config", "user.name", "Test", cwd=path)
    _git("commit", "--allow-empty", "-m", "initial", cwd=path)


class ChdirTestCase(unittest.TestCase):
    """Base: temp dir + cwd save/restore."""

    def setUp(self):
        self._orig_cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()

    def tearDown(self):
        os.chdir(self._orig_cwd)
        self._tmp.cleanup()


class TestResolveMainBasenameRealGit(ChdirTestCase):
    def test_primary_checkout_returns_repo_basename(self):
        main = self.tmp / "myrepo"
        main.mkdir()
        _init_repo_with_commit(main)
        os.chdir(main)
        self.assertEqual(repo.resolve_main_basename(), "myrepo")

    def test_primary_checkout_subdirectory_returns_repo_basename(self):
        main = self.tmp / "myrepo"
        sub = main / "a" / "b"
        sub.mkdir(parents=True)
        _init_repo_with_commit(main)
        os.chdir(sub)
        self.assertEqual(repo.resolve_main_basename(), "myrepo")

    def test_linked_worktree_returns_main_basename_not_worktree_basename(self):
        main = self.tmp / "myrepo"
        main.mkdir()
        _init_repo_with_commit(main)
        wt = self.tmp / "issue-999-linked"
        _git("worktree", "add", str(wt), "-b", "issue-999-linked", cwd=main)
        os.chdir(wt)
        self.assertEqual(repo.resolve_main_basename(), "myrepo")

    def test_linked_worktree_subdirectory_returns_main_basename(self):
        main = self.tmp / "myrepo"
        main.mkdir()
        _init_repo_with_commit(main)
        wt = self.tmp / "issue-998-linked"
        _git("worktree", "add", str(wt), "-b", "issue-998-linked", cwd=main)
        sub = wt / "nested"
        sub.mkdir()
        os.chdir(sub)
        self.assertEqual(repo.resolve_main_basename(), "myrepo")

    def test_outside_any_git_repo_returns_none(self):
        bare_dir = self.tmp / "not-a-repo"
        bare_dir.mkdir()
        os.chdir(bare_dir)
        env_patch = mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(self.tmp)})
        with env_patch:
            self.assertIsNone(repo.resolve_main_basename())


class TestResolveMainBasenameMocked(unittest.TestCase):
    def test_git_not_on_path_returns_none(self):
        with mock.patch.object(repo.subprocess, "run", side_effect=FileNotFoundError):
            self.assertIsNone(repo.resolve_main_basename())

    def test_deleted_main_checkout_returns_none_and_writes_diagnostic(self):
        fake = mock.Mock()
        fake.stdout = "/nonexistent-main-checkout/myrepo/.git\n"
        stderr = io.StringIO()
        with mock.patch.object(repo.subprocess, "run", return_value=fake):
            with contextlib.redirect_stderr(stderr):
                self.assertIsNone(repo.resolve_main_basename())
        self.assertIn("main checkout has been deleted", stderr.getvalue())
        self.assertIn("--repo NAME", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
