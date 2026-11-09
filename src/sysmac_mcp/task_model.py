"""Link native task rows and OEM associations, then stage coherent program moves."""

from __future__ import annotations

from uuid import UUID, uuid4

from lxml import etree as ET

from .project import NativeProject, parse_xml, serialize_xml
from .symbols import controller

ASSOCIATION_NS = (
    "http://schemas.datacontract.org/2004/07/Omron.Cxap.Modules.TaskConfiguration.Models"
)


def tracking(value):
    try:
        return UUID(value).hex
    except (ValueError, TypeError, AttributeError):
        return None


def _states(project, cid=None):
    programs = [e for e in project.entities.values() if e.get("type") == "Program"]
    result = []
    for entity in project.entities.values():
        if entity.get("type") != "NexTask" or cid is not None and controller(entity) != cid:
            continue
        member = project.member(entity.get("id"))
        issues, rows = [], []
        state = {
            "entity": entity,
            "member": member,
            "issues": issues,
            "rows": rows,
            "root": None,
            "container": None,
        }
        result.append(state)
        if not member:
            issues.append("task_model_missing")
            continue
        root = parse_xml(project.archive.read(member))
        state["root"] = root
        containers = root.findall("Programs")
        if len(containers) != 1:
            issues.append("task_program_container_unsupported")
            continue
        container = state["container"] = containers[0]
        if any(child.tag != "AssociatedProgramData" for child in container):
            issues.append("unsupported_task_program_element")
        associations = entity.findall('./ChildEntities/Entity[@type="NexAssociatedProgram"]')
        used = set()
        for row in container.findall("AssociatedProgramData"):
            row_issues = []
            local_programs = [p for p in programs if controller(p) == controller(entity)]
            key = tracking(row.get("IniFileTrackingId"))
            candidates = (
                [p for p in local_programs if tracking(p.get("trackingId")) == key]
                if key
                else [
                    p
                    for p in local_programs
                    if p.get("name", "").casefold() == row.get("ProgramName", "").casefold()
                ]
            )
            program = candidates[0] if len(candidates) == 1 else None
            if program is None:
                row_issues.append("program_identity_unresolved")
            elif program.get("name", "").casefold() != row.get("ProgramName", "").casefold():
                row_issues.append("program_name_tracking_mismatch")
            if key is None:
                row_issues.append("task_program_tracking_missing")
            instance = row.get("InstanceName", row.get("ProgramName", ""))
            matches = [
                a for a in associations if a.get("name", "").casefold() == instance.casefold()
            ]
            association = matches[0] if len(matches) == 1 else None
            if association is None or association.get("id") in used:
                row_issues.append("oem_task_association_unresolved")
            else:
                used.add(association.get("id"))
                model_member = project.member(association.get("id"))
                model = parse_xml(project.archive.read(model_member)) if model_member else None
                values = (
                    [n.text for n in model.findall(f"{{{ASSOCIATION_NS}}}PouInstanceName")]
                    if model is not None
                    else []
                )
                if (
                    model is None
                    or model.tag != f"{{{ASSOCIATION_NS}}}AssociatedProgramModel"
                    or len(values) != 1
                    or values[0] != instance
                ):
                    row_issues.append("association_model_instance_mismatch")
                elif model_member:
                    project.recognized.add(model_member)
            try:
                sequence = int(row.get("SequenceNumber", ""))
                if sequence < 1:
                    raise ValueError
            except ValueError:
                sequence = 0
                row_issues.append("program_sequence_invalid")
            rows.append(
                {
                    "node": row,
                    "program": program,
                    "association": association,
                    "sequence": sequence,
                    "issues": row_issues,
                }
            )
            issues.extend(row_issues)
        if len(used) != len(associations):
            issues.append("orphan_oem_task_association")
        if len({r["sequence"] for r in rows}) != len(rows):
            issues.append("program_sequence_ambiguous")
        rows.sort(key=lambda r: r["sequence"])
        project.recognized.add(member)
    return result


def read_tasks(project):
    return [
        {
            "id": state["entity"].get("id"),
            "name": state["entity"].get("name"),
            "controller_id": controller(state["entity"]),
            "programs": [
                {
                    **dict(r["node"].attrib),
                    "program_id": r["program"].get("id") if r["program"] is not None else None,
                    "association_id": r["association"].get("id")
                    if r["association"] is not None
                    else None,
                }
                for r in state["rows"]
            ],
            "diagnostics": sorted(set(state["issues"])),
            "sysmac_build": "not_executed",
        }
        for state in _states(project)
    ]


def prepare_assignment(project, task_id, program_id, *, position=None, startup=None):
    task = project.resolve(task_id, {"NexTask"})
    program = project.resolve(program_id, {"Program"})
    if controller(task) != controller(program):
        raise ValueError("Task and program must belong to the same controller.")
    program_tracking = tracking(program.get("trackingId"))
    if program_tracking is None:
        raise ValueError("Program needs a known tracking identity for task assignment.")
    if (
        sum(
            e.get("type") == "Program"
            and controller(e) == controller(program)
            and tracking(e.get("trackingId")) == program_tracking
            for e in project.entities.values()
        )
        != 1
    ):
        raise ValueError("Program tracking identity must be unique in its controller.")
    if startup is not None and not isinstance(startup, bool):
        raise ValueError("startup must be boolean or omitted.")
    states = _states(project, controller(task))
    if any(s["issues"] for s in states):
        raise ValueError(
            "Task XML/OEM assignments are inconsistent or unsupported: "
            + ", ".join(sorted({i for s in states for i in s["issues"]}))
        )
    target = next(s for s in states if s["entity"].get("id") == task.get("id"))
    matches = [
        (s, r)
        for s in states
        for r in s["rows"]
        if r["program"] is not None and r["program"].get("id") == program.get("id")
    ]
    if len(matches) > 1:
        raise ValueError("Multiple program instances require explicit instance support.")
    previous = matches[0][0] if matches else None
    row = matches[0][1] if matches else None
    old_position = previous["rows"].index(row) + 1 if previous is target else None
    if row:
        previous["rows"].remove(row)
    remaining = len(target["rows"])
    position = (
        old_position
        if position is None and old_position
        else remaining + 1
        if position is None
        else position
    )
    if type(position) is not int or not 1 <= position <= remaining + 1:
        raise ValueError("position is one-based and must fit the target task order.")
    updates = {}
    if row is None:
        instance = program.get("name")
        if any(
            r["node"].get("InstanceName", r["node"].get("ProgramName", "")).casefold()
            == instance.casefold()
            for s in states
            for r in s["rows"]
        ):
            raise ValueError("Program instance name is already used.")
        node = ET.Element(
            "AssociatedProgramData",
            ProgramName=program.get("name"),
            InstanceName=instance,
            IniFileTrackingId=program_tracking,
            StartupSetting="TRUE",
            SequenceNumber=str(position),
            IsDebugProgram="false",
        )
        association = ET.Element(
            "Entity",
            type="NexAssociatedProgram",
            subtype=program.get("subtype", ""),
            id=str(uuid4()),
            name=instance,
            DN=instance,
            version="0",
            trackingId=str(uuid4()),
        )
        ET.SubElement(association, "AccessInfos")
        ET.SubElement(association, "ChildEntities")
        model = ET.Element(
            f"{{{ASSOCIATION_NS}}}AssociatedProgramModel", nsmap={None: ASSOCIATION_NS}
        )
        ET.SubElement(model, f"{{{ASSOCIATION_NS}}}PouInstanceName").text = instance
        updates[project.prefix + association.get("id") + ".xml"] = serialize_xml(model)
        row = {
            "node": node,
            "program": program,
            "association": association,
            "sequence": position,
            "issues": [],
        }
    if startup is not None:
        row["node"].set("StartupSetting", "TRUE" if startup else "FALSE")
    target["rows"].insert(position - 1, row)
    child_entities = task.find("ChildEntities")
    if child_entities is None:
        child_entities = ET.SubElement(task, "ChildEntities")
    child_entities.append(row["association"])
    affected = [target] + ([previous] if previous is not None and previous is not target else [])
    for state in affected:
        for node in list(state["container"]):
            state["container"].remove(node)
        for n, entry in enumerate(state["rows"], 1):
            entry["node"].set("SequenceNumber", str(n))
            state["container"].append(entry["node"])
        updates[state["member"]] = serialize_xml(
            state["root"], project.archive.read(state["member"])
        )
    updates[project.oem_name] = serialize_xml(project.oem, project.archive.read(project.oem_name))
    candidate = NativeProject(project.archive.path, {**project.archive.overrides, **updates})
    if any(state["issues"] for state in _states(candidate, controller(program))):
        raise ValueError("Assignment did not produce coherent task XML/OEM links.")
    return updates, {
        "operation": "assign_program",
        "program_id": program.get("id"),
        "task_id": task.get("id"),
        "previous_task_id": previous["entity"].get("id") if previous is not None else None,
        "association_id": row["association"].get("id"),
        "position": position,
        "startup": row["node"].get("StartupSetting"),
        "sysmac_build": "not_executed",
    }


def prepare_unassignment(project, task_id, program_id):
    task = project.resolve(task_id, {"NexTask"})
    program = project.resolve(program_id, {"Program"})
    if controller(task) != controller(program):
        raise ValueError("Task and program must belong to the same controller.")

    states = _states(project, controller(task))
    if any(state["issues"] for state in states):
        raise ValueError(
            "Task XML/OEM assignments are inconsistent or unsupported: "
            + ", ".join(sorted({issue for state in states for issue in state["issues"]}))
        )
    target = next(state for state in states if state["entity"].get("id") == task.get("id"))
    matches = [
        (state, row)
        for state in states
        for row in state["rows"]
        if row["program"] is not None and row["program"].get("id") == program.get("id")
    ]
    if len(matches) != 1:
        raise ValueError("Program assignment is missing or ambiguous in this controller.")
    state, row = matches[0]
    if state is not target:
        raise ValueError("Program is not assigned to the selected task.")

    association = row["association"]
    children = task.find("ChildEntities")
    if children is None or association.getparent() is not children:
        raise ValueError("Task association changed; read the task list again.")
    state["container"].remove(row["node"])
    children.remove(association)
    state["rows"].remove(row)
    for sequence, entry in enumerate(state["rows"], 1):
        entry["node"].set("SequenceNumber", str(sequence))

    updates = {
        state["member"]: serialize_xml(state["root"], project.archive.read(state["member"])),
        project.oem_name: serialize_xml(project.oem, project.archive.read(project.oem_name)),
    }
    candidate = NativeProject(project.archive.path, {**project.archive.overrides, **updates})
    if any(candidate_state["issues"] for candidate_state in _states(candidate, controller(task))):
        raise ValueError("Unassignment did not produce coherent task XML/OEM links.")

    model_member = project.member(association.get("id"))
    return updates, {
        "operation": "unassign_program",
        "program_id": program.get("id"),
        "program_name": program.get("name"),
        "task_id": task.get("id"),
        "association_id": association.get("id"),
        "preserved_model_member": model_member,
        "sysmac_build": "not_executed",
    }
