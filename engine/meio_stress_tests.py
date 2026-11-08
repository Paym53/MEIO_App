#!/usr/bin/env python3
"""
================================================================================
 MEIO stress tests — how well does the Seeker model handle big changes in the data?
================================================================================

WHAT IT DOES
  Runs the model in meio_seeker_stochastic.py (unchanged, imported from MODEL_FILE) on a list of test
  cases. Each case changes the data in one way (lead times, demand, costs, shelf life, stock-outs,
  fill-rate targets, freshness requirements), then:
    1. builds the start policy, 2. lets Seeker optimize (short time limit, few scenarios),
    3. checks the plan on fresh scenarios (out-of-sample), 4. compares it with the baseline.

HOW TO JUDGE THE SOLVER
  Per case (sheet Results):
    - In-sample rules met     : does Seeker find a plan that meets every fill-rate rule?
    - Saving vs. start policy : does the search improve on the formula start?
    - Out-of-sample           : cost and rules met on fresh demand (is the plan robust?)
  Per case vs. baseline (sheet Checks): does the plan react in the direction supply-chain logic expects?
    e.g. more demand → higher cost, higher holding cost → less stock, higher fixed production cost
    → fewer production orders. A second baseline with other random demand measures the noise; a change
    smaller than the noise is reported as "no clear change", a change against expectation as "CHECK".

HOW TO RUN (Spyder: open this file, F5)
  Keep this file next to your model file and the Excel template; set MODEL_FILE below to the model file
  name (e.g. "meio_seeker_stochastic (10).py"). The model file's LICENSE_FILE and DATA_FILE are used.
  Runtime ≈ 1.5–2 min per case with the defaults; all 26 cases ≈ 45 min. Use CASES_TO_RUN for a subset.

NOTE ON 60 SCENARIOS
  With 60 scenarios and the 99% chance level, the rule allows 0 misses (⌊1% × 60⌋ = 0): every one of the
  60 futures must meet the fill-rate target. That is stricter than with 200 scenarios (2 misses).
================================================================================
"""
import copy
import importlib.util
import math
import sys
import time
import traceback
from pathlib import Path

import numpy as np
import pandas as pd

# ============================================================================
# 0. SETTINGS — edit here
# ============================================================================
MODEL_FILE = "meio_seeker_stochastic.py"     # <<< your model file (same folder or full path)
TIME_LIMIT_SECONDS = 60                      # <<< Seeker search time per case
SCENARIOS = 60                               # <<< scenarios Seeker optimizes on per case
OUT_OF_SAMPLE_SCENARIOS = 1000               # fresh scenarios for the out-of-sample check per case
CASES_TO_RUN = None                          # None = all; or e.g. ["D1", "F2", "O1"] (baselines always run)
OUTPUT_FILE = "MEIO_Stress_Tests.xlsx"
NOISE_FLOOR = 0.005                          # changes below max(0.5%, baseline noise) count as "no clear change"


# ============================================================================
# 1. LOAD THE MODEL FILE (all rules come from there; nothing is re-implemented here)
# ============================================================================
def load_model(path):
    p = Path(path)
    if not p.exists():
        sys.exit(f"Model file not found: {p.resolve()}  (set MODEL_FILE at the top of this file)")
    spec = importlib.util.spec_from_file_location("meio_model", p)
    ms = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ms)                  # defines everything; main() only runs under __main__
    return ms


# ============================================================================
# 2. DATA CHANGES (helpers used by the test cases)
# ============================================================================
def rm(d, rid):
    return next(k for k in d.rms if k.id == rid)


def channel(d, cid, target=None, min_remaining=None):
    """Change a channel; keeps the serving priority rule (highest target first, H02) and the demand rows aligned."""
    for c in d.channels:
        if c.id == cid:
            c.target = c.target if target is None else target
            c.min_remaining = c.min_remaining if min_remaining is None else min_remaining
    order = sorted(range(len(d.channels)), key=lambda i: -d.channels[i].target)      # stable
    d.channels = [d.channels[i] for i in order]
    d.demand_q = d.demand_q[order]


def lead_time(ms, values, probs):
    return ms.LeadTime(np.array(values), np.array(probs, dtype=float))


def scale_demand(d, factor, channel_id=None, weeks=None):
    ci = [i for i, c in enumerate(d.channels) if channel_id in (None, c.id)]
    tt = list(weeks) if weeks else list(range(1, d.horizon + 1))
    for i in ci:
        d.demand_q[i, tt, :] *= factor


def widen_demand(d, factor):
    """Spread of every forecast around its median × factor (median unchanged, quantiles ≥ 0)."""
    med = d.demand_q[:, :, 4:6].mean(2, keepdims=True)
    d.demand_q = np.maximum(0.0, med + factor * (d.demand_q - med))


def fg_shelf_life(d, weeks):
    d.fg_shelf_life = weeks
    d.init_fg = [(min(a, weeks), q) for a, q in d.init_fg]


def rm_shelf_life(d, rid, weeks):
    k = rm(d, rid)
    k.shelf_life = weeks
    d.init_rm[rid] = [(None if a is None else min(a, weeks), q) for a, q in d.init_rm[rid]]


def scale_costs(d, holding=1.0, waste=1.0):
    d.fg_holding *= holding
    d.fg_waste *= waste
    for k in d.rms:
        k.holding *= holding
        k.waste *= waste


# ============================================================================
# 3. TEST CASES — (id, group, change, function(ms, d), expectations [(metric, "up"/"down"), ...])
# ============================================================================
#   metrics: oos_cost (expected cost on fresh scenarios), fg_stock (average FG stock at the DC),
#            waste_units (FG cans + perishable RM kg scrapped), prod_orders (expected production orders)
#   ("feasible", "no"): the case is impossible on purpose; the right answer is "no plan meets all rules"
CASES = [
    ("BASE", "Baseline", "Data as in the Excel", lambda ms, d: None, []),
    ("BASE2", "Baseline", "Same data, other random demand (measures noise)", lambda ms, d: setattr(d, "seed", d.seed + 1000), []),
    # --- lead times
    ("L1", "Lead time", "Strawberry (RM3) lead time 8 → 12 weeks",
     lambda ms, d: setattr(rm(d, "RM3"), "lead_time", lead_time(ms, [12], [1])), [("oos_cost", "up")]),
    ("L2", "Lead time", "Production lead time 6 → 8 weeks",
     lambda ms, d: setattr(d, "prod_lead_time", lead_time(ms, [8], [1])), [("oos_cost", "up")]),
    ("L3", "Lead time", "All lead times uncertain (Excel *_STOCH: +2/+4 weeks in 10–20% of orders)",
     lambda ms, d: [setattr(d, "prod_lead_time", lead_time(ms, [6, 8], [0.8, 0.2]))] +
                   [setattr(rm(d, i), "lead_time", lead_time(ms, v, [0.9, 0.1]))
                    for i, v in (("RM1", [4, 6]), ("RM2", [4, 6]), ("RM3", [8, 12]), ("RM4", [12, 16]))],
     [("oos_cost", "up")]),
    # --- demand
    ("D1", "Demand", "All demand +30%", lambda ms, d: scale_demand(d, 1.3), [("oos_cost", "up")]),
    ("D2", "Demand", "All demand −30%", lambda ms, d: scale_demand(d, 0.7), [("oos_cost", "down")]),
    ("D3", "Demand", "Forecast uncertainty doubled (same median)", lambda ms, d: widen_demand(d, 2.0),
     [("oos_cost", "up"), ("fg_stock", "up")]),
    ("D4", "Demand", "Extra Amazon promotion: weeks 14–15 × 3", lambda ms, d: scale_demand(d, 3.0, "AMZ", (14, 15)),
     [("oos_cost", "up")]),
    # --- costs
    ("C1", "Cost", "Holding cost × 3 (all items)", lambda ms, d: scale_costs(d, holding=3.0), [("fg_stock", "down")]),
    ("C2", "Cost", "Waste cost × 5 (all items)", lambda ms, d: scale_costs(d, waste=5.0), [("waste_units", "down")]),
    ("C3", "Cost", "Fixed cost per production order × 3", lambda ms, d: setattr(d, "prod_fixed", d.prod_fixed * 3),
     [("prod_orders", "down")]),
    ("C4", "Cost", "Strawberry (RM3) price +50%",
     lambda ms, d: setattr(rm(d, "RM3"), "var_cost", rm(d, "RM3").var_cost * 1.5), [("oos_cost", "up")]),
    # --- shelf life
    ("S1", "Shelf life", "FG shelf life 36 → 30 weeks", lambda ms, d: fg_shelf_life(d, 30), [("oos_cost", "up")]),
    ("S2", "Shelf life", "Strawberry shelf life 52 → 46 weeks (usable window 16 → 10)",
     lambda ms, d: rm_shelf_life(d, "RM3", 46), [("oos_cost", "up")]),
    # --- stock-outs and disruptions
    ("O1", "Stock-out", "DC starts with 20% of its FG stock", lambda ms, d: setattr(d, "init_fg", [(a, q * 0.2) for a, q in d.init_fg]),
     [("oos_cost", "up")]),
    ("O2", "Stock-out", "All open strawberry (RM3) orders cancelled", lambda ms, d: d.open_rm.__setitem__("RM3", []),
     [("oos_cost", "up")]),
    ("O3", "Stock-out", "Raw-material stock at the warehouse −50%",
     lambda ms, d: d.init_rm.update({k: [(a, q * 0.5) for a, q in v] for k, v in d.init_rm.items()}), [("oos_cost", "up")]),
    ("O4", "Stock-out", "Strawberry supplier capacity −25% (max order 1,000 → 750 kg per week)",
     lambda ms, d: setattr(rm(d, "RM3"), "max_order", 750.0), [("oos_cost", "up")]),
    ("O5", "Stock-out", "Impossible: strawberry capacity 500 kg/week (= 1,000 cans, below average demand)",
     lambda ms, d: setattr(rm(d, "RM3"), "max_order", 500.0), [("feasible", "no")]),
    # --- fill-rate targets
    ("F1", "Fill-rate target", "All targets +1 point (D2C 99%, B2B 96%, AMZ 91%)",
     lambda ms, d: [channel(d, c.id, target=min(1.0, round(c.target + 0.01, 4))) for c in list(d.channels)],
     [("oos_cost", "up")]),
    ("F2", "Fill-rate target", "Amazon target 90% → 97% (now served before B2B)",
     lambda ms, d: channel(d, "AMZ", target=0.97), [("oos_cost", "up")]),
    ("F3", "Fill-rate target", "Chance level 99% → 95% of scenarios", lambda ms, d: setattr(d, "chance_level", 0.95),
     [("oos_cost", "down")]),
    # --- freshness requirements
    ("R1", "Freshness rule", "Amazon and B2B need ≥ 28 weeks left (was 24)",
     lambda ms, d: [channel(d, "AMZ", min_remaining=28), channel(d, "B2B", min_remaining=28)], [("oos_cost", "up")]),
    ("R2", "Freshness rule", "D2C needs ≥ 20 weeks left (was 16; cans now scrapped below 20)",
     lambda ms, d: channel(d, "D2C", min_remaining=20), [("oos_cost", "up"), ("waste_units", "up")]),
    ("R3", "Freshness rule", "Strawberries usable only with ≥ 40 weeks left (was 36)",
     lambda ms, d: setattr(rm(d, "RM3"), "min_remaining", 40), [("oos_cost", "up")]),
]


# ============================================================================
# 4. RUN ONE CASE — start policy, Seeker search, out-of-sample check (all via the model file)
# ============================================================================
def run_case(ms, skr, base, case):
    cid, group, text, change, _ = case
    d = copy.deepcopy(base)
    change(ms, d)
    ms.validate(d)                                   # data must still be valid (checked with the Excel scenario count)
    d.n_scenarios = SCENARIOS
    layout = ms.PolicyLayout(d)
    pol0 = ms.start_policy(d)
    layout.include_start(pol0)
    x0 = layout.encode(pol0)
    ms.TIMINGS.clear()
    t0 = time.time()
    x, ins = ms.optimize(skr, d, layout, x0, ms.LICENSE_FILE, TIME_LIMIT_SECONDS)
    oos = ms.out_of_sample(skr, d, layout, x, ms.LICENSE_FILE, detail=False)   # stress checks need no report series
    return kpis(ms, d, layout, x, ins, oos, time.time() - t0)


def kpis(ms, d, layout, x, ins, oos, seconds):
    H, lvl, con = d.horizon, d.chance_level, ins["constrained"]
    shares = [oos[("fr_share", ci, t)] for ci, t in con]
    worst = min(range(len(con)), key=lambda i: shares[i])
    waste_units = sum(oos[("fg_waste", t)] for t in range(1, H + 1)) + \
        sum(oos[(k.id, "waste", t)] for k in d.rms if k.perishable for t in range(1, H + 1))
    return {
        "in_cost": ins["cost"], "in_met": ins["met"], "rules": len(con),
        "start_cost": ins["start"]["cost"], "start_met": ins["start"]["met"],
        "saving": 1 - ins["cost"] / ins["start"]["cost"] if ins["start"]["cost"] else 0.0,
        "oos_cost": oos["cost"], "oos_met": sum(s >= lvl - 1e-9 for s in shares),
        "worst_rule": f"{d.channels[con[worst][0]].id} week {con[worst][1]}: {shares[worst]:.1%}",
        "cost_per_can": oos["cost"] / max(1.0, float(d.demand_q[:, 1:, :].mean(2).sum())),
        "fg_stock": float(np.mean([oos[("fg_end", t)] for t in range(1, H + 1)])),
        "waste_units": waste_units, "prod_orders": sum(oos[("prod", "p_order", t)] for t in range(1, H + 1)),
        "evaluations": ins["evaluations"], "status": ins["status"], "seconds": seconds,
        "at_bound": ms.bound_note(layout, x),
    }


# ============================================================================
# 5. COMPARE WITH THE BASELINE
# ============================================================================
def checks(results):
    base, base2 = results.get("BASE"), results.get("BASE2")
    rows = []
    if not isinstance(base, dict):
        return rows, {}
    noise = {m: (abs(base2[m] - base[m]) / abs(base[m]) if isinstance(base2, dict) and base[m] else 0.0)
             for m in ("oos_cost", "fg_stock", "waste_units", "prod_orders")}
    for cid, _, text, _, expect in CASES:
        r = results.get(cid)
        if not isinstance(r, dict):
            continue
        for metric, direction in expect:
            if metric == "feasible":                         # impossible case: Seeker must not claim feasibility
                ok = r["in_met"] < r["rules"]
                rows.append({"Case": cid, "Change": text, "Metric": "all rules met", "Expected": "no",
                             "Baseline": base["in_met"], "Value": r["in_met"], "Change vs baseline": None,
                             "Noise tolerance": None, "Outcome": "as expected" if ok else "CHECK: claims feasible"})
                continue
            b, v = base[metric], r[metric]
            rel = (v - b) / abs(b) if b else (0.0 if v == b else math.copysign(1.0, v - b))
            tol = max(NOISE_FLOOR, noise[metric])
            if abs(rel) <= tol:
                out = "no clear change"
            elif (rel > 0) == (direction == "up"):
                out = "as expected"
            else:
                out = "CHECK: opposite direction"
            rows.append({"Case": cid, "Change": text, "Metric": metric, "Expected": direction, "Baseline": b,
                         "Value": v, "Change vs baseline": rel, "Noise tolerance": tol, "Outcome": out})
    return rows, noise


def verdict(r, chk):
    if not isinstance(r, dict):
        return "ERROR"
    if any(c["Metric"] == "all rules met" for c in chk):
        return "OK (impossible case recognised)" if chk[0]["Outcome"] == "as expected" else "CHECK"
    if r["in_met"] < r["rules"]:
        return f"NO FEASIBLE PLAN FOUND ({r['rules'] - r['in_met']} rule(s) short)"
    if any(c["Outcome"].startswith("CHECK") for c in chk):
        return "CHECK"
    if r["oos_met"] < r["rules"]:
        return f"OK (fresh scenarios: {r['rules'] - r['oos_met']} rule(s) short)"
    return "OK"


# ============================================================================
# 6. REPORT
# ============================================================================
def report(results, check_rows, noise, ms, path):
    rows = []
    for cid, group, text, _, _ in CASES:
        if cid not in results:
            continue
        r = results[cid]
        chk = [c for c in check_rows if c["Case"] == cid]
        row = {"Case": cid, "Group": group, "Change": text, "Verdict": verdict(r, chk),
               "Direction checks": "; ".join(f"{c['Metric']} {c['Change vs baseline']:+.1%} ({c['Outcome']})"
                                             if c["Change vs baseline"] is not None else f"{c['Metric']}: {c['Outcome']}"
                                             for c in chk)}
        if isinstance(r, dict):
            row.update({"In-sample cost": r["in_cost"], "In-sample rules met": f"{r['in_met']}/{r['rules']}",
                        "Start policy cost": r["start_cost"], "Start rules met": f"{r['start_met']}/{r['rules']}",
                        "Saving vs start": r["saving"], "Out-of-sample cost": r["oos_cost"],
                        "Out-of-sample rules met": f"{r['oos_met']}/{r['rules']}", "Weakest rule (fresh)": r["worst_rule"],
                        "Cost per can (fresh)": r["cost_per_can"], "Avg FG stock (fresh)": r["fg_stock"],
                        "Waste units (fresh)": r["waste_units"], "Production orders (fresh)": r["prod_orders"],
                        "Seeker evaluations": r["evaluations"], "Run time [s]": r["seconds"],
                        "Reorder points at bound": r["at_bound"]})
        else:
            row["Error"] = r
        rows.append(row)
    settings = pd.DataFrame([("Model file", MODEL_FILE), ("Model version", ms.SCRIPT_VERSION),
                             ("Time limit per case [s]", TIME_LIMIT_SECONDS), ("Scenarios (optimization)", SCENARIOS),
                             ("Fresh scenarios (out-of-sample)", OUT_OF_SAMPLE_SCENARIOS), ("Noise floor", NOISE_FLOOR)] +
                            [(f"Baseline noise: {m}", f"{v:.2%}") for m, v in noise.items()], columns=["Item", "Value"])
    sheets = {"Results": pd.DataFrame(rows), "Checks": pd.DataFrame(check_rows), "Settings": settings}
    path = ms.usable_output_path(path)
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        for name, df in sheets.items():
            df.to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            for col in ws.columns:
                ws.column_dimensions[col[0].column_letter].width = max(10, min(60, max(len(str(c.value or "")) for c in col) + 2))
            for row in ws.iter_rows(min_row=2):
                for c in row:
                    head = str(ws.cell(1, c.column).value)
                    if isinstance(c.value, float):
                        c.number_format = "0.0%" if any(k in head for k in ("Saving", "Change", "tolerance")) else "#,##0.00" if "per can" in head else "#,##0"
            ws.freeze_panes = "B2"
    return path, sheets["Results"]


# ============================================================================
# 7. MAIN
# ============================================================================
def main():
    ms = load_model(MODEL_FILE)
    print(f"MEIO stress tests on model version {ms.SCRIPT_VERSION}: {TIME_LIMIT_SECONDS} s, {SCENARIOS} scenarios, "
          f"{OUT_OF_SAMPLE_SCENARIOS:,} fresh scenarios per case", flush=True)
    if not Path(ms.LICENSE_FILE).exists():
        sys.exit(f"License file not found: {ms.LICENSE_FILE}  (set LICENSE_FILE in the model file)")
    ms.OUT_OF_SAMPLE_SCENARIOS = ms.OUT_OF_SAMPLE_BATCH = OUT_OF_SAMPLE_SCENARIOS
    base = ms.load_data(ms.DATA_FILE)
    ms.validate(base)
    skr = ms.import_seeker()
    todo = [c for c in CASES if CASES_TO_RUN is None or c[0] in ("BASE", "BASE2") or c[0] in CASES_TO_RUN]
    results, t_all = {}, time.time()
    for n, case in enumerate(todo, 1):
        print(f"\n=== Case {n}/{len(todo)}: {case[0]} — {case[2]} ===", flush=True)
        try:
            results[case[0]] = run_case(ms, skr, base, case)
            r = results[case[0]]
            print(f"--> in-sample {r['in_cost']:,.0f} ({r['in_met']}/{r['rules']} rules), saving {r['saving']:.1%}, "
                  f"fresh {r['oos_cost']:,.0f} ({r['oos_met']}/{r['rules']} rules)", flush=True)
        except Exception as e:  # noqa: BLE001 — one failing case must not stop the suite
            results[case[0]] = f"{type(e).__name__}: {e}"
            print(f"--> ERROR {results[case[0]]}\n{traceback.format_exc()}", flush=True)
    check_rows, noise = checks(results)
    path, table = report(results, check_rows, noise, ms, OUTPUT_FILE)
    print("\n" + table[["Case", "Change", "Verdict"]].to_string(index=False))
    print(f"\n{len(todo)} cases in {(time.time() - t_all) / 60:.1f} min. Results written to {Path(path).resolve()}")


if __name__ == "__main__":
    main()
