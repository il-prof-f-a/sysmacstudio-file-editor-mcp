"""IEC 61131-10 generator verified against the locally installed Omron schema.

Schema conformance is independent of successful import or build in Sysmac Studio.
"""

from __future__ import annotations

import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from lxml import etree as ET
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .project import parse_xml

IEC = "www.iec.ch/public/TC65SC65BWG7TF10"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
Identifier = r"^[A-Za-z_][A-Za-z0-9_]{0,126}$"
IdentifierPath = r"^[A-Za-z_][A-Za-z0-9_]{0,126}(?:\\[A-Za-z_][A-Za-z0-9_]{0,126})*$"


class VariableSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=Identifier)
    data_type: str = Field(min_length=1, max_length=1024)
    scope: Literal["local", "input", "output", "inout", "external", "temp", "global"] = "local"
    initial_value: str | None = None
    retained: bool = False
    constant: bool = False
    comment: str | None = None


class DataTypeMemberSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=Identifier)
    data_type: str = Field(min_length=1, max_length=1024)
    comment: str | None = None


class EnumValueSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=Identifier)
    value: int
    comment: str | None = None


class DataTypeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=IdentifierPath)
    kind: Literal["STRUCT", "UNION", "ENUM", "ARRAY"]
    # ARRAY aliases are expanded inline in Sysmac variables/members; ENUM stores its integer base type.
    base_type: str | None = Field(default=None, max_length=1024)
    members: list[DataTypeMemberSpec] = Field(default_factory=list, max_length=10000)
    values: list[EnumValueSpec] = Field(default_factory=list, max_length=10000)
    comment: str | None = None

    @model_validator(mode="after")
    def valid_shape(self):
        if self.kind in {"STRUCT", "UNION"} and (not self.members or self.values or self.base_type):
            raise ValueError("Structure and union types require members only.")
        if self.kind == "ENUM" and (
            not self.values or self.members or self.base_type is None
        ):
            raise ValueError("Enumerations require a base type and named values only.")
        if self.kind == "ARRAY" and (
            self.base_type is None or self.members or self.values
        ):
            raise ValueError("Array types require one native array base type only.")
        if self.kind in {"STRUCT", "UNION"}:
            unique_names(self.members, "data type member")
        if self.kind == "ENUM":
            unique_names(self.values, "enumerator")
            if len({item.value for item in self.values}) != len(self.values):
                raise ValueError("duplicate enumeration values")
        return self


class LadderNode(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=127)
    kind: Literal["left_rail", "right_rail", "contact_no", "contact_nc", "coil", "coil_negated"]
    variable: str | None = Field(default=None, max_length=4096)


class LadderNetwork(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nodes: list[LadderNode] = Field(min_length=3, max_length=1000)
    connections: list[tuple[str, str]] = Field(min_length=2, max_length=4000)

    @model_validator(mode="after")
    def valid_graph(self):
        nodes = {n.id: n for n in self.nodes}
        if len(nodes) != len(self.nodes):
            raise ValueError("duplicate node identity")
        if len(set(self.connections)) != len(self.connections):
            raise ValueError("duplicate connection")
        incoming = {key: [] for key in nodes}
        outgoing = {key: [] for key in nodes}
        for a, b in self.connections:
            if a not in nodes or b not in nodes:
                raise ValueError("connection references a missing node")
            incoming[b].append(a)
            outgoing[a].append(b)
        if len(self.topological_order()) != len(nodes):
            raise ValueError("Ladder network contains a cycle")
        left = [n.id for n in self.nodes if n.kind == "left_rail"]
        right = [n.id for n in self.nodes if n.kind == "right_rail"]
        if len(left) != 1 or len(right) != 1:
            raise ValueError("exactly one left and right rail required")
        for key, node in nodes.items():
            if node.kind == "left_rail":
                if incoming[key] or not outgoing[key]:
                    raise ValueError("invalid left rail connections")
            elif node.kind == "right_rail":
                if outgoing[key] or not incoming[key]:
                    raise ValueError("invalid right rail connections")
            elif not incoming[key] or not outgoing[key] or not node.variable:
                raise ValueError("all contacts/coils require operands and connected input/output")
        return self

    def topological_order(self):
        counts = {n.id: 0 for n in self.nodes}
        outgoing = {n.id: [] for n in self.nodes}
        for a, b in self.connections:
            if a in counts and b in counts:
                counts[b] += 1
                outgoing[a].append(b)
        queue = deque(key for key, count in counts.items() if count == 0)
        order = []
        while queue:
            key = queue.popleft()
            order.append(key)
            for child in outgoing[key]:
                counts[child] -= 1
                if counts[child] == 0:
                    queue.append(child)
        return order


class PouSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=Identifier)
    kind: Literal["Program", "Function", "FunctionBlock"] = "Program"
    language: Literal["ST", "LD"] = "ST"
    source: str | None = None
    network: LadderNetwork | None = None
    networks: list[LadderNetwork] | None = Field(default=None, min_length=1, max_length=1000)
    return_type: str | None = None
    variables: list[VariableSpec] = Field(default_factory=list, max_length=10000)

    @model_validator(mode="after")
    def valid_body(self):
        if self.language == "ST" and (
            self.source is None or self.network is not None or self.networks is not None
        ):
            raise ValueError("ST requires source text and no Ladder network")
        if self.language == "LD" and (
            self.source is not None or (self.network is None) == (self.networks is None)
        ):
            raise ValueError("LD requires either one Ladder network or a network list, and no ST source")
        if self.kind == "Function" and not self.return_type:
            raise ValueError("Function requires return_type")
        for variable in self.variables:
            if variable.scope == "global":
                raise ValueError("Project-global variables cannot be POU declarations.")
            if self.kind == "Program" and variable.scope in {"input", "output", "inout", "temp"}:
                raise ValueError("Program declarations must be local or external")
            if self.kind == "FunctionBlock" and variable.scope == "temp":
                raise ValueError("FunctionBlock does not support TempVars")
            if variable.scope == "external" and (
                variable.retained or variable.initial_value is not None
            ):
                raise ValueError("External declarations do not support retention or initialization")
            if variable.scope == "inout" and variable.retained:
                raise ValueError("InoutVars does not support retention")
            if variable.scope in {"input", "output", "inout"} and variable.constant:
                raise ValueError(
                    "Constant parameters require an Omron extension not yet implemented"
                )
        return self


def sub(parent, tag: str, **attrs):
    return ET.SubElement(parent, f"{{{IEC}}}{tag}", **attrs)


def unique_names(items, label):
    names = [item.name.casefold() for item in items]
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate {label} names")


def declaration(parent, variable: VariableSpec, order: int | None = None):
    attrs = {"name": variable.name}
    if order is not None:
        attrs["orderWithinParamSet"] = str(order)
    element = sub(parent, "Variable", **attrs)
    if variable.comment is not None:
        comment = sub(element, "Documentation")
        comment.set(f"{{{XSI}}}type", "SimpleText")
        comment.text = variable.comment
    typ = sub(element, "Type")
    sub(typ, "TypeName").text = variable.data_type
    if variable.initial_value is not None:
        sub(sub(element, "InitialValue"), "SimpleValue", value=variable.initial_value)


def ladder_body(body, network: LadderNetwork, evaluation_order: int):
    rung = sub(body, "Rung", evaluationOrder=str(evaluation_order))
    ids = {node_id: str(i + 1) for i, node_id in enumerate(network.topological_order())}
    nodes = {node.id: node for node in network.nodes}
    kinds = {
        "left_rail": "LeftPowerRail",
        "right_rail": "RightPowerRail",
        "contact_no": "Contact",
        "contact_nc": "Contact",
        "coil": "Coil",
        "coil_negated": "Coil",
    }
    for node_id in network.topological_order():
        node = nodes[node_id]
        obj = sub(rung, "LdObject")
        obj.set(f"{{{XSI}}}type", kinds[node.kind])
        if node.variable:
            obj.set("operand", node.variable)
        if node.kind in {"contact_nc", "coil_negated"}:
            obj.set("negated", "true")
        incoming = [a for a, b in network.connections if b == node_id]
        if incoming:
            point = sub(obj, "ConnectionPointIn")
            for source in incoming:
                sub(point, "Connection", refConnectionPointOutId=ids[source])
        if node.kind != "right_rail":
            sub(obj, "ConnectionPointOut", connectionPointOutId=ids[node_id])


def build_iec_project(
    name: str, pous: list[PouSpec], global_variables: list[VariableSpec]
) -> bytes:
    unique_names(pous, "POU")
    unique_names(global_variables, "global variable")
    root = ET.Element(f"{{{IEC}}}Project", nsmap={None: IEC, "xsi": XSI}, schemaVersion="1")
    sub(
        root,
        "FileHeader",
        companyName="Local project tools",
        productName="sysmacstudio-file-editor-mcp",
        productVersion="0.1.0",
    )
    sub(
        root,
        "ContentHeader",
        name=name,
        creationDateTime=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    namespace = sub(sub(root, "Types"), "GlobalNamespace")
    for pou in pous:
        unique_names(pou.variables, "POU variable")
        element = sub(namespace, pou.kind, name=pou.name)
        if pou.kind == "Function":
            sub(sub(element, "ResultType"), "TypeName").text = pou.return_type
        params = [v for v in pou.variables if v.scope in {"input", "output", "inout"}]
        if params or pou.kind == "Function":
            parameters = sub(element, "Parameters")
            group = None
            previous = None
            tags = {"input": "InputVars", "inout": "InoutVars", "output": "OutputVars"}
            for index, variable in enumerate(params):
                signature = (variable.scope, variable.retained)
                if signature != previous:
                    attributes = {"retain": "true"} if variable.retained else {}
                    group = sub(parameters, tags[variable.scope], **attributes)
                    previous = signature
                declaration(group, variable, index)
        for scope, tag in [("external", "ExternalVars"), ("local", "Vars"), ("temp", "TempVars")]:
            for variable in [v for v in pou.variables if v.scope == scope]:
                attributes = {}
                if scope == "local":
                    attributes["accessSpecifier"] = "private"
                if variable.retained:
                    attributes["retain"] = "true"
                if variable.constant:
                    attributes["constant"] = "true"
                effective_tag = "TempVars" if pou.kind == "Function" and scope == "local" else tag
                if effective_tag == "TempVars":
                    attributes.pop("accessSpecifier", None)
                declaration(sub(element, effective_tag, **attributes), variable)
        content = sub(sub(element, "MainBody"), "BodyContent")
        content.set(f"{{{XSI}}}type", pou.language)
        if pou.language == "ST":
            sub(content, "ST").text = pou.source
        else:
            networks = [pou.network] if pou.network is not None else pou.networks
            for order, network in enumerate(networks, start=1):
                ladder_body(content, network, order)
    instances = sub(root, "Instances")
    if global_variables:
        resource = sub(
            sub(instances, "Configuration", name="Configuration"),
            "Resource",
            name="Resource",
            resourceTypeName="",
        )
        for variable in global_variables:
            attrs = {}
            if variable.retained:
                attrs["retain"] = "true"
            if variable.constant:
                attrs["constant"] = "true"
            declaration(sub(resource, "GlobalVars", **attrs), variable)
    return ET.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def validate_iec_xml(data: bytes, schema_path: str | Path) -> dict:
    parser = ET.XMLParser(resolve_entities=False, no_network=True)
    schema = ET.XMLSchema(ET.parse(str(schema_path), parser))
    root = parse_xml(data)
    schema.assertValid(root)
    return {
        "status": "passed",
        "schema_path": str(Path(schema_path).resolve()),
        "sysmac_import": "not_executed",
        "sysmac_build": "not_executed",
    }


def find_iec_schema(schema_path: str | Path | None = None) -> Path | None:
    if schema_path is not None:
        path = Path(schema_path).expanduser().resolve(strict=True)
        if not path.is_file():
            raise ValueError("IEC schema path is not a file.")
        return path
    candidates = [
        Path(
            r"C:\Program Files\OMRON\Sysmac Studio\Sample\IEC 61131-10 XML\Controller\IEC61131_10_Ed1_0_Spc1_0.xsd"
        ),
        Path(
            r"C:\Program Files (x86)\OMRON\Sysmac Studio\Sample\IEC 61131-10 XML\Controller\IEC61131_10_Ed1_0_Spc1_0.xsd"
        ),
    ]
    return next((path for path in candidates if path.is_file()), None)


def _xml_elements(element):
    return [child for child in element if isinstance(child.tag, str)]


def _iec_tag(element):
    name = ET.QName(element)
    if name.namespace != IEC:
        raise ValueError("PLCopen XML must use the IEC 61131-10 namespace.")
    return name.localname


def _check_children(element, allowed: set[str], context: str):
    for child in _xml_elements(element):
        tag = _iec_tag(child)
        if tag not in allowed:
            raise ValueError(f"Unsupported {context} element: {tag}.")


def _one_child(element, name: str, *, required: bool = False):
    found = [child for child in _xml_elements(element) if _iec_tag(child) == name]
    if len(found) > 1 or required and not found:
        qualifier = "required" if required else "unique"
        raise ValueError(f"Expected one {qualifier} {name} element.")
    return found[0] if found else None


def _children_named(element, name: str):
    return [child for child in _xml_elements(element) if _iec_tag(child) == name]


def _parse_import_variable(element, scope: str, group_attrs: dict[str, str]) -> VariableSpec:
    _check_children(element, {"Documentation", "Type", "InitialValue"}, "Variable")
    if set(element.attrib) - {"name", "orderWithinParamSet"}:
        raise ValueError("Unsupported Variable attributes in PLCopen XML.")
    if "orderWithinParamSet" in element.attrib:
        try:
            int(element.attrib["orderWithinParamSet"])
        except ValueError as exc:
            raise ValueError("Invalid PLCopen parameter order.") from exc
    name = element.get("name")
    if not name:
        raise ValueError("PLCopen variable name is required.")
    type_element = _one_child(element, "Type", required=True)
    if type_element.attrib:
        raise ValueError("Unsupported Type attributes in PLCopen XML.")
    _check_children(type_element, {"TypeName"}, "Type")
    type_name = _one_child(type_element, "TypeName", required=True)
    if type_name.attrib or _xml_elements(type_name) or not (type_name.text or "").strip():
        raise ValueError("Only non-empty PLCopen TypeName declarations are supported.")
    data_type = (type_name.text or "").strip()

    comment = None
    documentation = _one_child(element, "Documentation")
    if documentation is not None:
        if (
            set(documentation.attrib) != {f"{{{XSI}}}type"}
            or documentation.get(f"{{{XSI}}}type") != "SimpleText"
            or _xml_elements(documentation)
        ):
            raise ValueError("Only simple PLCopen documentation text is supported.")
        comment = documentation.text or ""

    initial_value = None
    initial = _one_child(element, "InitialValue")
    if initial is not None:
        if initial.attrib:
            raise ValueError("Unsupported InitialValue attributes in PLCopen XML.")
        _check_children(initial, {"SimpleValue"}, "InitialValue")
        value = _one_child(initial, "SimpleValue", required=True)
        if set(value.attrib) != {"value"} or _xml_elements(value) or value.text:
            raise ValueError("Only simple PLCopen initial values are supported.")
        initial_value = value.get("value")

    if set(group_attrs) - {"retain", "constant", "accessSpecifier"}:
        raise ValueError("Unsupported PLCopen variable group attributes.")
    if "accessSpecifier" in group_attrs and scope != "local":
        raise ValueError("PLCopen accessSpecifier is supported only for local variables.")
    if scope == "local" and group_attrs.get("accessSpecifier", "private") != "private":
        raise ValueError("Only private local PLCopen variables are supported.")
    for flag in ("retain", "constant"):
        if group_attrs.get(flag, "false") not in {"true", "false"}:
            raise ValueError(f"Invalid PLCopen {flag} flag.")
    return VariableSpec(
        name=name,
        data_type=data_type,
        scope=scope,
        initial_value=initial_value,
        retained=group_attrs.get("retain") == "true",
        constant=group_attrs.get("constant") == "true",
        comment=comment,
    )


def _parse_import_variable_group(element, scope: str) -> list[VariableSpec]:
    allowed = {"retain", "constant", "accessSpecifier"}
    if set(element.attrib) - allowed:
        raise ValueError("Unsupported PLCopen variable group attributes.")
    children = _xml_elements(element)
    if any(_iec_tag(child) != "Variable" for child in children):
        raise ValueError("Only Variable entries are supported in PLCopen declarations.")
    return [_parse_import_variable(child, scope, element.attrib) for child in children]


def _parse_import_add_data(element, context: str):
    add_data = _one_child(element, "AddData")
    if add_data is None:
        return
    if add_data.attrib:
        raise ValueError(f"Unsupported {context} AddData attributes.")
    for data in _children_named(add_data, "Data"):
        if set(data.attrib) != {"name", "handleUnknown"} or data.get("handleUnknown") != "discard":
            raise ValueError(f"Unsupported {context} AddData; only discardable vendor data is accepted.")
    if any(_iec_tag(child) != "Data" for child in _xml_elements(add_data)):
        raise ValueError(f"Unsupported {context} AddData element.")


def _parse_import_documentation(element, context: str) -> str | None:
    documentation = _one_child(element, "Documentation")
    if documentation is None:
        return None
    if (
        set(documentation.attrib) != {f"{{{XSI}}}type"}
        or documentation.get(f"{{{XSI}}}type") != "SimpleText"
        or _xml_elements(documentation)
    ):
        raise ValueError(f"Only simple PLCopen {context} documentation is supported.")
    return documentation.text or ""


def _parse_import_type_ref(element) -> str:
    if element.attrib:
        raise ValueError("Unsupported PLCopen Type attributes in data type declarations.")
    children = _xml_elements(element)
    if len(children) != 1:
        raise ValueError("PLCopen data type references require exactly one type specification.")
    child = children[0]
    child_name = _iec_tag(child)
    if child_name == "TypeName":
        if child.attrib or _xml_elements(child) or not (child.text or "").strip():
            raise ValueError("Only non-empty PLCopen TypeName declarations are supported.")
        return (child.text or "").strip()
    if child_name == "InstantlyDefinedType":
        return _parse_import_array_type_spec(child)
    raise ValueError(f"Unsupported PLCopen data type reference: {child_name}.")


def _parse_import_array_type_spec(element) -> str:
    if element.get(f"{{{XSI}}}type") != "ArrayTypeSpec":
        raise ValueError("Only fixed-size PLCopen array type references are supported.")
    if set(element.attrib) != {f"{{{XSI}}}type"}:
        raise ValueError("Unsupported PLCopen array type attributes.")
    _check_children(element, {"BaseType", "DimensionSpec", "AddData"}, "ArrayTypeSpec")
    base_elements = _children_named(element, "BaseType")
    dimensions = _children_named(element, "DimensionSpec")
    if len(base_elements) != 1 or len(dimensions) != 1:
        raise ValueError("Only one-dimensional PLCopen arrays with one base type are supported.")
    _parse_import_add_data(element, "array")
    dimension = dimensions[0]
    if set(dimension.attrib) != {"dimensionNumber"} or dimension.get("dimensionNumber") != "1":
        raise ValueError("Only the first fixed PLCopen array dimension is supported.")
    choices = _xml_elements(dimension)
    if len(choices) != 1 or _iec_tag(choices[0]) != "IndexRange":
        raise ValueError("Variable-length PLCopen arrays cannot be imported as native data types.")
    index_range = choices[0]
    if set(index_range.attrib) != {"lower", "upper"} or _xml_elements(index_range):
        raise ValueError("Unsupported PLCopen array index range.")
    try:
        lower, upper = int(index_range.get("lower")), int(index_range.get("upper"))
    except (TypeError, ValueError) as exc:
        raise ValueError("PLCopen array bounds must be integer constants.") from exc
    if lower > upper:
        raise ValueError("PLCopen array lower bound exceeds its upper bound.")
    base_type = _parse_import_type_ref(base_elements[0])
    if base_type.upper().startswith("ARRAY["):
        raise ValueError("Nested PLCopen array base types are not supported.")
    return f"ARRAY[{lower}..{upper}] OF {base_type}"


def _parse_import_data_type_member(element) -> DataTypeMemberSpec:
    if set(element.attrib) != {"name"} or not element.get("name"):
        raise ValueError("PLCopen data type members require only a name attribute.")
    _check_children(element, {"Documentation", "AddData", "Type"}, "data type member")
    type_element = _one_child(element, "Type", required=True)
    _parse_import_add_data(element, "data type member")
    return DataTypeMemberSpec(
        name=element.get("name"),
        data_type=_parse_import_type_ref(type_element),
        comment=_parse_import_documentation(element, "data type member"),
    )


def _parse_import_data_type(element, namespace_path="") -> DataTypeSpec:
    if set(element.attrib) != {"name"} or not element.get("name"):
        raise ValueError("PLCopen data type declarations require only a name attribute.")
    name = f"{namespace_path}\\{element.get('name')}" if namespace_path else element.get("name")
    _check_children(element, {"Documentation", "AddData", "UserDefinedTypeSpec"}, "DataTypeDecl")
    _parse_import_add_data(element, "data type")
    spec = _one_child(element, "UserDefinedTypeSpec", required=True)
    kind = spec.get(f"{{{XSI}}}type")
    comment = _parse_import_documentation(element, "data type")
    if kind == "StructTypeSpec":
        if set(spec.attrib) - {f"{{{XSI}}}type", "overlap"}:
            raise ValueError("Unsupported PLCopen structure type attributes.")
        overlap = spec.get("overlap", "false")
        if overlap not in {"true", "false"}:
            raise ValueError("Invalid PLCopen structure overlap flag.")
        _check_children(spec, {"Member", "AddData"}, "StructTypeSpec")
        _parse_import_add_data(spec, "structure")
        members = [_parse_import_data_type_member(item) for item in _children_named(spec, "Member")]
        return DataTypeSpec(
            name=name,
            kind="UNION" if overlap == "true" else "STRUCT",
            members=members,
            comment=comment,
        )
    if kind == "EnumTypeWithNamedValueSpec":
        if set(spec.attrib) != {f"{{{XSI}}}type"}:
            raise ValueError("Unsupported PLCopen enumeration type attributes.")
        _check_children(spec, {"Enumerator", "BaseType", "AddData"}, "EnumTypeWithNamedValueSpec")
        _parse_import_add_data(spec, "enumeration")
        bases = _children_named(spec, "BaseType")
        if len(bases) != 1 or bases[0].attrib or _xml_elements(bases[0]):
            raise ValueError("PLCopen named enumerations require one simple base type.")
        base_type = (bases[0].text or "").strip()
        if not base_type:
            raise ValueError("PLCopen enumeration base type is required.")
        values = []
        for item in _children_named(spec, "Enumerator"):
            if set(item.attrib) != {"name", "value"} or not item.get("name"):
                raise ValueError("PLCopen named enumerators require name and integer value attributes.")
            if any(_iec_tag(child) != "Documentation" for child in _xml_elements(item)):
                raise ValueError("Unsupported PLCopen enumerator child element.")
            documentation = _parse_import_documentation(item, "enumerator")
            try:
                numeric_value = int(item.get("value"))
            except (TypeError, ValueError) as exc:
                raise ValueError("PLCopen enumerator values must be integer constants.") from exc
            values.append(EnumValueSpec(name=item.get("name"), value=numeric_value, comment=documentation))
        return DataTypeSpec(
            name=name, kind="ENUM", base_type=base_type, values=values, comment=comment
        )
    if kind == "ArrayTypeSpec":
        base_type = _parse_import_array_type_spec(spec)
        return DataTypeSpec(name=name, kind="ARRAY", base_type=base_type, comment=comment)
    raise ValueError(f"Unsupported PLCopen user-defined type: {kind or 'missing xsi:type'}.")


def _parse_import_namespace_types(element, parent_path="") -> list[DataTypeSpec]:
    if set(element.attrib) != {"name"} or not element.get("name"):
        raise ValueError("PLCopen namespaces require only a name attribute.")
    name = element.get("name")
    if re.fullmatch(Identifier, name) is None:
        raise ValueError("PLCopen namespace names must be simple identifiers.")
    _check_children(
        element, {"Documentation", "AddData", "DataTypeDecl", "NamespaceDecl"}, "NamespaceDecl"
    )
    _parse_import_add_data(element, "namespace")
    documentation = _parse_import_documentation(element, "namespace")
    if documentation and documentation.strip():
        raise ValueError("Namespace documentation cannot be represented in a native DataType table.")
    path = f"{parent_path}\\{name}" if parent_path else name
    data_types = []
    for child in _xml_elements(element):
        child_name = _iec_tag(child)
        if child_name == "DataTypeDecl":
            data_types.append(_parse_import_data_type(child, path))
        elif child_name == "NamespaceDecl":
            data_types.extend(_parse_import_namespace_types(child, path))
    return data_types


def _flatten_imported_type_reference(value: str, declared_types: dict[str, str]) -> str:
    array = re.fullmatch(r"(\s*array\s*\[[^\]]+\]\s*of\s+)(.+)", value, re.IGNORECASE)
    if array:
        return array.group(1) + _flatten_imported_type_reference(array.group(2).strip(), declared_types)
    reference = value.strip()
    local_name = declared_types.get(reference.casefold())
    if local_name is not None:
        return local_name
    return value


def _flatten_imported_data_type_namespaces(data_types, pous, global_variables):
    """Map PLCopen namespace groups into the flat native DataType symbol scope."""
    local_names = [item.name.rpartition("\\")[2] for item in data_types]
    if len({name.casefold() for name in local_names}) != len(local_names):
        raise ValueError("duplicate data type names after flattening PLCopen namespaces")
    declared_types = {
        item.name.casefold(): item.name.rpartition("\\")[2] for item in data_types
    }
    flattened_types = []
    for data_type in data_types:
        members = [
            {
                **member.model_dump(),
                "data_type": _flatten_imported_type_reference(member.data_type, declared_types),
            }
            for member in data_type.members
        ]
        flattened_types.append(
            DataTypeSpec.model_validate(
                {
                    **data_type.model_dump(),
                    "name": data_type.name.rpartition("\\")[2],
                    "members": members,
                    "base_type": (
                        _flatten_imported_type_reference(data_type.base_type, declared_types)
                        if data_type.kind == "ARRAY"
                        else data_type.base_type
                    ),
                }
            )
        )

    flattened_pous = [
        PouSpec.model_validate(
            {
                **pou.model_dump(),
                "return_type": (
                    _flatten_imported_type_reference(pou.return_type, declared_types)
                    if pou.return_type is not None
                    else None
                ),
                "variables": [
                    {
                        **variable.model_dump(),
                        "data_type": _flatten_imported_type_reference(
                            variable.data_type, declared_types
                        ),
                    }
                    for variable in pou.variables
                ],
            }
        )
        for pou in pous
    ]
    flattened_globals = [
        VariableSpec.model_validate(
            {
                **variable.model_dump(),
                "data_type": _flatten_imported_type_reference(
                    variable.data_type, declared_types
                ),
            }
        )
        for variable in global_variables
    ]
    return flattened_types, flattened_pous, flattened_globals


def _parse_import_ld(body_content) -> list[LadderNetwork]:
    _check_children(body_content, {"Rung"}, "LD body")
    rungs = _children_named(body_content, "Rung")
    if not rungs:
        raise ValueError("PLCopen LD body must contain at least one rung.")
    if len(rungs) > 1000:
        raise ValueError("PLCopen LD body exceeds the 1000-rung limit.")
    parsed = []
    seen_orders = set()
    for rung in rungs:
        if len(rungs) > 1 and "evaluationOrder" not in rung.attrib:
            raise ValueError("Multiple PLCopen LD rungs require explicit evaluationOrder values.")
        order_text = rung.get("evaluationOrder", "1")
        if not order_text.isdecimal() or int(order_text) < 1:
            raise ValueError("PLCopen LD rung evaluationOrder must be a positive integer.")
        order = int(order_text)
        if order in seen_orders:
            raise ValueError("PLCopen LD rung evaluationOrder values must be unique.")
        seen_orders.add(order)
        parsed.append((int(order), _parse_import_ld_rung(rung)))
    return [network for _, network in sorted(parsed, key=lambda item: item[0])]


def _parse_import_ld_rung(rung) -> LadderNetwork:
    if set(rung.attrib) - {"evaluationOrder"}:
        raise ValueError("Unsupported PLCopen LD rung attributes.")
    _check_children(rung, {"LdObject"}, "LD rung")
    objects = _children_named(rung, "LdObject")
    if not objects:
        raise ValueError("PLCopen LD rung must contain connected objects.")

    type_kinds = {
        "LeftPowerRail": "left_rail",
        "RightPowerRail": "right_rail",
        "Contact": "contact_no",
        "Coil": "coil",
    }
    nodes = []
    output_ids = {}
    incoming_refs = []
    for index, element in enumerate(objects):
        kind = element.get(f"{{{XSI}}}type")
        if kind not in type_kinds:
            raise ValueError(f"Unsupported PLCopen LD object type: {kind or 'missing'}.")
        node_id = f"node-{index}"
        node_kind = type_kinds[kind]
        allowed_attributes = {f"{{{XSI}}}type"}
        variable = None
        if node_kind in {"contact_no", "coil"}:
            allowed_attributes.update({"operand", "negated"})
            variable = element.get("operand")
            if variable is None or not variable.strip() or "\x00" in variable:
                raise ValueError("PLCopen LD contacts and coils require a non-empty operand.")
            negated = element.get("negated", "false")
            if negated not in {"true", "false"}:
                raise ValueError("PLCopen LD negated must be true or false.")
            if node_kind == "contact_no" and negated == "true":
                node_kind = "contact_nc"
            elif node_kind == "coil" and negated == "true":
                node_kind = "coil_negated"
        if set(element.attrib) - allowed_attributes:
            raise ValueError("Unsupported PLCopen LD object attributes.")
        _check_children(element, {"ConnectionPointIn", "ConnectionPointOut"}, "LD object")
        point_in = _one_child(element, "ConnectionPointIn")
        point_out = _one_child(element, "ConnectionPointOut")
        needs_input = node_kind != "left_rail"
        needs_output = node_kind != "right_rail"
        if (point_in is not None) != needs_input or (point_out is not None) != needs_output:
            raise ValueError("PLCopen LD object has invalid connection points.")
        nodes.append({"id": node_id, "kind": node_kind, "variable": variable})
        if point_in is not None:
            if point_in.attrib:
                raise ValueError("Unsupported PLCopen connection-point input attributes.")
            _check_children(point_in, {"Connection"}, "LD input connection point")
            for connection in _xml_elements(point_in):
                if (
                    set(connection.attrib) != {"refConnectionPointOutId"}
                    or not connection.get("refConnectionPointOutId")
                    or _xml_elements(connection)
                    or (connection.text or "").strip()
                ):
                    raise ValueError("Malformed PLCopen LD connection reference.")
                incoming_refs.append((node_id, connection.get("refConnectionPointOutId")))
        if point_out is not None:
            output_id = point_out.get("connectionPointOutId")
            if (
                set(point_out.attrib) != {"connectionPointOutId"}
                or not output_id
                or _xml_elements(point_out)
                or (point_out.text or "").strip()
            ):
                raise ValueError("Malformed PLCopen LD output connection point.")
            if output_id in output_ids:
                raise ValueError("Duplicate PLCopen LD output connection-point ID.")
            output_ids[output_id] = node_id

    connections = []
    for destination, source_id in incoming_refs:
        source = output_ids.get(source_id)
        if source is None:
            raise ValueError("PLCopen LD connection references an unknown output point.")
        connections.append((source, destination))
    return LadderNetwork(nodes=nodes, connections=connections)


def _parse_import_pou(element) -> PouSpec:
    kind = _iec_tag(element)
    if kind not in {"Program", "Function", "FunctionBlock"}:
        raise ValueError(f"Unsupported PLCopen POU kind: {kind}.")
    if set(element.attrib) - {"name"} or not element.get("name"):
        raise ValueError("PLCopen POU requires a name and no unsupported attributes.")
    allowed = {"Parameters", "ExternalVars", "Vars", "TempVars", "MainBody", "ResultType"}
    _check_children(element, allowed, "POU")
    variables = []
    parameters = _one_child(element, "Parameters")
    if parameters is not None:
        if parameters.attrib:
            raise ValueError("Unsupported Parameters attributes in PLCopen XML.")
        groups = {"InputVars": "input", "OutputVars": "output", "InoutVars": "inout"}
        _check_children(parameters, set(groups), "Parameters")
        for group in _xml_elements(parameters):
            variables.extend(_parse_import_variable_group(group, groups[_iec_tag(group)]))
    for tag, scope in (
        ("ExternalVars", "external"),
        ("Vars", "local"),
        ("TempVars", "temp"),
    ):
        for group in _children_named(element, tag):
            variables.extend(_parse_import_variable_group(group, scope))
    unique_names(variables, "POU variable")

    result_type = None
    result_element = _one_child(element, "ResultType")
    if kind == "Function":
        if result_element is None:
            result_element = _one_child(element, "ResultType", required=True)
        _check_children(result_element, {"TypeName"}, "ResultType")
        if result_element.attrib:
            raise ValueError("Unsupported ResultType attributes in PLCopen XML.")
        type_name = _one_child(result_element, "TypeName", required=True)
        result_type = (type_name.text or "").strip()
        if type_name.attrib or _xml_elements(type_name) or not result_type:
            raise ValueError("Function return type must be a non-empty TypeName.")
    elif result_element is not None:
        raise ValueError("ResultType is supported only for Function POUs.")

    main_body = _one_child(element, "MainBody", required=True)
    if main_body.attrib:
        raise ValueError("Unsupported MainBody attributes in PLCopen XML.")
    _check_children(main_body, {"BodyContent"}, "MainBody")
    body_content = _one_child(main_body, "BodyContent", required=True)
    if set(body_content.attrib) != {f"{{{XSI}}}type"}:
        raise ValueError("PLCopen BodyContent must contain only its language attribute.")
    language = body_content.get(f"{{{XSI}}}type")
    if language == "ST":
        _check_children(body_content, {"ST"}, "ST body")
        source_element = _one_child(body_content, "ST", required=True)
        if source_element.attrib or _xml_elements(source_element):
            raise ValueError("PLCopen ST body must contain plain source text.")
        source = source_element.text or ""
        if not source.strip() or "\x00" in source:
            raise ValueError("PLCopen ST body must be non-empty text without NUL.")
        return PouSpec(
            name=element.get("name"),
            kind=kind,
            language="ST",
            source=source,
            return_type=result_type,
            variables=variables,
        )
    if language == "LD":
        networks = _parse_import_ld(body_content)
        return PouSpec(
            name=element.get("name"),
            kind=kind,
            language="LD",
            network=networks[0] if len(networks) == 1 else None,
            networks=networks if len(networks) > 1 else None,
            return_type=result_type,
            variables=variables,
        )
    raise ValueError(f"Unsupported PLCopen POU language: {language or 'missing'}.")


def _parse_import_global_variables(instances) -> list[VariableSpec]:
    if instances is None or not _xml_elements(instances):
        return []
    if instances.attrib:
        raise ValueError("Unsupported PLCopen Instances attributes.")
    _check_children(instances, {"Configuration"}, "Instances")
    configurations = _children_named(instances, "Configuration")
    if len(configurations) != 1:
        raise ValueError("Only one PLCopen Configuration is supported for global variables.")
    configuration = configurations[0]
    if set(configuration.attrib) - {"name"}:
        raise ValueError("Unsupported PLCopen Configuration attributes.")
    _check_children(configuration, {"Resource"}, "Configuration")
    resources = _children_named(configuration, "Resource")
    if len(resources) != 1:
        raise ValueError("Only one PLCopen Resource is supported for global variables.")
    resource = resources[0]
    if set(resource.attrib) - {"name", "resourceTypeName"}:
        raise ValueError("Unsupported PLCopen Resource attributes.")
    _check_children(resource, {"GlobalVars"}, "Resource")
    groups = _children_named(resource, "GlobalVars")
    if not groups:
        raise ValueError("PLCopen resource contains no supported global variable group.")
    variables = []
    for group in groups:
        variables.extend(_parse_import_variable_group(group, "global"))
    unique_names(variables, "global variable")
    return variables


def parse_iec_xml_document(data: bytes) -> dict[str, list]:
    """Parse supported IEC POUs, globals and controller data type declarations."""
    root = parse_xml(data)
    if _iec_tag(root) != "Project":
        raise ValueError("PLCopen XML root must be Project.")
    _check_children(root, {"FileHeader", "ContentHeader", "Types", "Instances"}, "Project")
    types = _one_child(root, "Types", required=True)
    _check_children(types, {"GlobalNamespace"}, "Types")
    namespace = _one_child(types, "GlobalNamespace", required=True)
    _check_children(
        namespace,
        {"Program", "Function", "FunctionBlock", "DataTypeDecl", "NamespaceDecl"},
        "GlobalNamespace",
    )
    instances = _one_child(root, "Instances")
    pous = []
    data_types = []
    for element in _xml_elements(namespace):
        tag = _iec_tag(element)
        if tag == "DataTypeDecl":
            data_types.append(_parse_import_data_type(element))
        elif tag == "NamespaceDecl":
            data_types.extend(_parse_import_namespace_types(element))
        else:
            pous.append(_parse_import_pou(element))
    unique_names(pous, "POU")
    global_variables = _parse_import_global_variables(instances)
    data_types, pous, global_variables = _flatten_imported_data_type_namespaces(
        data_types, pous, global_variables
    )
    unique_names(data_types, "data type")
    if not pous and not global_variables and not data_types:
        raise ValueError("PLCopen document contains no supported POUs, global variables or data types.")
    return {"pous": pous, "global_variables": global_variables, "data_types": data_types}


def parse_iec_xml_pous(data: bytes) -> list[PouSpec]:
    """Parse ST POUs and connected PLCopen LD networks without project globals."""
    document = parse_iec_xml_document(data)
    if document["global_variables"]:
        raise ValueError("PLCopen global variables require project-level import context.")
    if document["data_types"]:
        raise ValueError("PLCopen data type declarations require project-level import context.")
    if not document["pous"]:
        raise ValueError("PLCopen document contains no supported POUs.")
    return document["pous"]
