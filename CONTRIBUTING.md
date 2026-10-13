# Contributing

Thanks for helping. tomeshift stays small on purpose: one file (`tomeshift.py`) and one dependency.

## Good first contributions

- **A new backend.** Add one entry to `AGENTS` for a CLI, or a function plus an `APIS` entry for an HTTP API. Make sure limit errors raise `LimitHit`.
- **A new language.** Add one line to `LANGS`: code, name, a regex for its script (or `None` for Latin), and an optional style note.
- **New limit messages.** If your CLI's "you are out of quota" text isn't detected, add it to `LIMIT_RE` and add a test case.
- **Better extraction** for PDF/EPUB edge cases. Please attach a small sample file that you have the right to share.
- **Gemini/OpenAI Batch API support**, which is about 50% cheaper for API users.

## Before opening a PR

```bash
uv run --no-project --with pymupdf python tests/test_tomeshift.py -v
```

The tests use a mock API, so no keys or quota are needed. If you change how a real backend is called, say in the PR which CLI or API version you tested it with.

## Style

Plain Python 3.10+, standard library first. Keep functions short and readable. No new dependencies without discussing them in an issue first.
