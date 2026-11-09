"""Resolve configured hardware ports from native configuration plus exact local profiles."""

from __future__ import annotations

import hashlib
import re

from lxml import etree as ET

from .nrbf import NrbfError, decode_nrbf
from .profiles import ProfileCatalog, integer, profile_xml
from .project import parse_xml

WIDTHS = {
    "BOOL": 1,
    "BYTE": 8,
    "SINT": 8,
    "USINT": 8,
    "WORD": 16,
    "INT": 16,
    "UINT": 16,
    "DWORD": 32,
    "DINT": 32,
    "UDINT": 32,
    "REAL": 32,
    "LWORD": 64,
    "LINT": 64,
    "ULINT": 64,
    "LREAL": 64,
}
ECAT_DIVISION_ID = re.compile(
    r"PdoMapping([0-9a-fA-F]{4})/Pdo([0-9a-fA-F]{4}):([0-9a-fA-F]{2})/Division(\d{1,6})/ID",
    re.IGNORECASE,
)
GUID_ID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


def inventory(project, profile_root=None):
    catalog = ProfileCatalog(profile_root)
    modules, ports, diagnostics = [], [], []
    variables = project.variables("global")
    by_address = {}
    for variable in variables:
        if variable["address"]:
            by_address.setdefault(variable["address"], []).append(variable)

    def module_record(entity, bus, **extra):
        item = {
            "id": entity.get("id"),
            "name": entity.get("name"),
            "bus": bus,
            "model": None,
            "enabled": True,
            "physical_verified": False,
            "address_prefix": None,
            **extra,
        }
        modules.append(item)
        return item

    def port(
        module,
        port_id,
        *,
        address=None,
        data_type=None,
        direction=None,
        member=None,
        profile=None,
        parent=None,
        offset=None,
        width=None,
        resolved=False,
        identity_valid=True,
    ):
        identity_valid = bool(identity_valid and isinstance(port_id, str) and port_id.strip())
        resolved = resolved and identity_valid
        attached = by_address.get(address, []) if address else []
        types = {v["data_type"].strip().casefold() for v in attached}
        if data_type is not None and any(t != data_type.strip().casefold() for t in types):
            resolved = False
            diagnostics.append(
                {
                    "module_id": module["id"],
                    "port_id": port_id,
                    "reason": "declared_binding_type_differs_from_profile",
                }
            )
        item = {
            "id": port_id,
            "module_id": module["id"],
            "address": address,
            "data_type": data_type,
            "direction": direction,
            "enabled": module["enabled"],
            "configuration_resolved": resolved,
            "identity_valid": identity_valid,
            "physical_verified": False,
            "parent_port_id": parent,
            "bit_offset": offset,
            "bit_length": width,
            "origin": "project_configuration_and_profile" if resolved else "project_configuration",
            "source_member": member,
            "profile_path": str(profile) if profile else None,
            "bindings": [
                {"variable_id": v["id"], "name": v["name"], "data_type": v["data_type"]}
                for v in attached
            ],
        }
        ports.append(item)
        return item

    nx_units = [
        e
        for e in project.entities.values()
        if e.get("type") == "NexNXBusConfiguration" and e.get("subtype") == "NXUnit"
    ]
    nx_numbers = {}
    for position, entity in enumerate(nx_units, 1):
        member = project.prefix + entity.get("id") + ".dat"
        if member not in project.archive.members:
            diagnostics.append(
                {"module_id": entity.get("id"), "reason": "module_configuration_missing"}
            )
            continue
        try:
            root = parse_xml(project.archive.read(member))
        except (ValueError, ET.XMLSyntaxError):
            diagnostics.append(
                {"module_id": entity.get("id"), "reason": "module_configuration_unsupported"}
            )
            continue
        if root.tag != "Module":
            continue
        display = entity.get("DN", "")
        label = re.search(r"(\d+)\s*:\s*(NX-[A-Za-z0-9-]+)(?:\s|\(|$)", display)
        number = int(label[1]) if label and int(label[1]) == position else None
        model_hint = label[2] if label else None
        vendor, product, revision = (
            integer(root.get(a)) for a in ["VenderID", "ProductCode", "Revision"]
        )
        profile = catalog.find_device(vendor, product, revision, model_hint)
        module = module_record(
            entity,
            "IOBus",
            model=profile.model if profile else model_hint,
            unit_number=number,
            node_id=root.get("IONodeID"),
            enabled=root.get("IsEnabled") == "1",
            vendor_id=vendor,
            product_code=product,
            revision=revision,
            address_evidence="oem_unit_label_and_order" if number is not None else "unresolved",
            address_prefix=f"IOBus://unit#{number}/" if number is not None else None,
        )
        if number is not None:
            nx_numbers[number] = module
        if profile is None or number is None:
            diagnostics.append(
                {"module_id": module["id"], "reason": "exact_profile_or_unit_address_unavailable"}
            )
        for configured in root.findall("IOPort"):
            port_id = configured.get("IOPortID")
            index, subindex = integer(configured.get("Index")), integer(configured.get("SubIndex"))
            entry = profile.entry(index, subindex) if profile else None
            address = (
                f"IOBus://unit#{number}/{entry['name']}" if entry and number is not None else None
            )
            parent = port(
                module,
                port_id,
                address=address,
                data_type=entry["data_type"] if entry else None,
                direction=entry["direction"] if entry else None,
                member=member,
                profile=profile.path if profile else None,
                width=entry["bit_length"] if entry else None,
                offset=0,
                resolved=bool(entry and address),
            )
            children = configured.findall("SubIOPort")
            divisions = profile.divisions(index, subindex) if profile else []
            matched = (
                bool(children)
                and len(children) == len(divisions)
                and all(
                    d["name"] and d["data_type"] in WIDTHS and d["bit_offset"] is not None
                    for d in divisions
                )
                and entry is not None
                and entry["bit_length"] is not None
                and all(
                    d["bit_offset"] + WIDTHS[d["data_type"]] <= entry["bit_length"]
                    for d in divisions
                )
            )
            for i, child in enumerate(children):
                division = divisions[i] if matched else None
                child_address = address + "/" + division["name"] if address and division else None
                width = WIDTHS.get(division["data_type"]) if division else None
                port(
                    module,
                    child.get("IOPortID"),
                    address=child_address,
                    data_type=division["data_type"] if division else None,
                    direction=entry["direction"] if entry else None,
                    parent=parent["id"],
                    offset=division["bit_offset"] if division else None,
                    width=width,
                    member=member,
                    profile=profile.path if profile else None,
                    resolved=bool(child_address),
                    identity_valid=parent["identity_valid"],
                )
            if children and not matched:
                diagnostics.append(
                    {
                        "module_id": module["id"],
                        "port_id": port_id,
                        "reason": "division_count_or_type_unresolved",
                    }
                )

    # Built-in ports carry native IDs; datatype/address come from the exact CPU/version parameter profile.
    controllers = {}
    for entity in project.entities.values():
        if entity.get("type") != "Device" or not project.member(entity.get("id")):
            continue
        try:
            data = parse_xml(project.archive.read(project.member(entity.get("id"))))
        except (ValueError, ET.XMLSyntaxError):
            continue
        if data.findtext("ModelName"):
            controllers[entity.get("id")] = data
    for entity in project.entities.values():
        if entity.get("type") != "NexCpuRackBuiltinIOMaster":
            continue
        member = project.member(entity.get("id"))
        if not member:
            continue
        root = parse_xml(project.archive.read(member))
        ancestor = entity.getparent()
        while ancestor is not None and ancestor.get("id") not in controllers:
            ancestor = ancestor.getparent()
        if ancestor is None:
            continue
        data = controllers[ancestor.get("id")]
        model = data.findtext("ModelName") + "-" + data.findtext("ModelDetail", "")
        major, minor = data.findtext("MajorVersion", ""), data.findtext("MinorVersion", "")
        module = module_record(
            ancestor,
            "BuiltInIO",
            model=model,
            node_id=root.findtext("NodeID"),
            address_prefix="BuiltInIO://cpu/#0/",
        )
        parameters = {}
        if (
            catalog.root
            and re.fullmatch(r"[A-Za-z0-9-]+", model)
            and major.isdigit()
            and minor.isdigit()
        ):
            profile_path = (
                catalog.root
                / "NX1DeviceParameters/BuiltinIO"
                / f"BuiltInIO_{model}_Ver{major}_{minor}.xml"
            )
            if profile_path.is_file():
                parameter_root = profile_xml(profile_path)
                for group in parameter_root.findall(".//ParameterGroup"):
                    for parameter in group.findall("Parameter"):
                        parameters[(group.get("TypeID"), parameter.findtext("ParameterName"))] = (
                            parameter
                        )
        for tag, direction in [("InputPorts", "input"), ("OutputPorts", "output")]:
            for group in root.findall(tag):
                for configured in group.findall("Port"):
                    parameter = parameters.get((group.get("TypeID"), configured.get("Name")))
                    address = parameter.findtext("Address/Path") if parameter is not None else None
                    dtype = parameter.findtext("AccessDataType") if parameter is not None else None
                    port(
                        module,
                        configured.get("ID"),
                        address=address,
                        data_type=dtype,
                        direction=direction,
                        member=member,
                        profile=profile_path if parameter is not None else None,
                        offset=0,
                        width=integer(parameter.findtext("BitSize"))
                        if parameter is not None
                        else None,
                        resolved=bool(address and dtype),
                    )

    # Controller rack status addresses are explicit; their types may only be known from declarations.
    for entity in project.entities.values():
        if entity.get("type") != "NxCpuRack" or entity.get("subtype") != "NXBus":
            continue
        member = project.member(entity.get("id"))
        if not member:
            continue
        root = parse_xml(project.archive.read(member))
        for direct in root.findall(".//DirectIOPort"):
            number = integer(direct.get("UnitNumber"))
            module = nx_numbers.get(number)
            if module:
                address = f"IOBus://unit#{number}/{direct.get('RelativeAtPath')}"
                attached = by_address.get(address, [])
                types = {v["data_type"] for v in attached}
                port(
                    module,
                    direct.get("IOPortID"),
                    address=address,
                    data_type=next(iter(types)) if len(types) == 1 else None,
                    direction="input",
                    member=member,
                    resolved=False,
                )

    # EtherCAT configuration is read as a passive NRBF record graph, never deserialized.
    for entity in project.entities.values():
        if (
            entity.get("type") != "NexEcatConfiguration"
            or entity.get("subtype") != "EcatConfigurationSubstance"
        ):
            continue
        member = project.prefix + entity.get("id") + ".dat"
        if member not in project.archive.members:
            continue
        try:
            graph = decode_nrbf(project.archive.read(member))
        except NrbfError as exc:
            diagnostics.append(
                {
                    "module_id": entity.get("id"),
                    "reason": "ethercat_configuration_unavailable",
                    "detail": str(exc),
                }
            )
            continue
        records = graph["records"]
        root = records[str(graph["root"])]
        if root.get("type") != "Omron.Cxap.Modules.NexEcatConfiguration.EcatConfigurationModel":
            diagnostics.append(
                {"module_id": entity.get("id"), "reason": "ethercat_configuration_unsupported"}
            )
            continue
        for key, ref in root.get("members", {}).items():
            if (
                not re.fullmatch(r"Device\d+", key)
                or not isinstance(ref, dict)
                or "$ref" not in ref
            ):
                continue
            record = records.get(str(ref["$ref"]), {})
            if record.get("type") != "Omron.Cxap.Modules.NexEcatConfiguration.EcatSlave":
                continue
            fields = record.get("members", {})
            identity = fields.get("Profile", "")
            match = (
                re.fullmatch(
                    r"([0-9a-fA-F]{8}):([0-9a-fA-F]{8}):([0-9a-fA-F]{8}):(.{1,128})", identity
                )
                if isinstance(identity, str)
                else None
            )
            vendor, product, revision = (
                tuple(int(match[i], 16) for i in (1, 2, 3)) if match else (None, None, None)
            )
            profile = catalog.find_device(vendor, product, revision, match[4] if match else None)
            station = fields.get("StationAddress")
            station = station if type(station) is int and 1 <= station <= 65535 else None
            module = module_record(
                entity,
                "EtherCAT",
                id=entity.get("id") + ":" + str(ref["$ref"]),
                name=fields.get("Discription"),
                model=match[4] if match else None,
                node_id=fields.get("Guid"),
                station_address=station,
                enabled=fields.get("IsEnabled") is True,
                vendor_id=vendor,
                product_code=product,
                revision=revision,
                address_evidence="configured_station_address",
                address_prefix=f"ECAT://node#{station}/" if station is not None else None,
            )
            if profile is None or station is None:
                diagnostics.append(
                    {
                        "module_id": module["id"],
                        "reason": "exact_profile_or_station_address_unavailable",
                    }
                )
            for key, value in fields.items():
                mapping = re.fullmatch(r"(PdoMapping([0-9a-fA-F]{4}))/Pdo\d+", key)
                if not mapping or fields.get(mapping[1] + "/IsEnabled") is not True:
                    continue
                object_ref = (
                    re.fullmatch(r"([0-9a-fA-F]{4}):([0-9a-fA-F]{2}):([0-9a-fA-F-]{36})", value)
                    if isinstance(value, str)
                    else None
                )
                if not object_ref:
                    diagnostics.append(
                        {
                            "module_id": module["id"],
                            "reason": "unsupported_pdo_entry",
                            "mapping": key,
                        }
                    )
                    continue
                index, subindex = int(object_ref[1], 16), int(object_ref[2], 16)
                entry = profile.entry(index, subindex, int(mapping[2], 16)) if profile else None
                port_id = object_ref[3]
                configured_width = fields.get(port_id + "/BitLength")
                consistent = (
                    configured_width is None
                    or entry is not None
                    and configured_width == entry["bit_length"]
                )
                address = (
                    f"ECAT://node#{station}/{entry['name']}"
                    if entry and station is not None
                    else None
                )
                port(
                    module,
                    port_id,
                    address=address,
                    data_type=entry["data_type"] if entry else None,
                    direction=entry["direction"] if entry else None,
                    member=member,
                    profile=profile.path if profile else None,
                    offset=0,
                    width=entry["bit_length"] if entry else None,
                    resolved=bool(address and consistent),
                )
                configured_divisions = []
                for division_key, division_id in fields.items():
                    division_match = ECAT_DIVISION_ID.fullmatch(division_key)
                    if (
                        division_match
                        and division_match[1].casefold() == mapping[2].casefold()
                        and int(division_match[2], 16) == index
                        and int(division_match[3], 16) == subindex
                    ):
                        configured_divisions.append((int(division_match[4]), division_id))
                if configured_divisions:
                    division_by_offset = {}
                    ambiguous_offsets = set()
                    for division in profile.divisions(index, subindex) if profile else []:
                        offset = division.get("bit_offset")
                        if offset is None:
                            continue
                        if offset in division_by_offset:
                            ambiguous_offsets.add(offset)
                        else:
                            division_by_offset[offset] = division
                    for offset in ambiguous_offsets:
                        division_by_offset.pop(offset, None)
                    for configured_offset, division_id in configured_divisions:
                        division = division_by_offset.get(configured_offset)
                        division_type = division.get("data_type") if division else None
                        division_width = WIDTHS.get(division_type) if division else None
                        division_name = division.get("name") if division else None
                        child_address = (
                            address + "/" + division_name if address and division_name else None
                        )
                        configured_division_width = (
                            fields.get(division_id + "/BitLength")
                            if isinstance(division_id, str)
                            else None
                        )
                        division_id_valid = bool(
                            isinstance(division_id, str) and GUID_ID.fullmatch(division_id)
                        )
                        width_consistent = (
                            configured_division_width is None
                            or type(configured_division_width) is int
                            and configured_division_width == division_width
                        )
                        division_consistent = bool(
                            child_address
                            and entry
                            and entry["bit_length"] is not None
                            and consistent
                            and division is not None
                            and division_type in WIDTHS
                            and division_width is not None
                            and division["bit_offset"] + division_width <= entry["bit_length"]
                            and division_id_valid
                            and width_consistent
                        )
                        port(
                            module,
                            division_id,
                            address=child_address,
                            data_type=division_type,
                            direction=entry["direction"] if entry else None,
                            member=member,
                            profile=profile.path if profile else None,
                            parent=port_id,
                            offset=(division["bit_offset"] if division else configured_offset),
                            width=division_width,
                            resolved=division_consistent,
                            identity_valid=division_id_valid,
                        )
                        if not division_consistent:
                            diagnostics.append(
                                {
                                    "module_id": module["id"],
                                    "port_id": division_id,
                                    "reason": "ethercat_division_profile_unresolved",
                                }
                            )
                        if division is not None and not width_consistent:
                            diagnostics.append(
                                {
                                    "module_id": module["id"],
                                    "port_id": division_id,
                                    "reason": "ethercat_division_width_differs_from_profile",
                                }
                            )
                if not consistent:
                    diagnostics.append(
                        {
                            "module_id": module["id"],
                            "port_id": port_id,
                            "reason": "configured_width_differs_from_profile",
                        }
                    )

    # IDs must identify ports and their containers uniquely within a module.
    identities = {}
    for item in ports:
        identities.setdefault((item["module_id"], item["id"]), []).append(item)
    ambiguous = {key for key, values in identities.items() if len(values) > 1}
    for item in ports:
        if (item["module_id"], item["id"]) in ambiguous or (
            item["module_id"],
            item["parent_port_id"],
        ) in ambiguous:
            item["identity_valid"] = False
            item["configuration_resolved"] = False
        if not item["identity_valid"]:
            diagnostics.append(
                {
                    "module_id": item["module_id"],
                    "port_id": item["id"],
                    "reason": "missing_or_ambiguous_port_identity",
                }
            )

    # A repeated canonical address cannot identify a unique hardware port.
    addresses = {}
    for item in ports:
        if item["address"]:
            addresses.setdefault(item["address"], []).append(item)
    for address, items in addresses.items():
        if len(items) > 1:
            for item in items:
                item["configuration_resolved"] = False
            diagnostics.append({"address": address, "reason": "ambiguous_configured_address"})

    resolved_addresses = {p["address"] for p in ports if p["address"]}
    for address, bindings in by_address.items():
        if address in resolved_addresses:
            continue
        types = {v["data_type"] for v in bindings}
        observed = {
            "id": "observed:" + hashlib.sha256(address.encode("utf-8")).hexdigest()[:24],
            "module_id": None,
            "address": address,
            "data_type": next(iter(types)) if len(types) == 1 else None,
            "direction": None,
            "enabled": None,
            "configuration_resolved": False,
            "identity_valid": False,
            "physical_verified": False,
            "parent_port_id": None,
            "bit_offset": None,
            "bit_length": None,
            "origin": "native_variable_AT",
            "source_member": None,
            "profile_path": None,
            "bindings": [
                {"variable_id": v["id"], "name": v["name"], "data_type": v["data_type"]}
                for v in bindings
            ],
        }
        ports.append(observed)
    for item in ports:
        item["overlapping_bindings"] = overlapping_bindings(ports, item)
        item["available"] = bool(
            item["enabled"]
            and item["configuration_resolved"]
            and not item["bindings"]
            and not item["overlapping_bindings"]
        )
    return {
        "modules": modules,
        "ports": ports,
        "diagnostics": diagnostics,
        "profile_root": str(catalog.root) if catalog.root else None,
        "coverage": {
            "physical_verified": False,
            "complete": False,
            "resolved_ports": sum(p["configuration_resolved"] for p in ports),
            "observed_only_ports": sum(p["origin"] == "native_variable_AT" for p in ports),
        },
    }


def overlapping_bindings(ports, target):
    """Find output aliases only when configured parent/child bit intervals are known."""
    if (
        target["direction"] != "output"
        or target["bit_offset"] is None
        or target["bit_length"] is None
    ):
        return []
    root = target["parent_port_id"] or target["id"]
    start, end = target["bit_offset"], target["bit_offset"] + target["bit_length"]
    result = {}
    for item in ports:
        if (
            item is target
            or item["module_id"] != target["module_id"]
            or (item["parent_port_id"] or item["id"]) != root
            or item["bit_offset"] is None
            or item["bit_length"] is None
        ):
            continue
        if item["bit_offset"] < end and start < item["bit_offset"] + item["bit_length"]:
            for binding in item["bindings"]:
                result[binding["variable_id"]] = {**binding, "address": item["address"]}
    return list(result.values())
