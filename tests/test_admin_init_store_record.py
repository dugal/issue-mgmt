#!/usr/bin/env python3
"""Unit tests for `issue admin init-store`'s scaffold record (IM-240).

`--json` and the human report are two renderings of one record, so the
record is what the tests pin: the preview pass and the real pass must
produce the SAME key set with different values, which is what makes the
verb's output shape a function of argv rather than of store state
(`surface.explicit-output-mode`). The preview marker is `dry_run`, the
field name `issue admin reap --json` already uses.

End-to-end CLI behavior (stdout purity, --help documentation) is covered
by tests/verification/im-240-admin-json.sh; these tests pin the record
builders and the human emitters.

Run:
    python3 -m unittest tests.test_admin_init_store_record -v
"""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))

from cli import admin_init_store as ais  # noqa: E402

# The record's documented key set, in the order --help lists it.
RECORD_KEYS = {
    "store", "dry_run", "store_created", "git_initialized",
    "seeded", "present", "committed",
}

SCAFFOLD_NAMES = [
    "README.md", "LAYOUT.md", ".gitignore", "INDEX.md", "ISSUE-TEMPLATE.md",
]


class InitStoreRecordBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        # Stand-in scaffold sources: the record builders only care that the
        # files exist and are copyable, not what the shipped ones contain.
        self.templates = self.tmp / "store-template"
        self.templates.mkdir()
        (self.templates / "README.md").write_text("readme\n")
        (self.templates / "LAYOUT.md").write_text("layout\n")
        self.issue_template = self.tmp / "ISSUE-TEMPLATE.md"
        self.issue_template.write_text("template\n")

    def plan(self, store):
        return ais.scaffold_dry_run_plan(
            Path(store), self.templates, self.issue_template
        )

    def scaffold(self, store):
        return ais.scaffold(
            Path(store), self.templates, self.issue_template
        )

    def public_keys(self, record):
        return {k for k in record if not k.startswith("_")}


class TestDryRunPlan(InitStoreRecordBase):
    def test_greenfield_plan_reports_every_action_as_pending(self):
        record = self.plan(self.tmp / "new-store")
        self.assertTrue(record["dry_run"])
        self.assertTrue(record["store_created"])
        self.assertTrue(record["git_initialized"])
        self.assertEqual(record["seeded"], SCAFFOLD_NAMES)
        self.assertEqual(record["present"], [])
        self.assertTrue(record["committed"])

    def test_plan_changes_nothing_on_disk(self):
        target = self.tmp / "untouched"
        self.plan(target)
        self.assertFalse(target.exists())

    def test_plan_exposes_only_the_documented_keys(self):
        record = self.plan(self.tmp / "new-store")
        self.assertEqual(self.public_keys(record), RECORD_KEYS)

    def test_plan_on_existing_git_store_plans_no_git_init(self):
        store = self.tmp / "existing"
        store.mkdir()
        (store / ".git").mkdir()
        record = self.plan(store)
        self.assertFalse(record["store_created"])
        self.assertFalse(record["git_initialized"])


class TestScaffoldRecord(InitStoreRecordBase):
    def test_greenfield_scaffold_seeds_every_file(self):
        record = self.scaffold(self.tmp / "fresh")
        self.assertFalse(record["dry_run"])
        self.assertTrue(record["store_created"])
        self.assertTrue(record["git_initialized"])
        self.assertEqual(sorted(record["seeded"]), sorted(SCAFFOLD_NAMES))
        self.assertEqual(record["present"], [])

    def test_converging_rerun_reports_present_and_seeds_nothing(self):
        store = self.tmp / "converge"
        self.scaffold(store)
        record = self.scaffold(store)
        self.assertEqual(record["seeded"], [])
        self.assertEqual(sorted(record["present"]), sorted(SCAFFOLD_NAMES))
        self.assertFalse(record["store_created"])
        self.assertFalse(record["git_initialized"])
        self.assertFalse(record["committed"])

    def test_key_set_is_identical_across_preview_and_real_runs(self):
        # The property that lets a caller branch on `dry_run` instead of
        # on key presence.
        preview = self.plan(self.tmp / "parity")
        real = self.scaffold(self.tmp / "parity")
        self.assertEqual(self.public_keys(preview), self.public_keys(real))
        self.assertEqual(self.public_keys(real), RECORD_KEYS)

    def test_store_path_is_reported_as_a_string(self):
        store = self.tmp / "stringy"
        record = self.scaffold(store)
        self.assertEqual(record["store"], str(store))


class TestJsonEmission(InitStoreRecordBase):
    def test_emit_json_drops_the_private_render_detail(self):
        record = self.plan(self.tmp / "emitted")
        buf = io.StringIO()
        with redirect_stdout(buf):
            ais.emit_json(record)
        obj = json.loads(buf.getvalue())
        self.assertEqual(set(obj), RECORD_KEYS)

    def test_emit_json_writes_exactly_one_document(self):
        record = self.plan(self.tmp / "one-doc")
        buf = io.StringIO()
        with redirect_stdout(buf):
            ais.emit_json(record)
        # json.loads over the WHOLE stream: a stray prose line would fail.
        json.loads(buf.getvalue())
        self.assertTrue(buf.getvalue().endswith("\n"))


class TestHumanEmitters(InitStoreRecordBase):
    def test_dry_run_report_uses_the_would_wording(self):
        record = self.plan(self.tmp / "worded")
        buf = io.StringIO()
        with redirect_stdout(buf):
            ais.emit_dry_run_report(record)
        out = buf.getvalue()
        self.assertIn("would create:", out)
        self.assertIn("would run: git init -b main", out)
        self.assertIn("would seed: README.md", out)
        self.assertIn("would commit: chore: scaffold issue store", out)

    def test_scaffold_report_uses_the_past_tense_wording(self):
        record = self.scaffold(self.tmp / "done")
        buf = io.StringIO()
        with redirect_stdout(buf):
            ais.emit_scaffold_report(record)
        out = buf.getvalue()
        self.assertIn("seeded: README.md", out)
        self.assertIn("initialized: .git (branch main)", out)
        self.assertIn("committed: chore: scaffold issue store", out)

    def test_converged_report_lists_present_files(self):
        store = self.tmp / "converged"
        self.scaffold(store)
        record = self.scaffold(store)
        buf = io.StringIO()
        with redirect_stdout(buf):
            ais.emit_scaffold_report(record)
        out = buf.getvalue()
        self.assertIn("present: README.md", out)
        self.assertNotIn("initialized:", out)
        self.assertNotIn("committed:", out)


if __name__ == "__main__":
    unittest.main()
