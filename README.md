# Workflows

Inspired by the more complete repo [workflows](https://github.com/kdeldycke/workflows) owned by [Kevin Deldycke](https://github.com/kdeldycke)

Maintaining project takes time. This repository contains workflows to automate most of the boring tasks.

These workflows are mostly used for ~~Poetry-based~~ Python CLI and their documentation, but not only. They're all [reuseable GitHub actions workflows](https://docs.github.com/en/actions/learn-github-actions/reusing-workflows).

Reasons for a centralized workflow repository:

- reuseability of course: no need to update dozens of repository where 95% of workflows are the same
- centralize all dependencies pertaining to automation: think of the point-release of an action that triggers dependabot upgrade to all your repositories dependeing on it

## Release management

**TODO**: To-Be-Review-And-Updated

It turns out [Release Engineering is a full-time job, and full of edge-cases](https://blog.axo.dev/2023/02/cargo-dist).

Rust has [`cargo-dist`](https://github.com/axodotdev/cargo-dist). Go has... ? But there is no equivalent for Python.

So I made up a [`release.yaml` workflow](https://github.com/kdeldycke/workflows/blob/main/.github/workflows/release.yaml), which:

1. Extracts project metadata from `pyproject.toml`
1. Generates a build matrix of all commits / os / arch / CLI entry points
1. Build Python wheel with Twine
1. Compile binaries of all CLI with Nuitka
1. Tag the release commit in Git
1. Publish new version to PyPi
1. Publish a GitHub release
1. Attach and rename build artifacts to it

## Changelog

A [detailed changelog](changelog.md) is available.

## Used in

Check these projects to get real-life examples of usage and inspiration:

- ![GitHub stars](https://img.shields.io/github/stars/DLRSP/django-errors?label=%E2%AD%90&style=flat-square) [django-errors](https://github.com/DLRSP/django-errors#readme) - Django application for handling server errors.
- ![GitHub stars](https://img.shields.io/github/stars/DLRSP/django-requests-api?label=%E2%AD%90&style=flat-square) [django-requests-api](https://github.com/DLRSP/django-requests-api#readme) - Django application to provide simple and shared requests client.
- ![GitHub stars](https://img.shields.io/github/stars/DLRSP/django-sp?label=%E2%AD%90&style=flat-square) [django-sp](https://github.com/DLRSP/django-sp#readme) - Django application for custom Social Profile Auth and User model.
- ![GitHub stars](https://img.shields.io/github/stars/DLRSP/django-custom-storage?label=%E2%AD%90&style=flat-square) [django-custom-storage](https://github.com/DLRSP/django-custom-storage#readme) - Django application provide custom storage uses S3 and Compressor.
- ![GitHub stars](https://img.shields.io/github/stars/DLRSP/django-iubenda?label=%E2%AD%90&style=flat-square) [django-iubenda](https://github.com/DLRSP/django-iubenda#readme) - Django application for handling privacy and cookie policies configured with Iubenda.
- ![GitHub stars](https://img.shields.io/github/stars/DLRSP/django-hashtag?label=%E2%AD%90&style=flat-square) [django-hashtag](https://github.com/DLRSP/django-hashtag#readme) - Django application provide hashtag functionality.
- ![GitHub stars](https://img.shields.io/github/stars/DLRSP/django-model-mixin?label=%E2%AD%90&style=flat-square) [django-model-mixin](https://github.com/DLRSP/django-model-mixin#readme) - Django application provide simple model's mixins to add common reusable attributes.
- ![GitHub stars](https://img.shields.io/github/stars/DLRSP/django-lang?label=%E2%AD%90&style=flat-square) [django-lang](https://github.com/DLRSP/django-lang#readme) - Django application to provide useful utils and reusable parts of code for multi-languages sites.
- ![GitHub stars](https://img.shields.io/github/stars/DLRSP/django-static-base?label=%E2%AD%90&style=flat-square) [django-static-base](https://github.com/DLRSP/django-static-base#readme) - Django's application to serve up-to-date common static files (JQuery, Bootstrap, Plugins, ...) as "base" static directory
- ![GitHub stars](https://img.shields.io/github/stars/DLRSP/django-sites-extra?label=%E2%AD%90&style=flat-square) [django-sites-extra](https://github.com/DLRSP/django-sites-extra#readme) - Django application to extend the standard "sites" framework with extra utils.

Feel free to send a PR to add your project in this list if you are relying on these scripts.

## Release process

**TODO**: To-Be-Review-And-Updated

All steps of the release process and version management are automated in the
[`changelog.yaml`](https://github.com/kdeldycke/workflows/blob/main/.github/workflows/changelog.yaml)
and
[`release.yaml`](https://github.com/kdeldycke/workflows/blob/main/.github/workflows/release.yaml)
workflows.

All there's left to do is to:

- [check the open draft `prepare-release` PR](https://github.com/DLRSP/workflows/pulls?q=is%3Apr+is%3Aopen+head%3Aprepare-release)
  and its changes,
- click the `Ready for review` button,
- click the `Rebase and merge` button,
- let the workflows tag the release and set back the `main` branch into a
  development state.