"""
================================================================================
 MEIO worker: one engine call per process (the engine keeps its settings and timings in module globals)
================================================================================
Started by api/server.py, never imported by it. This is the ONLY app file that imports the engine.

  python -u api/worker.py validate <input.xlsx> <answer.json>
  python -u api/worker.py run <run folder> <license file>          (settings in <run folder>/request.json)
  python -u api/worker.py sheets <input.xlsx> <answer.json>        (the editable sheets, for what-if editing)
  python -u api/worker.py whatif <original.xlsx> <edits.json> <copy.xlsx> <answer.json>

  validate : engine.load_data() + engine.validate()  → answer.json {"valid", "errors", "warnings", "input"}
  sheets   : the template's input sheets with Excel row numbers and which cells may be edited
  whatif   : the edits written into a COPY of the original workbook (openpyxl, values only; the original is never
             changed), then validate → answer.json {"valid", "errors", "warnings", "input", "changes"}
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
  4b. WHAT-IF ........ editable sheets; edits → a copy of the original workbook (app rules W01–W03)
  5. RESULT .......... RunResult → result.json (contract version 4 = engine contract 4, v18.1)
  6. RUN ............. the whole engine pipeline for one run folder (the PC is kept awake meanwhile)
  7. MAIN
================================================================================
"""
import contextlib
import ctypes
import datetime as dt
import json
import math
import os
import sys
import time
import traceback
from pathlib import Path

import numpy as np
import openpyxl

# ============================================================================
# 0. SETTINGS
# ============================================================================
ENGINE_DIR = Path(__file__).resolve().parent.parent / "engine"
TEST_DOUBLE_ENV = "MEIO_TEST_DOUBLE"      # "1" = use the Seeker test double from engine/test_engine.py (tests only!)
RESULT_CONTRACT_VERSION = 4               # bump on any change to the result.json layout (and to api/openapi.json)
EXIT_DONE, EXIT_FAILED, EXIT_INVALID = 0, 1, 2
# run setting (request.json) → engine module constant (CLAUDE.md §6 "Settings"); None = keep the engine's default
SETTING_CONSTANTS = {"out_of_sample_scenarios": "OUT_OF_SAMPLE_SCENARIOS",
                     "feasibility_check": "FEASIBILITY_CHECK",
                     "start_policy_out_of_sample": "START_POLICY_OUT_OF_SAMPLE"}
# The fill-rate target and the review period come only from the workbook (Items!TargetFillRate, Settings!ReviewPeriod;
# what-if edits change them): the engine's OPTIMIZATION_FILL_RATE stays None (app rule W04, user decision 13.11.).
# Windows SetThreadExecutionState flags: keep the PC awake during a run (a sleeping PC stretched a 300 s search to
# 12,100 s on 12.11.); the flags are cleared when the run ends (and by Windows when the process exits)
ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
RAW_TABLES = ("Locations", "Lanes", "Items", "Sourcing", "Model_Assumptions")   # template sheets shown as they are
EXCEL_ONLY_COLUMNS = ["RowCheck", "EffectiveMaxOrderQty"]   # Excel formulas (empty in edited copies); engine values
                                                            # are in raw_materials[] (max_order)
# What-if editing (API update 2). Every input sheet of the template has its headers in row 4 and data from row 5.
# Key columns identify a row (shown in the change list) and are not editable: renaming would break the links between
# sheets. Formula cells (RowCheck, MeanApprox, FreshLotSales, WeeksLate, …) are calculated by Excel: never editable.
HEADER_ROW = 4
EDIT_SHEETS = {"Settings": ["Parameter"], "Items": ["ItemID"], "Channels": ["ChannelID"],
               "Demand": ["ChannelID", "Period"], "Init_Inventory": ["LocationID", "ItemID"],
               "Open_Orders": ["OrderID"],
               "Sourcing": ["ItemID"], "LeadTimeDist": ["DistID"], "Production": ["ItemID"],
               "BOM": ["ParentItemID", "ComponentItemID"], "Costs": ["ItemID", "LocationID"],
               "CostTiers": ["TierTableID", "TierNo"], "Lanes": ["LaneID"], "Locations": ["LocationID"]}
# Settings: only these rows (column Value) are master data. Solver settings (NumScenarios, RandomSeed,
# SolverTimeLimit) are set per run in the Run dialog, and the model-structure texts (PolicyType, IssuingRule, …) are
# fixed by the engine: one place per input (user decision 13.11.).
SETTINGS_EDITABLE = ("PlanningStartDate", "HorizonPeriods", "ReviewPeriod")
DEMAND_QUANTILES = ("Q05", "Q15", "Q25", "Q35", "Q45", "Q55", "Q65", "Q75", "Q85", "Q95")


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


def excel_target(d):
    """The product's fill-rate target as entered in Items!TargetFillRate (the engine keeps it apart from a run's own
    target, K08; right after load_data() only fill_rate_target is set)."""
    excel = getattr(d, "fill_rate_target_excel", None)
    return d.fill_rate_target if excel is None else excel


def describe_input(engine, d, path):
    unit = d.uom or {}
    with engine.pd.ExcelFile(str(path)) as xls:                    # the engine's own sheet reader (_sheet)
        raw = {s: table(engine._sheet(xls, s).drop(columns=EXCEL_ONLY_COLUMNS, errors="ignore")) for s in RAW_TABLES}
    return {
        "settings": {"horizon_weeks": d.horizon, "review_period_weeks": d.review_period,
                     "start_date": d.start_date.date().isoformat(),
                     "scenarios": d.n_scenarios, "random_seed": d.seed, "time_limit_seconds": cell(d.time_limit),
                     "rules_from_first_controllable_week": d.enforce_from_first_controllable},
        "product": {"id": d.fg_id, "unit": unit.get(d.fg_id), "shelf_life_weeks": d.fg_shelf_life,
                    "fill_rate_target": cell(excel_target(d)),                 # Items!TargetFillRate (H04)
                    "holding_cost": cell(d.fg_holding), "waste_cost": cell(d.fg_waste),
                    "production": {"moq": cell(d.prod_moq), "batch": cell(d.prod_batch), "max": cell(d.prod_max),
                                   "lead_time": lead_time(d.prod_lead_time), "fixed_cost": cell(d.prod_fixed),
                                   "cost_tiers": tiers(d.prod_tiers), "transport_tiers": tiers(d.transport_tiers)}},
        "raw_materials": [raw_material(k, unit.get(k.id)) for k in d.rms],
        "channels": [{"id": c.id, "serving_order": int(c.priority),          # Channels!ShortagePriority (H02)
                      "min_remaining_shelf_life": c.min_remaining} for c in d.channels],
        "demand": {"quantile_levels": [cell(x) for x in d.levels],          # weeks 1..H, one list per channel
                   "quantiles": {c.id: [[cell(q) for q in d.demand_q[ci, t]] for t in range(1, d.horizon + 1)]
                                 for ci, c in enumerate(d.channels)}},
        "initial_stock": {d.fg_id: lots(d.init_fg), **{k: lots(v) for k, v in d.init_rm.items()}},
        "open_orders": {"production": orders(d.open_prod), **{k: orders(v) for k, v in d.open_rm.items()}},
        "warnings": list(d.warnings or []),
        "tables": raw,
    }


# ============================================================================
# 4b. WHAT-IF — the editable sheets and edits into a copy of the original workbook (app rules W01–W03)
# ============================================================================
class EditError(ValueError):
    """An edit that addresses a cell the app does not let anyone change (the engine checks the values themselves)."""


def headers(ws):
    return [ws.cell(HEADER_ROW, c).value for c in range(1, ws.max_column + 1)]


def data_rows(ws, keys):
    """Excel row numbers of the data rows: rows below the header whose key cells are filled."""
    cols = [headers(ws).index(k) + 1 for k in keys]
    return [r for r in range(HEADER_ROW + 1, ws.max_row + 1) if all(ws.cell(r, c).value is not None for c in cols)]


def is_formula(c):
    return c.data_type == "f" or (isinstance(c.value, str) and c.value.startswith("="))


def editable(ws, sheet, row, column):
    """May the cell (row, column name) be edited? Formula cells and key columns never; Settings: SETTINGS_EDITABLE."""
    names = headers(ws)
    if column not in names or column in EDIT_SHEETS[sheet] or column in EXCEL_ONLY_COLUMNS:
        return False
    if sheet == "Settings" and (column != "Value" or ws.cell(row, 1).value not in SETTINGS_EDITABLE):
        return False
    return not is_formula(ws.cell(row, names.index(column) + 1))


def row_key(ws, sheet, row):
    names = headers(ws)
    return " · ".join(str(cell(ws.cell(row, names.index(k) + 1).value)) for k in EDIT_SHEETS[sheet])


def describe_sheets(path):
    """The editable sheets: columns, key columns and per data row its Excel row number, key, values and editable flags.
    Formula cells show the value Excel last calculated (empty in a copy saved by the app)."""
    wb, values = openpyxl.load_workbook(path), openpyxl.load_workbook(path, data_only=True)
    out = {}
    for sheet, keys in EDIT_SHEETS.items():
        ws, wv = wb[sheet], values[sheet]
        names = headers(ws)
        out[sheet] = {"columns": [str(n) for n in names], "key_columns": keys, "rows": [
            {"row": r, "key": row_key(ws, sheet, r),
             "values": [cell(wv.cell(r, c).value) for c in range(1, len(names) + 1)],
             "editable": [editable(ws, sheet, r, n) for n in names]} for r in data_rows(ws, keys)]}
    return {"sheets": out}


def change(ws, sheet, row, column, new):
    c = ws.cell(row, headers(ws).index(column) + 1)
    old = cell(c.value)
    c.value = new
    return {"sheet": sheet, "row": row, "key": row_key(ws, sheet, row), "column": column, "old": old, "new": cell(new)}


def check_target(wb, edit):
    """The sheet and row an edit points at, or EditError with a plain message."""
    sheet, row = edit.get("sheet"), edit.get("row")
    if sheet not in EDIT_SHEETS:
        raise EditError(f"{sheet}: this sheet cannot be edited in the app.")
    ws = wb[sheet]
    if row not in data_rows(ws, EDIT_SHEETS[sheet]):
        raise EditError(f"{sheet} row {row}: there is no data row with this number.")
    return ws, sheet, row


def apply_edit(wb, edit):
    """One edit → its changes. {"shift": x} on a Demand row adds x units to all 10 quantiles (W03); otherwise
    {"column", "value"} sets one input cell. The values themselves are checked by the engine afterwards."""
    ws, sheet, row = check_target(wb, edit)
    if edit.get("shift") is not None:
        if sheet != "Demand":
            raise EditError(f"{sheet} row {row}: a shift is only possible on Demand rows.")
        names = headers(ws)
        return [change(ws, sheet, row, q, ws.cell(row, names.index(q) + 1).value + edit["shift"])
                for q in DEMAND_QUANTILES]
    column = edit.get("column")
    if not editable(ws, sheet, row, column):
        raise EditError(f"{sheet} row {row}: {column!r} cannot be edited (a key, a formula calculated by Excel, "
                        "or a setting that is set elsewhere).")
    return [change(ws, sheet, row, column, edit.get("value"))]


def merged(changes):
    """One entry per cell: the original value and the last new value (an edited cell may be edited again)."""
    out = {}
    for ch in changes:
        k = (ch["sheet"], ch["row"], ch["column"])
        out[k] = {**ch, "old": out[k]["old"]} if k in out else ch
    return [ch for ch in out.values() if ch["old"] != ch["new"]]


def write_copy(original, edits, copy):
    """Writes the edits into a copy of the original (openpyxl keeps formulas; Excel recalculates them on opening, the
    engine runs every check in Python, K19). The original file is only read. Returns the change list."""
    wb = openpyxl.load_workbook(original)
    changes = []
    for edit in edits:
        changes += apply_edit(wb, edit)
    wb.save(copy)
    return merged(changes)


def whatif(engine, original, edits, copy):
    try:
        changes = write_copy(original, edits, copy)
    except EditError as e:
        return {"valid": False, "errors": [str(e)], "warnings": [], "input": None, "changes": []}
    return {**validate(engine, copy), "changes": changes}


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
    """Constrained cells the engine marks BELOW TARGET on the fresh scenarios (FillRates sheet, contract 3): the
    expected fill rate (the number rule H05 judges) and the tail columns that show how deep the bad futures go."""
    cols = {"expected_fill_rate": column(fill_rates, "Expected FR ("),
            "fully_served": column(fill_rates, "Fully served ("),
            "bad_scenario_p10": column(fill_rates, "FR in a bad scenario, P10 ("),
            "worst_5pct_average": column(fill_rates, "Worst 5% of futures, average FR (")}
    rows = fill_rates[(fill_rates["Constrained"] == "yes") & (fill_rates["Status out-of-sample"] == "BELOW TARGET")]
    return [{"channel": r["Channel"], "week": int(r["Week"]), "target": float(r["Target"]),
             **{k: float(r[c]) for k, c in cols.items()}} for _, r in rows.iterrows()]


def summary_rows(res):
    """Summary sheet as {Item: Value}. Phase rows ('Time: …') repeat; nothing here is keyed by them (handover B5)."""
    return {str(k): v for k, v in res.sheets["Summary"].values}


def start_record(ins):
    """Start check (K07): z₀ from the target, the z it was raised to, and every evaluated step."""
    z = ins.get("start_z") or {}
    steps = [{"z": cell(s.get("z")), "met": int(s["met"]), "cost": cell(s.get("cost"))} for s in z.get("steps", [])]
    return {"z0": cell(z.get("z0")), "z": cell(z.get("z")), "steps": steps}


def headline(engine, res):
    ins, oos, fr, d = res.ins, res.oos, res.sheets["FillRates"], res.data
    summary = summary_rows(res)
    waste = {k[len("Expected waste "):].split(" (")[0]: str(v) for k, v in summary.items()
             if k.startswith("Expected waste ")}                               # FG1 and each perishable RM (L10)
    return {
        "cost_out_of_sample": float(oos["cost"]),                              # the headline (CLAUDE.md §2)
        "rules_total": int(len(ins["constrained"])),
        "rules_met_out_of_sample": int((fr[fr["Constrained"] == "yes"]["Status out-of-sample"] == "OK").sum()),
        "rules_missed_out_of_sample": missed_rules(fr),
        "feasibility": str(ins["feasibility"]),                                # one line, section 4b
        "bound_note": str(engine.bound_note(res.layout, res.x)),               # "none" or which setting to raise
        "weakest_cell": str(summary.get("Weakest cell (out-of-sample)", "")),  # tail of the weakest cell (L04)
        "fill_rate_target": float(d.fill_rate_target),                         # the target used in this run (K08)
        "fill_rate_target_excel": float(excel_target(d)),                      # Items!TargetFillRate
        "fill_rate_target_source": engine.target_source(d),                    # Excel or the run's own target
        "review_period": str(summary.get("Review period (A03)", "")),          # which weeks this run commits
        "start_policy": str(summary.get("Start policy (warm start)", "")),
        "start_check": start_record(ins),
        "start_repair_used": bool((ins.get("repair") or {}).get("used")),      # K21: show as a notice, not an error
        "start_repair": str(summary.get("Start repair (section 4c, K21)", "")),
        "start_policy_before_repair": (str(summary["Start policy before repair"])
                                       if "Start policy before repair" in summary else None),
        "expected_waste": waste,
        "waste_warning": str(summary.get("Waste warning (L10)", "")),          # "none" or the warning (L10)
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
            "scenarios_in_sample": d.n_scenarios, "random_seed": d.seed,
            "fill_rate_target": float(d.fill_rate_target), "fill_rate_target_excel": float(excel_target(d)),
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


def apply_settings(engine, settings):
    """Run settings → engine constants. Returns the previous values, so restore_settings() can put them back: the
    target (OPTIMIZATION_FILL_RATE) must never leak into a later run (one run per process anyway)."""
    previous = {c: getattr(engine, c) for c in SETTING_CONSTANTS.values()}
    for key, constant in SETTING_CONSTANTS.items():
        if settings.get(key) is not None:
            setattr(engine, constant, settings[key])
    return previous


def restore_settings(engine, previous):
    for constant, value in previous.items():
        setattr(engine, constant, value)


@contextlib.contextmanager
def keep_awake():
    """Windows: the PC must not sleep while Seeker runs. Elsewhere (tests, CI on Linux) nothing happens."""
    kernel32 = getattr(getattr(ctypes, "windll", None), "kernel32", None)
    if kernel32 is not None:
        kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    try:
        yield
    finally:
        if kernel32 is not None:
            kernel32.SetThreadExecutionState(ES_CONTINUOUS)


def run(engine, skr, test_double, run_dir, license_file):
    run_dir = Path(run_dir)
    settings = json.loads((run_dir / "request.json").read_text(encoding="utf-8"))
    try:
        d = engine.load_data(str(input_workbook(run_dir)))
        write_json(run_dir / "input.json", describe_input(engine, d, input_workbook(run_dir)))
        previous = apply_settings(engine, settings)
        try:
            with keep_awake():
                res = engine.run(str(input_workbook(run_dir)), str(license_file),
                                 time_limit=float(settings["time_limit"]), skr=skr, overrides=settings.get("overrides"))
            result = build_result(engine, res, test_double, settings)      # target_source() reads the run's target
        finally:
            restore_settings(engine, previous)
    except ValueError as e:
        set_status(run_dir, state="invalid", message="The data did not pass the engine's checks.",
                   errors=error_lines(e))
        return EXIT_INVALID
    except (FileNotFoundError, ImportError) as e:
        set_status(run_dir, state="failed", message=str(e), errors=[])
        return EXIT_FAILED
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
    if len(argv) == 3 and argv[0] == "sheets":
        write_json(argv[2], describe_sheets(argv[1]))
        return EXIT_DONE
    if len(argv) == 5 and argv[0] == "whatif":
        edits = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
        write_json(argv[4], whatif(engine, argv[1], edits, argv[3]))
        return EXIT_DONE
    if len(argv) == 3 and argv[0] == "run":
        try:
            return run(engine, skr, test_double, *argv[1:])
        except Exception as e:  # noqa: BLE001 — record any crash for the user; the traceback goes to log.txt
            traceback.print_exc()
            set_status(argv[1], state="failed", message=f"Unexpected error: {type(e).__name__}: {e}", errors=[])
            return EXIT_FAILED
    print(__doc__, file=sys.stderr)
    if argv and argv[0] == "run" and len(argv) >= 2:      # an API started before an update calls the old way
        print("The API that started this worker is older than the worker (an update was pulled while it was "
              "running). Close both windows and start start_worker.bat again.", file=sys.stderr)
        set_status(argv[1], state="failed", errors=[], message="The API needs a restart after the last update: "
                   "close both windows and start start_worker.bat again.")
    return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], os.environ))
