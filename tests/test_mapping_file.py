import json

import pytest
from conftest import AS_OF, table_of

from onboarding_mapper import engine, report, synth


@pytest.fixture
def bamboo(employee, tmp_path):
    fx = synth.bamboo_us()
    table = table_of(fx)
    run = engine.run(fx.filename, employee, as_of=AS_OF, table=table)
    paths = report.write_all(run, tmp_path)
    return fx, table, json.loads(paths["mapping.json"].read_text()), tmp_path


def _write(tmp_path, data):
    path = tmp_path / "edited.json"
    path.write_text(json.dumps(data))
    return path


def _field(data, name):
    return next(f for f in data["fields"] if f["field"] == name)


def test_round_trip_gives_the_same_records(bamboo, employee):
    fx, table, data, tmp = bamboo
    first = engine.run(fx.filename, employee, as_of=AS_OF, table=table)
    second = engine.run(fx.filename, employee, mapping_path=_write(tmp, data), as_of=AS_OF, table=table)
    assert first.result.records == second.result.records
    assert second.mapping_source.endswith("edited.json")


def test_accepting_a_review_mapping_clears_it_from_the_queue(bamboo, employee):
    fx, table, data, tmp = bamboo
    _field(data, "employment_type")["status"] = "accepted"
    run = engine.run(fx.filename, employee, mapping_path=_write(tmp, data), as_of=AS_OF, table=table)
    assert not any(r.code == "CONFIRM_MAPPING" for r in run.review)


def test_rejecting_a_mapping_empties_the_field(bamboo, employee):
    fx, table, data, tmp = bamboo
    _field(data, "job_title")["status"] = "rejected"
    run = engine.run(fx.filename, employee, mapping_path=_write(tmp, data), as_of=AS_OF, table=table)
    assert all(r["job_title"] == "" for r in run.result.records)


def test_changing_date_order_changes_the_output(bamboo, employee):
    fx, table, data, tmp = bamboo
    _field(data, "start_date")["options"]["date_order"] = "dmy"
    run = engine.run(fx.filename, employee, mapping_path=_write(tmp, data), as_of=AS_OF, table=table)
    col = table.headers.index("Hire Date")
    i = next(
        i
        for i, r in enumerate(table.rows)
        if int(r[col][:2]) <= 12 and int(r[col][3:5]) <= 12 and r[col][:2] != r[col][3:5]
    )
    assert run.result.records[i]["start_date"] != fx.records[i]["start_date"]
    # A date only one order can read is left alone either way.
    j = next(j for j, r in enumerate(table.rows) if int(r[col][3:5]) > 12)
    assert run.result.records[j]["start_date"] == fx.records[j]["start_date"]


def test_pointing_a_field_at_another_column(bamboo, employee):
    fx, table, data, tmp = bamboo
    f = _field(data, "location")
    f.update({"source": "Department", "status": "accepted"})
    run = engine.run(fx.filename, employee, mapping_path=_write(tmp, data), as_of=AS_OF, table=table)
    assert run.result.records[0]["location"] == fx.records[0]["department"]


def test_bad_mapping_files_are_refused(bamboo, employee):
    fx, table, data, tmp = bamboo
    cases = []
    d = json.loads(json.dumps(data))
    _field(d, "job_title").update({"source": "Not A Column", "status": "accepted"})
    cases.append(d)
    d = json.loads(json.dumps(data))
    _field(d, "job_title").update({"source": "SSN", "status": "accepted"})
    cases.append(d)
    d = json.loads(json.dumps(data))
    d["schema"]["name"] = "payroll"
    cases.append(d)
    d = json.loads(json.dumps(data))
    _field(d, "job_title")["status"] = "maybe"
    cases.append(d)
    for bad in cases:
        with pytest.raises(engine.MappingError):
            engine.run(fx.filename, employee, mapping_path=_write(tmp, bad), as_of=AS_OF, table=table)
