"""Build the local managed status/build adapter without redistributing vendor assemblies."""
import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    sources = sorted((root / "bridge/SysmacRuntime").glob("*.cs"))
    fingerprint = hashlib.sha256(Path(__file__).read_bytes() + b"\0" + b"".join(path.read_bytes() for path in sources)).hexdigest()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=root / "bridge/SysmacRuntime/bin" / fingerprint[:16])
    args = parser.parse_args()
    compiler = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    if not compiler.is_file():
        parser.error("Windows .NET Framework compiler is required.")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    destination = output / ("SysmacRuntime_" + fingerprint[:16] + ".dll")
    profile_path = output / "build-profile.json"
    version = ".".join(str(int(fingerprint[i:i + 4], 16) % 65535) for i in range(0, 16, 4))
    if profile_path.is_file():
        existing = json.loads(profile_path.read_text(encoding="utf-8"))
        if (existing.get("source_sha256") == fingerprint and destination.is_file()
                and existing.get("assembly_sha256") == hashlib.sha256(destination.read_bytes()).hexdigest()
                and existing.get("assembly_version") == version and existing.get("architecture") == "x64"
                and existing.get("vendor_binaries_copied") is False):
            existing["assembly"] = str(destination)
            profile_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(existing))
            return
    if destination.exists():
        parser.error("An unverified runtime assembly already exists; choose a fresh output directory.")
    stamp = output / "AssemblyVersion.cs"
    stamp.write_text('using System.Reflection;\n[assembly:AssemblyVersion("' + version + '")]\n', encoding="utf-8")
    command = [str(compiler), "/nologo", "/target:library", "/platform:x64", "/out:" + str(destination),
               "/reference:System.Web.Extensions.dll", "/reference:System.Xaml.dll",
               "/reference:" + str(compiler.parent / "WPF/WindowsBase.dll"),
               "/reference:" + str(compiler.parent / "WPF/PresentationFramework.dll"),
               *map(str, sources), str(stamp)]
    subprocess.run(command, check=True)
    profile = {"assembly": str(destination), "assembly_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
               "source_sha256": fingerprint, "assembly_version": version,
               "vendor_binaries_copied": False, "architecture": "x64"}
    profile_path.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(profile))


if __name__ == "__main__":
    main()
