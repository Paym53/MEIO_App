"""
meio_baseline.py — textbook (s,S) baseline for the MEIO engine, for comparison only
==============================================================================================

WHAT IT DOES
  1. Computes ONE fixed (s,S) per stock point (FG1 at the DC, each raw material at the RMW) with the standard
     textbook formulas (section 2), from the same Excel template the engine reads.
  2. Simulates this fixed policy on the SAME fresh hold-out scenarios as the optimized plan, with the engine's own
     out-of-sample check (same Seeker model, same seeds, same number of scenarios, same settings; section 3).
  3. Puts both plans side by side: cost, rules met, weakest cell, LOST DEMAND IN UNITS, waste, and the expected
     fill rate per channel and week (section 4), and writes MEIO_Baseline_<time>.xlsx (section 5).

WHAT IT DOES NOT DO
  It never changes the engine. It only calls existing engine functions (load_data, PolicyLayout, out_of_sample,
  cell_met, import_seeker). The engine, its tests and the app run exactly as before without this file.

THE FORMULAS (per stock point j; FG1 has BOM = 1; a raw material is checked against its echelon position, C03)
     W = L + R                    protection weeks: mean lead time to the DC (FG1: production; raw material k:
                                  its supplier lead time + production) + review period R (Settings!ReviewPeriod)
     D = BOM × μ                  average weekly demand; μ = forecast mean per week (mean of the 10 quantiles, summed
                                  over the channels), averaged over the horizon
     σ = BOM × σ_week × √W        uncertainty of the demand over W weeks; σ_week = √(average weekly variance), the
                                  weekly variance = Σ over channels ((Q95 − Q05) / 3.29)²
     Q = √(2·K·D / h)             EOQ; K = fixed cost per order (production order / raw-material order line),
                                  h = holding cost per unit and week; then the order rules: round UP to the batch /
                                  order multiple, at least the MOQ, at most the maximum order quantity
     z from G(z) = (1 − β)·Q / σ  textbook fill rate: expected shortage per order cycle = (1 − β)·Q;
                                  β = fill-rate target, G = standard normal loss function; z ≥ 0
     s = W·D + z·σ                reorder level (z·σ is the safety stock)
     S = s + Q                    order-up-to level
  s and S are rounded to the engine's decision steps and are the SAME in every week. Only in the last weeks, where
  an order can no longer arrive before the end of the horizon, they are 0 — exactly as in the optimized plan (K04).
  Ignored, as in the textbook: shelf life and freshness, capacities, price tiers, seasonality and promotions (one μ
  for the whole horizon), and the per-week fill-rate rule (the textbook fill rate is an average over all weeks).

SAME SETTINGS AS THE OPTIMIZED RUN (data edits, what-ifs, overrides, target, review period, fresh scenarios)
  - In the app: call run_baseline(optimized=res) with the engine's RunResult `res`. The baseline then uses exactly
    that run's data object (with its overrides and fill-rate target), its constrained cells, and its fresh-scenario
    batches (read from res.timings). Run it in the same worker job, right after engine.run().
  - In Spyder: set DATA_FILE and RESULTS_FILE in section 0 and press F5. The baseline re-reads the data file and takes
    the run's settings from the results workbook: RandomSeed / NumScenarios overrides, the fill-rate target used, the
    fresh-scenario batches. It stops with an error if the results workbook does not fit the data file (horizon,
    channels, review period, or the start z₀, which depends on demand and target).
  Fresh scenarios: same seeds (RandomSeed + 1, + 2, … per batch) and sizes as the optimized run. Seeker assigns the
  draws to its scenarios in its own order, so the comparison is unbiased but not paired scenario by scenario (as L08).

RUN TIME: one more out-of-sample evaluation, about as long as the optimized one (~85 s on the user's PC at 2,500).
TESTS:    test_baseline.py (no Seeker license needed).
"""
import copy
import math
import re
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import meio_seeker_stochastic as engine

# ============================================================================
# 0. SETTINGS (Spyder: set these and press F5)
# ============================================================================
DATA_FILE = "MEIO_Data_Template.xlsx"        # the data file of the optimized run
RESULTS_FILE = None                          # the optimized run's results workbook; None = newest MEIO_Results*.xlsx
LICENSE_FILE = None                          # None = the engine's LICENSE_FILE
OUTPUT_PREFIX = "MEIO_Baseline"              # output: MEIO_Baseline_<yyyymmdd-hhmmss>.xlsx
COST_PARTS = ("rm_ordering", "production", "transport", "holding", "waste")


# ============================================================================
# 1. DATA — the engine's own reader and checks
# ============================================================================
def mean_lead_time(lt):
    """Mean of a lead-time distribution (weeks)."""
    return float(np.dot(lt.values, lt.probs))


def weekly_mu_sigma(d):
    """μ = average weekly forecast mean, σ_week = √(average weekly variance), all channels together."""
    q = np.asarray(d.demand_q, float)[:, 1:, :]               # channel × week × 10 quantiles
    mu_week = q.mean(axis=2).sum(axis=0)                      # mean of the 10 quantiles, summed over channels
    var_week = (((q[:, :, -1] - q[:, :, 0]) / 3.29) ** 2).sum(axis=0)   # (Q95 − Q05)/3.29 per channel, squared
    return float(mu_week.mean()), math.sqrt(float(var_week.mean()))


# ============================================================================
# 2. TEXTBOOK FORMULAS — one fixed (s,S) per stock point
# ============================================================================
def normal_loss(z):
    """Standard normal loss function G(z) = φ(z) − z·(1 − Φ(z))."""
    pdf = math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)
    cdf = 0.5 * (1 + math.erf(z / math.sqrt(2)))
    return pdf - z * (1 - cdf)


def z_from_fill_rate(beta, Q, sigma, z_max=4.0):
    """z ≥ 0 with G(z) = (1 − β)·Q / σ (bisection; G falls from 0.399 at z = 0 to 0)."""
    if sigma <= 0:
        return 0.0
    y = (1 - beta) * Q / sigma
    if y >= normal_loss(0.0):
        return 0.0                                            # the cycle stock alone reaches the target
    lo, hi = 0.0, z_max
    if y <= normal_loss(hi):
        return hi
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        lo, hi = (mid, hi) if normal_loss(mid) > y else (lo, mid)
    return 0.5 * (lo + hi)


def apply_order_rules(q, multiple, moq, qmax):
    """Round up to the multiple, at least the MOQ, at most the maximum order quantity."""
    q = multiple * math.ceil(q / multiple - 1e-9)
    return min(qmax, max(moq, q))


def baseline_policy(d, layout=None):
    """The textbook (s,S) per stock point: policy {point: (s per week, S per week)} and the table of every value."""
    layout = layout or engine.PolicyLayout(d)
    mu, sd_week = weekly_mu_sigma(d)
    R, beta, Lp = d.review_period, d.fill_rate_target, mean_lead_time(d.prod_lead_time)
    points = [("FG", d.fg_id, 1.0, Lp, d.prod_fixed, d.fg_holding, d.prod_batch, d.prod_moq, d.prod_max)]
    points += [(k.id, k.id, k.bom, mean_lead_time(k.lead_time) + Lp, k.fixed_cost, k.holding, k.multiple, k.moq,
                k.max_order) for k in d.rms]
    weeks = np.arange(d.horizon + 1)
    policy, rows = {}, []
    for j, item, bom, L, K, h, multiple, moq, qmax in points:
        W = L + R                                             # protection weeks
        D = bom * mu                                          # weekly demand
        sigma = bom * sd_week * math.sqrt(W)                  # uncertainty over W weeks
        eoq = math.sqrt(2 * K * D / h) if h > 0 else qmax     # EOQ
        Q = apply_order_rules(eoq, multiple, moq, qmax)       # order quantity after the order rules
        z = z_from_fill_rate(beta, Q, sigma)                  # fill-rate safety factor
        step = layout.mult[j]                                 # the engine's decision step (K11)
        s = step * round((W * D + z * sigma) / step)          # reorder level
        S = s + step * round(Q / step)                        # order-up-to level
        last = layout.active_until[j]                         # last week an order can still arrive in time (K04)
        on = (weeks >= 1) & (weeks <= last)
        policy[j] = (np.where(on, float(s), 0.0), np.where(on, float(S), 0.0))
        rows.append(dict(zip(BASELINE_COLUMNS, [j, item, L, R, W, D, sigma, K, h, eoq, Q, beta, z, s, S, f"1–{last}"])))
    return policy, pd.DataFrame(rows, columns=BASELINE_COLUMNS)


BASELINE_COLUMNS = ["Stock point", "Item", "Lead time to DC L (weeks)", "Review period R (weeks)",
                    "Protection weeks W = L + R", "Weekly demand D = BOM × μ", "σ over W weeks = BOM × σ_week × √W",
                    "Order fixed cost K", "Holding cost h (per unit and week)", "EOQ = √(2KD/h)",
                    "Q (after order rules)", "Fill-rate target β", "z from G(z) = (1 − β)Q/σ", "s = W·D + z·σ",
                    "S = s + Q", "Weeks with s,S (later 0)"]


# ============================================================================
# 3. HOLD-OUT SIMULATION — the engine's own out-of-sample check, same seeds and batches as the optimized run
# ============================================================================
def fresh_batches(timings):
    """Sizes of the optimized run's fresh-scenario batches, from its phase rows
    '6. Out-of-sample batch b/k: build + evaluate on n fresh scenarios'."""
    sizes = []
    for name, _ in timings:
        m = re.search(r"6\. Out-of-sample batch (\d+)/(\d+): build \+ evaluate on ([\d,]+) fresh scenarios", str(name))
        if m:
            sizes.append(int(m.group(3).replace(",", "")))
    if not sizes:
        raise ValueError("The optimized run has no out-of-sample batches in its timings / Summary rows.")
    return sizes


def simulate_baseline(d, policy, batches, license_file, skr=None):
    """Fixed (s,S) on the fresh scenarios: the engine's out_of_sample() with the same seeds (RandomSeed + 1 + b) and
    batch sizes as the optimized run. The engine's settings are restored afterwards."""
    layout = engine.PolicyLayout(d)
    x = layout.encode(policy, clip=False)
    layout.ub_var = [max(u, v) for u, v in zip(layout.ub_var, x)]   # bounds only have to contain the values
    keep = engine.OUT_OF_SAMPLE_SCENARIOS, engine.OUT_OF_SAMPLE_BATCH
    engine.OUT_OF_SAMPLE_SCENARIOS, engine.OUT_OF_SAMPLE_BATCH = sum(batches), batches[0]
    try:
        return engine.out_of_sample(skr or engine.import_seeker(), d, layout, x, license_file,
                                    label="Baseline (textbook s,S)")
    finally:
        engine.OUT_OF_SAMPLE_SCENARIOS, engine.OUT_OF_SAMPLE_BATCH = keep


# ============================================================================
# 4. COMPARISON — the same numbers for both plans
# ============================================================================
def plan_numbers(d, oos, constrained):
    """Numbers of one plan on the fresh scenarios, from the engine's out-of-sample results (RunResult.oos)."""
    H, chs = d.horizon, [c.id for c in d.channels]
    t0 = first_constrained_week(d)
    lost_ini = {c: oos.get(("kpi", "lost_initial_weeks", c, "mean"), 0.0) for c in chs}
    lost_con = {c: oos[("kpi", "lost_controllable_weeks", c, "mean")] for c in chs}
    waste = {d.fg_id: sum(oos[("fg_waste", t)] for t in range(1, H + 1))}
    waste.update({k.id: sum(oos[(k.id, "waste", t)] for t in range(1, H + 1)) for k in d.rms if k.perishable})
    return dict(cost=oos["cost"], parts={n: oos.get(("part", n), float("nan")) for n in COST_PARTS},
                fr={(chs[ci], t): oos[("fr_mean", ci, t)] for ci in range(len(chs)) for t in range(1, H + 1)},
                lost_cell={(c, t): oos.get(("band", "lost", c, t, "mean"), float("nan")) for c in chs
                           for t in range(1, H + 1)},
                constrained={(chs[ci], t) for ci, t in constrained}, lost_initial=lost_ini, lost_controllable=lost_con,
                waste=waste, t0=t0, n=oos.get("n"))


def first_constrained_week(d):
    return 1 + d.prod_lead_time.min if d.enforce_from_first_controllable else 1


def expected_demand(d):
    """Expected demand over the horizon, all channels (forecast mean, as the engine's waste shares, L10)."""
    return float(np.asarray(d.demand_q, float)[:, 1:, :].mean(axis=2).sum())


def comparison_tables(d, opt, base):
    """Comparison (one row per measure) and FillRates_Compare (one row per channel and week)."""
    met = lambda p: sum(engine.cell_met(d, p["fr"][k]) for k in p["constrained"])
    weakest = lambda p: min(p["constrained"], key=lambda k: p["fr"][k]) if p["constrained"] else None
    lost = lambda p: sum(p["lost_initial"].values()) + sum(p["lost_controllable"].values())
    dem, H, t0, unit = expected_demand(d), d.horizon, opt["t0"], (getattr(d, "uom", None) or {}).get(d.fg_id, "units")
    rows = [("Expected total cost (fresh scenarios)", "currency", opt["cost"], base["cost"], "")]
    rows += [(f"Cost: {n}", "currency", opt["parts"][n], base["parts"][n], "") for n in COST_PARTS]
    rows += [(f"Fill-rate rules met (of {len(opt['constrained'])})", "cells", met(opt), met(base),
              f"expected fill rate ≥ target {d.fill_rate_target:.1%} per channel and week (H05)")]
    wo, wb = weakest(opt), weakest(base)
    rows += [("Weakest cell: expected fill rate", "share", opt["fr"][wo] if wo else float("nan"),
              base["fr"][wb] if wb else float("nan"),
              f"optimized: {wo[0]} week {wo[1]}; baseline: {wb[0]} week {wb[1]}" if wo and wb else "")]
    rows += [(f"Lost demand, all channels, weeks 1–{H}", unit, lost(opt), lost(base),
              f"expected demand {dem:,.0f}")]
    if t0 > 1:
        rows += [(f"Lost demand, all channels, weeks 1–{t0 - 1} (fixed by the starting state)", unit,
                  sum(opt["lost_initial"].values()), sum(base["lost_initial"].values()), "no decision can change them; a difference is sampling")]
    rows += [(f"Lost demand, all channels, weeks {t0}–{H} (weeks with fill-rate rules)", unit,
              sum(opt["lost_controllable"].values()), sum(base["lost_controllable"].values()), "")]
    rows += [("Lost demand, share of expected demand", "share", lost(opt) / dem if dem else float("nan"),
              lost(base) / dem if dem else float("nan"), "")]
    rows += [(f"Lost demand {c}, weeks 1–{H}", unit, opt["lost_initial"][c] + opt["lost_controllable"][c],
              base["lost_initial"][c] + base["lost_controllable"][c], "") for c in opt["lost_controllable"]]
    uom = getattr(d, "uom", None) or {}
    rows += [(f"Waste {i}, weeks 1–{H}", uom.get(i, "units"), opt["waste"][i], base["waste"][i], "") for i in opt["waste"]]
    comp = pd.DataFrame(rows, columns=["Measure", "Unit", "Optimized", "Baseline (textbook s,S)", "Note"])
    comp.insert(4, "Optimized − baseline", comp["Optimized"] - comp["Baseline (textbook s,S)"])
    cells = pd.DataFrame([(c, t, "yes" if (c, t) in opt["constrained"] else "no", d.fill_rate_target,
                           opt["fr"][(c, t)], base["fr"][(c, t)], opt["lost_cell"][(c, t)], base["lost_cell"][(c, t)])
                          for c in [ch.id for ch in d.channels] for t in range(1, H + 1)],
                         columns=["Channel", "Week", "Constrained", "Target", "Expected FR optimized",
                                  "Expected FR baseline", "Lost demand optimized", "Lost demand baseline"])
    return comp, cells


def summary_lines(d, comp):
    """Short console summary."""
    v = comp.set_index("Measure")
    get = lambda m: (v.loc[m, "Optimized"], v.loc[m, "Baseline (textbook s,S)"])
    rules = [m for m in v.index if m.startswith("Fill-rate rules met")][0]
    lost = f"Lost demand, all channels, weeks 1–{d.horizon}"
    (co, cb), (ro, rb), (lo, lb) = get("Expected total cost (fresh scenarios)"), get(rules), get(lost)
    return [f"Optimized: cost {co:,.0f}, {ro:.0f} rules met, lost demand {lo:,.0f} units",
            f"Baseline:  cost {cb:,.0f}, {rb:.0f} rules met, lost demand {lb:,.0f} units",
            f"Optimized − baseline: cost {co - cb:+,.0f}, rules met {ro - rb:+.0f}, lost demand {lo - lb:+,.0f} units "
            f"(no lost-sales cost in the model: compare costs only at equal rules met)"]


# ============================================================================
# 5. ENTRY POINTS — app (RunResult) and Spyder (results workbook)
# ============================================================================
def run_baseline(optimized, license_file=None, skr=None):
    """App entry: `optimized` = the engine's RunResult. Uses that run's data object (overrides, target, review period
    included), its constrained cells and fresh-scenario batches. Returns {"Baseline_sS", "Comparison",
    "FillRates_Compare"} as DataFrames, plus "oos" (the baseline's out-of-sample numbers)."""
    d = optimized.data
    policy, table = baseline_policy(d)
    oos = simulate_baseline(d, policy, fresh_batches(optimized.timings), license_file or engine.LICENSE_FILE, skr)
    constrained = list(optimized.ins["constrained"])
    comp, cells = comparison_tables(d, plan_numbers(d, optimized.oos, constrained), plan_numbers(d, oos, constrained))
    return {"Baseline_sS": table, "Comparison": comp, "FillRates_Compare": cells, "oos": oos}


def run_baseline_from_files(data_file, results_file, license_file=None, skr=None):
    """Spyder entry: the data file and the optimized run's results workbook. Takes the run's settings from the
    workbook and stops if the two do not fit together."""
    xl = pd.ExcelFile(results_file)
    summ = pd.read_excel(xl, "Summary").set_index("Item")["Value"].astype(str)
    summ.index = summ.index.astype(str).str.strip()
    fr = pd.read_excel(xl, "FillRates")
    d = engine.load_data(data_file)
    over = {k: int(v) for k, v in re.findall(r"(NumScenarios|RandomSeed) = (\d+)", summ.get("Run overrides (K20)", ""))}
    engine.apply_overrides(d, over or None)
    d.fill_rate_target = float(fr["Target"].iloc[0])          # the target the run used (Excel or a what-if)
    for ch in d.channels:
        ch.target = d.fill_rate_target
    engine.validate(d)
    check_fits(d, summ, fr)
    batches = fresh_batches([(str(i)[len("Time: "):], None) for i in summ.index if str(i).startswith("Time: ")])
    policy, table = baseline_policy(d)
    oos = simulate_baseline(d, policy, batches, license_file or engine.LICENSE_FILE, skr)
    chs = [c.id for c in d.channels]
    constrained = [(chs.index(c), int(t)) for c, t, k in zip(fr["Channel"], fr["Week"], fr["Constrained"]) if k == "yes"]
    comp, cells = comparison_tables(d, numbers_from_workbook(d, xl, summ, fr, constrained),
                                    plan_numbers(d, oos, constrained))
    return {"Baseline_sS": table, "Comparison": comp, "FillRates_Compare": cells, "oos": oos}


def check_fits(d, summ, fr):
    """The results workbook must come from this data file: same horizon, channels, review period and start z₀
    (z₀ depends on the demand forecast and the target)."""
    problems = []
    if int(fr["Week"].max()) != d.horizon:
        problems.append(f"horizon {int(fr['Week'].max())} in the results, {d.horizon} in the data")
    if sorted(fr["Channel"].unique()) != sorted(c.id for c in d.channels):
        problems.append("different channels")
    m = re.search(r"R = (\d+) week", summ.get("Review period (A03)", ""))
    if m and int(m.group(1)) != d.review_period:
        problems.append(f"review period {m.group(1)} in the results, {d.review_period} in the data")
    m = re.search(r"z₀ = ([\d.]+)", summ.get("Start policy (warm start)", ""))
    if m and abs(float(m.group(1)) - engine.start_z(d)) > 0.006:
        problems.append(f"start z₀ {m.group(1)} in the results, {engine.start_z(d):.2f} from the data "
                        f"(demand forecast or target differ)")
    if problems:
        raise ValueError("The results workbook does not fit the data file: " + "; ".join(problems)
                         + ". Use the data file of the optimized run.")


def numbers_from_workbook(d, xl, summ, fr, constrained):
    """The optimized plan's numbers on the fresh scenarios, read from its results workbook."""
    H, chs = d.horizon, [c.id for c in d.channels]
    num = lambda s: float(str(s).replace(",", ""))
    parts = {}                                                # (the workbook rounds them to whole currency units)
    for n in COST_PARTS:
        v = summ.get(f"cost: {n} (in-sample / out-of-sample)")
        parts[n] = num(v.split("/")[1]) if v is not None else float("nan")
    col = [c for c in fr.columns if c.startswith("Expected FR (")][0]
    kpi = pd.read_excel(xl, "KPIs").set_index("Item")["Mean"]
    t0 = first_constrained_week(d)
    lost_ini = {c: float(kpi.get(f"Lost sales {c}, weeks 1–{t0 - 1} (initial state, no decision can change them)", 0.0))
                for c in chs}
    lost_con = {c: float(kpi[f"Lost sales {c}, weeks {t0}–{H} (weeks with fill-rate rules)"]) for c in chs}
    sw = pd.read_excel(xl, "Stock_Waste")
    waste = {d.fg_id: float(sw[f"{d.fg_id} waste (mean)"].sum())}
    waste.update({k.id: float(sw[f"{k.id} waste (mean)"].sum()) for k in d.rms if k.perishable})
    bands = pd.read_excel(xl, "Bands")
    lost_b = bands[bands["Series"] == "lost"].set_index(["Item", "Week"])["Mean"]
    return dict(cost=num(summ["Expected total cost (out-of-sample)"]), parts=parts,
                fr={(c, int(t)): float(v) for c, t, v in zip(fr["Channel"], fr["Week"], fr[col])},
                lost_cell={(c, t): float(lost_b.get((c, t), float("nan"))) for c in chs for t in range(1, H + 1)},
                constrained={(chs[ci], t) for ci, t in constrained}, lost_initial=lost_ini, lost_controllable=lost_con,
                waste=waste, t0=t0, n=None)


def write_baseline_report(tables, path):
    """MEIO_Baseline_<time>.xlsx with the sheets Comparison, Baseline_sS and FillRates_Compare."""
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        for name in ("Comparison", "Baseline_sS", "FillRates_Compare"):
            df = tables[name]
            df.to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            for i, c in enumerate(df.columns, start=1):
                width = max([len(str(c))] + [len(f"{v:,.2f}" if isinstance(v, float) else str(v)) for v in df[c]])
                ws.column_dimensions[ws.cell(1, i).column_letter].width = min(60, width + 2)
    return path


def newest_results_file(folder="."):
    files = sorted(Path(folder).glob("MEIO_Results*.xlsx"), key=lambda p: p.stat().st_mtime)
    if not files:
        raise FileNotFoundError("No MEIO_Results*.xlsx found; set RESULTS_FILE in section 0.")
    return str(files[-1])


def main():
    results = RESULTS_FILE or newest_results_file(Path(DATA_FILE).parent)
    print(f"Baseline (textbook s,S) for {DATA_FILE}, compared with {results}", flush=True)
    tables = run_baseline_from_files(DATA_FILE, results)
    d = engine.load_data(DATA_FILE)
    print(tables["Baseline_sS"][["Stock point", "Q (after order rules)", "z from G(z) = (1 − β)Q/σ", "s = W·D + z·σ",
                                 "S = s + Q"]].round(2).to_string(index=False))
    for line in summary_lines(d, tables["Comparison"]):
        print(line)
    out = f"{OUTPUT_PREFIX}_{datetime.now():%Y%m%d-%H%M%S}.xlsx"
    print(f"Written: {write_baseline_report(tables, out)}")


if __name__ == "__main__":
    main()
