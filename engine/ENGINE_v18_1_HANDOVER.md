# Engine v18.1: hand-over to the app session (everything since v17.1)

Engine `2026-11-12 v18.1 (Seeker-only)`, **contract version 4**. Your last engine was v17.1 (08.11., contract version 2,
note `ENGINE_v17_RESPONSE.md`). Between them: v17.2 (10.11.), v18.0 (12.11., contract 3), v18.1 (12.11., contract 4), plus
decisions that only changed the documentation. This note lists **every** change, why it was made, and what the app must do.
Keep it as the change record. The day-to-day content is in `CLAUDE.md` (§4–§8), `MEIO_model_spec.md` (85 rules, IDs in brackets
below) and `MEIO_math_model.md`.

---

## A. Install

1. Replace your `engine/` files with the files in this zip:
   - engine and tests: `meio_seeker_stochastic.py`, `reference_simulator.py`, `meio_stress_tests.py`, `test_engine.py`
   - template: `MEIO_Data_Template.xlsx`
   - documents: `MEIO_model_spec.md`, `MEIO_math_model.md`, `CLAUDE.md`, `MEIO_shelf_life_aware_policies.md`
   - Delete `ENGINE_v17_RESPONSE.md`; this note replaces it.
2. Run `python -m pytest -q test_engine.py`: **141 passed** (v17.1: 117), about 2.5 min.
3. **`CLAUDE.md`:** if you edited your copy, merge §1, §4, §5, §6 and §8 instead of replacing it.
4. **New files:**
   - `MEIO_math_model.md`: the formal model, now part of the bundle.
   - `MEIO_shelf_life_aware_policies.md`: an archived design. The waste warning (L10) names it, so users may ask for it.
5. **Workbooks from v17.x are rejected.** The message is `Items row 5: TargetFillRate of FG1 must be in (0, 1] (one target per
   product, H04)`. See B1 for the migration.

---

## B. Breaking changes for the app: checklist

### B1. Excel template (input)

| Sheet | Change | Rule |
|---|---|---|
| Items | **New column F `TargetFillRate`**: one target per product. FG rows: a value in (0, 1]; RM rows: empty. Row check moved to column H | H04 |
| Channels | **`TargetFillRate` removed.** **`ShortagePriority` (column F) is now an input**: whole numbers 1..n, each used once; 1 = served first. New formula column `FreshLotSales` (I). Layout: A ChannelID, B Name, C SourceLocationID, D ItemID, E MinRemainingShelfLife, F ShortagePriority, G UnmetDemand, H DemandInput, I FreshLotSales, J RowCheck | H02, K19 |
| Open_Orders | New formula column L `WeeksLate`, with a row WARNING (X08). RowCheck moved to M | K19 |
| Production, BOM | Row WARNING when the MOQ exceeds what one fresh lot can sell/use (X09) | K19 |
| Settings | **`FillRateChanceLevel` removed.** **`ReviewPeriod`: default 2 → 1, with a new meaning** (C6 below). Text values: `ChannelAllocationRule = ByShortagePriority_FEFO`, `FillRateDefinition = ExpectedPerChannelPerPeriod` | A03, H05 |
| Checks | `X07` removed (the old fill-rate cross-check). **`X10` added** (WARNING: horizon too short) | K19, A02 |
| README, Model_Assumptions | Texts updated; 85 rules (v17.1: 82) | |

**Migrating an old workbook:** copy the data into the v18.1 template.
- **Fill-rate target:** the old per-channel targets (D2C 98%, B2B 95%, AMZ 90%) become one product target in
  Items!TargetFillRate; the template uses 95%. This is the user's choice per product.
- **ShortagePriority:** the old serving order came from the targets; as input it is D2C 1, B2B 2, AMZ 3.
- **ReviewPeriod:** check its meaning (C6). Leave it at 1 unless the user really re-plans less often.
- **Don't migrate in code:** the app must not build the engine's data object itself (CLAUDE.md §2). If needed, write cell
  values into a copy of the v18.1 template.

### B2. Data object (`res.data`)
- `chance_level` is removed.
- **New:** `fill_rate_target` (the target used in this run) and `fill_rate_target_excel` (the Excel value).
- `Channel.target` is now the product target, the same for every channel. `Channel.priority` is new.
- `data.channels` is sorted by ShortagePriority (it was sorted by target).

### B3. Settings constants (module globals, set before `run()`)
- **Removed:** `OPTIMIZATION_CHANCE_LEVEL`.
- **Changed:**
  - `FILL_RATE_CONSTRAINT_FORM` is now `"mean"` (default) or `"none"` (diagnostic only); `"frequency"` and `"quantile"` are gone.
    Use only these two values: any other value currently runs `"mean"` silently (G1).
  - `START_Z`: 4.0 → `None` (z from the target, see C4).
- **New, user-facing:**
  - `OPTIMIZATION_FILL_RATE` (None = Items!TargetFillRate; a number replaces the target for the whole run, K08)
  - `START_REPAIR` (True, K21)
  - `WASTE_WARNING_SHARE` (0.05, L10)
  - `HORIZON_MARGIN_WEEKS` (8, X10)
- **New, internal (leave them alone):** `START_Z_STEP` (0.5), `START_Z_MAX` (4.0), `TAIL_SHARE` (0.05), `REPAIR_STEPS`.
- **Run overrides (K20):** still only `NumScenarios` and `RandomSeed`. The review period and the target are changed through a
  workbook copy, or through `OPTIMIZATION_FILL_RATE` for the target.

### B4. `RunResult`
- **`ins` (Seeker's own scenarios):**
  - `fr_share` → **`fr_mean[(ci, t)]`**, the expected fill rate per cell.
  - **New `start_z`** = `{z0, z, steps}`.
  - **New `repair`** = `{"used": False}` or the repair record (steps, chosen step, `formula_start{cost, met}`).
- **`oos` (fresh scenarios):**
  - `("fr_share", ci, t)` is removed.
  - Still there: `("fr_mean", ci, t)`.
  - **New:** `("fr_full", ci, t)` (share fully served), `("fr_p10", ci, t)` and `("fr_tail", ci, t)` (average of the worst 5% of futures).
- `run()` and `write_report()` keep their signatures. `optimize()` changed, but it is internal.

### B5. Result sheets
- **FillRates (contract 3):** one row per channel and week.

  | Columns | Status |
  |---|---|
  | Channel, Week, Target, Constrained, Status in-sample (`OK`/`VIOLATED`), Status out-of-sample (`OK`/`BELOW TARGET`) | kept |
  | `P(FR ≥ target) in-sample (N Seeker scenarios)`, `Mean FR (N fresh scenarios)`, `P(FR ≥ target) (N fresh scenarios)` | removed |
  | `Expected FR in-sample (N Seeker scenarios)`, `Expected FR (N fresh scenarios)`, `Fully served (N fresh scenarios)`, `FR in a bad scenario, P10 (N fresh scenarios)`, `Worst 5% of futures, average FR (N fresh scenarios)` | added |

  N is part of the header, so match columns by their prefix.
- **Commit_Now (contract 4):**
  - Column `Quantity / rule` → **`Quantity`**.
  - **One block of rows per committed week 1..R:** production order, the RMW → KAJA transfers (one per raw material), and the
    supplier orders (one per raw material), with arrival weeks. That is 9 rows per week on the template.
  - **No more `Rule …` rows.** All quantities are exact numbers.
  - A production row can add "raw material permitting (P10 …, P90 …)" when uncertain lead times can cut it.
- **Decisions, column `Status`** (match by prefix):
  - weeks 1..R: `COMMIT NOW (exact)`
  - later run weeks: `Indicative — re-optimized at the run in week t`
  - weeks between runs: `Indicative — planned at the run in week r`
  - Removed: `COMMIT RULE: apply (s,S) to actual stock` and `Indicative — re-optimized at next review`.
- **Policy_sS:** new column **`Planned at run week`** (after `WeekStart`).
- **Summary, new rows:**
  - `Review period (A03)`, `Start repair (section 4c, K21)`
  - `Start policy before repair` (only when the repair was used)
  - `Weakest cell (out-of-sample)`
  - `Expected waste <item> (out-of-sample, L10)`, one row for FG1 and one per perishable raw material
  - `Waste warning (L10)`
  - `Time: 4. Start check: Seeker evaluates the start with z = …` (one row per z step), `Time: 4c. Start repair …` (when used)
- **Summary, changed texts:** `Seeker settings` (constraint form `mean`, the fill-rate target and its source) and `Start
  policy (warm start)` (z₀ and how it was raised).
- **Phase names:** `Time: 5. Seeker model build` and `Time: 5. Seeker evaluates the start policy` appear twice after a
  start check (a second build). Don't key a dict by phase name.
- **Unchanged:** Stock_Waste, Bands, KPIs.

### B6. Console (progress streaming)
- **New phase lines:**
  - `[hh:mm:ss] 4. Start check: Seeker evaluates the start with z = 2.00 ...`, then `  z = 2.00: 89 of 90 cells met, cost 107,637`
    and `Start check: Seeker starts from z = …`
  - `4c. Start repair R1/R2/R3: Seeker evaluates ...`
  - `START REPAIR USED: …`
  - `WARNING (L10): …`
  - `NOTE: reorder point(s) at their upper bound: …` (as before)
- **Seeker's progress lines** (`At time …: Objective = …`) come irregularly, sometimes not for several minutes. Don't treat
  silence as a hang.

---

## C. What changed and why, by topic

### C1. Fast-moving, very perishable data (v17.2, 10.–11.11.)
- **What happened:** the user tried a fast-moving copy (FG shelf life 3 weeks, short lead times, MOQ 6,000 and 2,000) and
  Seeker could not solve it.
- **Causes:**
  - The formula start missed fill-rate rules, and from an infeasible start Seeker makes little progress.
  - The MOQ was larger than one fresh lot can sell before it expires.
  - The template's open orders still had the old long lead times.
- **Built:**
  - **X08:** an open order that arrives later than placement + the longest lead time (Excel row WARNING, column `WeeksLate`).
  - **X09:** a MOQ larger than one fresh lot can sell (Channels `FreshLotSales`).
  - **L10:** expected waste rows per item and a warning above 5% of expected demand.
  - **Start lot cap and raw-material lot guard (K07).**
  - **The start check and repair (K21, C4).**
  - **K17, tighter maximum-supply policy:** the smallest reorder point that still always orders the maximum.
- **Seeker check (11.11., old chance rule):**

  | Run | Result |
  |---|---|
  | Template | no repair; €114.6k on fresh futures; 89/90 rules met |
  | Fast, MOQ 6,000 | repair R2 used; €279k |
  | Fast, MOQ 2,000 | no repair; €94.6k |

- **Learned:**
  - About 20 points of the fast case's FG waste (6,926 pcs) came from opening stock and open orders that no plan can save.
  - "Maximum supply" (up to 50,000 per week) makes a feasible but very expensive start.
  - v18's target-aware start solves the MOQ 6,000 case without repair: €100.9k, 63/69 on fresh futures, weakest cell in
    week 23 of 24 (12.11.).

### C2. Shelf-life-aware inventory positions (studied 10.11., rejected)
- **Studied:** the literature (Haijema and others) and an age-aware (s,S) design for FG and raw materials.
- **Decision (K22):** positions stay age-blind. They count lots that will expire before they are sold, and Seeker is left
  to learn the pattern. The extra complexity and run time weren't justified.
- **Where it is documented:** K22 says which stock the position fails to subtract. The design is archived in
  `MEIO_shelf_life_aware_policies.md`, and the L10 warning points to it.

### C3. Fill-rate rule and its reporting (v18.0, 12.11.)
- **Problem:**
  - The old rule required the target to be met in at least 99% of the futures, per channel and week.
  - A week's fill rate is almost always either 100% or far below the target.
  - So targets of 98, 95 and 90% gave the same plan and cost, and only the chance level counted.
- **Fix, decided by the user:**
  - **One target per product (H04)**, the same for all channels.
  - **Rule E (H05):** per channel and week, the **expected** fill rate (mean over the futures of served ÷ demand) must be
    ≥ the target. There is no chance level any more.
  - **ShortagePriority becomes an input (H02).**
- **Seeker check (12.11.), cost on fresh futures:**

  | Target | Cost | Rules met |
  |---|---|---|
  | 98% | €109.9k | 89/90 |
  | 95% | €106.4k | 88/90 |
  | 90% | €102.5k | 89/90 |

  Cost now falls with the target.
- **The tail:**
  - The user asked whether the mean lets the plan deliberately fail some futures.
  - Option A was chosen: no second rule, but watch the tail.
  - New FillRates column "Worst 5% of futures, average FR". It is computed exactly, also when many futures tie at 0% or 100%.
  - New Summary row "Weakest cell".
  - Read one cell as explained in CLAUDE.md §8.
- **Fitting the last week (found 12.11.):**
  - With 200 Seeker futures, the plan fits the last week to those futures. Example: AMZ week 36 at 88.8% on fresh futures
    against 95% (L04, A05).
  - With **500 scenarios** it disappeared: 90/90 on fresh futures, weakest cell 96.3% (run 12.11., 700 s).

### C4. Start policy, start check and repair (v17.2 → v18.0)
- **z₀ follows the target (K07):** z₀ is derived from the target with the normal loss function. On the template:

  | Target | 90% | 95% | 98% |
  |---|---|---|---|
  | z₀ | 0.86 | 1.22 | 1.63 |

- **Start check (K07, K21):**
  1. Seeker evaluates the start.
  2. If it misses cells that the maximum-supply policy meets, z is raised on a fixed 0.5 grid up to 4.
  3. Only if cells are still missed, the repair runs, step by step (R1 → R2 → R3, below).
  - **R1:** s and S at their upper bounds in the 12 weeks before the missed week.
  - **R2:** maximum supply in the 6, then 12 weeks before it.
  - **R3:** maximum supply everywhere.
  - Seeker starts from the cheapest step that meets the most cells.
- **On real data z₀ comes out 1.0–1.9 too low,** so the z steps do most of the work, at about 20–25 s per step with 200
  scenarios and 30–40 s with 500.
- **Why the repair stays (12.11.):**
  - The user asked whether the z steps alone suffice. A test of 73 data variants said no: in 24 the z steps left meetable
    cells unmet. Typical causes: freshness rules, large MOQs, the last weeks, capacity limits.
  - The current R1 → R2 → R3 order was the only variant that reached every meetable cell at moderate cost.
  - `START_REPAIR = True`. The repair runs only when needed, prints `START REPAIR USED`, and is shown in the Summary.
  - A repaired start is expensive, so give such runs more search time.

### C5. Planning horizon and the end-of-horizon effect (12.11., documentation only)
- **No value for stock left after week H (A05).** So the cheapest plan runs stock down at the end.
- **Measured:** a NumPy search reached €94.7k against Seeker's €112.7k at almost the same service, nearly all of it by leaving
  less stock after week 36.
- **Kept on purpose (lean):** only weeks 1..R are committed and the model is re-run, so the end never reaches them.
- **How to read results (A05):**
  - act only on the commit-now decisions;
  - the last weeks show a business winding down;
  - total cost and "Saving vs. start" are spending within weeks 1..H, partly from running stock down.
- **Horizon rule (A02):** H ≥ longest raw-material lead time + longest production lead time + R + a margin. The template has
  36 ≥ 12 + 6 + 1 + 8. The new warning **X10** checks it, in Excel and in Python.

### C6. Review period (v18.1, 12.11.)
- **New meaning of `Settings!ReviewPeriod = R`:** the model is run every R weeks, and new data arrive only at a run. Each run
  **commits every decision of weeks 1..R as exact quantities.**
- **Rule (A03):**
  - In a run week the (s,S) rules use the actual position.
  - In the weeks between runs they use the **planned position** = position at the last run + orders planned since − expected
    demand since. Expected demand = mean of the 10 quantiles. The planned position is never below 0 and is age-blind.
  - Nothing inside a cycle reacts to the actual demand. The supply chain itself still runs on the actual demand of every
    scenario.
  - Weekly order chances and capacities stay. An order-only-every-R-weeks design was rejected, because it would cut RM3's
    1,000 kg per order to one order per cycle.
  - The start policy's safety stock counts the uncertainty since the last run (K07).
- **Literature:**
  - Bookbinder & Tan 1988 ("static uncertainty" within a cycle)
  - Dural-Selcuk et al. 2020 (receding horizon)
  - Bensoussan, Çakanyıldırım & Sethi 2006/2007: the "reference inventory position" under information delays, with (s,S)
    levels that depend on the age of the information
- **R = 1 reproduces v18.0 exactly.** This was tested, and on real Seeker the start-check numbers were identical.
- **Seeker check (12.11.), template, fresh futures:**

  | R | Cost | Rules met |
  |---|---|---|
  | 1 | €105.9k | 88/90 |
  | 2 | €105.0k | 89/90 |
  | 4 | €106.0k | 89/90 |

  - The differences are within search noise. A production lot covers 4–5 weeks of demand, so a reaction delay rarely matters
    here.
  - R = 4 needs a higher start z (3.0 instead of 2.5) and loses 1.6–2.6 times more sales at the same safety
    factor (reference simulator, 3,000 futures).
  - R = 4 commits RM3 1,000 kg in weeks 1–3 and a production order of 8,000 in week 3.

### C7. Independence, quantile sums and whole units (12.11., documentation only)
- **Independent futures (G02):** futures are drawn independently per channel and week. The 10 quantiles per cell can't
  express correlation. If real forecast errors carry over from week to week or move together across channels, the engine
  underestimates the spread of multi-week totals and its fill rates are too optimistic. For the template's 9-week FG window:

  | Week-to-week correlation | Spread of the 9-week total |
  |---|---|
  | 0 (independent) | 1,793 |
  | 0.3 | 2,240 |
  | 0.6 | 2,971 |

  This is noted as an open topic for the user's forecasting tool, either as simulated demand paths or as a correlation setting.
- **"You can't add up P90 forecasts":**
  - The core (scenarios, KPIs, the start formula's √Σσ²) combines correctly.
  - Only the upper bounds of the decision variables (K05) add up the channels' weekly Q95s. That is 59–61% too generous over a
    15–23-week window: harmless for the plan, but it enlarges the search space. The K05 wording is to be corrected (G2).
  - **For the UI:** never add up P-columns of the Bands sheet across weeks or items. Only `Mean` adds up. Totals with P10/P90
    are in the KPIs sheet.
- **Whole units:** every physical quantity per future is a whole number: demand, production, orders, transfers, stock,
  sales, lost sales, waste. Averages over futures have decimals by nature; "RM3 order mean 46.7 kg" means a 1,000 kg order
  in about 5% of futures. The internal positions of RM3/RM4 (BOM × FG position) and the planned position have decimals too.
- **Smaller fixes:** the K09 text (it still called "frequency" the default), the template README, and a header note in the
  shelf-life archive.

---

## D. Settings for a usable run

| Use | Scenarios | Search time | Note |
|---|---|---|---|
| Decisions you act on | `NumScenarios` **500** | 600–900 s | ~19 min in total on the user's PC; Seeker makes about 8.5 evaluations/s |
| Quick what-if | 200 | 200–300 s | the last weeks can look better than they are |

- Keep the horizon rule (X10).
- The run machine must not sleep. On 12.11. one 300 s search ran 12,100 s; the likely cause is that the PC went to sleep,
  but this is not confirmed.
- Seeker overruns the time limit by about 4–15%.
- Compare costs only between runs with the same horizon and data, and with the same number of rules met.

## E. What the app must show
- **Commit table** for weeks 1..R, with the `Review period` Summary row next to it (when the next run is). Show it as the
  action list.
- **Status lines:** feasibility line, bound NOTE, start-repair notice (when used), waste warning, and the data warnings
  X08/X09/X10 (never errors).
- **FillRates:**
  - fresh-scenario status as the headline;
  - the cell explanation from CLAUDE.md §8;
  - the `Weakest cell` row;
  - mark weeks 1–6 "not judged" and the last weeks "indicative (end of horizon)".
- **The fill-rate target used and its source** (Excel or section 0), and the number of scenarios.
- **Bands:** quantile bands per week. Never sum quantiles (C7).

## F. Verification

| Check | Result |
|---|---|
| Test suite | 141 tests (v17.1: 117) |
| Mutation test | 46/46 deliberate rule errors caught, 12 of them for the review period |
| Engine = reference simulator in every scenario | incl. R = 2, 3, 4 and uncertain lead times |
| R = 1 = v18.0 exactly | ✓ |
| Committed weeks identical in every future; cycles don't react to later demand | ✓ |
| Excel = Python checks (LibreOffice) | 47 broken copies, 6 valid edits (incl. R = 4), X10 at R = 1, 10, 11 |

**Real Seeker runs on the user's PC:**

| Date | Engine | Run | Result |
|---|---|---|---|
| 11.11. | v17.2 | check (3 runs) | all as predicted (C1) |
| 12.11. | v18.0 | targets 98/95/90% + fast case | cost falls with the target; no repair; tail values consistent |
| 12.11. | v18.0 | 500 scenarios, 700 s | €112.7k, 90/90 on fresh futures |
| 12.11. | v18.1 | R = 1, 2, 4 | as designed (C6) |

## G. Open: proposals not built (they need the user's sign-off), and known limits
1. **A check on `FILL_RATE_CONSTRAINT_FORM`:** any value other than "mean"/"none" should stop the run.
2. **K05 wording:** "sum of the channels' weekly Q95s", not "Q95 of total demand". Optionally, correct bounds from the true
   quantile of the window total (needs testing: it shrinks the search space).
3. **Summary row "unavoidable waste":** waste from opening stock and open orders, so that L10 judges only the plan's own waste.
4. **A gentler repair step R2:** cap "maximum supply" at what one fresh lot can sell.
5. **No bound NOTE when the maximum order quantity is the real limit** (RM3 is a false alarm).
6. **The last weeks:** a label "end of horizon, not reliable" or a small in-sample margin, if 500 scenarios aren't enough.
7. **A "how sure is the commit decision" line,** for near ties (12.11.: producing in week 1 or not differed by €269).
8. **The exact mean of the sampled distribution** instead of the mean of the 10 quantiles (0.25% too low on the template).
   It would be used for the planned position, the start, X09 and Excel's MeanApprox.
9. **A whole-number check for FG quantities** (opening stock, open orders, MOQ, batch, maximum).
10. **Correlated demand:** for the forecasting tool (C7).
11. **ReviewPeriod and the target as run overrides (K20):** today they need a workbook copy or section 0. This would be a
    change request from the app if needed.

**Known limits:**
- **J04:** a lot that is already unusable when it enters is visible for one week.
- The NumPy optimizer (`meio_simopt.py`, not in the bundle) still uses the old chance rule.
- Seeker 2.2 is untested.
- The data is example data.
- Rules still "Proposed": A04, D05, F02, G02, G03, H03, H06, H07, I07, J03, K07, K20, K21, L05–L08, L10.
