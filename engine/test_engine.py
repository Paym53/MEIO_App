"""
================================================================================
 MEIO ENGINE — QUALITY CONTROL (the one test suite)            run:  python -m pytest -q test_engine.py
================================================================================
Checks the engine (meio_seeker_stochastic.py) without a Seeker license, using the Seeker test double in
section 0. The supply-chain rules are checked against reference_simulator.py, an independent second
implementation, scenario by scenario.

  0. SEEKER TEST DOUBLE ...... strict stand-in for Seeker's Python API (evaluates the same model Terms)
  1. INPUT CONTROL ........... the Excel is read correctly; every Excel check (C01–C14, X01–X07) also runs in
                               Python, so edited copies are checked as strictly; every error has a message;
                               warnings X08 (open orders vs lead time) and X09 (lot size vs shelf life)
  2. PHYSICS & DYNAMICS ...... engine = reference in every scenario: lots, FEFO, priority, (s,S), echelon,
                               lead times, no overtaking, tiers, waste, fill rates; edge cases
  3. MODEL FUNCTIONALITY ..... decision variables & bounds, start policy (target-aware z, lot cap, lot guard), fill-rate rule,
                               fill-rate target from Excel or section 0, priorities, feasibility check, start check
                               and repair, search model unchanged by the report series, bands, arrival weeks,
                               KPIs, expected waste and warning, run overrides, start policy on fresh scenarios
  4. OUTPUT CONTROL .......... whole run vs reference, run() result, frozen output schema, errors, files
  5. STRESS-TEST CASES ....... the 26 data changes of meio_stress_tests.py do what they say
  6. ENGINE FINGERPRINT ...... the engine files are unchanged (they are maintained in the engine session)
================================================================================
"""
import copy
import math
import hashlib
import sys
import types
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# ============================================================================
# 0. SEEKER TEST DOUBLE — strict about the documented Seeker 2.1 rules: aggregators take lists of Terms only;
#    get_value() refused on stochastic Terms; the optimization target must be deterministic; comparisons use
#    the documented 1e-8 slack; set_value takes one argument. Evaluates the Term graph on numpy vectors (one
#    entry per scenario). Its "search" is a trivial random search: numbers are real model numbers, not optima.
# ============================================================================
import random  # noqa: E402
import time  # noqa: E402

import time

SLACK = 1e-8


class UserDistribution:
    def __init__(self):
        pass


class UserEvalVector:
    def __init__(self):
        pass


def _is_term(x):
    return isinstance(x, Term)


class Term:
    __slots__ = ("env", "i", "stoch")

    def __init__(self, env, fn, deps, stoch):
        self.env, self.stoch = env, stoch
        self.i = env._register(fn, deps)

    # --- helpers
    def _bin(self, other, f, swap=False):
        e = self.env
        if _is_term(other):
            assert other.env is e, "terms from different environments"
            a, b = (other, self) if swap else (self, other)
            return Term(e, f, [a.i, b.i], a.stoch or b.stoch)
        c = float(other)
        if swap:
            return Term(e, lambda x, c=c: f(c, x), [self.i], self.stoch)
        return Term(e, lambda x, c=c: f(x, c), [self.i], self.stoch)

    def __add__(self, o): return self._bin(o, lambda a, b: a + b)
    def __radd__(self, o): return self._bin(o, lambda a, b: a + b, True)
    def __sub__(self, o): return self._bin(o, lambda a, b: a - b)
    def __rsub__(self, o): return self._bin(o, lambda a, b: a - b, True)
    def __mul__(self, o): return self._bin(o, lambda a, b: a * b)
    def __rmul__(self, o): return self._bin(o, lambda a, b: a * b, True)
    def __truediv__(self, o): return self._bin(o, lambda a, b: a / b)
    def __rtruediv__(self, o): return self._bin(o, lambda a, b: a / b, True)
    def __neg__(self): return Term(self.env, lambda a: -a, [self.i], self.stoch)
    def __lt__(self, o): return self._bin(o, lambda a, b: (a < b - SLACK) * 1.0)
    def __le__(self, o): return self._bin(o, lambda a, b: (a <= b + SLACK) * 1.0)
    def __gt__(self, o): return self._bin(o, lambda a, b: (a > b + SLACK) * 1.0)
    def __ge__(self, o): return self._bin(o, lambda a, b: (a >= b - SLACK) * 1.0)
    def __eq__(self, o): return self._bin(o, lambda a, b: (np.abs(a - b) <= SLACK) * 1.0)
    __hash__ = object.__hash__

    def __bool__(self):
        raise TypeError("Python truth value of a Term is not allowed (use env.if_ / and_ / or_)")

    def not_(self): return Term(self.env, lambda a: (a == 0) * 1.0, [self.i], self.stoch)

    def get_value(self):
        if self.stoch:
            raise RuntimeError("Please use Term::get_values() instead")
        return float(self.env._vals[self.i])

    def set_value(self, v):                       # Seeker 2.1 signature: (value) only
        k = self.env._var_of.get(self.i)
        assert k is not None, "strict test double: set_value only on decision variables"
        _, l, h = self.env._vars[k]
        assert l <= v <= h, "value outside the variable's domain"
        self.env._x[k] = float(v)

    def get_values(self):
        v = self.env._vals[self.i]
        return [float(x) for x in np.broadcast_to(v, (self.env.R,))]


class Env:
    ACCEPT_MAX_SAMPLE = False          # False = Seeker 2.1 signatures (no maxSample), True = 2.2

    def __init__(self, license, stochastic=False, *args):
        assert isinstance(license, str)
        self.stochastic = bool(stochastic)
        self.R, self._fns, self._deps, self._vals = 100, [], [], []
        self._vars, self._x, self._cons, self._var_of = [], [], [], {}
        self.status, self.n_eval, self.rng = "unoptimized", 0, np.random.default_rng(0)

    # --- graph
    def _register(self, fn, deps):
        self._fns.append(fn); self._deps.append(deps); self._vals.append(None)
        i = len(self._fns) - 1
        self._vals[i] = fn(*[self._vals[j] for j in deps])          # eager evaluation
        return i

    def _recompute(self):
        vals, fns, deps = self._vals, self._fns, self._deps
        for i in range(len(fns)):
            vals[i] = fns[i](*[vals[j] for j in deps[i]])
        self.n_eval += 1

    def n_terms(self):
        return len(self._fns)

    # --- API
    def version(self): return "FAKE stochastic 2.1.0"
    def set_stochastic_parameters(self, resolution, speed):
        assert self.stochastic and len(self._fns) == 0 and 0 <= speed <= 1
        self.R = int(resolution)
    def set_report(self, s): pass
    def get_status(self): return self.status
    def get_number_evaluations(self): return self.n_eval
    def end(self): self.ended = True
    def evaluate(self): self._recompute()

    def convert(self, x):
        assert not isinstance(x, (list, tuple))
        c = float(x)
        return Term(self, lambda c=c: c, [], False)

    def ordinal(self, l, h, v):
        assert l <= v <= h and float(v).is_integer()
        k = len(self._x); self._x.append(float(v))
        t = Term(self, lambda k=k: self._x[k], [], False)
        t_info = (t, l, h); self._vars.append(t_info)
        self._var_of[t.i] = k
        return t

    def user_distribution(self, ud, *maxSample):
        if maxSample and not self.ACCEPT_MAX_SAMPLE:
            raise TypeError("user_distribution(): incompatible function arguments (2.1: (self, arg0: UserDistribution))")
        assert isinstance(ud, UserDistribution)
        assert self.stochastic, "Creating random data for non-stochastic environment!"
        s = np.asarray(ud.sample_n(max(self.R, int(maxSample[0]) if maxSample else self.R)), dtype=float)
        v = s[: self.R].copy()
        return Term(self, lambda v=v: v, [], True)

    def categorical_distribution(self, weights, values, *maxSample):
        if maxSample and not self.ACCEPT_MAX_SAMPLE:
            raise TypeError("categorical_distribution(): incompatible function arguments")
        assert self.stochastic
        w = np.asarray(weights, float); w = w / w.sum()
        v = self.rng.choice(np.asarray(values, float), size=self.R, p=w)
        return Term(self, lambda v=v: v, [], True)

    def _list(self, ts):
        assert isinstance(ts, list) and len(ts) > 0 and all(_is_term(t) for t in ts), "aggregators need a non-empty list of Terms"
        return [t.i for t in ts], any(t.stoch for t in ts)

    def sum(self, ts):
        idx, st = self._list(ts)
        return Term(self, lambda *a: sum(a[1:], a[0]), idx, st)

    def min(self, ts):
        idx, st = self._list(ts)
        return Term(self, lambda *a: np.minimum.reduce(np.broadcast_arrays(*a)) if len(a) > 1 else a[0], idx, st)

    def max(self, ts):
        idx, st = self._list(ts)
        return Term(self, lambda *a: np.maximum.reduce(np.broadcast_arrays(*a)) if len(a) > 1 else a[0], idx, st)

    def max_0(self, t): return Term(self, lambda a: np.maximum(a, 0.0), [t.i], t.stoch)
    def ceil(self, t): return Term(self, np.ceil, [t.i], t.stoch)
    def floor(self, t): return Term(self, np.floor, [t.i], t.stoch)

    def if_(self, c, a, b):
        assert _is_term(c) and _is_term(a) and _is_term(b), "strict test double: if_ only with Terms"
        return Term(self, lambda cc, x, y: np.where(cc != 0, x, y), [c.i, a.i, b.i], c.stoch or a.stoch or b.stoch)

    # --- aggregators (stochastic → deterministic)
    def _agg(self, t, f):
        assert _is_term(t) and t.stoch, "aggregator source must be stochastic"
        return Term(self, lambda a: float(f(np.broadcast_to(a, (self.R,)))), [t.i], False)

    def aggregate_mean(self, t): return self._agg(t, np.mean)

    def aggregate_quantile(self, t, ratio, maximize):
        k = int(np.floor(ratio * self.R + 1e-9))
        return self._agg(t, (lambda a: np.sort(a)[::-1][k]) if maximize else (lambda a: np.sort(a)[k]))

    def aggregate_relative_frequency_geq(self, t, thr):
        assert _is_term(thr) and not thr.stoch
        return Term(self, lambda a, b: float(np.mean(np.broadcast_to(a, (self.R,)) >= b - SLACK)), [t.i, thr.i], False)

    def enforce_geq(self, l, r, name=""):
        assert _is_term(l) and not l.stoch, "You cannot define constraints over stochastic terms"
        self._cons.append((l, float(r), name))

    # --- optimization: simple local search with a penalty
    def _score(self):
        self._recompute()
        viol = sum(max(0.0, r - self._vals[l.i]) for l, r, _ in self._cons)
        return self._vals[self._obj.i] + 1e7 * viol, viol

    def minimize(self, obj, seconds, bound=-1e20):
        assert _is_term(obj) and not obj.stoch, "Optimizing a term that fluctuates over different futures"
        self._obj = obj
        rnd = random.Random(0)
        best, viol = self._score()
        x = list(self._x)
        t0 = time.time()
        while time.time() - t0 < seconds:
            y = list(x)
            for _ in range(rnd.randint(1, 3)):
                i = rnd.randrange(len(y)); _, l, h = self._vars[i]
                step = max(1, int((h - l) * rnd.choice([0.01, 0.05, 0.2])))
                y[i] = min(h, max(l, y[i] + rnd.choice([-step, step])))
            self._x[:] = y
            f, v = self._score()
            if f <= best:
                x, best, viol = y, f, v
        self._x[:] = x
        self._recompute()
        self.status = "StatusType.timeout" if viol == 0 else "StatusType.infeasibleSolution"
        return self._vals[obj.i]


def _variable_priority(self, var, prio):
    assert any(var is v for v, _, _ in self._vars), "Must provide a variable to set variable priority!"
    assert _is_term(prio) and not prio.stoch, "priority must be a deterministic Term"
    self.priorities = getattr(self, "priorities", []) + [(var, prio)]


Env.variable_priority = _variable_priority


fake = types.ModuleType("seeker")
fake.Env, fake.UserDistribution, fake.UserEvalVector, fake.Term = Env, UserDistribution, UserEvalVector, Term
sys.modules["seeker"] = fake                        # the engine's `import seeker` gets the double, never real Seeker

import meio_seeker_stochastic as ms  # noqa: E402
import meio_stress_tests as st  # noqa: E402
import reference_simulator as ref  # noqa: E402

DATA = str(ROOT / "MEIO_Data_Template.xlsx")
D0 = ref.load_data(DATA)
ref.make_scenarios(D0, 10, 1)            # sorts open orders by placement (the engine's loader does the same)
STRESS = {c[0]: c for c in st.CASES}
CONTRACT_VERSION = 4


# ---------------------------------------------------------------------------- shared helpers
def build(d, x, R=60, seed=3, detail=False):
    env = fake.Env("lic", True)
    env.set_stochastic_parameters(R, 0.0)
    lay = ms.PolicyLayout(d)
    model = ms.build_model(fake, env, d, lay, x, seed, R, detail=detail)
    env.evaluate()
    return env, lay, model


def seeker_scenarios(model, d):
    """The scenarios Seeker evaluated (read per scenario — fine in the test double)."""
    n, H, C = model.env.R, d.horizon, len(d.channels)
    vals = lambda a: np.full(n, a) if isinstance(a, (int, np.integer)) else np.asarray(a.get_values())
    dem = np.zeros((n, C, H + 1))
    for (ci, t), term in model.demand.items():
        dem[:, ci, t] = term.get_values()
    lt_prod = np.full((n, H + 1), d.prod_lead_time.min)
    for t, L in model.lead_prod.items():
        lt_prod[:, t] = vals(L)
    lt_rm = {}
    for k in d.rms:
        lt_rm[k.id] = np.full((n, H + 1), k.lead_time.min)
        for t, L in model.lead_rm[k.id].items():
            lt_rm[k.id][:, t] = vals(L)
    op = np.stack([vals(a) for a in model.open_prod_arrival], 1) if model.open_prod_arrival else np.zeros((n, 0))
    orm = {k.id: np.stack([vals(a) for a in model.open_rm_arrival[k.id]], 1) if model.open_rm_arrival[k.id]
           else np.zeros((n, 0)) for k in d.rms}
    return ref.Scenarios(n, dem, lt_prod.astype(int), {k: v.astype(int) for k, v in lt_rm.items()},
                         op.astype(int), {k: v.astype(int) for k, v in orm.items()})


def v(term, n):
    return np.full(n, float(term)) if not hasattr(term, "get_values") else np.asarray(term.get_values())


def check_equal(d, x, R=60, seed=3):
    """Every quantity the model exposes equals the reference simulator in every scenario, including all
    report series of the out-of-sample model (detail=True, L05–L07) and the conservation identities."""
    env, lay, model = build(d, x, R, seed, detail=True)
    res = ref.MEIOSimulator(d, seeker_scenarios(model, d)).run(lay.decode(x), detail=True)
    rec = res.detail
    np.testing.assert_allclose(model.cost.get_values(), res.cost, rtol=1e-10, atol=1e-6)
    for n, term in model.cost_parts.items():
        np.testing.assert_allclose(v(term, R), res.cost_parts[n], rtol=1e-10, atol=1e-6)
    for (ci, t), term in model.fill_rate.items():
        np.testing.assert_allclose(term.get_values(), res.fill_rate[:, ci, t], atol=1e-12)
    for t, term in model.prod_qty.items():
        np.testing.assert_allclose(v(term, R), rec["prod_qty"][:, t], atol=1e-9)
        placed = rec["prod_qty"][:, t] > 0
        np.testing.assert_allclose(v(model.prod_arrival[t], R)[placed], rec["prod_arrival"][placed, t])
    for (k, t), term in model.rm_order.items():
        np.testing.assert_allclose(v(term, R), rec[f"{k}_order"][:, t], atol=1e-9)
        placed = rec[f"{k}_order"][:, t] > 0
        np.testing.assert_allclose(v(model.rm_arrival[(k, t)], R)[placed], rec[f"{k}_arrival"][placed, t])
    for t in range(1, d.horizon + 1):
        np.testing.assert_allclose(v(model.fg_end[t], R), rec["fg_end"][:, t], atol=1e-9)
        np.testing.assert_allclose(v(model.fg_waste[t], R), rec["fg_waste"][:, t], atol=1e-9)
        np.testing.assert_allclose(v(model.holding[t], R), rec["holding"][:, t], rtol=1e-12, atol=1e-9)
        for k in d.rms:
            np.testing.assert_allclose(v(model.rm_end[(k.id, t)], R), rec[f"{k.id}_end"][:, t], atol=1e-9)
            if k.perishable:
                np.testing.assert_allclose(v(model.rm_waste[(k.id, t)], R), rec[f"{k.id}_waste"][:, t], atol=1e-9)
    assert model.objective.get_value() == pytest.approx(res.cost.mean(), rel=1e-12)
    check_detail_equal(d, model, rec, R)
    check_conservation(d, model, R)
    return env, lay, model, res


def check_detail_equal(d, model, rec, R):
    """The report series (L05–L07) equal the reference simulator in every scenario."""
    det, fg, H = model.detail, d.fg_id, d.horizon
    close = lambda a, b: np.testing.assert_allclose(a, b, rtol=1e-12, atol=1e-9)
    t_first = 1 + d.prod_lead_time.min
    for t in range(1, H + 1):
        close(v(det[("position", fg, t)], R), rec["ip_fg"][:, t])
        close(v(det[("receipts", fg, t)], R), rec["fg_received"][:, t])
        close(v(det[("rsl_num", fg, t)], R), rec["rsl_num"][:, t])
        end = rec["fg_end"][:, t]
        close(v(det[("rsl_mean", fg, t)], R), np.where(end > 0.5, rec["rsl_num"][:, t] / np.maximum(end, 1.0), 0.0))
        for k in d.rms:
            close(v(det[("position", k.id, t)], R), rec[f"{k.id}_ip"][:, t])
            close(v(det[("receipts", k.id, t)], R), rec[f"{k.id}_received"][:, t])
        for ci, ch in enumerate(d.channels):
            close(v(det[("served", ch.id, t)], R), rec["served"][:, ci, t])
            close(v(det[("lost", ch.id, t)], R), rec["lost"][:, ci, t])
            close(v(det[("eligible_next_week", ch.id, t)], R), rec["eligible_next_week"][:, ci, t])
    close(v(det[("kpi", "avg_end_stock", fg)], R), rec["fg_end"][:, 1:].mean(1))
    close(v(det[("kpi", "waste", fg)], R), rec["fg_waste"][:, 1:].sum(1))
    for k in d.rms:
        close(v(det[("kpi", "avg_end_stock", k.id)], R), rec[f"{k.id}_end"][:, 1:].mean(1))
        if k.perishable:
            close(v(det[("kpi", "waste", k.id)], R), rec[f"{k.id}_waste"][:, 1:].sum(1))
    for ci, ch in enumerate(d.channels):
        close(v(det[("kpi", "lost_initial_weeks", ch.id)], R), rec["lost"][:, ci, 1:t_first].sum(1))
        close(v(det[("kpi", "lost_controllable_weeks", ch.id)], R), rec["lost"][:, ci, t_first:].sum(1))


def check_conservation(d, model, R):
    """Deterministic gate, per scenario and week: served + lost = demand; stock balances of the DC and the RMW
    (end stock includes the leftover of a lot's last usable week, which is also that week's waste, F06)."""
    det, fg, H = model.detail, d.fg_id, d.horizon
    tol = dict(rtol=1e-12, atol=1e-6)
    prev_fg = np.full(R, sum(q for _, q in d.init_fg))
    prev_rm = {k.id: np.full(R, sum(q for _, q in d.init_rm[k.id])) for k in d.rms}
    waste_prev = {k.id: np.zeros(R) for k in d.rms}
    w_fg_prev = np.zeros(R)
    for t in range(1, H + 1):
        sold = np.zeros(R)
        for ci, ch in enumerate(d.channels):
            served, lost = v(det[("served", ch.id, t)], R), v(det[("lost", ch.id, t)], R)
            np.testing.assert_allclose(served + lost, v(model.demand[(ci, t)], R), **tol)
            assert (served >= -1e-9).all() and (lost >= -1e-9).all()
            sold += served
        end_fg = v(model.fg_end[t], R)
        np.testing.assert_allclose(end_fg, prev_fg - w_fg_prev + v(det[("receipts", fg, t)], R) - sold, **tol)
        prod = v(model.prod_qty.get(t, 0.0), R)
        for k in d.rms:
            end_k = v(model.rm_end[(k.id, t)], R)
            np.testing.assert_allclose(end_k, prev_rm[k.id] - waste_prev[k.id] + v(det[("receipts", k.id, t)], R)
                                       - k.bom * prod, **tol)
            prev_rm[k.id] = end_k
            waste_prev[k.id] = v(model.rm_waste[(k.id, t)], R) if k.perishable else np.zeros(R)
        prev_fg, w_fg_prev = end_fg, v(model.fg_waste[t], R)


def policies(d):
    lay = ms.PolicyLayout(d)
    rng = np.random.default_rng(5)
    return {
        "start": lay.encode(ms.start_policy(d)),
        "push": [lay.upper(i) for i in range(len(lay))],
        "random_small": [int(rng.integers(0, lay.upper(i) // 3 + 2)) for i in range(len(lay))],
        "random_wide": [int(rng.integers(0, lay.upper(i) + 1)) for i in range(len(lay))],
        "zero": [0] * len(lay),
    }


def stressed(cid):
    """Template data with stress case `cid` applied (meio_stress_tests.py), validated by the engine."""
    d = ms.load_data(DATA)
    STRESS[cid][3](ms, d)
    ms.validate(d)
    return d


# ============================================================================
# 1. INPUT CONTROL
# ============================================================================
def test_data_loading_equals_reference():
    d = ms.load_data(DATA)
    for f in ("horizon", "review_period", "fill_rate_target", "n_scenarios", "seed", "time_limit", "fg_shelf_life",
              "prod_moq", "prod_batch", "prod_max", "prod_fixed", "fg_holding", "fg_waste", "init_fg", "init_rm",
              "open_prod", "open_rm", "enforce_from_first_controllable"):
        assert getattr(d, f) == getattr(D0, f), f
    np.testing.assert_array_equal(d.demand_q, D0.demand_q)
    assert [(c.id, c.priority, c.target) for c in d.channels] == [(c.id, c.priority, c.target) for c in D0.channels]
    assert [c.id for c in d.channels] == ["D2C", "B2B", "AMZ"] and d.fill_rate_target == 0.95     # H02, H04
    for a, b in zip(d.rms, D0.rms):
        assert (a.id, a.bom, a.shelf_life, a.min_remaining, a.moq, a.multiple, a.max_order, a.var_cost) == \
               (b.id, b.bom, b.shelf_life, b.min_remaining, b.moq, b.multiple, b.max_order, b.var_cost)

INVALID = {   # data change → message the engine must give (validate collects every problem)
    "review period > horizon": (lambda d: setattr(d, "review_period", d.horizon + 1), "ReviewPeriod"),
    "missing demand quantiles": (lambda d: d.demand_q.__setitem__((0, 5, 3), np.nan), "Demand quantiles missing"),
    "quantiles decreasing": (lambda d: d.demand_q.__setitem__((1, 5, 9), 0.0), "non-decreasing"),
    "negative demand": (lambda d: d.demand_q.__setitem__((2, 7, 0), -1.0), "non-decreasing"),
    "quantile levels not increasing": (lambda d: d.levels.__setitem__(0, 0.5), "DemandQuantileLevels"),
    "lead-time probabilities ≠ 1": (lambda d: setattr(d, "prod_lead_time", ms.LeadTime(np.array([6]), np.array([0.5]))), "Lead time of production"),
    "production MOQ not a batch multiple": (lambda d: setattr(d, "prod_moq", 6050.0), "Production: MOQ"),
    "production max < MOQ": (lambda d: setattr(d, "prod_max", 5000.0), "Production: MOQ"),
    "RM MOQ not a multiple": (lambda d: setattr(d.rms[2], "moq", 205.0), "MOQ must be a multiple of OrderMultiple"),
    "RM shelf life ≤ freshness rule": (lambda d: setattr(d.rms[2], "shelf_life", 36), "shelf life must exceed"),
    "initial FG fresher than possible": (lambda d: d.init_fg.append((40, 100.0)), "Initial FG lot"),
    "fill-rate target > 1": (lambda d: setattr(d, "fill_rate_target_excel", 1.2), "Items!TargetFillRate must be in (0, 1]"),
    "channel needs more shelf life than FG has": (lambda d: setattr(d.channels[1], "min_remaining", 40), "shelf-life need"),
    "shortage priority repeated": (lambda d: setattr(d.channels[1], "priority", 1), "ShortagePriority must rank"),
    "Excel Checks sheet not READY": (lambda d: setattr(d, "data_status", "ERRORS"), "OVERALL"),
}


def test_template_passes_validation():
    ms.validate(ms.load_data(DATA))


@pytest.mark.parametrize("case", list(INVALID))
def test_invalid_input_is_rejected_with_a_clear_message(case):
    change, message = INVALID[case]
    d = ms.load_data(DATA)
    change(d)
    with pytest.raises(ValueError, match="Data validation failed") as e:
        ms.validate(d)
    assert message in str(e.value), str(e.value)


def test_every_problem_is_listed_at_once():
    d = ms.load_data(DATA)
    for case in ("review period > horizon", "production max < MOQ", "fill-rate target > 1"):
        INVALID[case][0](d)
    with pytest.raises(ValueError) as e:
        ms.validate(d)
    assert all(m in str(e.value) for m in ("ReviewPeriod", "Production: MOQ", "TargetFillRate must be in (0, 1]"))


def _row(ws, col, val):
    return next(r for r in range(5, ws.max_row + 1) if ws.cell(r, col).value == val)


def _set(sheet, key, col, value, keycol=1):
    def edit(wb):
        ws = wb[sheet]
        ws.cell(_row(ws, keycol, key), col).value = value
    return edit


def _demand(ch, week, col, value):
    def edit(wb):
        ws = wb["Demand"]
        r = next(r for r in range(5, ws.max_row + 1) if ws.cell(r, 1).value == ch and ws.cell(r, 2).value == week)
        if col is None:
            for c in range(1, 14):
                ws.cell(r, c).value = None
        else:
            ws.cell(r, col).value = value
    return edit


def _copy_row(sheet, key, last_col, new_key=None):
    def edit(wb):
        ws = wb[sheet]
        src = _row(ws, 1, key)
        last = max(r for r in range(5, ws.max_row + 1) if ws.cell(r, 1).value is not None)
        for c in range(1, last_col + 1):
            val = ws.cell(src, c).value
            ws.cell(last + 1, c).value = None if isinstance(val, str) and val.startswith("=") else val
        if new_key:
            ws.cell(last + 1, 1).value = new_key
    return edit


def _tier(table, no, col, value):
    def edit(wb):
        ws = wb["CostTiers"]
        r = next(r for r in range(5, ws.max_row + 1) if ws.cell(r, 1).value == table and ws.cell(r, 2).value == no)
        ws.cell(r, col).value = value
    return edit


S = lambda name, value: _set("Settings", name, 2, value)
EXCEL_CHECKS = {   # Excel check → edit of an openpyxl copy → message of the engine. Cross-checked once against
    # Excel's own verdict (LibreOffice recalculation of every copy): all rejected by both (last check 12.11.).
    "C01 parameter blank": (S("RandomSeed", None), "parameter RandomSeed has no value"),
    "X03 horizon 60": (S("HorizonPeriods", 60), "HorizonPeriods must be a whole number between 1 and 52"),
    "X06 review period 40": (S("ReviewPeriod", 40), "ReviewPeriod must be a whole number"),
    "C02 duplicate location": (_copy_row("Locations", "DC", 7), "duplicate LocationID DC"),
    "C02 location type blank": (_set("Locations", "RMW", 3, None), "Name and Type must be filled"),
    "C03 duplicate item": (_copy_row("Items", "RM2", 7), "duplicate ItemID RM2"),
    "C03 FG target 1.2": (_set("Items", "FG1", 6, 1.2), "TargetFillRate of FG1 must be in (0, 1]"),
    "C03 FG target blank": (_set("Items", "FG1", 6, None), "TargetFillRate of FG1 must be in (0, 1]"),
    "C03 RM with a target": (_set("Items", "RM3", 6, 0.9), "TargetFillRate is only for finished goods"),
    "C03 shelf life 0": (_set("Items", "RM3", 5, 0), "ShelfLife of RM3 must be a whole number > 0"),
    "C04 MOQ not a multiple": (_set("Sourcing", "RM1", 4, 725), "MOQ must be a multiple of OrderMultiple"),
    "C04 max below MOQ": (_set("Sourcing", "RM4", 6, 500), "MaxOrderQty must be ≥ MOQ"),
    "C04 unknown lead time": (_set("Sourcing", "RM2", 9, "LT_NOPE"), "lead-time distribution LT_NOPE unknown"),
    "C04 supplier not a supplier": (_set("Sourcing", "RM3", 2, "RMW"), "supplier RMW is not a Supplier location"),
    "X01 RM sourced twice": (_copy_row("Sourcing", "RM2", 9), "RM2 needs exactly one Sourcing row"),
    "C05 production MOQ 6050": (_set("Production", "FG1", 4, 6050), "Production row 5: MOQ must be a multiple of BatchSize"),
    "C05 production max below MOQ": (_set("Production", "FG1", 6, 5000), "MaxQtyPerOrder must be ≥ MOQ"),
    "C05 no production orders": (_set("Production", "FG1", 7, 0), "MaxOrdersPerPeriod must be ≥ 1"),
    "C05 capacity below demand": (lambda wb: (_set("Production", "FG1", 4, 1000)(wb), _set("Production", "FG1", 6, 1000)(wb)),
                                  "exceeds the production capacity"),
    "C06 freshness ≥ shelf life": (_set("BOM", "RM4", 4, 52, keycol=2), "freshness rule of RM4 must be"),
    "C06 unknown component": (_set("BOM", "RM2", 2, "RM9", keycol=2), "component RM9 unknown"),
    "C06 strawberry need > supply": (_set("BOM", "RM3", 3, 0.8, keycol=2), "exceeds its maximum weekly supply"),
    "C07 lane end point unknown": (_set("Lanes", "L_DC_AMZ", 3, "XYZ"), "unknown end point"),
    "C07 lane tier table unknown": (_set("Lanes", "L_KAJA_DC", 6, "CT_NOPE"), "unknown tier table CT_NOPE"),
    "C08 lead time 6.6": (_set("LeadTimeDist", "LT_PROD_KAJA", 2, 6.6), "must be whole weeks ≥ 0"),
    "C08 probability 1.2": (_set("LeadTimeDist", "LT_RM1", 3, 1.2), "probability of LT_RM1 must be in (0, 1]"),
    "C09 priority used twice": (_set("Channels", "AMZ", 6, 1), "ShortagePriority 1 is used twice"),
    "C09 priority 4 of 3": (_set("Channels", "AMZ", 6, 4), "ShortagePriority must be a whole number from 1 to 3"),
    "C09 priority blank": (_set("Channels", "B2B", 6, None), "all fields of B2B must be filled"),
    "C09 shelf need 40": (_set("Channels", "B2B", 5, 40), "shelf-life need must be whole weeks"),
    "C10 demand row twice": (_copy_row("Demand", "D2C", 13), "D2C week 1 appears twice"),
    "X05 demand row missing": (_demand("B2B", 20, None, None), "channel B2B has no row for week(s) 20"),
    "C10 quantiles decreasing": (_demand("AMZ", 5, 13, 1), "quantiles must be ≥ 0 and non-decreasing"),
    "C10 negative quantile": (_demand("D2C", 8, 4, -3), "quantiles must be ≥ 0 and non-decreasing"),
    "C10 unknown channel": (_copy_row("Demand", "AMZ", 13, new_key="EBAY"), "unknown channel EBAY"),
    "C11 tier gap": (_tier("CT_FG1_PROD", 2, 3, 20000), "tier 2 must start where tier 1 ends"),
    "C11 first tier not at 0": (_tier("CT_FG1_PROD", 1, 3, 5), "the first tier must start at 0"),
    "C11 To ≤ From": (_tier("CT_FG1_TRANSPORT", 1, 4, 0), "ToQty must be greater than FromQty"),
    "C12 holding cost blank": (_set("Costs", "RM3", 3, None), "HoldingCost of RM3 must be filled"),
    "C12 negative cost": (_set("Costs", "RM1", 6, -0.1), "OrderVariableCost must be ≥ 0"),
    "C12 FG tier table blank": (_set("Costs", "FG1", 8, None), "ProductionVarCostTierTableID of FG1 must be filled"),
    "C12 FG waste cost blank": (_set("Costs", "FG1", 4, None), "WasteCost of FG1 must be filled"),
    "C13 negative stock": (_set("Init_Inventory", "FG1", 3, -500, keycol=2), "Quantity must be ≥ 0"),
    "C13 shelf life 40 > 36": (_set("Init_Inventory", "FG1", 4, 40, keycol=2), "RemainingShelfLife must be whole weeks"),
    "C14 open order quantity 0": (_set("Open_Orders", "PO-RM1-01", 6, 0), "Quantity must be > 0"),
    "C14 placed in week 1": (_set("Open_Orders", "PO-RM3-02", 7, 1), "PlacedInPeriod must be a whole week ≤ 0"),
    "C14 arrival week 0": (_set("Open_Orders", "PRD-FG1-01", 8, 0), "ExpectedArrivalPeriod must be a whole week ≥ 1"),
}
VALID_EDITS = {
    "price change": _set("Costs", "RM3", 6, 3.5),
    "stochastic production lead time": _set("Production", "FG1", 8, "LT_PROD_KAJA_STOCH"),
    "400 scenarios, seed 7": lambda wb: (S("NumScenarios", 400)(wb), S("RandomSeed", 7)(wb)),
    "product target 97%": _set("Items", "FG1", 6, 0.97),
    "Amazon served first": lambda wb: (_set("Channels", "AMZ", 6, 1)(wb), _set("Channels", "D2C", 6, 3)(wb)),
    "review period 4 weeks": S("ReviewPeriod", 4),
}


def edited_copy(tmp_path, edit):
    import openpyxl
    wb = openpyxl.load_workbook(DATA)
    edit(wb)
    path = tmp_path / "edited.xlsx"
    wb.save(path)                                    # no cached formula results, as an app would write it
    return str(path)


def test_openpyxl_copy_passes_and_an_explicit_excel_error_still_fails(tmp_path):
    d = ms.load_data(edited_copy(tmp_path, lambda wb: None))
    assert d.data_status == "unknown" and not ms.input_problems(pd.ExcelFile(edited_copy(tmp_path, lambda wb: None)))[0]
    ms.validate(d)
    assert len(d.warnings) == 2 and all("MaxOrderQty" in w for w in d.warnings)   # same 2 warnings as Excel
    for status in ("ERROR", "INCOMPLETE"):
        d.data_status = status
        with pytest.raises(ValueError, match=f"OVERALL = {status}"):
            ms.validate(d)


def test_workbook_is_closed_after_reading_also_when_rejected(tmp_path, monkeypatch):
    """The app writes, replaces and deletes edited copies; on Windows that fails while the file is still open."""
    opened = []
    real = pd.ExcelFile

    class Tracked(real):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            opened.append(self)
            self.closed_flag = False

        def close(self):
            self.closed_flag = True
            super().close()
    monkeypatch.setattr(ms.pd, "ExcelFile", Tracked)
    ms.load_data(edited_copy(tmp_path, lambda wb: None))
    with pytest.raises(ValueError):
        ms.load_data(edited_copy(tmp_path, EXCEL_CHECKS["C05 production MOQ 6050"][0]))
    assert len(opened) == 2 and all(x.closed_flag for x in opened)


@pytest.mark.parametrize("case", list(EXCEL_CHECKS))
def test_every_excel_check_runs_in_python(case, tmp_path):
    edit, message = EXCEL_CHECKS[case]
    with pytest.raises(ValueError, match="Data validation failed") as e:
        ms.validate(ms.load_data(edited_copy(tmp_path, edit)))
    assert message in str(e.value), str(e.value)


@pytest.mark.parametrize("case", list(VALID_EDITS))
def test_valid_edits_pass(case, tmp_path):
    d = ms.load_data(edited_copy(tmp_path, VALID_EDITS[case]))
    ms.validate(d)


def test_errors_are_exceptions_not_exits(tmp_path, monkeypatch):
    lic = tmp_path / "license.sio"
    lic.write_text("x")
    with pytest.raises(FileNotFoundError, match="Data file"):
        ms.run(str(tmp_path / "missing.xlsx"), str(lic), skr=fake)
    with pytest.raises(FileNotFoundError, match="License file"):
        ms.run(str(DATA), str(tmp_path / "missing.sio"), skr=fake)
    import openpyxl
    wb = openpyxl.load_workbook(DATA)
    ws = wb["Settings"]
    ws = wb["Items"]
    for row in ws.iter_rows():
        if row[0].value == "FG1":
            row[5].value = 1.5                     # fill-rate target outside (0, 1]
    bad = tmp_path / "bad.xlsx"
    wb.save(bad)
    with pytest.raises(ValueError, match="TargetFillRate"):
        ms.run(str(bad), str(lic), skr=fake)
    monkeypatch.setitem(sys.modules, "seeker", None)
    with pytest.raises(ImportError, match="pip install ioseeker"):
        ms.import_seeker()

# ============================================================================
# 2. PHYSICS & DYNAMICS — engine = reference simulator in every scenario
# ============================================================================
@pytest.mark.parametrize("name", ["start", "push", "random_small", "random_wide", "zero"])
def test_model_equals_reference_fixed_lead_times(name):
    d = copy.deepcopy(D0)
    check_equal(d, policies(d)[name])


@pytest.mark.parametrize("name", ["start", "random_small", "random_wide"])
def test_model_equals_reference_stochastic_lead_times_and_uncertain_open_orders(name):
    d = copy.deepcopy(D0)
    d.prod_lead_time = ref.LeadTime(np.array([6, 8]), np.array([0.6, 0.4]))
    for k in d.rms:
        k.lead_time = ref.LeadTime(np.array([k.lead_time.min, k.lead_time.min + 4]), np.array([0.7, 0.3]))
    for o in d.open_rm["RM3"]:
        o["certain"] = False
    d.open_prod[1]["remaining"] = 30
    d.open_rm["RM3"][0]["remaining"] = 45                         # open orders arriving partly aged (J02)
    _, _, model, _ = check_equal(d, policies(d)[name], R=80)
    assert any(not isinstance(a, int) for a in model.open_rm_arrival["RM3"])


def test_receipts_include_partly_aged_open_orders_in_the_same_week():
    """Two open production orders arrive in the same week, one already partly aged (J02): both are receipts."""
    d = copy.deepcopy(D0)
    d.open_prod[0]["arrival"] = d.open_prod[1]["arrival"]
    d.open_prod[1]["remaining"] = 30
    _, _, model, res = check_equal(d, policies(d)["random_small"])
    t = d.open_prod[1]["arrival"]
    assert np.allclose(res.detail["fg_received"][:, t], d.open_prod[0]["qty"] + d.open_prod[1]["qty"])


def test_general_arrival_path_with_fixed_lead_time():
    d = copy.deepcopy(D0)
    d.open_prod[1]["arrival"] = 9
    check_equal(d, policies(d)["random_small"])


def test_incremental_tiers():
    d = copy.deepcopy(D0)
    d.prod_tiers.mode = "Incremental"
    d.transport_tiers.mode = "Incremental"
    check_equal(d, policies(d)["random_small"])


def test_edge_rm_lot_exactly_at_freshness_limit_is_usable():
    d = copy.deepcopy(D0)
    d.init_rm["RM3"] = [(36, 4000.0)]
    d.open_rm["RM3"] = []
    for k in d.rms:
        if k.id != "RM3":
            d.init_rm[k.id] = [(50 if k.perishable else None, 1e6)]
    d.init_fg, d.open_prod = [], []
    lay = ms.PolicyLayout(d)
    _, _, _, res = check_equal(d, [lay.upper(i) for i in range(len(lay))])
    assert np.allclose(res.detail["prod_qty"][:, 1], 8000)


def test_edge_zero_demand_weeks_have_fill_rate_one():
    d = copy.deepcopy(D0)
    d.demand_q[:, 10:13, :] = 0.0
    _, _, model, _ = check_equal(d, policies(d)["zero"])
    for ci in range(len(d.channels)):
        for t in (10, 11, 12):
            assert np.all(np.asarray(model.fill_rate[(ci, t)].get_values()) == 1.0)


def test_edge_stock_in_its_last_usable_week_and_stock_unusable_on_entry():
    """F06: a lot in its last usable week counts as stock that week and its leftover is that week's waste.
    J04 (known limitation, not handled): a lot that is already unusable when it enters is never sold and never
    in a stock position, but appears for one week in end stock and holding before it is scrapped."""
    d = copy.deepcopy(D0)
    d.init_fg, d.open_prod = [(16, 300.0), (10, 500.0)], []          # 16 = last usable week; 10 = already unusable
    d.demand_q[:, 1, :] = 0.0                                        # no demand in week 1
    _, _, model, res = check_equal(d, policies(d)["zero"])
    det = model.detail
    R = model.env.R
    assert np.allclose(v(det[("position", d.fg_id, 1)], R), 300)     # only the usable lot is in the position
    assert np.allclose(v(model.fg_end[1], R), 800)                   # both in week-1 end stock (J04: the 500)
    assert np.allclose(v(model.fg_waste[1], R), 800)                 # both scrapped at the end of week 1
    assert np.allclose(v(model.fg_end[2], R), v(det[("receipts", d.fg_id, 2)], R) - res.detail["served"][:, :, 2].sum(1))


@pytest.mark.parametrize("accept", [False, True])
def test_both_seeker_signatures_and_stratified_demand(accept, monkeypatch):
    monkeypatch.setattr(fake.Env, "ACCEPT_MAX_SAMPLE", accept)
    ms.SUPPORTS_MAX_SAMPLE.clear()
    d = copy.deepcopy(D0)
    d.prod_lead_time = ref.LeadTime(np.array([6, 8]), np.array([0.6, 0.4]))
    _, _, model, _ = check_equal(d, policies(d)["start"], R=100)
    assert ms.SUPPORTS_MAX_SAMPLE["user_distribution"] is accept
    if accept:
        return
    ci = [c.id for c in D0.channels].index("AMZ")
    vals = np.asarray(model.demand[(ci, 9)].get_values())
    edges = ms.sample_from_quantiles(D0.demand_q[ci, 9], D0.levels, np.linspace(0.1, 0.9, 9))
    below = np.array([(vals <= e + 0.5).sum() for e in edges])
    assert np.all(np.abs(below - np.arange(10, 100, 10)) <= 2), below

# ---------------------------------------------------------------------------- review period (A03)
V18_START = {"template z1.5": [104634.978775, 0.993502104, 86], "template z2.5": [110845.472425, 0.997880384, 89],
             "L2 z1.5": [104162.212625, 0.993085005, 81], "L2 z2.5": [110794.0597, 0.997919183, 83],
             "S1 z1.5": [105747.132225, 0.985541841, 83], "S1 z2.5": [112315.037825, 0.988679752, 86]}


def test_review_period_1_is_exactly_the_v18_model():
    """R = 1: every week is a run week, so cost, mean fill rate and met cells equal the v18.0 engine (recorded 12.11.)."""
    for name, cid in (("template", None), ("L2", "L2"), ("S1", "S1")):
        d = stressed(cid) if cid else copy.deepcopy(D0)
        assert d.review_period == 1
        d.n_scenarios = 200
        lay = ms.PolicyLayout(d)
        for z in (1.5, 2.5):
            fr, cost = ms.evaluate_policy(fake, d, lay, ms.start_policy(d, z=z), "lic", "x")
            got = [round(cost, 6), round(float(np.mean(list(fr.values()))), 9), len(ms.rules_met(d, fr))]
            assert got == V18_START[f"{name} z{z}"], (name, z, got)


@pytest.mark.parametrize("R", [2, 4])
def test_model_equals_reference_with_a_review_period(R):
    """The planned position between runs (A03) is written a second time in the reference: same in every scenario,
    with fixed and with uncertain lead times; the zero policy checks that s = 0 never orders (planned position ≥ 0)."""
    for name in ("start", "random_wide", "zero"):
        d = copy.deepcopy(D0)
        d.review_period = R
        check_equal(d, policies(d)[name])
    d = stochastic_data()
    d.review_period = R
    for name in ("start", "random_small"):
        check_equal(copy.deepcopy(d), policies(d)[name], R=80)


def _reference_run(d, x, scen):
    return ref.MEIOSimulator(d, scen).run(ms.PolicyLayout(d).decode(x), detail=True)


def test_committed_weeks_are_the_same_in_every_future_and_cycles_ignore_later_demand():
    """Weeks 1..R are decided at this run: identical in every scenario. Inside a later cycle the orders depend only on
    what was known at its run week; changing the demand of the run week itself changes nothing until the next run."""
    d = copy.deepcopy(D0)
    d.review_period = 4
    x = policies(d)["random_wide"]
    scen = ref.make_scenarios(d, 120, 11)
    res = _reference_run(d, x, scen)
    keys = ["prod_qty"] + [f"{k.id}_order" for k in d.rms]
    for key in keys:
        assert np.all(res.detail[key][:, 1:5] == res.detail[key][:1, 1:5]), key
    assert any(res.detail[key][:, 1:5].sum() > 0 for key in keys)
    changed = copy.deepcopy(scen)
    changed.demand[:, :, 5] = changed.demand[:, :, 5] * 2 + 300                 # run week 5: demand after the review
    res2 = _reference_run(d, x, changed)
    for key in keys:
        np.testing.assert_array_equal(res.detail[key][:, 1:9], res2.detail[key][:, 1:9])
    assert any(not np.array_equal(res.detail[key][:, 9:], res2.detail[key][:, 9:]) for key in keys)
    d.review_period = 1                                                          # control: weekly runs do react
    a, b = _reference_run(d, x, scen), _reference_run(d, x, changed)
    assert any(not np.array_equal(a.detail[key][:, 6:9], b.detail[key][:, 6:9]) for key in keys)


def test_start_policy_safety_stock_counts_the_uncertainty_since_the_last_run():
    d1, d4 = copy.deepcopy(D0), copy.deepcopy(D0)
    d4.review_period = 4
    p1, p4 = ms.start_policy(d1, z=2.0), ms.start_policy(d4, z=2.0)
    for t in (1, 5, 9):                                                          # run weeks: unchanged
        assert p4["FG"][0][t] == pytest.approx(p1["FG"][0][t])
    for t in (2, 3, 4, 6):                                                       # planned weeks: more safety stock
        assert p4["FG"][0][t] > p1["FG"][0][t]
    assert p4["FG"][0][4] - p1["FG"][0][4] > p4["FG"][0][2] - p1["FG"][0][2]      # the older the information, the more
    assert ms.run_week(d4, 4) == 1 and ms.run_week(d4, 5) == 5 and ms.run_week(d1, 7) == 7


def test_x10_warns_when_the_horizon_is_too_short_and_matches_excel():
    d = copy.deepcopy(D0)
    assert not [w for w in ms.data_warnings(d) if "(X10)" in w]                  # 36 ≥ 18 + 1 + 8
    d.review_period = 11                                                         # 36 < 18 + 11 + 8 = 37
    w = [w for w in ms.data_warnings(d) if "(X10)" in w]
    assert len(w) == 1 and "echelon lead time 18" in w[0]
    import openpyxl
    ws = openpyxl.load_workbook(DATA)["Checks"]
    f = [r[5].value for r in ws.iter_rows() if r[0].value == "X10"]
    assert len(f) == 1 and f"+{ms.HORIZON_MARGIN_WEEKS},1,0)" in f[0]            # same margin in Excel and Python


# ============================================================================
# 3. MODEL FUNCTIONALITY
# ============================================================================
def test_bounds_contain_start_policy_and_rm_gap_cap_is_lossless():
    d = copy.deepcopy(D0)
    lay = ms.PolicyLayout(d)
    pol = ms.start_policy(d)
    raw = lay.encode(pol, clip=False)
    lay.include_start(pol)
    x = lay.encode(pol)
    for i, (_j, _t, kind) in enumerate(lay.index):
        assert 0 <= x[i] <= lay.upper(i)
        if kind == "sigma":
            assert x[i] == raw[i]
    sim = ref.MEIOSimulator(d, ref.make_scenarios(d, 100, 9))
    a, b = sim.run(lay.decode(raw)), sim.run(lay.decode(x))
    np.testing.assert_array_equal(a.cost, b.cost)
    np.testing.assert_array_equal(a.fill_rate, b.fill_rate)
    assert any(raw[i] > x[i] for i, (j, _, k) in enumerate(lay.index) if k == "delta" and j != "FG")


def test_bounds_follow_forecast():
    d = copy.deepcopy(D0)
    lay = ms.PolicyLayout(d)
    q95 = d.demand_q[:, 1:, -1].sum(0)
    i = lay.index.index(("FG", 1, "sigma"))
    assert lay.upper(i) == int(np.ceil(q95[0:d.prod_lead_time.max + 1 + ms.EXTRA_COVER_WEEKS["FG"]].sum() / lay.mult["FG"]))
    # RM3 uses its own extra cover weeks (8): window 8 + 6 + 1 + 8 = 23 weeks, × BOM, in 50 kg steps
    k = [r for r in d.rms if r.id == "RM3"][0]
    i = lay.index.index(("RM3", 1, "sigma"))
    assert lay.upper(i) == int(np.ceil(k.bom * q95[0:23].sum() / 50))


def test_raising_extra_cover_weeks_raises_only_that_bound_and_note_says_so(monkeypatch):
    d = copy.deepcopy(D0)
    base = ms.PolicyLayout(d)
    monkeypatch.setattr(ms, "EXTRA_COVER_WEEKS", {**ms.EXTRA_COVER_WEEKS, "RM4": 8})
    more = ms.PolicyLayout(d)
    for i, (j, t, kind) in enumerate(base.index):
        if j == "RM4" and kind == "sigma" and t <= 10:
            assert more.upper(i) > base.upper(i)
        elif j != "RM4" or kind != "sigma":
            assert more.upper(i) == base.upper(i)
    x = [0] * len(more)
    i = more.index.index(("RM3", 12, "sigma"))
    x[i] = more.upper(i)
    assert ms.bound_note(more, x) == "RM3 week 12 → raise EXTRA_COVER_WEEKS['RM3'] in section 0"
    assert ms.bound_note(more, [0] * len(more)) == "none"
    assert more.mult["RM3"] == 50 and more.mult["FG"] == 100


def z_reference(d):
    """K07 target-aware z₀, built independently: G(z) = E[(X − z)⁺] by numerical integration, solved on a grid."""
    xs = np.linspace(-8, 12, 200001)
    pdf = np.exp(-0.5 * xs ** 2) / math.sqrt(2 * math.pi)
    G = lambda z: float(np.trapezoid(np.maximum(xs - z, 0) * pdf, xs))
    mean = d.demand_q[:, 1:, :].mean(2).sum(0)
    var = (((d.demand_q[:, 1:, -1] - d.demand_q[:, 1:, 0]) / 3.29) ** 2).sum(0)
    W = d.prod_lead_time.max + 1 + 2
    y = (1 - d.fill_rate_target) * mean.mean() / math.sqrt(W * var.mean())
    grid = np.arange(0.0, 4.0 + 1e-9, 0.001)
    return float(next((z for z in grid if G(z) <= y), 4.0))


def k07_reference(d, z=None):
    """K07, built independently: the v17.1 formula (reference heuristic) with the target-aware z + lot cap + lot guard."""
    pol = ref.heuristic_policy(d, z_reference(d) if z is None else z, 2, 4, 4)
    mean = d.demand_q[:, 1:, :].mean(2)
    sell = sum(mean[ci].mean() * max(0, d.fg_shelf_life - c.min_remaining + 1) for ci, c in enumerate(d.channels))
    lot = max(d.prod_moq, min(4 * mean.sum(0).mean(), sell))
    s_fg = pol["FG"][0]
    pol["FG"] = (s_fg, np.where(s_fg > 0, s_fg + lot, 0.0))
    for k in d.rms:
        s, _ = pol[k.id]
        s = np.where(s > 0, np.maximum(s, k.bom * (s_fg + d.prod_moq)), 0.0)
        pol[k.id] = (s, np.where(s > 0, s + max(k.moq, k.bom * lot), 0.0))
    return pol


def test_normal_loss_function_and_target_aware_z():
    """K07: G(z) and its inverse against known values; z₀ falls with the target; z = 4 at a 100% target."""
    assert ms.normal_loss(0.0) == pytest.approx(0.398942, abs=1e-6)
    assert ms.normal_loss(1.0) == pytest.approx(0.083315, abs=1e-6)
    assert ms.normal_loss(2.0) == pytest.approx(0.008491, abs=1e-6)
    for y in (0.3, 0.05, 0.001):
        assert ms.normal_loss(ms.normal_loss_inverse(y)) == pytest.approx(y, rel=1e-9)
    d = copy.deepcopy(D0)
    zs = []
    for beta in (1.0, 0.99, 0.98, 0.95, 0.90, 0.80):
        d.fill_rate_target = beta
        zs.append(ms.start_z(d))
        assert zs[-1] == pytest.approx(z_reference(d), abs=2e-3), beta
    assert zs[0] == ms.START_Z_MAX and all(a > b for a, b in zip(zs, zs[1:])), zs
    d.fill_rate_target = 0.95
    assert ms.start_z(d) == pytest.approx(1.216, abs=1e-3)                    # template value (12.11.)


def test_start_policy_formula():
    d = copy.deepcopy(D0)
    pol = ms.start_policy(d)
    ref_pol = k07_reference(d, ms.start_z(d))          # z itself is checked against z_reference in the test above
    for j in pol:
        np.testing.assert_allclose(pol[j][0], ref_pol[j][0])
        np.testing.assert_allclose(pol[j][1], ref_pol[j][1])
    # the lot guard raises only the last raw-material weeks on the template (cover window cut at the horizon); with z = 4
    pol = ms.start_policy(d, z=4.0)
    np.testing.assert_allclose(pol["FG"][0], k07_reference(d, 4.0)["FG"][0])
    old = ref.heuristic_policy(d, 4.0, 2, 4, 4)
    raised = [(k.id, t) for k in d.rms for t in range(1, d.horizon + 1) if pol[k.id][0][t] > old[k.id][0][t] + 1e-9]
    assert len(raised) == 4 and min(t for _, t in raised) >= 20, raised
    # the lot cap binds when a fresh lot can sell less than 4 weeks of demand (here: only D2C, 5 weeks)
    d2 = copy.deepcopy(D0)
    d2.fg_shelf_life, d2.prod_moq, d2.prod_batch = 20, 100.0, 100.0
    p2 = ms.start_policy(d2)
    d2c = [ci for ci, c in enumerate(d2.channels) if c.id == "D2C"][0]
    sell = d2.demand_q[d2c, 1:, :].mean(1).mean() * 5
    assert p2["FG"][1][1] - p2["FG"][0][1] == pytest.approx(sell) and ms.fresh_lot_sales(d2) == pytest.approx(sell)
    # hand calculation, FG week 1: window = max production LT 6 + 1 + 2 = 9 weeks (1..9)
    q = d.demand_q
    mu = q[:, 1:10, :].mean(2).sum(0)
    sd2 = (((q[:, 1:10, -1] - q[:, 1:10, 0]) / 3.29) ** 2).sum()
    assert pol["FG"][0][1] == pytest.approx(mu.sum() + 4.0 * np.sqrt(sd2))
    lot = max(d.prod_moq, 4 * q[:, 1:, :].mean(2).sum(0).mean())
    assert pol["FG"][1][1] - pol["FG"][0][1] == pytest.approx(lot)
    # RM3 week 1: window = 8 + 6 + 1 + 4 = 19 weeks, times BOM
    k = [r for r in d.rms if r.id == "RM3"][0]
    mu = q[:, 1:20, :].mean(2).sum(0)
    sd2 = (((q[:, 1:20, -1] - q[:, 1:20, 0]) / 3.29) ** 2).sum()
    assert pol["RM3"][0][1] == pytest.approx(k.bom * (mu.sum() + 4.0 * np.sqrt(sd2)))
    # inactive tail weeks are 0 (K04); settings are honoured
    assert pol["FG"][0][31] == 0 and pol["RM4"][0][19] == 0
    assert ms.start_policy(d, z=2.0)["FG"][0][1] < pol["FG"][0][1]



def test_fill_rate_rule_is_the_expected_fill_rate_per_cell():
    """H05: one constraint per constrained cell, judging the mean over the scenarios of served ÷ demand against the
    product's target; the cell example of the spec (94 × 100%, 3 × 80%, 3 × 0% → 96.4%) is met at 95%, not at 97%."""
    d = copy.deepcopy(D0)
    _, _, model, res = check_equal(d, policies(d)["random_small"], R=200)
    assert len(model.constrained) == 90 and len(model.env._cons) == 90
    assert {r for _, r, _ in model.env._cons} == {0.95}
    mean = ref.expected_fill_rate(res.fill_rate)
    for (ci, t), agg in model.fr_mean.items():
        assert agg.get_value() == pytest.approx(mean[ci, t])
    assert ms.constraints_met(model, d) == sum(mean[ci, t] >= 0.95 - 1e-9 for ci, t in model.constrained)
    d90 = copy.deepcopy(D0)
    d90.fill_rate_target = 0.90
    _, _, m90 = build(d90, policies(d90)["random_small"], R=20)
    assert {r for _, r, _ in m90.env._cons} == {0.90}                              # the rule uses the run's target
    example = (94 * 1.0 + 3 * 0.8 + 3 * 0.0) / 100
    d.fill_rate_target = 0.95
    assert example == pytest.approx(0.964) and ms.cell_met(d, example)
    d.fill_rate_target = 0.97
    assert not ms.cell_met(d, example)


def test_priorities_set_for_every_variable():
    d = copy.deepcopy(D0)
    env, lay, model = build(d, policies(d)["start"], R=20)
    assert len(env.priorities) == len(model.variables)
    for (j, _, _), (_var, prio) in zip(lay.index, env.priorities):
        assert prio.get_value() == ms.VARIABLE_PRIORITY.get(j, 1.0)


def wide_max_supply(d):
    lay = copy.deepcopy(ms.PolicyLayout(d))
    pol = ms.max_supply_policy(d, lay)
    lay.include_start(pol, margin=1.0)
    return lay, pol, lay.encode(pol)


def test_max_supply_policy_orders_the_maximum_and_equals_reference():
    """The huge reorder points fit the widened bounds and make every review order the maximum; the model
    with this policy still equals the independent reference scenario by scenario."""
    d = copy.deepcopy(D0)
    lay, pol, x = wide_max_supply(d)
    assert lay.decode(x)["FG"][0][1] >= pol["FG"][0][1] - 1e-9          # not clipped by the bounds
    env = fake.Env("lic", True)
    env.set_stochastic_parameters(60, 0.0)
    model = ms.build_model(fake, env, d, lay, x, 3, 60)
    env.evaluate()
    res = ref.MEIOSimulator(d, seeker_scenarios(model, d)).run(lay.decode(x), detail=True)
    np.testing.assert_allclose(model.cost.get_values(), res.cost, rtol=1e-10, atol=1e-6)
    for (ci, t), term in model.fill_rate.items():
        np.testing.assert_allclose(term.get_values(), res.fill_rate[:, ci, t], atol=1e-12)
    for k in d.rms:                                                     # every active week: the max order
        for t in range(1, lay.active_until[k.id] + 1):
            np.testing.assert_allclose(res.detail[f"{k.id}_order"][:, t], k.max_order)
    prod = res.detail["prod_qty"][:, 1:lay.active_until["FG"] + 1]
    assert prod[prod > 0].min() >= d.prod_moq and prod.max() <= d.prod_max
    # strawberries arrive at 1,000 kg = 2,000 cans per week, so the 6,000-can MOQ is reached every 3rd week;
    # in total max supply must produce at least as much as the start policy on the same scenarios
    start = ref.MEIOSimulator(d, seeker_scenarios(model, d)).run(ms.start_policy(d), detail=True)
    assert prod.sum(1).min() >= start.detail["prod_qty"][:, 1:lay.active_until["FG"] + 1].sum(1).max() - 1e-6


def note_for(cid, n=200):
    d = stressed(cid)
    d.n_scenarios = n
    layout = ms.PolicyLayout(d)
    pol0 = ms.start_policy(d)
    layout.include_start(pol0)
    max_share = ms.feasibility_check(fake, d, layout, "lic")
    env = fake.Env("lic", True)
    env.set_stochastic_parameters(n, 0.0)
    model = ms.build_model(fake, env, d, layout, layout.encode(pol0), d.seed, n)
    env.evaluate()
    return ms.feasibility_note(d, {k: model.fr_mean[k].get_value() for k in model.constrained}, max_share)


def test_feasibility_check_no_false_alarm_on_feasible_cases():
    for cid in ("BASE", "L2", "S1", "R1"):          # max supply meets every rule here (NumPy check, 2,000 futures)
        assert note_for(cid).startswith("OK"), (cid, note_for(cid))


def test_feasibility_check_finds_the_supply_gaps():
    """Under H05 (expected fill rate ≥ 95%) a gap counts only if it lowers the mean of a cell below the target:
    demand +30% (D1) is reachable on average (it failed the old chance rule in weeks 15–19); cancelled strawberry
    orders (O2) leave Amazon week 16 at 91.6%; strawberry capacity below demand (O5) fails B2B/AMZ weeks."""
    assert note_for("D1").startswith("OK")
    o2 = note_for("O2")
    assert "fail whatever we decide" in o2 and "AMZ w16" in o2 and "91.6%" in o2, o2
    o5 = note_for("O5")
    assert "fail whatever we decide" in o5 and "AMZ w" in o5 and "B2B w" in o5, o5


def test_feasibility_note_needs_both_policies_to_fail_and_groups_weeks():
    d = copy.deepcopy(D0)
    keys = [(ci, t) for ci in range(3) for t in range(7, 37)]
    ok = {k: 1.0 for k in keys}
    bad_max = {**ok, (0, 15): 0.5, (0, 16): 0.5, (0, 19): 0.5, (2, 30): 0.2}
    assert ms.feasibility_note(d, ok, bad_max).startswith("OK")        # start policy meets them → possible
    bad_start = {**ok, (0, 15): 0.9, (0, 16): 0.9, (0, 19): 0.9, (2, 30): 0.9}
    note = ms.feasibility_note(d, bad_start, bad_max)
    c0, c2 = d.channels[0].id, d.channels[2].id
    assert note.startswith("4 rule(s)") and f"{c0} w15–16, 19" in note and f"{c2} w30" in note, note


def test_search_model_has_no_report_series():
    """The report series are built only for the out-of-sample check (L05). The search and feasibility models have
    13,099 Terms on the template (v16–v17.2: 13,102; v18 uses one aggregate_mean per cell instead of a share with a
    target constant per channel, so 3 constants fewer; the Seeker call sequence was compared with v17.2: identical
    apart from the fill-rate aggregates)."""
    d = copy.deepcopy(D0)
    x = policies(d)["start"]
    env, _, model = build(d, x, R=20)
    env_d, _, model_d = build(d, x, R=20, detail=True)
    assert model.detail is None and env.n_terms() == 13099
    assert model_d.detail and env_d.n_terms() > env.n_terms()
    assert model.objective.get_value() == model_d.objective.get_value()


def detail_oos(d, x, R=150, seed=7):
    """One out-of-sample batch done by hand: the numbers section 6 reads, and the reference on the same scenarios."""
    env, lay, model = build(d, x, R, seed, detail=True)
    readers = ms.report_numbers(model, d)
    env.evaluate()
    oos = ms.derived_numbers({k: r() for k, r in readers.items()}, d)
    oos["n"] = R
    res = ref.MEIOSimulator(d, seeker_scenarios(model, d)).run(lay.decode(x), detail=True)
    return model, oos, res


def stat(a, kind):
    """The statistic as the Seeker test double computes it ((⌊q·N⌋+1)-th smallest value for quantiles)."""
    a = np.asarray(a, float)
    return a.mean() if kind == "mean" else np.sort(a)[int(np.floor(int(kind[1:]) / 100 * len(a) + 1e-9))]


def stochastic_data():
    d = copy.deepcopy(D0)
    d.prod_lead_time = ref.LeadTime(np.array([6, 8]), np.array([0.6, 0.4]))
    for k in d.rms:
        k.lead_time = ref.LeadTime(np.array([k.lead_time.min, k.lead_time.min + 4]), np.array([0.7, 0.3]))
    for o in d.open_rm["RM3"]:
        o["certain"] = False
    return d


@pytest.mark.parametrize("lead", ["fixed", "stochastic"])
def test_bands_are_the_statistics_of_the_reference_series(lead):
    d = copy.deepcopy(D0) if lead == "fixed" else stochastic_data()
    _, oos, res = detail_oos(d, policies(d)["random_small"])
    rec, fg, R = res.detail, d.fg_id, 150
    src = {("end_stock", fg): rec["fg_end"], ("position", fg): rec["ip_fg"], ("waste", fg): rec["fg_waste"],
           ("order_qty", fg): rec["prod_qty"], ("receipts", fg): rec["fg_received"]}
    for k in d.rms:
        src.update({("end_stock", k.id): rec[f"{k.id}_end"], ("position", k.id): rec[f"{k.id}_ip"],
                    ("order_qty", k.id): rec[f"{k.id}_order"], ("receipts", k.id): rec[f"{k.id}_received"]})
        if k.perishable:
            src[("waste", k.id)] = rec[f"{k.id}_waste"]
    for ci, ch in enumerate(d.channels):
        for name in ("served", "lost", "eligible_next_week"):
            src[(name, ch.id)] = rec[name][:, ci, :]
    end = rec["fg_end"]
    src[("rsl_mean", fg)] = np.where(end > 0.5, rec["rsl_num"] / np.maximum(end, 1.0), 0.0)
    n_rows = 0
    for (name, item), arr in src.items():
        for t in range(1, d.horizon + 1):
            got = [oos[("band", name, item, t, kind)] for kind in ms.BAND_STATS]
            for kind, g in zip(ms.BAND_STATS, got):
                if (name, kind) == ("rsl_mean", "mean"):          # stock-weighted: ratio of means, nan if no stock
                    want = rec["rsl_num"][:, t].mean() / end[:, t].mean() if end[:, t].mean() > 1e-9 else float("nan")
                else:
                    want = stat(arr[:, t], kind)
                assert (np.isnan(g) and np.isnan(want)) or g == pytest.approx(want, rel=1e-9, abs=1e-9), (name, item, t, kind)
            assert all(a <= b + 1e-9 for a, b in zip(got[1:], got[2:])), (name, item, t)     # P05 ≤ … ≤ P95
            assert arr[:, t].min() - 1e-9 <= got[0] <= arr[:, t].max() + 1e-9 or name == "rsl_mean"
            n_rows += 1
    for t in range(1, d.horizon + 1):
        assert oos[("band", "no_stock_share", fg, t, "mean")] == pytest.approx((end[:, t] <= 0.5).mean())
    assert n_rows == 33 * d.horizon


def test_arrival_weeks_are_conditional_on_an_order():
    """L06: arrival week of the order placed in week t, over the scenarios where it is placed (nan if never)."""
    d = stochastic_data()
    d.review_period = 2                                              # commit-now notes for weeks 1 and 2
    d.open_prod[1]["arrival"] = 9                                    # pushes some arrivals (no overtaking)
    _, oos, res = detail_oos(d, policies(d)["random_wide"], R=200)
    rec, checked = res.detail, 0
    for item, q_key, a_key in [(d.fg_id, "prod_qty", "prod_arrival")] + [(k.id, f"{k.id}_order", f"{k.id}_arrival") for k in d.rms]:
        for t in range(1, d.horizon + 1):
            if ("band", "arrival_week", item, t, "p50") not in oos:
                continue
            placed = rec[q_key][:, t] > 0
            arr = rec[a_key][placed, t]
            for kind in ms.BAND_STATS:
                got = oos[("band", "arrival_week", item, t, kind)]
                if not placed.any():
                    assert np.isnan(got)
                    continue
                want = arr.mean() if kind == "mean" else min(w for w in np.unique(arr)
                                                             if (arr <= w).mean() >= int(kind[1:]) / 100 - 1e-9)
                assert got == pytest.approx(want), (item, t, kind)
            checked += placed.any()
    assert checked > 20
    for t in range(1, d.review_period + 1):                          # commit-now notes use the conditional P50
        assert oos[("prod", "arrival", t)] == oos[("band", "arrival_week", d.fg_id, t, "p50")] or \
            np.isnan(oos[("prod", "arrival", t)])


def test_kpis_match_the_reference_totals():
    d = stochastic_data()
    _, oos, res = detail_oos(d, policies(d)["random_small"], R=200)
    rec, fg, t0 = res.detail, d.fg_id, 1 + d.prod_lead_time.min
    totals = {("avg_end_stock", fg): rec["fg_end"][:, 1:].mean(1), ("waste", fg): rec["fg_waste"][:, 1:].sum(1)}
    for k in d.rms:
        totals[("avg_end_stock", k.id)] = rec[f"{k.id}_end"][:, 1:].mean(1)
        if k.perishable:
            totals[("waste", k.id)] = rec[f"{k.id}_waste"][:, 1:].sum(1)
    for ci, ch in enumerate(d.channels):
        totals[("lost_initial_weeks", ch.id)] = rec["lost"][:, ci, 1:t0].sum(1)
        totals[("lost_controllable_weeks", ch.id)] = rec["lost"][:, ci, t0:].sum(1)
    for key, arr in totals.items():
        for kind in ("mean", "p10", "p90"):
            assert oos[("kpi",) + key + (kind,)] == pytest.approx(stat(arr, kind), rel=1e-9, abs=1e-9), (key, kind)
        if key[0] != "avg_end_stock":
            assert oos[("kpi",) + key + ("share",)] == pytest.approx((arr >= 0.5).mean()), key
    kpis = ms.band_and_kpi_tables(d, {"constrained": []}, oos)[1]
    assert len(kpis) == 5 + 3 + 3 * 2 + 1 + 3 * 2 and list(kpis.columns) == ["Item", "Unit", "Mean", "P10", "P90"]


def headless_run(tmp_path, monkeypatch, **kw):
    lic = tmp_path / "license.sio"
    lic.write_text("x")
    monkeypatch.setattr(ms, "OUT_OF_SAMPLE_SCENARIOS", 60)
    monkeypatch.setattr(ms, "OUT_OF_SAMPLE_BATCH", 60)
    return ms.run(DATA, str(lic), time_limit=1, skr=fake, **kw)


def test_run_overrides_change_the_in_sample_run_and_are_checked(tmp_path, monkeypatch):
    seen = []
    real = ms.build_model
    monkeypatch.setattr(ms, "build_model", lambda *a, **k: seen.append((a[5], a[6], k.get("detail", False))) or real(*a, **k))
    res = headless_run(tmp_path, monkeypatch, overrides={"NumScenarios": 100, "RandomSeed": 43})
    assert res.overrides == {"NumScenarios": 100, "RandomSeed": 43} and res.ins["n"] == 100
    assert all(c == (43, 100, False) for c in seen if not c[2]) and len(seen) >= 3  # feasibility, start check, search
    assert [c for c in seen if c[2]] == [(44, 60, True)]                           # fresh seeds never = 43
    summ = dict(res.sheets["Summary"].values)
    assert summ["Run overrides (K20)"] == "NumScenarios = 100, RandomSeed = 43"
    for bad, msg in (({"NumScenarios": 0}, "must be a whole number ≥ 1"), ({"Speed": 1}, "unknown run override Speed"),
                     ({"NumScenarios": 100.5}, "must be a whole number"), ({"RandomSeed": -1}, "must be a whole number")):
        with pytest.raises(ValueError, match=msg):
            headless_run(tmp_path, monkeypatch, overrides=bad)


def test_start_policy_on_the_same_fresh_seeds(tmp_path, monkeypatch):
    monkeypatch.setattr(ms, "START_POLICY_OUT_OF_SAMPLE", True)
    res = headless_run(tmp_path, monkeypatch)
    d = res.data
    assert not res.ins["repair"]["used"]
    x0 = res.layout.encode(ms.start_policy(d, z=res.ins["start_z"]["z"]))          # the start Seeker used (K07)
    _, _, model = build(copy.deepcopy(D0), x0, R=60, seed=d.seed + 1)
    assert res.oos_start["cost"] == pytest.approx(model.objective.get_value(), rel=1e-12)
    kpi = dict(zip(res.sheets["KPIs"]["Item"], res.sheets["KPIs"]["Mean"]))
    assert kpi["Saving vs. start policy (fresh, same seeds)"] == pytest.approx(1 - res.oos["cost"] / res.oos_start["cost"])
    monkeypatch.setattr(ms, "START_POLICY_OUT_OF_SAMPLE", False)
    assert headless_run(tmp_path, monkeypatch).oos_start is None


def fast_copy(tmp_path, moq=None, name="fast.xlsx"):
    """The fast-moving test case of 10.11. as an openpyxl copy of the template: 24 weeks, FG shelf life 3, RM3/RM4 8,
    short lead times, no freshness needs, starting stock with 1–3 weeks left (open orders keep the old lead times)."""
    import openpyxl
    wb = openpyxl.load_workbook(DATA)
    for row in wb["Items"].iter_rows(min_row=5):
        if row[0].value == "FG1":
            row[4].value = 3
        if row[0].value in ("RM3", "RM4"):
            row[4].value = 8
    lts = {"LT_RM1": 2, "LT_RM2": 1, "LT_RM3": 2, "LT_RM4": 4, "LT_PROD_KAJA": 1}
    for row in wb["LeadTimeDist"].iter_rows(min_row=5):
        if row[0].value in lts:
            row[1].value = lts[row[0].value]
    for row in wb["Channels"].iter_rows(min_row=5, max_row=7):
        row[4].value = 0                                   # MinRemainingShelfLife
    for row in wb["BOM"].iter_rows(min_row=5):
        if row[1].value in ("RM3", "RM4"):
            row[3].value = 0
    for row in wb["Init_Inventory"].iter_rows(min_row=5):
        if row[3].value is not None:
            row[3].value = min(row[3].value, 3 if row[1].value == "FG1" else 2)
    for row in wb["Settings"].iter_rows(min_row=5):
        if row[0].value == "HorizonPeriods":
            row[1].value = 24
    if moq:
        wb["Production"]["D5"].value = moq
    path = tmp_path / name
    wb.save(path)
    return str(path)


def run_file(tmp_path, monkeypatch, path, **kw):
    lic = tmp_path / "license.sio"
    lic.write_text("x")
    monkeypatch.setattr(ms, "OUT_OF_SAMPLE_SCENARIOS", 60)
    monkeypatch.setattr(ms, "OUT_OF_SAMPLE_BATCH", 60)
    return ms.run(path, str(lic), time_limit=1, skr=fake, **kw)


def test_fill_rate_target_from_excel_or_section_0(tmp_path, monkeypatch):
    """K08: None → Items!TargetFillRate; a number replaces it for the search, start, checks and report."""
    seen = []
    real = ms.build_model
    monkeypatch.setattr(ms, "build_model", lambda *a, **k: seen.append(a[2].fill_rate_target) or real(*a, **k))
    res = headless_run(tmp_path, monkeypatch)
    summ = dict(res.sheets["Summary"].values)
    assert res.data.fill_rate_target == res.data.fill_rate_target_excel == 0.95 and set(seen) == {0.95}
    assert "fill-rate target 95.0% (Excel Items!TargetFillRate of FG1)" in summ["Seeker settings"]
    monkeypatch.setattr(ms, "OPTIMIZATION_FILL_RATE", 0.90)
    seen.clear()
    res = headless_run(tmp_path, monkeypatch)
    summ = dict(res.sheets["Summary"].values)
    assert res.data.fill_rate_target == 0.90 and res.data.fill_rate_target_excel == 0.95 and set(seen) == {0.90}
    assert {c.target for c in res.data.channels} == {0.90}
    assert "fill-rate target 90.0% (section 0 OPTIMIZATION_FILL_RATE; Excel 95.0%)" in summ["Seeker settings"]
    assert res.ins["start_z"]["z0"] == pytest.approx(0.858, abs=1e-3)                 # start z from 90%
    fr = res.sheets["FillRates"]
    col = [c for c in fr.columns if c.startswith("Expected FR in-sample")][0]
    con = fr[fr["Constrained"] == "yes"]
    assert (fr["Target"] == 0.90).all()
    assert ((con[col] >= 0.90 - 1e-9) == (con["Status in-sample"] == "OK")).all()        # status judged at 90%
    assert int(summ["Constraints met in-sample"].split(" of ")[0]) == int((con[col] >= 0.90 - 1e-9).sum())
    for bad in (0.0, 1.5):
        monkeypatch.setattr(ms, "OPTIMIZATION_FILL_RATE", bad)
        with pytest.raises(ValueError, match="OPTIMIZATION_FILL_RATE"):
            headless_run(tmp_path, monkeypatch)


def test_fill_rates_sheet_explains_each_cell(tmp_path, monkeypatch):
    """L04: per cell the expected fill rate (in-sample, fresh), the share of fresh scenarios fully served and the
    P10 fill rate, all equal to the reference simulator on the same fresh scenarios."""
    res = headless_run(tmp_path, monkeypatch)
    fr = res.sheets["FillRates"].set_index(["Channel", "Week"])
    n = res.oos["n"]
    cols = {c.split(" (")[0]: c for c in fr.columns if "(" in c}
    assert set(cols) >= {"Expected FR in-sample", "Expected FR", "Fully served", "FR in a bad scenario, P10",
                         "Worst 5% of futures, average FR"}
    d, x = res.data, res.x
    _, _, model = build(copy.deepcopy(D0), x, R=n, seed=d.seed + 1)
    sim = ref.MEIOSimulator(copy.deepcopy(D0), seeker_scenarios(model, d)).run(res.layout.decode(x))
    for ci, ch in enumerate(d.channels):
        for t in (7, 20, 36):
            f = sim.fill_rate[:, ci, t]
            assert fr.loc[(ch.id, t), cols["Expected FR"]] == pytest.approx(f.mean(), rel=1e-9)
            assert fr.loc[(ch.id, t), cols["Fully served"]] == pytest.approx((f >= 1 - 1e-6).mean())
            assert fr.loc[(ch.id, t), "Status out-of-sample"] == ("OK" if f.mean() >= 0.95 - 1e-9 else "BELOW TARGET")
            tail = np.sort(f)[:int(round(0.05 * n))].mean()                     # the worst 5% (n = 60 → 3 futures)
            assert fr.loc[(ch.id, t), cols["Worst 5% of futures, average FR"]] == pytest.approx(tail, abs=1e-9)
    ci, t = min(res.ins["constrained"], key=lambda k: res.oos[("fr_mean",) + k])
    assert dict(res.sheets["Summary"].values)["Weakest cell (out-of-sample)"].startswith(
        f"{d.channels[ci].id} week {t}: expected FR {res.oos[('fr_mean', ci, t)]:.1%} (target 95.0%)")


def test_worst_5_percent_average_is_exact_also_with_ties():
    """L04: the tail formula equals the mean of the lowest 5% of futures, also for all-or-nothing weeks."""
    rng = np.random.default_rng(3)
    for f in (np.minimum(1, rng.gamma(2, 0.6, 2000)), np.where(rng.random(2000) < 0.046, 0.0, 1.0),
              np.where(rng.random(2000) < 0.06, rng.random(2000), 1.0), np.ones(2000)):
        env = fake.Env("lic", True)
        env.set_stochastic_parameters(len(f), 0.0)
        term = fake.Term(env, lambda: f.copy(), [], True)
        read = ms.single_number(types.SimpleNamespace(env=env, T=ms._T(env)), term, "tail5")
        env.evaluate()
        assert read() == pytest.approx(np.sort(f)[:100].mean(), abs=1e-9)


def test_input_warnings_x08_open_orders_and_x09_lot_size(tmp_path):
    """X08 / X09 (K19): silent on the template; on the fast-moving copy every open order placed under the old lead
    times and the MOQ 6,000 (> about 5,600 cans a fresh lot can sell) are flagged; MOQ 2,000 is not."""
    assert len(ms.load_data(DATA).warnings) == 2                                   # only the two Sourcing warnings
    w = ms.load_data(fast_copy(tmp_path)).warnings
    x08 = [m for m in w if "(X08)" in m]
    assert len(x08) == 9 and any("PRD-FG1-02" in m and "no overtaking" in m for m in x08)
    assert any(m.startswith("Open_Orders row 11 (PO-RM4-02)") and "12 weeks after placement" in m and "4 weeks" in m for m in x08)
    x09 = [m for m in w if "(X09)" in m]
    assert len(x09) == 1 and "Production MOQ 6,000" in x09[0]
    d = ms.load_data(fast_copy(tmp_path, moq=2000, name="moq2000.xlsx"))
    assert not [m for m in d.warnings if "(X09)" in m]
    sell = ms.fresh_lot_sales(d)                                                   # 4 sellable weeks × mean demand
    assert sell == pytest.approx(4 * d.demand_q[:, 1:, :].mean(2).mean(1).sum())
    tpl = ms.load_data(DATA)                                                       # D2C 21 weeks, AMZ/B2B 13 weeks
    mean_c = tpl.demand_q[:, 1:, :].mean(2).mean(1)
    assert ms.fresh_lot_sales(tpl) == pytest.approx(sum(m * (37 - c.min_remaining) for m, c in zip(mean_c, tpl.channels)))


def test_max_supply_reorder_point_is_the_reachable_position_plus_one_order():
    """K17 v17.2: s = S = reachable position + max order (small enough for a repaired start, K21)."""
    d = copy.deepcopy(D0)
    lay = ms.PolicyLayout(d)
    pol = ms.max_supply_policy(d, lay)
    fg0 = sum(q for _, q in d.init_fg) + sum(o["qty"] for o in d.open_prod)
    rm0 = {k.id: sum(q for _, q in d.init_rm[k.id]) + sum(o["qty"] for o in d.open_rm[k.id]) for k in d.rms}
    for t in (1, 10):
        reach = fg0 + min([(t - 1) * d.prod_max] + [(rm0[k.id] + (t - 1) * k.max_order) / k.bom for k in d.rms])
        assert reach + d.prod_max < pol["FG"][0][t] <= reach + d.prod_max + lay.mult["FG"]
        for k in d.rms:
            r = rm0[k.id] + k.bom * fg0 + (t - 1) * k.max_order + k.max_order
            assert r < pol[k.id][0][t] <= r + lay.mult[k.id] and pol[k.id][1][t] == pol[k.id][0][t]
    assert pol["FG"][0][lay.active_until["FG"] + 1] == 0


def test_start_check_raises_z_on_the_template_without_repair(tmp_path, monkeypatch, capsys):
    """K07: the target-aware start (z₀ = 1.22 at 95%) misses a few cells on Seeker's scenarios; the start check raises z
    in steps of 0.5 until every cell is met, then Seeker starts from it — no repair."""
    res = headless_run(tmp_path, monkeypatch)
    out = capsys.readouterr().out
    zr = res.ins["start_z"]
    assert zr["z0"] == pytest.approx(1.216, abs=1e-3) and zr["z"] > zr["z0"] and zr["z"] <= ms.START_Z_MAX
    assert [s["z"] for s in zr["steps"]] == [zr["z0"]] + [1.5 + 0.5 * i for i in range(len(zr["steps"]) - 1)]
    assert zr["steps"][-1]["met"] == 90 and all(s["met"] < 90 for s in zr["steps"][:-1])
    assert res.ins["start"]["met"] == 90 and f"Seeker starts from z = {zr['z']:.2f}" in out
    assert "START REPAIR USED" not in out and not res.ins["repair"]["used"]
    summ = dict(res.sheets["Summary"].values)
    assert summ["Start repair (section 4c, K21)"] == "not needed — the start check raised z (see 'Start policy')"
    assert summ["Start policy (warm start)"].startswith(f"z = {zr['z']:.2f} (z₀ = 1.22 from fill-rate target 95.0% "
                                                        "via the normal loss function, raised by the start check)")
    assert "Start policy before repair (in-sample)" not in summ
    monkeypatch.setattr(ms, "START_Z", 4.0)                                        # fixed z: no steps
    res = headless_run(tmp_path, monkeypatch)
    assert res.ins["start_z"]["steps"] == [] and res.ins["repair"] == {"used": False}
    assert dict(res.sheets["Summary"].values)["Start policy (warm start)"].startswith("z = 4.00 (fixed in section 0")


def test_lower_target_gives_a_lower_start_z_and_cheaper_start():
    d = copy.deepcopy(D0)
    costs = []
    for beta in (0.98, 0.95, 0.90):
        d.fill_rate_target = beta
        lay, pol = ms.PolicyLayout(d), ms.start_policy(d)
        lay.include_start(pol)
        _, _, model = build(d, lay.encode(pol), R=50)
        costs.append(model.objective.get_value())
    assert costs[0] > costs[1] > costs[2], costs


def test_start_check_and_repair_on_the_fast_moving_case(tmp_path, monkeypatch, capsys):
    """Fast-moving copy (MOQ 6,000). (a) Target-aware start: z₀ ≈ 1.0 misses cells, the start check raises z to 2.0 and
    every cell is met — no repair. (b) With z fixed at 1.0 (no steps) the repair (K21) makes the start feasible."""
    res = run_file(tmp_path, monkeypatch, fast_copy(tmp_path))
    zr = res.ins["start_z"]
    assert zr["z0"] == pytest.approx(0.997, abs=1e-3) and zr["z"] == 2.0 and [s["z"] for s in zr["steps"]][1:] == [1.5, 2.0]
    assert res.ins["start"]["met"] == 69 and not res.ins["repair"]["used"]
    capsys.readouterr()
    monkeypatch.setattr(ms, "START_Z", 1.0)
    calls = []
    real = ms.evaluate_policy
    monkeypatch.setattr(ms, "evaluate_policy", lambda *a, **k: calls.append(a[5]) or real(*a, **k))
    res = run_file(tmp_path, monkeypatch, fast_copy(tmp_path, name="fast2.xlsx"))
    out = capsys.readouterr().out
    rec = res.ins["repair"]
    assert res.ins["start_z"]["steps"] == [] and rec["used"] and rec["start_met"] < rec["target"] == rec["n"] == 69
    assert rec["steps"][-1]["met"] == 69 and rec["chosen_met_target"] and res.ins["start"]["met"] == 69
    assert all(c.startswith("4c. Start repair R") for c in calls[1:]) and len(calls[1:]) == len(
        [s for s in rec["steps"] if s["step"] != "R3"])
    assert out.count("START REPAIR USED") == 2
    summ = dict(res.sheets["Summary"].values)
    assert summ["Start repair (section 4c, K21)"].startswith("USED: start policy met")
    assert summ["Start policy before repair (in-sample)"].endswith(f"{rec['start_met']} of 69 rules met")
    # the repaired weeks: maximum supply only in the order weeks before a missed week, elsewhere the formula
    pol0, msp = ms.start_policy(res.data, z=1.0), ms.max_supply_policy(res.data, res.layout)
    raised = ms._raise_before(res.data, res.layout, pol0, msp, [24], 6)
    lead = res.data.prod_lead_time.min
    for t in range(1, 24 - lead - 5):
        assert raised["FG"][0][t] == pol0["FG"][0][t]
    for t in range(24 - lead - 5, 24 - lead + 1):
        assert raised["FG"][0][t] == msp["FG"][0][t]


def test_start_repair_off_and_not_needed_cases(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ms, "START_REPAIR", False)
    monkeypatch.setattr(ms, "START_Z", 1.0)                                      # no z steps, no repair
    res = run_file(tmp_path, monkeypatch, fast_copy(tmp_path))
    assert "START_REPAIR = False" in capsys.readouterr().out and res.ins["repair"] == {"used": False}
    monkeypatch.setattr(ms, "START_Z", None)                                     # z steps still run without repair
    res = run_file(tmp_path, monkeypatch, fast_copy(tmp_path, name="f3.xlsx"))
    assert res.ins["start_z"]["z"] > res.ins["start_z"]["z0"] and not res.ins["repair"]["used"]
    monkeypatch.setattr(ms, "START_REPAIR", True)
    res = run_file(tmp_path, monkeypatch, fast_copy(tmp_path, moq=2000, name="m2.xlsx"))  # the guard suffices here
    assert not res.ins["repair"]["used"] and res.ins["start"]["met"] == len(res.ins["constrained"])


def test_repair_keeps_the_best_step_when_no_step_meets_the_target(monkeypatch):
    """If no step meets every target rule, Seeker starts from the step meeting the most (then the cheapest)."""
    d = copy.deepcopy(D0)
    lay = ms.PolicyLayout(d)
    keys = [(0, 10), (0, 11), (0, 12)]
    start = {keys[0]: 1.0, keys[1]: 0.5, keys[2]: 0.5}
    maxs = {keys[0]: 0.5, keys[1]: 1.0, keys[2]: 1.0}
    results = iter([({keys[0]: 0.5, keys[1]: 1.0, keys[2]: 0.5}, 200.0),     # R1: 1 of 3 target rules
                    ({keys[0]: 0.5, keys[1]: 1.0, keys[2]: 1.0}, 300.0),     # R2 (6): 2 of 3
                    ({keys[0]: 0.5, keys[1]: 1.0, keys[2]: 1.0}, 250.0)])    # R2 (12): 2 of 3, cheaper
    monkeypatch.setattr(ms, "evaluate_policy", lambda *a, **k: next(results))
    pol0 = ms.start_policy(d)
    _, rec = ms.repair_start(fake, d, lay, pol0, start, 100.0, maxs, "lic")
    assert [s["step"] for s in rec["steps"]] == ["R1", "R2", "R2", "R3"]
    assert rec["chosen"].startswith("R2: maximum supply in the 12") and not rec["chosen_met_target"]


def test_waste_shares_use_the_right_demand():
    """L10: FG waste ÷ expected FG demand; raw-material waste ÷ (BOM × expected FG demand)."""
    d = copy.deepcopy(D0)
    oos = {("fg_waste", t): 10.0 for t in range(1, 37)}
    oos.update({(k.id, "waste", t): 2.0 for k in d.rms for t in range(1, 37)})
    rows, _ = ms.waste_summary(d, oos)
    demand = d.demand_q[:, 1:, :].mean(2).sum()
    got = {i: sh for i, _, sh in rows}
    assert got["FG1"] == pytest.approx(360 / demand)
    for k in d.rms:
        if k.perishable:
            assert got[k.id] == pytest.approx(72 / (k.bom * demand))


def test_expected_waste_rows_and_warning(tmp_path, monkeypatch, capsys):
    """L10: waste rows on the fresh scenarios; no warning on the template; a warning above the limit."""
    res = headless_run(tmp_path, monkeypatch)
    summ = dict(res.sheets["Summary"].values)
    fg = sum(res.oos[("fg_waste", t)] for t in range(1, 37))
    demand = res.data.demand_q[:, 1:, :].mean(2).sum()
    assert summ["Expected waste FG1 (out-of-sample, L10)"] == f"{fg:,.0f} pcs = {fg / demand:.1%} of expected demand"
    assert "Expected waste RM3 (out-of-sample, L10)" in summ and "Expected waste RM1 (out-of-sample, L10)" not in summ
    rm4 = [k for k in res.data.rms if k.id == "RM4"][0]
    w4 = sum(res.oos[("RM4", "waste", t)] for t in range(1, 37))
    assert summ["Expected waste RM4 (out-of-sample, L10)"] == f"{w4:,.0f} kg = {w4 / (rm4.bom * demand):.1%} of expected demand"
    assert summ["Waste warning (L10)"] == "none" and "WARNING (L10)" not in capsys.readouterr().out
    monkeypatch.setattr(ms, "WASTE_WARNING_SHARE", 0.0)
    res = headless_run(tmp_path, monkeypatch)
    assert "WARNING (L10): expected waste is high" in capsys.readouterr().out
    assert "MEIO_shelf_life_aware_policies.md" in dict(res.sheets["Summary"].values)["Waste warning (L10)"]


def test_single_number_reads_aggregates_and_constant_terms():
    d = copy.deepcopy(D0)
    env = fake.Env("lic", True)
    env.set_stochastic_parameters(50, 0.0)
    lay = ms.PolicyLayout(d)
    model = ms.build_model(fake, env, d, lay, policies(d)["random_small"], 3, 50)
    q1 = model.rm_order[("RM3", 1)]                                  # week 1: the same in every scenario
    readers = {"mean1": ms.single_number(model, q1), "p90_1": ms.single_number(model, q1, "p90"),
               "share1": ms.single_number(model, q1, "share", 0.5), "num": ms.single_number(model, 7.0, "p50"),
               "fr_mean": ms.single_number(model, model.fill_rate[(0, 20)]),
               "fr_p10": ms.single_number(model, model.fill_rate[(0, 20)], "p10"),
               "end_share": ms.single_number(model, model.fg_end[20], "share", 5000)}
    env.evaluate()
    fr = np.asarray(model.fill_rate[(0, 20)].get_values())
    end = np.asarray(model.fg_end[20].get_values())
    assert readers["mean1"]() == readers["p90_1"]() == q1.get_value()
    assert readers["share1"]() == float(q1.get_value() > 0) and readers["num"]() == 7.0
    assert readers["fr_mean"]() == pytest.approx(fr.mean())
    assert readers["fr_p10"]() == np.sort(fr)[5]
    assert readers["end_share"]() == pytest.approx(np.mean(end >= 5000 - 1e-6))


def test_out_of_sample_batches_combine_exactly(monkeypatch):
    d = copy.deepcopy(D0)
    lay = ms.PolicyLayout(d)
    x = policies(d)["start"]
    monkeypatch.setattr(ms, "OUT_OF_SAMPLE_SCENARIOS", 120)
    monkeypatch.setattr(ms, "OUT_OF_SAMPLE_BATCH", 50)                # batches 50 + 50 + 20
    oos = ms.out_of_sample(fake, d, lay, x, "lic")
    costs, shares, n = [], [], [50, 50, 20]
    for b, nb in enumerate(n):
        env, _, model = build(d, x, R=nb, seed=d.seed + 1 + b)
        res = ref.MEIOSimulator(d, seeker_scenarios(model, d)).run(lay.decode(x))
        costs.append(res.cost.sum())
        shares.append(res.fill_rate[:, 1, 20].sum())                    # expected fill rate: sum of FR, then ÷ N
    assert oos["n"] == 120
    assert oos["cost"] == pytest.approx(sum(costs) / 120, rel=1e-12)
    assert oos[("fr_mean", 1, 20)] == pytest.approx(sum(shares) / 120)

# ============================================================================
# 4. OUTPUT CONTROL
# ============================================================================
def test_whole_run_report_matches_reference(tmp_path, monkeypatch):
    out = tmp_path / "res.xlsx"
    lic = tmp_path / "license.sio"
    lic.write_text("x")
    for name, val in dict(LICENSE_FILE=str(lic), DATA_FILE=DATA, OUTPUT_FILE=str(out), TIME_LIMIT_SECONDS=2,
                          OUT_OF_SAMPLE_SCENARIOS=150, OUT_OF_SAMPLE_BATCH=150).items():
        monkeypatch.setattr(ms, name, val)
    seen = {}
    real_opt = ms.optimize

    def spy(*a, **k):
        x, ins = real_opt(*a, **k)
        seen["x"], seen["layout"] = x, a[2]
        return x, ins
    monkeypatch.setattr(ms, "optimize", spy)
    real_report = ms.build_report
    monkeypatch.setattr(ms, "build_report", lambda *a: seen.update(args=a) or real_report(*a))
    ms.main()
    xl = pd.read_excel(out, sheet_name=None)
    assert set(xl) == {"Summary", "Commit_Now", "Policy_sS", "Decisions", "FillRates", "Stock_Waste", "Bands", "KPIs"}
    # recompute the out-of-sample figures with the reference simulator on the same fresh scenarios
    d = copy.deepcopy(D0)                                            # same data (equality tested above)
    x, lay = seen["x"], seen["layout"]
    env, _, model = build(d, x, R=150, seed=d.seed + 1)
    res = ref.MEIOSimulator(d, seeker_scenarios(model, d)).run(lay.decode(x), detail=True)
    summ = dict(zip(xl["Summary"]["Item"], xl["Summary"]["Value"]))
    assert float(summ["Expected total cost (out-of-sample)"]) == pytest.approx(res.cost.mean(), rel=1e-9)
    dec = xl["Decisions"].set_index("Week")
    np.testing.assert_allclose(dec["Production mean"].values, res.detail["prod_qty"][:, 1:].mean(0), rtol=1e-9, atol=1e-9)
    np.testing.assert_allclose(dec["RM3 order mean"].values, res.detail["RM3_order"][:, 1:].mean(0), rtol=1e-9, atol=1e-9)
    sw = xl["Stock_Waste"].set_index("Week")
    np.testing.assert_allclose(sw["FG1 end stock (mean)"].values, res.detail["fg_end"][:, 1:].mean(0), rtol=1e-9)
    fr = xl["FillRates"]
    col = [c for c in fr.columns if c.startswith("Expected FR (")][0]
    ref_mean = [res.fill_rate[:, ci, t].mean() for ci in range(len(d.channels)) for t in range(1, d.horizon + 1)]
    np.testing.assert_allclose(fr[col].values, ref_mean, rtol=1e-9)
    np.testing.assert_allclose(dec["P(production)"].values, (res.detail["prod_qty"][:, 1:] > 0).mean(0))
    np.testing.assert_allclose(dec["P(RM3 order)"].values, (res.detail["RM3_order"][:, 1:] > 0).mean(0))
    # commit-now rows must come from week 1 (make week 2 different to be sure)
    d_, lay_, x_, ins_, oos_ = seen["args"][:5]
    oos2 = dict(oos_)
    for key in list(oos2):
        if isinstance(key, tuple) and len(key) == 3 and key[1] == "mean" and key[2] == 2:
            oos2[key] = 12345.0
    rows = real_report(d_, lay_, x_, ins_, oos2)["Commit_Now"]
    week1 = rows[rows["When"] == "Week 1"].set_index("Decision")["Quantity"]
    assert week1["Production order FG1 (KAJA)"] == res.detail["prod_qty"][0, 1]
    for k in d.rms:
        assert week1[f"Supplier order {k.id}"] == res.detail[f"{k.id}_order"][0, 1]
        assert week1[f"Transfer {k.id} RMW → KAJA"] == pytest.approx(res.detail[f"{k.id}_transfer"][0, 1])
    commit = xl["Commit_Now"]
    rm3 = commit[(commit["When"] == "Week 1") & (commit["Decision"] == "Supplier order RM3")]["Quantity"].iloc[0]
    assert float(rm3) == res.detail["RM3_order"][0, 1]


def test_whole_run_prints_and_reports_the_feasibility_line(tmp_path, monkeypatch, capsys):
    out = tmp_path / "res.xlsx"
    lic = tmp_path / "license.sio"
    lic.write_text("x")
    for name, val in dict(LICENSE_FILE=str(lic), DATA_FILE=DATA, OUTPUT_FILE=str(out), TIME_LIMIT_SECONDS=1,
                          OUT_OF_SAMPLE_SCENARIOS=60, OUT_OF_SAMPLE_BATCH=60).items():
        monkeypatch.setattr(ms, name, val)
    ms.main()
    printed = capsys.readouterr().out
    assert "Feasibility check: OK" in printed and "4b. Feasibility check" in printed
    summ = dict(pd.read_excel(out, sheet_name="Summary").values)
    assert str(summ["Feasibility check (section 4b)"]).startswith("OK")
    monkeypatch.setattr(ms, "FEASIBILITY_CHECK", False)                 # switch off → no extra model
    ms.main()
    printed = capsys.readouterr().out
    assert "Feasibility check" not in printed.split("Time per phase")[0]


def test_locked_output_file_gets_alternative_name(tmp_path, monkeypatch):
    f = tmp_path / "MEIO_Results_stochastic.xlsx"
    assert ms.usable_output_path(f) == f
    f.write_bytes(b"x")
    assert ms.usable_output_path(f) == f
    real_open = open

    def locked(path, mode="r", *a, **k):
        if Path(path) == f and ("+" in mode or "w" in mode):
            raise PermissionError(13, "Permission denied")
        return real_open(path, mode, *a, **k)
    monkeypatch.setattr("builtins.open", locked)
    alt = ms.usable_output_path(f)
    assert alt != f and alt.name.startswith("MEIO_Results_stochastic_")


def test_no_simulator_left_in_production_file():
    """The rules live only in section 3: no NumPy dynamics in the file the user runs."""
    src = (ROOT / "meio_seeker_stochastic.py").read_text(encoding="utf-8")
    for banned in ("class MEIOSimulator", "def make_scenarios", "class Scenarios", "def run(self, policy",
                   "def fefo_take", "def cost(self, q)", "get_values("):
        assert banned not in src, banned


def headless(tmp_path, monkeypatch):
    lic = tmp_path / "license.sio"
    lic.write_text("x")
    monkeypatch.setattr(ms, "OUT_OF_SAMPLE_SCENARIOS", 60)
    monkeypatch.setattr(ms, "OUT_OF_SAMPLE_BATCH", 60)
    return ms.run(str(DATA), str(lic), time_limit=1, skr=fake)


def test_commit_now_lists_every_decision_of_the_review_period(tmp_path, monkeypatch):
    """R = 4: Commit_Now holds exact quantities for weeks 1–4 (no rules), equal to the reference in every scenario;
    the Decisions sheet marks weeks 1–4 COMMIT NOW and names the run week that plans each later week (L02, L03)."""
    import openpyxl
    wb = openpyxl.load_workbook(DATA)
    for row in wb["Settings"].iter_rows():
        if row[0].value == "ReviewPeriod":
            row[1].value = 4
    path = tmp_path / "r4.xlsx"
    wb.save(path)
    lic = tmp_path / "license.sio"
    lic.write_text("x")
    monkeypatch.setattr(ms, "OUT_OF_SAMPLE_SCENARIOS", 60)
    monkeypatch.setattr(ms, "OUT_OF_SAMPLE_BATCH", 60)
    res = ms.run(str(path), str(lic), time_limit=1, skr=fake)
    d, commit, dec = res.data, res.sheets["Commit_Now"], res.sheets["Decisions"]
    assert d.review_period == 4 and list(commit["When"].unique()) == ["Week 1", "Week 2", "Week 3", "Week 4"]
    assert not commit["Decision"].str.contains("Rule").any()
    dr = ref.load_data(str(path))
    rr = ref.MEIOSimulator(dr, ref.make_scenarios(dr, 50, 5)).run(res.policy, detail=True)
    for t in range(1, 5):
        rows = commit[commit["When"] == f"Week {t}"].set_index("Decision")["Quantity"]
        assert np.all(rr.detail["prod_qty"][:, t] == rows["Production order FG1 (KAJA)"])
        for k in d.rms:
            assert np.all(rr.detail[f"{k.id}_order"][:, t] == rows[f"Supplier order {k.id}"])
    assert list(dec["Status"][:4]) == ["COMMIT NOW (exact)"] * 4
    assert dec["Status"][4] == "Indicative — re-optimized at the run in week 5"
    assert dec["Status"][5] == "Indicative — planned at the run in week 5"
    assert list(res.sheets["Policy_sS"]["Planned at run week"][:9]) == [1, 1, 1, 1, 5, 5, 5, 5, 9]
    assert "R = 4 weeks" in res.sheets["Summary"].set_index("Item").loc["Review period (A03)", "Value"]


def schema_of(res):
    return {"contract_version": CONTRACT_VERSION,
            "sheets": {name: list(map(str, df.columns)) for name, df in res.sheets.items()}}


def test_run_returns_everything_an_app_needs(tmp_path, monkeypatch):
    res = headless(tmp_path, monkeypatch)
    d, H = res.data, res.data.horizon
    assert len(res.x) == len(res.layout) == 244
    assert set(res.policy) == {"FG", "RM1", "RM2", "RM3", "RM4"}
    for s, S in res.policy.values():
        assert len(s) == len(S) == H + 1 and (S >= s).all()
    for key in ("cost", "parts", "fr_mean", "met", "start", "status", "feasibility", "constrained", "repair", "start_z"):
        assert key in res.ins, key
    assert res.oos["n"] == 60 and "cost" in res.oos
    assert res.ins["feasibility"].startswith("OK")
    assert [p for p, _ in res.timings][0].startswith("1. Reading")
    summary = dict(res.sheets["Summary"].values)
    assert summary["Expected total cost (in-sample)"] == pytest.approx(res.ins["cost"])
    assert len(res.sheets["Policy_sS"]) == H and len(res.sheets["FillRates"]) == H * len(d.channels)


def test_output_schema_is_frozen(tmp_path, monkeypatch):
    got = schema_of(headless(tmp_path, monkeypatch))
    assert got == OUTPUT_SCHEMA, "output schema changed: update OUTPUT_SCHEMA and CONTRACT_VERSION (engine session)"


# frozen output schema (contract version 3: v2 + rule E — FillRates columns, one target per product): sheet → column names, template data
OUTPUT_SCHEMA = {
 "contract_version": 4,
 "sheets": {
  "Summary": [
   "Item",
   "Value"
  ],
  "Commit_Now": [
   "When",
   "Decision",
   "Quantity",
   "Note"
  ],
  "Policy_sS": [
   "Week",
   "WeekStart",
   "Planned at run week",
   "FG s",
   "FG S",
   "FG active",
   "RM1 s",
   "RM1 S",
   "RM1 active",
   "RM2 s",
   "RM2 S",
   "RM2 active",
   "RM3 s",
   "RM3 S",
   "RM3 active",
   "RM4 s",
   "RM4 S",
   "RM4 active"
  ],
  "Decisions": [
   "Week",
   "WeekStart",
   "Status",
   "Production mean",
   "Production P10",
   "Production P90",
   "P(production)",
   "RM1 order mean",
   "RM1 order P10",
   "RM1 order P90",
   "P(RM1 order)",
   "RM1 transfer→KAJA mean",
   "RM2 order mean",
   "RM2 order P10",
   "RM2 order P90",
   "P(RM2 order)",
   "RM2 transfer→KAJA mean",
   "RM3 order mean",
   "RM3 order P10",
   "RM3 order P90",
   "P(RM3 order)",
   "RM3 transfer→KAJA mean",
   "RM4 order mean",
   "RM4 order P10",
   "RM4 order P90",
   "P(RM4 order)",
   "RM4 transfer→KAJA mean"
  ],
  "FillRates": [
   "Channel",
   "Week",
   "Target",
   "Constrained",
   "Expected FR in-sample (200 Seeker scenarios)",
   "Status in-sample",
   "Expected FR (60 fresh scenarios)",
   "Status out-of-sample",
   "Fully served (60 fresh scenarios)",
   "FR in a bad scenario, P10 (60 fresh scenarios)",
   "Worst 5% of futures, average FR (60 fresh scenarios)"
  ],
  "Stock_Waste": [
   "Week",
   "FG1 end stock (mean)",
   "FG1 waste (mean)",
   "RM1 end stock (mean)",
   "RM2 end stock (mean)",
   "RM3 end stock (mean)",
   "RM3 waste (mean)",
   "RM4 end stock (mean)",
   "RM4 waste (mean)",
   "Holding cost (mean)"
  ],
  "Bands": [
   "Series",
   "Item",
   "Week",
   "Mean",
   "P05",
   "P10",
   "P25",
   "P50",
   "P75",
   "P90",
   "P95"
  ],
  "KPIs": [
   "Item",
   "Unit",
   "Mean",
   "P10",
   "P90"
  ]
 }
}



# ============================================================================
# 5. STRESS-TEST CASES — every data change does exactly what its name says
# ============================================================================
def test_every_case_produces_valid_data_and_a_model_layout():
    for cid in STRESS:
        d = stressed(cid)
        assert len(ms.PolicyLayout(d)) > 0, cid


def test_channel_change_keeps_priority_rule_and_demand_rows_aligned():
    """Stress changes keep the serving order of ShortagePriority (H02) and the demand rows; targets are per product."""
    d = stressed("R1")                                           # freshness change: order and demand rows unchanged
    assert [c.id for c in d.channels] == ["D2C", "B2B", "AMZ"]
    for i, c in enumerate(d.channels):
        np.testing.assert_array_equal(d.demand_q[i], D0.demand_q[i])
    for cid, value in (("F1", 0.96), ("F2", 0.98), ("F3", 0.90)):
        d = stressed(cid)
        assert d.fill_rate_target == value and {c.target for c in d.channels} == {value}


def test_demand_changes():
    np.testing.assert_allclose(stressed("D1").demand_q, D0.demand_q * 1.3)
    d = stressed("D3")
    med0, med = D0.demand_q[:, :, 4:6].mean(2), d.demand_q[:, :, 4:6].mean(2)
    np.testing.assert_allclose(med, med0)                     # median unchanged (no clipping there)
    assert (d.demand_q[:, 1:, -1] - med[:, 1:] >= D0.demand_q[:, 1:, -1] - med0[:, 1:] - 1e-9).all()
    amz = [c.id for c in D0.channels].index("AMZ")
    d = stressed("D4")
    np.testing.assert_allclose(d.demand_q[amz, [14, 15]], D0.demand_q[amz, [14, 15]] * 3)
    np.testing.assert_allclose(d.demand_q[amz, 13], D0.demand_q[amz, 13])


def test_shelf_life_freshness_and_supply_changes():
    d = stressed("S1")
    assert d.fg_shelf_life == 30 and max(a for a, _ in d.init_fg) <= 30
    assert st.rm(stressed("S2"), "RM3").shelf_life == 46
    assert stressed("R2").rho_min == 20                          # cans now scrapped below 20 weeks
    assert st.rm(stressed("O4"), "RM3").max_order == 750 and st.rm(stressed("O5"), "RM3").max_order == 500
    assert stressed("O2").open_rm["RM3"] == []
    assert st.rm(stressed("L1"), "RM3").lead_time.max == 12 and stressed("L2").prod_lead_time.min == 8


def test_noise_and_direction_logic():
    base = {"oos_cost": 100.0, "fg_stock": 10.0, "waste_units": 1.0, "prod_orders": 5.0, "in_met": 90, "rules": 90}
    res = {"BASE": base, "BASE2": dict(base, oos_cost=101.0),
           "D1": dict(base, oos_cost=110.0), "D2": dict(base, oos_cost=100.5), "F3": dict(base, oos_cost=105.0),
           "O5": dict(base, in_met=70)}
    rows, noise = st.checks(res)
    out = {r["Case"]: r["Outcome"] for r in rows}
    assert noise["oos_cost"] == 0.01
    assert out["D1"] == "as expected" and out["D2"] == "no clear change"
    assert out["F3"].startswith("CHECK") and out["O5"] == "as expected"
    assert st.verdict(res["O5"], [r for r in rows if r["Case"] == "O5"]) == "OK (impossible case recognised)"

# ============================================================================
# 6. ENGINE FINGERPRINT
# ============================================================================


ENGINE_SHA256 = {   # engine v18.1 — set by the engine session (python test_engine.py --fingerprint)
    "meio_seeker_stochastic.py": "1c6eed4c49ce15932987b100d30182e745ae02f8011a28fdbae39e95b8e8521d",
    "reference_simulator.py": "a308374a5ae34f9798d4457a0cdcfe16e470861630f14ce1844f4019680c9025",
    "meio_stress_tests.py": "f0fc2fe4fe372f045874de1d35846961eb1eb01d027a24b91810c17982af7a5c",
}


def test_engine_files_unchanged():
    """The engine is maintained in the engine session. An edit here is a bug: request the change there."""
    for name, digest in ENGINE_SHA256.items():
        got = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        assert got == digest, f"{name} was edited — engine files are read-only in the app (see CLAUDE.md)"


if __name__ == "__main__" and "--fingerprint" in sys.argv:          # engine session only
    print({n: hashlib.sha256((ROOT / n).read_bytes()).hexdigest()
           for n in ("meio_seeker_stochastic.py", "reference_simulator.py", "meio_stress_tests.py",
                     )})
