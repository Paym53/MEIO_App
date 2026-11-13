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
  9. SETTINGS, MASTER DATA ..... run settings reach the engine; no run target (only the workbook's, W04); overrides;
                                 master data = engine's reading; label/note; time estimate = measured runs incl. the
                                 start check; keep-awake; runs of older engines are not shown
 10. WHAT-IF ................... edits go into a copy (original byte-identical); engine's words; demand shift exact;
                                 formula/key/solver cells refused; target and review period edits; no chaining (W05)
================================================================================
"""
import ast
import hashlib
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
    assert r == {"valid": False, "errors": expected, "warnings": [], "input": None}


def test_validate_accepts_the_template(api):
    r = api.post("/validate", files=upload(TEMPLATE)).json()
    assert r["valid"] is True and r["errors"] == [] and r["input"] is not None
    assert r["warnings"] == te.ms.load_data(str(TEMPLATE)).warnings and len(r["warnings"]) == 2   # RM1/RM2 max order


def test_invalid_data_starts_no_optimization(api, invalid_workbook):
    run_id = start(api, invalid_workbook)
    st = wait(api, run_id)
    assert st["state"] == "invalid" and st["errors"] == engine_errors(invalid_workbook)
    assert not any("Seeker optimization" in ln for ln in st["log_tail"])
    assert api.get(f"/runs/{run_id}/result").status_code == 409
    assert api.get(f"/runs/{run_id}/workbook").status_code == 409
    assert api.get(f"/runs/{run_id}/input").status_code == 409                # the engine could not read it


def test_a_workbook_that_is_not_the_template_is_a_data_error(api, tmp_path):
    empty = tmp_path / "empty.xlsx"
    openpyxl.Workbook().save(empty)
    assert api.post("/validate", files=upload(empty)).json() == {
        "valid": False, "errors": ["Worksheet named 'Settings' not found"], "warnings": [], "input": None}
    wb = openpyxl.load_workbook(TEMPLATE)
    wb["Production"]["A4"].value = "Item"                        # a renamed column: the engine names the problem
    renamed = tmp_path / "renamed.xlsx"
    wb.save(renamed)
    assert api.post("/validate", files=upload(renamed)).json() == {
        "valid": False, "errors": engine_errors(renamed), "warnings": [], "input": None}
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


def test_missed_rules_are_the_rows_the_engine_marks_below_target(done):
    _, _, result = done
    t = result["sheets"]["FillRates"]
    fr = pd.DataFrame(t["rows"], columns=t["columns"])
    hit = fr.index[fr["Constrained"] == "yes"][[0, 5]]
    fr.loc[hit, "Status out-of-sample"] = "BELOW TARGET"
    col = {k: next(c for c in fr.columns if c.startswith(p)) for k, p in
           [("expected_fill_rate", "Expected FR ("), ("fully_served", "Fully served ("),
            ("bad_scenario_p10", "FR in a bad scenario, P10 ("), ("worst_5pct_average", "Worst 5% of futures")]}
    below = fr.index[(fr["Constrained"] == "yes") & (fr["Status out-of-sample"] == "BELOW TARGET")]
    assert set(hit) <= set(below)                                # the forced rows plus the run's own misses
    expected = [{"channel": fr.at[i, "Channel"], "week": int(fr.at[i, "Week"]), "target": fr.at[i, "Target"],
                 **{k: fr.at[i, c] for k, c in col.items()}} for i in below]
    assert worker.missed_rules(fr) == expected
    assert "Expected FR in-sample (" not in col["expected_fill_rate"]          # the fresh column, not Seeker's own


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
    status = {"state": "running", "started": started, "time_limit": 250}            # a run from before settings
    left = server.progress(folder, status)["seconds_left"]
    full = server.expected_seconds({"time_limit": 250})
    assert full - 100 - 5 < left < full - 100 + 5


# ============================================================================
# 3. EXACT NUMBERS (AC3): result.json = RunResult = results workbook
# ============================================================================
def test_result_json_is_an_exact_copy_of_the_run_result(tmp_path, monkeypatch):
    res = te.headless(tmp_path, monkeypatch)                        # engine run in this process (test double)
    out = json.loads(json.dumps(worker.build_result(te.ms, res, True, {"time_limit": 1}), allow_nan=False))
    for name, df in res.sheets.items():
        assert out["sheets"][name]["columns"] == list(map(str, df.columns))
        assert out["sheets"][name]["rows"] == [[worker.cell(v) for v in row] for row in df.itertuples(index=False)]
        assert len(out["sheets"][name]["rows"]) == len(df)
    h = out["headline"]
    assert h["cost_out_of_sample"] == res.oos["cost"] and h["cost_in_sample"] == res.ins["cost"]
    assert h["rules_met_in_sample"] == res.ins["met"] and h["feasibility"] == res.ins["feasibility"]
    assert h["bound_note"] == te.ms.bound_note(res.layout, res.x) and h["start_policy_cost"] == res.ins["start"]["cost"]
    assert h["cost_parts_in_sample"] == res.ins["parts"] and len(h["cost_parts_in_sample"]) == 5
    assert h["cost_parts_out_of_sample"] == {n: res.oos[("part", n)] for n in res.ins["parts"]}
    summary = dict(res.sheets["Summary"].values)
    assert h["saving_vs_start_in_sample"] == summary["Saving vs. start policy (in-sample)"]
    assert (h["fill_rate_target"], h["fill_rate_target_excel"]) == (res.data.fill_rate_target, 0.95)
    assert h["fill_rate_target_source"] == "Excel Items!TargetFillRate of FG1"
    assert (h["weakest_cell"], h["review_period"]) == (summary["Weakest cell (out-of-sample)"],
                                                       summary["Review period (A03)"])
    assert h["start_policy"] == summary["Start policy (warm start)"]
    assert h["start_repair"] == summary["Start repair (section 4c, K21)"]
    assert h["start_repair_used"] is bool(res.ins["repair"].get("used"))
    assert h["start_check"]["z0"] == res.ins["start_z"]["z0"] and h["start_check"]["z"] == res.ins["start_z"]["z"]
    assert [s["z"] for s in h["start_check"]["steps"]] == [s["z"] for s in res.ins["start_z"]["steps"]]
    assert h["waste_warning"] == summary["Waste warning (L10)"]
    waste = {k.split(" ")[2]: v for k, v in summary.items() if str(k).startswith("Expected waste")}
    assert h["expected_waste"] == waste
    assert set(h["expected_waste"]) == {"FG1", "RM3", "RM4"}                   # FG1 and the perishable materials
    info = out["run_info"]
    assert (info["horizon_weeks"], info["review_period_weeks"], info["first_rule_week"]) == (36, 1, 7)
    assert (info["fill_rate_target"], info["fill_rate_target_excel"]) == (0.95, 0.95) and "optimization_fill_rate" \
        not in info
    assert info["warnings"] == res.data.warnings and info["product_id"] == "FG1" and info["overrides"] is None


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
                                                   "Stock_Waste", "Bands", "KPIs"]          # engine contract v4
    for name, df in book.items():
        t = result["sheets"][name]
        assert list(map(str, df.columns)) == t["columns"], name
        assert len(df) == len(t["rows"]), name
        for i, row in enumerate(df.itertuples(index=False)):
            for j, v in enumerate(row):
                assert same(t["rows"][i][j], v.item() if hasattr(v, "item") else v), (name, i, t["columns"][j])


def test_column_lookup_refuses_ambiguity():
    df = pd.DataFrame(columns=["Expected FR (60 fresh)", "Expected FR (61 fresh)"])
    with pytest.raises(KeyError):
        worker.column(df, "Expected FR (")


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
    assert len(routes) == 14                    # incl. the what-if endpoints (sheets, changes, validate, app rules)
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
                 "current_run": None, "test_double": True, "restart_needed": False}
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
    monkeypatch.setattr(server, "expected_seconds", lambda *args, **kwargs: 0.5)
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
    (folder / "request.json").write_text(json.dumps({"time_limit": 1}), encoding="utf-8")
    monkeypatch.setattr(te.ms, "run", lambda *a, **k: 1 / 0)
    assert worker.main(["run", str(folder), "lic"], {worker.TEST_DOUBLE_ENV: "1"}) == worker.EXIT_FAILED
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


# ============================================================================
# 9. SETTINGS, MASTER DATA, LABELS, TIME ESTIMATE
# ============================================================================
def test_master_data_is_exactly_what_the_engine_read(api, done):
    inp = api.post("/validate", files=upload(TEMPLATE)).json()["input"]
    d = te.ms.load_data(str(TEMPLATE))
    st = inp["settings"]
    assert (st["horizon_weeks"], st["review_period_weeks"], st["scenarios"], st["random_seed"]) == \
        (d.horizon, d.review_period, d.n_scenarios, d.seed)
    assert st["start_date"] == "2026-11-09" and "chance_level" not in st          # no chance level since v18.0
    assert inp["product"]["fill_rate_target"] == d.fill_rate_target == 0.95     # one target per product (H04)
    assert [(c["id"], c["serving_order"]) for c in inp["channels"]] == [(c.id, c.priority) for c in d.channels] == \
        [("D2C", 1), ("B2B", 2), ("AMZ", 3)]                                    # ShortagePriority is an input (H02)
    assert all("target_fill_rate" not in c for c in inp["channels"])
    for ci, c in enumerate(d.channels):                                     # every quantile, every week
        assert inp["demand"]["quantiles"][c.id] == d.demand_q[ci, 1:].tolist()
    assert inp["demand"]["quantile_levels"] == d.levels.tolist()
    assert [(r["id"], r["max_order"], r["lead_time"]["weeks"]) for r in inp["raw_materials"]] == \
        [(k.id, k.max_order, k.lead_time.values.tolist()) for k in d.rms]
    prod = inp["product"]["production"]
    assert (prod["moq"], prod["batch"], prod["max"]) == (d.prod_moq, d.prod_batch, d.prod_max)
    assert prod["cost_tiers"]["tiers"][-1]["to"] is None                       # last tier: no upper limit
    assert inp["initial_stock"]["FG1"] == [{"remaining_shelf_life": a, "quantity": q} for a, q in d.init_fg]
    assert len(inp["open_orders"]["production"]) == len(d.open_prod) and inp["warnings"] == d.warnings
    assert len(inp["tables"]["Model_Assumptions"]["rows"]) == 85
    src = inp["tables"]["Sourcing"]                                            # which supplier delivers which material
    item, supplier = src["columns"].index("ItemID"), src["columns"].index("SupplierID")
    assert [(r[item], r[supplier]) for r in src["rows"]] == [("RM1", "SUP_VPL"), ("RM2", "SUP_VPL"),
                                                             ("RM3", "SUP_EHK"), ("RM4", "SUP_ACR")]
    assert "EffectiveMaxOrderQty" not in src["columns"]                        # Excel formula; engine: max_order
    assert "RowCheck" not in inp["tables"]["Locations"]["columns"]
    assert api.get(f"/runs/{done[0]}/input").json() == inp                     # the run keeps the same master data


def test_run_settings_reach_the_engine_and_are_recorded(api):
    form = {"time_limit": "1", "out_of_sample_scenarios": "600", "feasibility_check": "false",
            "start_policy_out_of_sample": "true", "optimization_fill_rate": "0.9", "num_scenarios": "100",  # ignored
            "random_seed": "43", "label": "seed 43", "note": "robustness check"}
    r = api.post("/runs", files=upload(TEMPLATE), data=form)
    assert r.status_code == 202, r.text
    st = wait(api, r.json()["id"])
    assert st["state"] == "done", st
    res = api.get(f"/runs/{st['id']}/result").json()
    info = res["run_info"]
    assert (info["scenarios_out_of_sample"], info["feasibility_check"], info["start_policy_out_of_sample"]) == \
        (600, False, True)
    assert (info["scenarios_in_sample"], info["random_seed"]) == (100, 43)
    h = res["headline"]                                  # no run target: an old client's field has no effect (W04)
    assert (h["fill_rate_target"], h["fill_rate_target_excel"], info["fill_rate_target"]) == (0.95, 0.95, 0.95)
    assert h["fill_rate_target_source"] == "Excel Items!TargetFillRate of FG1"
    assert info["overrides"] == {"NumScenarios": 100, "RandomSeed": 43}
    assert res["headline"]["feasibility"] == "not run"
    kpi_items = [row[0] for row in res["sheets"]["KPIs"]["rows"]]
    assert any(str(i).startswith("Start policy") for i in kpi_items)            # start policy on fresh scenarios
    assert st["settings"]["out_of_sample_scenarios"] == 600 and (st["label"], st["note"]) == ("seed 43",
                                                                                              "robustness check")
    assert st["file"] == {"name": TEMPLATE.name, "sha256": hashlib.sha256(TEMPLATE.read_bytes()).hexdigest(),
                          "bytes": TEMPLATE.stat().st_size}


def test_run_settings_are_restored_and_the_target_is_never_set_per_run(monkeypatch):
    monkeypatch.setattr(te.ms, "OPTIMIZATION_FILL_RATE", None)
    previous = worker.apply_settings(te.ms, {"optimization_fill_rate": 0.9, "feasibility_check": False})
    assert (te.ms.OPTIMIZATION_FILL_RATE, te.ms.FEASIBILITY_CHECK) == (None, False)       # W04: workbook only
    worker.restore_settings(te.ms, previous)
    assert te.ms.FEASIBILITY_CHECK is True


def test_the_pc_is_kept_awake_during_a_run_and_released_after(monkeypatch):
    calls = []
    kernel32 = type("K", (), {"SetThreadExecutionState": staticmethod(calls.append)})
    monkeypatch.setattr(worker.ctypes, "windll", type("W", (), {"kernel32": kernel32}), raising=False)
    with worker.keep_awake():
        assert calls == [worker.ES_CONTINUOUS | worker.ES_SYSTEM_REQUIRED]
    assert calls[-1] == worker.ES_CONTINUOUS                     # cleared again
    monkeypatch.delattr(worker.ctypes, "windll")
    with worker.keep_awake():                                    # Linux / CI: nothing to call
        pass


@pytest.mark.parametrize("field, value", [("out_of_sample_scenarios", "100"), ("num_scenarios", "0"),
                                          ("random_seed", "-1"), ("label", "x" * 81)])
def test_settings_outside_their_limits_are_refused(api, field, value):
    r = api.post("/runs", files=upload(TEMPLATE), data={field: value})
    assert r.status_code == 422


def test_label_and_note_can_be_changed_later(api, done):
    run_id = done[0]
    assert api.patch(f"/runs/{run_id}", json={"label": "Baseline"}).json()["label"] == "Baseline"
    r = api.patch(f"/runs/{run_id}", json={"note": "first v18 run"}).json()
    assert (r["label"], r["note"]) == ("Baseline", "first v18 run")             # one field changes, the other stays
    assert next(x for x in api.get("/runs").json() if x["id"] == run_id)["label"] == "Baseline"
    assert api.patch(f"/runs/{run_id}", json={"label": "x" * 81}).status_code == 422
    assert api.patch("/runs/20991231-235959-ffff", json={"label": "a"}).status_code == 404


def scenarios(n, t):
    return server.expected_seconds({"time_limit": t, "overrides": {"NumScenarios": n}})


def test_the_time_estimate_matches_the_measured_runs():
    assert scenarios(120, 120) == pytest.approx(290, rel=0.2)    # user's PC 13.11., v18.1: ≈ 290 s, 3 z steps
    assert scenarios(200, 300) == pytest.approx(600, rel=0.1)    # engine session: ≈ 10 min
    assert scenarios(500, 700) == pytest.approx(1140, rel=0.1)   # engine session: ≈ 19 min
    default = server.expected_seconds({})
    assert server.expected_seconds({"feasibility_check": False}) == pytest.approx(default - 21)
    assert server.expected_seconds({"start_policy_out_of_sample": True}) == pytest.approx(default + 82.5)
    assert server.expected_seconds({}, z_steps=0) < default < server.expected_seconds({}, repair=True)


def test_the_estimate_follows_the_start_check_in_the_log():
    z = server.Z_STEP_PHASE
    assert server.start_check_course([], []) == (3, False)                       # nothing seen yet: 3 steps
    assert server.start_check_course([f"{z} = 1.50", f"{z} = 2.00", f"{z} = 2.50"], []) == (4, False)  # still raising
    done = ["Start check: Seeker starts from z = 2.50"]
    assert server.start_check_course([f"{z} = 1.50", f"{z} = 2.00"], done) == (2, False)
    assert server.start_check_course(["5. Seeker optimization (time limit 1 s)"], []) == (0, False)  # no check needed
    assert server.start_check_course([f"{z} = 4.00", "4c. Start repair R1: Seeker evaluates"], done) == (1, True)


def test_runs_of_older_engines_are_not_shown(api, tmp_path, monkeypatch, done):
    old = server.RUNS_DIR / "20261108-203843-160f"
    old.mkdir()
    server.write_json(old / "status.json", {"id": old.name, "state": "done", "created": "x", "finished": "x",
                                             "time_limit": 250, "message": "Finished.", "errors": []})  # no api_version
    ids = [r["id"] for r in api.get("/runs").json()]
    assert old.name not in ids and done[0] in ids
    assert api.get(f"/runs/{old.name}").status_code == 404
    assert "older engine" in api.get(f"/runs/{old.name}/result").json()["detail"]


def start_with(client, form):
    r = client.post("/runs", files=upload(TEMPLATE), data={"time_limit": "1", **form})
    assert r.status_code == 202, r.text
    return r.json()["id"]


def test_health_says_when_the_api_needs_a_restart_after_an_update(api, monkeypatch, tmp_path):
    code = tmp_path / "server.py"
    code.write_text("v1", encoding="utf-8")
    monkeypatch.setattr(server, "CODE_FILES", (code,))
    monkeypatch.setattr(server, "STARTED_WITH", server.code_stamp())
    assert api.get("/health").json()["restart_needed"] is False
    os.utime(code, ns=(code.stat().st_atime_ns, code.stat().st_mtime_ns + 10**9))     # an update was pulled
    assert api.get("/health").json()["restart_needed"] is True


def test_a_worker_called_the_old_way_tells_the_user_to_restart(tmp_path, capsys):
    folder = tmp_path / "run"
    folder.mkdir()
    assert worker.main(["run", str(folder), "lic", "250"], {worker.TEST_DOUBLE_ENV: "1"}) == worker.EXIT_FAILED
    assert "start_worker.bat again" in capsys.readouterr().err
    assert "restart" in json.loads((folder / "status.json").read_text(encoding="utf-8"))["message"]


# ============================================================================
# 10. WHAT-IF (API update 2, app rules W01–W05)
# ============================================================================
def row_of(api, run_id, sheet, key):
    """Excel row number of the data row with this key (as the sheets endpoint names it, e.g. "AMZ · 12")."""
    rows = api.get(f"/runs/{run_id}/sheets").json()["sheets"][sheet]["rows"]
    return next(r["row"] for r in rows if r["key"] == key)


def test_the_sheets_say_which_cells_may_be_edited(api, done):
    sheets = api.get(f"/runs/{done[0]}/sheets").json()["sheets"]
    st = sheets["Settings"]
    assert [r["key"] for r in st["rows"] if any(r["editable"])] == list(worker.SETTINGS_EDITABLE)  # not NumScenarios …
    demand = sheets["Demand"]
    week12 = next(r for r in demand["rows"] if r["key"] == "AMZ · 12")
    editable = {c for c, e in zip(demand["columns"], week12["editable"], strict=True) if e}
    assert set(worker.DEMAND_QUANTILES) <= editable
    assert not {"ChannelID", "Period", "MeanApprox", "RowCheck", "PeriodStart"} & editable        # keys and formulas
    items = sheets["Items"]
    fg1 = next(r for r in items["rows"] if r["key"] == "FG1")
    assert dict(zip(items["columns"], fg1["editable"], strict=True))["TargetFillRate"] is True      # W04
    assert {k: len(v["rows"]) for k, v in sheets.items()}["Demand"] == 108                         # 3 channels × 36


def test_a_whatif_changes_only_a_copy_and_the_engine_checks_it(api, done):
    original = server.RUNS_DIR / done[0] / "input.xlsx"
    before = hashlib.sha256(original.read_bytes()).hexdigest()
    fg1, rm3 = row_of(api, done[0], "Items", "FG1"), row_of(api, done[0], "Sourcing", "RM3")
    review = row_of(api, done[0], "Settings", "ReviewPeriod")
    edits = [{"sheet": "Items", "row": fg1, "column": "TargetFillRate", "value": 0.9},
             {"sheet": "Sourcing", "row": rm3, "column": "MaxOrderQty", "value": 1500},
             {"sheet": "Settings", "row": review, "column": "Value", "value": 2}]
    r = api.post("/whatif/validate", json={"base_run": done[0], "edits": edits}).json()
    assert r["valid"] is True and r["errors"] == []
    assert r["input"]["product"]["fill_rate_target"] == 0.9 and r["input"]["settings"]["review_period_weeks"] == 2
    assert next(m for m in r["input"]["raw_materials"] if m["id"] == "RM3")["max_order"] == 1500
    assert [(c["sheet"], c["key"], c["column"], c["old"], c["new"]) for c in r["changes"]] == [
        ("Items", "FG1", "TargetFillRate", 0.95, 0.9), ("Sourcing", "RM3", "MaxOrderQty", 1000, 1500),
        ("Settings", "ReviewPeriod", "Value", 1, 2)]
    assert hashlib.sha256(original.read_bytes()).hexdigest() == before                             # W01


def test_a_demand_shift_moves_all_ten_quantiles_of_one_week_and_channel(api, done):
    row = row_of(api, done[0], "Demand", "AMZ · 12")
    r = api.post("/whatif/validate", json={"base_run": done[0], "edits": [
        {"sheet": "Demand", "row": row, "shift": 2000}]}).json()
    assert r["valid"] is True
    old = api.get(f"/runs/{done[0]}/input").json()["demand"]["quantiles"]
    new = r["input"]["demand"]["quantiles"]
    assert new["AMZ"][11] == [q + 2000 for q in old["AMZ"][11]]                                    # W03, exact
    assert all(new[c][w] == old[c][w] for c in old for w in range(36) if (c, w) != ("AMZ", 11))  # nothing else
    assert [c["column"] for c in r["changes"]] == list(worker.DEMAND_QUANTILES)


def test_an_invalid_whatif_is_refused_in_the_engines_words(api, done, tmp_path):
    rm3 = row_of(api, done[0], "Sourcing", "RM3")
    edit = {"sheet": "Sourcing", "row": rm3, "column": "MOQ", "value": 5000}
    wb = openpyxl.load_workbook(TEMPLATE)
    wb["Sourcing"].cell(rm3, 4).value = 5000                                       # the same edit by hand
    wb.save(tmp_path / "by_hand.xlsx")
    expected = engine_errors(tmp_path / "by_hand.xlsx")
    r = api.post("/whatif/validate", json={"base_run": done[0], "edits": [edit]}).json()
    assert r["valid"] is False and r["errors"] == expected and expected
    started = api.post("/runs", data={"base_run": done[0], "edits": json.dumps([edit]), "time_limit": "1"})
    assert started.status_code == 422 and started.json()["detail"]["errors"] == expected          # nothing starts


@pytest.mark.parametrize("edit, words", [
    ({"sheet": "Demand", "key": "AMZ · 12", "column": "MeanApprox", "value": 1}, "cannot be edited"),     # formula
    ({"sheet": "Items", "key": "FG1", "column": "ItemID", "value": "FG9"}, "cannot be edited"),           # key
    ({"sheet": "Settings", "key": "NumScenarios", "column": "Value", "value": 50}, "cannot be edited"),   # W07
    ({"sheet": "Items", "key": "FG1", "shift": 5}, "only possible on Demand"),
    ({"sheet": "Checks", "key": None, "column": "x", "value": 1}, "cannot be edited"),
])
def test_cells_the_app_does_not_let_anyone_change_are_refused(api, done, edit, words):
    row = row_of(api, done[0], edit["sheet"], edit["key"]) if edit["key"] else 5
    e = {k: v for k, v in edit.items() if k != "key"} | {"row": row}
    r = api.post("/whatif/validate", json={"base_run": done[0], "edits": [e]}).json()
    assert r["valid"] is False and words in r["errors"][0] and r["changes"] == []


def test_a_row_that_does_not_exist_is_refused(api, done):
    r = api.post("/whatif/validate", json={"base_run": done[0], "edits": [
        {"sheet": "Demand", "row": 999, "shift": 1}]}).json()
    assert r["valid"] is False and "no data row" in r["errors"][0]


def test_a_whatif_run_records_its_original_and_changes(api, done):
    row = row_of(api, done[0], "Items", "FG1")
    edits = [{"sheet": "Items", "row": row, "column": "TargetFillRate", "value": 0.92},
             {"sheet": "Items", "row": row, "column": "TargetFillRate", "value": 0.9}]       # edited twice: one change
    r = api.post("/runs", data={"base_run": done[0], "edits": json.dumps(edits), "time_limit": "1",
                                "label": "target 90 %"})
    assert r.status_code == 202, r.text
    st = wait(api, r.json()["id"])
    assert st["state"] == "done", st
    assert (st["base_run"], st["changes"], st["label"]) == (done[0], 1, "target 90 %")
    assert st["file"]["name"] == f"What-if of {TEMPLATE.name}"
    assert st["file"]["sha256"] != done[1]["file"]["sha256"]                                       # a different file
    changes = api.get(f"/runs/{st['id']}/changes").json()
    assert [(c["key"], c["old"], c["new"]) for c in changes] == [("FG1", 0.95, 0.9)]
    res = api.get(f"/runs/{st['id']}/result").json()
    assert res["headline"]["fill_rate_target"] == res["run_info"]["fill_rate_target"] == 0.9     # W04
    assert api.get(f"/runs/{st['id']}/input").json()["product"]["fill_rate_target"] == 0.9
    assert api.get(f"/runs/{done[0]}/changes").json() == []                                        # the original


def test_a_run_again_on_the_same_data_uses_the_same_workbook_with_new_settings(api, done):
    row = row_of(api, done[0], "Items", "FG1")
    edit = [{"sheet": "Items", "row": row, "column": "TargetFillRate", "value": 0.9}]
    whatif = wait(api, api.post("/runs", data={"base_run": done[0], "edits": json.dumps(edit),
                                               "time_limit": "1"}).json()["id"])
    for source in (done[1], whatif):                                                   # an upload and a what-if
        r = api.post("/runs", data={"same_data_as": source["id"], "time_limit": "2", "num_scenarios": "30"})
        assert r.status_code == 202, r.text
        again = wait(api, r.json()["id"])
        assert again["state"] == "done", again
        assert again["file"] == source["file"]                                         # byte for byte (W08)
        assert (again["base_run"], again["changes"]) == (source["base_run"], source["changes"])
        assert again["settings"]["time_limit"] == 2 and again["settings"]["overrides"] == {"NumScenarios": 30}
        assert api.get(f"/runs/{again['id']}/changes").json() == api.get(f"/runs/{source['id']}/changes").json()
    assert api.get(f"/runs/{again['id']}/input").json()["product"]["fill_rate_target"] == 0.9
    r = api.post("/whatif/validate", json={"base_run": again["id"], "edits": edit})
    assert r.status_code == 409 and done[0] in r.json()["detail"]                    # still a what-if (W05)


def test_a_whatif_is_never_the_base_of_another_whatif(api, done):
    row = row_of(api, done[0], "Items", "FG1")
    edit = [{"sheet": "Items", "row": row, "column": "TargetFillRate", "value": 0.9}]
    first = wait(api, api.post("/runs", data={"base_run": done[0], "edits": json.dumps(edit),
                                              "time_limit": "1"}).json()["id"])
    r = api.post("/whatif/validate", json={"base_run": first["id"], "edits": edit})
    assert r.status_code == 409 and done[0] in r.json()["detail"]                    # W05: use the original instead
    assert api.post("/runs", data={"base_run": first["id"], "edits": "[]"}).status_code == 409


def test_a_run_needs_either_a_file_or_a_whatif(api, done):
    assert api.post("/runs", data={"time_limit": "1"}).status_code == 400
    both = api.post("/runs", files=upload(TEMPLATE), data={"base_run": done[0], "edits": "[]"})
    assert both.status_code == 400
    assert api.post("/runs", data={"base_run": done[0], "edits": "not json"}).status_code == 422
    assert api.post("/runs", data={"base_run": done[0], "edits": "[]", "same_data_as": done[0]}).status_code == 400
    assert api.post("/runs", data={"same_data_as": "20991231-235959-ffff"}).status_code == 404
    assert api.post("/whatif/validate", json={"base_run": "20991231-235959-ffff", "edits": []}).status_code == 404


def test_the_app_rules_are_served_in_the_model_assumptions_columns(api):
    rules = api.get("/app-rules").json()
    assert rules["columns"] == ["ID", "Topic", "Title", "Rule / mechanic", "Source", "Status", "Where"]
    assert [r[0] for r in rules["rows"]] == ["W01", "W02", "W03", "W04", "W05", "W06", "W07", "W08"]
    assert all(len(r) == len(rules["columns"]) and all(r) for r in rules["rows"])


def test_lots_orders_and_lead_time_branches_can_be_added_and_removed(api, done):
    rm4_lot = next(r["row"] for r in api.get(f"/runs/{done[0]}/sheets").json()["sheets"]["Init_Inventory"]["rows"]
                   if r["key"] == "RMW · RM4" and r["values"][3] == 38)                       # the 400 kg lot
    lt_rm3 = row_of(api, done[0], "LeadTimeDist", "LT_RM3")
    edits = [
        {"sheet": "Init_Inventory", "add": {"LocationID": "RMW", "ItemID": "RM3", "Quantity [UoM]": 500,
                                            "RemainingShelfLife [periods]": 50}},
        {"sheet": "Open_Orders", "add": {"OrderID": "WIF-RM4-01", "OrderType": "Purchase", "ItemID": "RM4",
                                         "FromID": "SUP_ACR", "ToID": "RMW", "Quantity [UoM]": 1000,
                                         "PlacedInPeriod": -1, "ExpectedArrivalPeriod": 11, "ArrivalCertain": "Yes"}},
        {"sheet": "LeadTimeDist", "row": lt_rm3, "column": "Probability", "value": 0.8},      # 8 weeks: 80 %
        {"sheet": "LeadTimeDist", "add": {"DistID": "LT_RM3", "Value [periods]": 10, "Probability": 0.2}},
        {"sheet": "Init_Inventory", "row": rm4_lot, "delete": True}]
    r = api.post("/whatif/validate", json={"base_run": done[0], "edits": edits}).json()
    assert r["valid"] is True, r["errors"]
    inp = r["input"]
    assert {"remaining_shelf_life": 50, "quantity": 500.0} in inp["initial_stock"]["RM3"]
    assert inp["initial_stock"]["RM4"] == [{"remaining_shelf_life": 45, "quantity": 900.0}]  # the 400 kg lot is gone
    assert any(o["qty"] == 1000 and o["arrival"] == 11 for o in inp["open_orders"]["RM4"])
    rm3 = next(m for m in inp["raw_materials"] if m["id"] == "RM3")
    assert rm3["lead_time"] == {"weeks": [8, 10], "probabilities": [0.8, 0.2]}                # now stochastic
    added = {c["row"] for c in r["changes"] if c["sheet"] == "Init_Inventory" and c["old"] is None}
    assert added == {14}                                        # the first row without data (it keeps its RowCheck)


@pytest.mark.parametrize("edit, words", [
    ({"sheet": "Sourcing", "add": {"ItemID": "RM9"}}, "rows can only be added"),
    ({"sheet": "Open_Orders", "add": {"OrderID": "X", "Notes": "no"}}, "a new row takes only"),
    ({"sheet": "Items", "row": 5, "delete": True}, "rows can only be removed"),
])
def test_rows_are_added_or_removed_only_where_allowed(api, done, edit, words):
    r = api.post("/whatif/validate", json={"base_run": done[0], "edits": [edit]}).json()
    assert r["valid"] is False and words in r["errors"][0]
