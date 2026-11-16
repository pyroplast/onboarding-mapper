"""Score the mapper against the synthetic fixtures' ground truth.

Run from the repo root:  python bench/evaluate.py [--json]

Metrics, per fixture and overall:
  mapping       fields whose proposed source (and method) matches the truth, out of all schema fields;
                a field with no source in the truth counts as correct when the mapper leaves it unmapped
  auto_precision  of the mappings applied without review, the share that are correct
  cells         output cells equal to the truth value, over every field the truth maps
  issues        expected problems (record, field, code) that show up as an issue, out of all expected
  held_ok       records held that the truth says should be held, out of all held
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from onboarding_mapper import engine, ingest, schema, synth  # noqa: E402


def evaluate(fx: synth.Fixture, sch: schema.Schema) -> dict:
    table = (
        ingest.read_xlsx_bytes(fx.data, fx.filename)
        if fx.filename.endswith(".xlsx")
        else ingest.read_csv_bytes(fx.data, fx.filename)
    )
    t0 = time.perf_counter()
    run = engine.run(fx.filename, sch, as_of=synth.AS_OF, table=table, default_country=fx.default_country)
    elapsed = (time.perf_counter() - t0) * 1000

    right, wrong = 0, []
    auto_total = auto_right = 0
    for name in sch.field_names:
        m = run.mappings[name]
        want = fx.mapping.get(name)
        want_method = fx.methods.get(name, "direct")
        got = m.source if m.active else None
        ok = got == want and (want is None or m.method == want_method)
        right += ok
        if not ok:
            wrong.append(
                {"field": name, "expected": want, "got": got, "status": m.status, "confidence": round(m.confidence, 3)}
            )
        if m.status == "auto":
            auto_total += 1
            auto_right += ok

    cells = cells_ok = 0
    cell_misses = []
    if len(run.result.records) != len(fx.records):
        raise AssertionError(f"{fx.name}: {len(run.result.records)} records, truth has {len(fx.records)}")
    for i, (got, want) in enumerate(zip(run.result.records, fx.records)):
        for name, source in fx.mapping.items():
            if source is None:
                continue
            cells += 1
            if got[name] == want[name]:
                cells_ok += 1
            elif len(cell_misses) < 12:
                cell_misses.append({"record": i, "field": name, "expected": want[name], "got": got[name]})

    found = {(x.record, x.field, x.code) for x in run.result.issues}
    exp = [(e["record"], e["field"], e["code"]) for e in fx.expected_issues]
    issues_hit = sum(1 for e in exp if e in found)
    should_hold = {
        e[0]
        for e in exp
        if e[2]
        in {
            "INVALID_EMAIL",
            "DUPLICATE_KEY",
            "END_BEFORE_START",
            "DATE_OUT_OF_RANGE",
            "FIELD_UNMAPPED",
            "MISSING_REQUIRED",
        }
    }
    held = set(run.result.held)
    held_ok = len(held & should_hold)

    flagged_review = {m.field for m in run.mappings.values() if m.status == "review"}
    s = run.stats()
    return {
        "fixture": fx.name,
        "rows": len(fx.records),
        "columns": len(table.headers),
        "mapping_correct": right,
        "mapping_total": len(sch.field_names),
        "auto_correct": auto_right,
        "auto_total": auto_total,
        "cells_correct": cells_ok,
        "cells_total": cells,
        "issues_found": issues_hit,
        "issues_expected": len(exp),
        "held": len(held),
        "held_expected": len(should_hold),
        "held_correct": held_ok,
        "held_back": sorted(h["source"] for h in run.proposal.sensitive),
        "held_back_expected": sorted(fx.held_back),
        "review_fields": sorted(flagged_review),
        "review_fields_expected": sorted(fx.expected_review_mappings),
        "review_items": s["review_items"],
        "ms": round(elapsed, 1),
        "mapping_errors": wrong,
        "cell_misses": cell_misses,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    sch = schema.load("employee")
    results = [evaluate(fx, sch) for fx in synth.generate_all()]
    results += [dict(evaluate(g(), sch), holdout=True) for g in synth.HOLDOUT]
    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
        return 0
    for r in results:
        print(
            f"{r['fixture'] + (' (held out)' if r.get('holdout') else ''):29} mapping {r['mapping_correct']}/{r['mapping_total']}  auto {r['auto_correct']}/{r['auto_total']}  "
            f"cells {r['cells_correct']}/{r['cells_total']}  issues {r['issues_found']}/{r['issues_expected']}  "
            f"held {r['held_correct']}/{r['held']} (expected {r['held_expected']})  held back {r['held_back']}  review {r['review_items']}  {r['ms']} ms"
        )
        for w in r["mapping_errors"]:
            print("   map miss:", w)
        for c in r["cell_misses"]:
            print("   cell miss:", c)
    return 0


if __name__ == "__main__":
    sys.exit(main())
