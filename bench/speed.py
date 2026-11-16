"""Time a full run on a scaled-up export.

Takes the US fixture (21 columns), repeats its rows with fresh ids and emails up
to each size, and times read → profile and match → apply and validate. Each
size runs three times; the table shows the median.

    python bench/speed.py [--sizes 1000,10000,50000,100000] [--json]
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import platform
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from onboarding_mapper import engine, ingest, schema, synth  # noqa: E402


def scaled_export(n: int) -> bytes:
    lines = synth.bamboo_us().data.decode("utf-8").splitlines()
    header, body = lines[0], [next(csv.reader([ln])) for ln in lines[1:]]
    buf = io.StringIO()
    buf.write(header + "\r\n")
    writer = csv.writer(buf, lineterminator="\r\n")
    for k in range(n):
        row = list(body[k % len(body)])
        row[0] = f"{k:06d}"
        row[3] = f"u{k}@acme-robotics.example"
        writer.writerow(row)
    return buf.getvalue().encode("utf-8")


def measure(n: int, sch, repeats: int = 3) -> dict:
    data = scaled_export(n)
    runs = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        table = ingest.read_csv_bytes(data, "scaled.csv")
        t1 = time.perf_counter()
        run = engine.run("scaled.csv", sch, as_of=synth.AS_OF, table=table)
        t2 = time.perf_counter()
        runs.append({**run.timings_ms, "ingest": (t1 - t0) * 1000, "total": (t2 - t0) * 1000})
    med = {k: statistics.median(r[k] for r in runs) for k in runs[0]}
    return {
        "rows": n,
        "columns": len(table.headers),
        "mb": round(len(data) / 1_000_000, 1),
        "read_ms": round(med["ingest"]),
        "profile_match_ms": round(med["profile_and_match"]),
        "apply_validate_ms": round(med["apply_and_validate"]),
        "total_ms": round(med["total"]),
        "rows_per_s": round(n / (med["total"] / 1000)),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="1000,10000,50000,100000")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    sch = schema.load("employee")
    sizes = [int(s) for s in args.sizes.split(",")]
    results = [measure(n, sch, repeats=3 if n <= 10000 else 1) for n in sizes]
    env = {
        "python": platform.python_version(),
        "machine": platform.machine(),
        "system": platform.system(),
        "cpus": os.cpu_count(),
    }
    if args.json:
        print(json.dumps({"env": env, "results": results}, indent=2))
        return 0
    print(f"Python {env['python']} · {env['system']} {env['machine']} · {env['cpus']} CPUs · single process")
    print("| Rows | Columns | File | Read | Profile + match | Apply + validate | Total | Rows/s |")
    print("|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in results:
        print(
            f"| {r['rows']:,} | {r['columns']} | {r['mb']} MB | {r['read_ms']:,} ms | {r['profile_match_ms']:,} ms | "
            f"{r['apply_validate_ms']:,} ms | {r['total_ms']:,} ms | {r['rows_per_s']:,} |"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
