"""Column profiling: what each source column looks like, independent of its header."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import lexicon, parsers
from .ingest import Table
from .normalize import header_hint, is_null, norm_header, norm_text

SAMPLE = 2000  # values per column looked at when profiling


@dataclass
class ColumnProfile:
    index: int
    header: str
    norm: str
    hint: str
    filled: int
    total: int
    distinct: int
    samples: list[str]
    fit: dict[str, float] = field(default_factory=dict)  # value-type -> share of filled values that fit
    date_order: str = "dmy"
    date_order_evidence: tuple[int, int, int] = (0, 0, 0)
    number_style: str = "eu"
    number_style_evidence: tuple[int, int, int] = (0, 0, 0)
    currencies: dict[str, int] = field(default_factory=dict)
    name_order: str = "first_last"
    sensitive: str = ""  # category from lexicon.SENSITIVE_LABELS, or ""
    sensitive_reason: str = ""

    @property
    def fill_rate(self) -> float:
        return self.filled / self.total if self.total else 0.0

    @property
    def unique_rate(self) -> float:
        return self.distinct / self.filled if self.filled else 0.0

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "header": self.header,
            "fill_rate": round(self.fill_rate, 3),
            "distinct": self.distinct,
            "fit": {k: round(v, 3) for k, v in sorted(self.fit.items()) if v > 0},
            "date_order": self.date_order if self.fit.get("date", 0) > 0 else None,
            "number_style": self.number_style if self.fit.get("decimal", 0) > 0 else None,
            "currencies": dict(sorted(self.currencies.items())),
            "sensitive": self.sensitive or None,
        }


_ID_LIKE = re.compile(r"^[A-Za-z]{0,6}[-_/ ]?\d{1,12}[A-Za-z]?$")
_NAME_WORD = re.compile(r"^[^\W\d_][^\W\d_'’.\-]*(?:['’.\-][^\W\d_]+)*\.?$")
_FTE = re.compile(r"^\s*(?:0?[.,]\d+|1(?:[.,]0+)?|0|\d{1,3}(?:[.,]\d+)?\s*%)\s*$")


def _name_like(value: str) -> tuple[bool, int]:
    words = value.replace(",", " ").split()
    if not 1 <= len(words) <= 6:
        return False, 0
    if not all(_NAME_WORD.match(w) for w in words):
        return False, 0
    return True, len(words)


def _sensitive_by_header(norm: str) -> str:
    if norm in lexicon.SENSITIVE_LOOKUP:
        return lexicon.SENSITIVE_LOOKUP[norm]
    toks = set(norm.split())
    for phrase, category in lexicon.SENSITIVE_LOOKUP.items():
        ptoks = phrase.split()
        if len(ptoks) >= 1 and set(ptoks) <= toks and (len(ptoks) > 1 or len(phrase) >= 3):
            return category
    return ""


def profile_column(table: Table, index: int, enum_maps: dict[str, dict[str, str]]) -> ColumnProfile:
    raw = table.column(index)
    total = len(raw)
    values = [v for v in raw if not is_null(v)]
    sample = values[:SAMPLE]
    header = table.headers[index]
    prof = ColumnProfile(
        index=index,
        header=header,
        norm=norm_header(header),
        hint=header_hint(header),
        filled=len(values),
        total=total,
        distinct=len({v.lower() for v in values}),
        samples=list(dict.fromkeys(sample))[:5],
    )
    n = len(sample)
    if not n:
        prof.sensitive = _sensitive_by_header(prof.norm)
        return prof

    # Date order and number style come first: they change how values parse.
    prof.date_order_evidence = parsers.date_order_evidence(sample)
    dmy, mdy, _ = prof.date_order_evidence
    hint = prof.hint.replace(" ", "")
    if hint.startswith(("mm/dd", "mm-dd", "m/d")):
        prof.date_order = "mdy"
    elif hint.startswith(("dd/mm", "dd.mm", "dd-mm", "d/m", "tt.mm", "jj/mm")):
        prof.date_order = "dmy"
    elif mdy > dmy:
        prof.date_order = "mdy"
    prof.number_style_evidence = parsers.number_style_evidence(sample)
    eu, us, _ = prof.number_style_evidence
    prof.number_style = "us" if us > eu else "eu"

    counts = {
        k: 0
        for k in (
            "email",
            "date",
            "serial",
            "decimal",
            "phone",
            "country",
            "currency",
            "id",
            "name",
            "name_multi",
            "fte",
            "iban",
            "national_id",
            "honorific",
        )
    }
    for v in sample:
        if parsers.parse_email(v):
            counts["email"] += 1
        pd = parsers.parse_date(v, prof.date_order)
        if pd:
            counts["date"] += 1
        elif parsers.looks_like_serial(v):
            counts["serial"] += 1
        if parsers.parse_number(v, prof.number_style) is not None:
            counts["decimal"] += 1
        cur = parsers.extract_currency(v)
        if cur:
            prof.currencies[cur] = prof.currencies.get(cur, 0) + 1
        if parsers.parse_country(v):
            counts["country"] += 1
        if norm_text(v).upper() in lexicon.CURRENCY_CODES or v.strip() in lexicon.CURRENCY_SYMBOLS:
            counts["currency"] += 1
        phone, _ = parsers.parse_phone(v)
        if phone and sum(ch.isdigit() for ch in v) >= 7 and not pd:
            counts["phone"] += 1
        if _ID_LIKE.match(v) and len(v) <= 16:
            counts["id"] += 1
        ok, words = _name_like(v)
        if ok:
            counts["name"] += 1
            if words >= 2:
                counts["name_multi"] += 1
        if _FTE.match(v):
            counts["fte"] += 1
        else:
            num = parsers.parse_number(v, prof.number_style)
            if num is not None and 0 < num <= 150 and "%" not in v and not pd:
                counts["fte"] += 1
        if parsers.is_iban(v):
            counts["iban"] += 1
        if parsers.is_national_id(v):
            counts["national_id"] += 1
        if norm_text(v) in lexicon.HONORIFICS:
            counts["honorific"] += 1

    fit = {k: c / n for k, c in counts.items()}
    # Plain integers parse as numbers and as ids; a column of 5-digit values is
    # not a date even though Excel serials are 5-digit numbers.
    fit["date_any"] = (counts["date"] + counts["serial"]) / n
    for name, mapping in enum_maps.items():
        fit["enum:" + name] = sum(1 for v in sample if norm_text(v) in mapping) / n
    if fit["name_multi"] > 0.6 and "," in "".join(sample[:50]):
        prof.name_order = "last_first"
    elif re.search(r"apellidos?\s*(?:y|,)?\s*nombre|name\s*,?\s*vorname|nom\s*(?:et)?\s*pr[eé]nom", prof.norm):
        prof.name_order = "last_first"
    prof.fit = fit

    category = _sensitive_by_header(prof.norm)
    reason = f"header “{header}”" if category else ""
    if not category and fit["iban"] >= 0.5:
        category, reason = "bank_account", f"{counts['iban']} values pass the IBAN checksum"
    elif not category and fit["national_id"] >= 0.5:
        category, reason = "government_id", f"{counts['national_id']} values look like national ID numbers"
    prof.sensitive, prof.sensitive_reason = category, reason
    return prof


def profile_table(table: Table, enum_maps: dict[str, dict[str, str]]) -> list[ColumnProfile]:
    return [profile_column(table, i, enum_maps) for i in range(len(table.headers))]
