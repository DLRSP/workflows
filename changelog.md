# Changelog

## [1.19.1 (unreleased)](https://github.com/DLRSP/workflows/compare/v1.19.0...v1.19.1)

### Changed

- Migrate remaining workflows from `BOT_PAT` to `dlrsp-actions` App installation token
  (`changelog`, `pr-rebase-*`, `release-*`, `update-used-in`, `upgrade-*`).

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

## [1.20.0 (unreleased)](https://github.com/DLRSP/workflows/compare/v1.6.11...v1.10.0)
```{important}
This version is not released yet and is under active development.
```
## [1.15.10 (2026-01-9)](https://github.com/DLRSP/workflows/compare/v1.6.11...v1.15.9)

## [1.8.1 (2023-09-24)](https://github.com/DLRSP/workflows/compare/v1.6.11...v1.8.1)

## [1.6.11 (2023-07-31)](https://github.com/DLRSP/workflows/compare/v0.0.1...v1.6.11)

## [0.0.1 (2023-07-30)](https://github.com/DLRSP/workflows/compare/e9ae391...v0.0.1)

- Initial public release.