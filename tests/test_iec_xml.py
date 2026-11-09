from pathlib import Path

import pytest
from lxml import etree as E
from pydantic import ValidationError

from sysmac_mcp.iec_xml import (
    LadderNetwork,
    PouSpec,
    VariableSpec,
    build_iec_project,
    parse_iec_xml_document,
    parse_iec_xml_pous,
    validate_iec_xml,
)

SCHEMA = Path(
    r"C:\Program Files\OMRON\Sysmac Studio\Sample\IEC 61131-10 XML\Controller\IEC61131_10_Ed1_0_Spc1_0.xsd"
)


def network():
    return LadderNetwork(
        nodes=[
            {"id": "left", "kind": "left_rail"},
            {"id": "start", "kind": "contact_no", "variable": "Start"},
            {"id": "permit", "kind": "contact_nc", "variable": "Blocked"},
            {"id": "coil", "kind": "coil", "variable": "Run"},
            {"id": "right", "kind": "right_rail"},
        ],
        connections=[
            ("left", "start"),
            ("left", "permit"),
            ("start", "coil"),
            ("permit", "coil"),
            ("coil", "right"),
        ],
    )


def series_network():
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


def test_st_generation_escapes_xml_and_preserves_source():
    source = "Run := Start AND (Counter < 5); // & < >"
    data = build_iec_project(
        "Demo",
        [
            PouSpec(
                name="Main",
                source=source,
                variables=[VariableSpec(name="Counter", data_type="INT", initial_value="0")],
            )
        ],
        [VariableSpec(name="Run", data_type="BOOL", initial_value="FALSE")],
    )
    root = E.fromstring(data)
    assert root.xpath('string(//*[local-name()="ST"])') == source
    assert len(root.xpath('//*[local-name()="GlobalVars"]/*[local-name()="Variable"]')) == 1


@pytest.mark.skipif(not SCHEMA.is_file(), reason="Local Omron XSD not installed")
@pytest.mark.parametrize("kind", ["Program", "Function", "FunctionBlock"])
def test_st_generated_document_validates_against_installed_omron_xsd(kind):
    variables = [
        VariableSpec(
            name="Input", data_type="BOOL", scope="local" if kind == "Program" else "input"
        ),
        VariableSpec(
            name="Output", data_type="BOOL", scope="local" if kind == "Program" else "output"
        ),
    ]
    pou = PouSpec(
        name="DemoPOU",
        kind=kind,
        source="Output := Input;",
        return_type="BOOL" if kind == "Function" else None,
        variables=variables,
    )
    assert validate_iec_xml(build_iec_project("Demo", [pou], []), SCHEMA)["status"] == "passed"


@pytest.mark.skipif(not SCHEMA.is_file(), reason="Local Omron XSD not installed")
def test_parallel_ladder_validates_against_omron_xsd():
    data = build_iec_project("Demo", [PouSpec(name="Ladder", language="LD", network=network())], [])
    assert validate_iec_xml(data, SCHEMA)["status"] == "passed"
    root = E.fromstring(data)
    assert (
        root.xpath(
            'count(//*[local-name()="LdObject"][@operand="Run"]/*[local-name()="ConnectionPointIn"]/*[local-name()="Connection"])'
        )
        == 2
    )


@pytest.mark.skipif(not SCHEMA.is_file(), reason="Local Omron XSD not installed")
def test_multiple_ladder_rungs_validate_and_emit_sequential_evaluation_order():
    data = build_iec_project(
        "Demo",
        [PouSpec(name="Ladder", language="LD", networks=[series_network(), network()])],
        [],
    )

    assert validate_iec_xml(data, SCHEMA)["status"] == "passed"
    root = E.fromstring(data)
    rungs = root.xpath('//*[local-name()="Rung"]')
    assert [rung.get("evaluationOrder") for rung in rungs] == ["1", "2"]
    assert len(rungs) == 2


def test_graph_rejects_cycles_missing_edges_and_unconnected_nodes():
    valid = network().model_dump()
    invalid = {**valid, "connections": valid["connections"] + [("coil", "start")]}
    with pytest.raises(ValidationError, match="cycle"):
        LadderNetwork.model_validate(invalid)
    with pytest.raises(ValidationError):
        LadderNetwork.model_validate({**valid, "connections": [("left", "missing")]})


def test_rejects_case_insensitive_duplicate_symbols():
    with pytest.raises(ValueError, match="duplicate"):
        build_iec_project(
            "Demo",
            [],
            [
                VariableSpec(name="Run", data_type="BOOL"),
                VariableSpec(name="rUN", data_type="BOOL"),
            ],
        )


def test_invalid_iec_identifier_rejected():
    with pytest.raises(ValidationError):
        VariableSpec(name="Injected\nName", data_type="BOOL")


def test_parse_roundtrip_supported_st_pous_and_declarations():
    data = build_iec_project(
        "Demo",
        [
            PouSpec(
                name="Main",
                source="Result := Input;",
                variables=[
                    VariableSpec(
                        name="Input", data_type="BOOL", scope="local", comment="input comment"
                    ),
                    VariableSpec(
                        name="Result",
                        data_type="BOOL",
                        scope="local",
                        retained=True,
                        initial_value="FALSE",
                    ),
                ],
            ),
            PouSpec(
                name="ToDint",
                kind="Function",
                source="ToDint := Value;",
                return_type="DINT",
                variables=[VariableSpec(name="Value", data_type="DINT", scope="input")],
            ),
            PouSpec(
                name="FB_Copy",
                kind="FunctionBlock",
                source="OutValue := InValue;",
                variables=[
                    VariableSpec(name="InValue", data_type="BOOL", scope="input"),
                    VariableSpec(name="OutValue", data_type="BOOL", scope="output"),
                ],
            ),
        ],
        [],
    )

    pous = parse_iec_xml_pous(data)

    assert [(p.kind, p.name, p.language) for p in pous] == [
        ("Program", "Main", "ST"),
        ("Function", "ToDint", "ST"),
        ("FunctionBlock", "FB_Copy", "ST"),
    ]
    assert pous[0].source == "Result := Input;"
    assert [(v.name, v.scope, v.comment) for v in pous[0].variables] == [
        ("Input", "local", "input comment"),
        ("Result", "local", None),
    ]
    assert pous[0].variables[1].retained is True
    assert pous[0].variables[1].initial_value == "FALSE"
    assert pous[1].return_type == "DINT"
    assert pous[1].variables[0].data_type == "DINT"


def test_parse_linear_ladder_xml_preserves_nodes_and_connections():
    data = build_iec_project(
        "Demo",
        [
            PouSpec(
                name="Ladder",
                language="LD",
                network=series_network(),
                variables=[
                    VariableSpec(name="Start", data_type="BOOL"),
                    VariableSpec(name="Blocked", data_type="BOOL"),
                    VariableSpec(name="Run", data_type="BOOL"),
                ],
            )
        ],
        [],
    )

    [pou] = parse_iec_xml_pous(data)

    assert pou.language == "LD"
    assert pou.source is None
    assert [(node.kind, node.variable) for node in pou.network.nodes] == [
        ("left_rail", None),
        ("contact_no", "Start"),
        ("contact_nc", "Blocked"),
        ("coil", "Run"),
        ("right_rail", None),
    ]
    assert len(pou.network.connections) == 4


def test_parse_multiple_ladder_rungs_sorts_by_evaluation_order():
    data = build_iec_project(
        "Demo",
        [PouSpec(name="Ladder", language="LD", networks=[series_network(), network()])],
        [],
    )
    root = E.fromstring(data)
    rungs = root.xpath('//*[local-name()="Rung"]')
    rungs[0].set("evaluationOrder", "20")
    rungs[1].set("evaluationOrder", "10")

    [pou] = parse_iec_xml_pous(E.tostring(root))

    assert pou.network is None
    assert len(pou.networks) == 2
    assert len(pou.networks[0].connections) == 5
    assert len(pou.networks[1].connections) == 4


@pytest.mark.parametrize("orders", [["1", "1"], ["01", "1"], [None, "2"]])
def test_parse_multiple_ladder_rungs_rejects_ambiguous_evaluation_order(orders):
    data = build_iec_project(
        "Demo",
        [PouSpec(name="Ladder", language="LD", networks=[series_network(), network()])],
        [],
    )
    root = E.fromstring(data)
    for rung, order in zip(root.xpath('//*[local-name()="Rung"]'), orders, strict=True):
        if order is None:
            rung.attrib.pop("evaluationOrder")
        else:
            rung.set("evaluationOrder", order)

    with pytest.raises(ValueError, match="evaluationOrder"):
        parse_iec_xml_pous(E.tostring(root))


def test_parse_single_ladder_rung_without_order_keeps_legacy_network_shape():
    data = build_iec_project(
        "Demo", [PouSpec(name="Ladder", language="LD", network=series_network())], []
    )
    root = E.fromstring(data)
    root.xpath('//*[local-name()="Rung"]')[0].attrib.pop("evaluationOrder")

    [pou] = parse_iec_xml_pous(E.tostring(root))

    assert pou.network is not None
    assert pou.networks is None


def test_parse_accepts_ladder_and_rejects_global_declarations():
    ladder = build_iec_project(
        "Demo", [PouSpec(name="Ladder", language="LD", network=network())], []
    )
    [pou] = parse_iec_xml_pous(ladder)
    assert pou.language == "LD"
    assert len(pou.network.connections) == 5

    globals_xml = build_iec_project(
        "Demo", [PouSpec(name="Main", source="Run := TRUE;")],
        [VariableSpec(name="Run", data_type="BOOL")],
    )
    with pytest.raises(ValueError, match="global"):
        parse_iec_xml_pous(globals_xml)


def test_parse_global_variable_declarations_with_project_context():
    data = build_iec_project(
        "Demo",
        [PouSpec(name="Main", source="Run := ImportedRun;")],
        [
            VariableSpec(
                name="ImportedRun",
                data_type="BOOL",
                initial_value="FALSE",
                retained=True,
                constant=True,
                comment="imported global",
            )
        ],
    )

    document = parse_iec_xml_document(data)

    assert [pou.name for pou in document["pous"]] == ["Main"]
    assert len(document["global_variables"]) == 1
    variable = document["global_variables"][0]
    assert (variable.name, variable.data_type, variable.scope) == (
        "ImportedRun",
        "BOOL",
        "global",
    )
    assert variable.initial_value == "FALSE"
    assert variable.retained is True
    assert variable.constant is True
    assert variable.comment == "imported global"


def test_parse_struct_union_enum_and_fixed_array_data_types():
    namespace_uri = "www.iec.ch/public/TC65SC65BWG7TF10"
    xsi_uri = "http://www.w3.org/2001/XMLSchema-instance"
    data = build_iec_project(
        "Types",
        [PouSpec(name="Main", source="Run := TRUE;")],
        [],
    )
    root = E.fromstring(data)
    namespace = root.find(f"{{{namespace_uri}}}Types/{{{namespace_uri}}}GlobalNamespace")

    structure = E.SubElement(namespace, f"{{{namespace_uri}}}DataTypeDecl", name="Status")
    docs = E.SubElement(structure, f"{{{namespace_uri}}}Documentation")
    docs.set(f"{{{xsi_uri}}}type", "SimpleText")
    docs.text = "Status structure"
    spec = E.SubElement(structure, f"{{{namespace_uri}}}UserDefinedTypeSpec")
    spec.set(f"{{{xsi_uri}}}type", "StructTypeSpec")
    member = E.SubElement(spec, f"{{{namespace_uri}}}Member", name="Ready")
    member_type = E.SubElement(member, f"{{{namespace_uri}}}Type")
    E.SubElement(member_type, f"{{{namespace_uri}}}TypeName").text = "BOOL"

    union = E.SubElement(namespace, f"{{{namespace_uri}}}DataTypeDecl", name="WordBits")
    union_spec = E.SubElement(union, f"{{{namespace_uri}}}UserDefinedTypeSpec")
    union_spec.set(f"{{{xsi_uri}}}type", "StructTypeSpec")
    union_spec.set("overlap", "true")
    union_member = E.SubElement(union_spec, f"{{{namespace_uri}}}Member", name="Raw")
    union_type = E.SubElement(union_member, f"{{{namespace_uri}}}Type")
    E.SubElement(union_type, f"{{{namespace_uri}}}TypeName").text = "WORD"

    enumeration = E.SubElement(namespace, f"{{{namespace_uri}}}DataTypeDecl", name="Mode")
    enum_spec = E.SubElement(enumeration, f"{{{namespace_uri}}}UserDefinedTypeSpec")
    enum_spec.set(f"{{{xsi_uri}}}type", "EnumTypeWithNamedValueSpec")
    E.SubElement(enum_spec, f"{{{namespace_uri}}}Enumerator", name="Stopped", value="0")
    E.SubElement(enum_spec, f"{{{namespace_uri}}}Enumerator", name="Running", value="1")
    E.SubElement(enum_spec, f"{{{namespace_uri}}}BaseType").text = "DINT"

    array = E.SubElement(namespace, f"{{{namespace_uri}}}DataTypeDecl", name="Samples")
    array_spec = E.SubElement(array, f"{{{namespace_uri}}}UserDefinedTypeSpec")
    array_spec.set(f"{{{xsi_uri}}}type", "ArrayTypeSpec")
    base = E.SubElement(array_spec, f"{{{namespace_uri}}}BaseType")
    E.SubElement(base, f"{{{namespace_uri}}}TypeName").text = "INT"
    dimension = E.SubElement(
        array_spec, f"{{{namespace_uri}}}DimensionSpec", dimensionNumber="1"
    )
    E.SubElement(dimension, f"{{{namespace_uri}}}IndexRange", lower="-1", upper="2")

    document = parse_iec_xml_document(E.tostring(root))

    assert [item.name for item in document["data_types"]] == [
        "Status",
        "WordBits",
        "Mode",
        "Samples",
    ]
    assert document["data_types"][0].kind == "STRUCT"
    assert document["data_types"][0].comment == "Status structure"
    assert document["data_types"][0].members[0].data_type == "BOOL"
    assert document["data_types"][1].kind == "UNION"
    assert document["data_types"][2].kind == "ENUM"
    assert [(value.name, value.value) for value in document["data_types"][2].values] == [
        ("Stopped", 0),
        ("Running", 1),
    ]
    assert document["data_types"][3].kind == "ARRAY"
    assert document["data_types"][3].base_type == "ARRAY[-1..2] OF INT"
    if SCHEMA.is_file():
        assert validate_iec_xml(E.tostring(root), SCHEMA)["status"] == "passed"


def test_parse_empty_structure_data_type_is_rejected():
    namespace_uri = "www.iec.ch/public/TC65SC65BWG7TF10"
    xsi_uri = "http://www.w3.org/2001/XMLSchema-instance"
    root = E.fromstring(
        build_iec_project("Empty type", [PouSpec(name="Main", source="Run := TRUE;")], [])
    )
    namespace = root.find(f"{{{namespace_uri}}}Types/{{{namespace_uri}}}GlobalNamespace")
    declaration = E.SubElement(namespace, f"{{{namespace_uri}}}DataTypeDecl", name="EmptyStatus")
    spec = E.SubElement(declaration, f"{{{namespace_uri}}}UserDefinedTypeSpec")
    spec.set(f"{{{xsi_uri}}}type", "StructTypeSpec")

    with pytest.raises(ValidationError, match="Structure and union types require members only"):
        parse_iec_xml_document(E.tostring(root))


def test_parse_namespace_types_into_native_scope_and_local_member_references():
    namespace_uri = "www.iec.ch/public/TC65SC65BWG7TF10"
    xsi_uri = "http://www.w3.org/2001/XMLSchema-instance"
    data = build_iec_project(
        "Namespaced types",
        [PouSpec(name="Main", source="Ready := TRUE;")],
        [],
    )
    root = E.fromstring(data)
    namespace = root.find(f"{{{namespace_uri}}}Types/{{{namespace_uri}}}GlobalNamespace")
    plc_namespace = E.SubElement(
        namespace, f"{{{namespace_uri}}}NamespaceDecl", name="Axis"
    )

    limit = E.SubElement(plc_namespace, f"{{{namespace_uri}}}DataTypeDecl", name="Limit")
    limit_spec = E.SubElement(limit, f"{{{namespace_uri}}}UserDefinedTypeSpec")
    limit_spec.set(f"{{{xsi_uri}}}type", "StructTypeSpec")
    limit_member = E.SubElement(limit_spec, f"{{{namespace_uri}}}Member", name="Value")
    limit_type = E.SubElement(limit_member, f"{{{namespace_uri}}}Type")
    E.SubElement(limit_type, f"{{{namespace_uri}}}TypeName").text = "DINT"

    status = E.SubElement(plc_namespace, f"{{{namespace_uri}}}DataTypeDecl", name="Status")
    status_spec = E.SubElement(status, f"{{{namespace_uri}}}UserDefinedTypeSpec")
    status_spec.set(f"{{{xsi_uri}}}type", "StructTypeSpec")
    status_member = E.SubElement(status_spec, f"{{{namespace_uri}}}Member", name="SoftLimit")
    status_type = E.SubElement(status_member, f"{{{namespace_uri}}}Type")
    E.SubElement(status_type, f"{{{namespace_uri}}}TypeName").text = "Limit"

    document = parse_iec_xml_document(E.tostring(root))

    assert [item.name for item in document["data_types"]] == ["Limit", "Status"]
    assert document["data_types"][1].members[0].data_type == "Limit"
    if SCHEMA.is_file():
        assert validate_iec_xml(E.tostring(root), SCHEMA)["status"] == "passed"
