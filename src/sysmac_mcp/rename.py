"""Concrete rename plans, native patches and rebinding checks before staging."""

from __future__ import annotations

import copy
import json
import re
from collections import defaultdict

from lxml import etree as ET

from .project import NativeProject, parse_xml
from .st_lexer import KEYWORDS, lex
from .symbols import SymbolIndex, controller


def prepare_rename(index, variable_id, new_name):
    analysis = index.analyze(variable_id)
    target = analysis["declaration"]
    declarations, references = analysis["declarations"], analysis["references"]
    family = {v["id"] for v in declarations}
    blockers = []
    if any(v["attributes"].get("RO") == "1" for v in declarations):
        blockers.append({"code": "read_only_declaration"})
    plan = {
        "variable_id": variable_id,
        "old_name": target["name"],
        "new_name": new_name,
        "can_apply": False,
        "blockers": blockers,
        "declarations": declarations,
        "references": references,
        "coverage": analysis["coverage"],
        "changed_members": [],
        "new_variable_id": None,
    }
    if (
        not isinstance(new_name, str)
        or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,126}", new_name)
        or new_name.upper() in KEYWORDS
    ):
        blockers.append({"code": "invalid_or_reserved_name"})
        return plan, {}
    if target["group"] == "VAR_EXTERNAL":
        blockers.append(
            {
                "code": "select_global_declaration",
                "canonical_variable_id": analysis["canonical_variable_id"],
            }
        )
    if target["group"] not in {
        "VAR_GLOBAL",
        "VAR",
        "VAR_INPUT",
        "VAR_OUTPUT",
        "VAR_IN_OUT",
        "VAR_TEMP",
    }:
        blockers.append({"code": "unsupported_declaration_group"})
    owner = index.project.entities.get(target["scope"])
    broad = target["scope"] == "global" or owner is not None and owner.get("type") != "Program"
    affected = (
        {
            p["id"]
            for p in index.pous
            if controller(index.project.entities[p["id"]]) == target["controller_id"]
        }
        if broad
        else {target["scope"]}
    )
    affected.update(r["pou_id"] for r in references)
    for v in index.variables:
        if (
            v["id"] not in family
            and v["controller_id"] == target["controller_id"]
            and v["scope"] == target["scope"]
            and v["name"].casefold() == new_name.casefold()
        ):
            blockers.append({"code": "declaration_collision", "variable_id": v["id"]})
        # Initializers and array dimensions are declarations, not source bodies.
        # Until their full expression contexts are modeled, refuse hidden references.
        if v["controller_id"] == target["controller_id"] and (
            broad or v["scope"] == target["scope"]
        ):
            expressions = [v["initial_value"] or "", *re.findall(r"\[([^]]+)\]", v["data_type"])]
            if any(
                t.kind == "identifier"
                and t.text.casefold() in {target["name"].casefold(), new_name.casefold()}
                for expression in expressions
                for t in lex(expression).tokens
            ):
                blockers.append(
                    {"code": "declaration_expression_not_supported", "variable_id": v["id"]}
                )
    if (target["controller_id"], new_name.casefold()) in index.pou_by_name:
        blockers.append({"code": "pou_name_collision"})
    for section in index.sections:
        if section["pou_id"] in affected and section["completeness"] != "observed":
            blockers.append(
                {
                    "code": "source_unavailable",
                    "section_id": section["id"],
                    "reason": section["completeness"],
                }
            )
    for diagnostic in index.diagnostics:
        if diagnostic.get("pou_id") in affected or family.intersection(
            diagnostic.get("variable_ids", [])
        ):
            blockers.append(diagnostic)
    for occurrence in index.occurrences:
        if (
            occurrence["pou_id"] in affected
            and occurrence["spelling"].casefold() == target["name"].casefold()
            and occurrence["kind"]
            in {"unresolved", "ambiguous", "unresolved_member", "unresolved_parameter"}
            and (
                occurrence["role"] == "variable"
                or broad
                and target["scope"] != "global"
                or occurrence["role"] == "member"
                and occurrence.get("base_kind") not in {"variable", "type_member"}
            )
        ):
            blockers.append(
                {
                    "code": "unresolved_possible_reference",
                    "section_id": occurrence["section_id"],
                    "path": occurrence["path"],
                    "start": occurrence["start"],
                }
            )
    if blockers:
        return plan, {}
    patches = _patches(index, declarations, references, new_name)
    candidate = NativeProject(
        index.project.archive.path, {**index.project.archive.overrides, **patches}
    )
    rebound = SymbolIndex(candidate)
    renamed_ids = {}
    for old in declarations:
        matches = [
            v
            for v in rebound.variables
            if v["table_id"] == old["table_id"]
            and v["group"] == old["group"]
            and v["name"] == new_name
        ]
        if len(matches) != 1:
            blockers.append({"code": "declaration_rebind_failed", "variable_id": old["id"]})
        else:
            renamed_ids[old["id"]] = matches[0]["id"]
    before = _bindings(index)
    after = _bindings(rebound)
    for key in before.keys() | after.keys():
        expected = renamed_ids.get(before.get(key), before.get(key))
        if after.get(key) != expected:
            blockers.append({"code": "reference_capture", "section_id": key[1], "path": key[3]})
            break
    for alias, canonical in index.aliases.items():
        if rebound.aliases.get(renamed_ids.get(alias, alias)) != renamed_ids.get(
            canonical, canonical
        ):
            blockers.append({"code": "external_alias_rebind_failed", "variable_id": alias})
    for alias, canonical in rebound.aliases.items():
        if alias not in renamed_ids.values() and index.aliases.get(alias) != canonical:
            blockers.append({"code": "external_alias_capture", "variable_id": alias})
    if blockers:
        return plan, {}
    plan.update(
        can_apply=True, changed_members=sorted(patches), new_variable_id=renamed_ids[variable_id]
    )
    return plan, patches


def _bindings(index):
    counts = defaultdict(int)
    result = {}
    for occurrence in index.occurrences:
        field = (
            occurrence["pou_id"],
            occurrence["section_id"],
            occurrence.get("rung"),
            occurrence["path"],
        )
        key = (*field, counts[field])
        counts[field] += 1
        result[key] = occurrence.get("variable_id")
    return result


def _patches(index, declarations, references, new_name):
    patches, tables = {}, defaultdict(list)
    for declaration in declarations:
        tables[declaration["member"]].append(declaration)
    for member, variables in tables.items():
        raw = index.project.archive.read(member)
        lines = raw.decode("utf-8-sig").splitlines(keepends=True)
        for variable in variables:
            line = variable["line_index"]
            lines[line], count = re.subn(
                r"(?:(?<=\t)|(?<=^\+\+))N=[^\t\r\n]*", "N=" + new_name, lines[line], count=1
            )
            if count != 1:
                raise ValueError("Declaration name field is not uniquely located.")
        patches[member] = (b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b"") + "".join(
            lines
        ).encode("utf-8")
    edits = defaultdict(list)
    for reference in references:
        edits[(reference["section_id"], reference.get("rung"), reference["path"])].append(reference)
    sections = {s["id"]: s for s in index.sections}
    fields = {(f["section_id"], f.get("rung"), f["path"]): f for f in index.fields}
    ladders = {}
    for key, occurrences in edits.items():
        field = fields[key]
        source = field["source"]
        for ref in sorted(occurrences, key=lambda r: r["start"], reverse=True):
            if source[ref["start"] : ref["end"]] != ref["spelling"]:
                raise ValueError("Source span changed while preparing rename.")
            source = source[: ref["start"]] + new_name + source[ref["end"] :]
        section = sections[key[0]]
        member = section["member"]
        if section["language"] == "ST":
            raw = index.project.archive.read(member)
            root = parse_xml(raw)
            root.xpath('//*[local-name()="Text"]')[0].text = source
            declaration = raw.lstrip(b"\xef\xbb\xbf \r\n").startswith(b"<?xml")
            patches[member] = (
                b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b""
            ) + ET.tostring(root, encoding="utf-8", xml_declaration=declaration)
        else:
            if member not in ladders:
                ladders[member] = copy.deepcopy(section["rungs"])
            node = ladders[member][key[1]]
            parts = key[2].split("/")[1:]
            for part in parts[:-1]:
                node = node[int(part)] if isinstance(node, list) else node[part]
            node[parts[-1]] = source
    for member, rungs in ladders.items():
        raw = index.project.archive.read(member)
        text = raw.decode("utf-8-sig")
        ending = "\r\n" if "\r\n" in text else "\n"
        content = ending.join(
            json.dumps(r, ensure_ascii=False, separators=(",", ":"), allow_nan=False) for r in rungs
        )
        if text.endswith(("\r", "\n")):
            content += ending
        patches[member] = (
            b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b""
        ) + content.encode("utf-8")
    return patches
