"""Write the synthetic exports, their ground truth, and a full run for each.

    python examples/generate.py

All data is made up. Outputs land in examples/exports, examples/truth and examples/output.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "src"))

from onboarding_mapper import engine, report, schema, synth  # noqa: E402


def main() -> int:
    sch = schema.load("employee")
    for sub in ("exports", "truth", "output"):
        shutil.rmtree(ROOT / sub, ignore_errors=True)
        (ROOT / sub).mkdir()
    for fx in [g() for g in synth.ALL + synth.HOLDOUT]:
        path = ROOT / "exports" / fx.filename
        path.write_bytes(fx.data)
        (ROOT / "truth" / f"{fx.name}.json").write_text(
            json.dumps(fx.truth(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        run = engine.run(path, sch, as_of=synth.AS_OF, default_country=fx.default_country)
        report.write_all(run, ROOT / "output" / fx.name)
        s = run.stats()
        print(
            f"{fx.filename:22} {s['records_ready']:3} ready · {s['records_held']:2} held · {s['review_items']:2} to review"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
