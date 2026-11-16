import json

import pytest

from onboarding_mapper import cli, schema, synth


@pytest.fixture
def exports(tmp_path):
    out = {}
    for g in synth.ALL:
        fx = g()
        path = tmp_path / fx.filename
        path.write_bytes(fx.data)
        out[fx.name] = path
    return out


def test_map_writes_every_output(exports, tmp_path, capsys):
    code = cli.main(["map", str(exports["hris_de"]), "--out", str(tmp_path / "o"), "--as-of", "2026-11-16"])
    assert code == 1  # one record held (start date in 2062)
    names = sorted(p.name for p in (tmp_path / "o").iterdir())
    assert names == [
        "held.csv",
        "mapped.csv",
        "mapping.json",
        "report.html",
        "report.json",
        "report.md",
        "review_queue.csv",
    ]
    summary = json.loads((tmp_path / "o" / "report.json").read_text())
    assert summary["stats"]["records_held"] == 1 and summary["stats"]["records_ready"] == 49
    assert "held back: Steuer-ID" in capsys.readouterr().out


def test_strict_fails_on_review_items(exports, tmp_path):
    fx = synth.holdout_fr()
    path = tmp_path / fx.filename
    path.write_bytes(fx.data)
    assert cli.main(["map", str(path), "--out", str(tmp_path / "a"), "--as-of", "2026-11-16", "--quiet"]) == 0
    assert (
        cli.main(["map", str(path), "--out", str(tmp_path / "b"), "--as-of", "2026-11-16", "--quiet", "--strict"]) == 1
    )


def test_apply_with_mapping(exports, tmp_path):
    out = tmp_path / "o"
    cli.main(["map", str(exports["bamboo_us"]), "--out", str(out), "--as-of", "2026-11-16", "--quiet"])
    code = cli.main(
        [
            "apply",
            str(exports["bamboo_us"]),
            "--mapping",
            str(out / "mapping.json"),
            "--out",
            str(tmp_path / "p"),
            "--as-of",
            "2026-11-16",
            "--quiet",
        ]
    )
    assert code == 1
    assert (out / "mapped.csv").read_bytes() == (tmp_path / "p" / "mapped.csv").read_bytes()


def test_errors_exit_2(tmp_path, capsys):
    empty = tmp_path / "empty.csv"
    empty.write_text("")
    assert cli.main(["map", str(empty), "--out", str(tmp_path / "o")]) == 2
    assert cli.main(["map", str(tmp_path / "missing.csv")]) == 2
    assert cli.main(["schema", "payroll"]) == 2
    assert "omap:" in capsys.readouterr().err


def test_profile_and_schema_commands(exports, capsys):
    assert cli.main(["profile", str(exports["nomina_es"])]) == 0
    out = capsys.readouterr().out
    assert "held back: bank_account" in out and "header on row 4" in out
    assert cli.main(["schema"]) == 0 and "employee: 20 fields" in capsys.readouterr().out
    assert cli.main(["schema", "employee"]) == 0 and "END_BEFORE_START" in capsys.readouterr().out


def test_custom_schema(tmp_path):
    spec = {
        "name": "contractors",
        "fields": [
            {"name": "contractor_ref", "type": "id", "required": True, "unique": True, "aliases": ["Vendor No"]},
            {"name": "email", "type": "email", "concept": "contact.work_email"},
            {
                "name": "tier",
                "type": "enum",
                "values": ["gold", "silver"],
                "synonyms": {"gold": ["G", "Tier 1"], "silver": ["S"]},
            },
        ],
    }
    path = tmp_path / "contractors.json"
    path.write_text(json.dumps(spec))
    data = tmp_path / "c.csv"
    data.write_text("Vendor No,Email,Tier\nV1,a@x.example,Tier 1\nV2,b@x.example,S\nV3,c@x.example,G\n")
    assert cli.main(["map", str(data), "--schema", str(path), "--out", str(tmp_path / "o"), "--quiet"]) == 0
    rows = (tmp_path / "o" / "mapped.csv").read_text().splitlines()
    assert rows[0] == "contractor_ref,email,tier" and rows[1] == "V1,a@x.example,gold"


@pytest.mark.parametrize(
    "spec,msg",
    [
        ({"fields": []}, "non-empty"),
        ({"fields": [{"name": "a", "type": "money"}]}, "unknown type"),
        ({"fields": [{"name": "a"}, {"name": "a"}]}, "unique"),
        ({"fields": [{"name": "a", "type": "enum"}]}, "needs 'values'"),
        ({"fields": [{"name": "a", "type": "ref", "ref": "b"}]}, "unknown field"),
        ({"fields": [{"name": "a", "concept": "x.y"}]}, "unknown concept"),
        ({"fields": [{"name": "a"}], "rules": [{"id": "R", "type": "magic"}]}, "unknown type"),
    ],
)
def test_schema_validation(spec, msg):
    with pytest.raises(schema.SchemaError, match=msg):
        schema.from_dict(spec)
