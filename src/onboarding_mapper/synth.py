"""Synthetic customer exports with ground truth.

Each generator builds clean employee records, then writes them the way a real
source system would: its own headers, date and number formats, encodings and
the usual damage. It also returns the truth: which column feeds which field,
the value every cell should become, and the problems a reviewer should be shown.

Everything is seeded and made up. Names are common given and family names,
domains end in .example, IBANs and ID numbers are generated to pass their
checksums but belong to nobody.
"""

from __future__ import annotations

import csv
import io
import random
import zipfile
from dataclasses import dataclass, field
from datetime import date, timedelta
from xml.sax.saxutils import escape

AS_OF = date(2026, 11, 16)

FIRST = {
    "en": ["James", "Olivia", "Liam", "Emma", "Noah", "Ava", "Ethan", "Mia", "Lucas", "Grace", "Henry", "Chloe", "Owen", "Zoe", "Caleb", "Nora", "Isaac", "Leah", "Ryan", "Hannah"],
    "es": ["María", "Javier", "Lucía", "Carlos", "Marta", "Pablo", "Laura", "Sergio", "Elena", "Andrés", "Paula", "Diego", "Carmen", "Álvaro", "Nuria", "Jordi", "Irene", "Raúl", "Sofía", "Adrián"],
    "fr": ["Camille", "Louis", "Manon", "Hugo", "Chloé", "Arthur", "Inès", "Jules", "Léa", "Gabriel", "Alice", "Raphaël", "Jade", "Théo", "Zoé", "Nathan", "Lina", "Tom", "Margaux", "Baptiste"],
    "de": ["Anna", "Jonas", "Lea", "Lukas", "Laura", "Felix", "Julia", "Paul", "Marie", "Leon", "Sophie", "Tim", "Lena", "Jan", "Clara", "Niklas", "Hannah", "Moritz", "Emilia", "Finn"],
}  # fmt: skip
LAST = {
    "en": ["Smith", "Johnson", "Brown", "Taylor", "Miller", "Wilson", "Moore", "Clark", "Walker", "Young", "Allen", "King", "Wright", "Scott", "Green", "Baker", "Adams", "Nelson", "Hill", "Campbell"],
    "es": ["García", "Martínez", "López", "Sánchez", "Pérez", "Gómez", "Fernández", "Ruiz", "Díaz", "Moreno", "Muñoz", "Álvarez", "Romero", "Navarro", "Torres", "Domínguez", "Vázquez", "Ramos", "Gil", "Serrano"],
    "fr": ["Martin", "Bernard", "Dubois", "Thomas", "Robert", "Richard", "Petit", "Durand", "Leroy", "Moreau", "Simon", "Laurent", "Lefèvre", "Michel", "Garcia", "David", "Bertrand", "Roux", "Vincent", "Fournier"],
    "de": ["Müller", "Schmidt", "Schneider", "Fischer", "Weber", "Meyer", "Wagner", "Becker", "Schulz", "Hoffmann", "Koch", "Richter", "Klein", "Wolf", "Schröder", "Neumann", "Schwarz", "Braun", "Zimmermann", "Krüger"],
}  # fmt: skip
DEPTS = {
    "en": [("Engineering", ["Software Engineer", "Senior Software Engineer", "Engineering Manager", "QA Engineer"]),
           ("Sales", ["Account Executive", "Sales Development Rep", "Sales Manager"]),
           ("Customer Success", ["Customer Success Manager", "Support Specialist"]),
           ("Finance", ["Financial Analyst", "Controller"]), ("People", ["HR Business Partner", "Recruiter"])],
    "es": [("Tecnología", ["Desarrollador", "Desarrolladora Senior", "Responsable de Sistemas"]),
           ("Comercial", ["Comercial", "Key Account Manager", "Director Comercial"]),
           ("Atención al Cliente", ["Agente de Soporte", "Customer Success"]),
           ("Administración", ["Contable", "Administrativo"]), ("Recursos Humanos", ["Técnico de RRHH"])],
    "fr": [("Technique", ["Développeur", "Développeuse senior", "Responsable technique"]),
           ("Commercial", ["Commercial", "Responsable grands comptes"]),
           ("Service client", ["Chargé de clientèle", "Responsable support"]),
           ("Finance", ["Comptable", "Contrôleur de gestion"]), ("RH", ["Chargée RH"])],
    "de": [("Technik", ["Softwareentwickler", "Senior Entwicklerin", "Teamleiter Technik"]),
           ("Vertrieb", ["Vertriebsmitarbeiter", "Key Account Managerin", "Vertriebsleiter"]),
           ("Kundenservice", ["Kundenbetreuer", "Support Spezialistin"]),
           ("Finanzen", ["Buchhalter", "Controllerin"]), ("Personal", ["HR Generalist"])],
}  # fmt: skip
CITIES = {"en": [("Austin", "US"), ("Denver", "US"), ("Boston", "US"), ("Toronto", "CA")],
          "es": [("Barcelona", "ES"), ("Madrid", "ES"), ("Valencia", "ES")],
          "fr": [("Paris", "FR"), ("Lyon", "FR"), ("Lille", "FR")],
          "de": [("Berlin", "DE"), ("München", "DE"), ("Hamburg", "DE"), ("Wien", "AT")]}  # fmt: skip


@dataclass
class Person:
    employee_id: str
    first_name: str
    last_name: str
    work_email: str
    personal_email: str
    phone_national: str  # digits as dialled at home, with trunk 0 where the country uses one
    phone_e164: str
    date_of_birth: date
    start_date: date
    end_date: date | None
    employment_status: str
    employment_type: str
    job_title: str
    department: str
    manager_id: str
    location: str
    country: str
    salary_amount: str  # canonical, 2 places
    salary_currency: str
    pay_frequency: str
    fte: str


@dataclass
class Fixture:
    name: str
    filename: str
    data: bytes
    description: str
    mapping: dict[str, str | None]  # field -> source header (None: no source)
    methods: dict[str, str] = field(default_factory=dict)
    held_back: list[str] = field(default_factory=list)
    records: list[dict[str, str]] = field(default_factory=list)  # truth per data row, in file order
    expected_issues: list[dict] = field(default_factory=list)  # {"record": i, "field": f, "code": c}
    expected_review_mappings: list[str] = field(default_factory=list)  # fields that should land in review
    default_country: str | None = None  # what an engineer would pass as --default-country

    def truth(self) -> dict:
        return {
            "fixture": self.name,
            "file": self.filename,
            "description": self.description,
            "as_of": AS_OF.isoformat(),
            "mapping": self.mapping,
            "methods": self.methods,
            "held_back": self.held_back,
            "expected_review_mappings": self.expected_review_mappings,
            "default_country": self.default_country,
            "expected_issues": self.expected_issues,
            "records": self.records,
        }


FIELDS = [
    "employee_id", "first_name", "last_name", "work_email", "personal_email", "phone", "date_of_birth", "start_date",
    "end_date", "employment_status", "employment_type", "job_title", "department", "manager_id", "location",
    "country", "salary_amount", "salary_currency", "pay_frequency", "fte",
]  # fmt: skip


def _ascii(text: str) -> str:
    import unicodedata

    text = (
        text.replace("ß", "ss")
        .replace("ü", "ue")
        .replace("ö", "oe")
        .replace("ä", "ae")
        .replace("Ü", "Ue")
        .replace("Ö", "Oe")
        .replace("Ä", "Ae")
    )
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def _iban(rng: random.Random, country: str, length: int) -> str:
    bban = "".join(str(rng.randint(0, 9)) for _ in range(length - 4))
    digits = "".join(str(int(ch, 36)) for ch in bban + country + "00")
    check = 98 - int(digits) % 97
    return f"{country}{check:02d}{bban}"


def _dni(rng: random.Random) -> str:
    n = rng.randint(10_000_000, 99_999_999)
    return f"{n}{'TRWAGMYFPDXBNJZSQVHLCKE'[n % 23]}"


_DIAL = {"US": ("1", ""), "CA": ("1", ""), "ES": ("34", ""), "DE": ("49", "0"), "AT": ("43", "0"), "FR": ("33", "0")}


def people(rng: random.Random, n: int, locale: str, domain: str, id_fmt: str, currency: str, base: int) -> list[Person]:
    used: set[tuple[str, str]] = set()
    out: list[Person] = []
    depts = DEPTS[locale]
    for i in range(n):
        while True:
            fn, ln = rng.choice(FIRST[locale]), rng.choice(LAST[locale])
            if locale == "es":
                ln = f"{ln} {rng.choice(LAST['es'])}"
            if (fn, ln) not in used:
                used.add((fn, ln))
                break
        dept, titles = depts[i % len(depts)]
        city, country = rng.choice(CITIES[locale])
        start = AS_OF - timedelta(days=rng.randint(40, 365 * 11))
        dob = date(rng.randint(1964, 2002), rng.randint(1, 12), rng.randint(1, 28))
        r = rng.random()
        status, end = "active", None
        if r < 0.12:
            status, end = "terminated", min(AS_OF - timedelta(days=rng.randint(5, 300)), AS_OF)
            if end <= start:
                end = start + timedelta(days=200)
                if end > AS_OF:
                    status, end = "active", None
        elif r < 0.2:
            status = "on_leave"
        etype = rng.choices(["full_time", "part_time", "contractor", "intern"], [70, 18, 7, 5])[0]
        fte = {
            "full_time": "1",
            "part_time": rng.choice(["0.5", "0.6", "0.8"]),
            "contractor": "1",
            "intern": rng.choice(["0.5", "1"]),
        }[etype]
        amount = round(base * rng.uniform(0.7, 2.2) * (float(fte) if etype == "part_time" else 1), -2)
        if etype == "intern":
            amount = round(base * 0.35, -2)
        code, trunk = _DIAL[country]
        digits = "".join(str(rng.randint(0, 9)) for _ in range(10))
        if code == "1":
            nat = str(rng.randint(2, 9)) + digits[:9]
        elif country == "ES":
            nat = rng.choice("67") + digits[:8]
        elif country == "FR":
            nat = "0" + rng.choice("67") + digits[:8]
        else:
            nat = "01" + rng.choice("567") + digits[:8]
        e164 = "+" + code + (nat[1:] if trunk and nat.startswith("0") else nat)
        local = _ascii(f"{fn}.{ln.split()[0]}").lower()
        n_same = sum(1 for q in out if q.work_email.split("@")[0].rstrip("0123456789") == local)
        if n_same:
            local += str(n_same + 1)
        out.append(
            Person(
                employee_id=id_fmt.format(i + 1),
                first_name=fn,
                last_name=ln,
                work_email=f"{local}@{domain}",
                personal_email=f"{_ascii(fn).lower()}{rng.randint(1, 99)}@{rng.choice(['gmail.com', 'personal.example', 'mail.example'])}",
                phone_national=nat,
                phone_e164=e164,
                date_of_birth=dob,
                start_date=start,
                end_date=end,
                employment_status=status,
                employment_type=etype,
                job_title=titles[i % len(titles)],
                department=dept,
                manager_id="",
                location=city,
                country=country,
                salary_amount=f"{amount:.2f}",
                salary_currency=currency,
                pay_frequency="annual",
                fte=fte,
            )
        )
    # A simple tree: the first person in each department reports to person 1.
    heads: dict[str, str] = {}
    for p in out:
        if p.department not in heads:
            heads[p.department] = p.employee_id
            if p.employee_id != out[0].employee_id:
                p.manager_id = out[0].employee_id
        else:
            p.manager_id = heads[p.department]
    return out


def _record(p: Person, include: set[str]) -> dict[str, str]:
    rec = {
        "employee_id": p.employee_id, "first_name": p.first_name, "last_name": p.last_name, "work_email": p.work_email,
        "personal_email": p.personal_email, "phone": p.phone_e164, "date_of_birth": p.date_of_birth.isoformat(),
        "start_date": p.start_date.isoformat(), "end_date": p.end_date.isoformat() if p.end_date else "",
        "employment_status": p.employment_status, "employment_type": p.employment_type, "job_title": p.job_title,
        "department": p.department, "manager_id": p.manager_id, "location": p.location, "country": p.country,
        "salary_amount": p.salary_amount, "salary_currency": p.salary_currency, "pay_frequency": p.pay_frequency,
        "fte": p.fte,
    }  # fmt: skip
    return {k: (v if k in include else "") for k, v in rec.items()}


def _csv_text(rows: list[list[str]], delimiter: str = ",") -> str:
    buf = io.StringIO()
    csv.writer(buf, delimiter=delimiter, lineterminator="\r\n").writerows(rows)
    return buf.getvalue()


def _money_us(v: str) -> str:
    return f"${float(v):,.2f}"


def _money_eu(v: str, symbol: str = "") -> str:
    s = f"{float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{s} {symbol}".strip() if symbol else s


# ---------------------------------------------------------------------------
# 1. US HRIS export (BambooHR-style headers)
# ---------------------------------------------------------------------------


def bamboo_us(n: int = 60, seed: int = 7) -> Fixture:
    rng = random.Random(seed)
    ps = people(rng, n, "en", "acme-robotics.example", "{:04d}", "USD", 70000)
    name_of = {p.employee_id: f"{p.first_name} {p.last_name}" for p in ps}
    headers = [
        "Employee #", "First Name", "Last Name", "Work Email", "Home Email", "Mobile Phone", "Date of Birth",
        "Hire Date", "Termination Date", "Status", "Employment Status", "Job Title", "Department", "Reports To",
        "Location", "Country", "Pay Rate", "Pay Per", "SSN", "Emergency Contact Phone", "Last Modified",
    ]  # fmt: skip
    rows = [headers]
    status_word = {"active": "Active", "on_leave": "On Leave", "terminated": "Terminated"}
    type_word = {"full_time": "Full-Time", "part_time": "Part-Time", "contractor": "Contractor", "intern": "Intern"}
    us = lambda d: f"{d.month:02d}/{d.day:02d}/{d.year}"  # noqa: E731
    include = set(FIELDS) - {"fte"}
    records, issues = [], []
    for i, p in enumerate(ps):
        rec = _record(p, include)
        phone = f"({p.phone_national[:3]}) {p.phone_national[3:6]}-{p.phone_national[6:]}"
        email = p.work_email
        if i == 17:
            email = p.work_email.replace("@", " @")  # stray space from a copy-paste
            rec["work_email"] = ""
            issues.append({"record": i, "field": "work_email", "code": "INVALID_EMAIL"})
        rows.append([
            p.employee_id, p.first_name, p.last_name, email, p.personal_email, phone, us(p.date_of_birth),
            us(p.start_date), us(p.end_date) if p.end_date else "", status_word[p.employment_status],
            type_word[p.employment_type], p.job_title, p.department, name_of.get(p.manager_id, ""), p.location,
            {"US": "United States", "CA": "Canada"}[p.country], _money_us(p.salary_amount), "Year",
            f"{rng.randint(100, 899)}-{rng.randint(10, 99)}-{rng.randint(1000, 9999)}",
            f"({rng.randint(200, 999)}) 555-{rng.randint(1000, 9999)}", "2026-11-0" + str(rng.randint(1, 9)),
        ])  # fmt: skip
        records.append(rec)
    # A rehire: the same employee number twice.
    dup = list(rows[5])
    dup[7] = us(AS_OF - timedelta(days=30))
    rows.append(dup)
    rec = dict(records[4])
    rec["start_date"] = (AS_OF - timedelta(days=30)).isoformat()
    records.append(rec)
    issues += [{"record": 4, "field": "employee_id", "code": "DUPLICATE_KEY"}, {"record": len(records) - 1, "field": "employee_id", "code": "DUPLICATE_KEY"},
               {"record": 4, "field": "work_email", "code": "DUPLICATE_KEY"}, {"record": len(records) - 1, "field": "work_email", "code": "DUPLICATE_KEY"}]  # fmt: skip
    mapping = {
        "employee_id": "Employee #", "first_name": "First Name", "last_name": "Last Name", "work_email": "Work Email",
        "personal_email": "Home Email", "phone": "Mobile Phone", "date_of_birth": "Date of Birth",
        "start_date": "Hire Date", "end_date": "Termination Date", "employment_status": "Status",
        "employment_type": "Employment Status", "job_title": "Job Title", "department": "Department",
        "manager_id": "Reports To", "location": "Location", "country": "Country", "salary_amount": "Pay Rate",
        "salary_currency": "Pay Rate", "pay_frequency": "Pay Per", "fte": None,
    }  # fmt: skip
    return Fixture(
        "bamboo_us",
        "bamboo_us.csv",
        _csv_text(rows).encode("utf-8"),
        "US HRIS export: MM/DD dates, $ amounts, 'Employment Status' that actually holds the contract type, "
        "manager by full name, SSN and emergency phone columns, one rehire with a duplicate employee number.",
        mapping,
        {"salary_currency": "currency_from_amount"},
        ["SSN"],
        records,
        issues,
        ["employment_type"],
    )


# ---------------------------------------------------------------------------
# 2. Spanish payroll listing (A3/Sage-style)
# ---------------------------------------------------------------------------


def nomina_es(n: int = 45, seed: int = 11) -> Fixture:
    rng = random.Random(seed)
    ps = people(rng, n, "es", "lumen-iberia.example", "E{:05d}", "EUR", 30000)
    name_of = {p.employee_id: f"{p.first_name} {p.last_name}" for p in ps}
    headers = [
        "Nº Empleado", "Apellidos, Nombre", "DNI/NIE", "Correo electrónico", "Teléfono", "Fecha nacimiento",
        "Fecha de alta", "Fecha de baja", "Situación", "Jornada", "% Jornada", "Tipo de contrato", "Puesto",
        "Departamento", "Responsable", "Centro de trabajo", "Salario bruto anual", "IBAN",
    ]  # fmt: skip
    rows: list[list[str]] = [
        ["Listado de empleados - Lumen Ibérica S.L."],
        ["Fecha de emisión: 10/11/2026"],
        [],
        headers,
    ]
    sit = {"active": "Alta", "on_leave": "Excedencia", "terminated": "Baja"}
    jor = {"full_time": "Completa", "part_time": "Parcial", "contractor": "Completa", "intern": "Prácticas"}
    es_ = lambda d: f"{d.day:02d}/{d.month:02d}/{d.year}"  # noqa: E731
    include = set(FIELDS) - {"personal_email", "country", "salary_currency", "pay_frequency"}
    records, issues = [], []
    for i, p in enumerate(ps):
        etype = p.employment_type if p.employment_type != "contractor" else "full_time"
        p.employment_type = etype
        rec = _record(p, include)
        rec["employment_type"] = etype
        fte_pct = str(int(float(p.fte) * 100))
        alta = es_(p.start_date)
        if i in (3, 21):  # cells pasted in from Excel as serial numbers
            alta = str((p.start_date - date(1899, 12, 30)).days)
        dob = es_(p.date_of_birth)
        if i == 9:
            dob = f"{p.date_of_birth.day:02d}/{p.date_of_birth.month:02d}/{str(p.date_of_birth.year)[2:]}"
        tel = f"{p.phone_national[:3]} {p.phone_national[3:5]} {p.phone_national[5:7]} {p.phone_national[7:]}"
        salary = _money_eu(p.salary_amount)
        if i == 30:  # a monthly figure typed into the annual column
            salary = _money_eu(f"{float(p.salary_amount) / 14:.2f}")
            rec["salary_amount"] = f"{float(p.salary_amount) / 14:.2f}"
            issues.append({"record": i, "field": "salary_amount", "code": "NUMBER_OUTLIER"})
        situacion = sit[p.employment_status]
        if p.employment_status == "terminated" and i % 2 == 0:
            situacion = "BAJA"
        baja = es_(p.end_date) if p.end_date else ""
        if i == 12 and p.end_date is None:
            pass
        name_cell = f"{p.last_name}, {p.first_name}"
        if i == 5:
            name_cell = name_cell.upper()
        rows.append([
            p.employee_id, name_cell, _dni(rng) if i % 4 else f"X{rng.randint(1000000, 9999999)}{'TRWAGMYFPDXBNJZSQVHLCKE'[0]}",
            p.work_email, tel, dob, alta, baja, situacion, jor[etype], fte_pct,
            rng.choice(["Indefinido", "Indefinido", "Temporal", "Obra y servicio"]) if etype != "intern" else "Formación",
            p.job_title, p.department, name_of.get(p.manager_id, ""), p.location, salary, _iban(rng, "ES", 24),
        ])  # fmt: skip
        records.append(rec)
    # A row whose end date comes before the start date.
    bad = len(records) - 2
    start = date.fromisoformat(records[bad]["start_date"])
    rows[4 + bad][7] = es_(start - timedelta(days=90))
    rows[4 + bad][8] = "Baja"
    records[bad]["end_date"] = (start - timedelta(days=90)).isoformat()
    records[bad]["employment_status"] = "terminated"
    issues.append({"record": bad, "field": "end_date", "code": "END_BEFORE_START"})
    rows.append([])
    rows.append(["Total empleados:", str(n)])
    mapping = {
        "employee_id": "Nº Empleado", "first_name": "Apellidos, Nombre", "last_name": "Apellidos, Nombre",
        "work_email": "Correo electrónico", "personal_email": None, "phone": "Teléfono",
        "date_of_birth": "Fecha nacimiento", "start_date": "Fecha de alta", "end_date": "Fecha de baja",
        "employment_status": "Situación", "employment_type": "Jornada", "job_title": "Puesto",
        "department": "Departamento", "manager_id": "Responsable", "location": "Centro de trabajo", "country": None,
        "salary_amount": "Salario bruto anual", "salary_currency": None, "pay_frequency": None, "fte": "% Jornada",
    }  # fmt: skip
    return Fixture(
        "nomina_es",
        "nomina_es.csv",
        _csv_text(rows, ";").encode("cp1252"),
        "Spanish payroll listing: Windows-1252, semicolons, title rows and a total row, 'Apellidos, Nombre' "
        "with double surnames, DNI/NIE and IBAN columns, decimal-comma salaries, two Excel serial dates, "
        "a two-digit year, a monthly salary in the annual column, an end date before the start date.",
        mapping,
        {"first_name": "split_full_name", "last_name": "split_full_name"},
        ["DNI/NIE", "IBAN"],
        records,
        issues,
        [],
        "ES",
    )


# ---------------------------------------------------------------------------
# 3. German HRIS workbook (.xlsx)
# ---------------------------------------------------------------------------


def _xlsx(rows: list[list], date_cols: set[int]) -> bytes:
    """Write a minimal .xlsx: inline strings, numbers, and date cells as serials with a date style."""

    def col(i: int) -> str:
        s = ""
        i += 1
        while i:
            i, r = divmod(i - 1, 26)
            s = chr(65 + r) + s
        return s

    sheet = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>']  # fmt: skip
    for r, row in enumerate(rows, 1):
        cells = []
        for c, v in enumerate(row):
            ref = f"{col(c)}{r}"
            if v is None or v == "":
                continue
            if isinstance(v, date):
                cells.append(f'<c r="{ref}" s="1"><v>{(v - date(1899, 12, 30)).days}</v></c>')
            elif isinstance(v, (int, float)):
                cells.append(f'<c r="{ref}"><v>{v}</v></c>')
            else:
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(str(v))}</t></is></c>')
        sheet.append(f'<row r="{r}">{"".join(cells)}</row>')
    sheet.append("</sheetData></worksheet>")
    files = {
        "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>',
        "_rels/.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": '<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Mitarbeiter" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>',
        "xl/styles.xml": '<?xml version="1.0" encoding="UTF-8"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><numFmts count="1"><numFmt numFmtId="164" formatCode="dd.mm.yyyy"/></numFmts><fonts count="1"><font/></fonts><fills count="1"><fill/></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs><cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="164" applyNumberFormat="1"/></cellXfs></styleSheet>',
        "xl/worksheets/sheet1.xml": "".join(sheet),
    }
    buf = io.BytesIO()
    # Stored, not deflated: deflate output differs between zlib builds, and the
    # fixture must be byte-identical everywhere.
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=(2026, 11, 16, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            z.writestr(info, files[name])
    return buf.getvalue()


def hris_de(n: int = 50, seed: int = 23) -> Fixture:
    rng = random.Random(seed)
    ps = people(rng, n, "de", "nordwind.example", "{:d}", "EUR", 52000)
    for i, p in enumerate(ps):
        p.employee_id = str(1000 + i + 1)
    # manager ids were built from the 1-based id format; remap to the 1000+ ids
    for p in ps:
        if p.manager_id:
            p.manager_id = str(1000 + int(p.manager_id))
    by_id = {p.employee_id: p for p in ps}
    headers = [
        "Personalnummer", "Vorname", "Nachname", "E-Mail", "Private E-Mail", "Handynummer", "Geburtsdatum",
        "Eintrittsdatum", "Austrittsdatum", "Status", "Beschäftigungsart", "Position", "Abteilung", "Vorgesetzter",
        "Standort", "Land", "Jahresgehalt", "Wochenstunden", "Steuer-ID", "Konfession",
    ]  # fmt: skip
    rows: list[list] = [headers]
    st = {"active": "aktiv", "on_leave": "Elternzeit", "terminated": "ausgeschieden"}
    art = {"full_time": "Vollzeit", "part_time": "Teilzeit", "contractor": "Freelancer", "intern": "Praktikant"}
    include = set(FIELDS) - {"pay_frequency", "fte", "salary_currency"}
    records, issues = [], []
    for i, p in enumerate(ps):
        rec = _record(p, include)
        rec["salary_currency"] = "EUR"
        mgr = by_id[p.manager_id].work_email if p.manager_id else ""
        if i == 33 and p.manager_id:
            mgr = "k.vogel@nordwind.example"  # a manager who is not in the export
            rec["manager_id"] = ""
            issues.append({"record": i, "field": "manager_id", "code": "UNRESOLVED_REF"})
        phone = p.phone_national[:4] + " " + p.phone_national[4:]
        land = {"DE": "Deutschland", "AT": "Österreich"}[p.country]
        rows.append([
            int(p.employee_id), p.first_name, p.last_name, p.work_email, p.personal_email, phone, p.date_of_birth,
            p.start_date, p.end_date or "", st[p.employment_status], art[p.employment_type], p.job_title,
            p.department, mgr, p.location, land, "€ " + _money_eu(p.salary_amount).replace(",00", ""),
            round(40 * float(p.fte)), f"{rng.randint(10, 99)} {rng.randint(100, 999)} {rng.randint(100, 999)} {rng.randint(100, 999)}",
            rng.choice(["rk", "ev", "--", "--"]),
        ])  # fmt: skip
        records.append(rec)
    # Start date in the far future: a typo of the year.
    rows[8][7] = date(2062, rows[8][7].month, rows[8][7].day)
    records[7]["start_date"] = rows[8][7].isoformat()
    issues.append({"record": 7, "field": "start_date", "code": "DATE_OUT_OF_RANGE"})
    mapping = {
        "employee_id": "Personalnummer", "first_name": "Vorname", "last_name": "Nachname", "work_email": "E-Mail",
        "personal_email": "Private E-Mail", "phone": "Handynummer", "date_of_birth": "Geburtsdatum",
        "start_date": "Eintrittsdatum", "end_date": "Austrittsdatum", "employment_status": "Status",
        "employment_type": "Beschäftigungsart", "job_title": "Position", "department": "Abteilung",
        "manager_id": "Vorgesetzter", "location": "Standort", "country": "Land", "salary_amount": "Jahresgehalt",
        "salary_currency": "Jahresgehalt", "pay_frequency": None, "fte": None,
    }  # fmt: skip
    return Fixture(
        "hris_de",
        "hris_de.xlsx",
        _xlsx(rows, {6, 7, 8}),
        "German HRIS workbook (.xlsx): real date cells, numeric employee ids, manager by email (one not in the "
        "export), country names in German, '€ 52.000' salaries, tax ID and church-tax columns, a start date typed "
        "as 2062.",
        mapping,
        {"salary_currency": "currency_from_amount"},
        ["Steuer-ID", "Konfession"],
        records,
        issues,
        [],
    )


# ---------------------------------------------------------------------------
# 4. A hand-kept spreadsheet
# ---------------------------------------------------------------------------


def spreadsheet_hell(n: int = 30, seed: int = 3) -> Fixture:
    rng = random.Random(seed)
    ps = people(rng, n, "en", "brightpath.example", "BP-{:03d}", "EUR", 48000)
    for p in ps:
        p.country = rng.choice(["IE", "ES", "PT", "NL"])
        p.location = {"IE": "Dublin", "ES": "Barcelona", "PT": "Lisbon", "NL": "Amsterdam"}[p.country]
    email_of = {p.employee_id: p.work_email for p in ps}
    headers = [
        "Title",
        "Name",
        "Email",
        "Job",
        "Team",
        "Manager email",
        "Start",
        "Left on",
        "Status",
        "Type",
        "FTE %",
        "Salary",
        "Country",
        "",
        "Notes",
        "Previous employer",
    ]
    rows: list[list[str]] = [
        ["BrightPath — staff list (KEEP UPDATED!!)"],
        ["owner: ops@brightpath.example", "", "last edit: Sept"],
        headers,
    ]
    include = {"employee_id", "first_name", "last_name", "work_email", "start_date", "end_date", "employment_status",
               "employment_type", "job_title", "department", "manager_id", "country", "salary_amount", "salary_currency", "fte"}  # fmt: skip
    records, issues = [], []
    date_styles = [
        lambda d: d.isoformat(),
        lambda d: f"{d.day} {['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][d.month - 1]} {d.year}",
        lambda d: f"{d.day:02d}/{d.month:02d}/{d.year}",
    ]  # fmt: skip
    for i, p in enumerate(ps):
        rec = _record(p, include)
        rec["employee_id"] = ""  # the sheet has no id column
        rec["manager_id"] = ""
        rec["phone"] = ""
        sal_styles = [
            _money_eu(p.salary_amount, "€"),
            f"€{float(p.salary_amount):,.0f}",
            f"{float(p.salary_amount) / 1000:g}k",
            f"{float(p.salary_amount):.0f} EUR",
        ]
        status = {
            "active": rng.choice(["Active", "active", "current"]),
            "on_leave": "on leave",
            "terminated": rng.choice(["Left", "leaver"]),
        }[p.employment_status]
        etype = {
            "full_time": rng.choice(["FT", "Full time"]),
            "part_time": rng.choice(["PT", "Part time"]),
            "contractor": "Freelance",
            "intern": "Intern",
        }[p.employment_type]
        email = p.work_email if i % 3 else f"{p.first_name} {p.last_name} <{p.work_email}>"
        start = date_styles[i % 3](p.start_date)
        rows.append([
            rng.choice(["Mr", "Ms", "Dr", ""]), f"{p.first_name} {p.last_name}", email, p.job_title, p.department,
            email_of.get(p.manager_id, ""), start, date_styles[i % 3](p.end_date) if p.end_date else "", status, etype,
            f"{int(float(p.fte) * 100)}%", sal_styles[i % 4], p.country, "", rng.choice(["", "", "laptop pending", "visa renewal"]),
            rng.choice(["", "Globex", "Initech", "Umbrella"]),
        ])  # fmt: skip
        records.append(rec)
    rows.append(["", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""])
    rows.append(["TOTAL", str(n)])
    mapping = {
        "employee_id": None, "first_name": "Name", "last_name": "Name", "work_email": "Email", "personal_email": None,
        "phone": None, "date_of_birth": None, "start_date": "Start", "end_date": "Left on",
        "employment_status": "Status", "employment_type": "Type", "job_title": "Job", "department": "Team",
        "manager_id": "Manager email", "location": None, "country": "Country", "salary_amount": "Salary",
        "salary_currency": "Salary", "pay_frequency": None, "fte": "FTE %",
    }  # fmt: skip
    for i in range(len(records)):
        issues.append({"record": i, "field": "employee_id", "code": "FIELD_UNMAPPED"})
    return Fixture(
        "spreadsheet_hell",
        "spreadsheet_hell.csv",
        _csv_text(rows).encode("utf-8-sig"),
        "Hand-kept staff sheet: no employee id at all, a 'Title' column of honorifics, full names, "
        "'Name <email>' cells, three date formats in one column, salaries as '€48,000', '48k' and '48000 EUR', "
        "FTE as percentages, an unnamed empty column, notes and a total row.",
        mapping,
        {"first_name": "split_full_name", "last_name": "split_full_name", "salary_currency": "currency_from_amount"},
        [],
        records,
        issues,
        [],
    )


# ---------------------------------------------------------------------------
# 5. Held out: a French export written after the lexicon was frozen
# ---------------------------------------------------------------------------


def holdout_fr(n: int = 40, seed: int = 31) -> Fixture:
    """Not used while tuning. Its first score is reported as-is in the README."""
    rng = random.Random(seed)
    ps = people(rng, n, "fr", "atelier-lumiere.example", "M{:04d}", "EUR", 42000)
    headers = [
        "Matricule", "Civilité", "Prénom", "Nom d'usage", "Adresse e-mail professionnelle", "E-mail personnel",
        "Téléphone portable", "Date de naissance", "Date d'entrée dans l'entreprise", "Date de sortie",
        "Statut du salarié", "Type de contrat de travail", "Temps de travail", "Intitulé du poste", "Service",
        "Manager (matricule)", "Établissement", "Pays", "Rémunération annuelle brute", "Devise", "Taux d'activité",
        "N° de sécurité sociale", "RIB",
    ]  # fmt: skip
    rows: list[list[str]] = [headers]
    st = {"active": "Actif", "on_leave": "En congé parental", "terminated": "Sorti"}
    tt = {"full_time": "Temps plein", "part_time": "Temps partiel", "contractor": "Temps plein", "intern": "Stage"}
    fr_ = lambda d: f"{d.day:02d}/{d.month:02d}/{d.year}"  # noqa: E731
    include = set(FIELDS) - {"pay_frequency"}
    records = []
    for p in ps:
        if p.employment_type == "contractor":
            p.employment_type = "full_time"
        rec = _record(p, include)
        amount = f"{float(p.salary_amount):,.2f}".replace(",", " ").replace(".", ",")
        tel = " ".join(p.phone_national[i : i + 2] for i in range(0, 10, 2))
        rows.append([
            p.employee_id, rng.choice(["M.", "Mme"]), p.first_name, p.last_name, p.work_email, p.personal_email, tel,
            fr_(p.date_of_birth), fr_(p.start_date), fr_(p.end_date) if p.end_date else "", st[p.employment_status],
            "Stage" if p.employment_type == "intern" else rng.choice(["CDI", "CDI", "CDD"]), tt[p.employment_type],
            p.job_title, p.department, p.manager_id, p.location, "France", amount, "EUR",
            f"{int(float(p.fte) * 100)} %", f"{rng.choice('12')} {rng.randint(60, 99)} {rng.randint(10, 12):02d} {rng.randint(10, 99)} {rng.randint(100, 999)} {rng.randint(100, 999)} {rng.randint(10, 97):02d}",
            _iban(rng, "FR", 27),
        ])  # fmt: skip
        records.append(rec)
    mapping = {
        "employee_id": "Matricule", "first_name": "Prénom", "last_name": "Nom d'usage",
        "work_email": "Adresse e-mail professionnelle", "personal_email": "E-mail personnel",
        "phone": "Téléphone portable", "date_of_birth": "Date de naissance",
        "start_date": "Date d'entrée dans l'entreprise", "end_date": "Date de sortie",
        "employment_status": "Statut du salarié", "employment_type": "Temps de travail",
        "job_title": "Intitulé du poste", "department": "Service", "manager_id": "Manager (matricule)",
        "location": "Établissement", "country": "Pays", "salary_amount": "Rémunération annuelle brute",
        "salary_currency": "Devise", "pay_frequency": None, "fte": "Taux d'activité",
    }  # fmt: skip
    return Fixture(
        "holdout_fr",
        "holdout_fr.csv",
        _csv_text(rows, ";").encode("utf-8"),
        "HELD OUT. French HRIS export written after the lexicon was frozen: long French headers, a CDI/CDD "
        "contract column next to the full/part-time column, space thousands separators, '80 %' activity rates, "
        "social security number and RIB columns.",
        mapping,
        {},
        ["N° de sécurité sociale", "RIB"],
        records,
        [],
        [],
    )


ALL = [bamboo_us, nomina_es, hris_de, spreadsheet_hell]
HOLDOUT = [holdout_fr]


def generate_all() -> list[Fixture]:
    return [g() for g in ALL]
