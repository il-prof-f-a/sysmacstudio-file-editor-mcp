"""Declaration identities and conservative ST/LD binding for native project copies."""

from __future__ import annotations

import re
from collections import defaultdict

from lxml import etree as ET

from .project import parse_xml
from .st_lexer import lex


def type_key(value):
    return re.sub(r"\s+", "", value).casefold()


def controller(entity):
    while entity is not None:
        if entity.tag == "Entity" and entity.get("type") == "Device":
            return entity.get("id")
        entity = entity.getparent()
    return None


class SymbolIndex:
    def __init__(self, project):
        self.project = project
        self.variables = project.variables()
        self.by_id = {v["id"]: v for v in self.variables}
        self.scopes = defaultdict(lambda: defaultdict(list))
        self.globals = defaultdict(lambda: defaultdict(list))
        self.aliases, self.diagnostics = {}, []
        self.pous = project.pous()
        self.pou_by_name = defaultdict(list)
        self.types = defaultdict(list)
        for pou in self.pous:
            self.pou_by_name[
                (controller(project.entities[pou["id"]]), pou["name"].casefold())
            ].append(pou)
        for v in self.variables:
            v["controller_id"] = controller(project.entities[v["table_id"]])
            mapping = (
                self.globals[v["controller_id"]]
                if v["scope"] == "global"
                else self.scopes[v["scope"]]
            )
            mapping[v["name"].casefold()].append(v)
        for v in self.variables:
            if v["group"] == "VAR_EXTERNAL":
                candidates = self.globals[v["controller_id"]][v["name"].casefold()]
                if len(candidates) == 1 and type_key(candidates[0]["data_type"]) == type_key(
                    v["data_type"]
                ):
                    self.aliases[v["id"]] = candidates[0]["id"]
                else:
                    self.diagnostics.append(
                        {
                            "code": "external_type_mismatch"
                            if len(candidates) == 1
                            else "external_target_unresolved",
                            "variable_ids": [v["id"], *(c["id"] for c in candidates)],
                        }
                    )
        for e in project.entities.values():
            if e.get("type") != "DataType" or not project.member(e.get("id")):
                continue
            try:
                root = parse_xml(project.archive.read(project.member(e.get("id"))))
            except (ValueError, ET.XMLSyntaxError):
                continue
            for node in root.findall(".//DataType"):
                if node.get("BaseType") and node.getparent().get("BaseType", "") == "":
                    self.types[(controller(e), node.get("Name", "").casefold())].append(node)
        self.sections, self.fields, self.references, self.occurrences = [], [], [], []
        for pou in self.pous:
            for section in project.read_pou(pou["id"])["sections"]:
                section = {**section, "pou_id": pou["id"]}
                self.sections.append(section)
                base = {
                    "pou_id": pou["id"],
                    "section_id": section["id"],
                    "member": section["member"],
                    "language": section["language"],
                }
                if section["language"] == "ST":
                    self._source({**base, "source": section["source"], "path": None})
                elif section["language"] == "LD":
                    for n, rung in enumerate(section["rungs"]):
                        self._ladder(rung, {**base, "rung": n}, "")

    def _lookup(self, pou_id, name):
        values = self.scopes[pou_id].get(name.casefold())
        if values:
            return self._variable(values)
        cid = controller(self.project.entities[pou_id])
        values = self.globals[cid].get(name.casefold(), [])
        if values:
            return self._variable(values)
        pous = self.pou_by_name.get((cid, name.casefold()), [])
        return (
            {"callable_pou_id": pous[0]["id"], "kind": "pou"}
            if len(pous) == 1
            else {"kind": "unresolved"}
        )

    def _variable(self, candidates):
        if len(candidates) != 1:
            return {"kind": "ambiguous", "candidate_ids": [v["id"] for v in candidates]}
        v = candidates[0]
        canonical = self.aliases.get(v["id"], v["id"] if v["group"] != "VAR_EXTERNAL" else None)
        return {
            "kind": "variable",
            "variable_id": canonical,
            "declaration_id": v["id"],
            "data_type": v["data_type"],
            "controller_id": v["controller_id"],
        }

    def _pou_type(self, value):
        if value.get("callable_pou_id"):
            return value["callable_pou_id"]
        values = self.pou_by_name.get(
            (value.get("controller_id"), value.get("data_type", "").casefold()), []
        )
        return values[0]["id"] if len(values) == 1 else None

    def _member(self, value, name, *, parameter=False):
        pou_id = self._pou_type(value)
        if pou_id:
            candidates = self.scopes[pou_id].get(name.casefold(), [])
            if parameter:
                candidates = [
                    v for v in candidates if v["group"] in {"VAR_INPUT", "VAR_OUTPUT", "VAR_IN_OUT"}
                ]
            return (
                self._variable(candidates)
                if candidates
                else {"kind": "unresolved_parameter" if parameter else "unresolved_member"}
            )
        # Struct members have types but are distinct from PLC variable declarations.
        definitions = self.types.get(
            (value.get("controller_id"), value.get("data_type", "").casefold()), []
        )
        if len(definitions) == 1:
            matches = [
                n
                for n in definitions[0].findall("DataType")
                if n.get("Name", "").casefold() == name.casefold()
            ]
            if len(matches) == 1:
                return {
                    "kind": "type_member",
                    "data_type": matches[0].get("BaseType", ""),
                    "controller_id": value.get("controller_id"),
                }
        return {"kind": "unresolved_member", "base_kind": value.get("kind")}

    def _source(self, field):
        self.fields.append(field)
        result = lex(field["source"])
        diagnostic_cursor, diagnostic_line, diagnostic_column = 0, 1, 1
        for issue in result.diagnostics:
            start = issue.get("start")
            location = {}
            if type(start) is int and diagnostic_cursor <= start <= len(field["source"]):
                segment = field["source"][diagnostic_cursor:start]
                newlines = segment.count("\n")
                if newlines:
                    diagnostic_line += newlines
                    diagnostic_column = len(segment) - segment.rfind("\n")
                else:
                    diagnostic_column += len(segment)
                diagnostic_cursor = start
                location = {"line": diagnostic_line, "column": diagnostic_column}
            self.diagnostics.append(
                {
                    **issue,
                    **location,
                    "pou_id": field["pou_id"],
                    "section_id": field["section_id"],
                    "path": field["path"],
                }
            )
        tokens, resolved, stack, brackets = result.tokens, {}, [], []
        delimiters = []
        for i, token in enumerate(tokens):
            if token.text in {"(", "["}:
                delimiters.append(token.text)
            elif token.text in {")", "]"}:
                expected = "(" if token.text == ")" else "["
                if not delimiters or delimiters[-1] != expected:
                    self.diagnostics.append(
                        {
                            "code": "unbalanced_delimiter",
                            "pou_id": field["pou_id"],
                            "section_id": field["section_id"],
                        }
                    )
                else:
                    delimiters.pop()
            previous = tokens[i - 1].text if i else None
            following = tokens[i + 1].text if i + 1 < len(tokens) else None
            if token.text == "(":
                stack.append(
                    resolved.get(i - 1, {}) if i and tokens[i - 1].kind == "identifier" else None
                )
            elif token.text == ")":
                if stack:
                    stack.pop()
                else:
                    self.diagnostics.append(
                        {
                            "code": "unbalanced_delimiter",
                            "pou_id": field["pou_id"],
                            "section_id": field["section_id"],
                        }
                    )
            elif token.text == "[":
                brackets.append(resolved.get(i - 1, {}))
            elif token.text == "]" and brackets:
                value = dict(brackets.pop())
                element = re.fullmatch(
                    r"ARRAY\s*\[.*\]\s+OF\s+(.+)", value.get("data_type", ""), re.I
                )
                if element:
                    value["data_type"] = element[1].strip()
                resolved[i] = value
            if token.kind != "identifier":
                continue
            role = "variable"
            if following == "#" or previous == "#":
                value, role = {"kind": "enum_or_type"}, "type_or_enum"
            elif previous in {".", "::"}:
                value, role = self._member(resolved.get(i - 2, {}), token.text), "member"
            elif stack and previous in {"(", ","} and following in {":=", "=>"}:
                value, role = (
                    self._member(stack[-1] or {}, token.text, parameter=True),
                    "named_argument",
                )
            else:
                value = self._lookup(field["pou_id"], token.text)
            resolved[i] = value
            occurrence = {
                k: field[k] for k in ("pou_id", "section_id", "language", "member", "path")
            }
            if "rung" in field:
                occurrence["rung"] = field["rung"]
            occurrence.update(value)
            occurrence.update(
                spelling=token.text,
                start=token.start,
                end=token.end,
                line=field["source"].count("\n", 0, token.start) + 1,
                column=token.start - field["source"].rfind("\n", 0, token.start),
                role=role,
                access="write"
                if following == ":=" and role != "named_argument"
                else "read_or_argument",
            )
            self.occurrences.append(occurrence)
            if occurrence.get("variable_id"):
                self.references.append(occurrence)
        if stack or brackets or delimiters:
            self.diagnostics.append(
                {
                    "code": "unbalanced_delimiter",
                    "pou_id": field["pou_id"],
                    "section_id": field["section_id"],
                }
            )

    def _ladder(self, value, base, path, callable_value=None):
        if path == "":
            # Rung extension/graphics fields are opaque, even when named Var/TXT.
            self._ladder(value["CLs"], base, "/CLs")
            return
        if isinstance(value, list):
            for n, child in enumerate(value):
                self._ladder(child, base, path + "/" + str(n), callable_value)
            return
        if not isinstance(value, dict):
            self.diagnostics.append(
                {"code": "unsupported_ladder_instruction", **base, "path": path}
            )
            return
        kind = value.get("__type")
        if kind in {"COMMENT", "CMT", "HL"}:
            return
        if kind not in {"LD", "ST", "FB", "F", "FUN", "PRM", "PF", "IST"}:
            self.diagnostics.append(
                {"code": "unsupported_ladder_instruction", **base, "path": path}
            )
            return
        if kind in {"FB", "F", "FUN"}:
            callable_value = self._lookup(base["pou_id"], value.get("Var") or value.get("Name", ""))
        for key in ["Var", *(["TXT"] if kind == "IST" else [])]:
            if isinstance(value.get(key), str):
                self._source({**base, "source": value[key], "path": path + "/" + key})
        if kind in {"PRM", "PF"} and isinstance(value.get("Arg"), str) and callable_value:
            child, location = value["Arg"], path + "/Arg"
            bound = self._member(callable_value, child, parameter=True)
            occurrence = {
                **base,
                **bound,
                "path": location,
                "spelling": child,
                "start": 0,
                "end": len(child),
                "role": "named_argument",
                "access": "parameter",
            }
            self.fields.append({**base, "source": child, "path": location})
            self.occurrences.append(occurrence)
            if bound.get("variable_id"):
                self.references.append(occurrence)
        if kind in {"FB", "F", "FUN"}:
            for key in ("In", "Out"):
                if key in value:
                    self._ladder(value[key], base, path + "/" + key, callable_value)

    def analyze(self, variable_id):
        if variable_id not in self.by_id:
            raise ValueError("Variable ID not found; list variables again.")
        canonical = self.aliases.get(variable_id, variable_id)
        declarations = [
            v for v in self.variables if self.aliases.get(v["id"], v["id"]) == canonical
        ]
        return {
            "declaration": self.by_id[variable_id],
            "canonical_variable_id": canonical,
            "declarations": declarations,
            "references": [r for r in self.references if r["variable_id"] == canonical],
            "diagnostics": self.diagnostics,
            "coverage": {
                "complete": False,
                "binding": "observed_native_declarations_and_code",
                "unavailable_sections": [
                    {"pou_id": s["pou_id"], "section_id": s["id"], "reason": s["completeness"]}
                    for s in self.sections
                    if s["completeness"] != "observed"
                ],
                "external_consumers": "not_acquired",
                "type_check": "declaration_alias_checks_only",
                "sysmac_build": "not_executed",
            },
        }

    def plan_rename(self, variable_id, new_name):
        from .rename import prepare_rename

        return prepare_rename(self, variable_id, new_name)[0]
