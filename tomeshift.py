#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pymupdf>=1.24"]
# ///
"""tomeshift: translate whole books between any languages with an LLM, as cheaply as possible.

Backends: coding-agent CLIs you already pay for (claude, agy, opencode, codex, gemini, any stdin command)
or direct APIs (gemini-api, openai-api for any OpenAI-compatible server, anthropic-api).

  tomeshift.py prep BOOK.pdf|.epub|.txt|.md -p PROJECT [--from en] [--lang bn]   split + clean
  tomeshift.py glossary -p PROJECT                         one agent call -> glossary.json (edit it!)
  tomeshift.py run -p PROJECT [-j 3] [--agents claude]     translate; pauses on limits, resumes itself
  tomeshift.py status -p PROJECT                           show % done
  tomeshift.py build -p PROJECT [--layout bilingual]       -> book_<lang>.epub (original layout kept) + .md + .docx
  tomeshift.py ui                                           local web UI at http://127.0.0.1:8787

Output files are the source of truth: kill it any time, run again, it continues.
"""

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

# Default instructions. Put a TRANSLATE.md in the project folder to override them for one book.
RULES = """You are a professional literary translator. Translate the user's {source} passage into natural, fluent {language} for a printed book.

Rules:
- Output ONLY the {language} translation. No preface, no notes, no explanations, no code fences.
- Translate everything. Do not summarise, skip, shorten, or add content.
- Keep the same paragraphs, in the same order, separated by one blank line.
- Keep markdown as-is: lines starting with "# " stay headings, *text* stays italic.
- Keep every marker exactly: a paragraph starting with [[12]] must start with [[12]] in your output, and inline markers like ⟦1⟧…⟦/1⟧ (formatting) and ⟦2/⟧ (images, note links) must wrap / sit at the matching translated words. Never drop, renumber or invent markers.
- Follow the glossary exactly when one is given. Transliterate other personal names consistently.
- Keep the author's tone: dialogue should sound like real people talking; formal prose stays formal.
- Keep numbers as they are written in the original.
- Translate ONLY the text inside <passage>...</passage>. Text inside <context>...</context> comes just before it and is for continuity only: never translate, repeat or summarise it. Do not output the tags.
- {style}
"""
# code -> (name, regex for the target script or None, extra style rule)
LANGS = {
    "bn": ("Bangla", r"[\u0980-\u09FF]", "Use standard colloquial Bangla (চলিত ভাষা) and the Bangla full stop (।)."),
    "hi": ("Hindi", r"[\u0900-\u097F]", "Use natural modern Hindi, not overly Sanskritised; use the danda (।)."),
    "ur": ("Urdu", r"[\u0600-\u06FF]", ""),
    "ar": ("Arabic", r"[\u0600-\u06FF]", "Use Modern Standard Arabic."),
    "ta": ("Tamil", r"[\u0B80-\u0BFF]", ""),
    "ne": ("Nepali", r"[\u0900-\u097F]", ""),
    "ru": ("Russian", r"[\u0400-\u04FF]", ""),
    "zh": ("Simplified Chinese", r"[\u4E00-\u9FFF]", ""),
    "ja": ("Japanese", r"[\u3040-\u30FF\u4E00-\u9FFF]", ""),
    "en": ("English", None, ""),
    "es": ("Spanish", None, ""),
    "fr": ("French", None, ""),
    "de": ("German", None, ""),
    "pt": ("Portuguese", None, ""),
    "id": ("Indonesian", None, ""),
}


def lang_info(code):
    name, script, style = LANGS.get(code, (code, None, ""))
    return name, (re.compile(script) if script else None), style


def rules_for(code, proj=None, source="en"):
    name, _, style = lang_info(code)
    custom = Path(proj, "TRANSLATE.md") if proj else None
    text = custom.read_text() if custom and custom.exists() else RULES
    return text.replace("{language}", name).replace("{style}", style).replace("{source}", lang_info(source)[0])


# Empty dir so agents don't load AGENTS.md / CLAUDE.md / project files (saves tokens).
SANDBOX = Path(tempfile.gettempdir()) / "tomeshift-empty-cwd"
SANDBOX.mkdir(exist_ok=True)

# ---------------------------------------------------------------- agents
AGENTS = {
    # --system-prompt replaces Claude Code's large default prompt; no tools, no MCP, no skills.
    "claude": lambda model, system, user: (
        [
            "claude",
            "-p",
            "--model",
            model or "sonnet",
            "--tools",
            "",
            "--strict-mcp-config",
            "--setting-sources",
            "",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--effort",
            "low",
            "--system-prompt",
            system,
        ],
        user,
    ),
    "agy": lambda model, system, user: (
        [
            "agy",
            "-p",
            system + "\n\n" + user,
            "--model",
            model or "gemini-3.8-flash-low",
            "--disable-slash-commands",
            "--print-timeout",
            "15m",
        ],
        None,
    ),
    "opencode": lambda model, system, user: (
        ["opencode", "run", "--pure", "-m", model or "opencode/big-pickle", system + "\n\n" + user],
        None,
    ),
    "codex": lambda model, system, user: (
        ["codex", "exec", "--skip-git-repo-check"] + (["-m", model] if model else []) + ["-"],
        system + "\n\n" + user,
    ),
    "gemini": lambda model, system, user: (["gemini"] + (["-m", model] if model else []) + ["-p", system], user),
    # Anything else: TOMESHIFT_CMD="llm -m gpt-4o-mini" gets the full prompt on stdin, prints the translation.
    "cmd": lambda model, system, user: (os.environ["TOMESHIFT_CMD"].split(), system + "\n\n" + user),
}

LIMIT_RE = re.compile(
    r"usage limit|rate.?limit|limit reached|hit your limit|quota|RESOURCE_EXHAUSTED|\b429\b|"
    r"too many requests|overloaded|resets? (at|in)|try again (later|in)|credit balance",
    re.I,
)


def parse_reset(text):
    """Best-effort: return a UTC datetime when the limit resets, or None."""
    now = datetime.now(timezone.utc)
    m = re.search(r"\|(\d{10})\b", text)  # "usage limit reached|1760000000"
    if m:
        return datetime.fromtimestamp(int(m.group(1)), timezone.utc)
    m = re.search(r"\b(?:in|after)\s+(?=\d)(?:(\d+)\s*h(?:ours?)?)?\s*(?:(\d+)\s*m(?:in(?:utes?)?)?)?", text, re.I)
    if m and (m.group(1) or m.group(2)):
        return now + timedelta(hours=int(m.group(1) or 0), minutes=int(m.group(2) or 0))
    m = re.search(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text, re.I)
    if m:
        h, mi = int(m.group(1)), int(m.group(2) or 0)
        if m.group(3):
            h = h % 12 + (12 if m.group(3).lower() == "pm" else 0)
        t = now.replace(hour=h, minute=mi, second=0, microsecond=0)  # assumes UTC (server clock)
        return t if t > now else t + timedelta(days=1)
    return None


class LimitHit(Exception):
    def __init__(self, until):
        self.until = until


# ---------------------------------------------------------------- direct APIs (pay per token)
USAGE: Counter[str] = Counter()  # input/output tokens reported by APIs
_usage_lock = threading.Lock()


def _post(url, body, headers, timeout):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        if e.code == 429 or (e.code >= 500 and "overload" in text.lower()):
            wait = e.headers.get("retry-after")
            m = re.search(r'"retryDelay":\s*"(\d+)s"', text)
            secs = int(m.group(1)) if m else int(wait) if wait and wait.isdigit() else None
            raise LimitHit(datetime.now(timezone.utc) + timedelta(seconds=secs) if secs else parse_reset(text)) from e
        raise RuntimeError(f"HTTP {e.code}: {text[:300]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"network: {e.reason}") from e


def _need(var):
    if not os.environ.get(var):
        raise SystemExit(f"set {var} to use this backend")
    return os.environ[var]


def api_gemini(model, system, user, timeout):
    model = model or "gemini-2.5-flash"
    r = _post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0.3, "maxOutputTokens": 32768, "thinkingConfig": {"thinkingBudget": 0}},
        },
        {"x-goog-api-key": _need("GEMINI_API_KEY")},
        timeout,
    )
    u = r.get("usageMetadata", {})
    cand = (r.get("candidates") or [{}])[0]
    if cand.get("finishReason") == "MAX_TOKENS":
        raise RuntimeError("output cut off (MAX_TOKENS) - use smaller --words")
    return (
        "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", [])),
        u.get("promptTokenCount", 0),
        u.get("candidatesTokenCount", 0),
    )


def api_openai(model, system, user, timeout):
    # Any OpenAI-compatible server: OpenAI, OpenRouter, DeepSeek, Groq, Together, local Ollama/vLLM...
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    key = os.environ.get("OPENAI_API_KEY", "none" if "localhost" in base or "127.0.0.1" in base else None) or _need(
        "OPENAI_API_KEY"
    )
    r = _post(
        f"{base}/chat/completions",
        {
            "model": model or "gpt-4o-mini",
            "temperature": 0.3,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        },
        {"Authorization": f"Bearer {key}"},
        timeout,
    )
    ch = r["choices"][0]
    if ch.get("finish_reason") == "length":
        raise RuntimeError("output cut off (length) - use smaller --words")
    u = r.get("usage", {})
    return ch["message"]["content"], u.get("prompt_tokens", 0), u.get("completion_tokens", 0)


def api_anthropic(model, system, user, timeout):
    r = _post(
        "https://api.anthropic.com/v1/messages",
        {
            "model": model or "claude-haiku-5-5",
            "max_tokens": 32000,
            "temperature": 0.3,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        {"x-api-key": _need("ANTHROPIC_API_KEY"), "anthropic-version": "2023-06-01"},
        timeout,
    )
    if r.get("stop_reason") == "max_tokens":
        raise RuntimeError("output cut off (max_tokens) - use smaller --words")
    u = r.get("usage", {})
    return "".join(b.get("text", "") for b in r["content"]), u.get("input_tokens", 0), u.get("output_tokens", 0)


APIS = {"gemini-api": api_gemini, "openai-api": api_openai, "anthropic-api": api_anthropic}
BACKENDS = list(AGENTS) + list(APIS)


def call_agent(name, model, system, user, timeout=1200):
    if name in APIS:
        text, tin, tout = APIS[name](model, system, user, timeout)
        with _usage_lock:
            USAGE["in"] += tin
            USAGE["out"] += tout
        return text.strip()
    cmd, stdin = AGENTS[name](model, system, user)
    try:
        r = subprocess.run(
            cmd,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=SANDBOX,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("timeout") from None
    out, err = r.stdout.strip(), r.stderr.strip()
    blob = out[-2000:] + "\n" + err[-2000:]
    # Only treat as a limit when the reply is short (a real translation may mention "quota").
    if (r.returncode != 0 or len(out) < 400) and LIMIT_RE.search(blob):
        raise LimitHit(parse_reset(blob))
    if r.returncode != 0:
        raise RuntimeError(f"exit {r.returncode}: {blob.strip()[-300:]}")
    return out


# ---------------------------------------------------------------- prep
class _Text(HTMLParser):
    BLOCK = frozenset({"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "br", "tr"})

    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append(
                "\n\n" + ("# " if tag in ("h1", "h2") else "## " if tag[0] == "h" and tag[1:].isdigit() else "")
            )
        elif tag in ("i", "em"):
            self.parts.append("*")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head"):
            self.skip -= 1
        elif tag in ("i", "em"):
            self.parts.append("*")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def read_epub(path):
    z = zipfile.ZipFile(path)
    opf_path = re.search(r'full-path="([^"]+)"', z.read("META-INF/container.xml").decode()).group(1)
    opf = z.read(opf_path).decode("utf-8", "replace")
    base = os.path.dirname(opf_path)
    items = dict(re.findall(r'<item\b[^>]*?id="([^"]+)"[^>]*?href="([^"]+)"', opf))
    items.update({i: h for h, i in re.findall(r'<item\b[^>]*?href="([^"]+)"[^>]*?id="([^"]+)"', opf)})
    texts = []
    for idref in re.findall(r'<itemref\b[^>]*idref="([^"]+)"', opf):
        href = items.get(idref)
        if not href:
            continue
        p = _Text()
        p.feed(z.read(os.path.normpath(os.path.join(base, href)).replace("\\", "/")).decode("utf-8", "replace"))
        texts.append("".join(p.parts))
    return "\n\n".join(texts)


def read_pdf(path):
    import fitz

    pages = []
    for page in fitz.open(path):
        blocks = [b[4] for b in page.get_text("blocks") if b[6] == 0]
        pages.append([re.sub(r"\s*\n\s*", "\n", b).strip() for b in blocks if b.strip()])

    # Headers/footers: short blocks that repeat (digits ignored) on many pages.
    def norm(s):
        return re.sub(r"\d+", "#", s.lower())

    freq = Counter(norm(b) for pg in pages for b in set(pg) if len(b) < 80)
    junk = {k for k, v in freq.items() if v >= max(3, len(pages) * 0.2)}
    if sum(len("".join(pg)) for pg in pages) < 200 * max(1, len(pages)) * 0.1:
        sys.exit("PDF has almost no text layer - it is scanned. OCR it first (e.g. ocrmypdf).")
    paras = []
    for pg in pages:
        for b in pg:
            if norm(b) in junk or re.fullmatch(r"[\divxlc\s\-–.]+", b, re.I):
                continue
            paras.append(b)
    return "\n\n".join(paras)


def clean(text):
    text = text.replace("\r", "").replace("­", "").replace("ﬁ", "fi").replace("ﬂ", "fl")
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)  # de-hyphenate line breaks
    paras = []
    for p in re.split(r"\n\s*\n", text):
        p = re.sub(r"[ \t]*\n[ \t]*", " ", p.strip())
        p = re.sub(r"[ \t]{2,}", " ", p)
        if p:
            paras.append(p)
    # Re-join paragraphs split across PDF page/column breaks (no end punctuation, next starts lowercase).
    merged = []
    for p in paras:
        if (
            merged
            and not re.search(r'[.!?:;"”’)\]*]$', merged[-1])
            and re.match(r"[a-z]", p)
            and not merged[-1].startswith("#")
        ):
            merged[-1] += " " + p
        else:
            merged.append(p)
    return merged


HEADING_RE = re.compile(r"^(#|(chapter|part|book|prologue|epilogue)\b|[IVXLC]+\.?$)", re.I)


def cmd_prep(a):
    src = Path(a.book)
    proj = Path(a.project)
    proj.mkdir(parents=True, exist_ok=True)
    ext = src.suffix.lower()
    mode = "text"
    if ext == ".epub" and not getattr(a, "flat", False):
        # In-place: keep the original EPUB and translate its text segments; layout, images and CSS stay untouched.
        import tomeshift_epub

        mode = "epub"
        segs = tomeshift_epub.extract(src)
        if src.resolve() != (proj / "source.epub").resolve():
            shutil.copyfile(src, proj / "source.epub")
        (proj / "segments.json").write_text(
            # keep the original text only for OPF/NCX entries: build() swaps those strings in place
            json.dumps(
                [{k: v for k, v in sg.items() if k != "text" or sg["kind"] == "xml"} for sg in segs], ensure_ascii=False
            )
        )
        items = [(f"[[{sg['id']}]] {sg['text']}", sg["head"]) for sg in segs]
    else:
        text = (
            read_pdf(src)
            if ext == ".pdf"
            else read_epub(src)
            if ext == ".epub"
            else read_docx(src)
            if ext == ".docx"
            else src.read_text()
        )
        items = []
        for p in clean(text):
            is_head = bool(len(p) < 80 and HEADING_RE.match(p))
            items.append(("# " + p if is_head and not p.startswith("#") else p, is_head))
    chunks, cur, words = [], [], 0
    for p, is_head in items:
        n = len(MARKERS.sub(" ", p).split())
        if cur and (words + n > a.words or (is_head and words > a.words * 0.3)):
            chunks.append(cur)
            cur, words = [], 0
        cur.append(p)
        words += n
    if cur:
        chunks.append(cur)
    (proj / "chunks").mkdir(parents=True, exist_ok=True)
    (proj / "out").mkdir(exist_ok=True)
    for old in (proj / "chunks").glob("*.md"):
        old.unlink()
    for i, c in enumerate(chunks, 1):
        (proj / "chunks" / f"{i:04d}.md").write_text("\n\n".join(c) + "\n")
    total = sum(len(MARKERS.sub(" ", " ".join(c)).split()) for c in chunks)
    src_lang = getattr(a, "from")
    manifest = {"source": str(src.resolve()), "lang": a.lang, "from": src_lang, "chunks": len(chunks), "words": total,
                "mode": mode}  # fmt: skip
    (proj / "manifest.json").write_text(json.dumps(manifest, indent=1))
    print(
        f"{len(chunks)} chunks, {total:,} words -> {proj}/chunks/  ({lang_info(src_lang)[0]} -> {lang_info(a.lang)[0]})"
        + ("  [EPUB kept in place]" if mode == "epub" else "")
    )


def project_lang(proj, key="lang"):
    m = Path(proj) / "manifest.json"
    default = "bn" if key == "lang" else "en"
    return json.loads(m.read_text()).get(key, default) if m.exists() else default


# ---------------------------------------------------------------- glossary
def cmd_glossary(a):
    proj = Path(a.project)
    text = MARKERS.sub("", "\n".join(p.read_text() for p in sorted((proj / "chunks").glob("*.md"))))
    src_code = project_lang(proj, "from")
    # Scripts with capital letters: capitalised words/phrases not at sentence start, seen >= 3 times.
    # Other scripts (Bangla, Chinese, Arabic...): let the model pick the terms from a longer excerpt.
    cased = src_code in ("en", "es", "fr", "de", "pt", "id", "ru") or src_code not in LANGS
    cands = Counter(
        m.group(1) for m in re.finditer(r"(?<![.!?]\s)(?<!^)\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})\b", text, re.M)
    )
    stop = {"I", "The", "A", "An", "He", "She", "It", "They", "We", "You", "But", "And", "Mr", "Mrs", "Then", "When"}
    terms = [t for t, n in cands.most_common(a.max_terms * 2) if n >= 3 and t not in stop][: a.max_terms]
    gfile = proj / "glossary.json"
    gloss = json.loads(gfile.read_text()) if gfile.exists() else {}
    todo = [t for t in terms if t not in gloss] if cased else None
    if todo == []:
        print("glossary.json already covers all candidates")
        return
    lang, src_name = lang_info(project_lang(proj))[0], lang_info(src_code)[0]
    system = (
        f"You build translation glossaries for {src_name}->{lang} book translation. Reply with ONLY a JSON object "
        f"mapping each {src_name} term to one fixed {lang} rendering (transliterate personal names; translate "
        "titles and common nouns naturally). Drop entries that are not names/terms (ordinary words)."
    )
    if todo is not None:
        user = f"Book excerpt for context:\n{text[:6000]}\n\nTerms:\n" + "\n".join(todo)
    else:
        user = (
            f"List the names, places, titles and recurring special terms in this excerpt "
            f"(up to {a.max_terms}).\n\n{text[:40000]}"
        )
    out = call_agent(a.agents.split(",")[0], a.model, system, user)
    m = re.search(r"\{.*\}", out, re.S)
    gloss.update(json.loads(m.group(0)) if m else {})
    gfile.write_text(json.dumps(gloss, ensure_ascii=False, indent=1))
    print(f"glossary.json: {len(gloss)} terms. Review and edit it before `run`.")


# ---------------------------------------------------------------- validate
LATIN = re.compile(r"[A-Za-z]")


def tidy(out):
    out = re.sub(r"^```[a-z]*\n|\n```$", "", out.strip())
    out = re.sub(r"^(here is|here's|sure|translation)[^\n]*:\s*\n", "", out, flags=re.I)
    out = re.sub(r"</?(passage|context)>", "", out).strip()
    return out.strip() + "\n"


DENSE = {"zh", "ja"}  # scripts where character counts aren't comparable to alphabetic text


MARKERS = re.compile(r"\[\[\d+\]\]|⟦/?\d+/?⟧")
SEG = re.compile(r"^\[\[(\d+)\]\][ \t]*", re.M)


def split_segments(text):
    """'[[3]] a\n\n[[4]] b' -> {3: 'a', 4: 'b'}"""
    parts = SEG.split(text)
    return {int(parts[i]): parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}


def check_markers(src, out):
    """EPUB mode: every [[id]] once, and the same inline ⟦n⟧ markers inside each segment."""
    s, o = split_segments(src), split_segments(out)
    if not s:
        return []
    problems = []
    missing, extra = sorted(set(s) - set(o)), sorted(set(o) - set(s))
    if missing or extra:
        problems.append(f"segment markers changed (missing {missing[:6]}, unexpected {extra[:6]}): keep every [[n]]")
    import tomeshift_epub

    bad = [(i, issue) for i in s if i in o and (issue := tomeshift_epub.marker_issue(s[i], o[i]))]
    if bad:
        detail = "; ".join(f"[[{i}]] {issue}" for i, issue in bad[:5])
        problems.append(f"inline markers broken ({detail}): keep ⟦n/⟧ markers and keep pairs whole")
    return problems


def check(src, out, gloss, src_code, tgt_code):
    problems = []
    if not out.strip():
        return ["output is empty"]
    problems += check_markers(src, out)
    tgt_script = lang_info(tgt_code)[1] or LATIN
    src_script = lang_info(src_code)[1] or LATIN
    if tgt_script.pattern != src_script.pattern:  # different scripts: we can tell languages apart
        native, left = len(tgt_script.findall(out)), len(src_script.findall(out))
        if native < 50:
            return ["output is not in the target language"]
        if left > 0.15 * (native + left):
            problems.append(f"too much untranslated source text ({left} source-script letters)")
    if not ({src_code, tgt_code} & DENSE):
        ratio = len(out) / max(1, len(src))
        if not 0.55 < ratio < 2.2:
            problems.append(f"length ratio {ratio:.2f} looks wrong (truncated or padded)")
    sp, op = src.count("\n\n"), out.count("\n\n")
    if op < sp * 0.75:
        problems.append(f"paragraphs merged/missing ({op + 1} vs {sp + 1})")
    elif op > sp * 1.25 + 1:
        problems.append(f"extra paragraphs ({op + 1} vs {sp + 1}): translate only the <passage>, not the <context>")
    missed = [f"{en} -> {tr}" for en, tr in gloss.items() if en in src and tr not in out]
    if len(missed) > 2:
        problems.append("glossary not followed: " + "; ".join(missed[:8]))
    return problems


# ---------------------------------------------------------------- run
class Runner:
    def __init__(self, a):
        self.a, self.proj = a, Path(a.project)
        self.agents = a.agents.split(",")
        self.paused = dict.fromkeys(self.agents)  # agent -> UTC datetime it resumes
        self.lock = threading.Lock()
        self.gloss = (
            json.loads((self.proj / "glossary.json").read_text()) if (self.proj / "glossary.json").exists() else {}
        )
        self.chunks = sorted((self.proj / "chunks").glob("*.md"))
        self.words = {c.stem: len(MARKERS.sub(" ", c.read_text()).split()) for c in self.chunks}
        self.total = sum(self.words.values())
        self.t0, self.done0 = time.time(), self.done_words()
        self.status = "running"
        self.lang = project_lang(self.proj)
        self.src_lang = project_lang(self.proj, "from")
        self.rules = rules_for(self.lang, self.proj, self.src_lang)

    def done_words(self):
        return sum(w for s, w in self.words.items() if (self.proj / "out" / f"{s}.md").exists())

    def notify(self, msg):
        print(f"\n[{datetime.now():%H:%M}] {msg}", flush=True)
        if self.a.notify:
            subprocess.run([self.a.notify, f"tomeshift {self.proj.name}: {msg}"], capture_output=True)

    def progress(self):
        done = self.done_words()
        pct = 100 * done / max(1, self.total)
        rate = (done - self.done0) / max(1, time.time() - self.t0)
        eta = f"ETA {timedelta(seconds=int((self.total - done) / rate))}" if rate > 0 else ""
        bar = "█" * int(pct / 4) + "░" * (25 - int(pct / 4))
        tok = f"  tokens {USAGE['in']:,} in / {USAGE['out']:,} out" if USAGE else ""
        line = f"{bar} {pct:5.1f}%  {done:,}/{self.total:,} words  {self.status}  {eta}{tok}"
        print(
            ("\r" + line.ljust(110)) if sys.stdout.isatty() else line,
            end="" if sys.stdout.isatty() else "\n",
            flush=True,
        )
        (self.proj / "progress.json").write_text(
            json.dumps(
                {
                    "percent": round(pct, 1),
                    "done_words": done,
                    "total_words": self.total,
                    "status": self.status,
                    "tokens_in": USAGE["in"],
                    "tokens_out": USAGE["out"],
                    "updated": datetime.now().isoformat(timespec="seconds"),
                }
            )
        )

    def pick_agent(self):
        """First non-paused agent. Without --fallback only the primary is ever used."""
        while True:
            with self.lock:
                now = datetime.now(timezone.utc)
                pool = self.agents if self.a.fallback else self.agents[:1]
                for n in pool:
                    if not self.paused[n] or self.paused[n] <= now:
                        self.paused[n] = None
                        return n
                wake = min(self.paused[n] for n in pool)
            self.status = f"PAUSED (limit) until {wake.astimezone():%H:%M}"
            time.sleep(min(60, max(5, (wake - datetime.now(timezone.utc)).total_seconds())))
            self.status = "running"

    def context_for(self, idx):
        if idx == 0:
            return ""
        prev = self.chunks[idx - 1].read_text().strip().split("\n\n")[-2:]
        return "<context>\n" + MARKERS.sub("", "\n\n".join(prev)).strip() + "\n</context>\n\n"

    def translate(self, idx):
        chunk = self.chunks[idx]
        dest = self.proj / "out" / f"{chunk.stem}.md"
        if dest.exists():
            return
        src = chunk.read_text()
        terms = {k: v for k, v in self.gloss.items() if k in src}  # only terms this chunk uses
        system = self.rules + (
            "\n\nGlossary (use exactly):\n" + "\n".join(f"{k} = {v}" for k, v in terms.items()) if terms else ""
        )
        feedback, backoff = "", 600
        for attempt in range(self.a.retries + 1):
            agent = self.pick_agent()
            user = self.context_for(idx) + feedback + "<passage>\n" + src.strip() + "\n</passage>"
            try:
                out = tidy(call_agent(agent, self.a.model if agent == self.agents[0] else None, system, user))
            except LimitHit as e:
                pad = timedelta(seconds=2 if agent in APIS else 60)  # CLI reset times are only minute-accurate
                until = (e.until or datetime.now(timezone.utc) + timedelta(seconds=backoff)) + pad
                backoff = min(backoff * 2, 3600)  # unknown reset time: probe 10m, 20m, 40m, 60m...
                with self.lock:
                    if not self.paused[agent]:
                        self.notify(f"{agent} hit its limit; paused until {until.astimezone():%H:%M}")
                    self.paused[agent] = until
                return self.translate(idx)  # limit doesn't use up a retry
            except RuntimeError as e:
                feedback = ""
                self.log(chunk.stem, agent, f"error: {e}")
                time.sleep(10)
                continue
            problems = check(src, out, terms, self.src_lang, self.lang)
            if not problems or attempt == self.a.retries:
                dest.write_text(out)
                if problems:
                    self.log(chunk.stem, agent, "accepted with problems: " + " | ".join(problems))
                    with (self.proj / "review.txt").open("a") as f:
                        f.write(f"{chunk.stem}: {' | '.join(problems)}\n")
                return
            self.log(chunk.stem, agent, "retry: " + " | ".join(problems))
            feedback = "Your previous attempt had these problems, fix them: " + "; ".join(problems) + "\n\n"

    def log(self, stem, agent, msg):
        with self.lock, (self.proj / "run.log").open("a") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {stem} [{agent}] {msg}\n")

    def go(self):
        todo = [i for i, c in enumerate(self.chunks) if not (self.proj / "out" / f"{c.stem}.md").exists()]
        stop = threading.Event()

        def ticker():
            while not stop.wait(2):
                self.progress()

        threading.Thread(target=ticker, daemon=True).start()
        with ThreadPoolExecutor(self.a.jobs) as ex:
            list(ex.map(self.translate, todo))
        stop.set()
        self.status = "done"
        self.progress()
        review = self.proj / "review.txt"
        self.notify(
            f"finished: {self.done_words():,} words. Problems to review: "
            f"{len(review.read_text().splitlines()) if review.exists() else 0}"
        )


def cmd_run(a):
    if not list(Path(a.project, "chunks").glob("*.md")):
        sys.exit("no chunks - run `prep` first")
    for n in a.agents.split(","):
        if n == "cmd" and not os.environ.get("TOMESHIFT_CMD"):
            sys.exit("agent 'cmd' needs TOMESHIFT_CMD, e.g. TOMESHIFT_CMD='llm -m gpt-4o-mini'")
        if n in APIS:
            continue
        if n not in AGENTS or not shutil.which(AGENTS[n](None, "", "")[0][0]):
            sys.exit(f"backend {n!r} not available (choose from {', '.join(BACKENDS)})")
    Runner(a).go()


def cmd_status(a):
    p = Path(a.project) / "progress.json"
    print(p.read_text() if p.exists() else "not started")


# ---------------------------------------------------------------- build
def book_paragraphs(proj):
    """[(source, translation or None, is_heading)] in reading order, as plain text (markers removed)."""
    man = json.loads((proj / "manifest.json").read_text())
    rows = []
    if man.get("mode") == "epub":
        import tomeshift_epub

        segs = json.loads((proj / "segments.json").read_text())
        src, out = {}, {}
        for c in sorted((proj / "chunks").glob("*.md")):
            src.update(split_segments(c.read_text()))
            o = proj / "out" / c.name
            if o.exists():
                out.update(split_segments(o.read_text()))
        for sg in segs:
            if sg["kind"] == "xhtml":
                tr = out.get(sg["id"])
                rows.append((tomeshift_epub.plain(src.get(sg["id"], "")), tr and tomeshift_epub.plain(tr), sg["head"]))
        return rows
    for c in sorted((proj / "chunks").glob("*.md")):
        sp = [p.strip() for p in c.read_text().split("\n\n") if p.strip()]
        o = proj / "out" / c.name
        op = [p.strip() for p in o.read_text().split("\n\n") if p.strip()] if o.exists() else []
        if len(sp) == len(op):
            rows += [(a, b, a.startswith("# ")) for a, b in zip(sp, op, strict=True)]
        else:  # can't pair paragraph by paragraph: keep the chunk together
            rows += [(a, None, a.startswith("# ")) for a in sp] if not op else []
            rows += [("", b, b.startswith("# ")) for b in op]
    return rows


def render_markdown(rows, layout):
    out = []
    for src, tr, head in rows:
        if tr is None:
            continue
        if layout == "bilingual" and src:
            if head:
                out += [tr if tr.startswith("#") else "# " + tr, "*" + src.lstrip("# ") + "*"]
            else:
                out += [src, tr]
        else:
            out.append(tr if not head or tr.startswith("#") else "# " + tr)
    return "\n\n".join(out) + "\n"


def cmd_build(a):
    proj = Path(a.project)
    man = json.loads((proj / "manifest.json").read_text())
    lang, layout = man.get("lang", "bn"), getattr(a, "layout", "replace")
    suffix = "" if layout == "replace" else "_bilingual"
    missing = len(list((proj / "chunks").glob("*.md"))) - len(list((proj / "out").glob("*.md")))
    rows = book_paragraphs(proj)
    md = render_markdown(rows, layout)
    (proj / f"book_{lang}{suffix}.md").write_text(md)
    title = a.title or proj.name
    write_docx(md, proj / f"book_{lang}{suffix}.docx", lang, title)
    epub = proj / f"book_{lang}{suffix}.epub"
    if man.get("mode") == "epub":
        import tomeshift_epub

        segs = json.loads((proj / "segments.json").read_text())
        translations = {}
        for o in sorted((proj / "out").glob("*.md")):
            translations.update(split_segments(o.read_text()))
        st = tomeshift_epub.build(proj / "source.epub", segs, translations, epub, lang, layout)
        detail = f"original layout kept; {st['translated']} segments translated"
        if st["fallback"]:
            detail += f", {st['fallback']} with formatting simplified"
        if st["missing"]:
            detail += f", {st['missing']} not translated yet"
    else:
        write_simple_epub(md, epub, lang, title)
        detail = "rebuilt from text"
    print(
        f"{proj}/book_{lang}{suffix}.md, .docx and .epub ({detail})"
        + (f"  WARNING: {missing} chunks not translated yet" if missing else "")
    )


def write_simple_epub(md, epub, lang, title):
    """Minimal EPUB 3 for plain-text sources (no pandoc needed): chapters split at "# " headings."""
    chapters, cur = [], []
    for para in md.split("\n\n"):
        if para.startswith("# ") and cur:
            chapters.append(cur)
            cur = []
        cur.append(para)
    chapters.append(cur)
    rtl = ' dir="rtl"' if lang.split("-")[0] in ("ar", "ur", "fa", "he") else ""

    def to_html(paras):
        body = []
        for p in paras:
            t = html.escape(p.strip())
            t = re.sub(r"\*(.+?)\*", r"<em>\1</em>", t)
            body.append(
                f"<h1>{t[2:]}</h1>"
                if t.startswith("# ")
                else f"<h2>{t[3:]}</h2>"
                if t.startswith("## ")
                else f"<p>{t}</p>"
            )
        return (
            f'<?xml version="1.0" encoding="utf-8"?>\n<html xmlns="http://www.w3.org/1999/xhtml" lang="{lang}"'
            f' xml:lang="{lang}"{rtl}><head><title>-</title><style>body{{line-height:1.7}}</style></head><body>'
            + "\n".join(body)
            + "</body></html>"
        )

    with zipfile.ZipFile(epub, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        manifest, spine, nav = [], [], []
        for i, ch in enumerate(chapters, 1):
            z.writestr(f"OEBPS/c{i}.xhtml", to_html(ch), compress_type=zipfile.ZIP_DEFLATED)
            manifest.append(f'<item id="c{i}" href="c{i}.xhtml" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="c{i}"/>')
            head = ch[0][2:] if ch[0].startswith("# ") else f"{i}"
            nav.append(f'<li><a href="c{i}.xhtml">{html.escape(head[:80])}</a></li>')
        z.writestr(
            "OEBPS/nav.xhtml",
            '<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="'
            + lang
            + '"><head><title>Contents</title></head><body><nav epub:type="toc"><ol>'
            + "".join(nav)
            + "</ol></nav></body></html>",
        )
        z.writestr(
            "OEBPS/content.opf",
            f'<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">tomeshift-{html.escape(title)}</dc:identifier><dc:title>{html.escape(title)}</dc:title><dc:language>{lang}</dc:language><meta property="dcterms:modified">{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}</meta></metadata><manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>{"".join(manifest)}</manifest><spine>{"".join(spine)}</spine></package>',
        )


# ---------------------------------------------------------------- DOCX (stdlib only)
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def read_docx(path):
    """DOCX -> Markdown-ish text: headings become '# ', italics '*...*'."""
    import xml.etree.ElementTree as ET

    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    w = f"{{{W}}}"
    paras = []
    for p in root.iter(w + "p"):
        style = p.find(f"{w}pPr/{w}pStyle")
        sv = (style.get(w + "val") or "").lower() if style is not None else ""
        level = 1 if sv in ("title", "heading1") else 2 if re.fullmatch(r"heading[2-9]", sv) else 0
        parts = []
        for r in p.iter(w + "r"):
            t = "".join(
                (e.text or "") if e.tag == w + "t" else "\t" if e.tag == w + "tab" else " "
                for e in r
                if e.tag in (w + "t", w + "tab", w + "br")
            )
            i = r.find(f"{w}rPr/{w}i")
            italic = i is not None and (i.get(w + "val") or "true") not in ("0", "false")
            parts.append(f"*{t.strip()}*" + (" " if t.endswith(" ") else "") if italic and t.strip() else t)
        text = "".join(parts).strip()
        if text:
            paras.append(("#" * level + " " if level else "") + text)
    return "\n\n".join(paras)


def write_docx(md, path, lang, title):
    """Markdown-ish text -> a minimal, valid DOCX (headings, italics, right-to-left where needed)."""
    rtl = lang.split("-")[0] in ("ar", "ur", "fa", "he")
    esc = lambda t: html.escape(t, quote=False)  # noqa: E731
    lang_attr = f'<w:lang w:val="{lang}" w:bidi="{lang}" w:eastAsia="{lang}"/>'

    def runs(text):
        out = []
        for i, part in enumerate(re.split(r"\*(.+?)\*", text)):
            if part:
                rpr = ("<w:i/><w:iCs/>" if i % 2 else "") + ("<w:rtl/>" if rtl else "") + lang_attr
                out.append(f'<w:r><w:rPr>{rpr}</w:rPr><w:t xml:space="preserve">{esc(part)}</w:t></w:r>')
        return "".join(out)

    body = []
    for para in md.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        m = re.match(r"(#{1,2}) (.*)", para, re.S)
        style = f'<w:pStyle w:val="Heading{len(m.group(1))}"/>' if m else ""
        text = m.group(2) if m else para
        body.append(f"<w:p><w:pPr>{style}{'<w:bidi/>' if rtl else ''}</w:pPr>{runs(' '.join(text.split()))}</w:p>")
    doc = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="{W}"><w:body>'
        + "".join(body)
        + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1440" w:right="1440" w:bottom="1440"'
        ' w:left="1440" w:header="708" w:footer="708" w:gutter="0"/></w:sectPr></w:body></w:document>'
    )
    styles = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles xmlns:w="{W}">'
        f'<w:docDefaults><w:rPrDefault><w:rPr><w:sz w:val="24"/><w:szCs w:val="24"/>{lang_attr}</w:rPr></w:rPrDefault>'
        '<w:pPrDefault><w:pPr><w:spacing w:after="160" w:line="300" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
        + "".join(
            f'<w:style w:type="paragraph" w:styleId="Heading{n}"><w:name w:val="heading {n}"/><w:basedOn w:val="Normal"/>'
            f'<w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/><w:spacing w:before="360" w:after="160"/>'
            f'<w:outlineLvl w:val="{n - 1}"/></w:pPr><w:rPr><w:b/><w:bCs/><w:sz w:val="{sz}"/><w:szCs w:val="{sz}"/></w:rPr></w:style>'
            for n, sz in ((1, 36), (2, 30))
        )
        + "</w:styles>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
            '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
            "</Types>",
        )
        z.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
            "</Relationships>",
        )
        z.writestr(
            "word/_rels/document.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            "</Relationships>",
        )
        z.writestr(
            "docProps/core.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"'
            ' xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>'
            + esc(title)
            + "</dc:title><dc:language>"
            + lang
            + "</dc:language></cp:coreProperties>",
        )
        z.writestr("word/document.xml", doc)
        z.writestr("word/styles.xml", styles)


# ---------------------------------------------------------------- cli
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("prep")
    s.add_argument("book")
    s.add_argument("-p", "--project", required=True)
    s.add_argument("--words", type=int, default=2500, help="target words per chunk (default 2500)")
    s.add_argument("--from", default="en", help="source language code (default en)")
    s.add_argument(
        "--flat", action="store_true", help="EPUB: rebuild from plain text instead of keeping the original layout"
    )
    s.add_argument("--lang", default="bn", help=f"target language code (default bn). Built in: {', '.join(LANGS)}")
    for name in ("glossary", "run", "status", "build"):
        s = sub.add_parser(name)
        s.add_argument("-p", "--project", required=True)
        if name in ("glossary", "run"):
            s.add_argument(
                "--agents", default="claude", help=f"comma list, first = main translator ({','.join(BACKENDS)})"
            )
            s.add_argument("--model", help="model for the main agent (e.g. sonnet, haiku, gemini-3.8-flash-low)")
        if name == "glossary":
            s.add_argument("--max-terms", type=int, default=250)
        if name == "run":
            s.add_argument("-j", "--jobs", type=int, default=3, help="parallel chunks (default 3)")
            s.add_argument("--retries", type=int, default=2)
            s.add_argument("--fallback", action="store_true", help="use the next agent while the main one is limited")
            s.add_argument("--notify", help="command to send status messages, e.g. send-telegram")
        if name == "build":
            s.add_argument("--title")
            s.add_argument("--layout", choices=["replace", "bilingual"], default="replace",
                           help="replace the text, or keep each original paragraph followed by its translation")  # fmt: skip
    s = sub.add_parser("ui", help="open the local web UI")
    s.add_argument("--dir", default="tomeshift-projects", help="folder for books (default ./tomeshift-projects)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--no-open", action="store_true", help="don't open a browser")
    a = ap.parse_args()
    {
        "prep": cmd_prep,
        "glossary": cmd_glossary,
        "run": cmd_run,
        "status": cmd_status,
        "build": cmd_build,
        "ui": cmd_ui,
    }[a.cmd](a)


def cmd_ui(a):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import tomeshift_ui

    tomeshift_ui.serve(a.dir, a.host, a.port, {c: v[0] for c, v in LANGS.items()}, BACKENDS, not a.no_open)


if __name__ == "__main__":
    main()
