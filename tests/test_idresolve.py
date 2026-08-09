#!/usr/bin/env python3
"""Unit tests for lib/idresolve.py.

idresolve is the single place issue IDs and prefixes are canonicalized: the
shared `PREFIX-NNN` / `PREFIX` grammar, the case-insensitive parse
(split_id), the provably-unique prefix resolver (resolve_prefix), the
syntactic dep-target canonicalizer (canonicalize_target), and the
fallback-ladder path resolver (resolve_issue_paths). The resolver builds the
per-issue lock path from the canonical stem (the P2 fix) and fails loud on a
D2 dup-padding collision.

These tests exercise the module directly against tempfile stores in BOTH
store and legacy modes. End-to-end CLI behavior (canonical commit messages,
--json ids, the P2 lock-contention proof) lives in
tests/verification/im-205-id-canon.sh.

Run:
    python3 -m unittest tests.test_idresolve -v
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))

import errors  # noqa: E402
import idresolve  # noqa: E402


class TestSplitId(unittest.TestCase):
    def test_canonical_id_parses(self):
        assert idresolve.split_id("IM-007") == ("IM", 7)

    def test_lowercase_prefix_is_upper_cased(self):
        assert idresolve.split_id("im-7") == ("IM", 7)

    def test_unpadded_number_parses_to_int(self):
        assert idresolve.split_id("IM-7") == ("IM", 7)

    def test_wide_padding_parses_to_int(self):
        assert idresolve.split_id("IM-0007") == ("IM", 7)

    def test_surrounding_whitespace_tolerated(self):
        assert idresolve.split_id("  im-42 ") == ("IM", 42)

    def test_multi_char_prefix(self):
        assert idresolve.split_id("abcd-1") == ("ABCD", 1)

    def test_bad_shape_returns_none(self):
        assert idresolve.split_id("not-an-id") is None

    def test_missing_number_returns_none(self):
        assert idresolve.split_id("IM-") is None

    def test_non_string_returns_none(self):
        assert idresolve.split_id(None) is None


class TestResolvePrefix(unittest.TestCase):
    def test_exact_match_wins(self):
        assert idresolve.resolve_prefix("IM", {"IM", "DOUG"}) == "IM"

    def test_sole_case_insensitive_match(self):
        assert idresolve.resolve_prefix("im", {"IM", "DOUG"}) == "IM"

    def test_zero_matches_returns_none(self):
        assert idresolve.resolve_prefix("XX", {"IM", "DOUG"}) is None

    def test_ambiguity_is_impossible_but_no_match_is_none(self):
        # Prefixes are unique upper-case by construction, so a >1 CI match
        # cannot arise from a real store; an unknown prefix -> None.
        assert idresolve.resolve_prefix("zz", set()) is None

    def test_empty_prefix_returns_none(self):
        assert idresolve.resolve_prefix("", {"IM"}) is None


class TestCanonicalizeTarget(unittest.TestCase):
    def test_case_fold_and_pad(self):
        assert idresolve.canonicalize_target("im-84", {"IM"}) == "IM-084"

    def test_already_canonical_is_identity(self):
        assert idresolve.canonicalize_target("IM-084", {"IM"}) == "IM-084"

    def test_wide_number_kept_min_width_only(self):
        assert idresolve.canonicalize_target("IM-1234", {"IM"}) == "IM-1234"

    def test_unknown_prefix_still_case_folds(self):
        # D1: case-fold ALWAYS, even for a cross-prefix target that does not
        # resolve (write-now / resolve-later).
        assert idresolve.canonicalize_target("rm-7", set()) == "RM-007"

    def test_bad_shape_returns_none(self):
        assert idresolve.canonicalize_target("garbage", {"IM"}) is None


class _StoreBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._prev_env = os.environ.get("ISSUE_STORE_ROOT")
        os.environ.pop("ISSUE_STORE_ROOT", None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        if self._prev_env is not None:
            os.environ["ISSUE_STORE_ROOT"] = self._prev_env
        else:
            os.environ.pop("ISSUE_STORE_ROOT", None)

    def _store(self):
        store = self.tmp / "store"
        (store / "IM" / ".locks").mkdir(parents=True)
        (store / ".repos").write_text("issue-mgmt:IM\n", encoding="utf-8")
        return store

    def _write(self, store, stem):
        (store / "IM" / f"{stem}.md").write_text(
            f"---\nid: {stem}\nstatus: open\n---\n\n## Summary\n", encoding="utf-8"
        )


class TestResolveIssuePathsStoreMode(_StoreBase):
    def test_exact_hit_already_canonical(self):
        store = self._store()
        self._write(store, "IM-007")
        r = idresolve.resolve_issue_paths("IM-007", store, None)
        assert r.canonical_id == "IM-007"
        assert r.prefix == "IM"
        assert r.issue_file == store / "IM" / "IM-007.md"
        assert r.lock_path == store / "IM" / ".locks" / "IM-007.lock"
        assert r.normalized is False

    def test_integer_normalize_via_glob(self):
        store = self._store()
        self._write(store, "IM-007")
        r = idresolve.resolve_issue_paths("IM-7", store, None)
        assert r.canonical_id == "IM-007"
        assert r.normalized is True
        # P2: the lock path is ALWAYS built from the canonical stem.
        assert r.lock_path == store / "IM" / ".locks" / "IM-007.lock"

    def test_case_fold(self):
        store = self._store()
        self._write(store, "IM-007")
        r = idresolve.resolve_issue_paths("im-007", store, None)
        assert r.canonical_id == "IM-007"
        assert r.normalized is True

    def test_width_agnostic_matches_wide_padding_on_disk(self):
        store = self._store()
        self._write(store, "IM-0007")
        r = idresolve.resolve_issue_paths("IM-7", store, None)
        # canonical id is the actual on-disk stem, whatever its padding.
        assert r.canonical_id == "IM-0007"
        assert r.lock_path == store / "IM" / ".locks" / "IM-0007.lock"

    def test_not_found_falls_back_to_min_width_canonical(self):
        store = self._store()
        r = idresolve.resolve_issue_paths("IM-7", store, None)
        # No file: the canonical is the :03d min-width form; the caller's own
        # existence check then reports not-found.
        assert r.canonical_id == "IM-007"
        assert not r.issue_file.exists()

    def test_valid_shape_no_file_no_crash(self):
        store = self._store()
        r = idresolve.resolve_issue_paths("IM-999999", store, None)
        assert r.canonical_id == "IM-999999"
        assert not r.issue_file.exists()

    def test_d2_dup_padding_fails_loud(self):
        store = self._store()
        self._write(store, "IM-007")
        self._write(store, "IM-0007")
        with self.assertRaises(errors.StoreIntegrityError) as cm:
            idresolve.resolve_issue_paths("IM-7", store, None)
        # The error names BOTH colliding files.
        msg = str(cm.exception)
        assert "IM-007.md" in msg
        assert "IM-0007.md" in msg
        assert cm.exception.exit_code == 1

    def test_exact_hit_does_not_trigger_d2(self):
        # Querying the exact-canonical id hits the fast path (no glob), so a
        # sibling wide-padding file never triggers D2.
        store = self._store()
        self._write(store, "IM-007")
        self._write(store, "IM-0007")
        r = idresolve.resolve_issue_paths("IM-007", store, None)
        assert r.canonical_id == "IM-007"

    def test_emit_note_fires_only_on_change(self):
        store = self._store()
        self._write(store, "IM-007")
        notes = []
        idresolve.resolve_issue_paths(
            "IM-007", store, None, emit_note=lambda b, a: notes.append((b, a))
        )
        assert notes == []
        idresolve.resolve_issue_paths(
            "IM-7", store, None, emit_note=lambda b, a: notes.append((b, a))
        )
        assert notes == [("IM-7", "IM-007")]

    def test_invalid_id_raises_value_error(self):
        store = self._store()
        with self.assertRaises(ValueError):
            idresolve.resolve_issue_paths("not-an-id", store, None)


class TestResolveIssuePathsLegacyMode(_StoreBase):
    def _legacy_anchor(self):
        anchor = self.tmp / "proj" / ".issues"
        (anchor / ".locks").mkdir(parents=True)
        (anchor / ".prefix").write_text("IM\n", encoding="utf-8")
        return anchor

    def test_exact_hit(self):
        anchor = self._legacy_anchor()
        (anchor / "IM-007.md").write_text(
            "---\nid: IM-007\n---\n", encoding="utf-8"
        )
        r = idresolve.resolve_issue_paths("IM-007", None, anchor)
        assert r.store_root is None
        assert r.canonical_id == "IM-007"
        assert r.issue_file == anchor / "IM-007.md"
        assert r.lock_path == anchor / ".locks" / "IM-007.lock"

    def test_integer_normalize(self):
        anchor = self._legacy_anchor()
        (anchor / "IM-007.md").write_text(
            "---\nid: IM-007\n---\n", encoding="utf-8"
        )
        r = idresolve.resolve_issue_paths("im-7", None, anchor)
        assert r.canonical_id == "IM-007"
        assert r.lock_path == anchor / ".locks" / "IM-007.lock"

    def test_not_found_min_width_canonical(self):
        anchor = self._legacy_anchor()
        r = idresolve.resolve_issue_paths("IM-42", None, anchor)
        assert r.canonical_id == "IM-042"
        assert not r.issue_file.exists()


class TestKnownPrefixes(_StoreBase):
    def test_store_mode_unions_dirs_and_registry(self):
        store = self.tmp / "store"
        (store / "IM").mkdir(parents=True)
        (store / "DOUG").mkdir(parents=True)
        (store / ".repos").write_text(
            "issue-mgmt:IM\nother:RM\n", encoding="utf-8"
        )
        known = idresolve._known_prefixes(store, None)
        assert {"IM", "DOUG", "RM"} <= known

    def test_legacy_mode_reads_prefix_sentinel(self):
        anchor = self.tmp / ".issues"
        anchor.mkdir(parents=True)
        (anchor / ".prefix").write_text("IM\n", encoding="utf-8")
        assert idresolve._known_prefixes(None, anchor) == {"IM"}


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
