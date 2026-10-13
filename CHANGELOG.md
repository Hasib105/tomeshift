# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.3.0] - 2026-10-13

### Added
- **EPUBs are translated in place.** Text becomes numbered segments with inline-formatting markers, then is written back into the original XHTML. CSS, images, fonts, footnotes, links, ids, tables and code are kept byte-for-byte. Title, description, NCX and nav table of contents, and image alt text are translated; `lang`, `xml:lang` and `dc:language` are set. Standard library only.
- **Bilingual layout**: `build --layout bilingual` writes each original paragraph followed by its translation (EPUB, DOCX, Markdown).
- **DOCX input and output**, standard library only. The output opens in Word, LibreOffice and Google Docs.
- Marker checks: every `[[n]]` segment and `⟦n⟧` inline marker must survive, or the chunk is retried. If it's still broken, that paragraph falls back to plain translated text and keeps its images and note links.
- UI: DOCX upload, a layout picker, DOCX downloads.

### Changed
- The README cost table uses model price tiers instead of model names, which go out of date.


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
