"""Local radio programmer with a web interface and an MCP server."""

__version__ = "0.1.0"


def main() -> None:
    from radiosmith.cli import main as cli_main

    raise SystemExit(cli_main())
