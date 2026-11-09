from zipfile import ZipFile

import pytest
from lxml import etree as ET

from sysmac_mcp.project import NativeProject
from sysmac_mcp.workspace import ProjectError, Workspace

DATA_TYPE_XML = b"""<data Version="1">
  <DataType Name="Data Types" BaseType="" Order="0">
    <DataType Name="Valve" BaseType="STRUCT" Comment="Valve status" Order="1" OffsetType="User">
      <DataType Name="IsOpen" BaseType="BOOL" OffsetBit="0" Comment="Open state" VendorFlag="keep" />
      <DataType Name="Position" BaseType="INT" OffsetChannel="2" />
    </DataType>
    <DataType Name="ValveArray" BaseType="ARRAY[1..3] OF Valve" ArrayType="1..3" Length="3" />
  </DataType>
</data>"""

ENUM_DATA_TYPE_XML = b"""<data Version="1">
  <DataType Name="Data Types" BaseType="" Order="0">
    <DataType Name="Mode" BaseType="DINT" Order="1" VendorFlag="keep">
      <DataType Name="Stopped" BaseType="DINT" EnumValue="0" />
      <DataType Name="Running" BaseType="DINT" EnumValue="1" />
    </DataType>
    <DataType Name="ModeAlias" BaseType="Mode" InitialValue="Mode#Stopped" />
    <DataType Name="ModeArray" BaseType="ARRAY[0..1] OF Mode" ArrayType="0..1" Length="2" />
  </DataType>
</data>"""


def project_with_data_types(project_file, tmp_path, xml_payload=DATA_TYPE_XML, table_id="types"):
    with ZipFile(project_file) as source:
        members = {item.filename: source.read(item) for item in source.infolist()}

    oem = ET.fromstring(members["s/s.oem"])
    controller = oem.find('.//Entity[@type="Device"][@id="cpu"]')
    table = ET.SubElement(
        controller.find("ChildEntities"),
        "Entity",
        type="DataType",
        subtype="",
        id=table_id,
        name="Data Types",
    )
    ET.SubElement(table, "ChildEntities")
    members["s/s.oem"] = ET.tostring(oem, encoding="utf-8")
    type_member = f"s/{table_id}.xml"
    if table_id != "types":
        members.pop("s/types.xml", None)
    if xml_payload is not None:
        members[type_member] = xml_payload
    else:
        members.pop(type_member, None)

    candidate = tmp_path / "with-data-types.smc2"
    with ZipFile(candidate, "w") as target:
        for name, content in members.items():
            target.writestr(name, content)
    return candidate


def rewrite_member(path, member, content):
    with ZipFile(path) as source:
        members = {item.filename: source.read(item) for item in source.infolist()}
    members[member] = content
    with ZipFile(path, "w") as target:
        for name, raw in members.items():
            target.writestr(name, raw)


def test_validate_reports_empty_structs_and_missing_user_offsets(project_file, tmp_path):
    data_types = b"""<data Version="1">
      <DataType Name="Data Types" BaseType="" Order="0">
        <DataType Name="UserStatus" BaseType="STRUCT" OffsetType="User" Order="1">
          <DataType Name="Mode" BaseType="DINT" />
          <DataType Name="Ready" BaseType="BOOL" />
        </DataType>
        <DataType Name="AutomaticStatus" BaseType="STRUCT" OffsetType="NJ" Order="2">
          <DataType Name="Ready" BaseType="BOOL" />
        </DataType>
        <DataType Name="EmptyStatus" BaseType="STRUCT" OffsetType="NJ" Order="3" />
      </DataType>
    </data>"""
    candidate = project_with_data_types(project_file, tmp_path, data_types)
    workspace = Workspace(tmp_path / "workspace")
    project_id = workspace.open_project(candidate)["project_id"]
    transaction_id = workspace.begin(project_id)["transaction_id"]

    result = workspace.validate(transaction_id)

    diagnostics = result["native_data_type_layout_diagnostics_preview"]
    assert result["native_data_type_layout_diagnostic_count"] == 3
    assert [item["code"] for item in diagnostics].count("user_offset_not_set") == 2
    assert [item["code"] for item in diagnostics].count("empty_data_type") == 1
    user_offsets = [item for item in diagnostics if item["code"] == "user_offset_not_set"]
    assert {tuple(item["missing_offsets"]) for item in user_offsets} == {
        ("byte",),
        ("byte", "bit"),
    }


def test_validate_skips_incomplete_data_type_tables(project_file, tmp_path):
    data_types = b"""<data Version="1">unparsed
      <DataType Name="Data Types" BaseType="" Order="0">
        <DataType Name="UserStatus" BaseType="STRUCT" OffsetType="User">
          <DataType Name="Ready" BaseType="BOOL" />
        </DataType>
      </DataType>
    </data>"""
    candidate = project_with_data_types(project_file, tmp_path, data_types)
    workspace = Workspace(tmp_path / "workspace")
    project_id = workspace.open_project(candidate)["project_id"]
    transaction_id = workspace.begin(project_id)["transaction_id"]

    result = workspace.validate(transaction_id)

    assert result["native_data_type_layout_check"] == "not_executed"
    assert result["native_data_type_layout_diagnostic_count"] == 0
    assert result["native_data_type_layout_coverage"]["skipped_incomplete_tables"] == 1


def project_with_mode_references(project_file, tmp_path, *, table_id="types"):
    candidate = project_with_data_types(
        project_file, tmp_path, ENUM_DATA_TYPE_XML, table_id=table_id
    )
    with ZipFile(candidate) as archive:
        globals_xml = archive.read("s/globals.xml")
        locals_xml = archive.read("s/locals.xml")
        ladder = archive.read("s/ld.xml")

    rewrite_member(
        candidate,
        "s/globals.xml",
        globals_xml
        + b"++D=Mode\tN=CurrentMode\tIV=Mode#Stopped\tG=VAR_GLOBAL\tUnknown=keep\r\n",
    )
    rewrite_member(
        candidate,
        "s/locals.xml",
        locals_xml + b"++D=ARRAY[0..1] OF Mode\tN=History\tG=VAR\r\n",
    )
    rewrite_member(
        candidate,
        "s/body.xml",
        b"<StructuredTextModel><Text>CurrentMode := Mode#Stopped; // Mode#Stopped\nMessage := 'Mode#Stopped';</Text></StructuredTextModel>",
    )
    rewrite_member(
        candidate,
        "s/ld.xml",
        ladder.replace(
            b'"__type":"ST","Var":"Run"',
            b'"__type":"ST","Var":"CurrentMode := Mode#Stopped; // Mode#Stopped"',
        ),
    )
    return candidate


def test_delete_unreferenced_top_level_leaf_data_type_stages_only_table(
    project_file, tmp_path
):
    candidate = project_with_data_types(project_file, tmp_path)
    original = candidate.read_bytes()
    workspace = Workspace(tmp_path / "workspace")
    project_id = workspace.open_project(candidate)["project_id"]
    transaction_id = workspace.begin(project_id)["transaction_id"]
    inventory = workspace.project(project_id, transaction_id).data_types(controller_id="cpu")
    array_type = next(item for item in inventory["items"] if item["name"] == "ValveArray")

    result = workspace.delete_data_type(
        transaction_id,
        array_type["id"],
        expected_name="ValveArray",
        expected_base_type="ARRAY[1..3] OF Valve",
        expected_revision=0,
    )

    assert result["status"] == "staged"
    assert workspace.preview(transaction_id)["changed_entries"] == ["s/types.xml"]
    remaining = workspace.project(project_id, transaction_id).data_types(controller_id="cpu")
    assert [item["name"] for item in remaining["items"]] == ["Valve"]
    assert candidate.read_bytes() == original


def test_rename_data_type_updates_only_observed_typed_references(project_file, tmp_path):
    candidate = project_with_mode_references(project_file, tmp_path, table_id="DataType:bundle")
    original = candidate.read_bytes()
    workspace = Workspace(tmp_path / "workspace")
    project_id = workspace.open_project(candidate)["project_id"]
    transaction_id = workspace.begin(project_id)["transaction_id"]
    project = workspace.project(project_id, transaction_id)
    mode = next(
        item
        for item in project.data_types(controller_id="cpu")["items"]
        if item["name"] == "Mode"
    )

    plan = workspace.plan_rename_data_type(
        project_id,
        mode["id"],
        expected_name="Mode",
        expected_base_type="DINT",
        new_name="OperationMode",
        transaction_id=transaction_id,
    )

    assert plan["can_apply"] is True, plan["blockers"]
    assert set(plan["changed_members"]) == {
        "s/DataType:bundle.xml",
        "s/globals.xml",
        "s/locals.xml",
        "s/body.xml",
        "s/ld.xml",
    }
    result = workspace.rename_data_type(
        transaction_id,
        mode["id"],
        expected_name="Mode",
        expected_base_type="DINT",
        new_name="OperationMode",
        expected_revision=0,
    )

    assert result["status"] == "staged"
    assert set(workspace.preview(transaction_id)["changed_entries"]) == set(
        plan["changed_members"]
    )
    renamed = workspace.project(project_id, transaction_id)
    types = renamed.data_types(controller_id="cpu")
    assert any(item["name"] == "OperationMode" for item in types["items"])
    alias = next(item for item in types["items"] if item["name"] == "ModeArray")
    assert alias["base_type"] == "ARRAY[0..1] OF OperationMode"
    mode_alias = next(item for item in types["items"] if item["name"] == "ModeAlias")
    assert mode_alias["base_type"] == "OperationMode"
    assert mode_alias["initial_value"] == "OperationMode#Stopped"
    global_mode = next(v for v in renamed.variables("global") if v["name"] == "CurrentMode")
    assert global_mode["data_type"] == "OperationMode"
    assert global_mode["initial_value"] == "OperationMode#Stopped"
    assert global_mode["attributes"]["Unknown"] == "keep"
    assert next(v for v in renamed.variables("p") if v["name"] == "History")["data_type"] == (
        "ARRAY[0..1] OF OperationMode"
    )
    assert b'VendorFlag="keep"' in renamed.archive.read("s/DataType:bundle.xml")
    assert candidate.read_bytes() == original

    st_source = renamed.read_section(renamed.entities["body"])["source"]
    assert "OperationMode#Stopped" in st_source
    assert "// Mode#Stopped" in st_source
    assert "'Mode#Stopped'" in st_source
    ladder_source = renamed.read_section(renamed.entities["ld"])["rungs"][0]["CLs"][1]["Var"]
    assert "OperationMode#Stopped" in ladder_source
    assert "// Mode#Stopped" in ladder_source
    assert renamed.archive.read("s/ld.xml").startswith(b"\xef\xbb\xbf")


def test_rename_data_type_plan_blocks_collisions_and_unsupported_type_shapes(
    project_file, tmp_path
):
    candidate = project_with_data_types(
        project_file,
        tmp_path,
        b'<data><DataType Name="Data Types" BaseType=""><DataType Name="Base" BaseType="DINT"/><DataType Name="OpaqueAlias" BaseType="POINTER TO ARRAY[0..1] OF Base"/></DataType></data>',
    )
    workspace = Workspace(tmp_path / "workspace")
    project_id = workspace.open_project(candidate)["project_id"]
    transaction_id = workspace.begin(project_id)["transaction_id"]
    base = next(
        item
        for item in workspace.project(project_id, transaction_id).data_types(
            controller_id="cpu"
        )["items"]
        if item["name"] == "Base"
    )

    collision = workspace.plan_rename_data_type(
        project_id,
        base["id"],
        expected_name="Base",
        expected_base_type="DINT",
        new_name="Counter",
        transaction_id=transaction_id,
    )
    unsupported = workspace.plan_rename_data_type(
        project_id,
        base["id"],
        expected_name="Base",
        expected_base_type="DINT",
        new_name="DataBase",
        transaction_id=transaction_id,
    )
    unchanged = workspace.plan_rename_data_type(
        project_id,
        base["id"],
        expected_name="Base",
        expected_base_type="DINT",
        new_name="Base",
        transaction_id=transaction_id,
    )

    assert collision["can_apply"] is False
    assert "declaration_collision" in {item["code"] for item in collision["blockers"]}
    assert unsupported["can_apply"] is False
    assert "unsupported_type_expression" in {item["code"] for item in unsupported["blockers"]}
    assert unchanged["can_apply"] is False
    assert "name_unchanged" in {item["code"] for item in unchanged["blockers"]}
    assert workspace.preview(transaction_id)["changed_entries"] == []


def test_rename_data_type_rejects_stale_identity(project_file, tmp_path):
    candidate = project_with_data_types(project_file, tmp_path)
    workspace = Workspace(tmp_path / "workspace")
    project_id = workspace.open_project(candidate)["project_id"]
    transaction_id = workspace.begin(project_id)["transaction_id"]
    array_type = next(
        item
        for item in workspace.project(project_id, transaction_id).data_types(
            controller_id="cpu"
        )["items"]
        if item["name"] == "ValveArray"
    )

    with pytest.raises(ProjectError, match="identity"):
        workspace.rename_data_type(
            transaction_id,
            array_type["id"],
            expected_name="ValveArray",
            expected_base_type="ARRAY[1..3] OF WrongType",
            new_name="ActuatorArray",
            expected_revision=0,
        )
    assert workspace.preview(transaction_id)["changed_entries"] == []


def test_delete_data_type_resolves_table_ids_containing_colons(project_file, tmp_path):
    candidate = project_with_data_types(
        project_file, tmp_path, table_id="DataType:bundle"
    )
    workspace = Workspace(tmp_path / "workspace")
    project_id = workspace.open_project(candidate)["project_id"]
    transaction_id = workspace.begin(project_id)["transaction_id"]
    array_type = next(
        item
        for item in workspace.project(project_id, transaction_id).data_types(
            controller_id="cpu"
        )["items"]
        if item["name"] == "ValveArray"
    )

    result = workspace.delete_data_type(
        transaction_id,
        array_type["id"],
        expected_name="ValveArray",
        expected_base_type="ARRAY[1..3] OF Valve",
        expected_revision=0,
    )

    assert result["status"] == "staged"
    assert workspace.preview(transaction_id)["changed_entries"] == [
        "s/DataType:bundle.xml"
    ]


def test_delete_data_type_rejects_members_and_stale_identity(project_file, tmp_path):
    candidate = project_with_data_types(project_file, tmp_path)
    workspace = Workspace(tmp_path / "workspace")
    project_id = workspace.open_project(candidate)["project_id"]
    transaction_id = workspace.begin(project_id)["transaction_id"]
    valve = next(
        item
        for item in workspace.project(project_id, transaction_id).data_types(controller_id="cpu")["items"]
        if item["name"] == "Valve"
    )

    with pytest.raises(ProjectError, match="leaf"):
        workspace.delete_data_type(
            transaction_id,
            valve["id"],
            expected_name="Valve",
            expected_base_type="STRUCT",
            expected_revision=0,
        )
    with pytest.raises(ProjectError, match="identity"):
        workspace.delete_data_type(
            transaction_id,
            valve["id"],
            expected_name="ValveArray",
            expected_base_type="STRUCT",
            expected_revision=0,
        )
    assert workspace.preview(transaction_id)["changed_entries"] == []


@pytest.mark.parametrize(
    ("xml_payload", "globals_suffix", "source"),
    [
        (
            DATA_TYPE_XML,
            b"++D=ValveArray\tN=Typed\tG=VAR_GLOBAL\r\n",
            "Run := Start;",
        ),
        (
            DATA_TYPE_XML,
            b"",
            "Run := ValveArray#1;",
        ),
        (
            b'<data><DataType Name="Data Types" BaseType="" Order="0"><DataType Name="Base" BaseType="INT"/><DataType Name="Alias" BaseType="ARRAY[1..2] OF Base"/></DataType></data>',
            b"",
            "Run := Start;",
        ),
    ],
)
def test_delete_data_type_rejects_observed_references(
    project_file, tmp_path, xml_payload, globals_suffix, source
):
    candidate = project_with_data_types(project_file, tmp_path, xml_payload)
    if globals_suffix:
        with ZipFile(candidate) as archive:
            globals_xml = archive.read("s/globals.xml")
        rewrite_member(candidate, "s/globals.xml", globals_xml + globals_suffix)
    rewrite_member(
        candidate,
        "s/body.xml",
        f"<StructuredTextModel><Text>{source}</Text></StructuredTextModel>".encode(),
    )
    workspace = Workspace(tmp_path / "workspace")
    project_id = workspace.open_project(candidate)["project_id"]
    transaction_id = workspace.begin(project_id)["transaction_id"]
    inventory = workspace.project(project_id, transaction_id).data_types(controller_id="cpu")
    target_name = "Base" if "Name=\"Base\"" in xml_payload.decode() else "ValveArray"
    target = next(item for item in inventory["items"] if item["name"] == target_name)

    with pytest.raises(ProjectError, match="referenced"):
        workspace.delete_data_type(
            transaction_id,
            target["id"],
            expected_name=target["name"],
            expected_base_type=target["base_type"],
            expected_revision=0,
        )
    assert workspace.preview(transaction_id)["changed_entries"] == []


def test_delete_data_type_requires_complete_controller_data_type_tables(project_file, tmp_path):
    candidate = project_with_data_types(project_file, tmp_path, b"<data><DataType")
    workspace = Workspace(tmp_path / "workspace")
    project_id = workspace.open_project(candidate)["project_id"]
    transaction_id = workspace.begin(project_id)["transaction_id"]

    with pytest.raises(ProjectError, match="completely observed"):
        workspace.delete_data_type(
            transaction_id,
            "types:0",
            expected_name="MaybeType",
            expected_base_type="INT",
            expected_revision=0,
        )
    assert workspace.preview(transaction_id)["changed_entries"] == []


def test_native_data_type_inventory_preserves_hierarchy_and_attributes(project_file, tmp_path):
    candidate = project_with_data_types(project_file, tmp_path)

    inventory = NativeProject(candidate).data_types()

    assert inventory["completeness"] == "observed"
    assert inventory["total"] == 2
    valve = next(item for item in inventory["items"] if item["name"] == "Valve")
    assert valve["base_type"] == "STRUCT"
    assert valve["depth"] == 0
    assert valve["child_count"] == 2
    assert valve["attributes"]["OffsetType"] == "User"

    fields = NativeProject(candidate).data_types(parent_id=valve["id"])
    assert [item["name"] for item in fields["items"]] == ["IsOpen", "Position"]
    assert fields["items"][0]["parent_id"] == valve["id"]
    assert fields["items"][0]["attributes"]["VendorFlag"] == "keep"

    info = NativeProject(candidate).info()
    assert info["data_type_count"] == 2
    assert "s/types.xml" not in info["coverage"]["unknown_entries"]


def test_controller_defined_data_types_are_available_to_iec_type_validation(project_file, tmp_path):
    candidate = project_with_data_types(project_file, tmp_path)
    project = NativeProject(candidate)

    available = Workspace._iec_type_names(project, "cpu")

    assert Workspace._iec_type_key("Valve") in available
    assert Workspace._iec_type_key("ValveArray") in available
    assert Workspace._iec_type_key("IsOpen") not in available
    Workspace._require_iec_type("Valve", "local", available)
    with pytest.raises(ProjectError, match="not present or supported"):
        Workspace._require_iec_type("UnknownType", "local", available)


async def test_data_type_inventory_is_exposed_as_a_paginated_read_tool(project_file, tmp_path):
    import sys
    from pathlib import Path

    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    candidate = project_with_data_types(project_file, tmp_path)
    root = Path(__file__).resolve().parents[1]
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "sysmac_mcp", "--workspace", str(tmp_path / "stdio-mcp")],
        env={"PYTHONPATH": str(root / "src")},
        cwd=root,
    )
    async with Client(params, read_timeout_seconds=20) as client:
        opened = await client.call_tool("plc_open_project", {"path": str(candidate)})
        project_id = opened.structured_content["project_id"]
        listing = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert "plc_list_data_types" in listing
        assert "plc_delete_data_type" in listing
        assert listing["plc_delete_data_type"].annotations is not None
        assert listing["plc_delete_data_type"].annotations.destructive_hint is True
        assert listing["plc_plan_rename_data_type"].annotations.read_only_hint is True
        assert listing["plc_rename_data_type"].annotations.destructive_hint is False
        output_schema = listing["plc_list_data_types"].output_schema
        assert output_schema["properties"]["items"]["type"] == "array"
        assert "completeness" in output_schema["properties"]

        page = await client.call_tool(
            "plc_list_data_types", {"project_id": project_id, "limit": 1}
        )
        assert page.structured_content["total"] == 2
        valve = page.structured_content["items"][0]
        assert valve["name"] == "Valve"
        fields = await client.call_tool(
            "plc_list_data_types",
            {"project_id": project_id, "parent_id": valve["id"]},
        )
        assert [item["name"] for item in fields.structured_content["items"]] == [
            "IsOpen",
            "Position",
        ]
        transaction = await client.call_tool(
            "plc_begin_transaction", {"project_id": project_id}
        )
        transaction_id = transaction.structured_content["transaction_id"]
        array_page = await client.call_tool(
            "plc_list_data_types",
            {"project_id": project_id, "limit": 1, "offset": 1},
        )
        array_type = array_page.structured_content["items"][0]
        staged = await client.call_tool(
            "plc_delete_data_type",
            {
                "transaction_id": transaction_id,
                "data_type_id": array_type["id"],
                "expected_name": array_type["name"],
                "expected_base_type": array_type["base_type"],
                "expected_revision": 0,
            },
        )
        assert staged.structured_content["status"] == "staged"
        rename_plan = await client.call_tool(
            "plc_plan_rename_data_type",
            {
                "project_id": project_id,
                "transaction_id": transaction_id,
                "data_type_id": valve["id"],
                "expected_name": "Valve",
                "expected_base_type": "STRUCT",
                "new_name": "Actuator",
            },
        )
        assert rename_plan.structured_content["can_apply"] is True
        renamed = await client.call_tool(
            "plc_rename_data_type",
            {
                "transaction_id": transaction_id,
                "data_type_id": valve["id"],
                "expected_name": "Valve",
                "expected_base_type": "STRUCT",
                "new_name": "Actuator",
                "expected_revision": 0,
            },
        )
        assert renamed.structured_content["status"] == "staged"
        preview = await client.call_tool(
            "plc_preview_changes", {"transaction_id": transaction_id}
        )
        assert preview.structured_content["changed_entries"] == ["s/types.xml"]


@pytest.mark.parametrize(
    ("xml_payload", "expected_reason"),
    [(b"<data><DataType", "invalid_xml"), (None, "missing_member")],
)
def test_unreadable_data_type_tables_are_explicit_and_remain_unrecognized(
    project_file, tmp_path, xml_payload, expected_reason
):
    candidate = project_with_data_types(project_file, tmp_path, xml_payload)

    inventory = NativeProject(candidate).data_types()

    assert inventory["completeness"] == "partial"
    assert inventory["tables"][0]["completeness"] == "unavailable"
    assert inventory["tables"][0]["reason"] == expected_reason
    project = NativeProject(candidate)
    if expected_reason == "missing_member":
        assert project.member("types") is None
    else:
        assert "s/types.xml" in project.info()["coverage"]["unknown_entries"]
