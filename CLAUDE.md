# CLAUDE.md: MEIO app

You build a **production-grade MEIO app** (multi-echelon inventory optimization) around a finished,
verified optimization engine. The engine is maintained in a separate claude.ai conversation, the
**engine session**. Your job is the app: data intake, running jobs, showing results, UX, deployment.
The supply-chain logic is not your job.

## 1. The files

The engine bundle lives in `engine/`, byte-exact. A new engine version replaces the folder's files wholesale.

| File | What it is | You may edit? |
|---|---|---|
| `CLAUDE.md` | this file: everything you need to know | yes (keep it current) |
| `engine/meio_seeker_stochastic.py` | **the engine** (v18.1, contract version 4): every rule, written once, as Seeker Terms | **no** |
| `engine/reference_simulator.py` | independent second implementation of the rules, used only by the tests | **no** |
| `engine/meio_stress_tests.py` | 26 data-change scenarios run through the engine (needs Seeker) | **no** |
| `engine/test_engine.py` | **the engine test suite** (141 tests): input control, physics & dynamics, model functionality, output control, stress cases, engine fingerprint; includes a Seeker test double, so no license needed | **no** |
| `engine/MEIO_Data_Template.xlsx` | the input file (v18.1 layout, **example data**; the tests depend on it) incl. sheet `Model_Assumptions`; workbooks from v17.x are rejected (handover B1) | **no** (real data goes in `data/`, gitignored) |
| `engine/MEIO_model_spec.md` | the dynamics/assumptions list: all 85 rules (IDs like C03, H05, K17), same as the Excel sheet | **no** |
| `engine/MEIO_math_model.md` | the formal model: sets, dynamics, objective, fill-rate rule, Seeker mapping | **no** |
| `engine/MEIO_shelf_life_aware_policies.md` | archived design of age-aware positions (rejected, K22); the waste warning (L10) refers to it | **no** |
| `engine/ENGINE_v18_1_HANDOVER.md` | the record of every engine change since v17.1 and why, what the app had to adapt, and the open proposals (section G) | **no** |
| `MEIO_Results_stochastic.xlsx` | results of a real Seeker run (sheet layout = engine output); on the user's PC only, gitignored | no |

The app's own files are listed in §9. Don't add files or docs beyond that list unless the user asks.
`.gitattributes` keeps every file byte-exact on Windows (`* -text`); without it, Git for Windows would turn LF
into CRLF on checkout and the engine fingerprint test would fail on the user's PC.

## 2. Hard rules

- **The engine files are read-only.** `test_engine.py` section 6 checks their fingerprints. If the app needs an engine
  change, follow §7. Never patch around an engine problem in the app.
- **One copy of every rule.** Never re-implement dynamics, costs, fill rates or data validation in the app. Every number
  the app shows comes from the engine's `RunResult`. The app may format, sort and compare those numbers, but not recompute them.
- **Input always goes through the Excel template and the engine's checks.** UI editing writes a **copy** of the workbook
  (openpyxl, values only; the original stays untouched), and the app never builds the engine's data object itself.
  The engine runs every Excel check in Python (rule K19), so an edited copy is checked as strictly as one checked by Excel.
- **One engine run per process** (settings and timings are module globals). Use a job worker or a subprocess per run.
- **Be honest with the numbers:**
  - Show the out-of-sample results as the headline.
  - Show the feasibility line and the bound NOTE prominently.
  - Show the rules missed on fresh scenarios.
  - Compare costs only between plans that meet the same number of rules (there is no lost-sales cost, so a plan that misses rules looks cheaper).
- **Checks stay green:** `python -m ruff check .` and `python -m pytest` from the repo root (141 engine tests,
  about 2.5 min, plus the app's own tests) on every change. CI runs both on Linux and Windows, plus a secret scan.

## 3. How to work (the user's method) and the user

**Method ("Disciplined Agentic Engineering"):**
- **Formulate before building.** Write a short app spec and get the user's OK before anything expensive or hard to reverse.
- **Plan → build → verify** as separate passes. Verify against the spec and tests, not by rereading your own code. For risky changes, use a fresh reviewer.
- **Prefer deterministic gates:** tests, types, linters, schema checks.
- **Verify in proportion to risk.** The numbers and commit-now quantities need end-to-end tests against the engine's output.

**The user:** Ruben, supply-chain planning, works in Europe/Berlin and writes in English.
- **Setup:** Windows PC, Spyder, Python venv (not conda) `C:\Users\ruben\seeker_env\Scripts\python.exe`, Python 3.13.15,
  packages pinned in `requirements.txt` (= his pip freeze), InsideOpt Seeker 2.1.0 (pip package `ioseeker` 0.2.5) with a
  license bound to that machine. GitHub Desktop pulls the repo. He runs things himself and pastes logs back.
- **Wants:**
  - concise and precise; plain language non-supply-chain people can follow; visuals welcome
  - **lean: few files, no clutter**
  - clear section labels in code; settings as named constants at the top
  - every assumption visible (Excel `Model_Assumptions`)
- **Asking:** only a few questions, and only where the answer changes what you build.

## 4. The business problem

One perishable product, **FG1 "Erdbeere Mix große Dose"** (strawberry mix, large can), made from four raw materials and sold through three channels.

```
Suppliers → RMW (raw-material warehouse) → KAJA (production) → DC → channels AMZ / D2C / B2B
RM1 can, RM2 tray: non-perishable, lead time 4 wk
RM3 strawberry: shelf life 52 wk, usable only with ≥ 36 wk left, LT 8 wk, max 1,000 kg/wk = 2,000 cans/wk (bottleneck)
RM4 pineapple: shelf life 52 wk, same freshness rule, LT 12 wk
FG1: shelf life 36 wk; production LT 6 wk; MOQ 6,000, batch 100, max 50,000; one start per week; price tiers
Fill-rate target: one per product, 95% for FG1, the same for every channel (Items!TargetFillRate)
Channels, served in this order (Channels!ShortagePriority): D2C (needs ≥ 16 wk left) → B2B (≥ 24) → AMZ (≥ 24)
Demand: 10 forecast quantiles per channel and week, ~1,400 cans/wk. Horizon 36 weeks, re-planned every ReviewPeriod weeks (template: 1).
```

The example data is generated; real data comes later.

**Outputs:**
- (s,S) per stock point and week
- the derived orders, transfers and production
- **commit now:** every decision of weeks 1..R (R = review period) as exact quantities; later weeks are indicative
- fill rates, stock, waste and cost

## 5. The model in one page

- **Decisions:**
  - a weekly (s,S) policy at the DC for FG1 (production orders)
  - an **echelon** (s,S) per raw material at the RMW (supplier orders); the echelon position = usable stock + open orders + BOM × the FG position
  - 244 integer variables
- **Weekly dynamics per scenario:**
  1. receipts arrive
  2. FG review and production (capped by MOQ/batch/max and by available raw materials, taken FEFO)
  3. raw-material review
  4. demand is served by channel priority, FEFO among cans fresh enough for that channel; unmet demand is lost
  5. holding cost
  6. ageing, then scrapping what no one can use
  - Lead times are fixed or random; orders never overtake.
- **Review period R (Settings!ReviewPeriod, default 1, A03):** the model is run every R weeks. A run commits every decision
  of weeks 1..R as exact quantities. Between runs the (s,S) rules use the *planned* position (position at the last run +
  orders planned since − expected demand since), so nothing inside a cycle reacts to the actual demand. R = 1 = weekly.
- **Objective:** minimize expected total cost: raw materials + production + transport + holding + waste. There is no lost-sales cost and no
  end-of-horizon value (user decision, A05): stock left after the last week is worth nothing, so plans run stock down near the end.
  Act only on the committed weeks; read the last weeks as indicative and total cost as spending within the horizon.
- **Constraints:** from week 7 (the first week a new decision can reach), per channel and week (a "cell"), the **expected fill rate**
  (mean over the scenarios of served ÷ demand) must reach the product's target. That gives 90 cell rules. There is no chance level.
- **Solving:** Seeker searches the (s,S) values on `NumScenarios` demand scenarios drawn from the forecast (template 200; use 500 for runs
  the user acts on). Then the same model is evaluated on 2,500 fresh scenarios (out-of-sample check).
- **Two modelling assumptions to keep in mind:** inventory positions are age-blind (K22: they count lots that expire before they are
  sold), and the futures are drawn independently per channel and week (G02: no correlation over weeks or across channels).
- **Classification:** a stochastic-service-type network (no guaranteed service times) solved by simulation-optimization. This is
  the right fit for perishability, lost sales, MOQs, capacity limits and per-week fill-rate rules; the classic formula methods can't handle these together.

## 6. Using the engine

```python
import meio_seeker_stochastic as engine
res = engine.run(data_file="MEIO_Data_Template.xlsx", license_file="license.sio", time_limit=250,
                 overrides=None)          # e.g. {"NumScenarios": 400, "RandomSeed": 7} for one run (K20)
```

`run()` executes the whole pipeline and writes nothing. It returns a `RunResult`:

| Field | Content |
|---|---|
| `data` | validated input. `data.channels` is sorted by priority; map channel index `ci` with `data.channels[ci].id` |
| `layout`, `x` | decision variables and the optimized values (`layout.index` = (stock point, week, "sigma"/"delta")) |
| `policy` | `{"FG", "RM1".."RM4": (s, S)}`, arrays indexed by week 1..H (index 0 unused) |
| `ins` | Seeker's own scenarios: `cost`, `parts`, `fr_mean[(ci,t)]` (expected fill rate per cell), `met`, `start{cost, met}`, `start_z{z0, z, steps}`, `status`, `feasibility` (one line), `repair` (`{"used": False}` or the repair record: steps, chosen step, `formula_start{cost, met}`) … |
| `oos` | fresh scenarios: `cost`, `("fr_mean"/"fr_full"/"fr_p10"/"fr_tail", ci, t)`, `(item, "mean"/"p10"/"p90"/"p_order", t)`, stock, waste; and `("band", series, item, week, stat)`, `("kpi", name, item, stat)` |
| `sheets` | report tables: `Summary`, `Commit_Now`, `Policy_sS`, `Decisions`, `FillRates`, `Stock_Waste`, `Bands`, `KPIs` (columns frozen in `test_engine.py`) |
| `timings` | (phase, seconds) |
| `overrides` | the run overrides applied, or None |
| `oos_start` | the start policy on the same fresh seeds (only with `START_POLICY_OUT_OF_SAMPLE = True`), else None |

**Uncertainty and flows (sheet `Bands`, long format: Series, Item, Week, Mean, P05, P10, P25, P50, P75, P90, P95;
all on the fresh scenarios; definitions in rules L05–L06):**

| Series | Items | Meaning |
|---|---|---|
| `end_stock` | FG1, RM1–RM4 | stock at week end, before the end-of-week scrapping (a lot's last usable week still counts, F06) |
| `position` | FG1, RM1–RM4 | the quantity s and S act on: FG stock position (C01), raw-material echelon position (C03). Draw s/S against this, not against end stock |
| `waste` | FG1, RM3, RM4 | units scrapped that week |
| `order_qty` | FG1 (production), RM1–RM4 | order quantity (0 when nothing is ordered) |
| `arrival_week` | FG1, RM1–RM4 | arrival week of the order placed that week, **only over scenarios that place it**; empty if never placed |
| `receipts` | FG1, RM1–RM4 | units arriving at the start of the week |
| `served`, `lost` | channels | units sold, demand lost (served + lost = demand) |
| `eligible_next_week` | channels | DC stock at week end the channel can still take next week |
| `rsl_mean` | FG1 | stock-weighted mean remaining shelf life of DC stock (Mean = ratio of totals; quantiles count 0 when there is no stock) |
| `no_stock_share` | FG1 | share of scenarios with no DC stock at week end (Mean column only) |

**KPIs (sheet `KPIs`: Item, Unit, Mean, P10, P90; rule L07):** average end stock per item, total waste (FG1, RM3, RM4),
lost sales per channel split into weeks 1–6 (initial state, no decision can change them) and weeks 7–36 (weeks with
fill-rate rules), shares of scenarios with any waste or lost sales. For rare events the mean can lie outside P10–P90 (e.g.
P10 = P90 = 0 and mean > 0); show the mean together with the share of scenarios. With `START_POLICY_OUT_OF_SAMPLE` also:
start-policy cost and rules met on the same fresh seeds and the fresh-vs-fresh saving (rule L08).

**Other entry points:**
- `engine.write_report(res.sheets, path)` writes the formatted results workbook.
- `engine.main()` = `run()` + workbook + console (the user's F5 workflow).

**Errors are exceptions:**
- `ValueError` "Data validation failed:" followed by every problem, one per line, each naming sheet, row and field (show them to the user).
  Warnings (e.g. a max order quantity that is not a multiple of the order step) don't stop the run: they are in `res.data.warnings` and the Summary.
- `FileNotFoundError` for a missing data or license file
- `ImportError` when Seeker is missing

**Settings** are module constants. Set them before `run()`:

| Constant | Default | Meaning |
|---|---|---|
| `TIME_LIMIT_SECONDS` | 250 | search time |
| `OUT_OF_SAMPLE_SCENARIOS` | 2500 | fresh scenarios for the check |
| `FEASIBILITY_CHECK` | True | about 25 s |
| `START_POLICY_OUT_OF_SAMPLE` | False | also evaluate the start policy on the same fresh seeds (+~75 s) |
| `OPTIMIZATION_FILL_RATE` | None | None = Items!TargetFillRate; a number replaces it for the whole run (search, start, checks, report; K08). `res.data.fill_rate_target` is the target used, `res.data.fill_rate_target_excel` the Excel value |
| `START_Z` | None | None = start safety factor from the target (normal loss function), raised by the start check; a number fixes it (K07) |
| `START_REPAIR` | True | repair an infeasible start before the search (K21); runs only when needed |
| `FILL_RATE_CONSTRAINT_FORM` | "mean" | "mean" = rule E (H05); "none" switches the rules off (diagnostic). Use only these two values |
| `WASTE_WARNING_SHARE` | 0.05 | waste warning limit (L10) |
| `HORIZON_MARGIN_WEEKS` | 8 | input warning X10 if horizon < echelon lead time + R + this (A02); the Excel Checks row X10 uses the same number |
| `EXTRA_COVER_WEEKS` | | upper bounds of the reorder points; raise the one named in a bound NOTE |

**Two lines the user must always see:**
- **Feasibility check:** "OK", or "N rule(s) fail whatever we decide: …". These are weeks the supply physically cannot reach.
- **Bound NOTE:** "none", or which setting to raise because a reorder point hit its upper bound.

**More things to show when they occur:**
- **Start repair (K21):** normally "not needed". If the console says `START REPAIR USED`, the formula start missed rules that
  can be met and Seeker started from a repaired, feasible but expensive plan; the Summary rows "Start repair" and "Start
  policy before repair" say which step was used. Show it as a notice, not an error.
- **Waste warning (L10):** `WARNING (L10)` / Summary row "Waste warning" when expected waste exceeds 5% of expected demand.
  The Summary also has "Expected waste <item>" rows for FG1 and each perishable raw material.
- **Data warnings X08/X09/X10 (K19):** open orders that arrive later than their lead time allows, MOQs larger than one fresh
  lot can sell/use before it expires, and (X10) a horizon too short for the review period and lead times. They are in
  `res.data.warnings` with the other warnings and never stop a run.

**Runtime on the user's PC (v18.1):**
- **Fixed parts:** feasibility check 20–30 s, model build 8–13 s, out-of-sample check 70–95 s.
- **Start check:** each z step costs 20–25 s with 200 scenarios and 30–40 s with 500.
- **Search:** the time limit + 4–15%.
- **Totals:** 200 scenarios with 300 s search ≈ 10 min; 500 scenarios with 700 s ≈ 19 min.
- **Progress:** run it as a background job and stream its stdout as progress: phase lines `[hh:mm:ss] …` and Seeker's
  `At time …` lines, which come irregularly and can pause for minutes.
- **The worker machine must not sleep:** one 300 s search ran 12,100 s on 12.11., most likely because the PC slept.

**License:** the Seeker license is bound to one machine, so the optimization worker must run there. Settle the deployment with the user first.

**Seeker-free development:** importing `test_engine.py`'s test double (`sys.modules["seeker"]`) lets the whole pipeline run without a license.
Its numbers are real model numbers, but its search is trivial.

## 7. Changing the engine

When the app needs something from the engine (a rule, an output, a setting, a bug fix, multi-product, a database loader):
1. Write the request in the chat to the user under the heading **"Engine change request"** with:
   - what the app needs, in business terms
   - the proposed change and the affected rule IDs
   - the impact on `RunResult` and the output tables (breaking or not)
   - for bugs: a minimal `run()` reproduction and the log
   - acceptance criteria
2. The user takes it to the engine session.
3. The engine session returns new engine files (version bumped, tests and fingerprints updated). Replace them wholesale, then run all tests.
4. Meanwhile, build against the current engine and mark the dependent part as pending.

## 8. Status, decisions, known facts

**Latest numbers (user's PC, template, target 95%, 12.11.):**

| Engine and settings | Start | Seeker in-sample | Fresh (2,500) |
|---|---|---|---|
| v18.0, 500 scenarios, 700 s | z 1.22 → 3.00, €113,804 | €112,735, 90/90 | €112,753, **90/90**; weakest AMZ week 36 at 96.3% |
| v18.1, R = 1, 200 scenarios, 300 s | z 2.50, €110,739 | €105,863, 90/90 | €105,878, 88/90; AMZ week 36 at 88.5% (fitted to the 200 planning futures, see 'How to read one fill-rate cell') |
| v18.1, R = 2, 200 scenarios, 300 s | €110,422 | €104,935 | €105,033, 89/90 |
| v18.1, R = 4, 200 scenarios, 300 s | z 3.00, €113,876 | €105,818 | €106,028, 89/90 |

- **Week-1 decision in every run:** no production; a 1,000 kg strawberry order arriving in week 9.
- **Seeker savings vs. the start:** small (0.9–7%). The search often stalls after 2–3 minutes. The large theoretical saving is
  the end-of-horizon run-down (A05), which is not a real saving.
- **Short runs:** about 25 s fixed overhead, so a 30 s search takes about 55 s.
**Through the local API:** v16 (07.11.) and v17.1 (08.11.) gave €112,128.16 and 88/90 on fresh scenarios, identical
to F5. **v18.1 (13.11., user's PC, 120 scenarios, 120 s, F5):** start z 1.22 → 2.50 in 3 steps of 12–14 s, €105,848 and
88/90 on fresh scenarios (B2B and AMZ week 36; weakest AMZ week 36 at 89.0%), about 4.8 min in total. The API's time
estimate (`api/server.py`): 20 s + 0.1 s per optimizer scenario + 21 s feasibility + 3 z steps of (8 s + 0.06 s per
scenario) + 12 s second build (+ 6 steps if a repair runs) + limit × 1.1 + 10 s + 0.033 s per fresh scenario; it follows
the `4. Start check … z = …` and `4c.` lines in the log (counted, never keyed by phase name: names repeat).


**v17 (08.11., change request "uncertainty, flows and what-ifs"):** report series, arrival weeks given an order, KPIs,
run overrides, optional start policy on fresh scenarios, and every Excel check in Python. The search model is unchanged:
the same Seeker calls as v16 (13,102 Terms on the template), so the search finds exactly the same plan. All v16 numbers are
identical except the arrival week of orders that are never placed (v16 showed a made-up week, v17 leaves it empty).
Confirmed on real Seeker 2.1 on the user's PC: the same improvement path as v16 (5,126 evaluations → €112,013.35;
6,282 → €111,783.95); the out-of-sample check takes 78 s instead of ~75 s; all 1,346 band rows are ordered P05 ≤ … ≤ P95;
the stock balances on the means close exactly. v17.1 fixes a file lock: the engine now closes the workbook after
reading, so the app can overwrite or delete its copies on Windows.

**v17.2 (10.11., engine session; confirmed by the user's Seeker run 11.11.):**
- Start policy (K07) with a lot cap (a lot never larger than one fresh lot can sell, unless the MOQ forces it) and a
  raw-material lot guard (raw-material reorder point ≥ BOM × (FG reorder point + production MOQ)). On the template the guard
  raises 4 raw-material reorder points in the last weeks, so the start and the saving baseline differ slightly from v17.1.
- Start check and repair (K21): section 5 evaluates the start; only if it misses rules that can be met, section 4c makes it
  feasible step by step (R1 bounds, R2 maximum supply before the missed weeks, R3 maximum supply everywhere) and Seeker
  starts from that. Not used on the template.
- Chance level (K08): a value in section 0 replaced the Excel level everywhere (superseded in v18.0 by `OPTIMIZATION_FILL_RATE`).
- Input warnings X08/X09 (K19), expected waste rows and warning (L10).
- Inventory positions stay age-blind (K22, user decision 10.11.); the shelf-life-aware design is parked in the project doc
  `MEIO_shelf_life_aware_policies.md`.
- The maximum-supply policy (K17) uses the smallest reorder point that still always orders the maximum; same decisions.
- New template columns: Channels `FreshLotSales`, Open_Orders `WeeksLate` (formulas). Since v18.0, workbooks from v17.x are
  rejected (Items!TargetFillRate missing); see `ENGINE_v18_1_HANDOVER.md`, B1.

**v18.0 (12.11., engine session; contract version 3; confirmed by the user's Seeker runs 12.11.):**
- **One fill-rate target per product** (Items!TargetFillRate, FG rows only), the same for every channel (H04).
  Channels!TargetFillRate is gone; **Channels!ShortagePriority is now an input** (1 = served first, ranks 1…n once, H02).
  Settings!FillRateChanceLevel is gone; `OPTIMIZATION_CHANCE_LEVEL` became `OPTIMIZATION_FILL_RATE`.
- **Fill-rate rule E (H05):** per channel and week the expected fill rate (mean over the scenarios) ≥ target. Reason: with
  the old chance rule a short week was nearly always empty, so lowering the target saved nothing. Re-optimized (NumPy):
  98 / 95 / 90% → €107.6k / €105.5k / €99.6k.
- **Target-aware start (K07):** z₀ from the target via the normal loss function; the start check raises z on a 0.5 grid
  until every cell is met, then the repair (K21) only if needed. Template: z₀ 1.22 at 95% (0.86 at 90%, 1.63 at 98%).
- **FillRates sheet (contract 3):** per cell `Target`, `Constrained`, `Expected FR in-sample (N Seeker scenarios)`,
  `Status in-sample` (OK/VIOLATED), `Expected FR (N fresh scenarios)`, `Status out-of-sample` (OK/BELOW TARGET),
  `Fully served (N fresh scenarios)`, `FR in a bad scenario, P10 (N fresh scenarios)`, `Worst 5% of futures, average FR (N fresh scenarios)`.
- 13,099 Terms on the template (3 constants fewer than before).

**v18.1 (12.11., engine session; contract version 4; confirmed by the user's Seeker run 12.11.): the review period is a real input (A03).**
- **Settings!ReviewPeriod = R** (template default now 1): the model is run every R weeks; each run commits weeks 1..R.
  In the weeks between runs the order rules use the planned position (see section 5). R = 1 reproduces v18.0 exactly
  (regression test); with R = 2 the template results change against v18.0, because week 2 no longer reacts to week-1 demand.
- **Commit_Now (contract 4):** columns `When`, `Decision`, `Quantity`, `Note` (was `Quantity / rule`). One block of rows per
  week 1..R (production order, the RMW → KAJA transfers, supplier orders, with arrival weeks); all exact numbers, no more
  `Rule …` rows. A production row can say "raw material permitting (P10 …, P90 …)" when uncertain lead times may cut it.
- **Decisions sheet `Status`:** weeks 1..R `COMMIT NOW (exact)`; later run weeks `Indicative — re-optimized at the run in
  week t`; weeks between runs `Indicative — planned at the run in week r`.
- **Policy_sS:** new column `Planned at run week` (after `WeekStart`).
- **Summary:** new row `Review period (A03)`, e.g. "R = 4 weeks: the model is run every 4 weeks; this run commits weeks 1–4
  (…); next run in week 5". Show it next to the commit table.
- **Input warning X10** (see above). The start policy's safety stock counts the uncertainty since the last run (K07).

**How to read one fill-rate cell (show this in the app):** row AMZ, week 12, target 95%:
- `Expected FR 96.4%` → averaged over all futures, Amazon gets 96.4% of its week-12 demand. The rule (H05) judges this
  number: 96.4% ≥ 95% → `OK`. Example of how it arises with 100 futures: 94 fully served, 3 at 80%, 3 at 0%.
- `Fully served 94%` → in 94 of 100 futures Amazon gets all of its week-12 demand; in the other 6 some of it is lost.
- `FR in a bad scenario, P10 100%` → even the 10th-worst of 100 futures is fully served.
- `Worst 5% of futures, average FR 32%` → in the worst 5 of 100 futures Amazon gets on average a third of its week-12
  demand. The rule judges the average, so it does not limit this; the column shows how deep the bad futures go (typically
  an empty DC before the next lot). The Summary row `Weakest cell (out-of-sample)` names the weakest cell with its tail.
- The in-sample column is what Seeker optimized against (its own futures); the fresh column is the honest check on
  futures Seeker never saw. Well below the target on fresh futures means the plan was fitted to the planning futures. On
  the template this happens in the last weeks with 200 scenarios (AMZ week 36: 88.5–92.9%); with 500 scenarios it disappeared.
- Weeks 1–6 are `Constrained = no (initial state)`: no decision can change them; they are shown, not judged.
- All these figures assume futures drawn independently per channel and week (G02). If real demand moves together
  over weeks or across channels, short weeks cluster and the true fill rates are lower; open topic for the forecasting tool.

**Decisions and why:**
- **Rules written once in Seeker; no second simulator in the engine** (the reference exists only for tests). This avoids errors from duplicate logic.
- **One formula start policy**, its z from the target (v18). Seeker 2.1's `set_value` re-evaluates the whole model per value, so only a few starts are tried (the z steps of the start check, 20–40 s each). A start that still misses reachable rules is repaired before the search (K21); a test of 73 data variants (12.11.) showed the z steps alone are not always enough, so the repair stays.
- **Bounds come from the forecast and are easy to raise.** RM3 and FG were raised from 4 to 8 after they hit their bounds.
- **No lost-sales cost and no end-of-horizon rule.** That is the user's choice (A05, kept 12.11. for leanness; alternatives: an end-stock credit or an end-stock target).
- **The feasibility check exists** because stress tests showed weeks the supply can't reach.
- **Seeker settings:** speed 0 tested best; the fill-rate rule is one `aggregate_mean` per cell (v18).
- **Multi-product is deferred but planned.** Don't hard-wire a single product in the app's data model.
- **The engine session also has a solver-free NumPy optimizer** (`meio_simopt.py`). It isn't part of this bundle and still uses the old chance rule of v17 (not updated to v18).

**Seeker facts (2.1.0):**
- Reading single numbers (aggregates) is instant; per-scenario lists cost 0.5–3 s each, so the engine never reads them.
- Seeker reshuffles the demand samples across its scenarios, so its scenarios exist only inside Seeker.
- From an infeasible start Seeker often makes no progress.
- It overruns the time limit by about 4–15%, and often stops improving after 2–3 minutes (12.11.: no improvement from 146 s to 788 s).
- If the results file is open in Excel, the engine writes a time-stamped name.

**Stress tests (06.11.):**
- No dynamics bug was found.
- Some cases are physically impossible in some weeks: demand +30%, cancelled strawberry orders, strawberry capacity 500 kg. The feasibility check now reports these.
- Earlier red verdicts came from running at chance level 0.95 while judging at 0.99, and from comparing costs of plans that met different numbers of rules.
- The stress tests search from the formula start (no repair), so their verdicts stay comparable with v17.1.

**Open:**
- Rules still "Proposed": A04, D05, F02, G02, G03, H03, H06, H07, I07, J03, K07, K20, K21, L05–L08, L10 (see `MEIO_model_spec.md`).
- Engine proposals that the user hasn't signed off yet (none is built): see section G of `ENGINE_v18_1_HANDOVER.md`, e.g. a check
  on `FILL_RATE_CONSTRAINT_FORM`, the K05 bound wording, an "unavoidable waste" row, the exact forecast mean, and a whole-unit check.
- Correlated demand (G02) is open for the user's forecasting tool.
- Known limitation J04: a lot that is already unusable when it enters (initial stock or open order) shows up for one week in
  end stock and holding before it is scrapped. It doesn't occur in practice.
- Seeker 2.2 is untested.
- The data is example data.

## 9. The app: decisions, files, plan (agreed with the user 06.–07.11.)

**Architecture:**
```
Browser → Lovable web app (own repo, made by Lovable; shows only API numbers, never computes)
        → ngrok tunnel (free fixed domain; requests send header ngrok-skip-browser-warning)
        → local API on the user's PC (FastAPI, localhost:8000, every request needs the token)
        → one subprocess per engine call (validate or run) → runs/<id>/ input.xlsx, request.json (settings),
          status.json, label.json (label/note), log.txt, input.json (master data), result.json, results.xlsx
```
- Data source: the Excel upload only (the user edits in Excel). A database comes later, through the one data-source
  function in `api/worker.py` and an engine change request (§7) for a loader.
- Editing data: since v17 (rule K19) the engine runs every Excel check in Python and treats a missing cached OVERALL as
  "unknown", so the API may write what-if edits into a **copy** of the uploaded workbook (openpyxl, values only).
  The original upload is never changed. (v16 rejected such copies with "Checks shows OVERALL = nan".)
- **Python UTF-8 mode (`PYTHONUTF8=1`) everywhere:** CI sets it, the user set it with `setx`, and the worker must set it
  in its subprocess env. Without it Windows uses cp1252: `engine/test_engine.py:822` `read_text()` cannot decode the
  engine source (CI failure 07.11.), and piped `print` of `→` (bound NOTE) raises UnicodeEncodeError. The engine's
  `read_text(encoding="utf-8")` fix is part of v17.1; UTF-8 mode stays for the worker's piped output.
- **Test double in the app:** `MEIO_TEST_DOUBLE=1` makes `api/worker.py` use the Seeker stand-in from `test_engine.py`
  (tests only). Results then carry `test_double: true` and `seeker_version` "FAKE …", and `/health` reports it.
- **Windows:** replacing a file another process is reading fails, so both `write_json` functions retry for up to 1 s.
- Users: the user only, one token. Run history = folders in `runs/`, no database.
- FillRates column names contain the scenario counts: match columns by prefix. Read the feasibility line from
  `res.ins["feasibility"]` and the bound NOTE from `engine.bound_note(res.layout, res.x)`, not from Summary labels.

**Repo files (the complete list; keep it lean):**

| File | Purpose | PR |
|---|---|---|
| `README.md` | the user's guide: what the app does (acceptance criteria), setup, daily use, updating | 1 |
| `CLAUDE.md` | this file | 1 |
| `.gitignore`, `.gitattributes` | secrets/data/runs out of git; files byte-exact on Windows | 1 |
| `requirements.txt` | exact package versions = the user's venv | 1 |
| `pyproject.toml` | ruff (incl. complexity ≤ 10) and pytest settings | 1 |
| `.github/workflows/ci.yml` | ruff + pytest on Linux and Windows, gitleaks secret scan | 1 |
| `engine/` | the bundle, unchanged | 1 |
| `api/server.py` | endpoints, token, CORS, run history, starts the worker; never imports the engine | 2 |
| `api/worker.py` | one engine call per process: validate or run → result.json + results.xlsx | 2 |
| `api/test_api.py` | the acceptance scenarios, with the Seeker test double; coverage gate ≥ 90 % on `api/` | 2 |
| `api/openapi.json` | frozen API contract (snapshot test); what Lovable builds against | 2 |
| `start_worker.bat`, `local_settings.example.bat` | start API + ngrok; template for token, license path, ngrok domain | 3 |

**Status 07.11.:** PR 1 (foundation), PR 2 (local API) and its Windows fix merged; real Seeker run via the API OK.
GitHub PR #4 = start script + ngrok (the "PR 3" step of the file list; #3 was the Windows fix). `start_worker.bat` runs ngrok as
`ngrok http 127.0.0.1:8000 --url=https://<domain>` (127.0.0.1, not localhost: Windows may resolve localhost to IPv6)
in a second window; its settings checks are tested on Windows CI (`MEIO_NO_PAUSE=1` skips `pause`).

**Status 07.11. (evening):** PR #4 merged; ngrok set up; phone check and the three dashboard connection checks passed
(green badge, wrong token → message, API closed → red within 15 s).

**The dashboard (Lovable):** repo `Paym53/meio-planning-dashboard` (TanStack Start + React + Tailwind/shadcn, bun;
Lovable-managed: its AGENTS.md forbids force-push/history rewrites; push only working states). Lovable read
`api/openapi.json` + `api/server.py` read-only and built `src/lib/api.ts` against them. Claude writes the prompts;
the user pastes them into Lovable; Claude reviews the repo against the 7 Knowledge rules (rules block given 07.11.:
never compute numbers, token/address only in localStorage, both headers, plain English, honesty rules, TEST MODE
banner, one API client and no backend/Supabase/GitHub). Review 07.11.: all 7 rules hold; API calls happen in the
browser only (SSR gets no token). Open: fonts load from fonts.googleapis.com (GDPR) → self-host (asked in Prompt 2).
This session cannot install the dashboard's packages (blocked), so its tests are verified only by CI.

**Deferred to the end (user decision 07.11.):** dashboard CI (`.github/workflows/ci.yml`: bun install, vitest, tsc,
lint, build) **and** the full UI test suite, added together once all screens exist. Collect the tests per screen here:
- Connect: badge green/amber/red; wrong token → "Missing or wrong token."; offline message; TEST MODE banner. (14 tests exist)
- New run: validation errors shown word for word and complete; Start disabled when invalid; 409 → message + link;
  progress steps from `phase`; time left from `seconds_left` only; cancel; end states done/invalid/failed/cancelled.
- Results (next): headline = out-of-sample cost + "N of M rules met"; feasibility line and bound NOTE above the fold;
  missed rules listed; every table cell = API value (no computed numbers); workbook download.
- Runs/compare (AC4): costs compared only when rules_met_out_of_sample AND fill_rate_target are equal; otherwise a
  warning, no difference.
- Overview: KPI strip = headline fields; fresh saving from KPIs row when present, else in-sample (labelled); heatmap
  status from FillRates "Status out-of-sample", fixed weeks before run_info.first_rule_week; KPIs rows by name.
  Fixed weeks (Status "info") show their share in grey without ✓/✕. Feasibility "not run" → "Not checked".
  Decisions to take now (contract 4): Commit_Now columns When / Decision / Quantity / Note, one block per committed
  week 1..R; cards for every quantity > 0; why in week 1 = Bands "position" week 1 Mean vs Policy_sS s/S + input order
  limits; later committed weeks = planned position (A03), no position number. Channel rows from FillRates (serving
  order), never a fixed list. Service grid: value = "Expected FR (" (fresh), status OK / BELOW TARGET, not judged before
  first_rule_week, end of horizon from the first "FG active" = "fixed 0" week, tooltip = §8 one-cell explanation.
- Supply-chain map (React Flow): nodes = input.tables.Locations (LocationID, Type) only; supplier edges from Sourcing
  (SupplierID → DestinationID, label "RMx · LT"), others from Lanes; runs without Sourcing show a hint. Panels:
  bars only for order_qty and receipts, other series as band/line charts; open orders keys qty/placed/arrival;
  channel fill rate from the column "Expected FR (" (fresh, not "Expected FR in-sample ("); target = the product's
  (headline.fill_rate_target); "fixed weeks" shading only on DC and channel charts; KPI unit "share of scenarios" as %.
- Orders: rows = (stream, week) with P(order) > 0 + open orders; quantity labelled "avg. over all futures (incl. no
  order)"; arrival P50 (P05–P95) only over futures that order; open orders "before W1 (week −3)"; review cards
  formatted with the item unit.
- Decisions/Orders: (s,S) from wide Policy_sS (`FG s`/`FG S` exact case, production point "FG"); badges only from
  Decisions.Status ("—" otherwise); units from KPIs "Average end stock <item>," Unit; arrival from Bands
  arrival_week ("never ordered" when empty); grey only rows where every "<point> active" is "fixed 0".
- Run dialog: optional form fields never sent when empty (null, undefined or ""); presets Decision run (500 / 700 s,
  default) and Quick what-if (100 / 150 s); fill-rate target entered in %, sent as a share; empty = Excel.
- Inventory: s/S only on Bands series "position"; served/lost/eligible_next_week read per channel (Item = channel id);
  units from the engine; no_stock_share = Mean only.
- Master data: demand bands by level position in demand.quantiles[channel][week][level] (Q05–Q95, Q25–Q75, Q45–Q55,
  no median); lead-time bars pair lead_time.weeks[i] with probabilities[i]. Nine subtabs (/master-data/<slug>);
  no solver settings there (time limit, scenarios, seed live only in the Run dialog / Solver settings); non-perishables
  "no expiry", never "0 weeks"; stock "usable for" chips = remaining shelf life ≥ channel / production minimum.
  Model assumptions are on Explainability, not Master data.

**Decisions 08.11. (user):** single user (one token, no per-user login); no floating assistant; order of work:
**engine v17 first → API update → Lovable prompts** (build pack v2: Knowledge rules A–J, design-shell Prompt A, page
specs with data sources ✅ now / 🔧 API / 🧩 engine v17; left sidebar, right side = 480 px drill-down panel).
UI rules from the review of the user's app concept: never draw s/S on on-hand stock without the "stock position" note
(s,S act on IP_FG / echelon IP); no invented "why" texts (explain decisions with s, S, P(order) and arrival weeks only);
no "transport decision" (transfers/transport follow production, B02/I04); forecast has no median (Q45–Q55 centre band);
weeks before the first constrained week are "Fixed weeks", not "warm-up"; KPI totals/averages come from the engine.
Comparing scenarios: same seed (common random numbers) and same rules met; different seeds = robustness test.

**Engine change request v17 (sent 08.11., contract version 2; delivered as v17.1 on 08.11., see §6/§8):** weekly bands (mean, P05–P95) on fresh scenarios for
FG/RM end stock, FG stock position, RM echelon position, waste, order quantities, receipts, served and lost per channel,
stock eligible per channel, mean remaining shelf life; arrival weeks (P05/P50/P95) for every order week; KPI block;
optional START_POLICY_OUT_OF_SAMPLE; `run(..., overrides={"NumScenarios", "RandomSeed"})` applied before validate();
edited (openpyxl-saved) workbooks pass validate() when the cached OVERALL is missing; new sheets `Bands`, `KPIs`;
new aggregates only in the out-of-sample model; acceptance incl. conservation tests and an unchanged Seeker
improvement path (5,126 → 112,013.35; 6,282 → 111,783.95). Already in v16, so done in the API: cost parts
(`ins["parts"]`, `oos[("part", n)]`).
**API update 1 (08.11., result contract 2, API version 2):**
- `POST /validate` → `{valid, errors, warnings, input}`; `input` = the master data as `engine.load_data()` read it
  (`worker.describe_input`: settings, product + production + tiers, raw materials, channels in serving order, demand
  quantiles per channel and week, initial stock lots, open orders, warnings, and the template sheets Locations, Lanes,
  Items, Model_Assumptions read with the engine's `_sheet`). Same JSON per run: `GET /runs/{id}/input` (409 if unreadable).
- `POST /runs` form fields: `time_limit`, `out_of_sample_scenarios` (500–20,000), `feasibility_check`,
  `start_policy_out_of_sample`, `optimization_chance_level` (the worker refuses a level below the Excel
  FillRateChanceLevel as a data error), `num_scenarios` / `random_seed` (→ `run(overrides=…)`, checked by the engine),
  `label`, `note`. Settings go to `request.json` → worker sets the engine constants (one run per process). status.json
  records `settings` and `file` (name, sha256, bytes); `PATCH /runs/{id}` sets label/note in `label.json`.
- Result contract 2: headline + `saving_vs_start_in_sample` (engine Summary), `cost_parts_in_sample` /
  `cost_parts_out_of_sample` (`ins["parts"]`, `oos[("part", n)]`); new `run_info` (product, horizon, review period,
  `first_rule_week`, start date, time limit, scenarios, seed, chance levels, fresh scenarios, switches, overrides,
  warnings); sheets incl. `Bands` and `KPIs` passed through unchanged.
**Verified 08.11. on the user's PC:** 168 tests pass on Windows (9 min); a real run through API v2 with NumScenarios
100 (the `/docs` page pre-fills optional form fields, so it sent overrides): €107,695 and 86/90 on fresh scenarios, 372 s
(vs €112,128 and 88/90 at 200 scenarios: cheaper because it meets fewer rules, so not comparable; AC4). The estimate
ignores NumScenarios (feasibility and build scale with it): fine for now. For default runs without the UI, use
`curl.exe -F file=@…` instead of `/docs`.
**Restart after a pull:** the API loads its code once; a pull while it runs left the old server calling the new worker
with old arguments (08.11.). Now `/health` returns `restart_needed` (mtimes of server.py, worker.py and the engine
changed since start) and the worker answers an old-style call with "restart start_worker.bat".
**Lovable build pack v3 (08.11., given to the user):** Knowledge rules A–K (v2 rules + F: s/S only on "position"
charts; J: restart_needed banner) and prompts in this order: 1 design shell · 2 Run optimization dialog + progress
(send optional form fields only when set) · 3 Overview · 4 Decisions · 5 Inventory dynamics · 6 Orders · 7a Master
data read-only (7b editing after API update 2) · 8 Solver settings · 9 Scenario comparison + Run history ·
10 How to use + Explainability (static texts from Claude). After each prompt: the user checks, Claude reviews the
dashboard repo against the Knowledge rules and adds the screen's tests to the list above.
**Dashboard progress (08.11.):** compact prompt sequence given in chat (Knowledge 1–8 + prompts 1–8: shell; run
dialog; Overview; Decisions + Orders; Inventory; Master data + Solver settings; Run history + comparison; How to use +
Explainability with verbatim texts), credit-saving: one API read in prompt 1, no Lovable tests, small fixes by Claude
via PRs in the dashboard repo (user must not prompt Lovable while such a PR is open). Prompt 1 ≈ 10 credits.
Prompts 1–4 done and checked by the user; review fixes: dashboard PR #1 (Policy_sS lookup, invented badge fallback,
units, (s,S) greying) merged. Prompt 5 (Inventory) → fix PR #2 (series "position", per-channel flows, units) merged.
Prompt 6 (Master data + Solver settings) → fix PR #3 (demand quantile layout, lead-time pairs). Lovable keeps guessing
field names: the fixes always come from the real /input and result layouts (worker.describe_input, engine sheets).
**Overview rework (09.11., user notes):** status line (targets reachable? + small bound-note and review chips), KPI
strip, "Decisions to take now" (week-1 action cards are certain: decided before demand; week-2 rules with P(order)),
number heatmap (shares, not counts), small cost and run-quality cards, Risk section removed; supply-chain map rebuilt
with React Flow (Prompt B) incl. a lead-time ladder (each segment = its own lead time, stochastic = range + the given
probabilities, no summed total). **User-approved exception to "never compute":** on the Overview, for the selected
week range, the mean of the Bands "Mean" values (FG1 end_stock) and their sum (FG1 waste) may be shown, labelled
"mean of selected weeks" / "expected total in selected weeks" (exact for means by linearity); never for quantiles.
Not in the model, so not shown: capacity overrides, closed weeks, cut reasons, forecast mean/std, RM remaining shelf
life. The map needs `input.tables.Sourcing` (supplier → material), added on 09.11.; `input.json` is written when a
run is made, so runs before 09.11. have no supplier arrows (start a new run).
**Dashboard progress (09.11.):** Overview, map + panels, Orders and Master data reworked and merged (dashboard PRs #4–#10;
Lovable prompts A, B, map rebuild, Orders, Master data; Claude's review fixes in the same PRs). One place per input:
solver settings only in the Run dialog. Open idea (not requested yet): engine output of the order quantity **only over
futures that order** (like arrival_week), so Orders can show "if ordered: …" instead of only the all-futures mean.
**Engine v18.1 + API 3 (13.11.; handover `engine/ENGINE_v18_1_HANDOVER.md`):** result contract 4 = engine contract 4.
- **User decisions 13.11.:** the run's own fill-rate target (`optimization_fill_rate`, any value in (0, 1], → engine
  `OPTIMIZATION_FILL_RATE`, set before `run()` and restored afterwards); run-dialog presets "Decision run" (500 scenarios,
  700 s, default) and "Quick what-if" (100 scenarios, 150 s; never act on it); runs of older engines are not shown
  (`status.json` `api_version` ≥ 3; the user empties `runs\` once). Engine session (13.11.): keep the venv; don't expose
  `FILL_RATE_CONSTRAINT_FORM` (any other value than "mean"/"none" silently runs "mean", proposal G1).
- **Worker:** keeps Windows awake during a run (`SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)`, cleared
  after); `/input`: `product.fill_rate_target` (Items!TargetFillRate), channels `serving_order` = ShortagePriority, no
  `chance_level`/`target_fill_rate`; headline adds `fill_rate_target`, `_excel`, `_source` (engine `target_source`),
  `weakest_cell`, `review_period`, `start_policy`, `start_check{z0, z, steps}`, `start_repair_used`, `start_repair`,
  `start_policy_before_repair`, `expected_waste{item: text}`, `waste_warning` (engine Summary texts, copied);
  missed rules carry `expected_fill_rate`, `fully_served`, `bad_scenario_p10`, `worst_5pct_average` (FillRates prefixes
  "Expected FR (", "Fully served (", "FR in a bad scenario, P10 (", "Worst 5% of futures, average FR (", status
  "BELOW TARGET"); run_info: `fill_rate_target`, `fill_rate_target_excel`, `optimization_fill_rate`.
- **Display rules for the dashboard (handover E, C7):** fresh-scenario status as the headline; weeks before
  `first_rule_week` "not judged" (H06); the last weeks "indicative (end of horizon)" (A05); the `Weakest cell` row and the
  one-cell explanation of §8; target used + source + scenarios; commit table for weeks 1..R next to the `Review period`
  row; notices: start repair, waste warning, data warnings X08–X10 (never errors); never add up Bands quantiles across
  weeks or channels (only Mean adds up; totals with P10/P90 are in KPIs).
- **Verified 13.11. (cloud, test double):** 141 engine + 51 app tests, coverage 98 %; the worker's missed rules on the
  user's real v18.1 FillRates = B2B and AMZ week 36, 88/90, as the engine's Summary.
- **Verified 13.11. on the user's PC:** all tests pass on Windows (197, coverage 98 %); first real Seeker run through
  API 3 (`curl`, 120 scenarios, 120 s): €105,847.76 and 88/90 on fresh scenarios = the F5 workbook exactly; 4 min 52 s
  (estimate 5.6 min). MEIO_App PR #10 and dashboard PR #12 (contract 4) merged; the user checked the dashboard: OK.
- **Dashboard on contract 4 (PR #12):** Overview status line (target + source, scenarios, engine review-period text),
  notices (start repair, waste warning, data warnings), decisions for every committed week 1..R, service grid on the
  expected FR with "not judged" weeks and end-of-horizon weeks = from the first Policy_sS `FG active` = "fixed 0" week
  (W31 on the template; Claude's choice, the engine defines no number); run presets; cost comparison only at the same
  target and the same rules met; localStorage key `meio.nextRunSettings.v2`.
- **Next:** API update 2 (what-if editing), see below.
**API update 2 (13.11., what-if editing; user decisions 13.11.):**
- **Rules (app rules W01–W07, `api/server.py` `APP_RULES`, served at `GET /app-rules`, shown on Explainability under
  the engine's Model_Assumptions; keep them current with every app change):** edits go into a copy of the ORIGINAL
  upload (openpyxl, values only; the original's sha256 never changes); only input cells (no keys, no formulas; Settings
  only PlanningStartDate, HorizonPeriods, ReviewPeriod); demand per channel and week either single quantiles or a shift
  that adds x units to all 10 quantiles; fill-rate target (Items!TargetFillRate) and review period (Settings!ReviewPeriod)
  only as master data, the run target field is gone (`OPTIMIZATION_FILL_RATE` stays None); **no chaining**: a what-if
  always starts from an original upload (409 otherwise, naming the original); costs compared only at the same target
  and rules met; solver settings only per run. No adding/deleting rows yet.
- **Worker:** `sheets <xlsx> <answer>` (editable sheets: header row 4, data from row 5, key columns per sheet in
  `EDIT_SHEETS`, Excel row numbers, editable flags, formula cells with Excel's last value) and
  `whatif <original> <edits.json> <copy> <answer>` (edits → copy → engine validate; answer + change list, one entry per
  cell with the original value). Edit = `{sheet, row, column, value}` or `{sheet: "Demand", row, shift}`.
- **API:** `GET /runs/{id}/sheets` (cached in `sheets.json`), `POST /whatif/validate` {base_run, edits} → valid, errors,
  warnings, input, changes; `POST /runs` with `base_run` + `edits` (JSON) instead of `file` (invalid → 422 with the
  engine's errors, nothing starts); status.json `base_run`, `changes` (count); `GET /runs/{id}/changes`.
- **Verified 13.11. (cloud, test double):** 204 tests (141 engine + 63 app), coverage 98.5 %; engine refusal of an
  invalid edit = the engine's words for the same edit made by hand; a +2,000 shift changes exactly the 10 quantiles of
  one channel and week.
- **Row edits (14.11.):** `{sheet, add: {column: value}}` (first row without data, keeps the template's check formulas)
  and `{sheet, row, delete: true}` (clears the row), only for Init_Inventory, Open_Orders, LeadTimeDist and their input
  columns (`worker.ROW_SHEETS`); W02 updated. Tested: new lot, new open order, stochastic lead time (8 wk 80 %, 10 wk
  20 %), removed lot; 208 tests, coverage 98.4 %.
- **Run again on the same data (14.11., W08):** `POST /runs` with `same_data_as` = a run id copies that run's
  input.xlsx byte for byte (same sha256); a what-if keeps its `base_run` and changes.json (still no chaining). Data set =
  runs with the same `file.sha256` (dashboard `src/lib/data-sets.ts`). Dashboard: top bar Data picker (uploads /
  what-ifs, what-if chip with change list) → Run picker (time · preset · cost · rules); Run dialog "Same data" (default
  when a run is selected) or "Upload"; Run history "Run again" and a Data column; run page and comparison show the data.
- **Dashboard edit mode (14.11., user notes; dashboard PR #14, built by Claude, no Lovable credits):** inline fields in
  the normal Master data screens for only these values: horizon, review period; shelf lives, BOM quantity, usable-while
  minimum; MOQ, order step, max per order, lead-time editor (weeks + probability per possibility, add/remove); target
  (%), shortage priority, channel minimum shelf life; demand per channel and week (quantiles or shift; "check to see the
  effect" = checked what-if bands + original Q05/Q95 dashed); lots and open orders (quantity, shelf life, arrival;
  add/remove; route from Sourcing/Production); costs and cost tiers (explained in words). Network is not editable.
  Field → cell lookups by row key in `src/lib/whatif-fields.ts`. The top-bar W1–W36 view filter is removed.

**Plan:** PR 1 foundation → user runs tests + one real F5 run from the repo → PR 2 local API → one real run via
`localhost:8000/docs` → PR 3 ngrok → test from the phone → Lovable UI (prompt pack from Claude, one screen at a time)
→ hardening. Claude works on its branch and opens a PR; the user merges and pulls with GitHub Desktop.
