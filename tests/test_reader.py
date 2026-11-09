from zipfile import ZipFile

import pytest

from sysmac_mcp.project import NativeProject


def test_reads_controller_programs_variables_ladder_and_tasks(project_file):
    p = NativeProject(project_file)
    assert p.info()["controllers"][0]["model"] == "NX-DEMO-CPU-001"
    assert p.info()["controllers"][0]["unit_version"] == "2.07"
    assert {v["name"] for v in p.variables()} == {"Start", "Run", "Counter"}
    assert p.read_pou("p")["sections"][0]["source"].startswith("Run := Start;")
    assert p.read_pou("ldp")["sections"][0]["rungs"][0]["CLs"][0]["Var"] == "Start"
    assert p.tasks()[0]["programs"][0]["ProgramName"] == "Main"
    assert p.info()["coverage"]["unknown_entries"]


def test_variable_attributes_and_io_are_not_discarded(project_file):
    p = NativeProject(project_file)
    v = next(v for v in p.variables() if v["name"] == "Start")
    assert v["attributes"]["Unknown"] == "preserve"
    assert p.io_bindings()[0]["address"] == "IOBus://test-unit#0/Synthetic Input 00"
    assert p.io_bindings()[0]["hardware_verified"] is False


def test_references_distinguish_code_from_strings_and_comments(project_file):
    refs = NativeProject(project_file).find_references("Start")
    st = [r for r in refs if r["language"] == "ST"]
    assert len(st) == 1
    assert st[0]["line"] == 1
    assert any(r["language"] == "LD" for r in refs)


@pytest.mark.parametrize("body", ["body", "ld", "wrapped"])
def test_protected_body_is_reported_without_decoding_or_hiding_other_sections(
    project_file, tmp_path, body
):
    with ZipFile(project_file) as source:
        members = {i.filename: source.read(i) for i in source.infolist()}
    members[f"s/{body}.xml"] = (
        b'\xef\xbb\xbf<?xml version="1.0" encoding="utf-16"?><EncryptedFile><EncryptionLock>opaque</EncryptionLock></EncryptedFile>'
    )
    if body == "wrapped":
        from xml.sax.saxutils import escape

        payload = members.pop("s/wrapped.xml").decode("utf-8-sig")
        members["s/body.xml"] = (
            "<StructuredTextModel>" + escape(payload) + "</StructuredTextModel>"
        ).encode("utf-8")
    candidate = tmp_path / "protected.smc2"
    with ZipFile(candidate, "w") as target:
        for name, content in members.items():
            target.writestr(name, content)
    project = NativeProject(candidate)
    info = project.info()
    assert info["protected_sections"] == 1
    section = project.read_pou("ldp" if body == "ld" else "p")["sections"][0]
    assert section["completeness"] == "protected"
    assert section["editable"] is False
