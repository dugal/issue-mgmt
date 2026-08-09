# Changelog

## [Unreleased]

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
