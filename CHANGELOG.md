# Changelog

## [Unreleased]

## [v3.4.0] - 2026-11-10

### Added
- Dev-tree CLI vocabulary registry and surface linter. `docs/cli-vocabulary.json`
  declares what the surface is allowed to MEAN -- all 64 flag names mapped onto
  60 concepts, each with a meaning, a canonical spelling, a default value space,
  the destinations it binds, and the deviations a documented capability licenses
  -- and `scripts/surface-lint` checks the IM-245 manifest against it. Six
  checks, each keyed to the `surface.*` clause it enforces: every flag name
  registered (both directions, so a registry entry for a spelling the CLI lacks
  fails too), one concept per spelling, value space uniform unless licensed,
  synonym sets uniform wherever a concept appears, each verb class's obligatory
  flags, and `-h` as the sole short flag. A licensed deviation must cite a
  `[REQ:]` key that exists in `docs/requirements.md`, so a licence terminates in
  the requirements document rather than in the registry's own prose. The lint is
  green on today's tree only via a 24-entry waiver baseline owned by seven open
  issues, and the baseline is shrink-only by construction: a waiver matching no
  finding fails as obsolete, a waiver whose issue is done or cancelled fails as
  unowned (`--check-issues`), and a waiver cannot suppress either of those.
  `tests/test_surface_lint.py` (46 tests) rides `make test` and proves per entry
  that removing it turns the lint red, plus one injected violation per check;
  `tests/verification/surface-vocabulary.sh` carries the black-box half. One
  authority per rule, so this linter does not re-implement `surface.self-naming`
  (IM-239's `SelfNamingTests`) or the `--store` column (IM-245); the clauses no
  manifest can answer are listed in the registry's `unenforced` table with the
  authority or owning issue for each, and a test asserts every `surface.*`
  clause is either enforced by an observed check or listed there. No shipped
  behavior change: the linter is excluded from the release tarball, as the
  extractor and `scripts/gen-formula` already are. (IM-246)
- `--json` on the six admin verbs that shipped without it -- `issue admin
  snapshot`, `init-project`, `init-store`, `install-skills`, `reindex`, and
  `selftest`. `docs/requirements.md` `output.json-stable` promises a stable
  `--json` shape on every subcommand, so an agent scripting these verbs
  reasonably reached for one and got an exit-2 usage error instead; the six are
  exactly the scaffolding and maintenance verbs a setup-automation agent drives
  non-interactively, where structured output matters most. Each has one
  documented shape whose key set is present on every outcome, so a caller
  branches on a field rather than on key presence, and under `--json` stdout
  carries exactly one JSON document with every progress line and notice on
  stderr. The shapes:
  `snapshot` -> `{store, committed, commit, paths}`;
  `init-project` -> `{repo, prefix, store, registry, already_registered,
  template_seeded, prefix_file, prefix_file_written, sentinel,
  sentinel_written}`;
  `init-store` -> `{store, dry_run, store_created, git_initialized, seeded,
  present, committed}`;
  `install-skills` -> `{source, dest, installed, count, cheatsheet}`;
  `reindex` -> `{mode, targets, indexes_written, issues}`;
  `selftest` -> `{result, checks, ok, skipped, failed}`.
  Human output, exit codes, and every other flag are unchanged. `snapshot
  --json` additionally reports WHICH paths the catchup sweep swept, which the
  prose line never did, and `selftest --json` emits its document on failure too,
  naming the failed check in `failed`. Only `--json` was added -- `--store` and
  `--dry-run` gaps on these verbs are tracked separately. (IM-240)
- Dev-tree CLI surface-change detector: `docs/cli-surface.json` is a committed,
  deterministic record of every `issue` verb's flags, positionals, `prog`
  strings, mutually exclusive groups, declared verb class, store kind, declared
  generation-metadata fields, and structured-errors conversion state. A unit
  test (`tests/test_cli_surface.py`) regenerates it in-process and
  byte-compares, so any surface change turns `make test` red until the manifest
  is regenerated with `make cli-surface` -- which is to be run only together
  with a lint pass, never to silence the test. `make cli-surface-check` is the
  non-mutating verdict. No shipped behavior change: the extractor
  (`scripts/cli_surface.py`, `scripts/cli-surface`) is excluded from the release
  tarball, as `scripts/gen-formula` already is. (IM-245)
- Every `lib/cli/` verb module now declares `SURFACE_CLASS`, `SURFACE_STORE`,
  and `SURFACE_STRUCTURED_ERRORS` (plus `SURFACE_GENERATION_FIELDS`,
  `SURFACE_SYNONYMS`, `SURFACE_PARSER`, or `SURFACE_MANUAL_FLAGS` where they
  apply); the extractor fails loud on a module missing one rather than emitting
  a partial manifest. The five admin verbs whose dispatcher subparser is a
  `prefix_chars="\x00"` passthrough are represented truthfully instead of as
  flagless -- three expose their internal parser through a new `build_parser()`
  seam, two declare their hand-walked flags literally. (IM-245)
- The two hand-walked-argv admin verbs are guarded against declaration
  drift: a test probes the real CLI with every flag name on the surface that
  the verb does not declare and requires each to be rejected, so a flag added
  to the argv loop without a matching `SURFACE_MANUAL_FLAGS` entry fails
  `make test` instead of silently reading as absent. Caught on the
  IM-245/IM-240 integration merge, where IM-240 taught both verbs `--json`
  and the declaration still reported only `--help`/`--version` -- the
  false-CLEAN failure the extractor exists to prevent, arriving through its
  own escape hatch. (IM-245, IM-240)
- Four `make` targets now give the CLI surface a human-invokable verdict, and
  the release ceremony names one of them. `make cli-surface` regenerates
  `docs/cli-surface.json`, `make cli-surface-check` reports whether the
  committed manifest is current, the new `make surface-lint` reports whether
  the surface that manifest records is ALLOWED (running `scripts/surface-lint
  --check-issues`, so a waiver whose owning issue has closed fails too), and
  the new `make surface-check` composes the last two into the single verdict a
  pre-commit hook, a CI step, or a release prep can run. The manifest answers
  what the surface is; the lint answers whether it is allowed to be that --
  detect, then validate. Both gates already rode `make test` through module
  discovery before this change, so what is new is the invokable verdict plus
  the discipline, not the enforcement: a release whose diff touches the
  surface now requires a green `make surface-check` and the regenerated
  manifest in the same prep commit (`surface.stability`). Regeneration cannot
  be used to escape the chain -- after regenerating an unregistered flag into
  the manifest, `make test` stays red through the lint's own test, so the flag
  has to be registered deliberately. Note that `--check-issues` fails soft
  when no store resolves, so in a fresh clone or a container that one arm
  no-ops and only the finding checks run. (IM-247)

### Changed
- `issue ready` now prints human-readable text by default; pass `--json` for the
  machine-readable shape. `--text` is still accepted for one release but hidden
  from `--help`, since it now names the default rather than an opt-out. (IM-241)
- Read verbs report emptiness through the exit code, one rule per class.
  Enumeration reads (`search`, `list`, `ready`, `index`, `history search`,
  `history activity`) exit 1 when they emit no rows -- `index`, `history search`,
  and `history activity` previously exited 0. Point reads (`show`,
  `history show`, `history at`) exit 0 when the record was found, 1 when it was
  not. Zero-row output still renders; only the exit code changed. (IM-238)
- Scoping a verb to a `--prefix` the store does not have is now an argument error
  (exit 2, `error=unknown-prefix`, naming the unknown value and the known set)
  rather than an empty result, so a typo stays distinguishable from a genuinely
  empty scope. Store mode only. (IM-238)
- `issue ready` no longer prints the `filtered_out` section by default; pass
  `--filtered all` to include it. The `--json` key remains present with an
  unchanged type, and an empty ready set points at the flag on stderr. (IM-249)
- `--prefix` is the spelling for prefix scoping on every verb that scopes by
  prefix (`search`, `list`, `index`, `ready`, `admin reap`, `history search`,
  `history activity`). `--repo` still works there for one release but is hidden
  from `--help`, and keeps its distinct meaning -- the registry repo basename --
  on `create`, `admin init-project`, and `admin migrate`. (IM-237)
- `--kind` and `--dep-note` are the dependency-edge spellings on both `issue dep`
  and `issue edit`. `dep`'s `--type` / `--note` and `edit`'s `--dep-type` are
  accepted for one release and hidden. `--type` now means issue type only and
  `--note` a closing note only. (IM-241)
- `--limit 0` means unlimited on every capped verb; `history show` and
  `history activity` previously rejected 0. Negative values remain exit 2, with
  the message naming the accepted range. (IM-241)
- `issue admin snapshot` spells its store-commit-lock budget `--commit-timeout`;
  `--timeout` is accepted for one release and hidden. `--lock-timeout` uniformly
  means the per-record advisory lock, and each flag's `--help` names the lock it
  bounds. (IM-242)
- `issue index --json` gained a `prefix` key; the existing `repo` key is retained,
  duplicating it. (IM-237)
- The `issue index` board labels its scope `(prefix IM)` rather than `(repo IM)`,
  whichever flag the caller used. (IM-237)

### Fixed
- `issue list` and `issue search` dropped all but the last value of a repeated
  `--status`, while `issue index` accumulated it. The same two-status query
  therefore returned one status on two of the three verbs, with exit 0 and no
  warning -- measured on a real store at 73 open + 184 done via `--status
  open,done` versus 184 done alone via `--status open --status done`. A wrong
  answer that looks like a right one, which is worse than an error, and exactly
  the `surface.one-meaning-per-flag` failure mode: an agent that learns the
  repeated form on `index` carries it to `list` and is quietly misled.
  All three verbs now accept both spellings, and mixing them in one invocation
  resolves to the union (`--status open --status done,cancelled` returns all
  three). The change is additive -- `action="append"` plus the comma split the
  `STATUS[,STATUS...]` metavar already advertised -- so every query that worked
  before returns the same rows. Registration moved to one
  `common.add_value_set_flag` helper rather than three inline copies, so a
  fourth verb cannot reintroduce the split; `--type` and `--prefix` carry the
  same axis and adopt it when their own issues land. Found by the IM-246
  surface linter's value-space check, which treats accumulation as part of a
  flag's value space precisely because the difference is agent-visible, and
  fixing it shrank that linter's waiver baseline from 24 to 22. (IM-269)
- Five shipped diagnostics named commands that no longer exist. `issue admin
  migrate --dry-run` printed `registry: ABSENT (run init-issue-project after
  migration)` -- a retired binary offered as the command to run next, which is
  the case `surface.self-naming` names explicitly -- and `issue admin reap`
  (store unresolvable), `issue admin install-skills` (brew absent), `issue
  admin snapshot --help` and `issue admin init-store --help` each named
  `migrate-to-store`, `issue-mgmt-install-skills`, or `new-issue`. `issue edit
  --help` also pointed at an "edit-issue reference", a document that does not
  exist under that or any name. All six now name the current verb;
  `install-skills`, which names itself, interpolates its own `PROG` and so
  cannot drift again.
  These survived IM-239's self-naming check because that check reads three
  AST-harvested manifest fields -- `PROG` assignments, `<name>: ` message
  heads, and lines under a usage heading -- and none of the six sits in any of
  those shapes. Reverting each fix in turn leaves that half reporting clean on
  all six, so the gap was structural rather than an oversight. The check now
  also drives `--help` for every verb in the manifest (a new verb is covered
  without being listed) plus three probes for output no `--help` reaches, with
  each probe asserting a sentinel of its own output first so a probe whose
  condition stops firing fails instead of silently scanning nothing. The
  retired-name set is derived from the one place the project enumerates all 21
  pre-v3.0.0 flat names and is checked in both directions; the historical
  qualifier that licenses a mention ("Ported from the flat `X` binary") is
  matched per line rather than per page, so one qualified sentence cannot
  excuse an unqualified instruction further down the same page. (IM-247)
- The test suite now uses ONE throwaway git identity instead of nine. Every
  verification script and python test that commits needed a git identity, the
  address is never asserted on, and each one invented its own named after
  whichever issue needed it -- so `.publish.yaml` `leaks.allow` had to
  enumerate all nine by full address. The publish detector suppresses an email
  finding only on a full-span match, so a domain-level token suppresses
  nothing and only the complete address works; that made each new fixture a
  separate hand-written allowlist entry, and three of four authors forgot.
  Twice inside this phase, and both times the first report came from the
  publish gate, where it blocks a release: the gate was red on the integration
  branch for a day with nothing failing locally.
  The fix removes the need rather than making the list easier to extend. The
  address is defined once in `tests/fixture_identity.py`; python tests import
  it and `tests/verification/lib.sh` parses that same assignment into
  `FIXTURE_EMAIL`, so there is no second copy to keep in step. `leaks.allow`
  drops from fourteen entries to six -- two email addresses, the shared
  fixture one and the `issue admin selftest` store identity, which is shipped
  runtime code and so cannot import a test constant. A new test has no
  address to invent.
  Shrinking that list is itself the point: an `allow` entry suppresses a
  `deny` hit inside its span, so each one is an unreviewed override of the
  deny list -- a bare-username address at a fixture domain reports only as
  `email`, and allowing the address quiets the username token entirely, which
  is the shape of a leak this project has already shipped once.
  Guarded by `scripts/check-fixture-emails`, riding `make test` and
  `project-meta.sh` rather than only the publish gate: every email-shaped
  literal in the public view must be the shared identity or an explicit
  `leaks.allow` entry, no allowlisted address may be unused, and no entry may
  span a deny token. Both sides are derived -- the address from the constant,
  the allowlist and the public file set from `.publish.yaml` -- and the checker
  refuses rather than reporting clean when either derivation comes back empty.
  Dev-tree only; nothing ships. (IM-268)
- Error, usage, and help text across thirteen verbs named the retired flat
  binaries that v3.0.0 tombstoned and v3.1.0 deleted, so the CLI was teaching
  callers commands that no longer exist. `issue admin migrate --bogus` printed
  a usage block headed `migrate-to-store`; `issue admin init-store --help`
  printed four copy-pasteable `init-issue-store ...` examples; `issue create
  -h` told the caller to run `init-issue-project`; every `issue history`
  diagnostic was prefixed with its flat name (`issue-history: --limit must
  be ...`), including the gh-style `--json` field-selection error, the one
  message specifically designed for an agent to key on; and `issue admin
  selftest`'s entire PASS report named seven retired binaries -- in the verb
  whose only job is confirming an install works. An agent reading any of
  those reasonably retries the named command and gets command-not-found.
  Every one is now the current `issue <verb>` form.
  The remedy is structural rather than ~95 corrections: each module declares
  its name once as `PROG = "issue <verb>"` and formats every diagnostic,
  usage line, and `prog=` keyword through it, so the surface manifest records
  exactly one name per verb and the self-naming rule is one comparison per
  verb instead of a grep over ~187 source occurrences (most of which are
  legitimate lineage docstrings). Three things fell out of doing it that way:
  `admin selftest`'s retired-name routing table was DELETED rather than
  exempted -- it keyed its CLI invocations on flat names and fell back to a
  flat sibling that cannot exist, so re-keying on the consolidated verb path
  removed the table and the eight message prefixes built from its keys;
  `issue transition` bound its prefix as a function-scope lowercase local,
  which no manifest field had been reading; and `issue create`'s hand-written
  usage line was correct but hand-copied, so it now formats through `PROG`
  too. Dev-tree lint added with it, every acceptance rule derived rather than
  listed: a printed name must be the verb's own prog, a prefix or subcommand
  extension of any verb's prog, or an error-condition key read from the
  `error_class` attributes in `lib/errors.py`. A verb that prints no command
  name is not required to declare one, so no declaration exists without a
  consumer. Five verification guards that had pinned the retired strings now
  assert the current ones; four of those were asserting a `--help` "banner"
  that in fact matched only the qualified lineage note the clause permits, so
  they proved nothing and now name the live verb. (IM-239)
- The CLI surface manifest's hand-declared fields are now checked against the
  code rather than against the manifest. `docs/cli-surface.json` is two
  artifacts in one file: the argparse-sourced half genuinely tracks the code,
  but six field families (`SURFACE_CLASS`, `SURFACE_STORE`,
  `SURFACE_STRUCTURED_ERRORS`, `SURFACE_GENERATION_FIELDS`,
  `SURFACE_SYNONYMS`, `SURFACE_MANUAL_FLAGS`) are module constants, and for
  those the regeneration diff test was TAUTOLOGICAL -- both sides read the same
  constant, so it could only detect that someone had edited a declaration,
  never that the declaration was wrong. Two were already wrong:
  `SURFACE_GENERATION_FIELDS` was declared complete on two verbs while
  `issue create --json` emitted a wall-clock `created` and
  `issue note --json` a wall-clock `date` (so the tree that introduced
  `surface.explicit-output-mode`'s ambient-value clause already violated it),
  and `SURFACE_STRUCTURED_ERRORS` read `unconverted` on all 28 verbs, which
  made the conversion ratchet unfalsifiable. Each family now terminates at
  something that is not another declaration: the passthrough verbs' flags come
  from an AST walk of the argv loop they describe (a mismatch either way fails
  the manifest BUILD, not merely a test); the ambient output fields are
  MEASURED against each verb's real `--json` by the new
  `tests/test_surface_ambient_fields.py`, which drives all 28 `--json` verbs
  against a fixture store dated 2020 in its frontmatter, bodies, and git commit
  dates, so a value carrying the run's own clock can only be ambient;
  `store == "flag"` is checked against the flag registrations, with the two
  real gaps (`admin reap`, `admin reindex`) carried as a named shrink-only
  waiver instead of the prose `# gap:` comments that would go silently false;
  the `structured_errors` converted set is compared against the
  `surface.structured-errors` clause text parsed out of
  `docs/requirements.md`, so converting a verb and amending the clause cannot
  drift apart; and a `SURFACE_SYNONYMS` entry naming a spelling the verb does
  not accept is now a dead declaration that fails. Each guard ships a negative
  test that injects the violation and asserts it fails. Dev-tree only -- no
  shipped behavior change. (IM-262)
- The surface manifest recorded no program name at all for the three verbs
  whose self-naming has drifted furthest, so a `surface.self-naming` lint keyed
  on `prog.declared` would have reported clean on exactly them.
  `issue admin selftest` and `issue admin install-skills` build their parsers
  with `prefix_chars=chr(0)` and pass no `prog=`, and `issue transition` binds
  its message prefix as a function-scope `prog = "transition-issue"` that the
  harvest did not look for -- while `admin selftest`'s whole PASS report names
  seven retired binaries and both passthrough verbs print a hand-written
  `Usage:` line naming a retired flat binary. The manifest now also records,
  derived by AST: lowercase `prog` assignments, every command-name-shaped
  `<name>: ` message prefix a module prints, and the program name in any
  hand-written `usage:` / `Synopsis:` block. Matching is shape-based rather
  than keyed to a list of retired names, which would itself be an unguarded
  declaration. Recording only -- the drifted text is corrected by IM-239.
  (IM-262)
- The public README Quickstart failed at step 5 as written -- the third
  Quickstart breakage in a shipped release, after IM-231's missing `git init`
  and the v3.2.1 search example. Step 4 completed the store's only issue, so
  step 5's `issue ready --prefix MYP`, commented "see what is claimable right
  now", was guaranteed to print an error and exit 1 for every new user who ran
  the block top to bottom. `ready` lists OPEN work, so it is meaningful only
  before the lifecycle steps, not after; the order is now create -> ready ->
  start/complete -> show/search. Step 2 is also executable now rather than a
  `cd` into a directory the reader has not created yet. The guard added for
  the FIRST breakage could not catch this one and did not: it claimed in its
  own header to run the quickstart "exactly as the README documents it" while
  being a hand-written re-implementation that had drifted in three ways -- it
  skipped README step 4 entirely (so `start`/`complete` never ran and `ready`
  was called while the issue was still open, accidentally testing the correct
  order while the README documented a broken one), it passed the retired
  `--repo` synonym where the README documents `--prefix`, and it passed a
  `--non-interactive` the README does not. The README is now the ground
  truth: the guard EXTRACTS the fenced block under `## Quickstart` and runs it
  line by line, asserting each exits 0, with no placeholder substitution --
  `HOME` is pinned to a throwaway tree so the block's own `~` paths expand
  inside it, and a separate assertion requires every executed command to
  appear literally in `README.md`. Any future change to the README's steps,
  flags, or order is exercised with no edit to the guard. Also corrected: the
  README said `issue admin init-project` "writes the config and sentinel for
  you". It writes the sentinel and `.prefix`; `~/.config/issue-mgmt/config` is
  written only on the interactive-prompt path, never on the `--store` flow the
  Quickstart documents -- so a user told the config was written for them, who
  later runs a verb from a directory with no sentinel, hits the
  `reason=store-unresolvable` payload instead. All three persistence facts are
  now pinned by assertions. (IM-266)
- The shipped cheat sheet's only statement about argument POSITION was wrong
  for four of the five flags it named. It said `--store`, `--json`,
  `--lock-timeout`, `--no-reindex` and `--version` were "accepted before the
  verb args"; in fact the dispatcher attaches the first four PER VERB, so
  `issue --store ROOT show IM-001` exits 2 reading `ROOT` as the verb, and
  only `--version` worked. The sheet installs next to the skills and is what a
  session reads to learn the CLI, so a session that learned flag placement
  there wrote commands that failed. It now states that flags follow the verb,
  that `--version`/`--help` are the dispatcher's own, and that which verb takes
  which flag is not uniform. Its read-verb exit guidance also gave only the
  grep convention; it now carries both per-class rules from
  `output.exit-codes` plus the two non-empty cases a caller must not read as a
  zero result (absent `--prefix` as exit 2 `error=unknown-prefix`, and an
  unusable store as `reason=store-unresolvable`). The sheet's content had no
  guard of any kind: every test touching it checked only that it gets
  INSTALLED, so its verb and skill inventories were correct by luck rather
  than construction. New `scripts/check-cheatsheet` diffs them against
  `docs/cli-surface.json` and the skills directory, and the new
  `tests/verification/cheatsheet.sh` asserts the placement claim the only
  honest way -- by EXECUTING each claimed placement and requiring each
  per-verb flag to be rejected before the verb and accepted after it.
  Verb-token detection is scoped to inline code spans and fenced blocks
  rather than filtered through a deny list of prose words, so the check needs
  no hand-maintained list; the tradeoff and its accepted limit are recorded on
  IM-267. (IM-267)
- Two shipped skills told the agent that a zero-result history query exits 0.
  The CLI has returned 1 since IM-238 flipped the enumeration reads in this
  same phase, and both skills' own error tables read exit 1 as "legacy mode
  active / store root not a git repo / prefix directory missing" -- so a
  perfectly normal empty `issue history search` or `issue history activity`
  was reported to the user as a broken store. The IM-228 guard
  (`skill-exit-codes.sh`) stayed green throughout, because it checks only the
  exit-code CEILING and never which code a skill maps to a condition. Both
  skills now state 1 and explain that 1 is double-booked, with the stderr line
  as the discriminator rather than the code. Fixed alongside three more
  declarations in the same artifact class: `issue-create` documented
  `--prefix` as forwarded to `issue create`, which has no such flag (it is
  `--repo`), so the agent got exit 2 `unrecognized arguments` and the skill's
  own table reads exit 2 as a bad argument VALUE; `issue-block` asserted that
  "the CLI does not have a special `unblock` verb", which it has had since
  v3.0.0; and the same file restated the legal-transition matrix and had
  drifted from it, omitting `cancelled -> blocked`. That restatement is
  removed rather than corrected -- `state.canonical-states` already requires
  the matrix not be duplicated in skills, and correcting a copy leaves the
  mechanism that drifted. New `scripts/check-skill-contract` adds the
  condition-level checks the ceiling check cannot make, each derived from
  ground truth: a read skill's zero-result code against its verb's class in
  the surface manifest, `concurrent-edit` membership on every mutating verb
  from the `EXIT_*` constants, and each documented flag against the verb it is
  declared passed to. `skill-exit-codes.sh` also widens from
  `.claude/skills/` to every shipped markdown surface that names an exit code
  -- the installed cheat sheet and the two `share/store-template/` files that
  `issue admin init-store` copies into every new store, which IM-228 had to
  correct by hand for exactly this reason. (IM-265)
- `issue list --json title_match` exited 2 with `unknown --json field` while
  bare `issue list --json` emitted `title_match` on every row -- the verb
  rejected selection of a field it demonstrably produced. Shipped in v3.3.1.
  `lib/cli/list.py` and `lib/cli/search.py` each carried a hand-maintained
  `VALID_JSON_FIELDS` tuple, both commented as a restatement of one function's
  output (`search.IssueMatch.to_dict`), and IM-232 updated only `search`'s copy
  when it added the field. That also broke `cli.list`'s promise that `list`'s
  output shape matches `issue search` run with an empty query. Both tuples are
  now DERIVED from the row builder via a new `IssueMatch.json_fields()`
  classmethod, so a key added to `to_dict` is selectable on both verbs by
  construction rather than by remembering to edit two more places. The guards
  that were missing: a test drives all nine field-selecting read verbs through
  the real CLI and requires each declared set to equal the keys it actually
  emits -- in both directions, since a declared-but-unemitted field is accepted
  and then silently dropped -- exercising `show` across its three modes so no
  verb needs a conditional-field waiver; `issue-list.sh` gained the
  `list`/`search ""` parity section the `cli.list` clause never had (the script
  had never invoked `search` at all); and the unknown-field assertion now pins
  the valid set the error advertises, plus that every advertised field is
  really selectable. The old assertion checked only that the error named the
  BAD field, which stayed true for the whole life of the defect. (IM-263)
- The Claude Code skill chain (`.claude/skills/` -> tarball -> formula ->
  `<pkgshare>/skills/` -> `~/.claude/skills/`) carried no set-or-count
  assertion against ground truth at any link, and `LICENSE` had never shipped.
  `gen-formula` enumerated the skill files by hand and silently omitted
  `issue-dep` and `issue-note`; its only guard compared the generated formula
  against a second hand-written list in `im-029-homebrew.sh` that had declared
  13 of the 15 shipped skills since v2.0.0, so neither side was ever compared
  to the skills directory. The failure direction was the quiet one -- a formula
  naming a file the tarball lacks fails `brew install` loudly, while a tarball
  skill the formula omits installs nothing and fails nothing, and
  `<pkgshare>/skills/` is the only place `issue admin install-skills` reads
  from. `issue-note` is a mutating skill, so an agent-facing surface was
  silently absent with nothing red. The formula now installs the staged
  `skills` DIRECTORY, the way `lib` and `store-template` already were, so it
  names no skill and has no list to drift; every test-side skill name and
  count is derived from `.claude/skills/<name>-dev/`. (IM-264)
- `LICENSE` is now in the distribution tarball and installed under `doc/`. It
  was tracked from v3.1.0 and present in none of the three hand-maintained
  packaging lists while the formula declared `license "MIT"`, so a
  brew-installed `issue-mgmt` carried no license text. Both halves of that gap
  are closed structurally rather than by adding a fourth list entry: the `tar`
  include list is derived from the staging tree (sorted, so the archive stays
  byte-reproducible), making staged-but-untarred impossible, and the new
  build-time `scripts/check-tarball-sources` fails `make dist` when a path
  declared in `SOURCES` has no content-identical member in the built tarball --
  matching by sha256 rather than by path, so it survives the staging renames
  without a second copy of the rename rules. That assertion is the one that
  would have caught IM-145. (IM-264)
- `issue list` and `issue search` reported an unresolvable issue store by exiting
  1 with zero bytes on both streams -- indistinguishable from "matched nothing"
  under the grep convention those verbs follow, so an unconfigured store looked
  exactly like an empty one. All four enumeration reads (`list`, `search`,
  `index`, `ready`) now report an unusable store through one structured payload:
  `error=precondition-failed reason=store-unresolvable`, the same condition as a
  one-line JSON object under `--json`, exit 1, naming the path in `store=` when a
  root resolved but was absent. `index` previously emitted unbranchable prose and
  `ready` was already correct; they are now the same shape. Legacy `.issues/` mode
  is unaffected -- an existing corpus is still a usable corpus, and a genuine
  zero-result there remains a silent exit 1. (IM-253)
- `issue admin init-project` exited through an unhandled `FileNotFoundError`
  traceback when the resolved store root did not exist: it lazy-seeded
  `ISSUE-TEMPLATE.md` into the directory without first checking the directory was
  there, handing a caller a Python stack trace where a branchable payload was
  expected. It now refuses with the same structured payload as above, pointing at
  `issue admin init-store`. (IM-252)
- `issue admin init-project` no longer synthesizes `$HOME/issues-store` when its
  store-root prompt reads an empty line or EOF, and no longer persists that guess
  to `~/.config/issue-mgmt/config`. `issue admin init-project </dev/null` reached
  that built-in default by EOF and then failed inside it; `docs/requirements.md`
  `substrate.store-root-resolution` states that no built-in default path is
  shipped. A piped answer is still a real answer and is still accepted -- only an
  empty read refuses. (IM-252)
- User-facing messages from `issue ready` and `issue admin init-project` prefixed
  themselves with the retired flat binary names (`issue-ready:`,
  `init-issue-project:`) rather than the verb a caller can actually run. Both now
  name their current form. (IM-253, IM-252)
- `issue edit <id> --set status=done` told the caller to "use `transition-issue`
  to change status" -- a binary retired in v3.0.0. The guarded-field message is
  the one place the CLI hands a caller a command to run next, so an agent
  following it invoked something that no longer exists. It now names
  `issue transition`. (IM-254, IM-239)
- `issue admin selftest` returned a bare exit 1 with nothing on either stream
  when `git config --global` failed inside its temp HOME -- the one failure path
  in the smoke that did not name itself. It now fails by name like every other
  check. (IM-240)

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
