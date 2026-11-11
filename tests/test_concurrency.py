#!/usr/bin/env python3
"""Unit tests for lib/concurrency.py.

Covers the pure helpers (atomic_write, capture_state, verify_unchanged,
StateSnapshot encode/decode, flock_acquire uncontended) under unittest.
Race / stale-lock / torn-write scenarios that need real subprocesses
live in tests/verification/concurrency.sh.

Run:
    python3 -m unittest tests.test_concurrency -v
"""

import contextlib
import errno
import fcntl
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

from fixture_identity import FIXTURE_EMAIL  # noqa: E402

import concurrency  # noqa: E402
import errors  # noqa: E402  -- path set above


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="im022-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestAtomicWrite(TempDirTest):
    def test_writes_str_content(self):
        target = self.tmp / "out.txt"
        concurrency.atomic_write(target, "hello\n")
        self.assertEqual(target.read_text(), "hello\n")

    def test_writes_bytes_content(self):
        target = self.tmp / "out.bin"
        concurrency.atomic_write(target, b"\x00\x01\x02")
        self.assertEqual(target.read_bytes(), b"\x00\x01\x02")

    def test_overwrites_existing(self):
        target = self.tmp / "out.txt"
        target.write_text("old")
        concurrency.atomic_write(target, "new")
        self.assertEqual(target.read_text(), "new")

    def test_creates_parent_dirs(self):
        target = self.tmp / "a" / "b" / "out.txt"
        concurrency.atomic_write(target, "x")
        self.assertEqual(target.read_text(), "x")

    def test_no_temp_files_left_on_success(self):
        target = self.tmp / "out.txt"
        concurrency.atomic_write(target, "x")
        leftover = [p for p in self.tmp.iterdir() if p.name.startswith(".out.txt.tmp.")]
        self.assertEqual(leftover, [])

    def test_no_temp_files_left_on_exception(self):
        target = self.tmp / "out.txt"
        original = os.replace

        def boom(src, dst):
            raise OSError("simulated")

        os.replace = boom
        try:
            with self.assertRaises(OSError):
                concurrency.atomic_write(target, "x")
        finally:
            os.replace = original
        leftover = [p for p in self.tmp.iterdir() if p.name.startswith(".out.txt.tmp.")]
        self.assertEqual(leftover, [])
        self.assertFalse(target.exists())


class TestStateSnapshot(TempDirTest):
    def test_capture_existing_file(self):
        path = self.tmp / "f.txt"
        path.write_text("abc")
        snap = concurrency.capture_state(path)
        self.assertEqual(snap.size, 3)
        self.assertEqual(
            snap.sha256,
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        )
        self.assertFalse(snap.is_absent())

    def test_capture_missing_file(self):
        snap = concurrency.capture_state(self.tmp / "nope.txt")
        self.assertTrue(snap.is_absent())

    def test_encode_decode_roundtrip(self):
        path = self.tmp / "f.txt"
        path.write_text("abc")
        snap = concurrency.capture_state(path)
        decoded = concurrency.StateSnapshot.decode(snap.encode())
        self.assertEqual(snap, decoded)

    def test_encode_decode_absent(self):
        absent = concurrency.StateSnapshot.absent()
        self.assertEqual(absent.encode(), "absent")
        self.assertTrue(concurrency.StateSnapshot.decode("absent").is_absent())


class TestVerifyUnchanged(TempDirTest):
    def test_passes_on_identical_file(self):
        path = self.tmp / "f.txt"
        path.write_text("abc")
        snap = concurrency.capture_state(path)
        concurrency.verify_unchanged(path, snap)  # no exception

    def test_raises_on_content_change(self):
        path = self.tmp / "f.txt"
        path.write_text("abc")
        snap = concurrency.capture_state(path)
        # Sleep briefly to ensure mtime advances, then overwrite via a method
        # that produces a different size+sha than the snapshot.
        time.sleep(0.01)
        path.write_text("XYZQ")
        with self.assertRaises(errors.ConcurrentEdit):
            concurrency.verify_unchanged(path, snap)

    def test_raises_on_mtime_change_only(self):
        path = self.tmp / "f.txt"
        path.write_text("abc")
        snap = concurrency.capture_state(path)
        # Bump mtime without changing content -- ConcurrentEdit still raises
        # because the snapshot is (mtime, size, sha) tuple equality.
        future = (snap.mtime_ns // 1_000_000_000) + 5
        os.utime(path, (future, future))
        with self.assertRaises(errors.ConcurrentEdit):
            concurrency.verify_unchanged(path, snap)

    def test_raises_on_disappeared_file(self):
        path = self.tmp / "f.txt"
        path.write_text("abc")
        snap = concurrency.capture_state(path)
        path.unlink()
        with self.assertRaises(errors.ConcurrentEdit):
            concurrency.verify_unchanged(path, snap)

    def test_raises_on_appeared_file(self):
        path = self.tmp / "f.txt"
        snap = concurrency.capture_state(path)  # absent
        path.write_text("abc")
        with self.assertRaises(errors.ConcurrentEdit):
            concurrency.verify_unchanged(path, snap)


class TestFlockAcquire(TempDirTest):
    def test_acquires_uncontended(self):
        lock = self.tmp / "a.lock"
        with concurrency.flock_acquire(lock, timeout=1.0):
            self.assertTrue(lock.exists())
            self.assertEqual(lock.read_text().strip(), str(os.getpid()))

    def test_can_reacquire_after_release(self):
        lock = self.tmp / "a.lock"
        with concurrency.flock_acquire(lock, timeout=1.0):
            pass
        with concurrency.flock_acquire(lock, timeout=1.0):
            pass

    def test_timeout_raises_concurrent_edit_with_contender_pid(self):
        lock = self.tmp / "a.lock"
        # Spawn a child that holds the lock for 3s.
        py = REPO_ROOT / "lib" / "concurrency.py"
        sleeper = subprocess.Popen(
            [
                sys.executable,
                str(py),
                "flock-run",
                str(lock),
                "--timeout",
                "1",
                "--",
                "sleep",
                "3",
            ]
        )
        try:
            # Give the child time to acquire.
            time.sleep(0.5)
            t0 = time.monotonic()
            with self.assertRaises(errors.ConcurrentEdit) as cm:
                with concurrency.flock_acquire(lock, timeout=0.5, poll=0.05):
                    pass
            elapsed = time.monotonic() - t0
            self.assertLess(elapsed, 2.0, "timeout should fire within budget")
            self.assertEqual(cm.exception.contender_pid, sleeper.pid)
            self.assertIn("concurrent-edit", cm.exception.format_stderr())
        finally:
            sleeper.terminate()
            sleeper.wait(timeout=5)

    def test_creates_lock_directory(self):
        lock = self.tmp / "nested" / "dir" / "a.lock"
        with concurrency.flock_acquire(lock, timeout=1.0):
            self.assertTrue(lock.exists())


class TestConcurrentEditFormat(unittest.TestCase):
    """ConcurrentEdit stderr is the key=value contract line
    (design Section 4.6); `--json` opt-in emits a one-line JSON object.
    """

    def test_stderr_format_with_pid(self):
        err = errors.ConcurrentEdit(
            "lock timeout after 5.0s",
            resource="IM-022",
            contender_pid=12345,
        )
        line = err.format_stderr("new-issue")
        self.assertIn("new-issue: error=concurrent-edit", line)
        self.assertIn("resource=IM-022", line)
        self.assertIn('detail="lock timeout after 5.0s"', line)
        self.assertIn("contender_pid=12345", line)

    def test_stderr_format_without_pid(self):
        err = errors.ConcurrentEdit("file changed since read", resource="path/x")
        line = err.format_stderr()
        self.assertIn("error=concurrent-edit", line)
        self.assertNotIn("contender_pid", line)

    def test_stderr_format_without_resource(self):
        err = errors.ConcurrentEdit("lock timeout after 5.0s")
        line = err.format_stderr()
        self.assertIn("error=concurrent-edit", line)
        self.assertNotIn("resource=", line)

    def test_stderr_json_mode(self):
        import json as _json
        err = errors.ConcurrentEdit(
            "lock timeout after 5.0s",
            resource="IM-022",
            contender_pid=12345,
        )
        line = err.format_stderr("new-issue", json_mode=True)
        obj = _json.loads(line)
        self.assertEqual(obj["exit"], 3)
        self.assertEqual(obj["class"], "concurrent-edit")
        self.assertEqual(obj["error"], "concurrent-edit")
        self.assertEqual(obj["resource"], "IM-022")
        self.assertEqual(obj["contender_pid"], 12345)


class TestStoreCommitErrorFormat(unittest.TestCase):
    """StoreCommitError stderr distinguishes pre-write (catchup
    sweep) failures from post-write (commit_after_write) failures so the
    user is not misled into looking for an artifact that does not exist.
    """

    def test_post_write_default_suffix_unchanged(self):
        err = concurrency.StoreCommitError(
            "git commit failed: fatal: disk full", git_error="fatal: disk full"
        )
        line = err.format_stderr("edit-issue")
        self.assertIn("edit-issue: store commit failed", line)
        self.assertIn("fatal: disk full", line)
        self.assertIn("file is on disk; fix and rerun any mutation to recover", line)
        self.assertNotIn("no changes were written", line)

    def test_pre_write_suffix_replaces_file_on_disk_claim(self):
        err = concurrency.StoreCommitError(
            "git status failed: fatal: not a git repository",
            git_error="fatal: not a git repository",
            write_attempted=False,
        )
        line = err.format_stderr("edit-issue")
        self.assertIn("edit-issue: store commit failed", line)
        self.assertIn("fatal: not a git repository", line)
        self.assertIn("no changes were written; fix the store and retry", line)
        self.assertNotIn("file is on disk", line)

    def test_write_attempted_defaults_true(self):
        err = concurrency.StoreCommitError("x")
        self.assertTrue(err.write_attempted)

    def test_write_attempted_kwarg_accepts_false(self):
        err = concurrency.StoreCommitError("x", write_attempted=False)
        self.assertFalse(err.write_attempted)


class TestCli(TempDirTest):
    def test_snapshot_and_verify(self):
        path = self.tmp / "f.txt"
        path.write_text("abc")
        py = REPO_ROOT / "lib" / "concurrency.py"
        snap = subprocess.check_output(
            [sys.executable, str(py), "snapshot", str(path)],
            text=True,
        ).strip()
        rc = subprocess.run(
            [sys.executable, str(py), "verify", str(path), snap]
        ).returncode
        self.assertEqual(rc, 0)
        time.sleep(0.01)
        path.write_text("XYZQ")
        rc2 = subprocess.run(
            [sys.executable, str(py), "verify", str(path), snap],
            stderr=subprocess.DEVNULL,
        ).returncode
        self.assertEqual(rc2, errors.EXIT_CONCURRENT_EDIT)

    def test_atomic_write_from_stdin(self):
        target = self.tmp / "out.txt"
        py = REPO_ROOT / "lib" / "concurrency.py"
        subprocess.run(
            [sys.executable, str(py), "atomic-write", str(target)],
            input=b"hello\n",
            check=True,
        )
        self.assertEqual(target.read_text(), "hello\n")

    def test_flock_run_propagates_child_exit_code(self):
        lock = self.tmp / "a.lock"
        py = REPO_ROOT / "lib" / "concurrency.py"
        rc = subprocess.run(
            [sys.executable, str(py), "flock-run", str(lock), "--", "false"]
        ).returncode
        self.assertEqual(rc, 1)


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True
    )


class GitStoreTest(TempDirTest):
    """Base fixture: a temp git-initialized store with one baseline commit.

    in-process unit tests for the git-backed helpers
    (catchup_sweep, commit_after_write, _git_status_pending_tracked_paths,
    _hook_likely_present, git_show, store_mutation) run against this
    fixture directly -- no CLI subprocess seam.
    """

    def setUp(self):
        super().setUp()
        self.store = self.tmp / "store"
        self.store.mkdir()
        _git(self.store, "init", "-q")
        _git(self.store, "config", "user.email", FIXTURE_EMAIL)
        _git(self.store, "config", "user.name", "Test")
        (self.store / "INDEX.md").write_text("# index\n")
        _git(self.store, "add", "INDEX.md")
        _git(self.store, "commit", "-q", "-m", "baseline")

    def head(self):
        return _git(self.store, "rev-parse", "HEAD").stdout.strip()

    def write_issue(self, rel="IM/IM-001.md", text="body\n"):
        p = self.store / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def add_rejecting_hook(self):
        hook = self.store / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        return hook


class TestLockPathHelpers(TempDirTest):
    def test_issue_lock_path_shape(self):
        p = concurrency.issue_lock_path(self.tmp, "IM", "IM-001")
        self.assertEqual(p, self.tmp / "IM" / ".locks" / "IM-001.lock")

    def test_index_lock_path_shape(self):
        p = concurrency.index_lock_path(self.tmp)
        self.assertEqual(p, self.tmp / ".locks" / "INDEX.lock")

    def test_registry_lock_path_shape(self):
        p = concurrency.registry_lock_path(self.tmp)
        self.assertEqual(p, self.tmp / ".locks" / ".repos.lock")

    def test_store_commit_lock_path_shape(self):
        p = concurrency.store_commit_lock_path(self.tmp)
        self.assertEqual(p, self.tmp / ".locks" / "store-commit.lock")

    def test_issue_lock_acquires_and_writes_pid(self):
        with concurrency.issue_lock(self.tmp, "IM", "IM-001", timeout=1.0):
            lock = concurrency.issue_lock_path(self.tmp, "IM", "IM-001")
            self.assertEqual(lock.read_text().strip(), str(os.getpid()))

    def test_index_lock_acquires(self):
        with concurrency.index_lock(self.tmp, timeout=1.0):
            self.assertTrue(concurrency.index_lock_path(self.tmp).exists())

    def test_registry_lock_acquires(self):
        with concurrency.registry_lock(self.tmp, timeout=1.0):
            self.assertTrue(concurrency.registry_lock_path(self.tmp).exists())


class TestStoreCommitLock(TempDirTest):
    def test_acquires_uncontended(self):
        entered = False
        with concurrency.store_commit_lock(self.tmp, timeout=1.0):
            entered = True
            self.assertTrue(concurrency.store_commit_lock_path(self.tmp).exists())
        self.assertTrue(entered)

    def test_contended_retries_once_then_raises_concurrent_edit(self):
        lock_path = concurrency.store_commit_lock_path(self.tmp)
        acquire_calls = []
        real_acquire = concurrency.flock_acquire

        def counting_acquire(path, **kw):
            acquire_calls.append(path)
            return real_acquire(path, **kw)

        # Hold the lock from a second fd in this process; flock scopes to
        # the open file description, so the inner acquisition times out.
        with real_acquire(lock_path, timeout=1.0):
            with mock.patch.object(
                concurrency, "flock_acquire", side_effect=counting_acquire
            ):
                with self.assertRaises(errors.ConcurrentEdit):
                    with concurrency.store_commit_lock(self.tmp, timeout=0.1):
                        self.fail("body must not run when lock is contended")
        self.assertEqual(len(acquire_calls), 2, "one retry before raising")

    def test_body_exception_propagates_without_retry(self):
        with self.assertRaises(RuntimeError):
            with concurrency.store_commit_lock(self.tmp, timeout=1.0):
                raise RuntimeError("body failure")


class TestFlockAcquireErrorPaths(TempDirTest):
    def test_non_wouldblock_oserror_reraises(self):
        lock = self.tmp / "a.lock"

        def eperm(fd, op):
            raise OSError(errno.EPERM, "operation not permitted")

        with mock.patch.object(concurrency.fcntl, "flock", side_effect=eperm):
            with self.assertRaises(OSError) as cm:
                with concurrency.flock_acquire(lock, timeout=0.2):
                    pass
        self.assertEqual(cm.exception.errno, errno.EPERM)

    def test_unlock_oserror_is_swallowed(self):
        lock = self.tmp / "a.lock"
        real_flock = fcntl.flock

        def flaky(fd, op):
            if op == fcntl.LOCK_UN:
                raise OSError(errno.EIO, "simulated unlock failure")
            return real_flock(fd, op)

        with mock.patch.object(concurrency.fcntl, "flock", side_effect=flaky):
            with concurrency.flock_acquire(lock, timeout=1.0):
                pass  # exiting the context must not raise


class TestReadContenderPid(unittest.TestCase):
    def test_oserror_returns_none(self):
        fh = mock.Mock()
        fh.seek.side_effect = OSError("gone")
        self.assertIsNone(concurrency._read_contender_pid(fh))

    def test_non_digit_content_returns_none(self):
        fh = io.StringIO("not-a-pid\n")
        self.assertIsNone(concurrency._read_contender_pid(fh))

    def test_digit_content_returns_int(self):
        fh = io.StringIO("4242")
        self.assertEqual(concurrency._read_contender_pid(fh), 4242)


class TestAtomicWriteTmpCleanupRace(TempDirTest):
    def test_tmp_already_gone_on_failure_is_tolerated(self):
        target = self.tmp / "out.txt"

        def replace_after_deleting_tmp(src, dst):
            os.unlink(src)  # simulate the tmp vanishing before cleanup
            raise OSError("simulated replace failure")

        with mock.patch.object(
            concurrency.os, "replace", side_effect=replace_after_deleting_tmp
        ):
            with self.assertRaises(OSError):
                concurrency.atomic_write(target, "x")
        self.assertFalse(target.exists())


class TestRunGitRaw(GitStoreTest):
    def test_success_returns_stdout(self):
        rc, out, err = concurrency._run_git_raw(self.store, ["rev-parse", "HEAD"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, self.head())

    def test_git_missing_returns_127(self):
        with mock.patch.object(
            concurrency.subprocess, "run", side_effect=FileNotFoundError()
        ):
            rc, out, err = concurrency._run_git_raw(self.store, ["status"])
        self.assertEqual(rc, 127)
        self.assertIn("git not installed", err)


class TestGitShow(GitStoreTest):
    def test_returns_bytes_at_ref(self):
        content = concurrency.git_show(self.store, "HEAD", "INDEX.md")
        self.assertEqual(content, b"# index\n")

    def test_unresolvable_ref_raises_value_error(self):
        with self.assertRaises(ValueError):
            concurrency.git_show(self.store, "no-such-ref", "INDEX.md")

    def test_missing_path_at_ref_raises_file_not_found(self):
        with self.assertRaises(FileNotFoundError):
            concurrency.git_show(self.store, "HEAD", "IM/absent.md")

    def test_git_missing_at_show_raises_value_error(self):
        with mock.patch.object(
            concurrency, "_run_git_raw", return_value=(0, "deadbeef", "")
        ), mock.patch.object(
            concurrency.subprocess, "run", side_effect=FileNotFoundError()
        ):
            with self.assertRaises(ValueError):
                concurrency.git_show(self.store, "HEAD", "INDEX.md")


class TestHookLikelyPresent(GitStoreTest):
    def test_no_hook_returns_false(self):
        self.assertFalse(concurrency._hook_likely_present(self.store))

    def test_executable_default_hook_returns_true(self):
        self.add_rejecting_hook()
        self.assertTrue(concurrency._hook_likely_present(self.store))

    def test_non_executable_hook_returns_false(self):
        hook = self.add_rejecting_hook()
        hook.chmod(0o644)
        self.assertFalse(concurrency._hook_likely_present(self.store))

    def test_absolute_hooks_path_config_is_resolved(self):
        hooks_dir = self.tmp / "custom-hooks"
        hooks_dir.mkdir()
        hook = hooks_dir / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        _git(self.store, "config", "core.hooksPath", str(hooks_dir))
        self.assertTrue(concurrency._hook_likely_present(self.store))

    def test_relative_hooks_path_config_is_resolved(self):
        hooks_dir = self.store / "my-hooks"
        hooks_dir.mkdir()
        hook = hooks_dir / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        _git(self.store, "config", "core.hooksPath", "my-hooks")
        self.assertTrue(concurrency._hook_likely_present(self.store))

    def test_oserror_probing_hook_returns_false(self):
        self.add_rejecting_hook()
        with mock.patch.object(
            concurrency.os, "access", side_effect=OSError("probe failed")
        ):
            self.assertFalse(concurrency._hook_likely_present(self.store))


class TestGitStatusPendingTrackedPaths(GitStoreTest):
    def test_clean_store_returns_empty(self):
        self.assertEqual(
            concurrency._git_status_pending_tracked_paths(self.store), []
        )

    def test_tracked_paths_returned_untracked_junk_ignored(self):
        self.write_issue("IM/IM-001.md")
        (self.store / "INDEX.md").write_text("# changed\n")
        (self.store / "scratch.txt").write_text("user junk\n")
        (self.store / ".hidden").write_text("hidden\n")
        paths = concurrency._git_status_pending_tracked_paths(self.store)
        self.assertEqual(sorted(paths), ["IM/IM-001.md", "INDEX.md"])

    def test_locks_dir_entries_ignored(self):
        locks = self.store / "IM" / ".locks"
        locks.mkdir(parents=True)
        (locks / "IM-001.lock").write_text("123")
        self.assertEqual(
            concurrency._git_status_pending_tracked_paths(self.store), []
        )

    def test_rename_entry_reports_new_path(self):
        issue = self.write_issue("IM/IM-001.md")
        _git(self.store, "add", "IM/IM-001.md")
        _git(self.store, "commit", "-q", "-m", "add issue")
        _git(self.store, "mv", "IM/IM-001.md", "IM/IM-002.md")
        paths = concurrency._git_status_pending_tracked_paths(self.store)
        self.assertIn("IM/IM-002.md", paths)
        self.assertNotIn("IM/IM-001.md", paths)
        self.assertFalse(issue.exists())

    def test_non_repo_raises_pre_write_store_commit_error(self):
        plain = self.tmp / "not-a-repo"
        plain.mkdir()
        with self.assertRaises(concurrency.StoreCommitError) as cm:
            concurrency._git_status_pending_tracked_paths(plain)
        self.assertFalse(cm.exception.write_attempted)


class TestPendingTrackedPaths(GitStoreTest):
    """The public read `issue admin snapshot --json` reports from (IM-240).

    The sweep itself returns only a commit sha, so `paths` in the snapshot
    document comes from reading the pending set just before the sweep.
    """

    def test_clean_store_returns_empty(self):
        self.assertEqual(concurrency.pending_tracked_paths(self.store), [])

    def test_matches_the_set_the_sweep_claims(self):
        self.write_issue("IM/IM-001.md")
        (self.store / "INDEX.md").write_text("# changed\n")
        reported = sorted(concurrency.pending_tracked_paths(self.store))
        self.assertEqual(reported, ["IM/IM-001.md", "INDEX.md"])
        self.assertIsNotNone(concurrency.catchup_sweep(self.store))
        # Post-sweep the set is empty, so the pre-sweep read is the only
        # moment the swept paths are observable.
        self.assertEqual(concurrency.pending_tracked_paths(self.store), [])

    def test_non_repo_raises_pre_write_store_commit_error(self):
        plain = self.tmp / "not-a-repo-public"
        plain.mkdir()
        with self.assertRaises(concurrency.StoreCommitError) as cm:
            concurrency.pending_tracked_paths(plain)
        self.assertFalse(cm.exception.write_attempted)


class TestCatchupSweep(GitStoreTest):
    def test_clean_store_returns_none(self):
        self.assertIsNone(concurrency.catchup_sweep(self.store))

    def test_dirty_tracked_path_commits_and_returns_sha(self):
        self.write_issue("IM/IM-001.md")
        before = self.head()
        sha = concurrency.catchup_sweep(self.store)
        self.assertIsNotNone(sha)
        self.assertNotEqual(sha, before)
        self.assertEqual(sha, self.head())
        # Working tree is clean afterwards.
        self.assertEqual(
            concurrency._git_status_pending_tracked_paths(self.store), []
        )
        log = _git(self.store, "log", "-1", "--format=%s").stdout.strip()
        self.assertEqual(log, concurrency.CATCHUP_COMMIT_MESSAGE)

    def test_exclude_leaves_caller_paths_pending(self):
        self.write_issue("IM/IM-001.md")
        before = self.head()
        sha = concurrency.catchup_sweep(self.store, exclude=["IM/IM-001.md"])
        self.assertIsNone(sha)
        self.assertEqual(self.head(), before)
        self.assertEqual(
            concurrency._git_status_pending_tracked_paths(self.store),
            ["IM/IM-001.md"],
        )

    def test_hook_rejection_warns_and_returns_none(self):
        self.write_issue("IM/IM-001.md")
        self.add_rejecting_hook()
        before = self.head()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            sha = concurrency.catchup_sweep(self.store)
        self.assertIsNone(sha)
        self.assertEqual(self.head(), before)
        self.assertIn("blocked by hook", stderr.getvalue())
        # Pending file stays on disk for the next mutation to retry.
        self.assertEqual(
            concurrency._git_status_pending_tracked_paths(self.store),
            ["IM/IM-001.md"],
        )

    def test_git_add_failure_raises_pre_write_error(self):
        self.write_issue("IM/IM-001.md")
        real = concurrency._run_git_raw

        def failing_add(cwd, args):
            if args[0] == "add":
                return (1, "", "simulated add failure")
            return real(cwd, args)

        with mock.patch.object(concurrency, "_run_git_raw", side_effect=failing_add):
            with self.assertRaises(concurrency.StoreCommitError) as cm:
                concurrency.catchup_sweep(self.store)
        self.assertFalse(cm.exception.write_attempted)
        self.assertIn("git add failed", cm.exception.message)

    def test_commit_failure_without_hook_raises_pre_write_error(self):
        self.write_issue("IM/IM-001.md")
        real = concurrency._run_git_raw

        def failing_commit(cwd, args):
            if args[0] == "commit":
                return (1, "", "simulated commit failure")
            return real(cwd, args)

        with mock.patch.object(
            concurrency, "_run_git_raw", side_effect=failing_commit
        ):
            with self.assertRaises(concurrency.StoreCommitError) as cm:
                concurrency.catchup_sweep(self.store)
        self.assertFalse(cm.exception.write_attempted)
        self.assertIn("git commit failed", cm.exception.message)


class TestCommitAfterWrite(GitStoreTest):
    def test_relative_path_commits_with_message(self):
        self.write_issue("IM/IM-001.md")
        sha = concurrency.commit_after_write(
            self.store, ["IM/IM-001.md"], "create IM-001"
        )
        self.assertEqual(sha, self.head())
        log = _git(self.store, "log", "-1", "--format=%s").stdout.strip()
        self.assertEqual(log, "create IM-001")

    def test_absolute_path_is_normalized(self):
        issue = self.write_issue("IM/IM-001.md")
        sha = concurrency.commit_after_write(
            self.store, [str(issue)], "create IM-001"
        )
        self.assertEqual(sha, self.head())

    def test_absolute_path_outside_store_raises(self):
        outside = self.tmp / "outside.md"
        outside.write_text("x\n")
        with self.assertRaises(concurrency.StoreCommitError) as cm:
            concurrency.commit_after_write(self.store, [str(outside)], "m")
        self.assertIn("not inside store", cm.exception.message)

    def test_missing_paths_dropped_all_missing_returns_none(self):
        before = self.head()
        result = concurrency.commit_after_write(
            self.store, ["IM/absent.md", "CCR/INDEX.md"], "m"
        )
        self.assertIsNone(result)
        self.assertEqual(self.head(), before)

    def test_idempotent_write_skips_commit(self):
        self.write_issue("IM/IM-001.md")
        concurrency.commit_after_write(self.store, ["IM/IM-001.md"], "create")
        before = self.head()
        result = concurrency.commit_after_write(
            self.store, ["IM/IM-001.md"], "create again"
        )
        self.assertIsNone(result)
        self.assertEqual(self.head(), before)

    def test_hook_rejection_warns_and_returns_none(self):
        self.write_issue("IM/IM-001.md")
        self.add_rejecting_hook()
        before = self.head()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = concurrency.commit_after_write(
                self.store, ["IM/IM-001.md"], "create IM-001"
            )
        self.assertIsNone(result)
        self.assertEqual(self.head(), before)
        self.assertIn("blocked by hook", stderr.getvalue())
        self.assertTrue((self.store / "IM" / "IM-001.md").exists())

    def test_git_add_failure_raises_post_write_error(self):
        self.write_issue("IM/IM-001.md")
        real = concurrency._run_git_raw

        def failing_add(cwd, args):
            if args[0] == "add":
                return (1, "", "simulated add failure")
            return real(cwd, args)

        with mock.patch.object(concurrency, "_run_git_raw", side_effect=failing_add):
            with self.assertRaises(concurrency.StoreCommitError) as cm:
                concurrency.commit_after_write(self.store, ["IM/IM-001.md"], "m")
        self.assertTrue(cm.exception.write_attempted)

    def test_diff_cached_failure_raises(self):
        self.write_issue("IM/IM-001.md")
        real = concurrency._run_git_raw

        def failing_diff(cwd, args):
            if args[0] == "diff":
                return (2, "", "simulated diff failure")
            return real(cwd, args)

        with mock.patch.object(concurrency, "_run_git_raw", side_effect=failing_diff):
            with self.assertRaises(concurrency.StoreCommitError) as cm:
                concurrency.commit_after_write(self.store, ["IM/IM-001.md"], "m")
        self.assertIn("git diff --cached failed", cm.exception.message)

    def test_commit_failure_without_hook_raises_post_write_error(self):
        self.write_issue("IM/IM-001.md")
        real = concurrency._run_git_raw

        def failing_commit(cwd, args):
            if args[0] == "commit":
                return (1, "", "simulated commit failure")
            return real(cwd, args)

        with mock.patch.object(
            concurrency, "_run_git_raw", side_effect=failing_commit
        ):
            with self.assertRaises(concurrency.StoreCommitError) as cm:
                concurrency.commit_after_write(self.store, ["IM/IM-001.md"], "m")
        self.assertTrue(cm.exception.write_attempted)


class TestStoreMutation(GitStoreTest):
    def test_lifecycle_sweeps_then_commits_caller_paths(self):
        # Pre-existing orphan from a "killed" earlier mutation.
        self.write_issue("IM/IM-000.md", "orphan\n")
        issue = self.store / "IM" / "IM-001.md"
        with concurrency.store_mutation(
            self.store, [str(issue)], "create IM-001"
        ):
            concurrency.atomic_write(issue, "body\n")
        log = _git(
            self.store, "log", "--format=%s", "-2"
        ).stdout.strip().splitlines()
        self.assertEqual(
            log, ["create IM-001", concurrency.CATCHUP_COMMIT_MESSAGE]
        )
        self.assertEqual(
            concurrency._git_status_pending_tracked_paths(self.store), []
        )


class TestMainInProcess(GitStoreTest):
    """In-process _main() dispatch tests. The flock-run success
    path calls os.execvp and is covered by the subprocess tests in
    TestCli; only non-exec paths run in-process here.
    """

    def _run_main(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = concurrency._main(argv)
        return rc, stdout.getvalue(), stderr.getvalue()

    def test_snapshot_prints_encoded_state(self):
        path = self.tmp / "f.txt"
        path.write_text("abc")
        rc, out, _ = self._run_main(["snapshot", str(path)])
        self.assertEqual(rc, 0)
        decoded = concurrency.StateSnapshot.decode(out.strip())
        self.assertEqual(decoded, concurrency.capture_state(path))

    def test_verify_matching_snapshot_exits_zero(self):
        path = self.tmp / "f.txt"
        path.write_text("abc")
        snap = concurrency.capture_state(path).encode()
        rc, _, _ = self._run_main(["verify", str(path), snap])
        self.assertEqual(rc, 0)

    def test_verify_stale_snapshot_exits_17(self):
        path = self.tmp / "f.txt"
        path.write_text("abc")
        snap = concurrency.capture_state(path).encode()
        time.sleep(0.01)
        path.write_text("XYZQ")
        rc, _, err = self._run_main(["verify", str(path), snap])
        self.assertEqual(rc, errors.EXIT_CONCURRENT_EDIT)
        self.assertIn("concurrent-edit", err)

    def test_atomic_write_reads_stdin_buffer(self):
        target = self.tmp / "out.txt"
        fake_stdin = types.SimpleNamespace(buffer=io.BytesIO(b"hello\n"))
        with mock.patch.object(sys, "stdin", fake_stdin):
            rc, _, _ = self._run_main(["atomic-write", str(target)])
        self.assertEqual(rc, 0)
        self.assertEqual(target.read_text(), "hello\n")

    def test_catchup_sweep_clean_store_exits_zero_silent(self):
        rc, out, _ = self._run_main(["catchup-sweep", str(self.store)])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    def test_catchup_sweep_dirty_store_prints_sha(self):
        self.write_issue("IM/IM-001.md")
        rc, out, _ = self._run_main(["catchup-sweep", str(self.store)])
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), self.head())

    def test_default_argv_reads_sys_argv(self):
        path = self.tmp / "f.txt"
        path.write_text("abc")
        with mock.patch.object(sys, "argv", ["concurrency", "snapshot", str(path)]):
            rc, out, _ = self._run_main(None)
        self.assertEqual(rc, 0)
        self.assertEqual(
            concurrency.StateSnapshot.decode(out.strip()),
            concurrency.capture_state(path),
        )

    def test_catchup_sweep_contended_lock_exits_17(self):
        lock_path = concurrency.store_commit_lock_path(self.store)
        with concurrency.flock_acquire(lock_path, timeout=1.0):
            rc, _, err = self._run_main(
                ["catchup-sweep", str(self.store), "--timeout", "0.1"]
            )
        self.assertEqual(rc, errors.EXIT_CONCURRENT_EDIT)
        self.assertIn("concurrent-edit", err)

    def test_commit_after_write_contended_lock_exits_17(self):
        lock_path = concurrency.store_commit_lock_path(self.store)
        with concurrency.flock_acquire(lock_path, timeout=1.0):
            rc, _, err = self._run_main(
                [
                    "commit-after-write",
                    str(self.store),
                    "--message",
                    "m",
                    "--timeout",
                    "0.1",
                ]
            )
        self.assertEqual(rc, errors.EXIT_CONCURRENT_EDIT)
        self.assertIn("concurrent-edit", err)

    def test_catchup_sweep_non_repo_exits_one(self):
        plain = self.tmp / "not-a-repo"
        plain.mkdir()
        rc, _, err = self._run_main(["catchup-sweep", str(plain)])
        self.assertEqual(rc, concurrency.EXIT_STORE_COMMIT_FAILED)
        self.assertIn("store commit failed", err)

    def test_commit_after_write_commits_caller_path(self):
        self.write_issue("IM/IM-001.md")
        rc, out, _ = self._run_main(
            [
                "commit-after-write",
                str(self.store),
                "--message",
                "create IM-001",
                "--path",
                "IM/IM-001.md",
            ]
        )
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), self.head())
        log = _git(self.store, "log", "-1", "--format=%s").stdout.strip()
        self.assertEqual(log, "create IM-001")

    def test_commit_after_write_sweeps_other_dirty_paths_first(self):
        self.write_issue("IM/IM-000.md", "orphan\n")
        self.write_issue("IM/IM-001.md")
        rc, _, _ = self._run_main(
            [
                "commit-after-write",
                str(self.store),
                "--message",
                "create IM-001",
                "--path",
                "IM/IM-001.md",
            ]
        )
        self.assertEqual(rc, 0)
        log = _git(
            self.store, "log", "--format=%s", "-2"
        ).stdout.strip().splitlines()
        self.assertEqual(
            log, ["create IM-001", concurrency.CATCHUP_COMMIT_MESSAGE]
        )

    def test_commit_after_write_non_repo_exits_one(self):
        plain = self.tmp / "not-a-repo"
        plain.mkdir()
        rc, _, err = self._run_main(
            ["commit-after-write", str(plain), "--message", "m"]
        )
        self.assertEqual(rc, concurrency.EXIT_STORE_COMMIT_FAILED)
        self.assertIn("store commit failed", err)

    def test_flock_run_missing_separator_exits_two(self):
        rc, _, err = self._run_main(["flock-run", str(self.tmp / "a.lock")])
        self.assertEqual(rc, 2)
        self.assertIn("missing '--' separator", err)

    def test_flock_run_missing_command_exits_two(self):
        rc, _, err = self._run_main(
            ["flock-run", str(self.tmp / "a.lock"), "--"]
        )
        self.assertEqual(rc, 2)
        self.assertIn("missing command", err)

    def test_flock_run_contended_exits_17(self):
        lock = self.tmp / "a.lock"
        with concurrency.flock_acquire(lock, timeout=1.0):
            rc, _, err = self._run_main(
                ["flock-run", str(lock), "--timeout", "0.2", "--", "true"]
            )
        self.assertEqual(rc, errors.EXIT_CONCURRENT_EDIT)
        self.assertIn("concurrent-edit", err)

    def test_store_mutation_missing_separator_exits_two(self):
        rc, _, err = self._run_main(
            ["store-mutation", str(self.store), "--message", "m"]
        )
        self.assertEqual(rc, 2)
        self.assertIn("missing '--' separator", err)

    def test_store_mutation_missing_command_exits_two(self):
        rc, _, err = self._run_main(
            ["store-mutation", str(self.store), "--message", "m", "--"]
        )
        self.assertEqual(rc, 2)
        self.assertIn("missing command", err)

    def test_store_mutation_happy_path_commits_paths(self):
        issue = self.store / "IM" / "IM-001.md"
        issue.parent.mkdir(parents=True)
        rc, _, _ = self._run_main(
            [
                "store-mutation",
                str(self.store),
                "--message",
                "create IM-001",
                "--path",
                "IM/IM-001.md",
                "--",
                "sh",
                "-c",
                f"echo body > {issue}",
            ]
        )
        self.assertEqual(rc, 0)
        log = _git(self.store, "log", "-1", "--format=%s").stdout.strip()
        self.assertEqual(log, "create IM-001")

    def test_store_mutation_child_failure_propagates_rc_no_commit(self):
        before = self.head()
        rc, _, _ = self._run_main(
            [
                "store-mutation",
                str(self.store),
                "--message",
                "m",
                "--path",
                "IM/IM-001.md",
                "--",
                "sh",
                "-c",
                "exit 3",
            ]
        )
        self.assertEqual(rc, 3)
        self.assertEqual(self.head(), before)

    def test_store_mutation_command_not_found_exits_two(self):
        rc, _, err = self._run_main(
            [
                "store-mutation",
                str(self.store),
                "--message",
                "m",
                "--",
                str(self.tmp / "no-such-binary"),
            ]
        )
        self.assertEqual(rc, 2)
        self.assertIn("store-mutation:", err)

    def test_store_mutation_non_repo_sweep_failure_exits_one(self):
        plain = self.tmp / "not-a-repo"
        plain.mkdir()
        rc, _, err = self._run_main(
            ["store-mutation", str(plain), "--message", "m", "--", "true"]
        )
        self.assertEqual(rc, concurrency.EXIT_STORE_COMMIT_FAILED)
        self.assertIn("store commit failed", err)

    def test_store_mutation_commit_failure_exits_one(self):
        outside = self.tmp / "outside.md"
        outside.write_text("x\n")
        rc, _, err = self._run_main(
            [
                "store-mutation",
                str(self.store),
                "--message",
                "m",
                "--path",
                str(outside),
                "--",
                "true",
            ]
        )
        self.assertEqual(rc, concurrency.EXIT_STORE_COMMIT_FAILED)
        self.assertIn("not inside store", err)

    def test_store_mutation_contended_lock_exits_17(self):
        lock_path = concurrency.store_commit_lock_path(self.store)
        with concurrency.flock_acquire(lock_path, timeout=1.0):
            rc, _, err = self._run_main(
                [
                    "store-mutation",
                    str(self.store),
                    "--message",
                    "m",
                    "--timeout",
                    "0.1",
                    "--",
                    "true",
                ]
            )
        self.assertEqual(rc, errors.EXIT_CONCURRENT_EDIT)
        self.assertIn("concurrent-edit", err)


if __name__ == "__main__":
    unittest.main()
