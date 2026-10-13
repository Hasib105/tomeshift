---
name: tomeshift
description: >
  Translate a whole book or long document (PDF, EPUB, DOCX, TXT, Markdown) into another language at the
  lowest possible cost with the tomeshift CLI. It chunks and cleans the text, builds a glossary for
  consistent names, translates through a coding-agent CLI (claude, codex, gemini, opencode, agy) or an
  API (Gemini, any OpenAI-compatible, Anthropic), pauses on usage limits, resumes by itself, and
  outputs EPUB (original layout kept), DOCX and Markdown, optionally bilingual. Use when the user wants to translate a book, novel, manual, thesis or any
  text too long for one reply, or asks to estimate or reduce the cost of such a translation.
---

# tomeshift: translate a whole book

Do **not** translate the book yourself inside this conversation. It would fill your context, lose
consistency and cost far more. Drive the `tomeshift` CLI instead. It runs each chunk as a separate
cheap call, checks the output, and survives usage limits.

## 0. Get the CLI

Use whichever works, in this order:

```bash
tomeshift --help                                                   # already installed
uvx --from git+https://github.com/Hasib105/tomeshift tomeshift --help   # no install (needs uv)
pip install git+https://github.com/Hasib105/tomeshift              # install
```

Below, `tomeshift` means whichever form worked.

## 1. Confirm the brief (ask only what you can't infer)

- **File**: path to the .pdf / .epub / .docx / .txt / .md. EPUBs are translated in place (layout, images, footnotes kept). A scanned PDF needs OCR first (`ocrmypdf in.pdf out.pdf`).
- **Languages**: `--from` (default `en`) and `--lang` (default `bn`). Built in: `en bn hi ur ar ta ne ru zh ja es fr de pt id`. Any other language works by name, e.g. `--lang Swahili`.
- **Backend**: prefer one the user already pays for, so the extra cost is $0. Check with `command -v claude codex gemini opencode agy`. Otherwise use an API whose key is set: `GEMINI_API_KEY` → `gemini-api` with the provider's cheapest "lite"/"flash" model, `OPENAI_API_KEY` → `openai-api`, `ANTHROPIC_API_KEY` → `anthropic-api`.

## 2. Prepare and pilot

```bash
tomeshift prep BOOK.pdf -p PROJECT --from en --lang bn     # PROJECT = a new folder
tomeshift glossary -p PROJECT --agents claude          # add --model <a small, fast model>
```

Show the user `PROJECT/glossary.json` (names and terms → fixed translations) and apply their
corrections. This is the single biggest quality lever.

For a book longer than ~50 pages, offer a pilot first: copy `chunks/0001.md` into a temp project,
translate it with 1–2 candidate models, and let the user pick by reading the output.

## 3. Translate (in the background)

```bash
nohup tomeshift run -p PROJECT --agents claude -j 3 > PROJECT/run.out 2>&1 &   # --model <name> to pick one
```

- `-j` is the number of chunks translated in parallel: faster, but it uses quota faster.
- `--agents claude,gemini --fallback` keeps going on a second agent while the first is rate-limited.
- `--notify <command>` sends one message on pause and one on finish.
- If a usage limit hits, tomeshift pauses and resumes by itself. Killing and re-running the same command continues where it stopped.

Check progress without reading big files:

```bash
tomeshift status -p PROJECT     # {"percent": 42.0, "status": "running" | "PAUSED (limit) until 15:01", ...}
tail -5 PROJECT/run.log         # retries and errors
```

Report the percentage and any pause to the user. Don't poll in a tight loop.

## 4. Review and build

- `PROJECT/review.txt` lists chunks that were accepted with problems. Open the matching `chunks/NNNN.md` and `out/NNNN.md`, then either fix `out/NNNN.md` directly or delete it and re-run `tomeshift run` to redo just that chunk.
- `tomeshift build -p PROJECT` writes `PROJECT/book_<lang>.epub`, `.docx` and `.md`. Add `--layout bilingual` for original + translation (good for language learners).

## UI alternative

If the user prefers clicking to commands, run `tomeshift ui` and send them http://127.0.0.1:8787. It has
upload, language and backend choice, a progress bar, a glossary editor, side-by-side preview and EPUB download.

## Notes

- Chunk size: the default `--words 2500` suits most models. Lower it if outputs get cut off.
- Custom style for one book: put a `TRANSLATE.md` in the project folder. Use `{language}`, `{source}` and `{style}` placeholders.
- Subscription plans are meant for personal use. Publishing a translation of a copyrighted book needs the rights holder's permission.
