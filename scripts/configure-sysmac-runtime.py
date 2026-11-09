"""Create a hash-pinned local runtime profile from already installed Node/Frida."""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


def component(path):
    path = Path(path).resolve(strict=True)
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--frida-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=root / "bridge/SysmacWcf/bin/runtime-profile.json")
    args = parser.parse_args()
    package = json.loads((args.frida_root / "package.json").read_text(encoding="utf-8"))
    if package.get("name") != "frida" or package.get("version") != "17.23.2":
        parser.error("This adapter requires the verified Frida 17.23.2 package.")
    built = subprocess.run([sys.executable, str(root / "scripts/build-sysmac-runtime.py")],
                           capture_output=True, text=True, check=True)
    managed = json.loads(built.stdout.splitlines()[-1])
    profile = {
        "schema_version": 1,
        "node": component(args.node),
        "worker": component(root / "bridge/SysmacRuntime/host.mjs"),
        "template": component(root / "bridge/SysmacRuntime/clr-entry.js"),
        "frida_module": component(args.frida_root / "build/src/frida.js"),
        "assembly": component(managed["assembly"]),
        "frida_version": package["version"],
        "managed_source_sha256": managed["source_sha256"],
        "vendor_binaries_copied": False,
    }
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        previous = json.loads(output.read_text(encoding="utf-8"))
        if previous != profile:
            parser.error("Profile already exists with different hashes; choose a fresh output path.")
    else:
        with output.open("x", encoding="utf-8") as stream:
            json.dump(profile, stream, indent=2)
            stream.write("\n")
    print(json.dumps({"runtime_profile": str(output), **profile}))


if __name__ == "__main__":
    main()
