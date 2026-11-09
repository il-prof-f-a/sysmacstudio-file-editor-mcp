"""Native SMC2 reader for observed OEM, ST, SLWD and LD JSON-lines representations."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from lxml import etree as ET

from .archive import ArchiveError, ProjectArchive


def parse_xml(raw: bytes):
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ArchiveError("DTD/entity declarations are not supported in project XML.")
    return ET.fromstring(raw, ET.XMLParser(resolve_entities=False, no_network=True))


def xml_local_name(node):
    return ET.QName(node).localname if isinstance(node.tag, str) else None


def serialize_xml(root, original: bytes = b"") -> bytes:
    bom = b"\xef\xbb\xbf" if original.startswith(b"\xef\xbb\xbf") else b""
    declaration = original.lstrip(b"\xef\xbb\xbf \r\n").startswith(b"<?xml")
    return bom + ET.tostring(root, encoding="utf-8", xml_declaration=declaration)


def is_encrypted_payload(payload: bytes | str) -> bool:
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    preview = raw.removeprefix(b"\xef\xbb\xbf").lstrip()
    if preview.startswith(b"<?xml"):
        preview = preview.partition(b"?>")[2].lstrip()
    return re.match(rb"<EncryptedFile(?:\s|>)", preview) is not None


def st_tokens(source: str):
    """Identifier locations excluding IEC strings and nested comments; not a type checker."""
    i, length = 0, len(source)
    while i < length:
        if source.startswith("//", i):
            end = source.find("\n", i)
            i = length if end < 0 else end + 1
        elif source.startswith("(*", i):
            depth, i = 1, i + 2
            while i < length and depth:
                if source.startswith("(*", i):
                    depth, i = depth + 1, i + 2
                elif source.startswith("*)", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
        elif source[i] in "'\"":
            quote, i = source[i], i + 1
            while i < length:
                if source[i] == "$":
                    i += 2
                elif source[i] == quote:
                    if i + 1 < length and source[i + 1] == quote:
                        i += 2
                    else:
                        i += 1
                        break
                else:
                    i += 1
        elif source[i].isalpha() or source[i] == "_":
            start = i
            i += 1
            while i < length and (source[i].isalnum() or source[i] == "_"):
                i += 1
            yield source[start:i], start, i
        else:
            i += 1


def walk_ld(value, path=""):
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}/{key}"
            if key == "Var" and isinstance(child, str):
                yield child_path, child
            elif key == "TXT" and isinstance(child, str):
                yield child_path, child
            else:
                yield from walk_ld(child, child_path)
    elif isinstance(value, list):
        for n, child in enumerate(value):
            yield from walk_ld(child, f"{path}/{n}")


class NativeProject:
    def __init__(self, path: str | Path, overrides: dict[str, bytes] | None = None):
        self.archive = ProjectArchive(path)
        self.archive.apply_overrides(overrides or {})
        oems = [n for n in self.archive.names if n.endswith(".oem")]
        if len(oems) != 1:
            raise ArchiveError("Expected one OEM entity tree; unsupported project variant.")
        self.oem_name = oems[0]
        self.oem = parse_xml(self.archive.read(self.oem_name))
        if self.oem.tag != "sysmac":
            raise ArchiveError("Unsupported OEM root.")
        self.prefix = self.oem_name.rsplit("/", 1)[0] + "/" if "/" in self.oem_name else ""
        self.entities = {}
        for entity in self.oem.findall(".//Entity"):
            key = entity.get("id")
            if not key or key in self.entities:
                raise ArchiveError("Missing or duplicate entity identity.")
            self.entities[key] = entity
        self.recognized = {self.oem_name}

    def member(self, entity_id: str):
        name = self.prefix + entity_id + ".xml"
        return name if name in self.archive.members else None

    def owner(self, entity):
        parent = entity.getparent()
        while parent is not None:
            if parent.tag == "Entity" and parent.get("type") in {
                "Program",
                "Function",
                "FunctionBlock",
            }:
                return parent.get("id")
            parent = parent.getparent()
        return None

    def resolve(self, entity_id: str, allowed: set[str]):
        if entity_id in self.entities and self.entities[entity_id].get("type") in allowed:
            return self.entities[entity_id]
        matches = [
            e
            for e in self.entities.values()
            if e.get("name") == entity_id and e.get("type") in allowed
        ]
        if len(matches) != 1:
            raise ValueError("Object not found or name ambiguous; use its entity ID.")
        return matches[0]

    def pous(self):
        return [
            {
                "id": e.get("id"),
                "name": e.get("name"),
                "kind": e.get("type"),
                "subtype": e.get("subtype"),
                "section_count": len(e.findall('./ChildEntities/Entity[@type="PouBody"]')),
            }
            for e in self.entities.values()
            if e.get("type") in {"Program", "Function", "FunctionBlock"}
        ]

    def read_section(self, entity):
        name = self.member(entity.get("id"))
        result = {
            "id": entity.get("id"),
            "name": entity.get("name"),
            "member": name,
            "language": "unknown",
            "completeness": "unavailable",
        }
        if not name:
            return result
        raw = self.archive.read(name)
        if is_encrypted_payload(raw):
            result.update(completeness="protected", editable=False, reason="encrypted_source")
            return result
        if entity.get("subtype") == "StructuredText":
            try:
                root = parse_xml(raw)
            except ET.XMLSyntaxError:
                result.update(editable=False, reason="invalid_model_xml")
                return result
            if is_encrypted_payload(root.text or ""):
                result.update(completeness="protected", editable=False, reason="encrypted_source")
                return result
            texts = root.xpath('//*[local-name()="Text"]')
            if ET.QName(root).localname != "StructuredTextModel" or len(texts) != 1:
                return result
            result.update(language="ST", source=texts[0].text or "", completeness="observed")
        elif entity.get("subtype") == "Ladder":
            try:
                rungs = [
                    json.loads(line)
                    for line in raw.decode("utf-8-sig").splitlines()
                    if line.strip()
                ]
            except (ValueError, UnicodeDecodeError):
                return result
            if not all(isinstance(r, dict) and isinstance(r.get("CLs"), list) for r in rungs):
                return result
            result.update(language="LD", rungs=rungs, completeness="observed")
        else:
            return result
        self.recognized.add(name)
        return result

    def read_pou(self, entity_id: str):
        entity = self.resolve(entity_id, {"Program", "Function", "FunctionBlock"})
        result = next(p for p in self.pous() if p["id"] == entity.get("id"))
        result["sections"] = [
            self.read_section(e) for e in entity.findall('./ChildEntities/Entity[@type="PouBody"]')
        ]
        result["variables"] = self.variables(scope=entity.get("id"))
        return result

    def variables(self, scope: str | None = None):
        result = []
        for table in self.entities.values():
            if table.get("type") != "Variables":
                continue
            owner = self.owner(table) or "global"
            if scope is not None and scope != owner:
                continue
            member = self.member(table.get("id"))
            if not member:
                continue
            raw = self.archive.read(member)
            if not raw.removeprefix(b"\xef\xbb\xbf").startswith(b"[SLWD version=1.0]"):
                continue
            try:
                lines = raw.decode("utf-8-sig").splitlines()
            except UnicodeDecodeError:
                continue
            self.recognized.add(member)
            for line_no, line in enumerate(lines):
                if not line.startswith("++"):
                    continue
                fields = dict(part.split("=", 1) for part in line[2:].split("\t") if "=" in part)
                if "N" not in fields or "D" not in fields:
                    continue
                result.append(
                    {
                        "id": table.get("id")
                        + ":"
                        + hashlib.sha256(
                            (fields.get("G", "") + "\0" + fields["N"].casefold()).encode("utf-8")
                        ).hexdigest()[:24],
                        "name": fields["N"],
                        "data_type": fields["D"],
                        "scope": owner,
                        "group": fields.get("G"),
                        "initial_value": fields.get("IV"),
                        "address": fields.get("AT"),
                        "retained": fields.get("R") == "1",
                        "attributes": fields,
                        "table_id": table.get("id"),
                        "member": member,
                        "line_index": line_no,
                        "completeness": "observed",
                    }
                )
        return result

    def data_types(self, controller_id: str | None = None, parent_id: str | None = None):
        """Read native DataType XML tables while retaining their raw attributes and paths."""
        tables = []
        records = []
        devices = {
            entity.get("id")
            for entity in self.entities.values()
            if entity.get("type") == "Device"
        }
        if controller_id is not None and controller_id not in devices:
            raise ValueError("Controller not found; use a Device entity ID.")

        for entity in self.entities.values():
            if entity.get("type") != "DataType":
                continue

            current = entity.getparent()
            owner_controller = None
            library = None
            while current is not None:
                if current.tag == "Entity":
                    if current.get("type") == "Device":
                        owner_controller = current.get("id")
                    elif current.get("type") == "Library" and library is None:
                        library = current
                current = current.getparent()
            if controller_id is not None and owner_controller != controller_id:
                continue

            member = self.member(entity.get("id"))
            table = {
                "id": entity.get("id"),
                "name": entity.get("name"),
                "member": member,
                "controller_id": owner_controller,
                "library_id": library.get("id") if library is not None else None,
                "library_name": library.get("name") if library is not None else None,
                "completeness": "unavailable",
            }
            tables.append(table)
            if not member:
                table["reason"] = "missing_member"
                continue

            raw = self.archive.read(member)
            if is_encrypted_payload(raw):
                table.update(completeness="protected", reason="encrypted_source")
                continue
            try:
                root = parse_xml(raw)
            except (ArchiveError, ET.LxmlError):
                table["reason"] = "invalid_xml"
                continue

            if xml_local_name(root) != "data":
                table["reason"] = "unsupported_root"
                continue
            table["xml_root_attributes"] = dict(root.attrib)

            complete = True
            reason = None
            if any((node.text or "").strip() or (node.tail or "").strip() for node in root.iter()):
                complete = False
                reason = "unsupported_text"
            root_children = list(root)
            if any(xml_local_name(child) != "DataType" for child in root_children):
                complete = False
                reason = reason or "unsupported_element"

            context = {
                "table_id": entity.get("id"),
                "table_name": entity.get("name"),
                "controller_id": owner_controller,
                "member": member,
                "library_id": table["library_id"],
                "library_name": table["library_name"],
            }

            definitions = []
            if not root_children:
                definitions = []
            elif (
                len(root_children) == 1
                and xml_local_name(root_children[0]) == "DataType"
                and not root_children[0].get("BaseType", "").strip()
            ):
                table["group_attributes"] = dict(root_children[0].attrib)
                definitions = list(root_children[0])
                if any(xml_local_name(child) != "DataType" for child in definitions):
                    complete = False
                    reason = reason or "unsupported_element"
            elif all(
                xml_local_name(child) == "DataType"
                and child.get("BaseType", "").strip()
                for child in root_children
            ):
                definitions = root_children
                table["group_attributes"] = None
            else:
                complete = False
                reason = reason or "unsupported_container"

            def walk(node, path, parent, depth, context):
                nonlocal complete, reason
                name = node.get("Name")
                child_nodes = list(node)
                data_type_children = [
                    child for child in child_nodes if xml_local_name(child) == "DataType"
                ]
                if (node.text or "").strip() or any((child.tail or "").strip() for child in child_nodes):
                    complete = False
                    reason = reason or "unsupported_text"
                if name is None:
                    complete = False
                    reason = reason or "missing_name"
                if len(data_type_children) != len(child_nodes):
                    complete = False
                    reason = reason or "unsupported_element"

                item_id = f"{context['table_id']}:{path}"
                item = {
                    "id": item_id,
                    "name": name,
                    "base_type": node.get("BaseType"),
                    "array_type": node.get("ArrayType"),
                    "length": node.get("Length"),
                    "initial_value": node.get("InitialValue"),
                    "enum_value": node.get("EnumValue"),
                    "comment": node.get("Comment"),
                    "offset_channel": node.get("OffsetChannel"),
                    "offset_bit": node.get("OffsetBit"),
                    "offset_type": node.get("OffsetType"),
                    "controller_defined": node.get("IsControllerDefinedType"),
                    "order": node.get("Order"),
                    "attributes": dict(node.attrib),
                    "parent_id": parent,
                    "depth": depth,
                    "child_count": len(data_type_children),
                    **context,
                    "identity_basis": "entity_id_plus_xml_element_path",
                    "completeness": "observed",
                }
                records.append(item)
                for index, child in enumerate(child_nodes):
                    if xml_local_name(child) == "DataType":
                        walk(child, f"{path}.{index}", item_id, depth + 1, context)

            for index, definition in enumerate(definitions):
                if xml_local_name(definition) != "DataType":
                    complete = False
                    reason = reason or "unsupported_element"
                    continue
                walk(definition, str(index), None, 0, context)

            table["completeness"] = "observed" if complete else "partial"
            if reason:
                table["reason"] = reason
            if complete:
                self.recognized.add(member)

        by_id = {item["id"]: item for item in records}
        if parent_id is not None:
            if parent_id not in by_id:
                raise ValueError("Data type not found; use a returned data type ID.")
            items = [item for item in records if item["parent_id"] == parent_id]
        else:
            items = [item for item in records if item["depth"] == 0]

        statuses = {table["completeness"] for table in tables}
        completeness = "observed" if statuses <= {"observed"} else "partial"
        return {
            "items": items,
            "total": len(items),
            "definition_count": sum(item["depth"] == 0 for item in records),
            "nested_member_count": sum(item["depth"] > 0 for item in records),
            "table_count": len(tables),
            "completeness": completeness,
            "tables": tables,
        }

    def hardware_inventory(self, profile_root=None):
        from .hardware import inventory

        return inventory(self, profile_root)

    def io_bindings(self):
        return [
            {
                "variable_id": v["id"],
                "variable_name": v["name"],
                "data_type": v["data_type"],
                "address": v["address"],
                "hardware_verified": False,
                "origin": "native_variable_AT",
            }
            for v in self.variables()
            if v["address"]
        ]

    def tasks(self):
        from .task_model import read_tasks

        return read_tasks(self)

    def info(self):
        controllers = []
        for entity in self.entities.values():
            if entity.get("type") != "Device":
                continue
            member = self.member(entity.get("id"))
            if not member:
                continue
            try:
                root = parse_xml(self.archive.read(member))
            except ET.XMLSyntaxError:
                continue
            model = root.findtext("ModelName")
            if not model:
                continue
            controllers.append(
                {
                    "id": entity.get("id"),
                    "name": entity.get("name"),
                    "model": model
                    + ("-" + root.findtext("ModelDetail") if root.findtext("ModelDetail") else ""),
                    "unit_version": root.findtext("MajorVersion", "?")
                    + "."
                    + root.findtext("MinorVersion", "?"),
                }
            )
            self.recognized.add(member)
        pous = self.pous()
        sections = [s for p in pous for s in self.read_pou(p["id"])["sections"]]
        variables, tasks = self.variables(), self.tasks()
        data_types = self.data_types()
        return {
            "name": next(iter(self.entities.values())).get("name"),
            "controllers": controllers,
            "pou_count": len(pous),
            "variable_count": len(variables),
            "data_type_count": data_types["definition_count"],
            "data_type_member_count": data_types["nested_member_count"],
            "data_type_table_count": data_types["table_count"],
            "data_type_completeness": data_types["completeness"],
            "task_count": len(tasks),
            "st_sections": sum(s["language"] == "ST" for s in sections),
            "ld_sections": sum(s["language"] == "LD" for s in sections),
            "io_binding_count": len(self.io_bindings()),
            "protected_sections": sum(s["completeness"] == "protected" for s in sections),
            "unacquired_sections": sum(s["completeness"] != "observed" for s in sections),
            "coverage": {
                "fully_understood": False,
                "recognized_entries": sorted(self.recognized),
                "unknown_entries": sorted(
                    n
                    for n in self.archive.names
                    if n not in self.recognized and not n.endswith("/")
                ),
            },
            "sysmac_build": "not_executed",
        }

    def find_references(self, symbol: str):
        results = []
        for pou in self.pous():
            for section in self.read_pou(pou["id"])["sections"]:
                if section["language"] == "ST":
                    source = section["source"]
                    for token, start, end in st_tokens(source):
                        if token.casefold() == symbol.casefold():
                            results.append(
                                {
                                    "pou_id": pou["id"],
                                    "section_id": section["id"],
                                    "language": "ST",
                                    "line": source.count("\n", 0, start) + 1,
                                    "column": start - source.rfind("\n", 0, start),
                                    "access": "write"
                                    if source[end:].lstrip().startswith(":=")
                                    else "read_or_argument",
                                    "scope_resolution": "lexical_only",
                                }
                            )
                elif section["language"] == "LD":
                    for rung_no, rung in enumerate(section["rungs"]):
                        for path, source in walk_ld(rung):
                            if any(
                                t.casefold() == symbol.casefold() for t, _, _ in st_tokens(source)
                            ):
                                results.append(
                                    {
                                        "pou_id": pou["id"],
                                        "section_id": section["id"],
                                        "language": "LD",
                                        "rung": rung_no,
                                        "path": path,
                                        "scope_resolution": "lexical_only",
                                    }
                                )
        return results
