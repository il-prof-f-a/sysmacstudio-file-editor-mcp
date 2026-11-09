from zipfile import ZipFile

from lxml import etree as ET

from sysmac_mcp.workspace import Workspace


def project_with_data_type_tables(project_file, tmp_path, tables, global_rows, source):
    with ZipFile(project_file) as archive:
        members = {item.filename: archive.read(item) for item in archive.infolist()}

    oem = ET.fromstring(members["s/s.oem"])
    controller = oem.find('.//Entity[@type="Device"][@id="cpu"]')
    children = controller.find("ChildEntities")
    for table_id, payload in tables:
        table = ET.SubElement(
            children,
            "Entity",
            type="DataType",
            subtype="",
            id=table_id,
            name=f"Data Types {table_id}",
        )
        ET.SubElement(table, "ChildEntities")
        members[f"s/{table_id}.xml"] = payload

    members["s/s.oem"] = ET.tostring(oem, encoding="utf-8")
    members["s/globals.xml"] += "".join(f"{row}\r\n" for row in global_rows).encode()
    members["s/body.xml"] = (
        "<StructuredTextModel><Text>" + source + "</Text></StructuredTextModel>"
    ).encode()

    candidate = tmp_path / "type-aliases.smc2"
    with ZipFile(candidate, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return candidate


def validate_project(project_file, tmp_path, tables, global_rows, source):
    candidate = project_with_data_type_tables(project_file, tmp_path, tables, global_rows, source)
    workspace = Workspace(tmp_path / "workspace")
    project_id = workspace.open_project(candidate)["project_id"]
    transaction_id = workspace.begin(project_id)["transaction_id"]
    return workspace.validate(transaction_id)


def test_validation_resolves_controller_scalar_alias_chains(project_file, tmp_path):
    data_types = b"""<data Version="1">
      <DataType Name="FlagAlias" BaseType="BOOL" />
      <DataType Name="FlagAlias2" BaseType="FlagAlias" />
      <DataType Name="CountAlias" BaseType="INT" />
    </data>"""
    result = validate_project(
        project_file,
        tmp_path,
        [("types", data_types)],
        [
            "++D=FlagAlias\tN=AliasFlag\tG=VAR_GLOBAL",
            "++D=FlagAlias2\tN=AliasFlag2\tG=VAR_GLOBAL",
            "++D=CountAlias\tN=AliasCount\tG=VAR_GLOBAL",
        ],
        "AliasFlag := 1; AliasCount := TRUE; AliasCount := AliasFlag; "
        "AliasFlag2 := AliasCount; AliasFlag2 := FALSE; AliasCount := 1;",
    )

    diagnostics = result["type_diagnostics_preview"]
    assert result["type_check"] == "partial_scalar_checks_with_controller_aliases"
    assert result["type_diagnostic_count"] == 4
    assert {item["code"] for item in diagnostics} == {
        "assignment_literal_type_mismatch",
        "assignment_variable_type_mismatch",
    }
    assert {item["target_type"] for item in diagnostics} == {
        "FlagAlias",
        "FlagAlias2",
        "CountAlias",
    }
    assert result["type_check_coverage"]["controller_scalar_aliases"] == {
        "status": "observed",
        "controllers_with_tables": 1,
        "controllers_with_incomplete_tables": 0,
        "controllers_without_tables": 0,
        "resolved_aliases": 3,
        "ambiguous_names": 0,
    }


def test_validation_skips_ambiguous_controller_aliases(project_file, tmp_path):
    first_table = b'<data><DataType Name="FlagAlias" BaseType="BOOL"/></data>'
    second_table = b'<data><DataType Name="FlagAlias" BaseType="DINT"/></data>'
    result = validate_project(
        project_file,
        tmp_path,
        [("types", first_table), ("types2", second_table)],
        ["++D=FlagAlias\tN=AliasFlag\tG=VAR_GLOBAL"],
        "AliasFlag := 'synthetic';",
    )

    assert result["type_diagnostic_count"] == 0
    assert result["type_check_coverage"]["controller_scalar_aliases"] == {
        "status": "observed",
        "controllers_with_tables": 1,
        "controllers_with_incomplete_tables": 0,
        "controllers_without_tables": 0,
        "resolved_aliases": 0,
        "ambiguous_names": 1,
    }


def test_validation_skips_aliases_when_controller_table_is_incomplete(project_file, tmp_path):
    incomplete_table = b"<data><DataType Name='FlagAlias' BaseType='BOOL'></data>"
    result = validate_project(
        project_file,
        tmp_path,
        [("types", incomplete_table)],
        ["++D=FlagAlias\tN=AliasFlag\tG=VAR_GLOBAL"],
        "AliasFlag := 'synthetic';",
    )

    assert result["type_diagnostic_count"] == 0
    assert result["type_check_coverage"]["controller_scalar_aliases"] == {
        "status": "partial",
        "controllers_with_tables": 1,
        "controllers_with_incomplete_tables": 1,
        "controllers_without_tables": 0,
        "resolved_aliases": 0,
        "ambiguous_names": 0,
    }


def test_validation_detects_numeric_scalar_expression_assigned_to_boolean(project_file, tmp_path):
    result = validate_project(
        project_file,
        tmp_path,
        [],
        [
            "++D=BOOL\tN=Flag\tG=VAR_GLOBAL",
            "++D=DINT\tN=Count\tG=VAR_GLOBAL",
        ],
        "Flag := Count + 1;",
    )

    assert result["type_diagnostic_count"] == 1
    diagnostic = result["type_diagnostics_preview"][0]
    assert diagnostic["code"] == "assignment_expression_type_mismatch"
    assert diagnostic["target_type"] == "BOOL"
    assert diagnostic["expression_category"] == "numeric"
    assert diagnostic["certainty"] == "definite_category_mismatch"
    assert diagnostic["line"] == 1
    assert diagnostic["column"] == 1
    assert "Flag" not in str(diagnostic)
    assert "Count" not in str(diagnostic)


def test_validation_detects_boolean_scalar_expression_assigned_to_numeric(project_file, tmp_path):
    result = validate_project(
        project_file,
        tmp_path,
        [],
        [
            "++D=BOOL\tN=Flag\tG=VAR_GLOBAL",
            "++D=BOOL\tN=OtherFlag\tG=VAR_GLOBAL",
            "++D=DINT\tN=Count\tG=VAR_GLOBAL",
        ],
        "Count := Flag AND OtherFlag;",
    )

    assert result["type_diagnostic_count"] == 1
    diagnostic = result["type_diagnostics_preview"][0]
    assert diagnostic["code"] == "assignment_expression_type_mismatch"
    assert diagnostic["target_type"] == "DINT"
    assert diagnostic["expression_category"] == "boolean"


def test_validation_uses_boolean_result_of_scalar_comparison(project_file, tmp_path):
    result = validate_project(
        project_file,
        tmp_path,
        [],
        [
            "++D=DINT\tN=Count\tG=VAR_GLOBAL",
        ],
        "Count := (Count + 1) >= 2;",
    )

    assert result["type_diagnostic_count"] == 1
    diagnostic = result["type_diagnostics_preview"][0]
    assert diagnostic["code"] == "assignment_expression_type_mismatch"
    assert diagnostic["target_type"] == "DINT"
    assert diagnostic["expression_category"] == "boolean"


def test_validation_skips_function_calls_and_unknown_expression_operands(project_file, tmp_path):
    result = validate_project(
        project_file,
        tmp_path,
        [],
        [
            "++D=BOOL\tN=Flag\tG=VAR_GLOBAL",
            "++D=DINT\tN=Count\tG=VAR_GLOBAL",
        ],
        "Flag := ABS(Count); Count := Count + UnknownValue;",
    )

    assert result["type_diagnostic_count"] == 0


def test_validation_checks_builtin_integer_literal_ranges_for_assignments(project_file, tmp_path):
    result = validate_project(
        project_file,
        tmp_path,
        [],
        [
            "++D=SINT\tN=Signed\tG=VAR_GLOBAL",
            "++D=USINT\tN=Unsigned\tG=VAR_GLOBAL",
            "++D=INT\tN=AtIntMin\tG=VAR_GLOBAL",
            "++D=UINT\tN=AtUIntMax\tG=VAR_GLOBAL",
            "++D=DINT\tN=TooWide\tG=VAR_GLOBAL",
            "++D=ULINT\tN=AtULintMax\tG=VAR_GLOBAL",
        ],
        "Signed := 128; Unsigned := -1; AtIntMin := -32768; "
        "AtUIntMax := 65535; TooWide := 2147483648; "
        "AtULintMax := 18446744073709551615;",
    )

    diagnostics = result["type_diagnostics_preview"]
    assert result["type_diagnostic_count"] == 3
    assert {item["code"] for item in diagnostics} == {"assignment_integer_range_mismatch"}
    assert {item["target_type"]: (item["minimum"], item["maximum"]) for item in diagnostics} == {
        "SINT": (-128, 127),
        "USINT": (0, 255),
        "DINT": (-(2**31), 2**31 - 1),
    }
    assert not any(name in str(diagnostics) for name in ("Signed", "Unsigned", "TooWide"))


def test_validation_checks_builtin_integer_literal_ranges_for_initializers(project_file, tmp_path):
    result = validate_project(
        project_file,
        tmp_path,
        [],
        [
            "++D=SINT\tN=TooSmall\tIV=-129\tG=VAR_GLOBAL",
            "++D=UINT\tN=AtMax\tIV=65535\tG=VAR_GLOBAL",
        ],
        "",
    )

    diagnostics = result["type_diagnostics_preview"]
    assert result["type_diagnostic_count"] == 1
    assert diagnostics[0]["code"] == "initializer_integer_range_mismatch"
    assert diagnostics[0]["target_type"] == "SINT"
    assert (diagnostics[0]["minimum"], diagnostics[0]["maximum"]) == (-128, 127)
    assert "TooSmall" not in str(diagnostics[0])


def test_validation_does_not_treat_integral_real_literals_as_integer_ranges(project_file, tmp_path):
    result = validate_project(
        project_file,
        tmp_path,
        [],
        [
            "++D=SINT\tN=DecimalValue\tG=VAR_GLOBAL",
            "++D=SINT\tN=TypedRealValue\tG=VAR_GLOBAL",
        ],
        "DecimalValue := 128.0; TypedRealValue := REAL#128.0;",
    )

    assert result["type_diagnostic_count"] == 0


def test_validation_does_not_treat_real_initializers_as_integer_ranges(project_file, tmp_path):
    result = validate_project(
        project_file,
        tmp_path,
        [],
        [
            "++D=SINT\tN=DecimalValue\tIV=128.0\tG=VAR_GLOBAL",
            "++D=SINT\tN=TypedRealValue\tIV=REAL#128.0\tG=VAR_GLOBAL",
        ],
        "",
    )

    assert result["type_diagnostic_count"] == 0
