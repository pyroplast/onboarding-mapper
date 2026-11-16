"""Propose a column mapping with a confidence score and the reasons behind it.

Every candidate (source column, target field) gets two scores:

header  how well the column name matches the field's aliases (exact, token
        overlap or character similarity), cut when the header carries a
        qualifier such as "emergency" or "previous"
values  how well the column's values fit the field's type, plus field-specific
        checks (birth-date years, a work-email domain, enum vocabulary)

The final confidence blends both. Fields are then assigned one-to-one, best
score first, so one column never feeds two fields by accident. The only
deliberate exceptions are a full-name column split into first and last name,
and a currency read from the salary column's own values.
"""

from __future__ import annotations

import difflib
import statistics
from dataclasses import dataclass, field
from datetime import date

from . import lexicon, parsers
from .ingest import Table
from .profile import ColumnProfile
from .schema import Field, Schema

AUTO = 0.80  # at or above: applied without asking
REVIEW = 0.55  # at or above: applied, but queued for a person to confirm
FULL_NAME = "__full_name__"

FREE_MAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.es", "yahoo.fr", "yahoo.de", "hotmail.com", "hotmail.es",
    "hotmail.fr", "outlook.com", "outlook.es", "live.com", "icloud.com", "me.com", "gmx.de", "gmx.net", "web.de",
    "proton.me", "protonmail.com", "aol.com", "mail.com", "orange.fr", "free.fr", "t-online.de", "wanadoo.fr",
    "telefonica.net", "personal.example", "mail.example",
}  # fmt: skip


@dataclass
class Candidate:
    field: str
    column: int
    header_score: float
    value_score: float
    confidence: float
    reasons: list[str] = field(default_factory=list)


@dataclass
class FieldMapping:
    field: str
    source: str | None = None  # source header
    column: int | None = None
    confidence: float = 0.0
    status: str = "unmapped"  # auto | review | unmapped | accepted | rejected
    method: str = "direct"  # direct | split_full_name | currency_from_amount
    options: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    alternatives: list[dict] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.source is not None and self.status in {"auto", "review", "accepted"}

    def to_dict(self) -> dict:
        return {
            "field": self.field,
            "source": self.source,
            "method": self.method,
            "confidence": round(self.confidence, 3),
            "status": self.status,
            "options": dict(sorted(self.options.items())),
            "reasons": self.reasons,
            "alternatives": self.alternatives,
        }


@dataclass
class Proposal:
    mappings: dict[str, FieldMapping]
    unused: list[dict]  # source columns not used, with why
    sensitive: list[dict]  # columns held back as sensitive


# ---------------------------------------------------------------------------
# Header score
# ---------------------------------------------------------------------------


def _qualifiers(tokens: set[str]) -> set[str]:
    """Qualifier words in a header, including German-style compounds ("Notfallkontakt")."""
    found = tokens & lexicon.QUALIFIERS
    for tok in tokens:
        for q in lexicon.QUALIFIER_STEMS:
            if q in tok and tok != q:
                found.add(q)
    return found


def header_score(norm: str, aliases: tuple[tuple[str, float], ...] | list[tuple[str, float]]) -> tuple[float, str]:
    """Return (score, why) for one normalised header against a field's aliases."""
    if not norm:
        return 0.0, ""
    hset = set(norm.split())
    best, why = 0.0, ""
    for alias, weight in aliases:
        aset = set(alias.split())
        quals = _qualifiers(hset - aset) - _qualifiers(aset)
        if norm == alias:
            score, how = 1.0, f"header matches alias “{alias}”"
        elif aset <= hset:
            extra = hset - aset
            score = 0.88 - 0.06 * max(0, len(extra) - 1)
            how = f"header contains alias “{alias}”"
        else:
            jac = len(aset & hset) / len(aset | hset)
            ratio = difflib.SequenceMatcher(None, norm.replace(" ", ""), alias.replace(" ", "")).ratio()
            score = max(jac * 0.8, ratio * 0.9 if ratio >= 0.82 else ratio * 0.6)
            how = f"header resembles alias “{alias}”"
        if quals:
            score *= 0.4
            how += f", but is qualified by “{', '.join(sorted(quals))}”"
        score *= weight
        if score > best:
            best, why = score, how
    return best, why


# ---------------------------------------------------------------------------
# Value score
# ---------------------------------------------------------------------------


_CACHE: dict[tuple, object] = {}


def _cached(kind: str, table: Table, prof: ColumnProfile, fn):
    key = (kind, id(table), prof.index)
    if key not in _CACHE:
        if len(_CACHE) > 4096:
            _CACHE.clear()
        _CACHE[key] = fn(table, prof)
    return _CACHE[key]


def _years(table: Table, prof: ColumnProfile) -> list[int]:
    return _cached("years", table, prof, _years_raw)


def _years_raw(table: Table, prof: ColumnProfile) -> list[int]:
    years = []
    for v in table.column(prof.index)[:1000]:
        pd = parsers.parse_date(v, prof.date_order, allow_serial=True)
        if pd:
            years.append(pd.value.year)
    return years


def _email_domains(table: Table, prof: ColumnProfile) -> tuple[float, float]:
    """Share of the most common domain, and share of free-mail domains."""
    return _cached("domains", table, prof, _email_domains_raw)


def _email_domains_raw(table: Table, prof: ColumnProfile) -> tuple[float, float]:
    domains = [e.split("@")[1] for v in table.column(prof.index)[:2000] if (e := parsers.parse_email(v))]
    if not domains:
        return 0.0, 0.0
    top = max(set(domains), key=domains.count)
    return domains.count(top) / len(domains), sum(d in FREE_MAIL for d in domains) / len(domains)


def value_score(f: Field | str, prof: ColumnProfile, table: Table, as_of: date) -> tuple[float, str]:
    """How well the column's values fit a field. 0.5 means "values say nothing either way"."""
    if prof.filled == 0:
        return 0.5, "column is empty"
    fit = prof.fit
    if f == FULL_NAME:
        s = fit.get("name_multi", 0)
        return s, f"{s:.0%} of values look like multi-word names"
    t = f.type
    if t == "id":
        s = min(1.0, prof.unique_rate) * (0.5 + 0.5 * max(fit.get("id", 0), 0.0))
        if fit.get("email", 0) > 0.5 or fit.get("date", 0) > 0.5:
            s *= 0.3
        return s, f"{prof.unique_rate:.0%} unique, {fit.get('id', 0):.0%} id-shaped"
    if t == "name":
        if fit.get("honorific", 0) > 0.5:
            return 0.05, "values are honorifics (Mr, Ms, Dr)"
        single = fit.get("name", 0) - fit.get("name_multi", 0) * 0.5
        return max(0.0, min(1.0, single + 0.2)), f"{fit.get('name', 0):.0%} of values look like names"
    if t == "email":
        s = fit.get("email", 0)
        top, free = _email_domains(table, prof)
        why = f"{s:.0%} valid emails"
        if f.concept == "contact.work_email":
            s *= 0.55 + 0.45 * max(top, 1 - free)
            if free > 0.5:
                s *= 0.5
                why += f", {free:.0%} on free-mail domains"
            else:
                why += f", {top:.0%} share one domain"
        elif f.concept == "contact.personal_email":
            s *= 0.5 + 0.5 * free
            why += f", {free:.0%} on free-mail domains"
        return s, why
    if t == "phone":
        s = fit.get("phone", 0)
        return s, f"{s:.0%} look like phone numbers"
    if t == "date":
        s = fit.get("date_any", 0)
        why = f"{s:.0%} parse as dates"
        years = _years(table, prof)
        if years and s > 0.3:
            med = statistics.median(years)
            if f.concept == "person.birth_date":
                ok = 1940 <= med <= as_of.year - 16
            elif f.concept == "employment.start_date":
                ok = 1975 <= med <= as_of.year + 1 and med > 1940 + 16
            else:
                ok = 1975 <= med <= as_of.year + 2
            if not ok:
                s *= 0.35
                why += f", median year {med:.0f} does not fit"
            if f.concept == "employment.end_date" and prof.fill_rate > 0.9:
                s *= 0.75
                why += ", almost every row has one (unusual for an end date)"
        return s, why
    if t == "enum":
        s = fit.get("enum:" + f.name, 0)
        return s, f"{s:.0%} of values are known {f.name} values"
    if t == "country":
        s = fit.get("country", 0)
        return s, f"{s:.0%} are countries"
    if t == "currency":
        s = fit.get("currency", 0)
        return s, f"{s:.0%} are currency codes"
    if t == "decimal":
        s = fit.get("decimal", 0)
        if s > 0.5 and fit.get("date", 0) > 0.5:
            s *= 0.3
        return s, f"{s:.0%} parse as amounts"
    if t == "ratio":
        s = fit.get("fte", 0)
        return s, f"{s:.0%} look like FTE values or percentages"
    if t == "ref":
        s = max(fit.get("id", 0), fit.get("email", 0), fit.get("name_multi", 0))
        return s, "values look like ids, emails or names"
    # Free text: penalise columns that are plainly something else.
    if fit.get("honorific", 0) > 0.4:
        return 0.05, f"{fit['honorific']:.0%} of values are honorifics (Mr, Ms, Dr)"
    other = max(fit.get("email", 0), fit.get("date_any", 0), fit.get("decimal", 0), fit.get("phone", 0))
    s = 0.6 * (1 - other) + 0.2
    return s, "free text" if other < 0.5 else "values look like numbers, dates or emails"


# Types whose values identify a field on their own, whatever the header says.
DISTINCTIVE = {"email", "enum", "country", "currency"}


def combine(h: float, v: float, distinctive: bool = False) -> float:
    if h >= 0.5:
        c = 0.65 * h + 0.35 * v
        if v < 0.3:
            c *= 0.75  # a good header with values that do not fit is not to be trusted
    elif distinctive and v >= 0.9:
        c = 0.6 + 0.1 * h  # values alone can reach review, never auto-apply
    else:
        c = min(0.5, 0.45 * v + 0.3 * h)
    return round(c, 4)


# ---------------------------------------------------------------------------
# Proposal
# ---------------------------------------------------------------------------

_FULL_NAME_ALIASES = tuple(lexicon.ALIASES["person.full_name"])


def _status(conf: float) -> str:
    return "auto" if conf >= AUTO else ("review" if conf >= REVIEW else "unmapped")


def propose(table: Table, profiles: list[ColumnProfile], schema: Schema, as_of: date | None = None) -> Proposal:
    as_of = as_of or date.today()
    usable = [p for p in profiles if not p.sensitive]
    sensitive = [
        {
            "source": p.header,
            "category": p.sensitive,
            "label": lexicon.SENSITIVE_LABELS[p.sensitive],
            "reason": p.sensitive_reason,
        }
        for p in profiles
        if p.sensitive
    ]

    targets: list[Field | str] = list(schema.fields)
    if schema.has("first_name") and schema.has("last_name"):
        targets.append(FULL_NAME)

    _CACHE.clear()
    candidates: list[Candidate] = []
    for p in usable:
        for f in targets:
            aliases = _FULL_NAME_ALIASES if f == FULL_NAME else f.aliases
            h, hwhy = header_score(p.norm, aliases)
            v, vwhy = value_score(f, p, table, as_of)
            conf = combine(h, v, f != FULL_NAME and f.type in DISTINCTIVE)
            if conf < 0.2:
                continue
            name = FULL_NAME if f == FULL_NAME else f.name
            reasons = [r for r in (hwhy, vwhy) if r]
            candidates.append(Candidate(name, p.index, h, v, conf, reasons))

    order = {name: i for i, name in enumerate(schema.field_names + [FULL_NAME])}
    candidates.sort(key=lambda c: (-c.confidence, order[c.field], c.column))

    by_field: dict[str, list[Candidate]] = {}
    for c in candidates:
        by_field.setdefault(c.field, []).append(c)

    assigned: dict[str, Candidate] = {}
    used: set[int] = set()
    for c in candidates:
        if c.field in assigned or c.column in used or c.confidence < REVIEW:
            continue
        if c.field == FULL_NAME and ("first_name" in assigned or "last_name" in assigned):
            continue
        if c.field in ("first_name", "last_name") and FULL_NAME in assigned:
            continue
        assigned[c.field] = c
        used.add(c.column)

    mappings: dict[str, FieldMapping] = {}
    prof_by_index = {p.index: p for p in profiles}
    for f in schema.fields:
        m = FieldMapping(f.name)
        c = assigned.get(f.name)
        alts = [a for a in by_field.get(f.name, []) if c is None or a.column != c.column][:2]
        m.alternatives = [
            {"source": table.headers[a.column], "confidence": round(a.confidence, 3)}
            for a in alts
            if a.confidence >= 0.3
        ]
        if c:
            p = prof_by_index[c.column]
            m.source, m.column, m.confidence = table.headers[c.column], c.column, c.confidence
            m.status, m.reasons = _status(c.confidence), list(c.reasons)
            m.options = _options(f, p)
        mappings[f.name] = m

    full = assigned.get(FULL_NAME)
    if full is not None:
        p = prof_by_index[full.column]
        for name, part in (("first_name", "first"), ("last_name", "last")):
            m = mappings[name]
            m.source, m.column, m.confidence = table.headers[full.column], full.column, full.confidence
            m.method, m.status = "split_full_name", _status(full.confidence)
            m.options = {"part": part, "name_order": p.name_order}
            m.reasons = list(full.reasons) + [f"split from a full-name column ({p.name_order.replace('_', ' ')})"]

    _currency_from_amount(mappings, prof_by_index, table, schema)

    used_cols = {m.column for m in mappings.values() if m.column is not None}
    unused = []
    for p in usable:
        if p.index in used_cols:
            continue
        best = next((c for c in candidates if c.column == p.index), None)
        why = "no target field matches"
        if best is not None:
            label = "full name" if best.field == FULL_NAME else best.field
            if best.confidence < REVIEW:
                why = f"closest field is {label} at {best.confidence:.2f}, below the {REVIEW} threshold"
            else:
                why = f"{label} was taken by a better-scoring column"
        unused.append({"source": p.header, "reason": why})
    return Proposal(mappings, unused, sensitive)


def _options(f: Field, p: ColumnProfile) -> dict:
    if f.type == "date":
        dmy, mdy, either = p.date_order_evidence
        return {
            "date_order": p.date_order,
            "date_order_evidence": {"day_first_only": dmy, "month_first_only": mdy, "either": either},
        }
    if f.type in ("decimal", "ratio"):
        eu, us, either = p.number_style_evidence
        return {
            "number_style": p.number_style,
            "number_style_evidence": {"decimal_comma": eu, "decimal_point": us, "either": either},
        }
    if f.type == "name":
        return {"name_order": p.name_order}
    return {}


def _currency_from_amount(
    mappings: dict[str, FieldMapping], profiles: dict[int, ColumnProfile], table: Table, schema: Schema
) -> None:
    if not (schema.has("salary_currency") and schema.has("salary_amount")):
        return
    cur, amount = mappings["salary_currency"], mappings["salary_amount"]
    if cur.active or amount.column is None or amount.method != "direct":
        return
    p = profiles[amount.column]
    found = sum(p.currencies.values())
    if not found:
        return
    share = found / p.filled if p.filled else 0
    cur.source, cur.column, cur.method = amount.source, amount.column, "currency_from_amount"
    cur.confidence = round(0.6 + 0.3 * share, 4)
    cur.status = _status(cur.confidence)
    cur.reasons = [
        f"read from the amount column: {share:.0%} of salary values carry a currency ({', '.join(sorted(p.currencies))})"
    ]
