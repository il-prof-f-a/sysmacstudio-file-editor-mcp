from pathlib import Path
from zipfile import ZipFile

import pytest

from sysmac_mcp.project import NativeProject
from sysmac_mcp.project_compare import compare_projects
from sysmac_mcp.workspace import Workspace, sha256


@pytest.mark.local_project
def test_real_project_native_roundtrip_preserves_all_unmodified_members(tmp_path):
    samples = list((Path(__file__).resolve().parents[1] / "materiali" / "esempi").glob("*.smc2"))
    if not samples:
        pytest.skip("No user-provided local project; synthetic protocol tests still run.")
    original = samples[0]
    digest = sha256(original)
    workspace = Workspace(tmp_path / "workspace")
    opened = workspace.open_project(original)
    pid = opened["project_id"]
    project = workspace.project(pid)
    info = project.info()
    assert info["controllers"]
    assert info["st_sections"] and info["ld_sections"]
    assert info["io_binding_count"]
    global_variable = next(v for v in project.variables() if v["scope"] == "global")
    section = next(
        s
        for pou in project.pous()
        if pou["kind"] == "Program"
        for s in project.read_pou(pou["id"])["sections"]
        if s["language"] == "ST"
    )
    tx = workspace.begin(pid)["transaction_id"]
    workspace.create_variable(
        tx,
        global_variable["table_id"],
        "MCP_Offline_Test",
        "BOOL",
        initial_value="FALSE",
        expected_revision=0,
    )
    workspace.update_st(
        tx, section["id"], section["source"] + "\nMCP_Offline_Test := FALSE;\n", expected_revision=0
    )
    pou = project.owner(project.entities[section["id"]])
    alias = next(
        v for v in workspace.project(pid, tx).variables(pou) if v["name"] == "MCP_Offline_Test"
    )
    assert alias["group"] == "VAR_EXTERNAL" and alias["data_type"] == "BOOL"
    assert workspace.validate(tx)["missing_external_count"] == 0
    committed = workspace.commit(tx)
    assert set(committed["changed_entries"]) == {
        section["member"],
        global_variable["member"],
        alias["member"],
    }
    output = tmp_path / "candidate.smc2"
    workspace.export(pid, output)
    reopened = NativeProject(output)
    assert any(v["name"] == "MCP_Offline_Test" for v in reopened.variables("global"))
    with ZipFile(original) as before, ZipFile(output) as after:
        assert before.namelist() == after.namelist()
        for name in before.namelist():
            if name not in committed["changed_entries"]:
                assert before.read(name) == after.read(name)
    assert sha256(original) == digest
    assert committed["sysmac_build"] == "not_executed"
    comparison = compare_projects(original, output)
    assert comparison["model_difference_categories"] == {"section": 1, "variable": 2}
    assert comparison["archive_difference_count"] == 3
    assert (
        comparison["coverage"]["before"]["unacquired_section_count"] == info["unacquired_sections"]
    )
