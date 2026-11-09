import json
import subprocess
import threading
from pathlib import Path

import pytest
from mcp import Client

from sysmac_mcp.server import create_server
from sysmac_mcp.workspace import ProjectError, Workspace


def bridge(tmp_path, monkeypatch, runner):
    from sysmac_mcp import ide
    monkeypatch.setattr(ide, "platform_supported", lambda: True)
    helper = tmp_path / "helper with spaces.exe"
    helper.write_bytes(b"test executable placeholder")
    return ide.IdeClient(helper, host_id="test-host", runner=runner)


def completed(command, **kwargs):
    operation = "probe" if command[1] == "probe" else command[command.index("--operation") + 1]
    payload = {"success": True, "status": "connected" if operation == "probe" else "callback_received",
               "callback_received": operation != "probe", "result": None if operation == "probe" else "Success",
               "execution_name": {"open": "OpenFromFile", "export": "Export"}.get(operation)}
    return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")


def test_optional_helper_absence_is_explicit_and_never_launches(tmp_path, monkeypatch):
    from sysmac_mcp.ide import IdeClient
    monkeypatch.delenv("SYSMAC_IDE_CLIENT", raising=False)
    client = IdeClient(tmp_path / "absent.exe")
    assert not client.capabilities()["configured"]
    with pytest.raises(ProjectError, match="helper"):
        client.probe()


def test_probe_uses_typed_argument_list_and_no_shell(tmp_path, monkeypatch):
    calls = []
    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return completed(command, **kwargs)
    client = bridge(tmp_path, monkeypatch, runner)
    result = client.probe()
    command, options = calls[0]
    assert command[1:] == ["probe", "--host-id", "test-host", "--timeout-seconds", "180"]
    assert options["shell"] is False
    assert result["success"] and result["sysmac_build"] == "not_executed"


def test_stale_revision_never_opens_ide(tmp_path, project_file, monkeypatch):
    workspace = Workspace(tmp_path / "state")
    project = workspace.open_project(project_file)
    calls = []
    client = bridge(tmp_path, monkeypatch, lambda *a, **k: calls.append(a))
    with pytest.raises(ProjectError, match="revision"):
        client.open_copy(workspace, project["project_id"], expected_revision=55)
    assert calls == []


def test_open_sends_verified_new_copy_and_keeps_source(tmp_path, project_file, monkeypatch):
    workspace = Workspace(tmp_path / "state")
    project = workspace.open_project(project_file)
    before = project_file.read_bytes()
    calls = []
    def runner(command, **kwargs):
        path = Path(command[command.index("--path") + 1])
        assert path != project_file and path.read_bytes() == before
        calls.append(path)
        return completed(command, **kwargs)
    client = bridge(tmp_path, monkeypatch, runner)
    result = client.open_copy(workspace, project["project_id"], expected_revision=0)
    assert result["success"] and result["revision"] == 0
    assert project_file.read_bytes() == before and calls[0].is_file()
    assert result["sysmac_build"] == "not_executed"


def test_open_already_imported_returns_typed_guidance_and_receipt(tmp_path, project_file, monkeypatch):
    workspace = Workspace(tmp_path / "state")
    project = workspace.open_project(project_file)
    before = project_file.read_bytes()
    vendor_error = "Il progetto è stato già importato."

    def runner(command, **kwargs):
        result = {
            "success": False,
            "status": "callback_received",
            "callback_received": True,
            "execution_name": "OpenFromFile",
            "result": "RuntimeError",
            "error": vendor_error,
        }
        return subprocess.CompletedProcess(command, 2, json.dumps(result), "")

    client = bridge(tmp_path, monkeypatch, runner)
    with pytest.raises(ProjectError) as raised:
        client.open_copy(workspace, project["project_id"], expected_revision=0)

    assert raised.value.code == "ide_project_already_imported"
    assert "File → Chiudi" in str(raised.value)
    assert "File → Importa" in str(raised.value)
    assert project_file.read_bytes() == before

    jobs = list((workspace.root / "ide-jobs").iterdir())
    assert len(jobs) == 1
    receipt = json.loads((jobs[0] / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["success"] is False
    assert receipt["phase"] == "callback_received"
    assert receipt["outcome_unknown"] is False
    assert receipt["ide_error"] == vendor_error


def test_legacy_export_is_refused_before_uac(tmp_path, monkeypatch):
    calls = []
    client = bridge(tmp_path, monkeypatch, lambda *a, **k: calls.append(a))
    with pytest.raises(ProjectError, match="Legacy WCF export is disabled"):
        client._execute("export", tmp_path / "not-created.smc2")
    assert not calls


def test_timeout_is_uncertain_and_not_retried(tmp_path, monkeypatch):
    calls = []
    def runner(command, **kwargs):
        calls.append(command)
        raise subprocess.TimeoutExpired(command, 1)
    client = bridge(tmp_path, monkeypatch, runner)
    with pytest.raises(ProjectError, match="unknown"):
        client.probe()
    assert len(calls) == 1


def test_export_existing_destination_is_rejected_before_uac(tmp_path, monkeypatch):
    calls = []
    client = bridge(tmp_path, monkeypatch, lambda *a, **k: calls.append(a))
    storage = tmp_path / "sysmac-storage"
    storage.mkdir()
    target = tmp_path / "keep.smc2"
    target.write_bytes(b"unchanged")
    with pytest.raises(ProjectError, match="exists"):
        client.export_active(tmp_path / "state", target, process_id=9123,
                             process_started_utc="2024-01-02T03:04:05.0000000Z",
                             solution_id="55c5f0a5-4330-4d36-aad4-f5b5926882d2",
                             storage_folder=str(storage))
    assert target.read_bytes() == b"unchanged" and not calls


def test_export_validates_and_publishes_normal_copy(tmp_path, project_file, monkeypatch):
    client = bridge(tmp_path, monkeypatch, lambda *a, **k: pytest.fail("runtime export must use the guarded adapter"))
    storage = tmp_path / "sysmac-storage"
    storage.mkdir()
    process_start = "2024-01-02T03:04:05.0000000Z"
    solution_id = "55c5f0a5-4330-4d36-aad4-f5b5926882d2"

    def runtime_export(root, request, *, job, defer_receipt):
        assert defer_receipt is True
        staged_archive = Path(request["archive_output"])
        assert staged_archive.parent == target.parent
        assert staged_archive.drive == storage.drive
        assert staged_archive != target
        client._request(job, {**request, "output": str(job / "managed-response.json"),
                              "timeout_ms": client.timeout * 1000})
        staged_archive.write_bytes(project_file.read_bytes())
        return {
            "success": True, "status": "export_succeeded", "schema_version": 1,
            "process_id": request["process_id"], "process_started_utc": request["process_started_utc"],
            "solution_id": request["solution_id"], "active_solution_id": request["solution_id"],
            "storage_folder": request["storage_folder"], "archive_output": request["archive_output"],
            "operation_started": True, "save_requested": True, "save_command_returned": True,
            "save_started": True, "save_completed": True, "export_preflight_passed": True,
            "export_started": True, "export_returned": True, "outcome_unknown": False,
            "cleanup_verified": True, "io_events": [], "transport_cleanup_verified": True,
            "runtime_work_settled": True,
        }
    monkeypatch.setattr(client, "_runtime_job", runtime_export)
    target = tmp_path / "export.smc2"
    result = client.export_active(tmp_path / "state", target, process_id=9123,
                                  process_started_utc=process_start, solution_id=solution_id,
                                  storage_folder=str(storage))
    assert result["success"] and target.read_bytes() == project_file.read_bytes()
    assert result["active_project_identity"] == "attested"
    assert result["sysmac_build"] == "not_executed"


async def test_ide_tools_discoverable_and_disabled_result_explicit(tmp_path):
    async with Client(create_server(tmp_path / "state")) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert {"plc_ide_probe", "plc_ide_open_copy", "plc_ide_export_active", "plc_ide_close_active"} <= names
        caps = (await client.call_tool("plc_get_capabilities", {})).structured_content
        assert caps["sysmac_ide"]["elevation"] == "one_shot_user_uac"
        assert caps["sysmac_ide"]["real_runtime_build_verified"] is True
        assert caps["sysmac_ide"]["real_runtime_export_verified"] is False
        assert caps["sysmac_ide"]["runtime_close"] == "experimental_attested_clean_solution_close"
        assert caps["sysmac_ide"]["real_runtime_close_verified"] is False
        assert caps["sysmac_build"] == "experimental_pinned_runtime_adapter"


def test_changed_helper_cannot_attest_a_successful_operation(tmp_path, monkeypatch):
    def runner(command, **kwargs):
        Path(command[0]).write_bytes(b"replaced during operation")
        return completed(command, **kwargs)
    client = bridge(tmp_path, monkeypatch, runner)
    with pytest.raises(ProjectError, match="helper changed"):
        client.probe()


@pytest.mark.parametrize("error,uncertain", [("AggregateException", True), ("OperationCanceledException", False)])
def test_transport_failure_is_conservative_except_proven_uac_cancellation(tmp_path, monkeypatch, error, uncertain):
    def runner(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, json.dumps({"success": False, "error_type": error}), "")
    client = bridge(tmp_path, monkeypatch, runner)
    assert client.probe()["outcome_unknown"] is uncertain


def test_concurrent_operation_is_refused_without_second_launch(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    def runner(command, **kwargs):
        calls.append(command)
        entered.set()
        assert release.wait(5)
        return completed(command, **kwargs)
    client = bridge(tmp_path, monkeypatch, runner)
    worker = threading.Thread(target=client.probe)
    worker.start()
    try:
        assert entered.wait(5)
        with pytest.raises(ProjectError, match="Another IDE operation"):
            client.probe()
        assert len(calls) == 1
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()


def test_failed_publication_never_leaves_partial_final_file(tmp_path, project_file, monkeypatch):
    from sysmac_mcp import ide
    client = bridge(tmp_path, monkeypatch, lambda *a, **k: pytest.fail("runtime export must use the guarded adapter"))
    storage = tmp_path / "sysmac-storage"
    storage.mkdir()
    process_start = "2024-01-02T03:04:05.0000000Z"
    solution_id = "55c5f0a5-4330-4d36-aad4-f5b5926882d2"
    def runtime_export(root, request, *, job, defer_receipt):
        assert defer_receipt is True
        client._request(job, {**request, "output": str(job / "managed-response.json"),
                              "timeout_ms": client.timeout * 1000})
        Path(request["archive_output"]).write_bytes(project_file.read_bytes())
        return {
            "success": True, "status": "export_succeeded", "schema_version": 1,
            "process_id": request["process_id"], "process_started_utc": request["process_started_utc"],
            "solution_id": request["solution_id"], "active_solution_id": request["solution_id"],
            "storage_folder": request["storage_folder"], "archive_output": request["archive_output"],
            "operation_started": True, "save_requested": True, "save_command_returned": True,
            "save_started": True, "save_completed": True, "export_preflight_passed": True,
            "export_started": True, "export_returned": True, "outcome_unknown": False,
            "cleanup_verified": True, "io_events": [], "transport_cleanup_verified": True,
            "runtime_work_settled": True,
        }
    monkeypatch.setattr(client, "_runtime_job", runtime_export)
    def interrupted(source, output):
        output.write(b"partial")
        raise OSError("Synthetic interrupted copy")
    monkeypatch.setattr(ide.shutil, "copyfileobj", interrupted)
    target = tmp_path / "final.smc2"
    with pytest.raises((ProjectError, OSError)):
        client.export_active(tmp_path / "state", target, process_id=9123,
                             process_started_utc=process_start, solution_id=solution_id,
                             storage_folder=str(storage))
    assert not target.exists()
    receipts = list((tmp_path / "state/ide-jobs").glob("*/receipt.json"))
    assert len(receipts) == 1 and json.loads(receipts[0].read_text())["success"] is False


def test_open_timeout_keeps_identifiable_request_and_failure_receipt(tmp_path, project_file, monkeypatch):
    workspace = Workspace(tmp_path / "state")
    pid = workspace.open_project(project_file)["project_id"]
    def runner(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 1)
    client = bridge(tmp_path, monkeypatch, runner)
    with pytest.raises(ProjectError, match="receipt"):
        client.open_copy(workspace, pid, expected_revision=0)
    receipts = list((workspace.root / "ide-jobs").glob("*/receipt.json"))
    assert len(receipts) == 1
    record = json.loads(receipts[0].read_text())
    assert record["outcome_unknown"] is True and record["project_id"] == pid
    request = json.loads((receipts[0].parent / "request.json").read_text())
    assert Path(request["copy"]).is_file()
