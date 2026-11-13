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

THE FILL-RATE RULE (H04, H05) — what a fill-rate cell means
  One target per product (Items!TargetFillRate, e.g. 95%), the same for every channel. A "cell" is one channel in
  one week (from week 1 + production lead time on). The rule: the EXPECTED fill rate of every cell reaches the target.
      expected fill rate of (channel c, week t) = mean over the scenarios of served(c, t) ÷ demand(c, t)  ≥ target
  Example, target 95%, 100 scenarios, AMZ week 12: 94 fully served (100%), 3 at 80%, 3 at 0%
      → (94 × 100% + 3 × 80% + 3 × 0%) ÷ 100 = 96.4% ≥ 95% → OK. On average AMZ gets 96.4% of its week-12 demand.
  There is no separate chance level: how often and how badly a week may be short is inside this one number.

WHAT EACH SECTION DOES
  0. SETTINGS ............ everything you may want to change (time limit, scenarios, files, search)
  1. DATA ................ reads MEIO_Data_Template.xlsx and checks it: every check of the Excel sheet
                           'Checks' also runs in Python, so copies edited by a program are checked too
  2. DECISION VARIABLES .. maps (s,S) per stock point and week to Seeker integer variables; lower bound
                           0, upper bound from the forecast (raise via EXTRA_COVER_WEEKS in section 0)
  3. SEEKER MODEL ........ the model itself: random demand and lead times, week-by-week dynamics,
                           cost, fill rates, objective and fill-rate constraints
  4. START POLICY ........ one fixed, explainable (s,S) policy computed from the forecast (formula
                           below); Seeker starts its search from it (warm start)
  4b. FEASIBILITY CHECK .. one extra evaluation: which fill-rate rules fail whatever we decide
                           (supply cannot reach those weeks); one line in the console and Summary
  4c. START REPAIR ....... safety net, normally NOT used: only if the start policy misses fill-rate rules
                           that can be met, it is made feasible step by step before the search starts
                           (console: "START REPAIR USED", Summary row "Start repair (K21)")
  5. OPTIMIZATION ........ Seeker evaluates the start policy on its own NumScenarios scenarios ("start
                           check", → 4c if needed), then searches for the cheapest (s,S) policy within
                           the time limit ("in-sample")
  6. OUT-OF-SAMPLE CHECK . the same model (section 3) is built again with FRESH random demand
                           (OUT_OF_SAMPLE_SCENARIOS), the decisions fixed, and evaluated once; only this
                           model also builds the report series (stock, positions, flows, freshness, KPIs)
  7. REPORT .............. results workbook (incl. Bands = uncertainty per week, KPIs) + console summary,
                           incl. expected waste and a warning if it is high (L10)
  8. RUN AND MAIN ........ run(): sections 1 → 7 in one call, returns all results (API for apps;
                           optional overrides of NumScenarios / RandomSeed for one run);
                           main(): run() + results workbook + console summary; every phase timed

  REVIEW PERIOD R (Settings!ReviewPeriod, A03): the model is run every R weeks (weeks 1, 1+R, 1+2R, …).
  Each run commits every decision of weeks 1..R as exact quantities; nothing inside a cycle reacts to the demand
  that actually happens. In a run week the (s,S) rule uses the actual inventory position; in the other weeks it
  uses the PLANNED position = position at the last run + orders planned since − expected demand since. R = 1:
  every week is a run week (the v18 model).

  Inventory positions are NOT shelf-life aware (K22): the (s,S) rules compare s with positions that count
  all usable stock, also stock that will expire unsold; Seeker adapts through the weekly s,S values.

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
FILL_RATE_CONSTRAINT_FORM = "mean"            # "mean": expected fill rate per channel and week ≥ target (H05)
                                              # "none": no fill-rate constraints (diagnostic only!)
OPTIMIZATION_FILL_RATE = None                 # None = Excel Items!TargetFillRate of the product; a number in (0, 1]
                                              # replaces it for the whole run: search, checks and report (K08)
START_POLICY_OUT_OF_SAMPLE = False            # also evaluate the start policy on the fresh scenarios (+~75 s)
FEASIBILITY_CHECK = True                      # section 4b: ~25 s (one extra model); False skips it
START_REPAIR = True                           # section 4c: repair an infeasible start (K21); normally not needed
WASTE_WARNING_SHARE = 0.05                    # warn if expected waste > 5% of expected demand (L10)
HORIZON_MARGIN_WEEKS = 8                      # input warning X10 if horizon < echelon lead time + R + this (A02)
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
START_Z = None                 # safety factor z (× standard deviation of demand over the cover window):
                               # None = from the fill-rate target via the normal loss function (K07); a number fixes it
START_Z_STEP = 0.5             # start check: if the start misses cells, z is raised by this step …
START_Z_MAX = 4.0              # … up to this value (then the start repair of section 4c takes over)
START_EXTRA_WEEKS_FG = 2       # FG cover window = max production lead time + 1 + this many weeks
START_EXTRA_WEEKS_RM = 4       # RM cover window = max RM lead time + max production lead time + 1 + this many
START_LOT_WEEKS = 4            # order-up-to S = reorder point s + this many weeks of average demand (≥ MOQ,
                               # ≤ what one fresh lot can sell before it expires)

SCRIPT_VERSION = "2026-11-12 v18.1 (Seeker-only)"
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
    target: float            # fill-rate target of the cell = the product's target β (H04), same for every channel
    min_remaining: int       # rest shelf life needed ρ_c
    priority: int = 0        # ShortagePriority from the Channels sheet: 1 = served first (H02)


@dataclass
class ModelData:
    horizon: int
    review_period: int
    start_date: pd.Timestamp
    fill_rate_target: float  # target used in this run (K08): OPTIMIZATION_FILL_RATE if set, else Items!TargetFillRate
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
    channels: list           # sorted by ShortagePriority (1 = served first, H02)
    levels: np.ndarray       # quantile probability levels
    demand_q: np.ndarray     # (C, H+1, 10) quantiles, index t = 1..H
    init_fg: list            # [(remaining shelf life, qty)]
    init_rm: dict            # rm id -> [(remaining shelf life or None, qty)]
    open_prod: list          # dicts: qty, placed, arrival, certain, remaining
    open_rm: dict            # rm id -> list of such dicts
    data_status: str = "unknown"
    warnings: list = None    # input-check warnings (K19)
    overrides: dict = None   # run overrides (K20)
    fill_rate_target_excel: float = None   # Items!TargetFillRate of the product as read (K08)
    uom: dict = None         # item id -> unit of measure (report)

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


def _blank(v):
    return v is None or (isinstance(v, float) and math.isnan(v)) or (isinstance(v, str) and not v.strip())


def _f(v):
    """Number or None (blank). Text that is not a number → nan, so every comparison below fails safely."""
    if _blank(v):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


def _whole(v):
    x = _f(v)
    return x is not None and not math.isnan(x) and abs(x - round(x)) < EPS


def _rows(xls, name, keys):
    """Records of a sheet (header row 4) that have a value in at least one of the key columns."""
    df = pd.read_excel(xls, name, header=3, keep_default_na=False, na_values=[""])   # text like "None" stays text
    df = df.loc[:, [c for c in df.columns if not str(c).startswith("Unnamed")]]
    cols = [c for c in df.columns if any(str(c).startswith(k) for k in keys)]
    return df.loc[df[cols].notna().any(axis=1)].reset_index(drop=True)


def input_problems(xls):
    """The Excel sheet 'Checks' (C01–C14, X01–X07) in Python, on the raw sheets, so a workbook edited by a
    program (no cached formula results) is checked as strictly as one checked by Excel (K19).
    Returns (errors, warnings), each a list of messages naming sheet, row and field."""
    E, W = [], []
    g = lambda r, prefix: next((r[c] for c in r.index if str(c).startswith(prefix)), None)
    row_no = lambda i: i + 5                                             # Excel row of record i

    # ---- C01 Settings: every parameter has a value
    st_df = _rows(xls, "Settings", ["Parameter"])
    st = {}
    for i, r in st_df.iterrows():
        st[str(r["Parameter"]).strip()] = r["Value"]
        if _blank(r["Value"]):
            E.append(f"Settings row {row_no(i)}: parameter {r['Parameter']} has no value")
    H = _f(st.get("HorizonPeriods"))
    # ---- X03 / X06
    if not (_whole(H) and 1 <= H <= 52):
        E.append("Settings: HorizonPeriods must be a whole number between 1 and 52")
        H = None
    rp = _f(st.get("ReviewPeriod"))
    if not (_whole(rp) and 1 <= rp <= (H or 52)):
        E.append("Settings: ReviewPeriod must be a whole number between 1 and HorizonPeriods")
    for p in ("NumScenarios", "RandomSeed"):
        if not _whole(st.get(p)) or _f(st.get(p)) < (1 if p == "NumScenarios" else 0):
            E.append(f"Settings: {p} must be a whole number{' ≥ 1' if p == 'NumScenarios' else ''}")

    # ---- C02 Locations
    loc = _rows(xls, "Locations", ["LocationID"])
    loc_type = {}
    for i, r in loc.iterrows():
        lid = r["LocationID"]
        if lid in loc_type:
            E.append(f"Locations row {row_no(i)}: duplicate LocationID {lid}")
        loc_type[lid] = r["Type"]
        if _blank(r["Name"]) or _blank(r["Type"]):
            E.append(f"Locations row {row_no(i)}: Name and Type must be filled")
    # ---- C03 Items
    it = _rows(xls, "Items", ["ItemID"])
    item_type, item_sl = {}, {}
    for i, r in it.iterrows():
        iid = r["ItemID"]
        if iid in item_type:
            E.append(f"Items row {row_no(i)}: duplicate ItemID {iid}")
        item_type[iid] = r["ItemType"]
        sl = _f(g(r, "ShelfLife"))
        item_sl[iid] = sl
        if _blank(r["Name"]) or _blank(r["ItemType"]) or _blank(r["UoM"]):
            E.append(f"Items row {row_no(i)}: Name, ItemType and UoM must be filled")
        if sl is not None and not (sl > 0 and _whole(sl)):
            E.append(f"Items row {row_no(i)}: ShelfLife of {iid} must be a whole number > 0 (or empty = no expiry)")
        tf = _f(g(r, "TargetFillRate"))                                        # H04: one target per product
        if r["ItemType"] == "FG" and (tf is None or math.isnan(tf) or not 0 < tf <= 1):
            E.append(f"Items row {row_no(i)}: TargetFillRate of {iid} must be in (0, 1] (one target per product, H04)")
        if r["ItemType"] == "RM" and tf is not None:
            E.append(f"Items row {row_no(i)}: TargetFillRate is only for finished goods; leave it empty for {iid}")
    # ---- C08 LeadTimeDist
    ltd = _rows(xls, "LeadTimeDist", ["DistID"])
    dist_sum, dist_max = {}, {}
    for i, r in ltd.iterrows():
        v, p = g(r, "Value"), _f(r["Probability"])
        if _blank(v) or p is None:
            E.append(f"LeadTimeDist row {row_no(i)}: Value and Probability must be filled")
            continue
        if not (_whole(v) and _f(v) >= 0):
            E.append(f"LeadTimeDist row {row_no(i)}: lead time {v} of {r['DistID']} must be whole weeks ≥ 0")
        if math.isnan(p) or not 0 < p <= 1:
            E.append(f"LeadTimeDist row {row_no(i)}: probability of {r['DistID']} must be in (0, 1]")
        dist_sum[r["DistID"]] = dist_sum.get(r["DistID"], 0.0) + (0.0 if math.isnan(p) else p)
        if _whole(v):
            dist_max[r["DistID"]] = max(dist_max.get(r["DistID"], 0.0), _f(v))
    for did, s in dist_sum.items():
        if abs(s - 1) > 1e-6:
            E.append(f"LeadTimeDist: probabilities of {did} sum to {s:g}, must be 1")
    dists = set(dist_sum)
    # ---- C11 CostTiers
    ct = _rows(xls, "CostTiers", ["TierTableID"])
    tiers = {}
    for i, r in ct.iterrows():
        if any(_blank(r[c]) for c in ("TierNo", "FromQty", "CostPerUnit", "PricingMode", "QuantityBasis")):
            E.append(f"CostTiers row {row_no(i)}: TierNo, FromQty, CostPerUnit, PricingMode, QuantityBasis must be filled")
            continue
        fr, to, c, no = _f(r["FromQty"]), _f(r["ToQty"]), _f(r["CostPerUnit"]), _f(r["TierNo"])
        if not fr >= 0 or not c >= 0:
            E.append(f"CostTiers row {row_no(i)}: FromQty and CostPerUnit must be ≥ 0")
        if to is not None and not to > fr:
            E.append(f"CostTiers row {row_no(i)}: ToQty must be greater than FromQty")
        tab = tiers.setdefault(r["TierTableID"], {})
        if no in tab:
            E.append(f"CostTiers row {row_no(i)}: tier {no:g} of {r['TierTableID']} appears twice")
        tab[no] = (fr, to)
    for tid, tab in tiers.items():
        nos = sorted(tab)
        if nos and tab[nos[0]][0] != 0:
            E.append(f"CostTiers {tid}: the first tier must start at 0")
        for a, b in zip(nos, nos[1:]):
            if tab[a][1] is None or abs(tab[a][1] - tab[b][0]) > EPS:
                E.append(f"CostTiers {tid}: tier {b:g} must start where tier {a:g} ends (no gaps or overlaps)")
    # ---- demand (needed by C05/C06 utilisation): mean of the quantiles per row
    dem = _rows(xls, "Demand", ["ChannelID"])
    qcols = [c for c in dem.columns if str(c).startswith("Q") and str(c)[1:].isdigit()]
    ch_ids = set(_rows(xls, "Channels", ["ChannelID"])["ChannelID"])
    seen, avg_week = set(), 0.0
    for i, r in dem.iterrows():
        t = r["Period"]
        qs = [_f(r[c]) for c in qcols]
        if r["ChannelID"] not in ch_ids:
            E.append(f"Demand row {row_no(i)}: unknown channel {r['ChannelID']}")
        if not (_whole(t) and 1 <= _f(t) <= 52):
            E.append(f"Demand row {row_no(i)}: Period must be a whole week 1..52")
        elif (r["ChannelID"], int(_f(t))) in seen:
            E.append(f"Demand row {row_no(i)}: {r['ChannelID']} week {int(_f(t))} appears twice")
        else:
            seen.add((r["ChannelID"], int(_f(t))))
        if any(q is None for q in qs):
            E.append(f"Demand row {row_no(i)}: all {len(qcols)} quantiles must be filled")
        elif any(math.isnan(q) or q < 0 for q in qs) or any(b < a - EPS for a, b in zip(qs, qs[1:])):
            E.append(f"Demand row {row_no(i)}: quantiles must be ≥ 0 and non-decreasing")
        elif H and _whole(t) and _f(t) <= H:
            avg_week += float(np.mean(qs)) / H
    # ---- X05 every channel has exactly one row for every week 1..H
    if H:
        for c in sorted(ch_ids):
            missing = [t for t in range(1, int(H) + 1) if (c, t) not in seen]
            if missing:
                E.append(f"Demand: channel {c} has no row for week(s) {', '.join(map(str, missing[:10]))}"
                         + (" …" if len(missing) > 10 else ""))
    # ---- C04 Sourcing
    src = _rows(xls, "Sourcing", ["ItemID"])
    eff_max, src_count, item_dist = {}, {}, {}                      # item_dist: (OrderType, item) → lead-time dist
    for i, r in src.iterrows():
        iid = r["ItemID"]
        src_count[iid] = src_count.get(iid, 0) + 1
        item_dist[("Purchase", iid)] = r["LeadTimeDistID"]
        need = ("SupplierID", "DestinationID", "MOQ", "OrderMultiple", "MaxOrderQty", "MaxQtyScope",
                "MaxOrdersPerPeriod", "LeadTimeDistID")
        if any(_blank(r[c]) for c in need):
            E.append(f"Sourcing row {row_no(i)}: all fields of {iid} must be filled")
            continue
        moq, mult, mx, mo = _f(r["MOQ"]), _f(r["OrderMultiple"]), _f(r["MaxOrderQty"]), _f(r["MaxOrdersPerPeriod"])
        bad = []
        if item_type.get(iid) != "RM":
            bad.append(f"{iid} is not an RM item")
        if loc_type.get(r["SupplierID"]) != "Supplier":
            bad.append(f"supplier {r['SupplierID']} is not a Supplier location")
        if r["DestinationID"] not in loc_type:
            bad.append(f"destination {r['DestinationID']} unknown")
        if r["LeadTimeDistID"] not in dists:
            bad.append(f"lead-time distribution {r['LeadTimeDistID']} unknown")
        if not (mult > 0 and moq > 0):
            bad.append("MOQ and OrderMultiple must be > 0")
        elif abs(moq / mult - round(moq / mult)) > 1e-9:
            bad.append("MOQ must be a multiple of OrderMultiple")
        if not mx >= moq:
            bad.append("MaxOrderQty must be ≥ MOQ")
        if not mo >= 1:
            bad.append("MaxOrdersPerPeriod must be ≥ 1")
        if bad:
            E.append(f"Sourcing row {row_no(i)}: " + "; ".join(bad))
        else:
            if abs(mx / mult - round(mx / mult)) > 1e-9:
                W.append(f"Sourcing row {row_no(i)}: MaxOrderQty of {iid} is not a multiple of OrderMultiple "
                         f"(the model uses {math.floor(mx / mult + EPS) * mult:,.0f})")
            eff_max[iid] = math.floor(mx / mult + EPS) * mult * mo
    # ---- C05 Production + X02
    prod = _rows(xls, "Production", ["ItemID"])
    prod_count = {}
    for i, r in prod.iterrows():
        iid = r["ItemID"]
        prod_count[iid] = prod_count.get(iid, 0) + 1
        item_dist[("Production", iid)] = r["LeadTimeDistID"]
        need = ("SiteID", "DestinationID", "MOQ", "BatchSize", "MaxQtyPerOrder", "MaxOrdersPerPeriod",
                "LeadTimeDistID", "ShelfLifeClockStart")
        if any(_blank(r[c]) for c in need):
            E.append(f"Production row {row_no(i)}: all fields of {iid} must be filled")
            continue
        moq, b, mx, mo = _f(r["MOQ"]), _f(r["BatchSize"]), _f(r["MaxQtyPerOrder"]), _f(r["MaxOrdersPerPeriod"])
        bad = []
        if item_type.get(iid) != "FG":
            bad.append(f"{iid} is not an FG item")
        if loc_type.get(r["SiteID"]) != "ProductionSite":
            bad.append(f"site {r['SiteID']} is not a ProductionSite location")
        if r["DestinationID"] not in loc_type:
            bad.append(f"destination {r['DestinationID']} unknown")
        if r["LeadTimeDistID"] not in dists:
            bad.append(f"lead-time distribution {r['LeadTimeDistID']} unknown")
        if not (b > 0 and moq > 0):
            bad.append("MOQ and BatchSize must be > 0")
        elif abs(moq / b - round(moq / b)) > 1e-9:
            bad.append("MOQ must be a multiple of BatchSize")
        if not mx >= moq:
            bad.append("MaxQtyPerOrder must be ≥ MOQ")
        if not mo >= 1:
            bad.append("MaxOrdersPerPeriod must be ≥ 1")
        util = avg_week / (mx * mo) if mx and mo and mx > 0 and mo > 0 else 0.0
        if util > 1:
            bad.append(f"average weekly demand {avg_week:,.0f} exceeds the production capacity {mx * mo:,.0f} per week")
        if bad:
            E.append(f"Production row {row_no(i)}: " + "; ".join(bad))
        else:
            if abs(mx / b - round(mx / b)) > 1e-9:
                W.append(f"Production row {row_no(i)}: MaxQtyPerOrder is not a multiple of BatchSize")
            if util > 0.9:
                W.append(f"Production row {row_no(i)}: capacity utilisation {util:.0%} (> 90%)")
    # ---- C06 BOM + X01 + X04
    bom = _rows(xls, "BOM", ["ParentItemID"])
    bom_parents = set()
    for i, r in bom.iterrows():
        par, comp = r["ParentItemID"], r["ComponentItemID"]
        bom_parents.add(par)
        if _blank(comp) or _blank(r["QtyPerParent"]):
            E.append(f"BOM row {row_no(i)}: ComponentItemID and QtyPerParent must be filled")
            continue
        qty, scrap = _f(r["QtyPerParent"]), _f(r["ScrapRate"]) or 0.0
        mr = _f(g(r, "MinRemainingShelfLife"))
        bad = []
        if item_type.get(par) != "FG":
            bad.append(f"parent {par} is not an FG item")
        if comp not in item_type:
            bad.append(f"component {comp} unknown")
        if not qty > 0:
            bad.append("QtyPerParent must be > 0")
        if not 0 <= scrap < 1:
            bad.append("ScrapRate must be in [0, 1)")
        sl = item_sl.get(comp)
        if sl is not None and mr is None:
            E.append(f"BOM row {row_no(i)}: MinRemainingShelfLife of perishable {comp} must be filled")
        elif sl is not None and not (_whole(mr) and 0 <= mr < sl):
            bad.append(f"freshness rule of {comp} must be whole weeks ≥ 0 and below its shelf life {sl:g} "
                       f"(usable window ≥ 1 week, X04)")
        if src_count.get(comp, 0) != 1:
            bad.append(f"{comp} needs exactly one Sourcing row (found {src_count.get(comp, 0)}, X01)")
        elif comp in eff_max and qty > 0 and 0 <= scrap < 1:
            util = qty * avg_week / (1 - scrap) / eff_max[comp] if eff_max[comp] > 0 else float("inf")
            if util > 1:
                bad.append(f"average weekly need of {comp} exceeds its maximum weekly supply ({util:.0%})")
            elif util > 0.9:
                W.append(f"BOM row {row_no(i)}: supply utilisation of {comp} {util:.0%} (> 90%)")
        if bad:
            E.append(f"BOM row {row_no(i)}: " + "; ".join(bad))
    for iid, typ in item_type.items():
        if typ == "FG" and (prod_count.get(iid, 0) != 1 or iid not in bom_parents):
            E.append(f"FG item {iid} needs exactly one Production row and at least one BOM line (X02)")
    # ---- C07 Lanes
    ln = _rows(xls, "Lanes", ["LaneID"])
    lane_ids = set()
    for i, r in ln.iterrows():
        bad = []
        if r["LaneID"] in lane_ids:
            bad.append(f"duplicate LaneID {r['LaneID']}")
        lane_ids.add(r["LaneID"])
        if any(_blank(r[c]) for c in ("FromID", "ToID", "ItemScope", "TransitTimeDistID")):
            E.append(f"Lanes row {row_no(i)}: FromID, ToID, ItemScope and TransitTimeDistID must be filled")
            continue
        if r["FromID"] not in loc_type or r["ToID"] not in loc_type:
            bad.append("unknown end point")
        if r["TransitTimeDistID"] not in dists or (not _blank(r["TransitIncludedIn"]) and r["TransitIncludedIn"] not in dists):
            bad.append("unknown lead-time distribution")
        if not _blank(r["TransportCostTierTableID"]) and r["TransportCostTierTableID"] not in tiers:
            bad.append(f"unknown tier table {r['TransportCostTierTableID']}")
        if bad:
            E.append(f"Lanes row {row_no(i)}: " + "; ".join(bad))
    # ---- C09 Channels
    ch = _rows(xls, "Channels", ["ChannelID"])
    got, prios = set(), set()
    for i, r in ch.iterrows():
        cid = r["ChannelID"]
        need = ("SourceLocationID", "ItemID", "MinRemainingShelfLife", "ShortagePriority", "UnmetDemand", "DemandInput")
        if any(_blank(g(r, c)) for c in need):
            E.append(f"Channels row {row_no(i)}: all fields of {cid} must be filled")
            continue
        prio, need_sl = _f(r["ShortagePriority"]), _f(g(r, "MinRemainingShelfLife"))
        bad = []
        if cid in got:
            bad.append(f"duplicate channel {cid}")
        got.add(cid)
        if loc_type.get(cid) != "Channel":
            bad.append(f"{cid} is not a Channel location")
        if r["SourceLocationID"] not in loc_type:
            bad.append(f"source {r['SourceLocationID']} unknown")
        if item_type.get(r["ItemID"]) != "FG":
            bad.append(f"item {r['ItemID']} is not an FG item")
        if not (_whole(prio) and 1 <= prio <= len(ch)):
            bad.append(f"ShortagePriority must be a whole number from 1 to {len(ch)} (1 = served first)")
        elif prio in prios:
            bad.append(f"ShortagePriority {prio:g} is used twice (each channel needs its own rank)")
        else:
            prios.add(prio)
        fsl = item_sl.get(r["ItemID"]) or 0
        if not (_whole(need_sl) and 0 <= need_sl <= fsl):
            bad.append(f"shelf-life need must be whole weeks between 0 and the FG shelf life {fsl:g}")
        if bad:
            E.append(f"Channels row {row_no(i)}: " + "; ".join(bad))
    # ---- C12 Costs
    co = _rows(xls, "Costs", ["ItemID"])
    for i, r in co.iterrows():
        iid = r["ItemID"]
        bad, miss = [], []
        if iid not in item_type:
            bad.append(f"unknown item {iid}")
        if r["LocationID"] not in loc_type:
            bad.append(f"unknown location {r['LocationID']}")
        tt = g(r, "ProductionVarCostTierTableID")
        if not _blank(tt) and tt not in tiers:
            bad.append(f"unknown tier table {tt}")
        fields = {"HoldingCost": True, "WasteCost": item_sl.get(iid) is not None,
                  "OrderFixedCost": item_type.get(iid) == "RM", "OrderVariableCost": item_type.get(iid) == "RM",
                  "ProductionFixedCost": item_type.get(iid) == "FG", "ProductionVarCostTierTableID": item_type.get(iid) == "FG"}
        for f, required in fields.items():
            v = g(r, f)
            if required and _blank(v):
                miss.append(f)
            if f != "ProductionVarCostTierTableID" and not _blank(v) and not _f(v) >= 0:
                bad.append(f"{f} must be ≥ 0")
        if miss:
            E.append(f"Costs row {row_no(i)}: {', '.join(miss)} of {iid} must be filled")
        if bad:
            E.append(f"Costs row {row_no(i)}: " + "; ".join(bad))
    # ---- C13 Init_Inventory
    inv = _rows(xls, "Init_Inventory", ["LocationID", "ItemID", "Quantity"])
    for i, r in inv.iterrows():
        q, a = _f(g(r, "Quantity")), _f(g(r, "RemainingShelfLife"))
        if _blank(r["LocationID"]) or _blank(r["ItemID"]) or q is None:
            E.append(f"Init_Inventory row {row_no(i)}: LocationID, ItemID and Quantity must be filled")
            continue
        bad = []
        if r["LocationID"] not in loc_type or r["ItemID"] not in item_type:
            bad.append("unknown location or item")
        if not q >= 0:
            bad.append("Quantity must be ≥ 0")
        sl = item_sl.get(r["ItemID"])
        if sl is not None and a is None:
            E.append(f"Init_Inventory row {row_no(i)}: RemainingShelfLife of perishable {r['ItemID']} must be filled")
        elif sl is not None and not (_whole(a) and 0 <= a <= sl):
            bad.append(f"RemainingShelfLife must be whole weeks between 0 and {sl:g}")
        if bad:
            E.append(f"Init_Inventory row {row_no(i)}: " + "; ".join(bad))
    # ---- C14 Open_Orders
    oo = _rows(xls, "Open_Orders", ["OrderType", "ItemID", "Quantity"])
    for i, r in oo.iterrows():
        need = (r["OrderType"], r["ItemID"], r["FromID"], r["ToID"], g(r, "Quantity"), r["ExpectedArrivalPeriod"])
        if any(_blank(v) for v in need):
            E.append(f"Open_Orders row {row_no(i)}: OrderType, ItemID, FromID, ToID, Quantity, ExpectedArrivalPeriod "
                     f"must be filled")
            continue
        q, arr, pl = _f(g(r, "Quantity")), _f(r["ExpectedArrivalPeriod"]), _f(r["PlacedInPeriod"])
        rem = _f(g(r, "RemainingShelfLifeAtArrival"))
        bad = []
        if r["ItemID"] not in item_type or r["FromID"] not in loc_type or r["ToID"] not in loc_type:
            bad.append("unknown item or location")
        if r["OrderType"] not in ("Purchase", "Production"):
            bad.append("OrderType must be Purchase or Production")
        if not q > 0:
            bad.append("Quantity must be > 0")
        if not (_whole(arr) and arr >= 1):
            bad.append("ExpectedArrivalPeriod must be a whole week ≥ 1")
        if pl is not None and not (_whole(pl) and pl <= 0 and pl < arr):
            bad.append("PlacedInPeriod must be a whole week ≤ 0 (orders placed before the plan starts)")
        sl = item_sl.get(r["ItemID"])
        if rem is not None and not (_whole(rem) and 0 <= rem <= (sl if sl is not None else rem)):
            bad.append("RemainingShelfLifeAtArrival must be whole weeks within the shelf life")
        if bad:
            E.append(f"Open_Orders row {row_no(i)}: " + "; ".join(bad))
            continue
        # ---- X08 (warning): arrival later than placement + the longest lead time of the item
        lt_max = dist_max.get(item_dist.get((r["OrderType"], r["ItemID"])))
        if pl is not None and lt_max is not None and arr - pl > lt_max + EPS:
            extra = ("; production lots started before then cannot arrive earlier (no overtaking, E04)"
                     if r["OrderType"] == "Production" else "")
            W.append(f"Open_Orders row {row_no(i)} ({r.get('OrderID', '')}): arrives in week {arr:g}, {arr - pl:g} weeks after "
                     f"placement, but the longest lead time of {r['ItemID']} is {lt_max:g} week{'' if lt_max == 1 else 's'} "
                     f"(X08){extra}")
    return E, W


def load_data(path):
    """Read the Excel template into a ModelData object (see sheet descriptions). The Python input checks run
    first (K19); any error stops here with the full list, warnings are kept in d.warnings. The file is closed
    again in every case (on Windows an open file cannot be overwritten or deleted)."""
    with pd.ExcelFile(path) as xls:
        return _read_workbook(xls)


def _read_workbook(xls):
    errors, warns = input_problems(xls)
    if errors:
        raise ValueError("Data validation failed:\n  - " + "\n  - ".join(errors))
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

    # --- one fill-rate target per product (H04); channels sorted by ShortagePriority (1 = served first, H02)
    target = float(items.loc[fg_id, _col(items.reset_index(), "TargetFillRate")])
    ch = _sheet(xls, "Channels")
    channels = [Channel(r["ChannelID"], target, int(r[_col(ch, "MinRemainingShelfLife")]), int(r["ShortagePriority"]))
                for _, r in ch.iterrows()]
    channels = sorted(channels, key=lambda c: c.priority)

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
            v = vals[vals.index("OVERALL") + 1]
            status = "unknown" if _blank(v) else str(v).strip()       # no cached result (file saved by a program)

    d = ModelData(
        horizon=H, review_period=int(st["ReviewPeriod"]), start_date=pd.Timestamp(st["PlanningStartDate"]),
        fill_rate_target=target,
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
        init_fg=init_fg, init_rm=init_rm, open_prod=open_prod, open_rm=open_rm, data_status=status, warnings=warns,
        uom={str(i): str(u) for i, u in items["UoM"].items()})
    d.warnings = warns + data_warnings(d)                       # X09 on the parsed data (K19)
    return d


def run_week(d, t):
    """The run (review) week whose information plans week t (A03): 1, 1 + R, 1 + 2R, … R = 1 → t itself."""
    return 1 + d.review_period * ((t - 1) // d.review_period)


def echelon_lead_time(d):
    """Longest raw-material lead time + longest production lead time (A02, input warning X10)."""
    return max([k.lead_time.max for k in d.rms] or [0]) + d.prod_lead_time.max


def fresh_lot_sales(d):
    """Units of FG1 that one fresh lot can sell before it expires, with average forecast demand: Σ over channels of
    average weekly demand × the weeks the channel may take a fresh lot (shelf life − its need + 1, F06, H01).
    Used by the start lot (K07) and the input warning X09."""
    mean_c = d.demand_q[:, 1:, :].mean(2).mean(1)                 # average weekly demand per channel
    return float(sum(m * max(0, d.fg_shelf_life - c.min_remaining + 1) for m, c in zip(mean_c, d.channels)))


def data_warnings(d):
    """Input warnings X09 and X10, which need the parsed data (Excel: Production / BOM row WARNINGs and the Checks
    row X10). Never stop a run."""
    W = []
    # ---- X10 horizon too short for the end-of-horizon effect (A02, A05)
    need = echelon_lead_time(d) + d.review_period + HORIZON_MARGIN_WEEKS
    if d.horizon < need:
        W.append(f"Horizon {d.horizon} weeks is shorter than the echelon lead time {echelon_lead_time(d)} + review "
                 f"period {d.review_period} + {HORIZON_MARGIN_WEEKS} weeks margin = {need}: the end-of-horizon effect "
                 f"(stock run down before the last week, A05) may reach the committed weeks 1..{d.review_period} (X10)")
    # ---- X09 lot size larger than what a fresh lot can sell / use before it expires
    sell = fresh_lot_sales(d)
    if d.prod_moq > sell + EPS:
        W.append(f"Production MOQ {d.prod_moq:,.0f} is larger than what one fresh lot can sell before it expires "
                 f"(about {sell:,.0f} with average demand), so part of every lot is expected to be wasted (X09)")
    mean_total = float(d.demand_q[:, 1:, :].mean(2).sum(0).mean())  # average weekly FG demand, all channels
    for k in d.rms:
        if k.perishable:
            use = k.bom * mean_total * max(0, k.shelf_life - k.min_remaining + 1)
            if k.moq > use + EPS:
                W.append(f"{k.id} MOQ {k.moq:,.0f} is larger than production can use before it expires (about {use:,.0f} "
                         f"with average demand), so part of every order is expected to be wasted (X09)")
    return W


def validate(d):
    """Hard data checks in Python (independent of the Excel 'Checks' formulas). Raises on error."""
    errors = []
    if getattr(d, "fill_rate_target_excel", None) is None:  # K08: section 0 replaces the Excel target when set
        d.fill_rate_target_excel = d.fill_rate_target
    d.fill_rate_target = d.fill_rate_target_excel if OPTIMIZATION_FILL_RATE is None else OPTIMIZATION_FILL_RATE
    if d.data_status not in ("READY", "unknown"):
        errors.append(f"Excel sheet Checks shows OVERALL = {d.data_status} (must be READY).")
    if not 1 <= d.horizon <= 52:
        errors.append("HorizonPeriods must be between 1 and 52.")
    if not (isinstance(d.fill_rate_target, (int, float)) and 0 < d.fill_rate_target <= 1):
        errors.append("Items!TargetFillRate must be in (0, 1]." if OPTIMIZATION_FILL_RATE is None
                      else "OPTIMIZATION_FILL_RATE (section 0) must be None or in (0, 1].")
        d.fill_rate_target = 0.5                          # placeholder so the remaining checks can run
    for c in d.channels:                                  # H04: every channel-week cell has the product's target
        c.target = d.fill_rate_target
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
        if c.min_remaining > d.fg_shelf_life:
            errors.append(f"Channel {c.id}: shelf-life need ≤ FG shelf life.")
    if sorted(c.priority for c in d.channels) != list(range(1, len(d.channels) + 1)):
        errors.append("Channels: ShortagePriority must rank the channels 1, 2, … without gaps or repeats.")
    if errors:
        raise ValueError("Data validation failed:\n  - " + "\n  - ".join(errors))


def target_source(d):
    """Where the fill-rate target of this run comes from (K08), for the console and the Summary."""
    if OPTIMIZATION_FILL_RATE is None:
        return f"Excel Items!TargetFillRate of {d.fg_id}"
    return f"section 0 OPTIMIZATION_FILL_RATE; Excel {d.fill_rate_target_excel:.1%}"


def cell_met(d, value):
    """H05: a channel-week cell meets the rule if its expected fill rate (mean over the scenarios) ≥ the target."""
    return value >= d.fill_rate_target - EPS


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
# Constraints: per channel and week, expected fill rate (aggregate_mean over the scenarios) ≥ the product's target


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
    fr_mean: dict                              # (ci, t) -> expected fill rate of the cell (single number, H05)
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
    detail: dict = None                        # report series (only built for the out-of-sample check, L05–L08)


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


def build_model(skr, env, d, layout, x_start, seed, resolution, detail=False):
    """THE MODEL. Creates in `env`: decision variables (start values x_start), random demand and lead times,
    the week-by-week dynamics E1–E6, total cost, fill rates, objective and fill-rate constraints.
    `seed` selects the random demand (optimization: Settings!RandomSeed; out-of-sample: other seeds).
    detail=True (out-of-sample check only, L05–L08): also builds the per-scenario report series; the
    optimization and feasibility models never contain them (K06, contract v2).
    """
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

    # ---- random data: demand from quantiles, independent per channel and week (G02); lead times (E03)
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
    det = {}                                                     # report series, only if detail (L05–L08)

    def tsum(parts):                                             # sum that accepts plain numbers (detail only)
        return T.sum([p if not isinstance(p, (int, float)) else T.C(p) for p in parts])

    def tier_cost(tiers, q):
        if tiers.mode == "Incremental":
            return T.sum([c * T.min(env.max_0(q - f), to - f) if np.isfinite(to) else c * env.max_0(q - f)
                          for f, to, c in zip(tiers.froms, tiers.tos, tiers.costs)])
        price = T.C(tiers.costs[-1])
        for to, c in reversed(list(zip(tiers.tos[:-1], tiers.costs[:-1]))):
            price = env.if_(q < to, T.C(c), price)
        return price * q

    mu = demand_moments(d)[0]                                    # expected demand per week (planned position, A03)
    plan_fg = plan_q_fg = None                                   # planned FG position and planned order of last week
    plan_rm, plan_q_rm = {}, {}                                  # the same per raw material (echelon)
    for t in range(1, H + 1):
        run = run_week(d, t) == t                                # run week: actual position; else planned (A03)
        plan_next = run_week(d, t + 1) != t + 1                  # next week is planned from this week's numbers
        # ---- E1: receipts with full shelf life (F01) -------------------------------------------
        got = fg_stream.receipts(t, d.fg_shelf_life)
        for a, term in got.items():
            fg[a] = term if fg.get(a) is None else fg[a] + term
        if detail:
            det[("receipts", d.fg_id, t)] = tsum(list(got.values()))
        for k in d.rms:
            got = rm_stream[k.id].receipts(t, k.shelf_life if k.perishable else 0)
            for a, term in got.items():
                key = a if k.perishable else 0
                rm[k.id][key] = term if rm[k.id].get(key) is None else rm[k.id][key] + term
            if detail:
                det[("receipts", k.id, t)] = tsum(list(got.values()))

        # ---- E2: FG review → production order (C01, C06, A03, D02) ---------------------------
        ip_fg = T.sum([v for a, v in fg.items() if a >= rho_min]) + fg_stream.pipeline(t) if run or detail else None
        if detail:
            det[("position", d.fg_id, t)] = ip_fg                # actual position (C01)
        ip_rule = ip_fg if run else env.max_0(plan_fg + plan_q_fg - float(mu[t - 1]))   # planned position (A03)
        q, q_plan = None, 0.0
        pol = policy("FG", t)
        if pol is not None:
            s, S = pol
            q_hat = T.min(d.prod_max, T.max(d.prod_moq, d.prod_batch * env.ceil((S - ip_rule) / d.prod_batch - EPS)))
            caps = []
            for k in d.rms:
                if k.perishable:
                    avail = T.sum([v for a, v in rm[k.id].items() if a >= k.min_remaining])
                else:
                    avail = rm[k.id].get(0, T.C(0))
                caps.append(avail / k.bom)
            cap = caps[0] if len(caps) == 1 else env.min(caps)
            q_chk = T.min(q_hat, d.prod_batch * env.floor(cap / d.prod_batch + EPS))
            q = (ip_rule < s) * (q_chk >= d.prod_moq - EPS) * q_chk      # produced (raw material permitting)
            if plan_next:
                q_plan = (ip_rule < s) * q_hat                   # planned at the run, before the material check
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

        plan_fg, plan_q_fg = ip_rule, q_plan

        # ---- E3: RM review on echelon position (C03, C04, A03, D01) ---------------------------
        ip_after = None if ip_fg is None else (ip_fg if q is None else ip_fg + q)
        for k in d.rms:
            pol = policy(k.id, t)
            ip_k = None
            if detail or (run and pol is not None):              # actual echelon position (C03)
                usable_next = T.sum([v for a, v in rm[k.id].items() if a >= k.min_remaining + 1]) if k.perishable \
                    else rm[k.id].get(0, T.C(0))
                ip_k = usable_next + rm_stream[k.id].pipeline(t) + k.bom * ip_after
                if detail:
                    det[("position", k.id, t)] = ip_k            # also in inactive weeks
            if pol is None:
                plan_rm[k.id] = plan_q_rm[k.id] = None           # inactive tail week (K04): no later orders
                continue
            s, S = pol
            if not run:                                          # planned echelon position between runs (A03)
                ip_k = env.max_0(plan_rm[k.id] + plan_q_rm[k.id] - k.bom * float(mu[t - 1]))
            qk = (ip_k < s) * T.min(k.max_order, T.max(k.moq, k.multiple * env.ceil((S - ip_k) / k.multiple - EPS)))
            plan_rm[k.id], plan_q_rm[k.id] = ip_k, qk
            rm_arr[(k.id, t)] = rm_stream[k.id].add(t, qk)
            rm_order[(k.id, t)] = qk
            costs["rm_ordering"] += [k.fixed_cost * (qk > 0), k.var_cost * qk]

        # ---- E4: demand, channels by priority, FEFO among eligible lots (H01–H04) -------------
        for ci, ch in enumerate(d.channels):
            dem = demand[(ci, t)]
            served = _fefo(T, fg, sorted(a for a in fg if a >= ch.min_remaining), dem)
            fill_rate[(ci, t)] = env.if_(dem > 0, served / T.max(dem, 1.0), T.C(1))
            if detail:
                det[("served", ch.id, t)], det[("lost", ch.id, t)] = served, dem - served

        # ---- E5: holding cost on end-of-week stock (I05) -------------------------------------
        fg_end[t] = T.sum(list(fg.values()))
        hold = [d.fg_holding * fg_end[t]]
        for k in d.rms:
            rm_end[(k.id, t)] = T.sum(list(rm[k.id].values())) if rm[k.id] else 0.0
            if rm[k.id]:
                hold.append(k.holding * rm_end[(k.id, t)])
        holding[t] = T.sum(hold)
        costs["holding"].append(holding[t])
        if detail:                                               # DC stock health at week end (L05)
            for ch in d.channels:                                # still sellable to the channel next week
                det[("eligible_next_week", ch.id, t)] = tsum([v for a, v in fg.items() if a >= ch.min_remaining + 1])
            num = tsum([a * v for a, v in fg.items()])
            det[("rsl_num", d.fg_id, t)] = num
            det[("rsl_mean", d.fg_id, t)] = env.if_(fg_end[t] > 0.5, num / T.max(fg_end[t], 1.0), T.C(0))
            det[("no_stock", d.fg_id, t)] = T.C(0) - fg_end[t]   # ≥ −0.5 ⇔ no stock (read as a share)

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
    if detail:                                                   # per-scenario totals for the KPI block (L07)
        t_first = 1 + d.prod_lead_time.min if d.enforce_from_first_controllable else 1
        det[("kpi", "avg_end_stock", d.fg_id)] = tsum([fg_end[t] for t in range(1, H + 1)]) / H
        det[("kpi", "waste", d.fg_id)] = tsum([fg_waste[t] for t in range(1, H + 1)])
        for k in d.rms:
            det[("kpi", "avg_end_stock", k.id)] = tsum([rm_end[(k.id, t)] for t in range(1, H + 1)]) / H
            if k.perishable:
                det[("kpi", "waste", k.id)] = tsum([rm_waste[(k.id, t)] for t in range(1, H + 1)])
        for ch in d.channels:
            det[("kpi", "lost_initial_weeks", ch.id)] = tsum([det[("lost", ch.id, t)] for t in range(1, t_first)])
            det[("kpi", "lost_controllable_weeks", ch.id)] = tsum([det[("lost", ch.id, t)] for t in range(t_first, H + 1)])
    objective = env.aggregate_mean(total)
    # ---- fill-rate rules (H04–H07): expected fill rate per channel and week ≥ the product's target ------
    t0 = 1 + d.prod_lead_time.min if d.enforce_from_first_controllable else 1
    constrained = [(ci, t) for ci in range(len(d.channels)) for t in range(t0, H + 1)]
    fr_mean = {(ci, t): env.aggregate_mean(fill_rate[(ci, t)])
               for ci in range(len(d.channels)) for t in range(1, H + 1)}          # mean over the scenarios
    for ci, t in constrained if FILL_RATE_CONSTRAINT_FORM != "none" else []:
        env.enforce_geq(fr_mean[(ci, t)], d.fill_rate_target, f"FR_{d.channels[ci].id}_w{t}")
    return SeekerModel(env, T, variables, objective, total, cost_parts, fill_rate, fr_mean, constrained,
                       demand, lead_prod, lead_rm, open_prod_arr, open_rm_arr, prod_qty, prod_arr, rm_order, rm_arr,
                       fg_end, fg_waste, rm_end, rm_waste, holding, keep, det if detail else None)


# ============================================================================
# 4. START POLICY — the warm start: one fixed (s,S) policy from the forecast (K07)
#    The start policy is a pure formula. Section 5 checks it on Seeker's scenarios ("start check"); only if it
#    misses fill-rate rules that can be met does section 4c repair it. Normally the repair is not needed.
# ============================================================================
def start_policy(d, z=None, extra_fg=None, extra_rm=None, lot_weeks=None):
    """Seeker's starting point (warm start) and the baseline for the reported saving. Pure formula on the
    forecast; no model evaluation. Parameters default to the START_* settings in section 0; z defaults to
    start_z(d), the safety factor that fits the fill-rate target (normal loss function, below).

      μ_u   = expected total demand in week u   = Σ over channels of the mean of the 10 forecast quantiles
      σ_u   = standard deviation of week u       = √ Σ over channels ((Q95 − Q05) / 3.29)²   (normal approx.)
      cover(t, W) = Σ_{u=t}^{t+W−1} μ_u + z · √ Σ_{u=r(t)}^{t+W−1} σ_u²   (window cut at the horizon)
              r(t) = the run week that plans week t (A03): the safety stock also covers the demand between
              the last run and week t, which the planned position only knows as its expectation. R = 1: r(t) = t.
      lot   = max(production MOQ, min(START_LOT_WEEKS × average weekly μ, what one fresh lot can sell before
              it expires = fresh_lot_sales, section 1))

      FG1 at the DC, every active week t:      s_t = cover(t, L_prod,max + 1 + START_EXTRA_WEEKS_FG)
                                               S_t = s_t + lot
      RM k at the RMW (echelon), active weeks: s_t = max(BOM_k · cover(t, L_k,max + L_prod,max + 1 + START_EXTRA_WEEKS_RM),
                                                         BOM_k · (s_FG,t + production MOQ))        ← lot guard
                                               S_t = s_t + max(MOQ_k, BOM_k · lot)
      Lot guard: when FG falls to its reorder point and needs a lot, the raw material on hand or on order covers at
      least one production MOQ (otherwise production can be blocked while the echelon position looks covered).
      Inactive tail weeks: s = S = 0 (K04). The values are then rounded to the decision steps (K11)."""
    z = (start_z(d) if START_Z is None else START_Z) if z is None else z
    extra_fg = START_EXTRA_WEEKS_FG if extra_fg is None else extra_fg
    extra_rm = START_EXTRA_WEEKS_RM if extra_rm is None else extra_rm
    lot_weeks = START_LOT_WEEKS if lot_weeks is None else lot_weeks
    H = d.horizon
    mean, sd = demand_moments(d)

    def cover(t, weeks):
        tt = np.arange(t, min(H, t + weeks - 1) + 1)
        uu = np.arange(run_week(d, t), min(H, t + weeks - 1) + 1)        # uncertainty since the last run (A03)
        return mean[tt].sum() + z * math.sqrt((sd[uu] ** 2).sum())

    pol = {}
    Lp = d.prod_lead_time.max
    lot = max(d.prod_moq, min(lot_weeks * mean[1:].mean(), fresh_lot_sales(d)))
    s, S = np.zeros(H + 1), np.zeros(H + 1)
    for t in range(1, H - d.prod_lead_time.min + 1):
        s[t] = cover(t, Lp + 1 + extra_fg)
        S[t] = s[t] + lot
    pol["FG"] = (s, S)
    for k in d.rms:
        s, S = np.zeros(H + 1), np.zeros(H + 1)
        for t in range(1, H - d.prod_lead_time.min - k.lead_time.min + 1):
            s[t] = max(k.bom * cover(t, k.lead_time.max + Lp + 1 + extra_rm), k.bom * (pol["FG"][0][t] + d.prod_moq))
            S[t] = s[t] + max(k.moq, k.bom * lot)
        pol[k.id] = (s, S)
    return pol


def demand_moments(d):
    """μ_u and σ_u per week u (index 0 unused): mean of the 10 quantiles and (Q95 − Q05) / 3.29, summed over channels."""
    mean = d.demand_q[:, 1:, :].mean(2).sum(0)
    sd = np.sqrt((((d.demand_q[:, 1:, -1] - d.demand_q[:, 1:, 0]) / 3.29) ** 2).sum(0))
    return np.concatenate(([0], mean)), np.concatenate(([0], sd))


def normal_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def normal_loss(z):
    """Standard normal loss function G(z) = E[(X − z)⁺] = φ(z) − z · (1 − Φ(z)), X ~ N(0, 1)."""
    return math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi) - z * (1.0 - normal_cdf(z))


def normal_loss_inverse(y, lo=-4.0, hi=8.0):
    """z with G(z) = y (G falls from G(lo) ≈ |lo| to 0); bisection, clipped to [lo, hi]."""
    if y >= normal_loss(lo):
        return lo
    if y <= normal_loss(hi):
        return hi
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        lo, hi = (mid, hi) if normal_loss(mid) > y else (lo, mid)
    return 0.5 * (lo + hi)


def start_z(d):
    """Safety factor of the start policy from the fill-rate target β (K07), with the normal loss function:
    the reorder point covers the FG cover window W = L_prod,max + 1 + START_EXTRA_WEEKS_FG with z standard deviations;
    the expected shortage before the next lot arrives is ≈ σ_W · G(z) and falls in the last week, so the fill rate
    of that week is ≈ 1 − σ_W · G(z) ÷ μ. Smallest z with fill rate ≥ β:  z₀ = G⁻¹((1 − β) · μ ÷ σ_W),
    μ = average weekly demand, σ_W = √(W × average weekly variance). Clipped to [0, START_Z_MAX]. The formula
    ignores lots, freshness and lost sales, so the start check (section 5) raises z if the start misses cells."""
    mean, sd = demand_moments(d)
    W = d.prod_lead_time.max + 1 + START_EXTRA_WEEKS_FG
    sigma_w = math.sqrt(W * float((sd[1:] ** 2).mean()))
    if sigma_w <= 0:
        return 0.0
    y = (1.0 - d.fill_rate_target) * float(mean[1:].mean()) / sigma_w
    return min(START_Z_MAX, max(0.0, normal_loss_inverse(y)))


def start_z_text(d, rec):
    """'z = 2.12 (z₀ = 1.62 from fill-rate target 95.0% via the normal loss function, raised by the start check)'."""
    rec = rec or {}
    z0, z = rec.get("z0"), rec.get("z", rec.get("z0"))
    if z0 is None:
        return f"z = {START_Z:g}" if START_Z is not None else "z = ?"
    if START_Z is not None:
        return f"z = {z:.2f} (fixed in section 0, START_Z)"
    src = f"from fill-rate target {d.fill_rate_target:.1%} via the normal loss function"
    if z > z0 + EPS:
        return f"z = {z:.2f} (z₀ = {z0:.2f} {src}, raised by the start check)"
    return f"z = {z:.2f} ({src})"


def raise_start_z(skr, d, layout, z0, start0, max_fr, license_file):
    """Start check, step 2 (K07): the start with z₀ misses cells that the maximum-supply policy meets. Raise z to the
    next multiples of START_Z_STEP up to START_Z_MAX, each start evaluated once by Seeker; stop at the first that misses nothing that
    can be met. Returns (z, policy, its start numbers, record); if none succeeds, the one meeting the most cells
    (then the cheaper), and the start repair (section 4c) continues from it."""
    rec = dict(z0=z0, steps=[dict(z=z0, met=start0["met"], cost=start0["cost"])])
    best = (start0["met"], -start0["cost"], z0, start_policy(d, z=z0), start0)
    first = START_Z_STEP * (math.floor((z0 + 0.1) / START_Z_STEP + EPS) + 1)  # fixed grid 0.5, 1.0, 1.5, … above z₀ + 0.1,
    zs = [first + START_Z_STEP * i for i in range(100)                         # so a lower target never ends on a
          if first + START_Z_STEP * i < START_Z_MAX - EPS] + [START_Z_MAX]     # higher z than a higher target
    print(f"Start check: the start with z = {z0:.2f} meets {start0['met']} of {len(start0['fr'])} cells → raising z "
          f"(section 4)", flush=True)
    for z in zs if z0 < START_Z_MAX - EPS else []:
        pol = start_policy(d, z=z)
        fr, cost = evaluate_policy(skr, d, layout, pol, license_file, f"4. Start check: Seeker evaluates the start with z = {z:.2f}")
        num = dict(cost=cost, met=len(rules_met(d, fr)), fr=fr)
        rec["steps"].append(dict(z=z, met=num["met"], cost=cost))
        ok = not start_misses(d, fr, max_fr)
        print(f"  z = {z:.2f}: {num['met']} of {len(fr)} cells met, cost {cost:,.0f}" + (" ✓" if ok else ""), flush=True)
        if ok:
            best = (num["met"], -cost, z, pol, num)
            break
        if (num["met"], -cost) > best[:2]:
            best = (num["met"], -cost, z, pol, num)
    rec["z"] = best[2]
    print(f"Start check: Seeker starts from z = {best[2]:.2f}", flush=True)
    return best[2], best[3], best[4], rec


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


TAIL_SHARE = 0.05   # L04: "worst 5% of futures": mean of the lowest 5% = (E[f · 1(f < q)] + q · (5% − P(f < q))) ÷ 5%,
#                     q = 5% quantile — exact also with ties (e.g. many futures at exactly 0% or 100%)


def single_number(model, term, kind="mean", threshold=0.0):
    """Creates an aggregate of `term` in the model and returns a function that reads it as ONE number
    after env.evaluate(). kind: mean | pNN (quantile, e.g. p05) | share (= share of scenarios with term ≥ threshold)
    | tail5 (average of the worst TAIL_SHARE = 5% of the scenarios).
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
        elif kind == "tail5":                              # average of the worst 5% of the scenarios (exact):
            q = env.aggregate_quantile(term, TAIL_SHARE, False)          # q = the 5% quantile
            below = term < q
            a, p = env.aggregate_mean(term * below), env.aggregate_mean(below)
            return lambda: (a.get_value() + q.get_value() * (TAIL_SHARE - p.get_value())) / TAIL_SHARE
        else:
            agg = env.aggregate_quantile(term, int(kind[1:]) / 100.0, False)          # "p05" … "p95"
        return agg.get_value
    except Exception:  # noqa: BLE001 — Seeker refuses aggregates of Terms that do not vary across scenarios
        return (lambda: float(term.get_value() >= threshold - 1e-6)) if kind == "share" else term.get_value


def constraints_met(model, d):
    return sum(cell_met(d, model.fr_mean[k].get_value()) for k in model.constrained)


# ============================================================================
# 4b. FEASIBILITY CHECK — which fill-rate rules fail whatever we decide (K17)
# ============================================================================
def max_supply_policy(d, layout):
    """Every active week a reorder point above any reachable inventory position, so every review orders the
    maximum: each raw material its max order, FG its max production (still limited by the raw materials on hand,
    MOQ and batch — the rules of section 3 apply unchanged). s = S = the position reachable by week t (no ordering
    decision can exceed it) + the maximum order, so S − position always exceeds the maximum and the order is capped
    at it; the values stay small enough to serve as a repaired start (K21). Reachable position by week t:
      RM k echelon: initial and open RM_k + BOM_k × (initial and open FG) + (t − 1) × max order_k
      FG          : initial and open FG + min((t − 1) × max production, min_k (initial and open RM_k + (t − 1) × max order_k) / BOM_k)"""
    H = d.horizon
    fg0 = sum(q for _, q in d.init_fg) + sum(o["qty"] for o in d.open_prod)
    rm0 = {k.id: sum(q for _, q in d.init_rm[k.id]) + sum(o["qty"] for o in d.open_rm[k.id]) for k in d.rms}
    rms = {k.id: k for k in d.rms}
    pol = {}
    for j, m, last in layout.points:
        k = rms.get(j)
        s, S = np.zeros(H + 1), np.zeros(H + 1)
        for t in range(1, last + 1):
            if k is None:
                reach = fg0 + min([(t - 1) * d.prod_max] + [(rm0[r.id] + (t - 1) * r.max_order) / r.bom for r in d.rms if r.bom > 0])
                top = d.prod_max
            else:
                reach, top = rm0[j] + k.bom * fg0 + (t - 1) * k.max_order, k.max_order
            s[t] = m * (math.floor((reach + top) / m) + 1)
        pol[j] = (s, s.copy())
    return pol


def evaluate_policy(skr, d, layout, pol, license_file, title):
    """Seeker evaluates one fixed policy on the optimization scenarios (same count and seed as section 5), in its
    own environment with bounds widened to contain the policy. Returns ({(channel, week): expected fill rate} for
    the constrained cells, expected cost)."""
    lay = copy.deepcopy(layout)
    lay.include_start(pol, margin=1.0)
    with phase(title):
        env = new_env(skr, license_file, d.n_scenarios, 0.0)
        model = build_model(skr, env, d, lay, lay.encode(pol), d.seed, d.n_scenarios)
        env.evaluate()
        fr = {k: model.fr_mean[k].get_value() for k in model.constrained}
        cost = model.objective.get_value()
        env.end()
    return fr, cost


def feasibility_check(skr, d, layout, license_file, title="4b. Feasibility check: Seeker evaluates the maximum-supply policy"):
    """Seeker evaluates the maximum-supply policy once, on the optimization scenarios (same count and seed).
    Returns {(channel, week): expected fill rate} for the constrained cells."""
    fr, _ = evaluate_policy(skr, d, layout, max_supply_policy(d, layout), license_file, title)
    return fr


def feasibility_note(d, start_fr, max_fr):
    """One line: rules that neither the start policy nor the maximum-supply policy meets. No single policy
    is an upper bound (producing everything at once can starve later weeks of fresh stock), so a rule only
    counts as impossible if both fail it (tested on the 25 stress cases against 11 policies: same verdict)."""
    bad = sorted((ci, t) for ci, t in max_fr
                 if not cell_met(d, max(start_fr[(ci, t)], max_fr[(ci, t)])))
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
    best = [max(start_fr[k], max_fr[k]) for k in bad]
    rng = f"{min(best):.1%}" if min(best) == max(best) else f"{min(best):.1%}–{max(best):.1%}"
    return (f"{len(bad)} rule(s) fail whatever we decide (supply too late or too small): " + "; ".join(parts)
            + f" (best expected fill rate {rng}, target {d.fill_rate_target:.1%})")


# ============================================================================
# 4c. START REPAIR — safety net for an infeasible start (K21). NORMALLY NOT USED.
#     Section 5 first evaluates the start policy (section 4) on Seeker's scenarios: the "start check". If the start
#     meets every fill-rate rule that can be met, Seeker starts from it and this section does nothing. Only if it
#     misses rules that the maximum-supply policy (section 4b) meets, the start is raised step by step in the weeks
#     before the missed weeks until it meets them; Seeker then starts from that feasible but expensive plan and
#     removes the extra cost. A used repair is printed as "START REPAIR USED" and shown in the Summary.
# ============================================================================
REPAIR_STEPS = (("R1", "bounds", 12), ("R2", "max", 6), ("R2", "max", 12), ("R3", "max", None))
#   R1: raise s and S to their upper bounds (section 2) in the 12 order weeks that can still supply each missed week
#   R2: maximum supply (section 4b) in those order weeks, first 6, then 12 of them
#   R3: maximum supply in every week (the policy of the feasibility check)


def rules_met(d, fr):
    return {k for k, v in fr.items() if cell_met(d, v)}


def start_misses(d, start_fr, max_fr):
    """Rules the start misses although the maximum-supply policy meets them (empty = no repair needed)."""
    return sorted(rules_met(d, max_fr) - rules_met(d, start_fr))


def _raise_before(d, layout, pol, src, weeks, window):
    """Copy of pol with s, S raised to at least src in the `window` order weeks before each week in `weeks` from
    which an order can still arrive in time: weeks t − L − window + 1 … t − L with L = the stock point's minimum
    lead time to the DC (FG: production; RM k: RM_k + production), only active weeks."""
    out = {j: (a.copy(), b.copy()) for j, (a, b) in pol.items()}
    lead = {"FG": d.prod_lead_time.min, **{k.id: k.lead_time.min + d.prod_lead_time.min for k in d.rms}}
    for j, _, last in layout.points:
        s_, S_ = out[j]
        for t in weeks:
            for u in range(max(1, t - lead[j] - window + 1), min(t - lead[j], last) + 1):
                s_[u], S_[u] = max(s_[u], src[j][0][u]), max(S_[u], src[j][1][u])
    return out


def repair_start(skr, d, layout, pol0, start_fr, start_cost, max_fr, license_file):
    """Section 4c (K21). Returns (start policy for Seeker, repair record). Target = every rule that the start or the
    maximum-supply policy meets. Steps REPAIR_STEPS in order, each evaluated once by Seeker; stop at the first that
    meets the target; otherwise keep the step that meets the most rules (then the lower cost)."""
    target = rules_met(d, start_fr) | rules_met(d, max_fr)
    ub = layout.decode([layout.upper(i) for i in range(len(layout))])
    msp = max_supply_policy(d, layout)
    rec = dict(used=True, start_met=len(rules_met(d, start_fr)), n=len(start_fr), target=len(target),
               steps=[], weeks=sorted({t for _, t in target - rules_met(d, start_fr)}))
    print(f"Start check: the start policy meets {rec['start_met']} of {rec['n']} fill-rate rules, but {rec['target']} can "
          f"be met (maximum supply meets them) → START REPAIR USED (section 4c)", flush=True)
    best = (len(target & rules_met(d, start_fr)), -start_cost, "start policy (no step)", pol0)
    cand, fr = pol0, start_fr
    for label, src, window in REPAIR_STEPS:
        missed = target - rules_met(d, fr)
        if not missed:
            break
        weeks = sorted({t for _, t in missed})
        if window is None:
            cand, desc = msp, "maximum supply in every week"
            fr, cost = max_fr, float("inf")               # evaluated in 4b; never cheaper than the steps before
        else:
            cand = _raise_before(d, layout, cand, ub if src == "bounds" else msp, weeks, window)
            desc = (f"{'s,S at their upper bounds' if src == 'bounds' else 'maximum supply'} in the {window} order weeks "
                    f"before week(s) {', '.join(map(str, weeks))}")
            fr, cost = evaluate_policy(skr, d, layout, cand, license_file, f"4c. Start repair {label}: Seeker evaluates")
        met = len(target & rules_met(d, fr))
        rec["steps"].append(dict(step=label, what=desc, met=len(rules_met(d, fr)), cost=cost))
        print(f"  {label}: {desc} → {len(rules_met(d, fr))} of {rec['n']} rules met"
              + ("" if cost == float("inf") else f", cost {cost:,.0f}"), flush=True)
        if (met, -cost) > best[:2]:
            best = (met, -cost, f"{label}: {desc}", cand)
    rec["chosen"], rec["chosen_met_target"] = best[2], best[0] == len(target)
    print(f"START REPAIR USED: Seeker starts from {best[2]}"
          + ("" if rec["chosen_met_target"] else
             f" — NOTE: it still misses {len(target) - best[0]} rule(s) that can be met") , flush=True)
    return best[3], rec


def repair_note(rec):
    """Summary text of the start repair (K21)."""
    if not rec or not rec.get("used"):
        return (rec or {}).get("note", "not needed — the start policy meets every rule that can be met")
    steps = "; ".join(f"{s['step']} {s['met']}/{rec['n']}" for s in rec["steps"])
    return (f"USED: start policy met {rec['start_met']} of {rec['n']} rules ({rec['target']} can be met); steps {steps}; "
            f"Seeker started from {rec['chosen']}")


# ============================================================================
# 5. OPTIMIZATION — Seeker searches; in-sample results are Seeker's own numbers
# ============================================================================
def optimize(skr, d, layout, x0, license_file, time_limit, max_fr=None, start_check=True, formula_fr=None):
    """Builds the search model, evaluates the start (start check), then searches. With start_check and START_REPAIR,
    a start that misses fill-rate rules is handed back before the search (returns None, start numbers) so that
    run() can repair it (section 4c); if max_fr is known and the missed rules cannot be met anyway, the search
    starts at once. formula_fr: the formula start's shares after a repair, so the feasibility note (K17) keeps
    judging the start policy of section 4, not the repaired start."""
    env = new_env(skr, license_file, d.n_scenarios, STOCHASTIC_SPEED)
    version = str(env.version())
    print(f"Seeker version: {version} | speed {STOCHASTIC_SPEED}, constraint form '{FILL_RATE_CONSTRAINT_FORM}', "
          f"fill-rate target {d.fill_rate_target:.1%} ({target_source(d)}), {d.n_scenarios} scenarios", flush=True)
    with phase("5. Seeker model build"):
        model = build_model(skr, env, d, layout, x0, d.seed, d.n_scenarios)
        parts = {n: single_number(model, t) for n, t in model.cost_parts.items()}
    with phase("5. Seeker evaluates the start policy"):
        env.evaluate()
        start = dict(cost=model.objective.get_value(), met=constraints_met(model, d),
                     fr={k: model.fr_mean[k].get_value() for k in model.constrained})
    print(f"Start policy on Seeker's scenarios: expected cost {start['cost']:,.0f}, "
          f"{start['met']}/{len(model.constrained)} fill-rate constraints met", flush=True)
    if start_check and (START_REPAIR or START_Z is None) and start["met"] < len(model.constrained) and \
            (max_fr is None or start_misses(d, start["fr"], max_fr)):
        env.end()                                                  # → higher z (section 4), repair (4c), new model
        return None, dict(start=start)
    if start_check and start["met"] == len(model.constrained):
        print("Start check: the start policy meets every fill-rate rule — no repair needed", flush=True)
    elif start_check and START_REPAIR:
        print("Start check: the start policy misses only rules that fail whatever we decide — no repair needed", flush=True)
    elif start_check:
        print(f"Start check: the start policy misses {len(model.constrained) - start['met']} rule(s); START_REPAIR = False "
              f"and START_Z fixed (section 0), so Seeker starts from it anyway", flush=True)
    feasibility = "not run"
    if max_fr is not None:                                       # section 4b
        feasibility = feasibility_note(d, formula_fr or start["fr"], max_fr)
        print(f"Feasibility check: {feasibility}", flush=True)
    env.set_report(REPORT_INTERVAL_SECONDS)
    with phase(f"5. Seeker optimization (time limit {time_limit:.0f} s)"):
        env.minimize(model.objective, time_limit)
    seconds = TIMINGS[-1][1]
    with phase("5. Reading Seeker's solution and in-sample results"):
        x = [int(round(v.get_value())) for v in model.variables]
        env.evaluate()
        ins = dict(cost=model.objective.get_value(), parts={n: f() for n, f in parts.items()},
                   fr_mean={k: v.get_value() for k, v in model.fr_mean.items()},
                   met=constraints_met(model, d), n=d.n_scenarios, start=start,
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
        num[("fr_mean", ci, t)] = model.fr_mean[(ci, t)].get_value             # the number the rule judges (H05)
        num[("fr_p10", ci, t)] = single_number(model, term, "p10")             # a bad scenario (information)
        num[("fr_full", ci, t)] = single_number(model, term, "share", 1.0)     # share of scenarios fully served
        num[("fr_tail", ci, t)] = single_number(model, term, "tail5")          # how bad the worst 5% of futures get
    series = [("prod", t, model.prod_qty.get(t, 0.0)) for t in range(1, H + 1)] + \
             [(k.id, t, model.rm_order.get((k.id, t), 0.0)) for k in d.rms for t in range(1, H + 1)]
    for j, t, q in series:
        for kind in ("mean", "p10", "p90"):
            num[(j, kind, t)] = single_number(model, q, kind)
        num[(j, "p_order", t)] = single_number(model, q, "share", 0.5)       # quantities are ≥ MOQ when > 0
    for t in range(1, H + 1):
        num[("fg_end", t)] = single_number(model, model.fg_end[t])
        num[("fg_waste", t)] = single_number(model, model.fg_waste[t])
        num[("holding", t)] = single_number(model, model.holding[t])
        for k in d.rms:
            num[(k.id, "end", t)] = single_number(model, model.rm_end[(k.id, t)])
            if k.perishable:
                num[(k.id, "waste", t)] = single_number(model, model.rm_waste[(k.id, t)])
    for item, t, q, arr, lt in arrival_streams(model, d):                    # arrival week given an order (L06)
        if isinstance(arr, (int, np.integer)):
            num[("arr_fixed", item, t)] = single_number(model, int(arr))
        else:
            for w in arrival_candidates(d, item, t, lt):
                num[("arr_share", item, t, w)] = single_number(model, (q > 0) * (arr <= w), "share", 0.5)
    if model.detail:
        num.update(detail_numbers(model, d))
    return num


BAND_STATS = ("mean", "p05", "p10", "p25", "p50", "p75", "p90", "p95")


def arrival_streams(model, d):
    """(item, week, order quantity, arrival week, lead time) of every order that can be placed."""
    out = [(d.fg_id, t, model.prod_qty[t], model.prod_arrival[t], d.prod_lead_time) for t in sorted(model.prod_qty)]
    for k in d.rms:
        out += [(k.id, t, model.rm_order[(k.id, t)], model.rm_arrival[(k.id, t)], k.lead_time)
                for (j, t) in sorted(model.rm_order) if j == k.id]
    return out


def arrival_candidates(d, item, t, lt):
    """Every week in which an order placed in week t can arrive: t + lead time, or later if an earlier
    (open) order arrives later (no overtaking, E04)."""
    opens = d.open_prod if item == d.fg_id else d.open_rm[item]
    latest = [o["arrival"] if o["certain"] else o["placed"] + int(lt.values.max()) for o in opens]
    hi = max([t + int(lt.values.max())] + latest)
    return range(t + int(lt.values.min()), hi + 1)


def detail_numbers(model, d):
    """Single numbers of the report series (L05–L07), read from the out-of-sample model only."""
    H, det, fg = d.horizon, model.detail, d.fg_id
    cache, num = {}, {}

    def read(term, kind, thr=0.0):                       # one aggregate per (Term, statistic)
        key = (id(term), kind, thr) if hasattr(term, "get_value") else (float(term), kind, thr)
        if key not in cache:
            cache[key] = single_number(model, term, kind, thr)
        return cache[key]

    series = []
    for t in range(1, H + 1):
        series += [("end_stock", fg, t, model.fg_end[t]), ("waste", fg, t, model.fg_waste[t]),
                   ("position", fg, t, det[("position", fg, t)]), ("receipts", fg, t, det[("receipts", fg, t)]),
                   ("order_qty", fg, t, model.prod_qty.get(t, 0.0)), ("rsl_mean", fg, t, det[("rsl_mean", fg, t)])]
        for k in d.rms:
            series += [("end_stock", k.id, t, model.rm_end[(k.id, t)]), ("position", k.id, t, det[("position", k.id, t)]),
                       ("receipts", k.id, t, det[("receipts", k.id, t)]), ("order_qty", k.id, t, model.rm_order.get((k.id, t), 0.0))]
            if k.perishable:
                series.append(("waste", k.id, t, model.rm_waste[(k.id, t)]))
        for ch in d.channels:
            series += [(name, ch.id, t, det[(name, ch.id, t)]) for name in ("served", "lost", "eligible_next_week")]
    for name, item, t, term in series:
        for kind in BAND_STATS:
            num[("band", name, item, t, kind)] = read(term, kind)
    for t in range(1, H + 1):
        num[("band_aux", "rsl_num", fg, t)] = read(det[("rsl_num", fg, t)], "mean")
        num[("band", "no_stock_share", fg, t, "mean")] = read(det[("no_stock", fg, t)], "share", -0.5)
    for key, term in det.items():
        if key[0] == "kpi":
            for kind in ("mean", "p10", "p90"):
                num[("kpi",) + key[1:] + (kind,)] = read(term, kind)
            if key[1] != "avg_end_stock":                # share of scenarios where it happens at all
                num[("kpi",) + key[1:] + ("share",)] = read(term, "share", 0.5)
    return num


def derived_numbers(oos, d):
    """After all batches are combined: conditional arrival weeks (exact, from shares) and the stock-weighted
    mean remaining shelf life (ratio of means). Weeks without any order get no arrival week (nan)."""
    fg = d.fg_id
    for (_, item, t) in [k for k in oos if isinstance(k, tuple) and k[0] == "arr_fixed"]:
        p = oos[("prod" if item == fg else item, "p_order", t)]
        for kind in BAND_STATS:
            oos[("band", "arrival_week", item, t, kind)] = oos[("arr_fixed", item, t)] if p > 1e-12 else float("nan")
    cdf = {}
    for (_, item, t, w) in [k for k in oos if isinstance(k, tuple) and k[0] == "arr_share"]:
        cdf.setdefault((item, t), []).append((w, oos[("arr_share", item, t, w)]))
    for (item, t), pts in cdf.items():
        p = oos[("prod" if item == fg else item, "p_order", t)]
        pts.sort()
        for kind in BAND_STATS:
            if p <= 1e-12:
                v = float("nan")
            elif kind == "mean":
                v, prev = 0.0, 0.0
                for w, sh in pts:
                    v, prev = v + w * (sh - prev) / p, sh
            else:
                q = int(kind[1:]) / 100.0
                v = next((w for w, sh in pts if sh / p >= q - 1e-9), pts[-1][0])
            oos[("band", "arrival_week", item, t, kind)] = v
    for t in range(1, d.horizon + 1):
        if ("band_aux", "rsl_num", fg, t) in oos:
            stock = oos[("fg_end", t)]
            oos[("band", "rsl_mean", fg, t, "mean")] = oos[("band_aux", "rsl_num", fg, t)] / stock if stock > 1e-9 else float("nan")
    for t in range(1, d.review_period + 1):                                  # commit-now notes (v16 keys)
        for item, key in [(fg, "prod")] + [(k.id, k.id) for k in d.rms]:
            oos[(key, "arrival", t)] = oos.get(("band", "arrival_week", item, t, "p50"), float("nan"))
    return oos


def out_of_sample(skr, d, layout, x, license_file, detail=True, label="Out-of-sample"):
    """Evaluates decisions x on OUT_OF_SAMPLE_SCENARIOS fresh scenarios (seeds ≠ the optimization seed), in
    batches of OUT_OF_SAMPLE_BATCH. Means and shares are combined exactly; quantiles are batch averages.
    detail=True also reads the report series of L05–L07 (Bands, KPIs)."""
    sizes = [min(OUT_OF_SAMPLE_BATCH, OUT_OF_SAMPLE_SCENARIOS - s) for s in range(0, OUT_OF_SAMPLE_SCENARIOS, OUT_OF_SAMPLE_BATCH)]
    total = {}
    for b, n in enumerate(sizes):
        with phase(f"6. {label} batch {b + 1}/{len(sizes)}: build + evaluate on {n:,} fresh scenarios"):
            env = new_env(skr, license_file, n, 0.0)
            model = build_model(skr, env, d, layout, x, d.seed + 1 + b, n, detail=detail)
            readers = report_numbers(model, d)
            env.evaluate()
            for key, read in readers.items():
                total[key] = total.get(key, 0.0) + read() * n
            env.end()
    oos = derived_numbers({key: v / sum(sizes) for key, v in total.items()}, d)
    oos["n"] = sum(sizes)
    odd = [k for k in oos if isinstance(k, tuple) and k[1] == "p10" and oos[k] > oos[(k[0], "p90", k[2])] + 1e-6]
    if odd:                                                  # plausibility gate on Seeker's quantile convention
        print(f"WARNING: P10 > P90 for {len(odd)} decision series — check aggregate_quantile semantics", flush=True)
    return oos


# ============================================================================
# 7. REPORT — results workbook and console summary (only formatting; all numbers come from Seeker)
# ============================================================================
def waste_summary(d, oos):
    """Expected waste over the horizon on the fresh scenarios (L10): rows [(item, units, share of expected demand)]
    and warnings for shares above WASTE_WARNING_SHARE. Raw materials: share of BOM × expected FG demand."""
    demand = float(d.demand_q[:, 1:, :].mean(2).sum())             # expected FG demand over the horizon
    rows = [(d.fg_id, sum(oos[("fg_waste", t)] for t in range(1, d.horizon + 1)), demand)]
    rows += [(k.id, sum(oos[(k.id, "waste", t)] for t in range(1, d.horizon + 1)), k.bom * demand)
             for k in d.rms if k.perishable]
    rows = [(i, w, w / dem if dem > 0 else 0.0) for i, w, dem in rows]
    hit = [f"{i} {sh:.1%}" for i, _, sh in rows if sh > WASTE_WARNING_SHARE]
    warn = ([f"expected waste is high ({', '.join(hit)} of expected demand; limit {WASTE_WARNING_SHARE:.0%}): shelf life "
             f"is short compared with lot sizes and lead times. Check MOQs and lot sizes (input warning X09), or consider "
             f"shelf-life-aware inventory positions (MEIO_shelf_life_aware_policies.md, K22)."] if hit else [])
    return rows, warn


def review_text(d):
    """Summary row 'Review period': which weeks this run commits and when the next run is (A03)."""
    R = d.review_period
    if R == 1:
        return "R = 1 week: the model is run every week; this run commits week 1; next run in week 2"
    return (f"R = {R} weeks: the model is run every {R} weeks; this run commits weeks 1–{R} (exact quantities, "
            f"planned with the expected demand between runs, A03); next run in week {R + 1}")


def weakest_cell(d, ins, oos):
    """The constrained cell with the lowest expected fill rate on the fresh scenarios, with its tail (L04)."""
    if not ins["constrained"]:
        return "none"
    ci, t = min(ins["constrained"], key=lambda k: oos[("fr_mean",) + k])
    return (f"{d.channels[ci].id} week {t}: expected FR {oos[('fr_mean', ci, t)]:.1%} (target {d.fill_rate_target:.1%}), "
            f"fully served in {oos[('fr_full', ci, t)]:.0%} of futures, worst 5% of futures average "
            f"{oos[('fr_tail', ci, t)]:.0%}")


def build_report(d, layout, x, ins, oos, oos_start=None):
    pol = layout.decode(x)
    H, R = d.horizon, d.review_period
    weeks = list(range(1, H + 1))
    dates = [(d.start_date + pd.Timedelta(weeks=t - 1)).date() for t in weeks]
    status_of = lambda t: ("COMMIT NOW (exact)" if t <= R else                         # A03, L03
                           f"Indicative — re-optimized at the run in week {t}" if run_week(d, t) == t else
                           f"Indicative — planned at the run in week {run_week(d, t)}")
    nv = f"{oos['n']:,} fresh scenarios"
    # --- 1. policy table (L01)
    pol_rows = []
    for t in weeks:
        row = {"Week": t, "WeekStart": dates[t - 1], "Planned at run week": run_week(d, t)}
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
    # --- 3. commit now (L03): every decision of weeks 1..R, fixed at this run from today's stock and the expected
    #        demand (A03), so the same in every scenario; only a production order can come out smaller if its raw
    #        material is late (uncertain lead times)
    commit = []
    for t in range(1, R + 1):
        w, qp = f"Week {t}", oos[("prod", "mean", t)]
        lo, hi = oos[("prod", "p10", t)], oos[("prod", "p90", t)]
        note = f"arrives at DC in week {oos[('prod', 'arrival', t)]:.0f}" if qp > 0 else "no production"
        if hi - lo > 0.5:
            note += f"; raw material permitting (P10 {lo:,.0f}, P90 {hi:,.0f})"
        commit.append([w, "Production order FG1 (KAJA)", qp, note])
        commit += [[w, f"Transfer {k.id} RMW → KAJA", k.bom * qp, "for this week's production order"] for k in d.rms]
        for k in d.rms:
            qk = oos[(k.id, "mean", t)]
            commit.append([w, f"Supplier order {k.id}", qk,
                           f"arrives at RMW in week {oos[(k.id, 'arrival', t)]:.0f}" if qk > 0 else "no order"])
    # --- 4. fill rates (L04)
    fr_rows, con = [], set(ins["constrained"])
    for ci, ch in enumerate(d.channels):
        for t in weeks:
            c = (ci, t) in con
            p_in, p_out = ins["fr_mean"][(ci, t)], oos[("fr_mean", ci, t)]
            fr_rows.append({"Channel": ch.id, "Week": t, "Target": d.fill_rate_target,
                            "Constrained": "yes" if c else "no (initial state)",
                            f"Expected FR in-sample ({ins['n']} Seeker scenarios)": p_in,
                            "Status in-sample": ("OK" if cell_met(d, p_in) else "VIOLATED") if c else "info",
                            f"Expected FR ({nv})": p_out,
                            "Status out-of-sample": ("OK" if cell_met(d, p_out) else "BELOW TARGET") if c else "info",
                            f"Fully served ({nv})": oos[("fr_full", ci, t)],
                            f"FR in a bad scenario, P10 ({nv})": oos[("fr_p10", ci, t)],
                            f"Worst 5% of futures, average FR ({nv})": oos[("fr_tail", ci, t)]})
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
    oos_met = sum(cell_met(d, oos[("fr_mean", ci, t)]) for ci, t in con)
    summ = [("Seeker status", ins["status"]), ("Seeker version", ins["version"]),
            ("Run time [s]", round(ins["seconds"], 1)), ("Seeker evaluations", ins["evaluations"]),
            ("Scenarios (Seeker in-sample / fresh out-of-sample)", f"{ins['n']} / {oos['n']}"),
            ("Decision variables / fill-rate constraints", f"{len(layout)} / {n_con}"),
            ("Seeker settings", f"speed {STOCHASTIC_SPEED}, constraint form {FILL_RATE_CONSTRAINT_FORM}, "
                                f"fill-rate target {d.fill_rate_target:.1%} ({target_source(d)})"),
            ("Review period (A03)", review_text(d)),
            ("Search space / priorities", f"10^{layout.search_space_log10():.0f} combinations; {VARIABLE_PRIORITY}"),
            ("Start policy (warm start)", f"{start_z_text(d, ins.get('start_z'))}, extra weeks FG {START_EXTRA_WEEKS_FG} / RM {START_EXTRA_WEEKS_RM}, "
                                          f"lot {START_LOT_WEEKS} weeks"),
            ("Start repair (section 4c, K21)", repair_note(ins.get("repair")))]
    fs = (ins.get("repair") or {}).get("formula_start")
    if (ins.get("repair") or {}).get("used") and fs:
        summ.append(("Start policy before repair (in-sample)", f"cost {fs['cost']:,.0f}, {fs['met']} of {len(con)} rules met"))
    summ += [("Expected total cost (in-sample)", ins["cost"]),
             ("Expected total cost (out-of-sample)", oos["cost"]),
             ("Start policy cost (in-sample)", ins["start"]["cost"]),
             ("Saving vs. start policy (in-sample)", 1 - ins["cost"] / ins["start"]["cost"] if ins["start"]["cost"] else 0)]
    summ += [(f"  cost: {n} (in-sample / out-of-sample)", f"{v:,.0f} / {oos[('part', n)]:,.0f}") for n, v in ins["parts"].items()]
    summ += [("Feasibility check (section 4b)", ins.get("feasibility", "not run")),
             ("Constraints met in-sample", f"{ins['met']} of {n_con} (start policy {ins['start']['met']})"),
             ("Constraints met out-of-sample", f"{oos_met} of {n_con}"),
             ("Weakest cell (out-of-sample)", weakest_cell(d, ins, oos)),
             ("Reorder points at their upper bound", bound_note(layout, x)),
             ("Run overrides (K20)", ", ".join(f"{k} = {v}" for k, v in getattr(d, "overrides", None).items())
              if getattr(d, "overrides", None) else "none"),
             ("Data warnings (input checks, K19)", "; ".join(getattr(d, "warnings", None) or []) or "none")]
    w_rows, w_warn = waste_summary(d, oos)
    summ += [(f"Expected waste {i} (out-of-sample, L10)", f"{w:,.0f} {d.uom.get(i, '') if getattr(d, 'uom', None) else ''}"
              f" = {sh:.1%} of expected demand".replace("  ", " ")) for i, w, sh in w_rows]
    summ.append(("Waste warning (L10)", w_warn[0] if w_warn else "none"))
    summ += [(f"Time: {n}", round(t, 1)) for n, t in TIMINGS]
    bands, kpis = band_and_kpi_tables(d, ins, oos, oos_start)
    return {"Summary": pd.DataFrame(summ, columns=["Item", "Value"]),
            "Commit_Now": pd.DataFrame(commit, columns=["When", "Decision", "Quantity", "Note"]),
            "Policy_sS": pd.DataFrame(pol_rows), "Decisions": pd.DataFrame(dec_rows),
            "FillRates": pd.DataFrame(fr_rows), "Stock_Waste": pd.DataFrame(sw),
            "Bands": bands, "KPIs": kpis}


BAND_SERIES = ("end_stock", "position", "waste", "order_qty", "arrival_week", "receipts", "served", "lost",
               "eligible_next_week", "rsl_mean", "no_stock_share")


def band_and_kpi_tables(d, ins, oos, oos_start=None):
    """Sheets Bands (L05/L06: every weekly series as mean + P05…P95 over the fresh scenarios, long format)
    and KPIs (L07: totals per scenario, mean + P10/P90; shares; L08: the start policy on the same seeds)."""
    cols = ["Mean", "P05", "P10", "P25", "P50", "P75", "P90", "P95"]
    order = {n: i for i, n in enumerate(BAND_SERIES)}
    keys = sorted({k[1:4] for k in oos if isinstance(k, tuple) and k[0] == "band"},
                  key=lambda k: (order.get(k[0], 99), k[1] != d.fg_id, k[1], k[2]))
    rows = [[name, item, t] + [oos.get(("band", name, item, t, s), float("nan")) for s in BAND_STATS]
            for name, item, t in keys]
    bands = pd.DataFrame(rows, columns=["Series", "Item", "Week"] + cols)
    H, fg, u = d.horizon, d.fg_id, getattr(d, "uom", None) or {}
    t0 = 1 + d.prod_lead_time.min if d.enforce_from_first_controllable else 1
    k_rows = []

    def add(label, unit, key, share_only=False):
        if ("kpi",) + key + ("mean",) not in oos and not share_only:
            return
        if share_only:
            k_rows.append([label, "share of scenarios", oos.get(("kpi",) + key + ("share",), float("nan")), None, None])
        else:
            k_rows.append([label, unit] + [oos[("kpi",) + key + (s,)] for s in ("mean", "p10", "p90")])
    add(f"Average end stock {fg}, weeks 1–{H}", u.get(fg, ""), ("avg_end_stock", fg))
    for k in d.rms:
        add(f"Average end stock {k.id}, weeks 1–{H}", u.get(k.id, ""), ("avg_end_stock", k.id))
    add(f"Total waste {fg}, weeks 1–{H}", u.get(fg, ""), ("waste", fg))
    for k in d.rms:
        if k.perishable:
            add(f"Total waste {k.id}, weeks 1–{H}", u.get(k.id, ""), ("waste", k.id))
    for ch in d.channels:
        if t0 > 1:
            add(f"Lost sales {ch.id}, weeks 1–{t0 - 1} (initial state, no decision can change them)", u.get(fg, ""),
                ("lost_initial_weeks", ch.id))
        add(f"Lost sales {ch.id}, weeks {t0}–{H} (weeks with fill-rate rules)", u.get(fg, ""), ("lost_controllable_weeks", ch.id))
    add(f"Scenarios with any {fg} waste", "", ("waste", fg), share_only=True)
    for ch in d.channels:
        if t0 > 1:
            add(f"Scenarios with lost sales {ch.id}, weeks 1–{t0 - 1}", "", ("lost_initial_weeks", ch.id), share_only=True)
        add(f"Scenarios with lost sales {ch.id}, weeks {t0}–{H}", "", ("lost_controllable_weeks", ch.id), share_only=True)
    if oos_start is not None:
        con = ins["constrained"]
        met = lambda o: sum(cell_met(d, o[("fr_mean", ci, t)]) for ci, t in con)
        k_rows += [["Start policy: expected total cost (same fresh seeds)", "currency", oos_start["cost"], None, None],
                   ["Start policy: fill-rate rules met (same fresh seeds)", f"of {len(con)}", met(oos_start), None, None],
                   ["Optimized policy: fill-rate rules met (fresh)", f"of {len(con)}", met(oos), None, None],
                   ["Saving vs. start policy (fresh, same seeds)", "share",
                    1 - oos["cost"] / oos_start["cost"] if oos_start["cost"] else float("nan"), None, None]]
    return bands, pd.DataFrame(k_rows, columns=["Item", "Unit", "Mean", "P10", "P90"])


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
                    color = "C6EFCE" if name == "Commit_Now" or "COMMIT NOW" in txt else None   # weeks 1..R
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
    overrides: dict = None   # run overrides that were applied (K20)
    oos_start: dict = None   # start policy on the same fresh seeds (START_POLICY_OUT_OF_SAMPLE, L08)


OVERRIDES = {"NumScenarios": ("n_scenarios", 1), "RandomSeed": ("seed", 0)}   # run override → field, minimum


def apply_overrides(d, overrides):
    """Run options that replace Excel settings for one run (K20). Applied before validate(), so they are checked."""
    bad = []
    for k, v in (overrides or {}).items():
        if k not in OVERRIDES:
            bad.append(f"unknown run override {k} (allowed: {', '.join(OVERRIDES)})")
        elif not (isinstance(v, (int, np.integer)) or (isinstance(v, float) and v.is_integer())) or v < OVERRIDES[k][1]:
            bad.append(f"run override {k} must be a whole number ≥ {OVERRIDES[k][1]}")
        else:
            setattr(d, OVERRIDES[k][0], int(v))
    if bad:
        raise ValueError("Data validation failed:\n  - " + "\n  - ".join(bad))
    d.overrides = {k: int(v) for k, v in overrides.items()} if overrides else None


def run(data_file=None, license_file=None, time_limit=None, skr=None, overrides=None):
    """Sections 1 → 7 without writing files. Settings come from section 0; arguments override the files and
    the time limit; overrides = {"NumScenarios": int, "RandomSeed": int} replaces those Excel settings for this
    run (K20). Raises ValueError (invalid data; the message lists every problem), FileNotFoundError (data or
    license file) and ImportError (Seeker not installed). Progress is printed to stdout."""
    data_file, license_file = data_file or DATA_FILE, license_file or LICENSE_FILE
    TIMINGS.clear()
    if not Path(data_file).exists():
        raise FileNotFoundError(f"Data file not found: {data_file}  (set DATA_FILE in section 0)")
    with phase(f"1. Reading and checking {Path(data_file).name}"):
        d = load_data(data_file)
        apply_overrides(d, overrides)
        validate(d)
    if d.warnings:
        print("Data warnings: " + "; ".join(d.warnings), flush=True)
    if d.overrides:
        print("Run overrides: " + ", ".join(f"{k} = {v}" for k, v in d.overrides.items()), flush=True)
    layout = PolicyLayout(d)                                                   # section 2
    time_limit = float(time_limit or TIME_LIMIT_SECONDS or d.time_limit)
    print(f"Data OK: horizon {d.horizon} weeks, {len(d.channels)} channels, {len(d.rms)} raw materials, "
          f"{len(layout)} decision variables. Seeker time limit {time_limit:.0f} s.", flush=True)
    if not Path(license_file).exists():
        raise FileNotFoundError(f"License file not found: {license_file}  (set LICENSE_FILE in section 0)")
    skr = skr or import_seeker()

    z0 = start_z(d) if START_Z is None else START_Z                            # section 4
    zrec = dict(z0=z0, z=z0, steps=[])
    pol0 = start_policy(d, z=z0)
    layout.include_start(pol0)
    x0 = layout.encode(pol0)
    print(f"Start policy (warm start): {start_z_text(d, zrec)}, extra weeks FG {START_EXTRA_WEEKS_FG} / RM "
          f"{START_EXTRA_WEEKS_RM}, lot {START_LOT_WEEKS} weeks. Bounds: section 2, EXTRA_COVER_WEEKS {EXTRA_COVER_WEEKS}. "
          f"Search space 10^{layout.search_space_log10():.0f}.", flush=True)

    max_fr = feasibility_check(skr, d, layout, license_file) if FEASIBILITY_CHECK else None   # section 4b
    x, ins = optimize(skr, d, layout, x0, license_file, time_limit, max_fr)  # section 5 (incl. start check)
    repair = dict(used=False)
    if x is None:                                    # start check failed → higher z (4), repair (4c), then 5 again
        start0 = ins["start"]
        if max_fr is None:                        # FEASIBILITY_CHECK = False: the start check needs maximum supply
            max_fr = feasibility_check(skr, d, layout, license_file,
                                       "4c. Start check: Seeker evaluates the maximum-supply policy")
        if start_misses(d, start0["fr"], max_fr) and START_Z is None:
            z, pol0, start0, zrec = raise_start_z(skr, d, layout, z0, start0, max_fr, license_file)
            layout.include_start(pol0)
            x0 = layout.encode(pol0)
        if start_misses(d, start0["fr"], max_fr) and not START_REPAIR:
            print(f"Start check: the start still misses {len(start_misses(d, start0['fr'], max_fr))} rule(s) that can be "
                  f"met; START_REPAIR = False (section 0), so Seeker starts from it anyway", flush=True)
            repair["note"] = "off (START_REPAIR = False) — the start misses rules that can be met"
        elif start_misses(d, start0["fr"], max_fr):
            formula = dict(cost=start0["cost"], met=start0["met"])
            pol0, repair = repair_start(skr, d, layout, pol0, start0["fr"], start0["cost"], max_fr, license_file)
            repair["formula_start"] = formula
            layout.include_start(pol0)
            x0 = layout.encode(pol0)
        else:
            repair["note"] = ("not needed — the start check raised z (see 'Start policy')" if zrec["steps"] else
                              "not needed — the start policy misses only rules that fail whatever we decide")
            if not zrec["steps"]:
                print("Start check: the start policy misses only rules that fail whatever we decide — no repair needed",
                      flush=True)
        x, ins = optimize(skr, d, layout, x0, license_file, time_limit, max_fr if FEASIBILITY_CHECK else None,
                          start_check=False, formula_fr=start0["fr"])
    ins["repair"], ins["start_z"] = repair, zrec
    if layout.at_bound(x):
        print(f"NOTE: reorder point(s) at their upper bound: {bound_note(layout, x)}", flush=True)
    oos = out_of_sample(skr, d, layout, x, license_file)                       # section 6
    _, waste_warn = waste_summary(d, oos)                                      # L10
    if waste_warn:
        print("WARNING (L10): " + " ".join(waste_warn), flush=True)
    oos_start = out_of_sample(skr, d, layout, x0, license_file, detail=False, label="Start policy, out-of-sample") \
        if START_POLICY_OUT_OF_SAMPLE else None
    sheets = build_report(d, layout, x, ins, oos, oos_start)                   # section 7
    return RunResult(d, layout, x, layout.decode(x), ins, oos, sheets, list(TIMINGS), d.overrides, oos_start)


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
