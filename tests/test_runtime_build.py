import json
import os
import shutil
import subprocess
import sys
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

class TestHost {
    static int Main(string[] args) {
        var runtime = new Fake(args[0]);
        var expected = new BuildContext { SolutionId = "11111111-1111-1111-1111-111111111111", DeviceId = "22222222-2222-2222-2222-222222222222" };
        var result = BuildCoordinator.Execute(runtime, expected, 80);
        runtime.RunDeferred();
        var data = result.ToDictionary();
        data["effect_invocations"] = runtime.Calls;
        data["cleanup_invocations"] = runtime.Disposals;
        Console.WriteLine(new JavaScriptSerializer().Serialize(data));
        return 0;
    }
}
class Subscription : IDisposable {
    public Action DisposeAction;
    public void Dispose() { DisposeAction(); }
}
class Fake : IBuildRuntime {
    string scenario;
    public int Calls, Disposals;
    Action<BuildSignal> started, completed;
    Action deferred;
    int uiCalls;
    int contextReads;
    Thread pending;
    ManualResetEvent snapshotEntered=new ManualResetEvent(false),snapshotContinue=new ManualResetEvent(false);
    BuildContext context = new BuildContext { SolutionId = "11111111-1111-1111-1111-111111111111", DeviceId = "22222222-2222-2222-2222-222222222222" };
    public Fake(string scenario) { this.scenario = scenario; }
    public void OnUi(Action action) {
        uiCalls++;
        if(scenario=="deferred-ui" || (scenario=="deferred-snapshot" && uiCalls==2)) { deferred=action; throw new TimeoutException("UI did not start the action."); }
        if(scenario=="slow-snapshot" && uiCalls==2) {
            pending=new Thread(()=>action()); pending.Start();
            if(!snapshotEntered.WaitOne(1000)) throw new Exception("snapshot never started");
            throw new TimeoutException("snapshot still running");
        }
        action();
    }
    public void RunDeferred() { if(deferred!=null) deferred(); snapshotContinue.Set(); if(pending!=null) pending.Join(1000); }
    public BuildContext Context() {
        contextReads++;
        if(scenario=="slow-final-context" && contextReads==2) Thread.Sleep(150);
        return context;
    }
    public bool CanRebuild() { return true; }
    public int ApplicableTargets() { return scenario == "empty-targets" ? 0 : 3; }
    public IDisposable Subscribe(Action<BuildSignal> start, Action<BuildSignal> end) {
        started=start; completed=end;
        if(scenario=="events-before-invocation") {
            var signal = new BuildSignal {DeviceId=context.DeviceId,IsBuildAction=true};
            started(signal); completed(signal);
        }
        return new Subscription { DisposeAction = () => { Disposals++; if(scenario=="cleanup-error") throw new Exception("synthetic cleanup failure"); } };
    }
    public void Rebuild() {
        Calls++;
        if(scenario=="slow-rebuild") Thread.Sleep(150);
        if(scenario=="no-event" || scenario=="events-before-invocation") return;
        var signal = new BuildSignal { DeviceId=context.DeviceId, IsBuildAction=true };
        started(signal);
        completed(new BuildSignal { DeviceId="99999999-9999-9999-9999-999999999999", IsBuildAction=true });
        completed(new BuildSignal { DeviceId=context.DeviceId, IsBuildAction=false });
        if(scenario!="wrong-events") completed(signal);
        if(scenario=="changed-context") context=new BuildContext {SolutionId=context.SolutionId,DeviceId="33333333-3333-3333-3333-333333333333"};
    }
    public BuildSnapshot Snapshot() {
        if(scenario=="slow-synchronous-snapshot") Thread.Sleep(150);
        if(scenario=="slow-snapshot") { snapshotEntered.Set(); if(!snapshotContinue.WaitOne(2000)) throw new Exception("snapshot release missing"); }
        return new BuildSnapshot { NativeSuccess=scenario!="native-failed", ControllerCompleted=true,
            ErrorCount=scenario=="compiler-errors" ? 1 : 0, WarningCount=2, DiagnosticsComplete=true,
            Diagnostics=new List<Dictionary<string,object>>() };
    }
}
'''


@pytest.fixture(scope="module")
def runtime_harness(tmp_path_factory):
    if os.name != "nt" or not CSC.is_file():
        pytest.skip("Requires Windows .NET Framework compiler.")
    folder = tmp_path_factory.mktemp("runtime-build")
    harness = folder / "TestHost.cs"
    harness.write_text(HARNESS, encoding="utf-8")
    executable = folder / "TestHost.exe"
    command = [str(CSC), "/nologo", "/out:" + str(executable),
               "/reference:System.Web.Extensions.dll",
               str(ROOT / "bridge/SysmacRuntime/BuildCoordinator.cs"),
               str(harness)]
    built = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert built.returncode == 0, built.stdout + built.stderr
    return executable


@pytest.mark.parametrize(
    "scenario,status,success,effects",
    [
        ("success", "build_succeeded", True, 1),
        ("empty-targets", "coverage_unavailable", False, 0),
        ("no-event", "completion_unknown", False, 1),
        ("wrong-events", "completion_unknown", False, 1),
        ("changed-context", "context_changed", False, 1),
        ("native-failed", "build_failed", False, 1),
        ("compiler-errors", "build_failed", False, 1),
        ("cleanup-error", "cleanup_failed", False, 1),
        ("deferred-ui", "runtime_error", False, 0),
        ("deferred-snapshot", "runtime_error", False, 1),
        ("slow-snapshot", "runtime_error", False, 1),
        ("events-before-invocation", "completion_unknown", False, 1),
        ("slow-final-context", "deadline_before_start", False, 0),
        ("slow-rebuild", "completion_deadline_exceeded", False, 1),
        ("slow-synchronous-snapshot", "diagnostic_deadline_exceeded", False, 1),
    ],
)
def test_build_requires_correlated_completion_context_and_nonvacuous_success(
    runtime_harness, scenario, status, success, effects
):
    run = subprocess.run([str(runtime_harness), scenario], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stdout + run.stderr
    result = json.loads(run.stdout)
    assert result["success"] is success and result["status"] == status
    assert result["effect_invocations"] == effects
    if effects:
        assert result["cleanup_invocations"] == 1
    if success:
        assert result["completed"] and result["applicable_targets"] == 3
        assert result["errors"] == 0 and result["warnings"] == 2


def test_runtime_entry_rejects_arbitrary_actions_without_vendor_context(tmp_path):
    if os.name != "nt" or not CSC.is_file():
        pytest.skip("Requires Windows .NET Framework compiler.")
    built = subprocess.run(
        [sys.executable, str(ROOT / "scripts/build-sysmac-runtime.py"), "--output", str(tmp_path)],
        capture_output=True, text=True, timeout=30,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    profile = json.loads(built.stdout.splitlines()[-1])
    assembly = Path(profile["assembly"])
    assert assembly.name == "SysmacRuntime_" + profile["source_sha256"][:16] + ".dll"
    relocated = tmp_path / "relocated"
    relocated.mkdir()
    for item in [assembly, tmp_path / "build-profile.json"]:
        shutil.copy2(item, relocated / item.name)
    cached = subprocess.run(
        [sys.executable, str(ROOT / "scripts/build-sysmac-runtime.py"), "--output", str(relocated)],
        capture_output=True, text=True, timeout=30, check=True,
    )
    cached_profile = json.loads(cached.stdout.splitlines()[-1])
    assert Path(cached_profile["assembly"]).resolve() == (relocated / assembly.name).resolve()
    source = tmp_path / "EntryHost.cs"
    source.write_text(
        'using System; class EntryHost { static void Main(string[] a) { Console.WriteLine(SysmacMcp.Runtime.Entry.Invoke(a[0])); } }',
        encoding="utf-8",
    )
    executable = tmp_path / "EntryHost.exe"
    subprocess.run([str(CSC), "/nologo", "/out:" + str(executable),
                    "/reference:" + str(assembly), str(source)],
                   capture_output=True, text=True, check=True)
    for operation, expected in [("delete", "unsupported_operation"), ("status", "framework_unavailable"),
                                ("list-projects", "framework_unavailable"),
                                ("open-existing", "process_identity_changed")]:
        output = tmp_path / (operation + ".json")
        request_body = {"operation": operation, "output": str(output)}
        if operation == "status":
            request_body["progress_output"] = str(tmp_path / "status-loop.jsonl")
        request = json.dumps(request_body)
        run = subprocess.run([str(executable), request], capture_output=True, text=True, timeout=10)
        assert run.returncode == 0, run.stdout + run.stderr
        result = json.loads(output.read_text(encoding="utf-8"))
        assert result["success"] is False and result["status"] == expected
        assert result["operation_started"] is False
    relative = "C:drive-relative.json"
    run = subprocess.run(
        [str(executable), json.dumps({"operation": "status", "output": relative})],
        capture_output=True, text=True, timeout=10, cwd=tmp_path,
    )
    assert run.returncode == 0
    assert not (tmp_path / "drive-relative.json").exists()

    progress_file = tmp_path / "status-progress.jsonl"
    response_file = tmp_path / "status-with-progress.json"
    request = json.dumps({"operation": "status", "output": str(response_file),
                          "progress_output": str(progress_file)})
    run = subprocess.run([str(executable), request], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0 and run.stdout.strip() == "2", run.stdout + run.stderr
    result = json.loads(response_file.read_text(encoding="utf-8"))
    assert result["status"] == "framework_unavailable"
    progress = [json.loads(line)["stage"] for line in progress_file.read_text(encoding="utf-8").splitlines()]
    assert progress == ["entry_invoked", "response_reserved", "response_serialization_started", "response_written"]


def test_status_runtime_path_does_not_enter_export_capability_adapter():
    source = (ROOT / "bridge/SysmacRuntime/Entry.cs").read_text(encoding="utf-8")
    status_start = source.index('if (operation == "status") {')
    status_end = source.index("} else {", status_start)
    status_path = source[status_start:status_end]
    assert "runtime.OnUi(delegate {" in status_path
    assert "observed = runtime.Status();" in status_path
    assert "ReflectionSolutionExportRuntime" not in status_path
    assert "SolutionCapabilityCoordinator.Observe" not in status_path


def test_managed_progress_writer_and_reader_keep_closed_stage_vocabulary(tmp_path):
    if os.name != "nt" or not CSC.is_file():
        pytest.skip("Requires Windows .NET Framework compiler.")
    source = tmp_path / "ProgressHost.cs"
    source.write_text(r'''
using SysmacMcp.Runtime;
class ProgressHost {
    static void Main(string[] args) {
        var progress = new RuntimeProgress(args[0]);
        progress.Record("entry_invoked");
        progress.Record("export_request_parameters_loaded");
        progress.Record("export_coordinator_entered");
        progress.Record("export_arguments_validated");
        progress.Record("export_storage_path_validated");
        progress.Record("export_destination_path_validated");
        progress.Record("project_name_must_not_be_logged");
        progress.Record("native_export_started");
        progress.Dispose();
    }
}''', encoding="utf-8")
    executable = tmp_path / "ProgressHost.exe"
    progress_file = tmp_path / "managed-progress.jsonl"
    subprocess.run(
        [str(CSC), "/nologo", "/out:" + str(executable),
         str(ROOT / "bridge/SysmacRuntime/RuntimeProgress.cs"), str(source)],
        capture_output=True, text=True, check=True,
    )
    run = subprocess.run([str(executable), str(progress_file)], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stdout + run.stderr
    from sysmac_mcp.ide import IdeClient
    assert IdeClient._managed_progress(tmp_path) == (
        [
            "entry_invoked",
            "export_request_parameters_loaded",
            "export_coordinator_entered",
            "export_arguments_validated",
            "export_storage_path_validated",
            "export_destination_path_validated",
            "native_export_started",
        ],
        "valid",
    )
