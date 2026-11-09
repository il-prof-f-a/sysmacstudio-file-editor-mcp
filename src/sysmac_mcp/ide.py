"""Optional offline IDE adapter. Each operation has a fresh user-approved helper."""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import threading
import uuid
from pathlib import Path

from .project import NativeProject
from .workspace import ProjectError, Workspace, sha256

_MANAGED_PROGRESS_STAGES = frozenset({
    "entry_invoked",
    "response_reserved",
    "runtime_lease_acquired",
    "export_runtime_created",
    "export_request_parameters_loaded",
    "export_coordinator_entered",
    "export_arguments_validated",
    "export_storage_path_validated",
    "export_destination_path_validated",
    "status_runtime_creation_started",
    "status_runtime_created",
    "status_context_started",
    "status_context_observed",
    "status_result_prepared",
    "export_request_validated",
    "export_subscribed",
    "save_command_available",
    "save_command_unavailable",
    "save_command_started",
    "save_command_returned",
    "save_completed",
    "export_command_available",
    "export_command_unavailable",
    "export_rule_rejected",
    "export_preflight_passed",
    "native_export_started",
    "native_export_returned",
    "export_verified",
    "subscription_cleanup_started",
    "subscription_cleanup_completed",
    "subscription_cleanup_failed",
    "managed_exception_caught",
    "response_serialization_started",
    "response_written",
})


def platform_supported() -> bool:
    return os.name == "nt"


def _is_reparse_point(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


class IdeClient:
    def __init__(self, helper=None, *, host_id=None, timeout_seconds=180, runner=None, runtime_profile=None):
        configured = helper or os.environ.get("SYSMAC_IDE_CLIENT")
        local = Path(__file__).resolve().parents[2] / "bridge/SysmacWcf/bin/SysmacWcf.exe"
        self.helper = Path(configured).resolve() if configured else local
        runtime = runtime_profile or os.environ.get("SYSMAC_IDE_RUNTIME_PROFILE")
        self.runtime_profile = Path(runtime).resolve() if runtime else None
        self.host_id = host_id or os.environ.get("SYSMAC_IDE_HOST_ID", "sysmac-mcp-offline")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", self.host_id):
            raise ValueError("Invalid local Sysmac IDE host ID.")
        if not 1 <= timeout_seconds <= 600:
            raise ValueError("IDE timeout must be between 1 and 600 seconds.")
        self.timeout = timeout_seconds
        self.runner = runner or subprocess.run
        self._lock = threading.Lock()

    def capabilities(self):
        return {
            "configured": platform_supported() and self.helper.is_file(),
            "helper": str(self.helper),
            "host_id": self.host_id,
            "elevation": "one_shot_user_uac",
            "operations": ["probe", "open_copy", "export_active", "runtime_status", "rebuild_active",
                           "list_projects", "open_existing", "close_active"],
            "build": "experimental_pinned_runtime_adapter",
            "real_runtime_build_verified": True,
            "runtime_export": "experimental_guarded_native_export",
            "real_runtime_export_verified": False,
            "runtime_close": "experimental_attested_clean_solution_close",
            "real_runtime_close_verified": False,
            "runtime_adapter_configured": bool(platform_supported() and self.helper.is_file()
                                               and self.runtime_profile and self.runtime_profile.is_file()),
            "online_operations": False,
            "requires_running_wcf_host": True,
            "runtime_network_required": False,
        }

    def _execute(self, operation, path=None):
        is_runtime = operation in {"runtime-status", "runtime-rebuild", "runtime-list-projects", "runtime-open-existing",
                                   "runtime-close-active", "runtime-export"}
        if operation == "export":
            raise ProjectError("Legacy WCF export is disabled; use the identity-guarded runtime export tool.",
                               "ide_legacy_export_disabled")
        if not is_runtime and operation not in {"probe", "open"}:
            raise ProjectError("Unsupported IDE operation.", "ide_invalid_operation")
        if not self.capabilities()["configured"]:
            raise ProjectError(
                "Windows Sysmac IDE helper unavailable. Build SysmacWcf and configure "
                "SYSMAC_IDE_CLIENT; start the documented local WCF host.", "ide_unavailable"
            )
        if is_runtime and not self.capabilities()["runtime_adapter_configured"]:
            raise ProjectError("Configure SYSMAC_IDE_RUNTIME_PROFILE before runtime operations.", "ide_unavailable")
        if not self._lock.acquire(blocking=False):
            raise ProjectError("Another IDE operation is running.", "ide_busy")
        try:
            helper_digest = sha256(self.helper)
            profile_digest = sha256(self.runtime_profile) if is_runtime else None
            if operation == "probe":
                command = [
                    str(self.helper), "probe", "--host-id", self.host_id,
                    "--timeout-seconds", str(self.timeout),
                ]
            else:
                command = [
                    str(self.helper), "elevated", "--operation", operation,
                    "--host-id", self.host_id, "--timeout-seconds", str(self.timeout),
                ]
            if path is not None:
                command.extend(["--path", str(path)])
            if is_runtime:
                command.extend(["--runtime-profile", str(self.runtime_profile),
                                "--request-sha256", sha256(path), "--runtime-profile-sha256", profile_digest])
            try:
                run = self.runner(
                    command, shell=False, capture_output=True, text=True, encoding="utf-8",
                    timeout=self.timeout + 60,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            except subprocess.TimeoutExpired as exc:
                raise ProjectError(
                    "IDE operation outcome is unknown after timeout; do not retry automatically. "
                    "Inspect Sysmac before issuing another operation.", "ide_outcome_unknown"
                ) from exc
            if sha256(self.helper) != helper_digest:
                raise ProjectError(
                    "IDE helper changed during execution; operation outcome is unknown.", "ide_helper_changed"
                )
            if is_runtime and sha256(self.runtime_profile) != profile_digest:
                raise ProjectError("IDE runtime profile changed; outcome is unknown.", "ide_helper_changed")
            if len(run.stdout.encode("utf-8")) > 1024 * 1024:
                raise ProjectError("IDE helper response exceeds the limit.", "ide_protocol")
            try:
                result = json.loads(run.stdout)
            except (ValueError, TypeError) as exc:
                raise ProjectError("IDE helper returned invalid JSON.", "ide_protocol") from exc
            if not isinstance(result, dict) or type(result.get("success")) is not bool:
                raise ProjectError("IDE helper success flag is invalid.", "ide_protocol")
            if result["success"]:
                expected = {"open": "OpenFromFile"}.get(operation)
                if run.returncode != 0:
                    raise ProjectError("IDE helper exit status contradicts its result.", "ide_protocol")
                if expected and (
                    result.get("callback_received") is not True
                    or result.get("execution_name") != expected or result.get("result") != "Success"
                ):
                    raise ProjectError("IDE success has no matching completion callback.", "ide_protocol")
                if operation == "probe" and result.get("status") != "connected":
                    raise ProjectError("IDE probe did not attest connection.", "ide_protocol")
            return {
                **result, "operation": operation, "host_id": self.host_id,
                "helper_sha256": helper_digest,
                "sysmac_build": "unknown" if operation == "runtime-rebuild" else "not_executed",
                "outcome_unknown": bool(result.get("outcome_unknown")) or result.get("status") == "timeout"
                or bool(result.get("error_type") and result["error_type"] != "OperationCanceledException"),
            }
        finally:
            self._lock.release()

    def probe(self):
        return self._execute("probe")

    @staticmethod
    def _job(root):
        path = Path(root) / "ide-jobs" / uuid.uuid4().hex
        path.mkdir(parents=True)
        return path

    @staticmethod
    def _receipt(job, result):
        with (job / "receipt.json").open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)

    def _failure(self, job, context, error):
        code = getattr(error, "code", "ide_job_failed")
        uncertain = context.get("phase") == "calling_ide" and code not in {
            "ide_unavailable", "ide_busy", "stale_revision", "project_not_found",
        }
        failure = {**context, "success": False, "error_type": type(error).__name__,
                   "error": str(error), "outcome_unknown": uncertain,
                   "sysmac_build": "unknown" if uncertain and context.get("operation") == "rebuild" else "not_executed"}
        try:
            self._receipt(job, failure)
        except OSError:
            pass  # The error still returns the job identity if storage is full.
        return ProjectError(
            f"{error} Job: {job}; inspect request.json and receipt.json (if persisted).", code
        )

    @staticmethod
    def _request(job, context):
        with (job / "request.json").open("x", encoding="utf-8") as stream:
            json.dump(context, stream, ensure_ascii=False, indent=2)

    @staticmethod
    def _managed_progress(job):
        """Read only bounded, closed-vocabulary progress emitted by the managed runtime."""
        path = job / "managed-progress.jsonl"
        try:
            info = path.lstat()
        except FileNotFoundError:
            return [], "missing"
        except OSError:
            return [], "invalid"
        if _is_reparse_point(path) or not stat.S_ISREG(info.st_mode):
            return [], "invalid"
        try:
            if info.st_size > 16384:
                return [], "invalid"
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            return [], "invalid"
        if len(lines) > 128:
            return [], "invalid"
        stages = []
        for sequence, line in enumerate(lines, start=1):
            try:
                item = json.loads(line)
            except (ValueError, TypeError):
                return [], "invalid"
            if (not isinstance(item, dict) or set(item) != {"schema_version", "sequence", "stage"}
                    or type(item["schema_version"]) is not int or item["schema_version"] != 1
                    or type(item["sequence"]) is not int or item["sequence"] != sequence
                    or not isinstance(item["stage"], str) or item["stage"] not in _MANAGED_PROGRESS_STAGES):
                return [], "invalid"
            stages.append(item["stage"])
        return stages, "valid" if lines else "empty"

    def runtime_status(self, workspace_root, *, process_id: int):
        return self._runtime_job(workspace_root, {"operation": "status", "process_id": process_id})

    def list_projects(self, workspace_root, *, process_id: int, offset=0, limit=100):
        if type(offset) is not int or not 0 <= offset <= 2147483647 or type(limit) is not int or not 1 <= limit <= 200:
            raise ProjectError("Project inventory requires offset >= 0 and limit between 1 and 200.")
        return self._runtime_job(workspace_root, {"operation": "list-projects", "process_id": process_id,
                                                 "offset": offset, "limit": limit})

    @staticmethod
    def _guid(value):
        try:
            parsed = uuid.UUID(value)
            if parsed.int == 0:
                raise ValueError("Empty GUID")
            return str(parsed)
        except (ValueError, TypeError, AttributeError) as error:
            raise ProjectError("A nonempty solution/controller GUID from the IDE inventory is required.") from error

    @staticmethod
    def _start_identity(value):
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{7}Z", value):
            raise ProjectError("Use the process start identity returned by the IDE runtime.")
        return value

    def open_existing(self, workspace_root, *, process_id: int, process_started_utc: str, solution_id: str):
        return self._runtime_job(workspace_root, {
            "operation": "open-existing", "process_id": process_id,
            "process_started_utc": self._start_identity(process_started_utc), "solution_id": self._guid(solution_id),
        })

    def close_active(self, workspace_root, *, process_id: int, process_started_utc: str, solution_id: str):
        return self._runtime_job(workspace_root, {
            "operation": "close-active", "process_id": process_id,
            "process_started_utc": self._start_identity(process_started_utc), "solution_id": self._guid(solution_id),
        })

    def rebuild_active(self, workspace_root, *, process_id: int, process_started_utc: str,
                       solution_id: str, controller_id: str):
        return self._runtime_job(workspace_root, {
            "operation": "rebuild", "process_id": process_id, "process_started_utc": self._start_identity(process_started_utc),
            "solution_id": self._guid(solution_id), "controller_id": self._guid(controller_id),
        })

    @staticmethod
    def _inventory_attested(result, request):
        items, total = result.get("items"), result.get("total")
        if not isinstance(items, list) or type(total) is not int or not 0 <= total <= 5000:
            return False
        offset, limit = request["offset"], request["limit"]
        if len(items) != min(limit, max(total - offset, 0)):
            return False
        expected_next = offset + len(items) if offset + len(items) < total else None
        actual_next = result.get("next_offset")
        if expected_next != actual_next or (actual_next is not None and type(actual_next) is not int):
            return False
        identities = []
        for item in items:
            if not isinstance(item, dict) or type(item.get("can_view")) is not bool:
                return False
            try:
                identities.append(IdeClient._guid(item.get("solution_id")))
            except ProjectError:
                return False
        return len(set(identities)) == len(identities)

    def _runtime_job(self, workspace_root, request, *, job=None, defer_receipt=False):
        if type(request["process_id"]) is not int or not 1 <= request["process_id"] <= 2147483647:
            raise ProjectError("A positive Sysmac process ID is required.")
        job = job or self._job(workspace_root)
        request = {**request, "output": str(job / "managed-response.json"), "timeout_ms": self.timeout * 1000}
        if request["operation"] in {"status", "export"}:
            request["progress_output"] = str(job / "managed-progress.jsonl")
        self._request(job, request)
        context = {**request, "phase": "calling_ide"}
        try:
            digest = sha256(job / "request.json")
            result = self._execute("runtime-" + request["operation"], job / "request.json")
            progress, progress_status = self._managed_progress(job)
            result["managed_progress"] = progress
            result["managed_progress_status"] = progress_status
            if sha256(job / "request.json") != digest:
                raise ProjectError("Runtime request changed; identity cannot be attested.", "ide_protocol")
            if result["success"]:
                valid = (type(result.get("schema_version")) is int and result["schema_version"] == 1
                         and type(result.get("process_id")) is int and result["process_id"] == request["process_id"]
                         and isinstance(result.get("process_started_utc"), str)
                         and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{7}Z", result["process_started_utc"])
                         and result.get("transport_cleanup_verified") is True
                         and result.get("runtime_work_settled") is True)
                if request["operation"] == "status":
                    valid = valid and result.get("status") == "context_observed" and result.get("operation_started") is False
                elif request["operation"] == "list-projects":
                    valid = (valid and result.get("status") == "projects_observed"
                             and result.get("operation_started") is False and self._inventory_attested(result, request))
                elif request["operation"] == "open-existing":
                    valid = (valid and result.get("status") == "open_succeeded"
                             and result.get("process_started_utc") == request["process_started_utc"]
                             and all(result.get(field) is True for field in
                                     ("operation_started", "open_requested", "open_accepted", "opened_event", "cleanup_verified"))
                             and result.get("outcome_unknown") is False
                             and all(isinstance(result.get(field), str) and result[field].lower() == request["solution_id"]
                                     for field in ("solution_id", "active_solution_id")))
                elif request["operation"] == "close-active":
                    valid = (valid and result.get("status") == "close_succeeded"
                             and result.get("process_started_utc") == request["process_started_utc"]
                             and all(result.get(field) is True for field in
                                     ("operation_started", "editor_commit_completed", "close_command_available",
                                      "close_requested", "close_returned", "closing_event", "closed_event",
                                      "cleanup_verified"))
                             and result.get("modified_after_editor_commit") is False
                             and result.get("cancelled_event") is False
                             and result.get("outcome_unknown") is False
                             and result.get("solution_id") == request["solution_id"]
                             and result.get("active_solution_id") is None)
                elif request["operation"] == "rebuild":
                    valid = (valid and result.get("status") == "build_succeeded"
                             and result.get("process_started_utc") == request["process_started_utc"]
                             and all(result.get(field) is True for field in
                                     ("operation_started", "completed", "native_success", "cleanup_verified"))
                             and result.get("outcome_unknown") is False
                             and type(result.get("errors")) is int and result["errors"] == 0
                             and all(type(result.get(field)) is int and result[field] > 0
                                     for field in ("applicable_targets", "targets_after"))
                             and all(isinstance(result.get(field), str)
                                     and result[field].lower() == request[field].lower()
                                     for field in ("solution_id", "controller_id")))
                elif request["operation"] == "export":
                    valid = (valid and result.get("status") == "export_succeeded"
                             and result.get("process_started_utc") == request["process_started_utc"]
                             and all(result.get(field) is True for field in
                                     ("operation_started", "save_requested", "save_command_returned", "save_started",
                                      "save_completed", "export_preflight_passed", "export_started", "export_returned",
                                      "cleanup_verified"))
                             and result.get("outcome_unknown") is False
                             and result.get("io_events") == []
                             and all(isinstance(result.get(field), str)
                                     and result[field].lower() == request["solution_id"]
                                     for field in ("solution_id", "active_solution_id"))
                             and self._same_path(result.get("storage_folder"), request["storage_folder"])
                             and self._same_path(result.get("archive_output"), request["archive_output"]))
                if not valid:
                    raise ProjectError("Runtime completion or identity attestation is inconsistent.", "ide_protocol")
                if request["operation"] == "rebuild":
                    result["sysmac_build"] = "succeeded"
            elif request["operation"] == "rebuild" and result.get("status") == "build_failed":
                result["sysmac_build"] = "failed"
            result["receipt"] = str(job / "receipt.json")
            if not defer_receipt or not result["success"]:
                self._receipt(job, result)
            return result
        except (OSError, ValueError) as error:
            progress, progress_status = self._managed_progress(job)
            context["managed_progress"] = progress
            context["managed_progress_status"] = progress_status
            if defer_receipt:
                raise
            raise self._failure(job, context, error) from error

    @staticmethod
    def _same_path(actual, expected):
        if not isinstance(actual, str) or not isinstance(expected, str):
            return False
        try:
            return os.path.normcase(os.path.abspath(actual)) == os.path.normcase(os.path.abspath(expected))
        except (OSError, ValueError):
            return False

    def open_copy(self, workspace: Workspace, project_id: str, *, expected_revision: int):
        # Export is revision-addressed; later commits cannot alter this candidate.
        job = self._job(workspace.root)
        candidate = job / "candidate.smc2"
        context = {"operation": "open", "project_id": project_id, "revision": expected_revision,
                   "copy": str(candidate), "host_id": self.host_id, "phase": "preparing"}
        self._request(job, context)
        try:
            exported = workspace.export(project_id, candidate)
            if exported["revision"] != expected_revision:
                raise ProjectError("Project revision differs; read current revision first.", "stale_revision")
            context.update(sha256=exported["sha256"], phase="calling_ide")
            result = self._execute("open", candidate)
            if result.get("callback_received") is True:
                context.update(phase="callback_received", ide_status=result.get("status"),
                               ide_result=result.get("result"), ide_error=result.get("error"))
                error_text = str(result.get("error") or "").casefold()
                if (not result.get("success") and result.get("execution_name") == "OpenFromFile"
                        and result.get("result") == "RuntimeError"
                        and ("already imported" in error_text or "già importato" in error_text)):
                    raise ProjectError(
                        "Sysmac segnala che il progetto è già importato. Chiudi il progetto attivo con "
                        "File → Chiudi, poi importa la copia SMC2 con File → Importa.",
                        "ide_project_already_imported",
                    )
            if sha256(candidate) != exported["sha256"]:
                raise ProjectError("IDE input changed; inspect the preserved job.", "ide_input_changed")
            result.update(project_id=project_id, revision=expected_revision, copy=str(candidate),
                          sha256=exported["sha256"], receipt=str(job / "receipt.json"))
            self._receipt(job, result)
            return result
        except (OSError, ValueError) as error:
            raise self._failure(job, context, error) from error

    def export_active(self, workspace_root, destination, *, process_id: int, process_started_utc: str,
                      solution_id: str, storage_folder: str):
        if not platform_supported():
            raise ProjectError("Guarded Sysmac runtime export is available only on Windows.", "ide_unsupported_platform")
        raw_target = Path(destination)
        if os.path.lexists(raw_target):
            raise ProjectError("Destination already exists; choose a new file.")
        target = raw_target.resolve()
        folder = Path(storage_folder).resolve()
        for path in (target, folder):
            value = str(path)
            if not re.match(r"^[A-Za-z]:[\\/]", value) or ":" in value[2:]:
                raise ProjectError("Sysmac export accepts local drive paths without alternate data streams.", "ide_invalid_path")
        if target.suffix.lower() != ".smc2":
            raise ProjectError("IDE export requires a new SMC2 destination.", "ide_invalid_path")
        if os.path.lexists(target):
            raise ProjectError("Destination already exists; choose a new file.")
        if not target.parent.is_dir():
            raise ProjectError("Destination parent directory must exist.")
        if not folder.is_dir() or str(folder).startswith("\\\\"):
            raise ProjectError("Use the existing local storage_folder returned by plc_ide_list_projects.", "ide_invalid_path")
        try:
            common = os.path.commonpath([str(target), str(folder)])
        except ValueError as error:
            raise ProjectError("Export destination and solution storage must be on a local drive.", "ide_invalid_path") from error
        if os.path.normcase(common) == os.path.normcase(str(folder)):
            raise ProjectError("Export destination must be outside the active Sysmac persistence folder.", "ide_invalid_path")
        exported = target.with_name("." + target.name + "." + uuid.uuid4().hex + ".ide-export.smc2")
        if os.path.lexists(exported):
            raise ProjectError("Export staging destination already exists; choose a new target.", "ide_invalid_path")
        solution_id = self._guid(solution_id)
        process_started_utc = self._start_identity(process_started_utc)
        if type(process_id) is not int or not 1 <= process_id <= 2147483647:
            raise ProjectError("A positive Sysmac process ID is required.")
        job = self._job(workspace_root)
        request = {"operation": "export", "process_id": process_id,
                   "process_started_utc": process_started_utc, "solution_id": solution_id,
                   "storage_folder": str(folder), "archive_output": str(exported.resolve())}
        context = {**request, "destination": str(target), "copy": str(exported),
                   "host_id": self.host_id, "phase": "calling_ide"}
        try:
            result = self._runtime_job(workspace_root, request, job=job, defer_receipt=True)
            if not result["success"]:
                return result
            context["phase"] = "validating_export"
            if _is_reparse_point(exported) or not exported.is_file():
                raise ProjectError("Sysmac reported export success without a regular SMC2 file.", "ide_export_missing")
            digest = sha256(exported)
            NativeProject(exported)  # Readable bounded project container; not a PLC build.
            temporary = target.with_name("." + target.name + "." + uuid.uuid4().hex + ".partial")
            context.update(phase="publishing", temporary=str(temporary), sha256=digest)
            with exported.open("rb") as source, temporary.open("xb") as output:
                shutil.copyfileobj(source, output)
                output.flush()
                os.fsync(output.fileno())
            if sha256(temporary) != digest:
                raise ProjectError("Sysmac export changed while copying; inspect the retained job.", "ide_export_changed")
            if os.path.lexists(target):
                raise ProjectError("Destination appeared during export; the existing file was left untouched.", "ide_destination_race")
            if os.name == "nt":
                temporary.rename(target)
            else:
                os.link(temporary, target)
                temporary.unlink()
            if sha256(target) != digest:
                raise ProjectError("Published SMC2 hash differs from the validated export.", "ide_export_changed")
            NativeProject(target)
            result.update(
                receipt=str(job / "receipt.json"), active_project_identity="attested",
                solution_id=solution_id, storage_folder=str(folder), path=str(target),
                sha256=digest, bytes=target.stat().st_size, export_validation="readable_native_smc2",
                sysmac_build="not_executed",
            )
            self._receipt(job, result)
            return result
        except (OSError, ValueError) as error:
            context["managed_progress"], context["managed_progress_status"] = self._managed_progress(job)
            raise self._failure(job, context, error) from error
