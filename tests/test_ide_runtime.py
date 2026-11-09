import json
import subprocess
from pathlib import Path

import pytest
from mcp import Client

from sysmac_mcp.ide import IdeClient
from sysmac_mcp.server import create_server
from sysmac_mcp.workspace import ProjectError

SOLUTION = "11111111-1111-1111-1111-111111111111"
CONTROLLER = "22222222-2222-2222-2222-222222222222"
STARTED = "2024-01-02T03:04:05.0000000Z"


def configured(tmp_path, monkeypatch, runner):
    from sysmac_mcp import ide
    monkeypatch.setattr(ide, "platform_supported", lambda: True)
    helper = tmp_path / "helper.exe"
    helper.write_bytes(b"owned fixture")
    profile = tmp_path / "profile.json"
    profile.write_text("{}")
    return IdeClient(helper, runtime_profile=profile, runner=runner)


def payload(request):
    result = dict(success=True, schema_version=1, process_id=request["process_id"],
                  process_started_utc=STARTED, solution_id=SOLUTION, controller_id=CONTROLLER,
                  operation_started=False, status="context_observed", applicable_targets=3, can_rebuild=True,
                  transport_cleanup_verified=True, runtime_work_settled=True,
                  file_mode=False)
    if request["operation"] == "rebuild":
        result.update(status="build_succeeded", operation_started=True, completed=True,
                      native_success=True, targets_after=3, errors=0, warnings=1,
                      cleanup_verified=True, outcome_unknown=False, diagnostics=[])
    return result


def test_runtime_status_and_build_have_receipts_and_bound_identities(tmp_path, monkeypatch):
    calls = []
    def runner(command, **kwargs):
        assert kwargs["shell"] is False
        request = json.loads(Path(command[command.index("--path") + 1]).read_text())
        calls.append(request)
        return subprocess.CompletedProcess(command, 0, json.dumps(payload(request)), "")
    client = configured(tmp_path, monkeypatch, runner)
    state = client.runtime_status(tmp_path / "state", process_id=123)
    result = client.rebuild_active(tmp_path / "state", process_id=123, process_started_utc=STARTED,
                                   solution_id=SOLUTION, controller_id=CONTROLLER)
    assert state["success"] and result["success"]
    assert state["can_rebuild"] is True and state["file_mode"] is False
    assert "can_save_solution" not in state and "can_export_solution" not in state
    assert result["sysmac_build"] == "succeeded"
    assert len(calls) == 2 and calls[1]["solution_id"] == SOLUTION
    assert json.loads(Path(result["receipt"]).read_text())["errors"] == 0


@pytest.mark.parametrize("field,bad", [("completed", False), ("native_success", False),
    ("errors", 1), ("cleanup_verified", False), ("applicable_targets", 0),
    ("targets_after", 0), ("outcome_unknown", True), ("process_id", 999),
    ("solution_id", CONTROLLER), ("solution_id", None), ("controller_id", 1),
    ("transport_cleanup_verified", False), ("runtime_work_settled", False), ("process_started_utc", "wrong")])
def test_false_runtime_build_attestations_rejected(tmp_path, monkeypatch, field, bad):
    def runner(command, **kwargs):
        request = json.loads(Path(command[command.index("--path") + 1]).read_text())
        result = payload(request)
        result[field] = bad
        return subprocess.CompletedProcess(command, 0, json.dumps(result), "")
    client = configured(tmp_path, monkeypatch, runner)
    with pytest.raises(ProjectError, match="attest|identity|completion"):
        client.rebuild_active(tmp_path / "state", process_id=123, process_started_utc=STARTED,
                              solution_id=SOLUTION, controller_id=CONTROLLER)
    receipts = list((tmp_path / "state/ide-jobs").glob("*/receipt.json"))
    assert len(receipts) == 1 and json.loads(receipts[0].read_text())["success"] is False


def test_runtime_timeout_preserves_request_and_never_retries(tmp_path, monkeypatch):
    calls = []
    def runner(command, **kwargs):
        calls.append(command)
        raise subprocess.TimeoutExpired(command, 1)
    client = configured(tmp_path, monkeypatch, runner)
    with pytest.raises(ProjectError, match="Job"):
        client.rebuild_active(tmp_path / "state", process_id=123, process_started_utc=STARTED,
                              solution_id=SOLUTION, controller_id=CONTROLLER)
    assert len(calls) == 1
    receipt = next((tmp_path / "state/ide-jobs").glob("*/receipt.json"))
    assert json.loads(receipt.read_text())["outcome_unknown"] is True


def test_export_transport_failure_receipt_includes_managed_progress(tmp_path, monkeypatch):
    stages = ["entry_invoked", "save_command_returned", "native_export_started"]

    def runner(command, **kwargs):
        request = json.loads(Path(command[command.index("--path") + 1]).read_text())
        if request.get("progress_output"):
            progress_path = Path(request["progress_output"])
            progress_path.write_text(
                "".join(
                    json.dumps({"schema_version": 1, "sequence": index, "stage": stage}) + "\n"
                    for index, stage in enumerate(stages, start=1)
                ),
                encoding="utf-8",
            )
        result = {
            "success": False,
            "status": "runtime_transport_error",
            "failure_stage": "ExecuteInDefaultAppDomain",
            "error": "system error",
            "operation_started": True,
            "outcome_unknown": True,
        }
        return subprocess.CompletedProcess(command, 2, json.dumps(result), "")

    client = configured(tmp_path, monkeypatch, runner)
    storage = tmp_path / "sysmac-storage"
    storage.mkdir()
    target = tmp_path / "export.smc2"

    result = client.export_active(
        tmp_path / "state",
        target,
        process_id=123,
        process_started_utc=STARTED,
        solution_id=SOLUTION,
        storage_folder=str(storage),
    )

    receipt = json.loads(Path(result["receipt"]).read_text())
    assert receipt["managed_progress"] == stages
    assert receipt["outcome_unknown"] is True
    assert not target.exists()


def test_status_transport_failure_receipt_includes_managed_progress(tmp_path, monkeypatch):
    stages = ["entry_invoked", "response_reserved", "runtime_lease_acquired",
              "status_runtime_creation_started", "status_runtime_created", "status_context_started"]

    def runner(command, **kwargs):
        request = json.loads(Path(command[command.index("--path") + 1]).read_text())
        progress_path = Path(request["progress_output"])
        progress_path.write_text(
            "".join(json.dumps({"schema_version": 1, "sequence": index, "stage": stage}) + "\n"
                    for index, stage in enumerate(stages, start=1)),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 2, json.dumps({
            "success": False, "status": "runtime_transport_error",
            "failure_stage": "ExecuteInDefaultAppDomain", "error": "system error",
            "operation_started": True, "outcome_unknown": True,
        }), "")

    client = configured(tmp_path, monkeypatch, runner)
    result = client.runtime_status(tmp_path / "state", process_id=123)
    receipt = json.loads(Path(result["receipt"]).read_text())
    assert result["managed_progress"] == stages
    assert receipt["managed_progress_status"] == "valid"
    assert receipt["outcome_unknown"] is True


def test_export_transport_exception_receipt_includes_managed_progress(tmp_path, monkeypatch):
    stages = ["entry_invoked", "save_command_returned"]

    def runner(command, **kwargs):
        request = json.loads(Path(command[command.index("--path") + 1]).read_text())
        progress_path = Path(request["progress_output"])
        progress_path.write_text(
            "".join(
                json.dumps({"schema_version": 1, "sequence": index, "stage": stage}) + "\n"
                for index, stage in enumerate(stages, start=1)
            ),
            encoding="utf-8",
        )
        raise subprocess.TimeoutExpired(command, 1)

    client = configured(tmp_path, monkeypatch, runner)
    storage = tmp_path / "sysmac-storage"
    storage.mkdir()
    target = tmp_path / "export.smc2"

    with pytest.raises(ProjectError, match="unknown"):
        client.export_active(
            tmp_path / "state",
            target,
            process_id=123,
            process_started_utc=STARTED,
            solution_id=SOLUTION,
            storage_folder=str(storage),
        )

    receipt = next((tmp_path / "state/ide-jobs").glob("*/receipt.json"))
    record = json.loads(receipt.read_text())
    assert record["managed_progress"] == stages
    assert record["managed_progress_status"] == "valid"
    assert record["outcome_unknown"] is True


def test_managed_progress_rejects_unrecognized_text_without_returning_it(tmp_path):
    from sysmac_mcp.ide import IdeClient

    job = tmp_path / "job"
    job.mkdir()
    secret_like_text = "customer-project-name-must-not-escape"
    (job / "managed-progress.jsonl").write_text(
        json.dumps({"schema_version": 1, "sequence": 1, "stage": secret_like_text}),
        encoding="utf-8",
    )

    stages, status = IdeClient._managed_progress(job)

    assert stages == [] and status == "invalid"
    assert secret_like_text not in json.dumps(stages)


def test_invalid_build_context_rejected_before_launch(tmp_path, monkeypatch):
    calls = []
    client = configured(tmp_path, monkeypatch, lambda *a, **k: calls.append(a))
    with pytest.raises(ProjectError):
        client.rebuild_active(tmp_path / "state", process_id=123, process_started_utc=STARTED,
                              solution_id="invalid", controller_id=CONTROLLER)
    assert not calls


async def test_runtime_tools_discoverable(tmp_path):
    async with Client(create_server(tmp_path / "state")) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert {"plc_ide_runtime_status", "plc_ide_rebuild_active", "plc_ide_close_active"} <= names


async def test_ide_observation_tools_advertise_read_only_idempotent_annotations(tmp_path):
    async with Client(create_server(tmp_path / "state")) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        for name in ("plc_ide_probe", "plc_ide_runtime_status", "plc_ide_list_projects"):
            annotations = tools[name].annotations
            assert annotations is not None
            assert annotations.read_only_hint is True
            assert annotations.destructive_hint is False
            assert annotations.idempotent_hint is True
            assert annotations.open_world_hint is False


def solution_payload(request):
    result = payload(request)
    if request["operation"] == "list-projects":
        result.update(status="projects_observed", items=[{"solution_id": SOLUTION, "name": "owned fixture",
                                                        "can_view": True}], total=1, next_offset=None)
    if request["operation"] == "open-existing":
        result.update(status="open_succeeded", solution_id=request["solution_id"],
                      active_solution_id=request["solution_id"], operation_started=True, open_requested=True,
                      open_accepted=True, opened_event=True, cleanup_verified=True, outcome_unknown=False)
    if request["operation"] == "close-active":
        result.update(status="close_succeeded", solution_id=request["solution_id"], active_solution_id=None,
                      operation_started=True, editor_commit_completed=True, modified_after_editor_commit=False,
                      close_command_available=True, close_requested=True, close_returned=True,
                      closing_event=True, closed_event=True, cancelled_event=False,
                      cleanup_verified=True, outcome_unknown=False)
    return result


def test_existing_project_inventory_and_open_use_typed_receipted_jobs(tmp_path, monkeypatch):
    calls = []
    def runner(command, **kwargs):
        request = json.loads(Path(command[command.index("--path") + 1]).read_text())
        calls.append(request)
        return subprocess.CompletedProcess(command, 0, json.dumps(solution_payload(request)), "")
    client = configured(tmp_path, monkeypatch, runner)
    projects = client.list_projects(tmp_path / "state", process_id=123)
    opened = client.open_existing(tmp_path / "state", process_id=123, process_started_utc=STARTED, solution_id=SOLUTION)
    assert projects["success"] and projects["items"][0]["solution_id"] == SOLUTION
    assert opened["success"] and opened["sysmac_build"] == "not_executed"
    assert [r["operation"] for r in calls] == ["list-projects", "open-existing"]
    assert json.loads(Path(opened["receipt"]).read_text())["active_solution_id"] == SOLUTION


@pytest.mark.parametrize("field,bad", [
    ("open_requested", False), ("open_accepted", False), ("opened_event", False),
    ("cleanup_verified", False), ("runtime_work_settled", False),
    ("active_solution_id", CONTROLLER), ("solution_id", None), ("process_started_utc", "wrong"),
])
def test_existing_project_open_rejects_inconsistent_attestations(tmp_path, monkeypatch, field, bad):
    def runner(command, **kwargs):
        request = json.loads(Path(command[command.index("--path") + 1]).read_text())
        result = solution_payload(request)
        result[field] = bad
        return subprocess.CompletedProcess(command, 0, json.dumps(result), "")
    client = configured(tmp_path, monkeypatch, runner)
    with pytest.raises(ProjectError, match="attest|identity|completion"):
        client.open_existing(tmp_path / "state", process_id=123, process_started_utc=STARTED, solution_id=SOLUTION)
    receipt = next((tmp_path / "state/ide-jobs").glob("*/receipt.json"))
    assert json.loads(receipt.read_text())["success"] is False


def test_project_inventory_cannot_silently_omit_its_declared_page(tmp_path, monkeypatch):
    def runner(command, **kwargs):
        request = json.loads(Path(command[command.index("--path") + 1]).read_text())
        result = solution_payload(request)
        result.update(items=[], total=2)
        return subprocess.CompletedProcess(command, 0, json.dumps(result), "")
    client = configured(tmp_path, monkeypatch, runner)
    with pytest.raises(ProjectError, match="attest|identity|completion"):
        client.list_projects(tmp_path / "state", process_id=123)


async def test_existing_project_tools_discoverable(tmp_path):
    async with Client(create_server(tmp_path / "state")) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert {"plc_ide_list_projects", "plc_ide_open_existing"} <= names


def test_close_active_uses_process_and_solution_identities_with_receipt(tmp_path, monkeypatch):
    calls = []
    def runner(command, **kwargs):
        request = json.loads(Path(command[command.index("--path") + 1]).read_text())
        calls.append(request)
        return subprocess.CompletedProcess(command, 0, json.dumps(solution_payload(request)), "")
    client = configured(tmp_path, monkeypatch, runner)
    result = client.close_active(tmp_path / "state", process_id=123,
                                 process_started_utc=STARTED, solution_id=SOLUTION)
    assert result["success"] and result["status"] == "close_succeeded"
    assert calls == [{"operation": "close-active", "process_id": 123,
                      "process_started_utc": STARTED, "solution_id": SOLUTION,
                      "output": calls[0]["output"], "timeout_ms": 180000}]
    assert json.loads(Path(result["receipt"]).read_text())["closed_event"] is True


@pytest.mark.parametrize("field,bad", [
    ("operation_started", False), ("editor_commit_completed", False),
    ("modified_after_editor_commit", True), ("close_command_available", False),
    ("close_requested", False), ("close_returned", False), ("closing_event", False),
    ("closed_event", False), ("cancelled_event", True), ("cleanup_verified", False),
    ("runtime_work_settled", False), ("outcome_unknown", True),
    ("solution_id", CONTROLLER), ("active_solution_id", SOLUTION),
    ("process_started_utc", "wrong"),
])
def test_close_active_rejects_inconsistent_attestations(tmp_path, monkeypatch, field, bad):
    def runner(command, **kwargs):
        request = json.loads(Path(command[command.index("--path") + 1]).read_text())
        result = solution_payload(request)
        result[field] = bad
        return subprocess.CompletedProcess(command, 0, json.dumps(result), "")
    client = configured(tmp_path, monkeypatch, runner)
    with pytest.raises(ProjectError, match="attest|identity|completion"):
        client.close_active(tmp_path / "state", process_id=123,
                            process_started_utc=STARTED, solution_id=SOLUTION)
    receipt = next((tmp_path / "state/ide-jobs").glob("*/receipt.json"))
    assert json.loads(receipt.read_text())["success"] is False
