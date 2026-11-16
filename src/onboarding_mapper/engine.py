"""One run end to end: read, profile, propose (or load) a mapping, apply, build the review queue."""

from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import __version__, apply, ingest, matcher, profile
from .apply import ERROR, INFO, WARNING, Issue, Result
from .ingest import Table
from .matcher import FieldMapping, Proposal
from .normalize import clean_cell, is_null
from .schema import Schema

SEVERITY_ORDER = {ERROR: 0, WARNING: 1, INFO: 2}


class MappingError(ValueError):
    pass


@dataclass
class ReviewItem:
    id: str
    kind: str  # mapping | value | record | privacy
    severity: str
    field: str
    source: str
    row: int | None
    value: str
    problem: str
    suggestion: str
    code: str

    @property
    def key(self) -> str:
        """Stays the same across re-runs, unlike the R-number, so decisions survive mapping edits."""
        return f"{self.code}|{self.field}|{self.source}|{self.row if self.row is not None else ''}"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "key": self.key,
            "kind": self.kind,
            "severity": self.severity,
            "code": self.code,
            "field": self.field,
            "source": self.source,
            "row": self.row if self.row is not None else "",
            "value": self.value,
            "problem": self.problem,
            "suggestion": self.suggestion,
        }


@dataclass
class Run:
    table: Table
    schema: Schema
    profiles: list[profile.ColumnProfile]
    proposal: Proposal
    result: Result
    review: list[ReviewItem]
    as_of: date
    mapping_source: str  # "proposed" or the path of a mapping file
    timings_ms: dict[str, float] = field(default_factory=dict)

    @property
    def mappings(self) -> dict[str, FieldMapping]:
        return self.proposal.mappings

    def stats(self) -> dict:
        maps = list(self.mappings.values())
        status = Counter(m.status for m in maps)
        ready, held = self.result.ready, self.result.held
        changed = cells = 0
        for name, m in self.mappings.items():
            if not m.active or m.column is None:
                continue
            for i, rec in enumerate(self.result.records):
                raw = self.table.rows[i][m.column]
                if is_null(raw):
                    continue
                cells += 1
                if rec[name] != clean_cell(raw):
                    changed += 1
        by_code = Counter(i.code for i in self.result.issues if i.code != "FIELD_UNMAPPED")
        return {
            "rows_in": len(self.table.rows),
            "records_ready": len(ready),
            "records_held": len(held),
            "columns_in": len(self.table.headers),
            "columns_used": len({m.column for m in maps if m.active and m.column is not None}),
            "columns_held_back": len(self.proposal.sensitive),
            "columns_unused": len(self.proposal.unused),
            "fields": len(maps),
            "fields_auto": status.get("auto", 0) + status.get("accepted", 0),
            "fields_review": status.get("review", 0),
            "fields_unmapped": status.get("unmapped", 0) + status.get("rejected", 0),
            "cells_mapped": cells,
            "cells_normalised": changed,
            "issues": {k: by_code[k] for k in sorted(by_code)},
            "review_items": len(self.review),
            "review_by_kind": dict(sorted(Counter(r.kind for r in self.review).items())),
        }


# ---------------------------------------------------------------------------
# Mapping file
# ---------------------------------------------------------------------------


def mapping_to_dict(run: Run) -> dict:
    return {
        "tool": "onboarding-mapper",
        "version": __version__,
        "schema": {"name": run.schema.name, "version": run.schema.version, "sha256": run.schema.sha256},
        "source": {"fingerprint": run.table.fingerprint, "headers": run.table.headers},
        "how_to_edit": (
            "Set a field's status to 'accepted' or 'rejected', or change its 'source' to another header and set "
            "status 'accepted'. Options such as date_order ('dmy'/'mdy') and number_style ('eu'/'us') can be "
            "changed too. Then run: omap apply <export> --mapping <this file>."
        ),
        "fields": [m.to_dict() for m in run.mappings.values()],
        "held_back": run.proposal.sensitive,
        "unused": run.proposal.unused,
    }


def load_mapping(path: str | Path, table: Table, schema: Schema) -> Proposal:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MappingError(f"cannot read mapping file: {exc}") from exc
    return mapping_from_dict(raw, table, schema)


def mapping_from_dict(raw: dict, table: Table, schema: Schema) -> Proposal:
    """Build a proposal from a mapping document (the shape mapping.json has)."""
    if raw.get("schema", {}).get("name") != schema.name:
        raise MappingError(f"mapping was made for schema '{raw.get('schema', {}).get('name')}', not '{schema.name}'")
    headers = {h: i for i, h in enumerate(table.headers)}
    mappings: dict[str, FieldMapping] = {f.name: FieldMapping(f.name) for f in schema.fields}
    for entry in raw.get("fields", []):
        name = entry.get("field")
        if name not in mappings:
            raise MappingError(f"mapping names unknown field '{name}'")
        status = entry.get("status", "unmapped")
        if status not in {"auto", "review", "unmapped", "accepted", "rejected"}:
            raise MappingError(f"field '{name}': unknown status '{status}'")
        source = entry.get("source")
        if source is not None and source not in headers and status in {"auto", "review", "accepted"}:
            raise MappingError(f"field '{name}': column '{source}' is not in this export")
        method = entry.get("method", "direct")
        if method not in {"direct", "split_full_name", "currency_from_amount"}:
            raise MappingError(f"field '{name}': unknown method '{method}'")
        mappings[name] = FieldMapping(
            field=name,
            source=source,
            column=headers.get(source) if source is not None else None,
            confidence=float(entry.get("confidence", 0)),
            status=status,
            method=method,
            options=dict(entry.get("options", {})),
            reasons=list(entry.get("reasons", [])),
            alternatives=list(entry.get("alternatives", [])),
        )
    sensitive_names = {s["source"] for s in raw.get("held_back", [])}
    for m in mappings.values():
        if m.active and m.source in sensitive_names:
            raise MappingError(
                f"field '{m.field}' maps a held-back sensitive column '{m.source}'; remove it from held_back first"
            )
    return Proposal(mappings, list(raw.get("unused", [])), list(raw.get("held_back", [])))


# ---------------------------------------------------------------------------
# Review queue
# ---------------------------------------------------------------------------

GROUP_AT = 5  # more identical issues than this become one queue item
GROUPABLE = {"SALARY_WITHOUT_CURRENCY", "TERMINATED_WITHOUT_END_DATE", "UNRESOLVED_REF", "PHONE_NO_COUNTRY",
             "UNKNOWN_ENUM", "UNKNOWN_COUNTRY", "INVALID_PHONE", "NAME_SPLIT_UNCERTAIN", "MISSING_REQUIRED"}  # fmt: skip

_CODE_KIND = {
    "DUPLICATE_KEY": "record",
    "MISSING_REQUIRED": "record",
    "END_BEFORE_START": "record",
    "HIRED_UNDER_14": "record",
    "TERMINATED_WITHOUT_END_DATE": "record",
    "SALARY_WITHOUT_CURRENCY": "record",
    "REPORTING_CYCLE": "record",
    "SELF_REF": "record",
}


def build_review(
    run_mappings: dict[str, FieldMapping],
    proposal: Proposal,
    profiles: list[profile.ColumnProfile],
    schema: Schema,
    result: Result,
) -> list[ReviewItem]:
    items: list[tuple] = []
    prof_by_header = {p.header: p for p in profiles}

    for m in run_mappings.values():
        f = schema.get(m.field)
        if m.status == "review":
            alt = "; ".join(f"{a['source']} ({a['confidence']:.2f})" for a in m.alternatives) or "none"
            items.append(
                (
                    "mapping",
                    WARNING,
                    "CONFIRM_MAPPING",
                    m.field,
                    m.source or "",
                    None,
                    "",
                    f"proposed with confidence {m.confidence:.2f}: " + "; ".join(m.reasons),
                    f"accept, or pick another column. Other candidates: {alt}",
                )
            )
        elif not m.active and f.required:
            alt = "; ".join(f"{a['source']} ({a['confidence']:.2f})" for a in m.alternatives) or "none"
            items.append(
                (
                    "mapping",
                    ERROR,
                    "UNMAPPED_REQUIRED",
                    m.field,
                    "",
                    None,
                    "",
                    "required field has no source column; every record is held",
                    f"pick a source column or ask the customer for this data. Closest candidates: {alt}",
                )
            )
        if m.active and f.type == "date" and m.method == "direct":
            p = prof_by_header.get(m.source or "")
            if p is not None:
                dmy, mdy, either = p.date_order_evidence
                if either and not dmy and not mdy and not p.hint:
                    order = "day/month" if m.options.get("date_order", "dmy") == "dmy" else "month/day"
                    items.append(
                        (
                            "mapping",
                            WARNING,
                            "DATE_ORDER_ASSUMED",
                            m.field,
                            m.source or "",
                            None,
                            "",
                            f"{either} dates read either way (e.g. 03/04) and none settles it; read as {order}",
                            "confirm with the customer and set options.date_order in the mapping file",
                        )
                    )
                elif dmy and mdy:
                    items.append(
                        (
                            "mapping",
                            ERROR,
                            "DATE_ORDER_MIXED",
                            m.field,
                            m.source or "",
                            None,
                            "",
                            f"{dmy} values are only valid day-first and {mdy} only month-first: the column mixes formats",
                            "ask for a re-export with ISO dates (YYYY-MM-DD)",
                        )
                    )
        if m.active and f.type in ("decimal", "ratio") and m.method == "direct":
            p = prof_by_header.get(m.source or "")
            if p is not None:
                eu, us, either = p.number_style_evidence
                if either and not eu and not us:
                    style = (
                        "1.234 = one thousand two hundred thirty-four"
                        if m.options.get("number_style") == "eu"
                        else "1.234 = one point two three four"
                    )
                    items.append(
                        (
                            "mapping",
                            WARNING,
                            "NUMBER_STYLE_ASSUMED",
                            m.field,
                            m.source or "",
                            None,
                            "",
                            f"{either} values read either way and none settles the decimal separator; assumed {style}",
                            "confirm and set options.number_style in the mapping file",
                        )
                    )
    for s in proposal.sensitive:
        items.append(
            (
                "privacy",
                WARNING,
                "SENSITIVE_HELD_BACK",
                "",
                s["source"],
                None,
                "",
                f"looks like {s['label']} ({s['reason']}); not read into any output",
                "confirm it should not be imported, and agree how it is transferred if it must be",
            )
        )

    # Cell and record issues. When the same problem hits many rows, one item
    # with the row list is easier to act on than dozens of identical lines.
    groups: dict[tuple, list[Issue]] = {}
    for issue in result.issues:
        if issue.severity == INFO or issue.code in ("FIELD_UNMAPPED", "UNMAPPED_REQUIRED"):
            continue
        key = (issue.code, issue.field, issue.message) if issue.code in GROUPABLE else (id(issue),)
        groups.setdefault(key, []).append(issue)
    for group in groups.values():
        first = group[0]
        kind = "mapping" if first.record is None else _CODE_KIND.get(first.code, "value")
        if len(group) > GROUP_AT:
            rows = [str(i.row) for i in group if i.row is not None]
            listed = ", ".join(rows[:12]) + (f" and {len(rows) - 12} more" if len(rows) > 12 else "")
            items.append(
                (
                    kind,
                    first.severity,
                    first.code,
                    first.field,
                    first.source,
                    None,
                    "",
                    f"{len(group)} rows: {first.message} (rows {listed})",
                    first.suggestion,
                )
            )
        else:
            for issue in group:
                items.append(
                    (
                        kind,
                        issue.severity,
                        issue.code,
                        issue.field,
                        issue.source,
                        issue.row,
                        issue.value,
                        issue.message,
                        issue.suggestion,
                    )
                )

    kind_order = {"mapping": 0, "privacy": 1, "record": 2, "value": 3}
    items.sort(key=lambda t: (SEVERITY_ORDER[t[1]], kind_order[t[0]], t[5] if t[5] is not None else -1, t[3], t[2]))
    return [ReviewItem(f"R{n:04d}", *t[:2], t[3], t[4], t[5], t[6], t[7], t[8], t[2]) for n, t in enumerate(items, 1)]


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def run(
    path: str | Path,
    schema: Schema,
    mapping_path: str | Path | None = None,
    as_of: date | None = None,
    default_country: str | None = None,
    table: Table | None = None,
    proposal: Proposal | None = None,
) -> Run:
    as_of = as_of or date.today()
    t0 = time.perf_counter()
    table = table or ingest.read(path)
    t1 = time.perf_counter()
    enum_maps = {f.name: f.enum_map for f in schema.fields if f.type == "enum"}
    profiles = profile.profile_table(table, enum_maps)
    if proposal is not None:
        source = "edited"
    elif mapping_path:
        proposal = load_mapping(mapping_path, table, schema)
        source = str(mapping_path)
    else:
        proposal = matcher.propose(table, profiles, schema, as_of)
        source = "proposed"
    t2 = time.perf_counter()
    result = apply.run(table, schema, proposal.mappings, as_of, default_country)
    review = build_review(proposal.mappings, proposal, profiles, schema, result)
    t3 = time.perf_counter()
    return Run(
        table,
        schema,
        profiles,
        proposal,
        result,
        review,
        as_of,
        source,
        {"read": (t1 - t0) * 1000, "profile_and_match": (t2 - t1) * 1000, "apply_and_validate": (t3 - t2) * 1000},
    )


__all__ = ["Run", "ReviewItem", "MappingError", "run", "load_mapping", "mapping_from_dict", "mapping_to_dict", "Issue"]
