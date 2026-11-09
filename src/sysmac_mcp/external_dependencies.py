"""Register native VAR_EXTERNAL links for known global dependencies."""

from __future__ import annotations

import re
from collections import Counter

from .project import NativeProject
from .symbols import SymbolIndex, controller, type_key


def missing_externals(project, *, pou_id=None, index=None):
    index = index if index is not None else SymbolIndex(project)
    missing = {}
    for ref in index.references:
        declaration = index.by_id.get(ref.get("declaration_id"))
        if (
            declaration is None
            or declaration["scope"] != "global"
            or ref["role"] != "variable"
            or pou_id is not None
            and ref["pou_id"] != pou_id
        ):
            continue
        key = (ref["pou_id"], declaration["id"])
        if key not in missing:
            missing[key] = {
                "pou_id": ref["pou_id"],
                "global_variable_id": declaration["id"],
                "name": declaration["name"],
                "data_type": declaration["data_type"],
                "section_id": ref["section_id"],
                "language": ref["language"],
            }
    return list(missing.values())


def external_diagnostics(index):
    """Expose aliases whose canonical global is unknown or incompatible."""
    diagnostics = [
        {**issue, "pou_id": index.by_id[issue["variable_ids"][0]]["scope"]}
        for issue in index.diagnostics
        if issue["code"] in {"external_type_mismatch", "external_target_unresolved"}
    ]
    for pou_id, names in index.scopes.items():
        for declarations in names.values():
            if len(declarations) > 1 and any(v["group"] == "VAR_EXTERNAL" for v in declarations):
                diagnostics.append(
                    {
                        "code": "external_declaration_ambiguous",
                        "pou_id": pou_id,
                        "variable_ids": [v["id"] for v in declarations],
                    }
                )
    return diagnostics


def register_externals(project, pou_id, global_ids):
    pou = project.resolve(pou_id, {"Program", "Function", "FunctionBlock"})
    tables = pou.findall('./ChildEntities/Entity[@type="Variables"]')
    if len(tables) != 1 or not project.member(tables[0].get("id")):
        raise ValueError("POU needs one readable variable table to register external dependencies.")
    table = tables[0]
    member = project.member(table.get("id"))
    raw = project.archive.read(member)
    text = raw.decode("utf-8-sig")
    if not text.startswith("[SLWD version=1.0]"):
        raise ValueError("Only observed SLWD variable tables support external registration.")
    cid = controller(pou)
    globals_ = project.variables("global")
    locals_ = project.variables(pou.get("id"))
    selected, details = [], []
    for global_id in dict.fromkeys(global_ids):
        matches = [v for v in globals_ if v["id"] == global_id]
        if not matches:
            raise ValueError("Select an existing global variable ID.")
        if len(matches) != 1:
            raise ValueError("Global declaration ID is ambiguous.")
        variable = matches[0]
        if cid is None or controller(project.entities[variable["table_id"]]) != cid:
            raise ValueError("POU and global declaration must belong to the same controller.")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,126}", variable["name"]):
            raise ValueError("Only flat IEC declaration names support automatic registration.")
        if any(c in variable["data_type"] for c in "\r\n\t\x00"):
            raise ValueError("Global type contains native declaration delimiters.")
        same_name = [
            v
            for v in globals_
            if v["name"].casefold() == variable["name"].casefold()
            and controller(project.entities[v["table_id"]]) == cid
        ]
        if len(same_name) != 1:
            raise ValueError("Global name is ambiguous in this controller.")
        existing = [v for v in locals_ if v["name"].casefold() == variable["name"].casefold()]
        if existing and (
            len(existing) != 1
            or existing[0]["group"] != "VAR_EXTERNAL"
            or type_key(existing[0]["data_type"]) != type_key(variable["data_type"])
        ):
            raise ValueError("POU already declares this name with a different scope or type.")
        details.append(
            {
                "pou_id": pou.get("id"),
                "global_variable_id": global_id,
                "name": variable["name"],
                "data_type": variable["data_type"],
                "table_id": table.get("id"),
                "already_registered": bool(existing),
            }
        )
        if not existing:
            selected.append(variable)
    if selected:
        lines = text.splitlines()
        headings = [i for i, line in enumerate(lines) if line.split("\t")[0] == "+GN=VAR_EXTERNAL"]
        if len(headings) > 1:
            raise ValueError("External declaration group is ambiguous.")
        if not headings:
            lines.append("+GN=VAR_EXTERNAL\tGA=External\tGVT=ExternalGroup")
            headings = [len(lines) - 1]
        end = next(
            (
                i
                for i in range(headings[0] + 1, len(lines))
                if lines[i].startswith("+") and not lines[i].startswith("++")
            ),
            len(lines),
        )
        rows = [f"++D={v['data_type']}\tN={v['name']}\tG=VAR_EXTERNAL" for v in selected]
        lines[end:end] = rows
        ending = "\r\n" if "\r\n" in text else "\n"
        bom = b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b""
        raw = bom + (ending.join(lines) + ending).encode("utf-8")
    candidate = NativeProject(project.archive.path, {**project.archive.overrides, member: raw})
    index = SymbolIndex(candidate)
    for detail in details:
        aliases = [
            v
            for v in candidate.variables(pou_id)
            if v["name"].casefold() == detail["name"].casefold()
        ]
        if len(aliases) != 1 or index.aliases.get(aliases[0]["id"]) != detail["global_variable_id"]:
            raise ValueError("Registered external alias could not bind to the selected global.")
    return {member: raw}, details


def _invalid_alias_uses(index, pou_id):
    uses = Counter()
    for ref in index.occurrences:
        if ref["pou_id"] != pou_id or ref["role"] != "variable" or ref.get("variable_id"):
            continue
        identities = (
            [ref["declaration_id"]] if ref.get("declaration_id") else ref.get("candidate_ids", [])
        )
        if any(index.by_id[i]["group"] == "VAR_EXTERNAL" for i in identities):
            uses[tuple(sorted(set(identities)))] += 1
    return uses


def register_new_dependencies(project, updates, pou_id):
    """Register only dependencies introduced by this edit; expose legacy gaps separately."""
    before_index = SymbolIndex(project)
    before = {
        (d["pou_id"], d["global_variable_id"])
        for d in missing_externals(project, pou_id=pou_id, index=before_index)
    }
    candidate = NativeProject(project.archive.path, {**project.archive.overrides, **updates})
    candidate_index = SymbolIndex(candidate)
    if _invalid_alias_uses(candidate_index, pou_id) - _invalid_alias_uses(before_index, pou_id):
        raise ValueError(
            "New source references an external alias with an unresolved or incompatible global."
        )
    introduced = [
        d
        for d in missing_externals(candidate, pou_id=pou_id, index=candidate_index)
        if (d["pou_id"], d["global_variable_id"]) not in before
    ]
    if not introduced:
        return updates, []
    additions, details = register_externals(
        candidate, pou_id, [d["global_variable_id"] for d in introduced]
    )
    return {**updates, **additions}, details
