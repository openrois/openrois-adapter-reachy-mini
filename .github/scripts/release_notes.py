#!/usr/bin/env python3
"""Print the notes of a GitHub release: the CHANGELOG section of one version.

The version heading is left out, because the release title already says it, and so are
the link references that close the file. A missing or empty section exits non-zero, so
that a release cannot go out without its notes.

Usage:
    python3 .github/scripts/release_notes.py CHANGELOG.md 0.1.0a1
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

LINK_REFERENCE = re.compile(r"^\[[^\]]+\]:\s")


def section(path: Path, version: str) -> str:
    """Return the body of the section of ``version``, without its heading."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        sys.exit(f"::error::{path}: {error.strerror}")

    heading = re.compile(rf"^## (?:\[{re.escape(version)}\]|{re.escape(version)})(?:\s|$)")
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if heading.match(line)), None)
    if start is None:
        sys.exit(f"::error::{path} has no section for {version}")
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    body = lines[start + 1 : end]

    # The link references close the file, so they follow the last section with no heading
    # of their own to stop at.
    while body and (not body[-1].strip() or LINK_REFERENCE.match(body[-1])):
        body.pop()
    while body and not body[0].strip():
        body.pop(0)

    # A section with only category headings left has nothing for a reader either.
    if not any(line.strip() and not line.startswith("#") for line in body):
        sys.exit(f"::error::{path} has an empty section for {version}")
    return "\n".join(body)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("changelog", type=Path, help="the CHANGELOG")
    parser.add_argument("version", help="the version as the CHANGELOG spells it")
    args = parser.parse_args()
    print(section(args.changelog, args.version))


if __name__ == "__main__":
    main()
