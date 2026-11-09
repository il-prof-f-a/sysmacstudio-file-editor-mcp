from zipfile import ZipFile

import pytest

from sysmac_mcp.workspace import ProjectError, Workspace


def opened(project_file, tmp_path):
    workspace = Workspace(tmp_path / "workspace")
    pid = workspace.open_project(project_file)["project_id"]
    tx = workspace.begin(pid)["transaction_id"]
    return workspace, pid, tx


def rewrite_members(path, updates):
    with ZipFile(path) as archive:
        members = {i.filename: archive.read(i) for i in archive.infolist()}
    members.update(updates)
    with ZipFile(path, "w") as archive:
        for name, raw in members.items():
            archive.writestr(name, raw)


def test_new_global_st_reference_registers_external_in_same_atomic_change(project_file, tmp_path):
    w, pid, tx = opened(project_file, tmp_path)
    original = project_file.read_bytes()
    w.create_variable(tx, "globals", "Probe", "BOOL", initial_value="FALSE", expected_revision=0)
    result = w.update_st(tx, "body", "Run := Start; Probe := FALSE;", expected_revision=0)
    aliases = [v for v in w.project(pid, tx).variables("p") if v["name"] == "Probe"]
    assert len(aliases) == 1 and aliases[0]["group"] == "VAR_EXTERNAL"
    assert aliases[0]["data_type"] == "BOOL" and aliases[0]["initial_value"] is None
    assert result["change"]["registered_externals"][0]["name"] == "Probe"
    assert set(w.preview(tx)["changed_entries"]) == {"s/globals.xml", "s/body.xml", "s/locals.xml"}
    assert not any(v["name"] == "Probe" for v in w.project(pid).variables("p"))
    w.commit(tx)
    assert project_file.read_bytes() == original
    assert (
        next(v for v in w.project(pid).variables("p") if v["name"] == "Probe")["group"]
        == "VAR_EXTERNAL"
    )


def test_automatic_registration_does_not_duplicate_or_repair_unrelated_legacy_refs(
    project_file, tmp_path
):
    w, pid, tx = opened(project_file, tmp_path)
    w.create_variable(tx, "globals", "Probe", "BOOL", expected_revision=0)
    w.update_st(tx, "body", "Run := Start; Probe := FALSE;", expected_revision=0)
    w.update_st(tx, "body", "Run := Start; Probe := TRUE;", expected_revision=0)
    variables = w.project(pid, tx).variables("p")
    assert len([v for v in variables if v["name"] == "Probe"]) == 1
    assert not any(v["name"] in {"Run", "Start"} for v in variables)


def test_local_shadow_does_not_register_global_with_same_name(project_file, tmp_path):
    w, pid, tx = opened(project_file, tmp_path)
    w.create_variable(tx, "globals", "Probe", "BOOL", expected_revision=0)
    w.create_variable(tx, "locals", "Probe", "BOOL", expected_revision=0)
    w.update_st(tx, "body", "Probe := FALSE;", expected_revision=0)
    assert [v["group"] for v in w.project(pid, tx).variables("p") if v["name"] == "Probe"] == [
        "VAR"
    ]


def test_explicit_registration_repairs_existing_source_without_initializing_alias(
    project_file, tmp_path
):
    w, pid, tx = opened(project_file, tmp_path)
    global_start = next(v for v in w.project(pid).variables("global") if v["name"] == "Start")
    w.register_external(tx, "p", global_start["id"], expected_revision=0)
    w.register_external(tx, "p", global_start["id"], expected_revision=0)
    local = [v for v in w.project(pid, tx).variables("p") if v["name"] == "Start"]
    assert len(local) == 1
    assert local[0]["attributes"] == {"D": "BOOL", "N": "Start", "G": "VAR_EXTERNAL"}
    assert w.preview(tx)["changed_entries"] == ["s/locals.xml"]


def test_registration_refuses_to_replace_local_declaration(project_file, tmp_path):
    w, pid, tx = opened(project_file, tmp_path)
    w.create_variable(tx, "globals", "Counter", "INT", expected_revision=0)
    counter = next(v for v in w.project(pid, tx).variables("global") if v["name"] == "Counter")
    before = w.preview(tx)
    with pytest.raises(ProjectError):
        w.register_external(tx, "p", counter["id"], expected_revision=0)
    assert w.preview(tx) == before


def test_missing_pou_table_rejects_new_global_dependency_without_partial_source(
    project_file, tmp_path
):
    w, pid, tx = opened(project_file, tmp_path)
    w.create_variable(tx, "globals", "Probe", "BOOL", expected_revision=0)
    before = w.preview(tx)
    rungs = w.project(pid, tx).read_pou("ldp")["sections"][0]["rungs"]
    rungs[0]["CLs"][0]["Var"] = "Probe"
    with pytest.raises(ProjectError):
        w.update_ld(tx, "ld", rungs, expected_revision=0)
    assert w.preview(tx) == before


def test_validation_reports_external_registration_needed(project_file, tmp_path):
    w, pid, tx = opened(project_file, tmp_path)
    result = w.validate(tx)
    assert result["missing_external_count"] == 4  # Start/Run in both synthetic POUs.
    assert result["external_declaration_check"] == "missing_declarations"
    assert result["sysmac_build"] == "not_executed"


def test_validation_surfaces_bounded_st_and_ladder_diagnostics(project_file, tmp_path):
    rewrite_members(
        project_file,
        {
            "s/body.xml": b"<StructuredTextModel><Text>Start := 'unterminated;</Text></StructuredTextModel>",
            "s/ld.xml": b'{"CLs":[{"__type":"FutureInstruction","Var":"Start"}],"LRI":0,"RRI":0,"VLs":[]}\r\n',
        },
    )
    w, _, tx = opened(project_file, tmp_path)

    result = w.validate(tx)

    assert result["st_syntax"] == "partial_lexer_and_delimiter_checks"
    assert result["type_check"] == "partial_scalar_checks_with_controller_aliases"
    assert result["type_diagnostic_count"] == 0
    assert result["ladder_semantics"] == "not_executed"
    assert result["ladder_instruction_check"] == "partial_unsupported_instruction_scan"
    assert result["source_diagnostic_count"] == 2
    assert {item["code"] for item in result["source_diagnostics_preview"]} == {
        "unterminated_string",
        "unsupported_ladder_instruction",
    }
    assert all(item["section_id"] and item["pou_id"] for item in result["source_diagnostics_preview"])
    st_issue = next(
        item for item in result["source_diagnostics_preview"] if item["code"] == "unterminated_string"
    )
    assert (st_issue["line"], st_issue["column"], st_issue["start"]) == (1, 10, 9)
    assert result["sysmac_build"] == "not_executed"


def test_validation_checks_definite_scalar_literal_assignment_mismatches(project_file, tmp_path):
    rewrite_members(
        project_file,
        {
            "s/body.xml": (
                b"<StructuredTextModel><Text>Run := 17; Counter := TRUE; "
                b"Start := 'SYNTHETIC_SENTINEL'; Run := Start; "
                b"Counter := Start OR TRUE; Counter := DINT#3;</Text></StructuredTextModel>"
            )
        },
    )
    w, _, tx = opened(project_file, tmp_path)

    result = w.validate(tx)

    assert result["type_check"] == "partial_scalar_checks_with_controller_aliases"
    assert result["type_diagnostic_count"] == 4
    assert {item["target_type"] for item in result["type_diagnostics_preview"]} == {
        "BOOL",
        "INT",
    }
    literal_diagnostics = [
        item
        for item in result["type_diagnostics_preview"]
        if item["code"] == "assignment_literal_type_mismatch"
    ]
    assert {item["literal_category"] for item in literal_diagnostics} == {
        "numeric",
        "boolean",
        "string",
    }
    diagnostics = result["type_diagnostics_preview"]
    assert sum(item["code"] == "assignment_literal_type_mismatch" for item in diagnostics) == 3
    expressions = [
        item for item in diagnostics if item["code"] == "assignment_expression_type_mismatch"
    ]
    assert len(expressions) == 1
    assert expressions[0]["target_type"] == "INT"
    assert expressions[0]["expression_category"] == "boolean"
    assert all(item["pou_id"] == "p" and item["section_id"] == "body" for item in diagnostics)
    assert all(item["line"] == 1 and item["column"] > 0 for item in diagnostics)
    assert "SYNTHETIC_SENTINEL" not in str(result["type_diagnostics_preview"])


def test_validation_checks_definite_scalar_variable_assignment_mismatches(project_file, tmp_path):
    with ZipFile(project_file) as archive:
        globals_xml = archive.read("s/globals.xml")
    rewrite_members(
        project_file,
        {
            "s/body.xml": (
                b"<StructuredTextModel><Text>Counter := Start; Start := Counter; "
                b"Run := Start; Counter := Run; Run := Counter; "
                b"Counter := Counter + 1; Counter := DINT#3; "
                b"Run := Unknown; Unknown := Run;</Text></StructuredTextModel>"
            ),
            "s/globals.xml": globals_xml + b"++D=CustomerRecord\tN=Unknown\tG=VAR_GLOBAL\r\n",
        },
    )
    w, _, tx = opened(project_file, tmp_path)

    result = w.validate(tx)
    diagnostics = [
        item for item in result["type_diagnostics_preview"]
        if item["code"] == "assignment_variable_type_mismatch"
    ]

    assert len(diagnostics) == 4
    assert { (item["target_type"], item["source_type"]) for item in diagnostics } == {
        ("BOOL", "INT"),
        ("INT", "BOOL"),
    }
    assert all(item["certainty"] == "definite_type_category_mismatch" for item in diagnostics)
    assert all(item["target_variable_id"] and item["source_variable_id"] for item in diagnostics)
    assert all(item["line"] == 1 and item["column"] > 0 for item in diagnostics)
    assert not any(name in str(diagnostics) for name in ("Start", "Run", "Counter"))


def test_validation_checks_definite_scalar_literal_initializers(project_file, tmp_path):
    rewrite_members(
        project_file,
        {
            "s/globals.xml": (
                b"[SLWD version=1.0]\r\n_EN=Variables\r\n+GN=VAR_GLOBAL\tGVT=GlobalNamespaceGroup\r\n"
                b"++D=BOOL\tN=Start\tG=VAR_GLOBAL\r\n"
                b"++D=BOOL\tN=Run\tIV=FALSE\tG=VAR_GLOBAL\r\n"
                b"++D=BOOL\tN=BadBool\tIV=17\tG=VAR_GLOBAL\r\n"
                b"++D=BOOL\tN=OkayBool\tIV=TRUE\tG=VAR_GLOBAL\r\n"
                b"++D=STRING\tN=OkayString\tIV='SAFE'\tG=VAR_GLOBAL\r\n"
                b"++D=INT\tN=OkayInt\tIV=16#10\tG=VAR_GLOBAL\r\n"
                b"++D=INT\tN=UnsupportedExpression\tIV=TRUE + FALSE\tG=VAR_GLOBAL\r\n"
                b"++D=CustomerRecord\tN=UnknownType\tIV=TRUE\tG=VAR_GLOBAL\r\n"
            ),
            "s/locals.xml": (
                b"[SLWD version=1.0]\r\n_EN=Variables\r\n+GN=VAR\r\n"
                b"++D=INT\tN=Counter\tG=VAR\r\n"
                b"++D=INT\tN=BadInt\tIV='SYNTHETIC_INITIALIZER_SENTINEL'\tG=VAR\r\n"
            ),
        },
    )
    w, _, tx = opened(project_file, tmp_path)

    result = w.validate(tx)

    assert result["type_diagnostic_count"] == 2
    initializers = [
        item for item in result["type_diagnostics_preview"]
        if item["code"] == "initializer_literal_type_mismatch"
    ]
    assert len(initializers) == 2
    assert {item["target_type"] for item in initializers} == {"BOOL", "INT"}
    assert {item["literal_category"] for item in initializers} == {"numeric", "string"}
    assert all(item["certainty"] == "definite_category_mismatch" for item in initializers)
    assert {item["line"] for item in initializers} == {5, 6}
    assert "SYNTHETIC_INITIALIZER_SENTINEL" not in str(initializers)
    assert "scalar variable initializers" in result["type_check_coverage"]["checked"]
    assert "compound or unsupported initializer forms" in result["type_check_coverage"]["not_checked"]


def test_ladder_reference_registers_alias_when_table_is_available(project_file, tmp_path):
    from lxml import etree as E

    with ZipFile(project_file) as z:
        members = {i.filename: z.read(i) for i in z.infolist()}
    root = E.fromstring(members["s/s.oem"])
    E.SubElement(
        root.find('.//Entity[@id="ldp"]/ChildEntities'),
        "Entity",
        type="Variables",
        id="ladder-vars",
        name="Variables",
    )
    members["s/s.oem"] = E.tostring(root)
    members["s/ladder-vars.xml"] = (
        b"[SLWD version=1.0]\r\n_EN=Variables\r\n+GN=VAR\tGVT=DefaultGroup\r\n"
    )
    with ZipFile(project_file, "w") as z:
        for name, raw in members.items():
            z.writestr(name, raw)
    w, pid, tx = opened(project_file, tmp_path)
    w.create_variable(tx, "globals", "Probe", "BOOL", expected_revision=0)
    rungs = w.project(pid, tx).read_pou("ldp")["sections"][0]["rungs"]
    rungs[0]["CLs"][0]["Var"] = "Probe"
    w.update_ld(tx, "ld", rungs, expected_revision=0)
    assert (
        next(v for v in w.project(pid, tx).variables("ldp") if v["name"] == "Probe")["group"]
        == "VAR_EXTERNAL"
    )


def test_new_pou_registers_used_globals_without_requiring_manual_external_specs(
    project_file, tmp_path
):
    w, pid, tx = opened(project_file, tmp_path)
    new = w.create_pou(tx, "p", {"name": "Worker", "source": "Run := Start;"}, expected_revision=0)[
        "change"
    ]
    aliases = w.project(pid, tx).variables(new["pou_id"])
    assert {v["name"] for v in aliases} == {"Run", "Start"}
    assert all(v["group"] == "VAR_EXTERNAL" for v in aliases)


async def test_explicit_external_registration_through_mcp(project_file, tmp_path):
    from mcp import Client

    from sysmac_mcp.server import create_server

    async with Client(create_server(tmp_path / "protocol")) as client:
        opened = await client.call_tool("plc_open_project", {"path": str(project_file)})
        pid = opened.structured_content["project_id"]
        variables = await client.call_tool(
            "plc_list_variables", {"project_id": pid, "scope": "global", "query": "Start"}
        )
        variable_id = variables.structured_content["items"][0]["id"]
        begin = await client.call_tool("plc_begin_transaction", {"project_id": pid})
        tx = begin.structured_content["transaction_id"]
        result = await client.call_tool(
            "plc_register_external",
            {
                "transaction_id": tx,
                "pou_id": "p",
                "global_variable_id": variable_id,
                "expected_revision": 0,
            },
        )
        assert not result.is_error
        aliases = await client.call_tool(
            "plc_list_variables",
            {"project_id": pid, "scope": "p", "query": "Start", "transaction_id": tx},
        )
        assert aliases.structured_content["items"][0]["group"] == "VAR_EXTERNAL"


def test_existing_external_with_different_type_is_rejected_atomically(project_file, tmp_path):
    with ZipFile(project_file) as archive:
        locals_ = archive.read("s/locals.xml")
    rewrite_members(
        project_file,
        {"s/locals.xml": locals_ + b"+GN=VAR_EXTERNAL\r\n++D=INT\tN=Start\tG=VAR_EXTERNAL\r\n"},
    )
    w, pid, tx = opened(project_file, tmp_path)
    variable = next(v for v in w.project(pid).variables("global") if v["name"] == "Start")
    before = w.preview(tx)
    with pytest.raises(ProjectError, match="different scope or type"):
        w.register_external(tx, "p", variable["id"], expected_revision=0)
    assert w.preview(tx) == before


def test_registration_preserves_bom_other_groups_and_opaque_attributes(project_file, tmp_path):
    raw = b"\xef\xbb\xbf[SLWD version=1.0]\r\n_EN=Variables\r\n+GN=VAR_EXTERNAL\tCustom=keep\r\n++D=BOOL\tN=Run\tG=VAR_EXTERNAL\tExtra=stay\r\n+GN=VAR\r\n++D=INT\tN=Counter\tG=VAR\r\n"
    rewrite_members(project_file, {"s/locals.xml": raw})
    w, pid, tx = opened(project_file, tmp_path)
    variable = next(v for v in w.project(pid).variables("global") if v["name"] == "Start")
    w.register_external(tx, "p", variable["id"], expected_revision=0)
    staged = w.project(pid, tx)
    assert staged.archive.read("s/locals.xml") == raw.replace(
        b"+GN=VAR\r\n", b"++D=BOOL\tN=Start\tG=VAR_EXTERNAL\r\n+GN=VAR\r\n"
    )
    alias = next(v for v in staged.variables("p") if v["name"] == "Start")
    assert "AT" not in alias["attributes"]


def test_global_from_other_controller_cannot_be_registered(project_file, tmp_path):
    from lxml import etree as E

    with ZipFile(project_file) as archive:
        root = E.fromstring(archive.read("s/s.oem"))
    children = root.find("./Entity/ChildEntities")
    device = E.SubElement(children, "Entity", type="Device", id="cpu2", name="Other")
    E.SubElement(
        E.SubElement(device, "ChildEntities"),
        "Entity",
        type="Variables",
        subtype="Global",
        id="foreign",
        name="GlobalVariables",
    )
    rewrite_members(
        project_file,
        {
            "s/s.oem": E.tostring(root),
            "s/foreign.xml": b"[SLWD version=1.0]\r\n_EN=Variables\r\n+GN=VAR_GLOBAL\r\n++D=BOOL\tN=Elsewhere\tG=VAR_GLOBAL\r\n",
        },
    )
    w, pid, tx = opened(project_file, tmp_path)
    variable = next(v for v in w.project(pid).variables("global") if v["name"] == "Elsewhere")
    before = w.preview(tx)
    with pytest.raises(ProjectError, match="same controller"):
        w.register_external(tx, "p", variable["id"], expected_revision=0)
    assert w.preview(tx) == before


def test_duplicate_global_rows_with_identical_id_do_not_register(project_file, tmp_path):
    with ZipFile(project_file) as archive:
        globals_ = archive.read("s/globals.xml")
    rewrite_members(
        project_file, {"s/globals.xml": globals_ + b"++D=BOOL\tN=Start\tG=VAR_GLOBAL\r\n"}
    )
    w, pid, tx = opened(project_file, tmp_path)
    variable = next(v for v in w.project(pid).variables("global") if v["name"] == "Start")
    before = w.preview(tx)
    with pytest.raises(ProjectError, match="ambiguous"):
        w.register_external(tx, "p", variable["id"], expected_revision=0)
    assert w.preview(tx) == before


@pytest.mark.parametrize(
    "declaration", ["++D=INT\tN=Start\tG=VAR_EXTERNAL", "++D=BOOL\tN=UnknownGlobal\tG=VAR_EXTERNAL"]
)
def test_validation_reports_incompatible_and_unresolved_external_aliases(
    project_file, tmp_path, declaration
):
    with ZipFile(project_file) as archive:
        locals_ = archive.read("s/locals.xml")
    rewrite_members(
        project_file,
        {
            "s/locals.xml": locals_ + ("+GN=VAR_EXTERNAL\r\n" + declaration + "\r\n").encode(),
            "s/body.xml": b"<StructuredTextModel><Text>Counter := 0;</Text></StructuredTextModel>",
            "s/ld.xml": b'{"CLs":[],"LRI":0,"RRI":0,"VLs":[]}\r\n',
        },
    )
    w, pid, tx = opened(project_file, tmp_path)
    result = w.validate(tx)
    assert result["missing_external_count"] == 0
    assert result["external_declaration_check"] == "unresolved_external_declarations"
    assert result["external_diagnostic_count"] == 1
    assert result["external_diagnostics_preview"][0]["pou_id"] == "p"


@pytest.mark.parametrize("name, data_type", [("Start", "INT"), ("UnknownGlobal", "BOOL")])
def test_new_use_of_invalid_external_alias_is_rejected_atomically(
    project_file, tmp_path, name, data_type
):
    with ZipFile(project_file) as archive:
        locals_ = archive.read("s/locals.xml")
    rewrite_members(
        project_file,
        {
            "s/locals.xml": locals_
            + f"+GN=VAR_EXTERNAL\r\n++D={data_type}\tN={name}\tG=VAR_EXTERNAL\r\n".encode(),
            "s/body.xml": b"<StructuredTextModel><Text>Counter := 0;</Text></StructuredTextModel>",
        },
    )
    w, pid, tx = opened(project_file, tmp_path)
    before = w.preview(tx)
    with pytest.raises(ProjectError, match="external"):
        w.update_st(tx, "body", f"{name} := 1;", expected_revision=0)
    assert w.preview(tx) == before


@pytest.mark.parametrize("other_group", ["VAR_EXTERNAL", "VAR"])
def test_external_local_name_collision_is_reported_and_new_use_rejected(
    project_file, tmp_path, other_group
):
    with ZipFile(project_file) as archive:
        locals_ = archive.read("s/locals.xml")
    rewrite_members(
        project_file,
        {
            "s/locals.xml": locals_
            + f"+GN=VAR_EXTERNAL\r\n++D=BOOL\tN=Start\tG=VAR_EXTERNAL\r\n+GN={other_group}\r\n++D=BOOL\tN=Start\tG={other_group}\r\n".encode(),
            "s/body.xml": b"<StructuredTextModel><Text>Counter := 0;</Text></StructuredTextModel>",
            "s/ld.xml": b'{"CLs":[],"LRI":0,"RRI":0,"VLs":[]}\r\n',
        },
    )
    w, pid, tx = opened(project_file, tmp_path)
    validation = w.validate(tx)
    assert validation["external_declaration_check"] == "unresolved_external_declarations"
    assert validation["external_diagnostics_preview"][0]["code"] == "external_declaration_ambiguous"
    before = w.preview(tx)
    with pytest.raises(ProjectError, match="external"):
        w.update_st(tx, "body", "Start := FALSE;", expected_revision=0)
    assert w.preview(tx) == before
