# Changelog

All notable changes to this project are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.2.0] - 2026-10-30

### Added
- `PARENT_PROCESS` patterns that point at a split group are rewritten: the
  entry is cloned once per group piece, in place, each clone pointing at one
  piece. Unsplit groups holding such entries are written as `Split_1_` copies.
- Synthetic example policy with `PARENT_PROCESS` entries, and tests for the
  rewrite (cloning, equal splitting after the rewrite, name-only matching,
  group chains, cycles, unknown fields).
- `pyproject.toml` (standard library only, Python 3.8+).
- This changelog.

### Changed
- The cap is a fixed 900 definitions per policy (899 apps per group). The
  importer's hard cap is 1000.
- The stale-reference warning now covers only cases the rewrite cannot
  handle.
- `.gitignore` ignores every `.epmp` file except the committed synthetic
  examples and test fixtures.
- README: limits, parent-process handling, synthetic-data rule, importer
  behavior confirmed in a lab set.

### Removed
- The `--limit` option.

## [0.1.0] - 2026-10-30

### Added
- Split-only output: new `Split_N_<name>` policies and groups only.
- Equal, contiguous, order-preserving splits of oversized groups and
  policies, with group references rewritten to the pieces.
- Fresh `LinkedAgentPolicies` self-entries with synthetic InternalIds.
- Post-import checklist, skipped and flagged cases, overwrite safety, BOM
  input support.
- Synthetic examples and unit tests.
