from datetime import date
from decimal import Decimal

import pytest

from onboarding_mapper import parsers
from onboarding_mapper.normalize import header_hint, mask, norm_header

AS_OF = date(2026, 11, 16)


@pytest.mark.parametrize(
    "header,expected",
    [
        ("Fecha de Alta (dd/mm/aaaa)", "fecha de alta"),
        ("hireDate", "hire date"),
        ("EMP_NO.", "employee number"),
        ("E-Mail", "email"),
        ("Personal-Nr.", "personal number"),
        ("Mgr E-mail", "manager email"),
        ("Prénom", "prenom"),
        ("Employee #", "employee number"),
    ],
)
def test_norm_header(header, expected):
    assert norm_header(header) == expected


def test_header_hint():
    assert header_hint("Start (MM/DD/YYYY)") == "mm/dd/yyyy"


def test_mask_keeps_shape_not_content():
    assert mask("anna.berg@nordwind.example") == "a***@nordwind.example"
    assert mask("Anna Berg") == "An•• ••••"
    assert mask("ES79 0000") == "ES•• ••••"


@pytest.mark.parametrize(
    "raw,order,expected",
    [
        ("2021-03-04", "dmy", date(2021, 3, 4)),
        ("04/03/2021", "dmy", date(2021, 3, 4)),
        ("04/03/2021", "mdy", date(2021, 4, 3)),
        ("31/12/2020", "mdy", date(2020, 12, 31)),  # only one order is valid
        ("04.03.2021", "dmy", date(2021, 3, 4)),
        ("4 Mar 2021", "dmy", date(2021, 3, 4)),
        ("4 de marzo de 2021", "dmy", date(2021, 3, 4)),
        ("4. März 2021", "dmy", date(2021, 3, 4)),
        ("4 févr. 2021", "dmy", date(2021, 2, 4)),
        ("March 4, 2021", "dmy", date(2021, 3, 4)),
        ("20210304", "dmy", date(2021, 3, 4)),
        ("2021-03-04T09:30:00Z", "dmy", date(2021, 3, 4)),
        ("04/03/88", "dmy", date(1988, 3, 4)),
        ("04/03/21", "dmy", date(2021, 3, 4)),
    ],
)
def test_parse_date(raw, order, expected):
    assert parsers.parse_date(raw, order, AS_OF).value == expected


def test_parse_date_flags():
    assert parsers.parse_date("04/03/2021", "dmy", AS_OF).ambiguous
    assert not parsers.parse_date("13/03/2021", "dmy", AS_OF).ambiguous
    assert parsers.parse_date("04/03/21", "dmy", AS_OF).two_digit_year
    assert parsers.parse_date("44197", "dmy", AS_OF) is None
    serial = parsers.parse_date("44197", "dmy", AS_OF, allow_serial=True)
    assert serial.value == date(2021, 1, 1) and serial.kind == "serial"
    assert parsers.parse_date("31/02/2021", "dmy", AS_OF) is None
    assert parsers.parse_date("next week", "dmy", AS_OF) is None


def test_date_order_evidence():
    assert parsers.date_order_evidence(["13/01/2020", "02/03/2020", "01/01/2020"]) == (1, 0, 1)
    assert parsers.date_order_evidence(["01/13/2020"]) == (0, 1, 0)


@pytest.mark.parametrize(
    "raw,style,expected",
    [
        ("45.000,50", "eu", "45000.50"),
        ("45,000.50", "eu", "45000.50"),
        ("45.000", "eu", "45000"),
        ("45.000", "us", "45.000"),
        ("45,000", "us", "45000"),
        ("45,5", "us", "45.5"),
        ("45 000", "eu", "45000"),
        ("1.234.567", "us", "1234567"),
        ("€ 62.000", "eu", "62000"),
        ("$85,000.00", "us", "85000.00"),
        ("62000 EUR", "eu", "62000"),
        ("45k", "eu", "45000"),
        ("80%", "eu", "80"),
        ("1'250'000", "eu", "1250000"),
    ],
)
def test_parse_number(raw, style, expected):
    assert parsers.parse_number(raw, style) == Decimal(expected)


def test_parse_number_rejects_text():
    assert parsers.parse_number("n/a") is None
    assert parsers.parse_number("12-34") is None


def test_number_readings():
    assert parsers.number_readings("62,000") == [Decimal("62.000"), Decimal("62000")]
    assert parsers.number_readings("62.000,50") == [Decimal("62000.50")]


@pytest.mark.parametrize(
    "raw,code",
    [
        ("€ 45.000", "EUR"),
        ("$100", "USD"),
        ("100 GBP", "GBP"),
        ("45000 euros", "EUR"),
        ("45000", None),
        ("ABC 10", None),
    ],
)
def test_extract_currency(raw, code):
    assert parsers.extract_currency(raw) == code


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Anna.Berg@Nordwind.example", "anna.berg@nordwind.example"),
        ("Anna Berg <anna.berg@nordwind.example>", "anna.berg@nordwind.example"),
        ("mailto:a@b.example", "a@b.example"),
        ("anna.berg @nordwind.example", None),
        ("anna..berg@nordwind.example", None),
        ("not an email", None),
    ],
)
def test_parse_email(raw, expected):
    assert parsers.parse_email(raw) == expected


@pytest.mark.parametrize(
    "raw,country,expected",
    [
        ("(561) 318-6091", "US", ("+15613186091", True)),
        ("612 34 56 78", "ES", ("+34612345678", True)),
        ("0171 1234567", "DE", ("+491711234567", True)),
        ("+49 (0)171 1234567", None, ("+491711234567", True)),
        ("0049 171 1234567", None, ("+491711234567", True)),
        ("06 12 34 56 78", "FR", ("+33612345678", True)),
        ("612345678", None, ("612345678", False)),
        ("555-1234 ext. 12", None, ("5551234", False)),
        ("call me", None, (None, False)),
    ],
)
def test_parse_phone(raw, country, expected):
    assert parsers.parse_phone(raw, country) == expected


@pytest.mark.parametrize(
    "raw,code",
    [
        ("Spain", "ES"),
        ("España", "ES"),
        ("Deutschland", "DE"),
        ("Österreich", "AT"),
        ("USA", "US"),
        ("NA", "NA"),
        ("Atlantis", None),
    ],
)
def test_parse_country(raw, code):
    assert parsers.parse_country(raw) == code


def test_split_name():
    assert parsers.split_name("Berg, Anna") == parsers.SplitName("Anna", "Berg", False)
    assert parsers.split_name("García López, María") == parsers.SplitName("María", "García López", False)
    assert parsers.split_name("Anna van der Berg") == parsers.SplitName("Anna", "van der Berg", False)
    assert parsers.split_name("Mary Ann Smith").uncertain
    assert parsers.split_name("Smith Anna", "last_first") == parsers.SplitName("Anna", "Smith", False)


def test_title_case_name():
    assert parsers.title_case_name("GARCÍA LÓPEZ") == "García López"
    assert parsers.title_case_name("o'neill") == "O'Neill"
    assert parsers.title_case_name("anna van der berg") == "Anna van der Berg"
    assert parsers.title_case_name("McDonald") == "McDonald"
    assert parsers.title_case_name("JEAN-LUC") == "Jean-Luc"


def test_sensitive_value_checks():
    assert parsers.is_iban("DE89 3704 0044 0532 0130 00")
    assert not parsers.is_iban("DE89 3704 0044 0532 0130 01")
    assert parsers.is_national_id("12345678Z")
    assert not parsers.is_national_id("12345678A")
    assert parsers.is_national_id("X1234567L")
    assert parsers.is_national_id("123-45-6789")
