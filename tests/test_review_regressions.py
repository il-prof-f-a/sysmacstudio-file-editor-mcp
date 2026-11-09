import sqlite3
from threading import Event, Thread, current_thread
from zipfile import ZipFile

import pytest
from lxml import etree as E

from sysmac_mcp.iec_xml import PouSpec, VariableSpec, build_iec_project, validate_iec_xml
from sysmac_mcp.project import NativeProject
from sysmac_mcp.workspace import ProjectError, Workspace

SCHEMA = r"C:\Program Files\OMRON\Sysmac Studio\Sample\IEC 61131-10 XML\Controller\IEC61131_10_Ed1_0_Spc1_0.xsd"


def test_commit_and_rollback_cannot_both_succeed(project_file, tmp_path):
    w = Workspace(tmp_path / "workspace")
    pid = w.open_project(project_file)["project_id"]
    tx = w.begin(pid)["transaction_id"]
    w.update_st(tx, "body", "Run := FALSE;", expected_revision=0)
    ready, resume = Event(), Event()
    original_connect = w.connect
    outcomes = {}

    def instrumented_connect():
        connection = original_connect()
        if current_thread().name == "rollback-thread":

            def trace(sql):
                if "SET status='rolled_back'" in sql:
                    ready.set()
                    assert resume.wait(10), "rollback barrier not released"

            connection.set_trace_callback(trace)
        return connection

    def run_rollback():
        try:
            outcomes["rollback"] = w.rollback(tx)
        except Exception as exc:
            outcomes["rollback_error"] = exc

    w.connect = instrumented_connect
    thread = Thread(target=run_rollback, name="rollback-thread")
    thread.start()
    try:
        assert ready.wait(10), "rollback did not reach barrier"
        probe = original_connect()
        try:
            probe.execute("PRAGMA busy_timeout=0")
            try:
                probe.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                assert "locked" in str(exc)
                locked = True
            else:
                locked = False
                probe.rollback()
        finally:
            probe.close()
        if not locked:
            outcomes["commit"] = w.commit(tx)
    finally:
        resume.set()
        thread.join(timeout=10)
        w.connect = original_connect
    assert not thread.is_alive()
    if locked:
        with pytest.raises(ProjectError, match="active"):
            w.commit(tx)
    assert ("rollback" in outcomes) != ("commit" in outcomes), outcomes
    with original_connect() as connection:
        status = connection.execute("SELECT status FROM transactions WHERE id=?", (tx,)).fetchone()[
            0
        ]
    assert status == ("committed" if "commit" in outcomes else "rolled_back")


def test_bom_variable_table_stays_visible_and_collision_is_rejected(project_file, tmp_path):
    with ZipFile(project_file) as source:
        entries = {i.filename: source.read(i) for i in source.infolist()}
    entries["s/globals.xml"] = b"\xef\xbb\xbf" + entries["s/globals.xml"]
    bom_project = tmp_path / "bom.smc2"
    with ZipFile(bom_project, "w") as target:
        for name, content in entries.items():
            target.writestr(name, content)
    assert {v["name"] for v in NativeProject(bom_project).variables()} == {
        "Start",
        "Run",
        "Counter",
    }
    w = Workspace(tmp_path / "workspace")
    pid = w.open_project(bom_project)["project_id"]
    tx = w.begin(pid)["transaction_id"]
    with pytest.raises(ProjectError, match="already"):
        w.create_variable(tx, "globals", "Start", "BOOL", expected_revision=0)


def test_required_variable_type_cannot_be_removed(project_file, tmp_path):
    w = Workspace(tmp_path / "workspace")
    pid = w.open_project(project_file)["project_id"]
    tx = w.begin(pid)["transaction_id"]
    start = next(v for v in w.project(pid).variables() if v["name"] == "Start")
    with pytest.raises(ProjectError, match="required|empty"):
        w.update_variable(tx, start["id"], {"data_type": None}, expected_revision=0)
    assert len(w.project(pid, tx).variables()) == 3


def test_interrupted_snapshot_copy_can_be_retried(project_file, tmp_path, monkeypatch):
    import sysmac_mcp.workspace as module

    w = Workspace(tmp_path / "workspace")

    def interrupted(source, target):
        target.write(source.read(10))
        raise OSError("simulated interrupted copy")

    with monkeypatch.context() as context:
        context.setattr(module.shutil, "copyfileobj", interrupted)
        with pytest.raises(OSError, match="interrupted"):
            w.open_project(project_file)
    opened = w.open_project(project_file)
    assert opened["info"]["variable_count"] == 3


@pytest.mark.skipif(
    not __import__("pathlib").Path(SCHEMA).is_file(), reason="Omron XSD unavailable"
)
def test_empty_function_parameters_and_function_local_declarations_are_valid():
    pou = PouSpec(
        name="F",
        kind="Function",
        return_type="BOOL",
        source="F := TRUE;",
        variables=[VariableSpec(name="Temporary", data_type="BOOL")],
    )
    assert validate_iec_xml(build_iec_project("Demo", [pou], []), SCHEMA)["status"] == "passed"


@pytest.mark.parametrize(
    "kind,scope,options",
    [
        ("Program", "temp", {}),
        ("FunctionBlock", "temp", {}),
        ("Program", "input", {}),
        ("FunctionBlock", "external", {"retained": True}),
        ("FunctionBlock", "external", {"initial_value": "FALSE"}),
        ("FunctionBlock", "inout", {"retained": True}),
        ("FunctionBlock", "input", {"constant": True}),
    ],
)
def test_xml_options_that_cannot_be_represented_are_rejected(kind, scope, options):
    with pytest.raises(ValueError):
        PouSpec(
            name="POU",
            kind=kind,
            source="",
            variables=[VariableSpec(name="V", data_type="BOOL", scope=scope, **options)],
        )


@pytest.mark.skipif(
    not __import__("pathlib").Path(SCHEMA).is_file(), reason="Omron XSD unavailable"
)
def test_retention_of_parameters_is_preserved_with_original_order():
    pou = PouSpec(
        name="FB",
        kind="FunctionBlock",
        source="Run := Start;",
        variables=[
            VariableSpec(name="Start", data_type="BOOL", scope="input", retained=True),
            VariableSpec(name="Run", data_type="BOOL", scope="output"),
            VariableSpec(name="Other", data_type="BOOL", scope="input"),
        ],
    )
    data = build_iec_project("Demo", [pou], [])
    validate_iec_xml(data, SCHEMA)
    root = E.fromstring(data)
    start = root.xpath('//*[local-name()="Variable"][@name="Start"]')[0]
    other = root.xpath('//*[local-name()="Variable"][@name="Other"]')[0]
    assert start.getparent().get("retain") == "true"
    assert other.getparent().get("retain", "false") == "false"
    assert start.get("orderWithinParamSet") == "0"
    assert other.get("orderWithinParamSet") == "2"
