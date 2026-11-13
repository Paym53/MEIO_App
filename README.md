# MEIO App

A web app around the finished MEIO engine (multi-echelon inventory optimization for FG1 "Erdbeere Mix große Dose").
You upload the Excel template, the app checks it, runs the optimization on your PC (where the Seeker license is) and
shows the results: what to commit now, the (s,S) policy, fill rates, stock, waste and cost.

```
Browser → Lovable web app → ngrok tunnel → local API on your PC → engine + Seeker license
```

**Status:** engine v18.1, local API v3, start script and the Lovable UI are in place. Next: what-if editing.

## What the app must do

These scenarios are the contract. Each one becomes an automated test.

1. **Invalid data:** if a template has errors, uploading it shows *every* error, word for word from the engine, and no run starts.
2. **Honest headline:** after a run, the headline is the cost on fresh scenarios with "N of 90 rules met". The feasibility
   line and the bound NOTE are visible without scrolling, and every missed rule is listed by channel and week.
3. **Exact commit-now:** every commit-now quantity equals the `Commit_Now` sheet of the results workbook.
4. **Fair comparison:** two runs are compared on cost only if they meet the same number of rules; otherwise the app shows a warning.
5. **Protected PC:** a request without the right token is refused (401) and does nothing.
6. **One run at a time:** while a run is in progress, a second one is refused.
7. **Safe what-ifs:** a what-if changes only a copy of the original upload, never the upload itself. The engine checks
   the copy like any workbook, invalid edits are refused in the engine's words, and every what-if run lists what it changed.

## What is in this repo

| File | Purpose |
|---|---|
| `engine/` | the engine bundle, **never edited here** (a test checks the fingerprints) |
| `README.md` | this guide |
| `CLAUDE.md` | rules, decisions and status for Claude |
| `requirements.txt` | exact package versions (= your `seeker_env`) |
| `pyproject.toml` | settings of the two checks (ruff linter, pytest tests) |
| `.github/workflows/ci.yml` | GitHub runs all checks on every pull request (Linux and Windows) plus a secret scan |
| `.gitignore`, `.gitattributes` | keep secrets, data and runs out of git; keep files byte-exact on Windows |
| `api/server.py` | the local API: endpoints, token check, run history; starts one worker per run |
| `api/worker.py` | runs the engine once (validate or optimize) and writes the results; the only file that uses the engine |
| `api/test_api.py` | the app's tests: the seven scenarios above, end to end |
| `api/openapi.json` | the frozen API contract (what the Lovable UI is built against) |
| `start_worker.bat` | double-click: starts the API and the ngrok tunnel |
| `local_settings.example.bat` | template for your settings (token, ngrok domain); your copy `local_settings.bat` stays private |
**Never in git:** `license.sio`, `local_settings.bat`, real data (`data/`), runs (`runs/`), results workbooks.

## Setup on your PC (once)

1. **Clone:** in GitHub Desktop choose *File → Clone repository → Paym53/MEIO_App*, local path for example `C:\dev\MEIO_App`.
2. **UTF-8 mode:** open Command Prompt and run the line below once, then **close it, open a new one and restart Spyder**.
   Windows otherwise reads text files as cp1252, which breaks one engine test and the engine's `→` output in the app.
   (UTF-8 is the default from Python 3.15, so this is harmless for other programs.)
   ```bat
   setx PYTHONUTF8 1
   ```
3. **Install packages:** in the new Command Prompt run:
   ```bat
   cd C:\dev\MEIO_App
   C:\Users\ruben\seeker_env\Scripts\python.exe -m pip install -r requirements.txt
   ```
4. **License:** copy your `license.sio` into `C:\dev\MEIO_App\engine\` (it is gitignored).
5. **Check:** run the tests. Every test must pass (`… passed`, no `failed`); the engine alone has 141 tests (about 5 min in total).
   ```bat
   C:\Users\ruben\seeker_env\Scripts\python.exe -m pytest
   ```

## Daily use

**Until the app exists:** open `engine\meio_seeker_stochastic.py` in Spyder and press F5. It runs on the example data
in `engine\MEIO_Data_Template.xlsx` and writes `engine\MEIO_Results_stochastic.xlsx` (gitignored).

**Your data:** keep real workbooks in `C:\dev\MEIO_App\data\` (gitignored) and run them through the app. Leave the template in
`engine\` as it is: the tests depend on its example data, and GitHub Desktop would show any edit as a change.

**Which run settings:**

| Use | Scenarios | Search time | Takes about |
|---|---|---|---|
| Decisions you act on | 500 | 700 s | 18–19 min |
| Quick what-if (never act on it) | 100 | 150 s | 6 min |

- **Keep the PC on:** the app keeps it awake during a run. Don't shut the lid of a laptop or turn the PC off: a sleeping PC stretched a 5-minute search to over 3 hours (12.11.).
- **Silence is normal:** Seeker can print nothing for minutes while it searches.

**What-ifs (from the Master data pages):** pick a run made from an uploaded workbook, switch on "Edit (what-if)" and
change the values shown as fields (for demand either single quantiles or a shift such as "+2,000 units" for one channel
and week; stock lots, open orders and possible lead times can also be added or removed), check, then run. A
what-if always starts from the original upload, never from another what-if. The fill-rate target and the review period
are changed only here, not in the run dialog. The app's own rules for this (W01–W08) are listed on Explainability.

**Data and runs:** the top bar first picks the **data** (an upload, or a what-if shown in blue with its changes on
click), then a **run** on that data. *Run optimization → Same data* (or *Run again* in Run history) runs the selected
data again, byte for byte, with other solver settings, e.g. a quick what-if again as a decision run.

**Since engine v18.1 (13.11.):**
- **Workbooks:** workbooks made for the old engine are refused. Copy your values by hand into the new
  `engine\MEIO_Data_Template.xlsx` layout; the app never converts them. Two new inputs:
  - one fill-rate target per product (Items!TargetFillRate, 95 % in the template)
  - the serving order of the channels (Channels!ShortagePriority, 1 = served first)
- **Old runs:** runs made before the update are no longer shown. Empty the folder `runs\` once after updating.

## Put the app online (once)

The API runs on your PC; ngrok gives it a fixed internet address so the web app can reach it.

1. **ngrok account:** sign up for free at https://ngrok.com.
2. **Install ngrok:** in Command Prompt run the line below, then close Command Prompt and open a new one. `ngrok version` should now print a version.
   ```bat
   winget install ngrok.ngrok
   ```
3. **Connect ngrok to your account:** in the ngrok dashboard open *Your Authtoken*, copy it, and run:
   ```bat
   ngrok config add-authtoken <your ngrok authtoken>
   ```
4. **Your address:** in the ngrok dashboard open *Domains* and note your free domain, e.g. `something.ngrok-free.app`.
5. **Your settings:** in the repo folder, copy `local_settings.example.bat` and rename the copy to `local_settings.bat`.
   Open it in Notepad (right-click → *Edit*) and fill in:
   - `MEIO_TOKEN`: your API token (the file explains how to make one)
   - `NGROK_DOMAIN`: your domain from step 4, without `https://`

   `local_settings.bat` holds your secret token. It is gitignored: GitHub Desktop never shows or uploads it.

## Start and stop the app

- **Start:** double-click `start_worker.bat`. The API runs in that window (its log scrolls there), and the tunnel runs in a
  second, minimised window "MEIO tunnel". Leave both open while the app is in use.
- **Check from outside:** on your phone (Wi-Fi off), open `https://<your domain>/health`. After ngrok's warning page
  (*Visit Site*) you should see `{"detail":"Missing or wrong token."}`: the PC is reachable **and** protected.
  `https://<your domain>/docs` → *Authorize* with your token → `GET /health` → *Execute* shows `"status": "ok"`.
- **Use on this PC only:** leave `NGROK_DOMAIN` empty; the API is then at http://127.0.0.1:8000/docs.
- **Stop:** press Ctrl+C in the API window (answer `Y`), then close the "MEIO tunnel" window.
- **After pulling an update:** stop both windows and start `start_worker.bat` again. The API loads its code once at
  start; `/health` shows `"restart_needed": true` (and the web app's status badge says so) until you restart.

The `/docs` page is visible to anyone who knows the address, but it only describes the API: every action needs the token.
Each run is kept in `runs\<id>\` (input, log, results; gitignored). Runs of older engine versions are not shown.

## Updating

- **Claude's changes:** Claude opens a pull request. Check that the checks are green, merge it on GitHub, then click
  *Fetch origin → Pull origin* in GitHub Desktop.
- **New engine version from the engine session:** replace the files in `engine\` wholesale, run the tests, and commit
  in GitHub Desktop.

## Checks (they fail the build, not just warn)

| Check | Command | What it guarantees |
|---|---|---|
| Lint | `python -m ruff check .` | no likely bugs, sorted imports, no function more complex than 10 branches |
| Tests | `python -m pytest` | engine = independent reference in every scenario, frozen output tables, engine files unchanged; the app's seven scenarios end to end; at least 90 % of the app's code tested |
| Secret scan | CI only (gitleaks) | no token, key or password in the git history |
