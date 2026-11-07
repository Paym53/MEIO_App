"""
================================================================================
 REFERENCE SIMULATOR — an independent second implementation of the MEIO rules (for tests only)
================================================================================
Written separately from the engine, as plain NumPy, from the math model (rules A–J of
MEIO_model_spec.md). The test suite runs the engine (Seeker Terms, via the test double) and this
simulator on the SAME scenarios and requires identical cost, cost parts, fill rates, orders, arrivals,
stock, waste and holding in every scenario. Frozen: change it only together with a rule change.

Contents: 1 data loading & validation · 2 scenario generation · 3 simulator (E1–E6) + chance statistic ·
          4 the formula start policy (to check the engine's start policy)
"""
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import warnings

warnings.filterwarnings("ignore", message="Data Validation extension is not supported")
EPS = 1e-9


# ============================================================================
# 1. DATA LOADING & VALIDATION
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
    """Quantity-dependent unit cost. AllUnits: whole qty at the tier it falls into."""
    froms: np.ndarray
    tos: np.ndarray          # np.inf for the last tier
    costs: np.ndarray
    mode: str                # "AllUnits" or "Incremental"

    def cost(self, q):
        q = np.asarray(q, dtype=float)
        if self.mode == "Incremental":
            slices = np.clip(q[..., None] - self.froms, 0, self.tos - self.froms)
            return (slices * self.costs).sum(-1)
        idx = np.clip(np.searchsorted(self.froms, q, side="right") - 1, 0, len(self.costs) - 1)
        return self.costs[idx] * q


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
# 2. SCENARIO GENERATION (G02, E03) — common random numbers, fixed seed
# ============================================================================
def sample_from_quantiles(q, levels, u):
    """Inverse-CDF sampling by linear interpolation through the quantile points (G02).
    Tails: from u=0 at max(0, Q_first − (Q_2nd − Q_first)/2) and to u=1 at Q_last + (Q_last − Q_2nd-last)/2."""
    lo = max(0.0, q[0] - (q[1] - q[0]) / 2.0)
    hi = q[-1] + (q[-1] - q[-2]) / 2.0
    x = np.concatenate(([0.0], levels, [1.0]))
    y = np.concatenate(([lo], q, [hi]))
    return np.interp(u, x, y)


def sample_lead_times(lt, size, rng):
    return rng.choice(lt.values, size=size, p=lt.probs)


@dataclass
class Scenarios:
    """All random inputs per scenario ω: demand D[ω,c,t] and a lead time per (source, week) (CRN)."""
    n: int
    demand: np.ndarray            # (N, C, H+1) integer units, index t = 1..H
    lt_prod: np.ndarray           # (N, H+1) production lead time if an order is placed in week t
    lt_rm: dict                   # rm id -> (N, H+1)
    open_prod_arrival: np.ndarray  # (N, n_open_prod)
    open_rm_arrival: dict         # rm id -> (N, n_open)


def make_scenarios(d, n, seed):
    ss = np.random.SeedSequence(seed)
    streams = [np.random.default_rng(s) for s in ss.spawn(3 + 2 * len(d.rms))]
    H, C = d.horizon, len(d.channels)
    u = streams[0].random((n, C, H + 1))
    demand = np.zeros((n, C, H + 1))
    for c in range(C):
        for t in range(1, H + 1):
            demand[:, c, t] = np.rint(sample_from_quantiles(d.demand_q[c, t], d.levels, u[:, c, t]))
    demand = np.maximum(demand, 0)
    lt_prod = sample_lead_times(d.prod_lead_time, (n, H + 1), streams[1])
    lt_rm = {k.id: sample_lead_times(k.lead_time, (n, H + 1), streams[3 + i]) for i, k in enumerate(d.rms)}

    def open_arrivals(orders, lt, rng):
        """Open orders: fixed arrival if certain; otherwise sampled (conditional on not yet arrived).
        No crossing (E04): each order arrives no earlier than the previously placed one."""
        orders = sorted(orders, key=lambda o: o["placed"])
        arr = np.zeros((n, len(orders)), dtype=int)
        last = np.zeros(n, dtype=int)
        for j, o in enumerate(orders):
            if o["certain"]:
                a = np.full(n, o["arrival"])
            else:
                ok = lt.values + o["placed"] >= 1
                vals, p = (lt.values[ok], lt.probs[ok] / lt.probs[ok].sum()) if ok.any() else (np.array([1 - o["placed"]]), np.array([1.0]))
                a = o["placed"] + rng.choice(vals, size=n, p=p)
            arr[:, j] = np.maximum(a, last)
            last = arr[:, j]
        return orders, arr

    d.open_prod, open_prod_arrival = open_arrivals(d.open_prod, d.prod_lead_time, streams[2])
    open_rm_arrival = {}
    for i, k in enumerate(d.rms):
        d.open_rm[k.id], open_rm_arrival[k.id] = open_arrivals(d.open_rm[k.id], k.lead_time, streams[4 + i])
    return Scenarios(n, demand, lt_prod, lt_rm, open_prod_arrival, open_rm_arrival)


# ============================================================================
# 3. SIMULATOR — the model dynamics (A04, E1–E6), vectorized over scenarios
# ============================================================================
def fefo_take(stock, need):
    """Take `need` units FEFO from lots stock[:, 0..m] ordered oldest → freshest.
    Returns the quantity taken from each lot (same shape as stock)."""
    before = np.cumsum(stock, axis=1) - stock           # stock in older lots
    return np.minimum(stock, np.maximum(0.0, need[:, None] - before))


def round_up(x, m):
    return m * np.ceil(x / m - EPS)


def round_down(x, m):
    return m * np.floor(x / m + EPS)


@dataclass
class SimResult:
    cost: np.ndarray                       # (N,) total cost per scenario
    cost_parts: dict                       # name -> (N,)
    fill_rate: np.ndarray                  # (N, C, H+1)
    detail: dict = field(default_factory=dict)


class MEIOSimulator:
    """Computes all states and costs for a policy and all scenarios.

    Lot bookkeeping: stock[:, a] = quantity with `a` weeks of remaining shelf life.
    Event order in week t (A04):
      E1 receipts → E2 FG review & production (+RM withdrawal) → E3 RM review & supplier orders
      → E4 demand & allocation → E5 holding cost → E6 ageing & immediate waste.
    """

    def __init__(self, data, scen):
        self.d, self.s = data, scen
        self.H, self.N = data.horizon, scen.n
        max_open = max([0] + [int(scen.open_prod_arrival.max(initial=0))] +
                       [int(a.max(initial=0)) for a in scen.open_rm_arrival.values()])
        max_lt = max([data.prod_lead_time.max] + [k.lead_time.max for k in data.rms])
        self.T = max(self.H, max_open) + max_lt + 2       # length of arrival calendars

    def run(self, policy, detail=False):
        d, sc, H, N = self.d, self.s, self.H, self.N
        rows = np.arange(N)
        C = len(d.channels)
        rho_min = d.rho_min
        SLf = d.fg_shelf_life

        # ---- initial state (J01, J02) ----------------------------------------------------
        X = np.zeros((N, SLf + 1))                               # FG lots at DC
        for a, q in d.init_fg:
            X[:, a] += q
        R = {}                                                   # RM lots at RMW
        for k in d.rms:
            R[k.id] = np.zeros((N, (k.shelf_life + 1) if k.perishable else 1))
            for a, q in d.init_rm[k.id]:
                R[k.id][:, a if k.perishable else 0] += q
        arr_fg = np.zeros((N, self.T))                           # arrival calendar (full shelf life)
        pipe_fg = np.zeros(N)
        special = []                                             # open orders with given rest shelf life
        for j, o in enumerate(d.open_prod):
            a_t = sc.open_prod_arrival[:, j]
            pipe_fg += o["qty"]
            if o["remaining"] is None:
                arr_fg[rows, a_t] += o["qty"]
            else:
                special.append(("FG", a_t, int(o["remaining"]), o["qty"]))
        last_prod = sc.open_prod_arrival.max(axis=1) if d.open_prod else np.zeros(N, dtype=int)
        arr_rm, pipe_rm, last_rm = {}, {}, {}
        for k in d.rms:
            arr_rm[k.id], pipe_rm[k.id] = np.zeros((N, self.T)), np.zeros(N)
            arrs = sc.open_rm_arrival[k.id]
            for j, o in enumerate(d.open_rm[k.id]):
                pipe_rm[k.id] += o["qty"]
                if o["remaining"] is None or not k.perishable:
                    arr_rm[k.id][rows, arrs[:, j]] += o["qty"]
                else:
                    special.append((k.id, arrs[:, j], int(o["remaining"]), o["qty"]))
            last_rm[k.id] = arrs.max(axis=1) if d.open_rm[k.id] else np.zeros(N, dtype=int)

        # ---- accumulators --------------------------------------------------------------
        parts = {n: np.zeros(N) for n in ("rm_ordering", "production", "transport", "holding", "waste")}
        FR = np.ones((N, C, H + 1))
        if detail:
            rec = {n: np.zeros((N, H + 1)) for n in ("prod_qty", "prod_arrival", "ip_fg", "fg_end", "fg_waste",
                                                     "fg_received", "holding")}
            for k in d.rms:
                for n in ("order", "arrival", "transfer", "ip", "end", "waste", "received"):
                    rec[f"{k.id}_{n}"] = np.zeros((N, H + 1))
            rec["served"] = np.zeros((N, C, H + 1))

        for t in range(1, H + 1):
            # ---- E1: receipts (full shelf life, F01) ------------------------------------
            got = arr_fg[:, t].copy()
            X[:, SLf] += got
            pipe_fg -= got
            if detail:
                rec["fg_received"][:, t] = got
            for k in d.rms:
                got_k = arr_rm[k.id][:, t].copy()
                R[k.id][:, k.shelf_life if k.perishable else 0] += got_k
                pipe_rm[k.id] -= got_k
                if detail:
                    rec[f"{k.id}_received"][:, t] = got_k
            for (who, a_t, rem, q) in special:
                hit = (a_t == t) * q
                if who == "FG":
                    X[:, rem] += hit
                    pipe_fg -= hit
                else:
                    R[who][:, rem] += hit
                    pipe_rm[who] -= hit

            # ---- E2: FG review at DC → production order (C01, C06, D02, D03, E04) -------
            s_t, S_t = policy["FG"][0][t], policy["FG"][1][t]
            ip_fg = X[:, rho_min:].sum(1) + pipe_fg              # sellable stock + pipeline
            trigger = ip_fg < s_t                                # strict trigger (C06)
            q_hat = np.minimum(d.prod_max, np.maximum(d.prod_moq, round_up(S_t - ip_fg, d.prod_batch)))
            cap = np.full(N, np.inf)                             # raw material availability (D02)
            for k in d.rms:
                avail = R[k.id][:, k.min_remaining:].sum(1) if k.perishable else R[k.id][:, 0]
                cap = np.minimum(cap, avail / k.bom)
            q_chk = np.minimum(q_hat, round_down(cap, d.prod_batch))
            q_p = np.where(trigger & (q_chk >= d.prod_moq - EPS), q_chk, 0.0)
            placed = q_p > 0
            arrival = np.maximum(t + sc.lt_prod[:, t], last_prod)   # no overtaking (E04)
            arr_fg[rows[placed], arrival[placed]] += q_p[placed]
            pipe_fg += q_p
            last_prod = np.where(placed, arrival, last_prod)
            for k in d.rms:                                      # transfer RMW → KAJA, FEFO (B02, F04)
                need = k.bom * q_p
                if k.perishable:
                    R[k.id][:, k.min_remaining:] -= fefo_take(R[k.id][:, k.min_remaining:], need)
                else:
                    R[k.id][:, 0] -= need
                if detail:
                    rec[f"{k.id}_transfer"][:, t] = need
            parts["production"] += d.prod_fixed * placed + d.prod_tiers.cost(q_p)
            if d.transport_tiers is not None:
                parts["transport"] += d.transport_tiers.cost(q_p)
            if detail:
                rec["prod_qty"][:, t], rec["ip_fg"][:, t] = q_p, ip_fg
                rec["prod_arrival"][:, t] = np.where(placed, arrival, 0)

            # ---- E3: RM review at RMW on echelon position → supplier orders (C03, C04, D01)
            ip_fg_after = ip_fg + q_p
            for k in d.rms:
                usable_next = R[k.id][:, k.min_remaining + 1:].sum(1) if k.perishable else R[k.id][:, 0]
                ip_k = usable_next + pipe_rm[k.id] + k.bom * ip_fg_after
                s_k, S_k = policy[k.id][0][t], policy[k.id][1][t]
                q_k = np.minimum(k.max_order, np.maximum(k.moq, round_up(S_k - ip_k, k.multiple)))
                q_k = np.where(ip_k < s_k, q_k, 0.0)
                placed_k = q_k > 0
                arr_k = np.maximum(t + sc.lt_rm[k.id][:, t], last_rm[k.id])
                arr_rm[k.id][rows[placed_k], arr_k[placed_k]] += q_k[placed_k]
                pipe_rm[k.id] += q_k
                last_rm[k.id] = np.where(placed_k, arr_k, last_rm[k.id])
                parts["rm_ordering"] += k.fixed_cost * placed_k + k.var_cost * q_k
                if detail:
                    rec[f"{k.id}_order"][:, t], rec[f"{k.id}_ip"][:, t] = q_k, ip_k
                    rec[f"{k.id}_arrival"][:, t] = np.where(placed_k, arr_k, 0)

            # ---- E4: demand, channels by priority, FEFO among eligible lots (H01–H04) ----
            for ci, ch in enumerate(d.channels):
                dem = sc.demand[:, ci, t]
                take = fefo_take(X[:, ch.min_remaining:], dem)
                X[:, ch.min_remaining:] -= take
                served = take.sum(1)
                FR[:, ci, t] = np.where(dem > 0, served / np.maximum(dem, EPS), 1.0)
                if detail:
                    rec["served"][:, ci, t] = served

            # ---- E5: holding cost on end-of-week on-hand (I05) ---------------------------
            hold = d.fg_holding * X.sum(1) + sum(k.holding * R[k.id].sum(1) for k in d.rms)
            parts["holding"] += hold
            if detail:
                rec["holding"][:, t] = hold
                rec["fg_end"][:, t] = X.sum(1)
                for k in d.rms:
                    rec[f"{k.id}_end"][:, t] = R[k.id].sum(1)

            # ---- E6: ageing; lots no longer usable are wasted immediately (F05, C02, C04)
            w_fg = X[:, :rho_min + 1].sum(1)                   # rest shelf life ≤ ρ_min → unusable next week
            X[:, :-1], X[:, -1] = X[:, 1:].copy(), 0.0
            X[:, :rho_min] = 0.0
            waste_cost = d.fg_waste * w_fg
            if detail:
                rec["fg_waste"][:, t] = w_fg
            for k in d.rms:
                if k.perishable:
                    w_k = R[k.id][:, :k.min_remaining + 1].sum(1)
                    R[k.id][:, :-1], R[k.id][:, -1] = R[k.id][:, 1:].copy(), 0.0
                    R[k.id][:, :k.min_remaining] = 0.0
                    waste_cost += k.waste * w_k
                    if detail:
                        rec[f"{k.id}_waste"][:, t] = w_k
            parts["waste"] += waste_cost

        total = sum(parts.values())
        res = SimResult(total, parts, FR)
        if detail:
            res.detail = rec
        return res


def chance_stat(fill_rate, chance_level):
    """(v+1)-th smallest fill rate across scenarios, v = ⌊(1−α)·N⌋ (H07).
    'At most v scenarios below target' ⇔ this value ≥ target."""
    n = fill_rate.shape[0]
    v = int(math.floor((1 - chance_level) * n + EPS))
    return np.partition(fill_rate, v, axis=0)[v]


# ============================================================================
# 4. FORMULA START POLICY (K07) — independent copy, to check the engine's start_policy
# ============================================================================
def heuristic_policy(d, z=4.0, extra_fg=2, extra_rm=4, lot_weeks=4):
    """Simple, explainable start policy for Seeker (and the baseline in the report):
      FG  reorder point s = expected demand over (production LT + 1 + extra_fg) weeks + z × std dev
      RM  reorder point s = BOM × same over (RM LT + production LT + 1 + extra_rm) weeks  (echelon)
      order-up-to S = s + about `lot_weeks` weeks of demand (at least the MOQ)."""
    H = d.horizon
    mean = d.demand_q[:, 1:, :].mean(2).sum(0)                    # per week, all channels
    sd = np.sqrt((((d.demand_q[:, 1:, -1] - d.demand_q[:, 1:, 0]) / 3.29) ** 2).sum(0))
    mean, sd = np.concatenate(([0], mean)), np.concatenate(([0], sd))

    def cover(t, weeks):
        tt = np.arange(t, min(H, t + weeks - 1) + 1)
        return mean[tt].sum() + z * math.sqrt((sd[tt] ** 2).sum())

    pol = {}
    Lp = d.prod_lead_time.max
    lot = max(d.prod_moq, lot_weeks * mean[1:].mean())
    s = np.zeros(H + 1); S = np.zeros(H + 1)
    for t in range(1, H - d.prod_lead_time.min + 1):
        s[t] = cover(t, Lp + 1 + extra_fg)
        S[t] = s[t] + lot
    pol["FG"] = (s, S)
    for k in d.rms:
        s = np.zeros(H + 1); S = np.zeros(H + 1)
        for t in range(1, H - d.prod_lead_time.min - k.lead_time.min + 1):
            s[t] = k.bom * cover(t, k.lead_time.max + Lp + 1 + extra_rm)
            S[t] = s[t] + max(k.moq, k.bom * lot)
        pol[k.id] = (s, S)
    return pol
