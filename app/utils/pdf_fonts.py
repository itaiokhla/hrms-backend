"""Embedded Noto fonts for PDFs (01-white-label §5.7).

ReportLab's built-in Helvetica uses WinAnsi encoding, which has no ₹ glyph, Arabic or
Devanagari; it silently printed amounts as "■1,234.00". Every PDF now embeds the Noto
Sans family (SIL Open Font License, app/assets/fonts/OFL.txt) and text runs in Arabic
or Devanagari switch to the matching Noto font. Complex-script shaping uses HarfBuzz
(uharfbuzz) when installed.
"""

import re
from html import escape
from pathlib import Path
from typing import Optional

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
SANS, SANS_BOLD = "NotoSans", "NotoSans-Bold"
ARABIC, ARABIC_BOLD = "NotoSansArabic", "NotoSansArabic-Bold"
DEVANAGARI, DEVANAGARI_BOLD = "NotoSansDevanagari", "NotoSansDevanagari-Bold"

_FILES = {
    SANS: "NotoSans-Regular.ttf",
    SANS_BOLD: "NotoSans-Bold.ttf",
    ARABIC: "NotoSansArabic-Regular.ttf",
    ARABIC_BOLD: "NotoSansArabic-Bold.ttf",
    DEVANAGARI: "NotoSansDevanagari-Regular.ttf",
    DEVANAGARI_BOLD: "NotoSansDevanagari-Bold.ttf",
}
_registered = False

ARABIC_RUN = re.compile(r"([؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]+(?:[\s.]+[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]+)*)")
DEVANAGARI_RUN = re.compile(r"([ऀ-ॿ꣠-ꣿ]+(?:\s+[ऀ-ॿ꣠-ꣿ]+)*)")

# Symbols that print correctly inside left-to-right amounts. Right-to-left symbols
# (e.g. AED د.إ) are printed as the ISO code so digits never reorder around them.
CURRENCY_SYMBOLS = {
    "INR": "₹", "USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "CNY": "¥", "SGD": "S$", "AUD": "A$",
    "CAD": "C$", "NZD": "NZ$", "HKD": "HK$", "CHF": "CHF ", "ZAR": "R", "KRW": "₩", "PHP": "₱", "THB": "฿",
    "NGN": "₦", "PKR": "Rs ", "LKR": "Rs ", "BDT": "৳", "NPR": "Rs ",
}


def register() -> None:
    global _registered
    if _registered:
        return
    for name, filename in _FILES.items():
        pdfmetrics.registerFont(TTFont(name, str(FONT_DIR / filename), shapable=True))
    pdfmetrics.registerFontFamily(SANS, normal=SANS, bold=SANS_BOLD, italic=SANS, boldItalic=SANS_BOLD)
    pdfmetrics.registerFontFamily(ARABIC, normal=ARABIC, bold=ARABIC_BOLD, italic=ARABIC, boldItalic=ARABIC_BOLD)
    pdfmetrics.registerFontFamily(DEVANAGARI, normal=DEVANAGARI, bold=DEVANAGARI_BOLD, italic=DEVANAGARI,
                                  boldItalic=DEVANAGARI_BOLD)
    _registered = True


def markup(text: Optional[str], bold: bool = False) -> str:
    """Escape `text` for a Paragraph and wrap Arabic/Devanagari runs in their Noto font."""
    value = escape(str(text if text is not None else ""))
    arabic = ARABIC_BOLD if bold else ARABIC
    devanagari = DEVANAGARI_BOLD if bold else DEVANAGARI
    value = ARABIC_RUN.sub(lambda m: f'<font name="{arabic}">{m.group(1)}</font>', value)
    value = DEVANAGARI_RUN.sub(lambda m: f'<font name="{devanagari}">{m.group(1)}</font>', value)
    return value


def money(amount, currency: Optional[str]) -> str:
    currency = (currency or "").upper()
    value = f"{float(amount or 0):,.2f}"
    if currency == "INR":
        value = _indian_grouping(float(amount or 0))
    symbol = CURRENCY_SYMBOLS.get(currency)
    if symbol:
        return f"{symbol}{value}"
    return f"{currency} {value}".strip()


def _indian_grouping(amount: float) -> str:
    negative = amount < 0
    whole, fraction = f"{abs(amount):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return f"{'-' if negative else ''}{whole}.{fraction}"
