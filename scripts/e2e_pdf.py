"""Inspect a downloaded PDF for the browser tests (local evidence only).

    python scripts/e2e_pdf.py <file.pdf> [page-1.png]

Prints JSON with the extracted text, the embedded font names, the SHA-256 of the file,
whether the WinAnsi fallback square (U+25A0) appears, the document metadata (author,
creator, title) and the fill colours of page 1's vector shapes as #RRGGBB (the brand's
accent strip and bands); optionally renders page 1 to a PNG so the evidence folder shows
what a person would see.
"""

import hashlib
import json
import sys

import pymupdf


def main(argv) -> None:
    path = argv[0]
    data = open(path, "rb").read()
    doc = pymupdf.open(stream=data, filetype="pdf")
    text = "".join(page.get_text() for page in doc)
    fonts = sorted({font[3] for page in doc for font in page.get_fonts()})
    fills = set()
    for drawing in doc[0].get_drawings():
        fill = drawing.get("fill")
        if fill:
            fills.add("#" + "".join(f"{round(c * 255):02X}" for c in fill[:3]))
    meta = doc.metadata or {}
    if len(argv) > 1:
        doc[0].get_pixmap(dpi=110).save(argv[1])
    print(json.dumps({
        "sha256": hashlib.sha256(data).hexdigest(),
        "pages": doc.page_count,
        "fonts": fonts,
        "has_black_square": "■" in text,
        "metadata": {k: meta.get(k) or "" for k in ("author", "creator", "title", "subject")},
        "fill_colours": sorted(fills),
        "text": text,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1:])
