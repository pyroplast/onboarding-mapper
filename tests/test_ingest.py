import pytest

from onboarding_mapper import ingest, synth


def test_bom_semicolon_preamble_footer():
    text = "﻿Staff list\nexported today\n\nId;Name;Start\n1;Anna;2020-01-01\n2;Jonas;2021-02-02\n\nTotal;2\n"
    t = ingest.read_csv_bytes(text.encode("utf-8"))
    assert t.headers == ["Id", "Name", "Start"]
    assert t.delimiter == ";"
    assert t.encoding == "utf-8-sig"
    assert t.header_row == 4
    assert [r[1] for r in t.rows] == ["Anna", "Jonas"]
    assert t.row_numbers == [5, 6]
    assert any("footer" in n for n in t.notes) and any("above the header" in n for n in t.notes)


def test_cp1252_is_detected():
    t = ingest.read_csv_bytes("Nombre,Situación\nMaría,Excedencia\n".encode("cp1252"))
    assert t.encoding == "cp1252"
    assert t.headers[1] == "Situación" and t.rows[0][0] == "María"


def test_utf16():
    t = ingest.read_csv_bytes("Id\tName\n1\tAnna\n".encode("utf-16"))
    assert t.headers == ["Id", "Name"] and t.delimiter == "\t"


def test_ragged_rows_empty_and_duplicate_headers():
    text = "Id,Name,,Name,Empty\n1,Anna,,Berg\n2,Jonas,,Müller,,extra\n"
    t = ingest.read_csv_bytes(text.encode())
    # The blank column goes; the unnamed column holding "extra" stays, so no value is lost.
    assert t.headers == ["Id", "Name", "Name (2)", "Empty", "column_5"]
    assert t.rows[1] == ["2", "Jonas", "Müller", "", "extra"]
    assert any("different number of cells" in n for n in t.notes)


def test_quoted_newlines_and_nbsp():
    text = 'Id,Notes,Salary\n1,"line one\nline two","45 000"\n'
    t = ingest.read_csv_bytes(text.encode())
    assert t.rows[0] == ["1", "line one line two", "45 000"]


def test_errors():
    with pytest.raises(ingest.IngestError):
        ingest.read_csv_bytes(b"Id,Name\n")
    with pytest.raises(ingest.IngestError):
        ingest.read_xlsx_bytes(b"not a zip")


def test_xlsx_with_date_cells():
    t = ingest.read_xlsx_bytes(synth.hris_de().data, "hris_de.xlsx")
    assert t.headers[0] == "Personalnummer" and t.headers[7] == "Eintrittsdatum"
    assert t.rows[0][0] == "1001"  # numeric cell, no trailing .0
    assert t.rows[0][7].count("-") == 2  # date-styled cell read as ISO


def test_fingerprint_depends_on_layout_not_values():
    a = ingest.read_csv_bytes(b"Id,Name\n1,Anna\n")
    b = ingest.read_csv_bytes(b"Id,Name\n2,Jonas\n")
    c = ingest.read_csv_bytes(b"Id,Full name\n1,Anna\n")
    assert a.fingerprint == b.fingerprint != c.fingerprint
    assert a.sha256 != b.sha256
