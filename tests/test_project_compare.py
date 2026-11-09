import json
import shutil
from zipfile import ZipFile

import pytest
from lxml import etree as E

from sysmac_mcp.project_compare import compare_projects


def changed_copy(source, tmp_path, updates):
    target = tmp_path / "after.smc2"
    with ZipFile(source) as archive:
        members = {i.filename: archive.read(i) for i in archive.infolist()}
    members.update(updates)
    with ZipFile(target, "w") as archive:
        for name, raw in members.items():
            archive.writestr(name, raw)
    return target


def model_items(report, category):
    return [i for i in report["items"] if i["domain"] == "model" and i["category"] == category]


def test_identical_projects_compare_observed_models_without_equivalence_claim(project_file):
    result = compare_projects(project_file, project_file)
    assert result["observed_models_equal"] is True
    assert result["total"] == result["model_difference_count"] == 0
    assert result["semantic_equivalence"] == "not_verified"
    assert result["round_trip_verified"] is False
    assert result["coverage"]["complete"] is False


def test_st_change_reports_body_digest_and_preserves_inputs(project_file, tmp_path):
    before = project_file.read_bytes()
    after = changed_copy(
        project_file,
        tmp_path,
        {"s/body.xml": b"<StructuredTextModel><Text>Run := FALSE;</Text></StructuredTextModel>"},
    )
    result = compare_projects(project_file, after)
    assert result["observed_models_equal"] is False
    assert len(model_items(result, "section")) == 1
    assert result["archive_difference_count"] == 1
    assert "Run := FALSE" not in json.dumps(result)
    assert project_file.read_bytes() == before


def test_opaque_change_is_visible_even_when_observed_models_match(project_file, tmp_path):
    after = changed_copy(project_file, tmp_path, {"s/unrecognised.bin": b"changed opaque"})
    result = compare_projects(project_file, after)
    assert result["observed_models_equal"] is True
    assert result["archive_difference_count"] == 1
    item = next(i for i in result["items"] if i["domain"] == "archive")
    assert item["member"] == "s/unrecognised.bin" and item["after_recognized"] is False
    assert result["semantic_equivalence"] == "not_verified"


def test_variable_mapping_and_unknown_attributes_are_compared(project_file, tmp_path):
    with ZipFile(project_file) as archive:
        raw = archive.read("s/globals.xml")
    after = changed_copy(
        project_file,
        tmp_path,
        {
            "s/globals.xml": raw.replace(b"Input Bit 00", b"Input Bit 01").replace(
                b"Unknown=preserve", b"Unknown=changed"
            )
        },
    )
    result = compare_projects(project_file, after)
    assert len(model_items(result, "variable")) == 1


def test_ladder_json_formatting_does_not_change_observed_model(project_file, tmp_path):
    with ZipFile(project_file) as archive:
        rung = json.loads(archive.read("s/ld.xml").decode("utf-8-sig").strip())
    after = changed_copy(
        project_file, tmp_path, {"s/ld.xml": (json.dumps(rung, sort_keys=True) + "\n").encode()}
    )
    result = compare_projects(project_file, after)
    assert result["observed_models_equal"] is True
    assert result["archive_difference_count"] == 1


def test_task_execution_settings_are_in_model_comparison(project_file, tmp_path):
    with ZipFile(project_file) as archive:
        root = E.fromstring(archive.read("s/task.xml"))
    E.SubElement(root, "GeneralSettingsData", ExecutionPriority="8")
    after = changed_copy(project_file, tmp_path, {"s/task.xml": E.tostring(root)})
    result = compare_projects(project_file, after)
    assert len(model_items(result, "task")) == 1


def test_protected_source_change_is_never_hidden(project_file, tmp_path):
    with ZipFile(project_file) as archive:
        members = {i.filename: archive.read(i) for i in archive.infolist()}
    members["s/body.xml"] = b"<EncryptedFile>first</EncryptedFile>"
    first = tmp_path / "protected.smc2"
    with ZipFile(first, "w") as archive:
        for name, raw in members.items():
            archive.writestr(name, raw)
    after = changed_copy(first, tmp_path, {"s/body.xml": b"<EncryptedFile>second</EncryptedFile>"})
    result = compare_projects(first, after)
    assert len(model_items(result, "section")) == 1
    assert result["coverage"]["before"]["unacquired_section_count"] == 1


def test_duplicate_logical_identity_is_inconclusive_not_silently_collapsed(project_file, tmp_path):
    with ZipFile(project_file) as archive:
        raw = archive.read("s/locals.xml")
    after = changed_copy(
        project_file, tmp_path, {"s/locals.xml": raw + b"++D=INT\tN=Counter\tG=VAR\r\n"}
    )
    result = compare_projects(after, after)
    assert result["observed_models_equal"] is None
    assert result["coverage"]["after"]["ambiguous_key_count"] == 1


def test_ids_and_oem_dates_can_change_without_matching_objects_by_old_ids(project_file, tmp_path):
    with ZipFile(project_file) as archive:
        members = {i.filename: archive.read(i) for i in archive.infolist()}
    root = E.fromstring(members["s/s.oem"])
    moved = dict(members)
    for entity in root.findall(".//Entity"):
        old_id = entity.get("id")
        entity.set("id", "new-" + old_id)
        entity.set("dateLastModified", "different timestamp")
        old_member = "s/" + old_id + ".xml"
        if old_member in moved:
            moved["s/new-" + old_id + ".xml"] = moved.pop(old_member)
    moved["s/s.oem"] = E.tostring(root, pretty_print=True)
    after = tmp_path / "ids.smc2"
    with ZipFile(after, "w") as archive:
        for name, raw in moved.items():
            archive.writestr(name, raw)
    result = compare_projects(project_file, after)
    assert result["observed_models_equal"] is True
    assert result["archive_difference_count"] > 0


def test_reports_are_paginated_with_full_counts(project_file, tmp_path):
    after = changed_copy(
        project_file,
        tmp_path,
        {"s/body.xml": b"<StructuredTextModel><Text>Run := FALSE;</Text></StructuredTextModel>"},
    )
    first = compare_projects(project_file, after, limit=1)
    second = compare_projects(project_file, after, limit=1, offset=first["next_offset"])
    assert len(first["items"]) == len(second["items"]) == 1
    assert first["total"] == second["total"] == 2
    assert second["next_offset"] is None


@pytest.mark.parametrize("offset,limit", [(-1, 1), (0, 0), (0, 201)])
def test_invalid_pagination_is_rejected(project_file, offset, limit):
    with pytest.raises(ValueError):
        compare_projects(project_file, project_file, offset=offset, limit=limit)


def test_repacked_zip_bytes_are_distinguished_from_member_content(project_file, tmp_path):
    after = tmp_path / "copy.smc2"
    shutil.copyfile(project_file, after)
    with ZipFile(after, "a") as archive:
        archive.comment = b"different comment"
    result = compare_projects(project_file, after)
    assert result["identical_bytes"] is False
    assert result["observed_models_equal"] is True
    assert result["archive_difference_count"] == result["total"] == 0


def test_long_st_source_is_hashed_instead_of_returned(project_file, tmp_path):
    source = "Run := '" + "private-content" * 5000 + "';"
    root = E.Element("StructuredTextModel")
    E.SubElement(root, "Text").text = source
    after = changed_copy(project_file, tmp_path, {"s/body.xml": E.tostring(root)})
    report = json.dumps(compare_projects(project_file, after))
    assert "private-content" not in report and len(report) < 30_000


def test_change_during_read_is_rejected(project_file, monkeypatch):
    import sysmac_mcp.project_compare as module

    original = module.NativeProject

    class ChangingInput(original):
        def __init__(self, path):
            super().__init__(path)
            with ZipFile(path, "a") as archive:
                archive.comment += b"changed concurrently"

    monkeypatch.setattr(module, "NativeProject", ChangingInput)
    with pytest.raises(ValueError, match="Input changed"):
        module.compare_projects(project_file, project_file)


def test_task_order_and_startup_changes_are_not_ignored(project_file, tmp_path):
    after = changed_copy(
        project_file,
        tmp_path,
        {
            "s/task.xml": b'<data><Programs><AssociatedProgramData ProgramName="Ladder" SequenceNumber="1" StartupSetting="FALSE"/><AssociatedProgramData ProgramName="Main" SequenceNumber="2" StartupSetting="TRUE"/></Programs></data>'
        },
    )
    result = compare_projects(project_file, after)
    assert len(model_items(result, "task")) == 1


def test_removed_declarations_and_added_pou_are_reported(project_file, tmp_path):
    from sysmac_mcp.workspace import Workspace

    w = Workspace(tmp_path / "workspace")
    pid = w.open_project(project_file)["project_id"]
    tx = w.begin(pid)["transaction_id"]
    w.create_pou(
        tx,
        "p",
        {
            "name": "Worker",
            "source": "Count := 1;",
            "variables": [{"name": "Count", "data_type": "DINT"}],
        },
        expected_revision=0,
    )
    w.commit(tx)
    target = tmp_path / "new-pou.smc2"
    w.export(pid, target)
    result = compare_projects(project_file, target)
    assert any(i["change"] == "added" for i in model_items(result, "section"))
    removed = changed_copy(
        project_file,
        tmp_path,
        {"s/locals.xml": b"[SLWD version=1.0]\r\n_EN=Variables\r\n+GN=VAR\r\n"},
    )
    assert (
        model_items(compare_projects(project_file, removed), "variable")[0]["change"] == "removed"
    )


async def test_mcp_project_comparison_is_read_only_and_paginated(project_file, tmp_path):
    from mcp import Client

    from sysmac_mcp.server import create_server

    after = changed_copy(project_file, tmp_path, {"s/unrecognised.bin": b"changed"})
    async with Client(create_server(tmp_path / "mcp")) as client:
        listing = await client.list_tools()
        tool = next(t for t in listing.tools if t.name == "plc_compare_projects")
        assert tool.annotations.read_only_hint is True
        result = await client.call_tool(
            "plc_compare_projects", {"before": str(project_file), "after": str(after), "limit": 1}
        )
        assert not result.is_error
        assert result.structured_content["archive_difference_count"] == 1
        invalid = await client.call_tool(
            "plc_compare_projects", {"before": str(project_file), "after": str(after), "limit": 201}
        )
        assert invalid.is_error


def test_cli_project_comparison(project_file):
    import subprocess
    import sys

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "sysmac_mcp",
            "compare-projects",
            str(project_file),
            str(project_file),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["observed_models_equal"] is True


def test_mixed_task_setting_text_after_child_is_compared(project_file, tmp_path):
    first_dir = tmp_path / "first"
    first_dir.mkdir()
    first = changed_copy(
        project_file,
        first_dir,
        {
            "s/task.xml": b"<data><Programs/><VendorSetting>prefix<Unit/>old-value</VendorSetting></data>"
        },
    )
    after = changed_copy(
        first,
        tmp_path,
        {
            "s/task.xml": b"<data><Programs/><VendorSetting>prefix<Unit/>new-value</VendorSetting></data>"
        },
    )
    assert len(model_items(compare_projects(first, after), "task")) == 1


def test_unresolved_task_tracking_is_not_discarded(project_file, tmp_path):
    first_dir = tmp_path / "first"
    first_dir.mkdir()
    template = '<data><Programs><AssociatedProgramData ProgramName="Missing" IniFileTrackingId="{}" SequenceNumber="1"/></Programs></data>'
    first = changed_copy(
        project_file, first_dir, {"s/task.xml": template.format("9" * 32).encode()}
    )
    after = changed_copy(first, tmp_path, {"s/task.xml": template.format("8" * 32).encode()})
    assert len(model_items(compare_projects(first, after), "task")) == 1


def test_long_archive_member_name_is_bounded_with_identity_fingerprint(project_file, tmp_path):
    import hashlib

    member = "s/" + "a" * 64_000
    after = changed_copy(project_file, tmp_path, {member: b"new opaque"})
    result = compare_projects(project_file, after, limit=1)
    assert len(json.dumps(result)) < 10_000
    item = result["items"][0]
    assert len(item["member"]) < 400
    assert item["member_name_sha256"] == hashlib.sha256(member.encode()).hexdigest()
    assert item["member_name_length"] == len(member)


@pytest.mark.parametrize(
    "first_xml, second_xml",
    [
        (b'<data><Programs Vendor="old"/></data>', b'<data><Programs Vendor="new"/></data>'),
        (b"<data><Programs/>old-tail</data>", b"<data><Programs/>new-tail</data>"),
        (
            b'<data><Programs><AssociatedProgramData ProgramName="Main" SequenceNumber="1"><Vendor>old</Vendor></AssociatedProgramData></Programs></data>',
            b'<data><Programs><AssociatedProgramData ProgramName="Main" SequenceNumber="1"><Vendor>new</Vendor></AssociatedProgramData></Programs></data>',
        ),
    ],
)
def test_unmodeled_task_container_data_is_kept_in_model_fingerprint(
    project_file, tmp_path, first_xml, second_xml
):
    first_dir = tmp_path / "first"
    first_dir.mkdir()
    first = changed_copy(project_file, first_dir, {"s/task.xml": first_xml})
    after = changed_copy(first, tmp_path, {"s/task.xml": second_xml})
    assert len(model_items(compare_projects(first, after), "task")) == 1


def test_resolved_tracking_can_change_while_logical_target_is_preserved(project_file, tmp_path):
    with ZipFile(project_file) as archive:
        oem = E.fromstring(archive.read("s/s.oem"))
    main = oem.find('.//Entity[@id="p"]')
    main.set("trackingId", "1" * 32)
    template = '<data><Programs><AssociatedProgramData ProgramName="Main" IniFileTrackingId="{}" SequenceNumber="1"/></Programs></data>'
    first_dir = tmp_path / "first"
    first_dir.mkdir()
    first = changed_copy(
        project_file,
        first_dir,
        {"s/s.oem": E.tostring(oem), "s/task.xml": template.format("1" * 32).encode()},
    )
    main.set("trackingId", "2" * 32)
    after = changed_copy(
        first,
        tmp_path,
        {"s/s.oem": E.tostring(oem), "s/task.xml": template.format("2" * 32).encode()},
    )
    assert compare_projects(first, after)["observed_models_equal"] is True


@pytest.mark.parametrize(
    "template",
    [
        '<data><Programs/><Vendor><Programs><AssociatedProgramData Value="{}"/></Programs></Vendor></data>',
        '<data><Programs><AssociatedProgramData Value="{}"/></Programs><Programs/></data>',
    ],
)
def test_only_acquired_task_rows_have_attributes_normalized(project_file, tmp_path, template):
    first_dir = tmp_path / "first"
    first_dir.mkdir()
    first = changed_copy(project_file, first_dir, {"s/task.xml": template.format("old").encode()})
    after = changed_copy(first, tmp_path, {"s/task.xml": template.format("new").encode()})
    assert len(model_items(compare_projects(first, after), "task")) == 1
