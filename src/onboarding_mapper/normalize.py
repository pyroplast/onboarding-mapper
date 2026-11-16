"""Small text helpers shared by every stage."""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

# Cell values that mean "nothing here". Matched after trimming, case-insensitively.
NULL_TOKENS = {
    "", "-", "--", "—", "n/a", "na", "n.a.", "null", "none", "nan", "#n/a", "(blank)", "(empty)", "?", "nil",
    "s/d", "k.a.", "n.d.",
}  # fmt: skip

_WS = re.compile(r"\s+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_BRACKETS = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def clean_cell(value: str | None) -> str:
    """Trim a raw cell and collapse the odd whitespace exports carry (NBSP, tabs, newlines)."""
    if value is None:
        return ""
    return _WS.sub(" ", value.replace(" ", " ").replace("​", "")).strip()


def is_null(value: str) -> bool:
    return clean_cell(value).lower() in NULL_TOKENS


def strip_accents(text: str) -> str:
    text = text.replace("ß", "ss").replace("ø", "o").replace("Ø", "O").replace("ł", "l").replace("Ł", "L")
    return "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))


@lru_cache(maxsize=65536)
def norm_text(text: str) -> str:
    """Lower-case, accent-free, single-spaced text with punctuation turned into spaces."""
    text = strip_accents(clean_cell(text)).lower()
    return _NON_ALNUM.sub(" ", text).strip()


# Abbreviations folded into one spelling before headers are compared.
_TOKEN_CANON = {
    "no": "number", "nr": "number", "num": "number", "numero": "number", "nbr": "number", "n": "number",
    "emp": "employee", "empl": "employee", "ee": "employee",
    "dept": "department", "dpto": "department", "depto": "department", "dep": "department",
    "mgr": "manager", "tel": "phone", "tlf": "phone", "tfno": "phone", "telf": "phone",
    "dt": "date", "addr": "address", "mail": "email", "correo": "email", "courriel": "email",
}  # fmt: skip


def norm_header(header: str) -> str:
    """Normalise a column header for matching.

    "Fecha de Alta (dd/mm/aaaa)" -> "fecha de alta"; "hireDate" -> "hire date";
    "EMP_NO." -> "employee number"; "Nº Empleado" -> "number empleado"; "E-Mail" -> "email".
    """
    text = clean_cell(header)
    text = _BRACKETS.sub(" ", text)
    text = text.replace("#", " number ").replace("%", " percent ").replace("&", " and ")
    text = _CAMEL.sub(" ", text)
    text = norm_text(text)
    text = re.sub(r"\be mail\b", "email", text)
    return " ".join(_TOKEN_CANON.get(t, t) for t in text.split(" ") if t)


def header_hint(header: str) -> str:
    """Text inside brackets in a header, e.g. the "dd/mm/yyyy" of "Start (dd/mm/yyyy)"."""
    return " ".join(m.group(0)[1:-1].strip().lower() for m in _BRACKETS.finditer(clean_cell(header)))


def tokens(text: str) -> list[str]:
    return [t for t in text.split(" ") if t]


def mask(value: str) -> str:
    """Hide most of a value while keeping its shape, for reports that should not carry personal data."""
    value = clean_cell(value)
    if not value:
        return value
    if "@" in value:
        local, _, domain = value.partition("@")
        return (local[:1] + "***@" + domain) if local else "***@" + domain
    out = []
    seen = 0
    for ch in value:
        if ch.isalnum():
            seen += 1
            out.append(ch if seen <= 2 else "•")
        else:
            out.append(ch)
    return "".join(out)
