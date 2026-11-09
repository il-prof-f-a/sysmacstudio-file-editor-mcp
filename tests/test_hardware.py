import re
from pathlib import Path
from zipfile import ZipFile

import pytest
from lxml import etree as E

from sysmac_mcp.project import NativeProject
from sysmac_mcp.workspace import ProjectError, Workspace


@pytest.fixture
def hardware_project(project_file, tmp_path):
    profiles = tmp_path / "profiles"
    (profiles / "DdfFiles").mkdir(parents=True)
    (profiles / "DdfFiles/NX-TESTOUT.xml").write_text(
        """<SysmacInfo><Vendor><Id>1</Id></Vendor><Descriptions><Devices><Device>
<Type ProductCode="#x00000001" RevisionNo="#x01000000">NX-TESTOUT</Type>
<RxPdo><Index>#x1600</Index><Entry><Index>#x7002</Index><SubIndex>1</SubIndex><Name LcId="1033">Synthetic Output Word</Name><DataType>UINT</DataType><BitLen>2</BitLen></Entry></RxPdo>
<VendorSpecific><Omron><PdoDivisions Index="#x7002" SubIndex="#x01"><Division Type="BOOL"><Name LcId="1033">Bit 00</Name></Division><Division Type="BOOL"><Name LcId="1033">Bit 01</Name></Division></PdoDivisions></Omron></VendorSpecific>
</Device></Devices></Descriptions></SysmacInfo>""",
        encoding="utf-8",
    )
    with ZipFile(project_file) as source:
        members = {i.filename: source.read(i) for i in source.infolist()}
    oem = E.fromstring(members["s/s.oem"])
    cpu = oem.find('.//Entity[@id="cpu"]/ChildEntities')
    rack = E.SubElement(cpu, "Entity", type="NxCpuRack", subtype="NXBus", id="rack", name="NXBus")
    units = E.SubElement(rack, "ChildEntities")
    E.SubElement(
        units,
        "Entity",
        type="NexNXBusConfiguration",
        subtype="NXUnit",
        id="unit",
        name="Unit",
        DN="Module 1: NX-TESTOUT (N1)",
    )
    members["s/s.oem"] = E.tostring(oem)
    members["s/unit.dat"] = (
        b'<Module VenderID="1" ProductCode="1" Revision="16777216" IONodeID="unit-node" IsEnabled="1"><IOPort IOPortID="word" Index="28674" SubIndex="1"><SubIOPort IOPortID="bit-0"/><SubIOPort IOPortID="bit-1"/></IOPort></Module>'
    )
    output = tmp_path / "hardware.smc2"
    with ZipFile(output, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return output, profiles


def test_only_configured_ports_are_resolved_from_matching_profile(hardware_project):
    path, profiles = hardware_project
    inventory = NativeProject(path).hardware_inventory(profiles)
    module = next(m for m in inventory["modules"] if m["id"] == "unit")
    assert module["model"] == "NX-TESTOUT"
    ports = {p["id"]: p for p in inventory["ports"]}
    assert ports["bit-1"]["address"] == "IOBus://unit#1/Synthetic Output Word/Bit 01"
    assert ports["bit-1"]["data_type"] == "BOOL"
    assert ports["bit-1"]["direction"] == "output"
    assert ports["bit-1"]["configuration_resolved"] is True
    assert ports["bit-1"]["physical_verified"] is False
    assert ports["bit-1"]["bindings"] == []
    assert ports["bit-1"]["parent_port_id"] == "word"


def test_profile_identity_mismatch_does_not_authorize_new_address(hardware_project):
    path, profiles = hardware_project
    profile = profiles / "DdfFiles/NX-TESTOUT.xml"
    profile.write_text(
        profile.read_text().replace('ProductCode="#x00000001"', 'ProductCode="#x00000002"')
    )
    inventory = NativeProject(path).hardware_inventory(profiles)
    assert not any(
        p["configuration_resolved"] for p in inventory["ports"] if p["module_id"] == "unit"
    )
    assert inventory["diagnostics"]


def test_missing_profile_keeps_configured_port_ids_without_inventing_types(
    hardware_project, tmp_path
):
    path, _ = hardware_project
    inventory = NativeProject(path).hardware_inventory(tmp_path / "missing-profiles")
    bit = next(p for p in inventory["ports"] if p["id"] == "bit-1")
    assert bit["address"] is None and bit["data_type"] is None
    assert bit["configuration_resolved"] is False


def test_binding_new_free_configured_bit_is_staged(hardware_project, tmp_path):
    path, profiles = hardware_project
    workspace = Workspace(tmp_path / "workspace", profile_root=profiles)
    pid = workspace.open_project(path)["project_id"]
    target = next(v for v in workspace.project(pid).variables() if v["name"] == "Run")
    tx = workspace.begin(pid)["transaction_id"]
    address = "IOBus://unit#1/Synthetic Output Word/Bit 01"
    workspace.bind_io(tx, target["id"], address, expected_revision=0)
    assert not any(b["address"] == address for b in workspace.project(pid).io_bindings())
    workspace.commit(tx)
    assert any(b["address"] == address for b in workspace.project(pid).io_bindings())


def test_binding_wrong_type_to_configured_port_is_rejected(hardware_project, tmp_path):
    path, profiles = hardware_project
    workspace = Workspace(tmp_path / "workspace", profile_root=profiles)
    pid = workspace.open_project(path)["project_id"]
    target = next(v for v in workspace.project(pid).variables() if v["name"] == "Run")
    tx = workspace.begin(pid)["transaction_id"]
    with pytest.raises(ProjectError, match="type"):
        workspace.bind_io(
            tx, target["id"], "IOBus://unit#1/Synthetic Output Word", expected_revision=0
        )


def replace_member(path, name, content):
    with ZipFile(path) as archive:
        members = {i.filename: archive.read(i) for i in archive.infolist()}
    members[name] = content
    with ZipFile(path, "w") as archive:
        for filename, raw in members.items():
            archive.writestr(filename, raw)


def _write_ethercat_fixture(
    path, profiles, *, pdo_enabled=True, division_layout=(), configured_divisions=None
):
    import struct

    (profiles / "EsiFiles").mkdir(exist_ok=True)
    profile = E.fromstring(
        """<EtherCATInfo><Vendor><Id>#x83</Id></Vendor><Descriptions><Devices><Device>
<Type ProductCode="#x1" RevisionNo="#x1">DemoDrive</Type>
<RxPdo><Index>#x1600</Index><Entry><Index>#x7000</Index><SubIndex>1</SubIndex>
<Name LcId="1033">SyntheticCommand</Name><DataType>WORD</DataType><BitLen>16</BitLen>
</Entry></RxPdo></Device></Devices></Descriptions></EtherCATInfo>"""
    )
    if division_layout:
        device = profile.find("./Descriptions/Devices/Device")
        omron = E.SubElement(E.SubElement(device, "VendorSpecific"), "Omron")
        divisions = E.SubElement(omron, "PdoDivisions", Index="#x7000", SubIndex="#x01")
        for item in division_layout:
            if item[0] == "padding":
                E.SubElement(divisions, "Padding", Size=str(item[1]))
            else:
                _, data_type, name = item
                division = E.SubElement(divisions, "Division", Type=data_type)
                E.SubElement(division, "Name", LcId="1033").text = name
    (profiles / "EsiFiles/Demo.xml").write_bytes(E.tostring(profile))

    string_id = 1000
    class_id = 1

    def text(value):
        raw = value.encode()
        prefix = bytearray()
        length = len(raw)
        while length >= 128:
            prefix.append((length & 127) | 128)
            length >>= 7
        prefix.append(length)
        return bytes(prefix) + raw

    def encode_class(name, values):
        nonlocal string_id, class_id
        current = class_id
        class_id += 1
        types, extra, payload = bytearray(), bytearray(), bytearray()
        for value in values.values():
            if isinstance(value, bool):
                types.append(0)
                extra.append(1)
                payload.extend(bytes([value]))
            elif isinstance(value, int):
                types.append(0)
                extra.append(8)
                payload.extend(struct.pack("<i", value))
            elif isinstance(value, str):
                types.append(1)
                payload.extend(b"\x06" + struct.pack("<i", string_id) + text(value))
                string_id += 1
            elif isinstance(value, tuple):
                types.append(2)
                payload.extend(encode_class(*value))
        return (
            b"\x05"
            + struct.pack("<i", current)
            + text(name)
            + struct.pack("<i", len(values))
            + b"".join(text(key) for key in values)
            + types
            + extra
            + struct.pack("<i", 42)
            + payload
        )

    slave = {
        "Guid": "00000000-0000-0000-0000-000000000003",
        "Discription": "Demo",
        "Profile": "00000083:00000001:00000001:DemoDrive",
        "StationAddress": 3,
        "IsEnabled": True,
        "PdoMapping1600/IsEnabled": pdo_enabled,
        "PdoMapping1600/Pdo0": "7000:01:00000000-0000-0000-0000-000000000004",
    }
    for bit_offset, port_id in (configured_divisions or {}).items():
        slave[f"PdoMapping1600/Pdo7000:01/Division{bit_offset}/ID"] = port_id

    payload = (
        b"\x00"
        + struct.pack("<iiii", 1, -1, 1, 0)
        + b"\x0c"
        + struct.pack("<i", 42)
        + text("FixtureAssembly")
    )
    payload += (
        encode_class(
            "Omron.Cxap.Modules.NexEcatConfiguration.EcatConfigurationModel",
            {"Device0": ("Omron.Cxap.Modules.NexEcatConfiguration.EcatSlave", slave)},
        )
        + b"\x0b"
    )
    with ZipFile(path) as archive:
        oem = E.fromstring(archive.read("s/s.oem"))
    E.SubElement(
        oem.find('.//Entity[@id="cpu"]/ChildEntities'),
        "Entity",
        type="NexEcatConfiguration",
        subtype="EcatConfigurationSubstance",
        id="ecat",
        name="EtherCAT",
    )
    replace_member(path, "s/s.oem", E.tostring(oem))
    replace_member(path, "s/ecat.dat", payload)


def test_disabled_configured_module_cannot_be_bound(hardware_project, tmp_path):
    path, profiles = hardware_project
    with ZipFile(path) as z:
        raw = z.read("s/unit.dat")
    replace_member(path, "s/unit.dat", raw.replace(b'IsEnabled="1"', b'IsEnabled="0"'))
    w = Workspace(tmp_path / "workspace", profile_root=profiles)
    pid = w.open_project(path)["project_id"]
    run = next(v for v in w.project(pid).variables() if v["name"] == "Run")
    tx = w.begin(pid)["transaction_id"]
    with pytest.raises(ProjectError, match="enabled"):
        w.bind_io(tx, run["id"], "IOBus://unit#1/Synthetic Output Word/Bit 01", expected_revision=0)


def test_output_word_binding_blocks_overlapping_bit_binding(hardware_project, tmp_path):
    path, profiles = hardware_project
    with ZipFile(path) as z:
        raw = z.read("s/globals.xml")
    raw += b"++D=UINT\tN=WholeWord\tAT=IOBus://unit#1/Synthetic Output Word\tG=VAR_GLOBAL\r\n"
    replace_member(path, "s/globals.xml", raw)
    w = Workspace(tmp_path / "workspace", profile_root=profiles)
    pid = w.open_project(path)["project_id"]
    run = next(v for v in w.project(pid).variables() if v["name"] == "Run")
    tx = w.begin(pid)["transaction_id"]
    with pytest.raises(ProjectError, match="overlap"):
        w.bind_io(tx, run["id"], "IOBus://unit#1/Synthetic Output Word/Bit 01", expected_revision=0)


def test_mismatched_division_count_is_not_inferred(hardware_project):
    path, profiles = hardware_project
    with ZipFile(path) as z:
        raw = z.read("s/unit.dat")
    replace_member(path, "s/unit.dat", raw.replace(b'<SubIOPort IOPortID="bit-1"/>', b""))
    inventory = NativeProject(path).hardware_inventory(profiles)
    bit = next(p for p in inventory["ports"] if p["id"] == "bit-0")
    assert bit["configuration_resolved"] is False


def test_iobus_division_padding_is_reflected_in_bit_offsets(hardware_project):
    path, profiles = hardware_project
    profile = profiles / "DdfFiles/NX-TESTOUT.xml"
    xml = profile.read_text(encoding="utf-8")
    xml = xml.replace("<BitLen>2</BitLen>", "<BitLen>3</BitLen>")
    xml = xml.replace(
        '</Division><Division Type="BOOL">',
        '</Division><Padding Size="1"/><Division Type="BOOL">',
    )
    profile.write_text(xml, encoding="utf-8")

    inventory = NativeProject(path).hardware_inventory(profiles)

    bit = next(port for port in inventory["ports"] if port["id"] == "bit-1")
    assert bit["configuration_resolved"] is True
    assert bit["bit_offset"] == 2


@pytest.mark.parametrize("pdo_enabled", [True, False])
def test_ethercat_station_address_and_selected_pdo_are_read_from_nrbf(
    hardware_project, tmp_path, pdo_enabled
):
    from sysmac_mcp.nrbf import find_helper

    if find_helper() is None:
        pytest.skip("NRBF helper not built")
    path, profiles = hardware_project
    _write_ethercat_fixture(path, profiles, pdo_enabled=pdo_enabled)
    inventory = NativeProject(path).hardware_inventory(profiles)
    module = next(m for m in inventory["modules"] if m["bus"] == "EtherCAT")
    assert module["station_address"] == 3 and module["model"] == "DemoDrive"
    if not pdo_enabled:
        assert not any(p["module_id"] == module["id"] for p in inventory["ports"])
        with ZipFile(path) as z:
            raw = z.read("s/globals.xml")
        raw += b"++D=WORD\tN=Previous\tAT=ECAT://node#3/SyntheticCommand\tG=VAR_GLOBAL\r\n++D=WORD\tN=Next\tG=VAR_GLOBAL\r\n"
        replace_member(path, "s/globals.xml", raw)
        w = Workspace(tmp_path / "w", profile_root=profiles)
        pid = w.open_project(path)["project_id"]
        variables = w.project(pid).variables()
        previous = next(v for v in variables if v["name"] == "Previous")
        target = next(v for v in variables if v["name"] == "Next")
        tx = w.begin(pid)["transaction_id"]
        w.unbind_io(tx, previous["id"], expected_revision=0)
        with pytest.raises(ProjectError, match="enabled"):
            w.bind_io(tx, target["id"], "ECAT://node#3/SyntheticCommand", expected_revision=0)
        return
    port = next(p for p in inventory["ports"] if p["address"] == "ECAT://node#3/SyntheticCommand")
    assert port["data_type"] == "WORD" and port["direction"] == "output"
    assert port["configuration_resolved"] is True


@pytest.mark.parametrize(
    ("division_offset", "resolved"),
    [(3, True), (4, False)],
)
def test_ethercat_configured_divisions_use_profile_bit_offsets(
    hardware_project, division_offset, resolved
):
    path, profiles = hardware_project
    child_id = "00000000-0000-0000-0000-000000000005"
    _write_ethercat_fixture(
        path,
        profiles,
        division_layout=[("padding", 3), ("division", "BOOL", "Synthetic flag")],
        configured_divisions={division_offset: child_id},
    )

    inventory = NativeProject(path).hardware_inventory(profiles)

    child = next(port for port in inventory["ports"] if port["id"] == child_id)
    assert child["configuration_resolved"] is resolved
    assert child["parent_port_id"] == "00000000-0000-0000-0000-000000000004"
    if resolved:
        assert child["address"] == "ECAT://node#3/SyntheticCommand/Synthetic flag"
        assert child["data_type"] == "BOOL"
        assert child["direction"] == "output"
        assert child["bit_offset"] == 3
        assert child["bit_length"] == 1
    else:
        assert child["address"] is None
        assert child["data_type"] is None
        assert child["bit_offset"] == 4
        assert any(
            d["reason"] == "ethercat_division_profile_unresolved" for d in inventory["diagnostics"]
        )


def test_ethercat_non_bool_division_type_is_not_resolved(hardware_project):
    path, profiles = hardware_project
    child_id = "00000000-0000-0000-0000-000000000006"
    _write_ethercat_fixture(
        path,
        profiles,
        division_layout=[("division", "UINT", "Unsupported division")],
        configured_divisions={0: child_id},
    )

    inventory = NativeProject(path).hardware_inventory(profiles)

    child = next(port for port in inventory["ports"] if port["id"] == child_id)
    assert child["configuration_resolved"] is False
    assert child["address"] is None
    assert any(
        d["reason"] == "ethercat_division_profile_unresolved" for d in inventory["diagnostics"]
    )


async def test_hardware_protocol_filters_ports_and_reads_staging(
    hardware_project, tmp_path, monkeypatch
):
    from mcp import Client

    from sysmac_mcp.server import create_server

    path, profiles = hardware_project
    monkeypatch.setenv("SYSMAC_PROFILE_ROOT", str(profiles))
    async with Client(create_server(tmp_path / "mcp")) as client:
        opened = await client.call_tool("plc_open_project", {"path": str(path)})
        pid = opened.structured_content["project_id"]
        modules = await client.call_tool("plc_list_hardware", {"project_id": pid})
        assert not modules.is_error
        assert modules.structured_content["total"] == 1
        assert modules.structured_content["coverage"]["physical_verified"] is False
        ports = await client.call_tool(
            "plc_list_ports",
            {"project_id": pid, "available_only": True, "direction": "output", "limit": 1},
        )
        assert not ports.is_error
        assert ports.structured_content["total"] == 3
        assert ports.structured_content["next_offset"] == 1
        assert ports.structured_content["items"][0]["configuration_resolved"]
        variables = await client.call_tool(
            "plc_list_variables", {"project_id": pid, "query": "Run"}
        )
        variable = variables.structured_content["items"][0]
        tx = (
            await client.call_tool("plc_begin_transaction", {"project_id": pid})
        ).structured_content["transaction_id"]
        staged = await client.call_tool(
            "plc_bind_io",
            {
                "transaction_id": tx,
                "variable_id": variable["id"],
                "address": "IOBus://unit#1/Synthetic Output Word/Bit 01",
                "expected_revision": 0,
            },
        )
        assert not staged.is_error
        committed_ports = await client.call_tool(
            "plc_list_ports", {"project_id": pid, "available_only": True}
        )
        staged_ports = await client.call_tool(
            "plc_list_ports", {"project_id": pid, "available_only": True, "transaction_id": tx}
        )
        assert committed_ports.structured_content["total"] == 3
        assert staged_ports.structured_content["total"] == 1
        assert staged_ports.structured_content["items"][0]["id"] == "bit-0"
        invalid = await client.call_tool(
            "plc_list_ports", {"project_id": pid, "direction": "garbage"}
        )
        assert invalid.is_error


def test_division_width_exceeding_parent_is_not_inferred(hardware_project):
    path, profiles = hardware_project
    profile = profiles / "DdfFiles/NX-TESTOUT.xml"
    profile.write_text(profile.read_text().replace("<BitLen>2</BitLen>", "<BitLen>1</BitLen>"))
    inventory = NativeProject(path).hardware_inventory(profiles)
    assert all(not p["configuration_resolved"] for p in inventory["ports"] if p["parent_port_id"])


def test_output_bit_binding_blocks_parent_word_binding(hardware_project, tmp_path):
    path, profiles = hardware_project
    with ZipFile(path) as z:
        raw = z.read("s/globals.xml")
    raw += b"++D=UINT\tN=WholeWord\tG=VAR_GLOBAL\r\n"
    replace_member(path, "s/globals.xml", raw)
    w = Workspace(tmp_path / "w", profile_root=profiles)
    pid = w.open_project(path)["project_id"]
    variables = w.project(pid).variables()
    run = next(v for v in variables if v["name"] == "Run")
    word = next(v for v in variables if v["name"] == "WholeWord")
    tx = w.begin(pid)["transaction_id"]
    w.bind_io(tx, run["id"], "IOBus://unit#1/Synthetic Output Word/Bit 01", expected_revision=0)
    with pytest.raises(ProjectError, match="overlap"):
        w.bind_io(tx, word["id"], "IOBus://unit#1/Synthetic Output Word", expected_revision=0)


def test_observed_address_cannot_bypass_exact_profile_type(hardware_project, tmp_path):
    path, profiles = hardware_project
    with ZipFile(path) as z:
        raw = z.read("s/globals.xml")
    raw = raw.replace(
        b"IOBus://test-unit#0/Synthetic Input 00", b"IOBus://unit#1/Synthetic Output Word"
    )
    replace_member(path, "s/globals.xml", raw)
    w = Workspace(tmp_path / "w", profile_root=profiles)
    pid = w.open_project(path)["project_id"]
    variables = w.project(pid).variables()
    start = next(v for v in variables if v["name"] == "Start")
    run = next(v for v in variables if v["name"] == "Run")
    tx = w.begin(pid)["transaction_id"]
    w.unbind_io(tx, start["id"], expected_revision=0)
    with pytest.raises(ProjectError, match="type"):
        w.bind_io(tx, run["id"], "IOBus://unit#1/Synthetic Output Word", expected_revision=0)


def test_changing_type_after_binding_requires_unbind(hardware_project, tmp_path):
    path, profiles = hardware_project
    w = Workspace(tmp_path / "w", profile_root=profiles)
    pid = w.open_project(path)["project_id"]
    run = next(v for v in w.project(pid).variables() if v["name"] == "Run")
    tx = w.begin(pid)["transaction_id"]
    w.bind_io(tx, run["id"], "IOBus://unit#1/Synthetic Output Word/Bit 01", expected_revision=0)
    with pytest.raises(ProjectError, match="unbind"):
        w.update_variable(tx, run["id"], {"data_type": "INT"}, expected_revision=0)
    w.unbind_io(tx, run["id"], expected_revision=0)
    w.update_variable(tx, run["id"], {"data_type": "INT"}, expected_revision=0)
    w.commit(tx)
    changed = next(v for v in w.project(pid).variables() if v["name"] == "Run")
    assert changed["data_type"] == "INT" and not changed["address"]


@pytest.mark.parametrize("replacement", [b"", b'IOPortID="bit-0"'])
def test_missing_or_duplicate_parent_identity_does_not_authorize_alias(
    hardware_project, tmp_path, replacement
):
    path, profiles = hardware_project
    with ZipFile(path) as z:
        raw = z.read("s/unit.dat").replace(b'IOPortID="word"', replacement)
        variables = z.read("s/globals.xml")
    variables += b"++D=UINT\tN=WholeWord\tAT=IOBus://unit#1/Synthetic Output Word\tG=VAR_GLOBAL\r\n"
    replace_member(path, "s/unit.dat", raw)
    replace_member(path, "s/globals.xml", variables)
    w = Workspace(tmp_path / "w", profile_root=profiles)
    pid = w.open_project(path)["project_id"]
    run = next(v for v in w.project(pid).variables() if v["name"] == "Run")
    tx = w.begin(pid)["transaction_id"]
    with pytest.raises(ProjectError):
        w.bind_io(tx, run["id"], "IOBus://unit#1/Synthetic Output Word/Bit 01", expected_revision=0)


def test_builtin_ports_require_exact_cpu_profile_and_configured_ids(hardware_project):
    path, profiles = hardware_project
    folder = profiles / "NX1DeviceParameters/BuiltinIO"
    folder.mkdir(parents=True)
    (folder / "BuiltInIO_NX-DEMO-CPU-001_Ver2_07.xml").write_text(
        """<NXDeviceParameter><Profile><Parameters><ParameterGroup TypeID="9"><Parameter><ParameterName>Synthetic Output 00</ParameterName><AccessDataType>BOOL</AccessDataType><BitSize>1</BitSize><Address><Path>BuiltInIO://cpu/#0/Synthetic_Output_00</Path></Address></Parameter><Parameter><ParameterName>Synthetic Output 01</ParameterName><AccessDataType>BOOL</AccessDataType><BitSize>1</BitSize><Address><Path>BuiltInIO://cpu/#0/Synthetic_Output_01</Path></Address></Parameter></ParameterGroup></Parameters></Profile></NXDeviceParameter>""",
        encoding="utf-8",
    )
    with ZipFile(path) as z:
        oem = E.fromstring(z.read("s/s.oem"))
    E.SubElement(
        oem.find('.//Entity[@id="cpu"]/ChildEntities'),
        "Entity",
        type="NexCpuRackBuiltinIOMaster",
        id="builtin",
        name="Built-in",
    )
    replace_member(path, "s/s.oem", E.tostring(oem))
    replace_member(
        path,
        "s/builtin.xml",
        b'<CpuRackNetworkData><NodeID>cpu-node</NodeID><OutputPorts TypeID="9"><Port ID="built-0" Name="Synthetic Output 00"/></OutputPorts></CpuRackNetworkData>',
    )
    inventory = NativeProject(path).hardware_inventory(profiles)
    cpu_ports = [p for p in inventory["ports"] if p["module_id"] == "cpu"]
    assert len(cpu_ports) == 1
    assert cpu_ports[0]["id"] == "built-0"
    assert cpu_ports[0]["address"] == "BuiltInIO://cpu/#0/Synthetic_Output_00"
    assert cpu_ports[0]["data_type"] == "BOOL" and cpu_ports[0]["available"]


def test_known_port_with_ambiguous_profile_address_cannot_be_reassigned(hardware_project, tmp_path):
    path, profiles = hardware_project
    with ZipFile(path) as z:
        config = z.read("s/unit.dat").replace(
            b"</Module>", b'<IOPort IOPortID="another-word" Index="28674" SubIndex="1"/></Module>'
        )
        variables = z.read("s/globals.xml")
    variables += b"++D=UINT\tN=Previous\tAT=IOBus://unit#1/Synthetic Output Word\tG=VAR_GLOBAL\r\n++D=UINT\tN=Next\tG=VAR_GLOBAL\r\n"
    replace_member(path, "s/unit.dat", config)
    replace_member(path, "s/globals.xml", variables)
    w = Workspace(tmp_path / "w", profile_root=profiles)
    pid = w.open_project(path)["project_id"]
    variables = w.project(pid).variables()
    previous = next(v for v in variables if v["name"] == "Previous")
    target = next(v for v in variables if v["name"] == "Next")
    tx = w.begin(pid)["transaction_id"]
    w.unbind_io(tx, previous["id"], expected_revision=0)
    with pytest.raises(ProjectError):
        w.bind_io(tx, target["id"], "IOBus://unit#1/Synthetic Output Word", expected_revision=0)


def test_hardware_fixture_addresses_are_explicitly_synthetic():
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in Path(__file__).parent.glob("*.py")
    )
    addresses = re.findall(r"(?i)(?:IOBus|ECAT|BuiltInIO)://[^\"'<>\r\n]+", source)

    assert addresses
    assert all(
        "synthetic" in address.casefold() or "test" in address.casefold() for address in addresses
    ), "hardware test fixtures must mark address paths as synthetic"
