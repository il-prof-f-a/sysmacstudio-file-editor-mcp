# Contributing

Bug reports, documentation improvements, and carefully scoped code contributions are welcome. For questions and design discussion, use [GitHub Discussions](https://github.com/il-prof-f-a/sysmacstudio-file-editor-mcp/discussions); for reproducible defects and feature requests, use [GitHub Issues](https://github.com/il-prof-f-a/sysmacstudio-file-editor-mcp/issues).

## Before opening a pull request

1. Open an issue first for broad changes or new behavior. Small documentation fixes can go directly to a pull request.
2. Keep changes focused and explain the user-visible result and known limits.
3. Add or update tests for code changes. Do not use customer projects as fixtures; prefer synthetic, minimal examples.
4. Run the checks from the [README](README.md):

   ```powershell
   .\.venv\Scripts\python.exe -m pytest -q
   .\.venv\Scripts\python.exe -m ruff check src tests
   .\.venv\Scripts\python.exe -m build --no-isolation
   ```

5. Review the diff and confirm it contains no real `.smc`/`.smc2` projects, extracted PLC code, customer or workplace identifiers, local paths, receipts, runtime profiles, credentials, or unsanitized IDE logs.

## License for contributions

There is no Contributor License Agreement and no copyright assignment. Contributors retain copyright in their contributions. By submitting a contribution to this repository, you agree to license that contribution under the [PolyForm Noncommercial License 1.0.0](LICENSE), and you confirm that you have the right to submit it on those terms. Third-party material must retain its own notices and license; do not copy code or assets unless their terms permit inclusion.

## Pull request checklist

- Describe the change and why it is useful.
- Link the relevant issue, if any.
- List tests or manual checks performed and their results.
- Mention any feature that remains experimental or unverified in Sysmac Studio.
- Confirm that the contribution contains only synthetic or authorized examples.
