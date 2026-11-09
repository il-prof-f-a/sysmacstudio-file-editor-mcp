"""Bounded subprocess adapter to Microsoft's passive NRBF decoder."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

MAX_INPUT = 8 * 1024 * 1024
MAX_OUTPUT = 32 * 1024 * 1024


class NrbfError(ValueError):
    """The optional NRBF reader is missing or rejected the payload."""


def find_helper() -> Path | None:
    configured = os.environ.get("SYSMAC_FORMATS_HELPER")
    if configured:
        path = Path(configured).resolve()
        return path if path.is_file() else None
    root = Path(__file__).resolve().parents[2]
    candidates = [
        Path(__file__).parent / "_bin" / "SysmacFormats.exe",
        root / "bridge/SysmacFormats/bin/Release/net10.0/win-x64/publish/SysmacFormats.exe",
    ]
    return next((p for p in candidates if p.is_file()), None)


def decode_nrbf(payload: bytes, *, helper: Path | None = None) -> dict:
    if len(payload) > MAX_INPUT:
        raise NrbfError("NRBF input size limit exceeded.")
    executable = helper or find_helper()
    if executable is None or not executable.is_file():
        raise NrbfError(
            "NRBF reader unavailable. Build SysmacFormats or set SYSMAC_FORMATS_HELPER."
        )
    try:
        result = subprocess.run(
            [str(executable)],
            input=payload,
            capture_output=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NrbfError("NRBF reader could not complete within its execution limits.") from exc
    if len(result.stdout) > MAX_OUTPUT:
        raise NrbfError("NRBF output size limit exceeded.")
    try:
        data = json.loads(result.stdout)
    except (ValueError, UnicodeDecodeError) as exc:
        raise NrbfError("NRBF reader returned an invalid response.") from exc
    if not isinstance(data, dict) or data.get("protocol") != 1:
        raise NrbfError("NRBF reader protocol mismatch.")
    if result.returncode or data.get("error"):
        error = data.get("error") or {}
        raise NrbfError(str(error.get("message", "NRBF reader failed.")))
    if not isinstance(data.get("records"), dict) or str(data.get("root")) not in data["records"]:
        raise NrbfError("NRBF reader returned an incomplete graph.")
    return data
