from zipfile import ZipFile

import pytest
from lxml import etree as E

from sysmac_mcp.project import NativeProject
from sysmac_mcp.workspace import ProjectError, Workspace


def replace_members(path, replacements):
    with ZipFile(path) as archive:
        members = {info.filename: archive.read(info) for info in archive.infolist()}
    members.update(replacements)
    with ZipFile(path, "w") as archive:
        for name, raw in members.items():
            archive.writestr(name, raw)


def source_model(source):
    root = E.Element("StructuredTextModel")
    E.SubElement(root, "Text").text = source
    return E.tostring(root)


def declaration(project, name, scope="global"):
    return next(v for v in project.variables(scope) if v["name"] == name)


def index(path):
    from sysmac_mcp.symbols import SymbolIndex

    return SymbolIndex(NativeProject(path))


def test_lexer_distinguishes_literals_from_identifiers():
    from sysmac_mcp.st_lexer import lex

    source = "Run := A > UINT#16#A; Hold := T#10ms+Delay; (* A (* nested *) *) X := 'A$'A';"
    result = lex(source)
    assert not result.diagnostics
    assert [t.text for t in result.tokens if t.kind == "identifier"] == [
        "Run",
        "A",
        "Hold",
        "Delay",
        "X",
    ]


@pytest.mark.parametrize("source", ["Run := 'unfinished", "Run := A; (* unfinished"])
def test_lexer_reports_incomplete_source(source):
    from sysmac_mcp.st_lexer import lex

    assert lex(source).diagnostics


def test_bound_references_exclude_strings_comments_and_include_ld(project_file):
    project = NativeProject(project_file)
    start = declaration(project, "Start")
    result = index(project_file).analyze(start["id"])
    assert result["canonical_variable_id"] == start["id"]
    assert len(result["references"]) == 2
    assert {r["language"] for r in result["references"]} == {"ST", "LD"}
    assert all(r["variable_id"] == start["id"] for r in result["references"])
    assert result["coverage"]["sysmac_build"] == "not_executed"


def test_local_shadow_is_not_a_global_reference(project_file):
    with ZipFile(project_file) as z:
        local = z.read("s/locals.xml")
    replace_members(project_file, {"s/locals.xml": local + b"++D=BOOL\tN=Start\tG=VAR\r\n"})
    project = NativeProject(project_file)
    global_start = declaration(project, "Start")
    local_start = declaration(project, "Start", "p")
    assert {
        r["section_id"] for r in index(project_file).analyze(global_start["id"])["references"]
    } == {"ld"}
    assert {
        r["section_id"] for r in index(project_file).analyze(local_start["id"])["references"]
    } == {"body"}


def test_external_declaration_resolves_to_global_identity(project_file):
    with ZipFile(project_file) as z:
        local = z.read("s/locals.xml")
    replace_members(
        project_file, {"s/locals.xml": local + b"++D=BOOL\tN=Start\tG=VAR_EXTERNAL\r\n"}
    )
    project = NativeProject(project_file)
    global_start = declaration(project, "Start")
    external = declaration(project, "Start", "p")
    result = index(project_file).analyze(external["id"])
    assert result["canonical_variable_id"] == global_start["id"]
    assert {v["id"] for v in result["declarations"]} == {global_start["id"], external["id"]}
    assert len(result["references"]) == 2


def test_external_type_mismatch_is_diagnostic(project_file):
    with ZipFile(project_file) as z:
        local = z.read("s/locals.xml")
    replace_members(project_file, {"s/locals.xml": local + b"++D=INT\tN=Start\tG=VAR_EXTERNAL\r\n"})
    start = declaration(NativeProject(project_file), "Start")
    result = index(project_file).analyze(start["id"])
    assert any(d["code"] == "external_type_mismatch" for d in result["diagnostics"])


@pytest.fixture
def member_project(project_file):
    with ZipFile(project_file) as z:
        oem = E.fromstring(z.read("s/s.oem"))
        globals_raw = z.read("s/globals.xml")
    cpu = oem.find('.//Entity[@id="cpu"]/ChildEntities')
    E.SubElement(cpu, "Entity", type="DataType", id="types", name="Types")
    fb = E.SubElement(cpu, "Entity", type="FunctionBlock", id="fb", name="Gate")
    children = E.SubElement(fb, "ChildEntities")
    E.SubElement(children, "Entity", type="Variables", id="fbvars", name="Variables")
    E.SubElement(
        children, "Entity", type="PouBody", subtype="StructuredText", id="fbbody", name="Code"
    )
    globals_raw += b"++D=S\tN=Data\tG=VAR_GLOBAL\r\n++D=Gate\tN=Gate1\tG=VAR_GLOBAL\r\n++D=ARRAY[0..3] OF S\tN=Arr\tG=VAR_GLOBAL\r\n++D=INT\tN=Index\tG=VAR_GLOBAL\r\n"
    replace_members(
        project_file,
        {
            "s/s.oem": E.tostring(oem),
            "s/globals.xml": globals_raw,
            "s/types.xml": b'<data><DataType Name="Types" BaseType=""><DataType Name="S" BaseType="STRUCT"><DataType Name="Start" BaseType="BOOL"/></DataType></DataType></data>',
            "s/fbvars.xml": b"[SLWD version=1.0]\r\n_EN=Variables\r\n+GN=VAR_INPUT\r\n++D=BOOL\tN=Start\tG=VAR_INPUT\r\n+GN=VAR_OUTPUT\r\n++D=BOOL\tN=Q\tG=VAR_OUTPUT\r\n",
            "s/fbbody.xml": source_model("Q := Start;"),
        },
    )
    return project_file


def test_struct_member_homonym_is_not_global_reference(member_project):
    replace_members(member_project, {"s/body.xml": source_model("Run := Data.Start AND Start;")})
    start = declaration(NativeProject(member_project), "Start")
    refs = index(member_project).analyze(start["id"])["references"]
    assert len(refs) == 2  # one ST, one Ladder
    assert next(r for r in refs if r["section_id"] == "body")["start"] == 22


def test_fb_named_arguments_and_member_access_resolve_parameter(member_project):
    replace_members(
        member_project, {"s/body.xml": source_model("Gate1(Start := Start); Run := Gate1.Start;")}
    )
    project = NativeProject(member_project)
    parameter = declaration(project, "Start", "fb")
    refs = index(member_project).analyze(parameter["id"])["references"]
    assert len(refs) == 3
    assert {r["role"] for r in refs} >= {"named_argument", "member", "variable"}
    global_start = declaration(project, "Start")
    assert len(index(member_project).analyze(global_start["id"])["references"]) == 2


def test_array_member_and_index_have_distinct_bindings(member_project):
    replace_members(
        member_project, {"s/body.xml": source_model("Run := Arr[Index].Start AND Start;")}
    )
    project = NativeProject(member_project)
    symbols = index(member_project)
    assert len(symbols.analyze(declaration(project, "Index")["id"])["references"]) == 1
    assert len(symbols.analyze(declaration(project, "Start")["id"])["references"]) == 2


def test_global_rename_updates_external_st_and_ld_atomically(project_file, tmp_path):
    with ZipFile(project_file) as z:
        local = z.read("s/locals.xml")
    replace_members(
        project_file,
        {"s/locals.xml": b"\xef\xbb\xbf" + local + b"++D=BOOL\tN=Start\tG=VAR_EXTERNAL\r\n"},
    )
    original = project_file.read_bytes()
    w = Workspace(tmp_path / "workspace")
    pid = w.open_project(project_file)["project_id"]
    start = declaration(w.project(pid), "Start")
    plan = index(project_file).plan_rename(start["id"], "Enable")
    assert plan["can_apply"] and not plan["blockers"]
    assert len(plan["declarations"]) == 2 and len(plan["references"]) == 2
    tx = w.begin(pid)["transaction_id"]
    staged = w.rename_variable(tx, start["id"], "Enable", expected_revision=0)
    assert staged["change"]["new_variable_id"] != start["id"]
    assert set(w.preview(tx)["changed_entries"]) == {
        "s/globals.xml",
        "s/locals.xml",
        "s/body.xml",
        "s/ld.xml",
    }
    assert declaration(w.project(pid), "Start")
    w = Workspace(tmp_path / "workspace")  # Staging survives a process restart.
    candidate = w.project(pid, tx)
    assert declaration(candidate, "Enable", "p")["group"] == "VAR_EXTERNAL"
    assert candidate.archive.read("s/locals.xml").startswith(b"\xef\xbb\xbf")
    assert "Run := Enable; // Start in comment" in candidate.read_pou("p")["sections"][0]["source"]
    assert "Message := 'Start';" in candidate.read_pou("p")["sections"][0]["source"]
    w.commit(tx)
    assert project_file.read_bytes() == original
    assert w.project(pid).archive.read("s/unrecognised.bin") == b"\x00\xffopaque data"


def test_local_rename_preserves_global_and_other_pou_homonym(project_file, tmp_path):
    with ZipFile(project_file) as z:
        local = z.read("s/locals.xml")
    replace_members(project_file, {"s/locals.xml": local + b"++D=BOOL\tN=Start\tG=VAR\r\n"})
    w = Workspace(tmp_path / "w")
    pid = w.open_project(project_file)["project_id"]
    local_start = declaration(w.project(pid), "Start", "p")
    tx = w.begin(pid)["transaction_id"]
    w.rename_variable(tx, local_start["id"], "LocalStart", expected_revision=0)
    p = w.project(pid, tx)
    assert declaration(p, "Start")
    assert p.read_pou("ldp")["sections"][0]["rungs"][0]["CLs"][0]["Var"] == "Start"
    assert len(index(project_file).plan_rename(local_start["id"], "LocalStart")["references"]) == 1


@pytest.mark.parametrize("new_name", ["Run", "IF", "Two Words", "a\tN=Bad"])
def test_rename_rejects_collision_keyword_and_delimiters(project_file, new_name):
    start = declaration(NativeProject(project_file), "Start")
    assert not index(project_file).plan_rename(start["id"], new_name)["can_apply"]


def test_rename_rejects_reference_capture_in_other_scope(project_file):
    with ZipFile(project_file) as z:
        local = z.read("s/locals.xml")
    replace_members(project_file, {"s/locals.xml": local + b"++D=BOOL\tN=Enable\tG=VAR\r\n"})
    start = declaration(NativeProject(project_file), "Start")
    plan = index(project_file).plan_rename(start["id"], "Enable")
    assert not plan["can_apply"]
    assert any(b["code"] == "reference_capture" for b in plan["blockers"])


def test_local_rename_cannot_capture_preexisting_global_reference(project_file):
    replace_members(project_file, {"s/body.xml": source_model("Counter := 1; Run := Start;")})
    counter = declaration(NativeProject(project_file), "Counter", "p")
    plan = index(project_file).plan_rename(counter["id"], "Start")
    assert not plan["can_apply"]
    assert any(b["code"] == "reference_capture" for b in plan["blockers"])


@pytest.mark.parametrize("body", [b"<EncryptedFile/>", source_model("Run := Start; (* incomplete")])
def test_incomplete_affected_source_blocks_rename_without_partial_staging(
    project_file, tmp_path, body
):
    replace_members(project_file, {"s/body.xml": body})
    w = Workspace(tmp_path / "w")
    pid = w.open_project(project_file)["project_id"]
    start = declaration(w.project(pid), "Start")
    tx = w.begin(pid)["transaction_id"]
    with pytest.raises(ProjectError):
        w.rename_variable(tx, start["id"], "Enable", expected_revision=0)
    assert w.preview(tx)["changed_entries"] == []


def test_fb_parameter_rename_updates_named_arguments_and_selectors(member_project, tmp_path):
    replace_members(
        member_project, {"s/body.xml": source_model("Gate1(Start := Start); Run := Gate1.Start;")}
    )
    w = Workspace(tmp_path / "w")
    pid = w.open_project(member_project)["project_id"]
    parameter = declaration(w.project(pid), "Start", "fb")
    tx = w.begin(pid)["transaction_id"]
    w.rename_variable(tx, parameter["id"], "Enable", expected_revision=0)
    p = w.project(pid, tx)
    assert (
        p.read_pou("p")["sections"][0]["source"] == "Gate1(Enable := Start); Run := Gate1.Enable;"
    )
    assert p.read_pou("fb")["sections"][0]["source"] == "Q := Enable;"


def test_renaming_external_alias_requires_global_declaration(project_file):
    with ZipFile(project_file) as z:
        local = z.read("s/locals.xml")
    replace_members(
        project_file, {"s/locals.xml": local + b"++D=BOOL\tN=Start\tG=VAR_EXTERNAL\r\n"}
    )
    external = declaration(NativeProject(project_file), "Start", "p")
    plan = index(project_file).plan_rename(external["id"], "Enable")
    assert not plan["can_apply"]
    assert any(b["code"] == "select_global_declaration" for b in plan["blockers"])


def test_lexer_accepts_signed_typed_numbers_and_short_time_prefix_variables():
    from sysmac_mcp.st_lexer import lex

    result = lex("T := INT#-1; D := DT#2024-01-02-03:04:05.5; T := T#1ms+T;")
    assert not result.diagnostics
    assert [t.text for t in result.tokens if t.kind == "identifier"] == ["T", "D", "T", "T"]


def test_ladder_fb_parameter_labels_and_inline_st_are_renamed(member_project, tmp_path):
    import json

    rungs = [
        {
            "CLs": [
                {
                    "__type": "FB",
                    "Name": "Gate",
                    "Var": "Gate1",
                    "In": [{"__type": "PRM", "Arg": "Start", "Var": "Start"}],
                    "Out": [],
                },
                {"__type": "IST", "TXT": "Gate1(Start := Start); // Start"},
                {"__type": "COMMENT", "TXT": "Start is a comment"},
            ],
            "LRI": 1,
            "RRI": 2,
            "VLs": [],
            "Unknown": {"Name": "Start"},
        }
    ]
    replace_members(member_project, {"s/ld.xml": b"\xef\xbb\xbf" + json.dumps(rungs[0]).encode()})
    w = Workspace(tmp_path / "w")
    pid = w.open_project(member_project)["project_id"]
    parameter = declaration(w.project(pid), "Start", "fb")
    tx = w.begin(pid)["transaction_id"]
    w.rename_variable(tx, parameter["id"], "Enable", expected_revision=0)
    rung = w.project(pid, tx).read_pou("ldp")["sections"][0]["rungs"][0]
    assert rung["CLs"][0]["In"][0] == {"__type": "PRM", "Arg": "Enable", "Var": "Start"}
    assert rung["CLs"][1]["TXT"] == "Gate1(Enable := Start); // Start"
    assert rung["CLs"][2]["TXT"] == "Start is a comment"
    assert rung["Unknown"]["Name"] == "Start"


def test_direct_function_named_argument_retains_call_site_scope(member_project):
    # A direct function call has no instance variable; call site and callee IDs differ.
    with ZipFile(member_project) as z:
        oem = E.fromstring(z.read("s/s.oem"))
    oem.find('.//Entity[@id="fb"]').set("type", "Function")
    replace_members(
        member_project,
        {"s/s.oem": E.tostring(oem), "s/body.xml": source_model("Run := Gate(Start := Start);")},
    )
    project = NativeProject(member_project)
    parameter = declaration(project, "Start", "fb")
    references = index(member_project).analyze(parameter["id"])["references"]
    assert any(r["pou_id"] == "p" and r["role"] == "named_argument" for r in references)


def test_duplicate_external_declarations_and_hidden_initializer_refs_block_rename(project_file):
    with ZipFile(project_file) as z:
        raw = z.read("s/globals.xml")
    raw += b"++D=BOOL\tN=Initial\tIV=Start\tG=VAR_GLOBAL\r\n"
    replace_members(project_file, {"s/globals.xml": raw})
    start = declaration(NativeProject(project_file), "Start")
    plan = index(project_file).plan_rename(start["id"], "Enable")
    assert any(b["code"] == "declaration_expression_not_supported" for b in plan["blockers"])


def test_unbalanced_delimiters_block_rename(project_file):
    replace_members(project_file, {"s/body.xml": source_model("Run := Start];")})
    start = declaration(NativeProject(project_file), "Start")
    assert not index(project_file).plan_rename(start["id"], "Enable")["can_apply"]


def test_failed_multi_member_rename_preserves_previous_staging(project_file, tmp_path):
    w = Workspace(tmp_path / "w")
    pid = w.open_project(project_file)["project_id"]
    start = declaration(w.project(pid), "Start")
    tx = w.begin(pid)["transaction_id"]
    w.update_variable(tx, start["id"], {"comment": "Previously staged"}, expected_revision=0)
    before = w.preview(tx)
    with pytest.raises(ProjectError):
        w.rename_variable(tx, start["id"], "Run", expected_revision=0)
    assert w.preview(tx) == before


def test_local_new_name_cannot_capture_initializer_reference(project_file):
    with ZipFile(project_file) as z:
        raw = z.read("s/locals.xml")
    replace_members(
        project_file,
        {
            "s/locals.xml": raw + b"++D=BOOL\tN=Cached\tIV=Start\tG=VAR\r\n",
            "s/body.xml": source_model("Counter := 1;"),
        },
    )
    counter = declaration(NativeProject(project_file), "Counter", "p")
    assert not index(project_file).plan_rename(counter["id"], "Start")["can_apply"]


def test_unknown_ladder_metadata_is_preserved_during_rename(project_file, tmp_path):
    import json

    with ZipFile(project_file) as z:
        rung = json.loads(z.read("s/ld.xml").decode("utf-8-sig"))
    rung["VendorExtension"] = {
        "Var": "Start",
        "TXT": "Start",
        "nested": {"__type": "PRM", "Var": "Start"},
    }
    replace_members(project_file, {"s/ld.xml": json.dumps(rung).encode()})
    w = Workspace(tmp_path / "w")
    pid = w.open_project(project_file)["project_id"]
    start = declaration(w.project(pid), "Start")
    tx = w.begin(pid)["transaction_id"]
    w.rename_variable(tx, start["id"], "Enable", expected_revision=0)
    candidate = w.project(pid, tx).read_pou("ldp")["sections"][0]["rungs"][0]
    assert candidate["CLs"][0]["Var"] == "Enable"
    assert candidate["VendorExtension"] == rung["VendorExtension"]


def test_direct_addresses_are_not_variable_identifiers():
    from sysmac_mcp.st_lexer import lex

    result = lex("IX0 := %IX0.0; Value := %MW12;")
    assert not result.diagnostics
    assert [t.text for t in result.tokens if t.kind == "identifier"] == ["IX0", "Value"]


def test_native_f_instruction_and_horizontal_line_are_supported(member_project, tmp_path):
    import json

    with ZipFile(member_project) as z:
        oem = E.fromstring(z.read("s/s.oem"))
    oem.find('.//Entity[@id="fb"]').set("type", "Function")
    rung = {
        "CLs": [
            {"__type": "HL", "X": 1},
            {
                "__type": "F",
                "Name": "Gate",
                "In": [{"__type": "PRM", "Arg": "Start", "Var": "Start"}],
                "Out": [],
            },
        ],
        "LRI": 1,
        "RRI": 2,
        "VLs": [],
    }
    replace_members(
        member_project, {"s/s.oem": E.tostring(oem), "s/ld.xml": json.dumps(rung).encode()}
    )
    w = Workspace(tmp_path / "w")
    pid = w.open_project(member_project)["project_id"]
    parameter = declaration(w.project(pid), "Start", "fb")
    tx = w.begin(pid)["transaction_id"]
    w.rename_variable(tx, parameter["id"], "Enable", expected_revision=0)
    output = w.project(pid, tx).read_pou("ldp")["sections"][0]["rungs"][0]
    assert output["CLs"][0] == rung["CLs"][0]
    assert output["CLs"][1]["In"][0] == {"__type": "PRM", "Arg": "Enable", "Var": "Start"}


def test_rename_preserves_variable_field_order_when_name_is_first(project_file, tmp_path):
    with ZipFile(project_file) as z:
        raw = z.read("s/globals.xml").replace(b"++D=BOOL\tN=Start", b"++N=Start\tD=BOOL")
    replace_members(project_file, {"s/globals.xml": raw})
    w = Workspace(tmp_path / "w")
    pid = w.open_project(project_file)["project_id"]
    start = declaration(w.project(pid), "Start")
    tx = w.begin(pid)["transaction_id"]
    w.rename_variable(tx, start["id"], "Enable", expected_revision=0)
    changed = w.project(pid, tx).archive.read("s/globals.xml")
    assert b"++N=Enable\tD=BOOL" in changed
    assert b"Unknown=preserve" in changed
