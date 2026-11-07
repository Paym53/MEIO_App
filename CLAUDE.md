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
| `engine/meio_seeker_stochastic.py` | **the engine** (v16): every rule, written once, as Seeker Terms | **no** |
| `engine/reference_simulator.py` | independent second implementation of the rules, used only by the tests | **no** |
| `engine/meio_stress_tests.py` | 26 data-change scenarios run through the engine (needs Seeker) | **no** |
| `engine/test_engine.py` | **the engine test suite**: input control, physics & dynamics, model functionality, output control, stress cases, engine fingerprint; includes a Seeker test double, so no license needed | **no** |
| `engine/MEIO_Data_Template.xlsx` | the input file (template v1.1, **example data**; the tests depend on it) incl. sheet `Model_Assumptions` | **no** (real data goes in `data/`, gitignored) |
| `engine/MEIO_model_spec.md` | the dynamics/assumptions list: all 74 rules (IDs like C03, H05, K17), same as the Excel sheet | **no** |
| `MEIO_Results_stochastic.xlsx` | results of a real Seeker run (sheet layout = engine output); on the user's PC only, gitignored | no |

The app's own files are listed in §9. Don't add files or docs beyond that list unless the user asks.
`.gitattributes` keeps every file byte-exact on Windows (`* -text`); without it, Git for Windows would turn LF
into CRLF on checkout and the engine fingerprint test would fail on the user's PC.

## 2. Hard rules

- **The engine files are read-only.** `test_engine.py` section 6 checks their fingerprints. If the app needs an engine
  change, follow §7. Never patch around an engine problem in the app.
- **One copy of every rule.** Never re-implement dynamics, costs, fill rates or data validation in the app. Every number
  the app shows comes from the engine's `RunResult`. The app may format, sort and compare those numbers, but not recompute them.
- **Input always goes through the Excel template and the engine's `validate()`.** UI editing produces the workbook, and the
  app never builds the engine's data object itself.
- **One engine run per process** (settings and timings are module globals). Use a job worker or a subprocess per run.
- **Be honest with the numbers:**
  - Show the out-of-sample results as the headline.
  - Show the feasibility line and the bound NOTE prominently.
  - Show the rules missed on fresh scenarios.
  - Compare costs only between plans that meet the same number of rules (there is no lost-sales cost, so a plan that misses rules looks cheaper).
- **Checks stay green:** `python -m ruff check .` and `python -m pytest` from the repo root (58 engine tests,
  about 25 s, plus the app's own tests) on every change. CI runs both on Linux and Windows, plus a secret scan.

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
res = engine.run(data_file="MEIO_Data_Template.xlsx", license_file="license.sio", time_limit=250)
```

`run()` executes the whole pipeline and writes nothing. It returns a `RunResult`:

| Field | Content |
|---|---|
| `data` | validated input. `data.channels` is sorted by priority; map channel index `ci` with `data.channels[ci].id` |
| `layout`, `x` | decision variables and the optimized values (`layout.index` = (stock point, week, "sigma"/"delta")) |
| `policy` | `{"FG", "RM1".."RM4": (s, S)}`, arrays indexed by week 1..H (index 0 unused) |
| `ins` | Seeker's own scenarios: `cost`, `parts`, `fr_share[(ci,t)]`, `met`, `start{cost, met}`, `status`, `feasibility` (one line) … |
| `oos` | fresh scenarios: `cost`, `("fr_mean"/"fr_share", ci, t)`, `(item, "mean"/"p10"/"p90"/"p_order", t)`, stock, waste … |
| `sheets` | report tables: `Summary`, `Commit_Now`, `Policy_sS`, `Decisions`, `FillRates`, `Stock_Waste` (columns frozen in `test_engine.py`) |
| `timings` | (phase, seconds) |

**Other entry points:**
- `engine.write_report(res.sheets, path)` writes the formatted results workbook.
- `engine.main()` = `run()` + workbook + console (the user's F5 workflow).

**Errors are exceptions:**
- `ValueError` "Data validation failed:" followed by every problem, one per line (show them to the user)
- `FileNotFoundError` for a missing data or license file
- `ImportError` when Seeker is missing

**Settings** are module constants. Set them before `run()`:

| Constant | Default | Meaning |
|---|---|---|
| `TIME_LIMIT_SECONDS` | 250 | search time |
| `OUT_OF_SAMPLE_SCENARIOS` | 2500 | fresh scenarios for the check |
| `FEASIBILITY_CHECK` | True | about 25 s |
| `OPTIMIZATION_CHANCE_LEVEL` | None | None = Excel level 0.99; never set it lower |
| `EXTRA_COVER_WEEKS` | | upper bounds of the reorder points; raise the one named in a bound NOTE |

**Two lines the user must always see:**
- **Feasibility check:** "OK", or "N rule(s) fail whatever we decide: …". These are weeks the supply physically cannot reach.
- **Bound NOTE:** "none", or which setting to raise because a reorder point hit its upper bound.

**Runtime:** a default run takes about 6.5 min on the user's PC (feasibility 25 s, build 11 s, search 250 s + ~15%, out-of-sample 75 s).
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

**Latest numbers (user's PC, 250 s, F5 from the repo copy, 07.11.):**
- start policy €119,981 (identical to earlier runs: the start evaluation does not depend on the search)
- Seeker in-sample €111,784 with 90/90 rules
- fresh scenarios €112,128 with 88/90 rules (the misses are usually the week-36 Amazon/B2B rules at the end of the horizon; this is accepted, because the end weeks are indicative)
- Week-1 decision: no production; a 1,000 kg strawberry order arriving in week 9.
- Timings: feasibility 21 s, build 8.8 s, start evaluation 3.3 s, search 266.6 s, reading 3.3 s, out-of-sample 66.4 s; total 382 s.
  Fixed part ≈ 115 s, so time left ≈ 115 s + 1.07 × time limit − elapsed (`api/server.py` ETA constants).
- **Same run through the local API (07.11., real Seeker):** €112,128.16 and 88/90 on fresh scenarios, identical to F5;
  377 s in total (estimate 382 s). The API reproduces the engine exactly.

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
- **The search is reproducible:** two runs on 07.11. found the same improvements after the same numbers of
  evaluations (5,126 → 112,013.35; 6,282 → 111,783.95). Results differ only when a different number of evaluations
  fits into the time limit (machine load). The earlier €112,858 run was an older state.
- Reading single numbers (aggregates) is instant; per-scenario lists cost 0.5–3 s each, so the engine never reads them.
- Seeker reshuffles the demand samples across its scenarios, so its scenarios exist only inside Seeker.
- From an infeasible start Seeker often makes no progress.
- It overruns the time limit by about 7–15% (266.6 s for 250 s on 07.11.).
- Its progress line `At time 134.5: Objective = 112013.35; Status = Feasible; Evaluations = 5126` appears only when the
  search improves (3 lines in 267 s on 07.11.), not every 30 s: show progress from elapsed time, not from these lines.
- If the results file is open in Excel, the engine writes a time-stamped name.

**Stress tests (06.11.):**
- No dynamics bug was found.
- Some cases are physically impossible in some weeks: demand +30%, cancelled strawberry orders, strawberry capacity 500 kg. The feasibility check now reports these.
- Earlier red verdicts came from running at chance level 0.95 while judging at 0.99, and from comparing costs of plans that met different numbers of rules.

**Open:**
- Rules still "Proposed": A03, A04, D05, F02, G02, G03, H03, H06, I07, J03 (see `MEIO_model_spec.md`).
- Seeker 2.2 is untested.
- The data is example data.

## 9. The app: decisions, files, plan (agreed with the user 06.–07.11.)

**Architecture:**
```
Browser → Lovable web app (own repo, made by Lovable; shows only API numbers, never computes)
        → ngrok tunnel (free fixed domain; requests send header ngrok-skip-browser-warning)
        → local API on the user's PC (FastAPI, localhost:8000, every request needs the token)
        → one subprocess per engine call (validate or run) → runs/<id>/ input.xlsx, log.txt, result.json, results.xlsx
```
- Data source: the Excel upload only (the user edits in Excel). A database comes later, through the one data-source
  function in `api/worker.py` and an engine change request (§7) for a loader.
- No editing of data in the browser yet: a workbook re-saved by openpyxl loses Excel's cached formula values, so
  `Checks` OVERALL reads `nan` and `validate()` rejects it (reproduced 06.11.). Revisit with a recalculation step or
  an engine change.
- **Python UTF-8 mode (`PYTHONUTF8=1`) everywhere:** CI sets it, the user set it with `setx`, and the worker must set it
  in its subprocess env. Without it Windows uses cp1252: `engine/test_engine.py:822` `read_text()` cannot decode the
  engine source (CI failure 07.11.), and piped `print` of `→` (bound NOTE) raises UnicodeEncodeError. The user applied the
  engine change request (`read_text(encoding="utf-8")` at line 822) on main on 07.11.; `test_engine.py` is not
  fingerprinted. The engine session must carry this line into the next engine version.
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

**Plan:** PR 1 foundation → user runs tests + one real F5 run from the repo → PR 2 local API → one real run via
`localhost:8000/docs` → PR 3 ngrok → test from the phone → Lovable UI (prompt pack from Claude, one screen at a time)
→ hardening. Claude works on its branch and opens a PR; the user merges and pulls with GitHub Desktop.
