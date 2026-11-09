from uuid import UUID
from zipfile import ZipFile

import pytest
from lxml import etree as E

from sysmac_mcp.project import NativeProject
from sysmac_mcp.workspace import ProjectError, Workspace


def replace_members(path, replacements):
    with ZipFile(path) as z:
        members = {i.filename: z.read(i) for i in z.infolist()}
    members.update(replacements)
    with ZipFile(path, "w") as z:
        for name, raw in members.items():
            z.writestr(name, raw)


@pytest.fixture
def template_project(project_file):
    with ZipFile(project_file) as z:
        oem = E.fromstring(z.read("s/s.oem"))
    main = oem.find('.//Entity[@id="p"]')
    main.set("trackingId", "11111111-1111-1111-1111-111111111111")
    oem.find('.//Entity[@id="ldp"]').set("trackingId", "22222222-2222-2222-2222-222222222222")
    E.SubElement(
        oem.find('.//Entity[@id="ldp"]/ChildEntities'),
        "Entity",
        type="Variables",
        subtype="Ladder",
        id="ld-vars",
        name="Variables",
    )
    children = main.find("ChildEntities")
    E.SubElement(children, "Entity", type="DebugProgramSetting", id="debug", name="Debug")
    body = main.find('./ChildEntities/Entity[@type="PouBody"]')
    bc = E.SubElement(body, "ChildEntities")
    E.SubElement(bc, "Entity", type="SourceHolder", id="body-source", name="Source")
    E.SubElement(bc, "Entity", type="PouBodySourceHolder", id="cache", name="Cache")
    task = oem.find('.//Entity[@id="task"]')
    tc = E.SubElement(task, "ChildEntities")
    E.SubElement(
        tc,
        "Entity",
        type="NexAssociatedProgram",
        subtype="StructuredText",
        id="association",
        name="Main",
        trackingId="33333333-3333-3333-3333-333333333333",
    )
    cpu = oem.find('.//Entity[@id="cpu"]/ChildEntities')
    second = E.SubElement(
        cpu, "Entity", type="NexTask", subtype="Periodic", id="second-task", name="Secondary"
    )
    E.SubElement(second, "ChildEntities")
    replace_members(
        project_file,
        {
            "s/s.oem": E.tostring(oem),
            "s/debug.xml": b'<data><DebugProgramSetting Flag="True"/></data>',
            "s/ld-vars.xml": b"[SLWD version=1.0]\r\n_EN=Variables\r\n+GN=VAR\tGVT=DefaultGroup\r\n",
            "s/cache.xml": b'<data><CxilVariableGroup POUTrackingId="old"/><OldCompiledData>do not clone</OldCompiledData></data>',
            "s/task.xml": b'<data><GeneralSettingsData ExecutionPriority="4" Vendor="keep"/><Programs><AssociatedProgramData ProgramName="Main" InstanceName="Main" IniFileTrackingId="11111111111111111111111111111111" SequenceNumber="1" StartupSetting="TRUE" IsDebugProgram="false" Vendor="preserve"/></Programs></data>',
            "s/second-task.xml": b'<data><GeneralSettingsData ExecutionPriority="16"/><Programs/></data>',
            "s/association.xml": b'<AssociatedProgramModel xmlns="http://schemas.datacontract.org/2004/07/Omron.Cxap.Modules.TaskConfiguration.Models"><PouInstanceName>Main</PouInstanceName></AssociatedProgramModel>',
        },
    )
    return project_file


def definition(name="Worker"):
    return {
        "name": name,
        "source": "Count := Count + 1;",
        "variables": [{"name": "Count", "data_type": "DINT"}],
    }


def test_create_st_pou_isolated_until_commit_and_additions_survive_restart(
    template_project, tmp_path
):
    original = template_project.read_bytes()
    w = Workspace(tmp_path / "w")
    pid = w.open_project(template_project)["project_id"]
    tx = w.begin(pid)["transaction_id"]
    change = w.create_pou(tx, "p", definition(), expected_revision=0)["change"]
    assert len(w.project(pid).pous()) == 2
    w = Workspace(tmp_path / "w")
    staged = w.project(pid, tx)
    pou = staged.read_pou(change["pou_id"])
    assert pou["name"] == "Worker" and pou["kind"] == "Program"
    assert [v["name"] for v in pou["variables"]] == ["Count"]
    assert pou["sections"][0]["source"] == "Count := Count + 1;"
    assert staged.archive.read(staged.member(change["debug_id"])).find(b'Flag="False"') >= 0
    assert b"OldCompiledData" not in staged.archive.read(staged.member(change["cache_ids"][0]))
    old_ids = set(NativeProject(template_project).entities)
    new_ids = set(staged.entities) - old_ids
    assert new_ids and all(UUID(i) for i in new_ids)
    preview = w.preview(tx)
    assert preview["changed_entries"] == ["s/s.oem"]
    assert set(preview["added_entries"]) == set(change["added_members"])
    committed = w.commit(tx)
    assert set(committed["added_entries"]) == set(change["added_members"])
    output = tmp_path / "created.smc2"
    w.export(pid, output)
    assert any(p["name"] == "Worker" for p in NativeProject(output).pous())
    with ZipFile(template_project) as before, ZipFile(output) as after:
        for member in before.namelist():
            if member != "s/s.oem":
                assert before.read(member) == after.read(member)
    assert template_project.read_bytes() == original


def test_new_pou_can_be_edited_in_same_transaction(template_project, tmp_path):
    w = Workspace(tmp_path / "w")
    pid = w.open_project(template_project)["project_id"]
    tx = w.begin(pid)["transaction_id"]
    change = w.create_pou(tx, "p", definition(), expected_revision=0)["change"]
    w.update_st(tx, change["section_id"], "Count := 3;", expected_revision=0)
    w.create_variable(tx, change["table_id"], "Ready", "BOOL", expected_revision=0)
    assert len(w.project(pid, tx).read_pou(change["pou_id"])["variables"]) == 2
    w.commit(tx)
    assert w.project(pid).read_pou(change["pou_id"])["sections"][0]["source"] == "Count := 3;"


def test_create_ld_pou_uses_native_rungs_and_preserves_unknown_rung_fields(
    template_project, tmp_path
):
    w = Workspace(tmp_path / "w")
    pid = w.open_project(template_project)["project_id"]
    tx = w.begin(pid)["transaction_id"]
    rungs = [
        {
            "CLs": [{"__type": "LD", "Var": "Start"}, {"__type": "ST", "Var": "Run"}],
            "LRI": 2,
            "RRI": 3,
            "VLs": [],
            "Vendor": {"keep": True},
        }
    ]
    change = w.create_pou(tx, "ldp", {"name": "WorkerLD", "rungs": rungs}, expected_revision=0)[
        "change"
    ]
    assert w.project(pid, tx).read_pou(change["pou_id"])["sections"][0]["rungs"] == rungs
    w.commit(tx)


@pytest.mark.parametrize(
    "bad",
    [
        {"name": "main", "source": "Run := FALSE;"},
        {"name": "IF", "source": "Run := FALSE;"},
        {
            "name": "Worker",
            "source": "x:=0;",
            "variables": [{"name": "x", "data_type": "BOOL"}, {"name": "X", "data_type": "BOOL"}],
        },
        {
            "name": "Worker",
            "source": "x:=0;",
            "variables": [{"name": "x", "data_type": "BOOL\r\n++N=Injected"}],
        },
        {"name": "Worker", "source": "x:=0;", "rungs": []},
    ],
)
def test_invalid_create_is_atomic(template_project, tmp_path, bad):
    w = Workspace(tmp_path / "w")
    pid = w.open_project(template_project)["project_id"]
    tx = w.begin(pid)["transaction_id"]
    with pytest.raises((ValueError, ProjectError)):
        w.create_pou(tx, "p", bad, expected_revision=0)
    assert w.preview(tx)["changed_entries"] == []
    assert w.preview(tx)["added_entries"] == []


def test_protected_template_does_not_create_partial_pou(template_project, tmp_path):
    replace_members(template_project, {"s/body.xml": b"<EncryptedFile/>"})
    w = Workspace(tmp_path / "w")
    pid = w.open_project(template_project)["project_id"]
    tx = w.begin(pid)["transaction_id"]
    with pytest.raises(ProjectError, match="template"):
        w.create_pou(tx, "p", definition(), expected_revision=0)
    assert not w.preview(tx)["added_entries"]


def test_assign_new_program_updates_task_and_oem_in_one_transaction(template_project, tmp_path):
    w = Workspace(tmp_path / "w")
    pid = w.open_project(template_project)["project_id"]
    tx = w.begin(pid)["transaction_id"]
    created = w.create_pou(tx, "p", definition(), expected_revision=0)["change"]
    w.assign_program(tx, "task", created["pou_id"], position=1, startup=False, expected_revision=0)
    project = w.project(pid, tx)
    task = next(t for t in project.tasks() if t["id"] == "task")
    assert [r["ProgramName"] for r in task["programs"]] == ["Worker", "Main"]
    assert task["programs"][0]["StartupSetting"] == "FALSE"
    assert task["programs"][1]["Vendor"] == "preserve"
    associations = project.entities["task"].findall(
        './ChildEntities/Entity[@type="NexAssociatedProgram"]'
    )
    assert {a.get("name") for a in associations} == {"Worker", "Main"}
    assert b'Vendor="keep"' in project.archive.read("s/task.xml")
    w.commit(tx)
    assert len(w.project(pid).tasks()[0]["programs"]) == 2


def test_move_program_keeps_association_identity_and_both_task_orders(template_project, tmp_path):
    w = Workspace(tmp_path / "w")
    pid = w.open_project(template_project)["project_id"]
    tx = w.begin(pid)["transaction_id"]
    w.assign_program(tx, "second-task", "p", expected_revision=0)
    project = w.project(pid, tx)
    tasks = {t["id"]: t for t in project.tasks()}
    assert tasks["task"]["programs"] == []
    assert tasks["second-task"]["programs"][0]["ProgramName"] == "Main"
    assert project.entities["association"].getparent().getparent().get("id") == "second-task"
    assert project.archive.read("s/association.xml") == NativeProject(
        template_project
    ).archive.read("s/association.xml")


def test_unassign_program_removes_task_links_and_preserves_association_model(
    template_project, tmp_path
):
    w = Workspace(tmp_path / "w")
    pid = w.open_project(template_project)["project_id"]
    tx = w.begin(pid)["transaction_id"]
    w.assign_program(tx, "task", "ldp", expected_revision=0)
    result = w.unassign_program(tx, "task", "p", expected_revision=0)

    project = w.project(pid, tx)
    assert result["change"]["operation"] == "unassign_program"
    programs = project.tasks()[0]["programs"]
    assert len(programs) == 1 and programs[0]["program_id"] == "ldp"
    assert programs[0]["SequenceNumber"] == "1"
    associations = project.entities["task"].findall(
        './ChildEntities/Entity[@type="NexAssociatedProgram"]'
    )
    assert len(associations) == 1
    assert associations[0].get("name") == programs[0]["InstanceName"]
    assert project.archive.read("s/association.xml") == NativeProject(
        template_project
    ).archive.read("s/association.xml")
    assert "s/association.xml" in project.info()["coverage"]["unknown_entries"]
    assert b'Vendor="keep"' in project.archive.read("s/task.xml")


def test_unassign_program_rejects_the_wrong_task_without_staging(template_project, tmp_path):
    w = Workspace(tmp_path / "w")
    pid = w.open_project(template_project)["project_id"]
    tx = w.begin(pid)["transaction_id"]
    with pytest.raises(ProjectError, match="selected task"):
        w.unassign_program(tx, "second-task", "p", expected_revision=0)
    assert w.preview(tx)["changed_entries"] == []


def test_delete_unassigned_pou_removes_oem_link_and_preserves_archive_members(
    template_project, tmp_path
):
    original = NativeProject(template_project)
    entity = original.resolve("ldp", {"Program", "Function", "FunctionBlock"})
    member_names = {
        original.member(child.get("id"))
        for child in [entity, *entity.iterdescendants()]
        if child.tag == "Entity" and original.member(child.get("id"))
    }
    member_bytes = {name: original.archive.read(name) for name in member_names}
    w = Workspace(tmp_path / "w")
    pid = w.open_project(template_project)["project_id"]
    tx = w.begin(pid)["transaction_id"]

    result = w.delete_pou(tx, "ldp", expected_revision=0)

    project = w.project(pid, tx)
    assert result["change"]["operation"] == "delete_pou"
    assert all(pou["id"] != "ldp" for pou in project.pous())
    assert project.archive.names == original.archive.names
    assert all(project.archive.read(name) == raw for name, raw in member_bytes.items())
    assert member_names <= set(project.info()["coverage"]["unknown_entries"])
    assert w.preview(tx)["changed_entries"] == [original.oem_name]


def test_delete_pou_rejects_program_still_assigned_to_task(template_project, tmp_path):
    w = Workspace(tmp_path / "w")
    pid = w.open_project(template_project)["project_id"]
    tx = w.begin(pid)["transaction_id"]
    with pytest.raises(ProjectError, match="Unassign"):
        w.delete_pou(tx, "p", expected_revision=0)
    assert w.preview(tx)["changed_entries"] == []


def test_delete_pou_rejects_cross_controller_task_reference(template_project, tmp_path):
    with ZipFile(template_project) as archive:
        oem = E.fromstring(archive.read("s/s.oem"))
    solution = oem.find('.//Entity[@id="s"]')
    cpu = E.SubElement(
        solution.find("ChildEntities"),
        "Entity",
        type="Device",
        id="cpu2",
        name="OtherController",
    )
    task = E.SubElement(
        E.SubElement(cpu, "ChildEntities"),
        "Entity",
        type="NexTask",
        id="other-task",
        name="OtherTask",
    )
    E.SubElement(task, "ChildEntities")
    replace_members(
        template_project,
        {
            "s/s.oem": E.tostring(oem),
            "s/other-task.xml": b'<data><Programs><AssociatedProgramData ProgramName="Ladder" InstanceName="Ladder" IniFileTrackingId="22222222222222222222222222222222" SequenceNumber="1"/></Programs></data>',
        },
    )
    w = Workspace(tmp_path / "w")
    pid = w.open_project(template_project)["project_id"]
    tx = w.begin(pid)["transaction_id"]
    with pytest.raises(ProjectError, match="another task"):
        w.delete_pou(tx, "ldp", expected_revision=0)
    assert w.preview(tx)["changed_entries"] == []


def test_delete_pou_rejects_an_observed_source_reference(template_project, tmp_path):
    name = NativeProject(template_project).entities["ldp"].get("name")
    replace_members(
        template_project,
        {"s/body.xml": f"<StructuredTextModel><Text>{name}();</Text></StructuredTextModel>".encode()},
    )
    w = Workspace(tmp_path / "w")
    pid = w.open_project(template_project)["project_id"]
    tx = w.begin(pid)["transaction_id"]
    with pytest.raises(ProjectError, match="source"):
        w.delete_pou(tx, "ldp", expected_revision=0)
    assert w.preview(tx)["changed_entries"] == []


def test_delete_pou_rejects_a_variable_type_reference(template_project, tmp_path):
    with ZipFile(template_project) as archive:
        declarations = archive.read("s/globals.xml")
    replace_members(
        template_project,
        {"s/globals.xml": declarations + b"++D=Ladder\tN=Instance\tG=VAR_GLOBAL\r\n"},
    )
    w = Workspace(tmp_path / "w")
    pid = w.open_project(template_project)["project_id"]
    tx = w.begin(pid)["transaction_id"]
    with pytest.raises(ProjectError, match="variable declaration"):
        w.delete_pou(tx, "ldp", expected_revision=0)
    assert w.preview(tx)["changed_entries"] == []


def test_delete_pou_rejects_a_data_type_reference(template_project, tmp_path):
    with ZipFile(template_project) as archive:
        oem = E.fromstring(archive.read("s/s.oem"))
    cpu = oem.find('.//Entity[@type="Device"][@id="cpu"]')
    E.SubElement(
        cpu.find("ChildEntities"),
        "Entity",
        type="DataType",
        subtype="",
        id="types",
        name="Data Types",
    )
    replace_members(
        template_project,
        {
            "s/s.oem": E.tostring(oem),
            "s/types.xml": b'<data Version="1"><DataType Name="Data Types" BaseType=""><DataType Name="CallableRef" BaseType="Ladder"/></DataType></data>',
        },
    )
    w = Workspace(tmp_path / "w")
    pid = w.open_project(template_project)["project_id"]
    tx = w.begin(pid)["transaction_id"]
    with pytest.raises(ProjectError, match="DataType definition"):
        w.delete_pou(tx, "ldp", expected_revision=0)
    assert w.preview(tx)["changed_entries"] == []


def test_delete_pou_rejects_unavailable_controller_source(template_project, tmp_path):
    replace_members(template_project, {"s/body.xml": b"<EncryptedFile/>"})
    w = Workspace(tmp_path / "w")
    pid = w.open_project(template_project)["project_id"]
    tx = w.begin(pid)["transaction_id"]
    with pytest.raises(ProjectError, match="source sections"):
        w.delete_pou(tx, "ldp", expected_revision=0)
    assert w.preview(tx)["changed_entries"] == []


def test_assignment_rejects_inconsistent_tracking_without_staging(template_project, tmp_path):
    with ZipFile(template_project) as z:
        task = z.read("s/task.xml").replace(
            b"11111111111111111111111111111111", b"99999999999999999999999999999999"
        )
    replace_members(template_project, {"s/task.xml": task})
    w = Workspace(tmp_path / "w")
    pid = w.open_project(template_project)["project_id"]
    tx = w.begin(pid)["transaction_id"]
    with pytest.raises(ProjectError):
        w.assign_program(tx, "second-task", "p", expected_revision=0)
    assert w.preview(tx)["changed_entries"] == []


@pytest.mark.parametrize("kind", ["FunctionBlock", "Function"])
def test_callable_pou_has_fresh_parameters_implicit_ports_and_return(
    template_project, tmp_path, kind
):
    with ZipFile(template_project) as z:
        oem = E.fromstring(z.read("s/s.oem"))
    parent = oem.find('.//Entity[@id="cpu"]/ChildEntities')
    template = E.SubElement(
        parent, "Entity", type=kind, subtype="StructuredText", id="callable", name="OldCallable"
    )
    children = E.SubElement(template, "ChildEntities")
    E.SubElement(
        children,
        "Entity",
        type="Variables",
        subtype="StructuredText" + kind,
        id="call-vars",
        name="Variables",
    )
    E.SubElement(
        children, "Entity", type="PouBody", subtype="StructuredText", id="call-body", name="Body"
    )
    implicit = (
        b"++D=BOOL\tN=EN\tG=VAR_INPUT\tOrd=0\tRO=1\r\n"
        if kind == "Function"
        else b"++D=BOOL\tN=ENO\tG=VAR_OUTPUT\tOrd=0\tRO=1\r\n"
    )
    table = (
        b"[SLWD version=1.0]\r\n_EN=Variables\r\n+GN=VAR\tGVT=DefaultGroup\r\n"
        + implicit
        + b"++D=BOOL\tN=OldApplicationFlag\tG=VAR\r\n"
    )
    if kind == "Function":
        table += b"++D=WORD\tN=OldCallable\tG=RETURN\r\n"
    replace_members(
        template_project,
        {
            "s/s.oem": E.tostring(oem),
            "s/call-vars.xml": table,
            "s/call-body.xml": b"<StructuredTextModel><Text>OldApplicationFlag := FALSE;</Text></StructuredTextModel>",
        },
    )
    w = Workspace(tmp_path / "w")
    pid = w.open_project(template_project)["project_id"]
    tx = w.begin(pid)["transaction_id"]
    new = {
        "name": "NewCallable",
        "source": "NewCallable := Amount;" if kind == "Function" else "Result := Amount;",
        "variables": [{"name": "Amount", "data_type": "DINT", "scope": "input"}],
    }
    if kind == "Function":
        new["return_type"] = "DINT"
    else:
        new["variables"].append({"name": "Result", "data_type": "DINT", "scope": "output"})
    created = w.create_pou(tx, "callable", new, expected_revision=0)["change"]
    variables = w.project(pid, tx).variables(created["pou_id"])
    assert not any(v["name"] == "OldApplicationFlag" for v in variables)
    implicit_port = next(v for v in variables if v["name"] in ("EN", "ENO"))
    assert implicit_port["attributes"]["RO"] == "1" and implicit_port["attributes"]["Ord"] == "0"
    assert next(v for v in variables if v["name"] == "Amount")["attributes"]["Ord"] == "1"
    if kind == "Function":
        returned = next(v for v in variables if v["group"] == "RETURN")
        assert returned["name"] == "NewCallable" and returned["data_type"] == "DINT"


def test_ld_inline_gets_own_fresh_entity_and_eid(template_project, tmp_path):
    w = Workspace(tmp_path / "w")
    pid = w.open_project(template_project)["project_id"]
    tx = w.begin(pid)["transaction_id"]
    rung = {
        "CLs": [{"__type": "IST", "EID": "old-id", "TXT": "Run := Start;"}],
        "LRI": 1,
        "RRI": 2,
        "VLs": [],
    }
    created = w.create_pou(
        tx, "ldp", {"name": "InlineProgram", "rungs": [rung]}, expected_revision=0
    )["change"]
    project = w.project(pid, tx)
    component = project.read_pou(created["pou_id"])["sections"][0]["rungs"][0]["CLs"][0]
    inline = project.entities[created["section_id"]].find('./ChildEntities/Entity[@type="Inline"]')
    assert UUID(component["EID"]) and component["EID"] != "old-id"
    assert inline.get("trackingId") == inline.get("name") == component["EID"]
    assert rung["CLs"][0]["EID"] == "old-id"  # Caller model is immutable.


def test_unknown_template_child_is_rejected(template_project, tmp_path):
    with ZipFile(template_project) as z:
        oem = E.fromstring(z.read("s/s.oem"))
    E.SubElement(
        oem.find('.//Entity[@id="p"]/ChildEntities'),
        "Entity",
        type="VendorSpecialCode",
        id="unknown",
        name="Opaque",
    )
    replace_members(template_project, {"s/s.oem": E.tostring(oem)})
    w = Workspace(tmp_path / "w")
    pid = w.open_project(template_project)["project_id"]
    tx = w.begin(pid)["transaction_id"]
    with pytest.raises(ProjectError, match="template"):
        w.create_pou(tx, "p", definition(), expected_revision=0)
    assert not w.preview(tx)["added_entries"]


def test_move_rejects_wrong_association_model_shape(template_project, tmp_path):
    replace_members(
        template_project,
        {
            "s/association.xml": b"<UnknownModel><PouInstanceName>Main</PouInstanceName></UnknownModel>"
        },
    )
    w = Workspace(tmp_path / "w")
    pid = w.open_project(template_project)["project_id"]
    tx = w.begin(pid)["transaction_id"]
    with pytest.raises(ProjectError):
        w.assign_program(tx, "second-task", "p", expected_revision=0)
    assert not w.preview(tx)["changed_entries"]


def test_assignment_rejects_cross_controller_move(template_project, tmp_path):
    with ZipFile(template_project) as z:
        oem = E.fromstring(z.read("s/s.oem"))
    second = oem.find('.//Entity[@id="second-task"]')
    parent = E.SubElement(
        oem.find('.//Entity[@id="s"]/ChildEntities'),
        "Entity",
        type="Device",
        id="cpu2",
        name="OtherController",
    )
    E.SubElement(parent, "ChildEntities").append(second)
    replace_members(template_project, {"s/s.oem": E.tostring(oem)})
    w = Workspace(tmp_path / "w")
    pid = w.open_project(template_project)["project_id"]
    tx = w.begin(pid)["transaction_id"]
    with pytest.raises(ProjectError, match="same controller"):
        w.assign_program(tx, "second-task", "p", expected_revision=0)


async def test_create_and_assign_through_mcp_protocol(template_project, tmp_path):
    from mcp import Client

    from sysmac_mcp.server import create_server

    async with Client(create_server(tmp_path / "mcp")) as client:

        async def invoke(tool_name, **arguments):
            result = await client.call_tool(tool_name, arguments)
            assert not result.is_error, result.content
            return result.structured_content

        pid = (await invoke("plc_open_project", path=str(template_project)))["project_id"]
        tx = (await invoke("plc_begin_transaction", project_id=pid))["transaction_id"]
        created = await invoke(
            "plc_create_pou",
            transaction_id=tx,
            template_pou_id="p",
            definition=definition(),
            expected_revision=0,
        )
        new_id = created["change"]["pou_id"]
        pous = await invoke("plc_list_pous", project_id=pid, transaction_id=tx)
        assert any(p["id"] == new_id for p in pous["items"])
        await invoke(
            "plc_assign_program",
            transaction_id=tx,
            task_id="task",
            program_id=new_id,
            expected_revision=0,
        )
        tasks = await invoke("plc_list_tasks", project_id=pid, transaction_id=tx)
        assert tasks["items"][0]["programs"][-1]["program_id"] == new_id
        await invoke(
            "plc_unassign_program",
            transaction_id=tx,
            task_id="task",
            program_id=new_id,
            expected_revision=0,
        )
        tasks = await invoke("plc_list_tasks", project_id=pid, transaction_id=tx)
        assert all(row["program_id"] != new_id for row in tasks["items"][0]["programs"])
        committed = await invoke("plc_commit_changes", transaction_id=tx)
        assert committed["added_entries"]
        assert committed["sysmac_build"] == "not_executed"


async def test_pou_deletion_through_mcp_protocol(template_project, tmp_path):
    from mcp import Client

    from sysmac_mcp.server import create_server

    async with Client(create_server(tmp_path / "mcp")) as client:
        async def invoke(tool_name, **arguments):
            result = await client.call_tool(tool_name, arguments)
            assert not result.is_error, result.content
            return result.structured_content

        pid = (await invoke("plc_open_project", path=str(template_project)))["project_id"]
        tx = (await invoke("plc_begin_transaction", project_id=pid))["transaction_id"]
        deleted = await invoke(
            "plc_delete_pou",
            transaction_id=tx,
            pou_id="ldp",
            expected_revision=0,
        )
        assert deleted["change"]["operation"] == "delete_pou"
        pous = await invoke("plc_list_pous", project_id=pid, transaction_id=tx)
        assert all(pou["id"] != "ldp" for pou in pous["items"])
        committed = await invoke("plc_commit_changes", transaction_id=tx)
        assert committed["changed_entries"] == ["s/s.oem"]


def test_duplicate_program_tracking_is_rejected_before_assignment(template_project, tmp_path):
    with ZipFile(template_project) as z:
        oem = E.fromstring(z.read("s/s.oem"))
    E.SubElement(
        oem.find('.//Entity[@id="cpu"]/ChildEntities'),
        "Entity",
        type="Program",
        subtype="StructuredText",
        id="other-program",
        name="Other",
        trackingId="22222222-2222-2222-2222-222222222222",
    )
    replace_members(template_project, {"s/s.oem": E.tostring(oem)})
    w = Workspace(tmp_path / "w")
    pid = w.open_project(template_project)["project_id"]
    tx = w.begin(pid)["transaction_id"]
    with pytest.raises(ProjectError):
        w.assign_program(tx, "second-task", "ldp", expected_revision=0)
    assert not w.preview(tx)["added_entries"]


def test_instance_name_collision_is_case_insensitive(template_project, tmp_path):
    with ZipFile(template_project) as z:
        oem = E.fromstring(z.read("s/s.oem"))
        task = z.read("s/task.xml").replace(b'InstanceName="Main"', b'InstanceName="worker"')
        model = z.read("s/association.xml").replace(b">Main<", b">worker<")
    oem.find('.//Entity[@id="association"]').set("name", "worker")
    replace_members(
        template_project,
        {"s/s.oem": E.tostring(oem), "s/task.xml": task, "s/association.xml": model},
    )
    w = Workspace(tmp_path / "w")
    pid = w.open_project(template_project)["project_id"]
    tx = w.begin(pid)["transaction_id"]
    created = w.create_pou(tx, "p", definition("Worker"), expected_revision=0)["change"]
    before = w.preview(tx)
    with pytest.raises(ProjectError):
        w.assign_program(tx, "task", created["pou_id"], expected_revision=0)
    assert w.preview(tx) == before


@pytest.mark.parametrize("location", ["p", "body"])
def test_template_application_xml_children_cannot_be_cloned(template_project, tmp_path, location):
    with ZipFile(template_project) as z:
        oem = E.fromstring(z.read("s/s.oem"))
    E.SubElement(
        oem.find(f'.//Entity[@id="{location}"]'), "ApplicationCache", SourceId="body"
    ).text = "Old application data"
    replace_members(template_project, {"s/s.oem": E.tostring(oem)})
    w = Workspace(tmp_path / "w")
    pid = w.open_project(template_project)["project_id"]
    tx = w.begin(pid)["transaction_id"]
    with pytest.raises(ProjectError, match="template"):
        w.create_pou(tx, "p", definition(), expected_revision=0)
    assert not w.preview(tx)["added_entries"]


@pytest.mark.parametrize("container_name", ["AccessInfos", "ChildEntities"])
def test_duplicate_template_containers_cannot_hide_application_data(
    template_project, tmp_path, container_name
):
    with ZipFile(template_project) as z:
        oem = E.fromstring(z.read("s/s.oem"))
    template = oem.find('.//Entity[@id="p"]')
    if template.find(container_name) is None:
        E.SubElement(template, container_name)
    duplicate = E.SubElement(template, container_name)
    if container_name == "AccessInfos":
        duplicate.text = "Old application metadata"
    else:
        E.SubElement(duplicate, "ApplicationCache", SourceId="body")
    replace_members(template_project, {"s/s.oem": E.tostring(oem)})
    w = Workspace(tmp_path / "w")
    pid = w.open_project(template_project)["project_id"]
    tx = w.begin(pid)["transaction_id"]
    with pytest.raises(ProjectError):
        w.create_pou(tx, "p", definition(), expected_revision=0)
    assert not w.preview(tx)["added_entries"]


def test_association_child_in_foreign_namespace_is_rejected(template_project, tmp_path):
    with ZipFile(template_project) as z:
        model = z.read("s/association.xml").replace(
            b"<PouInstanceName>", b'<PouInstanceName xmlns="urn:unrelated">'
        )
    replace_members(template_project, {"s/association.xml": model})
    w = Workspace(tmp_path / "w")
    pid = w.open_project(template_project)["project_id"]
    tx = w.begin(pid)["transaction_id"]
    with pytest.raises(ProjectError):
        w.assign_program(tx, "second-task", "p", expected_revision=0)
    assert not w.preview(tx)["changed_entries"]


def test_new_variable_rows_stay_inside_their_slwd_groups(template_project, tmp_path):
    w = Workspace(tmp_path / "w")
    pid = w.open_project(template_project)["project_id"]
    tx = w.begin(pid)["transaction_id"]
    created = w.create_pou(tx, "p", definition(), expected_revision=0)["change"]
    project = w.project(pid, tx)
    lines = project.archive.read(project.member(created["table_id"])).decode().splitlines()
    local = next(n for n, line in enumerate(lines) if line.startswith("++") and "N=Count\t" in line)
    assert (
        lines.index("+GN=VAR\tGVT=DefaultGroup")
        < local
        < lines.index("+GN=VAR_EXTERNAL\tGA=External\tGVT=ExternalGroup")
    )


@pytest.mark.parametrize("operation", ["rename", "update", "bind", "unbind"])
def test_read_only_native_declarations_cannot_be_changed(template_project, tmp_path, operation):
    with ZipFile(template_project) as z:
        variables = z.read("s/locals.xml").replace(b"N=Counter\tG=VAR", b"N=Counter\tG=VAR\tRO=1")
    with ZipFile(template_project) as z:
        globals_raw = z.read("s/globals.xml").replace(b"N=Start\t", b"N=Start\tRO=1\t")
    replace_members(template_project, {"s/locals.xml": variables, "s/globals.xml": globals_raw})
    w = Workspace(tmp_path / "w")
    pid = w.open_project(template_project)["project_id"]
    variable = next(
        v
        for v in w.project(pid).variables()
        if v["name"] == ("Start" if operation in ("bind", "unbind") else "Counter")
    )
    tx = w.begin(pid)["transaction_id"]
    with pytest.raises(ProjectError):
        if operation == "rename":
            w.rename_variable(tx, variable["id"], "OtherCounter", expected_revision=0)
        elif operation == "update":
            w.update_variable(tx, variable["id"], {"data_type": "DINT"}, expected_revision=0)
        elif operation == "bind":
            w.bind_io(tx, variable["id"], variable["address"], expected_revision=0)
        else:
            w.unbind_io(tx, variable["id"], expected_revision=0)
    assert not w.preview(tx)["changed_entries"]
