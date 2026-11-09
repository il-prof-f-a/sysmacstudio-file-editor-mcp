import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CSC = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")
HARNESS = r'''
using System;
using System.Collections.Generic;
using System.Threading;
using System.Web.Script.Serialization;
using SysmacMcp.Runtime;
class Host {
 static void Main(string[] args) {
  var fake=new Fake(args[0]);
  var report=ExistingSolutionCoordinator.Open(fake,"11111111-1111-1111-1111-111111111111",80);
  fake.RunDeferred();
  var result=report.ToDictionary();
  result["open_calls"]=fake.Opens; result["initializations"]=fake.Initializations;
  result["disposals"]=fake.Disposals;
  Console.WriteLine(new JavaScriptSerializer().Serialize(result));
 }
}
class Token : IDisposable {
 public Action Disposing;
 public void Dispose() { Disposing(); }
}
class Fake : IExistingSolutionRuntime {
 string scenario;
 public int Opens,Initializations,Disposals;
 int states,permissions;
 bool permissionRevoked;
 Action<string> listener;
 Action deferred;
 bool hasActive;
 string activeId;
 public Fake(string s) {
  scenario=s; hasActive=s=="already-open";
  activeId=hasActive?"22222222-2222-2222-2222-222222222222":null;
 }
 public void OnUi(Action action) {
  if(scenario=="deferred-ui") { deferred=action; throw new TimeoutException("owned UI delay"); }
  action();
 }
 public SolutionState State() {
  states++;
  if(scenario=="slow-final-check" && states==2) Thread.Sleep(150);
  if(scenario=="slow-snapshot" && Opens>0) Thread.Sleep(150);
  return new SolutionState { HasActiveSolution=hasActive,ActiveSolutionId=activeId,
   IsImporting=scenario=="importing",OpeningDisabled=scenario=="disabled",
   ApplicationReady=scenario!="late-ready" && scenario!="ready-callback-too-early" };
 }
 public int MatchingProjects(string id) { return scenario=="missing"?0:scenario=="duplicate"?2:1; }
 public bool CanView(string id) {
  permissions++;
  return !permissionRevoked && scenario!="permission-denied" && !(scenario=="permission-changed" && permissions>1);
 }
 public bool CanOpen(string id) {
  if(scenario=="state-changed-during-check") {hasActive=true;activeId="22222222-2222-2222-2222-222222222222";}
  if(scenario=="permission-revoked-during-check") permissionRevoked=true;
  return scenario!="command-unavailable";
 }
 public IDisposable SubscribeOpened(Action<string> opened) {
  listener=opened;
  if(scenario=="event-before-request") listener("11111111-1111-1111-1111-111111111111");
  return new Token {Disposing=()=>{Disposals++;if(scenario=="cleanup-failed")throw new Exception("owned cleanup failure");}};
 }
 public void InitializeController() { Initializations++; }
 public void WhenReady(Action action) { if(scenario=="late-ready")deferred=action;else action(); }
 public bool TryOpen(string id,out string error) {
  Opens++; error=null;
  if(scenario=="native-exception") throw new Exception("owned native failure");
  if(scenario=="native-false") error="owned refusal";
  hasActive=true;
  activeId=scenario=="changed-context"?"22222222-2222-2222-2222-222222222222":id;
  if(scenario!="no-event" && scenario!="event-before-request")
   listener(scenario=="wrong-event"?"33333333-3333-3333-3333-333333333333":id);
  return scenario!="native-false";
 }
 public void RunDeferred() { if(deferred!=null) deferred(); }
}
'''


@pytest.fixture(scope="module")
def solution_host(tmp_path_factory):
    if os.name != "nt" or not CSC.is_file():
        pytest.skip("Requires Windows .NET Framework compiler.")
    folder = tmp_path_factory.mktemp("runtime-solutions")
    source = folder / "Host.cs"
    source.write_text(HARNESS, encoding="utf-8")
    executable = folder / "Host.exe"
    run = subprocess.run([str(CSC), "/nologo", "/out:" + str(executable),
                          "/reference:System.Web.Extensions.dll",
                          str(ROOT / "bridge/SysmacRuntime/SolutionCoordinator.cs"), str(source)],
                         capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stdout + run.stderr
    return executable


@pytest.mark.parametrize("scenario,status,success,opens,initializations", [
    ("success", "open_succeeded", True, 1, 0),
    ("already-open", "solution_already_open", False, 0, 0),
    ("importing", "opening_unavailable", False, 0, 0),
    ("disabled", "opening_unavailable", False, 0, 0),
    ("missing", "project_not_found", False, 0, 0),
    ("duplicate", "project_identity_ambiguous", False, 0, 0),
    ("permission-denied", "permission_denied", False, 0, 0),
    ("permission-changed", "permission_denied", False, 0, 0),
    ("command-unavailable", "open_command_unavailable", False, 0, 0),
    ("state-changed-during-check", "solution_already_open", False, 0, 0),
    ("permission-revoked-during-check", "permission_denied", False, 0, 0),
    ("no-event", "open_completion_unknown", False, 1, 0),
    ("wrong-event", "open_completion_unknown", False, 1, 0),
    ("event-before-request", "open_completion_unknown", False, 1, 0),
    ("native-false", "open_failed", False, 1, 0),
    ("changed-context", "context_changed", False, 1, 0),
    ("late-ready", "application_ready_timeout", False, 0, 1),
    ("ready-callback-too-early", "application_not_ready", False, 0, 1),
    ("slow-final-check", "deadline_before_open", False, 0, 0),
    ("slow-snapshot", "snapshot_deadline_exceeded", False, 1, 0),
    ("deferred-ui", "runtime_error", False, 0, 0),
    ("native-exception", "runtime_error", False, 1, 0),
    ("cleanup-failed", "cleanup_failed", False, 1, 0),
])
def test_existing_open_requires_current_permission_native_acceptance_and_event(
    solution_host, scenario, status, success, opens, initializations
):
    run = subprocess.run([str(solution_host), scenario], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stdout + run.stderr
    result = json.loads(run.stdout)
    assert result["status"] == status and result["success"] is success, result
    assert result["open_calls"] == opens and result["initializations"] == initializations
    if success:
        assert result["open_accepted"] and result["opened_event"] and result["cleanup_verified"]
    if opens or initializations:
        assert result["disposals"] == 1


@pytest.mark.parametrize("scenario,active,save,export,importing,disabled,file_mode", [
    ("ready", True, True, True, False, False, False),
    ("export-disabled", True, True, False, False, False, False),
    ("importing", True, False, False, True, True, False),
    ("file-mode", True, False, False, False, False, True),
    ("no-solution", False, False, False, False, False, False),
])
def test_solution_capability_snapshot_reports_read_only_command_availability(
    tmp_path, scenario, active, save, export, importing, disabled, file_mode
):
    if os.name != "nt" or not CSC.is_file():
        pytest.skip("Requires Windows .NET Framework compiler.")
    source = tmp_path / "CapabilityHost.cs"
    source.write_text(r'''
using System;
using System.Collections.Generic;
using System.Web.Script.Serialization;
using SysmacMcp.Runtime;
class CapabilityHost {
 static void Main(string[] args) {
  var runtime=new Fake(args[0]);
  var result=SolutionCapabilityCoordinator.Observe(runtime);
  result["save_checks"]=runtime.SaveChecks; result["export_checks"]=runtime.ExportChecks;
  Console.WriteLine(new JavaScriptSerializer().Serialize(result));
 }
}
class Fake : ISolutionExportRuntime {
 readonly string scenario;
 public int SaveChecks,ExportChecks;
 public Fake(string value) {scenario=value;}
 public void OnUi(Action action) {action();}
 public SolutionExportState State() {return new SolutionExportState {
  HasActiveSolution=scenario!="no-solution", FileMode=scenario=="file-mode",
  IsImporting=scenario=="importing", OpeningDisabled=scenario=="importing"};}
 public IDisposable Subscribe(Action<string> saving,Action<string> saved,Action<Dictionary<string,object>> io) {return null;}
 public bool CanSave() {SaveChecks++;return scenario!="importing" && scenario!="file-mode" && scenario!="no-save";}
 public void Save() {throw new Exception("Status must not save.");}
 public bool CanExport() {ExportChecks++;return scenario!="importing" && scenario!="file-mode" && scenario!="export-disabled";}
 public bool PreExecuteExport() {throw new Exception("Status must not execute export preflight.");}
 public void Export(string destination) {throw new Exception("Status must not export.");}
}''', encoding="utf-8")
    executable = tmp_path / "CapabilityHost.exe"
    compiled = subprocess.run(
        [str(CSC), "/nologo", "/out:" + str(executable), "/reference:System.Web.Extensions.dll",
         str(ROOT / "bridge/SysmacRuntime/SolutionCoordinator.cs"), str(source)],
        capture_output=True, text=True, timeout=30,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    run = subprocess.run([str(executable), scenario], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stdout + run.stderr
    result = json.loads(run.stdout)
    assert result["has_active_solution"] is active
    assert result["can_save_solution"] is save and result["can_export_solution"] is export
    assert result["is_importing"] is importing and result["opening_disabled"] is disabled
    assert result["file_mode"] is file_mode
    assert result["save_checks"] == int(active) and result["export_checks"] == int(active)


def test_process_runtime_lease_serializes_across_assembly_versions(tmp_path):
    if os.name != "nt" or not CSC.is_file():
        pytest.skip("Requires Windows .NET Framework compiler.")
    assemblies = []
    for name in ("LeaseOne", "LeaseTwo"):
        assembly = tmp_path / (name + ".dll")
        run = subprocess.run([str(CSC), "/nologo", "/target:library", "/out:" + str(assembly),
                              str(ROOT / "bridge/SysmacRuntime/RuntimeLease.cs")],
                             capture_output=True, text=True, timeout=30)
        assert run.returncode == 0, run.stdout + run.stderr
        assemblies.append(assembly)
    source = tmp_path / "LeaseHost.cs"
    source.write_text(r'''
using System;
using System.Reflection;
class Host {
 static object Enter(string file) {
  return Assembly.LoadFrom(file).GetType("SysmacMcp.Runtime.RuntimeLease")
   .GetMethod("TryEnter",BindingFlags.Static|BindingFlags.NonPublic).Invoke(null,null);
 }
 static void Main(string[] args) {
  var first=(IDisposable)Enter(args[0]);
  if(first==null || Enter(args[1])!=null) throw new Exception("Concurrent lease accepted.");
  first.Dispose(); first.Dispose();
  var next=(IDisposable)Enter(args[1]);
  if(next==null) throw new Exception("Lease was not released.");
  next.Dispose(); Console.WriteLine("passed");
 }
}''', encoding="utf-8")
    executable = tmp_path / "LeaseHost.exe"
    subprocess.run([str(CSC), "/nologo", "/out:" + str(executable), str(source)],
                   capture_output=True, text=True, check=True, timeout=30)
    run = subprocess.run([str(executable), *map(str, assemblies)], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0 and run.stdout.strip() == "passed", run.stdout + run.stderr


@pytest.mark.parametrize("scenario", ["running", "pending"])
def test_runtime_lease_covers_inflight_ui_work_and_cancels_unstarted_callbacks(tmp_path, scenario):
    if os.name != "nt" or not CSC.is_file():
        pytest.skip("Requires Windows .NET Framework compiler.")
    source = tmp_path / "WorkHost.cs"
    source.write_text(r'''
using System;
using System.Threading;
using SysmacMcp.Runtime;
class Host {
 static void Main(string[] args) {
  var entered=new ManualResetEvent(false); var release=new ManualResetEvent(false);
  int effects=0;
  var lease=RuntimeLease.TryEnter();
  var work=lease.Track(()=>{Interlocked.Increment(ref effects);entered.Set();release.WaitOne(3000);});
  Thread thread=null;
  if(args[0]=="running") {
   thread=new Thread(work.Invoke);thread.Start();
   if(!entered.WaitOne(1000))throw new Exception("Work never entered.");
   if(lease.CloseAndInspect())throw new Exception("Running work declared settled.");
   if(RuntimeLease.TryEnter()!=null)throw new Exception("Lease released while target still running.");
   release.Set();thread.Join(1000);
  } else {
   if(!lease.CloseAndInspect())throw new Exception("Pending work was not cancelled.");
   work.Invoke();
   if(effects!=0)throw new Exception("Cancelled callback ran.");
  }
  lease.Dispose();
  var next=RuntimeLease.TryEnter();
  if(next==null)throw new Exception("Completed work kept lease.");
  next.Dispose();Console.WriteLine("passed");
 }
}''', encoding="utf-8")
    executable = tmp_path / "WorkHost.exe"
    compiled = subprocess.run([str(CSC), "/nologo", "/out:" + str(executable),
                               str(ROOT / "bridge/SysmacRuntime/RuntimeLease.cs"), str(source)],
                              capture_output=True, text=True, timeout=30)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    run = subprocess.run([str(executable), scenario], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0 and run.stdout.strip() == "passed", run.stdout + run.stderr


def test_entry_keeps_lease_and_reports_uncertain_cleanup_until_late_dispatch_finishes(tmp_path):
    if os.name != "nt" or not CSC.is_file():
        pytest.skip("Requires Windows .NET Framework and WPF.")
    source = tmp_path / "EntryLifetimeHost.cs"
    source.write_text(r'''
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows;
using System.Windows.Threading;
using SysmacMcp.Runtime;
namespace SysmacMcp.Runtime {
 internal class ReflectionSolutionRuntime : IExistingSolutionRuntime {
  internal static ManualResetEvent Entered=new ManualResetEvent(false),Release=new ManualResetEvent(false);
  readonly int timeout; readonly RuntimeLease lease;
  internal ReflectionSolutionRuntime(int timeout,RuntimeLease lease) {this.timeout=timeout;this.lease=lease;}
  public void OnUi(Action action) {ReflectionRuntime.Dispatch(action,timeout,lease);}
  public SolutionState State() {return new SolutionState {ApplicationReady=true};}
  public int MatchingProjects(string id) {return 1;}
  public bool CanView(string id) {return true;}
  public bool CanOpen(string id) {return true;}
  public IDisposable SubscribeOpened(Action<string> callback) {
   var token=new Token();Entered.Set();
   if(!Release.WaitOne(5000))throw new TimeoutException("owned test release missing");
   return token;
  }
  public void InitializeController() {throw new Exception("Unexpected initialization");}
  public void WhenReady(Action action) {throw new Exception("Unexpected ready request");}
  public bool TryOpen(string id,out string error) {throw new Exception("Unexpected project effect");}
  internal Dictionary<string,object> ListProjects(int offset,int limit) {throw new Exception("Unexpected listing");}
  class Token : IDisposable {public void Dispose() {throw new Exception("owned late cleanup failure");}}
 }
}
class EntryLifetimeHost {
 [STAThread] static int Main(string[] args) {
  var app=new Application {ShutdownMode=ShutdownMode.OnExplicitShutdown};
  Exception failure=null; Dictionary<string,object> report=null;
  var thread=new Thread(()=>{
   try {
    var request=new Dictionary<string,object> {{"operation","open-existing"},{"output",args[0]},
     {"process_id",Process.GetCurrentProcess().Id},{"process_started_utc",Process.GetCurrentProcess().StartTime.ToUniversalTime().ToString("O")},
     {"solution_id","11111111-1111-1111-1111-111111111111"},{"timeout_ms",300}};
    int code=Entry.Invoke(new JavaScriptSerializer().Serialize(request));
    report=new JavaScriptSerializer().Deserialize<Dictionary<string,object>>(File.ReadAllText(args[0]));
    if(code!=2 || (bool)report["success"] || (bool)report["cleanup_verified"] || (bool)report["runtime_work_settled"])
     throw new Exception("Unsettled entry claimed completion/cleanup.");
    if(!ReflectionSolutionRuntime.Entered.WaitOne(0))throw new Exception("Owned subscription never entered.");
    if(RuntimeLease.TryEnter()!=null)throw new Exception("Lease released before UI callback completed.");
    ReflectionSolutionRuntime.Release.Set();
    RuntimeLease next=null;var clock=Stopwatch.StartNew();
    while(clock.ElapsedMilliseconds<3000 && next==null) {Thread.Sleep(1);next=RuntimeLease.TryEnter();}
    if(next==null)throw new Exception("Completed callback never released lease.");
    next.Dispose();
   } catch(Exception error) {failure=error;}
   finally {ReflectionSolutionRuntime.Release.Set();app.Dispatcher.BeginInvokeShutdown(DispatcherPriority.Background);}
  });
  thread.Start();app.Run();thread.Join(5000);
  if(failure!=null) {Console.Error.WriteLine(failure);return 1;}
  Console.WriteLine(new JavaScriptSerializer().Serialize(report));return 0;
 }
}''', encoding="utf-8")
    executable = tmp_path / "EntryLifetimeHost.exe"
    runtime = ROOT / "bridge/SysmacRuntime"
    command = [str(CSC), "/nologo", "/platform:x64", "/out:" + str(executable),
               "/reference:System.Web.Extensions.dll", "/reference:System.Xaml.dll",
               "/reference:" + str(CSC.parent / "WPF/WindowsBase.dll"),
               "/reference:" + str(CSC.parent / "WPF/PresentationFramework.dll"),
                 *[str(runtime / name) for name in ("RuntimeLease.cs", "SolutionCoordinator.cs",
                                                       "BuildCoordinator.cs", "RuntimeProgress.cs", "Entry.cs", "ReflectionRuntime.cs",
                                                   "ReflectionSolutionExportRuntime.cs",
                                                   "ReflectionSolutionCloseRuntime.cs")],
               str(source)]
    compiled = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    run = subprocess.run([str(executable), str(tmp_path / "receipt.json")],
                         capture_output=True, text=True, timeout=15)
    assert run.returncode == 0, run.stdout + run.stderr
    result = json.loads(run.stdout)
    assert result["status"] == "runtime_work_unsettled"
    assert result["cleanup_verified"] is False and result["runtime_work_settled"] is False
