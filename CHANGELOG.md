# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Agent skill (`skills/tomeshift/SKILL.md`) plus plugin manifests for Claude Code (marketplace), Codex, Cursor, Gemini CLI and agent-plugins.
- `tomeshift ui`: a local web UI with upload, progress and pause state, glossary editor, side-by-side preview, redo-chunk and EPUB download.
- CI: ruff lint and format, mypy, tests on Linux and macOS with Python 3.10–3.13, manifest checks, shellcheck, build-and-install smoke test, CodeQL, Dependabot, tag-based releases.

### Fixed
- Crash on Python 3.10 and 3.11 caused by an f-string that is only valid on Python 3.12+.
- The model sometimes translated the previous chunk's context as well. Context and passage are now tagged, and outputs with extra paragraphs are retried.

## [0.1.0] - 2026-10-13

### Added
- First release: `prep`, `glossary`, `run`, `status`, `build`; 9 backends; pause on usage limits and resume automatically; Markdown and EPUB output.
