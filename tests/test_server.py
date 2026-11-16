"""The client panel's HTTP API, exercised on a real local server."""

import json
import threading
import urllib.error
import urllib.request

import pytest

from onboarding_mapper import server, synth


@pytest.fixture(scope="module")
def base():
    httpd = server.make_server(port=0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def call(base, path, data=None, headers=None, method=None):
    req = urllib.request.Request(
        base + path, data=data, headers=headers or {}, method=method or ("POST" if data is not None else "GET")
    )
    try:
        with urllib.request.urlopen(req) as r:
            body = r.read()
            return r.status, body, r.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers


def post_json(base, path, obj):
    status, body, _ = call(base, path, json.dumps(obj).encode(), {"Content-Type": "application/json"})
    return status, json.loads(body)


def test_page_and_samples(base):
    status, body, headers = call(base, "/")
    assert status == 200 and b"Onboarding Mapper" in body and "text/html" in headers["Content-Type"]
    status, body, _ = call(base, "/api/samples")
    names = [s["name"] for s in json.loads(body)["samples"]]
    assert names == ["bamboo_us", "nomina_es", "hris_de", "spreadsheet_hell", "holdout_fr"]


def test_upload_edit_decide_download(base):
    fx = synth.nomina_es()
    status, body, _ = call(base, "/api/run?country=ES&as_of=2026-11-16", fx.data, {"X-Filename": fx.filename})
    assert status == 200
    view = json.loads(body)
    sid = view["session"]
    assert view["stats"]["records_ready"] == 44 and view["file"]["encoding"] == "cp1252"
    assert {h["source"] for h in view["held_back"]} == {"DNI/NIE", "IBAN"}
    # sensitive values never reach the panel
    dni = next(c for c in view["columns"] if c["header"] == "DNI/NIE")
    assert dni["samples"] == [] and dni["state"] == "held back"

    # point location at another column, then clear it
    status, view = post_json(base, f"/api/session/{sid}/mapping", {"edits": {"location": {"source": "Departamento"}}})
    loc = next(f for f in view["fields"] if f["field"] == "location")
    assert status == 200 and loc["source"] == "Departamento" and loc["status"] == "accepted"
    assert view["preview"][0]["cells"]["location"]["out"] == view["preview"][0]["cells"]["department"]["out"]
    status, view = post_json(base, f"/api/session/{sid}/mapping", {"edits": {"location": {"source": None}}})
    assert not next(f for f in view["fields"] if f["field"] == "location")["active"]

    # flip the date order and back
    status, view = post_json(
        base, f"/api/session/{sid}/mapping", {"edits": {"start_date": {"options": {"date_order": "mdy"}}}}
    )
    assert status == 200

    # decide a review item; it lands in the downloaded queue
    key = view["review"][0]["key"]
    status, out = post_json(base, f"/api/session/{sid}/decision", {"key": key, "decision": "fix"})
    assert status == 200 and out["decided"] == 1
    status, body, headers = call(base, f"/api/session/{sid}/download/review_queue.csv")
    assert status == 200 and body.decode().splitlines()[1].endswith(",fix")
    assert 'filename="nomina_es_review_queue.csv"' in headers["Content-Disposition"]
    for name in ("mapped.csv", "held.csv", "mapping.json", "report.html"):
        status, body, _ = call(base, f"/api/session/{sid}/download/{name}")
        assert status == 200 and body


def test_sample_run(base):
    status, body, _ = call(base, "/api/run?sample=bamboo_us", b"")
    view = json.loads(body)
    assert status == 200 and view["stats"]["records_held"] == 3 and len(view["review"]) == 8


def test_refusals(base):
    fx = synth.bamboo_us()
    _, body, _ = call(base, "/api/run", fx.data, {"X-Filename": fx.filename})
    sid = json.loads(body)["session"]
    status, out = post_json(base, f"/api/session/{sid}/mapping", {"edits": {"job_title": {"source": "SSN"}}})
    assert status == 422 and "sensitive" in out["error"]
    status, out = post_json(base, f"/api/session/{sid}/mapping", {"edits": {"job_title": {"source": "Nope"}}})
    assert status == 422
    status, out = post_json(base, f"/api/session/{sid}/decision", {"key": "x", "decision": "delete"})
    assert status == 422
    assert call(base, "/api/session/nope")[0] == 404
    assert call(base, f"/api/session/{sid}/download/secrets.txt")[0] == 404
    assert call(base, "/api/run", b"", {"X-Filename": "empty.csv"})[0] == 422
    # a page on another origin cannot drive the local server
    status, _, _ = call(base, "/api/run?sample=bamboo_us", b"", {"Origin": "https://evil.example"})
    assert status == 403
