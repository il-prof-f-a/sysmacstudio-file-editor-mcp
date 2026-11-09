"""Persistent local revisions. Native exports are candidates until rebuilt in Sysmac."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import shutil
import sqlite3
import uuid
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from lxml import etree as ET

from .archive import ArchiveError, ProjectArchive, compare_files
from .project import NativeProject, parse_xml, serialize_xml, xml_local_name


class ProjectError(ValueError):
    def __init__(self, message: str, code: str = "invalid_operation"):
        super().__init__(message)
        self.code = code


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def clean_field(value) -> str:
    text = str(value)
    if any(c in text for c in "\r\n\t\x00"):
        raise ProjectError("Variable fields cannot contain SLWD delimiters.")
    return text


IEC_IMPORT_MAX_BYTES = 8 * 1024 * 1024
IEC_ELEMENTARY_TYPES = frozenset(
    {
        "bool",
        "byte",
        "word",
        "dword",
        "lword",
        "sint",
        "int",
        "dint",
        "lint",
        "usint",
        "uint",
        "udint",
        "ulint",
        "real",
        "lreal",
        "time",
        "ltime",
        "date",
        "ldate",
        "tod",
        "time_of_day",
        "dt",
        "date_and_time",
        "ldt",
        "string",
        "wstring",
        "char",
        "wchar",
        "ton",
        "tof",
        "tp",
        "ctu",
        "ctd",
        "ctud",
        "r_trig",
        "f_trig",
        "rs",
        "sr",
    }
)


def read_iec_import(
    path: str | Path,
    schema_path: str | Path | None = None,
    *,
    expected_sha256: str | None = None,
):
    from .iec_xml import find_iec_schema, parse_iec_xml_document, validate_iec_xml

    source = Path(path).expanduser().resolve(strict=True)
    if not source.is_file():
        raise ProjectError("PLCopen XML source must be a local file.")
    with source.open("rb") as stream:
        data = stream.read(IEC_IMPORT_MAX_BYTES + 1)
    if len(data) > IEC_IMPORT_MAX_BYTES:
        raise ProjectError("PLCopen XML source exceeds the 8 MiB import limit.", "input_too_large")
    source_sha256 = hashlib.sha256(data).hexdigest()
    if expected_sha256 is not None and source_sha256.casefold() != expected_sha256.casefold():
        raise ProjectError("PLCopen XML source changed since preview.", "source_changed")
    schema = find_iec_schema(schema_path)
    validation = (
        validate_iec_xml(data, schema)
        if schema is not None
        else {"status": "not_executed", "reason": "No local Omron XSD found"}
    )
    document = parse_iec_xml_document(data)
    return {
        "path": source,
        "data": data,
        "sha256": source_sha256,
        "schema_path": schema,
        "schema_validation": validation,
        "pous": document["pous"],
        "global_variables": document["global_variables"],
        "data_types": document["data_types"],
    }


def iec_import_plan_id(
    project, project_id, base_revision, source_sha256, schema_path, templates, data_type_table_id=None
):
    staged = [
        {"member": name, "sha256": hashlib.sha256(data).hexdigest()}
        for name, data in sorted(project.archive.overrides.items())
    ]
    body = {
        "project_id": project_id,
        "base_revision": base_revision,
        "source_sha256": source_sha256,
        "schema_path": str(schema_path) if schema_path else None,
        "template_pou_ids": dict(sorted(templates.items())),
        "data_type_table_id": data_type_table_id,
        "staged_members": staged,
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class Workspace:
    def __init__(self, root: str | Path, *, profile_root: str | Path | None = None):
        self.profile_root = profile_root
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "projects.sqlite3"
        with self.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, source_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL, snapshot_path TEXT NOT NULL,
                    working_path TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS transactions (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
                    base_revision INTEGER NOT NULL, status TEXT NOT NULL,
                    patches TEXT NOT NULL DEFAULT '{}', changes TEXT NOT NULL DEFAULT '[]'
                );
            """)

    def connect(self):
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _record(self, connection, project_id):
        record = connection.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if record is None:
            raise ProjectError("Project ID not found. Open the project first.", "project_not_found")
        return record

    def open_project(self, path: str | Path):
        path = Path(path).resolve(strict=True)
        digest = sha256(path)
        NativeProject(path)  # Validate before storing the copy.
        project_id = "project_" + digest
        originals = self.root / "originals"
        originals.mkdir(exist_ok=True)
        snapshot = originals / (digest + ".smc2")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not snapshot.exists():
                temporary = originals / ("." + digest + "." + uuid.uuid4().hex + ".partial")
                try:
                    with path.open("rb") as source, temporary.open("xb") as target:
                        shutil.copyfileobj(source, target)
                    if sha256(temporary) != digest or sha256(path) != digest:
                        raise ProjectError(
                            "Source changed while copying the original.", "source_changed"
                        )
                    temporary.replace(snapshot)
                finally:
                    temporary.unlink(missing_ok=True)
            if sha256(snapshot) != digest or sha256(path) != digest:
                raise ProjectError(
                    "Source changed while copying, or original snapshot was altered.",
                    "source_changed",
                )
            connection.execute(
                "INSERT OR IGNORE INTO projects(id,source_path,sha256,snapshot_path,working_path) VALUES (?,?,?,?,?)",
                (project_id, str(path), digest, str(snapshot), str(snapshot)),
            )
            record = self._record(connection, project_id)
        return {
            "project_id": project_id,
            "revision": record["revision"],
            "source_sha256": digest,
            "info": self.project(project_id).info(),
        }

    @staticmethod
    def decode_patches(record):
        return {
            name: base64.b64decode(value, validate=True)
            for name, value in json.loads(record["patches"]).items()
        }

    def _transaction(self, connection, transaction_id, expected_revision=None):
        tx = connection.execute(
            "SELECT * FROM transactions WHERE id=?", (transaction_id,)
        ).fetchone()
        if tx is None or tx["status"] != "active":
            raise ProjectError(
                "Transaction is not active; begin a new transaction.", "transaction_not_active"
            )
        project = self._record(connection, tx["project_id"])
        if project["revision"] != tx["base_revision"] or (
            expected_revision is not None and expected_revision != project["revision"]
        ):
            raise ProjectError(
                "Project revision changed. Read the new revision and begin a new transaction.",
                "revision_conflict",
            )
        return tx, project

    def project(self, project_id: str, transaction_id: str | None = None):
        with self.connect() as connection:
            record = self._record(connection, project_id)
            patches = {}
            if transaction_id:
                tx, _ = self._transaction(connection, transaction_id)
                if tx["project_id"] != project_id:
                    raise ProjectError("Transaction belongs to another project.")
                patches = self.decode_patches(tx)
        return NativeProject(record["working_path"], patches)

    def begin(self, project_id: str):
        transaction_id = "tx_" + uuid.uuid4().hex
        with self.connect() as connection:
            record = self._record(connection, project_id)
            connection.execute(
                "INSERT INTO transactions(id,project_id,base_revision,status) VALUES (?,?,?,'active')",
                (transaction_id, project_id, record["revision"]),
            )
        return {
            "transaction_id": transaction_id,
            "base_revision": record["revision"],
            "status": "active",
        }

    def _stage(self, transaction_id, expected_revision, operation):
        def single(project):
            member, content, change = operation(project)
            return {member: content}, change

        return self._stage_many(transaction_id, expected_revision, single)

    def _stage_many(self, transaction_id, expected_revision, operation, *, allow_added=False):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            tx, record = self._transaction(connection, transaction_id, expected_revision)
            patches = self.decode_patches(tx)
            project = NativeProject(record["working_path"], patches)
            updates, change = operation(project)
            if (
                not updates
                or not allow_added
                and any(member not in project.archive.members for member in updates)
            ):
                raise ProjectError("Expected patches for existing archive members.")
            patches.update(updates)
            ProjectArchive(record["working_path"]).apply_overrides(patches)
            changes = json.loads(tx["changes"])
            changes.append(change)
            connection.execute(
                "UPDATE transactions SET patches=?,changes=? WHERE id=?",
                (
                    json.dumps(
                        {k: base64.b64encode(v).decode("ascii") for k, v in patches.items()}
                    ),
                    json.dumps(changes),
                    transaction_id,
                ),
            )
        return {
            "status": "staged",
            "transaction_id": transaction_id,
            "change": change,
            "applied_to_sysmac": False,
            "sysmac_build": "not_executed",
        }

    def update_st(self, transaction_id, section_id, source, *, expected_revision):
        if not isinstance(source, str) or not source.strip() or "\x00" in source:
            raise ProjectError("ST source must be non-empty text without NUL.")

        def operation(project):
            entity = project.resolve(section_id, {"PouBody"})
            if entity.get("subtype") != "StructuredText":
                raise ProjectError("Selected section is not Structured Text.")
            member = project.member(entity.get("id"))
            raw = project.archive.read(member)
            root = parse_xml(raw)
            texts = root.xpath('//*[local-name()="Text"]')
            if len(texts) != 1 or ET.QName(root).localname != "StructuredTextModel":
                raise ProjectError("Unrecognised ST model; refusing to rewrite it.")
            texts[0].text = source
            from .external_dependencies import register_new_dependencies

            try:
                updates, registrations = register_new_dependencies(
                    project, {member: serialize_xml(root, raw)}, project.owner(entity)
                )
            except ValueError as exc:
                raise ProjectError(str(exc)) from exc
            return updates, {
                "operation": "update_st",
                "section_id": entity.get("id"),
                "registered_externals": registrations,
            }

        return self._stage_many(transaction_id, expected_revision, operation)

    def register_external(self, transaction_id, pou_id, global_variable_id, *, expected_revision):
        from .external_dependencies import register_externals

        def operation(project):
            try:
                updates, details = register_externals(project, pou_id, [global_variable_id])
            except ValueError as exc:
                raise ProjectError(str(exc)) from exc
            return updates, {"operation": "register_external", "registrations": details}

        return self._stage_many(transaction_id, expected_revision, operation)

    def rename_variable(self, transaction_id, variable_id, new_name, *, expected_revision):
        from .rename import prepare_rename
        from .symbols import SymbolIndex

        def operation(project):
            plan, patches = prepare_rename(SymbolIndex(project), variable_id, new_name)
            if not plan["can_apply"]:
                raise ProjectError(
                    "Rename blocked: "
                    + ", ".join(dict.fromkeys(b["code"] for b in plan["blockers"]))
                )
            return patches, {
                "operation": "rename_variable",
                "variable_id": variable_id,
                "new_variable_id": plan["new_variable_id"],
                "old_name": plan["old_name"],
                "new_name": new_name,
                "reference_count": len(plan["references"]),
                "declaration_count": len(plan["declarations"]),
                "external_consumers": "not_acquired",
            }

        return self._stage_many(transaction_id, expected_revision, operation)

    @staticmethod
    def _iec_type_key(value):
        return re.sub(r"\s+", "", value).casefold()

    @staticmethod
    def _iec_type_names(project, controller_id, additional=()):
        from .symbols import SymbolIndex, controller

        index = SymbolIndex(project)
        names = {Workspace._iec_type_key(value) for value in IEC_ELEMENTARY_TYPES}
        names.update(Workspace._iec_type_key(value) for value in additional)
        names.update(
            Workspace._iec_type_key(pou["name"])
            for pou in project.pous()
            if pou["kind"] == "FunctionBlock"
            and controller(project.entities[pou["id"]]) == controller_id
        )
        names.update(
            Workspace._iec_type_key(name)
            for (owner, name), definitions in index.types.items()
            if owner == controller_id and len(definitions) == 1
        )
        return names

    @staticmethod
    def _require_iec_type(type_name, scope, available):
        def supported(value, depth=0):
            if depth > 8:
                return False
            key = Workspace._iec_type_key(value.strip())
            if key in available:
                return True
            string_type = re.fullmatch(r"(?:w?string)\[(\d+)\]", key)
            if string_type:
                return int(string_type.group(1)) > 0
            array_type = re.fullmatch(
                r"array\s*\[\s*(\*|([+-]?\d+)\s*\.\.\s*([+-]?\d+))\s*\]\s*of\s*(.+)",
                value.strip(),
                flags=re.IGNORECASE,
            )
            if array_type:
                lower, upper = array_type.group(2), array_type.group(3)
                if lower is None:
                    if scope not in {"input", "inout"}:
                        return False
                elif int(lower) > int(upper):
                    return False
                return supported(array_type.group(4), depth + 1)
            return False

        if not supported(type_name):
            raise ProjectError(
                f"IEC data type '{type_name}' is not present or supported in this controller."
            )

    @staticmethod
    def _materialize_iec_array_aliases(pous, global_variables, data_types):
        aliases = {
            Workspace._iec_type_key(item.name): item
            for item in data_types
            if item.kind == "ARRAY"
        }
        if not aliases:
            return pous, global_variables, data_types, []

        array_expression = re.compile(
            r"^array\s*\[\s*([^\]]+)\s*\]\s*of\s*(.+)$", re.IGNORECASE
        )
        resolved = {}
        resolving = set()

        def expand(type_name):
            value = type_name.strip()
            key = Workspace._iec_type_key(value)
            if key in aliases:
                if key in resolving:
                    raise ProjectError(
                        "PLCopen array type aliases contain a recursive dependency."
                    )
                if key not in resolved:
                    resolving.add(key)
                    resolved[key] = expand(aliases[key].base_type)
                    resolving.remove(key)
                return resolved[key]

            match = array_expression.fullmatch(value)
            if not match:
                return value
            element_type = expand(match.group(2))
            if array_expression.fullmatch(element_type):
                raise ProjectError(
                    "Nested PLCopen array aliases cannot be represented as one Sysmac array type."
                )
            return f"ARRAY[{match.group(1).strip()}] OF {element_type}"

        native_data_types = []
        for data_type in data_types:
            if data_type.kind == "ARRAY":
                continue
            if data_type.kind in {"STRUCT", "UNION"}:
                members = [
                    item.model_copy(update={"data_type": expand(item.data_type)})
                    for item in data_type.members
                ]
                data_type = data_type.model_copy(update={"members": members})
            native_data_types.append(data_type)

        imported_pous = [
            pou.model_copy(
                update={
                    "variables": [
                        variable.model_copy(
                            update={"data_type": expand(variable.data_type)}
                        )
                        for variable in pou.variables
                    ],
                    "return_type": (
                        expand(pou.return_type) if pou.return_type is not None else None
                    ),
                }
            )
            for pou in pous
        ]
        imported_globals = [
            variable.model_copy(update={"data_type": expand(variable.data_type)})
            for variable in global_variables
        ]
        alias_summaries = [
            {
                "name": item.name,
                "kind": "ARRAY",
                "base_type": expand(item.base_type),
                "member_count": 0,
                "value_count": 0,
                "comment": item.comment,
                "table_id": None,
                "native_representation": "inline_array",
            }
            for item in data_types
            if item.kind == "ARRAY"
        ]
        return imported_pous, imported_globals, native_data_types, alias_summaries

    @staticmethod
    def _append_iec_globals(
        project, global_variables, template_pou_ids, pous, *, target_controller_id=None
    ):
        if not global_variables:
            return {}, [], project

        from .symbols import controller

        template_keys = {
            f"{pou.kind}:LD" if pou.language == "LD" else pou.kind for pou in pous
        }
        template_ids = [template_pou_ids[key] for key in sorted(template_keys)]
        controller_ids = set()
        for template_id in template_ids:
            if template_id not in project.entities:
                raise ProjectError("Global import templates must be native entity IDs.")
            template = project.entities[template_id]
            cid = controller(template)
            if cid is None:
                raise ProjectError("Global import templates must belong to a known controller.")
            controller_ids.add(cid)
        if len(controller_ids) > 1:
            raise ProjectError("Global imports require all selected POU templates on one controller.")
        if controller_ids:
            controller_id = next(iter(controller_ids))
            if target_controller_id is not None and target_controller_id != controller_id:
                raise ProjectError("Global import controller does not match the selected data type table.")
        elif target_controller_id is not None:
            controller_id = target_controller_id
        else:
            devices = [
                entity.get("id")
                for entity in project.entities.values()
                if entity.get("type") == "Device"
            ]
            if len(devices) != 1:
                raise ProjectError(
                    "A global-only import requires exactly one controller in the project."
                )
            controller_id = devices[0]

        imported_function_blocks = set()
        for pou in pous:
            if pou.kind != "FunctionBlock":
                continue
            key = f"{pou.kind}:LD" if pou.language == "LD" else pou.kind
            template_id = template_pou_ids[key]
            if (
                template_id in project.entities
                and controller(project.entities[template_id]) == controller_id
            ):
                imported_function_blocks.add(pou.name)
        known_types = Workspace._iec_type_names(
            project, controller_id, imported_function_blocks
        )
        for variable in global_variables:
            Workspace._require_iec_type(variable.data_type, "global", known_types)

        tables = [
            entity
            for entity in project.entities.values()
            if entity.get("type") == "Variables"
            and entity.get("subtype") == "Global"
            and controller(entity) == controller_id
        ]
        if len(tables) != 1:
            raise ProjectError(
                "The selected controller must have exactly one native global variable table."
            )
        table = tables[0]
        member = project.member(table.get("id"))
        if not member:
            raise ProjectError("The native global variable table has no readable member.")
        raw = project.archive.read(member)
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ProjectError("The native global variable table is not UTF-8 SLWD.") from exc
        if not text.startswith("[SLWD version=1.0]"):
            raise ProjectError("Only observed SLWD global variable tables can be edited.")
        lines = text.splitlines()
        headings = [
            index
            for index, line in enumerate(lines)
            if line.split("\t", 1)[0] == "+GN=VAR_GLOBAL"
        ]
        if len(headings) != 1:
            raise ProjectError(
                "The global table must have exactly one observed VAR_GLOBAL declaration group."
            )
        heading = headings[0]
        end = next(
            (
                index
                for index in range(heading + 1, len(lines))
                if lines[index].startswith("+") and not lines[index].startswith("++")
            ),
            len(lines),
        )

        existing_globals = [
            variable
            for variable in project.variables("global")
            if controller(project.entities[variable["table_id"]]) == controller_id
        ]
        names = {variable["name"].casefold() for variable in existing_globals}
        pou_names = {
            pou["name"].casefold()
            for pou in project.pous()
            if controller(project.entities[pou["id"]]) == controller_id
        }
        imported_names = set()
        appended = []
        for variable in global_variables:
            folded = variable.name.casefold()
            if folded in names or folded in imported_names:
                raise ProjectError(
                    f"Global variable '{variable.name}' already exists in this controller."
                )
            if folded in pou_names:
                raise ProjectError(
                    f"Global variable '{variable.name}' conflicts with a POU in this controller."
                )
            names.add(folded)
            imported_names.add(folded)
            fields = {
                "D": clean_field(variable.data_type),
                "N": clean_field(variable.name),
                "G": "VAR_GLOBAL",
            }
            if variable.initial_value is not None:
                fields["IV"] = clean_field(variable.initial_value)
            if variable.comment is not None:
                fields["Com"] = clean_field(variable.comment)
            if variable.retained:
                fields["R"] = "1"
            if variable.constant:
                fields["Const"] = "1"
            appended.append("++" + "\t".join(f"{key}={value}" for key, value in fields.items()))

        lines[end:end] = appended
        ending = "\r\n" if "\r\n" in text else "\r" if "\r" in text else "\n"
        rendered = ending.join(lines)
        if text.endswith(("\r", "\n")):
            rendered += ending
        bom = b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b""
        updates = {member: bom + rendered.encode("utf-8")}
        project = NativeProject(project.archive.path, {**project.archive.overrides, **updates})
        return updates, [
            {"name": variable.name, "data_type": variable.data_type}
            for variable in global_variables
        ], project

    @staticmethod
    def _select_iec_import_controller(
        project, pous, template_pou_ids, global_variables, data_types, data_type_table_id
    ):
        from .symbols import controller

        if data_type_table_id is not None and not data_types:
            raise ProjectError("data_type_table_id is only valid when importing PLCopen data types.")
        template_keys = {
            f"{pou.kind}:LD" if pou.language == "LD" else pou.kind for pou in pous
        }
        controller_ids = set()
        for key in template_keys:
            template_id = template_pou_ids.get(key)
            template = project.entities.get(template_id) if template_id else None
            if template is not None:
                controller_id = controller(template)
                if controller_id is None:
                    raise ProjectError("Import templates must belong to a known controller.")
                controller_ids.add(controller_id)
        if len(controller_ids) > 1:
            raise ProjectError("PLCopen imports require all selected templates on one controller.")
        selected_controller = next(iter(controller_ids), None)

        if data_types:
            if data_type_table_id is not None:
                table = project.entities.get(data_type_table_id)
                if table is None or table.get("type") != "DataType":
                    raise ProjectError("data_type_table_id must be an existing native DataType table ID.")
                table_controller = controller(table)
                if table_controller is None:
                    raise ProjectError("Selected DataType table must belong to a known controller.")
            else:
                tables = [
                    entity
                    for entity in project.entities.values()
                    if entity.get("type") == "DataType"
                ]
                if not tables:
                    raise ProjectError("The selected controller has no observed native DataType table.")
                if len(tables) != 1:
                    raise ProjectError(
                        "Select data_type_table_id because the project has multiple native DataType tables."
                    )
                table_controller = controller(tables[0])
                if table_controller is None:
                    raise ProjectError("The DataType table must belong to a known controller.")
            if selected_controller is not None and selected_controller != table_controller:
                raise ProjectError("Selected POU templates and DataType table belong to different controllers.")
            selected_controller = table_controller
        elif global_variables and selected_controller is None:
            devices = [
                entity.get("id")
                for entity in project.entities.values()
                if entity.get("type") == "Device"
            ]
            if len(devices) != 1:
                raise ProjectError(
                    "A global-only import requires exactly one controller in the project."
                )
            selected_controller = devices[0]
        return selected_controller

    @staticmethod
    def _append_iec_data_types(
        project, data_types, data_type_table_id, controller_id, pous, global_variables
    ):
        if not data_types:
            return {}, [], project
        if not controller_id or not data_type_table_id:
            raise ProjectError("An observed native DataType table is required for PLCopen data types.")

        from .symbols import controller

        inventory = project.data_types(controller_id=controller_id)
        if inventory["completeness"] != "observed":
            raise ProjectError(
                "All native DataType tables on the selected controller must be completely observed."
            )
        selected_tables = [table for table in inventory["tables"] if table["id"] == data_type_table_id]
        if len(selected_tables) != 1 or selected_tables[0]["completeness"] != "observed":
            raise ProjectError("Selected DataType table must be completely observed and editable.")
        table_entity = project.entities[data_type_table_id]
        if controller(table_entity) != controller_id:
            raise ProjectError("Selected DataType table belongs to another controller.")
        member = project.member(data_type_table_id)
        if not member:
            raise ProjectError("Selected DataType table has no readable native XML member.")
        raw = project.archive.read(member)
        try:
            root = parse_xml(raw)
        except (ArchiveError, ET.LxmlError) as exc:
            raise ProjectError("Selected native DataType table is not readable XML.") from exc
        if xml_local_name(root) != "data":
            raise ProjectError("Only observed native DataType XML tables can be edited.")
        root_children = list(root)
        if not root_children or any(xml_local_name(item) != "DataType" for item in root_children):
            raise ProjectError("Selected native DataType table has an unsupported root layout.")
        if (
            len(root_children) == 1
            and not root_children[0].get("BaseType", "").strip()
        ):
            group = root_children[0]
            if any(xml_local_name(item) != "DataType" for item in group):
                raise ProjectError("Selected native DataType group contains unsupported entries.")
        elif all(item.get("BaseType", "").strip() for item in root_children):
            group = root
        else:
            raise ProjectError("Selected native DataType table is not a recognized definitions group.")

        existing_types = inventory["items"]
        existing_names = {
            item["name"].casefold() for item in existing_types if item.get("name")
        }
        existing_enum_values = set()
        for data_type in existing_types:
            if (data_type.get("base_type") or "").upper() == "ENUM":
                existing_enum_values.update(
                    item["name"].casefold()
                    for item in project.data_types(parent_id=data_type["id"])["items"]
                    if item.get("name")
                )
        existing_pous = {
            item["name"].casefold()
            for item in project.pous()
            if controller(project.entities[item["id"]]) == controller_id
        }
        existing_globals = {
            item["name"].casefold()
            for item in project.variables("global")
            if controller(project.entities[item["table_id"]]) == controller_id
        }
        imported_pous = {pou.name.casefold() for pou in pous}
        imported_globals = {variable.name.casefold() for variable in global_variables}
        imported_names = {item.name.casefold() for item in data_types}
        occupied_names = (
            existing_names
            | existing_enum_values
            | existing_pous
            | existing_globals
            | imported_pous
            | imported_globals
        )
        for data_type in data_types:
            if data_type.name.casefold() in occupied_names:
                raise ProjectError(f"Data type '{data_type.name}' conflicts with an existing controller symbol.")
        occupied_names.update(imported_names)

        enum_names = set(existing_enum_values)
        for data_type in data_types:
            for value in data_type.values:
                folded = value.name.casefold()
                if folded in occupied_names or folded in enum_names:
                    raise ProjectError(f"Enumeration value '{value.name}' conflicts with a controller symbol.")
                enum_names.add(folded)

        additional_function_blocks = {
            pou.name for pou in pous if pou.kind == "FunctionBlock"
        }
        known_types = Workspace._iec_type_names(project, controller_id, additional_function_blocks)
        known_types.update(Workspace._iec_type_key(item.name) for item in data_types)
        for data_type in data_types:
            if data_type.kind == "ENUM":
                if data_type.base_type.upper() != "DINT":
                    raise ProjectError("Only the installed Omron IEC schema's DINT enumeration base type is supported.")
            elif data_type.kind == "ARRAY":
                Workspace._require_iec_type(data_type.base_type, "local", known_types)
            else:
                for item in data_type.members:
                    Workspace._require_iec_type(item.data_type, "local", known_types)

        imported_by_name = {item.name.casefold(): item for item in data_types}
        dependencies = {name: set() for name in imported_by_name}
        array_element = re.compile(r"^\s*array\s*\[[^\]]+\]\s*of\s*(.+)$", re.IGNORECASE)
        for data_type in data_types:
            references = (
                [item.data_type for item in data_type.members]
                if data_type.kind in {"STRUCT", "UNION"}
                else [data_type.base_type] if data_type.kind == "ARRAY" else []
            )
            for reference in references:
                target = reference
                while True:
                    match = array_element.fullmatch(target)
                    if not match:
                        break
                    target = match.group(1).strip()
                key = target.casefold()
                if key in imported_by_name:
                    dependencies[data_type.name.casefold()].add(key)
        visiting, visited = set(), set()

        def visit(name):
            if name in visiting:
                raise ProjectError("PLCopen data type declarations contain a recursive type dependency.")
            if name in visited:
                return
            visiting.add(name)
            for dependency in dependencies[name]:
                visit(dependency)
            visiting.remove(name)
            visited.add(name)

        for name in dependencies:
            visit(name)

        def attributes(name, base_type, *, comment=None, enum_value=None, order="0", offset_type=""):
            return {
                "Name": name,
                "BaseType": base_type,
                "ArrayType": "",
                "Length": "",
                "InitialValue": "",
                "EnumValue": str(enum_value) if enum_value is not None else "",
                "Comment": comment or "",
                "OffsetChannel": "",
                "OffsetBit": "",
                "IsControllerDefinedType": "false",
                "Order": str(order),
                "OffsetType": offset_type,
            }

        prior_orders = []
        for item in (root_children if group is root else list(group)):
            if xml_local_name(item) != "DataType" or not item.get("BaseType", "").strip():
                continue
            try:
                prior_orders.append(int(item.get("Order", "0")))
            except ValueError as exc:
                raise ProjectError("Existing DataType order value is not an integer.") from exc
        next_order = max(prior_orders, default=0) + 1
        summaries = []
        for data_type in data_types:
            base_type = {
                "STRUCT": "STRUCT",
                "UNION": "UNION",
                "ENUM": "ENUM",
                "ARRAY": data_type.base_type,
            }[data_type.kind]
            offset_type = "NJ" if data_type.kind == "STRUCT" else ""
            node = ET.SubElement(
                group,
                "DataType",
                **attributes(
                    data_type.name,
                    base_type,
                    comment=data_type.comment,
                    order=next_order,
                    offset_type=offset_type,
                ),
            )
            if data_type.kind in {"STRUCT", "UNION"}:
                for item in data_type.members:
                    ET.SubElement(
                        node,
                        "DataType",
                        **attributes(item.name, item.data_type, comment=item.comment),
                    )
            elif data_type.kind == "ENUM":
                for value in data_type.values:
                    ET.SubElement(
                        node,
                        "DataType",
                        **attributes(
                            value.name,
                            data_type.base_type,
                            comment=value.comment,
                            enum_value=value.value,
                        ),
                    )
            summaries.append(
                {
                    "name": data_type.name,
                    "kind": data_type.kind,
                    "base_type": base_type,
                    "member_count": len(data_type.members),
                    "value_count": len(data_type.values),
                    "comment": data_type.comment,
                    "table_id": data_type_table_id,
                }
            )
            next_order += 1

        updates = {member: serialize_xml(root, raw)}
        project = NativeProject(project.archive.path, {**project.archive.overrides, **updates})
        return updates, summaries, project

    @staticmethod
    def _prepare_iec_xml_pous(
        project, pous, template_pou_ids, global_variables=(), data_types=(), data_type_table_id=None
    ):
        from .native_pou import NativePouDefinition, native_ladder_rungs, prepare_pou
        from .symbols import controller

        original_data_types = data_types
        pous, global_variables, data_types, array_alias_summaries = (
            Workspace._materialize_iec_array_aliases(pous, global_variables, data_types)
        )
        kinds = {"Program", "Function", "FunctionBlock"}
        allowed_templates = kinds | {f"{kind}:LD" for kind in kinds}
        if set(template_pou_ids) - allowed_templates:
            raise ProjectError("Template map contains an unsupported POU kind.")
        template_keys = {
            f"{pou.kind}:LD" if pou.language == "LD" else pou.kind for pou in pous
        }
        missing = sorted(template_keys - set(template_pou_ids))
        if missing:
            raise ProjectError("A native template is required for: " + ", ".join(missing))
        controller_id = Workspace._select_iec_import_controller(
            project,
            pous,
            template_pou_ids,
            global_variables,
            original_data_types,
            data_type_table_id,
        )
        type_updates, type_summaries, project = Workspace._append_iec_data_types(
            project, data_types, data_type_table_id, controller_id, pous, global_variables
        )
        native_type_summaries = {
            item["name"].casefold(): item for item in type_summaries
        }
        array_summaries = {
            item["name"].casefold(): item for item in array_alias_summaries
        }
        type_summaries = [
            (array_summaries if item.kind == "ARRAY" else native_type_summaries)[
                item.name.casefold()
            ]
            for item in original_data_types
        ]
        global_updates, global_summaries, project = Workspace._append_iec_globals(
            project,
            global_variables,
            template_pou_ids,
            pous,
            target_controller_id=controller_id,
        )
        updates = {**type_updates, **global_updates}
        imported_function_blocks = {}
        for pou in pous:
            if pou.kind != "FunctionBlock":
                continue
            template_key = f"{pou.kind}:LD" if pou.language == "LD" else pou.kind
            template_id = template_pou_ids[template_key]
            template = project.entities.get(template_id)
            if template is not None:
                cid = controller(template)
                if cid is not None:
                    imported_function_blocks.setdefault(cid, set()).add(pou.name)
        known_types_by_controller = {}
        summaries = []
        for pou in pous:
            template_key = f"{pou.kind}:LD" if pou.language == "LD" else pou.kind
            template_id = template_pou_ids[template_key]
            if template_id not in project.entities:
                raise ProjectError(f"The selected {template_key} template must be an entity ID.")
            try:
                template = project.resolve(template_id, {pou.kind})
                if template.get("type") != pou.kind:
                    raise ProjectError(
                        f"The selected template for {pou.kind} has kind {template.get('type')}."
                    )
                controller_id = controller(template)
                if controller_id is None:
                    raise ProjectError("The selected POU template has no controller.")
                if controller_id not in known_types_by_controller:
                    known_types_by_controller[controller_id] = Workspace._iec_type_names(
                        project,
                        controller_id,
                        imported_function_blocks.get(controller_id, ()),
                    )
                known_types = known_types_by_controller[controller_id]
                for variable in pou.variables:
                    Workspace._require_iec_type(
                        variable.data_type, variable.scope, known_types
                    )
                if pou.return_type is not None:
                    Workspace._require_iec_type(pou.return_type, "local", known_types)
                body = (
                    {"source": pou.source}
                    if pou.language == "ST"
                    else {
                        "rungs": [
                            native_rung
                            for network in (
                                pou.networks
                                if pou.networks is not None
                                else [pou.network]
                            )
                            for native_rung in native_ladder_rungs(network)
                        ]
                    }
                )
                definition = NativePouDefinition(
                    name=pou.name,
                    **body,
                    variables=pou.variables,
                    return_type=pou.return_type,
                )
                new_updates, change = prepare_pou(project, template_id, definition)
            except ValueError as exc:
                raise ProjectError(str(exc)) from exc
            updates.update(new_updates)
            summaries.append(
                {
                    "name": pou.name,
                    "kind": pou.kind,
                    "language": pou.language,
                    "template_key": template_key,
                    "template_pou_id": template_id,
                    "pou_id": change["pou_id"],
                    "variable_count": len(pou.variables),
                }
            )
            project = NativeProject(
                project.archive.path,
                {**project.archive.overrides, **updates},
            )
        return updates, summaries, global_summaries, type_summaries, project

    def preview_iec_xml_import(
        self,
        transaction_id,
        xml_path,
        template_pou_ids,
        *,
        expected_revision,
        schema_path=None,
        data_type_table_id=None,
    ):
        with self.connect() as connection:
            tx, record = self._transaction(connection, transaction_id, expected_revision)
        patches = self.decode_patches(tx)
        project = NativeProject(record["working_path"], patches)
        source = read_iec_import(xml_path, schema_path)
        pre_import_members = set(project.archive.members)
        try:
            updates, summaries, global_summaries, type_summaries, final_project = self._prepare_iec_xml_pous(
                project,
                source["pous"],
                template_pou_ids,
                source["global_variables"],
                source["data_types"],
                data_type_table_id,
            )
        except (ArchiveError, ProjectError, ValueError) as exc:
            return {
                "status": "blocked",
                "transaction_id": transaction_id,
                "project_id": tx["project_id"],
                "base_revision": tx["base_revision"],
                "source_path": str(source["path"]),
                "source_sha256": source["sha256"],
                "schema_validation": source["schema_validation"],
                "blockers": [{"message": str(exc)}],
                "applied_to_sysmac": False,
                "sysmac_build": "not_executed",
            }
        plan_id = iec_import_plan_id(
            project,
            tx["project_id"],
            tx["base_revision"],
            source["sha256"],
            source["schema_path"],
            template_pou_ids,
            data_type_table_id,
        )
        final_members = set(final_project.archive.members)
        return {
            "status": "ready",
            "transaction_id": transaction_id,
            "project_id": tx["project_id"],
            "base_revision": tx["base_revision"],
            "plan_id": plan_id,
            "source_path": str(source["path"]),
            "source_sha256": source["sha256"],
            "schema_path": str(source["schema_path"]) if source["schema_path"] else None,
            "schema_validation": source["schema_validation"],
            "pous": summaries,
            "global_variables": global_summaries,
            "data_types": type_summaries,
            "predicted_added_members": sorted(final_members - pre_import_members),
            "predicted_changed_members": sorted(
                name for name in updates if name in pre_import_members
            ),
            "staged": False,
            "applied_to_sysmac": False,
            "sysmac_build": "not_executed",
        }

    def stage_iec_xml_import(
        self,
        transaction_id,
        xml_path,
        template_pou_ids,
        expected_source_sha256,
        expected_plan_id,
        *,
        expected_revision,
        schema_path=None,
        data_type_table_id=None,
    ):
        with self.connect() as connection:
            tx, _ = self._transaction(connection, transaction_id, expected_revision)
        project_id = tx["project_id"]
        base_revision = tx["base_revision"]

        def operation(project):
            source = read_iec_import(
                xml_path, schema_path, expected_sha256=expected_source_sha256
            )
            actual_plan_id = iec_import_plan_id(
                project,
                project_id,
                base_revision,
                source["sha256"],
                source["schema_path"],
                template_pou_ids,
                data_type_table_id,
            )
            if actual_plan_id.casefold() != expected_plan_id.casefold():
                raise ProjectError("PLCopen import plan changed since preview.", "plan_changed")
            updates, summaries, global_summaries, type_summaries, _ = self._prepare_iec_xml_pous(
                project,
                source["pous"],
                template_pou_ids,
                source["global_variables"],
                source["data_types"],
                data_type_table_id,
            )
            return updates, {
                "operation": "import_iec_xml_pous",
                "source_path": str(source["path"]),
                "source_sha256": source["sha256"],
                "plan_id": actual_plan_id,
                "schema_validation": source["schema_validation"],
                "pous": summaries,
                "global_variables": global_summaries,
                "data_types": type_summaries,
                "sysmac_import": "not_executed",
                "sysmac_build": "not_executed",
            }

        return self._stage_many(transaction_id, expected_revision, operation, allow_added=True)

    def create_pou(self, transaction_id, template_pou_id, definition, *, expected_revision):
        from .native_pou import NativePouDefinition, prepare_pou

        definition = NativePouDefinition.model_validate(definition)

        def operation(project):
            try:
                return prepare_pou(project, template_pou_id, definition)
            except ValueError as exc:
                raise ProjectError(str(exc)) from exc

        return self._stage_many(transaction_id, expected_revision, operation, allow_added=True)

    def delete_pou(self, transaction_id, pou_id, *, expected_revision):
        from .native_pou import prepare_pou_deletion

        def operation(project):
            try:
                return prepare_pou_deletion(project, pou_id)
            except ValueError as exc:
                raise ProjectError(str(exc)) from exc

        return self._stage_many(transaction_id, expected_revision, operation)

    def delete_data_type(
        self,
        transaction_id,
        data_type_id,
        *,
        expected_name,
        expected_base_type,
        expected_revision,
    ):
        from .native_data_types import prepare_data_type_deletion

        def operation(project):
            try:
                return prepare_data_type_deletion(
                    project, data_type_id, expected_name, expected_base_type
                )
            except ValueError as exc:
                raise ProjectError(str(exc)) from exc

        return self._stage_many(transaction_id, expected_revision, operation)

    def plan_rename_data_type(
        self,
        project_id,
        data_type_id,
        *,
        expected_name,
        expected_base_type,
        new_name,
        transaction_id=None,
    ):
        from .native_data_types import prepare_data_type_rename

        try:
            plan, _ = prepare_data_type_rename(
                self.project(project_id, transaction_id),
                data_type_id,
                expected_name,
                expected_base_type,
                new_name,
            )
            return plan
        except ValueError as exc:
            raise ProjectError(str(exc)) from exc

    def rename_data_type(
        self,
        transaction_id,
        data_type_id,
        *,
        expected_name,
        expected_base_type,
        new_name,
        expected_revision,
    ):
        from .native_data_types import prepare_data_type_rename

        def operation(project):
            try:
                plan, patches = prepare_data_type_rename(
                    project,
                    data_type_id,
                    expected_name,
                    expected_base_type,
                    new_name,
                )
            except ValueError as exc:
                raise ProjectError(str(exc)) from exc
            if not plan["can_apply"]:
                codes = ", ".join(dict.fromkeys(item["code"] for item in plan["blockers"]))
                raise ProjectError(f"DataType rename blocked: {codes}")
            return patches, {
                "operation": "rename_data_type",
                "data_type_id": data_type_id,
                "old_name": plan["old_name"],
                "new_name": new_name,
                "reference_count": len(plan["references"]),
                "changed_members": plan["changed_members"],
                "external_consumers": "not_acquired",
                "sysmac_build": "not_executed",
            }

        return self._stage_many(transaction_id, expected_revision, operation)

    def assign_program(
        self, transaction_id, task_id, program_id, *, position=None, startup=None, expected_revision
    ):
        from .task_model import prepare_assignment

        def operation(project):
            try:
                return prepare_assignment(
                    project, task_id, program_id, position=position, startup=startup
                )
            except ValueError as exc:
                raise ProjectError(str(exc)) from exc

        return self._stage_many(transaction_id, expected_revision, operation, allow_added=True)

    def unassign_program(self, transaction_id, task_id, program_id, *, expected_revision):
        from .task_model import prepare_unassignment

        def operation(project):
            try:
                return prepare_unassignment(project, task_id, program_id)
            except ValueError as exc:
                raise ProjectError(str(exc)) from exc

        return self._stage_many(transaction_id, expected_revision, operation)

    def update_ld(self, transaction_id, section_id, rungs, *, expected_revision):
        if not isinstance(rungs, list) or not rungs:
            raise ProjectError("Provide a non-empty list of native Ladder rung objects.")
        for rung in rungs:
            if (
                not isinstance(rung, dict)
                or not isinstance(rung.get("CLs"), list)
                or not all(
                    isinstance(e, dict) and isinstance(e.get("__type"), str) for e in rung["CLs"]
                )
            ):
                raise ProjectError("Invalid native Ladder rung structure.")
        try:
            encoded = b"\xef\xbb\xbf" + (
                "\r\n".join(
                    json.dumps(r, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                    for r in rungs
                )
                + "\r\n"
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ProjectError("Ladder data must be finite JSON values.") from exc

        def operation(project):
            entity = project.resolve(section_id, {"PouBody"})
            if project.read_section(entity)["language"] != "LD":
                raise ProjectError("Selected section is not a recognised native Ladder section.")
            from .external_dependencies import register_new_dependencies

            try:
                updates, registrations = register_new_dependencies(
                    project, {project.member(entity.get("id")): encoded}, project.owner(entity)
                )
            except ValueError as exc:
                raise ProjectError(str(exc)) from exc
            return updates, {
                "operation": "update_ld",
                "section_id": entity.get("id"),
                "registered_externals": registrations,
            }

        return self._stage_many(transaction_id, expected_revision, operation)

    def update_variable(self, transaction_id, variable_id, changes, *, expected_revision):
        if "data_type" in changes and (
            not isinstance(changes["data_type"], str) or not changes["data_type"].strip()
        ):
            raise ProjectError("data_type is required and cannot be empty or removed.")
        mapping = {"data_type": "D", "initial_value": "IV", "retained": "R", "comment": "Com"}
        if not changes or set(changes) - mapping.keys():
            raise ProjectError(
                "Supported fields: data_type, initial_value, retained, comment. Use bind_io for addresses."
            )

        def operation(project):
            matches = [v for v in project.variables() if v["id"] == variable_id]
            if len(matches) != 1:
                raise ProjectError("Variable ID not found; list variables again.")
            variable = matches[0]
            if variable["attributes"].get("RO") == "1":
                raise ProjectError("Variable is read-only in the native model.")
            if (
                variable["address"]
                and "data_type" in changes
                and clean_field(changes["data_type"]).strip().casefold()
                != variable["data_type"].strip().casefold()
            ):
                raise ProjectError("Use unbind_io before changing the type of a bound variable.")
            raw = project.archive.read(variable["member"])
            text = raw.decode("utf-8-sig")
            ending = "\r\n" if "\r\n" in text else "\n"
            lines = text.splitlines()
            fields = variable["attributes"].copy()
            for key, value in changes.items():
                field = mapping[key]
                if value is None:
                    fields.pop(field, None)
                elif key == "retained":
                    if not isinstance(value, bool):
                        raise ProjectError("retained must be boolean.")
                    fields[field] = "1" if value else "0"
                else:
                    fields[field] = clean_field(value)
                    if key == "data_type" and not fields[field].strip():
                        raise ProjectError("data_type cannot be empty.")
            lines[variable["line_index"]] = "++" + "\t".join(f"{k}={v}" for k, v in fields.items())
            bom = b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b""
            output = bom + (
                ending.join(lines) + (ending if text.endswith(("\r", "\n")) else "")
            ).encode("utf-8")
            return (
                variable["member"],
                output,
                {
                    "operation": "update_variable",
                    "variable_id": variable_id,
                    "fields": sorted(changes),
                },
            )

        return self._stage(transaction_id, expected_revision, operation)

    def delete_variable(self, transaction_id, variable_id, *, expected_revision):
        def operation(project):
            matches = [variable for variable in project.variables() if variable["id"] == variable_id]
            if len(matches) != 1:
                raise ProjectError("Variable ID is not uniquely resolvable; list variables again.")
            variable = matches[0]
            if variable["attributes"].get("RO") == "1":
                raise ProjectError("Variable is read-only in the native model.")
            if variable["address"]:
                raise ProjectError("Unbind the variable with plc_unbind_io before deleting it.")
            if variable["group"] not in {
                "VAR_GLOBAL",
                "VAR",
                "VAR_INPUT",
                "VAR_OUTPUT",
                "VAR_IN_OUT",
                "VAR_TEMP",
                "VAR_EXTERNAL",
            }:
                raise ProjectError("Variable declaration group is not supported for deletion.")

            from .st_lexer import lex
            from .symbols import SymbolIndex, controller

            index = SymbolIndex(project)
            analysis = index.analyze(variable_id)
            indexed_variable = index.by_id[variable_id]
            family = {declaration["id"] for declaration in analysis["declarations"]}
            if variable["scope"] == "global":
                if any(
                    declaration["id"] != variable_id
                    and declaration["group"] == "VAR_EXTERNAL"
                    for declaration in analysis["declarations"]
                ):
                    raise ProjectError(
                        "Remove VAR_EXTERNAL aliases before deleting their global declaration."
                    )
                affected_pous = {
                    pou["id"]
                    for pou in index.pous
                    if controller(project.entities.get(pou["id"]))
                    == indexed_variable["controller_id"]
                }
                expression_variables = [
                    item
                    for item in index.variables
                    if item["controller_id"] == indexed_variable["controller_id"]
                ]
            else:
                affected_pous = {variable["scope"]}
                expression_variables = [
                    item for item in index.variables if item["scope"] == variable["scope"]
                ]

            unavailable = [
                section
                for section in index.sections
                if section["pou_id"] in affected_pous
                and section["completeness"] != "observed"
            ]
            if unavailable:
                raise ProjectError(
                    "Cannot safely delete a variable while affected source sections are unavailable."
                )
            diagnostics = [
                diagnostic
                for diagnostic in index.diagnostics
                if diagnostic.get("pou_id") in affected_pous
                or family.intersection(diagnostic.get("variable_ids", []))
            ]
            if diagnostics:
                raise ProjectError(
                    "Cannot safely delete a variable while affected symbol diagnostics remain."
                )
            references = [
                reference
                for reference in analysis["references"]
                if reference.get("pou_id") in affected_pous
            ]
            if references:
                raise ProjectError(
                    "Variable has observed source references; remove them in the transaction before deleting it."
                )
            unresolved = [
                occurrence
                for occurrence in index.occurrences
                if occurrence.get("pou_id") in affected_pous
                and occurrence.get("spelling", "").casefold() == variable["name"].casefold()
                and occurrence.get("kind")
                in {"unresolved", "ambiguous", "unresolved_member", "unresolved_parameter"}
            ]
            if unresolved:
                raise ProjectError(
                    "Variable name appears as an unresolved or ambiguous source reference."
                )

            for item in expression_variables:
                expressions = [item["data_type"], item.get("initial_value") or ""]
                for expression in expressions:
                    parsed = lex(expression)
                    if parsed.diagnostics:
                        raise ProjectError(
                            "A declaration expression cannot be completely analyzed; variable deletion is unsafe."
                        )
                    if any(
                        token.kind == "identifier"
                        and token.text.casefold() == variable["name"].casefold()
                        for token in parsed.tokens
                    ):
                        raise ProjectError(
                            "Variable is referenced by a declaration type or initial-value expression."
                        )

            raw = project.archive.read(variable["member"])
            bom = b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b""
            payload = raw[len(bom) :]
            lines = payload.splitlines(keepends=True)
            line_index = variable["line_index"]
            if line_index >= len(lines):
                raise ProjectError("Variable declaration changed; list variables again.")
            line = lines[line_index].decode("utf-8").rstrip("\r\n")
            if not line.startswith("++"):
                raise ProjectError("Variable declaration changed; list variables again.")
            fields = dict(
                part.split("=", 1) for part in line[2:].split("\t") if "=" in part
            )
            if fields.get("N") != variable["name"] or fields.get("G") != variable["group"]:
                raise ProjectError("Variable declaration changed; list variables again.")
            lines.pop(line_index)
            return (
                variable["member"],
                bom + b"".join(lines),
                {
                    "operation": "delete_variable",
                    "variable_id": variable_id,
                    "name": variable["name"],
                    "scope": variable["scope"],
                    "group": variable["group"],
                    "external_consumers": "not_acquired",
                },
            )

        return self._stage(transaction_id, expected_revision, operation)

    def create_variable(
        self, transaction_id, table_id, name, data_type, *, initial_value=None, expected_revision
    ):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,126}", name):
            raise ProjectError(
                "Use an IEC identifier of 1–127 ASCII letters, digits or underscores."
            )
        dtype = clean_field(data_type)
        if not dtype.strip():
            raise ProjectError("data_type cannot be empty.")
        initial = clean_field(initial_value) if initial_value is not None else None

        def operation(project):
            table = project.resolve(table_id, {"Variables"})
            actual_id = table.get("id")
            owner = project.owner(table) or "global"
            if any(v["name"].casefold() == name.casefold() for v in project.variables(owner)):
                raise ProjectError("A variable with this name already exists in this scope.")
            member = project.member(actual_id)
            raw = project.archive.read(member)
            text = raw.decode("utf-8-sig")
            if not text.startswith("[SLWD version=1.0]"):
                raise ProjectError("Only observed SLWD variable tables can be edited.")
            group = "VAR_GLOBAL" if table.get("subtype") == "Global" else "VAR"
            lines = text.splitlines()
            heading = next(
                (i for i, line in enumerate(lines) if line.split("\t")[0] == "+GN=" + group), None
            )
            if heading is None:
                raise ProjectError(
                    "Target declaration group missing; refusing to infer a namespace."
                )
            end = next(
                (
                    i
                    for i in range(heading + 1, len(lines))
                    if lines[i].startswith("+") and not lines[i].startswith("++")
                ),
                len(lines),
            )
            fields = {"D": dtype, "N": name, "G": group}
            if initial is not None:
                fields["IV"] = initial
            lines.insert(end, "++" + "\t".join(f"{k}={v}" for k, v in fields.items()))
            ending = "\r\n" if "\r\n" in text else "\n"
            bom = b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b""
            return (
                member,
                bom + (ending.join(lines) + ending).encode("utf-8"),
                {"operation": "create_variable", "table_id": actual_id, "name": name},
            )

        return self._stage(transaction_id, expected_revision, operation)

    def bind_io(self, transaction_id, variable_id, address, *, expected_revision):
        address = clean_field(address)

        def operation(project):
            variables = project.variables()
            targets = [v for v in variables if v["id"] == variable_id and v["scope"] == "global"]
            if len(targets) != 1:
                raise ProjectError("Select an existing global variable ID.")
            target = targets[0]
            if target["attributes"].get("RO") == "1":
                raise ProjectError("Variable is read-only in the native model.")
            with self.connect() as connection:
                tx, record = self._transaction(connection, transaction_id)
            baseline = NativeProject(record["snapshot_path"])
            known = [v for v in baseline.variables() if v["address"] == address]
            hardware = project.hardware_inventory(self.profile_root)
            described = [p for p in hardware["ports"] if p["address"] == address]
            configured_modules = [
                m
                for m in hardware["modules"]
                if m["address_prefix"] and address.startswith(m["address_prefix"])
            ]
            if configured_modules and not any(
                p["source_member"] and p["enabled"] for p in described
            ):
                raise ProjectError("Address is not part of enabled configured hardware/PDOs.")
            if any(p["enabled"] is False for p in described):
                raise ProjectError("Port must belong to enabled configured hardware.")
            if any(p["source_member"] and not p["identity_valid"] for p in described):
                raise ProjectError("Port identity is missing or ambiguous.")
            if any(
                p["profile_path"]
                and p["data_type"]
                and p["data_type"].strip().casefold() != target["data_type"].strip().casefold()
                for p in described
            ):
                raise ProjectError("Port data type does not match the target variable.")
            if any(p["profile_path"] and not p["configuration_resolved"] for p in described):
                raise ProjectError("Port configuration cannot be resolved unambiguously.")
            if not known:
                ports = [p for p in described if p["configuration_resolved"]]
                if len(ports) != 1 or not ports[0]["enabled"]:
                    raise ProjectError(
                        "Address not observed or resolved from enabled configured hardware."
                    )
                if (
                    ports[0]["data_type"].strip().casefold()
                    != target["data_type"].strip().casefold()
                ):
                    raise ProjectError("Port data type does not match the target variable.")
            if any(v["address"] == address and v["id"] != variable_id for v in variables):
                raise ProjectError("Port is already assigned to another variable.")
            if any(
                b["variable_id"] != variable_id
                for p in described
                for b in p["overlapping_bindings"]
            ):
                raise ProjectError("Output address would overlap another variable binding.")
            if not any(
                p["profile_path"] and p["configuration_resolved"] for p in described
            ) and any(v["data_type"].casefold() != target["data_type"].casefold() for v in known):
                raise ProjectError("Port data type does not match the target variable.")
            raw = project.archive.read(target["member"])
            text = raw.decode("utf-8-sig")
            lines = text.splitlines()
            fields = target["attributes"].copy()
            fields["AT"] = address
            lines[target["line_index"]] = "++" + "\t".join(f"{k}={v}" for k, v in fields.items())
            ending = "\r\n" if "\r\n" in text else "\n"
            bom = b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b""
            return (
                target["member"],
                bom + (ending.join(lines) + ending).encode("utf-8"),
                {
                    "operation": "bind_io",
                    "variable_id": variable_id,
                    "address": address,
                    "hardware_verified": False,
                },
            )

        return self._stage(transaction_id, expected_revision, operation)

    def unbind_io(self, transaction_id, variable_id, *, expected_revision):
        def operation(project):
            matches = [v for v in project.variables("global") if v["id"] == variable_id]
            if len(matches) != 1:
                raise ProjectError("Select an existing global variable ID.")
            variable = matches[0]
            if variable["attributes"].get("RO") == "1":
                raise ProjectError("Variable is read-only in the native model.")
            raw = project.archive.read(variable["member"])
            text = raw.decode("utf-8-sig")
            lines = text.splitlines()
            fields = variable["attributes"].copy()
            fields.pop("AT", None)
            lines[variable["line_index"]] = "++" + "\t".join(f"{k}={v}" for k, v in fields.items())
            ending = "\r\n" if "\r\n" in text else "\n"
            bom = b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b""
            return (
                variable["member"],
                bom + (ending.join(lines) + ending).encode("utf-8"),
                {
                    "operation": "unbind_io",
                    "variable_id": variable_id,
                    "previous_address": variable["address"],
                },
            )

        return self._stage(transaction_id, expected_revision, operation)

    def preview(self, transaction_id):
        with self.connect() as connection:
            tx, record = self._transaction(connection, transaction_id)
        patches = self.decode_patches(tx)
        existing = set(ProjectArchive(record["working_path"]).names)
        return {
            "transaction_id": transaction_id,
            "project_id": tx["project_id"],
            "base_revision": tx["base_revision"],
            "changed_entries": sorted(patches.keys() & existing),
            "added_entries": sorted(patches.keys() - existing),
            "changes": json.loads(tx["changes"]),
            "applied_to_sysmac": False,
            "validation": {"st_syntax": "not_executed", "sysmac_build": "not_executed"},
        }

    def validate(self, transaction_id):
        from .external_dependencies import external_diagnostics, missing_externals
        from .native_data_types import layout_diagnostics
        from .symbols import SymbolIndex
        from .typecheck import controller_scalar_alias_families, scalar_type_diagnostics

        with self.connect() as connection:
            tx, _ = self._transaction(connection, transaction_id)
        project = self.project(tx["project_id"], transaction_id)
        info = project.info()
        data_type_layout = layout_diagnostics(project)
        index = SymbolIndex(project)
        missing = missing_externals(project, index=index)
        diagnostics = external_diagnostics(index)
        project_variables = index.variables
        has_initializers = any(item.get("initial_value") is not None for item in project_variables)
        type_aliases, type_alias_coverage = controller_scalar_alias_families(
            project, project_variables
        )
        type_diagnostics, type_omitted = scalar_type_diagnostics(
            index, project_variables, aliases=type_aliases
        )
        external_codes = {"external_type_mismatch", "external_target_unresolved"}
        source_diagnostics = [
            item for item in index.diagnostics if item.get("code") not in external_codes
        ]
        source_diagnostics.sort(
            key=lambda item: (
                item.get("pou_id", ""),
                item.get("section_id", ""),
                item.get("path") or "",
                item.get("start", -1),
                item.get("code", ""),
            )
        )
        observed_languages = {
            section["language"]
            for section in index.sections
            if section["completeness"] == "observed"
        }
        return {
            "container_models": "parsed",
            "coverage": info["coverage"],
            "st_syntax": "partial_lexer_and_delimiter_checks"
            if "ST" in observed_languages
            else "not_executed",
            "type_check": "partial_scalar_checks_with_controller_aliases"
            if "ST" in observed_languages or has_initializers
            else "not_executed",
            "type_check_coverage": {
                "completeness": "partial"
                if "ST" in observed_languages or has_initializers
                else "not_executed",
                "checked": (
                    "direct assignments and bounded scalar expressions using numeric arithmetic, "
                    "Boolean logic, matching-family equality, numeric ordering, and parentheses "
                    "(up to 256 tokens and 32 nested parentheses), scalar variable initializers, "
                    "and built-in integer literal range checks. Category inference uses known scalar "
                    "IEC types and unambiguous aliases from complete controller tables"
                ),
                "certainty": "definite type-category mismatches only",
                "controller_scalar_aliases": type_alias_coverage,
                "not_checked": [
                    "function or function-block calls",
                    "expressions outside the bounded grammar or its token and nesting limits",
                    "members and array elements",
                    "user-defined enums, structures, unions, arrays, pointers, references, and library types",
                    "implicit conversions, floating-point ranges, and user-defined range constraints",
                    "compound or unsupported initializer forms",
                ],
            },
            "type_diagnostic_count": len(type_diagnostics) + type_omitted,
            "type_diagnostics_preview": type_diagnostics[:50],
            "type_diagnostics_omitted_count": max(0, len(type_diagnostics) - 50) + type_omitted,
            "native_data_type_layout_check": data_type_layout["check"],
            "native_data_type_layout_coverage": data_type_layout["coverage"],
            "native_data_type_layout_diagnostic_count": len(data_type_layout["diagnostics"]),
            "native_data_type_layout_diagnostics_preview": data_type_layout["diagnostics"][:50],
            "native_data_type_layout_diagnostics_omitted_count": max(
                0, len(data_type_layout["diagnostics"]) - 50
            ),
            "ladder_semantics": "not_executed",
            "ladder_instruction_check": "partial_unsupported_instruction_scan"
            if "LD" in observed_languages
            else "not_executed",
            "sysmac_build": "not_executed",
            "source_diagnostic_count": len(source_diagnostics),
            "source_diagnostics_preview": source_diagnostics[:50],
            "source_diagnostics_omitted_count": max(0, len(source_diagnostics) - 50),
            "external_declaration_check": "missing_declarations"
            if missing
            else "unresolved_external_declarations"
            if diagnostics
            else "passed_observed_code",
            "missing_external_count": len(missing),
            "missing_externals_preview": missing[:50],
            "external_diagnostic_count": len(diagnostics),
            "external_diagnostics_preview": diagnostics[:50],
        }

    def commit(self, transaction_id):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            tx, record = self._transaction(connection, transaction_id)
            source = Path(record["source_path"])
            snapshot = Path(record["snapshot_path"])
            if (
                not source.is_file()
                or sha256(source) != record["sha256"]
                or sha256(snapshot) != record["sha256"]
            ):
                raise ProjectError(
                    "The original project changed; reopen and review before committing.",
                    "source_changed",
                )
            patches = self.decode_patches(tx)
            if not patches:
                raise ProjectError("No changes staged.")
            project = NativeProject(record["working_path"], patches)
            project.info()  # Parse the staged models before writing.
            revisions = self.root / "revisions" / record["id"]
            revisions.mkdir(parents=True, exist_ok=True)
            target = revisions / (uuid.uuid4().hex + ".smc2")
            with ZipFile(record["working_path"]) as before, ZipFile(target, "x") as after:
                added = set(patches) - set(before.namelist())
                after.comment = before.comment
                for info in before.infolist():
                    after.writestr(copy.copy(info), patches.get(info.filename, before.read(info)))
                for name in sorted(added):
                    after.writestr(name, patches[name], compress_type=ZIP_DEFLATED)
            delta = compare_files(record["working_path"], target)
            if (
                set(delta["added_entries"]) != added
                or delta["removed_entries"]
                or set(delta["changed_entries"]) - patches.keys()
            ):
                raise ProjectError("Unexpected archive changes; revision not committed.")
            NativeProject(target).info()
            revision = record["revision"] + 1
            connection.execute(
                "UPDATE projects SET working_path=?, revision=? WHERE id=?",
                (str(target), revision, record["id"]),
            )
            connection.execute(
                "UPDATE transactions SET status='committed' WHERE id=?", (transaction_id,)
            )
        return {
            "status": "committed",
            "revision": revision,
            "project_id": record["id"],
            "changed_entries": delta["changed_entries"],
            "added_entries": delta["added_entries"],
            "applied_to_sysmac": False,
            "sysmac_build": "not_executed",
            "export_status": "candidate_unverified",
        }

    def rollback(self, transaction_id):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            tx = connection.execute(
                "SELECT * FROM transactions WHERE id=?", (transaction_id,)
            ).fetchone()
            if tx is None or tx["status"] != "active":
                raise ProjectError("Transaction is not active.")
            connection.execute(
                "UPDATE transactions SET status='rolled_back' WHERE id=?", (transaction_id,)
            )
        return {"transaction_id": transaction_id, "status": "rolled_back"}

    def export(self, project_id, destination):
        target = Path(destination).resolve()
        with self.connect() as connection:
            record = self._record(connection, project_id)
        if target == Path(record["source_path"]) or target.exists():
            raise ProjectError(
                "Destination already exists or is the original project; choose a new file."
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with Path(record["working_path"]).open("rb") as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
        except FileExistsError as exc:
            raise ProjectError("Destination already exists.") from exc
        return {
            "status": "candidate_unverified",
            "path": str(target),
            "revision": record["revision"],
            "sha256": sha256(target),
            "sysmac_build": "not_executed",
            "original_sha256": record["sha256"],
        }
