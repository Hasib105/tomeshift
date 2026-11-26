# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **Translation memory** (`memory.jsonl`): accepted paragraphs are reused, so re-running `prep` with other settings doesn't pay for finished work again. Partly known chunks send only their missing paragraphs.
- `tomeshift redo -p PROJECT N...` forgets chunk translations, memory included. The UI's redo button uses it.

### Changed
- Thinking is off or minimal for API backends: Gemini 3.x gets `thinkingLevel` (2.x keeps budget 0), OpenAI-compatible servers get `reasoning_effort=low` (`TOMESHIFT_REASONING`), and a server that rejects an optional setting is retried without it. Gemini thinking tokens are now counted as output.
- Re-running `prep` keeps translations whose chunk didn't change and drops the rest, instead of leaving outputs that no longer match their chunk.

### Fixed
- opencode: the removed `--pure` flag is gone and the prompt goes over stdin.
- Windows: npm `.cmd` CLIs start, project files are read and written as UTF-8, and the progress bar no longer crashes cp1252 consoles.

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
