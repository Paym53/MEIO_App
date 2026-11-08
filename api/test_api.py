"""
================================================================================
 MEIO APP — the app's tests                                     run:  python -m pytest   (from the repo root)
================================================================================
The acceptance scenarios of README.md "What the app must do", end to end through the real API and real worker
processes. Seeker is replaced by the test double of engine/test_engine.py, so no license is needed: the numbers
are real model numbers, the search is trivial.

  1. INVALID DATA (AC1) ......... every engine error is shown word for word; no run starts
  2. HONEST HEADLINE (AC2) ...... the headline is the engine's out-of-sample result, feasibility line, bound NOTE
  3. EXACT NUMBERS (AC3) ........ result.json = the engine's RunResult = the results workbook, cell for cell
  4. COMPARISON DATA (AC4) ...... history carries the rules met next to the cost (the UI enforces the rule)
  5. PROTECTED PC (AC5) ......... no token, no answer, no action
  6. ONE RUN AT A TIME (AC6) .... second run refused; cancel; watchdog; crash and restart recovery
  7. ARCHITECTURE & CONTRACT .... server never imports the engine; frozen API contract; result model = worker output
  8. START SCRIPT .............. settings template complete, secrets gitignored; the .bat checks (Windows)
================================================================================
"""
import ast
import json
import math
import os
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import openpyxl
import pandas as pd
import pytest
import server
import test_engine as te  # the Seeker test double + the engine (te.ms)
import worker
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "engine" / "MEIO_Data_Template.xlsx"
TOKEN = "test-token-" + "x" * 40
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# ---------------------------------------------------------------------------- helpers
@pytest.fixture(scope="module")
def api(tmp_path_factory):
    """A running API (test double, temporary run folder, license present), authorised with the token."""
    tmp = tmp_path_factory.mktemp("api")
    (tmp / "license.sio").write_text("x", encoding="utf-8")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(worker.TEST_DOUBLE_ENV, "1")
        mp.setattr(server, "TOKEN", TOKEN)
        mp.setattr(server, "RUNS_DIR", tmp / "runs")
        mp.setattr(server, "LICENSE_FILE", tmp / "license.sio")
        with TestClient(server.app) as client:
            client.headers["Authorization"] = f"Bearer {TOKEN}"
            yield client


def upload(path, name=None):
    return {"file": (name or Path(path).name, Path(path).read_bytes(), XLSX)}


def wait(client, run_id, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = client.get(f"/runs/{run_id}").json()
        if st["state"] != "running":
            return st
        time.sleep(0.3)
    raise AssertionError(f"run {run_id} did not finish in {timeout} s")


def start(client, path=TEMPLATE, time_limit=1):
    r = client.post("/runs", files=upload(path), data={"time_limit": str(time_limit)})
    assert r.status_code == 202, r.text
    return r.json()["id"]


@pytest.fixture(scope="module")
def done(api):
    """One finished run on the template: (run id, status, result.json)."""
    run_id = start(api)
    status = wait(api, run_id)
    assert status["state"] == "done", status
    return run_id, status, api.get(f"/runs/{run_id}/result").json()


@pytest.fixture(scope="module")
def invalid_workbook(tmp_path_factory):
    """The template with two data errors (review period > horizon; production MOQ not a multiple of the batch)."""
    wb = openpyxl.load_workbook(TEMPLATE)
    ws = wb["Settings"]
    row = next(r for r in range(5, ws.max_row + 1) if ws.cell(r, 1).value == "ReviewPeriod")
    ws.cell(row, 2).value = 99
    wb["Production"]["D5"].value = 6050
    path = tmp_path_factory.mktemp("bad") / "bad.xlsx"
    wb.save(path)
    return path


def engine_errors(path):
    """What the engine itself says about a workbook (its documented format: one '  - problem' per line)."""
    with pytest.raises(ValueError) as e:
        te.ms.validate(te.ms.load_data(str(path)))
    return [ln.strip()[2:] for ln in str(e.value).splitlines()[1:]]


def summary(result):
    return dict(result["sheets"]["Summary"]["rows"])


# ============================================================================
# 1. INVALID DATA (AC1)
# ============================================================================
def test_validate_shows_every_engine_error_word_for_word(api, invalid_workbook):
    expected = engine_errors(invalid_workbook)
    assert len(expected) >= 2
    r = api.post("/validate", files=upload(invalid_workbook)).json()
    assert r == {"valid": False, "errors": expected}


def test_validate_accepts_the_template(api):
    assert api.post("/validate", files=upload(TEMPLATE)).json() == {"valid": True, "errors": []}


def test_invalid_data_starts_no_optimization(api, invalid_workbook):
    run_id = start(api, invalid_workbook)
    st = wait(api, run_id)
    assert st["state"] == "invalid" and st["errors"] == engine_errors(invalid_workbook)
    assert not any("Seeker optimization" in ln for ln in st["log_tail"])
    assert api.get(f"/runs/{run_id}/result").status_code == 409
    assert api.get(f"/runs/{run_id}/workbook").status_code == 409


def test_a_workbook_that_is_not_the_template_is_a_data_error(api, tmp_path):
    empty = tmp_path / "empty.xlsx"
    openpyxl.Workbook().save(empty)
    assert api.post("/validate", files=upload(empty)).json() == {"valid": False,
                                                                 "errors": ["Worksheet named 'Settings' not found"]}
    wb = openpyxl.load_workbook(TEMPLATE)
    wb["Production"]["A4"].value = "Item"                        # a renamed column: the engine names the problem
    renamed = tmp_path / "renamed.xlsx"
    wb.save(renamed)
    assert api.post("/validate", files=upload(renamed)).json() == {"valid": False, "errors": engine_errors(renamed)}
    broken = tmp_path / "broken.xlsx"                            # a zip file without a workbook inside
    with zipfile.ZipFile(broken, "w") as z:
        z.writestr("hello.txt", "not a workbook")
    r = api.post("/validate", files=upload(broken)).json()
    assert r["valid"] is False and len(r["errors"]) == 1
    assert r["errors"][0].startswith("The workbook could not be read as the MEIO template:")


def test_only_xlsx_files_of_sensible_size_are_accepted(api, monkeypatch):
    assert api.post("/validate", files={"file": ("a.csv", b"PK\x03\x04", "text/csv")}).status_code == 400
    assert api.post("/validate", files={"file": ("a.xlsx", b"not a zip", XLSX)}).status_code == 400
    monkeypatch.setattr(server, "MAX_UPLOAD_MB", 0)
    assert api.post("/validate", files=upload(TEMPLATE)).status_code == 413


# ============================================================================
# 2. HONEST HEADLINE (AC2)
# ============================================================================
def test_headline_is_the_engines_out_of_sample_result(done):
    _, _, result = done
    h, s = result["headline"], summary(result)
    assert h["cost_out_of_sample"] == s["Expected total cost (out-of-sample)"]
    assert f"{h['rules_met_out_of_sample']} of {h['rules_total']}" == s["Constraints met out-of-sample"]
    assert h["rules_total"] == 90
    assert h["feasibility"] == s["Feasibility check (section 4b)"]
    assert h["bound_note"] == s["Reorder points at their upper bound"]
    assert len(h["rules_missed_out_of_sample"]) == h["rules_total"] - h["rules_met_out_of_sample"]
    assert result["test_double"] is True and result["seeker_version"].startswith("FAKE")


def test_missed_rules_are_the_rows_the_engine_marks_below_level(done):
    _, _, result = done
    t = result["sheets"]["FillRates"]
    fr = pd.DataFrame(t["rows"], columns=t["columns"])
    hit = fr.index[fr["Constrained"] == "yes"][[0, 5]]
    fr.loc[hit, "Status out-of-sample"] = "BELOW LEVEL"
    share = [c for c in fr.columns if c.startswith("P(FR ≥ target) (")][0]
    below = fr.index[(fr["Constrained"] == "yes") & (fr["Status out-of-sample"] == "BELOW LEVEL")]
    assert set(hit) < set(below)                                 # the forced rows plus the run's own misses
    expected = [{"channel": fr.at[i, "Channel"], "week": int(fr.at[i, "Week"]), "target": fr.at[i, "Target"],
                 "share": fr.at[i, share]} for i in below]
    assert worker.missed_rules(fr) == expected


def test_progress_shows_phase_and_log(done, api):
    run_id, status, _ = done
    assert status["phase"].startswith("6. Out-of-sample") and status["seconds_left"] is None
    assert any(ln.startswith("Feasibility check:") for ln in status["log_tail"])
    st = api.get(f"/runs/{run_id}").json()
    assert st["finished"] and st["summary"]["rules_total"] == 90


def test_seconds_left_counts_down_while_running(tmp_path):
    folder = tmp_path / "20261107-000000-abcd"
    folder.mkdir()
    started = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(seconds=100)).isoformat()
    status = {"state": "running", "started": started, "time_limit": 250}
    left = server.progress(folder, status)["seconds_left"]
    assert server.expected_seconds(250) - 100 - 5 < left < server.expected_seconds(250) - 100 + 5


# ============================================================================
# 3. EXACT NUMBERS (AC3): result.json = RunResult = results workbook
# ============================================================================
def test_result_json_is_an_exact_copy_of_the_run_result(tmp_path, monkeypatch):
    res = te.headless(tmp_path, monkeypatch)                        # engine run in this process (test double)
    out = json.loads(json.dumps(worker.build_result(te.ms, res, True), allow_nan=False))
    for name, df in res.sheets.items():
        assert out["sheets"][name]["columns"] == list(map(str, df.columns))
        assert out["sheets"][name]["rows"] == [[worker.cell(v) for v in row] for row in df.itertuples(index=False)]
        assert len(out["sheets"][name]["rows"]) == len(df)
    h = out["headline"]
    assert h["cost_out_of_sample"] == res.oos["cost"] and h["cost_in_sample"] == res.ins["cost"]
    assert h["rules_met_in_sample"] == res.ins["met"] and h["feasibility"] == res.ins["feasibility"]
    assert h["bound_note"] == te.ms.bound_note(res.layout, res.x) and h["start_policy_cost"] == res.ins["start"]["cost"]


def same(a, b):
    if a is None:                                     # empty cell, e.g. arrival week of an order never placed
        return b is None or (isinstance(b, float) and math.isnan(b))
    if isinstance(a, float) or isinstance(b, float):
        return b == pytest.approx(a, rel=1e-12, abs=1e-9)
    if isinstance(b, pd.Timestamp):
        return a == b.date().isoformat()
    return a == b


def test_every_number_shown_equals_the_results_workbook(done, api, tmp_path):
    run_id, _, result = done
    r = api.get(f"/runs/{run_id}/workbook")
    assert r.status_code == 200 and r.content.startswith(b"PK")
    (tmp_path / "w.xlsx").write_bytes(r.content)
    book = pd.read_excel(tmp_path / "w.xlsx", sheet_name=None)
    assert list(book) == list(result["sheets"]) == ["Summary", "Commit_Now", "Policy_sS", "Decisions", "FillRates",
                                                   "Stock_Waste", "Bands", "KPIs"]          # engine contract v2
    for name, df in book.items():
        t = result["sheets"][name]
        assert list(map(str, df.columns)) == t["columns"], name
        assert len(df) == len(t["rows"]), name
        for i, row in enumerate(df.itertuples(index=False)):
            for j, v in enumerate(row):
                assert same(t["rows"][i][j], v.item() if hasattr(v, "item") else v), (name, i, t["columns"][j])


def test_column_lookup_refuses_ambiguity():
    df = pd.DataFrame(columns=["P(FR ≥ target) (60 fresh)", "P(FR ≥ target) (61 fresh)"])
    with pytest.raises(KeyError):
        worker.column(df, "P(FR ≥ target) (")


def test_cells_become_plain_json():
    import datetime as dt

    import numpy as np
    assert [worker.cell(v) for v in (np.int64(3), np.float64(0.5), float("nan"), np.bool_(True), dt.date(2026, 11, 9),
                                     None, "x")] == [3, 0.5, None, True, "2026-11-09", None, "x"]


# ============================================================================
# 4. COMPARISON DATA (AC4) — the UI compares costs only between runs meeting the same number of rules
# ============================================================================
def test_history_lists_cost_next_to_the_rules_met_newest_first(done, api):
    run_id, _, result = done
    runs = api.get("/runs").json()
    assert [r["id"] for r in runs] == sorted((r["id"] for r in runs), reverse=True)
    mine = next(r for r in runs if r["id"] == run_id)
    h = result["headline"]
    assert mine["summary"] == {"cost_out_of_sample": h["cost_out_of_sample"],
                               "rules_met_out_of_sample": h["rules_met_out_of_sample"], "rules_total": 90}
    assert mine["test_double"] is True


# ============================================================================
# 5. PROTECTED PC (AC5)
# ============================================================================
@pytest.mark.parametrize("auth", [None, "Bearer wrong-token", f"Basic {TOKEN}"])
def test_every_endpoint_refuses_requests_without_the_token(api, auth):
    headers = {"Authorization": auth} if auth else {}
    routes = [r for r in server.app.routes if isinstance(r, APIRoute)]
    assert len(routes) == 8
    before = sorted(p.name for p in server.RUNS_DIR.iterdir())
    for route in routes:
        path = route.path.replace("{run_id}", "20261107-000000-abcd")
        for method in route.methods:
            r = api.request(method, path, headers={"Authorization": ""} | headers, files=upload(TEMPLATE)
                            if method == "POST" else None)
            assert r.status_code == 401, (method, path, r.status_code)
    assert sorted(p.name for p in server.RUNS_DIR.iterdir()) == before          # nothing was started or stored


def test_the_api_does_not_start_without_a_strong_token(monkeypatch):
    monkeypatch.setattr(server, "TOKEN", "short")
    with pytest.raises(RuntimeError, match="MEIO_TOKEN"), TestClient(server.app):
        pass


def test_unknown_or_malformed_run_ids_are_not_found(api):
    for rid in ("20991231-235959-ffff", "..", "x/../../etc"):
        assert api.get(f"/runs/{rid}").status_code == 404


def test_health_reports_engine_license_and_test_double(api, monkeypatch, tmp_path):
    h = api.get("/health").json()
    assert h == {"status": "ok", "engine_version": te.ms.SCRIPT_VERSION, "license_found": True, "busy": False,
                 "current_run": None, "test_double": True}
    monkeypatch.setattr(server, "LICENSE_FILE", tmp_path / "missing.sio")
    assert api.get("/health").json()["license_found"] is False
    run_id = start(api)
    st = wait(api, run_id)
    assert st["state"] == "failed" and "License file not found" in st["message"]


def test_the_worker_never_receives_the_token(monkeypatch):
    monkeypatch.setenv("MEIO_TOKEN", TOKEN)
    env = server.worker_env()
    assert "MEIO_TOKEN" not in env and env["PYTHONUTF8"] == "1"


# ============================================================================
# 6. ONE RUN AT A TIME (AC6), cancel, watchdog, crash and restart recovery
# ============================================================================
def test_a_second_run_is_refused_and_cancel_stops_the_first(api):
    run_id = start(api, time_limit=60)
    r = api.post("/runs", files=upload(TEMPLATE), data={"time_limit": "1"})
    assert r.status_code == 409 and "already in progress" in r.json()["detail"]
    assert api.get("/health").json()["busy"] is True
    st = api.post(f"/runs/{run_id}/cancel").json()
    assert st["state"] == "cancelled"
    assert api.get("/health").json()["busy"] is False
    assert api.post(f"/runs/{run_id}/cancel").status_code == 409


SLOW_EXIT_WORKER = """
import json, sys, time
from pathlib import Path
status = Path(sys.argv[2]) / "status.json"
s = json.loads(status.read_text(encoding="utf-8"))
s.update(state="done", message="Finished.")
status.write_text(json.dumps(s), encoding="utf-8")
time.sleep(3)                                    # the process is still exiting after its final state is written
"""


@pytest.mark.parametrize("exit_wait", [30.0, 0.2])                 # normal exit / hanging exit (is stopped)
def test_a_new_run_can_start_as_soon_as_the_last_one_is_finished(api, monkeypatch, tmp_path, exit_wait):
    monkeypatch.setattr(server, "EXIT_WAIT_SECONDS", exit_wait)
    slow = tmp_path / "slow_exit_worker.py"
    slow.write_text(SLOW_EXIT_WORKER, encoding="utf-8")
    monkeypatch.setattr(server, "WORKER", slow)
    first = start(api)
    assert wait(api, first)["state"] == "done"
    assert api.get("/health").json()["busy"] is False             # finished = not busy, even while still exiting
    second = start(api)                                           # 202, not "already in progress"
    assert server.read_status(server.RUNS_DIR / first)["state"] == "done"
    assert wait(api, second)["state"] == "done"
    server.CURRENT["proc"].wait(timeout=30)


def test_a_run_far_over_its_expected_time_is_stopped(api, monkeypatch):
    monkeypatch.setattr(server, "ETA_FIXED_SECONDS", 0.5)
    monkeypatch.setattr(server, "ETA_SEARCH_FACTOR", 0.0)
    monkeypatch.setattr(server, "WATCHDOG_FACTOR", 1.0)
    st = wait(api, start(api, time_limit=60))
    assert st["state"] == "failed" and "far longer" in st["message"]


def test_a_crashed_worker_is_reported(api, monkeypatch, tmp_path):
    monkeypatch.setattr(server, "WORKER", tmp_path / "missing_worker.py")
    st = wait(api, start(api))
    assert st["state"] == "failed" and "stopped unexpectedly" in st["message"]


def test_runs_cut_off_by_a_restart_are_marked_failed(tmp_path, monkeypatch):
    folder = tmp_path / "runs" / "20261107-000000-abcd"
    folder.mkdir(parents=True)
    server.write_json(folder / "status.json", {"id": folder.name, "state": "running"})
    monkeypatch.setattr(server, "TOKEN", TOKEN)
    monkeypatch.setattr(server, "RUNS_DIR", tmp_path / "runs")
    with TestClient(server.app):
        pass
    st = server.read_status(folder)
    assert st["state"] == "failed" and st["message"].startswith("Interrupted")


def test_worker_usage_and_unexpected_errors(tmp_path, monkeypatch, capsys):
    assert worker.main(["nonsense"], {worker.TEST_DOUBLE_ENV: "1"}) == worker.EXIT_FAILED
    folder = tmp_path / "run"
    folder.mkdir()
    (folder / "input.xlsx").write_bytes(TEMPLATE.read_bytes())
    monkeypatch.setattr(te.ms, "run", lambda *a, **k: 1 / 0)
    assert worker.main(["run", str(folder), "lic", "1"], {worker.TEST_DOUBLE_ENV: "1"}) == worker.EXIT_FAILED
    assert "ZeroDivisionError" in json.loads((folder / "status.json").read_text(encoding="utf-8"))["message"]


@pytest.mark.parametrize("module", [server, worker])
def test_status_writes_survive_a_reader_holding_the_file(module, tmp_path, monkeypatch):
    real, calls = Path.replace, []

    def flaky(self, target):                                    # Windows: replacing a file that is open fails
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError("in use")
        return real(self, target)

    monkeypatch.setattr(Path, "replace", flaky)
    module.write_json(tmp_path / "status.json", {"state": "done"})
    assert json.loads((tmp_path / "status.json").read_text(encoding="utf-8")) == {"state": "done"}
    calls.clear()
    monkeypatch.setattr(Path, "replace", lambda self, target: (_ for _ in ()).throw(PermissionError("in use")))
    with pytest.raises(PermissionError):
        module.write_json(tmp_path / "status.json", {"state": "x"})


# ============================================================================
# 7. ARCHITECTURE & CONTRACT
# ============================================================================
ENGINE_MODULES = {"meio_seeker_stochastic", "test_engine", "reference_simulator", "meio_stress_tests", "seeker"}


def imports(path):
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_only_the_worker_imports_the_engine():
    assert not imports(ROOT / "api" / "server.py") & (ENGINE_MODULES | {"worker"})
    assert imports(ROOT / "api" / "worker.py") & ENGINE_MODULES == {"meio_seeker_stochastic", "test_engine"}


def test_api_contract_is_frozen():
    frozen = json.loads((ROOT / "api" / "openapi.json").read_text(encoding="utf-8"))
    assert server.app.openapi() == frozen, "API changed: if deliberate, run  python api/server.py --openapi"


def test_result_model_matches_what_the_worker_writes(done):
    _, _, result = done
    assert set(result) == set(server.Result.model_fields)
    assert set(result["headline"]) == set(server.Headline.model_fields)
    assert result["contract_version"] == worker.RESULT_CONTRACT_VERSION


# ============================================================================
# 8. START SCRIPT (start_worker.bat, local_settings.example.bat)
# ============================================================================
def bat_vars_used(path):
    return set(re.findall(r"%([A-Z_][A-Z0-9_]*)%", Path(path).read_text(encoding="utf-8")))


def test_the_settings_template_defines_every_setting_the_script_and_server_read():
    defined = set(re.findall(r"^set ([A-Z_]+)=", (ROOT / "local_settings.example.bat").read_text(encoding="utf-8"),
                             re.M))
    assert bat_vars_used(ROOT / "start_worker.bat") - {"PYTHONUTF8"} <= defined
    server_reads = set(re.findall(r'os\.environ\.get\("(MEIO_[A-Z_]+)"', (ROOT / "api" / "server.py").read_text(
        encoding="utf-8"))) - {"MEIO_PORT", "MEIO_RUNS_DIR", "MEIO_ALLOWED_ORIGINS", "MEIO_TEST_DOUBLE"}
    assert server_reads <= defined                                  # token and license path come from the settings


def test_the_real_settings_file_is_never_committed():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "local_settings.bat" in ignored and "*.sio" in ignored
    assert "paste-your-token-here" in (ROOT / "local_settings.example.bat").read_text(encoding="utf-8")


def settings_bat(**values):
    return "\r\n".join(f"set {k}={v}" for k, v in values.items()) + "\r\n"


GUARD_CASES = {
    "missing settings": (None, "local_settings.bat is missing", 1),
    "placeholder token": ({"MEIO_TOKEN": "paste-your-token-here"}, "Set MEIO_TOKEN", 1),
    "wrong python": ({"MEIO_TOKEN": TOKEN, "MEIO_PYTHON": r"C:\nowhere\python.exe"}, "Python not found", 1),
    "no ngrok": ({"MEIO_TOKEN": TOKEN, "MEIO_PYTHON": sys.executable, "NGROK_DOMAIN": "x.ngrok-free.app",
                  "NGROK_EXE": "ngrok-missing-xyz"}, "ngrok not found", 1),
    "local only": ({"MEIO_TOKEN": TOKEN, "MEIO_PYTHON": sys.executable, "NGROK_DOMAIN": ""}, "runs on this PC only", 0),
}


@pytest.mark.skipif(sys.platform != "win32", reason="Windows batch file (runs on Windows CI and the license PC)")
@pytest.mark.parametrize("case", GUARD_CASES)
def test_start_script_checks_its_settings_before_starting_anything(case, tmp_path):
    settings, message, code = GUARD_CASES[case]
    (tmp_path / "start_worker.bat").write_bytes((ROOT / "start_worker.bat").read_bytes())
    if settings is not None:
        (tmp_path / "local_settings.bat").write_text(settings_bat(**settings), encoding="utf-8")
    done = subprocess.run(["cmd", "/c", "start_worker.bat"], cwd=tmp_path, capture_output=True, timeout=60,
                          env=dict(os.environ, MEIO_NO_PAUSE="1"))
    out = done.stdout.decode("utf-8", "replace") + done.stderr.decode("utf-8", "replace")
    assert message in out and done.returncode == code, out
    if case == "local only":                                       # reached the API step (no api\server.py here)
        assert "The API has stopped" in out
