"""The client panel: a local web app on 127.0.0.1, standard library only.

    omap ui            → opens http://127.0.0.1:8765 in the browser

The browser sends the export to this process and nowhere else. Sessions live
in memory and disappear when the process stops.

API (all JSON unless noted)
    GET  /                                  the panel
    GET  /api/samples                       synthetic exports to try
    GET  /api/schemas                       built-in schemas
    POST /api/run?schema=&country=&as_of=   body: file bytes, header X-Filename (or ?sample=name)
    POST /api/session/<id>/mapping          body: {"edits": {field: {source?, status?, options?}}}
    POST /api/session/<id>/decision         body: {"key": ..., "decision": "accept"|"fix"|"ignore"|""}
    GET  /api/session/<id>/download/<name>  mapped.csv, held.csv, review_queue.csv, mapping.json, report.html
"""

from __future__ import annotations

import copy
import json
import secrets
import threading
import webbrowser
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, unquote, urlsplit

from . import __version__, engine, ingest, matcher, report, schema, synth
from .engine import Run
from .matcher import FieldMapping
from .normalize import clean_cell, is_null

MAX_UPLOAD = 50 * 1024 * 1024
PREVIEW_ROWS = 200
DOWNLOADS = {
    "mapped.csv": "text/csv",
    "held.csv": "text/csv",
    "review_queue.csv": "text/csv",
    "mapping.json": "application/json",
    "report.html": "text/html",
}


class Session:
    def __init__(self, run: Run, filename: str, country: str | None, as_of: date) -> None:
        self.run = run
        self.filename = filename
        self.country = country
        self.as_of = as_of
        self.decisions: dict[str, str] = {}
        self.lock = threading.Lock()


SESSIONS: dict[str, Session] = {}
_SAMPLES = {g.__name__: g for g in synth.ALL + synth.HOLDOUT}


# ---------------------------------------------------------------------------
# View model: everything the panel draws, in one JSON document
# ---------------------------------------------------------------------------


def view(sid: str, s: Session) -> dict:
    run = s.run
    t = run.table
    res = run.result
    stats = run.stats()
    errors = res.errors_by_record()
    issues_by_cell: dict[tuple[int, str], list] = {}
    for i in res.issues:
        if i.record is not None and i.code != "FIELD_UNMAPPED":
            issues_by_cell.setdefault((i.record, i.field), []).append(i)

    fields = []
    for f in run.schema.fields:
        m = run.mappings[f.name]
        d = m.to_dict()
        d.update(
            {
                "type": f.type,
                "required": f.required,
                "description": f.description,
                "values": list(f.values),
                "active": m.active,
            }
        )
        fields.append(d)

    preview = []
    for i, rec in enumerate(res.records[:PREVIEW_ROWS]):
        cells = {}
        for name, m in run.mappings.items():
            raw = t.rows[i][m.column] if m.active and m.column is not None else ""
            out = rec[name]
            iss = issues_by_cell.get((i, name), [])
            sev = (
                "error"
                if any(x.severity == "error" for x in iss)
                else ("warning" if any(x.severity == "warning" for x in iss) else ("info" if iss else ""))
            )
            cells[name] = {
                "out": out,
                "src": "" if is_null(raw) else raw,
                "changed": bool(out) and out != clean_cell(raw),
                "sev": sev,
                "notes": [f"{x.message}" + (f" — {x.suggestion}" if x.suggestion else "") for x in iss],
            }
        preview.append(
            {
                "line": res.rows[i],
                "held": i in errors,
                "why": [f"{x.field}: {x.message}" for x in errors.get(i, [])],
                "cells": cells,
            }
        )

    columns = []
    used = {m.source for m in run.mappings.values() if m.active}
    held = {h["source"]: h for h in run.proposal.sensitive}
    unused = {u["source"]: u["reason"] for u in run.proposal.unused}
    for p in run.profiles:
        top = sorted(
            ((v, k) for k, v in p.fit.items() if v >= 0.5 and not k.startswith(("name_multi", "date_any", "enum:"))),
            reverse=True,
        )[:3]
        columns.append(
            {
                "header": p.header,
                "fill": round(p.fill_rate, 3),
                "distinct": p.distinct,
                "looks_like": [k for _, k in top],
                "samples": [] if p.header in held else p.samples[:3],
                "state": "held back" if p.header in held else ("used" if p.header in used else "not used"),
                "note": held[p.header]["label"] if p.header in held else unused.get(p.header, ""),
            }
        )

    review = []
    for r in run.review:
        d = r.to_dict()
        d["decision"] = s.decisions.get(r.key, "")
        review.append(d)

    return {
        "session": sid,
        "version": __version__,
        "file": {
            "name": s.filename,
            "rows": len(t.rows),
            "columns": len(t.headers),
            "encoding": t.encoding,
            "delimiter": t.delimiter,
            "header_row": t.header_row,
            "notes": t.notes,
            "fingerprint": t.fingerprint,
        },
        "schema": {"name": run.schema.name, "version": run.schema.version},
        "as_of": s.as_of.isoformat(),
        "country": s.country,
        "headers": t.headers,
        "held_back": run.proposal.sensitive,
        "stats": stats,
        "fields": fields,
        "preview": preview,
        "preview_total": len(res.records),
        "columns": columns,
        "review": review,
        "manager_resolution": res.ref_resolution,
        "mapping_source": run.mapping_source,
    }


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def start(data: bytes, filename: str, schema_name: str, country: str | None, as_of: date) -> tuple[str, Session]:
    sch = schema.load(schema_name)
    if filename.lower().endswith((".xlsx", ".xlsm")) or data[:4] == b"PK\x03\x04":
        table = ingest.read_xlsx_bytes(data, filename)
    else:
        table = ingest.read_csv_bytes(data, filename)
    run = engine.run(filename, sch, as_of=as_of, default_country=country, table=table)
    sid = secrets.token_urlsafe(12)
    SESSIONS[sid] = Session(run, filename, country, as_of)
    if len(SESSIONS) > 20:  # keep memory bounded: drop the oldest
        SESSIONS.pop(next(iter(SESSIONS)))
    return sid, SESSIONS[sid]


def edit(s: Session, edits: dict) -> None:
    """Apply reviewer edits to the current mapping and re-run."""
    run = s.run
    headers = {h: i for i, h in enumerate(run.table.headers)}
    held = {h["source"] for h in run.proposal.sensitive}
    profiles = {p.index: p for p in run.profiles}
    mappings = copy.deepcopy(run.mappings)
    for name, change in edits.items():
        if name not in mappings:
            raise engine.MappingError(f"unknown field '{name}'")
        m = mappings[name]
        f = run.schema.get(name)
        if "source" in change:
            src = change["source"]
            if src in (None, ""):
                mappings[name] = FieldMapping(
                    name, status="rejected", reasons=["cleared by reviewer"], alternatives=m.alternatives
                )
                continue
            if src not in headers:
                raise engine.MappingError(f"column '{src}' is not in this export")
            if src in held:
                raise engine.MappingError(f"column '{src}' is held back as sensitive")
            if src != m.source or m.method != "direct":
                col = headers[src]
                mappings[name] = FieldMapping(
                    name, src, col, 1.0, "accepted", "direct", matcher._options(f, profiles[col]),
                    ["chosen by reviewer"], m.alternatives,
                )  # fmt: skip
                m = mappings[name]
        if "status" in change:
            status = change["status"]
            if status not in {"accepted", "rejected", "auto", "review"}:
                raise engine.MappingError(f"unknown status '{status}'")
            if status == "accepted" and m.source is None:
                raise engine.MappingError(f"'{name}' has no source column to accept")
            m.status = status
        if "options" in change:
            for key, value in change["options"].items():
                if key == "date_order" and value in ("dmy", "mdy"):
                    m.options[key] = value
                elif key == "number_style" and value in ("eu", "us"):
                    m.options[key] = value
                else:
                    raise engine.MappingError(f"unknown option {key}={value}")
    proposal = matcher.Proposal(mappings, run.proposal.unused, run.proposal.sensitive)
    s.run = engine.run(
        s.filename, run.schema, as_of=s.as_of, default_country=s.country, table=run.table, proposal=proposal
    )


def download(s: Session, name: str) -> bytes:
    run = s.run
    if name == "mapped.csv":
        return report.mapped_csv(run).encode("utf-8")
    if name == "held.csv":
        return report.held_csv(run).encode("utf-8")
    if name == "review_queue.csv":
        return report.review_csv(run, s.decisions).encode("utf-8")
    if name == "mapping.json":
        return (json.dumps(engine.mapping_to_dict(run), indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if name == "report.html":
        return report.report_html(run).encode("utf-8")
    raise KeyError(name)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _page() -> bytes:
    return resources.files("onboarding_mapper").joinpath("ui.html").read_bytes()


class Handler(BaseHTTPRequestHandler):
    server_version = f"onboarding-mapper/{__version__}"

    def log_message(self, fmt: str, *args) -> None:  # quiet by default
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    # -- helpers ----------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: dict) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, code: int, message: str) -> None:
        self._json(code, {"error": message})

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            raise ValueError(f"file is larger than {MAX_UPLOAD // (1024 * 1024)} MB")
        return self.rfile.read(length) if length else b""

    def _session(self, sid: str) -> Session:
        if sid not in SESSIONS:
            raise LookupError("session expired; load the file again")
        return SESSIONS[sid]

    def _same_origin(self) -> bool:
        """Refuse writes from other web pages (a page elsewhere cannot drive this local server)."""
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        return origin is None or urlsplit(origin).netloc == host

    # -- routes -----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        url = urlsplit(self.path)
        parts = [unquote(p) for p in url.path.strip("/").split("/") if p]
        try:
            if not parts:
                self._send(200, _page(), "text/html; charset=utf-8")
            elif parts == ["api", "samples"]:
                self._json(
                    200,
                    {
                        "samples": [
                            {"name": n, "file": g().filename, "description": g().description}
                            for n, g in _SAMPLES.items()
                        ]
                    },
                )
            elif parts == ["api", "schemas"]:
                self._json(200, {"schemas": schema.builtin_names()})
            elif len(parts) == 5 and parts[:2] == ["api", "session"] and parts[3] == "download":
                s = self._session(parts[2])
                name = parts[4]
                if name not in DOWNLOADS:
                    raise KeyError(name)
                with s.lock:
                    body = download(s, name)
                stem = s.filename.rsplit(".", 1)[0]
                self._send(200, body, DOWNLOADS[name] + "; charset=utf-8",
                           {"Content-Disposition": f'attachment; filename="{stem}_{name}"'})  # fmt: skip
            elif len(parts) == 3 and parts[:2] == ["api", "session"]:
                s = self._session(parts[2])
                self._json(200, view(parts[2], s))
            else:
                self._error(404, "not found")
        except LookupError as exc:
            self._error(404, str(exc.args[0]) if exc.args else "not found")

    def do_POST(self) -> None:  # noqa: N802
        if not self._same_origin():
            self._error(403, "cross-origin request refused")
            return
        url = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        parts = [unquote(p) for p in url.path.strip("/").split("/") if p]
        try:
            if parts == ["api", "run"]:
                as_of = date.fromisoformat(q["as_of"]) if q.get("as_of") else date.today()
                country = (q.get("country") or "").upper() or None
                if q.get("sample"):
                    if q["sample"] not in _SAMPLES:
                        raise LookupError("unknown sample")
                    fx = _SAMPLES[q["sample"]]()
                    data, filename = fx.data, fx.filename
                    country = country or fx.default_country
                    as_of = synth.AS_OF if not q.get("as_of") else as_of
                else:
                    data = self._body()
                    filename = unquote(self.headers.get("X-Filename") or "export.csv").replace("/", "_")[:200]
                    if not data:
                        raise ValueError("no file received")
                sid, s = start(data, filename, q.get("schema") or "employee", country, as_of)
                self._json(200, view(sid, s))
            elif len(parts) == 4 and parts[:2] == ["api", "session"] and parts[3] == "mapping":
                s = self._session(parts[2])
                payload = json.loads(self._body() or b"{}")
                with s.lock:
                    edit(s, payload.get("edits", {}))
                    self._json(200, view(parts[2], s))
            elif len(parts) == 4 and parts[:2] == ["api", "session"] and parts[3] == "decision":
                s = self._session(parts[2])
                payload = json.loads(self._body() or b"{}")
                decision = payload.get("decision", "")
                if decision not in ("", "accept", "fix", "ignore"):
                    raise ValueError("decision must be accept, fix or ignore")
                with s.lock:
                    if decision:
                        s.decisions[str(payload.get("key", ""))] = decision
                    else:
                        s.decisions.pop(str(payload.get("key", "")), None)
                self._json(200, {"ok": True, "decided": len(s.decisions)})
            else:
                self._error(404, "not found")
        except LookupError as exc:
            self._error(404, str(exc.args[0]) if exc.args else "not found")
        except (ingest.IngestError, schema.SchemaError, engine.MappingError, ValueError, json.JSONDecodeError) as exc:
            self._error(HTTPStatus.UNPROCESSABLE_ENTITY, str(exc))


def make_server(host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    httpd = make_server(host, port)
    url = f"http://{host}:{httpd.server_address[1]}/"
    print(f"onboarding-mapper panel running at {url}  (Ctrl+C to stop)")
    print("Files you load are processed on this computer only.")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
