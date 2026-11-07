"""
================================================================================
 MEIO local API: the web API that runs on the license PC (behind ngrok)
================================================================================
Start:   python api/server.py              (http://127.0.0.1:8000, interactive docs at /docs)
Update the frozen contract after a deliberate API change:   python api/server.py --openapi

Every request needs the header  Authorization: Bearer <MEIO_TOKEN>.  The server never imports the engine:
each validation or run is one api/worker.py process (one engine run per process, CLAUDE.md §2), and every number
it returns was written by that worker from the engine's RunResult.

Run folder  runs/<id>/ :  input.xlsx · status.json · log.txt · result.json · results.xlsx

  0. SETTINGS ........ environment variables (set in local_settings.bat) and named constants
  1. SECURITY ........ token check
  2. RUN FILES ....... status.json read/write, run ids
  3. PROGRESS ........ current phase, time left, log tail (from the worker's log)
  4. WORKER .......... start, watch, cancel; recover runs cut off by a restart
  5. API ............. response shapes and endpoints
  6. MAIN
================================================================================
"""
import datetime as dt
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
from pydantic import BaseModel

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
MIN_TOKEN_LENGTH = 32
MAX_UPLOAD_MB = 20
DEFAULT_TIME_LIMIT, MAX_TIME_LIMIT = 250, 3600                             # Seeker search time [s]
VALIDATE_TIMEOUT_SECONDS = 120
# Time left = fixed phases + search (Seeker overruns its limit by ~7 %); user's PC, 07.11.: 115 s + 266.6 s at 250 s
ETA_FIXED_SECONDS, ETA_SEARCH_FACTOR = 115.0, 1.07
WATCHDOG_FACTOR = 3.0                                                      # a run taking 3 × its estimate is stopped
EXIT_WAIT_SECONDS = 30.0                                                   # a finished worker gets this to exit
LOG_TAIL_LINES = 40
RUN_ID = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")
PHASE_START = re.compile(r"^\[\d\d:\d\d:\d\d\] (.+) \.\.\.$")              # engine phase lines: "[19:52:44] 5. … ..."


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


def run_dir(run_id):
    if not RUN_ID.match(run_id) or not (RUNS_DIR / run_id / "status.json").exists():
        raise HTTPException(404, f"No run with id {run_id!r}.")
    return RUNS_DIR / run_id


def read_status(folder):
    return json.loads((Path(folder) / "status.json").read_text(encoding="utf-8"))


def update_status(folder, **fields):
    status = read_status(folder)
    status.update(fields)
    write_json(Path(folder) / "status.json", status)
    return status


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
def expected_seconds(time_limit):
    return ETA_FIXED_SECONDS + ETA_SEARCH_FACTOR * float(time_limit)


def progress(folder, status):
    """Current engine phase, estimated seconds left and the last log lines (all read from the worker's log)."""
    log = Path(folder) / "log.txt"
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines() if log.exists() else []
    phases = [m.group(1) for m in map(PHASE_START.match, lines) if m]
    left = None
    if status["state"] == "running":
        elapsed = (dt.datetime.now().astimezone() - dt.datetime.fromisoformat(status["started"])).total_seconds()
        left = max(0.0, expected_seconds(status["time_limit"]) - elapsed)
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


def watch(run_id, proc, folder, time_limit):
    """Waits for the worker; stops it after WATCHDOG_FACTOR × the expected time; records crashes."""
    try:
        code = proc.wait(timeout=WATCHDOG_FACTOR * expected_seconds(time_limit))
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


def start_run(data, time_limit):
    with LOCK:
        if busy():
            raise HTTPException(409, f"A run is already in progress ({CURRENT['id']}).")
        let_finished_worker_exit()
        run_id = new_run_id()
        folder = RUNS_DIR / run_id
        folder.mkdir(parents=True)
        (folder / "input.xlsx").write_bytes(data)
        write_json(folder / "status.json", {"id": run_id, "state": "running", "created": now(), "started": now(),
                                            "finished": None, "time_limit": time_limit, "message": "Running.",
                                            "errors": []})
        cmd = [sys.executable, "-u", str(WORKER), "run", str(folder), str(LICENSE_FILE), str(time_limit)]
        with open(folder / "log.txt", "wb") as log:
            proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=str(ROOT), env=worker_env())
        thread = threading.Thread(target=watch, args=(run_id, proc, folder, time_limit), daemon=True)
        CURRENT.update(id=run_id, proc=proc, thread=thread, cancelled=False)
        thread.start()
    return run_id


def recover_interrupted_runs():
    """Runs still marked 'running' when the API starts were cut off by a restart or a crash."""
    for f in RUNS_DIR.glob("*/status.json") if RUNS_DIR.exists() else []:
        if read_status(f.parent)["state"] == "running":
            update_status(f.parent, state="failed", finished=now(),
                          message="Interrupted: the API stopped during this run.")


def validate_upload(data):
    with tempfile.TemporaryDirectory() as tmp:
        src, answer = Path(tmp) / "input.xlsx", Path(tmp) / "answer.json"
        src.write_bytes(data)
        done = subprocess.run([sys.executable, "-u", str(WORKER), "validate", str(src), str(answer)], cwd=str(ROOT),
                              env=worker_env(), capture_output=True, timeout=VALIDATE_TIMEOUT_SECONDS)
        if not answer.exists():
            tail = done.stdout.decode("utf-8", "replace")[-2000:] + done.stderr.decode("utf-8", "replace")[-2000:]
            raise HTTPException(500, f"The validation worker failed: {tail}")
        return json.loads(answer.read_text(encoding="utf-8"))


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


class Validation(BaseModel):
    valid: bool
    errors: list[str]                     # the engine's messages, one problem per entry


class RunSummary(BaseModel):
    id: str
    state: str                            # running | done | invalid | failed | cancelled
    created: str
    finished: str | None
    time_limit: float
    message: str
    summary: dict[str, Any] | None = None  # done runs: cost_out_of_sample, rules_met_out_of_sample, rules_total
    test_double: bool = False             # True = run on the Seeker test double (development only)


class RunStatus(RunSummary):
    errors: list[str]
    phase: str | None
    seconds_left: float | None
    log_tail: list[str]


class MissedRule(BaseModel):
    channel: str
    week: int
    target: float
    share: float                          # share of fresh scenarios meeting the target


class Headline(BaseModel):
    cost_out_of_sample: float
    rules_total: int
    rules_met_out_of_sample: int
    rules_missed_out_of_sample: list[MissedRule]
    feasibility: str
    bound_note: str
    cost_in_sample: float
    rules_met_in_sample: int
    start_policy_cost: float
    scenarios_in_sample: int
    scenarios_out_of_sample: int
    seeker_status: str


class Table(BaseModel):
    columns: list[str]
    rows: list[list[Any]]


class Result(BaseModel):
    contract_version: int
    engine_version: str
    seeker_version: str
    test_double: bool
    headline: Headline
    sheets: dict[str, Table]              # Summary, Commit_Now, Policy_sS, Decisions, FillRates, Stock_Waste
    timings: list[tuple[str, float]]


@asynccontextmanager
async def lifespan(_app):
    if len(TOKEN) < MIN_TOKEN_LENGTH:
        raise RuntimeError(f"Set MEIO_TOKEN to a secret of at least {MIN_TOKEN_LENGTH} characters "
                           "(local_settings.bat).")
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    recover_interrupted_runs()
    yield


app = FastAPI(title="MEIO local API", version="1", lifespan=lifespan, dependencies=[Depends(require_token)])
app.add_middleware(CORSMiddleware, allow_origin_regex=ALLOWED_ORIGINS, allow_methods=["GET", "POST"],
                   allow_headers=["Authorization", "Content-Type", "ngrok-skip-browser-warning"])


@app.get("/health", response_model=Health)
def health():
    src = ENGINE_FILE.read_text(encoding="utf-8") if ENGINE_FILE.exists() else ""
    m = re.search(r'^SCRIPT_VERSION = "(.+)"', src, re.M)                  # read as text: never import the engine
    return Health(status="ok", engine_version=m.group(1) if m else None, license_found=LICENSE_FILE.exists(),
                  busy=busy(), current_run=CURRENT["id"] if busy() else None,
                  test_double=os.environ.get("MEIO_TEST_DOUBLE") == "1")


@app.post("/validate", response_model=Validation)
def validate(file: Annotated[UploadFile, File()]):
    """Checks a workbook with the engine's own data checks. Nothing is stored."""
    return validate_upload(read_upload(file))


@app.post("/runs", response_model=RunStatus, status_code=202)
def create_run(file: Annotated[UploadFile, File()],
               time_limit: Annotated[int, Form(ge=1, le=MAX_TIME_LIMIT)] = DEFAULT_TIME_LIMIT):
    """Starts an optimization run in the background (refused while another run is in progress)."""
    return get_run(start_run(read_upload(file), time_limit))


@app.get("/runs", response_model=list[RunSummary])
def list_runs():
    """Run history, newest first."""
    folders = sorted(RUNS_DIR.glob("*/status.json"), reverse=True) if RUNS_DIR.exists() else []
    return [read_status(f.parent) for f in folders if RUN_ID.match(f.parent.name)]


@app.get("/runs/{run_id}", response_model=RunStatus)
def get_run(run_id: str):
    """Status of one run, with the current phase, estimated seconds left and the last log lines."""
    folder = run_dir(run_id)
    status = read_status(folder)
    return {**status, **progress(folder, status)}


@app.get("/runs/{run_id}/result", response_model=Result)
def get_result(run_id: str):
    """Everything the engine reported, as JSON: the headline and the six result tables."""
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
