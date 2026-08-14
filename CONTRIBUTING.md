# Contributing

## Commit messages and releases

This repository uses Conventional Commit messages to calculate Semantic
Versions automatically:

- `fix:` creates a patch release, such as `1.0.0` to `1.0.1`.
- `feat:` creates a minor release, such as `1.0.0` to `1.1.0`.
- A type followed by `!`, such as `feat!:`, creates a major release.
- `BREAKING CHANGE:` in the commit body also creates a major release.
- Other commit types do not create a release unless they contain a breaking
  change.

Release Please maintains a release pull request containing `version.txt` and
`CHANGELOG.md`. Merging that pull request creates the `vX.Y.Z` tag and GitHub
Release. CI then publishes both Docker images with `X.Y.Z`, `X.Y`, `X`,
`latest`, and an immutable `sha-*` tag.

Do not create release tags manually.
