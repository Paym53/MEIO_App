"""
================================================================================
 MEIO worker: one engine call per process (the engine keeps its settings and timings in module globals)
================================================================================
Started by api/server.py, never imported by it. This is the ONLY app file that imports the engine.

  python -u api/worker.py validate <input.xlsx> <answer.json>
  python -u api/worker.py run <run folder> <license file>          (settings in <run folder>/request.json)

  validate : engine.load_data() + engine.validate()  → answer.json {"valid", "errors", "warnings", "input"}
  run      : engine.load_data()                      → input.json   (the master data as the engine reads it)
             engine.run()                            → result.json  (the API's result contract)
                                                       results.xlsx (the engine's own workbook, engine.write_report)
                                                       status.json  (done / invalid / failed)
Everything printed (engine phase lines, Seeker progress) goes to the run's log.txt, which the server streams.

Every number in input.json and result.json is copied from the engine; nothing is recomputed (CLAUDE.md §2).

  0. SETTINGS ........ where the engine lives; the test-double switch; run settings → engine constants
  1. ENGINE .......... import the engine (and, for tests only, the Seeker test double)
  2. DATA SOURCE ..... where the input workbook comes from (Excel now; a database later)
  3. VALIDATE ........ data checks only
  4. INPUT ........... the master data as the engine read it → JSON (Master data page)
  5. RESULT .......... RunResult → result.json (contract version 2)
  6. RUN ............. the whole engine pipeline for one run folder
  7. MAIN
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
RESULT_CONTRACT_VERSION = 2               # bump on any change to the result.json layout (and to api/openapi.json)
EXIT_DONE, EXIT_FAILED, EXIT_INVALID = 0, 1, 2
# run setting (request.json) → engine module constant (CLAUDE.md §6 "Settings"); None = keep the engine's default
SETTING_CONSTANTS = {"out_of_sample_scenarios": "OUT_OF_SAMPLE_SCENARIOS",
                     "feasibility_check": "FEASIBILITY_CHECK",
                     "start_policy_out_of_sample": "START_POLICY_OUT_OF_SAMPLE",
                     "optimization_chance_level": "OPTIMIZATION_CHANCE_LEVEL"}
RAW_TABLES = ("Locations", "Lanes", "Items", "Model_Assumptions")   # template sheets shown as they are


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
        d = engine.load_data(str(path))
        engine.validate(d)
    except ValueError as e:
        return {"valid": False, "errors": error_lines(e), "warnings": [], "input": None}
    except Exception as e:  # noqa: BLE001 — any unreadable workbook is a data problem for the user
        return {"valid": False, "warnings": [], "input": None,
                "errors": [f"The workbook could not be read as the MEIO template: {type(e).__name__}: {e}"]}
    return {"valid": True, "errors": [], "warnings": list(d.warnings or []), "input": describe_input(engine, d, path)}


# ============================================================================
# 4. INPUT — the master data exactly as engine.load_data() read it (copies only)
# ============================================================================
def cell(v):
    """One value as plain JSON (numbers exact, dates ISO, missing or infinite = null)."""
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    if isinstance(v, (int, np.integer)):
        return int(v)
    if isinstance(v, (float, np.floating)):
        return float(v) if math.isfinite(v) else None
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()
    return None if v is None else str(v)


def table(df):
    return {"columns": [str(c) for c in df.columns],
            "rows": [[cell(v) for v in row] for row in df.itertuples(index=False)]}


def lead_time(lt):
    return {"weeks": [cell(v) for v in lt.values], "probabilities": [cell(p) for p in lt.probs]}


def tiers(t):
    """Cost tiers; "to": null = no upper limit."""
    if t is None:
        return None
    rows = zip(t.froms, t.tos, t.costs, strict=True)
    return {"mode": t.mode, "tiers": [{"from": cell(f), "to": cell(to), "cost_per_unit": cell(c)} for f, to, c in rows]}


def lots(pairs):
    return [{"remaining_shelf_life": cell(a), "quantity": cell(q)} for a, q in pairs]


def orders(rows):
    return [{k: cell(v) for k, v in o.items()} for o in rows]


def raw_material(k, unit):
    return {"id": k.id, "unit": unit, "bom_per_unit": cell(k.bom), "perishable": k.perishable,
            "shelf_life_weeks": cell(k.shelf_life), "min_remaining_shelf_life": cell(k.min_remaining),
            "moq": cell(k.moq), "order_multiple": cell(k.multiple), "max_order": cell(k.max_order),
            "lead_time": lead_time(k.lead_time), "order_fixed_cost": cell(k.fixed_cost),
            "order_variable_cost": cell(k.var_cost), "holding_cost": cell(k.holding), "waste_cost": cell(k.waste)}


def describe_input(engine, d, path):
    unit = d.uom or {}
    with engine.pd.ExcelFile(str(path)) as xls:                    # the engine's own sheet reader (_sheet)
        raw = {s: table(engine._sheet(xls, s).drop(columns=["RowCheck"], errors="ignore")) for s in RAW_TABLES}
    return {
        "settings": {"horizon_weeks": d.horizon, "review_period_weeks": d.review_period,
                     "start_date": d.start_date.date().isoformat(), "chance_level": cell(d.chance_level),
                     "scenarios": d.n_scenarios, "random_seed": d.seed, "time_limit_seconds": cell(d.time_limit),
                     "rules_from_first_controllable_week": d.enforce_from_first_controllable},
        "product": {"id": d.fg_id, "unit": unit.get(d.fg_id), "shelf_life_weeks": d.fg_shelf_life,
                    "holding_cost": cell(d.fg_holding), "waste_cost": cell(d.fg_waste),
                    "production": {"moq": cell(d.prod_moq), "batch": cell(d.prod_batch), "max": cell(d.prod_max),
                                   "lead_time": lead_time(d.prod_lead_time), "fixed_cost": cell(d.prod_fixed),
                                   "cost_tiers": tiers(d.prod_tiers), "transport_tiers": tiers(d.transport_tiers)}},
        "raw_materials": [raw_material(k, unit.get(k.id)) for k in d.rms],
        "channels": [{"id": c.id, "serving_order": i + 1, "target_fill_rate": cell(c.target),
                      "min_remaining_shelf_life": c.min_remaining} for i, c in enumerate(d.channels)],
        "demand": {"quantile_levels": [cell(x) for x in d.levels],          # weeks 1..H, one list per channel
                   "quantiles": {c.id: [[cell(q) for q in d.demand_q[ci, t]] for t in range(1, d.horizon + 1)]
                                 for ci, c in enumerate(d.channels)}},
        "initial_stock": {d.fg_id: lots(d.init_fg), **{k: lots(v) for k, v in d.init_rm.items()}},
        "open_orders": {"production": orders(d.open_prod), **{k: orders(v) for k, v in d.open_rm.items()}},
        "warnings": list(d.warnings or []),
        "tables": raw,
    }


# ============================================================================
# 5. RESULT — RunResult → result.json. Copies only; the comments name the engine source of every field.
# ============================================================================
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


def headline(engine, res):
    ins, oos, fr = res.ins, res.oos, res.sheets["FillRates"]
    summary = dict(res.sheets["Summary"].values)
    return {
        "cost_out_of_sample": float(oos["cost"]),                              # the headline (CLAUDE.md §2)
        "rules_total": int(len(ins["constrained"])),
        "rules_met_out_of_sample": int((fr[fr["Constrained"] == "yes"]["Status out-of-sample"] == "OK").sum()),
        "rules_missed_out_of_sample": missed_rules(fr),
        "feasibility": str(ins["feasibility"]),                                # one line, section 4b
        "bound_note": str(engine.bound_note(res.layout, res.x)),               # "none" or which setting to raise
        "cost_in_sample": float(ins["cost"]),
        "rules_met_in_sample": int(ins["met"]),
        "start_policy_cost": float(ins["start"]["cost"]),
        "saving_vs_start_in_sample": float(summary["Saving vs. start policy (in-sample)"]),
        "cost_parts_in_sample": {n: float(v) for n, v in ins["parts"].items()},
        "cost_parts_out_of_sample": {n: float(oos[("part", n)]) for n in ins["parts"]},
        "scenarios_in_sample": int(ins["n"]),
        "scenarios_out_of_sample": int(oos["n"]),
        "seeker_status": str(ins["status"]),
    }


def run_info(engine, res, settings):
    d = res.data                                                               # after the run overrides (K20)
    return {"product_id": d.fg_id, "horizon_weeks": d.horizon, "review_period_weeks": d.review_period,
            "first_rule_week": min((t for _, t in res.ins["constrained"]), default=None),
            "start_date": d.start_date.date().isoformat(), "time_limit_seconds": cell(settings["time_limit"]),
            "scenarios_in_sample": d.n_scenarios, "random_seed": d.seed, "chance_level": cell(d.chance_level),
            "optimization_chance_level": cell(engine.OPTIMIZATION_CHANCE_LEVEL or d.chance_level),
            "scenarios_out_of_sample": int(res.oos["n"]), "feasibility_check": bool(engine.FEASIBILITY_CHECK),
            "start_policy_out_of_sample": bool(engine.START_POLICY_OUT_OF_SAMPLE),
            "overrides": res.overrides, "warnings": list(d.warnings or [])}


def build_result(engine, res, test_double, settings):
    return {
        "contract_version": RESULT_CONTRACT_VERSION,
        "engine_version": engine.SCRIPT_VERSION,
        "seeker_version": str(res.ins["version"]),
        "test_double": test_double,                         # True = fake Seeker: numbers are not optimal
        "headline": headline(engine, res),
        "run_info": run_info(engine, res, settings),
        "sheets": {name: table(df) for name, df in res.sheets.items()},
        "timings": [[str(p), float(s)] for p, s in res.timings],
    }


# ============================================================================
# 6. RUN
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


def apply_settings(engine, d, settings):
    """Run settings → engine constants. The optimization chance level may never be below the Excel rule."""
    level = settings.get("optimization_chance_level")
    if level is not None and level < d.chance_level:
        raise ValueError(f"Data validation failed:\n  - Optimization chance level {level:g} is below the Excel "
                         f"FillRateChanceLevel {d.chance_level:g}; it may only be equal or higher (stricter).")
    for key, constant in SETTING_CONSTANTS.items():
        if settings.get(key) is not None:
            setattr(engine, constant, settings[key])


def run(engine, skr, test_double, run_dir, license_file):
    run_dir = Path(run_dir)
    settings = json.loads((run_dir / "request.json").read_text(encoding="utf-8"))
    try:
        d = engine.load_data(str(input_workbook(run_dir)))
        write_json(run_dir / "input.json", describe_input(engine, d, input_workbook(run_dir)))
        apply_settings(engine, d, settings)
        res = engine.run(str(input_workbook(run_dir)), str(license_file), time_limit=float(settings["time_limit"]),
                         skr=skr, overrides=settings.get("overrides"))
    except ValueError as e:
        set_status(run_dir, state="invalid", message="The data did not pass the engine's checks.",
                   errors=error_lines(e))
        return EXIT_INVALID
    except (FileNotFoundError, ImportError) as e:
        set_status(run_dir, state="failed", message=str(e), errors=[])
        return EXIT_FAILED
    result = build_result(engine, res, test_double, settings)
    write_json(run_dir / "result.json", result)
    engine.write_report(res.sheets, run_dir / "results.xlsx")
    h = result["headline"]
    summary = {k: h[k] for k in ("cost_out_of_sample", "rules_met_out_of_sample", "rules_total")}
    set_status(run_dir, state="done", message="Finished.", errors=[], summary=summary, test_double=test_double)
    return EXIT_DONE


# ============================================================================
# 7. MAIN
# ============================================================================
def main(argv, environ):
    engine, skr, test_double = load_engine(environ)
    if len(argv) == 3 and argv[0] == "validate":
        write_json(argv[2], validate(engine, argv[1]))
        return EXIT_DONE
    if len(argv) == 3 and argv[0] == "run":
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
