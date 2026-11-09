from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from sysmac_mcp.workspace import ProjectError, Workspace


def setup(project_file, tmp_path):
    workspace = Workspace(tmp_path / "workspace")
    opened = workspace.open_project(project_file)
    pid = opened["project_id"]
    tx = workspace.begin(pid)["transaction_id"]
    return workspace, pid, tx


def replace_members(project_file, changes, tmp_path):
    with ZipFile(project_file) as source:
        members = {item.filename: source.read(item.filename) for item in source.infolist()}
        comment = source.comment
    members.update(changes)
    candidate = tmp_path / "modified.smc2"
    with ZipFile(candidate, "w", ZIP_DEFLATED) as output:
        output.comment = comment
        for name, content in members.items():
            output.writestr(name, content)
    return candidate


def test_staging_is_isolated_and_commit_survives_restart(project_file, tmp_path):
    w, pid, tx = setup(project_file, tmp_path)
    original = project_file.read_bytes()
    w.update_st(tx, "body", "Run := NOT Start;", expected_revision=0)
    assert w.project(pid).read_pou("p")["sections"][0]["source"].startswith("Run := Start;")
    assert w.project(pid, tx).read_pou("p")["sections"][0]["source"] == "Run := NOT Start;"
    assert w.preview(tx)["changed_entries"] == ["s/body.xml"]
    restarted = Workspace(tmp_path / "workspace")
    result = restarted.commit(tx)
    assert result["revision"] == 1
    assert result["sysmac_build"] == "not_executed"
    assert restarted.project(pid).read_pou("p")["sections"][0]["source"] == "Run := NOT Start;"
    assert project_file.read_bytes() == original


def test_export_preserves_opaque_members_and_cannot_overwrite(project_file, tmp_path):
    w, pid, tx = setup(project_file, tmp_path)
    w.update_st(tx, "body", "Run := FALSE;", expected_revision=0)
    w.commit(tx)
    target = tmp_path / "candidate.smc2"
    output = w.export(pid, target)
    assert output["status"] == "candidate_unverified"
    with ZipFile(project_file) as original, ZipFile(target) as candidate:
        assert candidate.comment == original.comment
        assert candidate.namelist() == original.namelist()
        for name in original.namelist():
            if name != "s/body.xml":
                assert candidate.read(name) == original.read(name)
    with pytest.raises(ProjectError, match="exist|original"):
        w.export(pid, project_file)
    with pytest.raises(ProjectError, match="exist"):
        w.export(pid, target)


def test_conflicting_transactions_cannot_clobber_new_revision(project_file, tmp_path):
    w, pid, a = setup(project_file, tmp_path)
    b = w.begin(pid)["transaction_id"]
    w.update_st(a, "body", "Run := FALSE;", expected_revision=0)
    w.update_st(b, "body", "Run := TRUE;", expected_revision=0)
    w.commit(a)
    with pytest.raises(ProjectError, match="revision"):
        w.commit(b)
    assert w.project(pid).read_pou("p")["sections"][0]["source"] == "Run := FALSE;"


def test_rollback_discards_staging(project_file, tmp_path):
    w, pid, tx = setup(project_file, tmp_path)
    w.update_st(tx, "body", "Run := FALSE;", expected_revision=0)
    assert w.rollback(tx)["status"] == "rolled_back"
    assert w.project(pid).read_pou("p")["sections"][0]["source"].startswith("Run := Start;")
    with pytest.raises(ProjectError, match="active"):
        w.commit(tx)


def test_variable_update_preserves_unknown_fields_and_rejects_injection(project_file, tmp_path):
    w, pid, tx = setup(project_file, tmp_path)
    start = next(v for v in w.project(pid).variables() if v["name"] == "Start")
    w.update_variable(tx, start["id"], {"initial_value": "TRUE"}, expected_revision=0)
    updated = next(v for v in w.project(pid, tx).variables() if v["name"] == "Start")
    assert updated["attributes"]["Unknown"] == "preserve"
    assert updated["initial_value"] == "TRUE"
    with pytest.raises(ProjectError, match="delimiter"):
        w.update_variable(
            tx, start["id"], {"initial_value": "TRUE\n++D=BOOL\tN=Injected"}, expected_revision=0
        )


def test_create_variable_case_insensitive_collisions(project_file, tmp_path):
    w, pid, tx = setup(project_file, tmp_path)
    w.create_variable(tx, "globals", "Enabled", "BOOL", initial_value="FALSE", expected_revision=0)
    assert any(v["name"] == "Enabled" for v in w.project(pid, tx).variables())
    with pytest.raises(ProjectError, match="already"):
        w.create_variable(tx, "globals", "eNABLED", "BOOL", expected_revision=0)
    w.commit(tx)
    assert any(v["name"] == "Enabled" for v in w.project(pid).variables())


def test_delete_unused_local_variable_is_staged_and_committable(project_file, tmp_path):
    original = project_file.read_bytes()
    declarations = (
        b"\xef\xbb\xbf[SLWD version=1.0]\r\n+GN=VAR\r\n"
        b"++D=INT\tN=Counter\tG=VAR\r\n++D=BOOL\tN=Keep\tG=VAR\n"
    )
    candidate = replace_members(project_file, {"s/locals.xml": declarations}, tmp_path)
    candidate_original = candidate.read_bytes()
    w, pid, tx = setup(candidate, tmp_path / "workspace")
    counter = next(v for v in w.project(pid).variables() if v["name"] == "Counter")

    result = w.delete_variable(tx, counter["id"], expected_revision=0)

    assert result["status"] == "staged"
    assert result["change"]["operation"] == "delete_variable"
    assert result["change"]["external_consumers"] == "not_acquired"
    assert any(v["name"] == "Counter" for v in w.project(pid).variables())
    assert not any(v["name"] == "Counter" for v in w.project(pid, tx).variables())
    assert w.project(pid, tx).archive.read("s/locals.xml") == (
        b"\xef\xbb\xbf[SLWD version=1.0]\r\n+GN=VAR\r\n"
        b"++D=BOOL\tN=Keep\tG=VAR\n"
    )
    assert w.preview(tx)["changed_entries"] == ["s/locals.xml"]
    w.commit(tx)
    assert not any(v["name"] == "Counter" for v in w.project(pid).variables())
    assert project_file.read_bytes() == original
    assert candidate.read_bytes() == candidate_original
    assert w.project(pid).archive.read("s/unrecognised.bin") == b"\x00\xffopaque data"


def test_delete_variable_refuses_observed_code_references(project_file, tmp_path):
    w, pid, tx = setup(project_file, tmp_path)
    run = next(v for v in w.project(pid).variables() if v["name"] == "Run")

    with pytest.raises(ProjectError, match="references"):
        w.delete_variable(tx, run["id"], expected_revision=0)

    assert w.preview(tx)["changed_entries"] == []


def test_delete_variable_requires_unbinding_and_refuses_read_only(project_file, tmp_path):
    w, pid, tx = setup(project_file, tmp_path)
    start = next(v for v in w.project(pid).variables() if v["name"] == "Start")
    with pytest.raises(ProjectError, match="unbind_io"):
        w.delete_variable(tx, start["id"], expected_revision=0)
    assert w.preview(tx)["changed_entries"] == []

    read_only_project = replace_members(
        project_file,
        {"s/locals.xml": b"[SLWD version=1.0]\r\n+GN=VAR\r\n++D=INT\tN=Counter\tG=VAR\tRO=1\r\n"},
        tmp_path,
    )
    protected, protected_id, protected_tx = setup(read_only_project, tmp_path / "readonly")
    counter = next(v for v in protected.project(protected_id).variables() if v["name"] == "Counter")
    with pytest.raises(ProjectError, match="read-only"):
        protected.delete_variable(protected_tx, counter["id"], expected_revision=0)
    assert protected.preview(protected_tx)["changed_entries"] == []


def test_delete_variable_refuses_unavailable_source_and_declaration_references(
    project_file, tmp_path
):
    unavailable = replace_members(project_file, {"s/body.xml": b"<EncryptedFile/>"}, tmp_path)
    w, pid, tx = setup(unavailable, tmp_path / "unavailable")
    counter = next(v for v in w.project(pid).variables() if v["name"] == "Counter")
    with pytest.raises(ProjectError, match="source|section"):
        w.delete_variable(tx, counter["id"], expected_revision=0)
    assert w.preview(tx)["changed_entries"] == []

    dependent = replace_members(
        project_file,
        {
            "s/locals.xml": (
                b"[SLWD version=1.0]\r\n+GN=VAR\r\n"
                b"++D=INT\tN=Counter\tG=VAR\r\n++D=INT\tN=Dependent\tIV=Counter\tG=VAR\r\n"
            )
        },
        tmp_path,
    )
    w, pid, tx = setup(dependent, tmp_path / "dependent")
    counter = next(v for v in w.project(pid).variables() if v["name"] == "Counter")
    with pytest.raises(ProjectError, match="declaration|expression"):
        w.delete_variable(tx, counter["id"], expected_revision=0)
    assert w.preview(tx)["changed_entries"] == []


def test_update_ladder_and_keep_input_data(project_file, tmp_path):
    w, pid, tx = setup(project_file, tmp_path)
    rungs = w.project(pid).read_pou("ldp")["sections"][0]["rungs"]
    rungs[0]["CLs"].insert(1, {"__type": "AND", "Var": "Enabled", "X": 1})
    w.update_ld(tx, "ld", rungs, expected_revision=0)
    w.commit(tx)
    assert w.project(pid).read_pou("ldp")["sections"][0]["rungs"][0]["CLs"][1]["Var"] == "Enabled"


def test_external_source_change_detected_before_commit(project_file, tmp_path):
    w, pid, tx = setup(project_file, tmp_path)
    w.update_st(tx, "body", "Run := FALSE;", expected_revision=0)
    project_file.write_bytes(b"externally changed")
    with pytest.raises(ProjectError, match="original"):
        w.commit(tx)


def test_binding_unknown_port_or_taken_port_fails(project_file, tmp_path):
    w, pid, tx = setup(project_file, tmp_path)
    variables = w.project(pid).variables()
    run = next(v for v in variables if v["name"] == "Run")
    with pytest.raises(ProjectError, match="observed"):
        w.bind_io(tx, run["id"], "IOBus://test-unknown", expected_revision=0)
    with pytest.raises(ProjectError, match="assigned"):
        w.bind_io(tx, run["id"], "IOBus://test-unit#0/Synthetic Input 00", expected_revision=0)


def test_variable_identity_survives_insertion_in_an_earlier_group(project_file, tmp_path):
    with ZipFile(project_file) as source:
        members = {i.filename: source.read(i) for i in source.infolist()}
    members["s/globals.xml"] += b"+GN=VAR_EXTERNAL\r\n++D=BOOL\tN=External\tG=VAR_EXTERNAL\r\n"
    alternate = tmp_path / "with-groups.smc2"
    with ZipFile(alternate, "w") as target:
        for name, content in members.items():
            target.writestr(name, content)
    w, pid, tx = setup(alternate, tmp_path)
    external = next(v for v in w.project(pid).variables() if v["name"] == "External")
    w.create_variable(tx, "globals", "Added", "BOOL", expected_revision=0)
    w.update_variable(
        tx, external["id"], {"comment": "Still the same variable"}, expected_revision=0
    )
    updated = next(v for v in w.project(pid, tx).variables() if v["name"] == "External")
    assert updated["id"] == external["id"]
    assert updated["attributes"]["Com"] == "Still the same variable"


def test_io_reassignment_is_atomic_and_preserves_unknown_attributes(project_file, tmp_path):
    w, pid, tx = setup(project_file, tmp_path)
    variables = w.project(pid).variables()
    start = next(v for v in variables if v["name"] == "Start")
    run = next(v for v in variables if v["name"] == "Run")
    w.unbind_io(tx, start["id"], expected_revision=0)
    w.bind_io(tx, run["id"], start["address"], expected_revision=0)
    assert w.project(pid).io_bindings()[0]["variable_name"] == "Start"
    assert w.project(pid, tx).io_bindings()[0]["variable_name"] == "Run"
    w.commit(tx)
    updated = next(v for v in w.project(pid).variables() if v["name"] == "Start")
    assert updated["address"] is None
    assert updated["attributes"]["Unknown"] == "preserve"
