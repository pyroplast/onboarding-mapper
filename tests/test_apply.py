from conftest import run_csv


def codes(run, field=None):
    return sorted({i.code for i in run.result.issues if field is None or i.field == field})


BASE = (
    "Employee ID,First Name,Last Name,Work Email,Start Date,Date of Birth,Manager,Status,End Date,Salary,Currency,FTE\n"
)


def row(i, mgr="", start="2020-01-15", dob="1985-06-01", status="active", end="", salary="50000", email=None, fte="1"):
    return f'{i},First{chr(64 + i)},Last{chr(64 + i)},{email or f"u{i}@acme.example"},{start},{dob},{mgr},{status},{end},"{salary}",EUR,{fte}\n'


def test_clean_file_is_all_ready(employee):
    run = run_csv(BASE + row(1) + row(2, "1") + row(3, "1") + row(4, "2") + row(5, "2"), employee)
    assert run.result.held == [] and len(run.result.ready) == 5
    assert run.result.records[1]["manager_id"] == "1"
    assert run.result.ref_resolution["manager_id"] == {"id": 4, "email": 0, "name": 0}


def test_duplicate_ids_hold_every_copy(employee):
    run = run_csv(BASE + row(1) + row(2) + row(2, email="other@acme.example"), employee)
    assert run.result.held == [1, 2]
    assert "DUPLICATE_KEY" in codes(run, "employee_id")


def test_date_checks(employee):
    rows = (
        row(1, start="2062-01-01")
        + row(2, dob="2019-01-01")
        + row(3, status="terminated", end="2019-01-01")
        + row(4, status="terminated")
    )
    run = run_csv(BASE + rows, employee)
    by_record: dict[int, set[str]] = {}
    for i in run.result.issues:
        if i.severity != "info":
            by_record.setdefault(i.record, set()).add(i.code)
    assert by_record[0] == {"DATE_OUT_OF_RANGE"}
    assert by_record[1] == {"DATE_OUT_OF_RANGE", "HIRED_UNDER_14"}
    assert by_record[2] == {"END_BEFORE_START"}
    assert by_record[3] == {"TERMINATED_WITHOUT_END_DATE"}
    assert run.result.held == [0, 1, 2]


def test_manager_by_email_and_name(employee):
    text = 'Id,First Name,Last Name,Email,Manager\n1,Anna,Berg,anna@x.example,\n2,Jonas,Müller,jonas@x.example,anna@x.example\n3,Lea,Klein,lea@x.example,"Müller, Jonas"\n4,Tim,Wolf,tim@x.example,Ghost Person\n'
    run = run_csv(text, employee)
    recs = run.result.records
    assert recs[1]["manager_id"] == "1" and recs[2]["manager_id"] == "2" and recs[3]["manager_id"] == ""
    assert run.result.ref_resolution["manager_id"] == {"id": 0, "email": 1, "name": 1}
    assert "UNRESOLVED_REF" in codes(run, "manager_id")


def test_reporting_cycle_and_self_reference(employee):
    run = run_csv(BASE + row(1, "2") + row(2, "3") + row(3, "1") + row(4, "4"), employee)
    found = codes(run, "manager_id")
    assert "REPORTING_CYCLE" in found and "SELF_REF" in found
    cycle = [i for i in run.result.issues if i.code == "REPORTING_CYCLE"]
    assert len(cycle) == 3 and "1 → 2 → 3 → 1" in cycle[0].message


def test_salary_outlier_and_separator_by_magnitude(employee):
    rows = "".join(
        row(i, salary=s)
        for i, s in enumerate(
            ["52.000,00", "48.500,00", "61.000,00", "55.250,00", "50.000,00", "62,000", "4.200,00"], 1
        )
    )
    run = run_csv(BASE + rows, employee)
    recs = run.result.records
    assert recs[5]["salary_amount"] == "62000.00"  # "62,000" read by magnitude, not as 62
    assert recs[6]["salary_amount"] == "4200.00"
    issues = {i.record: i.code for i in run.result.issues if i.field == "salary_amount"}
    assert issues == {5: "SEPARATOR_BY_MAGNITUDE", 6: "NUMBER_OUTLIER"}


def test_fte_percentages(employee):
    rows = row(1, fte="100") + row(2, fte="80") + row(3, fte="50 %") + row(4, fte="60")
    run = run_csv(BASE + rows, employee)
    assert [r["fte"] for r in run.result.records] == ["1", "0.8", "0.5", "0.6"]


def test_unknown_enum_suggests_close_value(employee):
    run = run_csv(BASE + row(1, status="activ") + row(2, status="on parental leave") + row(3) + row(4), employee)
    issue = next(i for i in run.result.issues if i.code == "UNKNOWN_ENUM")
    assert "active" in issue.suggestion
    assert run.result.records[1]["employment_status"] == "on_leave"


def test_unmapped_required_holds_everything(employee):
    run = run_csv("Name,Email\nAnna Berg,a@x.example\nJonas Müller,j@x.example\n", employee)
    assert len(run.result.held) == 2
    assert any(r.code == "UNMAPPED_REQUIRED" and r.field == "employee_id" for r in run.review)
