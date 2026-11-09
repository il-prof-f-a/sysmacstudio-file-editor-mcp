import sys
from pathlib import Path

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from sysmac_mcp.server import create_server


async def test_protocol_exposes_real_tools_and_structured_errors(tmp_path, project_file):
    server = create_server(tmp_path / "workspace")
    async with Client(server) as client:
        listing = await client.list_tools()
        names = {t.name for t in listing.tools}
        assert {
            "plc_open_project",
            "plc_list_pous",
            "plc_begin_transaction",
            "plc_update_st",
            "plc_export_project",
            "plc_delete_variable",
            "plc_delete_pou",
            "plc_unassign_program",
            "plc_preview_iec_xml_import",
            "plc_stage_iec_xml_import",
        } <= names
        unassign = next(t for t in listing.tools if t.name == "plc_unassign_program")
        delete_pou = next(t for t in listing.tools if t.name == "plc_delete_pou")
        assert unassign.annotations is not None
        assert unassign.annotations.destructive_hint is True
        assert delete_pou.annotations is not None
        assert delete_pou.annotations.destructive_hint is True
        assert not any("download" in name or "force" in name for name in names)
        opened = await client.call_tool("plc_open_project", {"path": str(project_file)})
        assert not opened.is_error
        pid = opened.structured_content["project_id"]
        page = await client.call_tool("plc_list_variables", {"project_id": pid, "limit": 1})
        assert page.structured_content["total"] == 3
        assert len(page.structured_content["items"]) == 1
        assert page.structured_content["next_offset"] == 1
        missing = await client.call_tool("plc_get_project_info", {"project_id": "missing"})
        assert missing.is_error
        assert "project_not_found" in str(missing.content)


async def test_capabilities_report_multi_network_and_limited_data_type_import(tmp_path):
    async with Client(create_server(tmp_path / "workspace")) as client:
        result = await client.call_tool("plc_get_capabilities", {})
        assert not result.is_error
        capabilities = result.structured_content
        assert (
            capabilities["iec_xml_import"]
            == "preview_stage_ST_multi_network_LD_global_variables_and_limited_data_types"
        )
        assert (
            capabilities["iec_xml_data_type_import"]
            == "limited_struct_union_named_dint_enum_inline_array_namespace_flattening"
        )
        assert (
            capabilities["native_data_types"]
            == "observed_read_with_guarded_rename_and_delete_of_controller_types"
        )
        assert (
            capabilities["rename_data_type"]
            == "experimental_guarded_top_level_controller_types_external_consumers_not_acquired"
        )


async def test_complete_protocol_transaction_workflow(tmp_path, project_file):
    server = create_server(tmp_path / "workspace")
    async with Client(server) as client:
        opened = await client.call_tool("plc_open_project", {"path": str(project_file)})
        pid = opened.structured_content["project_id"]
        begin = await client.call_tool("plc_begin_transaction", {"project_id": pid})
        tx = begin.structured_content["transaction_id"]
        staged = await client.call_tool(
            "plc_update_st",
            {
                "transaction_id": tx,
                "section_id": "body",
                "source": "Run := FALSE;",
                "expected_revision": 0,
            },
        )
        assert not staged.is_error
        assert staged.structured_content["status"] == "staged"
        preview = await client.call_tool("plc_preview_changes", {"transaction_id": tx})
        assert preview.structured_content["changed_entries"] == ["s/body.xml"]
        result = await client.call_tool("plc_commit_changes", {"transaction_id": tx})
        assert result.structured_content["revision"] == 1
        exported = await client.call_tool(
            "plc_export_project", {"project_id": pid, "destination": str(tmp_path / "out.smc2")}
        )
        assert exported.structured_content["status"] == "candidate_unverified"


async def test_protocol_delete_variable_stages_preview_and_commit(tmp_path, project_file):
    async with Client(create_server(tmp_path / "workspace")) as client:
        opened = await client.call_tool("plc_open_project", {"path": str(project_file)})
        project_id = opened.structured_content["project_id"]
        transaction = await client.call_tool("plc_begin_transaction", {"project_id": project_id})
        transaction_id = transaction.structured_content["transaction_id"]
        listing = await client.call_tool(
            "plc_list_variables", {"project_id": project_id, "scope": "p"}
        )
        counter = next(item for item in listing.structured_content["items"] if item["name"] == "Counter")

        deleted = await client.call_tool(
            "plc_delete_variable",
            {"transaction_id": transaction_id, "variable_id": counter["id"], "expected_revision": 0},
        )

        assert not deleted.is_error
        assert deleted.structured_content["change"]["external_consumers"] == "not_acquired"
        preview = await client.call_tool("plc_preview_changes", {"transaction_id": transaction_id})
        assert preview.structured_content["changed_entries"] == ["s/locals.xml"]
        committed = await client.call_tool("plc_commit_changes", {"transaction_id": transaction_id})
        assert not committed.is_error
        assert committed.structured_content["revision"] == 1
        assert committed.structured_content["sysmac_build"] == "not_executed"


async def test_real_stdio_subprocess_initialization_and_call(tmp_path, project_file):
    root = Path(__file__).resolve().parents[1]
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "sysmac_mcp", "--workspace", str(tmp_path / "stdio-workspace")],
        env={"PYTHONPATH": str(root / "src")},
        cwd=root,
    )
    async with Client(params, read_timeout_seconds=20) as client:
        result = await client.call_tool("plc_open_project", {"path": str(project_file)})
        assert not result.is_error
        assert result.structured_content["info"]["pou_count"] == 2
        caps = await client.call_tool("plc_get_capabilities", {})
        assert caps.structured_content["sysmac_build"] == "experimental_pinned_runtime_adapter"


async def test_parameter_bounds_are_enforced(tmp_path, project_file):
    async with Client(create_server(tmp_path / "workspace")) as client:
        result = await client.call_tool(
            "plc_list_variables", {"project_id": "missing", "limit": 1000000}
        )
        assert result.is_error


async def test_generate_iec_xml_through_protocol(tmp_path):
    destination = tmp_path / "import.xml"
    async with Client(create_server(tmp_path / "workspace")) as client:
        result = await client.call_tool(
            "plc_generate_iec_xml",
            {
                "name": "Demo",
                "destination": str(destination),
                "pous": [{"name": "Main", "source": "Run := FALSE;"}],
                "global_variables": [{"name": "Run", "data_type": "BOOL"}],
            },
        )
        assert not result.is_error
        assert destination.read_bytes().startswith(b"<?xml")
        assert result.structured_content["sysmac_import"] == "not_executed"
        again = await client.call_tool(
            "plc_generate_iec_xml",
            {"name": "Demo", "destination": str(destination), "pous": [], "global_variables": []},
        )
        assert again.is_error


async def test_iec_xml_import_preview_and_stage_through_protocol(tmp_path, project_file):
    xml_path = tmp_path / "pous.xml"
    async with Client(create_server(tmp_path / "workspace")) as client:
        generated = await client.call_tool(
            "plc_generate_iec_xml",
            {
                "name": "ImportDemo",
                "destination": str(xml_path),
                "pous": [
                    {
                        "name": "ImportedProgram",
                        "source": "OutputValue := TRUE;",
                        "variables": [{"name": "OutputValue", "data_type": "BOOL"}],
                    }
                ],
                "global_variables": [],
            },
        )
        assert not generated.is_error
        opened = await client.call_tool("plc_open_project", {"path": str(project_file)})
        pid = opened.structured_content["project_id"]
        transaction = await client.call_tool("plc_begin_transaction", {"project_id": pid})
        tx = transaction.structured_content["transaction_id"]
        preview = await client.call_tool(
            "plc_preview_iec_xml_import",
            {
                "transaction_id": tx,
                "xml_path": str(xml_path),
                "template_pou_ids": {"Program": "p"},
                "expected_revision": 0,
            },
        )
        assert not preview.is_error, preview.content
        assert preview.structured_content["status"] == "ready"
        stage = await client.call_tool(
            "plc_stage_iec_xml_import",
            {
                "transaction_id": tx,
                "xml_path": str(xml_path),
                "template_pou_ids": {"Program": "p"},
                "expected_source_sha256": preview.structured_content["source_sha256"],
                "expected_plan_id": preview.structured_content["plan_id"],
                "expected_revision": 0,
            },
        )
        assert not stage.is_error, stage.content
        assert stage.structured_content["change"]["pous"][0]["name"] == "ImportedProgram"


async def test_symbol_analysis_and_rename_through_protocol(tmp_path, project_file):
    async with Client(create_server(tmp_path / "workspace")) as client:

        async def invoke(tool, **arguments):
            result = await client.call_tool(tool, arguments)
            assert not result.is_error, result.content
            return result.structured_content

        pid = (await invoke("plc_open_project", path=str(project_file)))["project_id"]
        variable = (
            await invoke("plc_list_variables", project_id=pid, scope="global", query="Start")
        )["items"][0]
        analysis = await invoke(
            "plc_analyze_symbol", project_id=pid, variable_id=variable["id"], limit=1
        )
        assert analysis["total"] == 2 and analysis["next_offset"] == 1
        assert analysis["canonical_variable_id"] == variable["id"]
        plan = await invoke(
            "plc_plan_rename",
            project_id=pid,
            variable_id=variable["id"],
            new_name="Enable",
            limit=1,
        )
        assert plan["can_apply"] and len(plan["items"]) == 1
        assert set(plan["changed_members"]) == {"s/globals.xml", "s/body.xml", "s/ld.xml"}
        tx = (await invoke("plc_begin_transaction", project_id=pid))["transaction_id"]
        renamed = await invoke(
            "plc_rename_variable",
            transaction_id=tx,
            variable_id=variable["id"],
            new_name="Enable",
            expected_revision=0,
        )
        assert renamed["change"]["reference_count"] == 2
        staged = await invoke(
            "plc_list_variables", project_id=pid, query="Enable", transaction_id=tx
        )
        assert staged["total"] == 1
        assert staged["items"][0]["id"] == renamed["change"]["new_variable_id"]
        committed = await invoke("plc_commit_changes", transaction_id=tx)
        assert committed["sysmac_build"] == "not_executed"
        stale = await client.call_tool(
            "plc_plan_rename",
            {"project_id": pid, "variable_id": variable["id"], "new_name": "Another"},
        )
        assert stale.is_error
