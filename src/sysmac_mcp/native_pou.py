"""Create native POU models from a readable observed template, without build caches."""

from __future__ import annotations

import copy
import json
from datetime import datetime
from typing import Any
from uuid import uuid4

from lxml import etree as ET
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .iec_xml import Identifier, LadderNetwork, PouSpec, VariableSpec
from .project import NativeProject, parse_xml, serialize_xml
from .st_lexer import KEYWORDS
from .symbols import controller


class NativePouDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=Identifier)
    source: str | None = Field(default=None, max_length=4 * 1024 * 1024)
    rungs: list[dict[str, Any]] | None = Field(default=None, min_length=1, max_length=10000)
    variables: list[VariableSpec] = Field(default_factory=list, max_length=10000)
    return_type: str | None = Field(default=None, min_length=1, max_length=1024)

    @model_validator(mode="after")
    def body(self):
        if (self.source is None) == (self.rungs is None):
            raise ValueError("Provide exactly one ST source or native Ladder rungs.")
        if self.source is not None and (not self.source.strip() or "\x00" in self.source):
            raise ValueError("Provide nonempty source without NUL.")
        if self.name.upper() in KEYWORDS:
            raise ValueError("POU name is reserved.")
        return self


def _field(value):
    text = str(value)
    if any(c in text for c in "\r\n\t\x00"):
        raise ValueError("Native declaration fields cannot contain SLWD delimiters.")
    return text


def native_ladder_rungs(network: LadderNetwork) -> list[dict[str, Any]]:
    """Convert a linear rung or one observed two-path branch to Sysmac JSON."""
    nodes = {node.id: node for node in network.nodes}
    left = next(node.id for node in network.nodes if node.kind == "left_rail")
    right = next(node.id for node in network.nodes if node.kind == "right_rail")
    incoming = {node_id: [] for node_id in nodes}
    outgoing = {node_id: [] for node_id in nodes}
    for source, target in network.connections:
        incoming[target].append(source)
        outgoing[source].append(target)

    order = network.topological_order()
    if len(order) != len(nodes):
        raise ValueError("Native Ladder import requires an acyclic connected rung.")
    forks = [node_id for node_id in nodes if len(outgoing[node_id]) > 1]
    joins = [node_id for node_id in nodes if len(incoming[node_id]) > 1]
    positions: dict[str, tuple[int, int]] = {}
    vertical_positions: list[int] = []
    horizontal_positions: list[int] = []

    if not forks and not joins:
        path = [left]
        while path[-1] != right:
            successors = outgoing[path[-1]]
            if len(successors) != 1:
                raise ValueError("Native Ladder import requires a connected series chain.")
            path.append(successors[0])
        if len(path) != len(nodes):
            raise ValueError("Native Ladder import does not support disconnected LD objects.")
        elements = [nodes[node_id] for node_id in path[1:-1]]
        positions = {node_id: (index, 0) for index, node_id in enumerate(path[1:-1])}
    else:
        if (
            len(forks) != 1
            or len(joins) != 1
            or len(outgoing[forks[0]]) != 2
            or len(incoming[joins[0]]) != 2
            or forks[0] == right
            or joins[0] in {left, right}
        ):
            raise ValueError(
                "Native Ladder import supports only one two-path branch with a single split and join."
            )

        split, join = forks[0], joins[0]
        prefix_route = [left]
        while prefix_route[-1] != split:
            successors = outgoing[prefix_route[-1]]
            if len(successors) != 1:
                raise ValueError(
                    "Native Ladder import supports only one two-path branch with a single split and join."
                )
            prefix_route.append(successors[0])
        prefix = prefix_route[1:]
        branch_paths = []
        for first in outgoing[split]:
            route = [first]
            while route[-1] != join:
                successors = outgoing[route[-1]]
                if len(successors) != 1 or len(route) > len(nodes):
                    raise ValueError(
                        "Native Ladder import supports only one two-path branch with linear paths."
                    )
                route.append(successors[0])
            branch = route[:-1]
            if not branch:
                raise ValueError("Each native Ladder branch must contain at least one contact.")
            branch_paths.append(branch)

        branch_ends = [path[-1] for path in branch_paths]
        if (
            branch_ends[0] == branch_ends[1]
            or set(incoming[join]) != set(branch_ends)
            or any(nodes[node_id].kind not in {"contact_no", "contact_nc"} for path in branch_paths for node_id in path)
        ):
            raise ValueError(
                "Native Ladder import supports only two disjoint contact paths with a shared join."
            )

        suffix = [join]
        while suffix[-1] != right:
            successors = outgoing[suffix[-1]]
            if len(successors) != 1 or len(suffix) > len(nodes):
                raise ValueError("Native Ladder import requires one linear output after the branch join.")
            suffix.append(successors[0])

        covered = {left, *prefix, *suffix}
        for path in branch_paths:
            covered.update(path)
        if covered != set(nodes):
            raise ValueError("Native Ladder import does not support disconnected LD objects.")

        branch_paths.sort(key=len)
        split_x = len(prefix)
        join_x = split_x + len(branch_paths[1])
        positions.update({node_id: (index, 0) for index, node_id in enumerate(prefix)})
        for row, path in enumerate(branch_paths):
            positions.update({node_id: (split_x + index, row) for index, node_id in enumerate(path)})
        for index, node_id in enumerate(suffix[:-1]):
            positions[node_id] = (join_x + index, 0)
        if split != left:
            vertical_positions.append(split_x)
        vertical_positions.append(join_x)
        horizontal_positions.extend(range(split_x + len(branch_paths[0]), join_x))

        elements = [nodes[node_id] for node_id in order if nodes[node_id].kind not in {"left_rail", "right_rail"}]

    coils = [node for node in elements if node.kind in {"coil", "coil_negated"}]
    if len(coils) != 1 or outgoing[coils[0].id] != [right] or incoming[right] != [coils[0].id]:
        raise ValueError("Native Ladder import requires one final output coil.")
    if coils[0].kind == "coil_negated":
        raise ValueError("Negated coils are not mapped to the observed Sysmac rung format.")
    if not any(node.kind in {"contact_no", "contact_nc"} for node in elements):
        raise ValueError("Native Ladder import requires at least one contact before the coil.")
    if any(node.kind not in {"contact_no", "contact_nc", "coil"} for node in elements):
        raise ValueError("Unsupported node in native Ladder network.")

    components = {}
    for index, node in enumerate(elements):
        component = {
            "__type": "ST" if node.kind == "coil" else "LD",
            "Var": _field(node.variable),
        }
        if node.kind == "contact_nc":
            component["Not"] = True
        if index:
            component["Ix"] = index
        x, y = positions[node.id]
        if x:
            component["X"] = x
        if y:
            component["Y"] = y
        components[node.id] = component

    cls = []
    for index, node in enumerate(elements):
        cls.append(components[node.id])
        next_x = positions[elements[index + 1].id][0] if index + 1 < len(elements) else float("inf")
        for x in horizontal_positions:
            if positions[node.id][0] <= x < next_x:
                cls.append({"__type": "HL", "X": x})

    return [
        {
            "CLs": cls,
            "LRI": len(elements),
            "RRI": len(elements) + 1,
            "VLs": [
                {"Ix": len(elements) + index + 2, "X": x}
                for index, x in enumerate(vertical_positions)
            ],
        }
    ]


def _variables(project, template, definition):
    kind = template.get("type")
    # Reuse the already verified declaration constraints, independently of body format.
    PouSpec(
        name=definition.name,
        kind=kind,
        source="(* native body *)",
        return_type=definition.return_type,
        variables=definition.variables,
    )
    if kind != "Function" and definition.return_type is not None:
        raise ValueError("Only functions have a return type.")
    implicit = []
    for variable in project.variables(template.get("id")):
        if variable["name"] in {"EN", "ENO"} and variable["attributes"].get("RO") == "1":
            if variable["data_type"] != "BOOL" or variable["group"] not in {
                "VAR_INPUT",
                "VAR_OUTPUT",
            }:
                raise ValueError("Unsupported implicit template parameter.")
            implicit.append({"D": "BOOL", "N": variable["name"], "RO": "1", "G": variable["group"]})
    names = {v["N"].casefold() for v in implicit}
    if kind == "Function":
        names.add(definition.name.casefold())
    groups = {
        "local": "VAR",
        "temp": "VAR",
        "input": "VAR_INPUT",
        "output": "VAR_OUTPUT",
        "inout": "VAR_IN_OUT",
        "external": "VAR_EXTERNAL",
    }
    rows = implicit[:]
    for variable in definition.variables:
        if variable.name.casefold() in names or variable.name.upper() in KEYWORDS:
            raise ValueError("Duplicate or reserved variable name in new POU.")
        names.add(variable.name.casefold())
        row = {"D": _field(variable.data_type), "N": variable.name, "G": groups[variable.scope]}
        if not row["D"].strip():
            raise ValueError("Variable data type is required.")
        for key, value in (("IV", variable.initial_value), ("Com", variable.comment)):
            if value is not None:
                row[key] = _field(value)
        if variable.retained:
            row["R"] = "1"
        if variable.constant:
            row["Const"] = "1"
        rows.append(row)
    groups_to_write = [("+GN=VAR\tGVT=DefaultGroup", {"VAR"})]
    if kind != "Program":
        groups_to_write.append(
            (
                "+GN=VAR_INPUT\tGA=InputOutput\tGVT=EdgeInputOutputGroup",
                {"VAR_INPUT", "VAR_OUTPUT", "VAR_IN_OUT"},
            )
        )
    groups_to_write.append(("+GN=VAR_EXTERNAL\tGA=External\tGVT=ExternalGroup", {"VAR_EXTERNAL"}))
    if kind == "Function":
        groups_to_write.append(("+GN=RETURN\tGA=Return\tGVT=ReturnGroup", {"RETURN"}))
        rows.append({"D": _field(definition.return_type), "N": definition.name, "G": "RETURN"})
    order = 0
    for row in rows:
        if row["G"] in {"VAR_INPUT", "VAR_OUTPUT", "VAR_IN_OUT"}:
            row["Ord"] = str(order)
            order += 1
    lines = ["[SLWD version=1.0]", "_EN=Variables"]
    for header, included in groups_to_write:
        lines.append(header)
        lines.extend(
            "++" + "\t".join(f"{k}={v}" for k, v in row.items())
            for row in rows
            if row["G"] in included
        )
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def prepare_pou(project, template_pou_id, definition):
    template = project.resolve(template_pou_id, {"Program", "Function", "FunctionBlock"})
    kind = template.get("type")
    cid = controller(template)
    if cid is None:
        raise ValueError("Template must belong to a known controller.")
    if any(
        p["name"].casefold() == definition.name.casefold()
        and controller(project.entities[p["id"]]) == cid
        for p in project.pous()
    ):
        raise ValueError("POU name already exists in this controller.")
    if any(
        v["scope"] == "global"
        and controller(project.entities[v["table_id"]]) == cid
        and v["name"].casefold() == definition.name.casefold()
        for v in project.variables()
    ):
        raise ValueError("POU name conflicts with a global declaration.")
    sections = project.read_pou(template.get("id"))["sections"]
    tables = template.findall('./ChildEntities/Entity[@type="Variables"]')
    if len(sections) != 1 or len(tables) != 1 or sections[0]["completeness"] != "observed":
        raise ValueError("Select a readable single-section template with one variable table.")
    variable_member = project.member(tables[0].get("id"))
    if not variable_member or not project.archive.read(variable_member).removeprefix(
        b"\xef\xbb\xbf"
    ).startswith(b"[SLWD version=1.0]"):
        raise ValueError("Unsupported variable template format.")
    language = sections[0]["language"]
    if (language == "ST") != (definition.source is not None):
        raise ValueError("Body language does not match the selected template.")
    allowed = {
        "Program",
        "Function",
        "FunctionBlock",
        "Variables",
        "PouBody",
        "SourceHolder",
        "PouBodySourceHolder",
        "PouIFLibraryHolder",
        "DebugProgramSetting",
        "Inline",
    }
    allowed_attrs = {
        "type",
        "subtype",
        "id",
        "name",
        "version",
        "dateCreated",
        "dateLastModified",
        "trackingId",
        "DN",
        "label",
        "author",
        "comment",
    }
    for entity in [template, *template.findall(".//Entity")]:
        if entity.get("type") not in allowed or set(entity.attrib) - allowed_attrs:
            raise ValueError("Unsupported template object or metadata shape.")
        if any(child.tag not in {"AccessInfos", "ChildEntities"} for child in entity):
            raise ValueError("Unsupported application XML in template.")
        if any(len(entity.findall(tag)) > 1 for tag in ("AccessInfos", "ChildEntities")):
            raise ValueError("Duplicate XML containers in template.")
        child_entities = entity.find("ChildEntities")
        if child_entities is not None and any(child.tag != "Entity" for child in child_entities):
            raise ValueError("Unsupported children in template entity container.")
        access = entity.find("AccessInfos")
        if access is not None and (len(access) or access.attrib or (access.text or "").strip()):
            raise ValueError("Template has access metadata that cannot be cloned.")
    variables = _variables(project, template, definition)
    root = copy.deepcopy(project.oem)
    original = next(e for e in root.findall(".//Entity") if e.get("id") == template.get("id"))
    clone = copy.deepcopy(original)
    for inline in list(clone.findall('.//Entity[@type="Inline"]')):
        inline.getparent().remove(inline)
    stamp = datetime.now().strftime("%m/%d/%Y %H:%M:%S")
    for entity in [clone, *clone.findall(".//Entity")]:
        entity.set("id", str(uuid4()))
        entity.set("trackingId", str(uuid4()))
        for date in ("dateCreated", "dateLastModified"):
            entity.set(date, stamp)
        for key in ("label", "author", "comment"):
            entity.attrib.pop(key, None)
    clone.set("name", definition.name)
    clone.set("DN", definition.name)
    table = clone.find('./ChildEntities/Entity[@type="Variables"]')
    body = clone.find('./ChildEntities/Entity[@type="PouBody"]')
    body.set("name", "Code" if language == "ST" else "Section0")
    original.getparent().append(clone)
    updates = {project.prefix + table.get("id") + ".xml": variables}
    if language == "ST":
        raw = project.archive.read(sections[0]["member"])
        model = parse_xml(raw)
        if len(model) != 1 or ET.QName(model[0]).localname != "Text":
            raise ValueError("Unsupported ST template model.")
        model[0].text = definition.source
        source = serialize_xml(model, raw)
    else:
        rungs = copy.deepcopy(definition.rungs)
        for rung in rungs:
            if not isinstance(rung.get("CLs"), list) or not all(
                isinstance(e, dict) and isinstance(e.get("__type"), str) for e in rung["CLs"]
            ):
                raise ValueError("Provide native Ladder rung objects with typed CLs.")
            for element in rung["CLs"]:
                if element.get("__type") == "IST":
                    if not isinstance(element.get("TXT"), str):
                        raise ValueError("Inline ST requires TXT source.")
                    tracking = str(uuid4())
                    element["EID"] = tracking
                    children = body.find("ChildEntities")
                    if children is None:
                        children = ET.SubElement(body, "ChildEntities")
                    inline = ET.SubElement(
                        children,
                        "Entity",
                        type="Inline",
                        subtype="StructuredText",
                        id=str(uuid4()),
                        name=tracking,
                        trackingId=tracking,
                        version="0",
                        dateCreated=stamp,
                        dateLastModified=stamp,
                    )
                    ET.SubElement(inline, "AccessInfos")
                    ET.SubElement(inline, "ChildEntities")
        source = b"\xef\xbb\xbf" + (
            "\r\n".join(
                json.dumps(r, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                for r in rungs
            )
            + "\r\n"
        ).encode("utf-8")
    updates[project.prefix + body.get("id") + ".xml"] = source
    cache_ids, debug_id = [], None
    for entity in clone.findall(".//Entity"):
        if entity.get("type") == "PouBodySourceHolder":
            cache_ids.append(entity.get("id"))
            data = ET.Element("data")
            ET.SubElement(data, "SectionUsingMCOrMcr").text = "false"
            ET.SubElement(data, "CxilVariableGroup", POUTrackingId=clone.get("trackingId"))
            updates[project.prefix + entity.get("id") + ".xml"] = serialize_xml(data)
        elif entity.get("type") == "DebugProgramSetting":
            debug_id = entity.get("id")
            data = ET.Element("data")
            ET.SubElement(data, "DebugProgramSetting", Flag="False")
            updates[project.prefix + debug_id + ".xml"] = serialize_xml(data)
    updates[project.oem_name] = serialize_xml(root, project.archive.read(project.oem_name))
    from .external_dependencies import register_new_dependencies

    updates, registrations = register_new_dependencies(project, updates, clone.get("id"))
    NativeProject(project.archive.path, {**project.archive.overrides, **updates}).read_pou(
        clone.get("id")
    )
    return updates, {
        "operation": "create_pou",
        "template_pou_id": template.get("id"),
        "pou_id": clone.get("id"),
        "name": definition.name,
        "kind": kind,
        "language": language,
        "table_id": table.get("id"),
        "section_id": body.get("id"),
        "debug_id": debug_id,
        "cache_ids": cache_ids,
        "registered_externals": registrations,
        "added_members": sorted(n for n in updates if n not in project.archive.members),
        "cached_build_data": "not_generated",
        "sysmac_build": "not_executed",
    }


def prepare_pou_deletion(project, pou_id):
    pou = project.resolve(pou_id, {"Program", "Function", "FunctionBlock"})
    controller_id = controller(pou)
    if controller_id is None:
        raise ValueError("POU must belong to a known controller.")
    name = pou.get("name", "")
    parent = pou.getparent()
    if parent is None or parent.tag != "ChildEntities":
        raise ValueError("POU is not attached through a supported OEM entity container.")

    controller_pous = [
        item
        for item in project.pous()
        if controller(project.entities[item["id"]]) == controller_id
    ]
    controller_pou_ids = {item["id"] for item in controller_pous}
    unavailable = [
        section
        for item in controller_pous
        for section in project.read_pou(item["id"])["sections"]
        if section["completeness"] != "observed"
    ]
    if unavailable:
        raise ValueError("Cannot safely delete a POU while controller source sections are unavailable.")

    all_tasks = project.tasks()
    tasks = [task for task in all_tasks if task["controller_id"] == controller_id]
    if any(task["diagnostics"] for task in tasks):
        raise ValueError("Cannot safely delete a POU while task assignments are inconsistent.")
    from .task_model import tracking

    target_tracking = tracking(pou.get("trackingId"))
    for task in all_tasks:
        for program in task["programs"]:
            if program.get("program_id") == pou.get("id"):
                raise ValueError("Unassign the Program from all tasks before deleting its POU.")
            if task["controller_id"] == controller_id:
                continue
            row_tracking = tracking(program.get("IniFileTrackingId"))
            same_identity = target_tracking is not None and row_tracking == target_tracking
            ambiguous_name = (
                row_tracking is None
                and program.get("ProgramName", "").casefold() == name.casefold()
            )
            if same_identity or ambiguous_name:
                raise ValueError("POU is referenced by another task outside its controller.")

    from .st_lexer import lex
    from .symbols import SymbolIndex

    index = SymbolIndex(project)
    source_diagnostics = [
        diagnostic
        for diagnostic in index.diagnostics
        if diagnostic.get("pou_id") in controller_pou_ids - {pou.get("id")}
    ]
    if source_diagnostics:
        raise ValueError("Cannot safely delete a POU while other controller source cannot be fully analyzed.")

    source_references = [
        occurrence
        for occurrence in index.occurrences
        if occurrence.get("pou_id") != pou.get("id")
        and occurrence.get("pou_id") in controller_pou_ids
        and occurrence.get("spelling", "").casefold() == name.casefold()
    ]
    if source_references:
        raise ValueError("POU name appears in another observed source section; remove those references first.")

    for variable in index.variables:
        if variable.get("controller_id") != controller_id or variable.get("scope") == pou.get("id"):
            continue
        for expression in (variable.get("data_type", ""), variable.get("initial_value") or ""):
            parsed = lex(expression)
            if parsed.diagnostics:
                raise ValueError("A controller declaration expression cannot be fully analyzed.")
            if any(
                token.kind == "identifier" and token.text.casefold() == name.casefold()
                for token in parsed.tokens
            ):
                raise ValueError("POU is referenced by another controller variable declaration.")

    type_inventory = project.data_types(controller_id=controller_id)
    if type_inventory["completeness"] != "observed":
        raise ValueError("Cannot safely delete a POU while controller DataType tables are incomplete.")
    for entity in project.entities.values():
        if entity.get("type") != "DataType" or controller(entity) != controller_id:
            continue
        member = project.member(entity.get("id"))
        if member is None:
            raise ValueError("Controller DataType member changed; read the project again.")
        root = parse_xml(project.archive.read(member))
        for node in root.iter():
            if not isinstance(node.tag, str) or ET.QName(node).localname != "DataType":
                continue
            for expression in (node.get("BaseType") or "", node.get("InitialValue") or ""):
                parsed = lex(expression)
                if parsed.diagnostics:
                    raise ValueError("A controller DataType expression cannot be fully analyzed.")
                if any(
                    token.kind == "identifier" and token.text.casefold() == name.casefold()
                    for token in parsed.tokens
                ):
                    raise ValueError("POU is referenced by a controller DataType definition.")

    preserved_members = {
        member
        for entity in [pou, *pou.iterdescendants()]
        if entity.tag == "Entity"
        for member in [project.member(entity.get("id"))]
        if member is not None
    }
    parent.remove(pou)
    updates = {
        project.oem_name: serialize_xml(project.oem, project.archive.read(project.oem_name))
    }
    candidate = NativeProject(project.archive.path, {**project.archive.overrides, **updates})
    if pou.get("id") in candidate.entities:
        raise ValueError("POU deletion did not remove its OEM entity.")
    return updates, {
        "operation": "delete_pou",
        "pou_id": pou.get("id"),
        "name": name,
        "kind": pou.get("type"),
        "controller_id": controller_id,
        "preserved_archive_members": len(preserved_members),
        "external_consumers": "not_acquired",
        "sysmac_build": "not_executed",
    }
