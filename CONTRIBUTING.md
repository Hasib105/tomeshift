# Contributing

Thanks for helping. tomeshift stays small on purpose: `tomeshift.py` (CLI) and `tomeshift_ui.py` (web UI), with one dependency. Please read the [Code of Conduct](CODE_OF_CONDUCT.md).

## Good first contributions

- **A new backend.** Add one entry to `AGENTS` for a CLI, or a function plus an `APIS` entry for an HTTP API. Make sure limit errors raise `LimitHit`.
- **A new language.** Add one line to `LANGS`: code, name, a regex for its script (or `None` for Latin), and an optional style note.
- **New limit messages.** If your CLI's "you are out of quota" text isn't detected, add it to `LIMIT_RE` and add a test case.
- **Better extraction** for PDF/EPUB edge cases. Please attach a small sample file that you have the right to share.
- **Gemini/OpenAI Batch API support**, which is about 50% cheaper for API users.

## Development setup

```bash
git clone https://github.com/Hasib105/tomeshift && cd tomeshift
uvx pre-commit install          # optional: lint and format on every commit
```

## Before opening a PR

Run what CI runs:

```bash
uvx ruff check . && uvx ruff format --check .        # lint + format
uvx mypy tomeshift.py tomeshift_ui.py                 # types
uv run --no-project --with pymupdf --with pytest pytest   # tests
python3 scripts/check_manifests.py                    # plugin/skill manifests agree
```

The tests use a mock API and a fake agent, so no keys or quota are needed. If you change how a real backend is called, say in the PR which CLI or API version you tested it with.

If you change the version, change it in `pyproject.toml` and every manifest (`scripts/check_manifests.py` will tell you which ones). Add a line to `CHANGELOG.md` under *Unreleased*.

## Releasing (maintainers)

1. Move the *Unreleased* entries in `CHANGELOG.md` to a new version section.
2. Bump the version in `pyproject.toml` and the manifests.
3. `git tag vX.Y.Z && git push --tags`. The release workflow builds the package and creates the GitHub release. It also publishes to PyPI if `PYPI_PUBLISH` is enabled.

## Style

Plain Python 3.10+, standard library first. Keep functions short and readable. No new dependencies without discussing them in an issue first.
