"""Read installed Omron DDF/ESI metadata; never execute profile content."""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

from lxml import etree as ET

from .project import parse_xml


def integer(value):
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    try:
        return (
            int(text[2:], 16)
            if text.lower().startswith("#x")
            else int(text, 0)
            if text.lower().startswith("0x")
            else int(text)
        )
    except ValueError:
        return None


def english_name(element):
    names = element.findall("Name")
    return next(
        (n.text for n in names if n.get("LcId") == "1033"),
        next((n.text for n in names if n.get("LcId") is None), None),
    )


def default_profile_root():
    configured = os.environ.get("SYSMAC_PROFILE_ROOT")
    if configured:
        return Path(configured).resolve()
    return next(
        (
            p
            for p in [
                Path(r"C:\Program Files\OMRON\Sysmac Studio\IODeviceProfiles"),
                Path(r"C:\Program Files (x86)\OMRON\Sysmac Studio\IODeviceProfiles"),
            ]
            if p.is_dir()
        ),
        None,
    )


@lru_cache(maxsize=256)
def load_xml(path: str, modified: int, size: int):
    if size > 16 * 1024 * 1024:
        raise ValueError("Profile size limit exceeded.")
    return parse_xml(Path(path).read_bytes())


def profile_xml(path: Path):
    stat = path.stat()
    return load_xml(str(path.resolve()), stat.st_mtime_ns, stat.st_size)


class DeviceProfile:
    def __init__(self, path, device):
        self.path, self.device = path, device
        self.model = device.findtext("Type")

    def entry(self, index, subindex, pdo_index=None):
        matches = []
        for tag, direction in [("RxPdo", "output"), ("TxPdo", "input")]:
            for pdo in self.device.findall(tag):
                if pdo_index is not None and integer(pdo.findtext("Index")) != pdo_index:
                    continue
                for item in pdo.findall("Entry"):
                    if (
                        integer(item.findtext("Index")) == index
                        and integer(item.findtext("SubIndex")) == subindex
                    ):
                        matches.append(
                            {
                                "name": english_name(item),
                                "data_type": item.findtext("DataType"),
                                "bit_length": integer(item.findtext("BitLen")),
                                "direction": direction,
                            }
                        )
        unique = {tuple(sorted(m.items())) for m in matches}
        if len(unique) != 1:
            return None
        item = matches[0]
        return item if item["name"] and item["data_type"] else None

    def divisions(self, index, subindex):
        matches = [
            e
            for e in self.device.findall(".//PdoDivisions")
            if integer(e.get("Index")) == index and integer(e.get("SubIndex")) == subindex
        ]
        if len(matches) != 1:
            return []
        divisions = []
        bit_offset = 0
        for child in matches[0]:
            if child.tag == "Padding":
                padding = integer(child.get("Size"))
                bit_offset = (
                    bit_offset + padding
                    if bit_offset is not None and padding is not None and padding >= 0
                    else None
                )
                continue
            if child.tag != "Division":
                continue
            data_type = child.get("Type")
            divisions.append(
                {
                    "name": english_name(child),
                    "data_type": data_type,
                    "bit_offset": bit_offset if data_type == "BOOL" else None,
                }
            )
            # Sysmac's ESI reader accepts BOOL divisions; unsupported types make
            # subsequent offsets indeterminate until a new profile is selected.
            bit_offset = bit_offset + 1 if bit_offset is not None and data_type == "BOOL" else None
        return divisions


class ProfileCatalog:
    def __init__(self, root=None):
        self.root = Path(root).resolve() if root is not None else default_profile_root()
        self.cache = {}

    def find_device(self, vendor, product, revision, model_hint=None):
        key = (vendor, product, revision, model_hint)
        if key in self.cache:
            return self.cache[key]
        if self.root is None or None in (vendor, product, revision):
            return None
        candidates = []
        if model_hint and re.fullmatch(r"[A-Za-z0-9_. -]{1,128}", model_hint):
            direct = self.root / "DdfFiles" / (model_hint + ".xml")
            if direct.is_file():
                candidates.append(direct)
        if not candidates:
            candidates = sorted((self.root / "EsiFiles").rglob("*.xml"))
        matches = []
        for path in candidates:
            try:
                root = profile_xml(path)
            except (OSError, ValueError, ET.XMLSyntaxError):
                continue
            if integer(root.findtext("Vendor/Id")) != vendor:
                continue
            for device in root.findall("./Descriptions/Devices/Device"):
                typ = device.find("Type")
                if typ is None:
                    continue
                if (
                    integer(typ.get("ProductCode")) == product
                    and integer(typ.get("RevisionNo")) == revision
                ):
                    if model_hint is None or typ.text == model_hint:
                        matches.append(DeviceProfile(path, device))
        # Identical profiles may be listed in multiple installed ESI revisions. Ambiguity is explicit.
        self.cache[key] = matches[0] if len(matches) == 1 else None
        return self.cache[key]
