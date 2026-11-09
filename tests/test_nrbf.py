import struct
from pathlib import Path

import pytest

from sysmac_mcp.nrbf import NrbfError, decode_nrbf, find_helper


@pytest.fixture
def helper():
    executable = find_helper()
    if executable is None:
        pytest.skip("Optional NRBF helper not built; run scripts/build-formats.ps1 on Windows.")
    return executable


def header():
    return b"\x00" + struct.pack("<iiii", 1, -1, 1, 0)


def text(value):
    raw = value.encode("utf-8")
    result = bytearray()
    remaining = len(raw)
    while remaining >= 128:
        result.append((remaining & 127) | 128)
        remaining >>= 7
    result.append(remaining)
    return bytes(result) + raw


def test_reads_unicode_string_without_application_assembly(helper):
    payload = header() + b"\x06" + struct.pack("<i", 1) + text("Example \u65e5\u672c") + b"\x0b"
    result = decode_nrbf(payload, helper=helper)
    assert result["protocol"] == 1
    assert result["records"][str(result["root"])]["value"] == "Example \u65e5\u672c"


def test_reads_unknown_class_and_self_reference_as_data(helper):
    payload = header()
    payload += b"\x0c" + struct.pack("<i", 42) + text("AssemblyThatDoesNotExist, Version=1.0.0.0")
    payload += b"\x05" + struct.pack("<i", 1) + text("Unknown.Namespace.Device")
    payload += struct.pack("<i", 3) + text("Name") + text("Index") + text("Self")
    payload += bytes([1, 0, 2]) + bytes([8]) + struct.pack("<i", 42)
    payload += b"\x06" + struct.pack("<i", 2) + text("Output")
    payload += struct.pack("<i", 7) + b"\x09" + struct.pack("<i", 1) + b"\x0b"
    result = decode_nrbf(payload, helper=helper)
    root = result["records"][str(result["root"])]
    assert root["type"] == "Unknown.Namespace.Device"
    assert root["members"]["Index"] == 7
    assert root["members"]["Name"] == "Output"
    assert root["members"]["Self"] == {"$ref": result["root"]}


def test_reads_bounded_primitive_array(helper):
    payload = header() + b"\x0f" + struct.pack("<iiBiii", 1, 3, 8, 2, 3, 4) + b"\x0b"
    result = decode_nrbf(payload, helper=helper)
    assert result["records"][str(result["root"])]["values"] == [2, 3, 4]


def test_truncated_or_inflated_payload_is_rejected(helper):
    with pytest.raises(NrbfError):
        decode_nrbf(header() + b"\x0f" + struct.pack("<iiB", 1, 2_000_000_000, 8), helper=helper)
    with pytest.raises(NrbfError):
        decode_nrbf(b"not an NRBF stream", helper=helper)


def test_payload_limit_checked_before_starting_helper():
    with pytest.raises(NrbfError, match="limit"):
        decode_nrbf(b"a" * (8 * 1024 * 1024 + 1), helper=Path("does-not-exist.exe"))


def test_missing_optional_reader_is_reported(monkeypatch):
    monkeypatch.setattr("sysmac_mcp.nrbf.find_helper", lambda: None)
    with pytest.raises(NrbfError, match="unavailable"):
        decode_nrbf(header())
