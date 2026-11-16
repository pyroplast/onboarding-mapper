"""The synthetic exports, scored against their ground truth. These double as regression tests."""

import pytest
from conftest import AS_OF, table_of

from onboarding_mapper import engine, report, synth

FIXTURES = [g() for g in synth.ALL + synth.HOLDOUT]
IDS = [f.name for f in FIXTURES]


@pytest.fixture(scope="module", params=FIXTURES, ids=IDS)
def case(request, employee):
    fx = request.param
    run = engine.run(fx.filename, employee, as_of=AS_OF, table=table_of(fx), default_country=fx.default_country)
    return fx, run


def test_every_field_maps_to_the_true_source(case):
    fx, run = case
    for name, want in fx.mapping.items():
        m = run.mappings[name]
        got = m.source if m.active else None
        assert got == want, f"{name}: expected {want!r}, got {got!r} ({m.status} {m.confidence:.2f})"
        if want is not None:
            assert m.method == fx.methods.get(name, "direct"), name


def test_auto_applied_mappings_are_never_wrong(case):
    fx, run = case
    for m in run.mappings.values():
        if m.status == "auto":
            assert m.source == fx.mapping[m.field], m.field


def test_expected_review_mappings(case):
    fx, run = case
    assert sorted(m.field for m in run.mappings.values() if m.status == "review") == sorted(fx.expected_review_mappings)


def test_every_cell_matches_the_truth(case):
    fx, run = case
    assert len(run.result.records) == len(fx.records)
    for i, (got, want) in enumerate(zip(run.result.records, fx.records)):
        for name, source in fx.mapping.items():
            if source is not None:
                assert got[name] == want[name], f"record {i} {name}: {got[name]!r} != {want[name]!r}"


def test_planted_problems_are_found(case):
    fx, run = case
    found = {(i.record, i.field, i.code) for i in run.result.issues}
    for e in fx.expected_issues:
        assert (e["record"], e["field"], e["code"]) in found, e


def test_sensitive_columns_held_back(case):
    fx, run = case
    assert sorted(s["source"] for s in run.proposal.sensitive) == sorted(fx.held_back)


def test_held_back_values_appear_in_no_output(case, tmp_path):
    fx, run = case
    paths = report.write_all(run, tmp_path, show_values=True)
    blob = "".join(p.read_text(encoding="utf-8") for p in paths.values())
    for h in fx.held_back:
        col = run.table.headers.index(h)
        for value in {r[col] for r in run.table.rows if len(r[col]) >= 6}:
            assert value not in blob, f"{h} value leaked"


def test_masked_report_carries_no_emails(case, tmp_path):
    fx, run = case
    paths = report.write_all(run, tmp_path)
    html = paths["report.html"].read_text(encoding="utf-8")
    for rec in fx.records[:20]:
        if rec["work_email"]:
            assert rec["work_email"] not in html


def test_runs_are_byte_identical(case, employee, tmp_path):
    fx, run = case
    again = engine.run(fx.filename, employee, as_of=AS_OF, table=table_of(fx), default_country=fx.default_country)
    a = report.write_all(run, tmp_path / "a")
    b = report.write_all(again, tmp_path / "b")
    for name in a:
        assert a[name].read_bytes() == b[name].read_bytes(), name


def test_generators_are_deterministic():
    for g in synth.ALL + synth.HOLDOUT:
        assert g().data == g().data
