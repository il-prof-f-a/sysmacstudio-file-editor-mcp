"""Preview or explicitly toggle the installed Sysmac WCF flag, with exact backup."""
import argparse
import ctypes
import hashlib
import json
import os
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

DISABLED = b'key="WcfServiceEnable" value="False"'
ENABLED = b'key="WcfServiceEnable" value="True"'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def enabled_bytes(data):
    data.decode("utf-8-sig", errors="strict")
    settings = [x for x in ET.fromstring(data).findall("./appSettings/add") if x.get("key") == "WcfServiceEnable"]
    if len(settings) != 1:
        raise ValueError("Requires exactly one WcfServiceEnable setting.")
    if settings[0].get("value") != "False" or data.count(DISABLED) != 1:
        raise ValueError("Requires the observed UTF-8 setting with value False and exact attribute layout.")
    candidate = data.replace(DISABLED, ENABLED, 1)
    effective = [x for x in ET.fromstring(candidate).findall("./appSettings/add") if x.get("key") == "WcfServiceEnable"]
    if len(effective) != 1 or effective[0].get("value") != "True":
        raise ValueError("Replacement does not enable the effective XML setting.")
    return candidate


def replace_config(path, data):
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".sysmac-mcp-", dir=path.parent)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    if os.name == "nt":
        # Flags=0: fail if Windows cannot preserve the original ACL/attributes.
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        replace = kernel.ReplaceFileW
        replace.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p]
        replace.restype = ctypes.c_int
        native_backup = temporary + ".original"
        if not replace(str(path), temporary, native_backup, 0, None, None):
            # Preserve the staged file and backup for diagnosis of partial OS errors.
            raise OSError(ctypes.get_last_error(), "ReplaceFileW failed; preserve staged file and native backup: " + temporary + ", " + native_backup)
        return native_backup
    else:
        os.replace(temporary, path)


def configure(config, action, expected=None):
    config = Path(config).absolute()
    backup = config.with_name(config.name + ".sysmac-mcp-wcf.bak")
    if config.is_symlink() or backup.is_symlink():
        raise ValueError("Symlink configuration/backup is not accepted.")
    if action not in {"preview", "enable", "restore"}:
        raise ValueError("Unknown action.")
    if action == "restore" and not config.exists():
        original = backup.read_bytes()
        enabled_bytes(original)
        if not expected or expected.lower() != sha(original):
            raise ValueError("Missing configuration recovery requires the reviewed original backup SHA-256.")
        candidates = [p for p in config.parent.glob(config.name + ".sysmac-mcp-*.original")
                      if p.is_file() and not p.is_symlink() and p.read_bytes() == original]
        if len(candidates) != 1:
            raise ValueError("Recovery requires exactly one verified native original backup; preserve all files for manual recovery.")
        native = candidates[0]
        if os.name == "nt":
            # Windows rename refuses a destination that appeared concurrently.
            native.rename(config)
        else:
            config.hardlink_to(native)
            native.unlink()
        if config.read_bytes() != original:
            raise OSError("Recovery hash verification failed; preserve backup.")
        return {"action": action, "configuration": str(config), "backup": str(backup),
                "resulting_sha256": sha(original), "modified": True, "recovered_missing_configuration": True}
    before = config.read_bytes()
    if action != "preview" and (not expected or sha(before).lower() != expected.lower()):
        raise ValueError("Current SHA-256 differs from the reviewed value, or expected hash is missing.")
    if action == "restore":
        original = backup.read_bytes()
        if enabled_bytes(original) != before:
            raise ValueError("Refusing restore: configuration includes unrelated edits.")
        after = original
    else:
        after = enabled_bytes(before)
    plan = {"action": action, "configuration": str(config), "backup": str(backup),
            "current_sha256": sha(before), "planned_sha256": sha(after), "modified": False}
    if action == "preview":
        return plan
    if action == "enable":
        try:
            with backup.open("xb") as stream:
                stream.write(before)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            if backup.read_bytes() != before:
                raise ValueError("Existing backup differs; it will not be overwritten.") from None
    if config.read_bytes() != before:
        raise ValueError("Configuration changed during preparation; refusing replacement.")
    native_backup = replace_config(config, after)
    if config.read_bytes() != after:
        raise OSError("Post-write verification failed; preserve backup for recovery.")
    plan.update(modified=True, resulting_sha256=sha(after))
    if native_backup:
        plan["native_backup"] = native_backup
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["preview", "enable", "restore"], default="preview", nargs="?")
    parser.add_argument("--config", type=Path, default=Path(r"C:\Program Files\OMRON\Sysmac Studio\SysmacStudio.exe.config"))
    parser.add_argument("--expected-sha256")
    args = parser.parse_args()
    print(json.dumps(configure(args.config, args.action, args.expected_sha256), indent=2))


if __name__ == "__main__":
    main()
