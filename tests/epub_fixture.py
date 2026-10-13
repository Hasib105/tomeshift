"""Builds a small but awkward EPUB for tests: DOCTYPE, &nbsp;, inline tags, footnotes, images, code, tables, NCX."""

import zipfile

PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000")  # header only; never decoded
CSS = b"p { text-indent: 1.5em } .note { font-size: small }"

CHAPTER = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="en" xml:lang="en">
<head><title>Chapter One</title><link rel="stylesheet" type="text/css" href="../style.css"/></head>
<body>
<section epub:type="chapter" id="ch1">
<h1 id="t1">Down the Rabbit-Hole</h1>
<p class="first">Alice was&nbsp;beginning to get <em>very</em> tired of sitting by her sister.<a href="#n1" id="r1" epub:type="noteref"><sup>1</sup></a></p>
<p>Then a <strong>White <em>Rabbit</em></strong> ran close by her.</p>
<div class="box">Loose words in a box.<p>A paragraph inside the box.</p>Trailing loose words.</div>
<figure><img src="../images/rabbit.png" alt="The White Rabbit with a watch"/><figcaption>The Rabbit checks the time.</figcaption></figure>
<pre><code>print("do not translate me")</code></pre>
<table><tr><td>Hatter</td><td>Tea party</td></tr></table>
<aside epub:type="footnote" id="n1"><p>A note about the sister.</p></aside>
</section>
</body>
</html>
"""

NAV = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Contents</title></head>
<body><nav epub:type="toc"><ol><li><a href="text/ch1.xhtml">Down the Rabbit-Hole</a></li></ol></nav></body></html>
"""

NCX = """<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head/>
<docTitle><text>Alice in Wonderland</text></docTitle>
<navMap><navPoint id="p1" playOrder="1"><navLabel><text>Down the Rabbit-Hole</text></navLabel><content src="text/ch1.xhtml"/></navPoint></navMap></ncx>
"""

OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="uid">urn:test:alice</dc:identifier><dc:title>Alice in Wonderland</dc:title>
<dc:language>en</dc:language><dc:description>A girl falls down a rabbit hole.</dc:description>
<meta property="dcterms:modified">2026-01-01T00:00:00Z</meta></metadata>
<manifest>
<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
<item id="css" href="style.css" media-type="text/css"/>
<item id="img" href="images/rabbit.png" media-type="image/png"/>
<item id="ch1" href="text/ch1.xhtml" media-type="application/xhtml+xml"/>
</manifest>
<spine toc="ncx"><itemref idref="ch1"/></spine></package>
"""


def make_epub(path):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
            "</rootfiles></container>",
        )
        z.writestr("OEBPS/content.opf", OPF)
        z.writestr("OEBPS/toc.ncx", NCX)
        z.writestr("OEBPS/nav.xhtml", NAV)
        z.writestr("OEBPS/style.css", CSS)
        z.writestr("OEBPS/images/rabbit.png", PNG)
        z.writestr("OEBPS/text/ch1.xhtml", CHAPTER)
    return path
