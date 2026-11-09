from hashlib import sha256
from zipfile import ZipFile

import pytest
from lxml import etree as ET

from sysmac_mcp import workspace as workspace_module
from sysmac_mcp.iec_xml import LadderNetwork, PouSpec, VariableSpec, build_iec_project
from sysmac_mcp.native_pou import native_ladder_rungs
from sysmac_mcp.workspace import ProjectError, Workspace


def xml_bytes(*names):
    return build_iec_project(
        "Imported",
        [
            PouSpec(
                name=name,
                source="OutValue := TRUE;",
                variables=[VariableSpec(name="OutValue", data_type="BOOL")],
            )
            for name in names
        ],
        [],
    )


def linear_ladder():
    return LadderNetwork(
        nodes=[
            {"id": "left", "kind": "left_rail"},
            {"id": "start", "kind": "contact_no", "variable": "Start"},
            {"id": "blocked", "kind": "contact_nc", "variable": "Blocked"},
            {"id": "coil", "kind": "coil", "variable": "Run"},
            {"id": "right", "kind": "right_rail"},
        ],
        connections=[
            ("left", "start"),
            ("start", "blocked"),
            ("blocked", "coil"),
            ("coil", "right"),
        ],
    )


def project_with_ld_template(project_file, tmp_path):
    with ZipFile(project_file) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    oem = ET.fromstring(members["s/s.oem"])
    children = oem.find('.//Entity[@id="ldp"]/ChildEntities')
    ET.SubElement(
        children,
        "Entity",
        type="Variables",
        subtype="Ladder",
        id="ld-vars",
        name="Variables",
    )
    members["s/s.oem"] = ET.tostring(oem)
    members["s/ld-vars.xml"] = b"[SLWD version=1.0]\r\n_EN=Variables\r\n+GN=VAR\tGVT=DefaultGroup\r\n"
    result = tmp_path / "ld-template.smc2"
    with ZipFile(result, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return result


def project_with_data_type_table(
    project_file, tmp_path, table_ids=("types",), incomplete_table_ids=()
):
    with ZipFile(project_file) as source:
        members = {item.filename: source.read(item) for item in source.infolist()}

    oem = ET.fromstring(members["s/s.oem"])
    controller = oem.find('.//Entity[@type="Device"][@id="cpu"]')
    for table_id in table_ids:
        table = ET.SubElement(
            controller.find("ChildEntities"),
            "Entity",
            type="DataType",
            subtype="",
            id=table_id,
            name=f"Types {table_id}",
        )
        ET.SubElement(table, "ChildEntities")
        members[f"s/{table_id}.xml"] = b"<data" if table_id in incomplete_table_ids else (
            b'<data Version="1"><DataType Name="Data Types" BaseType="" Order="0" />'
            b"</data>"
        )
    members["s/s.oem"] = ET.tostring(oem, encoding="utf-8")

    result = tmp_path / "with-data-type-table.smc2"
    with ZipFile(result, "w") as target:
        for name, content in members.items():
            target.writestr(name, content)
    return result


def data_types_xml_bytes(*, include_global=False, include_array_usage=False):
    namespace_uri = "www.iec.ch/public/TC65SC65BWG7TF10"
    xsi_uri = "http://www.w3.org/2001/XMLSchema-instance"
    data = build_iec_project(
        "Imported types",
        [
            PouSpec(
                name="TypedMain",
                source="StatusValue.Ready := TRUE;",
                variables=[
                    VariableSpec(name="StatusValue", data_type="MotorStatus"),
                    *(
                        [VariableSpec(name="SampleValues", data_type="Samples")]
                        if include_array_usage
                        else []
                    ),
                ],
            )
        ],
        [
            VariableSpec(
                name="MCPImportedGlobal20261108",
                data_type="MotorStatus",
                scope="global",
            ),
            *(
                [
                    VariableSpec(
                        name="MCPImportedArray20261108",
                        data_type="Samples",
                        scope="global",
                    )
                ]
                if include_array_usage
                else []
            ),
        ]
        if include_global
        else [],
    )
    root = ET.fromstring(data)
    namespace = root.find(f"{{{namespace_uri}}}Types/{{{namespace_uri}}}GlobalNamespace")

    structure = ET.SubElement(namespace, f"{{{namespace_uri}}}DataTypeDecl", name="MotorStatus")
    docs = ET.SubElement(structure, f"{{{namespace_uri}}}Documentation")
    docs.set(f"{{{xsi_uri}}}type", "SimpleText")
    docs.text = "Motor status"
    struct_spec = ET.SubElement(structure, f"{{{namespace_uri}}}UserDefinedTypeSpec")
    struct_spec.set(f"{{{xsi_uri}}}type", "StructTypeSpec")
    ready = ET.SubElement(struct_spec, f"{{{namespace_uri}}}Member", name="Ready")
    ready_type = ET.SubElement(ready, f"{{{namespace_uri}}}Type")
    ET.SubElement(ready_type, f"{{{namespace_uri}}}TypeName").text = "BOOL"
    if include_array_usage:
        samples = ET.SubElement(
            struct_spec, f"{{{namespace_uri}}}Member", name="Samples"
        )
        samples_type = ET.SubElement(samples, f"{{{namespace_uri}}}Type")
        ET.SubElement(samples_type, f"{{{namespace_uri}}}TypeName").text = "Samples"

    enumeration = ET.SubElement(namespace, f"{{{namespace_uri}}}DataTypeDecl", name="RunMode")
    enum_spec = ET.SubElement(enumeration, f"{{{namespace_uri}}}UserDefinedTypeSpec")
    enum_spec.set(f"{{{xsi_uri}}}type", "EnumTypeWithNamedValueSpec")
    ET.SubElement(enum_spec, f"{{{namespace_uri}}}Enumerator", name="Stopped", value="0")
    ET.SubElement(enum_spec, f"{{{namespace_uri}}}Enumerator", name="Running", value="1")
    ET.SubElement(enum_spec, f"{{{namespace_uri}}}BaseType").text = "DINT"

    array = ET.SubElement(namespace, f"{{{namespace_uri}}}DataTypeDecl", name="Samples")
    array_spec = ET.SubElement(array, f"{{{namespace_uri}}}UserDefinedTypeSpec")
    array_spec.set(f"{{{xsi_uri}}}type", "ArrayTypeSpec")
    base_type = ET.SubElement(array_spec, f"{{{namespace_uri}}}BaseType")
    ET.SubElement(base_type, f"{{{namespace_uri}}}TypeName").text = "INT"
    dimension = ET.SubElement(
        array_spec, f"{{{namespace_uri}}}DimensionSpec", dimensionNumber="1"
    )
    ET.SubElement(dimension, f"{{{namespace_uri}}}IndexRange", lower="-1", upper="2")
    return ET.tostring(root)


def namespaced_data_types_xml_bytes(namespace_name="MCPProof20261108", *, include_global=False):
    namespace_uri = "www.iec.ch/public/TC65SC65BWG7TF10"
    root = ET.fromstring(data_types_xml_bytes(include_global=include_global))
    global_namespace = root.find(f"{{{namespace_uri}}}Types/{{{namespace_uri}}}GlobalNamespace")
    namespace = ET.SubElement(
        global_namespace,
        f"{{{namespace_uri}}}NamespaceDecl",
        name=namespace_name,
    )
    for declaration in list(global_namespace.findall(f"{{{namespace_uri}}}DataTypeDecl")):
        global_namespace.remove(declaration)
        namespace.append(declaration)

    motor_status = namespace.find(f"{{{namespace_uri}}}DataTypeDecl[@name='MotorStatus']")
    member = ET.SubElement(
        motor_status.find(f"{{{namespace_uri}}}UserDefinedTypeSpec"),
        f"{{{namespace_uri}}}Member",
        name="Mode",
    )
    member_type = ET.SubElement(member, f"{{{namespace_uri}}}Type")
    ET.SubElement(member_type, f"{{{namespace_uri}}}TypeName").text = (
        f"{namespace_name}\\RunMode"
    )
    for type_name in root.iter(f"{{{namespace_uri}}}TypeName"):
        if type_name.text == "MotorStatus":
            type_name.text = f"{namespace_name}\\MotorStatus"
        elif type_name.text == "INT":
            type_name.text = f"{namespace_name}\\RunMode"
    return ET.tostring(root)


def ladder_xml_bytes(network=None, networks=None):
    return build_iec_project(
        "Imported",
        [
            PouSpec(
                name="ImportedLD",
                language="LD",
                network=(network or linear_ladder()) if networks is None else None,
                networks=networks,
                variables=[
                    VariableSpec(name="Start", data_type="BOOL"),
                    VariableSpec(name="Blocked", data_type="BOOL"),
                    VariableSpec(name="Run", data_type="BOOL"),
                    VariableSpec(name="Run2", data_type="BOOL"),
                ],
            )
        ],
        [],
    )


def global_xml_bytes(global_name="ImportedGlobal"):
    return build_iec_project(
        "Imported",
        [PouSpec(name="UsesImportedGlobal", source=f"Run := {global_name};")],
        [
            VariableSpec(
                name=global_name,
                data_type="BOOL",
                initial_value="FALSE",
                retained=True,
                constant=True,
                comment="imported global",
            )
        ],
    )


def test_native_ladder_conversion_matches_observed_linear_rung():
    assert native_ladder_rungs(linear_ladder()) == [
        {
            "CLs": [
                {"__type": "LD", "Var": "Start"},
                {"__type": "LD", "Not": True, "Ix": 1, "Var": "Blocked", "X": 1},
                {"__type": "ST", "Ix": 2, "Var": "Run", "X": 2},
            ],
            "LRI": 3,
            "RRI": 4,
            "VLs": [],
        }
    ]


def test_native_ladder_conversion_matches_observed_parallel_contacts():
    parallel = LadderNetwork(
        nodes=[
            {"id": "left", "kind": "left_rail"},
            {"id": "start", "kind": "contact_no", "variable": "Start"},
            {"id": "blocked", "kind": "contact_nc", "variable": "Blocked"},
            {"id": "coil", "kind": "coil", "variable": "Run"},
            {"id": "right", "kind": "right_rail"},
        ],
        connections=[
            ("left", "start"),
            ("left", "blocked"),
            ("start", "coil"),
            ("blocked", "coil"),
            ("coil", "right"),
        ],
    )

    assert native_ladder_rungs(parallel) == [
        {
            "CLs": [
                {"__type": "LD", "Var": "Start"},
                {"__type": "LD", "Not": True, "Ix": 1, "Var": "Blocked", "Y": 1},
                {"__type": "ST", "Ix": 2, "Var": "Run", "X": 1},
            ],
            "LRI": 3,
            "RRI": 4,
            "VLs": [{"Ix": 5, "X": 1}],
        }
    ]


def test_native_ladder_conversion_pads_short_parallel_path_with_horizontal_link():
    parallel = LadderNetwork(
        nodes=[
            {"id": "left", "kind": "left_rail"},
            {"id": "top", "kind": "contact_nc", "variable": "Stop"},
            {"id": "lower", "kind": "contact_no", "variable": "Start"},
            {"id": "lower-next", "kind": "contact_no", "variable": "Permit"},
            {"id": "coil", "kind": "coil", "variable": "Run"},
            {"id": "right", "kind": "right_rail"},
        ],
        connections=[
            ("left", "top"),
            ("left", "lower"),
            ("top", "coil"),
            ("lower", "lower-next"),
            ("lower-next", "coil"),
            ("coil", "right"),
        ],
    )

    assert native_ladder_rungs(parallel) == [
        {
            "CLs": [
                {"__type": "LD", "Not": True, "Var": "Stop"},
                {"__type": "LD", "Ix": 1, "Var": "Start", "Y": 1},
                {"__type": "LD", "Ix": 2, "Var": "Permit", "X": 1, "Y": 1},
                {"__type": "HL", "X": 1},
                {"__type": "ST", "Ix": 3, "Var": "Run", "X": 2},
            ],
            "LRI": 4,
            "RRI": 5,
            "VLs": [{"Ix": 6, "X": 2}],
        }
    ]


def test_native_ladder_conversion_adds_split_and_join_connectors():
    parallel = LadderNetwork(
        nodes=[
            {"id": "left", "kind": "left_rail"},
            {"id": "guard", "kind": "contact_no", "variable": "Safety"},
            {"id": "top", "kind": "contact_no", "variable": "Auto"},
            {"id": "lower", "kind": "contact_no", "variable": "Manual"},
            {"id": "coil", "kind": "coil", "variable": "Run"},
            {"id": "right", "kind": "right_rail"},
        ],
        connections=[
            ("left", "guard"),
            ("guard", "top"),
            ("guard", "lower"),
            ("top", "coil"),
            ("lower", "coil"),
            ("coil", "right"),
        ],
    )

    assert native_ladder_rungs(parallel) == [
        {
            "CLs": [
                {"__type": "LD", "Var": "Safety"},
                {"__type": "LD", "Ix": 1, "Var": "Auto", "X": 1},
                {"__type": "LD", "Ix": 2, "Var": "Manual", "X": 1, "Y": 1},
                {"__type": "ST", "Ix": 3, "Var": "Run", "X": 2},
            ],
            "LRI": 4,
            "RRI": 5,
            "VLs": [{"Ix": 6, "X": 1}, {"Ix": 7, "X": 2}],
        }
    ]


def test_native_ladder_conversion_rejects_nested_branches_and_negated_coils():
    nested = LadderNetwork(
        nodes=[
            {"id": "left", "kind": "left_rail"},
            {"id": "common", "kind": "contact_no", "variable": "Enable"},
            {"id": "upper", "kind": "contact_no", "variable": "Upper"},
            {"id": "lower", "kind": "contact_no", "variable": "Lower"},
            {"id": "upper-a", "kind": "contact_no", "variable": "UpperA"},
            {"id": "upper-b", "kind": "contact_no", "variable": "UpperB"},
            {"id": "coil", "kind": "coil", "variable": "Run"},
            {"id": "right", "kind": "right_rail"},
        ],
        connections=[
            ("left", "common"),
            ("common", "upper"),
            ("common", "lower"),
            ("upper", "upper-a"),
            ("upper", "upper-b"),
            ("lower", "coil"),
            ("upper-a", "coil"),
            ("upper-b", "coil"),
            ("coil", "right"),
        ],
    )
    negated_coil = LadderNetwork(
        nodes=[
            {"id": "left", "kind": "left_rail"},
            {"id": "start", "kind": "contact_no", "variable": "Start"},
            {"id": "coil", "kind": "coil_negated", "variable": "Run"},
            {"id": "right", "kind": "right_rail"},
        ],
        connections=[("left", "start"), ("start", "coil"), ("coil", "right")],
    )

    with pytest.raises(ValueError, match="single split and join"):
        native_ladder_rungs(nested)
    with pytest.raises(ValueError, match="Negated coils"):
        native_ladder_rungs(negated_coil)


def test_iec_import_stages_linear_ladder_through_language_specific_template(
    project_file, tmp_path
):
    template_project = project_with_ld_template(project_file, tmp_path)
    workspace = Workspace(tmp_path / "workspace")
    opened = workspace.open_project(template_project)
    tx = workspace.begin(opened["project_id"])["transaction_id"]
    xml_path = tmp_path / "ladder.xml"
    data = ladder_xml_bytes()
    xml_path.write_bytes(data)

    plan = workspace.preview_iec_xml_import(
        tx, xml_path, {"Program:LD": "ldp"}, expected_revision=opened["revision"]
    )
    assert plan["status"] == "ready"
    assert plan["pous"][0]["language"] == "LD"
    staged = workspace.stage_iec_xml_import(
        tx,
        xml_path,
        {"Program:LD": "ldp"},
        plan["source_sha256"],
        plan["plan_id"],
        expected_revision=opened["revision"],
    )
    assert staged["status"] == "staged"

    workspace.commit(tx)
    project = workspace.project(opened["project_id"])
    imported = next(pou for pou in project.pous() if pou["name"] == "ImportedLD")
    assert project.read_pou(imported["id"])["sections"][0]["rungs"] == [
        {
            "CLs": [
                {"__type": "LD", "Var": "Start"},
                {"__type": "LD", "Not": True, "Ix": 1, "Var": "Blocked", "X": 1},
                {"__type": "ST", "Ix": 2, "Var": "Run", "X": 2},
            ],
            "LRI": 3,
            "RRI": 4,
            "VLs": [],
        }
    ]


def test_iec_import_requires_a_language_specific_ld_template(project_file, tmp_path):
    template_project = project_with_ld_template(project_file, tmp_path)
    workspace = Workspace(tmp_path / "workspace")
    opened = workspace.open_project(template_project)
    tx = workspace.begin(opened["project_id"])["transaction_id"]
    xml_path = tmp_path / "ladder.xml"
    xml_path.write_bytes(ladder_xml_bytes())

    plan = workspace.preview_iec_xml_import(
        tx, xml_path, {"Program": "p"}, expected_revision=opened["revision"]
    )

    assert plan["status"] == "blocked"
    assert "Program:LD" in plan["blockers"][0]["message"]
    assert workspace.preview(tx)["changes"] == []


def test_iec_import_stages_observed_parallel_ladder(project_file, tmp_path):
    template_project = project_with_ld_template(project_file, tmp_path)
    workspace = Workspace(tmp_path / "workspace")
    opened = workspace.open_project(template_project)
    tx = workspace.begin(opened["project_id"])["transaction_id"]
    xml_path = tmp_path / "parallel.xml"
    branched = LadderNetwork(
        nodes=[
            {"id": "left", "kind": "left_rail"},
            {"id": "a", "kind": "contact_no", "variable": "Start"},
            {"id": "b", "kind": "contact_nc", "variable": "Blocked"},
            {"id": "coil", "kind": "coil", "variable": "Run"},
            {"id": "right", "kind": "right_rail"},
        ],
        connections=[
            ("left", "a"),
            ("left", "b"),
            ("a", "coil"),
            ("b", "coil"),
            ("coil", "right"),
        ],
    )
    xml_path.write_bytes(ladder_xml_bytes(branched))

    plan = workspace.preview_iec_xml_import(
        tx, xml_path, {"Program:LD": "ldp"}, expected_revision=opened["revision"]
    )

    assert plan["status"] == "ready"
    staged = workspace.stage_iec_xml_import(
        tx,
        xml_path,
        {"Program:LD": "ldp"},
        plan["source_sha256"],
        plan["plan_id"],
        expected_revision=opened["revision"],
    )
    assert staged["status"] == "staged"
    workspace.commit(tx)
    project = workspace.project(opened["project_id"])
    imported = next(pou for pou in project.pous() if pou["name"] == "ImportedLD")
    assert project.read_pou(imported["id"])["sections"][0]["rungs"] == [
        {
            "CLs": [
                {"__type": "LD", "Var": "Start"},
                {"__type": "LD", "Not": True, "Ix": 1, "Var": "Blocked", "Y": 1},
                {"__type": "ST", "Ix": 2, "Var": "Run", "X": 1},
            ],
            "LRI": 3,
            "RRI": 4,
            "VLs": [{"Ix": 5, "X": 1}],
        }
    ]


def test_iec_import_stages_multiple_ld_rungs_in_evaluation_order(project_file, tmp_path):
    template_project = project_with_ld_template(project_file, tmp_path)
    workspace = Workspace(tmp_path / "workspace")
    opened = workspace.open_project(template_project)
    tx = workspace.begin(opened["project_id"])["transaction_id"]
    xml_path = tmp_path / "multiple-ladder.xml"
    first = linear_ladder()
    second = LadderNetwork(
        nodes=[
            {"id": "left", "kind": "left_rail"},
            {"id": "contact", "kind": "contact_no", "variable": "Start"},
            {"id": "coil", "kind": "coil", "variable": "Run2"},
            {"id": "right", "kind": "right_rail"},
        ],
        connections=[("left", "contact"), ("contact", "coil"), ("coil", "right")],
    )
    xml_path.write_bytes(ladder_xml_bytes(networks=[first, second]))

    plan = workspace.preview_iec_xml_import(
        tx, xml_path, {"Program:LD": "ldp"}, expected_revision=opened["revision"]
    )
    assert plan["status"] == "ready"
    staged = workspace.stage_iec_xml_import(
        tx,
        xml_path,
        {"Program:LD": "ldp"},
        plan["source_sha256"],
        plan["plan_id"],
        expected_revision=opened["revision"],
    )
    assert staged["status"] == "staged"
    workspace.commit(tx)

    project = workspace.project(opened["project_id"])
    imported = next(pou for pou in project.pous() if pou["name"] == "ImportedLD")
    assert project.read_pou(imported["id"])["sections"][0]["rungs"] == [
        *native_ladder_rungs(first),
        *native_ladder_rungs(second),
    ]


def test_iec_import_preview_then_stage_is_atomic_and_preserves_original(project_file, tmp_path):
    workspace = Workspace(tmp_path / "workspace")
    opened = workspace.open_project(project_file)
    transaction = workspace.begin(opened["project_id"])
    xml_path = tmp_path / "import.xml"
    data = xml_bytes("ImportedA", "ImportedB")
    xml_path.write_bytes(data)

    plan = workspace.preview_iec_xml_import(
        transaction["transaction_id"],
        xml_path,
        {"Program": "p"},
        expected_revision=opened["revision"],
    )

    assert plan["status"] == "ready"
    assert plan["source_sha256"] == sha256(data).hexdigest()
    assert [p["name"] for p in plan["pous"]] == ["ImportedA", "ImportedB"]
    assert workspace.preview(transaction["transaction_id"])["changes"] == []

    staged = workspace.stage_iec_xml_import(
        transaction["transaction_id"],
        xml_path,
        {"Program": "p"},
        plan["source_sha256"],
        plan["plan_id"],
        expected_revision=opened["revision"],
    )
    assert staged["status"] == "staged"
    preview = workspace.preview(transaction["transaction_id"])
    assert len(preview["changes"]) == 1
    assert preview["changes"][0]["operation"] == "import_iec_xml_pous"
    assert {item["name"] for item in preview["changes"][0]["pous"]} == {
        "ImportedA",
        "ImportedB",
    }

    workspace.commit(transaction["transaction_id"])
    assert {p["name"] for p in workspace.project(opened["project_id"]).pous()} >= {
        "ImportedA",
        "ImportedB",
    }
    assert sha256(project_file.read_bytes()).hexdigest() == opened["source_sha256"]


def test_iec_import_stage_rejects_changed_source_without_staging(project_file, tmp_path):
    workspace = Workspace(tmp_path / "workspace")
    opened = workspace.open_project(project_file)
    transaction = workspace.begin(opened["project_id"])
    xml_path = tmp_path / "import.xml"
    xml_path.write_bytes(xml_bytes("Imported"))
    plan = workspace.preview_iec_xml_import(
        transaction["transaction_id"], xml_path, {"Program": "p"}, expected_revision=0
    )
    xml_path.write_bytes(b"changed source is not valid XML")

    with pytest.raises(ProjectError, match="source changed"):
        workspace.stage_iec_xml_import(
            transaction["transaction_id"],
            xml_path,
            {"Program": "p"},
            plan["source_sha256"],
            plan["plan_id"],
            expected_revision=0,
        )
    assert workspace.preview(transaction["transaction_id"])["changes"] == []


def test_iec_import_stages_global_variables_with_new_external_aliases(project_file, tmp_path):
    workspace = Workspace(tmp_path / "workspace")
    opened = workspace.open_project(project_file)
    tx = workspace.begin(opened["project_id"])["transaction_id"]
    xml_path = tmp_path / "globals.xml"
    xml_path.write_bytes(global_xml_bytes())

    plan = workspace.preview_iec_xml_import(
        tx, xml_path, {"Program": "p"}, expected_revision=opened["revision"]
    )

    assert plan["status"] == "ready"
    assert plan["global_variables"] == [{"name": "ImportedGlobal", "data_type": "BOOL"}]
    staged = workspace.stage_iec_xml_import(
        tx,
        xml_path,
        {"Program": "p"},
        plan["source_sha256"],
        plan["plan_id"],
        expected_revision=opened["revision"],
    )
    assert staged["status"] == "staged"

    workspace.commit(tx)
    project = workspace.project(opened["project_id"])
    global_variable = next(
        variable for variable in project.variables("global") if variable["name"] == "ImportedGlobal"
    )
    assert global_variable["initial_value"] == "FALSE"
    assert global_variable["retained"] is True
    assert global_variable["attributes"]["Const"] == "1"
    assert global_variable["attributes"]["Com"] == "imported global"
    pou = next(pou for pou in project.pous() if pou["name"] == "UsesImportedGlobal")
    alias = next(
        variable
        for variable in project.variables(pou["id"])
        if variable["name"] == "ImportedGlobal"
    )
    assert alias["group"] == "VAR_EXTERNAL"
    from sysmac_mcp.symbols import SymbolIndex

    assert SymbolIndex(project).aliases[alias["id"]] == global_variable["id"]


def test_iec_import_stages_global_only_document_with_one_controller(project_file, tmp_path):
    workspace = Workspace(tmp_path / "workspace")
    opened = workspace.open_project(project_file)
    tx = workspace.begin(opened["project_id"])["transaction_id"]
    xml_path = tmp_path / "global-only.xml"
    xml_path.write_bytes(
        build_iec_project(
            "Imported globals only",
            [],
            [VariableSpec(name="ImportedOnly", data_type="DINT", initial_value="12")],
        )
    )

    plan = workspace.preview_iec_xml_import(
        tx, xml_path, {}, expected_revision=opened["revision"]
    )

    assert plan["status"] == "ready"
    assert plan["pous"] == []
    assert plan["global_variables"] == [{"name": "ImportedOnly", "data_type": "DINT"}]
    staged = workspace.stage_iec_xml_import(
        tx,
        xml_path,
        {},
        plan["source_sha256"],
        plan["plan_id"],
        expected_revision=opened["revision"],
    )
    assert staged["status"] == "staged"

    workspace.commit(tx)
    project = workspace.project(opened["project_id"])
    imported = next(
        variable for variable in project.variables("global") if variable["name"] == "ImportedOnly"
    )
    assert imported["data_type"] == "DINT"
    assert imported["initial_value"] == "12"


def test_iec_import_blocks_global_only_document_with_multiple_controllers(project_file, tmp_path):
    with ZipFile(project_file) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    oem = ET.fromstring(members["s/s.oem"])
    solution = oem.find("./Entity")
    children = solution.find("ChildEntities")
    second_controller = ET.SubElement(
        children, "Entity", type="Device", id="cpu2", name="Controller2"
    )
    ET.SubElement(second_controller, "ChildEntities")
    members["s/s.oem"] = ET.tostring(oem)
    multi_controller = tmp_path / "multi-controller.smc2"
    with ZipFile(multi_controller, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)

    workspace = Workspace(tmp_path / "workspace")
    opened = workspace.open_project(multi_controller)
    tx = workspace.begin(opened["project_id"])["transaction_id"]
    xml_path = tmp_path / "global-only.xml"
    xml_path.write_bytes(
        build_iec_project(
            "Imported globals only",
            [],
            [VariableSpec(name="ImportedOnly", data_type="DINT")],
        )
    )

    plan = workspace.preview_iec_xml_import(
        tx, xml_path, {}, expected_revision=opened["revision"]
    )

    assert plan["status"] == "blocked"
    assert "exactly one controller" in plan["blockers"][0]["message"]
    assert workspace.preview(tx)["changes"] == []


def test_iec_import_blocks_global_name_conflict_without_staging(project_file, tmp_path):
    workspace = Workspace(tmp_path / "workspace")
    opened = workspace.open_project(project_file)
    tx = workspace.begin(opened["project_id"])["transaction_id"]
    xml_path = tmp_path / "globals.xml"
    xml_path.write_bytes(global_xml_bytes("Start"))

    plan = workspace.preview_iec_xml_import(
        tx, xml_path, {"Program": "p"}, expected_revision=opened["revision"]
    )

    assert plan["status"] == "blocked"
    assert "already exists" in plan["blockers"][0]["message"]
    assert workspace.preview(tx)["changes"] == []


def test_iec_import_blocks_type_name_missing_from_controller(project_file, tmp_path):
    workspace = Workspace(tmp_path / "workspace")
    opened = workspace.open_project(project_file)
    tx = workspace.begin(opened["project_id"])["transaction_id"]
    xml_path = tmp_path / "unknown-type.xml"
    xml_path.write_bytes(
        build_iec_project(
            "Unknown type",
            [
                PouSpec(
                    name="UsesMissingType",
                    source="Value := Value;",
                    variables=[VariableSpec(name="Value", data_type="MissingType")],
                )
            ],
            [],
        )
    )

    plan = workspace.preview_iec_xml_import(
        tx, xml_path, {"Program": "p"}, expected_revision=opened["revision"]
    )

    assert plan["status"] == "blocked"
    assert "MissingType" in plan["blockers"][0]["message"]
    assert "not present" in plan["blockers"][0]["message"]
    assert workspace.preview(tx)["changes"] == []


def test_iec_import_allows_existing_controller_data_type(project_file, tmp_path):
    with ZipFile(project_file) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    oem = ET.fromstring(members["s/s.oem"])
    cpu = oem.find('.//Entity[@id="cpu"]')
    ET.SubElement(
        cpu.find("ChildEntities"),
        "Entity",
        type="DataType",
        id="custom-type",
        name="ExistingType",
    )
    members["s/s.oem"] = ET.tostring(oem)
    members["s/custom-type.xml"] = (
        b'<data><DataType Name="Types" BaseType="">'
        b'<DataType Name="ExistingType" BaseType="DINT"/></DataType></data>'
    )
    typed_project = tmp_path / "typed.smc2"
    with ZipFile(typed_project, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)

    workspace = Workspace(tmp_path / "workspace")
    opened = workspace.open_project(typed_project)
    tx = workspace.begin(opened["project_id"])["transaction_id"]
    xml_path = tmp_path / "existing-type.xml"
    xml_path.write_bytes(
        build_iec_project(
            "Existing type",
            [
                PouSpec(
                    name="UsesExistingType",
                    source="Value := Value;",
                    variables=[VariableSpec(name="Value", data_type="ExistingType")],
                )
            ],
            [],
        )
    )

    plan = workspace.preview_iec_xml_import(
        tx, xml_path, {"Program": "p"}, expected_revision=opened["revision"]
    )

    assert plan["status"] == "ready"
    assert workspace.preview(tx)["changes"] == []


def test_iec_import_stages_struct_enum_and_array_alias_atomically(project_file, tmp_path):
    source = project_with_data_type_table(project_file, tmp_path)
    workspace = Workspace(tmp_path / "workspace")
    opened = workspace.open_project(source)
    transaction = workspace.begin(opened["project_id"])["transaction_id"]
    xml_path = tmp_path / "types.xml"
    xml_path.write_bytes(data_types_xml_bytes())

    plan = workspace.preview_iec_xml_import(
        transaction,
        xml_path,
        {"Program": "p"},
        expected_revision=opened["revision"],
        data_type_table_id="types",
    )

    assert plan["status"] == "ready"
    assert [item["name"] for item in plan["data_types"]] == [
        "MotorStatus",
        "RunMode",
        "Samples",
    ]
    assert "s/types.xml" in plan["predicted_changed_members"]
    staged = workspace.stage_iec_xml_import(
        transaction,
        xml_path,
        {"Program": "p"},
        plan["source_sha256"],
        plan["plan_id"],
        expected_revision=opened["revision"],
        data_type_table_id="types",
    )
    assert staged["status"] == "staged"
    workspace.commit(transaction)

    project = workspace.project(opened["project_id"])
    types = project.data_types(controller_id="cpu")
    assert {item["name"] for item in types["items"]} == {"MotorStatus", "RunMode"}
    motor_status = next(item for item in types["items"] if item["name"] == "MotorStatus")
    assert motor_status["base_type"] == "STRUCT"
    assert motor_status["comment"] == "Motor status"
    assert motor_status["offset_type"] == "NJ"
    assert [item["name"] for item in project.data_types(parent_id=motor_status["id"])["items"]] == [
        "Ready"
    ]
    run_mode = next(item for item in types["items"] if item["name"] == "RunMode")
    assert [item["enum_value"] for item in project.data_types(parent_id=run_mode["id"])["items"]] == [
        "0",
        "1",
    ]


def test_iec_import_uses_automatic_nj_layout_for_struct_members(project_file, tmp_path):
    source = project_with_data_type_table(project_file, tmp_path)
    workspace = Workspace(tmp_path / "workspace")
    opened = workspace.open_project(source)
    transaction = workspace.begin(opened["project_id"])["transaction_id"]
    root = ET.fromstring(data_types_xml_bytes())
    namespace_uri = "www.iec.ch/public/TC65SC65BWG7TF10"
    structure = root.find(
        f"{{{namespace_uri}}}Types/{{{namespace_uri}}}GlobalNamespace/{{{namespace_uri}}}DataTypeDecl[@name='MotorStatus']"
    )
    struct_spec = structure.find(f"{{{namespace_uri}}}UserDefinedTypeSpec")
    mode = ET.Element(f"{{{namespace_uri}}}Member", name="Mode")
    mode_type = ET.SubElement(mode, f"{{{namespace_uri}}}Type")
    ET.SubElement(mode_type, f"{{{namespace_uri}}}TypeName").text = "RunMode"
    struct_spec.insert(0, mode)
    xml_path = tmp_path / "member-offsets.xml"
    xml_path.write_bytes(ET.tostring(root))

    plan = workspace.preview_iec_xml_import(
        transaction,
        xml_path,
        {"Program": "p"},
        expected_revision=opened["revision"],
        data_type_table_id="types",
    )
    staged = workspace.stage_iec_xml_import(
        transaction,
        xml_path,
        {"Program": "p"},
        plan["source_sha256"],
        plan["plan_id"],
        expected_revision=opened["revision"],
        data_type_table_id="types",
    )
    assert staged["status"] == "staged"
    workspace.commit(transaction)

    project = workspace.project(opened["project_id"])
    motor_status = next(
        item
        for item in project.data_types(controller_id="cpu")["items"]
        if item["name"] == "MotorStatus"
    )
    members = project.data_types(parent_id=motor_status["id"])["items"]
    assert motor_status["offset_type"] == "NJ"
    assert [(item["name"], item["offset_channel"], item["offset_bit"]) for item in members] == [
        ("Mode", "", ""),
        ("Ready", "", ""),
    ]


def test_iec_import_materializes_array_aliases_inline(project_file, tmp_path):
    source = project_with_data_type_table(project_file, tmp_path)
    workspace = Workspace(tmp_path / "workspace")
    opened = workspace.open_project(source)
    transaction = workspace.begin(opened["project_id"])["transaction_id"]
    xml_path = tmp_path / "array-alias.xml"
    xml_path.write_bytes(
        data_types_xml_bytes(include_global=True, include_array_usage=True)
    )

    plan = workspace.preview_iec_xml_import(
        transaction,
        xml_path,
        {"Program": "p"},
        expected_revision=opened["revision"],
        data_type_table_id="types",
    )

    assert plan["status"] == "ready"
    staged = workspace.stage_iec_xml_import(
        transaction,
        xml_path,
        {"Program": "p"},
        plan["source_sha256"],
        plan["plan_id"],
        expected_revision=opened["revision"],
        data_type_table_id="types",
    )
    assert staged["status"] == "staged"
    workspace.commit(transaction)

    project = workspace.project(opened["project_id"])
    types = project.data_types(controller_id="cpu")["items"]
    assert "Samples" not in {item["name"] for item in types}
    motor_status = next(item for item in types if item["name"] == "MotorStatus")
    samples_member = next(
        item
        for item in project.data_types(parent_id=motor_status["id"])["items"]
        if item["name"] == "Samples"
    )
    assert samples_member["base_type"] == "ARRAY[-1..2] OF INT"

    imported_global = next(
        item
        for item in project.variables("global")
        if item["name"] == "MCPImportedArray20261108"
    )
    assert imported_global["data_type"] == "ARRAY[-1..2] OF INT"
    typed_main = next(item for item in project.pous() if item["name"] == "TypedMain")
    imported_local = next(
        item
        for item in project.read_pou(typed_main["id"])["variables"]
        if item["name"] == "SampleValues"
    )
    assert imported_local["data_type"] == "ARRAY[-1..2] OF INT"


def test_iec_import_flattens_namespace_types_to_native_controller_scope(project_file, tmp_path):
    source = project_with_data_type_table(project_file, tmp_path)
    workspace = Workspace(tmp_path / "workspace")
    opened = workspace.open_project(source)
    transaction = workspace.begin(opened["project_id"])["transaction_id"]
    xml_path = tmp_path / "namespaced-types.xml"
    xml_path.write_bytes(namespaced_data_types_xml_bytes(include_global=True))

    plan = workspace.preview_iec_xml_import(
        transaction,
        xml_path,
        {"Program": "p"},
        expected_revision=opened["revision"],
        data_type_table_id="types",
    )

    assert plan["status"] == "ready"
    assert [item["name"] for item in plan["data_types"]] == [
        "MotorStatus",
        "RunMode",
        "Samples",
    ]
    staged = workspace.stage_iec_xml_import(
        transaction,
        xml_path,
        {"Program": "p"},
        plan["source_sha256"],
        plan["plan_id"],
        expected_revision=opened["revision"],
        data_type_table_id="types",
    )
    assert staged["status"] == "staged"
    workspace.commit(transaction)

    project = workspace.project(opened["project_id"])
    types = project.data_types(controller_id="cpu")["items"]
    assert all("\\" not in item["name"] for item in types)
    motor_status = next(item for item in types if item["name"] == "MotorStatus")
    assert motor_status["offset_type"] == "NJ"
    mode_member = next(
        item
        for item in project.data_types(parent_id=motor_status["id"])["items"]
        if item["name"] == "Mode"
    )
    assert mode_member["base_type"] == "RunMode"
    typed_main = next(item for item in project.pous() if item["name"] == "TypedMain")
    assert project.read_pou(typed_main["id"])["variables"][0]["data_type"] == "MotorStatus"
    assert next(
        item
        for item in project.variables("global")
        if item["name"] == "MCPImportedGlobal20261108"
    )["data_type"] == "MotorStatus"
    assert "Samples" not in {item["name"] for item in types}
    samples = next(item for item in plan["data_types"] if item["name"] == "Samples")
    assert samples["base_type"] == "ARRAY[-1..2] OF RunMode"
    assert samples["native_representation"] == "inline_array"


def test_iec_import_blocks_duplicate_flattened_namespace_type_names(project_file, tmp_path):
    source = project_with_data_type_table(project_file, tmp_path)
    workspace = Workspace(tmp_path / "workspace")
    opened = workspace.open_project(source)
    transaction = workspace.begin(opened["project_id"])["transaction_id"]
    root = ET.fromstring(namespaced_data_types_xml_bytes())
    namespace_uri = "www.iec.ch/public/TC65SC65BWG7TF10"
    global_namespace = root.find(f"{{{namespace_uri}}}Types/{{{namespace_uri}}}GlobalNamespace")
    second_namespace = ET.SubElement(
        global_namespace,
        f"{{{namespace_uri}}}NamespaceDecl",
        name="AnotherNamespace",
    )
    duplicate = ET.SubElement(
        second_namespace,
        f"{{{namespace_uri}}}DataTypeDecl",
        name="MotorStatus",
    )
    spec = ET.SubElement(duplicate, f"{{{namespace_uri}}}UserDefinedTypeSpec")
    spec.set("{http://www.w3.org/2001/XMLSchema-instance}type", "StructTypeSpec")
    field = ET.SubElement(spec, f"{{{namespace_uri}}}Member", name="Active")
    field_type = ET.SubElement(field, f"{{{namespace_uri}}}Type")
    ET.SubElement(field_type, f"{{{namespace_uri}}}TypeName").text = "BOOL"
    xml_path = tmp_path / "duplicate-namespaced-types.xml"
    xml_path.write_bytes(ET.tostring(root))

    with pytest.raises(ValueError, match="duplicate data type names after flattening"):
        workspace.preview_iec_xml_import(
            transaction,
            xml_path,
            {"Program": "p"},
            expected_revision=opened["revision"],
            data_type_table_id="types",
        )
    assert workspace.preview(transaction)["changes"] == []


def test_iec_data_type_import_requires_table_id_when_multiple_tables_exist(project_file, tmp_path):
    source = project_with_data_type_table(project_file, tmp_path, table_ids=("types-a", "types-b"))
    workspace = Workspace(tmp_path / "workspace")
    opened = workspace.open_project(source)
    transaction = workspace.begin(opened["project_id"])["transaction_id"]
    xml_path = tmp_path / "types.xml"
    xml_path.write_bytes(data_types_xml_bytes())

    plan = workspace.preview_iec_xml_import(
        transaction,
        xml_path,
        {"Program": "p"},
        expected_revision=opened["revision"],
    )

    assert plan["status"] == "blocked"
    assert "data_type_table_id" in plan["blockers"][0]["message"]
    assert workspace.preview(transaction)["changes"] == []


def test_iec_data_type_table_selection_is_bound_to_preview_plan(project_file, tmp_path):
    source = project_with_data_type_table(project_file, tmp_path, table_ids=("types-a", "types-b"))
    workspace = Workspace(tmp_path / "workspace")
    opened = workspace.open_project(source)
    transaction = workspace.begin(opened["project_id"])["transaction_id"]
    xml_path = tmp_path / "types.xml"
    xml_path.write_bytes(data_types_xml_bytes())
    plan = workspace.preview_iec_xml_import(
        transaction,
        xml_path,
        {"Program": "p"},
        expected_revision=opened["revision"],
        data_type_table_id="types-a",
    )
    assert plan["status"] == "ready"

    with pytest.raises(ProjectError, match="plan changed"):
        workspace.stage_iec_xml_import(
            transaction,
            xml_path,
            {"Program": "p"},
            plan["source_sha256"],
            plan["plan_id"],
            expected_revision=opened["revision"],
            data_type_table_id="types-b",
        )
    assert workspace.preview(transaction)["changes"] == []


async def test_iec_data_type_table_selection_is_exposed_through_mcp(project_file, tmp_path):
    from mcp import Client

    from sysmac_mcp.server import create_server

    source = project_with_data_type_table(project_file, tmp_path)
    xml_path = tmp_path / "types.xml"
    xml_path.write_bytes(data_types_xml_bytes())
    async with Client(create_server(tmp_path / "workspace")) as client:
        opened = await client.call_tool("plc_open_project", {"path": str(source)})
        project_id = opened.structured_content["project_id"]
        transaction = await client.call_tool(
            "plc_begin_transaction", {"project_id": project_id}
        )
        transaction_id = transaction.structured_content["transaction_id"]
        preview = await client.call_tool(
            "plc_preview_iec_xml_import",
            {
                "transaction_id": transaction_id,
                "xml_path": str(xml_path),
                "template_pou_ids": {"Program": "p"},
                "expected_revision": 0,
                "data_type_table_id": "types",
            },
        )
        assert not preview.is_error, preview.content
        assert [item["name"] for item in preview.structured_content["data_types"]] == [
            "MotorStatus",
            "RunMode",
            "Samples",
        ]
        staged = await client.call_tool(
            "plc_stage_iec_xml_import",
            {
                "transaction_id": transaction_id,
                "xml_path": str(xml_path),
                "template_pou_ids": {"Program": "p"},
                "expected_source_sha256": preview.structured_content["source_sha256"],
                "expected_plan_id": preview.structured_content["plan_id"],
                "expected_revision": 0,
                "data_type_table_id": "types",
            },
        )
        assert not staged.is_error, staged.content
        assert staged.structured_content["change"]["data_types"][0]["kind"] == "STRUCT"


def test_iec_data_type_import_blocks_when_another_controller_table_is_incomplete(
    project_file, tmp_path
):
    source = project_with_data_type_table(
        project_file,
        tmp_path,
        table_ids=("types", "unreadable"),
        incomplete_table_ids=("unreadable",),
    )
    workspace = Workspace(tmp_path / "workspace")
    opened = workspace.open_project(source)
    transaction = workspace.begin(opened["project_id"])["transaction_id"]
    xml_path = tmp_path / "types.xml"
    xml_path.write_bytes(data_types_xml_bytes())

    plan = workspace.preview_iec_xml_import(
        transaction,
        xml_path,
        {"Program": "p"},
        expected_revision=opened["revision"],
        data_type_table_id="types",
    )

    assert plan["status"] == "blocked"
    assert "all native datatype tables" in plan["blockers"][0]["message"].casefold()
    assert workspace.preview(transaction)["changes"] == []


def test_iec_data_type_name_cannot_shadow_existing_enumerator(project_file, tmp_path):
    source = project_with_data_type_table(project_file, tmp_path)
    with ZipFile(source) as archive:
        members = {item.filename: archive.read(item) for item in archive.infolist()}
    members["s/types.xml"] = (
        b'<data Version="1"><DataType Name="Data Types" BaseType="" Order="0">'
        b'<DataType Name="ExistingMode" BaseType="ENUM" Order="1">'
        b'<DataType Name="Busy" BaseType="DINT" EnumValue="1" Order="0" />'
        b"</DataType></DataType></data>"
    )
    with ZipFile(source, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)

    xml_root = ET.fromstring(data_types_xml_bytes())
    namespace_uri = "www.iec.ch/public/TC65SC65BWG7TF10"
    xsi_uri = "http://www.w3.org/2001/XMLSchema-instance"
    namespace = xml_root.find(f"{{{namespace_uri}}}Types/{{{namespace_uri}}}GlobalNamespace")
    definition = ET.SubElement(namespace, f"{{{namespace_uri}}}DataTypeDecl", name="Busy")
    spec = ET.SubElement(definition, f"{{{namespace_uri}}}UserDefinedTypeSpec")
    spec.set(f"{{{xsi_uri}}}type", "StructTypeSpec")
    field = ET.SubElement(spec, f"{{{namespace_uri}}}Member", name="Active")
    field_type = ET.SubElement(field, f"{{{namespace_uri}}}Type")
    ET.SubElement(field_type, f"{{{namespace_uri}}}TypeName").text = "BOOL"

    workspace = Workspace(tmp_path / "workspace")
    opened = workspace.open_project(source)
    transaction = workspace.begin(opened["project_id"])["transaction_id"]
    xml_path = tmp_path / "types.xml"
    xml_path.write_bytes(ET.tostring(xml_root))
    plan = workspace.preview_iec_xml_import(
        transaction,
        xml_path,
        {"Program": "p"},
        expected_revision=opened["revision"],
        data_type_table_id="types",
    )

    assert plan["status"] == "blocked"
    assert "Busy" in plan["blockers"][0]["message"]


def test_iec_import_enforces_xml_size_limit_before_parsing(tmp_path, monkeypatch):
    xml_path = tmp_path / "oversized.xml"
    xml_path.write_bytes(b"x" * 17)
    monkeypatch.setattr(workspace_module, "IEC_IMPORT_MAX_BYTES", 16)

    with pytest.raises(ProjectError, match="8 MiB import limit"):
        workspace_module.read_iec_import(xml_path)


def test_iec_import_preview_reports_template_conflict_without_staging(project_file, tmp_path):
    workspace = Workspace(tmp_path / "workspace")
    opened = workspace.open_project(project_file)
    transaction = workspace.begin(opened["project_id"])
    xml_path = tmp_path / "import.xml"
    xml_path.write_bytes(xml_bytes("Imported", "Main"))

    plan = workspace.preview_iec_xml_import(
        transaction["transaction_id"], xml_path, {"Program": "p"}, expected_revision=0
    )

    assert plan["status"] == "blocked"
    assert "already exists" in plan["blockers"][0]["message"]
    assert workspace.preview(transaction["transaction_id"])["changes"] == []


def test_iec_import_plan_binds_existing_staging_snapshot(project_file, tmp_path):
    workspace = Workspace(tmp_path / "workspace")
    opened = workspace.open_project(project_file)
    transaction = workspace.begin(opened["project_id"])
    tx = transaction["transaction_id"]
    xml_path = tmp_path / "import.xml"
    xml_path.write_bytes(xml_bytes("Imported"))
    plan = workspace.preview_iec_xml_import(
        tx, xml_path, {"Program": "p"}, expected_revision=0
    )
    workspace.update_st(tx, "body", "Run := FALSE;", expected_revision=0)

    with pytest.raises(ProjectError, match="plan changed"):
        workspace.stage_iec_xml_import(
            tx,
            xml_path,
            {"Program": "p"},
            plan["source_sha256"],
            plan["plan_id"],
            expected_revision=0,
        )
    assert [change["operation"] for change in workspace.preview(tx)["changes"]] == [
        "update_st"
    ]
