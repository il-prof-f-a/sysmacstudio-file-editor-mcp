"""Guarded edits for completely observed native Sysmac DataType tables."""

import copy
import json
import re
from collections import defaultdict

from lxml import etree as ET

from .project import NativeProject, parse_xml, serialize_xml, xml_local_name
from .st_lexer import KEYWORDS, lex
from .symbols import SymbolIndex, controller


class _RenameBlocked(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.blocker = {"code": code, "message": message, **details}


def _uses_identifier(source, name, context):
    result = lex(str(source or ""))
    if result.diagnostics:
        raise ValueError(f"Cannot safely delete a DataType while {context} cannot be analyzed.")
    folded = name.casefold()
    return any(token.kind == "identifier" and token.text.casefold() == folded for token in result.tokens)


def _table_root(project, table):
    try:
        root = parse_xml(project.archive.read(table["member"]))
    except (ValueError, OSError, ET.LxmlError) as exc:
        raise ValueError("A native DataType table cannot be read completely.") from exc
    if xml_local_name(root) != "data":
        raise ValueError("A native DataType table has an unsupported XML root.")
    return root


def _definition_parent(root, table):
    children = list(root)
    if any(xml_local_name(child) != "DataType" for child in children):
        raise ValueError("A native DataType table contains unsupported entries.")
    if table.get("group_attributes") is not None:
        if (
            len(children) != 1
            or children[0].get("BaseType", "").strip() != ""
            or any(xml_local_name(child) != "DataType" for child in children[0])
        ):
            raise ValueError("A native DataType definitions group is not recognized.")
        return children[0]
    return root


def _inspect_struct_layout(node, table_id, path, diagnostics, counts):
    if (node.get("BaseType") or "").strip().upper() != "STRUCT":
        return

    struct_id = f"{table_id}:{path}"
    members = [child for child in node if xml_local_name(child) == "DataType"]
    counts["structs"] += 1
    if not members:
        diagnostics.append(
            {
                "code": "empty_data_type",
                "data_type_id": struct_id,
                "message": "A STRUCT data type has no members.",
            }
        )

    if (node.get("OffsetType") or "").strip().casefold() == "user":
        for index, member in enumerate(members):
            counts["user_layout_members"] += 1
            missing = []
            if not (member.get("OffsetChannel") or "").strip():
                missing.append("byte")
            if (member.get("BaseType") or "").strip().upper() == "BOOL" and not (
                member.get("OffsetBit") or ""
            ).strip():
                missing.append("bit")
            if missing:
                diagnostics.append(
                    {
                        "code": "user_offset_not_set",
                        "data_type_id": struct_id,
                        "member_id": f"{struct_id}.{index}",
                        "missing_offsets": missing,
                        "message": "A STRUCT member is missing an explicit User offset.",
                    }
                )

    for index, child in enumerate(members):
        _inspect_struct_layout(child, table_id, f"{path}.{index}", diagnostics, counts)


def layout_diagnostics(project):
    """Report definite empty-STRUCT and explicit User-offset omissions."""
    inventory = project.data_types()
    diagnostics = []
    checked_tables = 0
    skipped_tables = 0
    counts = {"structs": 0, "user_layout_members": 0}

    for table in inventory["tables"]:
        if table["completeness"] != "observed":
            skipped_tables += 1
            continue
        try:
            root = _table_root(project, table)
            parent = _definition_parent(root, table)
        except ValueError:
            skipped_tables += 1
            continue

        checked_tables += 1

        for index, definition in enumerate(parent):
            if xml_local_name(definition) == "DataType":
                _inspect_struct_layout(definition, table["id"], str(index), diagnostics, counts)

    diagnostics.sort(
        key=lambda item: (
            item.get("data_type_id", ""),
            item.get("member_id", ""),
            item["code"],
        )
    )
    return {
        "check": "partial_observed_struct_checks" if checked_tables else "not_executed",
        "coverage": {
            "completeness": "partial" if checked_tables else "not_executed",
            "checked_tables": checked_tables,
            "skipped_incomplete_tables": skipped_tables,
            "checked_structs": counts["structs"],
            "checked_user_layout_members": counts["user_layout_members"],
            "checked": (
                "empty STRUCT definitions and missing explicit byte offsets for members of "
                "OffsetType=User structures; BOOL members also require an explicit bit offset"
            ),
            "not_checked": [
                "automatic NJ/CJ layout calculations",
                "UNION layout",
                "type size, overlap, alignment, and offset range validation",
                "partial, protected, or otherwise incomplete DataType tables",
            ],
        },
        "diagnostics": diagnostics,
    }


def _type_tables(project, controller_id):
    inventory = project.data_types(controller_id=controller_id)
    tables = inventory["tables"]
    if not tables or any(table["completeness"] != "observed" for table in tables):
        raise ValueError(
            "All controller DataType tables must be completely observed before native DataType editing."
        )
    return inventory, tables


def _controller_pous(project, controller_id):
    pous = [
        pou
        for pou in project.pous()
        if controller(project.entities.get(pou["id"])) == controller_id
    ]
    pou_ids = {pou["id"] for pou in pous}
    index = SymbolIndex(project)
    sections = [section for section in index.sections if section["pou_id"] in pou_ids]
    if any(section["completeness"] != "observed" for section in sections):
        raise ValueError(
            "Controller source sections must be completely observed before native DataType editing."
        )
    if any(pou_id not in {section["pou_id"] for section in sections} for pou_id in pou_ids):
        raise ValueError(
            "Every controller POU must expose observed source sections before native DataType editing."
        )
    return index, pou_ids


def _references(project, index, tables, pou_ids, controller_id, target_name, target_table_id, target_node):
    for variable in index.variables:
        if variable.get("controller_id") != controller_id:
            continue
        for expression in (variable.get("data_type"), variable.get("initial_value")):
            if _uses_identifier(expression, target_name, "a variable declaration"):
                raise ValueError("DataType is referenced by a controller variable declaration.")

    for field in index.fields:
        if field.get("pou_id") in pou_ids:
            if _uses_identifier(field.get("source"), target_name, "controller source"):
                raise ValueError("DataType is referenced by observed controller source.")

    for table in tables:
        root = _table_root(project, table)
        for node in root.iter():
            if xml_local_name(node) != "DataType" or (table["id"] == target_table_id and node is target_node):
                continue
            for attribute in ("BaseType", "ArrayType", "InitialValue"):
                if _uses_identifier(node.get(attribute), target_name, "a DataType definition"):
                    raise ValueError("DataType is referenced by another controller DataType.")


def _identifier_tokens(source, name, context):
    result = lex(str(source or ""))
    if result.diagnostics:
        raise _RenameBlocked(
            "source_unanalyzable",
            f"Cannot safely inspect {context} because lexical analysis is incomplete.",
            context=context,
        )
    folded = name.casefold()
    return result.tokens, [
        index
        for index, token in enumerate(result.tokens)
        if token.kind in {"identifier", "keyword"} and token.text.casefold() == folded
    ]


def _replace_token_spans(source, tokens, positions, new_name):
    result = source
    for position in reversed(positions):
        token = tokens[position]
        if result[token.start : token.end] != token.text:
            raise _RenameBlocked(
                "source_span_changed", "A DataType reference changed while preparing the rename."
            )
        result = result[: token.start] + new_name + result[token.end :]
    return result


def _rewrite_type_expression(source, old_name, new_name, context):
    if not isinstance(source, str) or not source:
        return source
    tokens, matches = _identifier_tokens(source, old_name, context)
    if not matches:
        return source

    supported = False
    if len(matches) == 1 and len(tokens) == 1 and matches == [0]:
        supported = True
    elif len(matches) == 1 and matches[0] == len(tokens) - 1:
        folded = [token.text.casefold() for token in tokens]
        if (
            len(tokens) >= 6
            and folded[0] == "array"
            and tokens[1].text == "["
            and tokens[-3].text == "]"
            and folded[-2] == "of"
        ):
            depth = 0
            closes_at_end = False
            for index, token in enumerate(tokens[1:-2], start=1):
                if token.text == "[":
                    depth += 1
                elif token.text == "]":
                    depth -= 1
                    closes_at_end = depth == 0 and index == len(tokens) - 3
                    if depth < 0:
                        break
            supported = closes_at_end and depth == 0
        elif folded in (["ref_to", old_name.casefold()], ["reference", "to", old_name.casefold()], ["pointer", "to", old_name.casefold()]):
            supported = True

    if not supported:
        raise _RenameBlocked(
            "unsupported_type_expression",
            f"A reference to {old_name!r} appears in an unsupported {context} expression.",
            context=context,
        )
    return _replace_token_spans(source, tokens, matches, new_name)


def _rewrite_typed_literals(source, old_name, new_name, context):
    if not isinstance(source, str) or not source:
        return source, 0
    tokens, matches = _identifier_tokens(source, old_name, context)
    edits = []
    for position in matches:
        if position + 1 >= len(tokens) or tokens[position + 1].text != "#":
            continue
        if position > 0 and tokens[position - 1].text in {".", "::"}:
            raise _RenameBlocked(
                "qualified_type_reference",
                f"A qualified reference to {old_name!r} in {context} is outside the supported type scope.",
                context=context,
            )
        edits.append(position)
    return _replace_token_spans(source, tokens, edits, new_name), len(edits)


def _controller_variable_tables(project, controller_id):
    variables = project.variables()
    by_member = defaultdict(dict)
    for variable in variables:
        variable_entity = project.entities.get(variable["table_id"])
        variable_controller = controller(variable_entity)
        variable["controller_id"] = variable_controller
        variable["library_id"] = _library_id(variable_entity)
        if variable_controller == controller_id:
            by_member[variable["member"]][variable["line_index"]] = variable

    tables = [
        entity
        for entity in project.entities.values()
        if entity.get("type") == "Variables"
        and controller(entity) == controller_id
    ]
    for table in tables:
        member = project.member(table.get("id"))
        if member is None:
            raise _RenameBlocked(
                "variable_table_unavailable",
                "A controller variable table has no readable archive member.",
                table_id=table.get("id"),
            )
        raw = project.archive.read(member)
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise _RenameBlocked(
                "variable_table_unavailable",
                "A controller variable table is not UTF-8 text.",
                table_id=table.get("id"),
            ) from exc
        lines = text.splitlines()
        if not lines or lines[0] != "[SLWD version=1.0]":
            raise _RenameBlocked(
                "variable_table_unavailable",
                "A controller variable table is not in the observed SLWD 1.0 format.",
                table_id=table.get("id"),
            )
        parsed_lines = []
        for line_index, line in enumerate(lines):
            if not line.startswith("++"):
                continue
            fields = {}
            for part in line[2:].split("\t"):
                if "=" not in part:
                    raise _RenameBlocked(
                        "variable_row_unrecognized",
                        "A controller variable row has an unrecognized field.",
                        table_id=table.get("id"),
                        line_index=line_index,
                    )
                key, value = part.split("=", 1)
                if key in fields:
                    raise _RenameBlocked(
                        "variable_row_unrecognized",
                        "A controller variable row has duplicate fields.",
                        table_id=table.get("id"),
                        line_index=line_index,
                    )
                fields[key] = value
            if not fields.get("N") or "D" not in fields:
                raise _RenameBlocked(
                    "variable_row_unrecognized",
                    "A controller variable row is missing its name or data type.",
                    table_id=table.get("id"),
                    line_index=line_index,
                )
            parsed_lines.append(line_index)
        if parsed_lines != sorted(by_member.get(member, {})):
            raise _RenameBlocked(
                "variable_table_partial",
                "The parsed controller variable rows do not match the observed declarations.",
                table_id=table.get("id"),
            )
    return variables


def _library_id(entity):
    current = entity
    while current is not None:
        if current.tag == "Entity" and current.get("type") == "Library":
            return current.get("id")
        current = current.getparent()
    return None


def _replace_slwd_fields(raw, line_index, replacements, table_id):
    lines = raw.decode("utf-8-sig").splitlines(keepends=True)
    if line_index >= len(lines):
        raise _RenameBlocked(
            "variable_row_unrecognized",
            "A controller variable row moved while preparing the rename.",
            table_id=table_id,
            line_index=line_index,
        )
    line = lines[line_index]
    ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else "\r" if line.endswith("\r") else ""
    content = line[: -len(ending)] if ending else line
    if not content.startswith("++"):
        raise _RenameBlocked(
            "variable_row_unrecognized",
            "A controller variable row moved while preparing the rename.",
            table_id=table_id,
            line_index=line_index,
        )
    pieces = content[2:].split("\t")
    seen = set()
    for index, piece in enumerate(pieces):
        if "=" not in piece:
            continue
        key, value = piece.split("=", 1)
        if key in replacements:
            expected, updated = replacements[key]
            if key in seen or value != expected:
                raise _RenameBlocked(
                    "variable_row_changed",
                    "A controller variable field changed while preparing the rename.",
                    table_id=table_id,
                    line_index=line_index,
                    field=key,
                )
            pieces[index] = f"{key}={updated}"
            seen.add(key)
    if seen != set(replacements):
        raise _RenameBlocked(
            "variable_row_changed",
            "A controller variable field could not be uniquely located.",
            table_id=table_id,
            line_index=line_index,
        )
    lines[line_index] = "++" + "\t".join(pieces) + ending
    return (b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b"") + "".join(lines).encode("utf-8")


def _ladder_instruction(rungs, rung_index, path):
    node = rungs[rung_index]
    parts = path.strip("/").split("/")
    for part in parts[:-1]:
        node = node[int(part)] if isinstance(node, list) else node[part]
    return node


def _set_ladder_field(rungs, rung_index, path, value):
    node = rungs[rung_index]
    parts = path.strip("/").split("/")
    for part in parts[:-1]:
        node = node[int(part)] if isinstance(node, list) else node[part]
    node[parts[-1]] = value


def prepare_data_type_rename(
    project, data_type_id, expected_name, expected_base_type, new_name
):
    if not isinstance(expected_name, str) or not expected_name.strip():
        raise ValueError("expected_name must match the listed DataType name.")
    if not isinstance(expected_base_type, str) or not expected_base_type.strip():
        raise ValueError("expected_base_type must match the listed DataType BaseType.")
    if not isinstance(data_type_id, str) or ":" not in data_type_id:
        raise ValueError("DataType ID must come from plc_list_data_types.")

    tables_by_id = {
        entity.get("id"): entity
        for entity in project.entities.values()
        if entity.get("type") == "DataType"
    }
    table_ids = [table_id for table_id in tables_by_id if data_type_id.startswith(f"{table_id}:")]
    if not table_ids:
        raise ValueError("DataType table identity is not present in this project revision.")
    table_id = max(table_ids, key=len)
    table_entity = tables_by_id[table_id]
    controller_id = controller(table_entity)
    if controller_id is None:
        raise ValueError("DataType table must belong to a known controller.")

    inventory, tables = _type_tables(project, controller_id)
    table = next((item for item in tables if item["id"] == table_id), None)
    if table is None or table.get("library_id") is not None:
        raise ValueError("Only controller-owned DataType tables can be edited.")
    matches = [item for item in inventory["items"] if item["id"] == data_type_id]
    if len(matches) != 1:
        raise ValueError("DataType ID is not uniquely resolvable; list DataTypes again.")
    record = matches[0]
    if record.get("name") != expected_name or record.get("base_type") != expected_base_type:
        raise ValueError("DataType identity changed; list DataTypes again.")
    if record.get("parent_id") is not None or record.get("depth") != 0:
        raise ValueError("Only top-level DataTypes can be renamed.")
    if not record.get("name") or not record.get("base_type"):
        raise ValueError("DataType is missing a name or BaseType and cannot be safely renamed.")

    path = data_type_id[len(table_id) + 1 :]
    if "." in path or not path.isdigit():
        raise ValueError("Only top-level DataType paths can be renamed.")
    target_root = _table_root(project, table)
    target_parent = _definition_parent(target_root, table)
    definitions = list(target_parent)
    position = int(path)
    if position >= len(definitions):
        raise ValueError("DataType table changed; list DataTypes again.")
    target_node = definitions[position]
    if (
        xml_local_name(target_node) != "DataType"
        or target_node.get("Name") != expected_name
        or target_node.get("BaseType") != expected_base_type
    ):
        raise ValueError("DataType identity changed; list DataTypes again.")

    plan = {
        "operation": "rename_data_type",
        "data_type_id": data_type_id,
        "controller_id": controller_id,
        "table_id": table_id,
        "old_name": expected_name,
        "new_name": new_name,
        "base_type": expected_base_type,
        "can_apply": False,
        "blockers": [],
        "references": [],
        "changed_members": [],
        "coverage": {
            "controller_data_type_tables": "observed",
            "controller_variable_tables": "not_checked",
            "controller_source_sections": "not_checked",
            "libraries_and_external_consumers": "not_acquired",
            "other_controllers": "not_scanned",
            "sysmac_build": "not_executed",
        },
    }
    if (
        not isinstance(new_name, str)
        or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,126}", new_name)
        or new_name.upper() in KEYWORDS
    ):
        plan["blockers"].append(
            {"code": "invalid_or_reserved_name", "message": "new_name is not a valid IEC identifier."}
        )
        return plan, {}
    if new_name == expected_name:
        plan["blockers"].append(
            {"code": "name_unchanged", "message": "The requested DataType name is unchanged."}
        )
        return plan, {}

    top_level_names = [
        item.get("name", "").casefold()
        for item in inventory["items"]
        if item.get("parent_id") is None and item.get("name")
    ]
    if top_level_names.count(expected_name.casefold()) != 1:
        plan["blockers"].append(
            {"code": "ambiguous_type_name", "message": "The existing DataType name is ambiguous."}
        )
        return plan, {}

    try:
        variables = _controller_variable_tables(project, controller_id)
        index, pou_ids = _controller_pous(project, controller_id)
        library_pou_ids = {
            pou["id"]
            for pou in index.pous
            if controller(project.entities.get(pou["id"])) == controller_id
            and _library_id(project.entities.get(pou["id"])) is not None
        }
        plan["coverage"].update(
            controller_variable_tables="observed", controller_source_sections="observed"
        )
        bad_diagnostics = [
            diagnostic
            for diagnostic in index.diagnostics
            if (
                diagnostic.get("pou_id") in pou_ids
                or any(
                    variable_id in index.by_id
                    and index.by_id[variable_id].get("controller_id") == controller_id
                    for variable_id in diagnostic.get("variable_ids", [])
                )
            )
            and diagnostic.get("code")
            not in {"external_type_mismatch", "external_target_unresolved"}
        ]
        if bad_diagnostics:
            raise _RenameBlocked(
                "source_coverage_incomplete",
                "Controller source contains constructs that prevent a complete DataType reference scan.",
                diagnostics=bad_diagnostics[:10],
            )

        collision = next(
            (
                item
                for item in inventory["items"]
                if item.get("parent_id") is None
                and item.get("id") != data_type_id
                and item.get("name", "").casefold() == new_name.casefold()
            ),
            None,
        )
        if collision is not None:
            raise _RenameBlocked(
                "data_type_collision",
                "Another top-level DataType already has the requested name.",
                conflicting_data_type_id=collision["id"],
            )
        variable_collision = next(
            (
                variable
                for variable in variables
                if variable.get("controller_id") == controller_id
                and variable.get("name", "").casefold() == new_name.casefold()
            ),
            None,
        )
        if variable_collision is not None:
            raise _RenameBlocked(
                "declaration_collision",
                "A controller variable already has the requested name.",
                variable_id=variable_collision["id"],
            )
        pou_collision = next(
            (
                pou
                for pou in index.pous
                if controller(project.entities.get(pou["id"])) == controller_id
                and pou["name"].casefold() == new_name.casefold()
            ),
            None,
        )
        if pou_collision is not None:
            raise _RenameBlocked(
                "pou_collision",
                "A controller POU already has the requested name.",
                pou_id=pou_collision["id"],
            )

        roots = {}
        for current_table in tables:
            raw = project.archive.read(current_table["member"])
            root = _table_root(project, current_table)
            parent = _definition_parent(root, current_table)
            roots[current_table["id"]] = (raw, root, parent)

        patches = {}
        references = [{"kind": "data_type_name", "member": table["member"], "old": expected_name, "new": new_name}]
        for current_table in tables:
            raw, root, _ = roots[current_table["id"]]
            changed = False
            for node in root.iter():
                if xml_local_name(node) != "DataType":
                    continue
                type_name = node.get("Name", "")
                base_type = node.get("BaseType")
                updated_base = _rewrite_type_expression(
                    base_type, expected_name, new_name, f"BaseType of DataType {type_name!r}"
                )
                old_initial = node.get("InitialValue")
                updated_initial = _rewrite_typed_literals(
                    old_initial,
                    expected_name,
                    new_name,
                    f"initial value of DataType {type_name!r}",
                )[0]
                for attribute, value in node.attrib.items():
                    if attribute in {"Name", "Comment", "BaseType", "InitialValue"}:
                        continue
                    _, attribute_matches = _identifier_tokens(
                        value, expected_name, f"attribute {attribute!r} of DataType {type_name!r}"
                    )
                    if attribute_matches:
                        raise _RenameBlocked(
                            "unsupported_type_attribute",
                            f"A possible DataType reference occurs in unsupported attribute {attribute!r}.",
                            member=current_table["member"],
                            data_type_name=type_name,
                            attribute=attribute,
                        )
                if updated_base != base_type:
                    if current_table.get("library_id") is not None:
                        raise _RenameBlocked(
                            "library_consumer",
                            "A library DataType references the target and cannot be rewritten by this operation.",
                            member=current_table["member"],
                            data_type_name=type_name,
                        )
                    node.set("BaseType", updated_base)
                    changed = True
                    references.append(
                        {
                            "kind": "data_type_base_type",
                            "member": current_table["member"],
                            "data_type_name": type_name,
                            "old": base_type,
                            "new": updated_base,
                        }
                    )
                if updated_initial != node.get("InitialValue"):
                    if current_table.get("library_id") is not None:
                        raise _RenameBlocked(
                            "library_consumer",
                            "A library DataType initial value references the target and cannot be rewritten by this operation.",
                            member=current_table["member"],
                            data_type_name=type_name,
                        )
                    node.set("InitialValue", updated_initial)
                    changed = True
                    references.append(
                        {
                            "kind": "data_type_initial_value",
                            "member": current_table["member"],
                            "data_type_name": type_name,
                            "old": old_initial,
                            "new": updated_initial,
                        }
                    )
            if current_table["id"] == table_id:
                raw, root, parent = roots[current_table["id"]]
                target = list(parent)[position]
                target.set("Name", new_name)
                changed = True
            if changed:
                patches[current_table["member"]] = serialize_xml(root, raw)

        variable_rows = defaultdict(dict)
        variable_raw = {}
        for variable in variables:
            if variable.get("controller_id") != controller_id:
                continue
            updated_type = _rewrite_type_expression(
                variable.get("data_type"),
                expected_name,
                new_name,
                f"declaration {variable['name']!r}",
            )
            updated_initial = _rewrite_typed_literals(
                variable.get("initial_value"),
                expected_name,
                new_name,
                f"initializer of declaration {variable['name']!r}",
            )[0]
            replacements = {}
            if updated_type != variable.get("data_type"):
                replacements["D"] = (variable["data_type"], updated_type)
                references.append(
                    {
                        "kind": "variable_type",
                        "variable_id": variable["id"],
                        "old": variable["data_type"],
                        "new": updated_type,
                    }
                )
            if updated_initial != variable.get("initial_value"):
                replacements["IV"] = (variable.get("initial_value"), updated_initial)
                references.append(
                    {
                        "kind": "variable_initial_value",
                        "variable_id": variable["id"],
                        "old": variable.get("initial_value"),
                        "new": updated_initial,
                    }
                )
            if replacements:
                if variable.get("library_id") is not None:
                    raise _RenameBlocked(
                        "library_consumer",
                        "A library variable declaration references the target and cannot be rewritten by this operation.",
                        variable_id=variable["id"],
                    )
                member = variable["member"]
                variable_raw.setdefault(member, project.archive.read(member))
                variable_rows[member][variable["line_index"]] = replacements

        for member, rows in variable_rows.items():
            updated = variable_raw[member]
            table_variables = [
                variable
                for variable in variables
                if variable.get("member") == member and variable.get("controller_id") == controller_id
            ]
            by_line = {variable["line_index"]: variable for variable in table_variables}
            for line_index, replacements in sorted(rows.items()):
                updated = _replace_slwd_fields(
                    updated, line_index, replacements, by_line[line_index]["table_id"]
                )
            patches[member] = updated

        sections = {section["id"]: section for section in index.sections}
        fields_by_section = defaultdict(list)
        for field in index.fields:
            if field.get("pou_id") in pou_ids:
                fields_by_section[field["section_id"]].append(field)
        st_sources = {}
        ladders = {}
        for section_id, fields in fields_by_section.items():
            section = sections[section_id]
            for field in fields:
                updated_source, count = _rewrite_typed_literals(
                    field.get("source"),
                    expected_name,
                    new_name,
                    f"source section {section_id!r}",
                )
                if count == 0:
                    continue
                if section["pou_id"] in library_pou_ids:
                    raise _RenameBlocked(
                        "library_consumer",
                        "A library POU source references the target and cannot be rewritten by this operation.",
                        section_id=section_id,
                    )
                references.append(
                    {
                        "kind": "source_type_literal",
                        "section_id": section_id,
                        "language": section["language"],
                        "path": field.get("path"),
                        "count": count,
                    }
                )
                if section["language"] == "ST":
                    st_sources[section_id] = updated_source
                elif section["language"] == "LD":
                    member = section["member"]
                    if member not in ladders:
                        ladders[member] = copy.deepcopy(section["rungs"])
                    instruction = _ladder_instruction(
                        ladders[member], field.get("rung", 0), field.get("path") or ""
                    )
                    if not (
                        instruction.get("__type") == "ST"
                        or instruction.get("__type") in {"PRM", "PF"}
                        and str(field.get("path", "")).endswith("/Arg")
                    ):
                        raise _RenameBlocked(
                            "unsupported_ladder_type_literal",
                            "A typed literal occurs in a Ladder field that is not modeled as ST or a parameter expression.",
                            section_id=section_id,
                            path=field.get("path"),
                        )
                    _set_ladder_field(
                        ladders[member], field.get("rung", 0), field.get("path") or "", updated_source
                    )
                else:
                    raise _RenameBlocked(
                        "unsupported_source_language",
                        "A typed literal occurs in an unsupported controller source language.",
                        section_id=section_id,
                    )

        for section_id, source in st_sources.items():
            section = sections[section_id]
            raw = project.archive.read(section["member"])
            root = parse_xml(raw)
            texts = root.xpath('//*[local-name()="Text"]')
            if xml_local_name(root) != "StructuredTextModel" or len(texts) != 1:
                raise _RenameBlocked(
                    "source_unrecognized",
                    "A controller Structured Text section cannot be rewritten safely.",
                    section_id=section_id,
                )
            texts[0].text = source
            patches[section["member"]] = serialize_xml(root, raw)
        for member, rungs in ladders.items():
            raw = project.archive.read(member)
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise _RenameBlocked(
                    "source_unrecognized", "A Ladder source member is not UTF-8 text."
                ) from exc
            ending = "\r\n" if "\r\n" in text else "\n"
            content = ending.join(
                json.dumps(rung, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                for rung in rungs
            )
            if text.endswith(("\r", "\n")):
                content += ending
            patches[member] = (b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b"") + content.encode(
                "utf-8"
            )

        candidate = NativeProject(project.archive.path, {**project.archive.overrides, **patches})
        after = candidate.data_types(controller_id=controller_id)
        renamed = [item for item in after["items"] if item.get("id") == data_type_id]
        if after["completeness"] != "observed" or len(renamed) != 1 or renamed[0].get("name") != new_name:
            raise _RenameBlocked(
                "data_type_rename_readback_failed",
                "The staged DataType rename did not pass structural read-back.",
            )
        after_index = SymbolIndex(candidate)
        for variable in after_index.variables:
            if variable.get("controller_id") != controller_id:
                continue
            _, type_matches = _identifier_tokens(
                variable.get("data_type"), expected_name, "candidate variable type"
            )
            initializer_tokens, literal_matches = _identifier_tokens(
                variable.get("initial_value"), expected_name, "candidate variable initializer"
            )
            if type_matches or any(
                position + 1 < len(initializer_tokens)
                and initializer_tokens[position + 1].text == "#"
                for position in literal_matches
            ):
                raise _RenameBlocked(
                    "data_type_reference_readback_failed",
                    "An observed variable reference still uses the old DataType name.",
                    variable_id=variable["id"],
                )
        for item in after["items"]:
            if item.get("controller_id") != controller_id:
                continue
            _, base_matches = _identifier_tokens(
                item.get("base_type"), expected_name, "candidate DataType BaseType"
            )
            initializer_tokens, initializer_matches = _identifier_tokens(
                item.get("initial_value"), expected_name, "candidate DataType initializer"
            )
            if base_matches or any(
                position + 1 < len(initializer_tokens)
                and initializer_tokens[position + 1].text == "#"
                for position in initializer_matches
            ):
                raise _RenameBlocked(
                    "data_type_reference_readback_failed",
                    "An observed DataType reference still uses the old name.",
                    data_type_id=item["id"],
                )
        for field in after_index.fields:
            if field.get("pou_id") not in pou_ids:
                continue
            field_tokens, literal_matches = _identifier_tokens(
                field.get("source"), expected_name, "candidate controller source"
            )
            if any(
                position + 1 < len(field_tokens)
                and field_tokens[position + 1].text == "#"
                for position in literal_matches
            ):
                raise _RenameBlocked(
                    "data_type_reference_readback_failed",
                    "An observed source reference still uses the old DataType name.",
                    section_id=field["section_id"],
                )

        plan.update(
            can_apply=True,
            references=references,
            changed_members=sorted(patches),
            coverage={
                **plan["coverage"],
                "controller_variable_tables": "observed",
                "controller_source_sections": "observed",
                "reference_scan": "complete_for_observed_controller_declarations_source_and_data_types",
            },
        )
        return plan, patches
    except _RenameBlocked as exc:
        plan["blockers"].append(exc.blocker)
        plan["references"] = []
        plan["changed_members"] = []
        plan["coverage"]["reference_scan"] = "blocked"
        return plan, {}


def prepare_data_type_deletion(project, data_type_id, expected_name, expected_base_type):
    if not isinstance(expected_name, str) or not expected_name.strip():
        raise ValueError("expected_name must match the listed DataType name.")
    if not isinstance(expected_base_type, str) or not expected_base_type.strip():
        raise ValueError("expected_base_type must match the listed DataType BaseType.")

    if not isinstance(data_type_id, str) or ":" not in data_type_id:
        raise ValueError("DataType ID must come from plc_list_data_types.")
    tables_by_id = {
        entity.get("id"): entity
        for entity in project.entities.values()
        if entity.get("type") == "DataType"
    }
    table_ids = [table_id for table_id in tables_by_id if data_type_id.startswith(f"{table_id}:")]
    if not table_ids:
        raise ValueError("DataType table identity is not present in this project revision.")
    table_id = max(table_ids, key=len)
    table_entity = tables_by_id.get(table_id)
    table_controller = controller(table_entity)
    if table_controller is None:
        raise ValueError("DataType table must belong to a known controller.")

    inventory, tables = _type_tables(project, table_controller)
    table = next((item for item in tables if item["id"] == table_id), None)
    if table is None or table.get("library_id") is not None:
        raise ValueError("Only controller-owned DataType tables can be edited.")
    matches = [item for item in inventory["items"] if item["id"] == data_type_id]
    if len(matches) != 1:
        raise ValueError("DataType ID is not uniquely resolvable; list DataTypes again.")
    record = matches[0]
    if record.get("name") != expected_name or record.get("base_type") != expected_base_type:
        raise ValueError("DataType identity changed; list DataTypes again.")
    if record.get("parent_id") is not None or record.get("depth") != 0:
        raise ValueError("Only top-level DataTypes can be deleted.")
    if record.get("child_count") != 0:
        raise ValueError("Only top-level leaf DataTypes without members can be deleted.")
    if not record.get("name") or not record.get("base_type"):
        raise ValueError("DataType is missing a name or BaseType and cannot be safely deleted.")

    index, controller_pou_ids = _controller_pous(project, table_controller)
    source_diagnostics = [
        diagnostic
        for diagnostic in index.diagnostics
        if diagnostic.get("pou_id") in controller_pou_ids
        and diagnostic.get("code") not in {"external_type_mismatch", "external_target_unresolved"}
    ]
    if source_diagnostics:
        raise ValueError("Controller source contains unsupported constructs; DataType deletion is unsafe.")

    raw = project.archive.read(table["member"])
    root = _table_root(project, table)
    parent = _definition_parent(root, table)
    path = data_type_id[len(table_id) + 1 :]
    if "." in path or not path.isdigit():
        raise ValueError("Only top-level DataType paths can be deleted.")
    position = int(path)
    definitions = list(parent)
    if position >= len(definitions):
        raise ValueError("DataType table changed; list DataTypes again.")
    target_node = definitions[position]
    if (
        xml_local_name(target_node) != "DataType"
        or target_node.get("Name") != expected_name
        or target_node.get("BaseType") != expected_base_type
        or list(target_node)
    ):
        raise ValueError("DataType identity changed; list DataTypes again.")

    all_names = [
        item.get("name", "").casefold()
        for item in inventory["items"]
        if item.get("name")
    ]
    if all_names.count(expected_name.casefold()) != 1:
        raise ValueError("DataType name is ambiguous in the controller.")

    _references(
        project,
        index,
        tables,
        controller_pou_ids,
        table_controller,
        expected_name,
        table_id,
        target_node,
    )
    parent.remove(target_node)
    output = serialize_xml(root, raw)
    candidate = NativeProject(project.archive.path, {**project.archive.overrides, table["member"]: output})
    after = candidate.data_types(controller_id=table_controller)
    if after["completeness"] != "observed" or any(
        item.get("table_id") == table_id and item.get("name") == expected_name
        for item in after["items"]
    ):
        raise ValueError("Staged DataType deletion failed structural read-back.")

    return {table["member"]: output}, {
        "operation": "delete_data_type",
        "data_type_id": data_type_id,
        "name": expected_name,
        "base_type": expected_base_type,
        "table_id": table_id,
        "controller_id": table_controller,
        "external_consumers": "not_acquired",
        "sysmac_build": "not_executed",
    }
