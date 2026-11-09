# Sysmac Studio File Editor MCP

[![CI](https://github.com/il-prof-f-a/sysmacstudio-file-editor-mcp/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/il-prof-f-a/sysmacstudio-file-editor-mcp/actions/workflows/ci.yml)
[![License: PolyForm Noncommercial 1.0.0](https://img.shields.io/badge/License-PolyForm--Noncommercial--1.0.0-blue)](LICENSE)

An offline-first [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server for inspecting, comparing, and transactionally editing **local copies** of Omron Sysmac Studio projects.

The project is experimental and source-available under the PolyForm Noncommercial License 1.0.0. It is not an Omron product and is not affiliated with or endorsed by Omron.

> **Safety and privacy:** The server does not connect to a PLC, download a program, or force I/O. Work on an authorized copy, review every proposed change, and rebuild the exported candidate in Sysmac Studio before relying on it. A successful build does not verify machine behavior or wiring. Project names, source, and tool results may be sent to the model provider by your MCP client; use only data you are authorized to share and follow your organization's data-handling rules.

## What it does

- Inspects `.smc2` archives and reports observed project members.
- Reads and compares project copies, with structural comparison of observed variables, POU, tasks, DataTypes, hardware, and I/O.
- Stages supported offline edits to ST/LD sections, variables, POU, task assignments, I/O bindings, and selected DataTypes.
- Uses transactions with expected revisions, previews, partial validation, commit to a local working copy, rollback, and export to a new path.
- Generates and imports a bounded subset of PLCopen IEC XML for ST, Ladder, globals, and selected DataTypes.
- Optionally communicates with a locally installed Sysmac Studio through typed Windows-only operations, including open-copy, runtime status, rebuild, and guarded export.

This is not a full IEC 61131-3 parser, compiler, or semantic checker. Unsupported or incomplete structures are reported as limitations. See [capabilities and limits](docs/capabilities.md) and the [validation matrix](docs/validation.md).

## Requirements

### Offline MCP core

- Python 3.11 or newer.
- An MCP client that supports the local `stdio` transport.
- No Sysmac Studio installation, cloud account, API key, or listening network port is required for the core archive and transaction workflows.

Windows is the primary development and validation platform. The Python core may run in other environments with compatible Python dependencies, but Linux, macOS, and WSL are not currently part of the tested support matrix.

### Optional Sysmac Studio integration

- Windows x64 and Sysmac Studio installed locally.
- The vendor contract assemblies and .NET Framework compiler supplied by the local installation, to build the WCF client.
- A local Node.js executable and the verified Frida 17.23.2 package, to create the hash-pinned runtime profile used by runtime operations.
- User approval of the normal Windows UAC prompt for operations that require elevation.

The bridge uses local vendor interfaces that can vary by Sysmac Studio release. The repository does not include Omron binaries. The IDE integration is experimental and must be validated against the exact IDE version and an authorized demonstration project. See [Sysmac Studio integration](docs/sysmac-wcf.md) and [runtime build and export](docs/runtime-build.md).

### Optional EtherCAT NRBF helper

Building the Windows x64 helper requires the .NET SDK version pinned in `bridge/SysmacFormats/global.json` (currently 10.0.403) and NuGet package restore. It is not needed for the basic MCP server. See [hardware and formats](docs/hardware-and-formats.md).

## Install on Windows

Clone the repository and create a virtual environment from the project root:

```powershell
git clone https://github.com/il-prof-f-a/sysmacstudio-file-editor-mcp.git
cd sysmacstudio-file-editor-mcp
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .
```

For development and tests, install the optional development dependencies:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

The server communicates with its MCP client over standard input/output. It does not open a listening TCP port.

## Configure an MCP client

Add an entry like this to the MCP client's configuration, replacing both paths with absolute paths on your machine:

```json
{
  "mcpServers": {
    "sysmacstudio": {
      "command": "C:/path/to/sysmacstudio-file-editor-mcp/.venv/Scripts/python.exe",
      "args": [
        "-m",
        "sysmac_mcp",
        "--workspace",
        "C:/path/to/sysmacstudio-file-editor-mcp/workspace"
      ]
    }
  }
}
```

See [mcp-config.example.json](mcp-config.example.json) for the optional IDE environment variables. The supplied sample contains placeholders and must be adapted locally. Do not commit a local configuration, runtime profile, project archive, or receipt.

You can also start the server manually:

```powershell
.\.venv\Scripts\python.exe -m sysmac_mcp --workspace .\workspace
```

The read-only command-line reports are:

```powershell
.\.venv\Scripts\python.exe -m sysmac_mcp inspect 'C:\path\to\project.smc2'
.\.venv\Scripts\python.exe -m sysmac_mcp compare 'C:\path\to\before.smc2' 'C:\path\to\after.smc2'
.\.venv\Scripts\python.exe -m sysmac_mcp compare-projects 'C:\path\to\before.smc2' 'C:\path\to\after.smc2'
```

## Recommended editing workflow

1. Read `plc_get_capabilities`, then open an authorized project copy with `plc_open_project`.
2. Inspect the relevant variables, POU, sections, tasks, hardware, and references.
3. Start a transaction with `plc_begin_transaction`, passing the revision returned by the project read.
4. Preview with `plc_preview_changes` and run `plc_validate`. Review every changed archive member.
5. Commit to the MCP-managed local copy with `plc_commit_changes`, or discard the staged changes with `plc_rollback`.
6. Export to a new path with `plc_export_project`. Treat the output as a candidate until Sysmac Studio imports and rebuilds it.

The local IDE tools are separate from archive editing. They use explicit, typed operations and do not expose arbitrary WCF method calls. `plc_ide_rebuild_active` rebuilds the active offline solution; it does not download to a controller. `plc_ide_export_active` uses the native IDE export path with identity, destination, save, and session checks. Both require the optional Windows integration and its UAC flow.

## Data handling

- Project snapshots, transactions, IDE jobs, receipts, and runtime profiles belong in local ignored folders such as `workspace/`.
- Do not add `.smc`/`.smc2` archives, extracted project data, IDE logs, runtime profiles, receipts, or customer identifiers to the repository or an issue.
- `.gitignore` helps prevent accidental additions, but does not remove files already tracked or erase Git history. Review the staged file list before committing.
- The server uses local `stdio`; your MCP client and model provider may process tool output separately. Choose a client and model approved for the project data you use.

## Contributing and support

Bug reports and feature suggestions are welcome through [GitHub Issues](https://github.com/il-prof-f-a/sysmacstudio-file-editor-mcp/issues). Questions and design discussion can go to [GitHub Discussions](https://github.com/il-prof-f-a/sysmacstudio-file-editor-mcp/discussions). Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request. Never attach a real customer project or unsanitized diagnostic output.

For security concerns, follow [SECURITY.md](SECURITY.md) and do not publish exploit details in an issue.

## Development checks

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check src tests
.\.venv\Scripts\python.exe -m build
```

## License

Original project material is licensed under the [PolyForm Noncommercial License 1.0.0](LICENSE). Its terms allow only the purposes defined by that license; a separate permission from the relevant rights holder is required for uses outside those terms. This is a source-available, noncommercial project, not OSI-approved open source: the Open Source Definition does not permit licenses to restrict fields of endeavor such as business use ([OSI definition](https://opensource.org/osd)).

Runtime dependencies are listed in [`pyproject.toml`](pyproject.toml) and remain under their respective upstream licenses. The license summary above is informational; the complete terms in `LICENSE` govern.
