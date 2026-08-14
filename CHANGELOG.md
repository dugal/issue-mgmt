# Changelog

## [Unreleased]

## [v3.3.1] - 2026-08-14

### Changed
- Deleted the vestigial `specs/interchange/` directory -- reference scripts and a
  JSON schema describing the frontmatter-to-bd projection for the `beads` workflow
  engine retired in v2.11.0. bd is gone, nothing imported or shipped these
  artifacts (dev-tree only, excluded from the tarball), and the schema duplicated
  the frontmatter it described. The workflow-interface concept now lives only
  where it is authoritative: `docs/requirements.md` / `docs/spec.md` "Workflow
  engine (frontmatter-native)" (IM-233).
- Removed the dead sibling-subprocess reindex path (`resolve_reindex` /
  `run_reindex`, plus `tests/test_reindex.py`) from `lib/reindex.py`. Since the
  v3.0.0 consolidation every mutating verb regenerates INDEX.md in-process via
  `reindex_after_mutation`, and the flat `reindex-issues` binary is a tombstone
  stub, so nothing resolved or subprocessed a sibling regenerator anymore. Also
  corrected stale docstrings/comments (in `lib/reindex_core.py`,
  `lib/cli/admin_reindex.py`, `lib/frontmatter.py`, `lib/cli/admin_init_store.py`)
  that still asserted a live bash twin or a pending "WP7" cutover the v3.0.0
  consolidation already completed (IM-234).

### Packaging notes
- The Makefile migrated to release-mgmt template schema 18. Schema 17 adds a
  `strict-leaks` pre-publish gate to `release-github` (the public release fails
  closed if any personal-data/host token leaks), and schema 18 builds the
  released tarball from the `publish-source` scrub mirror, so the installable
  tarball's bytes and formula sha256 match the scrubbed public repo (RM-147 /
  DOUG-080).

## [v3.3.0] - 2026-08-08

### Changed
- `issue search` now matches an issue's **title** as well as its body, so an
  issue is found by what it is about even when its body is empty (the common
  case right after `issue create`) -- e.g. `issue search dry-run` finds an issue
  titled "add --dry-run flag". Previously the free-text query scanned the body
  only; the title (a frontmatter field) was reachable solely through the
  `--status`/`--type`/`--prefix` filters. The frontmatter filters and exit
  conventions are unchanged (IM-232).

### Added
- The `issue search --json` per-issue object gains a `title_match` boolean
  (true when the query hit the title), alongside the existing `body_matches`
  list; `--json title_match` is selectable in the gh-style field list. TSV
  output is unchanged (it already shows the title). New verification
  `tests/verification/search-issues.sh` title-matching section, plus the
  README Quickstart search step is now exercised by
  `tests/verification/im-231-quickstart.sh` so the documented flow can't
  silently break (IM-232).
