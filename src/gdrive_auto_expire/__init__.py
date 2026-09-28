"""Share files on Google Drive with a public link that revokes itself."""

from __future__ import annotations

import sys

__version__ = "0.1.0"


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point, re-exported here so the existing
    `[project.scripts]` target in pyproject.toml keeps working."""
    from .cli import main as cli_main

    return cli_main(argv)


def run() -> None:
    sys.exit(main())
