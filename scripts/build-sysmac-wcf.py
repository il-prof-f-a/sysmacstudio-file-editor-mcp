"""Compile the experimental WCF client against the locally installed vendor contract."""
import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vendor-root", type=Path, default=Path(r"C:\Program Files\OMRON\Sysmac Studio"))
    parser.add_argument("--output", type=Path, default=root / "bridge/SysmacWcf/bin")
    args = parser.parse_args()
    compiler = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    contract = args.vendor_root.resolve() / "FrameworkModules.Core.dll"
    framework = args.vendor_root.resolve() / "Omron.Cxap.Framework.Core.dll"
    if not compiler.is_file() or not contract.is_file() or not framework.is_file():
        parser.error("Requires Windows .NET Framework compiler and installed Sysmac contract assembly.")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    destination = output / "SysmacWcf.exe"
    subprocess.run([str(compiler), "/nologo", "/target:exe", "/out:" + str(destination), "/reference:System.ServiceModel.dll", "/reference:System.Runtime.Serialization.dll", "/reference:System.Web.Extensions.dll", "/reference:" + str(contract), "/reference:" + str(framework), str(root / "bridge/SysmacWcf/Program.cs"), str(root / "bridge/SysmacWcf/ElevatedTransport.cs"), str(root / "bridge/SysmacWcf/RuntimeRunner.cs")], check=True)
    profile = {"executable": str(destination), "vendor_root": str(args.vendor_root.resolve()), "contract_sha256": hashlib.sha256(contract.read_bytes()).hexdigest(), "vendor_binaries_copied": False}
    (output / "build-profile.json").write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(profile))


if __name__ == "__main__":
    main()
