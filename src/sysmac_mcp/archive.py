"""Bounded, read-only access to project containers. Never extract members to disk."""

from __future__ import annotations

import copy
import hashlib
import math
import re
from collections import Counter
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile, ZipInfo


class ArchiveError(ValueError):
    """Invalid, unsupported or unsafe project container."""


class ProjectArchive:
    def __init__(
        self,
        path: str | Path,
        *,
        max_total_bytes: int = 512 * 1024 * 1024,
        max_member_bytes: int = 128 * 1024 * 1024,
        max_entries: int = 10000,
    ):
        self.overrides: dict[str, bytes] = {}
        self.limits = (max_total_bytes, max_member_bytes, max_entries)
        self.path = Path(path).resolve(strict=True)
        try:
            with ZipFile(self.path) as archive:
                self.infos = archive.infolist()
                self.comment = archive.comment
        except (BadZipFile, OSError) as exc:
            raise ArchiveError("Not a readable ZIP project; inspect the format first.") from exc
        if len(self.infos) > max_entries:
            raise ArchiveError("Archive entry limit exceeded.")
        total, names = 0, set()
        for item in self.infos:
            name = item.filename
            portable = name.replace("\\", "/")
            parts = PurePosixPath(portable).parts
            if (
                not name
                or "\\" in name
                or portable.startswith("/")
                or re.match(r"^[A-Za-z]:", portable)
                or ".." in parts
                or "\x00" in name
            ):
                raise ArchiveError(f"Unsafe member path: {name!r}")
            if portable.casefold() in names:
                raise ArchiveError(f"Duplicate member path: {name!r}")
            names.add(portable.casefold())
            if item.flag_bits & 1:
                raise ArchiveError("Encrypted members are not supported.")
            if (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise ArchiveError("Archive symlinks are not supported.")
            total += item.file_size
            if item.file_size > max_member_bytes or total > max_total_bytes:
                raise ArchiveError("Uncompressed archive size limit exceeded.")
        self.names = [item.filename for item in self.infos]
        self.members = {item.filename: item for item in self.infos}
        self._base_infos = list(self.infos)

    def apply_overrides(self, overrides: dict[str, bytes]):
        """Publish a validated virtual view, including additions, without extraction."""
        infos = {item.filename: copy.copy(item) for item in self._base_infos}
        folded = {name.casefold(): name for name in infos}
        for name, data in overrides.items():
            if (
                not isinstance(name, str)
                or not name
                or "\\" in name
                or name.startswith("/")
                or name.endswith("/")
                or re.match(r"^[A-Za-z]:", name)
                or ".." in PurePosixPath(name).parts
                or "\x00" in name
            ):
                raise ArchiveError("Unsafe virtual member path.")
            if not isinstance(data, bytes):
                raise ArchiveError("Virtual members must contain bytes.")
            existing = folded.get(name.casefold())
            if existing is not None and existing != name:
                raise ArchiveError("Duplicate virtual member path.")
            if name in infos and infos[name].is_dir():
                raise ArchiveError("Cannot replace an archive directory with a file.")
            item = infos.get(name) or ZipInfo(name)
            item.file_size = len(data)
            infos[name] = item
            folded[name.casefold()] = name
        total, per_member, count = self.limits
        if (
            len(infos) > count
            or any(i.file_size > per_member for i in infos.values())
            or sum(i.file_size for i in infos.values()) > total
        ):
            raise ArchiveError("Virtual archive size or entry limit exceeded.")
        self.overrides = dict(overrides)
        self.infos = list(infos.values())
        self.members = infos
        self.names = list(infos)

    def read(self, name: str) -> bytes:
        if name in self.overrides:
            return self.overrides[name]
        try:
            with ZipFile(self.path) as archive:
                return archive.read(self.members[name])
        except (BadZipFile, RuntimeError) as exc:
            raise ArchiveError(f"Cannot read member {name!r}; CRC or compression failure.") from exc

    def hashes(self) -> dict[str, str]:
        return {
            i.filename: hashlib.sha256(self.read(i.filename)).hexdigest()
            for i in self.infos
            if not i.is_dir()
        }


def inspect_file(path: str | Path) -> dict:
    path = Path(path).resolve(strict=True)
    counts: Counter[int] = Counter()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        head = stream.read(64)
        stream.seek(0)
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            counts.update(block)
    size = sum(counts.values())
    entropy = -sum((c / size) * math.log2(c / size) for c in counts.values()) if size else 0
    result = {
        "path": str(path),
        "size_bytes": size,
        "sha256": digest.hexdigest(),
        "header_hex": head.hex(),
        "entropy_bits_per_byte": round(entropy, 6),
        "container": "unknown",
        "project_semantics_verified": False,
    }
    try:
        archive = ProjectArchive(path)
    except ArchiveError as exc:
        result["container_diagnostic"] = str(exc)
    else:
        hashes = archive.hashes()
        result.update(
            container="zip",
            entry_count=len(archive.infos),
            uncompressed_bytes=sum(i.file_size for i in archive.infos),
            entries=[
                {
                    "name": i.filename,
                    "size_bytes": i.file_size,
                    "compressed_bytes": i.compress_size,
                    "sha256": hashes.get(i.filename),
                }
                for i in archive.infos
            ],
        )
    return result


def compare_files(before: str | Path, after: str | Path) -> dict:
    a, b = inspect_file(before), inspect_file(after)
    result = {
        "before_sha256": a["sha256"],
        "after_sha256": b["sha256"],
        "identical_bytes": a["sha256"] == b["sha256"],
        "semantic_equivalence": "not_verified",
    }
    if a["container"] == b["container"] == "zip":
        ah, bh = ProjectArchive(before).hashes(), ProjectArchive(after).hashes()
        result.update(
            added_entries=sorted(bh.keys() - ah.keys()),
            removed_entries=sorted(ah.keys() - bh.keys()),
            changed_entries=sorted(k for k in ah.keys() & bh.keys() if ah[k] != bh[k]),
        )
    return result
