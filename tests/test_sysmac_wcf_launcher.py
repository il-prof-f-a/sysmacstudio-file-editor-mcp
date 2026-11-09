import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell launcher")


def preview(*args):
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-File", str(ROOT / "scripts/start-sysmac-wcf.ps1"), *args],
        capture_output=True, text=True, timeout=15,
    )


def test_launcher_only_previews_and_keeps_uac(tmp_path):
    # Fake executable is never executed by the preview.
    (tmp_path / "SysmacStudio.exe").write_bytes(b"preview only")
    run = preview("-VendorRoot", str(tmp_path), "-HostId", "test-offline")
    assert run.returncode == 0, run.stdout + run.stderr
    plan = json.loads(run.stdout)
    assert plan["launched"] is False
    assert plan["verb"] == "RunAs"
    assert plan["arguments"] == ["-wcfhost:test-offline"]
    assert plan["endpoint"] == "net.pipe://localhost/test-offline/V1.0/SolutionService/"


def test_launcher_rejects_endpoint_or_argument_injection(tmp_path):
    (tmp_path / "SysmacStudio.exe").write_bytes(b"preview only")
    run = preview("-VendorRoot", str(tmp_path), "-HostId", "bad/host -noui")
    assert run.returncode != 0
