"""
================================================================================
 MEIO local API: the web API that runs on the license PC (behind ngrok)
================================================================================
Start:   python api/server.py              (http://127.0.0.1:8000, interactive docs at /docs)
Update the frozen contract after a deliberate API change:   python api/server.py --openapi

Every request needs the header  Authorization: Bearer <MEIO_TOKEN>.  The server never imports the engine:
each validation or run is one api/worker.py process (one engine run per process, CLAUDE.md §2), and every number
it returns was written by that worker from the engine's RunResult.

Run folder  runs/<id>/ :  input.xlsx · request.json (settings) · status.json · label.json · log.txt · input.json ·
                          result.json · results.xlsx · changes.json (what-if runs) · sheets.json (cache)

What-if (API update 2): edits always go into a copy of an ORIGINAL upload (never of another what-if); the worker writes
the copy, the engine checks it, and the run keeps the change list. The app's own rules are APP_RULES (/app-rules).

  0. SETTINGS ........ environment variables (set in local_settings.bat), named constants, the app's rules
  1. SECURITY ........ token check
  2. RUN FILES ....... status.json read/write, run ids
  3. PROGRESS ........ current phase, time left, log tail (from the worker's log)
  4. WORKER .......... start, watch, cancel; recover runs cut off by a restart
  5. API ............. response shapes and endpoints
  6. MAIN
================================================================================
"""
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

# ============================================================================
# 0. SETTINGS
# ============================================================================
ROOT = Path(__file__).resolve().parent.parent
TOKEN = os.environ.get("MEIO_TOKEN", "")                                   # secret, at least 32 characters
LICENSE_FILE = Path(os.environ.get("MEIO_LICENSE_FILE", ROOT / "engine" / "license.sio"))
RUNS_DIR = Path(os.environ.get("MEIO_RUNS_DIR", ROOT / "runs"))
ALLOWED_ORIGINS = os.environ.get("MEIO_ALLOWED_ORIGINS", r"https://[a-z0-9-]+\.(lovable\.app|lovableproject\.com)")
HOST, PORT = "127.0.0.1", int(os.environ.get("MEIO_PORT", "8000"))        # localhost only: ngrok forwards to it

WORKER = ROOT / "api" / "worker.py"
ENGINE_FILE = ROOT / "engine" / "meio_seeker_stochastic.py"
OPENAPI_FILE = ROOT / "api" / "openapi.json"
CODE_FILES = (ROOT / "api" / "server.py", WORKER, ENGINE_FILE)            # changed on disk → the API needs a restart
MIN_TOKEN_LENGTH = 32
MAX_UPLOAD_MB = 20
DEFAULT_TIME_LIMIT, MAX_TIME_LIMIT = 250, 3600                             # Seeker search time [s]
DEFAULT_OOS, MIN_OOS, MAX_OOS = 2500, 500, 20000                           # fresh scenarios for the check
MAX_SCENARIOS = 2000                                                       # NumScenarios override (optimizer)
DEFAULT_SETTINGS = {"time_limit": DEFAULT_TIME_LIMIT, "out_of_sample_scenarios": DEFAULT_OOS,
                    "feasibility_check": True, "start_policy_out_of_sample": False, "overrides": None}
API_VERSION = 3                       # recorded in every run's status.json; runs of older APIs (engine before v18.1,
                                      # other result layout) are not listed or served (user decision 13.11.)
VALIDATE_TIMEOUT_SECONDS = 120
# Expected run time (engine v18.1, user's PC 12.–13.11.; engine-session figures in the hand-over note):
#   reading/builds/gaps 20 s + 0.1 s per optimizer scenario, feasibility check 21 s,
#   start check: ETA_Z_STEPS z steps of 8 s + 0.06 s per scenario each (≈ 15 s at 120, 20 s at 200, 38 s at 500)
#     + one more build and start evaluation; a start repair (4c lines) adds up to 6 more evaluations,
#   search = time limit + 10 % + 10 s, out-of-sample check 0.033 s per fresh scenario (twice with the start policy).
#   Totals: 200 scenarios / 300 s ≈ 9.5 min, 500 / 700 s ≈ 18 min (engine session: ≈ 10 and 19 min).
ETA_BASE_SECONDS, ETA_PER_SCENARIO_SECONDS, ETA_FEASIBILITY_SECONDS = 20.0, 0.1, 21.0
ETA_Z_STEPS, ETA_STEP_SECONDS, ETA_STEP_PER_SCENARIO_SECONDS, ETA_SECOND_BUILD_SECONDS = 3, 8.0, 0.06, 12.0
ETA_REPAIR_STEPS = 6
ETA_ASSUMED_SCENARIOS = 200                                                # Excel NumScenarios of the template
ETA_OOS_SECONDS_PER_SCENARIO, ETA_SEARCH_OVERRUN, ETA_SEARCH_OVERHEAD_SECONDS = 0.033, 0.10, 10.0
WATCHDOG_FACTOR = 3.0                                                      # a run taking 3 × its estimate is stopped
EXIT_WAIT_SECONDS = 30.0                                                   # a finished worker gets this to exit
LOG_TAIL_LINES = 40
RUN_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")
PHASE_START = re.compile(r"^\[\d\d:\d\d:\d\d\] (.+) \.\.\.$")              # engine phase lines: "[19:52:44] 5. … ..."
Z_STEP_PHASE = "4. Start check: Seeker evaluates the start with z"             # one per z step (phase names repeat:
REPAIR_PHASE = "4c. Start repair"                                              # lines are counted, never keyed)
START_CHECK_DONE = "Start check: Seeker starts from z"
MAX_EDITS = 5000                                                           # edits per what-if (one request)

# The app's own rules (what-if editing, run settings, comparisons), shown on Explainability next to the engine's
# Model_Assumptions and in the same columns. The engine's rules live in the template; these belong to the app.
APP_RULE_COLUMNS = ["ID", "Topic", "Title", "Rule / mechanic", "Source", "Status", "Where"]
APP_RULES = [
    ["W01", "What-if", "Edits go into a copy",
     "Every what-if edit is written into a copy of the original uploaded workbook (openpyxl, values only). "
     "The original upload is never changed; its sha256 fingerprint stays the same. The engine checks the "
     "copy exactly like an uploaded workbook (K19).",
     "User decision 13.11.", "Decided", "api/worker.py write_copy"],
    ["W02", "What-if", "Only input cells; rows only for stock, open orders and lead times",
     "Only input cells of the template's input sheets can be edited. Key columns (IDs), cells Excel "
     "calculates (RowCheck, MeanApprox, FreshLotSales, WeeksLate …) and, in Settings, everything except "
     "PlanningStartDate, HorizonPeriods and ReviewPeriod stay read-only. Rows can be added or removed only "
     "for initial stock lots, open orders and lead-time branches (one row per possible lead time with its "
     "probability); a new row takes only its input columns and goes into the first empty row of the sheet.",
     "User decisions 13.11. and 14.11.", "Decided", "api/worker.py editable, add_row, delete_row"],
    ["W03", "Demand", "Shifting a forecast week",
     "A shift of x units on one channel and week adds x to all 10 quantiles (Q05 … Q95) of that row: the "
     "forecast keeps its shape and moves up (or down for a negative x). The quantiles can also be edited "
     "one by one. The engine then checks the row like any workbook (for example no negative or falling "
     "quantiles).",
     "User decision 13.11.", "Decided", "api/worker.py apply_edit"],
    ["W04", "What-if", "Target and review period only in the master data",
     "The fill-rate target (Items!TargetFillRate) and the review period (Settings!ReviewPeriod) are "
     "changed only as master data (a what-if edit), never per run; the engine's OPTIMIZATION_FILL_RATE "
     "stays off.",
     "User decision 13.11.", "Decided", "api/worker.py SETTING_CONSTANTS"],
    ["W05", "What-if", "Always based on an original",
     "A what-if always starts from an originally uploaded workbook, never from another what-if. Its change "
     "list holds every difference to that original (sheet, row, key, field, original value, new value).",
     "User decision 13.11.", "Decided", "api/server.py create_run"],
    ["W06", "Comparison", "Fair comparison",
     "Costs of two runs are compared only when both meet the same number of rules and use the same "
     "fill-rate target: there is no lost-sales cost, so a plan that misses rules or aims lower always "
     "looks cheaper.",
     "User decisions 07.11. and 13.11.", "Decided", "dashboard Scenario comparison"],
    ["W07", "Run settings", "Solver settings are set per run",
     "Search time, optimizer scenarios, random seed, fresh scenarios, the feasibility check and the start "
     "policy on fresh scenarios are set per run in the Run dialog, not in the master data. Optimizer "
     "scenarios and random seed replace the Excel values for that run only (engine K20).",
     "User decision 09.11.", "Decided", "Run dialog; api/server.py"],
    ["W08", "Run settings", "Run again on the same data",
     "Any run can be run again with other solver settings on exactly the same data: the API copies that "
     "run's workbook byte for byte (same sha256 fingerprint). A what-if run again keeps its original and its "
     "change list, so it is still a what-if of the original, not a what-if of a what-if (W05). Runs with the "
     "same fingerprint use the same data; the dashboard groups them under one data set.",
     "User decision 14.11.", "Decided", "api/server.py same_data; dashboard top bar and Run dialog"],
]


# ============================================================================
# 1. SECURITY
# ============================================================================
bearer = HTTPBearer(auto_error=False, description="The MEIO_TOKEN from local_settings.bat")


def require_token(cred: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
    ok = cred is not None and len(TOKEN) >= MIN_TOKEN_LENGTH and \
        hmac.compare_digest(cred.credentials.encode(), TOKEN.encode())
    if not ok:
        raise HTTPException(401, "Missing or wrong token.", headers={"WWW-Authenticate": "Bearer"})


# ============================================================================
# 2. RUN FILES
# ============================================================================
def now():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def write_json(path, obj):
    tmp = Path(path).with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    for attempt in range(20):                  # Windows refuses to replace a file another process is reading
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)


def is_original(folder):
    """A run made from an uploaded workbook (not a what-if): only these can be the base of a what-if (W05)."""
    return read_status(folder).get("base_run") is None


def current_api(folder):
    """Runs made by this API version (older runs have another result layout and are no longer shown)."""
    return read_status(folder).get("api_version", 0) >= API_VERSION


def run_dir(run_id):
    if not RUN_ID.match(run_id) or not (RUNS_DIR / run_id / "status.json").exists():
        raise HTTPException(404, f"No run with id {run_id!r}.")
    if not current_api(RUNS_DIR / run_id):
        raise HTTPException(404, f"Run {run_id!r} was made with an older engine and is no longer shown.")
    return RUNS_DIR / run_id


def read_status(folder):
    return json.loads((Path(folder) / "status.json").read_text(encoding="utf-8"))


def read_label(folder):
    """Label and note live in their own file, so they never collide with the worker writing status.json."""
    path = Path(folder) / "label.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def run_view(folder):
    return {**read_status(folder), **read_label(folder)}


def update_status(folder, **fields):
    status = read_status(folder)
    status.update(fields)
    write_json(Path(folder) / "status.json", status)
    return status


def code_stamp():
    """Modification times of the app's code. The API loads its code once; after a pull it must be restarted."""
    return tuple(f.stat().st_mtime_ns if f.exists() else None for f in CODE_FILES)


STARTED_WITH = code_stamp()


def new_run_id():
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def read_upload(file: UploadFile):
    """The uploaded workbook as bytes; refuses anything that is not an .xlsx file of sensible size."""
    data = file.file.read(MAX_UPLOAD_MB * 2**20 + 1)
    if len(data) > MAX_UPLOAD_MB * 2**20:
        raise HTTPException(413, f"The file is larger than {MAX_UPLOAD_MB} MB.")
    if not (file.filename or "").lower().endswith(".xlsx") or not data.startswith(b"PK\x03\x04"):
        raise HTTPException(400, "Please upload the MEIO template as an Excel .xlsx file.")
    return data


# ============================================================================
# 3. PROGRESS
# ============================================================================
def settings_of(status):
    """The run's settings (runs from before settings existed only recorded the time limit)."""
    recorded = status.get("settings", {})
    return {**DEFAULT_SETTINGS, "time_limit": status.get("time_limit", DEFAULT_TIME_LIMIT), **recorded}


def step_seconds(scenarios):
    return ETA_STEP_SECONDS + ETA_STEP_PER_SCENARIO_SECONDS * scenarios


def expected_seconds(settings, z_steps=ETA_Z_STEPS, repair=False):
    """Expected run time. z_steps / repair: the start check's real course, once the log shows it."""
    s = {**DEFAULT_SETTINGS, **settings}
    n = (s["overrides"] or {}).get("NumScenarios", ETA_ASSUMED_SCENARIOS)
    check = ETA_OOS_SECONDS_PER_SCENARIO * s["out_of_sample_scenarios"] * (2 if s["start_policy_out_of_sample"] else 1)
    feasibility = ETA_FEASIBILITY_SECONDS if s["feasibility_check"] else 0.0
    start = z_steps * step_seconds(n) + (ETA_SECOND_BUILD_SECONDS if z_steps else 0.0) + \
        (ETA_REPAIR_STEPS * step_seconds(n) if repair else 0.0)
    search = float(s["time_limit"]) * (1 + ETA_SEARCH_OVERRUN) + ETA_SEARCH_OVERHEAD_SECONDS
    return ETA_BASE_SECONDS + ETA_PER_SCENARIO_SECONDS * n + feasibility + start + search + check


def start_check_course(phases, lines):
    """(z steps to plan for, repair seen) from the log: every z step and repair step prints its own phase line; while
    the check is still raising z, plan for at least one more step (the engine session's default: 3 steps)."""
    seen = sum(p.startswith(Z_STEP_PHASE) for p in phases)
    done = any(START_CHECK_DONE in ln for ln in lines) or any(p.startswith("5. Seeker optimization") for p in phases)
    steps = seen if done else max(ETA_Z_STEPS, seen + 1)
    return steps, any(p.startswith(REPAIR_PHASE) for p in phases)


def progress(folder, status):
    """Current engine phase, estimated seconds left and the last log lines (all read from the worker's log)."""
    log = Path(folder) / "log.txt"
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines() if log.exists() else []
    phases = [m.group(1) for m in map(PHASE_START.match, lines) if m]
    left = None
    if status["state"] == "running":
        elapsed = (dt.datetime.now().astimezone() - dt.datetime.fromisoformat(status["started"])).total_seconds()
        steps, repair = start_check_course(phases, lines)
        left = max(0.0, expected_seconds(settings_of(status), steps, repair) - elapsed)
    return {"phase": phases[-1] if phases else None, "seconds_left": left, "log_tail": lines[-LOG_TAIL_LINES:]}


# ============================================================================
# 4. WORKER — at most one run at a time
# ============================================================================
LOCK = threading.Lock()
CURRENT = {"id": None, "proc": None, "thread": None, "cancelled": False}


def worker_env():
    env = dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1")         # UTF-8: the engine prints "→" (CLAUDE.md §9)
    env.pop("MEIO_TOKEN", None)                                            # the worker never needs the secret
    return env


def busy():
    """A run is in progress while its worker is alive AND its state is still 'running'. A finished worker can take a
    moment to exit after writing its final state (Python shutdown; on Windows noticeably), and is not busy then."""
    proc, run_id = CURRENT["proc"], CURRENT["id"]
    return proc is not None and run_id is not None and proc.poll() is None and \
        read_status(RUNS_DIR / run_id)["state"] == "running"


def let_finished_worker_exit():
    """Before a new run starts: wait for the previous, finished worker to exit; stop it if it hangs (its results
    are already written)."""
    proc = CURRENT["proc"]
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.wait(timeout=EXIT_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def watch(run_id, proc, folder, settings):
    """Waits for the worker; stops it after WATCHDOG_FACTOR × the expected time; records crashes."""
    try:
        code = proc.wait(timeout=WATCHDOG_FACTOR * expected_seconds(settings))
    except subprocess.TimeoutExpired:
        proc.kill()
        code = proc.wait()
        update_status(folder, state="failed", finished=now(), message="Stopped: the run took far longer than expected.")
    status = read_status(folder)
    if CURRENT["cancelled"]:
        update_status(folder, state="cancelled", finished=now(), message="Cancelled by the user.")
    elif status["state"] == "running":                                    # the worker died without writing a result
        update_status(folder, state="failed", finished=now(),
                      message=f"The worker stopped unexpectedly (exit code {code}); see the log.")
    with LOCK:
        if CURRENT["id"] == run_id:
            CURRENT.update(id=None, proc=None, thread=None, cancelled=False)


def start_run(data, file_name, settings, label_and_note=None, origin=None):
    """origin = {"base_run": id, "changes": [...]} for a what-if (its input.xlsx is the checked copy)."""
    with LOCK:
        if busy():
            raise HTTPException(409, f"A run is already in progress ({CURRENT['id']}).")
        let_finished_worker_exit()
        run_id = new_run_id()
        folder = RUNS_DIR / run_id
        folder.mkdir(parents=True)
        (folder / "input.xlsx").write_bytes(data)
        write_json(folder / "request.json", settings)
        write_json(folder / "label.json", label_and_note or {})
        if origin:
            write_json(folder / "changes.json", origin["changes"])
        file = {"name": file_name, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        write_json(folder / "status.json", {"id": run_id, "api_version": API_VERSION, "state": "running",
                                            "created": now(), "started": now(),
                                            "finished": None, "time_limit": settings["time_limit"],
                                            "settings": settings, "file": file, "message": "Running.", "errors": [],
                                            "base_run": origin["base_run"] if origin else None,
                                            "changes": len(origin["changes"]) if origin else 0})
        cmd = [sys.executable, "-u", str(WORKER), "run", str(folder), str(LICENSE_FILE)]
        with open(folder / "log.txt", "wb") as log:
            proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=str(ROOT), env=worker_env())
        thread = threading.Thread(target=watch, args=(run_id, proc, folder, settings), daemon=True)
        CURRENT.update(id=run_id, proc=proc, thread=thread, cancelled=False)
        thread.start()
    return run_id


def recover_interrupted_runs():
    """Runs still marked 'running' when the API starts were cut off by a restart or a crash."""
    for f in RUNS_DIR.glob("*/status.json") if RUNS_DIR.exists() else []:
        if read_status(f.parent)["state"] == "running":
            update_status(f.parent, state="failed", finished=now(),
                          message="Interrupted: the API stopped during this run.")


def worker_call(args, answer):
    """One short worker process (validate, sheets, whatif); its JSON answer, or 500 with the worker's output."""
    done = subprocess.run([sys.executable, "-u", str(WORKER), *map(str, args)], cwd=str(ROOT), env=worker_env(),
                          capture_output=True, timeout=VALIDATE_TIMEOUT_SECONDS)
    if not Path(answer).exists():
        tail = done.stdout.decode("utf-8", "replace")[-2000:] + done.stderr.decode("utf-8", "replace")[-2000:]
        raise HTTPException(500, f"The worker failed: {tail}")
    return json.loads(Path(answer).read_text(encoding="utf-8"))


def validate_upload(data):
    with tempfile.TemporaryDirectory() as tmp:
        src, answer = Path(tmp) / "input.xlsx", Path(tmp) / "answer.json"
        src.write_bytes(data)
        return worker_call(["validate", src, answer], answer)


def sheets_of(folder):
    """The run's editable sheets (worker 'sheets'), cached in the run folder: input.xlsx never changes."""
    cache = Path(folder) / "sheets.json"
    if not cache.exists():
        worker_call(["sheets", Path(folder) / "input.xlsx", cache], cache)
    return json.loads(cache.read_text(encoding="utf-8"))


def original_base(base_run):
    folder = run_dir(base_run)
    if not is_original(folder):
        raise HTTPException(409, f"Run {base_run!r} is itself a what-if. A what-if always starts from the original "
                                 f"upload: use run {read_status(folder)['base_run']!r} as the base.")
    return folder


def same_data(run_id):
    """(bytes, file name) of an earlier run's input.xlsx: a run again on the same data (W08)."""
    folder = run_dir(run_id)
    return (folder / "input.xlsx").read_bytes(), (read_status(folder).get("file") or {}).get("name", run_id)


def same_origin(run_id):
    """A what-if run again keeps its original run and change list (still no chaining, W05/W08); None for uploads."""
    folder = run_dir(run_id)
    base = read_status(folder).get("base_run")
    changes = folder / "changes.json"
    return {"base_run": base, "changes": json.loads(changes.read_text(encoding="utf-8")) if changes.exists() else []} \
        if base else None


def whatif_copy(base_run, edits):
    """(answer, bytes of the checked copy or None): the worker writes the edits into a copy of the original run's
    workbook and runs the engine's checks on it. The original input.xlsx is only read (W01)."""
    folder = original_base(base_run)
    with tempfile.TemporaryDirectory() as tmp:
        edits_file, copy, answer = Path(tmp) / "edits.json", Path(tmp) / "input.xlsx", Path(tmp) / "answer.json"
        edits_file.write_text(json.dumps(edits, ensure_ascii=False), encoding="utf-8")
        result = worker_call(["whatif", folder / "input.xlsx", edits_file, copy, answer], answer)
        return result, (copy.read_bytes() if result["valid"] and copy.exists() else None)


# ============================================================================
# 5. API
# ============================================================================
class Health(BaseModel):
    status: str
    engine_version: str | None
    license_found: bool
    busy: bool
    current_run: str | None
    test_double: bool
    restart_needed: bool = False          # the app's code changed on disk (an update was pulled): restart the API


class Table(BaseModel):
    columns: list[str]
    rows: list[list[Any]]


class InputSettings(BaseModel):
    horizon_weeks: int
    review_period_weeks: int
    start_date: str
    scenarios: int
    random_seed: int
    time_limit_seconds: float | None
    rules_from_first_controllable_week: bool


class InputData(BaseModel):
    """The master data exactly as the engine read it from the workbook (rows and fields named as in the template)."""
    settings: InputSettings
    product: dict[str, Any]               # id, unit, shelf life, fill_rate_target (Items, H04), costs, production
    raw_materials: list[dict[str, Any]]   # per RM: BOM, shelf life, freshness rule, MOQ, multiple, max, LT, costs
    channels: list[dict[str, Any]]        # id, serving_order (ShortagePriority, 1 = first), min_remaining_shelf_life
    demand: dict[str, Any]                # quantile_levels; quantiles[channel][week 1..H] = 10 values
    initial_stock: dict[str, list[dict[str, Any]]]
    open_orders: dict[str, list[dict[str, Any]]]
    warnings: list[str]
    tables: dict[str, Table]              # Locations, Lanes, Items, Sourcing, Model_Assumptions as in the template


class Validation(BaseModel):
    valid: bool
    errors: list[str]                     # the engine's messages, one problem per entry
    warnings: list[str] = []              # do not stop a run (e.g. a max order not a multiple of the order step)
    input: InputData | None = None        # the master data, when the workbook is valid


class Edit(BaseModel):
    """One what-if edit: a new value for one input cell, a shift of all 10 quantiles of one Demand row (W03), or a
    row added to / removed from Init_Inventory, Open_Orders or LeadTimeDist (W02)."""
    sheet: str                            # e.g. "Sourcing"
    row: int | None = None                # Excel row number, as GET /runs/{id}/sheets gives it (not with add)
    column: str | None = None             # header of the cell's column, e.g. "MaxOrderQty"
    value: Any = None                     # the new value (with column)
    shift: float | None = None            # Demand only: units added to Q05 … Q95 of that row
    add: dict[str, Any] | None = None     # a new row: {column header: value} (input columns only)
    delete: bool = False                  # remove the row (true)


class WhatIf(BaseModel):
    base_run: str                         # an ORIGINAL run (made from an upload), never a what-if (W05)
    edits: list[Edit] = Field(max_length=MAX_EDITS)


class Change(BaseModel):
    sheet: str
    row: int
    key: str                              # the row's key cells, e.g. "AMZ · 12" (channel · week)
    column: str
    old: Any                              # value in the original upload
    new: Any


class WhatIfValidation(Validation):
    changes: list[Change] = []            # every difference to the original upload


class EditRow(BaseModel):
    row: int                              # Excel row number
    key: str
    values: list[Any]                     # in column order; cells Excel calculates show its last value (or null)
    editable: list[bool]                  # in column order


class EditSheet(BaseModel):
    columns: list[str]
    key_columns: list[str]
    rows: list[EditRow]


class Sheets(BaseModel):
    sheets: dict[str, EditSheet]          # Settings, Items, Channels, Demand, Init_Inventory, Open_Orders, Sourcing, …


class RunSummary(BaseModel):
    id: str
    state: str                            # running | done | invalid | failed | cancelled
    created: str
    finished: str | None
    time_limit: float
    message: str
    summary: dict[str, Any] | None = None  # done runs: cost_out_of_sample, rules_met_out_of_sample, rules_total
    test_double: bool = False             # True = run on the Seeker test double (development only)
    settings: dict[str, Any] | None = None  # the run settings (see POST /runs)
    file: dict[str, Any] | None = None    # uploaded workbook: name, sha256 fingerprint, bytes
    base_run: str | None = None           # what-if runs: the original run it was made from (W05)
    changes: int = 0                      # what-if runs: number of changed cells (GET /runs/{id}/changes)
    label: str = ""
    note: str = ""


class RunLabel(BaseModel):
    label: str | None = Field(None, max_length=80)
    note: str | None = Field(None, max_length=1000)


class RunStatus(RunSummary):
    errors: list[str]
    phase: str | None
    seconds_left: float | None
    log_tail: list[str]


class MissedRule(BaseModel):
    channel: str
    week: int
    target: float
    expected_fill_rate: float             # mean over the fresh scenarios of served ÷ demand: what rule H05 judges
    fully_served: float                   # share of fresh scenarios with all of the week's demand served
    bad_scenario_p10: float               # fill rate of a bad scenario (10th percentile)
    worst_5pct_average: float             # average fill rate in the worst 5 % of fresh scenarios


class StartCheck(BaseModel):
    z0: float | None                      # start safety factor from the target (K07)
    z: float | None                       # the z the search started from
    steps: list[dict[str, Any]]           # every evaluated z: z, met, cost


class Headline(BaseModel):
    cost_out_of_sample: float
    rules_total: int
    rules_met_out_of_sample: int
    rules_missed_out_of_sample: list[MissedRule]
    feasibility: str
    bound_note: str
    weakest_cell: str                     # engine Summary "Weakest cell (out-of-sample)"
    fill_rate_target: float               # the target used in this run (K08)
    fill_rate_target_excel: float         # Items!TargetFillRate
    fill_rate_target_source: str          # "Excel Items!TargetFillRate of FG1" or the run's own target
    review_period: str                    # engine Summary "Review period (A03)": which weeks this run commits
    start_policy: str                     # engine Summary "Start policy (warm start)"
    start_check: StartCheck
    start_repair_used: bool               # K21: show as a notice, not an error
    start_repair: str                     # engine Summary "Start repair (section 4c, K21)"
    start_policy_before_repair: str | None
    expected_waste: dict[str, str]        # item → engine Summary text, e.g. "31 pcs = 0.1% of expected demand"
    waste_warning: str                    # "none" or the L10 warning
    cost_in_sample: float
    rules_met_in_sample: int
    start_policy_cost: float
    saving_vs_start_in_sample: float      # engine Summary; share (0.068 = 6.8 %)
    cost_parts_in_sample: dict[str, float]
    cost_parts_out_of_sample: dict[str, float]
    scenarios_in_sample: int
    scenarios_out_of_sample: int
    seeker_status: str


class RunInfo(BaseModel):
    product_id: str
    horizon_weeks: int
    review_period_weeks: int
    first_rule_week: int | None           # weeks before it are fixed by stock on hand and open orders
    start_date: str
    time_limit_seconds: float
    scenarios_in_sample: int
    random_seed: int
    fill_rate_target: float               # the target used in this run
    fill_rate_target_excel: float         # Items!TargetFillRate (= the target used: no run's own target, W04)
    scenarios_out_of_sample: int
    feasibility_check: bool
    start_policy_out_of_sample: bool
    overrides: dict[str, int] | None
    warnings: list[str]


class Result(BaseModel):
    contract_version: int
    engine_version: str
    seeker_version: str
    test_double: bool
    headline: Headline
    run_info: RunInfo
    sheets: dict[str, Table]  # Summary, Commit_Now, Policy_sS, Decisions, FillRates, Stock_Waste, Bands, KPIs
    timings: list[tuple[str, float]]


@asynccontextmanager
async def lifespan(_app):
    if len(TOKEN) < MIN_TOKEN_LENGTH:
        raise RuntimeError(f"Set MEIO_TOKEN to a secret of at least {MIN_TOKEN_LENGTH} characters "
                           "(local_settings.bat).")
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    recover_interrupted_runs()
    yield


app = FastAPI(title="MEIO local API", version=str(API_VERSION), lifespan=lifespan,
              dependencies=[Depends(require_token)])
app.add_middleware(CORSMiddleware, allow_origin_regex=ALLOWED_ORIGINS, allow_methods=["GET", "POST", "PATCH"],
                   allow_headers=["Authorization", "Content-Type", "ngrok-skip-browser-warning"])


@app.get("/health", response_model=Health)
def health():
    src = ENGINE_FILE.read_text(encoding="utf-8") if ENGINE_FILE.exists() else ""
    m = re.search(r'^SCRIPT_VERSION = "(.+)"', src, re.M)                  # read as text: never import the engine
    return Health(status="ok", engine_version=m.group(1) if m else None, license_found=LICENSE_FILE.exists(),
                  busy=busy(), current_run=CURRENT["id"] if busy() else None,
                  test_double=os.environ.get("MEIO_TEST_DOUBLE") == "1", restart_needed=code_stamp() != STARTED_WITH)


@app.post("/validate", response_model=Validation)
def validate(file: Annotated[UploadFile, File()]):
    """Checks a workbook with the engine's own data checks. Nothing is stored."""
    return validate_upload(read_upload(file))


@app.post("/whatif/validate", response_model=WhatIfValidation)
def validate_whatif(body: WhatIf):
    """Writes the edits into a copy of the original run's workbook and checks the copy with the engine's own checks.
    Nothing is stored; the original is only read (W01). Errors are the engine's words (sheet, row, field)."""
    answer, _ = whatif_copy(body.base_run, [e.model_dump() for e in body.edits])
    return answer


@app.post("/runs", response_model=RunStatus, status_code=202)
def create_run(file: Annotated[UploadFile | None, File()] = None,
               base_run: Annotated[str | None, Form()] = None,
               edits: Annotated[str | None, Form()] = None,
               same_data_as: Annotated[str | None, Form()] = None,
               time_limit: Annotated[int, Form(ge=1, le=MAX_TIME_LIMIT)] = DEFAULT_TIME_LIMIT,
               out_of_sample_scenarios: Annotated[int, Form(ge=MIN_OOS, le=MAX_OOS)] = DEFAULT_OOS,
               feasibility_check: Annotated[bool, Form()] = True,
               start_policy_out_of_sample: Annotated[bool, Form()] = False,
               num_scenarios: Annotated[int | None, Form(ge=1, le=MAX_SCENARIOS)] = None,
               random_seed: Annotated[int | None, Form(ge=0)] = None,
               label: Annotated[str, Form(max_length=80)] = "",
               note: Annotated[str, Form(max_length=1000)] = ""):
    """Starts an optimization run in the background (refused while another run is in progress), either on an uploaded
    workbook (file), as a what-if (base_run = an original run, edits = JSON list of edits, see POST /whatif/validate;
    an invalid what-if is refused with the engine's errors, 422, and starts nothing) or again on the data of an earlier
    run (same_data_as = that run: the same workbook byte for byte, a what-if keeps its original and changes; W08).
    num_scenarios / random_seed replace the Excel NumScenarios / RandomSeed for this run only (engine rule K20).
    The fill-rate target and the review period come from the workbook only (W04)."""
    if sum(x is not None for x in (file, base_run, same_data_as)) != 1:
        raise HTTPException(400, "Send exactly one of: a workbook (file), a what-if (base_run and edits) or the "
                                 "run whose data to use again (same_data_as).")
    overrides = {k: v for k, v in (("NumScenarios", num_scenarios), ("RandomSeed", random_seed)) if v is not None}
    settings = {"time_limit": time_limit, "out_of_sample_scenarios": out_of_sample_scenarios,
                "feasibility_check": feasibility_check, "start_policy_out_of_sample": start_policy_out_of_sample,
                "overrides": overrides or None}
    label_and_note = {"label": label, "note": note}
    if file is not None:
        return get_run(start_run(read_upload(file), file.filename, settings, label_and_note))
    if same_data_as is not None:
        return get_run(start_run(*same_data(same_data_as), settings, label_and_note, same_origin(same_data_as)))
    try:
        parsed = [Edit.model_validate(e).model_dump() for e in json.loads(edits or "[]")]
    except (ValueError, TypeError) as e:
        raise HTTPException(422, f"edits must be a JSON list of edits: {e}") from e
    answer, data = whatif_copy(base_run, parsed)
    if data is None:
        raise HTTPException(422, {"message": "The what-if did not pass the engine's checks.",
                                  "errors": answer["errors"]})
    base_file = read_status(RUNS_DIR / base_run).get("file") or {}
    name = f"What-if of {base_file.get('name', base_run)}"
    origin = {"base_run": base_run, "changes": answer["changes"]}
    return get_run(start_run(data, name, settings, label_and_note, origin))


@app.get("/runs", response_model=list[RunSummary])
def list_runs():
    """Run history, newest first (runs made with the current engine only)."""
    folders = sorted(RUNS_DIR.glob("*/status.json"), reverse=True) if RUNS_DIR.exists() else []
    return [run_view(f.parent) for f in folders if RUN_ID.match(f.parent.name) and current_api(f.parent)]


@app.get("/runs/{run_id}", response_model=RunStatus)
def get_run(run_id: str):
    """Status of one run, with the current phase, estimated seconds left and the last log lines."""
    folder = run_dir(run_id)
    status = run_view(folder)
    return {**status, **progress(folder, status)}


@app.patch("/runs/{run_id}", response_model=RunSummary)
def label_run(run_id: str, body: RunLabel):
    """Sets the run's label and/or note (shown in the history and the scenario comparison)."""
    folder = run_dir(run_id)
    write_json(folder / "label.json", {**read_label(folder), **body.model_dump(exclude_none=True)})
    return run_view(folder)


@app.get("/runs/{run_id}/input", response_model=InputData)
def get_input(run_id: str):
    """The master data of the run's workbook, exactly as the engine read it."""
    folder = run_dir(run_id)
    if not (folder / "input.json").exists():
        raise HTTPException(409, "The run has no master data (the workbook could not be read).")
    return json.loads((folder / "input.json").read_text(encoding="utf-8"))


@app.get("/runs/{run_id}/sheets", response_model=Sheets)
def get_sheets(run_id: str):
    """The run's input sheets for what-if editing: per data row its Excel row number, key, values and which cells may
    be edited (W02). Edits are always made on an original run (W05)."""
    return sheets_of(run_dir(run_id))


@app.get("/runs/{run_id}/changes", response_model=list[Change])
def get_changes(run_id: str):
    """A what-if run's changes against its original (empty for a run made from an upload)."""
    path = run_dir(run_id) / "changes.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


@app.get("/app-rules", response_model=Table)
def app_rules():
    """The app's own rules (what-if editing, run settings, comparisons), in the columns of Model_Assumptions."""
    return {"columns": APP_RULE_COLUMNS, "rows": APP_RULES}


@app.get("/runs/{run_id}/result", response_model=Result)
def get_result(run_id: str):
    """Everything the engine reported, as JSON: headline, run info and the eight result tables."""
    folder = run_dir(run_id)
    state = read_status(folder)["state"]
    if state != "done":
        raise HTTPException(409, f"The run has no result (state: {state}).")
    return json.loads((folder / "result.json").read_text(encoding="utf-8"))


@app.get("/runs/{run_id}/workbook", response_class=FileResponse)
def get_workbook(run_id: str):
    """The engine's formatted results workbook."""
    folder = run_dir(run_id)
    if not (folder / "results.xlsx").exists():
        raise HTTPException(409, "The run has no results workbook.")
    return FileResponse(folder / "results.xlsx", filename=f"MEIO_Results_{run_id}.xlsx")


@app.post("/runs/{run_id}/cancel", response_model=RunStatus)
def cancel_run(run_id: str):
    folder = run_dir(run_id)
    with LOCK:
        if CURRENT["id"] != run_id or not busy():
            raise HTTPException(409, "This run is not in progress.")
        CURRENT["cancelled"] = True
        CURRENT["proc"].kill()
        thread = CURRENT["thread"]
    thread.join(timeout=30)                                                # the watcher records "cancelled"
    return get_run(folder.name)


# ============================================================================
# 6. MAIN
# ============================================================================
def write_openapi():
    OPENAPI_FILE.write_text(json.dumps(app.openapi(), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if sys.argv[1:] == ["--openapi"]:
        write_openapi()
        print(f"Wrote {OPENAPI_FILE}")
    else:
        import uvicorn

        uvicorn.run(app, host=HOST, port=PORT)
