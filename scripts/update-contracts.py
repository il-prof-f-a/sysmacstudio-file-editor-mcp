"""Regenerate MCP discovery and IEC model contracts from the installed source."""

import asyncio
import json
import tempfile
from pathlib import Path

from mcp import Client

from sysmac_mcp.iec_xml import LadderNetwork, PouSpec, VariableSpec
from sysmac_mcp.native_pou import NativePouDefinition
from sysmac_mcp.server import create_server


async def main():
    destination = Path(__file__).resolve().parents[1] / "contracts"
    destination.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as workspace:
        async with Client(create_server(workspace)) as client:
            listing = await client.list_tools()
            data = {
                "server": "sysmacstudio-file-editor",
                "transport": "stdio",
                "tools": [
                    tool.model_dump(by_alias=True, exclude_none=True) for tool in listing.tools
                ],
            }
    outputs = {
        "mcp-tools.json": data,
        "pou.schema.json": PouSpec.model_json_schema(),
        "variable.schema.json": VariableSpec.model_json_schema(),
        "ld-network.schema.json": LadderNetwork.model_json_schema(),
        "native-pou.schema.json": NativePouDefinition.model_json_schema(),
    }
    for filename, value in outputs.items():
        (destination / filename).write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(f"Wrote {len(listing.tools)} MCP tools and four IEC/native model schemas.")


if __name__ == "__main__":
    asyncio.run(main())
