#!/usr/bin/env python3
"""
================================================================================
 MEIO optimization with InsideOpt Seeker (stochastic mode)
 Product FG1 "Erdbeere Mix große Dose" — Suppliers → RMW → KAJA → DC → AMZ / D2C / B2B
================================================================================

ONE MODEL, ONE PLACE
  Every supply-chain rule — lots and shelf life, FEFO, channel priority, (s,S) ordering with MOQ /
  batch / max, echelon positions, lead times without overtaking, waste, costs and fill rates — is written
  exactly once: as Seeker Terms in SECTION 3. No other part of this file computes supply-chain dynamics.
  Every number in the results comes from Seeker evaluating that one model.

WHAT EACH SECTION DOES
  0. SETTINGS ............ everything you may want to change (time limit, scenarios, files, search)
  1. DATA ................ reads MEIO_Data_Template.xlsx and checks it
  2. DECISION VARIABLES .. maps (s,S) per stock point and week to Seeker integer variables; lower bound
                           0, upper bound from the forecast (raise via EXTRA_COVER_WEEKS in section 0)
  3. SEEKER MODEL ........ the model itself: random demand and lead times, week-by-week dynamics,
                           cost, fill rates, objective and fill-rate constraints
  4. START POLICY ........ one fixed, explainable (s,S) policy computed from the forecast (formula
                           below); Seeker starts its search from it (warm start)
  4b. FEASIBILITY CHECK .. one extra evaluation: which fill-rate rules fail whatever we decide
                           (supply cannot reach those weeks); one line in the console and Summary
  5. OPTIMIZATION ........ Seeker searches for the cheapest (s,S) policy within the time limit
                           ("in-sample": on Seeker's own NumScenarios scenarios)
  6. OUT-OF-SAMPLE CHECK . the same model (section 3) is built again with FRESH random demand
                           (OUT_OF_SAMPLE_SCENARIOS), the decisions fixed, and evaluated once
  7. REPORT .............. results workbook + console summary
  8. RUN AND MAIN ........ run(): sections 1 → 7 in one call, returns all results (API for apps);
                           main(): run() + results workbook + console summary; every phase timed

  Reading results from Seeker: after one evaluation the script reads only single numbers (means,
  quantiles, shares of scenarios). Seeker 2.1 returns those instantly, whereas reading a list of
  per-scenario values takes 0.5–3 s each, so such lists are never read.

HOW TO RUN (Spyder: open this file, press F5)
  1. pip install ioseeker numpy pandas openpyxl
  2. Set LICENSE_FILE below to your Seeker license file.
  3. Keep MEIO_Data_Template.xlsx next to this file (or change DATA_FILE).
================================================================================
"""
import copy
import math
import sys
import time
import warnings
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

# openpyxl cannot keep Excel's newer data-validation lists when READING the template; harmless here
warnings.filterwarnings("ignore", message="Data Validation extension is not supported")

# ============================================================================
# 0. SETTINGS — edit here
# ============================================================================
LICENSE_FILE = "license.sio"                  # <<< path to your Seeker license file
DATA_FILE = "MEIO_Data_Template.xlsx"         # input data (template v1.1)
OUTPUT_FILE = "MEIO_Results_stochastic.xlsx"  # results; if it is open in Excel, a time-stamped name is used
TIME_LIMIT_SECONDS = 250                      # <<< Seeker search time. None = Excel Settings!SolverTimeLimit.
                                              #     Seeker 2.1 needs ~15% longer to finish after the limit.
OUT_OF_SAMPLE_SCENARIOS = 2500                # <<< fresh scenarios for the out-of-sample check (section 6)
OUT_OF_SAMPLE_BATCH = 2500                    # scenarios per Seeker model in section 6; lower it if memory
                                              # runs short (batches are combined; P10/P90 then averaged)
# Seeker search (section 5). Seeker optimizes on Excel Settings!NumScenarios scenarios (200).
STOCHASTIC_SPEED = 0.0                        # keep 0: at 0.5 Seeker 2.1 rejected every move (K13)
FILL_RATE_CONSTRAINT_FORM = "frequency"       # "frequency": share of scenarios with FR ≥ target ≥ chance level
                                              # "quantile" : (1 − chance level)-quantile of FR ≥ target
                                              # "none"     : no fill-rate constraints (diagnostic only!)
OPTIMIZATION_CHANCE_LEVEL = None              # None = Excel Settings!FillRateChanceLevel (0.99). E.g. 0.995 to
                                              # optimize with a safety margin; the report checks the Excel level.
FEASIBILITY_CHECK = True                      # section 4b: ~25 s (one extra model); False skips it
REPORT_INTERVAL_SECONDS = 30                  # Seeker progress line every … seconds
OVERSAMPLE_FACTOR = 5                         # Seeker 2.2+: maxSample = factor × scenarios (2.1: stratified)
# Search space (section 2; Model_Assumptions K05, K10, K11)
VARIABLE_PRIORITY = {"FG": 5.0, "RM3": 4.0, "RM4": 2.0, "RM1": 1.0, "RM2": 1.0}
#   how often Seeker tries to change the (s,S) variables of a stock point, relative to the others
STEP_FACTOR = {"RM3": 5}       # decision step = factor × order multiple (RM3: 5 × 10 kg = 50 kg); others 1
EXTRA_COVER_WEEKS = {"FG": 8, "RM1": 4, "RM2": 4, "RM3": 8, "RM4": 4}
#   upper bound of each reorder point s = Q95 demand over (lead time(s) + 1 + these weeks), section 2.
#   If the run reports "at its upper bound" for a stock point, raise its number here (RM3, FG raised 4 → 8).
START_BOUND_MARGIN = 1.25      # bounds are never below 1.25 × the start policy's reorder point
# Start policy = Seeker's warm start (section 4, Model_Assumptions K07)
START_Z = 4.0                  # safety factor z (× standard deviation of demand over the cover window)
START_EXTRA_WEEKS_FG = 2       # FG cover window = max production lead time + 1 + this many weeks
START_EXTRA_WEEKS_RM = 4       # RM cover window = max RM lead time + max production lead time + 1 + this many
START_LOT_WEEKS = 4            # order-up-to S = reorder point s + this many weeks of average demand (≥ MOQ)

SCRIPT_VERSION = "2026-11-06 v16 (Seeker-only)"
EPS = 1e-9
TIMINGS = []                                  # (phase, seconds) — printed at the end and written to the report


class phase:
    """Times a block of the run:  with phase("Model build"): ..."""

    def __init__(self, name):
        self.name = name

    def __enter__(self):
        self.t = time.time()
        print(f"[{time.strftime('%H:%M:%S')}] {self.name} ...", flush=True)
        return self

    def __exit__(self, *exc):
        dt = time.time() - self.t
        TIMINGS.append((self.name, dt))
        print(f"[{time.strftime('%H:%M:%S')}] {self.name}: {dt:.1f} s", flush=True)
        return False


def usable_output_path(path):
    """Returns `path` if the results can be written there, otherwise a time-stamped name next to it.
    On Windows a workbook that is open in Excel is locked; opening it for writing raises PermissionError."""
    p = Path(path)
    try:
        if p.exists():
            with open(p, "r+b"):
                pass
        return p
    except PermissionError:
        alt = p.with_name(f"{p.stem}_{time.strftime('%Y%m%d_%H%M%S')}{p.suffix}")
        print(f"NOTE: {p.name} is open in Excel (or read-only) → the results will be written to {alt.name}", flush=True)
        return alt


# ============================================================================
# 1. DATA — read MEIO_Data_Template.xlsx and check it (no dynamics here)
# ============================================================================
@dataclass
class LeadTime:
    """Discrete lead-time distribution: values (weeks) with probabilities."""
    values: np.ndarray
    probs: np.ndarray

    @property
    def min(self):
        return int(self.values.min())

    @property
    def max(self):
        return int(self.values.max())


@dataclass
class Tiers:
    """Quantity-dependent unit cost table (priced in section 3). AllUnits: whole qty at the tier it falls into."""
    froms: np.ndarray
    tos: np.ndarray          # np.inf for the last tier
    costs: np.ndarray
    mode: str                # "AllUnits" or "Incremental"


@dataclass
class RawMaterial:
    id: str
    bom: float               # qty per FG unit
    shelf_life: int | None   # None = does not expire
    min_remaining: int       # freshness rule at production (0 for non-perishables)
    moq: float
    multiple: float
    max_order: float         # effective max = MaxOrderQty rounded down to a multiple
    lead_time: LeadTime
    fixed_cost: float
    var_cost: float
    holding: float
    waste: float

    @property
    def perishable(self):
        return self.shelf_life is not None


@dataclass
class Channel:
    id: str
    target: float            # fill-rate target β_c
    min_remaining: int       # rest shelf life needed ρ_c


@dataclass
class ModelData:
    horizon: int
    review_period: int
    start_date: pd.Timestamp
    chance_level: float
    enforce_from_first_controllable: bool
    n_scenarios: int
    seed: int
    time_limit: float
    # finished good
    fg_id: str
    fg_shelf_life: int
    prod_moq: float
    prod_batch: float
    prod_max: float
    prod_lead_time: LeadTime
    prod_fixed: float
    prod_tiers: Tiers
    transport_tiers: Tiers | None
    fg_holding: float
    fg_waste: float
    # other entities
    rms: list
    channels: list           # sorted by priority (highest fill-rate target first)
    levels: np.ndarray       # quantile probability levels
    demand_q: np.ndarray     # (C, H+1, 10) quantiles, index t = 1..H
    init_fg: list            # [(remaining shelf life, qty)]
    init_rm: dict            # rm id -> [(remaining shelf life or None, qty)]
    open_prod: list          # dicts: qty, placed, arrival, certain, remaining
    open_rm: dict            # rm id -> list of such dicts
    data_status: str = "unknown"

    @property
    def rho_min(self):
        return min(c.min_remaining for c in self.channels)


def _sheet(xls, name):
    """Read a template sheet: headers in row 4, records from row 5, drop empty rows."""
    df = pd.read_excel(xls, name, header=3)
    df = df.loc[df.iloc[:, 0].notna()].copy()
    return df.reset_index(drop=True)


def _col(df, prefix):
    """Column whose header starts with prefix (headers carry units in brackets)."""
    for c in df.columns:
        if str(c).startswith(prefix):
            return c
    raise KeyError(f"column '{prefix}' not found in {list(df.columns)}")


def _num(v, default=None):
    return default if (v is None or (isinstance(v, float) and math.isnan(v))) else float(v)


def load_data(path):
    """Read the Excel template into a ModelData object (see sheet descriptions)."""
    xls = pd.ExcelFile(path)
    st = _sheet(xls, "Settings").set_index("Parameter")["Value"].to_dict()

    # --- lead-time distributions
    ltd = _sheet(xls, "LeadTimeDist")
    lead_times = {}
    for did, g in ltd.groupby("DistID", sort=False):
        lead_times[did] = LeadTime(g[_col(ltd, "Value")].astype(int).to_numpy(),
                                   g["Probability"].astype(float).to_numpy())

    # --- tiers
    tiers_df = _sheet(xls, "CostTiers")
    tiers = {}
    for tid, g in tiers_df.groupby("TierTableID", sort=False):
        g = g.sort_values("TierNo")
        tiers[tid] = Tiers(g["FromQty"].astype(float).to_numpy(),
                           g["ToQty"].astype(float).fillna(np.inf).to_numpy(),
                           g["CostPerUnit"].astype(float).to_numpy(), str(g["PricingMode"].iloc[0]))

    items = _sheet(xls, "Items").set_index("ItemID")
    costs = _sheet(xls, "Costs").set_index("ItemID")
    prod = _sheet(xls, "Production")
    if len(prod) != 1:
        raise ValueError("This model supports exactly one finished good / production row.")
    prod = prod.iloc[0]
    fg_id = prod["ItemID"]
    bom = _sheet(xls, "BOM")
    src = _sheet(xls, "Sourcing").set_index("ItemID")
    lanes = _sheet(xls, "Lanes")
    kaja_dc = lanes[(lanes["FromID"] == prod["SiteID"]) & (lanes["ToID"] == prod["DestinationID"])]
    tr_id = kaja_dc["TransportCostTierTableID"].iloc[0] if len(kaja_dc) else None
    transport_tiers = tiers.get(tr_id) if isinstance(tr_id, str) else None

    # --- raw materials (BOM order)
    rms = []
    for _, b in bom[bom["ParentItemID"] == fg_id].iterrows():
        k = b["ComponentItemID"]
        sl = _num(items.loc[k, _col(items.reset_index(), "ShelfLife")])
        s = src.loc[k]
        c = costs.loc[k]
        mult = float(s["OrderMultiple"])
        rms.append(RawMaterial(
            id=k, bom=float(b["QtyPerParent"]) / (1.0 - _num(b["ScrapRate"], 0.0)),
            shelf_life=None if sl is None else int(sl),
            min_remaining=int(_num(b[_col(bom, "MinRemainingShelfLife")], 0)),
            moq=float(s["MOQ"]), multiple=mult,
            max_order=math.floor(float(s["MaxOrderQty"]) / mult + EPS) * mult,
            lead_time=lead_times[s["LeadTimeDistID"]],
            fixed_cost=_num(c[_col(costs.reset_index(), "OrderFixedCost")], 0.0),
            var_cost=_num(c[_col(costs.reset_index(), "OrderVariableCost")], 0.0),
            holding=_num(c[_col(costs.reset_index(), "HoldingCost")], 0.0),
            waste=_num(c[_col(costs.reset_index(), "WasteCost")], 0.0)))

    # --- channels, sorted by fill-rate target (highest first = served first, H02)
    ch = _sheet(xls, "Channels")
    channels = [Channel(r["ChannelID"], float(r["TargetFillRate"]), int(r[_col(ch, "MinRemainingShelfLife")]))
                for _, r in ch.iterrows()]
    channels = sorted(channels, key=lambda c: -c.target)  # stable: ties keep sheet order

    # --- demand quantiles
    H = int(st["HorizonPeriods"])
    levels = np.array([float(x) for x in str(st["DemandQuantileLevels"]).split(";")])
    dem = _sheet(xls, "Demand")
    qcols = [c for c in dem.columns if str(c).startswith("Q") and str(c)[1:].isdigit()]
    if len(qcols) != len(levels):
        raise ValueError(f"{len(qcols)} quantile columns but {len(levels)} levels in Settings.")
    demand_q = np.full((len(channels), H + 1, len(levels)), np.nan)
    for _, r in dem.iterrows():
        t = int(r["Period"])
        if t <= H and r["ChannelID"] in [c.id for c in channels]:
            ci = [c.id for c in channels].index(r["ChannelID"])
            demand_q[ci, t, :] = r[qcols].astype(float).to_numpy()

    # --- initial inventory & open orders
    inv = _sheet(xls, "Init_Inventory") if len(pd.read_excel(xls, "Init_Inventory", header=3).dropna(how="all")) else None
    init_fg, init_rm = [], {k.id: [] for k in rms}
    if inv is not None:
        for _, r in inv.iterrows():
            a = _num(r[_col(inv, "RemainingShelfLife")])
            q = float(r[_col(inv, "Quantity")])
            if r["ItemID"] == fg_id:
                init_fg.append((int(a), q))
            elif r["ItemID"] in init_rm:
                init_rm[r["ItemID"]].append((None if a is None else int(a), q))
    oo = _sheet(xls, "Open_Orders") if "Open_Orders" in xls.sheet_names else None
    open_prod, open_rm = [], {k.id: [] for k in rms}
    if oo is not None and len(oo):
        oo = oo.loc[oo["OrderType"].notna()]
        for _, r in oo.iterrows():
            rec = dict(qty=float(r[_col(oo, "Quantity")]), placed=int(_num(r["PlacedInPeriod"], 0)),
                       arrival=int(r["ExpectedArrivalPeriod"]),
                       certain=str(r["ArrivalCertain"]).strip().lower() != "no",
                       remaining=_num(r[_col(oo, "RemainingShelfLifeAtArrival")]))
            if r["OrderType"] == "Production":
                open_prod.append(rec)
            else:
                open_rm[r["ItemID"]].append(rec)
    open_prod.sort(key=lambda o: o["placed"])                 # oldest first: needed for no-overtaking (E04)
    for k in rms:
        open_rm[k.id].sort(key=lambda o: o["placed"])

    fgc = costs.loc[fg_id]
    checks = pd.read_excel(xls, "Checks", header=None)
    status = "unknown"
    for _, row in checks.iterrows():
        vals = list(row.values)
        if "OVERALL" in vals:
            status = str(vals[vals.index("OVERALL") + 1])

    return ModelData(
        horizon=H, review_period=int(st["ReviewPeriod"]), start_date=pd.Timestamp(st["PlanningStartDate"]),
        chance_level=float(st["FillRateChanceLevel"]),
        enforce_from_first_controllable=str(st["FillRateEnforcementStart"]) == "FirstControllablePeriod",
        n_scenarios=int(st["NumScenarios"]), seed=int(st["RandomSeed"]), time_limit=float(st["SolverTimeLimit"]),
        fg_id=fg_id, fg_shelf_life=int(items.loc[fg_id, _col(items.reset_index(), "ShelfLife")]),
        prod_moq=float(prod["MOQ"]), prod_batch=float(prod["BatchSize"]), prod_max=float(prod["MaxQtyPerOrder"]),
        prod_lead_time=lead_times[prod["LeadTimeDistID"]],
        prod_fixed=_num(fgc[_col(costs.reset_index(), "ProductionFixedCost")], 0.0),
        prod_tiers=tiers[fgc[_col(costs.reset_index(), "ProductionVarCostTierTableID")]],
        transport_tiers=transport_tiers,
        fg_holding=_num(fgc[_col(costs.reset_index(), "HoldingCost")], 0.0),
        fg_waste=_num(fgc[_col(costs.reset_index(), "WasteCost")], 0.0),
        rms=rms, channels=channels, levels=levels, demand_q=demand_q,
        init_fg=init_fg, init_rm=init_rm, open_prod=open_prod, open_rm=open_rm, data_status=status)


def validate(d):
    """Hard data checks in Python (independent of the Excel 'Checks' formulas). Raises on error."""
    errors = []
    if d.data_status not in ("READY", "unknown"):
        errors.append(f"Excel sheet Checks shows OVERALL = {d.data_status} (must be READY).")
    if not 1 <= d.review_period <= d.horizon:
        errors.append("ReviewPeriod must be between 1 and HorizonPeriods.")
    q = d.demand_q[:, 1:, :]
    if np.isnan(q).any():
        errors.append("Demand quantiles missing for some channel/week within the horizon.")
    elif (np.diff(q, axis=2) < -EPS).any() or (q < 0).any():
        errors.append("Demand quantiles must be ≥ 0 and non-decreasing.")
    if not (np.all(np.diff(d.levels) > 0) and d.levels[0] > 0 and d.levels[-1] < 1):
        errors.append("DemandQuantileLevels must be increasing within (0, 1).")
    lts = [("production", d.prod_lead_time)] + [(k.id, k.lead_time) for k in d.rms]
    for name, lt in lts:
        if abs(lt.probs.sum() - 1) > 1e-6 or (lt.values < 0).any():
            errors.append(f"Lead time of {name}: probabilities must sum to 1, values ≥ 0.")
    if abs(d.prod_moq % d.prod_batch) > EPS or d.prod_max < d.prod_moq:
        errors.append("Production: MOQ must be a multiple of BatchSize and ≤ MaxQtyPerOrder.")
    for k in d.rms:
        if abs(k.moq % k.multiple) > EPS or k.max_order < k.moq:
            errors.append(f"{k.id}: MOQ must be a multiple of OrderMultiple and ≤ effective max.")
        if k.perishable and k.shelf_life <= k.min_remaining:
            errors.append(f"{k.id}: shelf life must exceed the freshness rule at production.")
    for a, _ in d.init_fg:
        if a > d.fg_shelf_life:
            errors.append("Initial FG lot has more remaining shelf life than the FG shelf life.")
    for c in d.channels:
        if not 0 < c.target <= 1 or c.min_remaining > d.fg_shelf_life:
            errors.append(f"Channel {c.id}: target in (0,1] and shelf-life need ≤ FG shelf life.")
    if d.n_scenarios * (1 - d.chance_level) < 1 - EPS and d.chance_level < 1:
        errors.append("NumScenarios too small for FillRateChanceLevel (need ≥ 1/(1−level)).")
    if errors:
        raise ValueError("Data validation failed:\n  - " + "\n  - ".join(errors))


# ============================================================================
# 2. DECISION VARIABLES — (s,S) ↔ Seeker variables and their bounds (K01, K04, K05, K11, K12)
# ============================================================================
class PolicyLayout:
    """Decision variables and their bounds (Model_Assumptions K01, K04, K05, K11, K12).

    Per stock point j and active week t two integers: σ = reorder point s in steps, δ = gap S − s in steps
    (s = m·σ, S = s + m·δ; step m = batch / order multiple × STEP_FACTOR). Weeks too late for an order to
    arrive within the horizon have no variables (s = S = 0).

    Lower bound 0 (= no order that week). Upper bound from the forecast, Q95 = sum of the weekly 95% quantiles:
      reorder point s : Q95 demand over (lead time + 1 + EXTRA_COVER_WEEKS[j]) weeks   (RM: × BOM, echelon lead
                        time = RM + production lead time)          ← raise EXTRA_COVER_WEEKS if s hits its bound
      gap S − s       : FG ≤ max production qty and ≤ Q95 demand over its 20 sellable weeks;
                        RM ≤ max order qty (perishable RM also ≤ BOM × Q95 over its 16 usable weeks)
    Bounds are rounded up to the step and raised, if needed, to contain the start policy (s up to 1.25 × start)."""

    def __init__(self, d):
        self.d = d
        H, Lp_min, Lp_max = d.horizon, d.prod_lead_time.min, d.prod_lead_time.max
        self.points = [("FG", d.prod_batch * STEP_FACTOR.get("FG", 1), H - Lp_min)] + \
                      [(k.id, k.multiple * STEP_FACTOR.get(k.id, 1), H - Lp_min - k.lead_time.min) for k in d.rms]
        self.mult = {j: m for j, m, _ in self.points}
        self.active_until = {j: last for j, _, last in self.points}
        q95 = d.demand_q[:, 1:, -1].sum(0)                               # weekly Q95 of total demand

        def Q95(t, weeks):                                               # weeks t … t + weeks − 1, cut at H
            return float(q95[t - 1:min(H, t - 1 + weeks)].sum()) if 1 <= t <= H else 0.0

        rms = {k.id: k for k in d.rms}
        self.index, self.ub_var = [], []                                  # (point, week, kind), upper bound in steps
        for j, m, last in self.points:
            extra = EXTRA_COVER_WEEKS.get(j, 4)
            for t in range(1, max(0, last) + 1):
                if j == "FG":
                    s_ub = Q95(t, Lp_max + 1 + extra)
                    gap_ub = min(d.prod_max, max(Q95(t + Lp_min, d.fg_shelf_life - d.rho_min), d.prod_moq))
                else:
                    k = rms[j]
                    s_ub = k.bom * Q95(t, k.lead_time.max + Lp_max + 1 + extra)
                    gap_ub = k.max_order
                    if k.perishable:
                        gap_ub = min(gap_ub, max(k.bom * Q95(t + k.lead_time.min, k.shelf_life - k.min_remaining), k.moq))
                self.index += [(j, t, "sigma"), (j, t, "delta")]
                self.ub_var += [max(1, math.ceil(s_ub / m)), max(1, math.ceil(gap_ub / m))]
        self.gap_is_lossless_cap = {j: j != "FG" for j, _, _ in self.points}   # RM: gap ≥ max order ≡ max order

    def __len__(self):
        return len(self.index)

    def upper(self, i):
        return self.ub_var[i]

    def include_start(self, pol, margin=START_BOUND_MARGIN):
        """Widen bounds so the start policy lies inside: σ_ub ≥ margin × start σ; FG δ_ub ≥ start δ.
        (RM gaps above the max order size are not widened: they behave exactly like the cap.)"""
        for i, (j, t, kind) in enumerate(self.index):
            m = self.mult[j]
            if kind == "sigma":
                self.ub_var[i] = max(self.ub_var[i], math.ceil(margin * pol[j][0][t] / m))
            elif not self.gap_is_lossless_cap[j]:
                self.ub_var[i] = max(self.ub_var[i], math.ceil((pol[j][1][t] - pol[j][0][t]) / m))

    def search_space_log10(self):
        return float(sum(math.log10(u + 1) for u in self.ub_var))

    def decode(self, x):
        H = self.d.horizon
        pol = {j: (np.zeros(H + 1), np.zeros(H + 1)) for j, _, _ in self.points}
        for (j, t, kind), v in zip(self.index, x):
            m = self.mult[j]
            if kind == "sigma":
                pol[j][0][t] = m * v
            else:
                pol[j][1][t] = m * v                             # temporarily δ·m
        for j in pol:
            pol[j][1][:] = pol[j][0] + pol[j][1]                 # S = s + m·δ
        return pol

    def encode(self, pol, clip=True):
        x = []
        for i, (j, t, kind) in enumerate(self.index):
            m = self.mult[j]
            s_, S_ = pol[j][0][t], pol[j][1][t]
            v = max(round((s_ if kind == "sigma" else S_ - s_) / m), 0)
            x.append(int(min(v, self.ub_var[i]) if clip else v))
        return x

    def at_bound(self, x):
        """Reorder points s at their upper bound (the bound may cut off the optimum → widen it).
        δ at its bound is harmless: S − s ≥ max order size simply means 'order the maximum'."""
        return [self.index[i] for i, v in enumerate(x) if v >= self.upper(i) and self.index[i][2] == "sigma"]


def bound_note(layout, x):
    """'none', or which reorder points sit at their upper bound and which setting raises it."""
    hits = layout.at_bound(x)
    if not hits:
        return "none"
    pts = sorted({j for j, _, _ in hits})
    weeks = "; ".join(f"{j} week {', '.join(str(t) for jj, t, _ in hits if jj == j)}" for j in pts)
    return f"{weeks} → raise " + ", ".join(f"EXTRA_COVER_WEEKS['{j}']" for j in pts) + " in section 0"


# ============================================================================
# 3. SEEKER MODEL — every supply-chain rule, written once as Seeker Terms
# ============================================================================
#
# Seeker's stochastic environment holds every random input as a distribution and evaluates every Term
# in all of its scenarios. The week-by-week dynamics follow the event order of rule A04:
#   E1 receipts → E2 FG review + production → E3 RM review (echelon) → E4 demand → E5 holding → E6 ageing/waste
# Building blocks:
#   lots per remaining shelf life  → one Term per lot bucket
#   FEFO                           → take = min(lot, max_0(need − stock in older lots))
#   (s,S) trigger and rounding     → comparisons (0/1 Terms), ceil/floor, min/max
#   cost tiers                     → nested if_
# Objective  : aggregate_mean(total cost) = expected cost
# Constraints: per channel and week, share of scenarios with fill rate ≥ target ≥ chance level


def sample_from_quantiles(q, levels, u):
    """Inverse-CDF sampling by linear interpolation through the quantile points (G02).
    Tails: from u=0 at max(0, Q_first − (Q_2nd − Q_first)/2) and to u=1 at Q_last + (Q_last − Q_2nd-last)/2."""
    lo = max(0.0, q[0] - (q[1] - q[0]) / 2.0)
    hi = q[-1] + (q[-1] - q[-2]) / 2.0
    x = np.concatenate(([0.0], levels, [1.0]))
    y = np.concatenate(([lo], q, [hi]))
    return np.interp(u, x, y)


def _quantile_distribution_class(skr):
    """Seeker user distribution that samples demand from the 10-quantile forecast (G02)."""

    class QuantileDistribution(skr.UserDistribution):
        def __init__(self, q, levels, seed):
            skr.UserDistribution.__init__(self)
            self.q, self.levels = np.asarray(q, float), np.asarray(levels, float)
            self.rng = np.random.default_rng(seed)

        def sample_n(self, n):
            """Stratified: one draw from each of n equal probability bands, in random order, so every set
            of scenarios Seeker asks for covers the whole forecast distribution evenly."""
            n = int(n)
            u = (np.arange(n) + self.rng.random(n)) / n
            self.rng.shuffle(u)
            vals = np.maximum(0.0, np.rint(sample_from_quantiles(self.q, self.levels, u)))
            return [float(v) for v in vals]

    return QuantileDistribution


class _Stream:
    """Orders from one source (production line or one supplier) and their arrivals, as Terms.
    No overtaking (E04): arrival = max(week + lead time, arrival of the previous order)."""

    def __init__(self, T, open_orders, open_arrivals, lead_terms, fixed_lt):
        self.T = T
        self.orders = []                        # (arrival: int or Term, qty: Term or float, rest shelf life or None)
        for o, a in zip(open_orders, open_arrivals):
            self.orders.append((a, o["qty"], o["remaining"]))
        self.lead = lead_terms                  # week -> int (fixed) or Term (stochastic)
        ints = [a for a, _, _ in self.orders if isinstance(a, int)]
        max_open = max(ints) if len(ints) == len(self.orders) and ints else (0 if not self.orders else None)
        # fast path: fixed lead time and open orders arriving before any new order can → arrival = t + L exactly
        self.fast = fixed_lt and max_open is not None and max_open <= 1 + min(lead_terms.values(), default=1)
        if not self.fast:
            last = [a if not isinstance(a, int) else T.C(a) for a, _, _ in self.orders]
            self.last = T.max(*last) if len(last) > 1 else (last[0] if last else T.C(0))

    def add(self, t, q):
        L = self.lead[t]
        if self.fast:
            arr = t + int(L)
        else:
            base = self.T.C(t + int(L)) if isinstance(L, (int, np.integer)) else L + t
            arr = self.T.max(base, self.last)
            self.last = self.T.env.if_(q > 0, arr, self.last)      # previous arrival only moves if an order is placed
        self.orders.append((arr, q, None))
        return arr

    def receipts(self, t, full_shelf_life):
        """Quantity arriving at the start of week t, per rest shelf life (full unless the open order says otherwise)."""
        out = {}
        for arr, q, rem in self.orders:
            a = full_shelf_life if rem is None else int(rem)
            if isinstance(arr, (int, np.integer)):
                if arr == t:
                    out.setdefault(a, []).append(q if not isinstance(q, (int, float)) else self.T.C(q))
            else:
                out.setdefault(a, []).append(self.T.env.if_(arr == t, q if not isinstance(q, (int, float)) else self.T.C(q), self.T.C(0)))
        return {a: self.T.sum(v) for a, v in out.items()}

    def pipeline(self, t):
        """Ordered and not yet arrived at the review in week t."""
        parts = []
        for arr, q, _ in self.orders:
            if isinstance(arr, (int, np.integer)):
                if arr > t:
                    parts.append(q if not isinstance(q, (int, float)) else self.T.C(q))
            else:
                parts.append(self.T.env.if_(arr > t, q if not isinstance(q, (int, float)) else self.T.C(q), self.T.C(0)))
        return self.T.sum(parts)


class _T:
    """Small helper around the Seeker environment: constants and list-aggregators that skip empty parts."""

    def __init__(self, env):
        self.env = env
        self._consts = {}

    def C(self, v):
        v = float(v)
        if v not in self._consts:
            self._consts[v] = self.env.convert(v)
        return self._consts[v]

    def sum(self, parts):
        parts = [p for p in parts if p is not None]
        if not parts:
            return self.C(0)
        return parts[0] if len(parts) == 1 else self.env.sum(parts)

    def min(self, *ts):
        return self.env.min([t if not isinstance(t, (int, float)) else self.C(t) for t in ts])

    def max(self, *ts):
        return self.env.max([t if not isinstance(t, (int, float)) else self.C(t) for t in ts])


@dataclass
class SeekerModel:
    """Handles to the Seeker Terms of one model (one environment). 'Stochastic' Terms have one value per
    scenario; only aggregates of them (single numbers) are read back."""
    env: object
    T: object
    variables: list                            # the decision variables (order = PolicyLayout.index)
    objective: object                          # aggregate_mean(total cost) = expected cost
    cost: object                               # total cost (stochastic)
    cost_parts: dict                           # name -> stochastic cost Term
    fill_rate: dict                            # (ci, t) -> stochastic fill rate
    fr_share: dict                             # (ci, t) -> share of scenarios with FR ≥ target (single number)
    fr_stat: dict                              # (ci, t) -> the statistic used in the constraint
    constrained: list                          # (ci, t) with a fill-rate constraint (H06)
    demand: dict                               # (ci, t) -> demand distribution Term
    lead_prod: dict                            # week -> int or Term
    lead_rm: dict                              # rm id -> {week: int or Term}
    open_prod_arrival: list                    # int or Term per open production order
    open_rm_arrival: dict                      # rm id -> list
    prod_qty: dict                             # week -> production order quantity
    prod_arrival: dict                         # week -> arrival week of that order (int or Term)
    rm_order: dict                             # (rm id, week) -> supplier order quantity
    rm_arrival: dict                           # (rm id, week) -> arrival week
    fg_end: dict                               # week -> FG1 stock at the DC at week end (before scrapping)
    fg_waste: dict                             # week -> FG1 units scrapped
    rm_end: dict                               # (rm id, week) -> RMW stock at week end
    rm_waste: dict                             # (rm id, week) -> RM units scrapped
    holding: dict                              # week -> holding cost
    keep_alive: list                           # Python objects Seeker calls back into (must outlive env)


SUPPORTS_MAX_SAMPLE = {}


def _with_max_sample(fn, *args, max_sample):
    """Seeker 2.2 accepts a trailing maxSample (built-in oversampling); Seeker 2.1 does not.
    Try with it first, fall back without it, and remember the answer per function."""
    name = getattr(fn, "__name__", str(fn))
    if SUPPORTS_MAX_SAMPLE.get(name, True):
        try:
            out = fn(*args, max_sample)
            SUPPORTS_MAX_SAMPLE[name] = True
            return out
        except TypeError:
            SUPPORTS_MAX_SAMPLE[name] = False
    return fn(*args)


def _fefo(T, lots, keys, need):
    """FEFO withdrawal of `need` from lots[keys] (keys sorted oldest first). Updates lots, returns total taken."""
    before = None
    for a in keys:
        lot = lots.get(a)
        if lot is None:
            continue
        rest = need if before is None else need - before
        take = T.min(lot, T.env.max_0(rest))
        before = lot if before is None else before + lot
        lots[a] = lot - take
    if before is None:
        return T.C(0)
    return T.min(need, before)


def build_model(skr, env, d, layout, x_start, seed, resolution):
    """THE MODEL. Creates in `env`: decision variables (start values x_start), random demand and lead times,
    the week-by-week dynamics E1–E6, total cost, fill rates, objective and fill-rate constraints.
    `seed` selects the random demand (optimization: Settings!RandomSeed; out-of-sample: other seeds)."""
    T = _T(env)
    max_sample = OVERSAMPLE_FACTOR * int(resolution)
    H, rho_min = d.horizon, d.rho_min
    keep = []

    # ---- decision variables (K01) ------------------------------------------------------------
    variables = [env.ordinal(0, layout.upper(i), int(x_start[i])) for i in range(len(layout))]

    # ---- variable priorities (K10): Seeker tries to change high-priority variables more often --
    for (j, _t, _kind), v in zip(layout.index, variables):
        env.variable_priority(v, T.C(VARIABLE_PRIORITY.get(j, 1.0)))     # only used by the search (section 5)

    sig, dl = {}, {}
    for (j, t, kind), v in zip(layout.index, variables):
        (sig if kind == "sigma" else dl)[(j, t)] = v

    def policy(j, t):
        if (j, t) not in sig:
            return None                                          # inactive week: never order (K04)
        m = layout.mult[j]
        s = m * sig[(j, t)]
        return s, s + m * dl[(j, t)]

    # ---- random data: demand from quantiles, lead times (G02, E03) ----------------------------
    QD = _quantile_distribution_class(skr)
    seeds = np.random.SeedSequence(seed).spawn(len(d.channels) * H + 2 + 2 * len(d.rms))
    demand = {}
    for ci in range(len(d.channels)):
        for t in range(1, H + 1):
            ud = QD(d.demand_q[ci, t], d.levels, seeds[ci * H + t - 1])
            keep.append(ud)
            demand[(ci, t)] = _with_max_sample(env.user_distribution, ud, max_sample=max_sample)

    def lead_terms(lt, weeks):
        if len(lt.values) == 1:
            return {t: int(lt.values[0]) for t in weeks}, True
        return {t: _with_max_sample(env.categorical_distribution, [float(p) for p in lt.probs],
                                    [float(v) for v in lt.values], max_sample=max_sample)
                for t in weeks}, False

    def open_arrival_terms(orders, lt):
        arrs, prev = [], None
        for o in orders:                                         # sorted by placement (section 1)
            if o["certain"]:
                a = int(o["arrival"])
            else:
                ok = lt.values + o["placed"] >= 1
                vals, p = (lt.values[ok], lt.probs[ok] / lt.probs[ok].sum()) if ok.any() else (np.array([1 - o["placed"]]), np.array([1.0]))
                a = _with_max_sample(env.categorical_distribution, [float(x) for x in p],
                                     [float(o["placed"] + v) for v in vals], max_sample=max_sample) \
                    if len(vals) > 1 else int(o["placed"] + vals[0])
            if prev is not None:                                 # open orders do not overtake each other either
                if isinstance(a, int) and isinstance(prev, int):
                    a = max(a, prev)
                else:
                    a = T.max(a if not isinstance(a, int) else T.C(a), prev if not isinstance(prev, int) else T.C(prev))
            arrs.append(a)
            prev = a
        return arrs

    weeks_fg = [t for t in range(1, H + 1) if ("FG", t) in sig]
    lead_prod, fixed_p = lead_terms(d.prod_lead_time, weeks_fg)
    open_prod_arr = open_arrival_terms(d.open_prod, d.prod_lead_time)
    fg_stream = _Stream(T, d.open_prod, open_prod_arr, lead_prod, fixed_p)
    lead_rm, open_rm_arr, rm_stream = {}, {}, {}
    for k in d.rms:
        weeks_k = [t for t in range(1, H + 1) if (k.id, t) in sig]
        lead_rm[k.id], fixed_k = lead_terms(k.lead_time, weeks_k)
        open_rm_arr[k.id] = open_arrival_terms(d.open_rm[k.id], k.lead_time)
        rm_stream[k.id] = _Stream(T, d.open_rm[k.id], open_rm_arr[k.id], lead_rm[k.id], fixed_k)

    # ---- initial stock (J01): one Term per lot bucket ------------------------------------------
    fg = {}
    for a, q in d.init_fg:
        fg[a] = T.C(q) if a not in fg else fg[a] + q
    rm = {k.id: {} for k in d.rms}
    for k in d.rms:
        for a, q in d.init_rm[k.id]:
            key = a if k.perishable else 0
            rm[k.id][key] = T.C(q) if key not in rm[k.id] else rm[k.id][key] + q

    costs = {n: [] for n in ("rm_ordering", "production", "transport", "holding", "waste")}
    fill_rate, prod_qty, rm_order, prod_arr, rm_arr = {}, {}, {}, {}, {}
    fg_end, fg_waste, rm_end, rm_waste, holding = {}, {}, {}, {}, {}

    def tier_cost(tiers, q):
        if tiers.mode == "Incremental":
            return T.sum([c * T.min(env.max_0(q - f), to - f) if np.isfinite(to) else c * env.max_0(q - f)
                          for f, to, c in zip(tiers.froms, tiers.tos, tiers.costs)])
        price = T.C(tiers.costs[-1])
        for to, c in reversed(list(zip(tiers.tos[:-1], tiers.costs[:-1]))):
            price = env.if_(q < to, T.C(c), price)
        return price * q

    for t in range(1, H + 1):
        # ---- E1: receipts with full shelf life (F01) -------------------------------------------
        for a, term in fg_stream.receipts(t, d.fg_shelf_life).items():
            fg[a] = term if fg.get(a) is None else fg[a] + term
        for k in d.rms:
            for a, term in rm_stream[k.id].receipts(t, k.shelf_life if k.perishable else 0).items():
                key = a if k.perishable else 0
                rm[k.id][key] = term if rm[k.id].get(key) is None else rm[k.id][key] + term

        # ---- E2: FG review → production order (C01, C06, D02) --------------------------------
        ip_fg = T.sum([v for a, v in fg.items() if a >= rho_min]) + fg_stream.pipeline(t)
        q = None
        pol = policy("FG", t)
        if pol is not None:
            s, S = pol
            q_hat = T.min(d.prod_max, T.max(d.prod_moq, d.prod_batch * env.ceil((S - ip_fg) / d.prod_batch - EPS)))
            caps = []
            for k in d.rms:
                if k.perishable:
                    avail = T.sum([v for a, v in rm[k.id].items() if a >= k.min_remaining])
                else:
                    avail = rm[k.id].get(0, T.C(0))
                caps.append(avail / k.bom)
            cap = caps[0] if len(caps) == 1 else env.min(caps)
            q_chk = T.min(q_hat, d.prod_batch * env.floor(cap / d.prod_batch + EPS))
            q = (ip_fg < s) * (q_chk >= d.prod_moq - EPS) * q_chk
            prod_arr[t] = fg_stream.add(t, q)
            prod_qty[t] = q
            for k in d.rms:                                       # transfer RMW → KAJA (B02), FEFO
                need = k.bom * q
                if k.perishable:
                    _fefo(T, rm[k.id], sorted(a for a in rm[k.id] if a >= k.min_remaining), need)
                else:
                    rm[k.id][0] = rm[k.id].get(0, T.C(0)) - need
            costs["production"] += [d.prod_fixed * (q > 0), tier_cost(d.prod_tiers, q)]
            if d.transport_tiers is not None:
                costs["transport"].append(tier_cost(d.transport_tiers, q))

        # ---- E3: RM review on echelon position (C03, C04, D01) --------------------------------
        ip_after = ip_fg if q is None else ip_fg + q
        for k in d.rms:
            pol = policy(k.id, t)
            if pol is None:
                continue
            s, S = pol
            usable_next = T.sum([v for a, v in rm[k.id].items() if a >= k.min_remaining + 1]) if k.perishable \
                else rm[k.id].get(0, T.C(0))
            ip_k = usable_next + rm_stream[k.id].pipeline(t) + k.bom * ip_after
            qk = (ip_k < s) * T.min(k.max_order, T.max(k.moq, k.multiple * env.ceil((S - ip_k) / k.multiple - EPS)))
            rm_arr[(k.id, t)] = rm_stream[k.id].add(t, qk)
            rm_order[(k.id, t)] = qk
            costs["rm_ordering"] += [k.fixed_cost * (qk > 0), k.var_cost * qk]

        # ---- E4: demand, channels by priority, FEFO among eligible lots (H01–H04) -------------
        for ci, ch in enumerate(d.channels):
            dem = demand[(ci, t)]
            served = _fefo(T, fg, sorted(a for a in fg if a >= ch.min_remaining), dem)
            fill_rate[(ci, t)] = env.if_(dem > 0, served / T.max(dem, 1.0), T.C(1))

        # ---- E5: holding cost on end-of-week stock (I05) -------------------------------------
        fg_end[t] = T.sum(list(fg.values()))
        hold = [d.fg_holding * fg_end[t]]
        for k in d.rms:
            rm_end[(k.id, t)] = T.sum(list(rm[k.id].values())) if rm[k.id] else 0.0
            if rm[k.id]:
                hold.append(k.holding * rm_end[(k.id, t)])
        holding[t] = T.sum(hold)
        costs["holding"].append(holding[t])

        # ---- E6: ageing; unusable lots are wasted immediately (F05, C02, C04) ----------------
        fg_waste[t] = T.sum([v for a, v in fg.items() if a <= rho_min]) if any(a <= rho_min for a in fg) else 0.0
        waste = [d.fg_waste * fg_waste[t]] if not isinstance(fg_waste[t], float) else []
        fg = {a - 1: v for a, v in fg.items() if a > rho_min}
        for k in d.rms:
            if k.perishable:
                rm_waste[(k.id, t)] = 0.0
                if any(a <= k.min_remaining for a in rm[k.id]):
                    rm_waste[(k.id, t)] = T.sum([v for a, v in rm[k.id].items() if a <= k.min_remaining])
                    waste.append(k.waste * rm_waste[(k.id, t)])
                rm[k.id] = {a - 1: v for a, v in rm[k.id].items() if a > k.min_remaining}
        if waste:
            costs["waste"].append(T.sum(waste))

    cost_parts = {n: T.sum(v) for n, v in costs.items()}
    total = T.sum(list(cost_parts.values()))
    objective = env.aggregate_mean(total)
    # ---- fill-rate chance constraints (H05–H07) ----------------------------------------------
    alpha = OPTIMIZATION_CHANCE_LEVEL or d.chance_level
    t0 = 1 + d.prod_lead_time.min if d.enforce_from_first_controllable else 1
    constrained = [(ci, t) for ci in range(len(d.channels)) for t in range(t0, H + 1)]
    fr_share = {(ci, t): env.aggregate_relative_frequency_geq(fill_rate[(ci, t)], T.C(ch.target - 1e-6))
                for ci, ch in enumerate(d.channels) for t in range(1, H + 1)}      # share of scenarios meeting target
    fr_stat = {}
    for ci, t in constrained if FILL_RATE_CONSTRAINT_FORM != "none" else []:
        ch = d.channels[ci]
        if FILL_RATE_CONSTRAINT_FORM == "frequency":
            fr_stat[(ci, t)] = fr_share[(ci, t)]
            env.enforce_geq(fr_share[(ci, t)], alpha, f"FR_{ch.id}_w{t}")
        else:
            fr_stat[(ci, t)] = env.aggregate_quantile(fill_rate[(ci, t)], 1.0 - alpha, False)
            env.enforce_geq(fr_stat[(ci, t)], ch.target, f"FR_{ch.id}_w{t}")
    return SeekerModel(env, T, variables, objective, total, cost_parts, fill_rate, fr_share, fr_stat, constrained,
                       demand, lead_prod, lead_rm, open_prod_arr, open_rm_arr, prod_qty, prod_arr, rm_order, rm_arr,
                       fg_end, fg_waste, rm_end, rm_waste, holding, keep)


# ============================================================================
# 4. START POLICY — the warm start: one fixed (s,S) policy from the forecast (K07)
# ============================================================================
def start_policy(d, z=None, extra_fg=None, extra_rm=None, lot_weeks=None):
    """Seeker's starting point (warm start) and the baseline for the reported saving. Pure formula on the
    forecast; no model evaluation. Parameters default to the START_* settings in section 0.

      μ_u   = expected total demand in week u   = Σ over channels of the mean of the 10 forecast quantiles
      σ_u   = standard deviation of week u       = √ Σ over channels ((Q95 − Q05) / 3.29)²   (normal approx.)
      cover(t, W) = Σ_{u=t}^{t+W−1} μ_u + z · √ Σ_{u=t}^{t+W−1} σ_u²     (window cut at the horizon)
      lot   = max(production MOQ, START_LOT_WEEKS × average weekly μ)

      FG1 at the DC, every active week t:      s_t = cover(t, L_prod,max + 1 + START_EXTRA_WEEKS_FG)
                                               S_t = s_t + lot
      RM k at the RMW (echelon), active weeks: s_t = BOM_k · cover(t, L_k,max + L_prod,max + 1 + START_EXTRA_WEEKS_RM)
                                               S_t = s_t + max(MOQ_k, BOM_k · lot)
      Inactive tail weeks: s = S = 0 (K04). The values are then rounded to the decision steps (K11)."""
    z = START_Z if z is None else z
    extra_fg = START_EXTRA_WEEKS_FG if extra_fg is None else extra_fg
    extra_rm = START_EXTRA_WEEKS_RM if extra_rm is None else extra_rm
    lot_weeks = START_LOT_WEEKS if lot_weeks is None else lot_weeks
    H = d.horizon
    mean = d.demand_q[:, 1:, :].mean(2).sum(0)                    # μ_u, all channels
    sd = np.sqrt((((d.demand_q[:, 1:, -1] - d.demand_q[:, 1:, 0]) / 3.29) ** 2).sum(0))
    mean, sd = np.concatenate(([0], mean)), np.concatenate(([0], sd))

    def cover(t, weeks):
        tt = np.arange(t, min(H, t + weeks - 1) + 1)
        return mean[tt].sum() + z * math.sqrt((sd[tt] ** 2).sum())

    pol = {}
    Lp = d.prod_lead_time.max
    lot = max(d.prod_moq, lot_weeks * mean[1:].mean())
    s, S = np.zeros(H + 1), np.zeros(H + 1)
    for t in range(1, H - d.prod_lead_time.min + 1):
        s[t] = cover(t, Lp + 1 + extra_fg)
        S[t] = s[t] + lot
    pol["FG"] = (s, S)
    for k in d.rms:
        s, S = np.zeros(H + 1), np.zeros(H + 1)
        for t in range(1, H - d.prod_lead_time.min - k.lead_time.min + 1):
            s[t] = k.bom * cover(t, k.lead_time.max + Lp + 1 + extra_rm)
            S[t] = s[t] + max(k.moq, k.bom * lot)
        pol[k.id] = (s, S)
    return pol


# ============================================================================
# Helpers to talk to Seeker (used by sections 5–6)
# ============================================================================
def import_seeker():
    try:
        import seeker as skr
    except ImportError as e:
        raise ImportError("Seeker is not installed: run  pip install ioseeker") from e
    return skr


def new_env(skr, license_file, n_scenarios, speed):
    env = skr.Env(license_file, True)                       # True = stochastic environment
    env.set_stochastic_parameters(int(n_scenarios), speed)
    return env


def single_number(model, term, kind="mean", threshold=0.0):
    """Creates an aggregate of `term` in the model and returns a function that reads it as ONE number
    after env.evaluate(). kind: mean | p10 | p50 | p90 | share (= share of scenarios with term ≥ threshold).
    Plain numbers and Terms that are equal in every scenario (e.g. week-1 decisions) are read directly."""
    env, T = model.env, model.T
    if not hasattr(term, "get_value"):                      # plain number (e.g. a fixed arrival week)
        v = float(term)
        return lambda: float(v >= threshold - 1e-6) if kind == "share" else v
    try:
        if kind == "mean":
            agg = env.aggregate_mean(term)
        elif kind == "share":
            agg = env.aggregate_relative_frequency_geq(term, T.C(threshold - 1e-6))
        else:
            agg = env.aggregate_quantile(term, {"p10": 0.10, "p50": 0.50, "p90": 0.90}[kind], False)
        return agg.get_value
    except Exception:  # noqa: BLE001 — Seeker refuses aggregates of Terms that do not vary across scenarios
        return (lambda: float(term.get_value() >= threshold - 1e-6)) if kind == "share" else term.get_value


def constraints_met(model, level):
    return sum(model.fr_share[k].get_value() >= level - 1e-9 for k in model.constrained)


# ============================================================================
# 4b. FEASIBILITY CHECK — which fill-rate rules fail whatever we decide (K17)
# ============================================================================
def max_supply_policy(d, layout):
    """Every active week a reorder point above any reachable inventory position, so every review orders
    the maximum: each raw material its max order, FG its max production (still limited by the raw
    materials on hand, MOQ and batch — the rules of section 3 apply unchanged)."""
    H = d.horizon
    fg_big = H * d.prod_max + sum(q for _, q in d.init_fg) + sum(o["qty"] for o in d.open_prod)
    rms = {k.id: k for k in d.rms}
    pol = {}
    for j, _, last in layout.points:
        k = rms.get(j)
        big = fg_big if k is None else (k.bom * fg_big + H * k.max_order + sum(q for _, q in d.init_rm[j])
                                        + sum(o["qty"] for o in d.open_rm[j]))
        s = np.zeros(H + 1)
        s[1:last + 1] = big
        pol[j] = (s, s.copy())
    return pol


def feasibility_check(skr, d, layout, license_file):
    """Seeker evaluates the maximum-supply policy once, on the optimization scenarios (same count and seed).
    Returns {(channel, week): share of scenarios meeting the target} for the constrained rules."""
    lay = copy.deepcopy(layout)                     # own bounds, wide enough for the huge reorder points
    pol = max_supply_policy(d, lay)
    lay.include_start(pol, margin=1.0)
    with phase("4b. Feasibility check: Seeker evaluates the maximum-supply policy"):
        env = new_env(skr, license_file, d.n_scenarios, 0.0)
        model = build_model(skr, env, d, lay, lay.encode(pol), d.seed, d.n_scenarios)
        env.evaluate()
        share = {k: model.fr_share[k].get_value() for k in model.constrained}
        env.end()
    return share


def feasibility_note(d, start_share, max_share):
    """One line: rules that neither the start policy nor the maximum-supply policy meets. No single policy
    is an upper bound (producing everything at once can starve later weeks of fresh stock), so a rule only
    counts as impossible if both fail it (tested on the 25 stress cases against 11 policies: same verdict)."""
    bad = sorted((ci, t) for ci, t in max_share
                 if max(start_share[(ci, t)], max_share[(ci, t)]) < d.chance_level - EPS)
    if not bad:
        return "OK — no week fails regardless of the decisions"
    parts = []
    for ci, ch in enumerate(d.channels):
        weeks = [t for c, t in bad if c == ci]
        if weeks:
            runs, a = [], weeks[0]                                      # 15, 16, 19 → "15–16, 19"
            for prev, cur in zip(weeks, weeks[1:] + [None]):
                if cur != prev + 1:
                    runs.append(f"{a}" if a == prev else f"{a}–{prev}")
                    a = cur
            parts.append(f"{ch.id} w{', '.join(runs)}")
    best = [max(start_share[k], max_share[k]) for k in bad]
    rng = f"{min(best):.1%}" if min(best) == max(best) else f"{min(best):.1%}–{max(best):.1%}"
    return (f"{len(bad)} rule(s) fail whatever we decide (supply too late or too small): " + "; ".join(parts)
            + f" (best share {rng}, needed {d.chance_level:.0%})")


# ============================================================================
# 5. OPTIMIZATION — Seeker searches; in-sample results are Seeker's own numbers
# ============================================================================
def optimize(skr, d, layout, x0, license_file, time_limit, max_share=None):
    env = new_env(skr, license_file, d.n_scenarios, STOCHASTIC_SPEED)
    version = str(env.version())
    print(f"Seeker version: {version} | speed {STOCHASTIC_SPEED}, constraint form '{FILL_RATE_CONSTRAINT_FORM}', "
          f"chance level {OPTIMIZATION_CHANCE_LEVEL or d.chance_level}, {d.n_scenarios} scenarios", flush=True)
    with phase("5. Seeker model build"):
        model = build_model(skr, env, d, layout, x0, d.seed, d.n_scenarios)
        parts = {n: single_number(model, t) for n, t in model.cost_parts.items()}
    with phase("5. Seeker evaluates the start policy"):
        env.evaluate()
        start = dict(cost=model.objective.get_value(), met=constraints_met(model, d.chance_level))
    print(f"Start policy on Seeker's scenarios: expected cost {start['cost']:,.0f}, "
          f"{start['met']}/{len(model.constrained)} fill-rate constraints met", flush=True)
    feasibility = "not run"
    if max_share is not None:                                       # section 4b
        feasibility = feasibility_note(d, {k: model.fr_share[k].get_value() for k in model.constrained}, max_share)
        print(f"Feasibility check: {feasibility}", flush=True)
    env.set_report(REPORT_INTERVAL_SECONDS)
    with phase(f"5. Seeker optimization (time limit {time_limit:.0f} s)"):
        env.minimize(model.objective, time_limit)
    seconds = TIMINGS[-1][1]
    with phase("5. Reading Seeker's solution and in-sample results"):
        x = [int(round(v.get_value())) for v in model.variables]
        env.evaluate()
        ins = dict(cost=model.objective.get_value(), parts={n: f() for n, f in parts.items()},
                   fr_share={k: v.get_value() for k, v in model.fr_share.items()},
                   met=constraints_met(model, d.chance_level), n=d.n_scenarios, start=start,
                   status=str(env.get_status()), evaluations=int(env.get_number_evaluations()),
                   seconds=seconds, version=version, constrained=list(model.constrained),
                   feasibility=feasibility)
    env.end()
    return x, ins


# ============================================================================
# 6. OUT-OF-SAMPLE CHECK — same model, fresh demand, decisions fixed, one evaluation (no search)
# ============================================================================
def report_numbers(model, d):
    """The single numbers the report needs from one evaluated model: {key: reader}."""
    H, R = d.horizon, d.review_period
    num = {"cost": model.objective.get_value}
    for n, t in model.cost_parts.items():
        num[("part", n)] = single_number(model, t)
    for (ci, t), term in model.fill_rate.items():
        num[("fr_mean", ci, t)] = single_number(model, term)
        num[("fr_share", ci, t)] = model.fr_share[(ci, t)].get_value
    series = [("prod", t, model.prod_qty.get(t, 0.0)) for t in range(1, H + 1)] + \
             [(k.id, t, model.rm_order.get((k.id, t), 0.0)) for k in d.rms for t in range(1, H + 1)]
    for j, t, q in series:
        for kind in ("mean", "p10", "p90"):
            num[(j, kind, t)] = single_number(model, q, kind)
        num[(j, "p_order", t)] = single_number(model, q, "share", 0.5)       # quantities are ≥ MOQ when > 0
    for t in range(1, R + 1):                                                 # arrival week of commit-now orders
        num[("prod", "arrival", t)] = single_number(model, model.prod_arrival.get(t, 0.0), "p50")
        for k in d.rms:
            num[(k.id, "arrival", t)] = single_number(model, model.rm_arrival.get((k.id, t), 0.0), "p50")
    for t in range(1, H + 1):
        num[("fg_end", t)] = single_number(model, model.fg_end[t])
        num[("fg_waste", t)] = single_number(model, model.fg_waste[t])
        num[("holding", t)] = single_number(model, model.holding[t])
        for k in d.rms:
            num[(k.id, "end", t)] = single_number(model, model.rm_end[(k.id, t)])
            if k.perishable:
                num[(k.id, "waste", t)] = single_number(model, model.rm_waste[(k.id, t)])
    return num


def out_of_sample(skr, d, layout, x, license_file):
    """Evaluates decisions x on OUT_OF_SAMPLE_SCENARIOS fresh scenarios (seeds ≠ the optimization seed), in
    batches of OUT_OF_SAMPLE_BATCH. Means and shares are combined exactly; P10/P90 are batch averages."""
    sizes = [min(OUT_OF_SAMPLE_BATCH, OUT_OF_SAMPLE_SCENARIOS - s) for s in range(0, OUT_OF_SAMPLE_SCENARIOS, OUT_OF_SAMPLE_BATCH)]
    total = {}
    for b, n in enumerate(sizes):
        with phase(f"6. Out-of-sample batch {b + 1}/{len(sizes)}: build + evaluate on {n:,} fresh scenarios"):
            env = new_env(skr, license_file, n, 0.0)
            model = build_model(skr, env, d, layout, x, d.seed + 1 + b, n)
            readers = report_numbers(model, d)
            env.evaluate()
            for key, read in readers.items():
                total[key] = total.get(key, 0.0) + read() * n
            env.end()
    oos = {key: v / sum(sizes) for key, v in total.items()}
    oos["n"] = sum(sizes)
    odd = [k for k in oos if isinstance(k, tuple) and k[1] == "p10" and oos[k] > oos[(k[0], "p90", k[2])] + 1e-6]
    if odd:                                                  # plausibility gate on Seeker's quantile convention
        print(f"WARNING: P10 > P90 for {len(odd)} decision series — check aggregate_quantile semantics", flush=True)
    return oos


# ============================================================================
# 7. REPORT — results workbook and console summary (only formatting; all numbers come from Seeker)
# ============================================================================
def build_report(d, layout, x, ins, oos):
    pol = layout.decode(x)
    H, R = d.horizon, d.review_period
    weeks = list(range(1, H + 1))
    dates = [(d.start_date + pd.Timedelta(weeks=t - 1)).date() for t in weeks]
    status_of = lambda t: ("COMMIT NOW (exact)" if t == 1 else
                           "COMMIT RULE: apply (s,S) to actual stock" if t <= R else
                           "Indicative — re-optimized at next review")
    nv = f"{oos['n']:,} fresh scenarios"
    # --- 1. policy table (L01)
    pol_rows = []
    for t in weeks:
        row = {"Week": t, "WeekStart": dates[t - 1]}
        for j, _, last in layout.points:
            row[f"{j} s"], row[f"{j} S"] = pol[j][0][t], pol[j][1][t]
            row[f"{j} active"] = "yes" if t <= last else "fixed 0"
        pol_rows.append(row)
    # --- 2. derived decisions (L02), distribution over the fresh scenarios
    dec_rows = []
    for t in weeks:
        row = {"Week": t, "WeekStart": dates[t - 1], "Status": status_of(t)}
        row.update({"Production mean": oos[("prod", "mean", t)], "Production P10": oos[("prod", "p10", t)],
                    "Production P90": oos[("prod", "p90", t)], "P(production)": oos[("prod", "p_order", t)]})
        for k in d.rms:
            row.update({f"{k.id} order mean": oos[(k.id, "mean", t)], f"{k.id} order P10": oos[(k.id, "p10", t)],
                        f"{k.id} order P90": oos[(k.id, "p90", t)], f"P({k.id} order)": oos[(k.id, "p_order", t)],
                        f"{k.id} transfer→KAJA mean": k.bom * oos[("prod", "mean", t)]})
        dec_rows.append(row)
    # --- 3. commit now (L03): week 1 is the same in every scenario (review before demand, A04)
    q1 = oos[("prod", "mean", 1)]
    commit = [["Week 1", "Production order FG1 (KAJA)", q1,
               f"arrives at DC in week {oos[('prod', 'arrival', 1)]:.0f}" if q1 > 0 else "no production"]]
    commit += [["Week 1", f"Transfer {k.id} RMW → KAJA", k.bom * q1, "for this week's production order"] for k in d.rms]
    for k in d.rms:
        qk = oos[(k.id, "mean", 1)]
        commit.append(["Week 1", f"Supplier order {k.id}", qk,
                       f"arrives at RMW in week {oos[(k.id, 'arrival', 1)]:.0f}" if qk > 0 else "no order"])
    for t in range(2, R + 1):
        commit.append([f"Week {t}", "Rule FG1 production",
                       f"if IP_FG < {pol['FG'][0][t]:,.0f}: produce up to {pol['FG'][1][t]:,.0f}",
                       f"expected {oos[('prod', 'mean', t)]:,.0f} (P(order) {oos[('prod', 'p_order', t)]:.0%})"])
        for k in d.rms:
            commit.append([f"Week {t}", f"Rule {k.id} supplier order",
                           f"if echelon IP < {pol[k.id][0][t]:,.0f}: order up to {pol[k.id][1][t]:,.0f}",
                           f"expected {oos[(k.id, 'mean', t)]:,.1f}"])
    # --- 4. fill rates (L04)
    fr_rows, con = [], set(ins["constrained"])
    for ci, ch in enumerate(d.channels):
        for t in weeks:
            c = (ci, t) in con
            p_in, p_out = ins["fr_share"][(ci, t)], oos[("fr_share", ci, t)]
            fr_rows.append({"Channel": ch.id, "Week": t, "Target": ch.target, "Constrained": "yes" if c else "no (initial state)",
                            f"P(FR ≥ target) in-sample ({ins['n']} Seeker scenarios)": p_in,
                            "Status in-sample": ("OK" if p_in >= d.chance_level - EPS else "VIOLATED") if c else "info",
                            f"Mean FR ({nv})": oos[("fr_mean", ci, t)], f"P(FR ≥ target) ({nv})": p_out,
                            "Status out-of-sample": ("OK" if p_out >= d.chance_level - EPS else "BELOW LEVEL") if c else "info"})
    # --- 5. stock & waste (means over the fresh scenarios)
    sw = []
    for t in weeks:
        row = {"Week": t, "FG1 end stock (mean)": oos[("fg_end", t)], "FG1 waste (mean)": oos[("fg_waste", t)]}
        for k in d.rms:
            row[f"{k.id} end stock (mean)"] = oos[(k.id, "end", t)]
            if k.perishable:
                row[f"{k.id} waste (mean)"] = oos[(k.id, "waste", t)]
        row["Holding cost (mean)"] = oos[("holding", t)]
        sw.append(row)
    # --- summary
    n_con = len(con)
    oos_met = sum(oos[("fr_share", ci, t)] >= d.chance_level - EPS for ci, t in con)
    summ = [("Seeker status", ins["status"]), ("Seeker version", ins["version"]),
            ("Run time [s]", round(ins["seconds"], 1)), ("Seeker evaluations", ins["evaluations"]),
            ("Scenarios (Seeker in-sample / fresh out-of-sample)", f"{ins['n']} / {oos['n']}"),
            ("Decision variables / fill-rate constraints", f"{len(layout)} / {n_con}"),
            ("Seeker settings", f"speed {STOCHASTIC_SPEED}, constraint form {FILL_RATE_CONSTRAINT_FORM}, "
                                f"chance level {OPTIMIZATION_CHANCE_LEVEL or d.chance_level}"),
            ("Search space / priorities", f"10^{layout.search_space_log10():.0f} combinations; {VARIABLE_PRIORITY}"),
            ("Start policy (warm start)", f"z={START_Z:g}, extra weeks FG {START_EXTRA_WEEKS_FG} / RM {START_EXTRA_WEEKS_RM}, "
                                          f"lot {START_LOT_WEEKS} weeks"),
            ("Expected total cost (in-sample)", ins["cost"]),
            ("Expected total cost (out-of-sample)", oos["cost"]),
            ("Start policy cost (in-sample)", ins["start"]["cost"]),
            ("Saving vs. start policy (in-sample)", 1 - ins["cost"] / ins["start"]["cost"] if ins["start"]["cost"] else 0)]
    summ += [(f"  cost: {n} (in-sample / out-of-sample)", f"{v:,.0f} / {oos[('part', n)]:,.0f}") for n, v in ins["parts"].items()]
    summ += [("Feasibility check (section 4b)", ins.get("feasibility", "not run")),
             ("Constraints met in-sample", f"{ins['met']} of {n_con} (start policy {ins['start']['met']})"),
             ("Constraints met out-of-sample", f"{oos_met} of {n_con}"),
             ("Reorder points at their upper bound", bound_note(layout, x))]
    summ += [(f"Time: {n}", round(t, 1)) for n, t in TIMINGS]
    return {"Summary": pd.DataFrame(summ, columns=["Item", "Value"]),
            "Commit_Now": pd.DataFrame(commit, columns=["When", "Decision", "Quantity / rule", "Note"]),
            "Policy_sS": pd.DataFrame(pol_rows), "Decisions": pd.DataFrame(dec_rows),
            "FillRates": pd.DataFrame(fr_rows), "Stock_Waste": pd.DataFrame(sw)}


def write_report(sheets, path):
    from openpyxl.styles import Font, PatternFill
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        for name, df in sheets.items():
            df.to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            for c in ws[1]:
                c.font = Font(bold=True, color="FFFFFF")
                c.fill = PatternFill("solid", fgColor="1F3864")
            for col in ws.columns:
                ws.column_dimensions[col[0].column_letter].width = max(10, min(45, max(len(str(c.value or "")) for c in col) + 2))
            ws.freeze_panes = "B2"
            if name in ("Decisions", "Commit_Now"):
                for row in ws.iter_rows(min_row=2):
                    txt = " ".join(str(c.value) for c in row[:3])
                    color = "C6EFCE" if "COMMIT NOW" in txt or "Week 1" in txt else ("FFEB9C" if "COMMIT RULE" in txt or "Rule" in txt else None)
                    if color:
                        for c in row:
                            c.fill = PatternFill("solid", fgColor=color)
            if name == "FillRates":
                for row in ws.iter_rows(min_row=2):
                    if row[-2].value == "VIOLATED" or row[-1].value == "BELOW LEVEL":
                        for c in row:
                            c.fill = PatternFill("solid", fgColor="FFC7CE")
            for row in ws.iter_rows(min_row=2):
                for c in row:
                    if isinstance(c.value, (int, float)) and not isinstance(c.value, bool):
                        head, label = str(ws.cell(1, c.column).value), str(row[0].value)
                        pct = any(k in head for k in ("FR", "P(", "Target")) or "Saving" in label
                        c.number_format = "0.0%" if pct else ("#,##0" if abs(c.value) >= 100 or float(c.value).is_integer() else "#,##0.00")


# ============================================================================
# 8. RUN AND MAIN — run(): sections 1 → 7 in one call, returns everything (the API for apps and scripts)
#                   main(): run() + results workbook + console summary (Spyder: F5)
# ============================================================================
@dataclass
class RunResult:
    data: ModelData
    layout: PolicyLayout
    x: list                  # decision vector, order = layout.index
    policy: dict             # {stock point: (s array, S array)}, index t = 1..H (index 0 unused)
    ins: dict                # in-sample numbers from Seeker (section 5)
    oos: dict                # out-of-sample numbers (section 6)
    sheets: dict             # report tables {sheet name: DataFrame} (section 7)
    timings: list            # [(phase, seconds)]


def run(data_file=None, license_file=None, time_limit=None, skr=None):
    """Sections 1 → 7 without writing files. Settings come from section 0; arguments override the files and
    the time limit. Raises ValueError (invalid data; the message lists every problem), FileNotFoundError
    (data or license file) and ImportError (Seeker not installed). Progress is printed to stdout."""
    data_file, license_file = data_file or DATA_FILE, license_file or LICENSE_FILE
    TIMINGS.clear()
    if not Path(data_file).exists():
        raise FileNotFoundError(f"Data file not found: {data_file}  (set DATA_FILE in section 0)")
    with phase(f"1. Reading and checking {Path(data_file).name}"):
        d = load_data(data_file)
        validate(d)
    layout = PolicyLayout(d)                                                   # section 2
    time_limit = float(time_limit or TIME_LIMIT_SECONDS or d.time_limit)
    print(f"Data OK: horizon {d.horizon} weeks, {len(d.channels)} channels, {len(d.rms)} raw materials, "
          f"{len(layout)} decision variables. Seeker time limit {time_limit:.0f} s.", flush=True)
    if not Path(license_file).exists():
        raise FileNotFoundError(f"License file not found: {license_file}  (set LICENSE_FILE in section 0)")
    skr = skr or import_seeker()

    pol0 = start_policy(d)                                                     # section 4
    layout.include_start(pol0)
    x0 = layout.encode(pol0)
    print(f"Start policy (warm start): z={START_Z:g}, extra weeks FG {START_EXTRA_WEEKS_FG} / RM {START_EXTRA_WEEKS_RM}, "
          f"lot {START_LOT_WEEKS} weeks. Bounds: section 2, EXTRA_COVER_WEEKS {EXTRA_COVER_WEEKS}. "
          f"Search space 10^{layout.search_space_log10():.0f}.", flush=True)

    max_share = feasibility_check(skr, d, layout, license_file) if FEASIBILITY_CHECK else None   # section 4b
    x, ins = optimize(skr, d, layout, x0, license_file, time_limit, max_share)  # section 5
    if layout.at_bound(x):
        print(f"NOTE: reorder point(s) at their upper bound: {bound_note(layout, x)}", flush=True)
    oos = out_of_sample(skr, d, layout, x, license_file)                       # section 6
    sheets = build_report(d, layout, x, ins, oos)                              # section 7
    return RunResult(d, layout, x, layout.decode(x), ins, oos, sheets, list(TIMINGS))


def main():
    t_total = time.time()
    print(f"MEIO model with Seeker, script version {SCRIPT_VERSION}", flush=True)
    out_path = usable_output_path(OUTPUT_FILE)                                 # checked before the long part
    try:
        res = run()
    except (FileNotFoundError, ImportError) as e:
        sys.exit(str(e))
    sheets = res.sheets
    out_path = usable_output_path(out_path)                                    # may have been opened meanwhile
    with phase(f"7. Writing {out_path.name}"):
        try:
            write_report(sheets, out_path)
        except PermissionError:
            out_path = out_path.with_name(f"{Path(OUTPUT_FILE).stem}_{time.strftime('%Y%m%d_%H%M%S')}_b.xlsx")
            write_report(sheets, out_path)
    print("\nTime per phase:\n" + "\n".join(f"  {nm:<75} {sec:8.1f} s" for nm, sec in TIMINGS)
          + f"\n  {'TOTAL':<75} {time.time() - t_total:8.1f} s")
    fmt = lambda v: f"{v:.1%}" if isinstance(v, float) and abs(v) < 1 else (f"{v:,.0f}" if isinstance(v, (int, float)) else str(v))
    print("\n" + "\n".join(f"  {i:<52} {fmt(v)}" for i, v in sheets["Summary"].itertuples(index=False)
                            if not str(i).startswith("Time:")))
    print("\nCommit now:\n" + sheets["Commit_Now"].to_string(index=False))
    print(f"\nResults written to {Path(out_path).resolve()}")
    return 0


if __name__ == "__main__":
    main()
