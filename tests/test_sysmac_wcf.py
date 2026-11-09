import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VENDOR = Path(r"C:\Program Files\OMRON\Sysmac Studio")
COMPILER = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")


@pytest.fixture(scope="module")
def wcf_client(tmp_path_factory):
    if os.name != "nt" or not COMPILER.is_file() or not (VENDOR / "FrameworkModules.Core.dll").is_file():
        pytest.skip("Requires installed Sysmac contract assembly and .NET Framework compiler.")
    output = tmp_path_factory.mktemp("sysmac-wcf")
    built = subprocess.run([sys.executable, str(ROOT / "scripts/build-sysmac-wcf.py"), "--vendor-root", str(VENDOR), "--output", str(output)], capture_output=True, text=True)
    assert built.returncode == 0, built.stdout + built.stderr
    return output / "SysmacWcf.exe"


def run_client(exe, *args):
    return subprocess.run([str(exe), *args, "--vendor-root", str(VENDOR)], capture_output=True, text=True, timeout=20)


def test_local_named_pipe_mock_validates_callbacks_failures_and_timeout(wcf_client, tmp_path):
    run = run_client(wcf_client, "self-test", "--directory", str(tmp_path))
    assert run.returncode == 0, run.stdout + run.stderr
    result = json.loads(run.stdout)
    assert result["mock_transport"] is True and result["real_sysmac_invoked"] is False
    assert result["open_callback"] and result["export_callback"]
    assert result["runtime_error_reported"] and result["timeout_reported"]
    assert result["unrelated_and_null_callbacks_ignored"]


def test_export_refuses_existing_file_before_any_connection(wcf_client, tmp_path):
    original = tmp_path / "already.smc2"
    original.write_bytes(b"keep this")
    run = run_client(wcf_client, "export", "--host-id", "missing-host", "--path", str(original))
    assert run.returncode != 0
    assert "already exists" in run.stdout.lower()
    assert original.read_bytes() == b"keep this"


def test_public_legacy_wcf_export_requires_guarded_runtime_before_any_connection(wcf_client, tmp_path):
    run = run_client(wcf_client, "export", "--host-id", "owned-no-service",
                     "--path", str(tmp_path / "new-export.smc2"))
    assert run.returncode != 0
    assert "guarded runtime export" in run.stdout.lower(), run.stdout
    assert "EndpointNotFoundException" not in run.stdout


def test_open_refuses_missing_file(wcf_client, tmp_path):
    run = run_client(wcf_client, "open", "--host-id", "missing-host", "--path", str(tmp_path / "absent.smc2"))
    assert run.returncode != 0
    assert "does not exist" in run.stdout.lower()


def test_destructive_or_arbitrary_commands_are_not_exposed(wcf_client):
    run = run_client(wcf_client, "delete", "--host-id", "unused")
    assert run.returncode != 0
    assert "unsupported command" in run.stdout.lower()


def test_host_identifier_cannot_select_remote_endpoint(wcf_client):
    run = run_client(wcf_client, "probe", "--host-id", "remote/../../other")
    assert run.returncode != 0
    assert "host-id" in run.stdout.lower()


def test_unavailable_host_keeps_inner_connection_diagnostics(wcf_client):
    run = run_client(wcf_client, "probe", "--host-id", "sysmac-mcp-no-service-" + os.urandom(8).hex(), "--timeout-seconds", "1")
    assert run.returncode != 0
    result = json.loads(run.stdout)
    assert result["success"] is False
    assert result["details"][0]["error_type"] == result["error_type"]
    assert len(result["details"]) >= 2


def test_one_shot_response_pipe_authenticates_peer_and_bounds_messages(wcf_client):
    run = run_client(wcf_client, "transport-self-test")
    assert run.returncode == 0, run.stdout + run.stderr
    result = json.loads(run.stdout)
    assert result["no_elevation_requested"] is True
    assert result["successful_child"] is True
    assert result["wrong_token_rejected"] is True
    assert result["wrong_process_rejected"] is True
    assert result["oversized_message_rejected"] is True
    assert result["child_without_go_rejected"] is True
    assert result["relative_paths_canonicalized"] is True


def test_elevation_request_validates_input_before_uac(wcf_client, tmp_path):
    run = run_client(wcf_client, "elevated", "--operation", "open", "--host-id", "unused", "--path", str(tmp_path / "missing.smc2"))
    assert run.returncode != 0
    assert "does not exist" in run.stdout.lower()


def test_runtime_request_validates_profile_before_uac(wcf_client, tmp_path):
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"operation": "status", "process_id": 123,
                                  "output": str(tmp_path / "response.json"), "timeout_ms": 10000}))
    run = run_client(wcf_client, "elevated", "--operation", "runtime-status", "--host-id", "unused",
                     "--path", str(request), "--runtime-profile", str(tmp_path / "missing.json"))
    assert run.returncode != 0
    assert "runtime profile" in run.stdout.lower() and "does not exist" in run.stdout.lower()


def test_configured_helper_accepts_runtime_close_before_uac(tmp_path):
    configured = ROOT / "bridge/SysmacWcf/bin/SysmacWcf.exe"
    if os.name != "nt" or not configured.is_file():
        pytest.skip("Requires the locally configured Sysmac WCF helper.")
    request = tmp_path / "request.json"
    request.write_text(json.dumps({
        "operation": "close-active", "process_id": 123,
        "process_started_utc": "2024-01-02T03:04:05.0000000Z",
        "solution_id": "11111111-1111-1111-1111-111111111111",
        "output": str(tmp_path / "response.json"), "timeout_ms": 1000,
    }))
    run = run_client(configured, "elevated", "--operation", "runtime-close-active",
                     "--host-id", "unused", "--path", str(request),
                     "--runtime-profile", str(tmp_path / "missing-profile.json"),
                     "--timeout-seconds", "1")
    assert run.returncode != 0
    assert "runtime profile" in run.stdout.lower() and "does not exist" in run.stdout.lower()


@pytest.mark.parametrize("operation,changes,message", [
    ("list-projects", {"offset": -1}, "invalid project inventory page"),
    ("open-existing", {"solution_id": "11111111-1111-1111-1111-111111111111"}, "expected process start identity missing"),
    ("close-active", {"solution_id": "11111111-1111-1111-1111-111111111111"}, "expected process start identity missing"),
])
def test_direct_runtime_validation_rejects_bad_solution_requests_without_uac_or_attach(
    wcf_client, tmp_path, operation, changes, message
):
    component_file = tmp_path / "owned-component.txt"
    component_file.write_text("owned non-executable fixture")
    component = {"path": str(component_file), "sha256": hashlib.sha256(component_file.read_bytes()).hexdigest()}
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"schema_version": 1, **{key: component for key in
        ("node", "worker", "assembly", "template", "frida_module")}}))
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"operation": operation, "process_id": os.getpid(), "timeout_ms": 1000,
                                  "output": str(tmp_path / "response.json"), **changes}))
    # Direct command: even a missing validation can only hit the non-Sysmac PID guard, never UAC.
    run = run_client(wcf_client, "runtime-" + operation, "--path", str(request),
                     "--runtime-profile", str(profile), "--timeout-seconds", "1",
                     "--request-sha256", hashlib.sha256(request.read_bytes()).hexdigest(),
                     "--runtime-profile-sha256", hashlib.sha256(profile.read_bytes()).hexdigest())
    assert run.returncode != 0
    assert message in run.stdout.lower(), run.stdout + run.stderr


def test_direct_runtime_validation_accepts_status_progress_output_before_process_guard(wcf_client, tmp_path):
    component_file = tmp_path / "owned-component.txt"
    component_file.write_text("owned non-executable fixture")
    component = {"path": str(component_file), "sha256": hashlib.sha256(component_file.read_bytes()).hexdigest()}
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"schema_version": 1, **{key: component for key in
        ("node", "worker", "assembly", "template", "frida_module")}}))
    request = tmp_path / "request.json"
    request.write_text(json.dumps({
        "operation": "status", "process_id": os.getpid(), "timeout_ms": 1000,
        "output": str(tmp_path / "response.json"),
        "progress_output": str(tmp_path / "progress.jsonl"),
    }))
    run = run_client(wcf_client, "runtime-status", "--path", str(request),
                     "--runtime-profile", str(profile), "--timeout-seconds", "1",
                     "--request-sha256", hashlib.sha256(request.read_bytes()).hexdigest(),
                     "--runtime-profile-sha256", hashlib.sha256(profile.read_bytes()).hexdigest())
    assert run.returncode != 0
    assert "selected process is not the pinned local sysmac build" in run.stdout.lower(), run.stdout + run.stderr

    missing_progress = tmp_path / "missing-progress-request.json"
    missing_progress.write_text(json.dumps({
        "operation": "status", "process_id": os.getpid(), "timeout_ms": 1000,
        "output": str(tmp_path / "missing-progress-response.json"),
    }))
    run = run_client(wcf_client, "runtime-status", "--path", str(missing_progress),
                     "--runtime-profile", str(profile), "--timeout-seconds", "1",
                     "--request-sha256", hashlib.sha256(missing_progress.read_bytes()).hexdigest(),
                     "--runtime-profile-sha256", hashlib.sha256(profile.read_bytes()).hexdigest())
    assert run.returncode != 0
    assert "missing runtime progress path" in run.stdout.lower(), run.stdout + run.stderr
