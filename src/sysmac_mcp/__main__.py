"""Command line entry point for the local stdio server and format reports."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(
        description="Offline Sysmac project MCP. Native exports need IDE verification."
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.home() / ".sysmac-mcp",
        help="Local state, immutable snapshots and revisions directory",
    )
    commands = parser.add_subparsers(dest="command")
    inspect = commands.add_parser("inspect", help="Read-only format report")
    inspect.add_argument("path", type=Path)
    compare = commands.add_parser("compare", help="Read-only member comparison")
    compare.add_argument("before", type=Path)
    compare.add_argument("after", type=Path)
    models = commands.add_parser(
        "compare-projects", help="Read-only observed models and member comparison"
    )
    models.add_argument("before", type=Path)
    models.add_argument("after", type=Path)
    models.add_argument("--offset", type=int, default=0)
    models.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    if args.command:
        from .archive import compare_files, inspect_file

        if args.command == "compare-projects":
            from .project_compare import compare_projects

            result = compare_projects(args.before, args.after, offset=args.offset, limit=args.limit)
        else:
            result = (
                inspect_file(args.path)
                if args.command == "inspect"
                else compare_files(args.before, args.after)
            )
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        os.environ.setdefault("OTEL_SDK_DISABLED", "true")
        from .server import create_server

        create_server(args.workspace).run(transport="stdio")


if __name__ == "__main__":
    main()
