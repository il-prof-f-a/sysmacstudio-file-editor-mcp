import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CSC = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")
EXPECTED = "11111111-1111-1111-1111-111111111111"

CLOSE_HARNESS = r'''
using System;
using System.Collections.Generic;
using System.Threading;
using System.Web.Script.Serialization;
using SysmacMcp.Runtime;
class Host {
 static void Main(string[] args) {
  var fake=new Fake(args[0]);
  var result=SolutionCloseCoordinator.Close(fake,"11111111-1111-1111-1111-111111111111",80);
  var row=result.ToDictionary();
  row["commits"]=fake.Commits; row["can_checks"]=fake.CanChecks; row["close_calls"]=fake.CloseCalls;
  row["disposals"]=fake.Disposals; row["modified_checks"]=fake.ModifiedChecks;
  Console.WriteLine(new JavaScriptSerializer().Serialize(row));
 }
}
class Token : IDisposable {
 public Action Disposing;
 public void Dispose() { Disposing(); }
}
class Fake : ISolutionCloseRuntime {
 const string ID="11111111-1111-1111-1111-111111111111";
 readonly string scenario;
 Action<string> closing,closed,cancelled;
 bool active=true,modified;
 int stateCalls;
 public int Commits,CanChecks,CloseCalls,Disposals,ModifiedChecks;
 public Fake(string value) { scenario=value; active=value!="no-active"; modified=false; }
 public void OnUi(Action action) { action(); }
 public SolutionCloseState State() {
  stateCalls++;
  if(scenario=="changed-after-commit" && stateCalls>=2) return MakeState("22222222-2222-2222-2222-222222222222");
  if(scenario=="post-snapshot-active" && CloseCalls>0) active=true;
  string id=scenario=="wrong-active"?"22222222-2222-2222-2222-222222222222":(active?ID:null);
  var state=MakeState(id);
  state.HasActiveSolution=active;
  state.FileMode=scenario=="file-mode";
  state.AutoConnect=scenario=="autoconnect";
  state.SuppressSave=scenario=="suppressed";
  state.IsImporting=scenario=="importing";
  state.OpeningDisabled=scenario=="opening-disabled";
  state.ApplicationReady=scenario!="not-ready";
  return state;
 }
 SolutionCloseState MakeState(string id) { return new SolutionCloseState { HasActiveSolution=active,ActiveSolutionId=id,ApplicationReady=true }; }
 public void CommitEditorChanges() { Commits++; if(scenario=="commit-error")throw new Exception("commit failed"); if(scenario=="dirty" || scenario=="dirty-after-commit")modified=true; }
 public bool IsSolutionModified(string id) { ModifiedChecks++; if(id!=ID)throw new Exception("wrong identity"); return modified; }
 public IDisposable SubscribeClose(Action<string> onClosing,Action<string> onClosed,Action<string> onCancelled) {
  closing=onClosing; closed=onClosed; cancelled=onCancelled;
  return new Token { Disposing=()=>{Disposals++; if(scenario=="cleanup-error")throw new Exception("cleanup failed");} };
 }
 public bool CanClose(string id) { CanChecks++; return scenario!="command-unavailable"; }
 public void Close(string id) {
  CloseCalls++;
  if(scenario=="close-error")throw new Exception("close failed");
  string actual=scenario=="wrong-events"?"33333333-3333-3333-3333-333333333333":ID;
  if(scenario!="no-events" && closing!=null)closing(actual);
  if(scenario=="cancelled") { if(cancelled!=null)cancelled(actual); return; }
  active=false;
  if(scenario!="no-events" && scenario!="no-closed-event" && closed!=null)closed(actual);
 }
}
'''


@pytest.fixture(scope="module")
def close_host(tmp_path_factory):
    if os.name != "nt" or not CSC.is_file():
        pytest.skip("Requires Windows .NET Framework compiler.")
    folder = tmp_path_factory.mktemp("runtime-close")
    source = folder / "CloseHost.cs"
    source.write_text(CLOSE_HARNESS, encoding="utf-8")
    executable = folder / "CloseHost.exe"
    run = subprocess.run(
        [str(CSC), "/nologo", "/out:" + str(executable),
         "/reference:System.Web.Extensions.dll",
         str(ROOT / "bridge/SysmacRuntime/SolutionCoordinator.cs"), str(source)],
        capture_output=True, text=True, timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    return executable


@pytest.mark.parametrize(
    "scenario,status,success,close_calls",
    [
        ("success", "close_succeeded", True, 1),
        ("no-active", "no_active_solution", False, 0),
        ("wrong-active", "solution_context_changed", False, 0),
        ("file-mode", "file_mode_unsupported", False, 0),
        ("autoconnect", "autoconnect_unsupported", False, 0),
        ("suppressed", "save_suppression_unsupported", False, 0),
        ("importing", "opening_unavailable", False, 0),
        ("opening-disabled", "opening_unavailable", False, 0),
        ("not-ready", "application_not_ready", False, 0),
        ("dirty", "unsaved_changes", False, 0),
        ("changed-after-commit", "solution_context_changed", False, 0),
        ("command-unavailable", "close_command_unavailable", False, 0),
        ("cancelled", "close_cancelled", False, 1),
        ("no-closed-event", "close_completion_unknown", False, 1),
        ("no-events", "close_completion_unknown", False, 1),
        ("wrong-events", "close_completion_unknown", False, 1),
        ("post-snapshot-active", "close_completion_unknown", False, 1),
        ("commit-error", "runtime_error", False, 0),
        ("close-error", "runtime_error", False, 1),
        ("cleanup-error", "cleanup_failed", False, 1),
    ],
)
def test_attested_close_requires_clean_matching_solution_and_vendor_events(
    close_host, scenario, status, success, close_calls
):
    run = subprocess.run([str(close_host), scenario], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stdout + run.stderr
    result = json.loads(run.stdout)
    assert result["status"] == status and result["success"] is success, result
    assert result["close_calls"] == close_calls
    if scenario in {"dirty", "changed-after-commit", "command-unavailable", "success", "cancelled"}:
        assert result["commits"] == 1
    if success:
        assert result["close_requested"] and result["close_returned"]
        assert result["closing_event"] and result["closed_event"]
        assert result["cleanup_verified"] and result["outcome_unknown"] is False
    if scenario == "cancelled":
        assert result["active_solution_id"] == EXPECTED and result["outcome_unknown"] is False
    if result["close_requested"]:
        assert result["disposals"] == 1
