from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from onboarding_mapper import engine, ingest, schema, synth  # noqa: E402

AS_OF = date(2026, 11, 16)


@pytest.fixture(scope="session")
def employee():
    return schema.load("employee")


def table_of(fx: synth.Fixture) -> ingest.Table:
    if fx.filename.endswith(".xlsx"):
        return ingest.read_xlsx_bytes(fx.data, fx.filename)
    return ingest.read_csv_bytes(fx.data, fx.filename)


def run_csv(text: str, employee, **kw) -> engine.Run:
    t = ingest.read_csv_bytes(text.encode("utf-8"), "inline.csv")
    return engine.run("inline.csv", employee, as_of=AS_OF, table=t, **kw)
