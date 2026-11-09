import hashlib
import runpy
from pathlib import Path

import pytest

CONFIG_TOOL = Path(__file__).resolve().parents[1] / "scripts/sysmac-wcf-config.py"
ORIGINAL = b'\xef\xbb\xbf<configuration>\r\n<appSettings><add key="WcfServiceEnable" value="False" /></appSettings>\r\n</configuration>'


def digest(value):
    return hashlib.sha256(value).hexdigest()


def change(path, action, expected=None):
    return runpy.run_path(str(CONFIG_TOOL))["configure"](path, action, expected)


def test_preview_writes_nothing_and_changes_only_flag(tmp_path):
    path = tmp_path / "SysmacStudio.exe.config"
    path.write_bytes(ORIGINAL)
    plan = change(path, "preview")
    assert plan["current_sha256"] == digest(ORIGINAL)
    assert plan["planned_sha256"] == digest(ORIGINAL.replace(b'"False"', b'"True"'))
    assert list(tmp_path.iterdir()) == [path]
    assert path.read_bytes() == ORIGINAL


def test_enable_restore_keeps_exact_original_and_exclusive_backup(tmp_path):
    path = tmp_path / "SysmacStudio.exe.config"
    path.write_bytes(ORIGINAL)
    enabled = change(path, "enable", digest(ORIGINAL))
    backup = Path(enabled["backup"])
    assert backup.read_bytes() == ORIGINAL
    assert path.read_bytes() == ORIGINAL.replace(b'"False"', b'"True"')
    if "native_backup" in enabled:
        assert Path(enabled["native_backup"]).read_bytes() == ORIGINAL
    change(path, "restore", digest(path.read_bytes()))
    assert path.read_bytes() == backup.read_bytes() == ORIGINAL


def test_stale_hash_is_rejected_without_mutations(tmp_path):
    path = tmp_path / "SysmacStudio.exe.config"
    path.write_bytes(ORIGINAL)
    with pytest.raises(ValueError, match="SHA-256"):
        change(path, "enable", "0" * 64)
    assert path.read_bytes() == ORIGINAL and len(list(tmp_path.iterdir())) == 1


def test_restore_refuses_unrelated_edits_and_retains_backup(tmp_path):
    path = tmp_path / "SysmacStudio.exe.config"
    path.write_bytes(ORIGINAL)
    plan = change(path, "enable", digest(ORIGINAL))
    path.write_bytes(path.read_bytes() + b"\r\n<!-- unrelated -->")
    current = path.read_bytes()
    with pytest.raises(ValueError, match="unrelated"):
        change(path, "restore", digest(current))
    assert path.read_bytes() == current
    assert Path(plan["backup"]).read_bytes() == ORIGINAL


def test_duplicate_setting_is_rejected(tmp_path):
    path = tmp_path / "SysmacStudio.exe.config"
    path.write_bytes(ORIGINAL.replace(b"</appSettings>", b'<add key="WcfServiceEnable" value="False" /></appSettings>'))
    with pytest.raises(ValueError, match="exactly one"):
        change(path, "preview")


def test_literal_in_comment_cannot_enable_the_effective_setting(tmp_path):
    path = tmp_path / "SysmacStudio.exe.config"
    content = b'<configuration><!-- key="WcfServiceEnable" value="False" --><appSettings><add value="False" key="WcfServiceEnable" /></appSettings></configuration>'
    path.write_bytes(content)
    with pytest.raises(ValueError):
        change(path, "enable", digest(content))
    assert path.read_bytes() == content and len(list(tmp_path.iterdir())) == 1


def test_partial_windows_replace_has_verified_recovery(tmp_path, monkeypatch):
    path = tmp_path / "SysmacStudio.exe.config"
    path.write_bytes(ORIGINAL)
    api = runpy.run_path(str(CONFIG_TOOL))

    def partial_replace(target, data):
        target.rename(target.with_name(target.name + ".sysmac-mcp-fault.original"))
        raise OSError(1177, "Synthetic partial Windows replacement")

    monkeypatch.setitem(api["configure"].__globals__, "replace_config", partial_replace)
    with pytest.raises(OSError):
        api["configure"](path, "enable", digest(ORIGINAL))
    assert not path.exists()
    # Recovery checks the hash of the original backup; it creates no replacement
    # if another process has already restored the destination.
    recovered = change(path, "restore", digest(ORIGINAL))
    assert recovered["recovered_missing_configuration"]
    assert path.read_bytes() == ORIGINAL
