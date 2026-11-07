"""
================================================================================
 MEIO worker: one engine call per process (the engine keeps its settings and timings in module globals)
================================================================================
Started by api/server.py, never imported by it. This is the ONLY app file that imports the engine.

  python -u api/worker.py validate <input.xlsx> <answer.json>
  python -u api/worker.py run <run folder> <license file> <time limit [s]>

  validate : engine.load_data() + engine.validate()  → answer.json {"valid", "errors"}
  run      : engine.run()                            → result.json (the API's result contract)
                                                       results.xlsx (the engine's own workbook, engine.write_report)
                                                       status.json  (done / invalid / failed)
Everything printed (engine phase lines, Seeker progress) goes to the run's log.txt, which the server streams.

Every number in result.json is copied from the engine's RunResult; nothing is recomputed (CLAUDE.md §2).

  0. SETTINGS ........ where the engine lives; the test-double switch
  1. ENGINE .......... import the engine (and, for tests only, the Seeker test double)
  2. DATA SOURCE ..... where the input workbook comes from (Excel now; a database later)
  3. VALIDATE ........ data checks only
  4. RESULT .......... RunResult → result.json (contract version 1)
  5. RUN ............. the whole engine pipeline for one run folder
  6. MAIN
================================================================================
"""
import datetime as dt
import json
import math
import os
import sys
import time
import traceback
from pathlib import Path

import numpy as np

# ============================================================================
# 0. SETTINGS
# ============================================================================
ENGINE_DIR = Path(__file__).resolve().parent.parent / "engine"
TEST_DOUBLE_ENV = "MEIO_TEST_DOUBLE"      # "1" = use the Seeker test double from engine/test_engine.py (tests only!)
RESULT_CONTRACT_VERSION = 1               # bump on any change to the result.json layout (and to api/openapi.json)
EXIT_DONE, EXIT_FAILED, EXIT_INVALID = 0, 1, 2


# ============================================================================
# 1. ENGINE
# ============================================================================
def load_engine(environ):
    """Returns (engine module, Seeker module or None = real Seeker, test_double flag)."""
    sys.path.insert(0, str(ENGINE_DIR))
    if environ.get(TEST_DOUBLE_ENV) == "1":
        import test_engine  # installs the Seeker test double as sys.modules["seeker"]

        return test_engine.ms, test_engine.fake, True
    import meio_seeker_stochastic as engine

    return engine, None, False


# ============================================================================
# 2. DATA SOURCE — the one place where a database source will be added later (engine change request first)
# ============================================================================
def input_workbook(run_dir):
    return Path(run_dir) / "input.xlsx"


# ============================================================================
# 3. VALIDATE
# ============================================================================
def error_lines(exc):
    """The engine's messages, one problem per line ("Data validation failed:\\n  - a\\n  - b" → [a, b])."""
    lines = [ln.strip() for ln in str(exc).splitlines() if ln.strip()]
    if lines and lines[0] == "Data validation failed:":
        lines = lines[1:]
    return [ln[2:] if ln.startswith("- ") else ln for ln in lines]


def validate(engine, path):
    try:
        engine.validate(engine.load_data(str(path)))
    except ValueError as e:
        return {"valid": False, "errors": error_lines(e)}
    except Exception as e:  # noqa: BLE001 — any unreadable workbook is a data problem for the user
        return {"valid": False,
                "errors": [f"The workbook could not be read as the MEIO template: {type(e).__name__}: {e}"]}
    return {"valid": True, "errors": []}


# ============================================================================
# 4. RESULT — RunResult → result.json. Copies only; the comments name the engine source of every field.
# ============================================================================
def cell(v):
    """One table cell as plain JSON (numbers exact, dates ISO, missing = null)."""
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    if isinstance(v, (int, np.integer)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        return None if math.isnan(v) else float(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    return None if v is None else str(v)


def table(df):
    return {"columns": [str(c) for c in df.columns],
            "rows": [[cell(v) for v in row] for row in df.itertuples(index=False)]}


def column(df, prefix):
    """Report columns can carry the scenario count in their name, e.g. 'P(FR ≥ target) (2,500 fresh scenarios)'."""
    hits = [c for c in df.columns if str(c).startswith(prefix)]
    if len(hits) != 1:
        raise KeyError(f"expected one column starting with {prefix!r}, found {hits}")
    return hits[0]


def missed_rules(fill_rates):
    """Constrained rules the engine marks BELOW LEVEL on the fresh scenarios (FillRates sheet)."""
    share = column(fill_rates, "P(FR ≥ target) (")
    rows = fill_rates[(fill_rates["Constrained"] == "yes") & (fill_rates["Status out-of-sample"] == "BELOW LEVEL")]
    return [{"channel": r["Channel"], "week": int(r["Week"]), "target": float(r["Target"]), "share": float(r[share])}
            for _, r in rows.iterrows()]


def build_result(engine, res, test_double):
    ins, oos, sheets = res.ins, res.oos, res.sheets
    fr = sheets["FillRates"]
    constrained = fr[fr["Constrained"] == "yes"]
    headline = {
        "cost_out_of_sample": float(oos["cost"]),                              # the headline (CLAUDE.md §2)
        "rules_total": int(len(ins["constrained"])),
        "rules_met_out_of_sample": int((constrained["Status out-of-sample"] == "OK").sum()),
        "rules_missed_out_of_sample": missed_rules(fr),
        "feasibility": str(ins["feasibility"]),                                # one line, section 4b
        "bound_note": str(engine.bound_note(res.layout, res.x)),               # "none" or which setting to raise
        "cost_in_sample": float(ins["cost"]),
        "rules_met_in_sample": int(ins["met"]),
        "start_policy_cost": float(ins["start"]["cost"]),
        "scenarios_in_sample": int(ins["n"]),
        "scenarios_out_of_sample": int(oos["n"]),
        "seeker_status": str(ins["status"]),
    }
    return {
        "contract_version": RESULT_CONTRACT_VERSION,
        "engine_version": engine.SCRIPT_VERSION,
        "seeker_version": str(ins["version"]),
        "test_double": test_double,                         # True = fake Seeker: numbers are not optimal
        "headline": headline,
        "sheets": {name: table(df) for name, df in sheets.items()},
        "timings": [[str(p), float(s)] for p, s in res.timings],
    }


# ============================================================================
# 5. RUN
# ============================================================================
def write_json(path, obj):
    """Write via a temporary file and rename, so the server never reads a half-written file."""
    tmp = Path(path).with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, allow_nan=False, indent=1), encoding="utf-8")
    for attempt in range(20):                  # Windows refuses to replace a file another process is reading
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)


def set_status(run_dir, **fields):
    path = Path(run_dir) / "status.json"
    status = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    status.update(fields, finished=dt.datetime.now().astimezone().isoformat(timespec="seconds"))
    write_json(path, status)


def run(engine, skr, test_double, run_dir, license_file, time_limit):
    run_dir = Path(run_dir)
    try:
        res = engine.run(str(input_workbook(run_dir)), str(license_file), time_limit=float(time_limit), skr=skr)
    except ValueError as e:
        set_status(run_dir, state="invalid", message="The data did not pass the engine's checks.",
                   errors=error_lines(e))
        return EXIT_INVALID
    except (FileNotFoundError, ImportError) as e:
        set_status(run_dir, state="failed", message=str(e), errors=[])
        return EXIT_FAILED
    result = build_result(engine, res, test_double)
    write_json(run_dir / "result.json", result)
    engine.write_report(res.sheets, run_dir / "results.xlsx")
    h = result["headline"]
    summary = {k: h[k] for k in ("cost_out_of_sample", "rules_met_out_of_sample", "rules_total")}
    set_status(run_dir, state="done", message="Finished.", errors=[], summary=summary, test_double=test_double)
    return EXIT_DONE


# ============================================================================
# 6. MAIN
# ============================================================================
def main(argv, environ):
    engine, skr, test_double = load_engine(environ)
    if len(argv) == 3 and argv[0] == "validate":
        write_json(argv[2], validate(engine, argv[1]))
        return EXIT_DONE
    if len(argv) == 4 and argv[0] == "run":
        try:
            return run(engine, skr, test_double, *argv[1:])
        except Exception as e:  # noqa: BLE001 — record any crash for the user; the traceback goes to log.txt
            traceback.print_exc()
            set_status(argv[1], state="failed", message=f"Unexpected error: {type(e).__name__}: {e}", errors=[])
            return EXIT_FAILED
    print(__doc__, file=sys.stderr)
    return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], os.environ))
