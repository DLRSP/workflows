# Changelog

## [1.19.1 (unreleased)]

### Changed

- Migrate remaining workflows from `BOT_PAT` to `dlrsp-actions` App installation token
  (`changelog`, `pr-rebase-*`, `release-*`, `update-used-in`, `upgrade-*`).
- Extend bot policies to `dependabot[bot]` and `pre-commit-ci[bot]` (TS-03/04).
- Run policy gate on all non-draft PRs; evaluate uses PR author, not workflow actor.
- Approve and auto-merge via `dlrsp-actions` App token (not `github-actions[bot]`).
- Fix requirements policy CI check names (`Verify Requirements / …`).
- Add `@dlrsp-actions[bot]` to `CODEOWNERS` so App approval satisfies branch protection.
- Tolerate rebased dependabot commits in policy gate (`skip-commit-verification` + body fallback).
- Mark Python 3.9 and py3.13/3.14 django verify matrix legs as optional.
- Route all policy-gate PR writes (approve, block label, block comment) through `dlrsp-actions` App token.
- Fix `labels.yaml` canonical URL (`main`, not `vmain`); sync labels via App token.
- Fix `update-used-in` central job missing `PyYAML`.
- Drop spurious `issues: write` from `pr-label-merge-conflicts` (fixes requirements `startup_failure`).
- Migrate merge-conflict labels and broken-link issues to `dlrsp-actions` App token.
- Trust `app/dlrsp-actions` PR author; skip self-approve; admin-merge self-authored bot PRs.
- Release tag pushes use `dlrsp-actions[bot]` git identity and App token.
- Workflows bot policy: no semver-major block (full CI matrix validates dev dep majors).
- Policy gate clears stale `needs-human-review` on approve; changelog version-increments non-blocking.
- CodeQL steps tolerate repos without GitHub Advanced Security enabled.
- broken-links: App token when org secrets available; GITHUB_TOKEN fallback on dependabot PRs; add permission-issues to dlrsp-actions-token.

## [1.19.0 (2026-06-15)](https://github.com/DLRSP/workflows/compare/v1.18.3...v1.19.0)

### Breaking changes

- Replace blind bot approve in `ci.yaml` with policy-gated `pr-policy-gate` workflow.
- Add GitHub App `dlrsp-actions` composites (`dlrsp-actions-token`, `evaluate-bot-policy`).
- `upgrade-dependency.yaml` opens PRs with App installation token (author `dlrsp-actions[bot]`).
- Org secrets required: `DLRSP_ACTIONS_APP_ID`, `DLRSP_ACTIONS_PRIVATE_KEY`.
- `pr-approve-bots.yaml` is now a thin wrapper around `pr-policy-gate`.

### Migration

1. Install `dlrsp-actions` GitHub App on each consumer repository.
2. Ensure org secrets are visible to repositories (`secrets: inherit`).
3. Pin reusable workflows to `@v1.19.1` (or `@v1.19.0` for policy gate only; `@v1.19.1` removes `BOT_PAT` from all workflows).
4. Keep org/repo `BOT_PAT` during 14-day shim window; revoke only after monitor passes.

## [1.20.4 (unreleased)](https://github.com/DLRSP/workflows/compare/v1.19.0...v1.20.0)
```{important}
This version is not released yet and is under active development.
```
## [1.15.10 (2026-01-9)](https://github.com/DLRSP/workflows/compare/v1.15.9...v1.15.10)

## [1.8.1 (2023-09-24)]

## [1.6.11 (2023-07-31)](https://github.com/DLRSP/workflows/releases/tag/v1.6.11)

## [0.0.1 (2023-07-30)](https://github.com/DLRSP/workflows/releases/tag/v0.0.1)

- Initial public release.