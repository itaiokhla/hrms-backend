"""PDF rendering of payroll v2 documents from their frozen view model (3.6 §4, 01 §5.7).

Pure apart from reading the content-addressed brand logo: the same content always gives the same
bytes (ReportLab ``invariant=1``: no timestamps or random ids), which is what lets a lost file be
re-rendered and compared with the stored SHA-256 (EC-DOC-03). ``RENDERER`` is stored with each
document; a change of layout code or ReportLab version changes it.

* Fonts: the embedded Noto Sans family of ``app.utils.pdf_fonts`` (₹, Arabic, Devanagari;
  EC-DOC-23). Complex scripts are shaped with HarfBuzz (``shaping = 1``).
* Layouts (``PAYSLIP_TEMPLATE.layout``): ``CLASSIC`` (two-column earnings and deductions tables),
  ``MODERN`` (net-pay highlight card first, then full-width sections), ``COMPACT`` (one table of
  every line, small type, one page for many components).
* Languages (``payslip.languages``): one language, or two side by side ("Earnings · الأرباح"). A
  right-to-left first language mirrors the layout (labels right-aligned, columns reversed);
  numbers always stay left-to-right with Latin digits.
* Money: decimal strings formatted with CLDR grouping for the locale (Indian grouping for en-IN),
  exactly the currency's ISO 4217 minor-unit digits, never floats. Right-to-left currency symbols
  print as the ISO code so digits never reorder. Net pay in words: English, with the Indian
  numbering system (lakh, crore) for Indian locales; other languages fall back to English words.
* ``protect`` encrypts a copy with the employee's password (AES-256) for e-mail attachments; it
  fails closed when no AES-256 implementation is available (never a weak cipher).
"""

from __future__ import annotations

import hashlib
from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import Any, Mapping, Optional, Sequence

import reportlab
from babel import Locale, numbers
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus import Flowable, Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.payroll import currencies
from app.utils import pdf_fonts
from app.utils.pdf_fonts import SANS, SANS_BOLD

RENDERER = f"PDFV2.4-RL{reportlab.Version}"
RTL_LANGUAGES = frozenset({"ar", "he", "fa", "ur"})
GRID = colors.HexColor("#D1D5DB")
SOFT = colors.HexColor("#F3F4F6")
MUTED = colors.HexColor("#4B5563")
_BIDI_MARKS = dict.fromkeys(map(ord, "‎‏؜‪‫‬‭‮"), None)


class ProtectionUnavailable(RuntimeError):
    """No AES-256 PDF encryption is available in this deployment; attachments are not sent."""


# --------------------------------------------------------------------------- vocabulary

LABELS: Mapping[str, Mapping[str, str]] = {
    "en": {
        "PAYSLIP": "Payslip", "REVISED_PAYSLIP": "Revised payslip", "REVERSAL_STATEMENT": "Reversal statement",
        "replaces": "Replaces", "period": "Pay period", "pay_date": "Pay date", "number": "Document no.",
        "employee": "Employee", "employee_code": "Employee ID", "designation": "Designation",
        "department": "Department", "branch": "Work location", "joining_date": "Date of joining",
        "pay_group": "Pay group", "currency": "Currency", "bank": "Bank account", "tax_id": "Tax ID",
        "ATTENDANCE_SUMMARY": "Attendance summary", "days_in_period": "Days in period", "paid_days": "Paid days",
        "lop_days": "Loss-of-pay days", "overtime_hours": "Overtime hours", "leave_days": "Leave taken",
        "EARNINGS": "Earnings", "DEDUCTIONS": "Deductions", "REIMBURSEMENTS": "Reimbursements (not wages)",
        "EMPLOYER_CONTRIBUTIONS": "Employer contributions",
        "employer_note": "Paid by your employer — not part of your take-home pay",
        "component": "Component", "rate": "Rate", "current": "Current", "ytd": "Year to date", "amount": "Amount",
        "TOTALS": "Totals", "gross": "Gross earnings", "total_deductions": "Total deductions",
        "total_reimbursements": "Reimbursements", "net": "Net pay", "in_words": "In words",
        "PAYMENT": "Payment", "status_at_issue": "Status when issued", "reference": "Reference",
        "LEAVE_BALANCES": "Leave balances", "leave_type": "Leave type", "remaining": "Remaining", "used": "Used",
        "TAX_SUMMARY": "Tax summary", "not_available": "Not available",
        "computer_generated": "This is a computer-generated document and does not require a signature.",
        "issued": "issued", "page": "Page", "of": "of", "verify": "Verify this document at",
        "arrears": "Arrears", "superseded": "SUPERSEDED", "draft": "DRAFT — not issued", "type": "Type",
        "kind_EARNING": "Earning", "kind_DEDUCTION": "Deduction", "kind_REIMBURSEMENT": "Reimbursement",
        "kind_EMPLOYER_CONTRIBUTION": "Employer", "signatory": "Authorised signatory",
    },
    "ar": {
        "PAYSLIP": "قسيمة الراتب", "REVISED_PAYSLIP": "قسيمة راتب معدلة", "REVERSAL_STATEMENT": "كشف إلغاء",
        "replaces": "تحل محل", "period": "فترة الدفع", "pay_date": "تاريخ الدفع", "number": "رقم المستند",
        "employee": "الموظف", "employee_code": "رقم الموظف", "designation": "المسمى الوظيفي",
        "department": "القسم", "branch": "موقع العمل", "joining_date": "تاريخ الالتحاق",
        "pay_group": "مجموعة الرواتب", "currency": "العملة", "bank": "الحساب البنكي", "tax_id": "الرقم الضريبي",
        "ATTENDANCE_SUMMARY": "ملخص الحضور", "days_in_period": "أيام الفترة", "paid_days": "الأيام المدفوعة",
        "lop_days": "أيام بدون أجر", "overtime_hours": "ساعات العمل الإضافي", "leave_days": "الإجازات",
        "EARNINGS": "الاستحقاقات", "DEDUCTIONS": "الاستقطاعات", "REIMBURSEMENTS": "المبالغ المستردة",
        "EMPLOYER_CONTRIBUTIONS": "مساهمات صاحب العمل",
        "employer_note": "يدفعها صاحب العمل — ليست جزءاً من صافي راتبك",
        "component": "البند", "rate": "المعدل", "current": "الحالي", "ytd": "منذ بداية السنة", "amount": "المبلغ",
        "TOTALS": "الإجماليات", "gross": "إجمالي الاستحقاقات", "total_deductions": "إجمالي الاستقطاعات",
        "total_reimbursements": "المبالغ المستردة", "net": "صافي الراتب", "in_words": "كتابةً",
        "PAYMENT": "الدفع", "status_at_issue": "الحالة عند الإصدار", "reference": "المرجع",
        "LEAVE_BALANCES": "أرصدة الإجازات", "leave_type": "نوع الإجازة", "remaining": "المتبقي", "used": "المستخدم",
        "TAX_SUMMARY": "ملخص الضرائب", "not_available": "غير متوفر",
        "computer_generated": "هذا مستند صادر إلكترونياً ولا يتطلب توقيعاً.",
        "issued": "صدر", "page": "صفحة", "of": "من", "verify": "تحقق من هذا المستند على",
        "arrears": "متأخرات", "superseded": "مستبدل", "draft": "مسودة", "type": "النوع",
        "kind_EARNING": "استحقاق", "kind_DEDUCTION": "استقطاع", "kind_REIMBURSEMENT": "استرداد",
        "kind_EMPLOYER_CONTRIBUTION": "صاحب العمل", "signatory": "المفوض بالتوقيع",
    },
    "hi": {
        "PAYSLIP": "वेतन पर्ची", "REVISED_PAYSLIP": "संशोधित वेतन पर्ची", "REVERSAL_STATEMENT": "निरसन विवरण",
        "period": "वेतन अवधि", "pay_date": "भुगतान तिथि", "number": "दस्तावेज़ संख्या", "employee": "कर्मचारी",
        "EARNINGS": "आय", "DEDUCTIONS": "कटौतियाँ", "REIMBURSEMENTS": "प्रतिपूर्ति",
        "EMPLOYER_CONTRIBUTIONS": "नियोक्ता अंशदान", "gross": "सकल आय", "total_deductions": "कुल कटौतियाँ",
        "net": "शुद्ध वेतन", "PAYMENT": "भुगतान", "LEAVE_BALANCES": "अवकाश शेष", "TOTALS": "योग",
        "ATTENDANCE_SUMMARY": "उपस्थिति सारांश",
    },
}


def language_of(locale: str | None) -> str:
    return (locale or "en").replace("_", "-").split("-")[0].lower()


def is_rtl(locale: str | None) -> bool:
    return language_of(locale) in RTL_LANGUAGES


def label(key: str, languages: Sequence[str]) -> str:
    """The label in each payslip language, side by side for bilingual payslips (unknown -> English)."""
    seen: list[str] = []
    for loc in languages or ("en",):
        text = LABELS.get(language_of(loc), {}).get(key) or LABELS["en"].get(key, key)
        if text not in seen:
            seen.append(text)
    return " · ".join(seen)


# --------------------------------------------------------------------------- money and numbers


def number_locale(languages: Sequence[str] | None, locale: str | None) -> str:
    """The locale whose grouping formats amounts; right-to-left languages use English digits of the region."""
    candidates = [locale] if locale else []
    candidates += list(languages or [])
    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = Locale.parse(candidate.replace("-", "_"))
        except Exception:  # noqa: BLE001 - unknown locale: try the next one
            continue
        if parsed.language in RTL_LANGUAGES:
            territory = parsed.territory
            return f"en_{territory}" if territory else "en"
        return str(parsed)
    return "en"


def to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, str)) and not isinstance(value, bool):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            pass
    raise ValueError(f"not a decimal amount: {value!r}")


def minor_unit(currency: str) -> int:
    info = currencies.get(currency)
    return 2 if info.minor_unit is None else int(info.minor_unit)


def format_amount(value: Any, currency: str, locale: str = "en") -> str:
    """Grouped amount with exactly the currency's minor-unit digits, no symbol (e.g. ``12,34,567.89``)."""
    amount = to_decimal(value)
    digits = minor_unit(currency)
    quantized = amount.quantize(Decimal(1).scaleb(-digits))
    pattern = "#,##0" + ("." + "0" * digits if digits else "")
    try:
        loc = Locale.parse(locale.replace("-", "_"))
        base = loc.decimal_formats.get(None)
        if base is not None and "#,##,##0" in base.pattern:
            pattern = "#,##,##0" + ("." + "0" * digits if digits else "")
        text = numbers.format_decimal(quantized, format=pattern, locale=loc, decimal_quantization=False)
    except Exception:  # noqa: BLE001 - unknown locale: plain international grouping
        text = numbers.format_decimal(quantized, format=pattern, locale="en", decimal_quantization=False)
    return text.translate(_BIDI_MARKS)


def currency_prefix(currency: str, locale: str = "en") -> str:
    """The left-to-right symbol of the embedded font table, else the ISO code (never a right-to-left symbol)."""
    return pdf_fonts.CURRENCY_SYMBOLS.get(currency) or f"{currency} "


def format_money(value: Any, currency: str, locale: str = "en") -> str:
    """``₹12,34,567.89``, ``$6,200.00``, ``AED 9,500.50``, ``¥1,500``, ``KWD 12.345`` (minus before the symbol)."""
    amount = to_decimal(value)
    text = format_amount(abs(amount), currency, locale)
    sign = "-" if amount < 0 and to_decimal(text.replace(",", "").replace(" ", "").replace("\xa0", "")
                                             .replace(" ", "") or "0") != 0 else ""
    return f"{sign}{currency_prefix(currency, locale)}{text}"


_ONES = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
         "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen")
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")
_MINOR_NAMES = {"INR": "paise", "USD": "cents", "EUR": "cents", "GBP": "pence", "AED": "fils", "SAR": "halalas",
                "KWD": "fils", "BHD": "fils", "OMR": "baisa", "QAR": "dirhams", "SGD": "cents", "AUD": "cents",
                "CAD": "cents", "NZD": "cents", "HKD": "cents", "ZAR": "cents", "PKR": "paisa", "LKR": "cents",
                "NPR": "paisa", "BDT": "poisha", "MYR": "sen", "PHP": "centavos", "CHF": "centimes"}


def _below_thousand(n: int) -> str:
    words = []
    if n >= 100:
        words.append(f"{_ONES[n // 100]} hundred")
        n %= 100
    if n >= 20:
        words.append(_TENS[n // 10] + (f"-{_ONES[n % 10]}" if n % 10 else ""))
    elif n or not words:
        if n:
            words.append(_ONES[n])
    return " ".join(words)


def integer_words(n: int, *, indian: bool = False) -> str:
    """English words of a non-negative integer; Indian system (thousand, lakh, crore) on request."""
    if n == 0:
        return "zero"
    parts: list[str] = []
    if indian:
        crore, n = divmod(n, 10_000_000)
        if crore:
            parts.append(f"{integer_words(crore, indian=True)} crore")
        lakh, n = divmod(n, 100_000)
        if lakh:
            parts.append(f"{_below_thousand(lakh)} lakh")
        thousand, n = divmod(n, 1000)
        if thousand:
            parts.append(f"{_below_thousand(thousand)} thousand")
        if n:
            parts.append(_below_thousand(n))
        return " ".join(parts)
    scales = ((10 ** 12, "trillion"), (10 ** 9, "billion"), (10 ** 6, "million"), (1000, "thousand"))
    for size, name in scales:
        count, n = divmod(n, size)
        if count:
            parts.append(f"{integer_words(count)} {name}")
    if n:
        parts.append(_below_thousand(n))
    return " ".join(parts)


def amount_in_words(value: Any, currency: str, locale: str = "en") -> str:
    """``Twelve lakh thirty-four thousand five hundred sixty-seven Indian rupees and eighty-nine paise``."""
    amount = to_decimal(value)
    digits = minor_unit(currency)
    quantized = abs(amount).quantize(Decimal(1).scaleb(-digits))
    whole = int(quantized)
    fraction = int((quantized - whole) * (10 ** digits)) if digits else 0
    loc = (locale or "en").replace("_", "-")
    indian = loc.endswith("-IN") or language_of(loc) in ("hi", "bn", "ta", "te", "mr", "gu", "kn", "ml")
    try:
        major = numbers.get_currency_name(currency, count=2 if whole != 1 else 1, locale="en")
    except Exception:  # noqa: BLE001
        major = currency
    text = f"{integer_words(whole, indian=indian)} {major}"
    if fraction:
        minor = _MINOR_NAMES.get(currency)
        text += f" and {integer_words(fraction, indian=indian)} {minor}" if minor else f" and {fraction}/{10 ** digits}"
    text = text[0].upper() + text[1:]
    return f"Minus {text[0].lower() + text[1:]}" if amount < 0 else text


# --------------------------------------------------------------------------- styles


def _styles(rtl: bool, compact: bool = False) -> dict[str, ParagraphStyle]:
    pdf_fonts.register()
    size = 7.6 if compact else 8.8
    lead = size + 2.6
    start = TA_RIGHT if rtl else TA_LEFT
    end = TA_LEFT if rtl else TA_RIGHT
    styles = {
        "title": ParagraphStyle("t", fontName=SANS_BOLD, fontSize=15, leading=19, alignment=start, spaceAfter=2),
        "h": ParagraphStyle("h", fontName=SANS_BOLD, fontSize=10, leading=13, alignment=start),
        "body": ParagraphStyle("b", fontName=SANS, fontSize=size, leading=lead, alignment=start),
        "bold": ParagraphStyle("bb", fontName=SANS_BOLD, fontSize=size, leading=lead, alignment=start),
        "small": ParagraphStyle("s", fontName=SANS, fontSize=7, leading=9.2, alignment=start, textColor=MUTED),
        "num": ParagraphStyle("n", fontName=SANS, fontSize=size, leading=lead, alignment=TA_RIGHT),
        "num_bold": ParagraphStyle("nb", fontName=SANS_BOLD, fontSize=size, leading=lead, alignment=TA_RIGHT),
        "head": ParagraphStyle("hw", fontName=SANS_BOLD, fontSize=size, leading=lead, alignment=start,
                               textColor=colors.white),
        "head_num": ParagraphStyle("hwn", fontName=SANS_BOLD, fontSize=size, leading=lead, alignment=TA_RIGHT,
                                   textColor=colors.white),
        "end": ParagraphStyle("e", fontName=SANS, fontSize=size, leading=lead, alignment=end),
        "end_bold": ParagraphStyle("eb", fontName=SANS_BOLD, fontSize=size, leading=lead, alignment=end),
        "big": ParagraphStyle("big", fontName=SANS_BOLD, fontSize=17, leading=21, alignment=TA_RIGHT,
                              textColor=colors.white),
        "big_label": ParagraphStyle("bigl", fontName=SANS_BOLD, fontSize=11, leading=14, alignment=start,
                                    textColor=colors.white),
        "center_small": ParagraphStyle("cs", fontName=SANS, fontSize=7, leading=9, alignment=TA_CENTER,
                                       textColor=MUTED),
    }
    for style in styles.values():
        # Latin text is never shaped: HarfBuzz would form ligatures ("fi" -> U+FB01) that break copy, search and
        # screen readers; complex scripts get a shaped clone in _p (joined Arabic, Devanagari conjuncts).
        style.shaping = 0
    return styles


def _shaped(style: ParagraphStyle) -> ParagraphStyle:
    """The HarfBuzz-shaped twin of ``style`` (cached on the style, which lives for one render)."""
    found = style.__dict__.get("_shaped_twin")
    if found is None:
        found = ParagraphStyle(f"{style.name}-shaped", parent=style)
        found.shaping = 1
        style.__dict__["_shaped_twin"] = found
    return found


def _font_for(token: str, bold: bool) -> str:
    if pdf_fonts.ARABIC_RUN.search(token):
        return pdf_fonts.ARABIC_BOLD if bold else pdf_fonts.ARABIC
    if pdf_fonts.DEVANAGARI_RUN.search(token):
        return pdf_fonts.DEVANAGARI_BOLD if bold else pdf_fonts.DEVANAGARI
    return SANS_BOLD if bold else SANS


class _RtlSafeParagraph(Flowable):
    """A paragraph with right-to-left runs, broken into lines that fit before ReportLab lays it out.

    ReportLab 5 fails ("list index out of range" in setRise) when it wraps a shaped Arabic line by
    itself, so lines are fitted here with explicit breaks (and the type shrunk when one word is wider
    than the column); the inner Paragraph then never wraps.
    """

    def __init__(self, text: str, style: ParagraphStyle, bold: bool) -> None:
        super().__init__()
        self.text, self.style, self.bold = text, style, bold
        self._inner: Optional[Paragraph] = None

    def _fit(self, available: float) -> Paragraph:
        limit = max(available - 2, 1) * 0.94
        size = self.style.fontSize
        words = self.text.split()
        widest = max((stringWidth(w, _font_for(w, self.bold), size) for w in words), default=0)
        style = self.style
        if widest > limit:
            scale = max(limit / widest, 0.55)
            style = self.style.clone(f"{self.style.name}-fit", fontSize=size * scale, leading=self.style.leading * scale)
            style.shaping = 1
            size = style.fontSize
        space = stringWidth(" ", SANS, size)
        lines: list[list[str]] = [[]]
        used = 0.0
        for word in words:
            width = stringWidth(word, _font_for(word, self.bold), size)
            if lines[-1] and used + space + width > limit:
                lines.append([])
                used = 0.0
            used += (space if lines[-1] else 0) + width
            lines[-1].append(word)
        markup = "<br/>".join(pdf_fonts.markup(" ".join(line), self.bold) for line in lines if line)
        return Paragraph(markup, style)

    def wrap(self, availWidth, availHeight):  # noqa: N803 - ReportLab signature
        self._inner = self._fit(availWidth)
        self.width, self.height = self._inner.wrap(availWidth, availHeight)
        return self.width, self.height

    def draw(self):
        self._inner.drawOn(self.canv, 0, 0)


class _ActualText(Flowable):
    """A shaped paragraph wrapped in a PDF ``/Span <</ActualText ...>>`` marked-content sequence.

    HarfBuzz turns Devanagari into reordered matras, half forms and conjunct glyphs that have no
    Unicode code point of their own, so their extracted text (copy, search, screen readers) would be
    garbled. ActualText (ISO 32000-1 14.9.4) gives readers the logical text of the run (EC-DOC-23).
    """

    def __init__(self, inner: Flowable, text: str) -> None:
        super().__init__()
        self.inner, self.text = inner, text

    def wrap(self, availWidth, availHeight):  # noqa: N803 - ReportLab signature
        self.width, self.height = self.inner.wrap(availWidth, availHeight)
        return self.width, self.height

    def split(self, availWidth, availHeight):  # noqa: N803 - one short run: never split
        return []

    def draw(self):
        encoded = ("FEFF" + self.text.encode("utf-16-be").hex()).upper()
        self.canv._code.append(f"/Span <</ActualText <{encoded}>>> BDC")
        self.inner.drawOn(self.canv, 0, 0)
        self.canv._code.append("EMC")


def _p(text: Any, style: ParagraphStyle, bold: bool = False):
    value = "" if text is None else str(text)
    if pdf_fonts.ARABIC_RUN.search(value):
        return _RtlSafeParagraph(value, _shaped(style), bold)
    if pdf_fonts.DEVANAGARI_RUN.search(value):
        return _ActualText(Paragraph(pdf_fonts.markup(value, bold), _shaped(style)), value)
    return Paragraph(pdf_fonts.markup(value, bold), style)


def _fit_numeric(widths: list[float], numeric: Sequence[bool], texts: Sequence[Sequence[str]], size: float,
                 *, keep: float = 0.3) -> list[float]:
    """Column widths in which every amount fits on one line (``12,34,567.89`` never wraps).

    Numeric columns grow to their widest text (bold, plus the cell padding) at the expense of the
    text columns, which keep at least ``keep`` of their share; the total width never changes.
    """
    total = sum(widths)
    needed = list(widths)
    for col, is_number in enumerate(numeric):
        if is_number:
            widest = max((stringWidth(t, SANS_BOLD, size) for t in texts[col] if t), default=0.0)
            needed[col] = max(widths[col], widest + 14)
    extra = sum(n - w for n, w in zip(needed, widths))
    if extra <= 0:
        return widths
    text_cols = [c for c, is_number in enumerate(numeric) if not is_number]
    room = sum(widths[c] * (1 - keep) for c in text_cols)
    if room <= 0:
        return widths
    take = min(extra, room) / room
    result = [needed[c] if numeric[c] else widths[c] - widths[c] * (1 - keep) * take for c in range(len(widths))]
    scale = total / sum(result)
    return [w * scale for w in result]


def _row(cells: list, rtl: bool) -> list:
    return list(reversed(cells)) if rtl else cells


def _widths(widths: list[float], rtl: bool) -> list[float]:
    return list(reversed(widths)) if rtl else widths


# --------------------------------------------------------------------------- shared blocks


def _logo(logo_bytes: Optional[bytes], width: float):
    if not logo_bytes:
        return None
    try:
        img = Image(BytesIO(logo_bytes))
        ratio = img.imageWidth / float(img.imageHeight or 1)
        height = 15 * mm
        img.drawHeight, img.drawWidth = height, min(height * ratio, width * 0.42)
        if img.drawWidth < height * ratio:
            img.drawHeight = img.drawWidth / ratio
        return img
    except Exception:  # noqa: BLE001 - a damaged logo never fails a document
        return None


def _header(content: Mapping[str, Any], st, width: float, rtl: bool, logo_bytes: Optional[bytes]) -> list:
    brand = content.get("brand") or {}
    employer = content.get("employer") or {}
    logo = _logo(logo_bytes, width) or _p(brand.get("display_name") or employer.get("legal_name"), st["title"], True)
    if hasattr(logo, "hAlign"):
        logo.hAlign = "RIGHT" if rtl else "LEFT"
    identity = [_p(employer.get("legal_name") or brand.get("display_name"), st["end_bold"], True)]
    if employer.get("trade_name"):
        identity.append(_p(employer["trade_name"], st["end"]))
    if employer.get("address_line"):
        identity.append(_p(employer["address_line"], st["end"]))
    for line in employer.get("registration_lines") or []:
        identity.append(_p(f"{line.get('label')}: {line.get('value')}", st["end"]))
    table = Table([_row([logo, identity], rtl)], colWidths=[width * 0.5, width * 0.5])
    table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                               ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    strip_colour = colors.HexColor(brand.get("accent_strip") or brand.get("accent") or "#4F46E5")
    strip = Table([[""]], colWidths=[width], rowHeights=[2 * mm])
    strip.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), strip_colour)]))
    return [table, Spacer(1, 2.5 * mm), strip, Spacer(1, 3 * mm)]


def _labels_block(content: Mapping[str, Any], st, width: float = A4[0] - 32 * mm) -> list:
    """Compliance / fixture / sandbox labels as a boxed warning (text only: the embedded fonts have no ⚠ glyph)."""
    texts = [str(text).lstrip("⚠ ").strip() for text in content.get("labels") or [] if str(text).strip()]
    if not texts:
        return []
    box = Table([[_p(text, st["bold"], True)] for text in texts], colWidths=[width])
    box.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#B45309")),
                             ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FEF3C7"))]))
    return [box, Spacer(1, 2 * mm)]


def _key_values(pairs: Sequence[Sequence[Any]], st, width: float, rtl: bool, columns: int = 2,
                bilingual: bool = False) -> Table:
    if bilingual:
        columns = 1  # two-language labels need the room
    cells = [[_p(k, st["bold"], True), _p(v if v not in (None, "") else "—", st["body"])] for k, v in pairs]
    rows = []
    for index in range(0, len(cells), columns):
        chunk = cells[index:index + columns]
        while len(chunk) < columns:
            chunk.append([_p("", st["body"]), _p("", st["body"])])
        rows.append(_row([cell for pair in chunk for cell in pair], rtl))
    unit = width / columns
    widths = _widths([unit * 0.38, unit * 0.62] * columns, rtl)
    table = Table(rows or [[""]], colWidths=widths)
    style = [("GRID", (0, 0), (-1, -1), 0.4, GRID), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]
    for col in range(columns):
        idx = (columns * 2 - 1 - col * 2) if rtl else col * 2
        style.append(("BACKGROUND", (idx, 0), (idx, -1), SOFT))
    table.setStyle(TableStyle(style))
    return table


def _money_table(title: str, rows: Sequence[Mapping[str, Any]], total: Optional[tuple[str, str]], content, st,
                 width: float, rtl: bool, accent, *, show_ytd: bool, show_rate: bool, loc: str,
                 note: Optional[str] = None) -> list:
    currency = content["currency"]
    langs = content.get("languages") or ["en"]
    head = [_p(title, st["head"], True)]
    widths = [0.52]
    if show_rate:
        head.append(_p(label("rate", langs), st["head_num"], True))
        widths.append(0.16)
    head.append(_p(label("current", langs), st["head_num"], True))
    widths.append(0.16)
    if show_ytd:
        head.append(_p(label("ytd", langs), st["head_num"], True))
        widths.append(0.16)
    scale = sum(widths)
    widths = [width * w / scale for w in widths]
    data = [_row(head, rtl)]
    for row in rows:
        name = row.get("label") or row.get("code")
        if row.get("period_ref_label"):
            name = f"{name} ({row['period_ref_label']})"
        cells = [_p(name, st["body"])]
        if show_rate:
            cells.append(_p(format_money(row["rate"], currency, loc) if row.get("rate") else "", st["num"]))
        cells.append(_p(format_money(row["amount"], currency, loc), st["num"]))
        if show_ytd:
            cells.append(_p(format_money(row["ytd"], currency, loc) if row.get("ytd") is not None else "—", st["num"]))
        data.append(_row(cells, rtl))
    if total is not None:
        cells = [_p(total[0], st["bold"], True)]
        if show_rate:
            cells.append(_p("", st["num"]))
        cells.append(_p(format_money(total[1], currency, loc), st["num_bold"], True))
        if show_ytd:
            cells.append(_p("", st["num"]))
        data.append(_row(cells, rtl))
    amounts: list[list[str]] = [[] for _ in widths]
    for row in rows:
        col = 1
        if show_rate:
            amounts[col].append(format_money(row["rate"], currency, loc) if row.get("rate") else "")
            col += 1
        amounts[col].append(format_money(row["amount"], currency, loc))
        if show_ytd and row.get("ytd") is not None:
            amounts[col + 1].append(format_money(row["ytd"], currency, loc))
    if total is not None:
        amounts[2 if show_rate else 1].append(format_money(total[1], currency, loc))
    widths = _fit_numeric(widths, [False] + [True] * (len(widths) - 1), amounts, st["num"].fontSize)
    table = Table(data, colWidths=_widths(widths, rtl), repeatRows=1)
    style = [("BACKGROUND", (0, 0), (-1, 0), accent), ("GRID", (0, 0), (-1, -1), 0.4, GRID),
             ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]
    if total is not None:
        style.append(("BACKGROUND", (0, -1), (-1, -1), SOFT))
    table.setStyle(TableStyle(style))
    block = [table]
    if note:
        block = [_p(note, st["small"])] + block
    return block


def _footer_flow(content: Mapping[str, Any], st) -> list:
    langs = content.get("languages") or ["en"]
    brand = content.get("brand") or {}
    template = content.get("template") or {}
    lines = []
    if template.get("footer_note"):
        lines.append(template["footer_note"])
    signature = template.get("signature")
    if signature:
        lines.append(" — ".join(v for v in (signature.get("name"), signature.get("title")) if v)
                     + f" ({label('signatory', langs)})")
    lines.append(label("computer_generated", langs))
    number = content.get("number")
    issued = str(content.get("issue_date") or "")[:10]
    lines.append(f"{label('number', langs)} {number or '—'} · {label('issued', langs)} {issued}")
    if content.get("verify_base") and content.get("public_id") and not content.get("draft"):
        lines.append(f"{label('verify', langs)} {content['verify_base']}/verify/{content['public_id']}")
    if brand.get("legal_footer"):
        lines.append(brand["legal_footer"])
    return [Spacer(1, 4 * mm)] + [_p(line, st["small"]) for line in lines]


class _NumberedDoc(SimpleDocTemplate):
    """Two passes so the footer can say "Page x of y"."""

    def __init__(self, *args, footer_left: str = "", watermark: Optional[str] = None, overlay: Optional[str] = None,
                 languages: Sequence[str] = ("en",), **kwargs):
        super().__init__(*args, **kwargs)
        self._footer_left = footer_left
        self._watermark = watermark
        self._overlay = overlay
        self._languages = languages

    def _draw(self, canvas, doc, total: Optional[int]):
        canvas.saveState()
        canvas.setFont(SANS, 7)
        canvas.setFillColor(colors.HexColor("#6B7280"))
        canvas.drawString(16 * mm, 9 * mm, self._footer_left)
        page = f"{label('page', ['en'])} {doc.page}" + (f" {label('of', ['en'])} {total}" if total else "")
        canvas.drawRightString(A4[0] - 16 * mm, 9 * mm, page)
        if self._watermark:
            canvas.setFont(SANS_BOLD, 54)
            canvas.setFillColor(colors.Color(0.85, 0.1, 0.1, alpha=0.12))
            canvas.translate(A4[0] / 2, A4[1] / 2)
            canvas.rotate(35)
            canvas.drawCentredString(0, 0, self._watermark)
            canvas.rotate(-35)
            canvas.translate(-A4[0] / 2, -A4[1] / 2)
        if self._overlay:
            canvas.setFont(SANS, 7.5)
            canvas.setFillColor(colors.Color(0.6, 0.0, 0.0, alpha=0.8))
            canvas.drawCentredString(A4[0] / 2, A4[1] - 7 * mm, self._overlay)
        canvas.restoreState()


def _build(story_factory, *, title: str, author: str, subject: str, creator: str, footer_left: str,
           watermark: Optional[str], overlay: Optional[str], languages: Sequence[str]) -> bytes:
    def make(total: Optional[int]) -> tuple[bytes, int]:
        buffer = BytesIO()
        doc = _NumberedDoc(buffer, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=13 * mm,
                           bottomMargin=17 * mm, title=title, author=author, subject=subject, creator=creator,
                           invariant=1, footer_left=footer_left, watermark=watermark, overlay=overlay,
                           languages=languages)
        pages = {"n": 0}

        def on_page(canvas, d):
            pages["n"] = d.page
            doc._draw(canvas, d, total)

        # Flowables are not reusable across builds: each pass lays out a fresh story.
        doc.build(story_factory(), onFirstPage=on_page, onLaterPages=on_page)
        return buffer.getvalue(), pages["n"]

    _, total = make(None)
    data, _ = make(total)
    return data


# --------------------------------------------------------------------------- payslip


def _payslip_story(content: Mapping[str, Any], logo_bytes: Optional[bytes]) -> list:
    template = content.get("template") or {}
    layout = template.get("layout") or "CLASSIC"
    langs = content.get("languages") or ["en"]
    rtl = is_rtl(langs[0])
    compact = layout == "COMPACT"
    st = _styles(rtl, compact)
    width = A4[0] - 32 * mm
    loc = number_locale(langs, content.get("locale"))
    brand = content.get("brand") or {}
    accent = colors.HexColor(brand.get("accent") or "#3730A3")
    currency = content["currency"]
    totals = content.get("totals") or {}
    show_ytd = bool(content.get("show_ytd"))
    story: list = []
    sections = [s for s in (template.get("sections") or [])]
    titles = template.get("titles") or {}

    def title_of(code: str) -> str:
        return titles.get(code) or label(code, langs)

    for code in sections:
        if code == "HEADER":
            story += _header(content, st, width, rtl, logo_bytes)
            heading = label(content.get("title_key") or "PAYSLIP", langs)
            period = content.get("period") or {}
            story.append(_p(f"{heading} · {period.get('label', '')}", st["title"], True))
            if content.get("replaces"):
                story.append(_p(f"{label('replaces', langs)} {content['replaces']}", st["bold"], True))
            story += _labels_block(content, st)
        elif code == "EMPLOYEE":
            employee = content.get("employee") or {}
            period = content.get("period") or {}
            pairs = [
                (label("employee", langs), employee.get("name")),
                (label("employee_code", langs), employee.get("code")),
                (label("designation", langs), employee.get("designation")),
                (label("department", langs), employee.get("department")),
                (label("branch", langs), employee.get("branch")),
                (label("joining_date", langs), employee.get("joining_date")),
                (label("pay_group", langs), employee.get("pay_group")),
                (label("currency", langs), content.get("currency_display") or currency),
                (label("period", langs), f"{period.get('start', '')} – {period.get('end', '')}"),
                (label("pay_date", langs), period.get("pay_date")),
            ]
            if employee.get("bank"):
                pairs.append((label("bank", langs), employee["bank"]))
            if employee.get("tax_id"):
                pairs.append((label("tax_id", langs), employee["tax_id"]))
            story += [_key_values(pairs, st, width, rtl, bilingual=len(langs) > 1), Spacer(1, 3 * mm)]
        elif code == "ATTENDANCE_SUMMARY":
            att = content.get("attendance")
            story.append(_p(title_of(code), st["h"], True))
            if att:
                pairs = [(label(k, langs), att.get(k)) for k in
                         ("days_in_period", "paid_days", "lop_days", "overtime_hours", "leave_days") if k in att]
                story.append(_key_values(pairs, st, width, rtl, columns=3 if not compact else 5, bilingual=len(langs) > 1))
            else:
                story.append(_p(label("not_available", langs), st["small"]))
            story.append(Spacer(1, 3 * mm))
        elif code == "EARNINGS" and not compact:
            earnings = _money_table(title_of("EARNINGS"), content.get("earnings") or [],
                                    (label("gross", langs), totals.get("gross", "0")), content, st,
                                    width if layout == "MODERN" else width * 0.5 - 1.5 * mm, rtl, accent,
                                    show_ytd=show_ytd and layout == "MODERN", show_rate=layout == "MODERN", loc=loc)
            if layout == "CLASSIC" and "DEDUCTIONS" in sections:
                deductions = _money_table(title_of("DEDUCTIONS"), content.get("deductions") or [],
                                          (label("total_deductions", langs), totals.get("deductions", "0")), content,
                                          st, width * 0.5 - 1.5 * mm, rtl, accent, show_ytd=False, show_rate=False,
                                          loc=loc)
                pair = Table([_row([earnings, deductions], rtl)], colWidths=[width * 0.5, width * 0.5])
                pair.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                          ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
                story += [pair, Spacer(1, 3 * mm)]
                if show_ytd:
                    story += _ytd_table(content, st, width, rtl, accent, loc, langs)
            else:
                story += earnings + [Spacer(1, 3 * mm)]
        elif code == "EARNINGS" and compact:
            story += _compact_table(content, st, width, rtl, accent, loc, langs) + [Spacer(1, 2 * mm)]
        elif code == "DEDUCTIONS":
            if layout == "MODERN":
                story += _money_table(title_of("DEDUCTIONS"), content.get("deductions") or [],
                                      (label("total_deductions", langs), totals.get("deductions", "0")), content, st,
                                      width, rtl, accent, show_ytd=show_ytd, show_rate=False, loc=loc)
                story.append(Spacer(1, 3 * mm))
        elif code == "REIMBURSEMENTS":
            rows = content.get("reimbursements") or []
            if rows and not compact:
                story += _money_table(title_of("REIMBURSEMENTS"), rows,
                                      (label("total_reimbursements", langs), totals.get("reimbursements", "0")),
                                      content, st, width, rtl, accent, show_ytd=False, show_rate=False, loc=loc)
                story.append(Spacer(1, 3 * mm))
        elif code == "EMPLOYER_CONTRIBUTIONS":
            rows = content.get("employer_contributions") or []
            if rows:
                story += _money_table(title_of("EMPLOYER_CONTRIBUTIONS"), rows, None, content, st, width, rtl,
                                      colors.HexColor("#6B7280"), show_ytd=False, show_rate=False, loc=loc,
                                      note=label("employer_note", langs))
                story.append(Spacer(1, 3 * mm))
        elif code == "TOTALS":
            story += _totals_block(content, st, width, rtl, accent, loc, langs, highlight=layout == "MODERN")
        elif code == "PAYMENT":
            payment = content.get("payment") or {}
            text = payment.get("text") or label("not_available", langs)
            pairs = [(label("status_at_issue", langs), text)]
            if payment.get("reference"):
                pairs.append((label("reference", langs), payment["reference"]))
            story += [_p(title_of("PAYMENT"), st["h"], True), _key_values(pairs, st, width, rtl, bilingual=len(langs) > 1),
                      Spacer(1, 3 * mm)]
        elif code == "LEAVE_BALANCES":
            balances = content.get("leave_balances")
            if balances:
                rows = [_row([_p(label("leave_type", langs), st["head"], True),
                              _p(label("used", langs), st["head_num"], True),
                              _p(label("remaining", langs), st["head_num"], True)], rtl)]
                for item in balances:
                    rows.append(_row([_p(item.get("name"), st["body"]), _p(item.get("used"), st["num"]),
                                      _p(item.get("remaining") if item.get("remaining") is not None else "—",
                                         st["num"])], rtl))
                table = Table(rows, colWidths=_widths([width * 0.6, width * 0.2, width * 0.2], rtl))
                table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), accent),
                                           ("GRID", (0, 0), (-1, -1), 0.4, GRID)]))
                story += [_p(title_of("LEAVE_BALANCES"), st["h"], True), table, Spacer(1, 3 * mm)]
        elif code == "TAX_SUMMARY":
            tax = content.get("tax_summary")
            if tax:
                story += [_p(title_of("TAX_SUMMARY"), st["h"], True),
                          _key_values([(k, v) for k, v in tax.items()], st, width, rtl), Spacer(1, 3 * mm)]
        elif code == "FOOTER":
            story += _footer_flow(content, st)
    return story


def _ytd_table(content, st, width, rtl, accent, loc, langs) -> list:
    rows = [r for r in (content.get("earnings") or []) + (content.get("deductions") or []) if r.get("ytd") is not None]
    if not rows:
        return []
    currency = content["currency"]
    data = [_row([_p(label("component", langs), st["head"], True), _p(label("ytd", langs), st["head_num"], True)], rtl)]
    for row in rows:
        data.append(_row([_p(row.get("label") or row.get("code"), st["body"]),
                          _p(format_money(row["ytd"], currency, loc), st["num"])], rtl))
    table = Table(data, colWidths=_widths([width * 0.7, width * 0.3], rtl))
    table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), accent), ("GRID", (0, 0), (-1, -1), 0.4, GRID)]))
    return [table, Spacer(1, 3 * mm)]


def _compact_table(content, st, width, rtl, accent, loc, langs) -> list:
    currency = content["currency"]
    show_ytd = bool(content.get("show_ytd"))
    head = [_p(label("component", langs), st["head"], True), _p(label("type", langs), st["head"], True),
            _p(label("current", langs), st["head_num"], True)]
    widths = [0.5, 0.18, 0.16]
    if show_ytd:
        head.append(_p(label("ytd", langs), st["head_num"], True))
        widths.append(0.16)
    scale = sum(widths)
    data = [_row(head, rtl)]
    for section, kind in (("earnings", "EARNING"), ("deductions", "DEDUCTION"), ("reimbursements", "REIMBURSEMENT")):
        for row in content.get(section) or []:
            name = row.get("label") or row.get("code")
            if row.get("period_ref_label"):
                name = f"{name} ({row['period_ref_label']})"
            cells = [_p(name, st["body"]), _p(label(f"kind_{kind}", langs), st["body"]),
                     _p(format_money(row["amount"], currency, loc), st["num"])]
            if show_ytd:
                cells.append(_p(format_money(row["ytd"], currency, loc) if row.get("ytd") is not None else "—",
                                st["num"]))
            data.append(_row(cells, rtl))
    amounts: list[list[str]] = [[] for _ in widths]
    for section in ("earnings", "deductions", "reimbursements"):
        for row in content.get(section) or []:
            amounts[2].append(format_money(row["amount"], currency, loc))
            if show_ytd and row.get("ytd") is not None:
                amounts[3].append(format_money(row["ytd"], currency, loc))
    fitted = _fit_numeric([width * w / scale for w in widths], [False, False] + [True] * (len(widths) - 2), amounts,
                          st["num"].fontSize)
    table = Table(data, colWidths=_widths(fitted, rtl), repeatRows=1)
    table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), accent), ("GRID", (0, 0), (-1, -1), 0.3, GRID),
                               ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5)]))
    return [table]


def _totals_block(content, st, width, rtl, accent, loc, langs, *, highlight: bool) -> list:
    totals = content.get("totals") or {}
    currency = content["currency"]
    rows = [
        _row([_p(label("gross", langs), st["body"]), _p(format_money(totals.get("gross", "0"), currency, loc),
                                                         st["num"])], rtl),
        _row([_p(label("total_deductions", langs), st["body"]),
              _p(format_money(totals.get("deductions", "0"), currency, loc), st["num"])], rtl),
    ]
    if totals.get("reimbursements") not in (None, "0", "0.00") and to_decimal(totals.get("reimbursements")) != 0:
        rows.append(_row([_p(label("total_reimbursements", langs), st["body"]),
                          _p(format_money(totals["reimbursements"], currency, loc), st["num"])], rtl))
    summary = Table(rows, colWidths=_widths([width * 0.7, width * 0.3], rtl))
    summary.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, GRID)]))
    net = Table([_row([_p(f"{label('net', langs)} ({currency})", st["big_label"], True),
                       _p(format_money(totals.get("net", "0"), currency, loc), st["big"], True)], rtl)],
                colWidths=_widths([width * 0.6, width * 0.4], rtl))
    net.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), accent), ("TOPPADDING", (0, 0), (-1, -1), 6),
                             ("BOTTOMPADDING", (0, 0), (-1, -1), 6), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    words = totals.get("net_words")
    block = [summary, Spacer(1, 1.5 * mm), net] if not highlight else [net, Spacer(1, 1.5 * mm), summary]
    if words:
        block.append(_p(f"{label('in_words', langs)}: {words}", st["small"]))
    return [KeepTogether(block), Spacer(1, 3 * mm)]


# --------------------------------------------------------------------------- generic documents


def _generic_story(content: Mapping[str, Any], logo_bytes: Optional[bytes]) -> list:
    langs = content.get("languages") or ["en"]
    rtl = is_rtl(langs[0])
    compact = bool(content.get("compact"))
    st = _styles(rtl, compact)
    width = A4[0] - 32 * mm
    brand = content.get("brand") or {}
    accent = colors.HexColor(brand.get("accent") or "#3730A3")
    story = _header(content, st, width, rtl, logo_bytes)
    story.append(_p(content.get("title") or "", st["title"], True))
    if content.get("subtitle"):
        story.append(_p(content["subtitle"], st["bold"], True))
    story += _labels_block(content, st)
    if content.get("info"):
        story += [_key_values(content["info"], st, width, rtl), Spacer(1, 3 * mm)]
    for paragraph in content.get("paragraphs") or []:
        story += [_p(paragraph, st["body"]), Spacer(1, 2 * mm)]
    for table in content.get("tables") or []:
        columns = table.get("columns") or []
        numeric = table.get("numeric") or [False] * len(columns)
        if table.get("title"):
            story.append(_p(table["title"], st["h"], True))
        weights = table.get("widths") or [1] * len(columns)
        scale = sum(weights) or 1
        widths = [width * w / scale for w in weights]
        data = [_row([_p(c, st["head_num"] if numeric[i] else st["head"], True) for i, c in enumerate(columns)], rtl)]
        for row in table.get("rows") or []:
            data.append(_row([_p(v, st["num"] if numeric[i] else st["body"]) for i, v in enumerate(row)], rtl))
        total_row = table.get("total_row")
        if total_row:
            data.append(_row([_p(v, st["num_bold"] if numeric[i] else st["bold"], True)
                              for i, v in enumerate(total_row)], rtl))
        if len(data) == 1:
            data.append(_row([_p("—", st["body"])] + [_p("", st["body"])] * (len(columns) - 1), rtl))
        if any(numeric) and not all(numeric):
            cells = [*(table.get("rows") or []), *([total_row] if total_row else [])]
            texts = [[str(r[i]) for r in cells if i < len(r) and r[i] not in (None, "")] for i in range(len(columns))]
            widths = _fit_numeric(widths, numeric, texts, st["num"].fontSize)
        grid = Table(data, colWidths=_widths(widths, rtl), repeatRows=1)
        style = [("BACKGROUND", (0, 0), (-1, 0), accent), ("GRID", (0, 0), (-1, -1), 0.3, GRID),
                 ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]
        if total_row:
            style.append(("BACKGROUND", (0, -1), (-1, -1), SOFT))
        grid.setStyle(TableStyle(style))
        story += [grid, Spacer(1, 3 * mm)]
    for note in content.get("notes") or []:
        story.append(_p(note, st["small"]))
    story += _footer_flow(content, st)
    return story


# --------------------------------------------------------------------------- entry points


def render(content: Mapping[str, Any], logo_bytes: Optional[bytes] = None, *, overlay: Optional[str] = None) -> bytes:
    """Deterministic PDF bytes of a frozen view model (``overlay``: an export watermark line on every page)."""
    kind = content.get("kind") or "GENERIC"
    story_of = _payslip_story if kind == "PAYSLIP" else _generic_story
    brand = content.get("brand") or {}
    employer = content.get("employer") or {}
    langs = content.get("languages") or ["en"]
    watermark = None
    if content.get("draft"):
        watermark = label("draft", ["en"])
    elif content.get("watermark"):
        watermark = str(content["watermark"])
    powered = brand.get("powered_by_label") if brand.get("show_powered_by") else None
    title = content.get("title") or label(content.get("title_key") or "PAYSLIP", ["en"])
    return _build(
        lambda: story_of(content, logo_bytes), title=str(title), author=employer.get("legal_name") or brand.get("display_name") or "",
        subject=str(content.get("doc_type") or kind), creator=brand.get("product_name") or "",
        footer_left=powered or brand.get("product_name") or "", watermark=watermark, overlay=overlay,
        languages=langs,
    )


def logo_for(content: Mapping[str, Any]) -> tuple[Optional[bytes], bool]:
    """(logo bytes, missing): the content-addressed brand file the document was issued with."""
    logo = (content.get("brand") or {}).get("logo")
    if not logo:
        return None, False
    from app.services import brand_asset_service

    data = brand_asset_service.read_asset(logo["sha256"], logo["ext"])
    return data, data is None


def render_with_brand(content: Mapping[str, Any], *, overlay: Optional[str] = None) -> tuple[bytes, bool]:
    logo, missing = logo_for(content)
    return render(content, logo, overlay=overlay), missing


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def protect(data: bytes, password: str) -> bytes:
    """A copy of ``data`` encrypted with AES-256 for ``password`` (printing allowed, editing not).

    Raises ProtectionUnavailable when no AES-256 implementation is installed: a weaker cipher is never
    used, and the caller then sends a link instead of an attachment.
    """
    if not password or not 8 <= len(password) <= 40:
        raise ValueError("a PDF password has 8 to 40 characters")
    try:
        import pymupdf  # type: ignore[import-not-found]
    except Exception as exc:  # noqa: BLE001
        raise ProtectionUnavailable("AES-256 PDF encryption is not installed") from exc
    document = pymupdf.open(stream=data, filetype="pdf")
    try:
        owner = hashlib.sha256(b"owner:" + password.encode("utf-8") + data[:64]).hexdigest()[:40]  # PDF limit: 40
        return document.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw=password, owner_pw=owner,
                                permissions=int(pymupdf.PDF_PERM_PRINT | pymupdf.PDF_PERM_ACCESSIBILITY))
    finally:
        document.close()
