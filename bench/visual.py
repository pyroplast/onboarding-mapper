"""One HTML page to judge the mapper by eye against ground truth.

    python bench/visual.py [--out docs/ground_truth.html]

For each synthetic export it shows the raw file as it arrives, the proposed
mapping (right or wrong against the truth), and every output cell with the
source value under it, coloured:

    green   output equals the truth and the tool changed the value
    plain   output equals the truth, nothing to change
    amber   output equals the truth, but the cell is in the review queue
    red     output differs from the truth
    grey    row held back from import (error), with the reason on hover

All data is synthetic, so values are shown unmasked.
"""

from __future__ import annotations

import argparse
import sys
from html import escape as e
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from onboarding_mapper import engine, ingest, schema, synth  # noqa: E402
from onboarding_mapper.normalize import clean_cell, is_null  # noqa: E402

CSS = """
:root{--bg:#f5f4f0;--card:#fff;--ink:#1c1d1f;--muted:#6a6d72;--line:#e2e0da;--code:#f0eee8;
--ok:#0b6b43;--ok-bg:#dff2e6;--warn:#8f4f07;--warn-bg:#fcecd0;--err:#b42318;--err-bg:#fde3e0;--held:#ecebe7;--acc:#2f5d8a}
@media (prefers-color-scheme:dark){:root{--bg:#141516;--card:#1c1e20;--ink:#eceae5;--muted:#a3a6ab;--line:#303236;--code:#26282b;
--ok:#72d3a1;--ok-bg:#133222;--warn:#f1b65f;--warn-bg:#3a2a10;--err:#ff8b7c;--err-bg:#3d1d19;--held:#25272a;--acc:#8db8e6}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:1400px;margin:0 auto;padding:28px 20px 60px}
h1{margin:0 0 4px;font-size:26px}p.sub{margin:0 0 18px;color:var(--muted)}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 18px}
.tabs button{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--ink);padding:8px 14px;border-radius:999px;cursor:pointer}
.tabs button[aria-selected=true]{background:var(--ink);color:var(--bg);border-color:var(--ink)}
.tabs small{opacity:.7;margin-left:6px}
section.fx{display:none}section.fx.on{display:block}
.desc{color:var(--muted);margin:0 0 14px;max-width:900px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:0 0 18px}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.tile b{display:block;font-size:24px;font-variant-numeric:tabular-nums}.tile span{font-size:12px;color:var(--muted)}
.tile.ok b{color:var(--ok)}.tile.bad b{color:var(--err)}.tile.warn b{color:var(--warn)}
h2{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin:22px 0 8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:auto;max-height:560px}
pre.raw{margin:0;padding:12px 14px;font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre;max-height:260px}
table{border-collapse:separate;border-spacing:0;font-size:12.5px;width:max-content;min-width:100%}
th,td{padding:6px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;white-space:nowrap}
thead th{position:sticky;top:0;background:var(--card);z-index:2;font-weight:600;color:var(--muted);font-size:11.5px}
th.rowh,td.rowh{position:sticky;left:0;background:var(--card);z-index:1;color:var(--muted);font-variant-numeric:tabular-nums}
thead th.rowh{z-index:3}
td .o{display:block;font-weight:600}td .s{display:block;color:var(--muted);font-size:11px;font-family:ui-monospace,Menlo,monospace}
td.chg{background:var(--ok-bg)}td.chg .o{color:var(--ok)}
td.rev{background:var(--warn-bg)}td.rev .o{color:var(--warn)}
td.bad{background:var(--err-bg)}td.bad .o{color:var(--err)}
tr.held td{background:var(--held)}tr.held td.rowh{color:var(--err);font-weight:700}
tr.held td.bad{background:var(--err-bg)}
.map td,.map th{white-space:normal}
.pill{display:inline-block;font-size:10.5px;font-weight:700;padding:1px 7px;border-radius:999px}
.pill.auto{color:var(--ok);background:var(--ok-bg)}.pill.review{color:var(--warn);background:var(--warn-bg)}.pill.unmapped{color:var(--muted);background:var(--held)}
.y{color:var(--ok);font-weight:700}.n{color:var(--err);font-weight:700}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin:6px 0 10px}
.legend i{display:inline-block;width:12px;height:12px;border-radius:3px;vertical-align:-2px;margin-right:5px;border:1px solid var(--line)}
.why{color:var(--muted);font-size:12px}
"""


def _table(fx: synth.Fixture) -> ingest.Table:
    if fx.filename.endswith(".xlsx"):
        return ingest.read_xlsx_bytes(fx.data, fx.filename)
    return ingest.read_csv_bytes(fx.data, fx.filename)


def _raw_preview(fx: synth.Fixture, table: ingest.Table) -> str:
    if fx.filename.endswith(".xlsx"):
        lines = ["(Excel workbook, shown as read; date cells come out as ISO dates)"]
        lines.append(" | ".join(table.headers))
        lines += [" | ".join(r) for r in table.rows[:8]]
        return "\n".join(lines)
    text = fx.data.decode("cp1252" if table.encoding == "cp1252" else "utf-8-sig", "replace")
    return "\n".join(text.splitlines()[:12])


def fixture_section(fx: synth.Fixture, sch: schema.Schema, held_out: bool) -> tuple[str, str]:
    table = _table(fx)
    run = engine.run(fx.filename, sch, as_of=synth.AS_OF, table=table, default_country=fx.default_country)
    res = run.result
    issues_by_cell: dict[tuple[int, str], list] = {}
    for i in res.issues:
        if i.record is not None and i.severity != "info":
            issues_by_cell.setdefault((i.record, i.field), []).append(i)
    errors = res.errors_by_record()

    fields = [f for f, src in fx.mapping.items() if src is not None]
    right = sum(
        1 for f in sch.field_names if (run.mappings[f].source if run.mappings[f].active else None) == fx.mapping[f]
    )
    cells = ok = changed = 0
    body = []
    for i, (got, want) in enumerate(zip(res.records, fx.records)):
        held = i in errors
        reason = "; ".join(f"{x.field}: {x.message}" for x in errors.get(i, []))
        tr = [
            f"<tr class='{'held' if held else ''}' title='{e(reason)}'><td class='rowh'>{res.rows[i]}{' ✕' if held else ''}</td>"
        ]
        for f in fields:
            m = run.mappings[f]
            raw = table.rows[i][m.column] if m.active and m.column is not None else ""
            out = got[f]
            cells += 1
            good = out == want[f]
            ok += good
            cell_issues = issues_by_cell.get((i, f), [])
            norm_changed = bool(out) and out != clean_cell(raw)
            changed += good and norm_changed
            cls = "bad" if not good else ("rev" if cell_issues else ("chg" if norm_changed else ""))
            tip = []
            if not good:
                tip.append(f"expected: {want[f] or '(empty)'}")
            tip += [f"{x.code}: {x.message}" for x in cell_issues]
            src = "" if is_null(raw) else raw
            show_src = src and src != out
            tr.append(
                f"<td class='{cls}' title='{e(' · '.join(tip))}'><span class='o'>{e(out) or '&nbsp;'}</span>"
                + (f"<span class='s'>{e(src)}</span>" if show_src else "")
                + "</td>"
            )
        tr.append("</tr>")
        body.append("".join(tr))

    s = run.stats()
    head = "".join(f"<th>{e(f)}<br><span class='why'>{e(run.mappings[f].source or '—')}</span></th>" for f in fields)
    map_rows = []
    for f in sch.field_names:
        m = run.mappings[f]
        got = m.source if m.active else None
        want = fx.mapping[f]
        mark = "<span class='y'>✓</span>" if got == want else "<span class='n'>✕</span>"
        how = "" if m.method == "direct" else f" <span class='why'>({m.method.replace('_', ' ')})</span>"
        map_rows.append(
            f"<tr><td>{mark}</td><td><code>{e(f)}</code></td><td>{e(got or '—')}{how}</td><td>{e(want or '—')}</td>"
            f"<td><span class='pill {m.status}'>{m.status.upper()}</span></td><td>{m.confidence:.2f}</td>"
            f"<td class='why'>{e('; '.join(m.reasons))}</td></tr>"
        )
    held_back = ", ".join(h["source"] for h in run.proposal.sensitive) or "none"
    review = "".join(
        f"<tr><td>{r.id}</td><td>{e(r.severity)}</td><td><code>{e(r.field or r.source)}</code></td><td>{r.row or ''}</td>"
        f"<td>{e(r.value)}</td><td style='white-space:normal'>{e(r.problem)}</td></tr>"
        for r in run.review
    )
    tag = " (held out)" if held_out else ""
    html = f"""
<section class='fx' id='{fx.name}'>
<p class='desc'><b>{e(fx.filename)}{tag}.</b> {e(fx.description)}</p>
<div class='tiles'>
<div class='tile {"ok" if right == len(sch.field_names) else "bad"}'><b>{right}/{len(sch.field_names)}</b><span>fields mapped to the right column</span></div>
<div class='tile {"ok" if ok == cells else "bad"}'><b>{ok}/{cells}</b><span>output cells equal to the truth</span></div>
<div class='tile ok'><b>{changed}</b><span>of those were rewritten by the tool</span></div>
<div class='tile {"warn" if s["records_held"] else "ok"}'><b>{s["records_held"]}</b><span>rows held back (grey)</span></div>
<div class='tile {"warn" if s["review_items"] else "ok"}'><b>{s["review_items"]}</b><span>items for a person to review</span></div>
<div class='tile'><b style='font-size:15px;line-height:1.6'>{e(held_back)}</b><span>sensitive columns not imported</span></div>
</div>
<h2>1 · The file as it arrives</h2>
<div class='card'><pre class='raw'>{e(_raw_preview(fx, table))}</pre></div>
<h2>2 · Proposed mapping vs the right answer</h2>
<div class='card map'><table><thead><tr><th></th><th>Target field</th><th>Tool picked</th><th>Correct column</th><th>Status</th><th>Conf.</th><th>Why</th></tr></thead><tbody>{"".join(map_rows)}</tbody></table></div>
<h2>3 · Every output cell (source value underneath when it changed)</h2>
<div class='legend'><span><i style='background:var(--ok-bg)'></i>rewritten correctly</span><span><i style='background:var(--card)'></i>already correct</span><span><i style='background:var(--warn-bg)'></i>correct, but flagged for review</span><span><i style='background:var(--err-bg)'></i>differs from truth</span><span><i style='background:var(--held)'></i>row held back (hover for why)</span></div>
<div class='card'><table><thead><tr><th class='rowh'>Line</th>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>
<h2>4 · Review queue</h2>
<div class='card map'><table><thead><tr><th>ID</th><th>Severity</th><th>Field</th><th>Line</th><th>Value</th><th>Problem</th></tr></thead><tbody>{review}</tbody></table></div>
</section>"""
    label = f"{fx.filename}{tag}<small>{ok}/{cells}</small>"
    return label, html


def build() -> str:
    sch = schema.load("employee")
    fixtures = [(g(), False) for g in synth.ALL] + [(g(), True) for g in synth.HOLDOUT]
    parts = [fixture_section(fx, sch, h) for fx, h in fixtures]
    tabs = "".join(
        f"<button role='tab' data-t='{fx.name}' aria-selected='{str(i == 0).lower()}'>{label}</button>"
        for i, ((fx, _), (label, _)) in enumerate(zip(fixtures, parts))
    )
    sections = "".join(html for _, html in parts).replace("class='fx' id", "class='fx on' id", 1)
    script = """
document.querySelectorAll('.tabs button').forEach(b=>b.addEventListener('click',()=>{
document.querySelectorAll('.tabs button').forEach(x=>x.setAttribute('aria-selected','false'));
b.setAttribute('aria-selected','true');
document.querySelectorAll('section.fx').forEach(s=>s.classList.toggle('on',s.id===b.dataset.t));}));"""
    return (
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>onboarding-mapper · output vs ground truth</title><style>{CSS}</style></head><body><main>"
        "<h1>onboarding-mapper: output vs ground truth</h1>"
        "<p class='sub'>Five synthetic HR exports with a known right answer for every cell. Pick a file; hover a cell for the expected value or the issue raised. "
        "All names, emails, IBANs and IDs are generated.</p>"
        f"<div class='tabs' role='tablist'>{tabs}</div>{sections}<script>{script}</script></main></body></html>\n"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="docs/ground_truth.html")
    args = ap.parse_args()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build(), encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
