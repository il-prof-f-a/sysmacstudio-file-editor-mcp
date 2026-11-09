"""Read-only comparison of observed native models, with all member deltas retained."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

from .archive import ArchiveError
from .project import NativeProject, parse_xml
from .task_model import tracking

OEM_METADATA = {"id", "trackingId", "dateCreated", "dateLastModified", "version"}


def _json(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _file_digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _brief(value):
    if isinstance(value, str) and len(value) > 240:
        return value[:240] + "… [sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest() + "]"
    if isinstance(value, dict):
        return {k: _brief(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_brief(v) for v in value[:20]] + (
            [{"truncated_items": len(value) - 20}] if len(value) > 20 else []
        )
    return value


def _path(entity):
    parents = []
    while entity is not None:
        if entity.tag == "Entity" and entity.get("type") != "Solution":
            parents.append((entity.get("type"), entity.get("subtype"), entity.get("name")))
        entity = entity.getparent()
    return list(reversed(parents))


def _xml_shape(node, *, normalized_rows=()):
    children = [
        _xml_shape(c, normalized_rows=normalized_rows) for c in node if isinstance(c.tag, str)
    ]
    text = node.text or ""
    if len(node) and not text.strip():
        text = ""
    tail = node.tail or ""
    if not tail.strip():
        tail = ""
    attributes = dict(node.attrib)
    if node in normalized_rows:
        attributes = {}  # Row attributes are represented by normalized program assignments.
    return {
        "tag": node.tag,
        "attributes": attributes,
        "text": text,
        "tail": tail,
        "children": children,
    }


class _Snapshot:
    def __init__(self, path):
        self.path = Path(path).resolve(strict=True)
        self.sha256 = _file_digest(self.path)
        self.project = project = NativeProject(self.path)
        self.records = defaultdict(list)
        self.paths = {}
        self.info = project.info()
        self.unacquired = []
        self.task_diagnostics = []
        for entity in project.entities.values():
            if entity.get("type") == "Solution":
                continue
            attrs = {k: v for k, v in entity.attrib.items() if k not in OEM_METADATA}
            self.add(
                "object",
                _path(entity),
                attrs,
                {"type": entity.get("type"), "name": entity.get("name")},
            )
        for device in self.info["controllers"]:
            self.add(
                "controller",
                _path(project.entities[device["id"]]),
                {k: v for k, v in device.items() if k != "id"},
                {"model": device["model"], "unit_version": device["unit_version"]},
            )
        variables = project.variables()
        for variable in variables:
            identity = _path(project.entities[variable["table_id"]]) + [
                ("Variable", variable["group"], variable["name"].casefold())
            ]
            self.add(
                "variable",
                identity,
                variable["attributes"],
                {
                    "name": variable["name"],
                    "group": variable["group"],
                    "data_type": variable["data_type"],
                    "address": variable["address"],
                    "initial_value": variable["initial_value"],
                    "retained": variable["retained"],
                },
            )
        for entity in project.entities.values():
            if entity.get("type") != "Variables":
                continue
            member = project.member(entity.get("id"))
            observed = member in project.recognized if member else False
            model = {"status": "observed" if observed else "unavailable"}
            if not observed:
                model["opaque_sha256"] = (
                    hashlib.sha256(project.archive.read(member)).hexdigest() if member else None
                )
            self.add("variable_table", _path(entity), model, model)
        for pou in project.pous():
            for section in project.read_pou(pou["id"])["sections"]:
                model = {"language": section["language"], "completeness": section["completeness"]}
                summary = dict(model)
                if section["language"] == "ST":
                    model["source"] = section["source"]
                    summary["line_count"] = len(section["source"].splitlines())
                elif section["language"] == "LD":
                    model["rungs"] = section["rungs"]
                    summary["rung_count"] = len(section["rungs"])
                else:
                    member = section["member"]
                    model["opaque_sha256"] = (
                        hashlib.sha256(project.archive.read(member)).hexdigest() if member else None
                    )
                    self.unacquired.append(
                        {
                            "object_path": _path(project.entities[section["id"]]),
                            "status": section["completeness"],
                        }
                    )
                self.add("section", _path(project.entities[section["id"]]), model, summary)
        for task in project.tasks():
            entity = project.entities[task["id"]]
            member = project.member(task["id"])
            programs = []
            for row in task["programs"]:
                normalized = {
                    k: v
                    for k, v in row.items()
                    if k not in {"IniFileTrackingId", "program_id", "association_id"}
                }
                program = project.entities.get(row["program_id"])
                normalized["program_path"] = _path(program) if program is not None else None
                if program is None or tracking(row.get("IniFileTrackingId")) is None:
                    normalized["unresolved_tracking"] = row.get("IniFileTrackingId")
                programs.append(normalized)
            settings = None
            if member:
                root = parse_xml(project.archive.read(member))
                containers = root.findall("Programs")
                normalized_rows = (
                    frozenset(containers[0].findall("AssociatedProgramData"))
                    if len(containers) == 1
                    else frozenset()
                )
                settings = _xml_shape(root, normalized_rows=normalized_rows)
            model = {
                "programs": programs,
                "diagnostics": task["diagnostics"],
                "settings": settings,
            }
            self.add(
                "task",
                _path(entity),
                model,
                {"program_count": len(programs), "diagnostics": task["diagnostics"]},
            )
            if task["diagnostics"]:
                self.task_diagnostics.append(
                    {"object_path": _path(entity), "codes": task["diagnostics"]}
                )
        self.hashes = project.archive.hashes()
        self.assert_unchanged()

    def add(self, category, path, model, summary):
        key = (category, _json(path))
        self.paths[key] = path
        self.records[key].append({"sha256": _digest(model), "summary": _brief(summary)})

    def assert_unchanged(self):
        if _file_digest(self.path) != self.sha256:
            raise ArchiveError("Input changed while comparing projects; retry with stable copies.")

    def coverage(self):
        ambiguous = [
            {"category": k[0], "object_path": self.paths[k], "count": len(v)}
            for k, v in self.records.items()
            if len(v) > 1
        ]
        return _brief(
            {
                "record_count": sum(map(len, self.records.values())),
                "ambiguous_key_count": len(ambiguous),
                "ambiguous_keys_preview": ambiguous[:20],
                "unacquired_section_count": len(self.unacquired),
                "unacquired_sections_preview": self.unacquired[:20],
                "unknown_member_count": len(self.info["coverage"]["unknown_entries"]),
                "task_diagnostic_count": len(self.task_diagnostics),
                "task_diagnostics_preview": self.task_diagnostics[:20],
            }
        )


def compare_projects(before, after, *, offset=0, limit=100):
    """Compare acquired models and member hashes; never infer build or functional equivalence."""
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("Use offset >= 0 and limit between 1 and 200.")
    a, b = _Snapshot(before), _Snapshot(after)
    differences = []
    categories = Counter()
    for key in sorted(a.records.keys() | b.records.keys()):
        left, right = a.records.get(key, []), b.records.get(key, [])
        if sorted(v["sha256"] for v in left) == sorted(v["sha256"] for v in right):
            continue
        categories[key[0]] += 1
        differences.append(
            {
                "domain": "model",
                "category": key[0],
                "object_path": _brief(a.paths.get(key, b.paths.get(key))),
                "change": "added" if not left else "removed" if not right else "modified",
                "before": left[:20],
                "after": right[:20],
                "before_count": len(left),
                "after_count": len(right),
            }
        )
    model_count = len(differences)
    for member in sorted(a.hashes.keys() | b.hashes.keys()):
        left, right = a.hashes.get(member), b.hashes.get(member)
        if left == right:
            continue
        differences.append(
            {
                "domain": "archive",
                "member": _brief(member),
                "member_name_sha256": hashlib.sha256(member.encode("utf-8")).hexdigest(),
                "member_name_length": len(member),
                "change": "added" if left is None else "removed" if right is None else "modified",
                "before_sha256": left,
                "after_sha256": right,
                "before_recognized": member in a.project.recognized,
                "after_recognized": member in b.project.recognized,
            }
        )
    coverage_a, coverage_b = a.coverage(), b.coverage()
    a.assert_unchanged()
    b.assert_unchanged()
    end = offset + limit
    return {
        "before_sha256": a.sha256,
        "after_sha256": b.sha256,
        "identical_bytes": a.sha256 == b.sha256,
        "observed_models_equal": None
        if coverage_a["ambiguous_key_count"] or coverage_b["ambiguous_key_count"]
        else model_count == 0,
        "model_difference_count": model_count,
        "model_difference_categories": dict(categories),
        "archive_difference_count": len(differences) - model_count,
        "unchanged_member_count": sum(
            a.hashes[n] == b.hashes[n] for n in a.hashes.keys() & b.hashes.keys()
        ),
        "items": differences[offset:end],
        "total": len(differences),
        "next_offset": end if end < len(differences) else None,
        "semantic_equivalence": "not_verified",
        "round_trip_verified": False,
        "coverage": {
            "complete": False,
            "identity": "logical_entity_path_case_sensitive_variable_name_case_insensitive",
            "ignored_oem_attributes": sorted(OEM_METADATA),
            "st": "exact_decoded_text",
            "ld": "full_native_rung_objects",
            "hardware": "raw_members_only",
            "unmodeled_fields": "may_exist_even_in_recognized_members",
            "zip_metadata": "not_compared_as_models",
            "before": coverage_a,
            "after": coverage_b,
        },
    }
