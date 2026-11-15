"""
test_baseline.py — tests of meio_baseline.py (textbook (s,S) baseline). No Seeker license needed: uses the Seeker test
double of test_engine.py. Run: python -m pytest -q test_baseline.py
The engine's own test suite (test_engine.py) is not touched by the baseline.
"""
import copy
import math

import numpy as np
import openpyxl
import pandas as pd
import pytest

import test_engine as te                     # installs the Seeker test double (sys.modules["seeker"])
import meio_seeker_stochastic as ms
import meio_baseline as mb
import reference_simulator as ref

DATA, fake = te.DATA, te.fake


def engine_run(tmp_path, monkeypatch, scenarios=60, batch=60, data=DATA, **kw):
    """One headless engine run (Seeker test double) with the given fresh-scenario batches."""
    lic = tmp_path / "license.sio"
    lic.write_text("x")
    monkeypatch.setattr(ms, "OUT_OF_SAMPLE_SCENARIOS", scenarios)
    monkeypatch.setattr(ms, "OUT_OF_SAMPLE_BATCH", batch)
    return ms.run(data, str(lic), time_limit=1, skr=fake, **kw), str(lic)


def spy_builds(monkeypatch):
    """Record (seed, scenarios, detail) of every Seeker model the engine builds."""
    seen, orig = [], ms.build_model

    def spy(skr, env, d, layout, x, seed, R, detail=False):
        seen.append((seed, R, detail))
        return orig(skr, env, d, layout, x, seed, R, detail=detail)
    monkeypatch.setattr(ms, "build_model", spy)
    return seen


# ---------------------------------------------------------------------------- 1. formulas
def test_formulas_by_hand_on_the_template():
    d = ms.load_data(DATA)
    pol, tab = mb.baseline_policy(d)
    row = tab.set_index("Stock point")
    mu = d.demand_q[:, 1:, :].mean(2).sum(0).mean()
    var = ((((d.demand_q[:, 1:, -1] - d.demand_q[:, 1:, 0]) / 3.29) ** 2).sum(0)).mean()
    # FG: W = 6 + 1; Q = EOQ √(2·2,500·μ/0.015) up to the batch 100 (≥ MOQ 6,000, ≤ 50,000); z = 0 because
    # (1 − 0.95)·Q/σ = 0.95 > G(0) = 0.399: the cycle stock alone covers 5% of Q
    Q = 100 * math.ceil(math.sqrt(2 * 2500 * mu / 0.015) / 100)
    assert row.loc["FG", "Protection weeks W = L + R"] == 7 and row.loc["FG", "Q (after order rules)"] == Q == 21700
    assert row.loc["FG", "z from G(z) = (1 − β)Q/σ"] == 0 and row.loc["FG", "s = W·D + z·σ"] == 100 * round(7 * mu / 100) == 9900
    assert row.loc["FG", "S = s + Q"] == 9900 + 21700
    # RM3 (echelon): W = 8 + 6 + 1; EOQ ≈ 3,064 kg cut to the max order 1,000; σ = 0.5·√(15·var); z from the engine's G⁻¹
    sig = 0.5 * math.sqrt(15 * var)
    z = ms.normal_loss_inverse(0.05 * 1000 / sig)
    assert row.loc["RM3", "Q (after order rules)"] == 1000 and 1.1 < z < 1.25
    assert row.loc["RM3", "z from G(z) = (1 − β)Q/σ"] == pytest.approx(z, abs=1e-9)
    assert row.loc["RM3", "s = W·D + z·σ"] == 50 * round((15 * 0.5 * mu + z * sig) / 50)
    assert row.loc["RM1", "Q (after order rules)"] == 720 * math.ceil(math.sqrt(2 * 150 * mu / 0.002) / 720)
    lay = ms.PolicyLayout(d)
    for j, s_, S_ in zip(tab["Stock point"], tab["s = W·D + z·σ"], tab["S = s + Q"]):   # fixed, 0 after the last week
        last = lay.active_until[j]
        assert (pol[j][0][1:last + 1] == s_).all() and (pol[j][1][1:last + 1] == S_).all() and S_ > s_ > 0
        assert not pol[j][0][last + 1:].any() and not pol[j][1][last + 1:].any() and pol[j][0][0] == 0
    d.review_period = 4                                                          # the review period widens W
    assert mb.baseline_policy(d)[1].set_index("Stock point").loc["FG", "Protection weeks W = L + R"] == 10
    d.fill_rate_target = 0.99                                                    # a higher target: more safety stock
    assert mb.baseline_policy(d)[1].set_index("Stock point").loc["RM3", "z from G(z) = (1 − β)Q/σ"] > z


def test_normal_loss_and_its_inverse():
    assert mb.normal_loss(0) == pytest.approx(1 / math.sqrt(2 * math.pi))
    for z in (0.0, 0.5, 1.17, 2.5):
        assert mb.z_from_fill_rate(0.95, mb.normal_loss(z) / 0.05, 1.0) == pytest.approx(z, abs=1e-9)
    assert mb.z_from_fill_rate(0.95, 100.0, 1.0) == 0.0 and mb.z_from_fill_rate(0.95, 1.0, 0.0) == 0.0


# ---------------------------------------------------------------------------- 2. same fresh scenarios, same settings
def test_baseline_runs_on_the_same_fresh_scenarios_and_equals_the_engine_model(tmp_path, monkeypatch):
    seen = spy_builds(monkeypatch)
    res, lic = engine_run(tmp_path, monkeypatch)
    opt = [c for c in seen if c[2]]
    seen.clear()
    tables = mb.run_baseline(res, lic, skr=fake)
    assert seen == opt == [(res.data.seed + 1, 60, True)]                       # same seed, size and report series
    assert (ms.OUT_OF_SAMPLE_SCENARIOS, ms.OUT_OF_SAMPLE_BATCH) == (60, 60)      # engine settings restored
    # the baseline's cost = the engine model evaluated with the baseline values, built by hand
    d = res.data
    pol, _ = mb.baseline_policy(d)
    lay = ms.PolicyLayout(d)
    x = lay.encode(pol, clip=False)
    assert lay.decode(x)["FG"][0][1] == pol["FG"][0][1]                          # values not clipped
    lay.ub_var = [max(u, v) for u, v in zip(lay.ub_var, x)]
    env = fake.Env("lic", True)
    env.set_stochastic_parameters(60, 0.0)
    model = ms.build_model(fake, env, d, lay, x, d.seed + 1, 60, detail=True)
    env.evaluate()
    assert tables["oos"]["cost"] == pytest.approx(model.objective.get_value(), rel=1e-12)
    comp = tables["Comparison"].set_index("Measure")
    assert comp.loc["Expected total cost (fresh scenarios)", "Optimized"] == res.oos["cost"]
    assert comp.loc["Expected total cost (fresh scenarios)", "Baseline (textbook s,S)"] == tables["oos"]["cost"]


def test_baseline_follows_the_what_ifs_of_the_optimized_run(tmp_path, monkeypatch):
    """RandomSeed / NumScenarios overrides, a run target (OPTIMIZATION_FILL_RATE) and two fresh batches."""
    monkeypatch.setattr(ms, "OPTIMIZATION_FILL_RATE", 0.90)
    seen = spy_builds(monkeypatch)
    res, lic = engine_run(tmp_path, monkeypatch, scenarios=60, batch=30, overrides={"RandomSeed": 11, "NumScenarios": 120})
    opt = [c for c in seen if c[2]]
    seen.clear()
    monkeypatch.setattr(ms, "OUT_OF_SAMPLE_SCENARIOS", 2500)                     # app settings changed since: ignored
    monkeypatch.setattr(ms, "OUT_OF_SAMPLE_BATCH", 2500)
    tables = mb.run_baseline(res, lic, skr=fake)
    assert seen == opt == [(12, 30, True), (13, 30, True)]
    tab = tables["Baseline_sS"].set_index("Stock point")
    assert (tab["Fill-rate target β"] == 0.90).all() and res.data.fill_rate_target == 0.90
    assert tables["FillRates_Compare"]["Target"].eq(0.90).all()


def test_workbook_mode_gives_the_same_comparison_as_the_app_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(ms, "OPTIMIZATION_FILL_RATE", 0.93)
    res, lic = engine_run(tmp_path, monkeypatch, scenarios=60, batch=30, overrides={"RandomSeed": 5})
    path = tmp_path / "MEIO_Results_test.xlsx"
    ms.write_report(res.sheets, str(path))
    app = mb.run_baseline(res, lic, skr=fake)
    monkeypatch.setattr(ms, "OPTIMIZATION_FILL_RATE", None)                     # the target must come from the workbook
    monkeypatch.setattr(ms, "OUT_OF_SAMPLE_SCENARIOS", 2500)                    # and the batches too
    monkeypatch.setattr(ms, "OUT_OF_SAMPLE_BATCH", 2500)
    seen = spy_builds(monkeypatch)
    wb = mb.run_baseline_from_files(DATA, str(path), lic, skr=fake)
    assert seen == [(6, 30, True), (7, 30, True)]
    for name in ("Comparison", "FillRates_Compare", "Baseline_sS"):
        a, b = app[name], wb[name]
        assert list(a.columns) == list(b.columns) and len(a) == len(b)
        for c in a.columns:
            if a[c].dtype.kind in "fi":
                part = (a["Measure"].str.startswith("Cost: ") if "Measure" in a else pd.Series(False, index=a.index))
                part = part & (c != "Baseline (textbook s,S)")         # the workbook rounds the optimized cost parts
                np.testing.assert_allclose(b[c][~part].astype(float), a[c][~part].astype(float), rtol=1e-9, atol=1e-9,
                                           err_msg=f"{name} {c}")
                np.testing.assert_allclose(b[c][part].astype(float), a[c][part].astype(float), atol=0.5, err_msg=f"{name} {c}")
            else:
                assert list(a[c]) == list(b[c]), (name, c)
    out = mb.write_baseline_report(wb, str(tmp_path / "MEIO_Baseline_test.xlsx"))
    assert openpyxl.load_workbook(out).sheetnames == ["Comparison", "Baseline_sS", "FillRates_Compare"]


def test_a_results_workbook_from_other_data_is_rejected(tmp_path, monkeypatch):
    res, lic = engine_run(tmp_path, monkeypatch)
    path = tmp_path / "MEIO_Results_test.xlsx"
    ms.write_report(res.sheets, str(path))
    other = tmp_path / "other.xlsx"                                              # demand changed in one week
    wb = openpyxl.load_workbook(DATA)
    ws = wb["Demand"]
    for r in ws.iter_rows(min_row=5):
        if r[0].value == "D2C" and r[1].value == 14:
            for c in r[3:13]:
                c.value += 4000
    wb.save(other)
    with pytest.raises(ValueError, match="z₀"):
        mb.run_baseline_from_files(str(other), str(path), lic, skr=fake)
    wb = openpyxl.load_workbook(DATA)                                            # review period changed
    ws = wb["Settings"]
    for r in ws.iter_rows():
        if r[0].value == "ReviewPeriod":
            r[1].value = 2
    wb.save(other)
    with pytest.raises(ValueError, match="review period"):
        mb.run_baseline_from_files(str(other), str(path), lic, skr=fake)


# ---------------------------------------------------------------------------- 3. the comparison numbers
def test_lost_demand_and_rules_met_match_the_reference_simulator(tmp_path, monkeypatch):
    res, lic = engine_run(tmp_path, monkeypatch)
    tables = mb.run_baseline(res, lic, skr=fake)
    d = res.data
    pol, _ = mb.baseline_policy(d)
    lay = ms.PolicyLayout(d)
    x = lay.encode(pol, clip=False)
    lay.ub_var = [max(u, v) for u, v in zip(lay.ub_var, x)]
    env = fake.Env("lic", True)
    env.set_stochastic_parameters(60, 0.0)
    model = ms.build_model(fake, env, d, lay, x, d.seed + 1, 60, detail=True)
    env.evaluate()
    dr = copy.deepcopy(te.D0)
    sim = ref.MEIOSimulator(dr, te.seeker_scenarios(model, dr)).run(pol, detail=True)
    lost, t0, H = sim.detail["lost"], 1 + d.prod_lead_time.min, d.horizon
    comp = tables["Comparison"].set_index("Measure")
    base = "Baseline (textbook s,S)"
    assert comp.loc[f"Lost demand, all channels, weeks 1–{H}", base] == pytest.approx(lost[:, :, 1:].sum((1, 2)).mean(), rel=1e-9)
    assert comp.loc[f"Lost demand, all channels, weeks {t0}–{H} (weeks with fill-rate rules)", base] == pytest.approx(
        lost[:, :, t0:].sum((1, 2)).mean(), rel=1e-9)
    for ci, ch in enumerate(d.channels):
        assert comp.loc[f"Lost demand {ch.id}, weeks 1–{H}", base] == pytest.approx(lost[:, ci, 1:].sum(1).mean(), rel=1e-9)
    fr = ref.expected_fill_rate(sim.fill_rate)
    rules = [m for m in comp.index if m.startswith("Fill-rate rules met")][0]
    assert comp.loc[rules, base] == sum(ms.cell_met(d, fr[ci, t]) for ci, t in res.ins["constrained"])
    assert comp.loc["Expected total cost (fresh scenarios)", base] == pytest.approx(sim.cost.mean(), rel=1e-12)
    # the optimized column = the engine's own out-of-sample numbers
    opt_lost = sum(res.oos[("kpi", n, ch.id, "mean")] for ch in d.channels
                   for n in ("lost_initial_weeks", "lost_controllable_weeks"))
    assert comp.loc[f"Lost demand, all channels, weeks 1–{H}", "Optimized"] == pytest.approx(opt_lost, rel=1e-12)
    assert ((comp["Optimized − baseline"] == comp["Optimized"] - comp[base]) | comp["Optimized"].isna()).all()
    assert len(mb.summary_lines(d, tables["Comparison"])) == 3


def test_lost_demand_counts_the_first_weeks_too(tmp_path, monkeypatch):
    """With little opening stock, weeks 1–6 lose demand in both plans; the total includes them."""
    low = tmp_path / "low_stock.xlsx"
    wb = openpyxl.load_workbook(DATA)
    for r in wb["Init_Inventory"].iter_rows(min_row=5):
        if r[1].value == "FG1":
            r[2].value = r[2].value // 10
    wb.save(low)
    res, lic = engine_run(tmp_path, monkeypatch, data=str(low))
    path = tmp_path / "MEIO_Results_low.xlsx"
    ms.write_report(res.sheets, str(path))
    H, t0 = res.data.horizon, 1 + res.data.prod_lead_time.min
    for tables in (mb.run_baseline(res, lic, skr=fake), mb.run_baseline_from_files(str(low), str(path), lic, skr=fake)):
        comp = tables["Comparison"].set_index("Measure")
        for col in ("Optimized", "Baseline (textbook s,S)"):
            ini = comp.loc[f"Lost demand, all channels, weeks 1–{t0 - 1} (fixed by the starting state)", col]
            con = comp.loc[f"Lost demand, all channels, weeks {t0}–{H} (weeks with fill-rate rules)", col]
            assert ini > 0 and comp.loc[f"Lost demand, all channels, weeks 1–{H}", col] == pytest.approx(ini + con)
        assert comp.loc[f"Lost demand, all channels, weeks 1–{t0 - 1} (fixed by the starting state)", "Optimized − baseline"] \
            == pytest.approx(0.0, abs=1e-9)                                      # the same for both plans


def test_the_engine_is_not_changed_by_the_baseline():
    """The baseline only reads the engine: its files keep their fingerprints (checked by test_engine.py section 6)."""
    import hashlib
    from pathlib import Path
    h = hashlib.sha256(Path(ms.__file__).read_bytes()).hexdigest()
    assert h == te.ENGINE_SHA256["meio_seeker_stochastic.py"]
