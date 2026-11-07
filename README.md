# MEIO App

A web app around the finished MEIO engine (multi-echelon inventory optimization for FG1 "Erdbeere Mix große Dose").
You upload the Excel template, the app checks it, runs the optimization on your PC (where the Seeker license is) and
shows the results: what to commit now, the (s,S) policy, fill rates, stock, waste and cost.

```
Browser → Lovable web app → ngrok tunnel → local API on your PC → engine + Seeker license
```

**Status:** PR 1 (foundation): engine, checks, CI. Next: PR 2 local API, PR 3 ngrok, then the Lovable UI.

## What the app must do

These scenarios are the contract. Each one becomes an automated test.

1. **Invalid data:** if a template has errors, uploading it shows *every* error, word for word from the engine, and no run starts.
2. **Honest headline:** after a run, the headline is the cost on fresh scenarios with "N of 90 rules met". The feasibility
   line and the bound NOTE are visible without scrolling, and every missed rule is listed by channel and week.
3. **Exact commit-now:** every commit-now quantity equals the `Commit_Now` sheet of the results workbook.
4. **Fair comparison:** two runs are compared on cost only if they meet the same number of rules; otherwise the app shows a warning.
5. **Protected PC:** a request without the right token is refused (401) and does nothing.
6. **One run at a time:** while a run is in progress, a second one is refused.

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

Coming next: `api/` (PR 2), then `start_worker.bat` and `local_settings.example.bat` (PR 3).
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
5. **Check:** run the tests. The expected result is `58 passed`.
   ```bat
   C:\Users\ruben\seeker_env\Scripts\python.exe -m pytest
   ```

## Daily use

**Until the app exists:** open `engine\meio_seeker_stochastic.py` in Spyder and press F5. It runs on the example data
in `engine\MEIO_Data_Template.xlsx` and writes `engine\MEIO_Results_stochastic.xlsx` (gitignored).

**Your data:** keep real workbooks in `C:\dev\MEIO_App\data\` (gitignored) and run them through the app. Leave the template in
`engine\` as it is: the tests depend on its example data, and GitHub Desktop would show any edit as a change.

## Updating

- **Claude's changes:** Claude opens a pull request. Check that the checks are green, merge it on GitHub, then click
  *Fetch origin → Pull origin* in GitHub Desktop.
- **New engine version from the engine session:** replace the files in `engine\` wholesale, run the tests, and commit
  in GitHub Desktop.

## Checks (they fail the build, not just warn)

| Check | Command | What it guarantees |
|---|---|---|
| Lint | `python -m ruff check .` | no likely bugs, sorted imports, no function more complex than 10 branches |
| Tests | `python -m pytest` | engine = independent reference in every scenario, frozen output tables, engine files unchanged |
| Secret scan | CI only (gitleaks) | no token, key or password in the git history |
