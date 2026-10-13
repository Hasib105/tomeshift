"""Translate an EPUB in place: only text changes, everything else (CSS, images, fonts, links, ids) is kept.

extract() turns the book into numbered segments. Inline tags become markers the model must keep:
    "Hello ⟦1⟧brave⟦/1⟧ world⟦2/⟧"   (1 = <em>, 2 = a footnote link / image / code: copied verbatim)
build() re-opens the original EPUB, applies the translations to the same elements and writes a new EPUB.
Both walk the files with identical code, so a segment's element path is stable between the two.
Standard library only.
"""

import copy
import html
import html.entities
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile

XHTML = "http://www.w3.org/1999/xhtml"
XML_NS = "http://www.w3.org/XML/1998/namespace"
OPF = "http://www.idpf.org/2007/opf"
DC = "http://purl.org/dc/elements/1.1/"
NCX = "http://www.daisy.org/z3986/2005/ncx/"
for _p, _u in {
    "dc": DC,
    "opf": OPF,
    "epub": "http://www.idpf.org/2007/ops",
    "svg": "http://www.w3.org/2000/svg",
    "xlink": "http://www.w3.org/1999/xlink",
    "m": "http://www.w3.org/1998/Math/MathML",
}.items():
    ET.register_namespace(_p, _u)
ET.register_namespace("", XHTML)  # we only re-serialise XHTML; OPF/NCX get exact text replacement instead

# Elements that hold paragraphs of text. A segment is the innermost one of these.
CONTAINERS = {
    "p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "dt", "dd", "td", "th", "caption", "figcaption",
    "blockquote", "div", "section", "article", "aside", "header", "footer", "nav", "main", "body",
    "table", "thead", "tbody", "tfoot", "tr", "ul", "ol", "dl", "figure", "address", "hgroup", "details", "summary",
}  # fmt: skip
PROTECT = {"pre", "code", "script", "style", "svg", "math", "kbd", "samp", "head", "title"}
HEADINGS = {"h1", "h2", "h3"}
RUN = "tomeshift-run"  # temporary wrapper for loose text that sits next to block elements
TOKEN = re.compile(r"⟦(/?)(\d+)(/?)⟧")
LETTER = re.compile(r"[^\W\d_]")  # any letter in any script


def local(tag):
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def ns_of(tag):
    return tag[1:].split("}")[0] if isinstance(tag, str) and tag.startswith("{") else None


# ---------------------------------------------------------------- parsing that round-trips
XML_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}
PROLOG = re.compile(r"\A(﻿?\s*(?:<\?xml[^>]*\?>\s*)?(?:<!--.*?-->\s*)*(?:<!DOCTYPE[^>\[]*(?:\[.*?\])?\s*>\s*)?)", re.S)


def parse(data):
    """Parse XML/XHTML bytes. Returns (root, prolog) so write() can restore the declaration and DOCTYPE."""
    text = data.decode("utf-8-sig", errors="replace")
    prolog = PROLOG.match(text).group(1)
    body = text[len(prolog) :]

    def entity(m):  # HTML named entities (&nbsp; ...) are not valid XML: make them numeric
        name = m.group(1)
        if name in XML_ENTITIES or name not in html.entities.name2codepoint:
            return m.group(0)
        return f"&#{html.entities.name2codepoint[name]};"

    body = re.sub(r"&([A-Za-z][A-Za-z0-9]*);", entity, body)
    return ET.fromstring(body), prolog.lstrip("﻿")


def write(root, prolog):
    out = ET.tostring(root, encoding="unicode")
    if not prolog.strip():
        prolog = '<?xml version="1.0" encoding="utf-8"?>\n'
    return (prolog + out).encode("utf-8")


# ---------------------------------------------------------------- book layout
def book_files(z):
    """Return (opf_path, content_files_in_order, ncx_path, nav_paths)."""
    container = ET.fromstring(z.read("META-INF/container.xml"))
    opf_path = next(e.get("full-path") for e in container.iter() if local(e.tag) == "rootfile")
    opf = ET.fromstring(z.read(opf_path))
    base = posixpath.dirname(opf_path)
    items, ncx, navs = {}, None, []
    for it in opf.iter():
        if local(it.tag) != "item":
            continue
        href = posixpath.normpath(posixpath.join(base, it.get("href", "")))
        mt = it.get("media-type", "")
        if mt == "application/xhtml+xml":
            items[it.get("id")] = href
            if "nav" in (it.get("properties") or "").split():
                navs.append(href)
        elif mt == "application/x-dtbncx+xml":
            ncx = href
    spine = [items[r.get("idref")] for r in opf.iter() if local(r.tag) == "itemref" and r.get("idref") in items]
    rest = [h for h in items.values() if h not in spine]
    names = set(z.namelist())
    files = [f for f in spine + rest if f in names]
    return opf_path, files, (ncx if ncx in names else None), navs


# ---------------------------------------------------------------- normalisation (same in extract and build)
def has_text(el):
    return bool("".join(_visible_text(el)).strip())


def _visible_text(el):
    if local(el.tag) in PROTECT:
        return
    if el.text:
        yield el.text
    for c in el:
        yield from _visible_text(c)
        if c.tail:
            yield c.tail


def is_container(el):
    return local(el.tag) in CONTAINERS or el.get(RUN) is not None


def wrap_loose_text(root):
    """Text sitting directly in a <div> next to <p>s would otherwise never be translated: wrap it in a <span>."""
    for parent in list(root.iter()):
        if local(parent.tag) in PROTECT or not is_container(parent) or parent.get(RUN) is not None:
            continue
        kids = list(parent)
        if not any(is_container(k) for k in kids):
            continue
        ns = ns_of(parent.tag)
        span_tag = f"{{{ns}}}span" if ns else "span"
        # rebuild the child list, grouping (leading text | inline children + tails) between block children
        new_kids: list = []
        lead = _flush_run(parent.text or "", [], new_kids, span_tag, None)
        run_text, run_kids = "", []
        first = True
        for k in kids:
            if is_container(k):
                if not first or run_kids:
                    lead = _flush_run(run_text, run_kids, new_kids, span_tag, lead)
                first = False
                run_text, run_kids = k.tail or "", []
                k.tail = None
                new_kids.append(k)
            else:
                if first and not run_kids:  # leading text belongs to the first run
                    run_text, lead = lead or "", None
                    first = False
                run_kids.append(k)
        lead = _flush_run(run_text, run_kids, new_kids, span_tag, lead)
        for k in kids:
            parent.remove(k)
        parent.text = lead or None
        parent.extend(new_kids)


def _flush_run(text, inline, new_kids, span_tag, lead):
    """Wrap a run that has words in a span; otherwise put its text and elements (images...) back as they were.
    Returns the parent's leading text (only set while nothing has been placed yet)."""
    if text.strip() or any(has_text(k) for k in inline):
        span = ET.Element(span_tag, {RUN: "1"})
        span.text = text
        span.extend(inline)
        new_kids.append(span)
        return lead
    if not new_kids:
        lead = (lead or "") + text
    else:
        new_kids[-1].tail = (new_kids[-1].tail or "") + text
    new_kids.extend(inline)
    return lead


def unwrap_runs(root):
    for parent in list(root.iter()):
        for i, k in reversed(list(enumerate(parent))):
            if k.get(RUN) is None:
                continue
            prev = parent[i - 1] if i else None
            if prev is None:
                parent.text = (parent.text or "") + (k.text or "")
            else:
                prev.tail = (prev.tail or "") + (k.text or "")
            inner = list(k)
            if inner:
                inner[-1].tail = (inner[-1].tail or "") + (k.tail or "")
            elif prev is None:
                parent.text = (parent.text or "") + (k.tail or "")
            else:
                prev.tail = (prev.tail or "") + (k.tail or "")
            parent.remove(k)
            for j, c in enumerate(inner):
                parent.insert(i + j, c)


def segments_in(root):
    """Yield (path, element) for every innermost text container, in document order."""

    def walk(el, path):
        if local(el.tag) in PROTECT:
            return
        child_containers = [c for c in el if is_container(c) and local(c.tag) not in PROTECT]
        if is_container(el) and not _has_container_below(el) and has_text(el):
            yield path, el
            return
        for i, c in enumerate(el):
            if c in child_containers or _has_container_below(c):
                yield from walk(c, [*path, i])

    yield from walk(root, [])


def _has_container_below(el):
    return any(is_container(d) for d in el.iter() if d is not el and local(d.tag) not in PROTECT)


def at(root, path):
    el = root
    for i in path:
        el = el[i]
    return el


# ---------------------------------------------------------------- inline markers
def encode(el):
    """Element content -> (marked text, store). store[n] = ["pair", tag, attrib] or ["void", xml]."""
    store, parts = {}, []

    def rec(e):
        if e.text:
            parts.append(e.text)
        for c in e:
            n = len(store) + 1
            if local(c.tag) in PROTECT or not isinstance(c.tag, str) or not LETTER.search("".join(_visible_text(c))):
                piece = copy.deepcopy(c)
                piece.tail = None
                store[n] = ["void", ET.tostring(piece, encoding="unicode")]
                parts.append(f"⟦{n}/⟧")
            else:
                store[n] = ["pair", c.tag, dict(c.attrib)]
                parts.append(f"⟦{n}⟧")
                rec(c)
                parts.append(f"⟦/{n}⟧")
            if c.tail:
                parts.append(c.tail)

    rec(el)
    return re.sub(r"\s+", " ", "".join(parts)).strip(), store


def tokens(text):
    return sorted(m.group(0) for m in TOKEN.finditer(text))


def marker_issue(src, out):
    """Compare inline markers of a source and translated segment.

    Returns None when the translation can be applied. Dropping a whole formatting pair (⟦1⟧…⟦/1⟧) is fine:
    the target language may simply have no word to put the emphasis on. Everything else is a real problem:
    a lost image/note marker (⟦2/⟧), an invented or duplicated marker, half a pair, or broken nesting.
    """
    want, got = tokens(src), tokens(out)
    if len(got) != len(set(got)):
        return "a marker appears twice"
    extra = set(got) - set(want)
    if extra:
        return f"unknown markers {sorted(extra)}"
    voids = {t for t in want if t.endswith("/⟧")}
    if voids - set(got):
        return f"lost image/note markers {sorted(voids - set(got))}"
    opened = {t[1:-1] for t in got if not t.startswith("⟦/") and not t.endswith("/⟧")}
    closed = {t[2:-1] for t in got if t.startswith("⟦/")}
    if opened != closed:
        return "a formatting marker is missing its partner"
    return None if _nested(out) else "formatting markers overlap instead of nesting"


def decode_into(el, text, store):
    """Replace el's content with translated `text`. Returns False (and does a safe fallback) if markers broke."""
    keep_tail = el.tail
    for c in list(el):
        el.remove(c)
    el.text = None
    ok = marker_issue(_marked(store), text) is None
    if not ok:  # fallback: plain translated text, then the void elements (images, note links) so nothing is lost
        el.text = TOKEN.sub("", text)
        for kind, *data in store.values():
            if kind == "void":
                el.append(ET.fromstring(data[0]))
        el.tail = keep_tail
        return False
    stack, last = [el], {el: None}
    pos = 0
    for m in TOKEN.finditer(text):
        _append_text(stack[-1], last, text[pos : m.start()])
        pos = m.end()
        close, n, void = m.group(1), int(m.group(2)), m.group(3)
        kind, *data = store[str(n)] if str(n) in store else store[n]
        if close:
            stack.pop()
        elif void or kind == "void":
            child = ET.fromstring(data[0])
            stack[-1].append(child)
            last[stack[-1]] = child
        else:
            child = ET.SubElement(stack[-1], data[0], data[1])
            last[stack[-1]] = child
            last[child] = None
            stack.append(child)
    _append_text(stack[-1], last, text[pos:])
    el.tail = keep_tail
    return True


def _marked(store):
    return "".join(f"⟦{n}/⟧" if v[0] == "void" else f"⟦{n}⟧⟦/{n}⟧" for n, v in store.items())


def _nested(text):
    stack = []
    for m in TOKEN.finditer(text):
        close, n, void = m.groups()
        if void:
            continue
        if close:
            if not stack or stack.pop() != n:
                return False
        else:
            stack.append(n)
    return not stack


def _append_text(parent, last, s):
    if not s:
        return
    prev = last.get(parent)
    if prev is None:
        parent.text = (parent.text or "") + s
    else:
        prev.tail = (prev.tail or "") + s


# ---------------------------------------------------------------- extract
def extract(epub_path):
    """Return a list of segments: {"id", "kind", "file", "path", "store"?, "attr"?, "head", "text"}."""
    segs = []
    with zipfile.ZipFile(epub_path) as z:
        opf_path, files, ncx, _navs = book_files(z)
        for f in files:
            try:
                root, _ = parse(z.read(f))
            except ET.ParseError:
                continue  # malformed file: left as-is (reported at build)
            wrap_loose_text(root)
            for path, el in segments_in(root):
                text, store = encode(el)
                if text and TOKEN.sub("", text).strip():
                    segs.append({"kind": "xhtml", "file": f, "path": path, "store": store,
                                 "head": local(el.tag) in HEADINGS, "text": text})  # fmt: skip
            for path, el in _iter_paths(root):
                alt = el.get("alt")
                if local(el.tag) == "img" and alt and alt.strip():
                    segs.append({"kind": "attr", "attr": "alt", "file": f, "path": path, "head": False, "text": alt})
        for f, wanted in ((opf_path, {"title", "description"}), (ncx, {"text"})):
            if not f:
                continue
            root, _ = parse(z.read(f))
            for path, el in _iter_paths(root):
                if local(el.tag) in wanted and (el.text or "").strip() and len(el) == 0:
                    segs.append({"kind": "xml", "file": f, "path": path, "head": False, "text": el.text.strip()})
    for i, s in enumerate(segs, 1):
        s["id"] = i
    return segs


def _iter_paths(root):
    def rec(el, path):
        yield path, el
        for i, c in enumerate(el):
            yield from rec(c, [*path, i])

    yield from rec(root, [])


# ---------------------------------------------------------------- build
RTL = {"ar", "ur", "fa", "he", "ps", "sd", "ug", "yi"}


def build(epub_path, segs, translations, out_path, lang, layout="replace"):
    """Write the translated EPUB. translations: {segment id: translated marked text}. Returns stats."""
    by_file = {}
    for s in segs:
        by_file.setdefault(s["file"], []).append(s)
    stats = {"translated": 0, "missing": 0, "fallback": 0}
    rtl = lang.split("-")[0] in RTL
    with zipfile.ZipFile(epub_path) as zin, zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zout:
        opf_path, _files, _ncx, navs = book_files(zin)
        zout.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        for info in zin.infolist():
            if info.filename == "mimetype":
                continue
            data = zin.read(info.filename)
            segs_here = by_file.get(info.filename, [])
            if info.filename == opf_path or (segs_here and segs_here[0]["kind"] == "xml"):
                data = _replace_text(data, segs_here, translations, stats, lang if info.filename == opf_path else None)
            elif segs_here:
                data = _rewrite(data, segs_here, translations, stats, lang, rtl,
                                layout if info.filename not in navs else "replace")  # fmt: skip
            zout.writestr(info, data)
    return stats


def _replace_text(data, segs, translations, stats, lang):
    """OPF/NCX: swap exact text nodes in the original bytes, so nothing else in the file changes."""
    text = data.decode("utf-8-sig")
    pos = 0
    for s in segs:
        tr = translations.get(s["id"])
        if not tr:
            stats["missing"] += 1
            continue
        orig = re.escape(html.escape(s["text"], quote=False))
        m = re.compile(r">(\s*)" + orig + r"(\s*)<").search(text, pos)
        if not m:
            stats["missing"] += 1
            continue
        new = ">" + m.group(1) + html.escape(plain(tr), quote=False) + m.group(2) + "<"
        text = text[: m.start()] + new + text[m.end() :]
        pos = m.start() + len(new)
        stats["translated"] += 1
    if lang:
        text = re.sub(
            r"(<dc:language\b[^>]*>)[^<]*(</dc:language>)", lambda m: m.group(1) + lang + m.group(2), text, count=1
        )
    return text.encode("utf-8")


def _rewrite(data, segs, translations, stats, lang, rtl, layout):
    root, prolog = parse(data)
    wrap_loose_text(root)
    bilingual = layout == "bilingual"
    parents = {c: p for p in root.iter() for c in p}
    inserts = []
    for s in segs:
        tr = translations.get(s["id"])
        if not tr:
            stats["missing"] += 1
            continue
        el = at(root, s["path"])
        stats["translated"] += 1
        if s["kind"] == "attr":
            el.set(s["attr"], plain(tr))
        elif bilingual:
            twin = copy.deepcopy(el)
            if not decode_into(twin, tr, s["store"]):
                stats["fallback"] += 1
            for d in twin.iter():  # ids must stay unique: links keep pointing at the original paragraph
                d.attrib.pop("id", None)
            twin.set("lang", lang)
            twin.set(f"{{{XML_NS}}}lang", lang)
            if rtl:
                twin.set("dir", "rtl")
            twin.tail = el.tail
            inserts.append((el, twin))
        elif not decode_into(el, tr, s["store"]):
            stats["fallback"] += 1
    for el, twin in inserts:
        p = parents[el]
        p.insert(list(p).index(el) + 1, twin)
    unwrap_runs(root)
    if not bilingual:
        root.set("lang", lang)
        root.set(f"{{{XML_NS}}}lang", lang)
        if rtl:
            root.set("dir", "rtl")
    return write(root, prolog)


def plain(text):
    """Translated marked text -> plain text (for the Markdown/DOCX copies of the book)."""
    return re.sub(r"\s+", " ", TOKEN.sub("", text)).strip()
