# Makefile for issue-mgmt
#
# Bootstrapped from release-mgmt's standard template via init-makefile.
# See $(brew --prefix)/share/doc/release-notes-tools/guide.md section 4 for the
# full standard (variables, targets, dependency chain).
#
# After bootstrap:
#   1. Edit SOURCES below to list this project's release artifacts.
#   2. Customize the $(TARBALL) recipe if your tarball needs a non-flat layout
#      (subdirectories, renamed files, skill staging, etc.).
#   3. Write scripts/gen-formula to emit your Homebrew formula on stdout.

# Makefile-template-schema: 16

# VERSION resolution: three-case ladder, picked in order.
#   1. HEAD_TAG        -- HEAD is exactly on a tag (production build).
#   2. PENDING_RELEASE -- CHANGELOG has a promoted entry whose tag has not
#                         yet been cut (release-in-flight build).
#   3. dev-<describe>  -- mid-cycle / dev build. The `dev-` prefix is the
#                         scannable marker that the artifact is NOT a real
#                         release; `git describe --tags --always --dirty`
#                         carries last-release anchor + commits-past +
#                         short sha + dirty flag for full traceability.
# See `release-mgmt`'s README "Version resolution" section for the design
# rationale.
HEAD_TAG        := $(shell git describe --tags --exact-match HEAD 2>/dev/null)
PENDING_RELEASE := $(shell pending-release CHANGELOG.md 2>/dev/null)
DEV_VERSION     := $(shell git describe --tags --always --dirty 2>/dev/null || echo "unknown")
VERSION         := $(or $(HEAD_TAG),$(PENDING_RELEASE),dev-$(DEV_VERSION))
DISTDIR  := dist
TARBALL  := $(DISTDIR)/issue-mgmt-$(VERSION).tar.gz
FORMULAS := $(DISTDIR)/issue-mgmt.rb

# Staged promotion pipeline (schema 16): an optional `promote:` block
# layers a staging -> gate -> production pipeline over the per-backend blocks,
# driving `make release` (below). Empty for single-target projects, which
# publish with release-<backend> directly. When `backend:` is absent (a
# promote-only project), the local-default target falls back to PROMOTE_STAGING,
# so `make install` / `make dist` operate against the staging backend.
PROMOTE_STAGING    := $(shell publish-config promote.staging 2>/dev/null)
PROMOTE_PRODUCTION := $(shell publish-config promote.production 2>/dev/null)
GATE_ISOLATION     := $(shell publish-config promote.gate.isolation 2>/dev/null)

# Consumer-tap identity (schema 15): derived from the ACTIVE backend's
# `.publish.yaml` block via `publish-config`, not hardcoded. The per-target
# vocabulary names two identities -- `<backend>.publisher_tap` (the repo a
# formula is pushed into) and `<backend>.consumer_tap` (the `brew install`
# alias); the consumer side below reads `consumer_tap`. `2>/dev/null` + the
# `||`/`$(or ...)` fallbacks keep this working (defaulting to dugal/tap) when
# publish-config or the block is absent, so `make dist` never hard-requires the
# tool. Two mutually-exclusive publish modes, keyed to `backend:`:
#   mode a ("no NAS"):    backend local  -- file:// formula, BREWTAP origin =
#                         the local source tap (TAPDIR = local.source_dir).
#   mode b ("no local"):  backend gitea/github -- remote formula, BREWTAP origin
#                         = the remote directly (one-time `brew tap ... <url>`).
# A tap is uniformly ONE mode (one git repo, one origin). sync-tap is then a
# single `git pull` from BREWTAP's origin -- no 2-hop.
# BACKEND: the active target. Explicit `backend:` wins; otherwise fall back to
# PROMOTE_STAGING so a promote-only config (no `backend:`) still resolves a
# local-default target (schema 16).
BACKEND  := $(or $(shell publish-config backend 2>/dev/null),$(PROMOTE_STAGING))
TAP_NAME := $(shell publish-config $(BACKEND).consumer_tap 2>/dev/null || echo dugal/tap)
TAP_USER := $(firstword $(subst /, ,$(TAP_NAME)))
TAP_REPO := $(word 2,$(subst /, ,$(TAP_NAME)))
# TAPDIR: mode-a source tap (file:// backend). Defaults to the workspace tap.
TAPDIR   := $(or $(shell publish-config local.source_dir 2>/dev/null),$(HOME)/homebrew-tap)
# BREWTAP: host brew clone. $(shell brew --prefix) fixes the /opt/homebrew
BREWTAP  :=

# GITHUB_REPO is derived from the 'github' remote URL (set by publish-source
# bootstrap). Used by publish-github to build the release-asset URL stamped
# into the published formula. Empty if no 'github' remote is configured --
# publish-github errors out in that case.
GITHUB_REPO := $(shell git remote get-url github 2>/dev/null | sed -E 's|^[^@/]+@[^:]+:||; s|^https?://[^/]+/||; s|\.git$$||')

# Source files staged into the tarball (renamed/restructured in the recipe).
# `scripts/issue` is the single CLI dispatcher. Python helper modules under
# lib/ (including the lib/cli/ verb package the dispatcher discovers) ship
# alongside; skill sources under .claude/skills/<name>-dev/SKILL.md are
# renamed to skills/<name>.md during staging (the -dev suffix is the
# dev-alias convention; canonical names are what end users install).
SCRIPTS := \
  scripts/issue
LIB_FILES := $(wildcard lib/*.py) $(wildcard lib/*.sh) $(wildcard lib/cli/*.py)
SKILL_DEV_SOURCES := $(wildcard .claude/skills/*-dev/SKILL.md)
STORE_TEMPLATE_FILES := $(wildcard share/store-template/*.md)
SOURCES  := $(SCRIPTS) $(LIB_FILES) $(SKILL_DEV_SOURCES) $(STORE_TEMPLATE_FILES) ISSUE-TEMPLATE.md share/issue-mgmt/CHEATSHEET.md CHANGELOG.md README.md

# Reproducible tarballs.
#   - Release builds (VERSION starts with 'v'): HEAD commit time. This is
#     correct for HEAD-on-tag (HEAD == tag commit) and for release-in-flight
#     (HEAD == CHANGELOG-promote commit, the natural build time).
#   - Dev builds: epoch (1970-01-01). Belt-and-suspenders with the dev-
#     VERSION prefix: even if someone inspects only file timestamps, the
#     dev marker is still visible.
MTIME    := $(shell if printf %s "$(VERSION)" | grep -q '^v'; then git log -1 --format='%cd' --date=format:'%Y%m%d%H%M.%S' HEAD; else echo "197001010000.00"; fi)

# Default target
all: dist

# --- Distribution targets ---

# stamp:
#   - Release builds (VERSION starts with 'v'): VERIFY ONLY. Errors if any
#     source file's VERSION constant does not already match VERSION, with a
#     recovery hint pointing to the missing `chore: stamp vX.Y.Z` commit.
#     This is the schema-6 strict gate that catches the v2.9.2-class bug
#     where the release commit lands on stale source.
#   - Dev builds (VERSION starts with 'dev-'): auto-stamp the working tree
#     so the dev tarball carries the resolved dev VERSION. Idempotent.
stamp:
	@case "$(VERSION)" in \
	  v*) \
	    if ! stamp-version --check --version-string $(VERSION) . ; then \
	      echo "error: refusing to build $(VERSION) -- source VERSION constants are stale."; \
	      echo "hint: stamp the source first and commit it as a separate change:"; \
	      echo "        stamp-version --version-string $(VERSION) ."; \
	      echo "        git commit -am 'chore: stamp $(VERSION)'"; \
	      exit 1; \
	    fi ;; \
	  *) \
	    stamp-version --version-string $(VERSION) . ;; \
	esac

$(TARBALL): $(SOURCES) $(wildcard docs/release-notes/*.md) stamp
	@mkdir -p $(DISTDIR)
	$(eval STAGE := $(shell mktemp -d))
	@mkdir -p $(STAGE)/docs/release-notes $(STAGE)/lib/cli $(STAGE)/skills $(STAGE)/store-template
	cp scripts/issue $(STAGE)/issue
	chmod +x $(STAGE)/issue
	cp share/store-template/*.md $(STAGE)/store-template/
	cp lib/*.py lib/*.sh $(STAGE)/lib/
	cp lib/cli/*.py $(STAGE)/lib/cli/
	@# Stage skills with the canonical (non-dev) name. Source paths look
	@# like .claude/skills/issue-create-dev/SKILL.md; target is
	@# skills/issue-create.md. Done inline so a missing -dev skill source
	@# fails the build loudly.
	@for src in .claude/skills/*-dev/SKILL.md; do \
	  d=$$(dirname $$src); \
	  name=$$(basename $$d); \
	  canonical=$${name%-dev}; \
	  cp $$src $(STAGE)/skills/$$canonical.md; \
	done
	cp ISSUE-TEMPLATE.md share/issue-mgmt/CHEATSHEET.md CHANGELOG.md README.md $(STAGE)/
	@cp docs/release-notes/*.md $(STAGE)/docs/release-notes/ 2>/dev/null || true
	find $(STAGE) -exec touch -t $(MTIME) {} +
	COPYFILE_DISABLE=1 tar -cf - -C $(STAGE) \
	  issue \
	  lib \
	  skills \
	  store-template \
	  ISSUE-TEMPLATE.md \
	  CHEATSHEET.md \
	  CHANGELOG.md \
	  README.md \
	  docs/release-notes \
	  | gzip -n > $(TARBALL)
	rm -rf $(STAGE)
	@echo "Created $(TARBALL)"

dist: $(TARBALL)

# Pattern rule: builds any $(DISTDIR)/<name>.rb from $(TARBALL) by invoking
# scripts/gen-formula. For multi-formula projects (more than one entry in
# FORMULAS), the recipe passes `--formula <name>` so gen-formula can emit the
# matching formula. Single-formula projects omit the flag, preserving
# backwards compatibility with schema-4 gen-formula scripts that take only
# the tarball as a positional argument.
$(DISTDIR)/%.rb: $(TARBALL)
	@if [ $(words $(FORMULAS)) -gt 1 ]; then \
	  scripts/gen-formula --formula $* $(TARBALL) > $@; \
	else \
	  scripts/gen-formula $(TARBALL) > $@; \
	fi
	@echo "Created $@"

formula: $(FORMULAS)

$(DISTDIR)/.published: $(FORMULAS)
	@case "$(VERSION)" in \
	  dev-*) \
	    for f in $(FORMULAS); do \
	      bn=$$(basename $$f); \
	      if [ -f $(TAPDIR)/Formula/$$bn ] && \
	         grep -E '^[[:space:]]*url[[:space:]]+.*-v[0-9][^/[:space:]]*\.tar\.gz' \
	         $(TAPDIR)/Formula/$$bn >/dev/null 2>&1; then \
	        echo "error: refusing to publish dev VERSION '$(VERSION)' over a tagged release in $(TAPDIR)/Formula/$$bn"; \
	        echo "hint: the source tap formula currently points at a v* tarball."; \
	        echo "      check out the release tag (git checkout <tag>) and re-run,"; \
	        echo "      or run from a release commit before publishing."; \
	        exit 1; \
	      fi; \
	    done ;; \
	esac
	@for f in $(FORMULAS); do \
	  bn=$$(basename $$f); \
	  cp $$f $(TAPDIR)/Formula/$$bn; \
	  git -C $(TAPDIR) add Formula/$$bn; \
	done
	git -C $(TAPDIR) diff --cached --quiet || \
	  git -C $(TAPDIR) commit -m "chore: update issue-mgmt formulas to $(VERSION)"
	@touch $@
	@echo "Published $(VERSION) to $(TAPDIR)"

publish-local: $(DISTDIR)/.published

# sync-tap is a leaf operation: it only syncs the local brew-tap clone
# from the source tap. Release artifacts (tarball + formula) come exclusively
# from explicit `make publish`; chaining publish through sync-tap was
# the schema-3 footgun fixed in schema 4.
sync-tap:
	@if command -v check-tap-coupling >/dev/null 2>&1; then \
	  check-tap-coupling . ; \
	else \
	  echo "note: check-tap-coupling not on PATH -- skipping tap coupling preflight (install: brew install dugal/tap/release-mgmt)"; \
	fi
	git -C $(BREWTAP) pull origin main
	@echo "Synced $(BREWTAP) from source tap (tarball/formula state untouched)"

# GitHub distribution: create a release with the tarball, regenerate the
# formula with the release-asset URL, and push it to the configured tap repo.
# Requires:
#   - a 'github' remote (publish-source --init configures this)
#   - `github:\n  publisher_tap: <user>/<repo>` in .publish.yaml (publish-formula
#     reads this)
#   - `gh` CLI authenticated against the source repo
publish-github: $(FORMULAS)
	@test -n "$(GITHUB_REPO)" || { \
	  echo "error: no 'github' remote configured -- run: git remote add github <url> (or: publish-source --init)"; \
	  exit 1; \
	}
	gh release create $(VERSION) $(TARBALL) \
	  --target main \
	  --title "$(VERSION)" \
	  --notes "Release $(VERSION)" \
	  $(if $(findstring -,$(VERSION)),--prerelease,)
	@for f in $(FORMULAS); do \
	  bn=$$(basename $$f); \
	  name=$${bn%.rb}; \
	  if [ $(words $(FORMULAS)) -gt 1 ]; then \
	    scripts/gen-formula --formula $$name $(TARBALL) \
	      --url "https://github.com/$(GITHUB_REPO)/releases/download/$(VERSION)/$(notdir $(TARBALL))" \
	      > $$f; \
	  else \
	    scripts/gen-formula $(TARBALL) \
	      --url "https://github.com/$(GITHUB_REPO)/releases/download/$(VERSION)/$(notdir $(TARBALL))" \
	      > $$f; \
	  fi; \
	  publish-formula --formula $$f --version-string $(VERSION); \
	done

# One-command full GitHub release: source mirror + tagged release + formula
# push to GitHub tap. Does not run `make install` -- post-release installation
# stays a manual choice.
release-github: stamp dist
	publish-source
	$(MAKE) publish-github

# Gitea (NAS) distribution: create a release on the Gitea `origin` with the
# tarball attached, regenerate each formula with the Gitea download URL, and
# push it to the configured Gitea tap. No source-mirror step -- the source
# repo already lives on the NAS Gitea as `origin`.
#
# Requires:
#   - the release tag already created on HEAD (gitea-release does not create it)
#   - `backend: gitea` + a `gitea:` block
#     (host/port/owner/ssh_alias/publisher_tap) in .publish.yaml
#   - $GITEA_API_TOKEN (or $GITEA_TOKEN) in the environment
#   - the `release-mgmt` formula installed (gitea-release / publish-formula)
#
# gitea-release prints the tarball download URL on stdout; the recipe captures
# it and feeds it to gen-formula --url, so no .publish.yaml parsing is needed
# here.
#
# Published-formula rule (schema 9): a *published* tap must never carry a
# `file://` URL. `file://` is dev-iteration-only (sync-tap). Both
# publish-github and publish-gitea regenerate the formula with a real
# release URL before pushing; publish-gitea additionally asserts it below.
publish-gitea: $(FORMULAS)
	@command -v gitea-release >/dev/null 2>&1 || \
	  { echo 'error: gitea-release not on PATH; install with: brew install dugal/tap/release-mgmt'; exit 1; }
	URL=$$(gitea-release --tag $(VERSION) --asset $(TARBALL)) || exit 1; \
	for f in $(FORMULAS); do \
	  bn=$$(basename $$f); \
	  name=$${bn%.rb}; \
	  if [ $(words $(FORMULAS)) -gt 1 ]; then \
	    scripts/gen-formula --formula $$name $(TARBALL) --url "$$URL" > $$f; \
	  else \
	    scripts/gen-formula $(TARBALL) --url "$$URL" > $$f; \
	  fi; \
	  if grep -Eq 'url[[:space:]]+"file://' $$f; then \
	    echo "error: refusing to publish $$f with a file:// URL"; exit 1; \
	  fi; \
	  publish-formula --backend gitea --formula $$f --version-string $(VERSION); \
	done

# One-command full Gitea release: tagged release + tarball attach + formula
# push to the Gitea tap. No source mirror (source already on the NAS Gitea).
# Does not run `make install`.
release-gitea: stamp dist
	$(MAKE) publish-gitea

# --- Staged promotion (schema 16) ---
#
# `make release` runs the staged pipeline defined by the `promote:` block:
#   release-<staging> -> gate -> release-<production>
# The gate installs the just-published STAGING artifact from
# `<staging>.consumer_tap` in a clean environment and smoke-tests it (the same
# `verify.smoke` the post-publish check uses -- one smoke command, not two).
# A non-zero gate ABORTS before any production publish; only a green gate
# proceeds. `promote.gate` carries no `smoke:` key -- publish-verify reads
# `verify.smoke`.
#
# Requires a `promote:` block (promote.staging + promote.production) and the
# `release-mgmt` formula (publish-verify / check-tap-coupling on PATH). Single-
# target projects do NOT define `promote:` and publish with release-<backend>
# directly; `make release` errors with that hint when the block is absent.
# ISOLATION on the command line overrides promote.gate.isolation for the gate.
release:
	@test -n "$(PROMOTE_STAGING)" -a -n "$(PROMOTE_PRODUCTION)" || { \
	  echo "error: no promote: block in .publish.yaml (need promote.staging + promote.production)"; \
	  echo "hint: single-target projects publish with: make release-$(or $(BACKEND),<backend>)"; \
	  exit 2; \
	}
	@command -v publish-verify >/dev/null 2>&1 || \
	  { echo 'error: publish-verify not on PATH; install with: brew install dugal/tap/release-mgmt'; exit 1; }
	$(MAKE) release-$(PROMOTE_STAGING)
	@echo "=== gate: install staged artifact from '$(PROMOTE_STAGING)' in a clean env + smoke ==="
	@if command -v check-tap-coupling >/dev/null 2>&1; then \
	  check-tap-coupling --target $(PROMOTE_STAGING) . ; \
	else \
	  echo "note: check-tap-coupling not on PATH -- skipping coupling preflight"; \
	fi
	publish-verify --only install --target $(PROMOTE_STAGING) \
	  $(if $(ISOLATION),--isolation $(ISOLATION),$(if $(GATE_ISOLATION),--isolation $(GATE_ISOLATION))) .
	@echo "=== gate passed; promoting to production '$(PROMOTE_PRODUCTION)' ==="
	$(MAKE) release-$(PROMOTE_PRODUCTION)

install: sync-tap
	@for f in $(FORMULAS); do \
	  bn=$$(basename $$f); \
	  name=$${bn%.rb}; \
	  brew list $$name >/dev/null 2>&1 \
	    && brew reinstall $(TAP_NAME)/$$name \
	    || brew install $(TAP_NAME)/$$name; \
	done

# --- Post-publish verification ---
#
# verify-publish (schema 14): the standing post-publish gate. Runs
# `publish-verify` against this project's PUBLIC GitHub presence (repo pages +
# release + public tap) after a publish, driven by the `verify:` block in
# .publish.yaml. It verifies only the public GitHub half of a dual-target
# publish; the private NAS Gitea target keeps its own verification. Requires the
# `release-mgmt` formula so `publish-verify` is on PATH; the recipe guards on
# that prerequisite (schema 8 precedent). ISOLATION overrides the clean-env mode
# for the functional-install check (container default, or prefix for fast local
# iteration without network isolation).
verify-publish:
	@command -v publish-verify >/dev/null 2>&1 || \
	  { echo 'error: publish-verify not on PATH; install with: brew install dugal/tap/release-mgmt'; exit 1; }
	publish-verify $(if $(ISOLATION),--isolation $(ISOLATION)) .

# --- Release gate ---

# Verify the working tree is in a releasable state. Fails if VERSION
# resolves to a 'dev-*' string, meaning HEAD is not on a tag AND CHANGELOG
# has not been promoted to a pending release. Run before `make dist` in a
# release flow to catch CHANGELOG drift before the tarball is built; if
# release-check passes, the next `make dist` will produce an artifact
# labeled with the resolved release version (no manual stamp-version
# invocation needed).
release-check:
	@case "$(VERSION)" in \
	  dev-*) \
	    echo "error: VERSION resolves to '$(VERSION)' -- not a release version."; \
	    echo "hint: HEAD is not on a tag and CHANGELOG has no pending release."; \
	    echo "      Promote the changelog first: promote-changelog --version vX.Y.Z"; \
	    exit 1 ;; \
	  *) \
	    echo "release-check: VERSION = $(VERSION)" ;; \
	esac

# Fused release preparation (schema 12): changelog promotion + source
# stamp as ONE invocation, so the release-in-flight window (CHANGELOG promoted,
# VERSION constants stale, `make dist` refused by the strict stamp gate) cannot
# be entered by checklist ordering. Usage:
#     make release-prep VERSION=vX.Y.Z
# VERSION must be passed explicitly on the command line -- the computed ladder
# value is never used, so a bare `make release-prep` refuses instead of
# promoting to a dev-* string. Safely re-runnable: when CHANGELOG is already
# promoted to the requested version the promote step is skipped and only the
# (idempotent) stamp re-runs, so a failure between the two steps is recovered
# by re-running the same command. Both tools ship with the `release-mgmt`
# formula.
release-prep:
	@if [ "$(origin VERSION)" != "command line" ]; then \
	  echo "usage: make release-prep VERSION=vX.Y.Z"; \
	  echo "hint: pass the release version explicitly (resolved VERSION '$(VERSION)' is ignored)."; \
	  exit 2; \
	fi
	@case "$(VERSION)" in \
	  v[0-9]*) ;; \
	  *) \
	    echo "error: '$(VERSION)' is not a release version (expected vX.Y.Z)"; \
	    exit 2 ;; \
	esac
	@if [ "$$(pending-release CHANGELOG.md 2>/dev/null)" = "$(VERSION)" ]; then \
	  echo "release-prep: CHANGELOG.md already promoted to $(VERSION) -- skipping promote"; \
	else \
	  promote-changelog --version $(VERSION); \
	fi
	stamp-version --version-string $(VERSION) .

# --- Test target ---

# Runtime stays stdlib-only; the unittest suite is likewise stdlib. `make test`
# is the single discoverable entry point in-container (DDE) and on the host.
test:
	python3 -m unittest discover tests/

# --- Coverage targets (thin delegators -- coverage-model CLI) ---
coverage-spec:
	@command -v coverage-model >/dev/null 2>&1 || \
	  { echo 'error: coverage-model not on PATH; install with: brew install dugal/tap/release-mgmt'; exit 1; }
	@COVERAGE_VERSION=$(VERSION) coverage-model run spec

coverage-black-box:
	@command -v coverage-model >/dev/null 2>&1 || \
	  { echo 'error: coverage-model not on PATH; install with: brew install dugal/tap/release-mgmt'; exit 1; }
	@COVERAGE_VERSION=$(VERSION) coverage-model run black-box

coverage-unit:
	@command -v coverage-model >/dev/null 2>&1 || \
	  { echo 'error: coverage-model not on PATH; install with: brew install dugal/tap/release-mgmt'; exit 1; }
	@COVERAGE_VERSION=$(VERSION) coverage-model run unit


.PHONY: all stamp dist formula publish-local release-check release-prep install sync-tap publish-github release-github publish-gitea release-gitea release verify-publish test coverage-unit coverage-black-box coverage-spec
