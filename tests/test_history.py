#!/usr/bin/env python3
"""Unit tests for lib/history.py.

Covers the in-process surface:
  - parse_id: valid IDs, invalid IDs, empty / non-string input
  - rel_path_for: returns "<prefix>/<id>.md", rejects bad IDs
  - Commit dataclass: to_dict shape, frozen semantics
  - parse_git_log: empty input, single commit, many commits,
    subject containing tabs/spaces (unit-separator format protects them),
    wrong field count silently skipped, trailing blank line ignored
  - git_log: invokes git in a real synthetic repo, returns commits in
    reverse chronological order; honors --limit; --follow tracks renames;
    empty result on a path with no history; raises HistoryError when
    store_root has no .git directory.

End-to-end CLI behavior (exit codes, --help, --json, legacy-mode error,
prefix-missing, file-missing) lives in tests/verification/issue-history.sh.

Run:
    python3 -m unittest tests.test_history -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

from fixture_identity import FIXTURE_EMAIL  # noqa: E402

import history  # noqa: E402  -- path set above


def _git(cwd: Path, *args: str, env_extra=None) -> None:
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "Test Author",
            "GIT_AUTHOR_EMAIL": FIXTURE_EMAIL,
            "GIT_COMMITTER_NAME": "Test Committer",
            "GIT_COMMITTER_EMAIL": FIXTURE_EMAIL,
        }
    )
    if env_extra:
        env.update(env_extra)
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def _init_store(root: Path) -> None:
    _git(root, "init", "--quiet", "--initial-branch=main")
    _git(root, "config", "user.email", FIXTURE_EMAIL)
    _git(root, "config", "user.name", "Test User")
    _git(root, "config", "commit.gpgsign", "false")


class ParseIdTests(unittest.TestCase):
    def test_basic_two_letter_prefix(self):
        self.assertEqual(history.parse_id("IM-030"), ("IM", "IM-030"))

    def test_longer_prefix(self):
        self.assertEqual(history.parse_id("DOUG-035"), ("DOUG", "DOUG-035"))

    def test_max_prefix_length(self):
        # PREFIX_RE allows up to 8 chars: [A-Z][A-Z0-9]{1,7}
        self.assertEqual(
            history.parse_id("AB123456-7"),
            ("AB123456", "AB123456-7"),
        )

    def test_alphanumeric_prefix(self):
        # Digits allowed after the first letter
        self.assertEqual(history.parse_id("A1-1"), ("A1", "A1-1"))

    def test_rejects_lowercase_prefix(self):
        with self.assertRaises(history.HistoryError):
            history.parse_id("im-030")

    def test_rejects_one_char_prefix(self):
        # PREFIX_RE requires at least 2 chars
        with self.assertRaises(history.HistoryError):
            history.parse_id("I-030")

    def test_rejects_missing_number(self):
        with self.assertRaises(history.HistoryError):
            history.parse_id("IM-")

    def test_rejects_missing_dash(self):
        with self.assertRaises(history.HistoryError):
            history.parse_id("IM030")

    def test_rejects_non_digit_number(self):
        with self.assertRaises(history.HistoryError):
            history.parse_id("IM-03a")

    def test_rejects_empty(self):
        with self.assertRaises(history.HistoryError):
            history.parse_id("")

    def test_rejects_none(self):
        with self.assertRaises(history.HistoryError):
            history.parse_id(None)  # type: ignore[arg-type]


class RelPathForTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(history.rel_path_for("IM-030"), "IM/IM-030.md")

    def test_long_prefix(self):
        self.assertEqual(history.rel_path_for("DOUG-035"), "DOUG/DOUG-035.md")

    def test_rejects_bad_id(self):
        with self.assertRaises(history.HistoryError):
            history.rel_path_for("not-an-id")


class CommitTests(unittest.TestCase):
    def test_to_dict_shape(self):
        c = history.Commit(
            sha="abc123",
            date="2026-05-14",
            author="Doug",
            subject="feat: thing",
        )
        self.assertEqual(
            c.to_dict(),
            {
                "sha": "abc123",
                "date": "2026-05-14",
                "author": "Doug",
                "subject": "feat: thing",
            },
        )

    def test_is_frozen(self):
        c = history.Commit(sha="x", date="y", author="z", subject="s")
        with self.assertRaises(Exception):
            c.sha = "y"  # type: ignore[misc]


class ParseGitLogTests(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(history.parse_git_log(""), [])

    def test_single_commit(self):
        line = "abc\x1f2026-05-14T10:00:00-04:00\x1fDoug\x1ffeat: thing"
        out = history.parse_git_log(line + "\n")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].sha, "abc")
        self.assertEqual(out[0].date, "2026-05-14T10:00:00-04:00")
        self.assertEqual(out[0].author, "Doug")
        self.assertEqual(out[0].subject, "feat: thing")

    def test_many_commits_preserve_order(self):
        text = (
            "1\x1fd1\x1fa1\x1fs1\n"
            "2\x1fd2\x1fa2\x1fs2\n"
            "3\x1fd3\x1fa3\x1fs3\n"
        )
        out = history.parse_git_log(text)
        self.assertEqual([c.sha for c in out], ["1", "2", "3"])

    def test_subject_with_tabs_and_spaces(self):
        # The unit-separator format means subjects can safely contain
        # any other whitespace.
        text = "abc\x1fd\x1fa\x1ffeat: do thing\twith\ttabs and spaces\n"
        out = history.parse_git_log(text)
        self.assertEqual(out[0].subject, "feat: do thing\twith\ttabs and spaces")

    def test_wrong_field_count_skipped(self):
        text = (
            "1\x1fd1\x1fa1\x1fs1\n"
            "bad-line-no-separators\n"
            "3\x1fd3\x1fa3\x1fs3\n"
        )
        out = history.parse_git_log(text)
        self.assertEqual([c.sha for c in out], ["1", "3"])

    def test_trailing_blank_line_ignored(self):
        text = "1\x1fd1\x1fa1\x1fs1\n\n"
        out = history.parse_git_log(text)
        self.assertEqual(len(out), 1)


class GitLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="im030-history-"))
        self.store = self.tmp / "store"
        self.store.mkdir()
        _init_store(self.store)
        (self.store / "IM").mkdir()
        self.issue_file = self.store / "IM" / "IM-030.md"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _commit(self, message: str, content: str) -> None:
        self.issue_file.write_text(content, encoding="utf-8")
        _git(self.store, "add", "IM/IM-030.md")
        _git(self.store, "commit", "-m", message, "--quiet")

    def test_empty_history_on_uncommitted_file(self):
        # File exists but never committed -- git log returns empty
        self.issue_file.write_text("uncommitted", encoding="utf-8")
        out = history.git_log(self.store, "IM/IM-030.md")
        self.assertEqual(out, [])

    def test_returns_commits_reverse_chronological(self):
        self._commit("feat: first commit", "v1")
        self._commit("feat: second commit", "v2")
        self._commit("feat: third commit", "v3")
        out = history.git_log(self.store, "IM/IM-030.md")
        self.assertEqual(len(out), 3)
        # git log default order: newest first
        self.assertEqual(out[0].subject, "feat: third commit")
        self.assertEqual(out[-1].subject, "feat: first commit")

    def test_limit_caps_results(self):
        self._commit("c1", "a")
        self._commit("c2", "b")
        self._commit("c3", "c")
        out = history.git_log(self.store, "IM/IM-030.md", limit=2)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0].subject, "c3")
        self.assertEqual(out[1].subject, "c2")

    def test_commit_fields_populated(self):
        self._commit("feat: probe", "x")
        out = history.git_log(self.store, "IM/IM-030.md")
        self.assertEqual(len(out), 1)
        c = out[0]
        # SHA is 40 hex chars (full sha from %H)
        self.assertEqual(len(c.sha), 40)
        self.assertTrue(all(ch in "0123456789abcdef" for ch in c.sha))
        # Date is ISO8601 from %aI
        self.assertRegex(c.date, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
        self.assertEqual(c.author, "Test Author")
        self.assertEqual(c.subject, "feat: probe")

    def test_follow_tracks_renames(self):
        # Create initial file under a different name, commit, then rename.
        old_path = self.store / "IM" / "IM-001.md"
        old_path.write_text("initial", encoding="utf-8")
        _git(self.store, "add", "IM/IM-001.md")
        _git(self.store, "commit", "-m", "first under old name", "--quiet")
        # Rename to IM-030.md
        _git(self.store, "mv", "IM/IM-001.md", "IM/IM-030.md")
        _git(self.store, "commit", "-m", "rename to IM-030", "--quiet")
        # Mutate and commit again
        self.issue_file.write_text("after rename", encoding="utf-8")
        _git(self.store, "add", "IM/IM-030.md")
        _git(self.store, "commit", "-m", "edit after rename", "--quiet")

        out = history.git_log(self.store, "IM/IM-030.md")
        subjects = [c.subject for c in out]
        # With --follow, all three commits are visible from the new name
        self.assertEqual(
            subjects,
            ["edit after rename", "rename to IM-030", "first under old name"],
        )

    def test_raises_on_missing_git_dir(self):
        nogit = self.tmp / "not-a-repo"
        nogit.mkdir()
        with self.assertRaises(history.HistoryError):
            history.git_log(nogit, "IM/IM-030.md")


class ValidatePrefixTests(unittest.TestCase):
    def test_two_letter_prefix(self):
        # Should not raise
        history.validate_prefix("IM")

    def test_long_prefix(self):
        history.validate_prefix("DOUG")

    def test_max_length_prefix(self):
        # _PREFIX_RE allows up to 8 chars: [A-Z][A-Z0-9]{1,7}
        history.validate_prefix("AB123456")

    def test_alphanumeric_prefix(self):
        history.validate_prefix("A1")

    def test_rejects_lowercase(self):
        with self.assertRaises(history.HistoryError):
            history.validate_prefix("im")

    def test_rejects_one_char(self):
        with self.assertRaises(history.HistoryError):
            history.validate_prefix("I")

    def test_rejects_empty(self):
        with self.assertRaises(history.HistoryError):
            history.validate_prefix("")

    def test_rejects_digit_first(self):
        with self.assertRaises(history.HistoryError):
            history.validate_prefix("1IM")

    def test_rejects_non_string(self):
        with self.assertRaises(history.HistoryError):
            history.validate_prefix(None)  # type: ignore[arg-type]


class GitPickaxeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="im031-pickaxe-"))
        self.store = self.tmp / "store"
        self.store.mkdir()
        _init_store(self.store)
        (self.store / "IM").mkdir()
        (self.store / "DOUG").mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _commit_file(self, rel: str, content: str, message: str, *, date: str = None) -> None:
        path = self.store / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        _git(self.store, "add", rel)
        env = None
        if date is not None:
            env = {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        _git(self.store, "commit", "-m", message, "--quiet", env_extra=env)

    def test_basic_term_found(self):
        self._commit_file("IM/IM-001.md", "no match here", "first")
        self._commit_file("IM/IM-001.md", "now contains MAGIC_TOKEN", "add MAGIC")
        out = history.git_pickaxe(self.store, "MAGIC_TOKEN")
        # Only the commit that introduced the token should match.
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].subject, "add MAGIC")

    def test_term_added_then_removed_returns_both(self):
        self._commit_file("IM/IM-001.md", "contains MAGIC_TOKEN", "add MAGIC")
        self._commit_file("IM/IM-001.md", "now removed", "remove MAGIC")
        out = history.git_pickaxe(self.store, "MAGIC_TOKEN")
        # Both commits change the count of MAGIC_TOKEN, newest first.
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0].subject, "remove MAGIC")
        self.assertEqual(out[1].subject, "add MAGIC")

    def test_no_match_returns_empty(self):
        self._commit_file("IM/IM-001.md", "ordinary content", "c1")
        out = history.git_pickaxe(self.store, "NEVER_PRESENT")
        self.assertEqual(out, [])

    def test_empty_repo_returns_empty(self):
        # Fresh repo with no commits -- HEAD does not resolve.
        out = history.git_pickaxe(self.store, "anything")
        self.assertEqual(out, [])

    def test_prefix_scoping_includes_matching_dir(self):
        self._commit_file("IM/IM-001.md", "has TOKEN", "im add")
        self._commit_file("DOUG/DOUG-001.md", "has TOKEN", "carol add")
        out = history.git_pickaxe(self.store, "TOKEN", prefixes=["IM"])
        subjects = [c.subject for c in out]
        self.assertIn("im add", subjects)
        self.assertNotIn("carol add", subjects)

    def test_prefix_scoping_excludes_non_matching_dir(self):
        self._commit_file("DOUG/DOUG-001.md", "has TOKEN", "carol add")
        out = history.git_pickaxe(self.store, "TOKEN", prefixes=["IM"])
        self.assertEqual(out, [])

    def test_multiple_prefixes_union(self):
        self._commit_file("IM/IM-001.md", "has TOKEN", "im add")
        self._commit_file("DOUG/DOUG-001.md", "has TOKEN", "carol add")
        out = history.git_pickaxe(self.store, "TOKEN", prefixes=["IM", "DOUG"])
        subjects = sorted(c.subject for c in out)
        self.assertEqual(subjects, ["carol add", "im add"])

    def test_since_filters_old_commits(self):
        # Two commits with explicit dates; --since at the middle date
        # excludes the older one.
        self._commit_file(
            "IM/IM-001.md",
            "has TOKEN",
            "old add",
            date="2026-01-01T00:00:00+0000",
        )
        self._commit_file(
            "IM/IM-001.md",
            "no token now",
            "old remove",
            date="2026-01-02T00:00:00+0000",
        )
        self._commit_file(
            "IM/IM-001.md",
            "has TOKEN again",
            "new add",
            date="2026-04-15T00:00:00+0000",
        )
        out = history.git_pickaxe(
            self.store,
            "TOKEN",
            since="2026-03-01",
        )
        subjects = [c.subject for c in out]
        self.assertEqual(subjects, ["new add"])

    def test_since_relative_form_accepted(self):
        # Relative forms (`X.days.ago`) are passed through verbatim.
        # We just need to confirm the call succeeds; selecting a window
        # large enough to include both commits is the simplest assertion.
        self._commit_file("IM/IM-001.md", "has TOKEN", "add")
        out = history.git_pickaxe(self.store, "TOKEN", since="365.days.ago")
        self.assertEqual(len(out), 1)

    def test_raises_on_missing_git_dir(self):
        nogit = self.tmp / "not-a-repo"
        nogit.mkdir()
        with self.assertRaises(history.HistoryError):
            history.git_pickaxe(nogit, "anything")

    def test_raises_on_bad_prefix_shape(self):
        self._commit_file("IM/IM-001.md", "has TOKEN", "c1")
        with self.assertRaises(history.HistoryError):
            history.git_pickaxe(self.store, "TOKEN", prefixes=["im"])

    def test_raises_on_bad_prefix_among_valid_ones(self):
        # Validation should fail on any bad entry, even if others are good.
        self._commit_file("IM/IM-001.md", "has TOKEN", "c1")
        with self.assertRaises(history.HistoryError):
            history.git_pickaxe(self.store, "TOKEN", prefixes=["IM", "bad-prefix"])

    def test_literal_metacharacters(self):
        # `-S` is a literal pickaxe -- regex metacharacters match literally.
        self._commit_file("IM/IM-001.md", "the term is a.b", "literal dots")
        out = history.git_pickaxe(self.store, "a.b")
        self.assertEqual(len(out), 1)
        # And a regex-style pattern that would normally match should not:
        out2 = history.git_pickaxe(self.store, "a[.]b")
        self.assertEqual(out2, [])


class ResolveRefTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="im032-resolve-"))
        self.store = self.tmp / "store"
        self.store.mkdir()
        _init_store(self.store)
        (self.store / "IM").mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _commit(self, message: str, *, date: str = None) -> str:
        # Write something so each commit changes the tree.
        f = self.store / "IM" / "IM-001.md"
        prev = f.read_text(encoding="utf-8") if f.exists() else ""
        f.write_text(prev + message + "\n", encoding="utf-8")
        _git(self.store, "add", "IM/IM-001.md")
        env = None
        if date is not None:
            env = {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        _git(self.store, "commit", "-m", message, "--quiet", env_extra=env)
        result = subprocess.run(
            ["git", "-C", str(self.store), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()

    def test_resolve_full_sha(self):
        sha = self._commit("first")
        self.assertEqual(history.resolve_ref(self.store, sha), sha)

    def test_resolve_short_sha(self):
        sha = self._commit("first")
        # Short SHA -> full SHA
        self.assertEqual(history.resolve_ref(self.store, sha[:8]), sha)

    def test_resolve_branch(self):
        sha = self._commit("first")
        self.assertEqual(history.resolve_ref(self.store, "main"), sha)

    def test_resolve_head(self):
        sha = self._commit("first")
        self.assertEqual(history.resolve_ref(self.store, "HEAD"), sha)

    def test_resolve_tag(self):
        sha = self._commit("first")
        _git(self.store, "tag", "v1.0")
        self.assertEqual(history.resolve_ref(self.store, "v1.0"), sha)

    def test_resolve_absolute_date(self):
        # Commit dated 2026-01-15; ask for state at 2026-02-01 ->
        # should resolve to that commit.
        sha = self._commit("dated commit", date="2026-01-15T12:00:00+0000")
        self.assertEqual(
            history.resolve_ref(self.store, "2026-02-01"),
            sha,
        )

    def test_resolve_iso_datetime(self):
        sha = self._commit("dated", date="2026-01-15T12:00:00+0000")
        self.assertEqual(
            history.resolve_ref(self.store, "2026-02-01T00:00:00"),
            sha,
        )

    def test_resolve_relative_date_via_yesterday(self):
        # "yesterday" is treated as a date keyword -- backdate the
        # commit so it precedes "yesterday" relative to today.
        sha = self._commit("old commit", date="2020-01-01T00:00:00+0000")
        self.assertEqual(
            history.resolve_ref(self.store, "yesterday"),
            sha,
        )

    def test_unknown_ref_raises(self):
        self._commit("first")
        with self.assertRaises(history.HistoryError) as cm:
            history.resolve_ref(self.store, "no-such-branch-xyz")
        self.assertIn("could not resolve", str(cm.exception))

    def test_garbage_does_not_silently_fall_back_to_head(self):
        # Approxidate would silently map "IM-030" to "now" if we didn't
        # gate the date fallback on a date-like pattern. Critical
        # behavioral guarantee: the user gets an error, not HEAD.
        self._commit("first")
        with self.assertRaises(history.HistoryError) as cm:
            history.resolve_ref(self.store, "IM-030")
        self.assertIn("could not resolve", str(cm.exception))

    def test_date_before_any_commit_raises(self):
        self._commit("recent", date="2026-04-01T00:00:00+0000")
        with self.assertRaises(history.HistoryError) as cm:
            history.resolve_ref(self.store, "1990-01-01")
        # Distinct error from "could not resolve" -- the user typed a
        # plausible date that just predates the repo.
        self.assertIn("no commit found", str(cm.exception))

    def test_empty_repo_raises_with_clear_message(self):
        with self.assertRaises(history.HistoryError) as cm:
            history.resolve_ref(self.store, "HEAD")
        self.assertIn("no commits", str(cm.exception))

    def test_empty_at_value_raises(self):
        self._commit("first")
        with self.assertRaises(history.HistoryError):
            history.resolve_ref(self.store, "")

    def test_raises_on_missing_git_dir(self):
        nogit = self.tmp / "not-a-repo"
        nogit.mkdir()
        with self.assertRaises(history.HistoryError):
            history.resolve_ref(nogit, "HEAD")


class GitShowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="im032-show-"))
        self.store = self.tmp / "store"
        self.store.mkdir()
        _init_store(self.store)
        (self.store / "IM").mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _commit_file(self, rel: str, content: str, message: str) -> str:
        path = self.store / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        _git(self.store, "add", rel)
        _git(self.store, "commit", "-m", message, "--quiet")
        result = subprocess.run(
            ["git", "-C", str(self.store), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()

    def test_returns_file_contents_at_ref(self):
        sha = self._commit_file("IM/IM-001.md", "first version", "c1")
        content = history.git_show(self.store, sha, "IM/IM-001.md")
        self.assertEqual(content, "first version")

    def test_returns_state_at_historical_commit(self):
        sha1 = self._commit_file("IM/IM-001.md", "v1", "c1")
        sha2 = self._commit_file("IM/IM-001.md", "v2", "c2")
        self.assertEqual(history.git_show(self.store, sha1, "IM/IM-001.md"), "v1")
        self.assertEqual(history.git_show(self.store, sha2, "IM/IM-001.md"), "v2")

    def test_file_did_not_exist_at_ref_raises_clear_error(self):
        # Commit IM-001.md, then ask for IM-099.md at that commit.
        sha = self._commit_file("IM/IM-001.md", "exists", "c1")
        with self.assertRaises(history.HistoryError) as cm:
            history.git_show(self.store, sha, "IM/IM-099.md")
        self.assertIn("did not exist", str(cm.exception))
        self.assertIn("IM/IM-099.md", str(cm.exception))

    def test_file_added_later_did_not_exist_in_earlier_commit(self):
        # File added in c2; asking for it at c1 should error.
        sha1 = self._commit_file("IM/IM-001.md", "v1", "c1")
        self._commit_file("IM/IM-002.md", "added later", "c2")
        with self.assertRaises(history.HistoryError) as cm:
            history.git_show(self.store, sha1, "IM/IM-002.md")
        self.assertIn("did not exist", str(cm.exception))

    def test_raises_on_missing_git_dir(self):
        nogit = self.tmp / "not-a-repo"
        nogit.mkdir()
        with self.assertRaises(history.HistoryError):
            history.git_show(nogit, "abc", "IM/IM-001.md")


class IssueActivityTests(unittest.TestCase):
    def _make(self) -> history.IssueActivity:
        c = history.Commit(
            sha="abc123",
            date="2026-05-14T10:00:00-04:00",
            author="Doug",
            subject="feat: IM-030",
        )
        return history.IssueActivity(
            issue_id="IM-030",
            prefix="IM",
            file="IM/IM-030.md",
            commit_count=3,
            latest=c,
        )

    def test_to_dict_shape(self):
        a = self._make()
        d = a.to_dict()
        self.assertEqual(d["id"], "IM-030")
        self.assertEqual(d["prefix"], "IM")
        self.assertEqual(d["file"], "IM/IM-030.md")
        self.assertEqual(d["commit_count"], 3)
        self.assertEqual(d["latest"]["sha"], "abc123")
        self.assertEqual(d["latest"]["subject"], "feat: IM-030")

    def test_is_frozen(self):
        a = self._make()
        with self.assertRaises(Exception):
            a.commit_count = 99  # type: ignore[misc]


class ParseGitLogWithNamesTests(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(history.parse_git_log_with_names(""), [])

    def test_single_commit_with_files(self):
        text = (
            "abc\x1f2026-05-14T10:00:00-04:00\x1fDoug\x1ffeat: thing\n"
            "IM/IM-001.md\n"
            "IM/IM-002.md\n"
        )
        out = history.parse_git_log_with_names(text)
        self.assertEqual(len(out), 1)
        commit, files = out[0]
        self.assertEqual(commit.sha, "abc")
        self.assertEqual(commit.subject, "feat: thing")
        self.assertEqual(files, ["IM/IM-001.md", "IM/IM-002.md"])

    def test_single_commit_no_files(self):
        # Format-only line (a merge commit with no changes touching the
        # pathspec, say) still parses cleanly with an empty file list.
        text = "abc\x1fd\x1fa\x1fs\n"
        out = history.parse_git_log_with_names(text)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][1], [])

    def test_many_commits_with_blank_separators(self):
        text = (
            "1\x1fd1\x1fa1\x1fs1\n"
            "IM/IM-001.md\n"
            "\n"
            "2\x1fd2\x1fa2\x1fs2\n"
            "IM/IM-002.md\n"
            "IM/IM-003.md\n"
            "\n"
            "3\x1fd3\x1fa3\x1fs3\n"
            "DOUG/DOUG-001.md\n"
        )
        out = history.parse_git_log_with_names(text)
        self.assertEqual([c.sha for c, _ in out], ["1", "2", "3"])
        self.assertEqual([files for _, files in out], [
            ["IM/IM-001.md"],
            ["IM/IM-002.md", "IM/IM-003.md"],
            ["DOUG/DOUG-001.md"],
        ])

    def test_wrong_field_count_header_skipped(self):
        # A header-shaped line with the wrong field count gets dropped;
        # the next valid header is picked up. (The format is under our
        # control, so this is purely defensive.)
        text = (
            "bad-no-separators\n"
            "1\x1fd1\x1fa1\x1fs1\n"
            "IM/IM-001.md\n"
        )
        out = history.parse_git_log_with_names(text)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][0].sha, "1")

    def test_trailing_blank_line_ignored(self):
        text = (
            "1\x1fd1\x1fa1\x1fs1\n"
            "IM/IM-001.md\n"
            "\n"
        )
        out = history.parse_git_log_with_names(text)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][1], ["IM/IM-001.md"])


class GitActivityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="im033-activity-"))
        self.store = self.tmp / "store"
        self.store.mkdir()
        _init_store(self.store)
        (self.store / "IM").mkdir()
        (self.store / "DOUG").mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _commit_file(
        self,
        rel: str,
        content: str,
        message: str,
        *,
        date: str = None,
    ) -> None:
        path = self.store / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        _git(self.store, "add", rel)
        env = None
        if date is not None:
            env = {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        _git(self.store, "commit", "-m", message, "--quiet", env_extra=env)

    def _commit_multi(
        self,
        files: dict,
        message: str,
        *,
        date: str = None,
    ) -> None:
        # files: {relpath: content}; commit all in one go.
        for rel, content in files.items():
            path = self.store / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            _git(self.store, "add", rel)
        env = None
        if date is not None:
            env = {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        _git(self.store, "commit", "-m", message, "--quiet", env_extra=env)

    def test_empty_repo_returns_empty(self):
        out = history.git_activity(self.store)
        self.assertEqual(out, [])

    def test_basic_group_by_issue(self):
        self._commit_file("IM/IM-001.md", "a", "first")
        self._commit_file("IM/IM-002.md", "b", "second")
        out = history.git_activity(self.store)
        self.assertEqual(len(out), 2)
        ids = {a.issue_id for a in out}
        self.assertEqual(ids, {"IM-001", "IM-002"})

    def test_multiple_commits_same_issue_increment_count(self):
        # Three commits to the same file: count == 3, latest is the
        # newest commit.
        self._commit_file(
            "IM/IM-001.md", "v1", "first",
            date="2026-01-01T00:00:00+0000",
        )
        self._commit_file(
            "IM/IM-001.md", "v2", "second",
            date="2026-02-01T00:00:00+0000",
        )
        self._commit_file(
            "IM/IM-001.md", "v3", "third",
            date="2026-03-01T00:00:00+0000",
        )
        out = history.git_activity(self.store)
        self.assertEqual(len(out), 1)
        a = out[0]
        self.assertEqual(a.commit_count, 3)
        self.assertEqual(a.latest.subject, "third")

    def test_sorted_newest_first(self):
        # IM-001 last touched Jan, IM-002 last touched Feb. Result is
        # newest-first.
        self._commit_file(
            "IM/IM-001.md", "x", "im1",
            date="2026-01-15T00:00:00+0000",
        )
        self._commit_file(
            "IM/IM-002.md", "y", "im2",
            date="2026-02-15T00:00:00+0000",
        )
        out = history.git_activity(self.store)
        self.assertEqual([a.issue_id for a in out], ["IM-002", "IM-001"])

    def test_ties_broken_by_id_ascending(self):
        # Two issues touched in the same commit have identical
        # latest.date -- secondary sort is id ascending.
        self._commit_multi(
            {"IM/IM-003.md": "c", "IM/IM-001.md": "a", "IM/IM-002.md": "b"},
            "bundled",
            date="2026-04-01T00:00:00+0000",
        )
        out = history.git_activity(self.store)
        self.assertEqual([a.issue_id for a in out], ["IM-001", "IM-002", "IM-003"])

    def test_non_issue_files_ignored(self):
        # A commit touching INDEX.md and .repos -- nothing under
        # <PREFIX>/<ID>.md -- contributes no activity. This is the
        # "rebased migration commit" graceful case.
        (self.store / "INDEX.md").write_text("# index\n", encoding="utf-8")
        (self.store / ".repos").write_text("issue-mgmt:IM\n", encoding="utf-8")
        _git(self.store, "add", "INDEX.md", ".repos")
        _git(self.store, "commit", "-m", "store maintenance", "--quiet")
        out = history.git_activity(self.store)
        self.assertEqual(out, [])

    def test_mixed_commit_only_issue_files_count(self):
        # A commit that touches both an issue file and a maintenance
        # file produces exactly one IssueActivity entry.
        (self.store / "INDEX.md").write_text("# index\n", encoding="utf-8")
        (self.store / "IM" / "IM-001.md").write_text("v1", encoding="utf-8")
        _git(self.store, "add", "INDEX.md", "IM/IM-001.md")
        _git(self.store, "commit", "-m", "feat: IM-001 + reindex", "--quiet")
        out = history.git_activity(self.store)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].issue_id, "IM-001")
        self.assertEqual(out[0].commit_count, 1)

    def test_prefix_path_mismatch_ignored(self):
        # A misplaced file like IM/DOUG-001.md fails the back-reference
        # match in _ISSUE_PATH_RE, so it does not produce an
        # IssueActivity. validate-issue is responsible for catching
        # the structural error; activity-issues just doesn't count it.
        self._commit_file("IM/DOUG-001.md", "misplaced", "bad path")
        out = history.git_activity(self.store)
        self.assertEqual(out, [])

    def test_prefix_filter_scopes_scan(self):
        self._commit_file("IM/IM-001.md", "a", "im")
        self._commit_file("DOUG/DOUG-001.md", "b", "carol")
        out = history.git_activity(self.store, prefixes=["IM"])
        ids = [a.issue_id for a in out]
        self.assertEqual(ids, ["IM-001"])

    def test_prefix_filter_multi_union(self):
        self._commit_file("IM/IM-001.md", "a", "im")
        self._commit_file("DOUG/DOUG-001.md", "b", "carol")
        out = history.git_activity(self.store, prefixes=["IM", "DOUG"])
        ids = sorted(a.issue_id for a in out)
        self.assertEqual(ids, ["DOUG-001", "IM-001"])

    def test_since_filters_old_commits(self):
        self._commit_file(
            "IM/IM-001.md", "old", "old commit",
            date="2026-01-01T00:00:00+0000",
        )
        self._commit_file(
            "IM/IM-002.md", "new", "new commit",
            date="2026-04-15T00:00:00+0000",
        )
        out = history.git_activity(self.store, since="2026-03-01")
        self.assertEqual([a.issue_id for a in out], ["IM-002"])

    def test_since_relative_form_accepted(self):
        # Relative forms are passed through verbatim; the window just
        # has to be wide enough.
        self._commit_file("IM/IM-001.md", "x", "c1")
        out = history.git_activity(self.store, since="365.days.ago")
        self.assertEqual(len(out), 1)

    def test_limit_caps_issues_not_commits(self):
        # Five commits across three issues -- limit=2 returns the
        # two most-recently-touched, not the first two commits.
        self._commit_file(
            "IM/IM-001.md", "a", "c1",
            date="2026-01-01T00:00:00+0000",
        )
        self._commit_file(
            "IM/IM-002.md", "b", "c2",
            date="2026-02-01T00:00:00+0000",
        )
        self._commit_file(
            "IM/IM-001.md", "a2", "c3",
            date="2026-02-15T00:00:00+0000",
        )
        self._commit_file(
            "IM/IM-003.md", "c", "c4",
            date="2026-03-01T00:00:00+0000",
        )
        self._commit_file(
            "IM/IM-002.md", "b2", "c5",
            date="2026-04-01T00:00:00+0000",
        )
        out = history.git_activity(self.store, limit=2)
        self.assertEqual([a.issue_id for a in out], ["IM-002", "IM-003"])
        # The capped-out issue still has its real commit count when it
        # is included -- limit slices after binning.
        self.assertEqual(out[0].commit_count, 2)  # IM-002 touched twice

    def test_limit_one(self):
        self._commit_file("IM/IM-001.md", "a", "c1")
        self._commit_file("IM/IM-002.md", "b", "c2")
        out = history.git_activity(self.store, limit=1)
        self.assertEqual(len(out), 1)

    def test_latest_commit_fields_populated(self):
        self._commit_file("IM/IM-001.md", "a", "feat: probe")
        out = history.git_activity(self.store)
        self.assertEqual(len(out), 1)
        a = out[0]
        self.assertEqual(a.issue_id, "IM-001")
        self.assertEqual(a.prefix, "IM")
        self.assertEqual(a.file, "IM/IM-001.md")
        self.assertEqual(a.commit_count, 1)
        # Full SHA from %H
        self.assertEqual(len(a.latest.sha), 40)
        self.assertTrue(all(ch in "0123456789abcdef" for ch in a.latest.sha))
        # ISO8601 from %aI
        self.assertRegex(
            a.latest.date, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
        )
        self.assertEqual(a.latest.author, "Test Author")
        self.assertEqual(a.latest.subject, "feat: probe")

    def test_raises_on_missing_git_dir(self):
        nogit = self.tmp / "not-a-repo"
        nogit.mkdir()
        with self.assertRaises(history.HistoryError):
            history.git_activity(nogit)

    def test_raises_on_bad_prefix_shape(self):
        self._commit_file("IM/IM-001.md", "a", "c1")
        with self.assertRaises(history.HistoryError):
            history.git_activity(self.store, prefixes=["im"])

    def test_raises_on_bad_prefix_among_valid_ones(self):
        self._commit_file("IM/IM-001.md", "a", "c1")
        with self.assertRaises(history.HistoryError):
            history.git_activity(self.store, prefixes=["IM", "bad-prefix"])


if __name__ == "__main__":
    unittest.main()
