"""Apply a mapping: turn source rows into target records and say what went wrong, cell by cell."""

from __future__ import annotations

import difflib
import math
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from . import parsers
from .ingest import Table
from .matcher import FieldMapping
from .normalize import clean_cell, is_null, norm_text
from .schema import Field, Schema

ERROR, WARNING, INFO = "error", "warning", "info"


@dataclass
class Issue:
    code: str
    severity: str
    message: str
    field: str = ""
    source: str = ""
    row: int | None = None  # line number in the source file
    record: int | None = None  # 0-based record index
    value: str = ""
    suggestion: str = ""

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "field": self.field,
            "source": self.source,
            "row": self.row,
            "value": self.value,
            "message": self.message,
            "suggestion": self.suggestion,
        }


@dataclass
class Result:
    fields: list[str]
    records: list[dict[str, str]]
    rows: list[int]  # source line number per record
    issues: list[Issue] = field(default_factory=list)
    ref_resolution: dict[str, dict[str, int]] = field(default_factory=dict)

    def errors_by_record(self) -> dict[int, list[Issue]]:
        out: dict[int, list[Issue]] = {}
        for issue in self.issues:
            if issue.record is not None and issue.severity == ERROR:
                out.setdefault(issue.record, []).append(issue)
        return out

    @property
    def ready(self) -> list[int]:
        bad = self.errors_by_record()
        return [i for i in range(len(self.records)) if i not in bad]

    @property
    def held(self) -> list[int]:
        bad = self.errors_by_record()
        return [i for i in range(len(self.records)) if i in bad]


# ---------------------------------------------------------------------------
# Cell transforms. Each returns (value, issue or None).
# ---------------------------------------------------------------------------


class _Ctx:
    def __init__(self, f: Field, m: FieldMapping, table: Table, as_of: date, default_country: str | None) -> None:
        self.f, self.m, self.as_of, self.default_country = f, m, as_of, default_country
        self.column = table.column(m.column) if m.column is not None else []
        self.percent_scale = False
        if f.type == "ratio":
            nums = [parsers.parse_number(v, m.options.get("number_style", "eu")) for v in self.column if not is_null(v)]
            nums = [n for n in nums if n is not None]
            self.percent_scale = bool(nums) and statistics.median(nums) > 1
        self.median: Decimal | None = None
        self.column_currency: str | None = None
        if f.type == "decimal":
            # The median comes from values only one reading fits, so ambiguous
            # cells ("62,000" in a decimal-comma column) can be judged against it.
            clear = [r[0] for v in self.column if not is_null(v) and len(r := parsers.number_readings(v)) == 1]
            clear = [n for n in clear if n > 0]
            self.median = statistics.median(clear) if len(clear) >= 5 else None
        if m.method == "currency_from_amount":
            found = {c for v in self.column if (c := parsers.extract_currency(v))}
            self.column_currency = found.pop() if len(found) == 1 else None


def _issue(code: str, severity: str, message: str, ctx: _Ctx, raw: str, suggestion: str = "") -> Issue:
    return Issue(code, severity, message, ctx.f.name, ctx.m.source or "", value=raw, suggestion=suggestion)


def transform(ctx: _Ctx, raw: str, row_country: str | None) -> tuple[str, Issue | None]:
    f, m = ctx.f, ctx.m
    if m.method == "split_full_name":
        parts = parsers.split_name(raw, m.options.get("name_order", "first_last"))
        if parts is None:
            return "", None
        value = parsers.title_case_name(parts.first if m.options.get("part") == "first" else parts.last)
        if parts.uncertain and m.options.get("part") == "last":
            return value, _issue(
                "NAME_SPLIT_UNCERTAIN",
                WARNING,
                "full name has three or more words; the first/last split is a guess",
                ctx,
                raw,
                f"first_name={parts.first!r}, last_name={parts.last!r}",
            )
        if not value:
            return "", _issue(
                "NAME_PART_MISSING", WARNING, f"full name has no {m.options.get('part')} name part", ctx, raw
            )
        return value, None
    if m.method == "currency_from_amount":
        cur = parsers.extract_currency(raw)
        if cur is None and ctx.column_currency:
            return ctx.column_currency, _issue(
                "CURRENCY_INFERRED",
                INFO,
                f"amount has no currency; every other amount in the column is {ctx.column_currency}",
                ctx,
                raw,
            )
        return (cur or ""), None

    t = f.type
    if t == "id":
        value = clean_cell(raw)
        if value.endswith(".0") and value[:-2].isdigit():
            value = value[:-2]
        return value, None
    if t == "string":
        return clean_cell(raw), None
    if t == "name":
        return parsers.title_case_name(clean_cell(raw)), None
    if t == "email":
        email = parsers.parse_email(raw)
        if email is None:
            return "", _issue("INVALID_EMAIL", ERROR if f.required else WARNING, "not a valid email address", ctx, raw)
        return email, None
    if t == "phone":
        country = row_country or ctx.default_country
        number, e164 = parsers.parse_phone(raw, country)
        if number is None:
            return "", _issue("INVALID_PHONE", WARNING, "not a phone number", ctx, raw)
        if not e164:
            return number, _issue(
                "PHONE_NO_COUNTRY",
                INFO,
                "no country code and no country for this row; kept as written",
                ctx,
                raw,
                "pass --default-country or map a country column",
            )
        return number, None
    if t == "date":
        pd = parsers.parse_date(raw, m.options.get("date_order", "dmy"), ctx.as_of, allow_serial=True)
        if pd is None:
            return "", _issue(
                "INVALID_DATE", ERROR if f.required else WARNING, "not a date in any known format", ctx, raw
            )
        value = pd.value
        lo_age, hi_age = f.opt("min_age_years"), f.opt("max_age_years")
        if lo_age is not None and value > _years_before(ctx.as_of, lo_age):
            return value.isoformat(), _issue("DATE_OUT_OF_RANGE", ERROR, f"implies an age under {lo_age}", ctx, raw)
        if hi_age is not None and value < _years_before(ctx.as_of, hi_age):
            return value.isoformat(), _issue("DATE_OUT_OF_RANGE", ERROR, f"implies an age over {hi_age}", ctx, raw)
        future = f.opt("max_future_days")
        if future is not None and value > ctx.as_of + timedelta(days=future):
            return value.isoformat(), _issue(
                "DATE_OUT_OF_RANGE", ERROR, f"more than {future} days in the future", ctx, raw
            )
        if value.year < 1900:
            return value.isoformat(), _issue("DATE_OUT_OF_RANGE", ERROR, "before 1900", ctx, raw)
        if pd.kind == "serial":
            return value.isoformat(), _issue("DATE_FROM_SERIAL", INFO, "read as an Excel serial date number", ctx, raw)
        if pd.two_digit_year:
            return value.isoformat(), _issue("TWO_DIGIT_YEAR", INFO, f"two-digit year read as {value.year}", ctx, raw)
        return value.isoformat(), None
    if t == "enum":
        key = norm_text(raw)
        canon = f.enum_map.get(key)
        if canon is None:
            canon = contained_enum(key, f.enum_map)
            if canon is not None:
                return canon, _issue(
                    "ENUM_BY_PHRASE", INFO, f"matched {canon!r} by a phrase inside the value", ctx, raw
                )
        if canon is None:
            close = difflib.get_close_matches(norm_text(raw), list(f.enum_map), n=1, cutoff=0.75)
            hint = f"did you mean {f.enum_map[close[0]]!r}?" if close else f"allowed: {', '.join(f.values)}"
            return "", _issue("UNKNOWN_ENUM", WARNING, f"value not recognised for {f.name}", ctx, raw, hint)
        return canon, None
    if t == "country":
        code = parsers.parse_country(raw)
        if code is None:
            return "", _issue("UNKNOWN_COUNTRY", WARNING, "country not recognised", ctx, raw)
        return code, None
    if t == "currency":
        code = parsers.parse_currency(raw)
        if code is None:
            return "", _issue("UNKNOWN_CURRENCY", WARNING, "currency not recognised", ctx, raw)
        return code, None
    if t == "decimal":
        n = parsers.parse_number(raw, m.options.get("number_style", "eu"))
        if n is None:
            return "", _issue("INVALID_NUMBER", WARNING, "not a number", ctx, raw)
        readings = parsers.number_readings(raw)
        resolved = None
        if len(readings) > 1 and ctx.median:
            best = min(readings, key=lambda r: abs(math.log(float(r) / float(ctx.median))) if r > 0 else math.inf)
            if best != n:
                resolved = _issue(
                    "SEPARATOR_BY_MAGNITUDE",
                    INFO,
                    f"read as {parsers.format_decimal(best)} not {parsers.format_decimal(n)}: closer to the column median",
                    ctx,
                    raw,
                )
                n = best
        lo = f.opt("min_exclusive")
        if lo is not None and n <= lo:
            return parsers.format_decimal(n, f.opt("places")), _issue(
                "NUMBER_OUT_OF_RANGE", ERROR, f"must be greater than {lo}", ctx, raw
            )
        value = parsers.format_decimal(n, f.opt("places"))
        if resolved is not None:
            return value, resolved
        if ctx.median and (n > ctx.median * 8 or n < ctx.median / 8):
            ratio = n / ctx.median
            return value, _issue(
                "NUMBER_OUTLIER",
                WARNING,
                f"{ratio:.2g}× the column median ({parsers.format_decimal(ctx.median, 0)})",
                ctx,
                raw,
                "check for a missing or extra digit, a monthly figure in an annual column, or a different currency",
            )
        return value, None
    if t == "ratio":
        n = parsers.parse_number(raw, m.options.get("number_style", "eu"))
        if n is None:
            return "", _issue("INVALID_NUMBER", WARNING, "not a number", ctx, raw)
        if "%" in raw or (ctx.percent_scale and n > 1):
            n = n / 100
        if not Decimal(0) < n <= Decimal("1.5"):
            return parsers.format_decimal(n), _issue(
                "NUMBER_OUT_OF_RANGE", ERROR, "FTE must be above 0 and at most 1.5", ctx, raw
            )
        return parsers.format_decimal(n.quantize(Decimal("0.0001"))), None
    if t == "ref":
        return clean_cell(raw), None  # resolved after every record exists
    return clean_cell(raw), None


def contained_enum(key: str, enum_map: dict[str, str]) -> str | None:
    """Match "en conge parental" through the known phrase "conge parental".

    Only phrases of two or more words count, and every phrase found must point
    at the same value; otherwise the cell stays unmatched and goes to review.
    """
    padded = f" {key} "
    found = {canon for phrase, canon in enum_map.items() if " " in phrase and f" {phrase} " in padded}
    return found.pop() if len(found) == 1 else None


def _years_before(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year - years)
    except ValueError:  # 29 February
        return d.replace(year=d.year - years, day=28)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def run(
    table: Table,
    schema: Schema,
    mappings: dict[str, FieldMapping],
    as_of: date | None = None,
    default_country: str | None = None,
) -> Result:
    as_of = as_of or date.today()
    names = schema.field_names
    result = Result(names, [], list(table.row_numbers))
    active = {name: m for name, m in mappings.items() if m.active and m.column is not None}
    contexts = {name: _Ctx(schema.get(name), m, table, as_of, default_country) for name, m in active.items()}
    country_ctx = contexts.get("country")

    for i, row in enumerate(table.rows):
        line = table.row_numbers[i]
        record = {name: "" for name in names}
        errored: set[str] = set()
        row_country = None
        if country_ctx is not None:
            row_country = (
                parsers.parse_country(row[country_ctx.m.column]) if not is_null(row[country_ctx.m.column]) else None
            )
        for name in names:
            ctx = contexts.get(name)
            if ctx is None:
                continue
            raw = row[ctx.m.column]
            if is_null(raw):
                continue
            value, issue = transform(ctx, raw, row_country)
            record[name] = value
            if issue is not None:
                issue.row, issue.record = line, i
                result.issues.append(issue)
                if issue.severity == ERROR:
                    errored.add(name)
        for f in schema.fields:
            if f.required and not record[f.name] and f.name not in errored:
                if f.name in active:
                    result.issues.append(
                        Issue(
                            "MISSING_REQUIRED",
                            ERROR,
                            "required value is empty",
                            f.name,
                            active[f.name].source or "",
                            line,
                            i,
                        )
                    )
        result.records.append(record)

    _unique(result, schema, active)
    _refs(result, schema, active)
    _rules(result, schema)
    _unmapped_required(result, schema, active)
    return result


def _unmapped_required(result: Result, schema: Schema, active: dict[str, FieldMapping]) -> None:
    for f in schema.fields:
        if f.required and f.name not in active:
            result.issues.append(
                Issue("UNMAPPED_REQUIRED", ERROR, "required field has no source column; every record is held", f.name)
            )
            for i in range(len(result.records)):
                result.issues.append(
                    Issue("FIELD_UNMAPPED", ERROR, "required field has no source column", f.name, "", result.rows[i], i)
                )


def _unique(result: Result, schema: Schema, active: dict[str, FieldMapping]) -> None:
    for f in schema.fields:
        if not f.unique or f.name not in active:
            continue
        seen: dict[str, list[int]] = {}
        for i, rec in enumerate(result.records):
            key = rec[f.name].lower()
            if key:
                seen.setdefault(key, []).append(i)
        for idxs in seen.values():
            if len(idxs) < 2:
                continue
            lines = ", ".join(str(result.rows[j]) for j in idxs)
            for j in idxs:
                result.issues.append(
                    Issue(
                        "DUPLICATE_KEY",
                        ERROR,
                        f"{f.name} appears on {len(idxs)} rows (lines {lines})",
                        f.name,
                        active[f.name].source or "",
                        result.rows[j],
                        j,
                        result.records[j][f.name],
                        "keep one row (often the latest hire record) or give each a distinct value",
                    )
                )


def _refs(result: Result, schema: Schema, active: dict[str, FieldMapping]) -> None:
    for f in schema.fields:
        if f.type != "ref" or f.name not in active:
            continue
        key_field = f.ref
        if key_field not in active:
            for rec in result.records:
                rec[f.name] = ""
            result.issues.append(
                Issue(
                    "REF_KEY_UNMAPPED",
                    WARNING,
                    f"{f.name} cannot be resolved: {key_field} has no source column",
                    f.name,
                    active[f.name].source or "",
                    suggestion=f"map {key_field} first; the column is kept in the mapping",
                )
            )
            continue
        by_id: dict[str, str] = {}
        by_email: dict[str, str] = {}
        by_name: dict[str, list[str]] = {}
        for rec in result.records:
            key = rec.get(key_field, "")
            if not key:
                continue
            by_id.setdefault(key.lower(), key)
            if rec.get("work_email"):
                by_email.setdefault(rec["work_email"].lower(), key)
            full = norm_text(f"{rec.get('first_name', '')} {rec.get('last_name', '')}")
            if full:
                by_name.setdefault(full, []).append(key)
                rev = norm_text(f"{rec.get('last_name', '')} {rec.get('first_name', '')}")
                if rev != full:
                    by_name.setdefault(rev, []).append(key)
        names_index = {k: sorted(set(v)) for k, v in by_name.items()}
        how: dict[str, int] = {"id": 0, "email": 0, "name": 0}
        for i, rec in enumerate(result.records):
            raw = rec[f.name]
            if not raw:
                continue
            target, kind = None, ""
            if raw.lower() in by_id:
                target, kind = by_id[raw.lower()], "id"
            elif raw.endswith(".0") and raw[:-2].lower() in by_id:
                target, kind = by_id[raw[:-2].lower()], "id"
            elif (email := parsers.parse_email(raw)) and email in by_email:
                target, kind = by_email[email], "email"
            else:
                key = norm_text(raw.replace(",", " ") if "," not in raw else " ".join(reversed(raw.split(",", 1))))
                matches = names_index.get(key, [])
                if len(matches) == 1:
                    target, kind = matches[0], "name"
                elif len(matches) > 1:
                    rec[f.name] = ""
                    result.issues.append(
                        Issue(
                            "AMBIGUOUS_REF",
                            WARNING,
                            f"{len(matches)} employees share this name",
                            f.name,
                            active[f.name].source or "",
                            result.rows[i],
                            i,
                            raw,
                            "candidates: " + ", ".join(matches[:5]) + (" …" if len(matches) > 5 else ""),
                        )
                    )
                    continue
            if target is None:
                rec[f.name] = ""
                result.issues.append(
                    Issue(
                        "UNRESOLVED_REF",
                        WARNING,
                        f"no employee in this file matches this {f.name.replace('_id', '')}",
                        f.name,
                        active[f.name].source or "",
                        result.rows[i],
                        i,
                        raw,
                        "the manager may be missing from the export, or have left",
                    )
                )
                continue
            how[kind] += 1
            rec[f.name] = target
            if target == rec.get(key_field):
                result.issues.append(
                    Issue(
                        "SELF_REF",
                        WARNING,
                        "employee is listed as their own manager",
                        f.name,
                        active[f.name].source or "",
                        result.rows[i],
                        i,
                        raw,
                    )
                )
        _cycles(result, f.name, key_field, active[f.name].source or "")
        result.ref_resolution[f.name] = how


def _cycles(result: Result, ref_field: str, key_field: str, source: str) -> None:
    parent = {rec[key_field]: rec[ref_field] for rec in result.records if rec.get(key_field) and rec.get(ref_field)}
    index = {rec[key_field]: i for i, rec in enumerate(result.records) if rec.get(key_field)}
    reported: set[str] = set()
    for start in sorted(parent):
        path: list[str] = []
        node = start
        while node in parent and node not in path:
            path.append(node)
            node = parent[node]
        if node not in path:
            continue
        loop = path[path.index(node) :]
        if len(loop) < 2 or reported & set(loop):  # self-references are reported as SELF_REF
            continue
        reported.update(loop)
        chain = " → ".join(loop + [loop[0]])
        for n in loop:
            i = index[n]
            result.issues.append(
                Issue(
                    "REPORTING_CYCLE",
                    WARNING,
                    f"reporting line loops: {chain}",
                    ref_field,
                    source,
                    result.rows[i],
                    i,
                    n,
                )
            )


def _rules(result: Result, schema: Schema) -> None:
    for rule in schema.rules:
        kind, sev, msg = rule["type"], rule.get("severity", WARNING), rule.get("message", rule["id"])
        for i, rec in enumerate(result.records):
            hit = False
            if kind in ("date_order", "min_gap_years"):
                a, b = rec.get(rule["earlier"], ""), rec.get(rule["later"], "")
                if a and b:
                    da, db = date.fromisoformat(a), date.fromisoformat(b)
                    hit = db < da if kind == "date_order" else db < _add_years(da, rule["years"])
                field_name = rule["later"]
            elif kind == "required_when":
                cond = rule["when"]
                hit = all(rec.get(k) == v for k, v in cond.items()) and not rec.get(rule["field"])
                field_name = rule["field"]
            elif kind == "required_with":
                hit = bool(rec.get(rule["with"])) and not rec.get(rule["field"])
                field_name = rule["field"]
            if hit:
                result.issues.append(Issue(rule["id"], sev, msg, field_name, "", result.rows[i], i))


def _add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        return d.replace(year=d.year + years, day=28)
