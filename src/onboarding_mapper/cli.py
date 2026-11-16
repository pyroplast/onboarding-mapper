"""Command line.

    omap map EXPORT [--schema employee] [--out out/]       propose a mapping, apply it, write everything
    omap apply EXPORT --mapping out/mapping.json [--out]   re-run with a reviewed mapping
    omap profile EXPORT                                     show what each column looks like
    omap schema [NAME|FILE]                                 list or show target schemas

Exit codes: 0 every record is ready, 1 some records are held or need review
(with --strict), 2 the run could not complete.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date

from . import __version__, engine, ingest, profile, report, schema


def _as_of(text: str | None) -> date:
    if not text:
        return date.today()
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--as-of must be YYYY-MM-DD, got {text!r}") from exc


def _color(stream) -> bool:
    return stream.isatty() and os.environ.get("NO_COLOR") is None


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("export", help="CSV, TXT or XLSX file from the customer's system")
    p.add_argument(
        "--schema", default="employee", help="built-in schema name or path to a schema JSON (default: employee)"
    )
    p.add_argument("--out", default="out", help="folder for the outputs (default: out)")
    p.add_argument("--as-of", help="reference date for age and future-date checks (default: today)")
    p.add_argument("--default-country", help="ISO country code for phone numbers without a country (e.g. ES)")
    p.add_argument(
        "--show-values", action="store_true", help="do not mask personal values in report.html and report.md"
    )
    p.add_argument("--strict", action="store_true", help="exit 1 when anything is held or waiting for review")
    p.add_argument("--quiet", action="store_true", help="print only the output folder")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="omap",
        description="Map a customer's messy HR export to a target schema, with confidence scores, a validation report and a review queue.",
    )
    parser.add_argument("--version", action="version", version=f"onboarding-mapper {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_map = sub.add_parser("map", help="propose a mapping, apply it and write all outputs")
    _common(p_map)
    p_apply = sub.add_parser("apply", help="apply a reviewed mapping file")
    _common(p_apply)
    p_apply.add_argument("--mapping", required=True, help="mapping.json from an earlier run, edited by a reviewer")

    p_prof = sub.add_parser("profile", help="show what each column looks like")
    p_prof.add_argument("export")
    p_prof.add_argument("--schema", default="employee")
    p_prof.add_argument("--json", action="store_true")

    p_schema = sub.add_parser("schema", help="list built-in schemas or print one")
    p_schema.add_argument("name", nargs="?")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command in ("map", "apply"):
            return _run(args)
        if args.command == "profile":
            return _profile(args)
        if args.command == "schema":
            return _schema(args)
    except (ingest.IngestError, schema.SchemaError, engine.MappingError, argparse.ArgumentTypeError) as exc:
        print(f"omap: {exc}", file=sys.stderr)
        return 2
    return 2


def _run(args: argparse.Namespace) -> int:
    sch = schema.load(args.schema)
    country = args.default_country.upper() if args.default_country else None
    run = engine.run(
        args.export, sch, mapping_path=getattr(args, "mapping", None), as_of=_as_of(args.as_of), default_country=country
    )
    paths = report.write_all(run, args.out, show_values=args.show_values)
    if args.quiet:
        print(args.out)
    else:
        print(report.to_text(run, color=_color(sys.stdout)))
        print(f"\nwrote {', '.join(sorted(paths))} to {args.out}/")
    s = run.stats()
    if s["records_held"]:
        return 1
    if args.strict and (s["review_items"] or s["fields_review"]):
        return 1
    return 0


def _profile(args: argparse.Namespace) -> int:
    sch = schema.load(args.schema)
    table = ingest.read(args.export)
    profiles = profile.profile_table(table, {f.name: f.enum_map for f in sch.fields if f.type == "enum"})
    if args.json:
        print(json.dumps([p.to_dict() for p in profiles], indent=2, ensure_ascii=False))
        return 0
    print(
        f"{table.source}: {len(table.rows)} rows, {len(table.headers)} columns, {table.encoding}, header on row {table.header_row}"
    )
    for note in table.notes:
        print(f"  note: {note}")
    for p in profiles:
        top = sorted(
            ((v, k) for k, v in p.fit.items() if v >= 0.5 and not k.startswith(("name_multi", "date_any"))),
            reverse=True,
        )[:3]
        kinds = ", ".join(f"{k} {v:.0%}" for v, k in top) or "text"
        flag = f"  [held back: {p.sensitive}]" if p.sensitive else ""
        print(f"  {p.header!r:32} filled {p.fill_rate:5.0%}  distinct {p.distinct:5}  {kinds}{flag}")
    return 0


def _schema(args: argparse.Namespace) -> int:
    if not args.name:
        for name in schema.builtin_names():
            s = schema.load(name)
            print(f"{name}: {len(s.fields)} fields. {s.description}")
        return 0
    s = schema.load(args.name)
    print(f"{s.name} v{s.version}: {s.description}")
    for f in s.fields:
        flags = " ".join(x for x in ("required" if f.required else "", "unique" if f.unique else "") if x)
        values = f" ({', '.join(f.values)})" if f.values else ""
        print(f"  {f.name:18} {f.type:9} {flags:16} {f.description}{values}")
    for r in s.rules:
        print(f"  rule {r['id']}: {r.get('message', '')} [{r.get('severity', 'warning')}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
