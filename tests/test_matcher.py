from datetime import date

from conftest import AS_OF, run_csv

from onboarding_mapper import lexicon, matcher
from onboarding_mapper.normalize import norm_header


def test_header_score_exact_contains_and_qualified():
    aliases = [(norm_header("phone"), 1.0), (norm_header("telefon"), 1.0)]
    assert matcher.header_score("phone", aliases)[0] == 1.0
    assert 0.8 < matcher.header_score("work phone number", aliases)[0] < 1.0
    assert matcher.header_score("emergency contact phone", aliases)[0] < 0.4
    assert matcher.header_score(norm_header("Notfallkontakt Telefon"), aliases)[0] < 0.4


EXPORT = """Employee #,First Name,Last Name,Work Email,Employment Status,Title,Job,Emergency Contact Phone,SSN,Hire Date
1,Anna,Berg,anna@acme.example,Full-Time,Ms,Engineer,(555) 111-2222,123-45-6789,03/14/2021
2,Jonas,Müller,jonas@acme.example,Part-Time,Mr,Designer,(555) 111-3333,123-45-6780,07/01/2022
3,Lea,Schmidt,lea@acme.example,Full-Time,Dr,Analyst,(555) 111-4444,123-45-6781,11/30/2020
4,Tim,Klein,tim@acme.example,Contractor,Mr,Engineer,(555) 111-5555,123-45-6782,01/02/2023
"""


def test_values_override_a_misleading_header(employee):
    run = run_csv(EXPORT, employee)
    m = run.mappings
    # "Employment Status" holds contract types: the values win, and a person confirms.
    assert m["employment_type"].source == "Employment Status" and m["employment_type"].status == "review"
    assert not m["employment_status"].active


def test_honorifics_are_not_job_titles(employee):
    run = run_csv(EXPORT, employee)
    assert run.mappings["job_title"].source == "Job"


def test_emergency_phone_is_not_the_employees_phone(employee):
    run = run_csv(EXPORT, employee)
    assert not run.mappings["phone"].active


def test_sensitive_columns_are_held_back(employee):
    run = run_csv(EXPORT, employee)
    assert [s["source"] for s in run.proposal.sensitive] == ["SSN"]
    assert all(m.source != "SSN" for m in run.mappings.values())


def test_month_first_dates_are_detected(employee):
    run = run_csv(EXPORT, employee)
    assert run.mappings["start_date"].options["date_order"] == "mdy"
    assert run.result.records[0]["start_date"] == "2021-03-14"


def test_one_column_feeds_one_field(employee):
    run = run_csv(EXPORT, employee)
    used = [m.source for m in run.mappings.values() if m.active and m.method == "direct"]
    assert len(used) == len(set(used))


def test_values_alone_never_auto_apply():
    for v in (0.0, 0.5, 1.0):
        assert matcher.combine(0.0, v, distinctive=True) < matcher.AUTO
        assert matcher.combine(0.3, v, distinctive=False) < matcher.REVIEW


def test_full_name_split_when_no_separate_columns(employee):
    run = run_csv("Name,Email\nBerg, Anna\n", employee)  # comma inside the name needs quotes, so:
    run = run_csv(
        'Name,Email\n"Berg, Anna",a@x.example\n"Müller, Jonas",j@x.example\n"Klein, Tim",t@x.example\n', employee
    )
    m = run.mappings
    assert m["first_name"].method == m["last_name"].method == "split_full_name"
    assert run.result.records[1]["first_name"] == "Jonas" and run.result.records[1]["last_name"] == "Müller"


def test_birth_years_do_not_pass_as_start_dates(employee):
    text = "Id,Start,Born\n" + "".join(f"{i},{1950 + i}-05-0{i},{1950 + i}-05-0{i}\n" for i in range(1, 8))
    run = run_csv(text, employee)
    start = run.mappings["start_date"]
    assert start.status != "auto" and any("does not fit" in r for r in start.reasons + [""]) or not start.active
    assert run.mappings["date_of_birth"].source == "Born" and run.mappings["date_of_birth"].status == "auto"


def test_work_vs_personal_email(employee):
    text = "Id,Email,Email 2\n" + "".join(f"{i},p{i}@gmail.com,w{i}@acme.example\n" for i in range(1, 8))
    run = run_csv(text, employee)
    assert run.mappings["work_email"].source == "Email 2"
    assert run.mappings["personal_email"].source == "Email"


def test_lexicon_is_consistent():
    for concept, aliases in lexicon.ALIASES.items():
        assert aliases, concept
        for phrase, weight in aliases:
            assert phrase == norm_header(phrase) and 0 < weight <= 1
    assert date(2026, 1, 1) < AS_OF
