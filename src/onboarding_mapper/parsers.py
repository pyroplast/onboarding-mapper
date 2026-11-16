"""Value parsers. Each returns a normalised value or None; none of them raise on bad input."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from . import lexicon
from .normalize import clean_cell, norm_text, strip_accents

# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

_TIME_TAIL = re.compile(r"[T ]\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?\s*(?:[AaPp][Mm])?\s*(?:Z|[+-]\d{2}:?\d{2})?$")
_YMD = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$")
_ABY = re.compile(r"^(\d{1,2})[-/. ](\d{1,2})[-/. ](\d{4}|\d{2})$")
_COMPACT = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_D_MON_Y = re.compile(
    r"^(\d{1,2})(?:st|nd|rd|th|\.|º)?[-/. ]*(?:de |of )?([^\W\d_]{3,12})\.?,?[-/. ]*(?:de |of )?(\d{4}|\d{2})$"
)
_MON_D_Y = re.compile(r"^([^\W\d_]{3,12})\.? (\d{1,2})(?:st|nd|rd|th)?,? (\d{4})$")
_SERIAL = re.compile(r"^\d{5}$")
_EXCEL_EPOCH = date(1899, 12, 30)


@dataclass(frozen=True)
class ParsedDate:
    value: date
    kind: str  # "ymd" | "aby" (day/month order applied) | "text" | "serial"
    ambiguous: bool = False  # a/b/yyyy where both orders give a valid date
    two_digit_year: bool = False


def _mk(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _year(text: str, as_of: date) -> tuple[int, bool]:
    if len(text) == 4:
        return int(text), False
    yy = int(text)
    year = 2000 + yy
    if year > as_of.year + 1:
        year -= 100
    return year, True


def parse_date(
    raw: str, order: str = "dmy", as_of: date | None = None, allow_serial: bool = False
) -> ParsedDate | None:
    """Parse one date. `order` settles 03/04/2021: "dmy" reads 3 April, "mdy" reads 4 March."""
    as_of = as_of or date.today()
    text = _TIME_TAIL.sub("", clean_cell(raw))
    if not text:
        return None
    m = _YMD.match(text)
    if m:
        d = _mk(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return ParsedDate(d, "ymd") if d else None
    m = _ABY.match(text)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        year, two = _year(m.group(3), as_of)
        dmy, mdy = _mk(year, b, a), _mk(year, a, b)
        first, second = (dmy, mdy) if order != "mdy" else (mdy, dmy)
        chosen = first or second
        if not chosen:
            return None
        return ParsedDate(chosen, "aby", ambiguous=bool(dmy and mdy and dmy != mdy), two_digit_year=two)
    m = _COMPACT.match(text)
    if m and 1900 <= int(m.group(1)) <= 2100:
        d = _mk(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        return ParsedDate(d, "ymd") if d else None
    m = _D_MON_Y.match(text)
    if m:
        month = lexicon.MONTHS.get(strip_accents(m.group(2)).lower())
        if month:
            year, two = _year(m.group(3), as_of)
            d = _mk(year, month, int(m.group(1)))
            return ParsedDate(d, "text", two_digit_year=two) if d else None
    m = _MON_D_Y.match(text)
    if m:
        month = lexicon.MONTHS.get(strip_accents(m.group(1)).lower())
        if month:
            d = _mk(int(m.group(3)), month, int(m.group(2)))
            return ParsedDate(d, "text") if d else None
    if allow_serial and _SERIAL.match(text) and 15000 <= int(text) <= 60000:
        return ParsedDate(_EXCEL_EPOCH + timedelta(days=int(text)), "serial")
    return None


def date_order_evidence(values: list[str]) -> tuple[int, int, int]:
    """Count a/b/yyyy values that can only be day-first, only month-first, or either."""
    dmy = mdy = either = 0
    for raw in values:
        m = _ABY.match(_TIME_TAIL.sub("", clean_cell(raw)))
        if not m:
            continue
        a, b = int(m.group(1)), int(m.group(2))
        if a > 12 and b <= 12:
            dmy += 1
        elif b > 12 and a <= 12:
            mdy += 1
        elif a <= 12 and b <= 12 and a != b:
            either += 1
    return dmy, mdy, either


def looks_like_serial(raw: str) -> bool:
    text = clean_cell(raw)
    return bool(_SERIAL.match(text)) and 15000 <= int(text) <= 60000


# ---------------------------------------------------------------------------
# Numbers and money
# ---------------------------------------------------------------------------

_CUR_CODE = re.compile(r"(?<![A-Za-z])([A-Za-z]{3})(?![A-Za-z])")
_NUM_BODY = re.compile(r"^[+-]?\d[\d.,' ]*$|^[+-]?[.,]\d+$")
_GROUPS_DOT = re.compile(r"^[+-]?\d{1,3}(\.\d{3})+$")
_GROUPS_COMMA = re.compile(r"^[+-]?\d{1,3}(,\d{3})+$")


def extract_currency(raw: str) -> str | None:
    """Find a currency in a value like "€ 45.000", "45000 EUR" or "USD"."""
    text = clean_cell(raw)
    if not text:
        return None
    for symbol, code in lexicon.CURRENCY_SYMBOLS.items():
        if symbol in text:
            return code
    for m in _CUR_CODE.finditer(text):
        if m.group(1).upper() in lexicon.CURRENCY_CODES:
            return m.group(1).upper()
    return lexicon.CURRENCY_WORDS.get(norm_text(re.sub(r"[\d.,]", " ", text)))


def _strip_money(raw: str) -> tuple[str, int]:
    text = clean_cell(raw)
    for symbol in lexicon.CURRENCY_SYMBOLS:
        text = text.replace(symbol, " ")
    text = _CUR_CODE.sub(lambda m: " " if m.group(1).upper() in lexicon.CURRENCY_CODES else m.group(0), text)
    text = re.sub(
        r"(?i)\b(?:euros?|dollars?|pounds?|gross|brutto|bruto|brut|p\.?a\.?|/ ?(?:year|yr|month|mo|hour|hr|h|año|mes|jahr|monat|an|mois))\b\.?",
        " ",
        text,
    )
    text = text.replace("%", " ").strip()
    factor = 1
    m = re.search(r"(?i)(?<=\d)\s*k$", text)
    if m:
        factor = 1000
        text = text[: m.start()]
    return text.strip(), factor


def number_style_evidence(values: list[str]) -> tuple[int, int, int]:
    """Count values that prove decimal comma ("eu"), decimal point ("us"), or fit both."""
    eu = us = either = 0
    for raw in values:
        text, _ = _strip_money(raw)
        text = text.replace(" ", "").replace("'", "")
        if not text or not _NUM_BODY.match(text):
            continue
        has_dot, has_comma = "." in text, "," in text
        if has_dot and has_comma:
            if text.rfind(",") > text.rfind("."):
                eu += 1
            else:
                us += 1
        elif has_comma:
            if _GROUPS_COMMA.match(text):
                either += 1
            else:
                eu += 1
        elif has_dot:
            if _GROUPS_DOT.match(text):
                either += 1
            else:
                us += 1
    return eu, us, either


def parse_number(raw: str, style: str = "eu") -> Decimal | None:
    """Parse "45.000,50", "45,000.50", "45 000", "€45k" or "80%".

    `style` only matters for values that fit both conventions, such as "45.000":
    "eu" reads forty-five thousand, "us" reads forty-five.
    """
    text, factor = _strip_money(raw)
    text = text.replace(" ", "").replace("'", "")
    if not text or not _NUM_BODY.match(text):
        return None
    has_dot, has_comma = "." in text, "," in text
    if has_dot and has_comma:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif has_comma:
        if _GROUPS_COMMA.match(text) and style != "eu":
            text = text.replace(",", "")
        elif _GROUPS_COMMA.match(text) and text.count(",") > 1:
            text = text.replace(",", "")
        else:
            text = text.replace(",", ".")
    elif has_dot:
        if _GROUPS_DOT.match(text) and (style == "eu" or text.count(".") > 1):
            text = text.replace(".", "")
    try:
        return Decimal(text) * factor
    except InvalidOperation:
        return None


def number_readings(raw: str) -> list[Decimal]:
    """Every distinct value a number can mean under the two separator conventions."""
    out: list[Decimal] = []
    for style in ("eu", "us"):
        n = parse_number(raw, style)
        if n is not None and n not in out:
            out.append(n)
    return out


def format_decimal(value: Decimal, places: int | None = None) -> str:
    if places is not None:
        value = value.quantize(Decimal(1).scaleb(-places))
        return format(value, "f")
    text = format(value.normalize(), "f")
    return text if "." not in text else text.rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------
# Email, phone
# ---------------------------------------------------------------------------

_EMAIL = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$")
_ANGLE = re.compile(r"<([^<>\s]+@[^<>\s]+)>")


def parse_email(raw: str) -> str | None:
    text = clean_cell(raw)
    m = _ANGLE.search(text)
    if m:
        text = m.group(1)
    text = re.sub(r"(?i)^mailto:", "", text).strip().strip(";,").lower()
    if " " in text or ".." in text or not _EMAIL.match(text):
        return None
    return text


# Calling code and whether a leading 0 is a trunk prefix to drop.
_DIAL = {
    "AT": ("43", True), "AU": ("61", True), "BE": ("32", True), "CH": ("41", True), "CZ": ("420", False),
    "DE": ("49", True), "DK": ("45", False), "ES": ("34", False), "FI": ("358", True), "FR": ("33", True),
    "GB": ("44", True), "IE": ("353", True), "IT": ("39", False), "NL": ("31", True), "NO": ("47", False),
    "PL": ("48", False), "PT": ("351", False), "SE": ("46", True), "UA": ("380", True), "US": ("1", False),
    "CA": ("1", False),
}  # fmt: skip
_PHONE_CHARS = re.compile(r"^[+(\d][\d\s().\-/]*$")


def parse_phone(raw: str, country: str | None = None) -> tuple[str | None, bool]:
    """Return (number, is_e164). Without a country code the digits are kept as written."""
    text = clean_cell(raw)
    text = re.sub(r"(?i)\s*(?:ext\.?|x|#)\s*\d+$", "", text)
    text = re.sub(r"(?i)^(?:tel|phone|mob|mobile|m|t)[.:]\s*", "", text)
    if not text or not _PHONE_CHARS.match(text):
        return None, False
    digits = re.sub(r"\D", "", text.replace("(0)", ""))
    if not 7 <= len(digits) <= 15:
        return None, False
    if text.startswith("+"):
        return "+" + digits, True
    if digits.startswith("00") and len(digits) > 9:
        return "+" + digits[2:], True
    dial = _DIAL.get(country or "")
    if dial:
        code, trunk = dial
        national = digits[1:] if trunk and digits.startswith("0") else digits
        if national.startswith(code) and len(national) > 10:
            return "+" + national, True
        return "+" + code + national, True
    return digits, False


# ---------------------------------------------------------------------------
# Enums, countries
# ---------------------------------------------------------------------------


def parse_country(raw: str) -> str | None:
    # Read the raw cell: "NA" is Namibia here, not a missing value.
    return lexicon.COUNTRY_LOOKUP.get(norm_text(raw))


def parse_currency(raw: str) -> str | None:
    return extract_currency(raw)


def enum_key(raw: str) -> str:
    return norm_text(raw)


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SplitName:
    first: str
    last: str
    uncertain: bool  # three or more name words and no comma: the split is a guess


def split_name(raw: str, order: str = "first_last") -> SplitName | None:
    """Split "Anna Berg", "Berg, Anna" or "Berg Anna" into given and family name."""
    text = clean_cell(raw)
    if not text:
        return None
    if "," in text:
        last, _, first = text.partition(",")
        first, last = first.strip(), last.strip()
        if first and last:
            return SplitName(first, last, False)
        text = (first or last).strip()
    words = text.split(" ")
    if len(words) == 1:
        return SplitName(words[0], "", True) if order == "first_last" else SplitName("", words[0], True)
    core = [w for w in words if norm_text(w) not in lexicon.NAME_PARTICLES]
    uncertain = len(core) >= 3
    if order == "last_first":
        # "García López María": the last word is the given name unless particles say otherwise.
        return SplitName(words[-1], " ".join(words[:-1]), uncertain)
    # "Anna van der Berg": the first word is the given name, particles stay with the surname.
    return SplitName(words[0], " ".join(words[1:]), uncertain)


def title_case_name(text: str) -> str:
    """Fix ALL-CAPS or all-lower names; leave mixed-case names alone ("McDonald", "de la Cruz")."""
    if not text or not (text.isupper() or text.islower()):
        return text

    def cap(word: str) -> str:
        return re.sub(r"[^\W\d_]+", lambda m: m.group(0)[:1].upper() + m.group(0)[1:].lower(), word)

    words = text.split(" ")
    return " ".join(
        w.lower() if (i > 0 and norm_text(w) in lexicon.NAME_PARTICLES) else cap(w) for i, w in enumerate(words)
    )


# ---------------------------------------------------------------------------
# Sensitive identifiers
# ---------------------------------------------------------------------------

_IBAN = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$")
_SSN = re.compile(r"^\d{3}-\d{2}-\d{4}$")
_DNI = re.compile(r"^(\d{8})[- ]?([A-Za-z])$")
_NIE = re.compile(r"^([XYZxyz])[- ]?(\d{7})[- ]?([A-Za-z])$")
_DNI_LETTERS = "TRWAGMYFPDXBNJZSQVHLCKE"


def is_iban(raw: str) -> bool:
    text = clean_cell(raw).replace(" ", "").upper()
    if not _IBAN.match(text):
        return False
    rearranged = text[4:] + text[:4]
    digits = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(digits) % 97 == 1


def is_national_id(raw: str) -> bool:
    text = clean_cell(raw)
    if _SSN.match(text):
        return True
    m = _DNI.match(text)
    if m:
        return _DNI_LETTERS[int(m.group(1)) % 23] == m.group(2).upper()
    m = _NIE.match(text)
    if m:
        number = int("XYZ".index(m.group(1).upper()) * 10_000_000 + int(m.group(2)))
        return _DNI_LETTERS[number % 23] == m.group(3).upper()
    return False
