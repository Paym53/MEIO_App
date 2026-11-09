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
| `engine/meio_seeker_stochastic.py` | **the engine** (v17.1, contract version 2): every rule, written once, as Seeker Terms | **no** |
| `engine/reference_simulator.py` | independent second implementation of the rules, used only by the tests | **no** |
| `engine/meio_stress_tests.py` | 26 data-change scenarios run through the engine (needs Seeker) | **no** |
| `engine/test_engine.py` | **the engine test suite**: input control, physics & dynamics, model functionality, output control, stress cases, engine fingerprint; includes a Seeker test double, so no license needed | **no** |
| `engine/MEIO_Data_Template.xlsx` | the input file (template v1.1, **example data**; the tests depend on it) incl. sheet `Model_Assumptions` | **no** (real data goes in `data/`, gitignored) |
| `engine/MEIO_model_spec.md` | the dynamics/assumptions list: all 82 rules (IDs like C03, H05, K17), same as the Excel sheet | **no** |
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
- **Checks stay green:** `python -m ruff check .` and `python -m pytest` from the repo root (117 engine tests,
  about 90 s, plus the app's own tests) on every change. CI runs both on Linux and Windows, plus a secret scan.

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
Channels, served in this order: D2C target 98% (needs ≥ 16 wk left) → B2B 95% (≥ 24) → AMZ 90% (≥ 24)
Demand: 10 forecast quantiles per channel and week, ~1,400 cans/wk. Horizon 36 weeks, re-planned every 2 weeks.
```

The example data is generated; real data comes later.

**Outputs:**
- (s,S) per stock point and week
- the derived orders, transfers and production
- **commit now:** week 1 exact, week 2 as a rule; later weeks are indicative
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
- **Objective:** minimize expected total cost: raw materials + production + transport + holding + waste. There is no lost-sales cost and no
  end-of-horizon value (user decision).
- **Constraints:** from week 7 (the first week a new decision can reach), per channel and week, at least 99% of scenarios must meet the
  fill-rate target. That gives 90 chance constraints.
- **Solving:** Seeker searches the (s,S) values on 200 demand scenarios drawn from the forecast. Then the same model is evaluated on
  2,500 fresh scenarios (out-of-sample check).
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
| `ins` | Seeker's own scenarios: `cost`, `parts`, `fr_share[(ci,t)]`, `met`, `start{cost, met}`, `status`, `feasibility` (one line) … |
| `oos` | fresh scenarios: `cost`, `("fr_mean"/"fr_share", ci, t)`, `(item, "mean"/"p10"/"p90"/"p_order", t)`, stock, waste; and `("band", series, item, week, stat)`, `("kpi", name, item, stat)` |
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
| `OPTIMIZATION_CHANCE_LEVEL` | None | None = Excel level 0.99; never set it lower |
| `EXTRA_COVER_WEEKS` | | upper bounds of the reorder points; raise the one named in a bound NOTE |

**Two lines the user must always see:**
- **Feasibility check:** "OK", or "N rule(s) fail whatever we decide: …". These are weeks the supply physically cannot reach.
- **Bound NOTE:** "none", or which setting to raise because a reorder point hit its upper bound.

**Runtime:** a default run takes about 6.5 min on the user's PC (feasibility 25 s, build 11 s, search 250 s + ~15%, out-of-sample 75 s in v16;
v17 builds about 8,600 more single numbers in the out-of-sample check, the extra time on Seeker is measured on the user's PC).
Run it as a background job and stream its stdout (phase lines `[hh:mm:ss] …` and Seeker's progress every 30 s) as progress.

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

**Latest numbers (user's PC, engine v17, 250 s, 08.11.):**
- start policy €119,981
- Seeker in-sample €111,784 with 90/90 rules (8,450 evaluations)
- fresh scenarios (2,500) €112,128 with 88/90 rules (the misses are usually the week-36 Amazon/B2B rules at the end of the horizon; this is accepted, because the end weeks are indicative)
- Week-1 decision: no production; a 1,000 kg strawberry order arriving in week 9.
- Phase times: feasibility check 20 s, model build 8 s, search 268 s (limit 250), out-of-sample check 78 s, about 6.5 min in total.
  Short time limits overrun more: a 30 s search took 54 s, so plan short what-if runs with about 25 s fixed overhead.
- **Through the local API (07.11., v16, real Seeker):** €112,128.16 and 88/90 on fresh scenarios, identical to F5; 377 s in
  total. **Same run on v17.1 through the API (08.11.):** €112,128.16, 88/90, 407 s; search 273.4 s for 250 s
  (= limit + 24 s, as the engine session's 30 s → 54 s), out-of-sample 82.5 s. The API's time estimate (`api/server.py`):
  30 s + 21 s feasibility + 0.033 s per fresh scenario (twice with the start policy on fresh scenarios) + limit + 24 s.

**v17 (08.11., change request "uncertainty, flows and what-ifs"):** report series, arrival weeks given an order, KPIs,
run overrides, optional start policy on fresh scenarios, and every Excel check in Python. The search model is unchanged:
the same Seeker calls as v16 (13,102 Terms on the template), so the search finds exactly the same plan. All v16 numbers are
identical except the arrival week of orders that are never placed (v16 showed a made-up week, v17 leaves it empty).
Confirmed on real Seeker 2.1 on the user's PC: the same improvement path as v16 (5,126 evaluations → €112,013.35;
6,282 → €111,783.95); the out-of-sample check takes 78 s instead of ~75 s; all 1,346 band rows are ordered P05 ≤ … ≤ P95;
the stock balances on the means close exactly. v17.1 fixes a file lock: the engine now closes the workbook after
reading, so the app can overwrite or delete its copies on Windows.

**Decisions and why:**
- **Rules written once in Seeker; no second simulator in the engine** (the reference exists only for tests). This avoids errors from duplicate logic.
- **One fixed formula start policy** (z = 4). Seeker 2.1's `set_value` re-evaluates the whole model per value, so testing many start policies was too slow.
- **Bounds come from the forecast and are easy to raise.** RM3 and FG were raised from 4 to 8 after they hit their bounds.
- **No lost-sales cost and no end-of-horizon rule.** That is the user's choice.
- **The feasibility check exists** because stress tests showed weeks the supply can't reach.
- **Seeker settings:** speed 0 and the "frequency" constraint form tested best.
- **Multi-product is deferred but planned.** Don't hard-wire a single product in the app's data model.
- **The engine session also has a solver-free NumPy optimizer** that was about 3.6% cheaper in benchmarks. It isn't part of this bundle; ask the user if you need it.

**Seeker facts (2.1.0):**
- Reading single numbers (aggregates) is instant; per-scenario lists cost 0.5–3 s each, so the engine never reads them.
- Seeker reshuffles the demand samples across its scenarios, so its scenarios exist only inside Seeker.
- From an infeasible start Seeker often makes no progress.
- It overruns the time limit by about 7–15% (266.6 s for 250 s on 07.11.; short limits relatively more).
- **The search is reproducible:** runs on 07.11. and 08.11. (v16 and v17) found the same improvements after the same
  numbers of evaluations (5,126 → 112,013.35; 6,282 → 111,783.95). Results differ only when a different number of
  evaluations fits into the time limit (machine load).
- Its progress line `At time 134.5: Objective = 112013.35; Status = Feasible; Evaluations = 5126` appears only when the
  search improves (3 lines in 267 s on 07.11.), not every 30 s: show progress from elapsed time, not from these lines.
- If the results file is open in Excel, the engine writes a time-stamped name.

**Stress tests (06.11.):**
- No dynamics bug was found.
- Some cases are physically impossible in some weeks: demand +30%, cancelled strawberry orders, strawberry capacity 500 kg. The feasibility check now reports these.
- Earlier red verdicts came from running at chance level 0.95 while judging at 0.99, and from comparing costs of plans that met different numbers of rules.

**Open:**
- Rules still "Proposed": A03, A04, D05, F02, G02, G03, H03, H06, I07, J03, K20, L05–L08 (see `MEIO_model_spec.md`).
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
- Runs/compare (AC4): costs compared only when rules_met_out_of_sample is equal; otherwise a warning, no difference.
- Overview: KPI strip = headline fields; fresh saving from KPIs row when present, else in-sample (labelled); heatmap
  status from FillRates "Status out-of-sample", fixed weeks before run_info.first_rule_week; KPIs rows by name.
  Fixed weeks (Status "info") show their share in grey without ✓/✕. Feasibility "not run" → "Not checked".
  Decisions to take now: Commit_Now columns When / Decision / "Quantity / rule" / Note (week from "Week 1"); cards only
  for week-1 quantity > 0; why = Bands "position" week 1 Mean vs Policy_sS s/S + input order limits; week-2 rules
  listed only when P(order) > 0 (Decisions). Channel rows from FillRates (serving order), never a fixed list.
- Supply-chain map (React Flow): nodes = input.tables.Locations (LocationID, Type) only; supplier edges from Sourcing
  (SupplierID → DestinationID, label "RMx · LT"), others from Lanes; runs without Sourcing show a hint. Panels:
  bars only for order_qty and receipts, other series as band/line charts; open orders keys qty/placed/arrival;
  channel fill rate from the column "P(FR ≥ target) (" (fresh, not the in-sample one that comes first); "fixed weeks"
  shading only on DC and channel charts; KPI unit "share of scenarios" shown as %.
- Orders: rows = (stream, week) with P(order) > 0 + open orders; quantity labelled "avg. over all futures (incl. no
  order)"; arrival P50 (P05–P95) only over futures that order; open orders "before W1 (week −3)"; review cards
  formatted with the item unit.
- Decisions/Orders: (s,S) from wide Policy_sS (`FG s`/`FG S` exact case, production point "FG"); badges only from
  Decisions.Status ("—" otherwise); units from KPIs "Average end stock <item>," Unit; arrival from Bands
  arrival_week ("never ordered" when empty); grey only rows where every "<point> active" is "fixed 0".
- Run dialog: optional form fields never sent when empty (null, undefined or "").
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
**API update 2 (next, before the Master data / scenario screens):** what-if edits written into a copy of the
uploaded workbook (openpyxl, values only; engine K19 checks it), with an original-vs-changed list per run.

**Plan:** PR 1 foundation → user runs tests + one real F5 run from the repo → PR 2 local API → one real run via
`localhost:8000/docs` → PR 3 ngrok → test from the phone → Lovable UI (prompt pack from Claude, one screen at a time)
→ hardening. Claude works on its branch and opens a PR; the user merges and pulls with GitHub Desktop.
